# -*- coding: utf-8 -*-
r"""构建弹道遮挡数据包：把 Squad 静态关卡物体（建筑/桥梁/围墙/岩石…）压成
紧凑的 AABB(+yaw) 列表，写入 data/los/<slug>.npz 与 build/datapack/los/<slug>.npz。

数据来源：F:\FModel\SquadAsset\maps\<slug>\<slug>_struct.npz（cm->m 已过滤）
坐标：app 相对坐标 = world - (minx, minz)，无翻转（用 4000 个结构的 minz 与
地形高差验证过：71% 落在 ±1 m 内）。

**高度用「相对地面」存储**，不带绝对基准：实测 app 的高程场与真实世界高程
是同一起伏、只差一个逐图常数（拟合斜率 a≈1.00，b=+66/-…），所以
「盒顶离地多少米」两边可以直传，运行时再叠到 map.h_at 上 → 与解算同一基准，
也不怕以后地图库换标定。

每图字段（小端、行对齐）：
  cx, cy  uint16 中心相对 (minx,minz)，单位 0.1 m
  top     uint16 盒顶离地高，单位 0.1 m（>=0，<=6553.5）
  bot     uint8  盒底离地高，单位 0.5 m（负值夹到 0）
  hx, hy  uint8  半长，单位 0.25 m（1..255 → 0.25..63.75 m）
  yaw     uint8  偏航，单位 180/256 度（盒对 180° 旋转对称）
  kind    uint8  类别码（见 KINDS）

树木单独存一组带 t 前缀的数组（tx/ty/ttop/tbot/thx/thy/tyaw/tkind），
和结构盒放在同一个 npz 里：
  - 结构盒（SOLID）参与遮挡 + 阵地/目标脚下高度；
  - 树（kind=tree/bush）**只参与遮挡**，不参与脚下高度（树干细、树冠不能站人）。
树盒是整棵树的包围盒（含树冠，所以盒顶 = 树顶尖），盒底一般就在地面附近。
"""
import json
import os
import re

import numpy as np
from PIL import Image

APP = r"D:\mil-ai\IFC\ArtyCalc"
SRC = r"F:\FModel\SquadAsset\maps"
OUTS = [os.path.join(APP, "data", "los"),
        os.path.join(APP, "build", "datapack", "los")]

# 类别码（0 保留给 other）
KINDS = ["other", "building", "house", "wall", "bridge", "tower", "container",
         "rock", "cliff", "roof", "ruin", "gate", "barrier", "pipe", "crate",
         "hut", "stair", "fence", "tree", "bush", "vehicle", "prop", "rubble",
         "sandbag"]
CODE = {name: i for i, name in enumerate(KINDS)}

SOLID = {"building", "house", "hut", "tower", "bridge", "wall", "gate", "roof",
         "ruin", "container", "barrier", "pipe", "crate", "cliff", "rock"}

TREE = ("tree", "bush")          # 树木：只参与遮挡，不参与脚下高度
TREE_MIN_TOP = 2.0               # 不到 2 m 高的树丛挡不住什么，别塞进包里
TREE_MAX_TOP = 45.0              # 再高就不是树的盒子了（远景山体已被单独过滤）

STRUCT_NAME = re.compile(
    r"bld|urbres|urbcnt|garrag|garage|house|compound|tent|shed|hangar|warehouse|"
    r"bunker|tower|bridge|wall|roof|apart|shop|school|mosque|market|factory|"
    r"container|block|store|barrack|office|hall|silo|tank|chimney|pillbox|sangar|"
    r"checkpoint|gate", re.I)
VEG_NAME = re.compile(
    r"tree|bush|shrub|foliage|palm|reed|crop|wheat|corn|grass|flower|plant|fern|"
    r"bamboo|leaf|canopy|vine|hedge|stump|root|pine|oak|birch|poplar|tamarix|"
    r"salix|olive|cypress|maple|willow|acacia|mangrove|moss|ivy|weed|spruce|"
    r"beech|siberian|nettle|grape|rice|cattail|popp|berry|fruit|bark|branch|"
    r"forest|thuja|fir_|fir0|log_pile|haybale|hay_|straw", re.I)

Q_XY = 0.1
Q_TOP = 0.1
Q_BOT = 0.5
Q_H = 0.25


def quant(a, q, lo, hi):
    return np.clip(np.rint(a / q), lo, hi).astype(np.int32)


