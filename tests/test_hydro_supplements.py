"""M5 雨量补报：累计补报、逐日补报、月量回读，两种观测依据分开保存。

覆盖：闰日、跨月、部分重叠、重叠只计一次、日值与累计不符、跨月归属待定、
过期决定拒绝、并发补报、观测保存中断后恢复核算、同笔请求重试不新增观测、
核算失败仍读原月量。电表计费与单位换算不在本文件内，由既有 M5/M7 测试守住。
"""
from __future__ import annotations

import tempfile
import threading
from decimal import Decimal

import pytest

from app import errors
from app.modules.m5_months import ledger, supplement
from app.store import Store


@pytest.fixture()
def hydro_store(tmp_path) -> Store:
    return Store(str(tmp_path / "hydro.sqlite"))


def submit(store, **body):
    return supplement.submit(store, body)


def months_of(result):
    return {row["month"]: row for row in result["months"]}


def month_view(store, station, month):
    rows = [row for row in supplement.view(store, station)["months"]
            if row["month"] == month]
    assert len(rows) == 1, rows
    return rows[0]


# ---------- 逐日补报：零雨保留，缺测不补 ----------
def test_daily_supplement_keeps_zero_and_counts_day(hydro_store):
    result = submit(hydro_store, request_id="d1", station="0012",
                    day="2024-02-01", rainfall_mm="0.0")
    assert result["decision"]["status"] == "accepted"
    row = month_view(hydro_store, "0012", "2024-02")
    assert row["daily_mm"] == "0.0"
    assert row["rainfall_mm"] == "0.0"
    assert row["days"] == 1
    assert row["missing_days"] == 28
    assert row["calendar_days"] == 29


def test_daily_supplement_requires_request_id(hydro_store):
    with pytest.raises(errors.BadRequest) as excinfo:
        submit(hydro_store, station="0012", day="2024-02-01", rainfall_mm="1.0")
    assert excinfo.value.code == "E_HYDRO_BAD_OBSERVATION"


def test_daily_and_cumulative_are_read_back_separately(hydro_store):
    submit(hydro_store, request_id="d1", station="0012",
           day="2024-02-27", rainfall_mm="4.0")
    result = submit(hydro_store, request_id="c1", station="0012",
                    start_day="2024-02-27", end_day="2024-02-29",
                    total_mm="12.0")
    row = months_of(result)["2024-02"]
    # 累计 12.0 覆盖 3 天，其中 27 日有逐日 4.0；同日只计一次，逐日转旁证。
    assert row["cumulative_mm"] == "12.0"
    assert row["daily_mm"] == "0.0"
    assert row["rainfall_mm"] == "12.0"
    assert row["shadowed_daily_mm"] == "4.0"
    # 逐日缺测仍保留：累计不拆日值，实有日只承认 27 日那一条逐日观测。
    assert row["days"] == 1
    assert row["missing_days"] == 28


def test_cumulative_does_not_fabricate_daily_values(hydro_store):
    submit(hydro_store, request_id="c1", station="0012",
           start_day="2024-02-01", end_day="2024-02-10", total_mm="50.0")
    store_obs = hydro_store.load_hydro_observations("0012")
    assert len(store_obs) == 1
    assert store_obs[0]["kind"] == "cumulative"
    assert store_obs[0]["day"] is None
    assert store_obs[0]["start_day"] == "2024-02-01"
    assert store_obs[0]["total_mm"] == Decimal("50.0")
    row = month_view(hydro_store, "0012", "2024-02")
    assert row["cumulative_mm"] == "50.0"
    assert row["daily_mm"] == "0.0"
    assert row["days"] == 0
    assert row["missing_days"] == 29


# ---------- 跨月与归属 ----------
def test_cross_month_without_attribution_is_pending(hydro_store):
    result = submit(hydro_store, request_id="c1", station="0012",
                    start_day="2024-01-30", end_day="2024-02-02", total_mm="30.0")
    assert result["decision"]["status"] == "pending"
    assert result["decision"]["reason"] == "K_HYDRO_ATTRIBUTION_PENDING"
    january = months_of(result)["2024-01"]
    february = months_of(result)["2024-02"]
    assert january["rainfall_mm"] == "0.0"
    assert february["rainfall_mm"] == "0.0"
    assert january["state"] == "pending" and february["state"] == "pending"
    assert january["pending_reported_mm"] == "30.0"   # 待定总量只挂起点月
    assert february["pending_reported_mm"] == "0.0"
    assert january["pending_count"] == 1


