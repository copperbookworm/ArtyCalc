# -*- coding: utf-8 -*-
"""Squad 间瞄火力计算内核：网格键位 + 弹道解算（与 Yehorivka 页面同口径）。

0.52.0 起与页面保持同一套实现：官方武器的初速 / 射界 / 弹丸寿命按游戏本体
pak 实测（MuzzleVelocity、SQTurretMovementComponent.PitchInterval、
InitialLifeSpan、SQProjectileMovement），射界与弹道一律用炮管物理角 elg，
减速弹道（UB-32）按真实速度剖面做 RK2 积分。解算框架源自 SquadCalc 46.1.0
（MIT-NC, sh4rkman），但「等效初速」与「显示角比射界」两个近似已去掉。
"""
from __future__ import annotations

import json
import math

import paths

DATA = paths.resource_dir() / "data"

KP_M = 300.0          # 主格边长（米）
KP_LEVELS = 4         # 主格 + 三级子格（F11-2-5-6；最后一级 300/81 = 3.7 m）
#                       望远镜火力支援必须细到第 4 段，所以整页坐标统一按 4 段显示
G0 = 9.78             # 沿用 SquadCalc App.gravity（DefaultEngine.ini 不在 pak 内，无法再证实）


# ---------------------------------------------------------------- 网格键位
def col_letter(col: int) -> str:
    s = ""
    while col >= 0:
        s = chr(65 + col % 26) + s
        col = col // 26 - 1
    return s


def letter_col(s: str) -> int:
    c = 0
    for ch in s:
        c = c * 26 + (ord(ch) - 64)
    return c - 1


def text_at(gx: float, gz: float, level: int = 4) -> str:
    """相对地图左上角的米数 -> 网格文本（D4-5-7-3）。"""
    out = col_letter(int(math.floor(gx / KP_M))) + str(int(math.floor(gz / KP_M)) + 1)
    for k in range(1, min(level, KP_LEVELS)):
        iv = KP_M / (3 ** k)
        sx = int(math.floor(gx / iv)) % 3
        sz = int(math.floor(gz / iv)) % 3
        out += "-" + str(7 - sz * 3 + sx)
    return out


class ParseError(ValueError):
    pass