def build_one(slug, meta, dry=False):
    d = os.path.join(SRC, slug)
    zp = os.path.join(d, slug + "_struct.npz")
    ep = os.path.join(d, slug + "_elev.png")
    if not (os.path.isfile(zp) and os.path.isfile(ep)):
        return None
    z = np.load(zp)
    kinds = [str(x) for x in z["kinds"]]
    meshes = [str(x) for x in z["meshes"]]
    m = z["mesh"]
    kind_col = z["kind"]
    x = z["x"].astype(np.float64)
    y = z["y"].astype(np.float64)
    hx = z["hx"].astype(np.float64)
    hy = z["hy"].astype(np.float64)
    top = z["maxz"].astype(np.float64)
    bot = z["minz"].astype(np.float64)
    yaw = np.mod(z["yaw"].astype(np.float64), 180.0)

    solid = np.zeros(len(kinds), bool)
    for i, kn in enumerate(kinds):
        if kn in SOLID:
            solid[i] = True
    veg = np.array([bool(VEG_NAME.search(s)) for s in meshes])
    st = np.array([bool(STRUCT_NAME.search(s)) for s in meshes])
    foot = (2 * hx) * (2 * hy)
    extra = (kind_col == 0) & st[m] & (~veg[m]) & (foot >= 25.0)
    keep = solid[kind_col] | extra

    # 真实高程场（F:\ 的 elev.png 是绝对世界高）
    em = json.load(open(os.path.join(d, slug + "_elev_meta.json"), encoding="utf-8"))
    q = np.asarray(Image.open(ep)).astype(np.float32)
    lo, hi = float(em["elevLo"]), float(em["elevHi"])
    E = lo + q / 65535.0 * (hi - lo)
    cellx = float(em["cellx"])
    minx, minz = float(em["minx"]), float(em["minz"])
    # F:\ 的网格 row0 = 最小 Y（meta 的 row0_is_min_y）
    fx = (x - minx) / cellx - 0.5
    fy = (y - minz) / cellx - 0.5
    xi = np.clip(np.rint(fx).astype(np.int32), 0, E.shape[1] - 1)
    yi = np.clip(np.rint(fy).astype(np.int32), 0, E.shape[0] - 1)
    grd = E[yi, xi]

    top_a = top - grd
    bot_a = np.maximum(0.0, bot - grd)
    keep &= (top_a >= 0.5) & (top_a <= 260.0) & ((top - bot) >= 1.2)
    keep &= (hx >= 0.4) & (hy >= 0.4) & (hx <= 60) & (hy <= 60)

    sel = np.where(keep)[0]

    # ---- 树木：单独一组（只参与遮挡，不参与脚下高度）
    name_row = np.array(kinds, dtype=object)[kind_col]
    tkeep = (np.isin(name_row, TREE)
             & (top_a >= TREE_MIN_TOP) & (top_a <= TREE_MAX_TOP)
             & ((top - bot) >= 1.5)
             & (hx >= 0.4) & (hy >= 0.4) & (hx <= 40) & (hy <= 40))
    tsel = np.where(tkeep)[0]
    tarr = dict(
        tx=quant(x[tsel] - meta["minx"], Q_XY, 0, 65535).astype(np.uint16),
        ty=quant(y[tsel] - meta["minz"], Q_XY, 0, 65535).astype(np.uint16),
        ttop=quant(top_a[tsel], Q_TOP, 1, 65535).astype(np.uint16),
        tbot=quant(bot_a[tsel], Q_BOT, 0, 255).astype(np.uint8),
        thx=quant(hx[tsel], Q_H, 1, 255).astype(np.uint8),
        thy=quant(hy[tsel], Q_H, 1, 255).astype(np.uint8),
        tyaw=np.clip(np.rint(yaw[tsel] / 180.0 * 256.0), 0, 255).astype(np.uint8),
        tkind=np.array([CODE.get(str(name_row[k]), 0) for k in tsel], np.uint8),
    )
    tarr["tbot"] = np.minimum(tarr["tbot"], (tarr["ttop"] * 0.1 / Q_BOT).astype(np.uint8))
    torder = np.argsort(tarr["tx"], kind="stable")
    tarr = {k: v[torder] for k, v in tarr.items()}

    if len(sel) == 0 and len(tsel) == 0:
        return 0, 0, 0
    arrays = dict(
        cx=quant(x[sel] - meta["minx"], Q_XY, 0, 65535).astype(np.uint16),
        cy=quant(y[sel] - meta["minz"], Q_XY, 0, 65535).astype(np.uint16),
        top=quant(top_a[sel], Q_TOP, 1, 65535).astype(np.uint16),
        bot=quant(bot_a[sel], Q_BOT, 0, 255).astype(np.uint8),
        hx=quant(hx[sel], Q_H, 1, 255).astype(np.uint8),
        hy=quant(hy[sel], Q_H, 1, 255).astype(np.uint8),
        yaw=np.clip(np.rint(yaw[sel] / 180.0 * 256.0), 0, 255).astype(np.uint8),
        kind=np.array([CODE.get(kinds[int(k)], 0) for k in kind_col[sel]], np.uint8),
    )
    arrays["bot"] = np.minimum(arrays["bot"], (arrays["top"] * 0.1 / Q_BOT).astype(np.uint8))
    if dry:
        return len(sel), len(tsel), len(sel)
    order = np.argsort(arrays["cx"], kind="stable")
    arrays = {k: v[order] for k, v in arrays.items()}
    arrays.update(tarr)
    size = 0
    for od in OUTS:
        os.makedirs(od, exist_ok=True)
        np.savez_compressed(os.path.join(od, slug + ".npz"), **arrays)
        size = os.path.getsize(os.path.join(od, slug + ".npz"))
    return len(sel), len(tsel), size


def main():
    maps = json.load(open(os.path.join(APP, "data", "maps.json"), encoding="utf-8"))
    meta = {mm["slug"]: mm for mm in maps}
    tot_n = 0
    tot_t = 0
    tot_b = 0
    for slug in sorted(os.listdir(SRC)):
        if slug not in meta:
            continue
        r = build_one(slug, meta[slug])
        if r is None:
            continue
        n, tn, b = r
        tot_n += n
        tot_t += tn
        tot_b += b
        print("%-14s %7d boxes + %7d trees %8.1f KB" % (slug, n, tn, b / 1024.0))
    print("TOTAL %d boxes + %d trees, %.2f MB (one copy)" % (tot_n, tot_t, tot_b / 1e6))


if __name__ == "__main__":
    main()
