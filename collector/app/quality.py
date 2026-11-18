"""站点质量序列:平线(flatline)判定、标记重算与查询。

判定规则(按站点):
- 取该站点在各已采集小时"当前供数版"下的确认读数,按 UTC 小时排序。
- 缺小时(从未采集)或站点在某小时被删除都会打断连续性。
- 连续 >= FLATLINE_MIN_RUN 个整点读数完全相同,整段判为 flatline;
  其余有读数的小时为 ok;无读数的小时不落标记,由查询层派生 missing/deleted。

一致性:
- 标记的读数列(pm25)与版本列(version)随确认版在同一事务写入,
  任何时刻查到的标记都对应已提交的快照,采集失败/并发查询/重启均不会错配。
- 修订某小时后对受影响站点做"全历史重算",因此邻近小时会同步改判,
  且判定天然把查询/采集窗口之外的连续读数计入,平线跨越边缘时长度计算正确。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db as dbmod

FLATLINE_MIN_RUN = 6
MAX_SERIES_HOURS = 48
HOUR_FMT = "%Y-%m-%dT%H"


class StationNotFound(Exception):
    """站点从未在任何已确认快照中出现。"""


# ---------------- 小时工具 ----------------
def parse_hour(text: str) -> datetime:
    return datetime.strptime(text, HOUR_FMT).replace(tzinfo=timezone.utc)


def fmt_hour(dt: datetime) -> str:
    return dt.strftime(HOUR_FMT)


def iter_hours(start: str, end: str):
    """闭区间 [start, end] 逐小时产出 UTC 小时字符串。"""
    cur = parse_hour(start)
    end_dt = parse_hour(end)
    while cur <= end_dt:
        yield fmt_hour(cur)
        cur += timedelta(hours=1)


# ---------------- 读取 ----------------
def all_station_ids(db) -> list[str]:
    rows = db.execute("SELECT DISTINCT station_id FROM entries ORDER BY station_id").fetchall()
    return [r["station_id"] for r in rows]


def affected_station_ids(db, hour: str, new_version: int) -> list[str]:
    """新版确认后可能改判的站点:新快照与上一版快照的并集(含被删站)。"""
    rows = db.execute(
        "SELECT DISTINCT station_id FROM entries WHERE hour=? AND version IN (?, ?) "
        "ORDER BY station_id",
        (hour, new_version - 1, new_version),
    ).fetchall()
    return [r["station_id"] for r in rows]


def station_series(db, station_id: str) -> list[dict]:
    """站点在各已采集小时当前供数版下的读数,按小时升序。

    以 hours.current_version 为准,与 entries 做内联版本匹配;
    某小时当前版不含该站(删除)则该小时不出现 —— 由调用方据此打断连续段。
    """
    rows = db.execute(
        "SELECT h.hour AS hour, h.current_version AS version, e.pm25 AS pm25 "
        "FROM hours h JOIN entries e "
        "ON e.hour = h.hour AND e.version = h.current_version "
        "WHERE h.current_version > 0 AND e.station_id = ? "
        "ORDER BY h.hour",
        (station_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def classify(series: list[dict]) -> dict[str, str]:
    """对按小时升序的读数序列做平线判定,返回 {hour: status}。

    - 时间上不相邻(缺小时)或读数变化即另起一段;
    - 同值连续段长度 >= FLATLINE_MIN_RUN 时整段 flatline,否则 ok。
    删除站不出现在 series 中,天然在删除小时处断开。
    """
    marks: dict[str, str] = {}
    i = 0
    n = len(series)
    while i < n:
        j = i + 1
        while j < n:
            prev_dt = parse_hour(series[j - 1]["hour"])
            cur_dt = parse_hour(series[j]["hour"])
            adjacent = cur_dt == prev_dt + timedelta(hours=1)
            same_value = series[j]["pm25"] == series[i]["pm25"]
            if not adjacent or not same_value:
                break
            j += 1
        status = "flatline" if (j - i) >= FLATLINE_MIN_RUN else "ok"
        for k in range(i, j):
            marks[series[k]["hour"]] = status
        i = j
    return marks


def recompute_station_quality(db, station_id: str) -> None:
    """全历史重算单个站点的质量标记(须在显式事务内调用)。

    全量替换该站标记,因此:
    - 修订后邻近小时同步改判;
    - 站点在某小时被删除时,旧标记随全量替换被清除,entries 历史版原值保留;
    - 平线判定使用全库连续读数,不受采集/查询窗口边缘影响。
    """
    series = station_series(db, station_id)
    marks = classify(series)
    db.execute("DELETE FROM quality_marks WHERE station_id=?", (station_id,))
    if marks:
        db.executemany(
            "INSERT INTO quality_marks(station_id, hour, version, pm25, status) "
            "VALUES (?,?,?,?,?)",
            [
                (station_id, item["hour"], item["version"], item["pm25"], marks[item["hour"]])
                for item in series
            ],
        )


# ---------------- 质量序列查询 ----------------
def quality_series(db, station_id: str, start: str, end: str) -> dict:
    """按站点返回 [start, end] 内逐小时的确认版、读数与质量状态。

    状态:
    - ok:       有读数,未落入平线;quality_pm25 同 pm25
    - flatline: 有读数但属于 >=6 整点同值的平线段;quality_pm25 置空
    - deleted:  该小时已采集,但站点在当前供数版中被删除
    - missing:  该小时从未成功采集(平线连续性在此截断)
    """
    exists = db.execute(
        "SELECT 1 FROM entries WHERE station_id=? LIMIT 1", (station_id,)
    ).fetchone()
    if exists is None:
        raise StationNotFound(f"station {station_id} has no confirmed reading")
    hours = list(iter_hours(start, end))
    with dbmod.read_tx(db):
        mark_rows = db.execute(
            "SELECT hour, version, pm25, status FROM quality_marks "
            "WHERE station_id=? AND hour>=? AND hour<=? ORDER BY hour",
            (station_id, start, end),
        ).fetchall()
        marks = {r["hour"]: dict(r) for r in mark_rows}

        current_versions = {
            r["hour"]: r["current_version"]
            for r in db.execute(
                "SELECT hour, current_version FROM hours "
                "WHERE hour>=? AND hour<=? AND current_version>0",
                (start, end),
            ).fetchall()
        }

        points = []
        for hour in hours:
            mark = marks.get(hour)
            if mark is not None:
                # 不变式自检:标记必须与当前供数版读数一致(同一事务写入,正常恒成立)。
                cur_v = current_versions.get(hour)
                if cur_v != mark["version"]:
                    raise RuntimeError(
                        f"quality mark inconsistent for {station_id} {hour}: "
                        f"mark version {mark['version']} != current {cur_v}"
                    )
                flat = mark["status"] == "flatline"
                points.append({
                    "hour": hour,
                    "version": mark["version"],
                    "pm25": mark["pm25"],
                    "quality_pm25": None if flat else mark["pm25"],
                    "status": "flatline" if flat else "ok",
                })
                continue
            if hour in current_versions:
                # 小时已采集且有确认版,但该站无标记:当前版快照中已被删除。
                points.append({
                    "hour": hour,
                    "version": current_versions[hour],
                    "pm25": None,
                    "quality_pm25": None,
                    "status": "deleted",
                })
            else:
                points.append({
                    "hour": hour,
                    "version": None,
                    "pm25": None,
                    "quality_pm25": None,
                    "status": "missing",
                })

    flatline_hours = [p["hour"] for p in points if p["status"] == "flatline"]
    return {
        "station_id": station_id,
        "start": start,
        "end": end,
        "hour_count": len(hours),
        "flatline_hours": flatline_hours,
        "hours": points,
    }