def test_cross_month_attribution_is_counted_by_month(hydro_store):
    result = submit(hydro_store, request_id="c1", station="0012",
                    start_day="2024-01-30", end_day="2024-02-02",
                    total_mm="30.0",
                    attribution={"2024-01": "10.0", "2024-02": "20.0"})
    assert result["decision"]["status"] == "accepted"
    assert result["decision"]["months"] == {"2024-01": "10.0", "2024-02": "20.0"}
    assert months_of(result)["2024-01"]["rainfall_mm"] == "10.0"
    assert months_of(result)["2024-02"]["rainfall_mm"] == "20.0"


def test_attribution_must_cover_every_month_and_add_up(hydro_store):
    with pytest.raises(errors.BadRequest) as partial:
        submit(hydro_store, request_id="c1", station="0012",
               start_day="2024-01-30", end_day="2024-02-02", total_mm="30.0",
               attribution={"2024-02": "30.0"})
    assert partial.value.code == "E_HYDRO_ATTRIBUTION_INVALID"

    with pytest.raises(errors.BadRequest) as misadd:
        submit(hydro_store, request_id="c2", station="0012",
               start_day="2024-01-30", end_day="2024-02-02", total_mm="30.0",
               attribution={"2024-01": "10.0", "2024-02": "19.0"})
    assert misadd.value.code == "E_HYDRO_ATTRIBUTION_INVALID"


def test_pending_observation_is_recomputed_once_evidence_arrives(hydro_store):
    pending = submit(hydro_store, request_id="c1", station="0012",
                     start_day="2024-01-30", end_day="2024-02-02", total_mm="30.0")
    assert pending["decision"]["status"] == "pending"

    fixed = submit(hydro_store, request_id="c2", station="0012",
                   start_day="2024-01-30", end_day="2024-02-02", total_mm="30.0",
                   attribution={"2024-01": "12.0", "2024-02": "18.0"}, version=2)
    assert fixed["decision"]["status"] == "accepted"
    assert months_of(fixed)["2024-01"]["rainfall_mm"] == "12.0"
    assert months_of(fixed)["2024-01"]["state"] != "pending"
    decisions = {item["observation_id"]: item
                 for item in supplement.view(hydro_store, "0012")["decisions"]}
    assert decisions[pending["observation"]["observation_id"]]["status"] == "superseded"


# ---------- 重叠覆盖只计一次 ----------
def test_full_overlap_is_counted_once(hydro_store):
    first = submit(hydro_store, request_id="c1", station="0012",
                   start_day="2024-02-01", end_day="2024-02-05", total_mm="20.0")
    second = submit(hydro_store, request_id="c2", station="0012",
                    start_day="2024-02-03", end_day="2024-02-04", total_mm="8.0")
    assert first["decision"]["status"] == "accepted"
    assert second["decision"]["status"] == "deduped"
    assert second["decision"]["reason"] == "K_HYDRO_COVERAGE_DEDUPED"
    row = month_view(hydro_store, "0012", "2024-02")
    assert row["cumulative_mm"] == "20.0"       # 不是 28.0
    assert row["rainfall_mm"] == "20.0"
    assert row["conflict_count"] == 0


def test_identical_duplicate_observation_is_deduped(hydro_store):
    first = submit(hydro_store, request_id="c1", station="0012",
                   start_day="2024-02-01", end_day="2024-02-03", total_mm="9.0")
    second = submit(hydro_store, request_id="c2", station="0012",
                    start_day="2024-02-01", end_day="2024-02-03", total_mm="9.0")
    assert first["decision"]["status"] == "accepted"
    assert second["decision"]["status"] == "deduped"
    assert month_view(hydro_store, "0012", "2024-02")["cumulative_mm"] == "9.0"


