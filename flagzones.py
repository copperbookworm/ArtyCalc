# -*- coding: utf-8 -*-
r"""旗点 · 占领区数据（把 Yehorivka-Passability 0.50.0 的「旗点占领区」搬过来）。

数据由 tools/build_flags.py 从 APK 的地图页（assets/maps/<slug>.html）里的
FLAG_DB / FLAG_CN 抽出，落在 data/flags.json：

    {"format": 1, "maps": {slug: {"layers": {层名: FLAG_DB项}, "cn": {英文: 中文}}}}

FLAG_DB 项：{"m": 模式, "v": 版本, "t": "aas"|"raas"|"inv", "c": [旗点...], "l": 进攻链}
旗点 c 的 7 个字段：
    [0] 英文名  [1] 序号-英文名  [2] 世界 x（米）  [3] 世界 z（米）
    [4] 占领区形状列表  [5] 序号  [6] 是否主基地
形状：圆 [0, r]；矩形 [1, w, h, rot, dx, dz]；胶囊 [2, len2, rad, rot, dx, dz]
（长度米、角度顺时针为正、dx/dz 是相对旗点中心的偏移）。

坐标换算：旗点坐标与 maps.json 的 minx/minz 同一世界系（UE 厘米/100），
交给地图显示前要减掉 minx/minz 才是「相对地图左上角的米数」——
本模块只负责原样返回，换算在 MapView 里做（见 MapView.rel_of_flag）。

旗点列表的拼装规则（RAAS 全部链 / RAAS 单链 / AAS / Invasion 四套）逐行照抄
APK 里的 flagItems()，所以两边看到的序号、链位字母、合并方式完全一致。
"""
from __future__ import annotations

import json
import math

import paths

DATA = paths.resource_dir() / "data"

ALL = "ALL"                     # 进攻链下拉里的「全部旗点」
MODES = ("AAS", "RAAS", "Invasion")
MODE_CN = {"AAS": "AAS 进攻", "RAAS": "RAAS 随机链", "Invasion": "Invasion 入侵"}
LANE_COL = ("#ffd166", "#8ecae6", "#f4978e", "#a3c9a8",
            "#c7a3d9", "#e0b76a", "#7fd1c1", "#d98cb3")

_DB = None
_CACHE: dict = {}


# ---------------------------------------------------------------- 读库
def _db() -> dict:
    global _DB
    if _DB is None:
        try:
            raw = (DATA / "flags.json").read_text(encoding="utf-8")
            _DB = json.loads(raw).get("maps") or {}
        except Exception as e:                       # noqa: BLE001
            print(f"[flags] 读不出旗点数据（{DATA / 'flags.json'}）：{e}", flush=True)
            _DB = {}
    return _DB


def reload():
    """换过数据文件后重新读（测试用）。"""
    global _DB
    _DB = None
    _CACHE.clear()


def info() -> str:
    maps = _db()
    n = 0
    for m in maps.values():
        for L in (m.get("layers") or {}).values():
            n += len(L.get("c") or [])
    return f"旗点数据 · {len(maps)} 张图 · {n} 个旗点"


def has(slug: str) -> bool:
    """这张图有没有旗点图层（jensen 那种空 FLAG_DB 返回 False）。"""
    return bool(((_db().get(slug) or {}).get("layers")))


def _layers(slug: str) -> dict:
    return ((_db().get(slug) or {}).get("layers")) or {}


def layers(slug: str) -> list[str]:
    """本图所有图层，按 AAS → RAAS → Invasion、再按版本号排（和 APK 一致）。"""
    m = _layers(slug)

    def order(k):
        L = m[k]
        mode = str(L.get("m") or "")
        i = MODES.index(mode) if mode in MODES else len(MODES)
        return (i, int(L.get("v") or 0), k)

    return sorted(m, key=order)


