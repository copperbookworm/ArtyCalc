# -*- coding: utf-8 -*-
r"""eval_position_ocr.py —— 本地坐标识别（posocr）的离线评估

干净图 + 抖动/亮度/对比度 + 分辨率缩放，统计 ok/decl/WRONG，并给出速度。
"WRONG" 才是要命的指标：本地认错比本地放弃严重得多（放弃还有 AI 兜底）。

用法：
    python eval_position_ocr.py                 # 用 data/position_bank.npz 做库
    python eval_position_ocr.py --bank X.npz --src D:\some
"""
import argparse
import glob
import os
import random
import sys
import time
from pathlib import Path

from PIL import Image, ImageEnhance

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import posocr  # noqa: E402

DEFAULT_SRC = r"D:\mil-ai\IFC\grid-position"


def label_of(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return "-".join(p.strip() for p in base.split("-"))


def run(data, bank, mutate=None, tag=""):
    ok = decl = wrong = 0
    bad = []
    t0 = time.time()
    for f, lab, img in data:
        im = mutate(img) if mutate else img
        got, conf, _info = posocr.read_pos(im, bank)
        if got == lab:
            ok += 1
        elif got is None:
            decl += 1
        else:
            wrong += 1
            bad.append((os.path.basename(f), lab, got))
    ms = (time.time() - t0) / max(1, len(data)) * 1000
    print(f"{tag:16s} n={len(data):3d} ok={ok:3d} decl={decl:3d} WRONG={wrong:3d}  {ms:.1f} ms/图")
    for b in bad[:6]:
        print("   WRONG:", b)
    return ok, decl, wrong


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--bank", default=str(HERE.parent / "data" / "position_bank.npz"))
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.src, "*.jpg")))
    data = [(f, label_of(f), Image.open(f).convert("RGB")) for f in files]
    bank = posocr.Bank(Path(a.bank)).load()
    print(f"样本 {len(data)} 张，模板库 {bank.count()} 条，尺度 {posocr.SCALES}")
    print(f"门槛 accept={posocr.ACCEPT} margin={posocr.MARGIN}\n")

    run(data, bank, tag="干净")

    def light(img):
        dx, dy = random.randint(-1, 1), random.randint(-1, 1)
        im = ImageEnhance.Brightness(img).enhance(random.uniform(0.9, 1.1))
        im = ImageEnhance.Contrast(im).enhance(random.uniform(0.9, 1.1))
        return im.transform(im.size, Image.AFFINE, (1, 0, dx, 0, 1, dy), fillcolor=(90, 96, 70))

    def heavy(img):
        dx, dy = random.randint(-2, 2), random.randint(-2, 2)
        im = ImageEnhance.Brightness(img).enhance(random.uniform(0.8, 1.2))
        im = ImageEnhance.Contrast(im).enhance(random.uniform(0.8, 1.25))
        return im.transform(im.size, Image.AFFINE, (1, 0, dx, 0, 1, dy), fillcolor=(90, 96, 70))

    random.seed(1)
    run([d for d in data for _ in range(4)], bank, lambda im: light(im), "轻微抖动×4")
    random.seed(2)
    run([d for d in data for _ in range(5)], bank, lambda im: heavy(im), "重抖动×5")
    for factor in (0.75, 0.85, 1.15, 1.3):
        run(data, bank,
            lambda im, f=factor: im.resize((max(20, int(im.width * f)), max(8, int(im.height * f))),
                                           Image.LANCZOS),
            f"缩放 x{factor}")


if __name__ == "__main__":
    main()
