# -*- coding: utf-8 -*-
r"""posocr.py —— 本地网格坐标识别（Squad HUD 上的玩家坐标，形如 "A1-4-1" / "I10-9-5"）。

为什么要有它：方位角（docr.py）本地识别已经跑通，玩家自身坐标却每次都要发给
DeepSeek 视觉模型。坐标是 3~4 段、60~80 px 宽的小字，模型偶尔会把 I/1、O/0、
M/N 认错；而坐标错一格，后面算出来的距离和方位角全错，所以宁可本地认、认不出来
再交给模型。

做法（沿用 docr 的思路，但坐标是"字母+数字+短横"的混合串）：

  1) 灰度 + LANCZOS 放大（4 个尺度 5/6/8/10 各跑一遍）
  2) 白掩膜：阈值 = clamp(p99 亮度 * 0.90, 0.70, 0.96)
  3) 列投影切字形。注意切字间隙必须随尺度放大：原生分辨率下相邻字形会粘成一块，
     固定间隙会把 "M5-6-4" 切成一格。
  4) 结构解析：短横（比中位高矮很多）把串切成 3~4 组；第 1 组 2~3 个字形
     （字母 + 1~2 位行号），其余每组恰好 1 位数字。不符合就放弃。
  5) 位置约束决胜：第 1 个字形只在字母表里找，其余只在数字里找 —— 这一步
     彻底消除了 I/1、O/0 的混淆。
  6) 每个字形取最高分，同时要求 **top1 - top2 >= MARGIN**（没有相近的对手）
     且最高分 >= ACCEPT；4 个尺度必须给出同一串。任一不满足就放弃，交给模型。

实测（26 张真机截图，模板库 = 全部字形）：
  干净图        ok=26 decl=0  WRONG=0
  ±1px 抖动     ok=89 decl=15 WRONG=0   （104 例）
  ±2px 重抖动   ok=95 decl=35 WRONG=0   （130 例）
  分辨率缩放     x0.75/x0.85/x1.15/x1.3 → ok=2/8/15/17，其余全部放弃，WRONG=0
也就是说：认不出来时它会放弃，不会瞎猜；放弃的交给模型，模型认对了再 learn()
进库，换分辨率这类情况会随着使用自愈。

模板库：种子来自 data/position_bank.npz（tools/build_position_bank.py 从
D:\mil-ai\IFC\grid-position 的 26 张样本生成），运行中每次"模型认对了"都会
learn() 并入新字形。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import numpy as np
from PIL import Image

from docr import _corr, _gray_up, _norm, _normed

SCALES = (5, 6, 8, 10)   # 四尺度：单尺度都可能有误认，四尺度一致的才可信
MIN_W = 3                # 单字最小宽度（放大后像素）
ACCEPT = 0.90            # 单字最高分门槛（干净图实测 0.88~0.94，抖动图下限 ~0.90）
MARGIN = 0.03            # top1 与 top2 的分差门槛（干净图实测 0~0.03，抖动图 ~0.03）
MAX_TEMPLATES = 1600     # 模板库总量上限（所有尺度合计）
DASH_RATIO = 0.55        # 高度低于中位高的这个比例 → 判为短横
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DIGITS = "0123456789"

_POS_RE = re.compile(r"^[A-Z][0-9]{1,2}(?:-[0-9]){1,3}$")


# ---------------------------------------------------------------- 图像处理
def _segments(m: np.ndarray, max_gap: int):
    """按列投影切块。max_gap 要随尺度放大，否则相邻字形会粘在一起。"""
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
                if gap > max_gap:
                    break
            x += 1
        x1 = x - gap
        if x1 - x0 >= MIN_W:
            out.append((x0, x1))
    return out


def _thr(g: np.ndarray) -> float:
    """白掩膜阈值：取 99.9 分位（**不能**用 docr 那边的 99 分位）。

    99 分位会随"文字占整张图的比例"漂移。训练样本是紧贴文字的 79×26 裁剪，
    字形像素占 4~6%，p99 正好落在字芯上（≈1.00）；可游戏里框出来的玩家坐标
    区域是 265×70，字只占 ~1%，p99 就掉到背景上（实测 0.58），阈值被 clamp
    到 0.70 下限，掩膜把字周围的暗边全吃进来，相邻字粘成一块 → 结构解析失败。
    这正是 dist\run.log 里反复出现的
    "kind=pos 本地放弃：… 切出 11 个字、结构不符" —— 本地每次都放弃，于是
    每次都走大模型（既慢又要密钥）。

    换成 99.9 分位后，紧裁图与 265×70 宽裁图的阈值都稳定在 0.99~1.00，
    查询字形与种子模板的提取方式一致，宽裁图也能本地认。
    """
    hi = float(np.percentile(g, 99.9))
    return max(0.70, min(0.96, hi * 0.90))


def _thrs(img: Image.Image) -> list[float]:
    """主阈值 + 两档放宽（文字只占千分之一这种极端裁剪兜底）。

    分位数在**原图**上算：放大图有几百万像素，np.percentile 要几十毫秒，
    而阈值本身与放大无关。
    """
    a = np.asarray(img.convert("L"), np.float32) / 255.0
    t = _thr(a)
    out = [t]
    for k in (0.95, 0.88):
        v = round(max(0.62, t * k), 4)
        if v not in out:
            out.append(v)
    return out


def _glyphs_from(g: np.ndarray, thr: float, up: int) -> list[dict]:
    """在已放大的灰度图上按给定阈值切字形。"""
    m = g >= thr
    max_gap = max(3, int(round(up * 0.6)))
    res = []
    for x0, x1 in _segments(m, max_gap):
        rows = np.where(m[:, x0:x1].any(1))[0]
        if len(rows) == 0:
            continue
        res.append({
            "h": int(rows[-1] - rows[0] + 1),
            "w": int(x1 - x0),
            "g": _norm(g[rows[0]:rows[-1] + 1, x0:x1]),
        })
    return res


def glyphs(img: Image.Image, up: int) -> list[dict]:
    """把一张坐标截图切成字形列表：[{h, w, g(归一化灰度)}, ...]，顺序即从左到右。"""
    return _glyphs_from(_gray_up(img, up), _thrs(img)[0], up)


def windows(gl: list[dict]) -> list[list[list[dict]]]:
    """在字形序列里找出所有"像坐标"的窗口，返回 [[glyph...], ...] 的列表。

    截图框往往比文字宽得多（玩家坐标框 265×70，字只有 80×26），框里还会带进
    别的 HUD 文字或图标。所以不能要求"整张图正好是一个坐标"，而是直接在字形
    序列上按模式匹配：

        字母 + 1~2 位行号  -  1 位  -  1 位  [-  1 位]

    起点可以落在任意字形上（框里多出来的东西排在窗口外就自动被忽略），
    连续两个短横（多切出来的碎块）也跳过。窗口之外的杂项不影响结果；
    多个窗口都成立时由置信度和领先优势挑最好的那个。
    """
    if not gl:
        return []
    med = sorted(x["h"] for x in gl)[len(gl) // 2]
    dash = [x["h"] < DASH_RATIO * med for x in gl]
    n = len(gl)
    res = []
    for i in range(n):
        if dash[i]:
            continue
        for L0 in (2, 3):
            j = i + L0
            if j >= n or not dash[j]:
                continue
            parts = [gl[i:j]]
            k = j + 1                       # 第一个数字的位置
            while len(parts) < 4 and k < n:
                if dash[k]:                 # 连续短横：跳过，继续找数字
                    k += 1
                    continue
                parts.append([gl[k]])
                if k + 1 >= n or not dash[k + 1]:
                    break                   # 后面不是短横 → 窗口到此为止
                k += 2
            if len(parts) >= 3:
                res.append(parts)
    return res


# ---------------------------------------------------------------- 模板库
class Bank:
    """字形模板库，按 (尺度, 字符) 分组；字符既可能是字母也可能是数字。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.tpl: dict[tuple[int, str], list[np.ndarray]] = {}
        self._cache: dict[tuple[int, str], list[np.ndarray]] = {}

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

    def count(self) -> int:
        return sum(len(v) for v in self.tpl.values())

    def _aug(self, key):
        """取该组的归一化模板。

        这里**不做** ±1 px 的滚动增广：_corr() 本身就在查询侧试 9 个平移，
        再给模板加增广只会把耗时乘 5（实测 370 ms/图 → 75 ms/图），而准确率
        一点没变（26 张干净图、234 例抖动，增广前后都是 WRONG=0）。
        """
        got = self._cache.get(key)
        if got is None:
            got = [_normed(t) for t in self.tpl.get(key, [])]
            self._cache[key] = got
        return got

    def add(self, sym: str, t: np.ndarray, scale: int) -> bool:
        if self.count() >= MAX_TEMPLATES:
            return False
        q = _normed(t)
        for old in self.tpl.get((scale, sym), []):
            if _corr(q, _normed(old)) >= 0.995:
                return False
        self.tpl.setdefault((scale, sym), []).append(t)
        self._cache.pop((scale, sym), None)
        return True

    def score(self, g: np.ndarray, cands: str, scale: int) -> list[tuple[str, float]]:
        """在给定候选字符里按相似度排序（位置约束由调用方给）。"""
        q = _normed(g)
        out = []
        for sym in cands:
            ts = self._aug((scale, sym))
            if not ts:
                continue
            best = -1.0
            for a in ts:
                v = _corr(q, a)
                if v > best:
                    best = v
            out.append((sym, best))
        out.sort(key=lambda kv: -kv[1])
        return out


