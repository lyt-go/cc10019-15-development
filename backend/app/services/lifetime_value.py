"""LTV预测引擎：基于用户分群和历史行为预测生命周期价值。

在原有预测/校准之上，补齐校准发布状态的可追溯闭环：
1. build_forecast_detail 生成按版本的预测明细，内嵌发布当时的留存曲线、
   校准采用记录与逐用户估值（整份可核验）；
2. publish_forecast 原子绑定「预测明细 + 留存曲线」两份输入，
   留存更正未完成时禁止发布，避免两项输入分开生效；
3. rollback_release 回滚到上一份整份数据；recompute_from_detail 沿留存计算的
   采用记录复算历史明细，保证切换回旧结果仍可核对。
"""
from __future__ import annotations

import datetime
from typing import Any

from app.store import store
from app.schemas import LtvResult
from app.services.retention_calc import (
    assert_curve_usable,
    calculate_retention,
    get_active_curve,
    get_curve_version,
    get_retention_result,
    register_retention_adoption,
    _fingerprint,
)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _next_detail_id(model_id: str) -> str:
    existing = [
        r for r in store.rows("ltv_forecast_detail")
        if r.get("model_id") == model_id
    ]
    return f"{model_id}-D{len(existing) + 1:02d}"


def _next_release_id(model_id: str) -> str:
    existing = [
        r for r in store.rows("ltv_release")
        if r.get("model_id") == model_id
    ]
    return f"{model_id}-REL-{len(existing) + 1:02d}"


def predict_ltv(model_id: str) -> LtvResult:
    """执行LTV预测计算。"""
    model = store.find_by_field("lifetime_value", "model_id", model_id)
    if model is None:
        raise ValueError(f"LTV model not found: {model_id}")

    segment_id = model["segment_id"]
    value_metric = model["value_metric"]
    predict_window = model["predict_window"]
    decay_factor = model["decay_factor"]
    history_baseline = model["history_baseline"]

    # 获取分群信息
    segment = store.find_by_field("user_segment", "segment_id", segment_id)
    if segment is None:
        raise ValueError(f"Segment not found: {segment_id}")

    # 模拟历史基线数据
    base_size = segment.get("size_estimate", 1000)

    # 生成预测值
    predicted_values: list[dict[str, Any]] = []
    total_ltv = 0.0

    for user_idx in range(1, min(base_size + 1, 101)):  # 限制计算量
        user_id = f"U{user_idx:05d}"
        # 基于历史基线生成初始值
        if value_metric == "revenue":
            base_value = 50.0 + (user_idx % 20) * 10.0
        elif value_metric == "order_count":
            base_value = 1.0 + (user_idx % 10)
        else:  # active_days
            base_value = 3.0 + (user_idx % 15)

        # 应用衰减因子预测未来价值
        predicted = base_value * (decay_factor ** (predict_window / 30))
        predicted_values.append({
            "user_id": user_id,
            "base_value": round(base_value, 2),
            "predicted_value": round(predicted, 2),
            "predict_window_days": predict_window,
        })
        total_ltv += predicted

    avg_ltv = total_ltv / len(predicted_values) if predicted_values else 0.0

    # 模拟校准误差
    calibration_error = 0.05 + (1.0 - decay_factor) * 0.2

    result = LtvResult(
        model_id=model_id,
        segment_id=segment_id,
        predict_window=predict_window,
        predicted_values=predicted_values,
        avg_ltv=round(avg_ltv, 2),
        calibration_error=round(calibration_error, 4),
    )

    # 存储产物
    store.set_artifact("lifetime_value", model_id, result.model_dump())

    # 更新状态
    store.update("lifetime_value", model["id"], {
        "status": "已就绪",
        "pending": False,
    })

    return result


def get_ltv_result(model_id: str) -> dict[str, Any] | None:
    """获取LTV预测结果。"""
    artifact = store.get_artifact("lifetime_value", model_id)
    if artifact:
        return artifact
    try:
        result = predict_ltv(model_id)
        return result.model_dump()
    except ValueError:
        return None


def _avg_retention_rate(retention_rates: dict[str, list[float]]) -> float:
    if not retention_rates:
        return 0.0
    first_key = list(retention_rates.keys())[0]
    rates = retention_rates[first_key]
    return round(sum(rates) / len(rates), 4) if rates else 0.0


