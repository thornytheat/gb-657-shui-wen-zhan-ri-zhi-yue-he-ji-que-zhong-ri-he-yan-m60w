"""雨量补报台账：逐日补报与跨日累计补报是两种观测依据，分开保存、分开回读。

铁律：
- 累计补报只登记「起止日 + 总量」，绝不按天数均摊成日值；日值表里的缺测
  不会被累计补报填平（`days` / `missing_days` 只认逐日观测）。
- 月量对重叠覆盖只计一次：累计观测按覆盖日认领，后一条完全被覆盖则只留证据；
  日值与累计观测落在同一天时，由累计观测认领该日，日值转作旁证（仍保留）。
- 日值与累计都能覆盖同一段时，两者必须一致；不一致就把累计观测隔离成冲突，
  月量退回日值口径，两种原值都留在证据里。
- 起止日跨月且没有给出分月归属证据时，状态明确为 `pending`，不计入月量，
  只单列 `pending_reported_mm`；补齐证据（更高版本重报）后自动重算。
"""
from __future__ import annotations

import calendar
import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from ... import config

ZERO = Decimal("0.0")
RAIN = config.RAIN_QUANT

STATUS_ACCEPTED = "accepted"
STATUS_SUPERSEDED = "superseded"
STATUS_DEDUPED = "deduped"
STATUS_VERSION_CONFLICT = "version_conflict"
STATUS_OVERLAP_CONFLICT = "overlap_conflict"
STATUS_DAILY_CONFLICT = "daily_conflict"
STATUS_PENDING = "pending"

CONFLICT_STATUSES = frozenset(
    {STATUS_VERSION_CONFLICT, STATUS_OVERLAP_CONFLICT, STATUS_DAILY_CONFLICT})


# ---------- 日历 ----------
def parse_day(text) -> dt.date | None:
    """只接受自然日：`YYYY-MM-DD`，或带时间的 ISO 串取其当地日期。"""
    raw = str(text or "").strip().replace("/", "-")
    if not raw:
        return None
    try:
        if "T" in raw or " " in raw:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


def month_of_day(day: str) -> str:
    return day[:7]


def shift_month(month: str, delta: int) -> str:
    index = int(month[:4]) * 12 + int(month[5:7]) - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def calendar_days(month: str) -> int:
    year, mon = int(month[:4]), int(month[5:7])
    return calendar.monthrange(year, mon)[1]


def month_spans(start: str, end: str) -> list[str]:
    """起止日（含）覆盖到的所有自然月。"""
    first, last = month_of_day(start), month_of_day(end)
    months: list[str] = []
    cursor = first
    while cursor <= last:
        months.append(cursor)
        cursor = shift_month(cursor, 1)
    return months


def day_span(start: str, end: str) -> list[str]:
    """闭区间 [start, end] 的每一天；起点大于终点返回空。"""
    try:
        left = dt.date.fromisoformat(start)
        right = dt.date.fromisoformat(end)
    except (TypeError, ValueError):
        return []
    days: list[str] = []
    cursor = left
    while cursor <= right:
        days.append(cursor.isoformat())
        cursor += dt.timedelta(days=1)
    return days


def quantize(value: Decimal) -> Decimal:
    return Decimal(value).quantize(RAIN, rounding=ROUND_HALF_UP)


def as_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


# ---------- 观测选择：版本优先，同版本不同值隔离 ----------
def _ranked(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: (-int(row.get("version", 0)),
                                         int(row.get("seq", 0))))


def _pick(rows: list[dict], value_fn, code: str) -> tuple[list[dict], dict[str, dict]]:
    """返回（选中观测, observation_id → 决定草稿）。版本优先，同版本不同值隔离。"""
    ranked = _ranked(rows)
    top_version = int(ranked[0].get("version", 0))
    top = [row for row in ranked if int(row.get("version", 0)) == top_version]
    values = {value_fn(row) for row in top}
    picks: list[dict] = []
    decisions: dict[str, dict] = {}
    if len(values) > 1:
        for row in ranked:
            if int(row.get("version", 0)) == top_version:
                decisions[row["observation_id"]] = {
                    "status": STATUS_VERSION_CONFLICT,
                    "reason": code,
                    "detail": {"values": sorted(_show(value) for value in values)},
                }
            else:
                decisions[row["observation_id"]] = {
                    "status": STATUS_SUPERSEDED, "reason": "K_HYDRO_SUPERSEDED",
                    "detail": {"superseded_by_version": top_version},
                }
        return picks, decisions
    picks.append(ranked[0])
    decisions[ranked[0]["observation_id"]] = {"status": STATUS_ACCEPTED, "reason": None,
                                              "detail": {}}
    for row in ranked[1:]:
        if (int(row.get("version", 0)) == top_version
                and value_fn(row) == value_fn(ranked[0])):
            status, reason = STATUS_DEDUPED, "K_DUPLICATE_IDENTICAL"
        else:
            status, reason = STATUS_SUPERSEDED, "K_HYDRO_SUPERSEDED"
        decisions[row["observation_id"]] = {
            "status": status, "reason": reason,
            "detail": {"superseded_by_version": top_version},
        }
    return picks, decisions


