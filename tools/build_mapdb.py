# -*- coding: utf-8 -*-
r"""建「高清地图库」：把 combat_ai 的 8192² 底图 + ArtyCalc 的高程图收进一个独立目录。

用法（在 D:\mil-ai\IFC\ArtyCalc 下）：
    python tools\build_mapdb.py                 # 默认 src=D:\mil-ai\combat_ai\maps，dst=dist\mapsdb
    python tools\build_mapdb.py --src X --dst Y

产物：<dst>\_db.json + <slug>_hq.jpg + <slug>_elev.png + _lo\<slug>_lo.jpg
    _db.json 是清单（format/kind/px/maps），程序运行时只认它；地图元数据（图幅、高程范围）
    仍然存在 exe 里的 data\maps.json —— 清单只负责「哪张图、哪张高程、哪张缩略图」。
    _lo\ 里是 2048² 缩略图：8192² 解一次要 1.3 s，界面先用缩略图顶上，全分辨率在后台解。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent           # ...\IFC\ArtyCalc
DATA = ROOT / "data"
DEFAULT_SRC = Path(r"D:\mil-ai\combat_ai\maps")
DEFAULT_DST = ROOT / "dist" / "mapsdb"


def human(n: int) -> str:
    return f"{n / 1048576:.1f} MB".replace(".0 MB", " MB")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="combat_ai 的 maps 目录（放 *_hq.jpg）")
    ap.add_argument("--dst", default=str(DEFAULT_DST), help="输出的地图库目录")
    ap.add_argument("--force", action="store_true", help="即使目标文件已存在也重新复制")
    a = ap.parse_args()

    src, dst = Path(a.src), Path(a.dst)
    if not (src / "").exists():
        print(f"[maps] 源目录不存在：{src}")
        return 2
    metas = json.loads((DATA / "maps.json").read_text(encoding="utf-8"))
    dst.mkdir(parents=True, exist_ok=True)

    from PIL import Image

    entries: dict[str, dict] = {}
    total = 0
    t0 = time.time()
    for i, m in enumerate(metas, 1):
        slug = m["slug"]
        hq = src / f"{slug}_hq.jpg"
        elev = DATA / m["elev"]
        if not hq.exists():
            print(f"[maps] 缺底图：{hq} —— 跳过 {slug}")
            continue
        if not elev.exists():
            print(f"[maps] 缺高程：{elev} —— 跳过 {slug}")
            continue
        with Image.open(hq) as im:
            w, h = im.size
        if w != h or w < 4096:
            print(f"[maps] {hq.name} 尺寸 {w}×{h} 不合格（要正方形且 >= 4096）")
            return 3
        d_hq, d_el = dst / hq.name, dst / elev.name
        d_lo = dst / "_lo" / f"{slug}_lo.jpg"
        copied = 0
        for s, d in ((hq, d_hq), (elev, d_el)):
            if a.force or not d.exists() or d.stat().st_size != s.stat().st_size:
                shutil.copy2(s, d)
                copied += 1
        if a.force or not d_lo.exists():
            # draft() 让 libjpeg 直接按 1/4 解码，比整张解完再缩快好几倍
            with Image.open(hq) as im:
                im.draft("RGB", (2048, 2048))
                thumb = im.convert("RGB").resize((2048, 2048), Image.LANCZOS)
            d_lo.parent.mkdir(parents=True, exist_ok=True)
            thumb.save(d_lo, "JPEG", quality=88)
            copied += 1
        n = d_hq.stat().st_size + d_el.stat().st_size + d_lo.stat().st_size
        total += n
        entries[slug] = {"base": hq.name, "elev": elev.name, "lo": f"_lo/{d_lo.name}", "px": w,
                         "ppm": round(w / max(m["spanx"], 1.0), 3), "bytes": n}
        print(f"[{i:2d}/{len(metas)}] {slug:12s} {w}×{h}  1px≈{m['spanx'] / w:.2f} m  "
              f"{human(n):>9s}  {'复制 %d 个' % copied if copied else '已是最新'}")

    manifest = {
        "format": 1,
        "kind": "hd",
        "built": time.strftime("%Y-%m-%d %H:%M:%S"),
        "src": str(src),
        "px": max((e["px"] for e in entries.values()), default=0),
        "maps": entries,
    }
    (dst / "_db.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    print(f"\n[maps] {len(entries)} 张 · {human(total)} · {time.time() - t0:.1f}s")
    print(f"[maps] 清单：{dst / '_db.json'}")
    return 0 if entries else 4


if __name__ == "__main__":
    sys.exit(main())
