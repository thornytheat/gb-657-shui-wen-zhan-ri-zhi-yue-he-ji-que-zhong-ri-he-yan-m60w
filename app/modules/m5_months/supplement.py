"""雨量补报编排：写入观测 → 重算站点 → 落库月量。

三条硬约束在这里落地：
1. 过期决定：请求带 `base_revision`（用户看到的站点决定号），小于当前版本即
   拒绝（409 `E_HYDRO_STALE_REVISION`），旧视图不可能覆盖新决定。
2. 观测保存与核算分开：观测一落库就提交，核算失败只登记待重算，绝不丢观测。
3. 同笔请求重试不新增观测：`request_id` 唯一，载荷摘要一致即原样返回首次结果。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from ... import config, errors, reasons
from . import ledger

KIND_DAILY = "daily"
KIND_CUMULATIVE = "cumulative"


def _stamp() -> str:
    return dt.datetime.now(config.UTC).isoformat()


def _canonical(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)


def request_digest(body: dict) -> str:
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


# ---------- 载荷校验 ----------
def base_revision(body: dict) -> int | None:
    """请求所依据的站点决定号；未给出表示「不对版本作要求」。"""
    raw = body.get("base_revision")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise errors.BadRequest("base_revision 必须是整数",
                                code="E_HYDRO_BAD_OBSERVATION") from exc


def _version(body: dict) -> int:
    raw = body.get("version", 1)
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise errors.BadRequest("version 必须是整数",
                                code="E_HYDRO_BAD_OBSERVATION") from exc
    if version < 1:
        raise errors.BadRequest("version 必须 ≥ 1", code="E_HYDRO_BAD_OBSERVATION")
    return version


def _text(value) -> str:
    number = ledger.as_decimal(value)
    if number is None or number < 0:
        raise errors.BadRequest("雨量必须是非负数", code="E_HYDRO_BAD_OBSERVATION")
    return str(ledger.quantize(number))


def daily_observation(body: dict) -> dict:
    day = ledger.parse_day(body.get("day") or body.get("date"))
    if day is None:
        raise errors.BadRequest("逐日补报缺少合法的 day（YYYY-MM-DD）",
                                code="E_HYDRO_BAD_OBSERVATION")
    return {"kind": KIND_DAILY, "day": day.isoformat(),
            "rainfall_mm": _text(body.get("rainfall_mm")), "total_mm": None,
            "start_day": None, "end_day": None, "attribution": {}}


def cumulative_observation(body: dict) -> dict:
    start = ledger.parse_day(body.get("start_day") or body.get("start"))
    end = ledger.parse_day(body.get("end_day") or body.get("end"))
    if start is None or end is None or start > end:
        raise errors.BadRequest("累计补报需要 start_day ≤ end_day（YYYY-MM-DD）",
                                code="E_HYDRO_BAD_OBSERVATION")
    total = _text(body.get("total_mm"))
    attribution = _attribution(body.get("attribution"), start.isoformat(),
                               end.isoformat(), total)
    return {"kind": KIND_CUMULATIVE, "day": None, "rainfall_mm": None,
            "total_mm": total, "start_day": start.isoformat(),
            "end_day": end.isoformat(), "attribution": attribution}


def _attribution(raw, start: str, end: str, total: str) -> dict[str, str]:
    """分月归属证据：{月份: 雨量}。要么不给（跨月时状态待定），要么给全且加总等于总量。"""
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, dict):
        raise errors.BadRequest("attribution 必须是 {月份: 雨量} 对象",
                                code="E_HYDRO_ATTRIBUTION_INVALID")
    spans = ledger.month_spans(start, end)
    out: dict[str, str] = {}
    for month, value in raw.items():
        key = str(month).strip()
        if key not in spans:
            raise errors.BadRequest(f"归属月份 {key} 不在覆盖范围内",
                                    code="E_HYDRO_ATTRIBUTION_INVALID")
        out[key] = _text(value)
    if set(out) != set(spans):
        raise errors.BadRequest(
            "跨月累计补报必须给出覆盖范围内每个月的归属证据：" + "、".join(spans),
            code="E_HYDRO_ATTRIBUTION_INVALID")
    attributed = sum((ledger.as_decimal(value) for value in out.values()),
                     ledger.ZERO)
    if ledger.quantize(attributed) != ledger.quantize(ledger.as_decimal(total)):
        raise errors.BadRequest(
            f"分月归属加总 {attributed} mm 与累计总量 {total} mm 不符",
            code="E_HYDRO_ATTRIBUTION_INVALID")
    return out


# ---------- 写入 ----------
def record(store, body: dict) -> dict:
    """登记一条补报观测；同一 (station, kind, 覆盖键) 的多版本共存，重算时择优。"""
    request_id = str(body.get("request_id") or "").strip()
    if not request_id:
        raise errors.BadRequest("缺少 request_id（同笔请求重试靠它去重）",
                                code="E_HYDRO_BAD_OBSERVATION")
    kind = str(body.get("kind") or "").strip().lower()
    if not kind:
        # 未显式声明时按字段推断：给起止日/总量的是累计补报，否则是逐日补报。
        kind = (KIND_CUMULATIVE
                if any(body.get(key) not in (None, "")
                       for key in ("start_day", "start", "end_day", "end", "total_mm"))
                else KIND_DAILY)
    if kind in ("daily", "逐日", "日值"):
        parsed = daily_observation(body)
    elif kind in ("cumulative", "累计", "跨日累计"):
        parsed = cumulative_observation(body)
    else:
        raise errors.BadRequest(f"未知补报类型：{body.get('kind')!r}",
                                code="E_HYDRO_BAD_OBSERVATION")
    station = str(body.get("station") or "").strip()
    if not station:
        raise errors.BadRequest("缺少 station", code="E_HYDRO_BAD_OBSERVATION")

    digest = request_digest({**parsed, "station": station})
    # 依据版本：调用方显式给出就用它（基于旧视图的请求必须被拒）；没给就在拿
    # 写锁前乐观读一次当前决定号。两个真正并发的补报会读到同一个版本号，
    # 只有先拿到写锁的那个能过，后到的按「过期决定」拒绝。
    revision_seen = base_revision(body)
    if revision_seen is None:
        revision_seen = store.hydro_revision(station)
    version = _version(body)
    stamp = _stamp()
    observation_id = "ho-" + uuid.uuid4().hex[:16]
    try:
        with store.connect(immediate=True) as conn:
            existing = store.find_hydro_by_request(request_id, conn)
            if existing is not None:
                if existing["request_digest"] != digest:
                    raise errors.Conflict(
                        f"request_id {request_id} 已被另一笔补报占用",
                        code="E_HYDRO_REQUEST_REUSED",
                        detail={"request_id": request_id,
                                "observation_id": existing["observation_id"]})
                return {"observation": existing, "replay": True}
            current = store.hydro_revision(station, conn)
            if revision_seen < current:
                raise errors.Conflict(
                    f"站点 {station} 的决定已更新到第 {current} 版，"
                    f"请求基于第 {revision_seen} 版",
                    code="E_HYDRO_STALE_REVISION",
                    detail={"station": station, "base_revision": revision_seen,
                            "current_revision": current})
            revision = store.begin_hydro_write(conn, station, stamp)
            item = {
                "observation_id": observation_id, "request_id": request_id,
                "request_digest": digest, "station": station,
                "kind": parsed["kind"], "day": parsed["day"],
                "start_day": parsed["start_day"], "end_day": parsed["end_day"],
                "rainfall_mm": parsed["rainfall_mm"], "total_mm": parsed["total_mm"],
                "attribution_json": json.dumps(parsed["attribution"],
                                               ensure_ascii=False, sort_keys=True),
                "version": version, "seq": store.next_hydro_seq(conn, station),
                "revision": revision, "created_at": stamp,
            }
            store.insert_hydro_observation(conn, item)
            # 与观测同事务登记「待重算」：观测落库后若进程中断，重启可按此恢复。
            store.mark_hydro_recompute(conn, station, "E_HYDRO_RECOMPUTE_FAILED", stamp)
    except errors.MeterbackError:
        raise
    except Exception as exc:  # 观测没落库：明确失败，月量保持原样
        raise errors.MeterbackError(
            "雨量观测保存失败，月量未改动", code="E_HYDRO_RECOMPUTE_FAILED",
            detail={"station": station, "observation_id": observation_id,
                    "cause": str(exc)}) from exc
    return {"observation": store.find_hydro_by_request(request_id), "replay": False}


def recompute(store, station: str) -> dict:
    """按已落库观测重算一个站点并覆盖月量表；失败只登记待重算。"""
    observations = store.load_hydro_observations(station)
    revision = store.hydro_revision(station)
    stamp = _stamp()
    result = ledger.resolve(station, observations, revision=revision,
                            computed_at=stamp)
    try:
        with store.connect() as conn:
            for decision in result["decisions"]:
                store.upsert_hydro_decision(conn, decision)
            store.replace_hydro_months(conn, station, result["months"])
            store.clear_hydro_recompute(conn, station)
    except Exception:
        with store.connect() as conn:
            store.mark_hydro_recompute(conn, station,
                                       "E_HYDRO_RECOMPUTE_FAILED", stamp)
        raise
    return {"station": station, "revision": revision,
            "months": result["months"], "decisions": result["decisions"]}


def recover(store) -> dict:
    """恢复核算：应用重启或核算中断后，按待重算表把站点月量补齐。"""
    pending = store.load_hydro_recompute()
    recovered, failed = [], []
    for row in pending:
        try:
            recompute(store, row["station"])
            recovered.append(row["station"])
        except Exception:
            failed.append(row["station"])
    return {"recovered": recovered, "failed": failed,
            "remaining": [item["station"] for item in store.load_hydro_recompute()]}


# ---------- 回读 ----------
def submit(store, body: dict) -> dict:
    outcome = record(store, body)
    observation = outcome["observation"]
    try:
        result = recompute(store, observation["station"])
    except Exception as exc:
        # 观测已落库、月量保持原值；同笔请求重试会再核算一次。
        raise errors.MeterbackError(
            "雨量观测已保存，月量重算失败，回读到的仍是原月量",
            code="E_HYDRO_RECOMPUTE_FAILED",
            detail={"station": observation["station"],
                    "observation_id": observation["observation_id"],
                    "request_id": observation["request_id"],
                    "cause": str(exc),
                    # 失败时把当前（原）月量一并带回，调用方无需二次猜测。
                    "months": [ledger.month_payload(row)
                               for row in store.load_hydro_months(
                                   observation["station"])]}) from exc
    months = [ledger.month_payload(row) for row in result["months"]
              if _day_in_month(observation, row["month"])]
    decision = next((item for item in result["decisions"]
                     if item["observation_id"] == observation["observation_id"]), None)
    return {
        "station": observation["station"],
        "observation": observation_payload(observation),
        "decision": decision,
        "months": months,
        "revision": result["revision"],
        "replay": outcome["replay"],
        "new_observation": not outcome["replay"],
    }


def _day_in_month(observation: dict, month: str) -> bool:
    day = observation.get("day")
    if day:
        return day[:7] == month
    start, end = observation.get("start_day"), observation.get("end_day")
    if not start or not end:
        return False
    return month in ledger.month_spans(start, end)


def observation_payload(observation: dict) -> dict:
    return {
        "observation_id": observation["observation_id"],
        "request_id": observation["request_id"],
        "station": observation["station"],
        "kind": observation["kind"],
        "day": observation["day"],
        "start_day": observation["start_day"],
        "end_day": observation["end_day"],
        "rainfall_mm": None if observation["rainfall_mm"] is None
        else str(observation["rainfall_mm"]),
        "total_mm": None if observation["total_mm"] is None
        else str(observation["total_mm"]),
        "attribution": dict(observation.get("attribution") or {}),
        "version": observation["version"],
        "seq": observation["seq"],
        "revision": observation["revision"],
        "created_at": observation["created_at"],
    }


def view(store, station: str | None = None) -> dict:
    observations = store.load_hydro_observations(station)
    decisions = store.load_hydro_decisions(station)
    months = store.load_hydro_months(station)
    stations = sorted({item["station"] for item in observations}) \
        if station is None else [station]
    return {
        "stations": stations,
        "observations": [observation_payload(item) for item in observations],
        "decisions": [{
            "observation_id": key, "station": value["station"],
            "status": value["status"], "reason": value["reason"],
            "reason_label": reasons.label(value["reason"]),
            "months": value["months"], "detail": value["detail"],
        } for key, value in sorted(decisions.items())],
        "months": [ledger.month_payload(row) for row in months],
        "recompute_pending": store.load_hydro_recompute(),
    }
