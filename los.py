# -*- coding: utf-8 -*-
r"""弹道遮挡：地形 + 静态建筑/桥梁/围墙/岩石对整条弹道的视线判定。

数据（随 exe 打包在 data/los/<slug>.npz，由 tools/build_los_pack.py 生成）：
每张图一串「偏航盒」（中心 cx/cy、半长 hx/hy、偏航 yaw、离地盒底 bot、盒顶 top、
类别 kind）。盒顶/盒底是**相对地面**的高度，读进来以后叠到 self.map.h_at 上，
就和解算用的是同一套高程基准。

判定思路（不做逐点像素遍历，全部向量化）：
  1. 地形：沿弹道按 step 米采样，取 max(地形) 与弹道高度比 —— 地形挡住就报。
  2. 建筑/桥梁/岩石：把弹道切成 ~50 m 的小段，每段先查空间索引（24 m 网格）拿出附近的盒，
     再做「线段 × 有偏航的长方体」slab 求交（先转进盒的局部坐标），全段向量化。
  3. 树木：同一套求交，但对**单独一棵树的那组盒**（npz 里 t 前缀的数组）。
     树盒是整棵树（含树冠）的包围盒，打进树冠也算挡 —— 弹道穿林时会被判成挡，
     这符合"树会挡弹道"的直觉，也偏保守。
     三者在 s 最小的那个上取挡点。

对外只有一个函数 check()，返回 dict（永不抛异常，没数据就说没数据）。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

import core
import paths

KINDS = ["other", "building", "house", "wall", "bridge", "tower", "container",
         "rock", "cliff", "roof", "ruin", "gate", "barrier", "pipe", "crate",
         "hut", "stair", "fence", "tree", "bush", "vehicle", "prop", "rubble",
         "sandbag"]

# 挡点的中文叫法（给小地图和状态栏用，要短）
LABELS = {"building": "建筑", "house": "房屋", "wall": "围墙", "bridge": "桥梁",
          "tower": "塔楼", "container": "集装箱", "rock": "岩石", "cliff": "崖壁",
          "roof": "屋顶", "ruin": "废墟", "gate": "门楼", "barrier": "路障",
          "pipe": "管道", "crate": "货箱", "hut": "棚屋", "stair": "台阶",
          "fence": "栅栏", "tree": "树木", "bush": "灌木", "other": "障碍物"}

CELL = 24.0          # 空间索引格边长（米）
STEP_TERRAIN = 2.0   # 地形采样步长（米）
CHUNK = 50.0         # 弹道分段长度（米，段内按直线做盒求交）
MARGIN = 0.5         # 地形余隙（米）：弹道贴着地面走不算挡

DATA_DIR = paths.resource_dir() / "data" / "los"


class LosMap:
    """一张图的遮挡盒 + 空间索引。"""

    def __init__(self, slug, arr, md):
        self.slug = slug
        self.md = md
        self.cx = arr["cx"].astype(np.float64) * 0.1
        self.cy = arr["cy"].astype(np.float64) * 0.1
        self.hx = arr["hx"].astype(np.float64) * 0.25
        self.hy = arr["hy"].astype(np.float64) * 0.25
        self.yaw = arr["yaw"].astype(np.float64) * (180.0 / 256.0)
        self.siny = np.sin(np.radians(self.yaw))
        self.cosy = np.cos(np.radians(self.yaw))
        # 离地高 -> 绝对高（与 map.h_at 同一基准）
        gnd = _h_at_many(md, self.cx, self.cy)
        self.bot = gnd + arr["bot"].astype(np.float64) * 0.5
        self.top = gnd + arr["top"].astype(np.float64) * 0.1
        self.bot = np.minimum(self.bot, self.top - 0.2)
        self.kind = arr["kind"].astype(np.int32)
        self.count = len(self.cx)
        self._idx = _make_index(self.cx, self.cy, self.hx, self.hy, md)
        self.nx, self.nz = self._idx["nx"], self._idx["nz"]

        # 树：单独一组盒，只参与遮挡（树冠挡弹道），不参与脚下高度。
        # 老数据包没有 t 前缀的数组，这里退化成「没有树」，不会报错。
        self.tcount = 0
        if "tx" in arr.files:
            self.tcx = arr["tx"].astype(np.float64) * 0.1
            self.tcy = arr["ty"].astype(np.float64) * 0.1
            self.thx = arr["thx"].astype(np.float64) * 0.25
            self.thy = arr["thy"].astype(np.float64) * 0.25
            tyaw = arr["tyaw"].astype(np.float64) * (180.0 / 256.0)
            self.tsiny = np.sin(np.radians(tyaw))
            self.tcosy = np.cos(np.radians(tyaw))
            tgnd = _h_at_many(md, self.tcx, self.tcy)
            self.tbot = tgnd + arr["tbot"].astype(np.float64) * 0.5
            self.ttop = tgnd + arr["ttop"].astype(np.float64) * 0.1
            self.tbot = np.minimum(self.tbot, self.ttop - 0.2)
            self.tkind = arr["tkind"].astype(np.int32)
            self.tcount = len(self.tcx)
            self._tidx = _make_index(self.tcx, self.tcy, self.thx, self.thy, md)

    def cell_ids(self, ix0, ix1, iz0, iz1):
        return _query(self._idx, ix0, ix1, iz0, iz1)

    def tcell_ids(self, ix0, ix1, iz0, iz1):
        if not self.tcount:
            return np.zeros(0, np.int64)
        return _query(self._tidx, ix0, ix1, iz0, iz1)


def _make_index(cx, cy, hx, hy, md):
    """把「盒 x 它盖住的 24 m 格」展开成 CSR 式倒排索引。"""
    nx = max(1, int(math.ceil(md.spanx / CELL)))
    nz = max(1, int(math.ceil(md.spanz / CELL)))
    x0 = np.floor((cx - hx) / CELL).astype(np.int32)
    x1 = np.floor((cx + hx) / CELL).astype(np.int32)
    z0 = np.floor((cy - hy) / CELL).astype(np.int32)
    z1 = np.floor((cy + hy) / CELL).astype(np.int32)
    np.clip(x0, 0, nx - 1, out=x0)
    np.clip(x1, 0, nx - 1, out=x1)
    np.clip(z0, 0, nz - 1, out=z0)
    np.clip(z1, 0, nz - 1, out=z1)
    w = x1 - x0 + 1
    h = z1 - z0 + 1
    # 大盒最多铺多少个格子：限制一下，免得 60 m 的崖壁把索引撑爆
    cnt = w * h
    ids = np.nonzero(cnt <= 64)[0]
    cnts = cnt[ids]
    if ids.size:
        rep = np.repeat(np.arange(ids.size), cnts)
        off = (np.arange(rep.size)
               - np.repeat(np.cumsum(cnts) - cnts, cnts))
        ww = w[ids][rep]
        cc = off % ww
        rr = off // ww
        cat = (z0[ids][rep] + rr) * nx + (x0[ids][rep] + cc)
        who = ids[rep]
    else:
        cat = np.zeros(0, np.int64)
        who = np.zeros(0, np.int64)
    order = np.argsort(cat, kind="stable")
    srt = cat[order]
    ptr = np.searchsorted(srt, np.arange(nx * nz + 1)).astype(np.int64)
    return dict(nx=nx, nz=nz, ptr=ptr, ids=who[order].astype(np.int64),
                big=np.nonzero(cnt > 64)[0])


def _query(idx, ix0, ix1, iz0, iz1):
    nx, nz = idx["nx"], idx["nz"]
    ix0 = max(0, min(int(ix0), nx - 1))
    ix1 = max(0, min(int(ix1), nx - 1))
    iz0 = max(0, min(int(iz0), nz - 1))
    iz1 = max(0, min(int(iz1), nz - 1))
    if ix0 > ix1 or iz0 > iz1:
        return np.zeros(0, np.int64)
    cells = (np.arange(iz0, iz1 + 1)[:, None] * nx
             + np.arange(ix0, ix1 + 1)[None, :]).ravel()
    lo = idx["ptr"][cells]
    hi = idx["ptr"][cells + 1]
    n = hi - lo
    if not n.any():
        out = np.zeros(0, np.int64)
    else:
        pos = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n))
        out = idx["ids"][np.repeat(lo, n) + pos]
    if len(idx["big"]):
        out = np.union1d(out, idx["big"])
    return out


def _h_at_many(md, xs, zs):
    """MapData.h_at 的向量化版本（双线性，夹边）。"""
    n = 1024
    fx = np.clip(np.asarray(xs) / md.cellx - 0.5, 0.0, n - 1.001)
    fz = np.clip(np.asarray(zs) / md.cellz - 0.5, 0.0, n - 1.001)
    x0 = np.floor(fx).astype(np.int32)
    z0 = np.floor(fz).astype(np.int32)
    tx = fx - x0
    tz = fz - z0
    e = md.elev
    a = e[z0, x0]
    b = e[z0, x0 + 1]
    c = e[z0 + 1, x0]
    d = e[z0 + 1, x0 + 1]
    return (a * (1 - tx) + b * tx) * (1 - tz) + (c * (1 - tx) + d * tx) * tz


class LosStore:
    """按地图 slug 惰性载入遮挡盒（一张图约 1～3 MB 内存，切图时清一份就够）。"""

    def __init__(self, folder=None):
        self.folder = Path(folder) if folder else DATA_DIR
        self._cache = {}
        self._warned = False

    def has(self, slug):
        return (self.folder / f"{slug}.npz").is_file()

    def get(self, md):
        slug = getattr(md, "slug", "")
        if not slug:
            return None
        if slug in self._cache:
            return self._cache[slug]
        p = self.folder / f"{slug}.npz"
        out = None
        if p.is_file():
            try:
                with np.load(p) as z:
                    out = LosMap(slug, z, md)
            except Exception as e:      # 数据坏了不能连累主流程
                print(f"[los] {slug} 载入失败：{e}", flush=True)
                out = None
        elif not self._warned:
            print(f"[los] 没有 {p}，弹道遮挡不参与判定", flush=True)
            self._warned = True
        self._cache = {slug: out}
        return out

    def drop(self):
        self._cache.clear()


# ---------------------------------------------------------------- 阵地脚下地物
# 自动抬升只认「人一定站在盒顶上」的地物：桥面、集装箱、货箱。
# 屋顶/房屋/塔楼不自动抬 —— 盒是整栋楼的包围盒，人站在楼里（一层）也会落进盒里，
# 自动抬到楼顶会凭空多出 6~22 m，比不修正更糟；这类只给提示，让用户手填。
AUTO_WALK = ("bridge", "container", "crate")
# 有屋顶但说不清人在楼上还是楼里：只提示
ROOF_HINT = ("roof", "building", "house", "hut", "tower", "ruin", "stair", "pipe")
_AUTO_IDS = np.array([KINDS.index(k) for k in AUTO_WALK], np.int32)
_HINT_IDS = np.array([KINDS.index(k) for k in ROOF_HINT], np.int32)
SUPPORT_MAX = 30.0      # 自动抬升上限（米）：比这还高说明盒子不是「脚下这块」
GROUND_TOL = 1.0        # 自动抬升只认「落在地上」的盒：盒底离地不超过 1 m
# 为什么要有 GROUND_TOL：struct.npz 里只有「整块模型的世界包围盒」，没有三角面。
# 一栋楼的水箱（fallujah 的 WaterContainer_1）盒顶离地 15 m、盒底离地 12.5 m，
# 玩家其实站在地面/楼里；栏杆、桁架这些也是独立盒、盒底在桥面之上。
# 只认盒底贴地的那些，等于「人真的能一脚踩上去」的表面，少掉的就是这类假抬升。


def _lift_at(md, lm, x, z, ids=None, max_dz=SUPPORT_MAX, grounded=False):
    """点位落在哪些盒的（内缩）占地里，取盒顶最高的那个，返回 (dz, label)。"""
    if lm is None or not getattr(lm, "count", 0):
        return 0.0, ""
    try:
        cid = lm.cell_ids(math.floor(float(x) / CELL), math.floor(float(x) / CELL),
                          math.floor(float(z) / CELL), math.floor(float(z) / CELL))
        cid = np.union1d(cid, getattr(lm, "_big", np.zeros(0, np.int64)))
        if not cid.size:
            return 0.0, ""
        if ids is not None:
            cid = cid[np.isin(lm.kind[cid], ids)]
            if not cid.size:
                return 0.0, ""
        dx = float(x) - lm.cx[cid]
        dz = float(z) - lm.cy[cid]
        si = lm.siny[cid]
        co = lm.cosy[cid]
        lx = dx * co + dz * si
        lz = -dx * si + dz * co
        hx = lm.hx[cid]
        hy = lm.hy[cid]
        shrink = np.minimum(1.0, 0.2 * np.minimum(hx, hy))
        inside = (np.abs(lx) <= hx - shrink) & (np.abs(lz) <= hy - shrink)
        gnd = float(md.h_at(x, z))
        lift = lm.top[cid] - gnd
        ok = inside & (lift >= 0.3) & (lift <= max_dz)
        if grounded:
            ok &= (lm.bot[cid] - gnd) <= GROUND_TOL
        if not ok.any():
            return 0.0, ""
        k = np.nonzero(ok)[0]
        k = k[np.argmax(lift[k])]
        return float(lift[k]), LABELS.get(KINDS[int(lm.kind[cid[k]])], "结构")
    except Exception as e:      # 检测失败绝不影响解算
        print(f"[los] 阵地高度检测失败：{e}", flush=True)
        return 0.0, ""


def support_at(md, lm, x, z, max_dz=SUPPORT_MAX):
    """阵地脚下能站上去的地物（桥面 / 集装箱 / 货箱）→ (dz, label)。

    点位要落在**内缩后**的占地里：盒是偏航包围盒，贴着墙站也会落进盒内。
    """
    return _lift_at(md, lm, x, z, _AUTO_IDS, max_dz, grounded=True)


def roof_hint(md, lm, x, z, max_dz=SUPPORT_MAX):
    """脚下有屋顶/楼体但不自动抬升，只给个提示高度 → (h, label)。"""
    return _lift_at(md, lm, x, z, _HINT_IDS, max_dz)


# ---------------------------------------------------------------- 弹道几何
# 0.52.0：弹道高度不再用「真空抛物线 tan/curv」近似，而是直接取核心模块积分出的
# 剖面（减速弹道也算得对）。zof(s) 给任意水平距离 s 处弹道的绝对高度。


def trace(md, lm, w, gun, tgt, hgun, sol, step=STEP_TERRAIN, margin=MARGIN):
    """算这条解对应的弹道被谁挡住。

    返回 dict：
      blocked  True/False
      kind     'terrain' / 'structure'（blocked 时有效）
      label    地形 / 建筑……（给界面）
      s        挡点离炮位的水平距离（米）
      hit      (rx, rz) 挡点在地图上的相对坐标
      top      挡点附近的地物高度（米，地形或盒顶）
      z_ray    弹道在那个位置的高度
      foot     结构盒的四角（地图相对坐标）——结构挡点时给小地图画轮廓
      kind_id  结构类别码（-1 = 地形）
      shadow   [(x, z) * 4] 被挡区域的四边形（小地图画斜线用）
      reason   没挡时的简短说明
    """
    out = {"blocked": False, "kind": "", "label": "", "s": 0.0, "hit": (0.0, 0.0),
           "top": 0.0, "z_ray": 0.0, "foot": None, "kind_id": -1,
           "shadow": [], "reason": "", "clear": 0.0}
    try:
        dx = tgt[0] - gun[0]
        dz = tgt[1] - gun[1]
        dist = math.hypot(dx, dz)
        if dist < 5.0:
            out["reason"] = "距离太近"
            return out
        if sol is None:
            out["reason"] = "没有解"
            return out
        # 物理角 elg：0.52.0 起解算结果里直接带 elg；老数据只有显示角 rad，退回时补回 aOff。
        elg = float(sol.get("elg") or 0.0)
        if not elg:
            elg = float(sol["rad"]) + math.radians(float(w.get("aOff") or 0))
        z0 = float(hgun) + float(w.get("hOff") or 0)
        if not math.isfinite(elg) or abs(elg) >= math.pi / 2:
            out["reason"] = "解算参数异常"
            return out
        prof = core.profile(w, elg, float(sol.get("dh") or 0.0), dist, 1.0)
        if not prof:
            out["reason"] = "弹道未落地"
            return out
        pxs, pys = prof

        def zof(sv):
            return z0 + np.interp(sv, pxs, pys)
        ux, uz = dx / dist, dz / dist

        # ---- 1) 地形
        n = max(2, int(dist / step) + 1)
        ss = np.linspace(0.0, dist, n)
        xs = gun[0] + ux * ss
        zs = gun[1] + uz * ss
        gnd = _h_at_many(md, xs, zs)
        zt = zof(ss)
        # 弹道要从炮口上方出来、在目标处落地，所以首尾各留一段不算遮挡
        keep = (ss > 5.0) & (ss < dist - 3.0)
        over = np.where(keep, (gnd - margin) - zt, -1.0)
        s_ter = None
        if np.any(over > 0):
            k = int(np.argmax(over > 0))
            # 二分细化到 0.25 m：找「刚好扎进地面」的那个点
            lo = ss[k - 1] if k > 0 else ss[k]
            hi = ss[k]
            for _ in range(18):
                mid = 0.5 * (lo + hi)
                g = float(_h_at_many(md, gun[0] + ux * mid, gun[1] + uz * mid))
                if g - margin > float(zof(mid)):
                    hi = mid
                else:
                    lo = mid
            s_ter = max(hi, 2.0)

        # ---- 2) 建筑/桥梁/岩石 + 树木：两组盒各跑一遍，取最近的
        hit = _trace_boxes(lm, gun, ux, uz, dist, zof)
        s_box = hit["s"] if hit else None
        thit = None
        if lm is not None and getattr(lm, "tcount", 0):
            thit = _trace_boxes(lm, gun, ux, uz, dist, zof, tree=True)
        s_tree = thit["s"] if thit else None

        # ---- 3) 三者谁更近
        cand = [(s_ter, "terrain"), (s_box, "box"), (s_tree, "tree")]
        cand = [(v, k) for v, k in cand if v is not None]
        if not cand:
            out["reason"] = "弹道通畅"
            if keep.any():
                out["clear"] = float(np.min((zt - gnd)[keep]))
            return out
        s, which = min(cand, key=lambda p: p[0])
        if which == "terrain":
            out.update(blocked=True, kind="terrain", label="地形", s=s,
                       hit=(gun[0] + ux * s, gun[1] + uz * s),
                       top=float(_h_at_many(md, gun[0] + ux * s, gun[1] + uz * s)),
                       z_ray=float(zof(s)), kind_id=-1)
            out["shadow"] = _shadow(gun, (ux, uz), 8.0, s, dist, md)
            return out
        h = thit if which == "tree" else hit
        kid = int(h["kind"])
        label = LABELS.get(KINDS[kid] if 0 <= kid < len(KINDS) else "other", "障碍物")
        out.update(blocked=True, kind="structure", label=label, s=s,
                   hit=(gun[0] + ux * s, gun[1] + uz * s),
                   top=float(h["top"]), z_ray=float(zof(s)),
                   foot=h["foot"], kind_id=kid)
        if which == "tree":
            out["veg"] = True
        half = max(2.0, min(float(h["half_deg"]), 22.0))
        out["shadow"] = _shadow(gun, (ux, uz), half, s, dist, md)
        return out
    except Exception as e:                       # 任何意外都不该影响出诸元
        out["blocked"] = False
        out["reason"] = f"判定失败：{e}"
        return out


def _boxes(lm, tree=False):
    """取「结构盒」或「树盒」那组数组（顺序：cx, cy, hx, hy, sin, cos, bot, top, kind）。"""
    if tree:
        return (lm.tcx, lm.tcy, lm.thx, lm.thy, lm.tsiny, lm.tcosy,
                lm.tbot, lm.ttop, lm.tkind)
    return (lm.cx, lm.cy, lm.hx, lm.hy, lm.siny, lm.cosy,
            lm.bot, lm.top, lm.kind)


def _trace_boxes(lm, gun, ux, uz, dist, zof, tree=False):
    """沿弹道分段做「线段 × 偏航盒」求交，返回最近的一次。"""
    if lm is None:
        return None
    if tree:
        if not getattr(lm, "tcount", 0):
            return None
    elif not getattr(lm, "count", 0):
        return None
    grp = _boxes(lm, tree)
    s = 0.0
    while s < dist:
        s2 = min(dist, s + CHUNK)
        za = float(zof(s))
        zb = float(zof(s2))
        ax, az = gun[0] + ux * s, gun[1] + uz * s
        bx, bz = gun[0] + ux * s2, gun[1] + uz * s2
        pad = 3.0
        fn = lm.tcell_ids if tree else lm.cell_ids
        ids = fn(math.floor((min(ax, bx) - pad) / CELL),
                 math.floor((max(ax, bx) + pad) / CELL),
                 math.floor((min(az, bz) - pad) / CELL),
                 math.floor((max(az, bz) + pad) / CELL))
        if len(ids):
            r = _seg_box(grp, ids, (ax, az, za), (bx, bz, zb))
            if r is not None:
                t, top, kid = r[0], r[1], r[2]
                ss = s + (s2 - s) * t
                # 盒的四角（地图相对坐标），给小地图画轮廓
                cx, cy = r[3], r[4]
                c = math.cos(r[5])
                si = math.sin(r[5])
                hx, hy = r[6], r[7]
                foot = [(cx + c * sx - si * sy, cy + si * sx + c * sy)
                        for sx, sy in ((hx, hy), (-hx, hy), (-hx, -hy), (hx, -hy))]
                half = 0.0
                for fx, fy in foot:
                    a = math.atan2(((fx - gun[0]) * -uz + (fy - gun[1]) * ux),
                                   ((fx - gun[0]) * ux + (fy - gun[1]) * uz))
                    half = max(half, abs(math.degrees(a)))
                return {"s": ss, "top": top, "kind": kid, "foot": foot,
                        "half_deg": half + 1.0}
        s = s2
    return None


def _slab(o, d, lo, hi, tmin, tmax, ok):
    """一维 slab：解 t 使 o + d*t ∈ [lo, hi]，和已有的 [tmin, tmax] 取交。"""
    par = np.abs(d) < 1e-12
    safe = np.where(par, 1.0, d)
    t1 = (lo - o) / safe
    t2 = (hi - o) / safe
    nlo = np.minimum(t1, t2)
    nhi = np.maximum(t1, t2)
    inside = (o >= lo) & (o <= hi)
    nlo = np.where(par, np.where(inside, -np.inf, np.inf), nlo)
    nhi = np.where(par, np.where(inside, np.inf, -np.inf), nhi)
    tmin = np.maximum(tmin, nlo)
    tmax = np.minimum(tmax, nhi)
    ok = ok & (tmax >= tmin)
    return tmin, tmax, ok


def _seg_box(grp, ids, p0, p1):
    """线段 p0→p1（3D：x, y=地图 z, z=高度）与一批偏航盒求交。

    grp 是 _boxes() 给的那组数组，ids 是候选盒下标。
    返回 (t, top, kind, cx, cy, yaw_rad, hx, hy) —— t 最小那个。
    """
    cx_a, cy_a, hx_a, hy_a, sin_a, cos_a, bot_a, top_a, kind_a = grp
    ox = cx_a[ids]
    oy = cy_a[ids]
    si = sin_a[ids]
    co = cos_a[ids]
    hx = hx_a[ids]
    hy = hy_a[ids]
    bot = bot_a[ids]
    top = top_a[ids]
    ax, ay, az = p0
    bx, by, bz = p1
    # 转进每个盒的局部坐标：局部 = R(-yaw) · (p - c)
    dax = ax - ox
    day = ay - oy
    dbx = bx - ox
    dby = by - oy
    lax = dax * co + day * si
    lay = -dax * si + day * co
    lbx = dbx * co + dby * si
    lby = -dbx * si + dby * co
    dx = lbx - lax
    dy = lby - lay
    dz = bz - az
    n = len(ids)
    tmin = np.zeros(n)
    tmax = np.ones(n)
    ok = np.ones(n, bool)
    # 三个轴各自是一段 [lo, hi] 的 slab：x/y 以盒心为中心，z 直接用世界高区间
    tmin, tmax, ok = _slab(lax, dx, -hx, hx, tmin, tmax, ok)
    tmin, tmax, ok = _slab(lay, dy, -hy, hy, tmin, tmax, ok)
    tmin, tmax, ok = _slab(np.full(n, az), np.full(n, dz), bot, top,
                           tmin, tmax, ok)
    ok &= (tmax >= 0.0) & (tmin <= 1.0)
    if not ok.any():
        return None
    tmin = np.maximum(tmin, 0.0)
    k = np.nonzero(ok)[0]
    k = k[np.argmin(tmin[k])]
    return (float(tmin[k]), float(top[k]), int(kind_a[ids[k]]),
            float(ox[k]), float(oy[k]), float(math.atan2(si[k], co[k])),
            float(hx[k]), float(hy[k]))


def _shadow(gun, u, half_deg, s_near, dist, md):
    """被挡区域（地图相对坐标的四点）：从挡点往后再延伸半个图幅的扇形。"""
    far = max(s_near + 60.0, min(dist * 2.4, math.hypot(md.spanx, md.spanz)))
    half = math.radians(max(1.0, half_deg))
    ca, sa = math.cos(half), math.sin(half)
    ux, uz = u
    # 垂直于弹道方向的两个单位向量
    vx, vz = -uz, ux
    def at(s, sgn):
        px = ux * ca - sgn * vx * sa
        pz = uz * ca - sgn * vz * sa
        n = math.hypot(px, pz) or 1.0
        return (gun[0] + px / n * s, gun[1] + pz / n * s)
    return [at(s_near, 1.0), at(s_near, -1.0), at(far, -1.0), at(far, 1.0)]
