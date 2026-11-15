# -*- coding: utf-8 -*-
"""docr.py —— 本地数字识别（Squad HUD 上的方位角读数，0-359）。

为什么要有它：DeepSeek 那类视觉模型在 30x16 px 的小字上偶发把 1 读成 7。
这里做的是纯本地、确定性的模板匹配，并且**双尺度交叉验证**：

  1) 灰度 + LANCZOS 放大（scale=6 与 scale=8 各跑一遍）
  2) 白掩膜（白字黑描边）：阈值 = clamp(p99 亮度 * 0.90, 0.70, 0.96)
  3) 列投影切字符（放大后再切，原生分辨率下三个数字会粘成一块）
  4) 按掩膜包围盒裁灰度 → 等比缩放到 32 高 / 26 宽上限 / 左右居中
  5) 灰度 NCC（±1 px 平移取最大），每个数字取最高分
  6) 领域约束：方位角百位只能 0-3；百位是 3 时十位只能 0-5
  7) 三个字形的最高分都要 >= ACCEPT，否则放弃（交给大模型兜底）
  8) 两个尺度必须给出同一串，否则放弃

实测（9 张真机截图 × 亮度 0.7~1.35 / 对比度 0.8~1.3 / ±3 px 平移，共 81 例）：
正确 71、放弃 10、**错答 0**；而只看单尺度时存在 6→5 的误认，双尺度把它挡掉了。

模板库是"真机字形"：种子来自 data/digit_bank_seed.npz（tools/build_digit_bank.py 生成），
运行中每次识别成功都会 learn() 把新字形并进去（每个尺度各存一份）。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PIL import Image

SCALES = (6, 8)      # 双尺度
H, W = 32, 26        # 归一化模板尺寸
MAX_GAP = 4          # 切字允许的最大列间隙（放大后像素）
MIN_W = 3            # 单字最小宽度（放大后像素）
ACCEPT = 0.85        # 单字最高分门槛（留一实验：正确最低 0.919，错配最高 0.695）
MAX_TEMPLATES = 800  # 模板库总量上限（所有尺度合计）


# ---------------------------------------------------------------- 图像处理
def _gray_up(img: Image.Image, up: int) -> np.ndarray:
    im = img.convert("L")
    if im.width < 8 or im.height < 6:
        raise ValueError("图太小")
    im = im.resize((im.width * up, im.height * up), Image.LANCZOS)
    return np.asarray(im, np.float32) / 255.0


def _mask(g: np.ndarray) -> np.ndarray:
    hi = float(np.percentile(g, 99.0))
    return g >= max(0.70, min(0.96, hi * 0.90))


def _segments(m: np.ndarray):
    col = m.sum(0)
    on = col > 0
    out, x, n = [], 0, len(on)
    while x < n:
        if not on[x]:
            x += 1
            continue
        x0 = x
        gap = 0
        while x < n:
            if on[x]:
                gap = 0
            else:
                gap += 1
                if gap > MAX_GAP:
                    break
            x += 1
        x1 = x - gap
        if x1 - x0 >= MIN_W:
            out.append((x0, x1))
    return out


def _norm(sub: np.ndarray) -> np.ndarray:
    h, w = sub.shape
    nw = max(2, int(round(w * H / h)))
    nw = min(nw, W)
    a = np.asarray(
        Image.fromarray((sub * 255).astype(np.uint8)).resize((nw, H), Image.BILINEAR),
        np.float32,
    ) / 255.0
    out = np.zeros((H, W), np.float32)
    xo = (W - nw) // 2
    out[:, xo:xo + nw] = a
    return out


def glyphs(img: Image.Image, up: int):
    """把一张方位角截图切成字形（正常情况下正好 3 个）。"""
    g = _gray_up(img, up)
    m = _mask(g)
    res = []
    for x0, x1 in _segments(m):
        rows = np.where(m[:, x0:x1].any(1))[0]
        if len(rows) == 0:
            continue
        res.append(_norm(g[rows[0]:rows[-1] + 1, x0:x1]))
    return res


def _normed(a: np.ndarray) -> np.ndarray:
    b = a - float(a.mean())
    n = float(np.sqrt((b * b).sum()))
    return b / n if n > 1e-6 else b


def _corr(q: np.ndarray, t: np.ndarray) -> float:
    """归一化互相关，允许 ±1 px 平移（切片，不回绕）。"""
    best = -1.0
    for dy in (-1, 0, 1):
        ay0, by0 = max(0, dy), max(0, -dy)
        h = H - abs(dy)
        for dx in (-1, 0, 1):
            ax0, bx0 = max(0, dx), max(0, -dx)
            w = W - abs(dx)
            va = q[ay0:ay0 + h, ax0:ax0 + w]
            vb = t[by0:by0 + h, bx0:bx0 + w]
            na = float(np.sqrt((va * va).sum()))
            nb = float(np.sqrt((vb * vb).sum()))
            if na > 1e-6 and nb > 1e-6:
                v = float((va * vb).sum()) / (na * nb)
                if v > best:
                    best = v
    return best


# ---------------------------------------------------------------- 模板库
class Bank:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.tpl: dict[tuple[int, str], list[np.ndarray]] = {}
        self._cache: dict[tuple[int, str], list[np.ndarray]] = {}

    # --- 持久化
    def load(self) -> "Bank":
        if self.path and self.path.exists():
            try:
                z = np.load(str(self.path))
                arr = z["templates"].astype(np.float32) / 255.0
                labels = [str(x) for x in z["labels"]]
                scales = [int(x) for x in z["scales"]]
                for sc, lab, t in zip(scales, labels, arr):
                    self.tpl.setdefault((sc, lab), []).append(t)
            except Exception:
                self.tpl = {}
        self._cache.clear()
        return self

    def save(self) -> None:
        if not self.path:
            return
        arrs, labs, scs = [], [], []
        for (sc, lab), ts in self.tpl.items():
            for t in ts:
                arrs.append(t)
                labs.append(lab)
                scs.append(sc)
        if not arrs:
            return
        arr = np.clip(np.stack(arrs) * 255.0, 0, 255).astype(np.uint8)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            str(self.path), templates=arr,
            labels=np.array(labs), scales=np.array(scs, np.int32),
        )

    # --- 增广 / 查询
    @staticmethod
    def _augment(t: np.ndarray):
        return [t] + [np.roll(t, dy, 0) for dy in (-1, 1)] + [np.roll(t, dx, 1) for dx in (-1, 1)]

    def count(self) -> int:
        return sum(len(v) for v in self.tpl.values())

    def _aug(self, key):
        got = self._cache.get(key)
        if got is None:
            got = [_normed(a) for t in self.tpl.get(key, []) for a in self._augment(t)]
            self._cache[key] = got
        return got

    def add(self, digit: str, t: np.ndarray, scale: int) -> bool:
        if self.count() >= MAX_TEMPLATES:
            return False
        q = _normed(t)
        for old in self.tpl.get((scale, digit), []):
            if _corr(q, _normed(old)) >= 0.995:
                return False
        self.tpl.setdefault((scale, digit), []).append(t)
        self._cache.pop((scale, digit), None)
        return True

    def score(self, g: np.ndarray, pos: int, scale: int) -> list[tuple[str, float]]:
        q = _normed(g)
        out = []
        for (sc, d), _ts in self.tpl.items():
            if sc != scale:
                continue
            if pos == 0 and d not in "0123":
                continue            # 方位角百位只能是 0-3
            best = -1.0
            for a in self._aug((sc, d)):
                v = _corr(q, a)
                if v > best:
                    best = v
            out.append((d, best))
        out.sort(key=lambda kv: -kv[1])
        return out


# ---------------------------------------------------------------- 单尺度
def _read_one(img: Image.Image, bank: Bank, scale: int):
    gl = glyphs(img, scale)
    if len(gl) != 3:
        return None, 0.0, f"尺度{scale} 切出 {len(gl)} 个字形"
    text, conf = "", 1.0
    for pos, g in enumerate(gl):
        ranked = bank.score(g, pos, scale)
        if not ranked:
            return None, 0.0, f"尺度{scale} 没有可用模板"
        if pos == 1 and text == "3":
            ranked = [(d, s) for d, s in ranked if d in "012345"]
            if not ranked:
                return None, 0.0, f"尺度{scale} 约束后无候选"
        d, s = ranked[0]
        if s < ACCEPT:
            return None, s, f"尺度{scale} 第 {pos + 1} 位只有 {s:.3f}"
        text += d
        conf = min(conf, s)
    return text, conf, ""


# ---------------------------------------------------------------- 对外接口
def read_az(img: Image.Image, bank: Bank):
    """返回 (text|None, conf, info)。text 一定是 3 位数字串；None 表示放弃、交给大模型。"""
    t0 = time.time()
    if bank.count() == 0:
        return None, 0.0, "模板库为空"
    got = []
    for sc in SCALES:
        try:
            got.append((sc,) + _read_one(img, bank, sc))
        except Exception as e:                      # noqa: BLE001
            got.append((sc, None, 0.0, f"尺度{sc} 出错:{e}"))
    texts = [g[1] for g in got]
    ms = int((time.time() - t0) * 1000)
    if texts[0] and texts[0] == texts[1]:
        conf = min(got[0][2], got[1][2])
        return texts[0], conf, f"本地 {ms} ms · 双尺度一致 · 最低分 {conf:.3f}"
    why = " | ".join(g[3] for g in got if g[3])
    return None, min(g[2] for g in got), f"本地 {ms} ms · {why or '双尺度不一致'}"


def open_bank(user_path, seed_path=None) -> Bank:
    """用户模板库；不存在时用随包的种子库初始化（并立刻存一份到用户目录）。"""
    bank = Bank(user_path).load()
    if bank.count() == 0 and seed_path:
        p = Path(seed_path)
        if p.exists():
            bank = Bank(p).load()
            bank.path = Path(user_path)
            bank.save()
    return bank


def learn(text: str, img: Image.Image, bank: Bank) -> int:
    """把一张已确认的图里的字形并进模板库（自学习）。返回新增条数。"""
    if not text or len(text) != 3 or not text.isdigit():
        return 0
    n = 0
    for sc in SCALES:
        try:
            gl = glyphs(img, sc)
        except Exception:                          # noqa: BLE001
            continue
        if len(gl) != 3:
            continue
        for c, g in zip(text, gl):
            if bank.add(c, g, sc):
                n += 1
    if n:
        bank.save()
    return n