def parse(text: str) -> tuple[float, float]:
    """网格文本 -> 相对米数（格心）。支持 D3 / D3-7 / D3-7-4 / D3-7-4-6。"""
    import re
    raw = str(text or "").strip()
    if not raw:
        raise ParseError("没有坐标")
    t = re.sub(r"[^A-Z0-9]+", "-", raw.upper()).strip("-")
    m = re.match(r"^([A-Z]+)0*(\d+)((?:-\d+)*)$", t)
    if not m:
        raise ParseError("格式不对，应形如 D4-5-7")
    col = letter_col(m.group(1))
    if col < 0:
        raise ParseError("列字母不合法")
    row = int(m.group(2)) - 1
    if row < 0:
        raise ParseError("行号要从 1 开始")
    gx, gz = col * KP_M, row * KP_M
    rest = [p for p in (m.group(3).split("-")[1:] if m.group(3) else []) if p != ""]
    if len(rest) > KP_LEVELS - 1:
        raise ParseError("最多三级子格（如 D4-5-7-3）")
    iv = KP_M
    for i, p in enumerate(rest):
        if not re.match(r"^0*[1-9]$", p):
            raise ParseError("子格只能是 1–9 的单个数字")
        iv = KP_M / (3 ** (i + 1))
        n = int(p)
        gx += iv * ((n - 1) % 3)
        gz += iv * (2 - (n - 1) // 3)
    gx += iv / 2
    gz += iv / 2
    return gx, gz


# ---------------------------------------------------------------- 弹道
def load_weapons() -> list[dict]:
    """读武器表，顺手做一次体检：坏条目当场报错指出是哪一条，
    不要拖到算诸元的时候才抛 KeyError（手改 weapons.json 很容易漏字段）。"""
    raw = json.loads((DATA / "weapons.json").read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("weapons.json 应该是非空数组")
    out = []
    for i, w in enumerate(raw):
        n = i + 1
        if not isinstance(w, dict):
            raise ValueError(f"weapons.json 第 {n} 条不是对象")
        tag = w.get("key") or f"第 {n} 条"
        for k in ("key", "name", "v", "moa", "minEl", "unit", "ang"):
            if w.get(k) in (None, ""):
                raise ValueError(f"weapons.json {tag} 缺字段 {k}")
        me = w["minEl"]
        if not isinstance(me, (list, tuple)) or len(me) != 2:
            raise ValueError(f"weapons.json {tag} 的 minEl 要写成 [低, 高]")
        try:
            lo, hi = (float(me[0]), float(me[1]))
        except (TypeError, ValueError):
            raise ValueError(f"weapons.json {tag} 的 minEl 不是数字")
        if lo > hi:
            raise ValueError(f"weapons.json {tag} 的 minEl 低值大于高值（{lo:g} > {hi:g}）")
        w["minEl"] = [lo, hi]          # 手写成字符串也统一成数字，解算里要拿来比大小
        if w["unit"] not in ("mil", "deg", "degMin"):
            raise ValueError(f"weapons.json {tag} 的 unit 只能是 mil / deg / degMin")
        if w["ang"] not in ("low", "high"):
            raise ValueError(f"weapons.json {tag} 的 ang 只能是 low / high")
        dec, dec_t = (w.get("dec") or 0), (w.get("decT") or 0)
        if dec and not dec_t:
            raise ValueError(f"weapons.json {tag} 写了 dec 却没有 decT（减速时长）")
        if dec and (dec * dec_t) >= float(w["v"]):
            raise ValueError(f"weapons.json {tag} 的 dec×decT（{dec * dec_t:g}）不小于初速 {w['v']}：减速结束时会停下或倒退")
        try:
            if float(w["v"]) <= 0 or float(w["moa"]) < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"weapons.json {tag} 的 v 要为正数、moa 不能为负")
        out.append(w)
    return out


def load_maps() -> list[dict]:
    return json.loads((DATA / "maps.json").read_text(encoding="utf-8"))


# ---- 0.52.0：与页面同一套解算。以前把减速折成「按距离平均的等效速度」再套真空
# 公式，UB-32 的低角射角因此偏小（900 m 少 3.0°），射界又拿显示角去比（aOff 一叠加
# 就偏移）。现在物理角 elg 一路到底，减速弹道按真实速度剖面做 RK2 积分。
def is_dec(w: dict) -> bool:
    """有减速段的武器（UB-32）：前 decT 秒沿弹道方向匀减速，之后匀速。"""
    dec, dec_t = (w.get("dec") or 0.0), (w.get("decT") or 0.0)
    return bool(dec > 0 and dec_t > 0 and dec * dec_t < float(w["v"]))


def flight(w: dict, elg: float, dh: float, step: float = 0.0, dt: float = 0.02):
    """从炮口按速度剖面积分到落地（y 相对炮口，向上为正）。

    dh 是目标相对炮位的高差；炮口比阵地高 hOff，所以要落地的高度是 dh - hOff。
    返回 {r, tof, samples}；落地前耗尽弹丸寿命返回 None。
    """
    dec, dec_t = (w.get("dec") or 0.0), (w.get("decT") or 0.0)
    g = G0 * float(w.get("gs") or 1.0)
    ty = dh - float(w.get("hOff") or 0.0)
    vx = float(w["v"]) * math.cos(elg)
    vy = float(w["v"]) * math.sin(elg)
    x = y = t = 0.0
    px = py = pt = 0.0
    sp: list[tuple[float, float]] = []
    next_s = float(step or 0.0)
    t_max = min(float(w.get("life") or 100.0), 120.0)
    guard = 0
    while t < t_max and guard < 60000:
        guard += 1
        s1 = math.hypot(vx, vy) or 1.0
        a1 = (dec / s1) if (dec and t < dec_t) else 0.0
        mx = vx - a1 * vx * dt / 2.0
        my = vy - (a1 * vy + g) * dt / 2.0
        s2 = math.hypot(mx, my) or 1.0
        a2 = (dec / s2) if (dec and (t + dt / 2.0) < dec_t) else 0.0
        px, py, pt = x, y, t
        x += mx * dt
        y += my * dt
        vx -= a2 * mx * dt
        vy -= (a2 * my + g) * dt
        t += dt
        if next_s and x >= next_s:
            sp.append((x, y))
            next_s += step
        if vy < 0 and y <= ty:
            f = ((py - ty) / (py - y)) if (py - y) > 1e-9 else 0.0
            if not (0.0 <= f <= 1.0):
                f = 0.0
            fx = px + (x - px) * f
            if next_s:
                sp.append((fx, ty))
            return {"r": fx, "tof": pt + (t - pt) * f, "samples": sp}
    return None


def range_at(w: dict, elg: float, dh: float, step: float = 0.0, dt: float = 0.02):
    """给定物理射角求落点；真空弹道走解析式，减速弹道走积分。"""
    if is_dec(w):
        return flight(w, elg, dh, step, dt)
    g = G0 * float(w.get("gs") or 1.0)
    ty = dh - float(w.get("hOff") or 0.0)
    v = float(w["v"])
    vy = v * math.sin(elg)
    vx = v * math.cos(elg)
    disc = vy * vy - 2.0 * g * ty
    if disc < 0:
        return None
    t = (vy + math.sqrt(disc)) / g
    if not (t > 0):
        return None
    out = {"r": vx * t, "tof": t, "samples": []}
    if step:
        ce = math.cos(elg)
        te = math.tan(elg)
        s = step
        while s < out["r"]:
            out["samples"].append((s, te * s - g * s * s / (2.0 * v * v * ce * ce)))
            s += step
    return out


def profile(w: dict, elg: float, dh: float, span: float, ds: float = 1.0):
    """弹道剖面 (xs, ys)：y 相对炮口。给遮挡判定插值用（减速武器也算得对）。"""
    fl = range_at(w, elg, dh, ds, 0.02)
    if not fl:
        return None
    xs = [0.0]
    ys = [0.0]
    for s, y in fl["samples"]:
        if s > 0:
            xs.append(s)
            ys.append(y)
    r = fl["r"]
    ty = dh - float(w.get("hOff") or 0.0)
    if xs[-1] < r:
        xs.append(r)
        ys.append(ty)
    if xs[-1] < span:
        xs.append(span)
        ys.append(ys[-1])
    return xs, ys


def angle_vac(w: dict, dist: float, dh: float, sgn: float):
    """真空弹道的发射角解析解（物理角，不含 aOff）。"""
    g = G0 * float(w.get("gs") or 1.0)
    v = float(w["v"])
    p1 = v ** 4 - g * (g * dist * dist + 2.0 * (dh - float(w.get("hOff") or 0.0)) * v * v)
    if p1 < 0:
        return None
    return math.atan((v * v + sgn * math.sqrt(p1)) / (g * dist))


def angle_num(w: dict, dist: float, dh: float, sgn: float, fast: bool = False):
    """减速武器没有解析解：在射界内二分（45° 以下射程单调增、以上单调减）。

    fast 只给画射程环用：步长与迭代次数放粗，判定留 ±0.5 m 容差。
    """
    lo = max(-89.0, w["minEl"][0])
    hi = min(89.0, w["minEl"][1])
    if sgn < 0:
        a, b = lo, min(45.0, hi)
    else:
        a, b = max(45.0, lo), hi
    if not (b > a):
        return None
    dt = 0.08 if fast else 0.02
    it = 14 if fast else 26
    ra = range_at(w, math.radians(a), dh, 0.0, dt)
    rb = range_at(w, math.radians(b), dh, 0.0, dt)
    if not ra or not rb:
        return None
    if sgn < 0:
        if dist < ra["r"] - 0.5 or dist > rb["r"] + 0.5:
            return None
    else:
        if dist > ra["r"] + 0.5 or dist < rb["r"] - 0.5:
            return None
    for _ in range(it):
        m = (a + b) / 2.0
        rm = range_at(w, math.radians(m), dh, 0.0, dt)
        if not rm:
            return None
        if sgn < 0:
            if rm["r"] < dist:
                a = m
            else:
                b = m
        else:
            if rm["r"] > dist:
                a = m
            else:
                b = m
    return math.radians((a + b) / 2.0)


def solve(w: dict, dist: float, dh: float, fast: bool = False) -> dict:
    """返回 {low, high, err}；每支含 rad/elg/deg/mil/tof/sprA/sprB/ok/why。

    elg 是炮管的物理角（弹道、射界、tof 全按它），rad/deg/mil 是界面显示角
    （= elg - aOff）。某一支解不出角度就整支置 None，不留 ok:true 的空壳。
    """
    moa_rad = math.radians(w["moa"] / 60.0)
    a_off = math.radians(float(w.get("aOff") or 0.0))
    out = {"dist": dist, "dh": dh, "v": float(w["v"]), "low": None, "high": None, "err": ""}
    if dist < 0.5:
        out["err"] = "炮位与目标重合"
        return out
    any_angle = False
    for key, sgn in (("low", -1.0), ("high", 1.0)):
        elg = angle_num(w, dist, dh, sgn, fast) if is_dec(w) else angle_vac(w, dist, dh, sgn)
        out[key] = None
        if elg is None or not math.isfinite(elg):
            continue
        any_angle = True
        o = {"key": key, "rad": 0.0, "elg": 0.0, "deg": 0.0, "mil": 0.0,
             "tof": 0.0, "ok": True, "why": "", "sprA": 0.0, "sprB": 0.0}
        out[key] = o
        el = elg - a_off
        o["elg"] = elg
        o["rad"] = el
        o["deg"] = math.degrees(el)
        o["mil"] = math.degrees(el) / (360.0 / 6400.0)
        lo, hi = w["minEl"]
        degp = math.degrees(elg)
        if degp < lo - 1e-9 or degp > hi + 1e-9:
            o["ok"] = False
            o["why"] = f"超出射界 {lo:g}–{hi:g}°"
            continue
        dtc = 0.08 if fast else 0.02
        fl = range_at(w, elg, dh, 0.0, dtc)
        if not fl:
            o["ok"] = False
            o["why"] = "弹道未落地"
            continue
        o["tof"] = fl["tof"]
        if not math.isfinite(o["tof"]) or o["tof"] > float(w.get("life") or 100):
            o["ok"] = False
            o["why"] = "弹丸寿命外"
        o["sprA"] = max(0.0, math.sin(moa_rad) * float(w["v"]) * o["tof"])
        f1 = range_at(w, elg + moa_rad / 2.0, dh, 0.0, dtc)
        f2 = range_at(w, elg - moa_rad / 2.0, dh, 0.0, dtc)
        o["sprB"] = abs(f1["r"] - f2["r"]) if (f1 and f2) else 0.0
    if not any_angle:
        out["err"] = "超出射程"
    return out


ARC_MODES = ('auto', 'low', 'high')


def evaluate(w: dict, gun_rel: tuple[float, float], tgt_rel: tuple[float, float],
             h_gun: float, h_tgt: float, arc: str = 'auto') -> dict:
    """gun_rel / tgt_rel 是相对地图左上角的米数。

    arc 决定用哪一支弹道解：
      'auto' —— 跟着武器自己的习惯走（w['ang']：迫击炮高抛、平射武器低抛），跟 1.11 及以前一样；
      'low' / 'high' —— 强制低抛 / 高抛。强制的那一支打不了（超射界、弹丸寿命外、
      弹道无解）就直接报错并说明另一端有没有解，**不会偷偷换成另一端**——
      玩家点了低抛却拿到高抛的密位，比报个错危险得多。
    """
    arc = arc if arc in ARC_MODES else 'auto'
    res = {"dist": 0.0, "dh": 0.0, "hGun": h_gun, "hTgt": h_tgt,
           "brg": 0.0, "milBrg": 0.0, "sol": None, "solName": "", "err": "", "warnMin": False}
    dx = tgt_rel[0] - gun_rel[0]
    dz = tgt_rel[1] - gun_rel[1]
    dist = math.hypot(dx, dz)
    res["dist"] = dist
    res["dh"] = h_tgt - h_gun
    brg = math.degrees(math.atan2(dx, -dz))
    if brg < 0:
        brg += 360
    res["brg"] = brg
    res["milBrg"] = brg / (360.0 / 6400.0)
    if dist < 1:
        res["err"] = "与阵地重合"
        return res
    sol = solve(w, dist, res["dh"])
    if arc in ("low", "high"):
        order = (arc,)
    else:
        order = ("low", "high") if w.get("ang") == "low" else ("high", "low")
    pick, last = None, None
    for key in order:
        o = sol.get(key)
        last = o
        if o and o["ok"]:
            pick = o
            break
    if pick is None:
        why = sol.get("err") or (last or {}).get("why") or "无有效解"
        if arc in ("low", "high"):
            other_key = "high" if arc == "low" else "low"
            other = sol.get(other_key)
            if other and other["ok"]:
                res["alt"] = other
                why = (f"{'低抛' if arc == 'low' else '高抛'}弹道打不了（{why}）"
                       f" · 这组距离只有{'高抛' if arc == 'low' else '低抛'}解")
                # 浮窗那行小字只有 22 个字的宽度，写短版
                res["errShort"] = (f"{'低抛' if arc == 'low' else '高抛'}打不了 · 只有"
                                   f"{'高抛' if arc == 'low' else '低抛'}解")
        res["err"] = why
        return res
    res["sol"] = pick
    res["solName"] = "高角" if pick["key"] == "high" else "低角"
    res["warnMin"] = dist < (w.get("minD") or 0)
    return res


def angle_text(w: dict, deg: float) -> str:
    """角度的文字形式——一律保留 2 位小数（1.11.0 起；以前显示 1 位）。"""
    if w.get("unit") == "mil":
        return f"{deg / (360.0 / 6400.0):.2f} mil"
    if w.get("unit") == "degMin":
        sign = "-" if deg < 0 else ""
        a = abs(deg)
        dd = int(a // 1)
        mm = round((a - dd) * 60.0, 2)
        if mm >= 60.0:
            dd += 1
            mm = 0.0
        return f"{sign}{dd}°{mm:05.2f}′"
    return f"{deg:.2f}°"


def angle_label(w: dict) -> str:
    """诸元里角度那一行的标签：mil 的炮写「密位」，deg / degMin 的（火箭巢、
    车载炮、榴弹发射器）写「射角」——面板和浮窗都跟着武器走。"""
    return "密位" if w.get("unit") == "mil" else "射角"


def angle_display(w: dict, sol: dict) -> str:
    """诸元里角度那一行的数值：mil 写「1326.42 mil」，射角写「12.34°」/「12°34.12′」。"""
    if w.get("unit") == "mil":
        return f"{sol['mil']:.2f} mil"
    return angle_text(w, sol["deg"])