def test_partial_overlap_is_isolated_not_merged(hydro_store):
    submit(hydro_store, request_id="c1", station="0012",
           start_day="2024-02-01", end_day="2024-02-05", total_mm="20.0")
    result = submit(hydro_store, request_id="c2", station="0012",
                    start_day="2024-02-04", end_day="2024-02-08", total_mm="15.0")
    assert result["decision"]["status"] == "overlap_conflict"
    assert result["decision"]["reason"] == "Q_HYDRO_CONFLICT_CUMULATIVE_OVERLAP"
    assert result["decision"]["detail"]["overlap_days"] == 2
    row = months_of(result)["2024-02"]
    assert row["cumulative_mm"] == "20.0"       # 冲突观测不进月量
    assert row["conflict_count"] == 1
    assert row["state"] == "conflict"
    # 冲突观测没有被静默拆分，原值仍在证据里
    stored = hydro_store.load_hydro_observations("0012")
    conflict = next(item for item in stored if item["start_day"] == "2024-02-04")
    assert conflict["total_mm"] == Decimal("15.0")


# ---------- 日值与累计不符 → 隔离冲突 ----------
def test_daily_and_cumulative_mismatch_isolates_cumulative(hydro_store):
    for index, (day, value) in enumerate(
            (("2024-02-01", "2.0"), ("2024-02-02", "3.0"), ("2024-02-03", "4.0"))):
        submit(hydro_store, request_id=f"d{index}", station="0012",
               day=day, rainfall_mm=value)
    result = submit(hydro_store, request_id="c1", station="0012",
                    start_day="2024-02-01", end_day="2024-02-03", total_mm="12.0")
    assert result["decision"]["status"] == "daily_conflict"
    assert result["decision"]["reason"] == "Q_HYDRO_CONFLICT_DAILY_CUMULATIVE"
    assert result["decision"]["detail"]["daily_sum"] == "9.0"
    assert result["decision"]["detail"]["difference"] == "-3.0"
    row = months_of(result)["2024-02"]
    assert row["daily_mm"] == "9.0"      # 月量退回日值口径
    assert row["cumulative_mm"] == "0.0"
    assert row["state"] == "conflict"
    # 两种观测依据都留着：逐日 3 条 + 累计 1 条
    stored = hydro_store.load_hydro_observations("0012")
    assert [item["kind"] for item in stored].count("daily") == 3
    assert [item["kind"] for item in stored].count("cumulative") == 1


def test_matching_daily_and_cumulative_are_reconciled(hydro_store):
    for index, (day, value) in enumerate(
            (("2024-02-01", "2.0"), ("2024-02-02", "3.0"), ("2024-02-03", "4.0"))):
        submit(hydro_store, request_id=f"d{index}", station="0012",
               day=day, rainfall_mm=value)
    result = submit(hydro_store, request_id="c1", station="0012",
                    start_day="2024-02-01", end_day="2024-02-03", total_mm="9.0")
    assert result["decision"]["status"] == "accepted"
    assert result["decision"]["reason"] == "K_HYDRO_DAILY_RECONCILED"
    row = months_of(result)["2024-02"]
    assert row["rainfall_mm"] == "9.0"       # 不重复相加
    assert row["days"] == 3
    assert row["missing_days"] == 26


def test_same_version_conflict_isolates_both_rows(hydro_store):
    first = submit(hydro_store, request_id="d1", station="0012",
                   day="2024-02-29", rainfall_mm="1.0")
    assert first["decision"]["status"] == "accepted"      # 当时还没有对手
    second = submit(hydro_store, request_id="d2", station="0012",
                    day="2024-02-29", rainfall_mm="2.0")
    assert second["decision"]["status"] == "version_conflict"
    assert second["decision"]["reason"] == "Q_HYDRO_CONFLICT_VERSION"
    # 后者到来后前者也一并被隔离，两条原值都留着，不猜哪条为准
    decisions = {item["observation_id"]: item["status"]
                 for item in supplement.view(hydro_store, "0012")["decisions"]}
    assert decisions[first["observation"]["observation_id"]] == "version_conflict"
    assert decisions[second["observation"]["observation_id"]] == "version_conflict"
    assert len(hydro_store.load_hydro_observations("0012")) == 2
    row = month_view(hydro_store, "0012", "2024-02")
    assert row["rainfall_mm"] == "0.0"
    assert row["state"] == "conflict"


def test_higher_version_wins_without_dropping_evidence(hydro_store):
    submit(hydro_store, request_id="d1", station="0012",
           day="2024-02-29", rainfall_mm="1.0", version=1)
    result = submit(hydro_store, request_id="d2", station="0012",
                    day="2024-02-29", rainfall_mm="6.0", version=2)
    assert result["decision"]["status"] == "accepted"
    assert month_view(hydro_store, "0012", "2024-02")["rainfall_mm"] == "6.0"
    assert len(hydro_store.load_hydro_observations("0012")) == 2