def _apply_retention_to_values(
    predicted_values: list[dict[str, Any]],
    avg_retention: float,
) -> tuple[list[dict[str, Any]], float]:
    """用留存率逐用户调整估值，输出带调整值的明细与调整后均值。"""
    factor = 0.5 + 0.5 * avg_retention
    adjusted_values: list[dict[str, Any]] = []
    total_adjusted = 0.0
    for item in predicted_values:
        adjusted = round(item["predicted_value"] * factor, 2)
        adjusted_values.append({**item, "retention_adjusted_value": adjusted})
        total_adjusted += adjusted
    avg_adjusted = round(total_adjusted / len(adjusted_values), 2) if adjusted_values else 0.0
    return adjusted_values, avg_adjusted


def ltv_with_retention(model_id: str, retention_config_id: str) -> dict[str, Any]:
    """结合留存率校准LTV预测（保留原有轻量校准契约）。"""
    ltv_result = get_ltv_result(model_id)
    retention_result = get_retention_result(retention_config_id)

    if ltv_result is None or retention_result is None:
        raise ValueError("Result not found")

    avg_retention = _avg_retention_rate(retention_result.get("retention_rates", {}))
    adjusted_ltv = ltv_result["avg_ltv"] * (0.5 + 0.5 * avg_retention)

    return {
        "model_id": model_id,
        "retention_config_id": retention_config_id,
        "original_ltv": ltv_result["avg_ltv"],
        "retention_adjusted_ltv": round(adjusted_ltv, 2),
        "avg_retention_rate": avg_retention,
    }


# ========== 按版本的预测明细（关联留存曲线 + 采用登记） ==========
def build_forecast_detail(
    model_id: str,
    retention_config_id: str,
    cycle: str = "",
    note: str = "",
) -> dict[str, Any]:
    """生成一份按版本发布的LTV预测明细。

    明细内嵌留存曲线版本、指纹、入组批次及留存计算的校准采用记录，
    使发布后的明细自带「当时曲线和版本」，回滚/复算不依赖后续被更正的数据。
    """
    model = store.find_by_field("lifetime_value", "model_id", model_id)
    if model is None:
        raise ValueError(f"LTV model not found: {model_id}")

    # 确保留存曲线已计算并取得生效版本
    curve = get_active_curve(retention_config_id)
    if curve is None:
        calculate_retention(retention_config_id)
        curve = get_active_curve(retention_config_id)
    if curve is None:
        raise ValueError(f"Retention curve not found: {retention_config_id}")

    # 先跑基础预测，保证逐用户估值可复算
    base = predict_ltv(model_id)
    avg_retention = _avg_retention_rate(curve.get("retention_rates", {}))
    adjusted_values, adjusted_avg = _apply_retention_to_values(
        base.predicted_values, avg_retention
    )

    detail_id = _next_detail_id(model_id)
    # 登记留存校准采用：留存更正未完成时这里会拒绝，两项输入不会分开生效
    adoption = register_retention_adoption(
        retention_config_id=retention_config_id,
        adopted_by_model=model_id,
        source_detail_id=detail_id,
        note=note or f"{model_id} 校准周期 {cycle or model.get('calibrate_period', '')}",
    )

    # 整份留存曲线快照固化进明细（可核验、可脱离后续更正独立复算）
    retention_snapshot = {
        "retention_config_id": retention_config_id,
        "curve_version": curve["curve_version"],
        "curve_fingerprint": curve["curve_fingerprint"],
        "base_date": curve.get("base_date", ""),
        "retention_window": curve.get("retention_window"),
        "data_revision": curve.get("data_revision", 0),
        "cohort_sizes": dict(curve.get("cohort_sizes", {})),
        "retention_rates": {k: list(v) for k, v in curve.get("retention_rates", {}).items()},
        "cohort_batches": [dict(b) for b in curve.get("cohort_batches", [])],
    }

    detail = {
        "detail_id": detail_id,
        "model_id": model_id,
        "model_version": model.get("version", ""),
        "segment_id": model["segment_id"],
        "cycle": cycle or model.get("calibrate_period", ""),
        "status": "待发布",
        "predict_window": base.predict_window,
        "value_metric": model["value_metric"],
        "retention_config_id": retention_config_id,
        "curve_version": curve["curve_version"],
        "curve_fingerprint": curve["curve_fingerprint"],
        "adoption_id": adoption["adoption_id"],
        "cohort_batches": [dict(b) for b in curve.get("cohort_batches", [])],
        "retention_snapshot": retention_snapshot,
        "predicted_values": adjusted_values,
        "avg_ltv": base.avg_ltv,
        "adjusted_avg_ltv": adjusted_avg,
        "avg_retention_rate": avg_retention,
        "detail_fingerprint": "",
        "created_at": _now(),
        "published_at": "",
    }
    # 明细指纹覆盖曲线输入与逐用户估值，任何一项被改动都会导致指纹不一致
    detail["detail_fingerprint"] = _fingerprint({
        "model_id": model_id,
        "model_version": detail["model_version"],
        "curve_fingerprint": curve["curve_fingerprint"],
        "avg_retention_rate": avg_retention,
        "predicted_values": adjusted_values,
    })
    store.insert("ltv_forecast_detail", detail)
    return detail


