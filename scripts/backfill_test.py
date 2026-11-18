#!/usr/bin/env python3
"""迁移与回填测试(无需运行服务)。

构造升级前的旧版数据库(只有 hours/versions/entries,且含采集中断状态),
执行新版 db.init() 后验证:
1. 既有确认数据被全量回填质量标记,平线判定与运行期口径一致;
2. 回填在事务内完成、重复启动幂等(user_version 防重);
3. 任意标记都能与当前供数版读数一一对上(读数与标记永不不一致);
4. 重启仅复位 collecting 状态元信息,不改动数据与标记。
"""
from __future__ import annotations

import os
import sqlite3
import sys

DB_PATH = "/tmp/backfill_test.db"
HOURS = [f"2026-12-01T{h:02d}" for h in range(8)]

OLD_SCHEMA = """
CREATE TABLE hours (
    hour TEXT PRIMARY KEY, current_version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'idle', pinned_version INTEGER,
    last_error TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE versions (
    hour TEXT NOT NULL, version INTEGER NOT NULL, total_pages INTEGER NOT NULL,
    item_count INTEGER NOT NULL, station_count INTEGER NOT NULL,
    confirmed_at TEXT NOT NULL, PRIMARY KEY (hour, version)
);
CREATE TABLE entries (
    hour TEXT NOT NULL, version INTEGER NOT NULL, station_id TEXT NOT NULL,
    pm25 REAL NOT NULL, PRIMARY KEY (hour, version, station_id)
);
"""


def build_old_database(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    for idx, hour in enumerate(HOURS):
        collecting = idx == 7  # 模拟最后一个小时采集中断
        db.execute(
            "INSERT INTO hours(hour, current_version, status, updated_at) VALUES (?,?,?,?)",
            (hour, 1, "collecting" if collecting else "idle", "2026-12-01T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO versions(hour, version, total_pages, item_count, "
            "station_count, confirmed_at) VALUES (?,1,1,?,?, 't')",
            (hour, 0, 0),
        )
        # S01 八小时恒为 30.0 -> 整段平线;S02 每小时变化 -> 全 ok;
        # S03 只存在于前 6 小时(模拟 H6 起删除),前 6 小时恒为 9.0 -> 6 连平。
        rows = [("S01", 30.0), ("S02", 100.0 + idx)]
        if idx < 6:
            rows.append(("S03", 9.0))
        db.executemany(
            "INSERT INTO entries(hour, version, station_id, pm25) VALUES (?,1,?,?)",
            [(hour, sid, pm) for sid, pm in rows],
        )
        db.execute(
            "UPDATE versions SET item_count=?, station_count=? WHERE hour=? AND version=1",
            (len(rows), len(rows), hour),
        )
    db.commit()
    db.close()


def main() -> int:
    build_old_database(DB_PATH)
    os.environ["DB_PATH"] = DB_PATH
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))

    from app import db as dbmod  # noqa: E402
    from app import quality  # noqa: E402

    print("-- 1. 旧库首次启动:建表并回填")
    dbmod.init()
    db = dbmod.connect()
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        n_marks = db.execute("SELECT COUNT(*) FROM quality_marks").fetchone()[0]
        # S01:8 + S02:8 + S03:6 = 22
        assert n_marks == 22, f"backfill count {n_marks} != 22"
        print(f"     回填标记 {n_marks} 条")

        print("-- 2. 平线/正常判定正确")
        s01 = {r["hour"]: r["status"] for r in db.execute(
            "SELECT hour, status FROM quality_marks WHERE station_id='S01' ORDER BY hour")}
        assert all(s == "flatline" for s in s01.values()), s01
        s02 = {r["hour"]: r["status"] for r in db.execute(
            "SELECT hour, status FROM quality_marks WHERE station_id='S02' ORDER BY hour")}
        assert all(s == "ok" for s in s02.values()), s02
        s03 = {r["hour"]: r["status"] for r in db.execute(
            "SELECT hour, status FROM quality_marks WHERE station_id='S03' ORDER BY hour")}
        assert set(s03) == set(HOURS[:6]) and all(s == "flatline" for s in s03.values()), s03
        print("     S01 全平线 / S02 全 ok / S03 前 6 小时平线且删除处截断")

        print("-- 3. 不变式:每条标记都与当前供数版读数严格一致")
        bad = db.execute(
            "SELECT q.* FROM quality_marks q JOIN hours h ON h.hour=q.hour "
            "JOIN entries e ON e.hour=q.hour AND e.version=h.current_version "
            "AND e.station_id=q.station_id "
            "WHERE q.version<>h.current_version OR q.pm25<>e.pm25"
        ).fetchall()
        assert not bad, f"inconsistent marks: {bad}"
        # 当前版有读数的(站,小时)必须都有标记;无读数的(删除)必须无标记
        missing_mark = db.execute(
            "SELECT e.station_id, e.hour FROM entries e JOIN hours h "
            "ON h.hour=e.hour AND e.version=h.current_version "
            "LEFT JOIN quality_marks q ON q.station_id=e.station_id AND q.hour=e.hour "
            "WHERE q.hour IS NULL"
        ).fetchall()
        assert not missing_mark, f"current reading without mark: {missing_mark}"
        dangling = db.execute(
            "SELECT q.* FROM quality_marks q JOIN hours h ON h.hour=q.hour "
            "LEFT JOIN entries e ON e.hour=q.hour AND e.version=h.current_version "
            "AND e.station_id=q.station_id WHERE e.station_id IS NULL"
        ).fetchall()
        assert not dangling, f"mark without current reading: {dangling}"
        print("     标记 ↔ 当前版读数一一对应,无悬挂/无遗漏")

        print("-- 4. 重启幂等:再次 init 不重复回填、标记不变")
        before = db.execute("SELECT COUNT(*) FROM quality_marks").fetchone()[0]
        dbmod.init()
        after = db.execute("SELECT COUNT(*) FROM quality_marks").fetchone()[0]
        assert before == after == 22
        stuck = db.execute("SELECT COUNT(*) FROM hours WHERE status='collecting'").fetchone()[0]
        assert stuck == 0, "collecting status not reset on startup"
        print("     user_version 防重,collecting 中断状态已复位")

        print("-- 5. 回填后查询接口派生 deleted/missing 正确")
        res = quality.quality_series(db, "S03", HOURS[0], HOURS[7])
        statuses = [p["status"] for p in res["hours"]]
        assert statuses == ["flatline"] * 6 + ["deleted", "deleted"], statuses
        res2 = quality.quality_series(db, "S01", "2026-11-30T22", HOURS[1])
        gap = [p["status"] for p in res2["hours"]]
        # 窗口前两小时从未采集 -> missing,平线在缺口前不计入窗口内判定
        assert gap[0] == "missing" and gap[1] == "missing", gap
        assert all(p["status"] == "flatline" for p in res2["hours"][2:]), gap
        print("     deleted/missing 派生与平线截断均正确")
    finally:
        db.close()
    print("== backfill 测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
