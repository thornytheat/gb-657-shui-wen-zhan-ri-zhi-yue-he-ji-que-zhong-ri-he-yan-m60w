"""M5 入口：从当前批次区间重算月度分摊与账单，页面按月份组展示。

雨量补报三条接口共用 `supplement` 编排：逐日补报、累计补报、月量回读。
补报接口返回本次观测的决定与受影响月份的最新月量，回读接口按站点返回全部
决定与月量；两者同源，页面与导出不做第二套计算。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...web import Ctx, get_ctx, render
from ..m6_batches import resume as resume_mod
from . import service, supplement
from .ledger import month_payload

router = APIRouter(tags=["M5 月度切分"])


@router.post("/api/hydro/supplements/daily")
def api_supplement_daily(payload: dict | None = None,
                         ctx: Ctx = Depends(get_ctx)) -> dict:
    return supplement.submit(ctx.store, {**(payload or {}), "kind": "daily"})


@router.post("/api/hydro/supplements/cumulative")
def api_supplement_cumulative(payload: dict | None = None,
                              ctx: Ctx = Depends(get_ctx)) -> dict:
    return supplement.submit(ctx.store, {**(payload or {}), "kind": "cumulative"})


@router.post("/api/hydro/supplements")
def api_supplement(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    return supplement.submit(ctx.store, payload or {})


@router.get("/api/hydro/supplements")
def api_supplements(station: str | None = None,
                    ctx: Ctx = Depends(get_ctx)) -> dict:
    return supplement.view(ctx.store, station)


@router.get("/api/hydro/months")
def api_hydro_months(station: str | None = None,
                     ctx: Ctx = Depends(get_ctx)) -> dict:
    """月量回读：两种观测依据分开的月量 + 每笔观测的判定，页面与导出共用。"""
    if station:
        body = supplement.view(ctx.store, station)
        body["months"] = [month_payload(row) for row in
                          ctx.store.load_hydro_months(station)]
        return body
    return supplement.view(ctx.store)


@router.post("/api/hydro/recompute")
def api_hydro_recompute(payload: dict | None = None,
                        ctx: Ctx = Depends(get_ctx)) -> dict:
    """补齐证据或核算中断后重算：带 station 重算一站点，否则恢复全部待重算站点。"""
    station = (payload or {}).get("station")
    if station:
        result = supplement.recompute(ctx.store, str(station))
        return {"station": result["station"], "revision": result["revision"],
                "months": [month_payload(row) for row in result["months"]],
                "decisions": result["decisions"], "recovered": [result["station"]],
                "failed": []}
    return supplement.recover(ctx.store)


@router.post("/api/hydro/monthly")
def api_hydro(payload: dict | None = None) -> dict:
    """旧版一次性月合计（逐日观测版本裁决），保留给手工核对与老调用方。"""
    from .hydro import monthly, reconcile, select_daily
    records = (payload or {}).get("records") or []
    selected = select_daily(records)
    result = monthly(selected["selected"], (payload or {}).get("expected_days") or {})
    accounts = reconcile(len(records), len(selected["selected"]),
                         len(selected["duplicates"]), len(selected["conflicts"]))
    return {"months": result, "selection": selected, "accounts": accounts}


@router.post("/api/months/allocate")
def api_allocate(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch_id = (payload or {}).get("batch_id")
    batch = resume_mod.require_batch(
        ctx.store, batch_id or (ctx.current_batch() or {}).get("batch_id", ""))
    result = service.run(ctx.settings, ctx.store, batch["batch_id"])
    body = service.payload(result["allocations"], result["bills"],
                           service.primary_map(ctx.settings))
    body["batch_id"] = result["batch_id"]
    body["stats"] = {key: str(value) if hasattr(value, "as_tuple") else value
                     for key, value in result["stats"].items()}
    return body


@router.get("/months")
def page_months(request: Request, batch_id: str | None = None,
                ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    groups: list[dict] = []
    bills: list[dict] = []
    adjustments = 0
    if batch is not None:
        allocations = ctx.store.load_allocations(batch["batch_id"])
        bills = ctx.store.load_bills(batch["batch_id"])
        groups = service.rows_by_month(allocations, service.primary_map(ctx.settings))
        adjustments = sum(1 for item in allocations if item["tou_adjusted"])
    ledger = supplement.view(ctx.store)
    return render(request, "months.html", page="months", page_title="月合计",
                  batch=batch, groups=groups, bills=bills, adjustments=adjustments,
                  hydro_supplements=ledger,
                  hydro_months=ledger["months"],
                  hydro_decisions=ledger["decisions"])