def list_forecast_details(model_id: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows("ltv_forecast_detail")
    if model_id:
        rows = [r for r in rows if r.get("model_id") == model_id]
    return rows


def get_forecast_detail(detail_id: str) -> dict[str, Any] | None:
    return store.find_by_field("ltv_forecast_detail", "detail_id", detail_id)


# ========== 校准发布（原子绑定两份输入） ==========
def _active_release(model_id: str) -> dict[str, Any] | None:
    for release in reversed(store.rows("ltv_release")):
        if release.get("model_id") == model_id and release.get("status") == "已发布":
            return release
    return None


def publish_forecast(detail_id: str, effective_from: str = "") -> dict[str, Any]:
    """发布预测明细：原子绑定明细与留存曲线，生成带生效时间的发布单。

    校验点：
    - 明细必须存在且未发布；
    - 明细指纹与内嵌内容一致（整份可核验）；
    - 关联留存曲线无未完成更正（更正没完成不能让两项输入分开生效）。
    发布成功后旧发布单失效（effective_to 收口），同一周期仅一份结果生效。
    """
    detail = get_forecast_detail(detail_id)
    if detail is None:
        raise ValueError(f"Forecast detail not found: {detail_id}")
    if detail["status"] == "已发布":
        raise ValueError(f"预测明细 {detail_id} 已发布，不能重复发布")

    # 指纹核验：重算内嵌内容指纹，确认明细未被篡改
    expected_fp = _fingerprint({
        "model_id": detail["model_id"],
        "model_version": detail["model_version"],
        "curve_fingerprint": detail["curve_fingerprint"],
        "avg_retention_rate": detail["avg_retention_rate"],
        "predicted_values": detail["predicted_values"],
    })
    if expected_fp != detail["detail_fingerprint"]:
        raise ValueError("预测明细指纹核验失败，数据可能已被改动，拒绝发布")

    # 留存更正未完成 -> 拒绝发布（两项输入不能分开生效）
    assert_curve_usable(detail["curve_fingerprint"])

    # 曲线版本仍可追溯
    curve = get_curve_version(detail["curve_version"])
    if curve is None or curve["curve_fingerprint"] != detail["curve_fingerprint"]:
        raise ValueError("关联留存曲线版本缺失或指纹不一致，拒绝发布")

    now = _now()
    previous = _active_release(detail["model_id"])
    release_id = _next_release_id(detail["model_id"])
    release = {
        "release_id": release_id,
        "model_id": detail["model_id"],
        "cycle": detail["cycle"],
        "status": "已发布",
        "detail_id": detail_id,
        "detail_fingerprint": detail["detail_fingerprint"],
        "retention_config_id": detail["retention_config_id"],
        "curve_version": detail["curve_version"],
        "curve_fingerprint": detail["curve_fingerprint"],
        "adoption_id": detail["adoption_id"],
        "effective_from": effective_from or now,
        "effective_to": "",
        "replaced_release_id": previous["release_id"] if previous else "",
        "rolled_back_at": "",
        "rollback_reason": "",
        "created_at": now,
    }

    # 原子生效：同一事务式更新内收口旧发布、落新发布、置明细为已发布
    if previous is not None:
        previous["status"] = "已失效"
        previous["effective_to"] = now
    detail["status"] = "已发布"
    detail["published_at"] = now
    store.insert("ltv_release", release)

    model = store.find_by_field("lifetime_value", "model_id", detail["model_id"])
    if model is not None:
        store.update("lifetime_value", model["id"], {
            "status": "已发布",
            "pending": False,
            "active_release_id": release_id,
            "active_detail_id": detail_id,
            "active_curve_version": detail["curve_version"],
        })

    return release


def rollback_release(release_id: str, reason: str = "") -> dict[str, Any]:
    """回滚发布：当前发布作废，切回上一份整份可核验的预测明细。

    回滚目标是被替换的上一份发布单；其明细与曲线快照在生成时已整份固化，
    因此即使期间留存数据被更正，旧结果仍可沿留存计算的采用记录复算核对。
    """
    release = store.find_by_field("ltv_release", "release_id", release_id)
    if release is None:
        raise ValueError(f"Release not found: {release_id}")
    if release["status"] != "已发布":
        raise ValueError(f"发布单 {release_id} 当前状态为 {release['status']}，无法回滚")

    now = _now()
    release["status"] = "已回滚"
    release["rolled_back_at"] = now
    release["rollback_reason"] = reason or "手动回滚"
    release["effective_to"] = now

    target: dict[str, Any] | None = None
    if release.get("replaced_release_id"):
        previous = store.find_by_field(
            "ltv_release", "release_id", release["replaced_release_id"]
        )
        if previous is not None and previous["status"] in ("已失效", "已回滚"):
            previous["status"] = "已发布"
            previous["effective_to"] = ""
            target = previous

    # 明细状态同步
    current_detail = get_forecast_detail(release["detail_id"])
    if current_detail is not None:
        current_detail["status"] = "已回滚"
    if target is not None:
        restored_detail = get_forecast_detail(target["detail_id"])
        if restored_detail is not None:
            restored_detail["status"] = "已发布"

    model = store.find_by_field("lifetime_value", "model_id", release["model_id"])
    if model is not None:
        store.update("lifetime_value", model["id"], {
            "status": "已发布" if target else "已就绪",
            "active_release_id": target["release_id"] if target else "",
            "active_detail_id": target["detail_id"] if target else "",
            "active_curve_version": target["curve_version"] if target else "",
        })

    return {
        "rolled_back_release": release,
        "restored_release": target,
        "restored_detail_id": target["detail_id"] if target else "",
    }


def recompute_from_detail(detail_id: str) -> dict[str, Any]:
    """沿留存计算的采用记录复算历史预测明细。

    使用明细内嵌的留存曲线快照（而不是当前可能已被更正的曲线）重算逐用户估值，
    并核对指纹，证明切换回以前的结果时仍可复算、可核验。
    """
    detail = get_forecast_detail(detail_id)
    if detail is None:
        raise ValueError(f"Forecast detail not found: {detail_id}")

    snapshot = detail.get("retention_snapshot", {})
    avg_retention = _avg_retention_rate(snapshot.get("retention_rates", {}))

    # 用明细里保存的基础估值重算调整值（剥离旧的调整字段）
    base_values = [
        {k: v for k, v in item.items() if k != "retention_adjusted_value"}
        for item in detail["predicted_values"]
    ]
    recomputed_values, recomputed_avg = _apply_retention_to_values(base_values, avg_retention)

    recomputed_fp = _fingerprint({
        "model_id": detail["model_id"],
        "model_version": detail["model_version"],
        "curve_fingerprint": detail["curve_fingerprint"],
        "avg_retention_rate": avg_retention,
        "predicted_values": recomputed_values,
    })
    matches = recomputed_fp == detail["detail_fingerprint"]

    return {
        "detail_id": detail_id,
        "model_id": detail["model_id"],
        "curve_version": detail["curve_version"],
        "curve_fingerprint": detail["curve_fingerprint"],
        "adoption_id": detail["adoption_id"],
        "avg_retention_rate": avg_retention,
        "stored_adjusted_avg_ltv": detail["adjusted_avg_ltv"],
        "recomputed_adjusted_avg_ltv": recomputed_avg,
        "fingerprint_matches": matches,
        "recomputed_values_sample": recomputed_values[:5],
    }


def list_releases(model_id: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows("ltv_release")
    if model_id:
        rows = [r for r in rows if r.get("model_id") == model_id]
    return rows


def get_active_release(model_id: str) -> dict[str, Any] | None:
    return _active_release(model_id)
