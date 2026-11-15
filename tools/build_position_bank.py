# -*- coding: utf-8 -*-
r"""build_position_bank.py —— 从人工标注的玩家坐标截图生成种子模板库

样本目录里的文件名就是标注，例如 "A1 - 4 - 1.jpg" → A1-4-1，"I10 - 9 - 5.jpg" → I10-9-5。

用法：
    python build_position_bank.py                  # -> ../../data/position_bank.npz
    python build_position_bank.py --src D:\some   # 换样本目录
    python build_position_bank.py --out X.npz
"""
import argparse
import glob
import os
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import posocr  # noqa: E402

DEFAULT_SRC = r"D:\mil-ai\IFC\grid-position"


def label_of(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return "-".join(p.strip() for p in base.split("-"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--out", default=str(HERE.parent / "data" / "position_bank.npz"))
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.src, "*.jpg")) + glob.glob(os.path.join(a.src, "*.png")))
    if not files:
        raise SystemExit(f"没有样本：{a.src}")

    bank = posocr.Bank(Path(a.out))
    added = 0
    for f in files:
        label = label_of(f)
        im = Image.open(f).convert("RGB")
        n0 = bank.count()
        added += posocr.learn(label, im, bank)
        print(f"{os.path.basename(f):24s} -> {label:10s} 字形 +{bank.count() - n0}")
    bank.save()
    per = {}
    for (sc, sym), ts in bank.tpl.items():
        per.setdefault(sc, {})[sym] = len(ts)
    print(f"seed: {bank.count()} templates (added {added}) -> {a.out}  "
          f"({Path(a.out).stat().st_size} bytes)")
    for sc in sorted(per):
        syms = "".join(sorted(per[sc]))
        print(f"  scale {sc}: {len(per[sc])} 种字符 [{syms}]")


if __name__ == "__main__":
    main()
