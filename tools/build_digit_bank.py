# -*- coding: utf-8 -*-
r"""build_digit_bank.py —— 从人工标注的方位角截图生成种子模板库（双尺度）

用法：
    python build_digit_bank.py            # 生成 ../../data/digit_bank_seed.npz
    python build_digit_bank.py --out X.npz
"""
import argparse
import os
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import docr  # noqa: E402

# 标注：目录, 文件名（不含扩展名）, 图上的三位数字
SAMPLES = [
    (r"D:\mil-ai\IFC\player-grid\NUMBER", "000", "000"),
    (r"D:\mil-ai\IFC\player-grid\NUMBER", "123", "123"),
    (r"D:\mil-ai\IFC\player-grid\NUMBER", "267", "267"),
    (r"D:\mil-ai\IFC\player-grid\NUMBER", "289", "289"),
    (r"D:\mil-ai\IFC\player-grid\NUMBER", "345", "345"),
    (r"D:\mil-ai\IFC\angle", "Snipaste_2026-11-04_12-57-15", "269"),
    (r"D:\mil-ai\IFC\angle", "Snipaste_2026-11-04_12-59-01", "180"),
    (r"D:\mil-ai\IFC\angle", "Snipaste_2026-11-04_12-59-34", "051"),
    (r"D:\mil-ai\IFC\angle", "Snipaste_2026-11-04_12-59-50", "122"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE.parent / "data" / "digit_bank_seed.npz"))
    a = ap.parse_args()

    bank = docr.Bank(Path(a.out))
    added = 0
    for folder, base, label in SAMPLES:
        p = os.path.join(folder, base + ".jpg")
        if not os.path.exists(p):
            print("missing", p)
            continue
        im = Image.open(p)
        line = [f"{base} -> {label}"]
        for sc in docr.SCALES:
            gl = docr.glyphs(im, sc)
            line.append(f"scale{sc}:{len(gl)}字")
            if len(gl) != 3:
                continue
            for c, g in zip(label, gl):
                if bank.add(c, g, sc):
                    added += 1
        print("  ".join(line))
    bank.save()
    per = {}
    for (sc, d), ts in bank.tpl.items():
        per.setdefault(sc, {}).setdefault(d, 0)
        per[sc][d] += len(ts)
    print(f"seed: {bank.count()} templates (added {added}) -> {a.out}  "
          f"({Path(a.out).stat().st_size} bytes)")
    for sc, d in sorted(per.items()):
        print(f"  scale {sc}: {d}")


if __name__ == "__main__":
    main()
