# -*- coding: utf-8 -*-
"""Generate data/command_factions.json from the squad-armor bundle.

Sources:
  D:\mil-ai\squad-armor\app\index-X2BtDX65.js   (unit setups + deployable definitions)
  D:\mil-ai\SquadCalc_res\layers\**\*.json       (verification only)
  data\recon_targets.json                        (Chinese faction labels)

Run:  python tools\build_command_factions.py
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = Path(r"D:\mil-ai\squad-armor\app\index-X2BtDX65.js")
OUT = ROOT / "data" / "command_factions.json"


def _js_string_end(s: str, start: int, quote: str) -> int:
    """Return index of the closing quote of a JS string starting at start."""
    i = start
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == quote:
            return i
        i += 1
    raise ValueError("unterminated string")


def _parse_js_json(s: str, anchor: str) -> dict:
    """Parse a JSON blob embedded as JSON.parse('...') or JSON.parse(`...`)."""
    i = s.find(anchor)
    if i < 0:
        raise ValueError(f"anchor not found: {anchor}")
    p = s.rfind("JSON.parse(", 0, i)
    if p < 0:
        raise ValueError("JSON.parse not found")
    q = s[p + len("JSON.parse(")]
    if q not in ("'", "`"):
        raise ValueError(f"unexpected quote {q!r} at {p}")
    j = p + len("JSON.parse(") + 1
    k = _js_string_end(s, j, q)
    raw = s[j:k]
    if q == "'":
        raw = raw.replace("\\'", "'")
    return json.loads(raw)


TYPE_CN = {
    "Combined Arms": "合成兵种",
    "Armored": "装甲",
    "Support": "支援",
    "Support (MLRS)": "支援（火箭炮）",
    "Mechanized": "机械化",
    "Mechanized (Wheeled)": "轮式机械化",
    "Mechanized (Wheeled MGS)": "轮式机械化（突击炮）",
    "Mechanized (Wheeled + MGS)": "轮式机械化（突击炮）",
    "Mechanized (Wheeled + Amphibious)": "轮式两栖机械化",
    "Mechanized (Wheeled Amphibious)": "轮式两栖机械化",
    "Mechanized (MGS)": "机械化（突击炮）",
    "Mechanized (Amphibious)": "两栖机械化",
    "Mechanized (Amphibious + MGS)": "两栖机械化（突击炮）",
    "Light Infantry": "轻步兵",
    "Mountain Infantry": "山地步兵",
    "Recon Infantry": "侦察步兵",
    "Armored Recon (Wheeled)": "轮式装甲侦察",
    "Motorized": "摩托化",
    "Special Forces": "特种部队",
    "Air Assault": "空中突击",
    "Infantry (Air Mobile)": "空中机动步兵",
}

TYPE_ORDER = [
    "Combined Arms", "Armored", "Mechanized", "Mechanized (Wheeled)",
    "Mechanized (Wheeled MGS)", "Mechanized (Wheeled + MGS)",
    "Mechanized (MGS)", "Mechanized (Amphibious)",
    "Mechanized (Amphibious + MGS)", "Mechanized (Wheeled + Amphibious)",
    "Mechanized (Wheeled Amphibious)",
    "Light Infantry", "Mountain Infantry", "Recon Infantry",
    "Motorized", "Air Assault", "Infantry (Air Mobile)",
    "Armored Recon (Wheeled)", "Special Forces", "Support", "Support (MLRS)",
]

# displayName -> 中文名（UI 里直接显示；句子里的「建造 X」取 short）
DN_CN = {
    "HASCO Bunker": "HESCO 掩体",
    "HASCO Block": "HESCO 墙块",
    "HASCO Wall": "HESCO 长墙",
    "Indirect Fire Shelter": "防炮掩体",
    "Sandbag Line": "沙袋墙",
    "Pillbox Sandbag Line": "沙袋碉堡墙",
    "Sandbag Crescent": "沙袋弧形墙",
    "Ladder": "梯子",
    "Razor Wire": "铁丝网",
    "HAB (Spawn Bunker)": "兵站（屯兵所）",
    "Ammo Crate": "弹药箱",
    "Repair Station": "维修站",
    "BGM-71 TOW Missile": "TOW 反坦克导弹",
    "M2A1 HMG Tripod": "M2A1 重机枪（三脚架）",
    "M2HB HMG Bunker": "M2HB 重机枪（掩体）",
    "Observation Tower": "观察塔",
    "Dirt-Filled Oil Barrels": "油桶掩体",
    "ZU-23-2 AAA Emplacement": "ZU-23-2 高射炮",
    "SPG-9 Recoilless AT Gun": "SPG-9 无后坐力炮",
    "DShK HMG Tripod": "DShK 重机枪（三脚架）",
    "DShK HMG (Shielded)": "DShK 重机枪（带盾）",
    "M1937 Mortar": "M1937 迫击炮",
    "Tire Fire": "轮胎火堆",
    "Hell Cannon": "地狱大炮",
    "M2A1 Ironsight HMG Tripod": "M2A1 重机枪（机械瞄具）",
    "Mk19 AGL Tripod": "Mk19 榴弹发射器（三脚架）",
    "Mk19 AGL Bunker": "Mk19 榴弹发射器（掩体）",
    "NSV HMG Tripod": "NSV 重机枪（三脚架）",
    "NSV HMG Bunker": "NSV 重机枪（掩体）",
    "9M133 Kornet Missile": "短号反坦克导弹",
    "2B14 Podnos Mortar": "2B14 迫击炮",
    "AGS17 AGL Tripod": "AGS-17 榴弹发射器（三脚架）",
    "M2A1 Ironsight HMG Bunker": "M2A1 重机枪（掩体）",
    "M2HB HMG Tripod": "M2HB 重机枪（三脚架）",
    "Milan AT Missile": "米兰反坦克导弹",
    "AGS17 AGL Bunker": "AGS-17 榴弹发射器（掩体）",
    "QJZ-89 HMG Tripod": "QJZ-89 重机枪（三脚架）",
    "QJZ-89 HMG Bunker": "QJZ-89 重机枪（掩体）",
    "HJ-8 ATGM": "红箭-8 反坦克导弹",
    "PP-87 Mortar": "PP-87 迫击炮",
    "QLZ-87 AGL Tripod": "QLZ-87 榴弹发射器（三脚架）",
    "QLZ-87 AGL Bunker": "QLZ-87 榴弹发射器（掩体）",
    "Log Wall": "原木墙",
    "Dirt Crates Low Wall": "土箱矮墙",
    "Dirt Crates High Wall": "土箱高墙",
    "Dirt Crates with Firing Ports": "土箱射击孔墙",
    "HedgeHog Tank Trap": "反坦克拒马",
    "Camo Net Wall": "伪装网墙",
    "Camo Net Lean-To": "伪装网斜棚",
    "Camo Net Canopy": "伪装网顶棚",
    "Hull Down": "掩体坑",
    "M2A1 HMG Tripod (Shielded)": "M2A1 重机枪（带盾）",
    "Zis3 76mm Field Gun": "Zis3 76mm 野炮",
    "UB-32 Rocket Artillery": "UB-32 火箭炮",
    "81mm Mortar": "81mm 迫击炮",
    "C16 AGL Tripod": "C16 榴弹发射器（三脚架）",
    "L16 Mortar": "L16 迫击炮",
    "L134A1 AGL Tripod": "L134A1 榴弹发射器（三脚架）",
    "Stugna-P Anti-Tank Guided Missle": "斯图格纳-P 反坦克导弹",
    "M252 Mortar": "M252 迫击炮",
}


def load_faction_labels() -> dict:
    p = ROOT / "data" / "recon_targets.json"
    labels = {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        for f in d.get("factions") or []:
            lab = str(f.get("label") or f.get("name") or f.get("id") or "")
            fid = str(f.get("id") or "")
            if fid and lab.endswith(" " + fid):
                lab = lab[: -(len(fid) + 1)]
            labels[fid] = lab or fid
    except Exception as e:  # noqa: BLE001
        print("recon_targets.json 读取失败：", e)
    return labels


def item_of(key: str, dep: dict, avail: dict, order_no: int) -> dict:
    dn = str(dep.get("displayName") or key)
    cn = DN_CN.get(dn) or dn
    short = re.sub(r"（.*?）", "", cn) or cn
    return OrderedDict((
        ("key", key),
        ("name", dn),
        ("cn", cn),
        ("short", short),
        ("cost", avail.get("cost")),
        ("limit", avail.get("availability")),
        ("no", order_no),
    ))


def main() -> None:
    s = BUNDLE.read_text(encoding="utf-8", errors="ignore")
    units = _parse_js_json(s, '"WPMC_LobbyFactionSetup"')
    deps = _parse_js_json(s, '"category":"Fortifications"')
    labels = load_faction_labels()

    order = {"HAB": 0, "AmmoCrate": 1, "RepairStation": 2}
    fac_order = ["USA", "USMC", "BAF", "CAF", "ADF", "RGF", "VDV", "PLA",
                 "PLAAGF", "PLANMC", "TLF", "IMF", "MEI", "GFI", "AFU",
                 "CRF", "WPMC"]

    grouped: dict[str, list] = {}
    missing = set()
    for key, u in units.items():
        if "_LO_" not in key:
            continue
        fid = key.split("_", 1)[0]
        if fid == "CIV":
            continue
        dep = u.get("deployables") or {}
        if not dep:
            continue
        func, fort, weap = [], [], []
        for dk, dv in dep.items():
            d = deps.get(dk)
            if not d:
                missing.add(dk)
                continue
            cat = d.get("category")
            if cat == "Tech Structures":
                pre = dk.split("_", 1)[0]
                if pre in order:
                    func.append(item_of(dk, d, dv, order[pre]))
            elif cat == "Fortifications":
                fort.append(item_of(dk, d, dv, len(fort)))
            elif cat == "Weapons":
                weap.append(item_of(dk, d, dv, len(weap)))
        if not (func or fort or weap):
            continue
        func.sort(key=lambda x: x["no"])
        typ = str(u.get("type") or "")
        grouped.setdefault(fid, []).append(OrderedDict((
            ("key", key),
            ("name", str(u.get("displayName") or key)),
            ("type", typ),
            ("type_cn", TYPE_CN.get(typ, typ)),
            ("functional", func),
            ("fortifications", fort),
            ("weapons", weap),
        )))

    factions = []
    ids = [f for f in fac_order if f in grouped] + sorted(f for f in grouped if f not in fac_order)
    for fid in ids:
        us = grouped[fid]
        us.sort(key=lambda x: (TYPE_ORDER.index(x["type"]) if x["type"] in TYPE_ORDER else 99,
                               x["name"]))
        factions.append(OrderedDict((
            ("id", fid),
            ("label", labels.get(fid, fid)),
            ("units", us),
        )))

    out = OrderedDict((
        ("version", 1),
        ("source", "squad-armor app bundle + SquadCalc_res layers + data/recon_targets.json"),
        ("note", "我方阵营 → 部队（LO 编制）→ 可建造的功能建筑 / 防御工事 / 武器"),
        ("factions", factions),
    ))
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    n_units = sum(len(f["units"]) for f in factions)
    n_items = sum(len(u["functional"]) + len(u["fortifications"]) + len(u["weapons"])
                  for f in factions for u in f["units"])
    print(f"写出 {OUT}")
    print(f"阵营 {len(factions)} · 部队 {n_units} · 建造项 {n_items}")
    if missing:
        print("缺少定义：", sorted(missing))
    for f in factions:
        print(f"  {f['id']:7} {f['label']:12} 部队 {len(f['units'])}")


if __name__ == "__main__":
    main()