def layer_info(slug: str, key: str):
    """下拉里显示的一行：{key, mode, v, t, n, label}。没有这个层返回 None。"""
    L = _layers(slug).get(key)
    if not L:
        return None
    mode = str(L.get("m") or "")
    n = len(L.get("c") or [])
    kind = "个链位点" if L.get("t") == "raas" else "点"
    return {"key": key, "mode": mode, "v": int(L.get("v") or 0),
            "t": str(L.get("t") or ""), "n": n,
            "label": f"{MODE_CN.get(mode, mode)} · v{L.get('v') or 0}（{n} {kind}）"}


def default_layer(slug: str):
    ls = layers(slug)
    return ls[0] if ls else None


def is_raas(slug: str, key: str) -> bool:
    L = _layers(slug).get(key)
    return bool(L) and str(L.get("t") or "") == "raas"


def lanes(slug: str, key: str) -> list[str]:
    """RAAS 的进攻链名（Alpha / Bravo …）；不是 RAAS 返回空表。"""
    L = _layers(slug).get(key)
    if not L or str(L.get("t") or "") != "raas":
        return []
    return list(((L.get("l") or {}).get("names")) or [])


def lane_counts(slug: str, key: str) -> dict:
    """每条进攻链有几个节点（组），给下拉里的括号数字用。"""
    L = _layers(slug).get(key)
    if not L or str(L.get("t") or "") != "raas":
        return {}
    box = L.get("l") or {}
    order = box.get("order") or {}
    return {str(nm): len(order.get(nm) or []) for nm in (box.get("names") or [])}


def cn(slug: str, en: str) -> str:
    return ((_db().get(slug) or {}).get("cn") or {}).get(en) or en


# ---------------------------------------------------------------- 旗点列表
def _jr(v) -> int:
    """JS 的 Math.round（.5 往上，不是 Python 的银行家舍入）。"""
    return int(math.floor(float(v) + 0.5))


def tag_cmp(a: str, b: str) -> int:
    """先比首字母，再比后面的数字（「A2」在「A10」前），最后比字符串。"""
    la = (a[:1] or "").upper()
    lb = (b[:1] or "").upper()
    if la != lb:
        return -1 if la < lb else 1
    try:
        na = int("".join(ch for ch in a[1:] if ch.isdigit()) or 0)
    except ValueError:
        na = 0
    try:
        nb = int("".join(ch for ch in b[1:] if ch.isdigit()) or 0)
    except ValueError:
        nb = 0
    if na != nb:
        return na - nb
    return -1 if a < b else (1 if a > b else 0)


def lane_color(tag: str) -> str:
    ch = (tag[:1] or "A").upper()
    k = ord(ch) - 65
    if k < 0 or k >= len(LANE_COL):
        k = 0
    return LANE_COL[k]