def test_leap_day_is_a_real_day(hydro_store):
    result = submit(hydro_store, request_id="d1", station="0012",
                    day="2024-02-29", rainfall_mm="7.5")
    row = months_of(result)["2024-02"]
    assert row["calendar_days"] == 29
    assert row["days"] == 1
    assert row["missing_days"] == 28
    assert row["rainfall_mm"] == "7.5"


def test_non_leap_day_is_rejected(hydro_store):
    with pytest.raises(errors.BadRequest) as excinfo:
        submit(hydro_store, request_id="d1", station="0012",
               day="2023-02-29", rainfall_mm="7.5")
    assert excinfo.value.code == "E_HYDRO_BAD_OBSERVATION"


def test_leap_month_reports_partial_until_all_days_exist(hydro_store):
    days = [f"2024-02-{day:02d}" for day in range(1, 30)]
    for index, day in enumerate(days):
        submit(hydro_store, request_id=f"d{index}", station="0012",
               day=day, rainfall_mm="1.0")
    row = month_view(hydro_store, "0012", "2024-02")
    assert row["days"] == 29
    assert row["missing_days"] == 0
    assert row["state"] == "complete"
    assert row["rainfall_mm"] == "29.0"


# ---------- 过期决定与并发 ----------
def test_stale_base_revision_is_rejected(hydro_store):
    submit(hydro_store, request_id="d1", station="0012",
           day="2024-02-01", rainfall_mm="1.0")
    with pytest.raises(errors.Conflict) as excinfo:
        submit(hydro_store, request_id="d2", station="0012",
               day="2024-02-02", rainfall_mm="2.0", base_revision=0)
    assert excinfo.value.code == "E_HYDRO_STALE_REVISION"
    assert len(hydro_store.load_hydro_observations("0012")) == 1
    assert month_view(hydro_store, "0012", "2024-02")["days"] == 1


def test_concurrent_supplements_only_first_decision_survives(hydro_store):
    barrier = threading.Barrier(2)
    outcomes: list = []

    def worker(index: int):
        barrier.wait()
        try:
            outcomes.append(submit(hydro_store, request_id=f"d{index}",
                                   station="0012", day=f"2024-02-0{index + 1}",
                                   rainfall_mm="1.0", base_revision=0))
        except errors.MeterbackError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ok = [item for item in outcomes if isinstance(item, dict)]
    rejected = [item for item in outcomes if isinstance(item, errors.MeterbackError)]
    assert len(ok) == 1
    assert len(rejected) == 1
    assert rejected[0].code == "E_HYDRO_STALE_REVISION"
    assert len(hydro_store.load_hydro_observations("0012")) == 1
    assert month_view(hydro_store, "0012", "2024-02")["days"] == 1


# ---------- 幂等重试 ----------
def test_same_request_retry_adds_no_observation(hydro_store):
    body = dict(request_id="same-1", station="0012", day="2024-02-01",
                rainfall_mm="5.0")
    first = supplement.submit(hydro_store, body)
    second = supplement.submit(hydro_store, body)
    assert first["new_observation"] is True
    assert second["new_observation"] is False
    assert second["replay"] is True
    assert second["observation"]["observation_id"] == \
        first["observation"]["observation_id"]
    assert len(hydro_store.load_hydro_observations("0012")) == 1
    assert month_view(hydro_store, "0012", "2024-02")["rainfall_mm"] == "5.0"


def test_same_request_id_with_different_payload_is_rejected(hydro_store):
    supplement.submit(hydro_store, {"request_id": "same-1", "station": "0012",
                                    "day": "2024-02-01", "rainfall_mm": "5.0"})
    with pytest.raises(errors.Conflict) as excinfo:
        supplement.submit(hydro_store, {"request_id": "same-1", "station": "0012",
                                        "day": "2024-02-01", "rainfall_mm": "6.0"})
    assert excinfo.value.code == "E_HYDRO_REQUEST_REUSED"
    assert len(hydro_store.load_hydro_observations("0012")) == 1