def daily_value(row: dict) -> Decimal:
    return quantize(as_decimal(row["rainfall_mm"]))


def cumulative_value(row: dict) -> tuple:
    attribution = tuple(sorted((month, str(quantize(as_decimal(value))))
                               for month, value in (row.get("attribution") or {}).items()))
    return quantize(as_decimal(row["total_mm"])), attribution


def _attribution_of(obs: dict) -> dict[str, Decimal]:
    """单月覆盖没有跨月歧义，归属由覆盖范围直接确定；跨月必须带分月证据。"""
    stored = {month: quantize(as_decimal(value))
              for month, value in (obs.get("attribution") or {}).items()}
    if stored:
        return stored
    spans = month_spans(obs["start_day"], obs["end_day"])
    if len(spans) == 1:
        return {spans[0]: quantize(as_decimal(obs["total_mm"]))}
    return {}


def _show(value) -> str:
    if isinstance(value, tuple):
        total, attribution = value
        if not attribution:
            return str(total)
        return f"{total} ({', '.join(f'{m}={v}' for m, v in attribution)})"
    return str(value)


def select_daily(observations: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for obs in observations:
        if obs["kind"] == "daily":
            groups.setdefault(obs["day"], []).append(obs)
    selected: dict[str, Decimal] = {}
    decisions: dict[str, dict] = {}
    for day, rows in sorted(groups.items()):
        picks, picked = _pick(rows, daily_value, "Q_HYDRO_CONFLICT_VERSION")
        decisions.update(picked)
        if picks:
            selected[day] = daily_value(picks[0])
    return {"selected": selected, "decisions": decisions}


def select_cumulative(observations: list[dict]) -> dict:
    """同一起止日的多版本：版本优先；同版本里先认「带分月归属」的那条证据。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for obs in observations:
        if obs["kind"] == "cumulative":
            groups.setdefault((obs["start_day"], obs["end_day"]), []).append(obs)
    selected: list[dict] = []
    decisions: dict[str, dict] = {}
    for _, rows in sorted(groups.items()):
        ranked = sorted(
            rows,
            key=lambda row: (-int(row.get("version", 0)),
                             0 if (row.get("attribution") or {}) else 1,
                             int(row.get("seq", 0))))
        top_version = int(ranked[0].get("version", 0))
        top = [row for row in ranked if int(row.get("version", 0)) == top_version]
        with_evidence = [row for row in top if row.get("attribution")]
        pool = with_evidence or top
        values = {cumulative_value(row) for row in pool}
        if len(values) > 1:
            for row in ranked:
                if int(row.get("version", 0)) == top_version:
                    decisions[row["observation_id"]] = {
                        "status": STATUS_VERSION_CONFLICT,
                        "reason": "Q_HYDRO_CONFLICT_VERSION",
                        "detail": {"values": sorted(_show(value) for value in values),
                                   "span": [row["start_day"], row["end_day"]]},
                    }
                else:
                    decisions[row["observation_id"]] = {
                        "status": STATUS_SUPERSEDED, "reason": "K_HYDRO_SUPERSEDED",
                        "detail": {"superseded_by_version": top_version},
                    }
            continue
        winner = pool[0]
        selected.append(winner)
        decisions[winner["observation_id"]] = {"status": STATUS_ACCEPTED,
                                               "reason": None, "detail": {}}
        for row in ranked:
            if row is winner:
                continue
            same_version = int(row.get("version", 0)) == top_version
            if same_version and cumulative_value(row) == cumulative_value(winner):
                status, reason = STATUS_DEDUPED, "K_DUPLICATE_IDENTICAL"
            else:
                status, reason = STATUS_SUPERSEDED, "K_HYDRO_SUPERSEDED"
            decisions[row["observation_id"]] = {
                "status": status, "reason": reason,
                "detail": {"superseded_by_version": top_version,
                           "evidence_upgraded": same_version
                           and not row.get("attribution")
                           and bool(winner.get("attribution"))},
            }
    return {"selected": selected, "decisions": decisions}


# ---------- 覆盖认领与跨依据对账 ----------
def _order(obs: dict) -> tuple:
    # 起点早的优先；同起点覆盖更宽的优先；版本高、登记早的次之。
    end_rank = -dt.date.fromisoformat(obs["end_day"]).toordinal()
    return (obs["start_day"], end_rank, -int(obs.get("version", 0)), int(obs.get("seq", 0)))


def resolve(station: str, observations: list[dict], revision: int = 0,
            computed_at: str = "") -> dict:
    """重算一个站点的全部决定与月量；纯函数，输入顺序不影响结果。"""
    daily = select_daily(observations)
    cumulative = select_cumulative(observations)
    selected_daily: dict[str, Decimal] = daily["selected"]
    decisions: dict[str, dict] = {}
    decisions.update(daily["decisions"])
    decisions.update(cumulative["decisions"])

    for obs in observations:
        base = decisions.get(obs["observation_id"])
        if base is None:
            continue
        base["detail"] = {
            **base.get("detail", {}),
            "kind": obs["kind"],
            "station": obs["station"],
            "version": int(obs.get("version", 0)),
            "touched": _touched_months(obs),
        }
        if obs["kind"] == "daily":
            base["detail"].update({"day": obs["day"],
                                   "rainfall_mm": _text(obs["rainfall_mm"])})
        else:
            base["detail"].update({
                "start_day": obs["start_day"], "end_day": obs["end_day"],
                "total_mm": _text(obs["total_mm"]),
                "attribution": {month: _text(value)
                                for month, value in (obs.get("attribution") or {}).items()},
            })

    accepted: list[dict] = []
    claimed: set[str] = set()
    shadowed: set[str] = set()
    counted_daily: dict[str, Decimal] = {}

    candidates = []
    for obs in cumulative["selected"]:
        state = decisions[obs["observation_id"]]
        if state["status"] != STATUS_ACCEPTED:
            continue
        attribution = _attribution_of(obs)
        if not attribution:
            spans = month_spans(obs["start_day"], obs["end_day"])
            state["status"] = STATUS_PENDING
            state["reason"] = "K_HYDRO_ATTRIBUTION_PENDING"
            state["detail"]["needed"] = "跨月累计补报缺少分月归属证据"
            state["detail"]["candidate_months"] = spans
            continue
        candidates.append({**obs, "_attribution": attribution})

    for obs in sorted(candidates, key=_order):
        state = decisions[obs["observation_id"]]
        days = day_span(obs["start_day"], obs["end_day"])
        total = quantize(as_decimal(obs["total_mm"]))
        observed = [day for day in days if day in selected_daily]
        observed_sum = sum((selected_daily[day] for day in observed), ZERO)
        # 先对日值：即便整段已被别的累计覆盖，日值与累计不符也要有独立证据。
        if observed:
            if len(observed) == len(days) and observed_sum != total:
                state["status"] = STATUS_DAILY_CONFLICT
                state["reason"] = "Q_HYDRO_CONFLICT_DAILY_CUMULATIVE"
                state["detail"].update({"daily_sum": _text(observed_sum),
                                        "difference": _text(observed_sum - total)})
                continue
            if observed_sum > total:
                state["status"] = STATUS_DAILY_CONFLICT
                state["reason"] = "Q_HYDRO_CONFLICT_DAILY_CUMULATIVE"
                state["detail"].update({"daily_sum": _text(observed_sum),
                                        "difference": _text(observed_sum - total),
                                        "partial": True})
                continue

        owned = [day for day in days if day not in claimed]
        if not owned:
            state["status"] = STATUS_DEDUPED
            state["reason"] = "K_HYDRO_COVERAGE_DEDUPED"
            state["detail"]["covered_days"] = len(days)
            continue
        if len(owned) != len(days):
            state["status"] = STATUS_OVERLAP_CONFLICT
            state["reason"] = "Q_HYDRO_CONFLICT_CUMULATIVE_OVERLAP"
            state["detail"].update({"overlap_days": len(days) - len(owned),
                                    "owned_days": len(owned)})
            continue

        # 只有被采纳的累计观测才认领覆盖日：冲突观测不占位，不挡住后面的合法补报。
        claimed.update(days)

        state["status"] = STATUS_ACCEPTED
        state["reason"] = ("K_HYDRO_DAILY_RECONCILED"
                           if len(observed) == len(days) else "K_HYDRO_CUMULATIVE_INCLUDED")
        state["months"] = {month: quantize(as_decimal(value))
                           for month, value in obs["_attribution"].items()}
        state["detail"].update({"daily_sum": _text(observed_sum),
                                "daily_days": len(observed),
                                "shadowed_days": len(observed)})
        shadowed.update(observed)
        accepted.append(obs)

    for day, value in selected_daily.items():
        if day in shadowed:
            continue
        month = month_of_day(day)
        counted_daily[month] = counted_daily.get(month, ZERO) + value

    months = _month_rows(
        station, observations, decisions, counted_daily, shadowed, selected_daily,
        revision=revision, computed_at=computed_at)
    return {
        "months": months,
        "decisions": [_decision_row(obs, decisions[obs["observation_id"]])
                      for obs in sorted(observations,
                                        key=lambda item: (item["station"],
                                                          int(item.get("seq", 0))))],
        "accepted": accepted,
    }


def _month_rows(station: str, observations: list[dict], decisions: dict[str, dict],
                counted_daily: dict[str, Decimal], shadowed: set[str],
                selected_daily: dict[str, Decimal], *, revision: int,
                computed_at: str) -> list[dict]:
    evidence_months: set[str] = set()
    for day in selected_daily:
        evidence_months.add(month_of_day(day))
    for obs in observations:
        evidence_months.update(_touched_months(obs))

    cumulative_mm: dict[str, Decimal] = {}
    accepted_count: dict[str, int] = {}
    conflict_count: dict[str, int] = {}
    pending_count: dict[str, int] = {}
    pending_mm: dict[str, Decimal] = {}
    shadowed_mm: dict[str, Decimal] = {}
    for day in shadowed:
        if day in selected_daily:
            month = month_of_day(day)
            shadowed_mm[month] = shadowed_mm.get(month, ZERO) + selected_daily[day]
    for obs in observations:
        state = decisions[obs["observation_id"]]
        touched = _touched_months(obs)
        if state["status"] == STATUS_ACCEPTED:
            for month, value in (state.get("months") or {}).items():
                cumulative_mm[month] = cumulative_mm.get(month, ZERO) + value
                accepted_count[month] = accepted_count.get(month, 0) + 1
        elif state["status"] in CONFLICT_STATUSES:
            for month in touched:
                conflict_count[month] = conflict_count.get(month, 0) + 1
        elif state["status"] == STATUS_PENDING:
            # 待定的总量只挂在起点月上，避免跨月重复显示；证据里列全部候选月。
            anchor = month_of_day(obs["start_day"])
            for month in touched:
                pending_count[month] = pending_count.get(month, 0) + 1
            pending_mm[anchor] = pending_mm.get(anchor, ZERO) + quantize(
                as_decimal(obs["total_mm"]))

    days_count: dict[str, int] = {}
    for day in selected_daily:
        month = month_of_day(day)
        days_count[month] = days_count.get(month, 0) + 1

    rows = []
    for month in sorted(evidence_months):
        daily_mm = counted_daily.get(month, ZERO)
        cumulative = cumulative_mm.get(month, ZERO)
        days = days_count.get(month, 0)
        span = calendar_days(month)
        missing = span - days
        conflicts = conflict_count.get(month, 0)
        pending = pending_count.get(month, 0)
        if pending:
            state = "pending"
        elif conflicts:
            state = "conflict"
        elif missing == 0:
            state = "complete"
        else:
            state = "partial"
        rows.append({
            "station": station,
            "month": month,
            "daily_mm": _text(daily_mm),
            "cumulative_mm": _text(cumulative),
            "rainfall_mm": _text(daily_mm + cumulative),
            "shadowed_daily_mm": _text(shadowed_mm.get(month, ZERO)),
            "days": days,
            "missing_days": missing,
            "calendar_days": span,
            "state": state,
            "conflict_count": conflicts,
            "pending_count": pending,
            "pending_reported_mm": _text(pending_mm.get(month, ZERO)),
            "accepted_cumulative": accepted_count.get(month, 0),
            "revision": int(revision),
            "computed_at": computed_at,
        })
    return rows


def month_payload(row: dict) -> dict:
    """回读用的稳定形状：数值统一一位小数字符串，计数为整数。"""
    return {
        "station": row["station"],
        "month": row["month"],
        "daily_mm": _text(row["daily_mm"]),
        "cumulative_mm": _text(row["cumulative_mm"]),
        "rainfall_mm": _text(row["rainfall_mm"]),
        "shadowed_daily_mm": _text(row.get("shadowed_daily_mm", ZERO)),
        "days": int(row["days"]),
        "missing_days": int(row["missing_days"]),
        "calendar_days": int(row["calendar_days"]),
        "state": row["state"],
        "conflict_count": int(row["conflict_count"]),
        "pending_count": int(row["pending_count"]),
        "pending_reported_mm": _text(row["pending_reported_mm"]),
        "accepted_cumulative": int(row.get("accepted_cumulative", 0)),
        "revision": int(row["revision"]),
        "computed_at": row["computed_at"],
    }


def _touched_months(obs: dict) -> list[str]:
    if obs["kind"] == "daily":
        return [month_of_day(obs["day"])]
    return month_spans(obs["start_day"], obs["end_day"])


def _decision_row(obs: dict, state: dict) -> dict:
    return {
        "observation_id": obs["observation_id"],
        "station": obs["station"],
        "status": state["status"],
        "reason": state.get("reason"),
        "months": {month: _text(value)
                   for month, value in (state.get("months") or {}).items()},
        "detail": state.get("detail") or {},
    }


def _text(value) -> str:
    number = as_decimal(value)
    return "" if number is None else str(quantize(number))