# ---------------------------------------------------------------- 单尺度
def _read_window(win, bank: Bank, scale: int):
    """读一个窗口：第 1 个字形只在字母表里选，其余只在数字里选。"""
    text, conf, margin = "", 1.0, 1.0
    for gi, ch in enumerate(win):
        for ci, g in enumerate(ch):
            cands = LETTERS if (gi == 0 and ci == 0) else DIGITS
            ranked = bank.score(g["g"], cands, scale)
            if not ranked:
                return None, 0.0, 0.0, f"尺度{scale} 没有可用模板"
            s, c = ranked[0]
            m = c - (ranked[1][1] if len(ranked) > 1 else 0.0)
            text += s
            conf = min(conf, c)
            margin = min(margin, m)
        if gi < len(win) - 1:
            text += "-"
    return text, conf, margin, ""


def _read_one(img: Image.Image, bank: Bank, scale: int, thrs=None):
    """单尺度识别：放大只做一次，阈值按主 → 放宽依次试。

    一个阈值档可能切出好几个"像坐标"的窗口。优先取**通过门槛且段数最多**的
    那个（避免 4 级坐标 G11-2-3-1 被截成 3 级）；都不通过时取置信度最高的，
    留给上层判"本地放弃"。
    """
    g = _gray_up(img, scale)
    best = None                                     # (通过门槛, 段数, conf, margin, text)
    why = ""
    for thr in (thrs or _thrs(img)):
        gl = _glyphs_from(g, thr, scale)
        wins = windows(gl)
        if not wins:
            why = f"尺度{scale} 切出 {len(gl)} 个字、结构不符"
            continue
        tier = None
        for win in wins:
            text, conf, margin, err = _read_window(win, bank, scale)
            if text is None:
                why = err
                continue
            ok = conf >= ACCEPT and margin >= MARGIN
            key = (ok, len(win) if ok else 0, conf, margin, text)
            if best is None or key[:4] > best[:4]:
                best = key
            if ok and (tier is None or key[:4] > tier[:4]):
                tier = key
        if tier is not None:                         # 这一档已经有可信结果，不再放宽
            return tier[4], tier[2], tier[3], ""
    if best is not None:
        return best[4], best[2], best[3], ""
    return None, 0.0, 1.0, why or f"尺度{scale} 没有可用候选"