# ---------- 观测保存中断 / 核算中断后的恢复 ----------
def test_record_without_recompute_is_recovered(hydro_store):
    """观测先落库、核算没跑（进程中断）：待重算表留下站点，恢复后月量补齐。"""
    supplement.record(hydro_store, {"request_id": "d1", "station": "0012",
                                    "day": "2024-02-01", "rainfall_mm": "5.0"})
    assert hydro_store.load_hydro_months("0012") == []
    assert [item["station"] for item in hydro_store.load_hydro_recompute()] == ["0012"]

    recovered = supplement.recover(hydro_store)
    assert recovered["recovered"] == ["0012"]
    assert recovered["failed"] == []
    assert recovered["remaining"] == []
    assert month_view(hydro_store, "0012", "2024-02")["rainfall_mm"] == "5.0"


def test_failed_recompute_keeps_original_months(hydro_store, monkeypatch):
    submit(hydro_store, request_id="d1", station="0012",
           day="2024-02-01", rainfall_mm="5.0")
    before = month_view(hydro_store, "0012", "2024-02")

    def boom(*args, **kwargs):
        raise RuntimeError("核算被中断")

    monkeypatch.setattr(hydro_store, "replace_hydro_months", boom)
    with pytest.raises(errors.MeterbackError) as excinfo:
        submit(hydro_store, request_id="d2", station="0012",
               day="2024-02-02", rainfall_mm="3.0")
    assert excinfo.value.code == "E_HYDRO_RECOMPUTE_FAILED"
    # 观测已保存，重试不会丢；回读到的仍是原月量
    assert len(hydro_store.load_hydro_observations("0012")) == 2
    assert month_view(hydro_store, "0012", "2024-02") == before
    assert hydro_store.load_hydro_recompute()[0]["station"] == "0012"

    monkeypatch.undo()
    supplement.recover(hydro_store)
    row = month_view(hydro_store, "0012", "2024-02")
    assert row["rainfall_mm"] == "8.0"
    assert row["days"] == 2


def test_failed_observation_insert_leaves_months_untouched(hydro_store, monkeypatch):
    submit(hydro_store, request_id="d1", station="0012",
           day="2024-02-01", rainfall_mm="5.0")
    before = month_view(hydro_store, "0012", "2024-02")

    def boom(*args, **kwargs):
        raise RuntimeError("磁盘写入失败")

    monkeypatch.setattr(hydro_store, "insert_hydro_observation", boom)
    with pytest.raises(errors.MeterbackError) as excinfo:
        submit(hydro_store, request_id="d2", station="0012",
               day="2024-02-02", rainfall_mm="3.0")
    assert excinfo.value.code == "E_HYDRO_RECOMPUTE_FAILED"
    assert len(hydro_store.load_hydro_observations("0012")) == 1
    assert month_view(hydro_store, "0012", "2024-02") == before


def test_recover_is_idempotent(hydro_store):
    supplement.record(hydro_store, {"request_id": "d1", "station": "0012",
                                    "day": "2024-02-01", "rainfall_mm": "5.0"})
    supplement.recover(hydro_store)
    first = month_view(hydro_store, "0012", "2024-02")
    supplement.recover(hydro_store)
    assert month_view(hydro_store, "0012", "2024-02") == first


def test_same_request_retry_after_recompute_failure_finishes_job(hydro_store,
                                                                 monkeypatch):
    submit(hydro_store, request_id="d1", station="0012",
           day="2024-02-01", rainfall_mm="5.0")

    def boom(*args, **kwargs):
        raise RuntimeError("核算被中断")

    monkeypatch.setattr(hydro_store, "replace_hydro_months", boom)
    with pytest.raises(errors.MeterbackError):
        submit(hydro_store, request_id="d2", station="0012",
               day="2024-02-02", rainfall_mm="3.0")
    monkeypatch.undo()

    retry = submit(hydro_store, request_id="d2", station="0012",
                   day="2024-02-02", rainfall_mm="3.0")
    assert retry["replay"] is True and retry["new_observation"] is False
    assert len(hydro_store.load_hydro_observations("0012")) == 2
    row = month_view(hydro_store, "0012", "2024-02")
    assert row["rainfall_mm"] == "8.0"
    assert hydro_store.load_hydro_recompute() == []


