"""SQLite 存储层。

关键不变式:
- 某一版的完整快照(entries)+ 版本登记(versions)+ 当前版指针(hours.current_version)
  + 质量标记(quality_marks)在同一个事务里提交,因此失败或重启都不会暴露"半版"数据,
  也不会出现读数与质量标记不一致。
- 未确认版本的页面不落库,采集中的残页只存在于内存,重试时整版重取。
- quality_marks 只保存"当前供数版"下各站点各小时的质量判定;确认新版时在同一事务
  内重算受影响站点的标记。删除站不落标记,查询时按"最新版无读数"派生为 deleted。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/collector.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hours (
    hour            TEXT PRIMARY KEY,          -- UTC 小时,如 2026-11-18T10
    current_version INTEGER NOT NULL DEFAULT 0, -- 当前版指针:0 表示尚无确认版
    status          TEXT NOT NULL DEFAULT 'idle',
    pinned_version  INTEGER,
    last_error      TEXT,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    hour           TEXT NOT NULL,
    version        INTEGER NOT NULL,
    total_pages    INTEGER NOT NULL,
    item_count     INTEGER NOT NULL,
    station_count  INTEGER NOT NULL,
    confirmed_at   TEXT NOT NULL,
    PRIMARY KEY (hour, version)
);
CREATE TABLE IF NOT EXISTS entries (
    hour       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    station_id TEXT NOT NULL,
    pm25       REAL NOT NULL,
    PRIMARY KEY (hour, version, station_id)
);
CREATE TABLE IF NOT EXISTS quality_marks (
    station_id TEXT NOT NULL,
    hour       TEXT NOT NULL,
    version    INTEGER NOT NULL,                -- 判定时该小时的当前供数版
    pm25       REAL NOT NULL,                   -- 判定所依据的读数(冗余,便于一致性校验)
    status     TEXT NOT NULL,                   -- ok / flatline
    PRIMARY KEY (station_id, hour)
);
CREATE INDEX IF NOT EXISTS idx_entries_station_hour
    ON entries (station_id, hour);
CREATE INDEX IF NOT EXISTS idx_quality_station_hour
    ON quality_marks (station_id, hour);
"""


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)  # 自动提交,显式事务见 tx()
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    db = connect()
    try:
        db.executescript(SCHEMA)
        # 重启清理:把中断在 collecting 的小时复位为 idle。
        # 数据本身由事务保证完整,这里只修正状态元信息。
        db.execute("UPDATE hours SET status='idle' WHERE status='collecting'")
        # 迁移:对升级前已确认的历史数据回填质量标记(user_version 0 -> 1)。
        if db.execute("PRAGMA user_version").fetchone()[0] < 1:
            _backfill_quality_marks(db)
            db.execute("PRAGMA user_version=1")
    finally:
        db.close()


def _backfill_quality_marks(db: sqlite3.Connection) -> None:
    """把既有确认数据按各小时当前供数版全量重算质量标记。

    与运行期重算共用同一套判定逻辑,保证存量数据与新数据口径一致。
    在单个显式事务内完成,回填中断不会留下半截标记。
    """
    # 延迟导入避免循环依赖:quality 模块本身只依赖本模块的连接/事务工具。
    from .quality import recompute_station_quality, all_station_ids

    with tx(db):
        db.execute("DELETE FROM quality_marks")
        for sid in all_station_ids(db):
            recompute_station_quality(db, sid)


@contextmanager
def tx(db: sqlite3.Connection):
    """显式事务:要么整体提交,要么整体回滚。"""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except Exception:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


@contextmanager
def read_tx(db: sqlite3.Connection):
    """只读快照事务:WAL 下多条 SELECT 始终读取同一个已提交快照,
    不会因采集端在查询期间提交新版而读到前后不一致的数据。"""
    db.execute("BEGIN")
    try:
        yield db
    except Exception:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def ensure_hour(db: sqlite3.Connection, hour: str, now: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO hours(hour, current_version, status, updated_at) "
        "VALUES (?, 0, 'idle', ?)",
        (hour, now),
    )


def get_hour(db: sqlite3.Connection, hour: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM hours WHERE hour=?", (hour,)).fetchone()


def snapshot(db: sqlite3.Connection, hour: str, version: int) -> dict[str, float]:
    rows = db.execute(
        "SELECT station_id, pm25 FROM entries WHERE hour=? AND version=?",
        (hour, version),
    ).fetchall()
    return {r["station_id"]: r["pm25"] for r in rows}
