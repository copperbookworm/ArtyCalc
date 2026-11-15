# -*- coding: utf-8 -*-
r"""地图数据：底图 + 1024 网格高程（米）。与页面 hAtM 同口径。

底图有两档，按「高清地图库」优先：
    1. 高清库 <mapsdb>\<slug>_hq.jpg    8192 x 8192，1 px ≈ 0.16～0.84 m（随图幅，见 _db.json 的 ppm）
    2. 自带旧底图 data\<meta.base>      2048 x 2048（打包后不再随 exe 发布，只在源码树里兜底）
高清库是独立目录（exe 外面），位置见 paths.mapdb_dir()；高程图也跟着放库里。
地图**元数据**（图幅、高程范围、网格）仍然在 exe 里的 data\maps.json —— 库只负责图片。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

import core
import paths

DATA = paths.resource_dir() / "data"
N = 1024

# ---------------------------------------------------------------- 高清地图库
_DB: dict | None = None


def _db() -> tuple[Path | None, dict]:
    """(库目录, {slug: {base, elev, px, ppm}}) —— 只读一次。"""
    global _DB
    if _DB is None:
        d = paths.mapdb_dir()
        maps: dict = {}
        if d is not None:
            try:
                maps = json.loads((d / "_db.json").read_text(encoding="utf-8")).get("maps") or {}
            except Exception:
                maps = {}
        _DB = {"dir": d, "maps": maps}
    return _DB["dir"], _DB["maps"]


def db_info() -> str:
    """给日志/界面用的一句话：库在哪、几张。没库时说明会退回旧底图。"""
    d, maps = _db()
    if d is None:
        return r"未找到高清地图库（mapsdb 目录里的 _db.json）—— 用自带旧底图"
    px = max((int(v.get("px") or 0) for v in maps.values()), default=0)
    return f"{d} · {len(maps)} 张 · {px}×{px}"


def reload_db():
    """换过库（或刚建好）后重新读清单。"""
    global _DB
    _DB = None


class MapData:
    def __init__(self, meta: dict):
        self.meta = meta
        self.slug = meta["slug"]
        self.label = meta["label"]
        self.name = meta["name"]
        self.minx = meta["minx"]
        self.minz = meta["minz"]
        self.spanx = meta["spanx"]
        self.spanz = meta["spanz"]
        self.cellx = meta["cellx"]
        self.cellz = meta["cellz"]
        self.elev_lo = meta["elevLo"]
        self.elev_hi = meta["elevHi"]
        self._basemap = None
        self._elev = None
        self._base_px = 0

    # ---- 资源（惰性加载，切图不占内存） ----
    def db_files(self) -> dict | None:
        """高清库里这张图的 {base, elev}（都是绝对路径）；库里没有就 None。"""
        d, maps = _db()
        if d is None:
            return None
        e = maps.get(self.slug) or {}
        base = d / str(e.get("base") or "")
        elev = d / str(e.get("elev") or "")
        if not base.is_file():
            return None
        return {"base": base, "elev": elev if elev.is_file() else None,
                "px": int(e.get("px") or 0)}

    @property
    def hd(self) -> bool:
        return self.db_files() is not None

    @property
    def basemap_path(self) -> Path:
        f = self.db_files()
        return f["base"] if f else DATA / self.meta["base"]

    @property
    def lo_path(self) -> Path | None:
        """库里的 2048² 缩略图（没有就 None）。界面先用它顶上，全分辨率在后台解。"""
        d, maps = _db()
        if d is None:
            return None
        e = maps.get(self.slug) or {}
        rel = str(e.get("lo") or "")
        if not rel:
            return None
        p = d / rel
        return p if p.is_file() else None

    @property
    def elev_path(self) -> Path:
        f = self.db_files()
        if f and f["elev"] is not None:
            return f["elev"]
        return DATA / self.meta["elev"]

    @property
    def base_px(self) -> int:
        """底图边长（像素）。0 = 读不到。"""
        if not self._base_px:
            try:
                with Image.open(self.basemap_path) as im:
                    self._base_px = int(im.size[0])
            except Exception:
                self._base_px = 0
        return self._base_px

    @property
    def px_per_m(self) -> float:
        """底图分辨率：1 m 占几个像素（高清库 8192 时约 1.3～6）。"""
        return self.base_px / max(self.spanx, 1.0)

    def basemap(self) -> Image.Image:
        if self._basemap is None:
            from PIL import ImageEnhance

            p = self.basemap_path
            if not p.is_file():
                raise FileNotFoundError(f"找不到底图：{p}")
            img = Image.open(p).convert("RGB")
            if not self.hd:
                # 旧的 2048 卫星底图偏暗，提亮一点更容易看清网格与地形（不改几何）
                # 高清库（8192 瓦片）本身就是亮的，再提亮会发灰
                img = ImageEnhance.Brightness(img).enhance(1.32)
                img = ImageEnhance.Contrast(img).enhance(1.06)
            self._basemap = img
            self._base_px = img.size[0]
        return self._basemap

    @property
    def elev(self) -> np.ndarray:
        if self._elev is None:
            p = self.elev_path
            if not p.is_file():
                raise FileNotFoundError(f"找不到高程图：{p}")
            img = Image.open(p)
            q = np.asarray(img).astype(np.float32)
            self._elev = self.elev_lo + q / 65535.0 * (self.elev_hi - self.elev_lo)
        return self._elev

    def drop_cache(self):
        self._elev = None
        self._basemap = None

    # ---- 坐标换算：相对米 <-> 底图像素 ----
    def rel_to_px(self, rx: float, rz: float, size: float) -> tuple[float, float]:
        return rx / self.spanx * size, rz / self.spanz * size

    def px_to_rel(self, px: float, pz: float, size: float) -> tuple[float, float]:
        return px / size * self.spanx, pz / size * self.spanz

    # ---- 高程（双线性，双轴分别换算，和页面一致） ----
    def h_at(self, rx: float, rz: float) -> float:
        fx = rx / self.cellx - 0.5
        fz = rz / self.cellz - 0.5
        fx = min(N - 1.001, max(0.0, fx))
        fz = min(N - 1.001, max(0.0, fz))
        x0 = int(np.floor(fx)); z0 = int(np.floor(fz))
        tx = fx - x0; tz = fz - z0
        e = self.elev
        a = e[z0, x0]; b = e[z0, x0 + 1]
        c = e[z0 + 1, x0]; dd = e[z0 + 1, x0 + 1]
        return (a * (1 - tx) + b * tx) * (1 - tz) + (c * (1 - tx) + dd * tx) * tz

    def grid_at(self, rx: float, rz: float, level: int = 4) -> str:
        """网格文本。level=3 → B11-2-5；level=4 → B11-2-5-6（望远镜/侦察才用 4 段）。"""
        return core.text_at(rx, rz, max(1, min(4, int(level))))

    def locate(self, grid_text: str) -> tuple[float, float]:
        """网格文本 -> 相对米（格心）。越界时抛 ParseError。"""
        rx, rz = core.parse(grid_text)
        if rx > self.spanx + 1 or rz > self.spanz + 1:
            raise core.ParseError(f"{grid_text} 不在这张地图上（图幅 {self.spanx:.0f}×{self.spanz:.0f} m）")
        return min(rx, self.spanx), min(rz, self.spanz)


def load_maps() -> list[MapData]:
    return [MapData(m) for m in json.loads((DATA / "maps.json").read_text(encoding="utf-8"))]
