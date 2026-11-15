# -*- coding: utf-8 -*-
r"""selftest_position_ocr.py —— 坐标本地识别的回归自测（不需要游戏、不花 API）

用 D:\mil-ai\IFC\grid-position 里 26 张标注截图，分别在「紧裁」和「贴进真实截图框尺寸
（265×70，居中 / 四角）」两种情况下跑 posocr.read_pos，统计：

    认对 / 放弃（回落 AI）/ 认错

**认错数必须是 0** —— 这是 posocr 的设计红线：拿不准就放弃，交给大模型，
绝不能猜一个错的坐标（错的坐标会直接导致打错目标）。

用法：
    python tools/selftest_position_ocr.py
    python tools/selftest_position_ocr.py --src D:\some\dir --bank data\position_bank.npz
"""
import argparse
import glob
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import posocr  # noqa: E402

DEFAULT_SRC = r"D:\mil-ai\IFC\grid-position"


def label_of(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return "-".join(p.strip() for p in base.split("-"))


def pad(img: Image.Image, w: int = 265, h: int = 70, pos=None) -> Image.Image:
    """把紧裁的坐标图贴到 w×h 的画布上，模拟游戏里框出来的识别区。"""
    g = np.asarray(img.convert("L"))
    bg = int(np.median(g))
    canvas = Image.new("RGB", (w, h), (bg, bg, bg))
    if pos is None:
        pos = ((w - img.width) // 2, (h - img.height) // 2)
    canvas.paste(img, pos)
    return canvas


def run(tag: str, data, maker, bank) -> int:
    ok = decl = wrong = 0
    bad = []
    for path, want in data:
        img = maker(Image.open(path).convert("RGB"))
        txt, _conf, _info = posocr.read_pos(img, bank)
        if txt is None:
            decl += 1
        elif txt == want:
            ok += 1
        else:
            wrong += 1
            bad.append(f"{want} -> {txt}")
    print(f"{tag:24s} n={len(data):3d} 认对={ok:3d} 放弃={decl:3d} 认错={wrong:3d}")
    for b in bad:
        print(f"    !! {b}")
    return wrong


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--bank", default=str(HERE.parent / "data" / "position_bank.npz"))
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.src, "*.jpg")) + glob.glob(os.path.join(a.src, "*.png")))
    if not files:
        raise SystemExit(f"没有样本：{a.src}")
    data = [(f, label_of(f)) for f in files]

    bank = posocr.Bank(Path(a.bank)).load()
    if bank.count() == 0:
        raise SystemExit(f"模板库是空的：{a.bank}")
    print(f"样本 {len(data)} 张，模板库 {bank.count()} 条，尺度 {posocr.SCALES}")
    print(f"门槛 accept={posocr.ACCEPT} margin={posocr.MARGIN}\n")

    wrong = 0
    wrong += run("紧裁（原始）", data, lambda im: im, bank)
    wrong += run("265×70 居中", data, lambda im: pad(im), bank)
    wrong += run("265×70 左上角", data, lambda im: pad(im, pos=(4, 4)), bank)
    wrong += run("265×70 右下角", data,
                 lambda im: pad(im, pos=(265 - im.width - 4, 70 - im.height - 4)), bank)
    wrong += run("265×70 偏左中", data, lambda im: pad(im, pos=(10, 22)), bank)

    print()
    if wrong:
        print(f"失败：出现 {wrong} 例认错 —— posocr 宁可放弃也不能认错，请查")
        return 1
    print("通过：所有场景 0 认错（放弃的会回落大模型）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
