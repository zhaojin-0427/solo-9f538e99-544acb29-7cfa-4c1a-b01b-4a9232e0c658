#!/usr/bin/env python3
"""站点质量序列端到端冒烟:对运行中的 source + collector 验证平线判定全链路。

用法:
    python3 scripts/quality_smoke.py
    COLLECTOR_URL=... SOURCE_URL=... python3 scripts/quality_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

COLLECTOR = os.environ.get("COLLECTOR_URL", "http://localhost:8000").rstrip("/")
SOURCE = os.environ.get("SOURCE_URL", "http://localhost:8001").rstrip("/")

HOURS = [f"2026-11-20T{h:02d}" for h in range(10)]  # H0..H9,其中 H6 永不采集

_checks = 0


def req(method: str, base: str, path: str, body=None, expect: int | None = 200):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            code, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    if expect is not None and code != expect:
        raise AssertionError(f"{method} {url} -> {code}, want {expect}: {payload[:400]!r}")
    return code, (json.loads(payload) if payload else {})


def check(name: str, cond: bool, extra: str = "") -> None:
    global _checks
    if not cond:
        raise AssertionError(f"CHECK FAILED: {name} {extra}")
    _checks += 1
    print(f"  ok [{_checks:02d}] {name}")


def quality(sid: str, start: str, end: str, expect: int = 200) -> dict:
    _, data = req("GET", COLLECTOR,
                  f"/stations/{sid}/quality?start={start}&end={end}", expect=expect)
    return data


def collect(hour: str, expect: int = 200) -> dict:
    _, r = req("POST", COLLECTOR, "/collections", {"hour": hour}, expect=expect)
    return r


def point(series: dict, hour: str) -> dict:
    for p in series["hours"]:
        if p["hour"] == hour:
            return p
    raise KeyError(hour)


def main() -> int:
    print(f"== quality smoke: collector={COLLECTOR} source={SOURCE}")

    print("-- 0. 健康检查与文档注册")
    req("GET", SOURCE, "/healthz")
    req("GET", COLLECTOR, "/healthz")
    _, spec = req("GET", COLLECTOR, "/openapi.json")
    check("/docs 含质量序列 API", "/stations/{station_id}/quality" in spec["paths"])

    print("-- 1. 重置并布置受控数据:S01 恒为 42,S03 恒为 20,S02 每小时变化")
    req("POST", SOURCE, "/admin/reset", {"station_count": 10, "seed": 777})
    req("POST", SOURCE, "/admin/publish",
        {"ops": [
            {"station_id": "S01", "op": "upsert", "pm25": 42.0},
            {"station_id": "S02", "op": "upsert", "pm25": 10.0},
            {"station_id": "S03", "op": "upsert", "pm25": 20.0},
        ]})

    print("-- 2. 采集 H0..H5(6 个连续小时),每次发布只改 S02")
    for h in HOURS[0:6]:
        idx = int(h[-2:])
        req("POST", SOURCE, "/admin/publish",
            {"ops": [{"station_id": "S02", "op": "upsert", "pm25": 10.0 + idx}]})
        r = collect(h)
        check(f"{h} 采集成功", r["status"] in ("ok", "up_to_date"), json.dumps(r))

    print("-- 3. H6 缺采;H7 时 S03 仍在,H8 起删除 S03")
    req("POST", SOURCE, "/admin/publish",
        {"ops": [{"station_id": "S02", "op": "upsert", "pm25": 77.7}]})
    check("H7 采集成功", collect(HOURS[7])["status"] == "ok")
    req("POST", SOURCE, "/admin/publish",
        {"ops": [{"station_id": "S03", "op": "delete"}]})
    check("H8 采集成功(含删除)", collect(HOURS[8])["status"] == "ok")
    req("POST", SOURCE, "/admin/publish",
        {"ops": [{"station_id": "S02", "op": "upsert", "pm25": 88.8}]})
    check("H9 采集成功", collect(HOURS[9])["status"] == "ok")

    print("-- 4. S01 平线判定:6 连平 + 缺小时截断 + 后续 3 小时不连坐")
    s01 = quality("S01", HOURS[0], HOURS[9])
    check("返回 10 个整点", s01["hour_count"] == 10)
    for h in HOURS[0:6]:
        p = point(s01, h)
        check(f"S01 {h} 平线且质量供数置空",
              p["status"] == "flatline" and p["pm25"] == 42.0 and p["quality_pm25"] is None
              and isinstance(p["version"], int), json.dumps(p))
    check("flatline_hours=H0..H5",
          s01["flatline_hours"] == HOURS[0:6], json.dumps(s01["flatline_hours"]))
    p6 = point(s01, HOURS[6])
    check("H6 缺采集 -> missing 且全空",
          p6["status"] == "missing" and p6["pm25"] is None and p6["version"] is None,
          json.dumps(p6))
    for h in HOURS[7:10]:
        p = point(s01, h)
        check(f"S01 {h} 缺口后同值但不满 6 -> ok,质量供数保留",
              p["status"] == "ok" and p["pm25"] == 42.0 and p["quality_pm25"] == 42.0,
              json.dumps(p))

    print("-- 5. S03:6 连平后缺小时、单小时 ok、删除截断")
    s03 = quality("S03", HOURS[0], HOURS[9])
    for h in HOURS[0:6]:
        p = point(s03, h)
        check(f"S03 {h} 平线(20.0)", p["status"] == "flatline" and p["pm25"] == 20.0
              and p["quality_pm25"] is None, json.dumps(p))
    check("S03 H7 缺口后单小时 ok",
          (lambda p: p["status"] == "ok" and p["pm25"] == 20.0)(point(s03, HOURS[7])))
    for h in (HOURS[8], HOURS[9]):
        p = point(s03, h)
        check(f"S03 {h} 删除 -> deleted,读数空但带确认版",
              p["status"] == "deleted" and p["pm25"] is None and p["quality_pm25"] is None
              and isinstance(p["version"], int), json.dumps(p))

    print("-- 6. S02 每小时读数不同:全部 ok,质量供数=读数")
    s02 = quality("S02", HOURS[0], HOURS[9])
    check("S02 无平线", s02["flatline_hours"] == [])
    seen_values = set()
    for h in HOURS[0:6] + HOURS[7:10]:
        p = point(s02, h)
        check(f"S02 {h} ok 且质量供数保留",
              p["status"] == "ok" and p["pm25"] is not None and p["quality_pm25"] == p["pm25"],
              json.dumps(p))
        seen_values.add(p["pm25"])
    check("S02 各小时读数确有变化", len(seen_values) >= 7, str(seen_values))
    check("S02 H6 missing", point(s02, HOURS[6])["status"] == "missing")

    print("-- 7. 范围边缘:窗口只取平线段中间 3 小时,仍判 flatline(考虑窗口外连续读数)")
    edge = quality("S01", HOURS[2], HOURS[4])
    check("边缘窗口 3 小时全平线",
          [p["status"] for p in edge["hours"]] == ["flatline"] * 3
          and edge["flatline_hours"] == HOURS[2:5], json.dumps(edge))

    print("-- 8. 修订一小时:邻近小时同步改判,平线整体解除")
    req("POST", SOURCE, "/admin/publish",
        {"ops": [{"station_id": "S01", "op": "upsert", "pm25": 99.0}]})
    r = collect(HOURS[4])
    check("H4 修订采集成功", r["status"] == "ok", json.dumps(r))
    s01b = quality("S01", HOURS[0], HOURS[9])
    check("修订后 S01 全线无平线", s01b["flatline_hours"] == [], json.dumps(s01b))
    p4 = point(s01b, HOURS[4])
    check("H4 读数已改 99 且供数保留", p4["pm25"] == 99.0 and p4["quality_pm25"] == 99.0)
    for h in (HOURS[3], HOURS[5]):
        check(f"邻近 {h} 同步改判 ok", point(s01b, h)["status"] == "ok")

    print("-- 9. 按版查询保留原值:历史版读数不受质量标记与修订影响")
    _, vers = req("GET", COLLECTOR, f"/hours/{HOURS[4]}/versions")
    vlist = [v["version"] for v in vers["versions"]]
    vals = {}
    for v in vlist:
        _, d = req("GET", COLLECTOR, f"/hours/{HOURS[4]}/data?version={v}")
        vals[v] = {s["station_id"]: s["pm25"] for s in d["stations"]}["S01"]
    check("最新版为修订值 99", vals[vlist[-1]] == 99.0, str(vals[vlist[-1]]))
    check("修订前的历史版仍保留原值 42",
          any(val == 42.0 for v, val in vals.items() if v < vlist[-1]), str(vals))
    # 被删站:删除前采集的小时仍保留读数;删除后采集的小时当前版已无该站
    _, h5v1 = req("GET", COLLECTOR, f"/hours/{HOURS[5]}/data?version=1")
    _, h5cur = req("GET", COLLECTOR, f"/hours/{HOURS[5]}/data")
    _, h8cur = req("GET", COLLECTOR, f"/hours/{HOURS[8]}/data")
    check("S03 删除前小时的历史版读数仍在",
          "S03" in {s["station_id"] for s in h5v1["stations"]})
    check("S03 在删除前小时的当前版中保留(该小时未再修订)",
          "S03" in {s["station_id"] for s in h5cur["stations"]})
    check("S03 在删除后小时的当前版中已消失",
          "S03" not in {s["station_id"] for s in h8cur["stations"]})

    print("-- 10. 回退修订:平线随确认版重新成立,邻近小时再次改判")
    req("POST", SOURCE, "/admin/publish",
        {"ops": [{"station_id": "S01", "op": "upsert", "pm25": 42.0}]})
    check("H4 回退采集成功", collect(HOURS[4])["status"] == "ok")
    s01c = quality("S01", HOURS[0], HOURS[9])
    check("回退后平线 H0..H5 恢复", s01c["flatline_hours"] == HOURS[0:6],
          json.dumps(s01c["flatline_hours"]))

    print("-- 11. 参数校验:48 小时上限、起止顺序、格式、未知站")
    quality("S01", "2026-11-20T00", "2026-11-21T23", expect=200)  # 恰好 48 小时
    quality("S01", "2026-11-20T00", "2026-11-22T00", expect=400)  # 49 小时拒绝
    quality("S01", HOURS[5], HOURS[3], expect=400)
    quality("S01", "2026-11-20-10", HOURS[1], expect=400)
    quality("S01", "not-a-hour", HOURS[1], expect=400)
    quality("X99-UNKNOWN", HOURS[0], HOURS[9], expect=404)

    print("-- 12. 采集失败:标记与读数保持一致,指针不动")
    before = quality("S01", HOURS[0], HOURS[9])
    _, state = req("GET", SOURCE, "/admin/state")
    next_ver = state["current_version"] + 1
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": next_ver, "page": 1, "kind": "missing", "times": -1}]})
    req("POST", SOURCE, "/admin/publish",
        {"ops": [{"station_id": "S02", "op": "upsert", "pm25": 55.5}]})
    r = collect(HOURS[4])
    check("故障下采集失败", r["status"] == "failed", json.dumps(r))
    _, status4 = req("GET", COLLECTOR, f"/collections/{HOURS[4]}")
    check("失败后小时状态 failed", status4["status"] == "failed")
    after = quality("S01", HOURS[0], HOURS[9])
    check("失败前后质量序列完全一致", after == before, "marks drifted after failed collection")
    req("DELETE", SOURCE, "/admin/faults")
    check("清障后补齐成功", collect(HOURS[4])["status"] == "ok")
    recovered = quality("S01", HOURS[0], HOURS[9])
    check("补齐后平线依旧", recovered["flatline_hours"] == HOURS[0:6])

    print("-- 13. 并发:采集进行中并发查询,无 500 且每条记录自洽")
    req("POST", SOURCE, "/admin/publish",
        {"ops": [{"station_id": "S02", "op": "upsert", "pm25": 66.6}]})
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def burst() -> None:
        barrier.wait()
        for _ in range(25):
            try:
                q = quality("S01", HOURS[0], HOURS[9])
                for p in q["hours"]:
                    if p["status"] == "flatline" and p["quality_pm25"] is not None:
                        errors.append("flatline point carries quality value")
                    if p["status"] == "ok" and p["quality_pm25"] != p["pm25"]:
                        errors.append("ok point quality mismatch")
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

    t = threading.Thread(target=burst)
    t.start()
    barrier.wait()
    collect(HOURS[0])
    t.join()
    check("25 次并发查询全部自洽无错误", not errors, str(errors[:3]))

    print("-- 14. 其他小时采集互不影响:H0 修订后 H1..H5 平线仍在")
    final = quality("S01", HOURS[0], HOURS[9])
    check("最终平线仍为 H0..H5", final["flatline_hours"] == HOURS[0:6],
          json.dumps(final["flatline_hours"]))

    print(f"== quality smoke 全部通过 ({_checks} 项检查)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
