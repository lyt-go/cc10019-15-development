"""生命周期价值路由：LTV预测、按版本预测明细、校准发布、回滚与复算。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import ActionPayload, ActionResult
from app.services.lifetime_value import (
    build_forecast_detail,
    get_active_release,
    get_forecast_detail,
    get_ltv_result,
    list_forecast_details,
    list_releases,
    ltv_with_retention,
    predict_ltv,
    publish_forecast,
    recompute_from_detail,
    rollback_release,
)
from app.store import store

router = APIRouter(prefix="/api/lifetime-value", tags=["lifetime_value"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("lifetime_value")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("lifetime_value")
    return {"items": rows, "total": len(rows)}


# ========== 按版本的预测明细与发布单（具体路径注册在 /{entry_id} 之前） ==========
@router.get("/forecast-details")
def forecast_details(model_id: str | None = None) -> dict[str, Any]:
    """预测明细列表：每条明细内嵌发布当时的曲线版本与逐用户估值。"""
    items = list_forecast_details(model_id)
    return {"items": items, "total": len(items)}


@router.get("/releases")
def releases(model_id: str | None = None) -> dict[str, Any]:
    """校准发布单列表：生效时间、关联曲线、替换关系均可核对。"""
    items = list_releases(model_id)
    return {"items": items, "total": len(items)}


@router.get("/{model_id}/active-release")
def active_release(model_id: str) -> dict[str, Any]:
    release = get_active_release(model_id)
    if release is None:
        raise HTTPException(status_code=404, detail="No active release")
    return release


@router.post("/{model_id}/forecast-details")
def create_forecast_detail(model_id: str, payload: dict[str, Any]) -> ActionResult:
    """生成按版本的预测明细，关联留存曲线并登记校准采用记录。"""
    retention_config_id = str(payload.get("retention_config_id", ""))
    cycle = str(payload.get("cycle", ""))
    note = str(payload.get("note", ""))
    if not retention_config_id:
        raise HTTPException(status_code=400, detail="retention_config_id 必填")
    try:
        detail = build_forecast_detail(model_id, retention_config_id, cycle, note)
        return ActionResult(ok=True, message="ForecastDetailBuilt", entry=detail)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/forecast-details/{detail_id}/publish")
def publish(detail_id: str, payload: dict[str, Any] | None = None) -> ActionResult:
    """发布预测明细：原子绑定明细与留存曲线，留存更正未完成时拒绝发布。"""
    effective_from = str((payload or {}).get("effective_from", ""))
    try:
        release = publish_forecast(detail_id, effective_from)
        return ActionResult(ok=True, message="Published", entry=release)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/releases/{release_id}/rollback")
def rollback(release_id: str, payload: dict[str, Any] | None = None) -> ActionResult:
    """回滚发布：切回上一份整份可核验的预测明细。"""
    reason = str((payload or {}).get("reason", ""))
    try:
        outcome = rollback_release(release_id, reason)
        return ActionResult(ok=True, message="RolledBack", entry=outcome)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/forecast-details/{detail_id}/recompute")
def recompute(detail_id: str) -> dict[str, Any]:
    """沿留存计算的采用记录复算历史明细并核对指纹。"""
    try:
        return recompute_from_detail(detail_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("lifetime_value", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{model_id}/run")
def run(model_id: str) -> ActionResult:
    try:
        result = predict_ltv(model_id)
        return ActionResult(ok=True, message="Predicted", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{model_id}/result")
def result(model_id: str) -> dict[str, Any]:
    res = get_ltv_result(model_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{model_id}/calibrate")
def calibrate(model_id: str, payload: dict[str, Any]) -> ActionResult:
    retention_config_id = payload.get("retention_config_id", "")
    try:
        result = ltv_with_retention(model_id, retention_config_id)
        return ActionResult(ok=True, message="Calibrated", entry=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("lifetime_value", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
