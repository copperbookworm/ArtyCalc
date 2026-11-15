# -*- coding: utf-8 -*-
r"""侦察目标库：把「侦察报告」里标记过的敌方目标存到本地 sqlite。

一条记录 = 位置（格号 + 米制坐标 + 高程）+ 属性（阵营 / 分类 / 型号 / 血量 / 票数）
+ 来源子模式（drone / scope）+ 身份码 UID（T01–T50，一局最多 50 个）。

数据库放用户目录（与 config.json 同处）：便携时在程序目录，程序目录不可写时在
%LOCALAPPDATA%\SquadArtyCalc\targets.db。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

UID_MAX = 50        # Squad 是 50 v 50：同一张图（同一局）最多 50 个目标身份码

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    created  REAL NOT NULL,
    map      TEXT NOT NULL,
    grid     TEXT NOT NULL,
    rx       REAL,
    rz       REAL,
    h        REAL,
    faction  TEXT,
    cat      TEXT,
    name     TEXT,
    hp       INTEGER DEFAULT 0,
    ticket   INTEGER DEFAULT 0,
    sub      TEXT,
    reported INTEGER DEFAULT 0,
    uid      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_targets_map ON targets(map, created DESC);
"""


class TargetDB:
    """最小封装：只做本程序要用的几种查询。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path))
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.commit()
        self._migrate_uid()

    # ---------- 迁移 ----------
    def _migrate_uid(self) -> None:
        """1.5.0 起每条目标有 UID（T01…T50，按图分配）。老库补列并按创建顺序补齐。"""
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(targets)").fetchall()}
        if "uid" not in cols:
            self.con.execute("ALTER TABLE targets ADD COLUMN uid TEXT DEFAULT ''")
            self.con.commit()
        rows = self.con.execute(
            "SELECT id, map, uid FROM targets ORDER BY created ASC, id ASC").fetchall()
        used: dict[str, set] = {}
        for r in rows:
            used.setdefault(r["map"], set()).add(r["uid"] or "")
        changed = 0
        for r in rows:
            if r["uid"]:
                continue
            pool = used.setdefault(r["map"], set())
            for i in range(1, UID_MAX + 1):
                u = "T%02d" % i
                if u not in pool:
                    pool.add(u)
                    self.con.execute("UPDATE targets SET uid=? WHERE id=?", (u, r["id"]))
                    changed += 1
                    break
        if changed:
            self.con.commit()

    # ---------- 写 ----------
    def add(self, map_slug: str, grid: str, rx: float, rz: float, h: float,
            faction: str, cat: str, name: str, hp: int = 0, ticket: int = 0,
            sub: str = "", uid: str = "") -> int:
        cur = self.con.execute(
            "INSERT INTO targets(created,map,grid,rx,rz,h,faction,cat,name,hp,ticket,sub,reported,uid)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (time.time(), map_slug, grid, float(rx), float(rz), float(h),
             faction or "", cat or "", name or "", int(hp or 0), int(ticket or 0),
             sub or "", uid or ""))
        self.con.commit()
        return int(cur.lastrowid)

    def update_pos(self, tid: int, grid: str, rx: float, rz: float, h: float,
                   sub: str = "") -> None:
        """只动位置：UID / 型号 / 属性保持不变（「更新已有目标」用）。"""
        self.con.execute(
            "UPDATE targets SET grid=?, rx=?, rz=?, h=?, sub=? WHERE id=?",
            (grid, float(rx), float(rz), float(h), sub or "", int(tid)))
        self.con.commit()

    def mark_reported(self, tid: int) -> None:
        self.con.execute("UPDATE targets SET reported=reported+1 WHERE id=?", (int(tid),))
        self.con.commit()

    def delete(self, tid: int) -> None:
        self.con.execute("DELETE FROM targets WHERE id=?", (int(tid),))
        self.con.commit()

    def delete_many(self, tids) -> int:
        ids = [int(t) for t in tids]
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        cur = self.con.execute("DELETE FROM targets WHERE id IN (%s)" % q, ids)
        self.con.commit()
        return int(cur.rowcount or 0)

    def clear(self, map_slug: str | None = None) -> int:
        if map_slug:
            cur = self.con.execute("DELETE FROM targets WHERE map=?", (map_slug,))
        else:
            cur = self.con.execute("DELETE FROM targets")
        self.con.commit()
        return int(cur.rowcount or 0)

    # ---------- 读 ----------
    def recent(self, map_slug: str, limit: int = 8, only_map: bool = True) -> list[dict]:
        sql = "SELECT * FROM targets"
        args: list = []
        if only_map:
            sql += " WHERE map=?"
            args.append(map_slug)
        sql += " ORDER BY created DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def list_map(self, map_slug: str, limit: int = UID_MAX) -> list[dict]:
        """本图所有目标，按 UID 顺序（= 标记先后）：更新 / 清除时按这个列表选。"""
        rows = self.con.execute(
            "SELECT * FROM targets WHERE map=? ORDER BY uid ASC, id ASC LIMIT ?",
            (map_slug, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def next_uid(self, map_slug: str) -> str | None:
        """按图分配最小可用 UID；本图 50 个用完了返回 None。"""
        used = {r["uid"] for r in self.con.execute(
            "SELECT uid FROM targets WHERE map=?", (map_slug,)).fetchall() if r["uid"]}
        for i in range(1, UID_MAX + 1):
            u = "T%02d" % i
            if u not in used:
                return u
        return None

    def count(self, map_slug: str | None = None) -> int:
        if map_slug:
            row = self.con.execute("SELECT COUNT(*) FROM targets WHERE map=?",
                                   (map_slug,)).fetchone()
        else:
            row = self.con.execute("SELECT COUNT(*) FROM targets").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:       # noqa: BLE001
            pass
