# -*- coding: utf-8 -*-
r"""呼叫弹幕的几何计算与「迫击炮阵地库」（纯计算，不依赖 Qt）。

一次呼叫弹幕 = 几门迫击炮 + 一条起始线 + 一个推进方向与深度 + 每轮推进多少米：

    起始点 ─┬──────── 起始线（宽度 W，关于起始点对称）────────┬─
            │                                                  │
            │  ← 方向 normal（玩家在线的某一侧点一下决定）        │
            │        ↑ 第 1 轮目标线（线距 0）                    │
            │        ↑ 第 2 轮目标线（线距 speed）                │
            ▼        ↑ 第 3 轮 …                                ▼
        矩形长度 = 推进距离 depth

宽度 = 2 × 玩家点到起始点的距离，钳制在 [10, 10 × 门数] 米（每门至少分到 10 m 的正面）。
门数决定宽度上限：宽度按门数等分，每门打自己那一段的中点（所以每门覆盖 ≤ 10 m）。
推进速度 = 相邻两轮目标线的间距（米/轮），最小 10。

这个模块只管几何与阵地库的整理；诸元（密位/方位）仍由 core.evaluate 算，在 main.py 里做。
"""
from __future__ import annotations

import math
import re

MAX_POSITIONS = 15         # 阵地库最多 15 门
WIDTH_PER_GUN = 10.0       # 每门迫击炮负责的正面宽度（米）→ 总宽上限 = 10 × 门数
MIN_WIDTH = 10.0           # 总宽最小 10 m
MIN_SPEED = 10.0           # 推进速度最小 10 m/轮
MIN_SPAN = 120.0           # 小地图视野下限（米）：1 门也行，别把窗口缩得太小
MAX_SPAN = 200.0           # 小地图视野上限（米）：窗口再大就挡屏幕了
_UID_RE = re.compile(r"^B\d{2}$")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- 阵地库
def _fnum(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def next_uid(lst) -> str:
    """下一个可用编号（B01…B15）；满了返回空串。"""
    used = {str(p.get("uid") or "") for p in (lst or [])}
    for i in range(1, MAX_POSITIONS + 1):
        u = "B%02d" % i
        if u not in used:
            return u
    return ""


def normalize_positions(raw) -> list:
    """把配置里读到的阵地整理干净：坐标必须是数、UID 必须唯一、最多 15 个。

    配置是手改得动的 JSON，坏条目直接丢掉比让界面崩掉强。
    """
    out = []
    seen = set()
    for it in (raw if isinstance(raw, list) else []):
        if not isinstance(it, dict):
            continue
        x, z = _fnum(it.get("x")), _fnum(it.get("z"))
        if x is None or z is None:
            continue
        uid = str(it.get("uid") or "").strip().upper()
        if not _UID_RE.match(uid) or uid in seen:
            uid = next_uid(out)
        if not uid:
            break
        seen.add(uid)
        out.append({
            "uid": uid,
            "name": str(it.get("name") or "").strip(),
            "x": x,
            "z": z,
            "weapon": str(it.get("weapon") or "").strip(),
        })
        if len(out) >= MAX_POSITIONS:
            break
    return out


def find(lst, uid):
    for p in (lst or []):
        if p.get("uid") == uid:
            return p
    return None


def label(p: dict) -> str:
    """列表里一行显示什么：B03 · 名称 · 武器键。"""
    bits = [str(p.get("uid") or "?")]
    if p.get("name"):
        bits.append(p["name"])
    if p.get("weapon"):
        bits.append(p["weapon"])
    return " · ".join(bits)


# ---------------------------------------------------------------- 几何
def max_width(n: int) -> float:
    """总宽上限：10 m × 参与门数（至少 10 m）。"""
    return max(MIN_WIDTH, WIDTH_PER_GUN * max(1, int(n)))


def span_for(n: int) -> float:
    """定宽度/方向时小地图该开多大：门数越多能点的宽度越大，视野跟着放大。"""
    return _clamp(30.0 * max(1, int(n)), MIN_SPAN, MAX_SPAN)


def segment(start, p, n: int):
    """宽度这一步：玩家点了 p → 起始线（关于 start 对称）。

    返回 (a, b, width, msg)：a/b 是线段两个端点（世界坐标），width 是钳制后的总宽；
    点得离起始点太近时返回 (None, None, 0, 说明)。
    """
    dx = float(p[0]) - float(start[0])
    dz = float(p[1]) - float(start[1])
    d = math.hypot(dx, dz)
    if d < 0.5:
        return None, None, 0.0, "点得离起始点太近 —— 往一侧挪几米再点"
    ux, uz = dx / d, dz / d
    half = _clamp(d, MIN_WIDTH / 2.0, max_width(n) / 2.0)
    return ((start[0] - ux * half, start[1] - uz * half),
            (start[0] + ux * half, start[1] + uz * half),
            half * 2.0, "")


def unit(a, b):
    dx, dz = float(b[0]) - float(a[0]), float(b[1]) - float(a[1])
    L = math.hypot(dx, dz)
    if L < 1e-9:
        return None
    return dx / L, dz / L


def direction(start, a, b, q):
    """方向这一步：玩家在起始线某一侧点了 q → (法线, 推进距离, 说明)。

    法线指向被点的那一侧（单位向量），推进距离 = q 到起始线的垂直距离。
    点在线上（或离得太近）时返回 (None, 0, 说明)，让玩家换个地方点。
    """
    u = unit(a, b)
    if u is None:
        return None, 0.0, "起始线段太短，宽度得重新点"
    ux, uz = u
    vx = float(q[0]) - float(start[0])
    vz = float(q[1]) - float(start[1])
    side = ux * vz - uz * vx          # >0 / <0 = 线段两侧
    nx, nz = (-uz, ux) if side > 0 else (uz, -ux)
    depth = vx * nx + vz * nz         # 按选定的法线取正
    if abs(side) < 0.8 or depth < 2.0:
        return None, 0.0, "点在起始线上（或太近）—— 往线的某一侧点远一点"
    if depth < 5.0:
        return None, 0.0, "推进距离只有 %.0f m，太短了 —— 再往一侧点远一点" % depth
    return (nx, nz), depth, ""


def round_count(depth: float, speed: float) -> int:
    """一共打几轮：从起始线开始，每隔 speed 米一条目标线，最后一条不超过 depth。"""
    if speed <= 0:
        return 0
    return int(math.floor(depth / speed + 1e-9)) + 1


def line_points(start, a, b, normal, off: float) -> tuple:
    """第 off 米处那条目标线的两个端点（画矩形用）。"""
    u = unit(a, b)
    if u is None:
        u = (1.0, 0.0)
    w = math.hypot(b[0] - a[0], b[1] - a[1])
    p0 = (start[0] - u[0] * w / 2.0 + normal[0] * off,
          start[1] - u[1] * w / 2.0 + normal[1] * off)
    p1 = (start[0] + u[0] * w / 2.0 + normal[0] * off,
          start[1] + u[1] * w / 2.0 + normal[1] * off)
    return p0, p1


def aims(start, a, b, normal, depth: float, speed: float, n: int):
    """每一轮里每门炮要打的目标点。

    宽度按门数等分（每段 = W/n ≤ 10 m），第 i 门打第 i 段的中点；第 k 轮整体沿法线推
    进 (k-1)×speed 米。返回 [{"round":1,"off":0.0,"aims":[(x,z), …]}, …]。
    """
    u = unit(a, b) or (1.0, 0.0)
    w = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(n))
    out = []
    for k in range(round_count(depth, speed)):
        off = k * speed
        pts = []
        for i in range(n):
            s = -w / 2.0 + (i + 0.5) * (w / n)
            pts.append((start[0] + u[0] * s + normal[0] * off,
                        start[1] + u[1] * s + normal[1] * off))
        out.append({"round": k + 1, "off": off, "aims": pts})
    return out
