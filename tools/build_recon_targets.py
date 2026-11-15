# -*- coding: utf-8 -*-
"""从 squad-armor 的 Game_vehicles.json + SquadCalc_res 的 layers 生成侦察目标表。

输出 data/recon_targets.json：
  factions: [{id, name, label, cats: [{key, label, items: [{name, hp, ticket, type}]}]}]
  common:   {label, items: [{key, label}]}   # 阵营无关的目标（步兵 / 工事）

用法：python tools/build_recon_targets.py
数据只读，不改任何上游文件。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
OUT = PROJ / "data" / "recon_targets.json"
VEH = Path(r"D:\mil-ai\squad-armor\data\Game_vehicles.json")
LAYERS = Path(r"D:\mil-ai\SquadCalc_res\layers")

# 车种 → (分类键, 中文分类名, 排序权重)
CAT_OF = {
    "MBT": ("MBT", "主战坦克", 10),
    "IFV": ("IFV", "步战车", 20),
    "APC": ("APC", "装甲运兵车", 30),
    "TD": ("TD", "反坦克车", 40),
    "SPA": ("SPA", "自行火炮", 50),
    "SPAA": ("SPAA", "防空车", 60),
    "MGS": ("LIGHT", "轻型火力车", 70),
    "LTV": ("LIGHT", "轻型火力车", 70),
    "MRAP": ("LIGHT", "轻型火力车", 70),
    "MSV": ("LIGHT", "轻型火力车", 70),
    "RSV": ("LIGHT", "轻型火力车", 70),
    "AH": ("AIR", "直升机", 80),
    "UH": ("AIR", "直升机", 80),
    "TRAN": ("LOG", "运输/后勤", 90),
    "LOGI": ("LOG", "运输/后勤", 90),
    "ULTV": ("LOG", "运输/后勤", 90),
}

# 车表里没有、但战场上要报的目标（与阵营无关）
COMMON = {
    "label": "通用目标",
    "items": [
        {"key": "INF", "label": "步兵小队"},
        {"key": "FOB", "label": "敌军 FOB / 集结地"},
        {"key": "MORTAR", "label": "迫击炮阵地"},
        {"key": "TOW", "label": "反坦克导弹阵地"},
        {"key": "HMG", "label": "重机枪阵地"},
        {"key": "REPAIR", "label": "维修站 / 补给点"},
        {"key": "UAV", "label": "敌方无人机 / 观察哨"},
    ],
}

# 阵营中文别名（车表 id → 玩家口头叫法）
CN = {
    "USA": "美军 USA", "USMC": "美陆战队 USMC", "BAF": "英军 BAF", "CAF": "加军 CAF",
    "ADF": "澳军 ADF", "RGF": "俄军陆军 RGF", "VDV": "俄空降 VDV", "PLA": "解放军 PLA",
    "PLAAGF": "陆战合成 PLAAGF", "PLANMC": "海军陆战 PLANMC", "TLF": "土军 TLF",
    "IMF": "民兵 IMF", "MEI": "中东叛乱 MEI", "GFI": "伊朗 GFI", "AFU": "乌军 AFU",
    "CRF": "加反抗军 CRF", "WPMC": "雇佣兵 WPMC", "OpFor": "旧版叛乱 OpFor",
}
# 同一阵营的合作版 id 合并
MERGE = {"WPMC_CoOp": "WPMC", "USMC_Coop": "USMC"}


def faction_names() -> dict:
    names = {}
    for mod_dir in LAYERS.iterdir():
        if not mod_dir.is_dir():
            continue
        for fp in mod_dir.glob("*.json"):
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            for team in ("team1Units", "team2Units"):
                for u in (d.get("units") or {}).get(team) or []:
                    fid = u.get("factionID")
                    if fid and fid not in names:
                        names[fid] = u.get("factionName") or u.get("displayName") or fid
    return names


def clean(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def main() -> int:
    if not VEH.exists():
        print(f"找不到车表：{VEH}")
        return 1
    data = json.loads(VEH.read_text(encoding="utf-8"))
    veh = data["vehicles"]
    names = faction_names()

    buckets = {}
    for key, v in veh.items():
        typ = v.get("type") or ""
        cat = CAT_OF.get(typ)
        if not cat:
            continue
        name = clean(v.get("displayName") or key)
        if not name:
            continue
        for fid in v.get("factions") or []:
            fid = MERGE.get(fid, fid)
            b = buckets.setdefault(fid, {})
            slot = b.setdefault(cat[0], {"label": cat[1], "order": cat[2], "items": {}})
            old = slot["items"].get(name)
            hp = int(v.get("vehicleHealth") or 0)
            tk = int(v.get("ticketValue") or 0)
            if old is None or hp > old["hp"]:
                slot["items"][name] = {"name": name, "hp": hp, "ticket": tk, "type": typ}

    factions = []
    for fid in sorted(buckets, key=lambda f: (CN.get(f, f))):
        cats = []
        for key, slot in buckets[fid].items():
            items = sorted(slot["items"].values(), key=lambda it: (it["name"].lower(), -it["hp"]))
            cats.append({"key": key, "label": slot["label"], "order": slot["order"], "items": items})
        cats.sort(key=lambda c: c["order"])
        for c in cats:
            c.pop("order", None)
        factions.append({
            "id": fid,
            "name": names.get(fid, fid),
            "label": CN.get(fid, fid),
            "cats": cats,
        })

    out = {
        "version": 1,
        "source": "squad-armor/data/Game_vehicles.json + SquadCalc_res/layers",
        "factions": factions,
        "common": COMMON,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写出 {OUT}（{OUT.stat().st_size} 字节）")
    print(f"阵营 {len(factions)} 个")
    for f in factions:
        cnt = sum(len(c["items"]) for c in f["cats"])
        print(f"  {f['id']:<10} {f['label']:<16} 分类 {len(f['cats'])} 目标 {cnt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
