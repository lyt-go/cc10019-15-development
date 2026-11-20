"""留存计算路由：执行留存计算、管理曲线版本、校准采用登记与迟到更正。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import ActionPayload, ActionResult
from app.services.retention_calc import (
    calculate_retention,
    complete_retention_correction,
    get_retention_result,
    list_adoptions,
    list_corrections,
    list_curve_versions,
    open_retention_correction,
    rollback_retention_correction,
)
from app.store import store

router = APIRouter(prefix="/api/retention-calc", tags=["retention_calc"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("retention_calc")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("retention_calc")
    return {"items": rows, "total": len(rows)}


# ========== 曲线版本（具体路径需注册在 /{entry_id} 之前） ==========
@router.get("/curves")
def curves(config_id: str | None = None) -> dict[str, Any]:
    """留存曲线版本列表，可按留存配置过滤。"""
    items = list_curve_versions(config_id)
    return {"items": items, "total": len(items)}


@router.get("/adoptions")
def adoptions(config_id: str | None = None) -> dict[str, Any]:
    """留存校准采用记录：已被LTV校准采用的曲线及入组批次。"""
    items = list_adoptions(config_id)
    return {"items": items, "total": len(items)}


@router.get("/corrections")
def corrections(config_id: str | None = None) -> dict[str, Any]:
    """影响基准群的迟到更正单列表。"""
    items = list_corrections(config_id)
    return {"items": items, "total": len(items)}


@router.post("/{config_id}/corrections")
def create_correction(config_id: str, payload: dict[str, Any]) -> ActionResult:
    """登记一次迟到更正；更正未完成前该曲线输入不能被新发布绑定。"""
    reason = str(payload.get("reason", ""))
    curve_version = str(payload.get("curve_version", ""))
    try:
        record = open_retention_correction(config_id, reason, curve_version)
        return ActionResult(ok=True, message="CorrectionOpened", entry=record)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/corrections/{correction_id}/complete")
def complete_correction(correction_id: str) -> ActionResult:
    """完成更正：重算产生替代曲线版本并解除阻断。"""
    try:
        record = complete_retention_correction(correction_id)
        return ActionResult(ok=True, message="CorrectionCompleted", entry=record)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/corrections/{correction_id}/rollback")
def rollback_correction(correction_id: str) -> ActionResult:
    """回滚更正：恢复到更正前整份可核验的曲线数据。"""
    try:
        record = rollback_retention_correction(correction_id)
        return ActionResult(ok=True, message="CorrectionRolledBack", entry=record)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("retention_calc", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{config_id}/run")
def run(config_id: str) -> ActionResult:
    try:
        result = calculate_retention(config_id)
        return ActionResult(ok=True, message="Calculated", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{config_id}/result")
def result(config_id: str) -> dict[str, Any]:
    res = get_retention_result(config_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("retention_calc", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