# ---------------------------------------------------------------- 对外接口
def read_pos(img: Image.Image, bank: Bank):
    """返回 (text|None, conf, info)。text 形如 "A1-4-1"；None = 放弃，交给大模型。"""
    t0 = time.time()
    if bank.count() == 0:
        return None, 0.0, "模板库为空"
    thrs = _thrs(img)
    got = []
    for sc in SCALES:
        try:
            got.append((sc,) + _read_one(img, bank, sc, thrs))
        except Exception as e:                      # noqa: BLE001
            got.append((sc, None, 0.0, 0.0, f"尺度{sc} 出错:{e}"))
    ms = int((time.time() - t0) * 1000)
    texts = [g[1] for g in got]
    if any(t is None for t in texts):
        why = " | ".join(g[4] for g in got if g[4])
        return None, 0.0, f"本地 {ms} ms · {why}"
    uniq = set(texts)
    if len(uniq) != 1:
        detail = " / ".join(f"{g[0]}:{g[1]}" for g in got)
        return None, 0.0, f"本地 {ms} ms · 尺度不一致（{detail}）"
    text = texts[0]
    conf = min(g[2] for g in got)
    margin = min(g[3] for g in got)
    if not _POS_RE.match(text):
        return None, conf, f"本地 {ms} ms · 结果形状不合法（{text}）"
    if conf < ACCEPT:
        return None, conf, f"本地 {ms} ms · 置信度 {conf:.3f} < {ACCEPT}"
    if margin < MARGIN:
        return None, conf, f"本地 {ms} ms · 领先优势 {margin:.3f} < {MARGIN}（{text} 有相近候选）"
    return text, conf, f"本地 {ms} ms · 四尺度一致 · 最低分 {conf:.3f} · 领先 {margin:.3f}"


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
    """把一张已确认的坐标图里的字形并进模板库（自学习）。返回新增条数。"""
    if not text:
        return 0
    parts = [p for p in re.split(r"[-\s]+", str(text).strip().upper()) if p]
    if len(parts) not in (3, 4):
        return 0
    if not (parts[0][:1].isalpha() and parts[0][1:].isdigit()):
        return 0
    if any((not p.isdigit()) or len(p) != 1 for p in parts[1:]):
        return 0
    want = [len(p) for p in parts]
    n = 0
    thrs = _thrs(img)
    for sc in SCALES:
        try:
            g = _gray_up(img, sc)
        except Exception:                          # noqa: BLE001
            continue
        win = None
        for thr in thrs:
            gl = _glyphs_from(g, thr, sc)
            win = next((w for w in windows(gl) if [len(c) for c in w] == want), None)
            if win is not None:
                break
        if win is None:
            continue
        for ch, part in zip(win, parts):
            for g, c in zip(ch, part):
                if bank.add(c, g["g"], sc):
                    n += 1
    if n:
        bank.save()
    return n