def items(slug: str, key: str, lane: str = ALL) -> list[dict]:
    """当前图层（+ 进攻链）要画的旗点。每个 dict：

    k 序号 / name 英文名 / cn 中文名 / x,z 世界米 / shapes 占领区 /
    main 是否主基地 / seq 序号 / letter 备选字母 / tags 链位标签 / tag 主标签
    """
    ck = (slug, key, ALL if lane is None else lane)
    if ck in _CACHE:
        return _CACHE[ck]
    L = _layers(slug).get(key)
    out: list[dict] = []
    if L:
        cs = L.get("c") or []
        name_cn = lambda en: cn(slug, en)                # noqa: E731
        if str(L.get("t") or "") == "raas" and (lane == ALL or not lane):
            # 全部链：每条链第 j 个节点打「字母+序号」标签，同一坐标合并成一项
            order = ((L.get("l") or {}).get("order")) or {}
            names = ((L.get("l") or {}).get("names")) or []
            slots: dict = {}
            for ln_i, (lk, grp) in enumerate(order.items()):
                letter = str((names[ln_i] if ln_i < len(names) else lk) or "A")[:1].upper() or "A"
                for j, node in enumerate(grp or []):
                    for idx in node or []:
                        arr = slots.setdefault(int(idx), [])
                        tag = f"{letter}{j + 1}"
                        if tag not in arr:
                            arr.append(tag)
            merged: dict = {}
            order_out: list[dict] = []
            for i, c in enumerate(cs):
                if not c:
                    continue
                caps = slots.get(i)
                if not caps and not c[6]:
                    continue
                kk = f"{_jr(c[2])},{_jr(c[3])}"
                it = merged.get(kk)
                if it is None:
                    it = {"k": i, "name": str(c[0]), "cn": name_cn(str(c[0])),
                          "x": float(c[2]), "z": float(c[3]), "shapes": list(c[4] or []),
                          "main": 0, "seq": "", "letter": "", "tags": []}
                    merged[kk] = it
                    order_out.append(it)
                if c[6]:
                    it["main"] = 1
                    if "主" not in it["tags"]:
                        it["tags"].append("主")
                for tg in caps or []:
                    if tg not in it["tags"]:
                        it["tags"].append(tg)
            for it in order_out:
                it["tags"].sort(key=_cmp_key)
            order_out.sort(key=lambda a: _cmp_key(a["tags"][0] if a["tags"] else ""))
            for it in order_out:
                it["tag"] = it["tags"][0] if it["tags"] else ""
            out = order_out
        elif str(L.get("t") or "") == "raas":
            order = ((L.get("l") or {}).get("order") or {}).get(lane) or []
            for i, grp in enumerate(order):
                for j, k in enumerate(grp or []):
                    if k is None or not (0 <= int(k) < len(cs)):
                        continue
                    c = cs[int(k)]
                    letter = chr(97 + j) if len(grp) > 1 else ""
                    out.append({"k": int(k), "name": str(c[0]), "cn": name_cn(str(c[0])),
                                "x": float(c[2]), "z": float(c[3]), "shapes": list(c[4] or []),
                                "main": 0, "seq": i + 1, "letter": letter,
                                "tags": [], "tag": f"{i + 1}{letter}"})
        else:
            arr = []
            for i, c in enumerate(cs):
                arr.append({"k": i, "name": str(c[0]), "cn": name_cn(str(c[0])),
                            "x": float(c[2]), "z": float(c[3]), "shapes": list(c[4] or []),
                            "main": int(c[6] or 0), "ord": int(c[5] or 0), "tags": []})
            arr.sort(key=lambda a: (a["main"], a["ord"]))
            for it in arr:
                it["seq"] = 0 if it["main"] else it["ord"]
                it["letter"] = ""
                it["tag"] = "主" if it["main"] else str(it["seq"])
            out = arr
    _CACHE[ck] = out
    return out


class _Cmp:
    """tag_cmp 的 key 包装（字符串按 tag_cmp 排序）。"""
    __slots__ = ("s",)

    def __init__(self, s):
        self.s = s or ""

    def __lt__(self, other):
        return tag_cmp(self.s, other.s) < 0


def _cmp_key(s):
    return _Cmp(s)


# ---------------------------------------------------------------- 文案
def shape_text(sh) -> str:
    try:
        if int(sh[0]) == 0:
            return f"圆 Ø{_jr(float(sh[1]) * 2)} m"
        if int(sh[0]) == 1:
            return f"矩形 {_jr(sh[1])}×{_jr(sh[2])} m"
        return f"胶囊 {_jr(sh[1])}×{_jr(float(sh[2]) * 2)} m"
    except Exception:                                   # noqa: BLE001
        return "占领区"


def info_text(it: dict, all_lanes: bool = False, grid: str = "") -> str:
    head = "主基地" if it.get("main") else (it.get("tag") or "")
    s = f"旗点 {head} · {it.get('cn')}（{it.get('name')}）"
    tags = it.get("tags") or []
    if all_lanes and len(tags) > 1:
        s += f"（{len(tags)} 个进攻链位：{' / '.join(tags)}）"
    parts = [shape_text(sh) for sh in (it.get("shapes") or [])]
    s += "｜占领区：" + (" + ".join(parts) if parts else "无")
    s += f"｜世界坐标 {_jr(it.get('x'))} / {_jr(it.get('z'))}"
    if grid:
        s += f"｜{grid}"
    return s