# ---------- 纯函数：输入顺序不影响判定 ----------
def test_resolve_is_order_independent():
    base = [
        {"observation_id": "a", "request_id": "a", "station": "0012", "kind": "daily",
         "day": "2024-02-01", "start_day": None, "end_day": None,
         "rainfall_mm": "2.0", "total_mm": None, "attribution": {}, "version": 1, "seq": 1},
        {"observation_id": "b", "request_id": "b", "station": "0012",
         "kind": "cumulative", "day": None, "start_day": "2024-01-31",
         "end_day": "2024-02-02", "rainfall_mm": None, "total_mm": "12.0",
         "attribution": {"2024-01": "5.0", "2024-02": "7.0"}, "version": 1, "seq": 2},
    ]
    forward = ledger.resolve("0012", base)
    backward = ledger.resolve("0012", list(reversed(base)))
    assert forward["months"] == backward["months"]
    assert [(item["observation_id"], item["status"]) for item in forward["decisions"]] \
        == [(item["observation_id"], item["status"]) for item in backward["decisions"]]


def test_month_spans_and_day_span_cover_leap_year():
    assert ledger.month_spans("2024-02-28", "2024-03-01") == ["2024-02", "2024-03"]
    assert len(ledger.day_span("2024-02-28", "2024-03-01")) == 3
    assert ledger.calendar_days("2024-02") == 29
    assert ledger.calendar_days("2023-02") == 28


# ---------- HTTP 接口 ----------
def test_http_supplement_roundtrip(client):
    daily = client.post("/api/hydro/supplements/daily", json={
        "request_id": "http-1", "station": "0012",
        "day": "2024-02-29", "rainfall_mm": "3.0"})
    assert daily.status_code == 200
    assert daily.json()["decision"]["status"] == "accepted"

    cumulative = client.post("/api/hydro/supplements/cumulative", json={
        "request_id": "http-2", "station": "0012",
        "start_day": "2024-02-27", "end_day": "2024-02-29", "total_mm": "10.0"})
    assert cumulative.status_code == 200
    # 29 日逐日 3.0 与累计 10.0 冲突（日值合计 3.0 ≠ 10.0 且 3.0 < 10.0，视为不完整覆盖）
    assert cumulative.json()["decision"]["status"] == "accepted"
    assert cumulative.json()["decision"]["reason"] == "K_HYDRO_CUMULATIVE_INCLUDED"

    read = client.get("/api/hydro/months?station=0012")
    assert read.status_code == 200
    body = read.json()
    month = next(row for row in body["months"] if row["month"] == "2024-02")
    assert month["cumulative_mm"] == "10.0"
    assert month["daily_mm"] == "0.0"
    assert month["days"] == 1


def test_http_stale_revision_is_409(client):
    client.post("/api/hydro/supplements/daily", json={
        "request_id": "http-1", "station": "0012",
        "day": "2024-02-01", "rainfall_mm": "1.0"})
    response = client.post("/api/hydro/supplements/daily", json={
        "request_id": "http-2", "station": "0012",
        "day": "2024-02-02", "rainfall_mm": "1.0", "base_revision": 0})
    assert response.status_code == 409
    assert response.json()["error"] == "E_HYDRO_STALE_REVISION"


def test_http_bad_payload_is_400(client):
    response = client.post("/api/hydro/supplements/daily", json={
        "station": "0012", "day": "2024-02-01", "rainfall_mm": "1.0"})
    assert response.status_code == 400
    assert response.json()["error"] == "E_HYDRO_BAD_OBSERVATION"


def test_http_recompute_recovers_pending(client, app):
    from app.modules.m5_months import supplement as supplement_mod
    store = app.state.store
    supplement_mod.record(store, {"request_id": "http-1", "station": "0012",
                                  "day": "2024-02-01", "rainfall_mm": "5.0"})
    assert store.load_hydro_months("0012") == []
    response = client.post("/api/hydro/recompute", json={})
    assert response.status_code == 200
    assert response.json()["recovered"] == ["0012"]
    month = client.get("/api/hydro/months?station=0012").json()["months"][0]
    assert month["rainfall_mm"] == "5.0"


def test_months_page_renders_supplement_ledger(client):
    client.post("/api/hydro/supplements/cumulative", json={
        "request_id": "page-1", "station": "0012",
        "start_day": "2024-01-30", "end_day": "2024-02-02", "total_mm": "30.0"})
    page = client.get("/months")
    assert page.status_code == 200
    assert "补报依据" in page.text
    assert "0012" in page.text
    assert "跨月归属待定" in page.text
