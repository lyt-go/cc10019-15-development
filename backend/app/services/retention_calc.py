"""留存计算引擎：计算用户留存率，支持按维度分段。

在原有留存率计算之上，补充了三条可核验能力：
1. 每次计算固化为带指纹的曲线版本（curve_version），并登记入组批次（cohort_batches）；
2. LTV 校准采用曲线时登记采用记录（register_retention_adoption），可追溯被谁采用；
3. 影响基准群的迟到更正走更正单（retention_correction），更正未完成前，
   其引用的曲线输入不能与预测明细分开生效，也不能被新的发布绑定。
"""
from __future__ import annotations

import datetime
import hashlib
import json
from collections import defaultdict
from typing import Any

from app.store import store
from app.schemas import RetentionResult


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _fingerprint(payload: Any) -> str:
    """对结构化数据计算稳定指纹，供曲线版本与预测明细做整份核验。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _next_curve_version(config_id: str) -> str:
    """同一留存配置下的曲线版本号递增（RC-001-curve-3）。"""
    existing = store.find_all_by_field("retention_curve", "retention_config_id", config_id)
    return f"{config_id}-curve-{len(existing) + 1}"


def _next_record_id(table: str, prefix: str) -> str:
    rows = store.rows(table)
    return f"{prefix}-{len(rows) + 1:03d}"


def _build_cohort_batches(
    retention_rates: dict[str, list[float]],
    cohort_sizes: dict[str, int],
    base_date: str,
) -> list[dict[str, Any]]:
    """把基准群拆成可追溯的入组批次：整体批次 + 各分段维度批次。"""
    batches: list[dict[str, Any]] = []
    for key, rates in retention_rates.items():
        batches.append({
            "batch_id": f"{base_date}:{key}",
            "segment_key": key,
            "base_date": base_date,
            "cohort_size": int(cohort_sizes.get(key, 0)),
            "retention_rate_head": round(rates[0], 4) if rates else 0.0,
            "retention_rate_tail": round(rates[-1], 4) if rates else 0.0,
        })
    return batches


def _generate_retention_events(
    event_id: str,
    base_date: str,
    days: int,
    data_revision: int = 0,
) -> list[dict[str, Any]]:
    """生成留存分析所需的模拟事件数据。

    data_revision 模拟迟到数据并入后的基准群修订：每次更正重算都会带上新的
    修订号，使基准群规模与后续留存人数发生变化，进而产生不同的曲线指纹。
    """
    events = []
    base = datetime.datetime.strptime(base_date, "%Y-%m-%d")
    base_ts = int(base.timestamp())

    for day in range(days):
        # 每天的用户数递减模拟留存；修订号带来迟到并入的增量用户
        num_users = max(1000 - day * 80 + data_revision * 60, 200)
        for user_idx in range(1, num_users + 1):
            user_id = f"U{user_idx:05d}"
            # 用户在不同维度上的分布
            attr_001 = ["app", "web", "miniapp", "h5"][user_idx % 4]
            attr_005 = ["普通", "银卡", "金卡", "钻石"][user_idx % 4]
            events.append({
                "user_id": user_id,
                "event_id": event_id,
                "timestamp": base_ts + day * 86400 + (user_idx % 3600),
                "ATTR-001": attr_001,
                "ATTR-005": attr_005,
                "date": (base + datetime.timedelta(days=day)).strftime("%Y-%m-%d"),
            })
    return events


def calculate_retention(config_id: str) -> RetentionResult:
    """执行留存计算，并固化为带指纹的曲线版本。"""
    config = store.find_by_field("retention_calc", "config_id", config_id)
    if config is None:
        raise ValueError(f"Retention config not found: {config_id}")

    init_event = config["init_event"]
    return_event = config["return_event"]
    calc_period = config.get("calc_period", "day")
    segment_dim = config.get("segment_dim", "")
    base_date = config["base_date"]
    retention_window = config.get("retention_window", 30)
    data_revision = int(config.get("data_revision", 0))

    # 生成事件数据（带数据修订号，迟到更正重算会改变基准群与留存结果）
    events = _generate_retention_events(
        init_event, base_date, retention_window, data_revision
    )

    # 按日期和用户分组
    date_users: dict[str, set[str]] = defaultdict(set)
    dim_date_users: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    for e in events:
        date_users[e["date"]].add(e["user_id"])
        if segment_dim:
            dim_val = e.get(segment_dim, "unknown")
            dim_date_users[dim_val][e["date"]].add(e["user_id"])

    # 计算留存率
    cohort_sizes: dict[str, int] = {}
    retention_rates: dict[str, list[float]] = {}

    # 基准日期的用户作为初始队列
    init_date = base_date
    init_users = date_users.get(init_date, set())
    cohort_sizes[init_date] = len(init_users)
    retention_rates[init_date] = []

    for day in range(retention_window):
        target_date = _add_days(base_date, day)
        target_users = date_users.get(target_date, set())
        retained = init_users & target_users
        rate = len(retained) / len(init_users) if init_users else 0.0
        retention_rates[init_date].append(round(rate, 4))

    # 按维度分段
    if segment_dim:
        for dim_val, dim_data in dim_date_users.items():
            dim_init_users = dim_data.get(init_date, set())
            key = f"{segment_dim}:{dim_val}"
            cohort_sizes[key] = len(dim_init_users)
            retention_rates[key] = []
            for day in range(retention_window):
                target_date = _add_days(base_date, day)
                target_users = dim_data.get(target_date, set())
                retained = dim_init_users & target_users
                rate = len(retained) / len(dim_init_users) if dim_init_users else 0.0
                retention_rates[key].append(round(rate, 4))

    # 固化曲线版本：指纹覆盖全部留存率与基准群规模，任何迟到更正都会产生新指纹
    curve_version = _next_curve_version(config_id)
    curve_fingerprint = _fingerprint({
        "config_id": config_id,
        "base_date": base_date,
        "retention_window": retention_window,
        "data_revision": data_revision,
        "cohort_sizes": cohort_sizes,
        "retention_rates": retention_rates,
    })
    cohort_batches = _build_cohort_batches(retention_rates, cohort_sizes, base_date)

    result = RetentionResult(
        config_id=config_id,
        init_event=init_event,
        return_event=return_event,
        base_date=base_date,
        cohort_sizes=cohort_sizes,
        retention_rates=retention_rates,
        result_table=config.get("result_table", ""),
        curve_version=curve_version,
        curve_fingerprint=curve_fingerprint,
        cohort_batches=cohort_batches,
    )

    # 存储产物（最新结果，保留既有契约）
    store.set_artifact("retention_calc", config_id, result.model_dump())

    # 曲线版本整份登记（历史版本不覆盖，供回滚与复算）
    store.insert("retention_curve", {
        "curve_version": curve_version,
        "retention_config_id": config_id,
        "curve_fingerprint": curve_fingerprint,
        "base_date": base_date,
        "retention_window": retention_window,
        "data_revision": data_revision,
        "segment_dim": segment_dim,
        "cohort_sizes": cohort_sizes,
        "retention_rates": retention_rates,
        "cohort_batches": cohort_batches,
        "result_table": config.get("result_table", ""),
        "created_at": _now(),
        "status": "生效中",
    })
    config["active_curve_version"] = curve_version
    config["active_curve_fingerprint"] = curve_fingerprint

    # 更新状态
    store.update("retention_calc", config["id"], {
        "status": "已完成",
        "pending": False,
        "active_curve_version": curve_version,
        "active_curve_fingerprint": curve_fingerprint,
    })

    return result


def _add_days(date_str: str, days: int) -> str:
    """日期加减。"""
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    return (d + datetime.timedelta(days=days)).strftime("%Y-%m-%d")


def get_retention_result(config_id: str) -> dict[str, Any] | None:
    """获取留存计算结果。"""
    artifact = store.get_artifact("retention_calc", config_id)
    if artifact:
        return artifact
    try:
        result = calculate_retention(config_id)
        return result.model_dump()
    except ValueError:
        return None


# ========== 曲线版本查询 ==========
def list_curve_versions(config_id: str | None = None) -> list[dict[str, Any]]:
    """列出留存曲线版本，可按留存配置过滤。"""
    rows = store.rows("retention_curve")
    if config_id:
        rows = [r for r in rows if r.get("retention_config_id") == config_id]
    return rows


def get_curve_version(curve_version: str) -> dict[str, Any] | None:
    return store.find_by_field("retention_curve", "curve_version", curve_version)


def get_active_curve(config_id: str) -> dict[str, Any] | None:
    """获取某留存配置当前生效的曲线版本。"""
    config = store.find_by_field("retention_calc", "config_id", config_id)
    if config is None or not config.get("active_curve_version"):
        return None
    return get_curve_version(config["active_curve_version"])


# ========== 校准采用登记 ==========
def _open_corrections_for_curve(curve_fingerprint: str) -> list[dict[str, Any]]:
    return [
        c for c in store.rows("retention_correction")
        if c.get("affected_curve_fingerprint") == curve_fingerprint
        and c.get("status") == "更正中"
    ]


def assert_curve_usable(curve_fingerprint: str) -> None:
    """校验曲线输入当前可被采用/绑定：存在未完成更正则拒绝，避免两项输入分开生效。"""
    open_corrections = _open_corrections_for_curve(curve_fingerprint)
    if open_corrections:
        ids = ", ".join(c["correction_id"] for c in open_corrections)
        raise ValueError(
            f"留存更正未完成（{ids}），基准群曲线输入暂不能生效，请先完成或回滚更正"
        )


def register_retention_adoption(
    retention_config_id: str,
    adopted_by_model: str,
    source_detail_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    """LTV 校准采用留存曲线时登记：固化曲线版本、指纹与入组批次。

    留存更正未完成时拒绝登记，保证被采用的曲线是整份可核验的数据。
    """
    curve = get_active_curve(retention_config_id)
    if curve is None:
        result = calculate_retention(retention_config_id)
        curve = get_curve_version(result.curve_version)
    assert curve is not None

    assert_curve_usable(curve["curve_fingerprint"])

    adoption_id = _next_record_id("retention_adoption", "RA")
    record = {
        "adoption_id": adoption_id,
        "retention_config_id": retention_config_id,
        "curve_version": curve["curve_version"],
        "curve_fingerprint": curve["curve_fingerprint"],
        "cohort_batches": [dict(b) for b in curve.get("cohort_batches", [])],
        "adopted_by_model": adopted_by_model,
        "adopted_at": _now(),
        "source_detail_id": source_detail_id,
        "note": note,
    }
    store.insert("retention_adoption", record)
    return record


def list_adoptions(config_id: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows("retention_adoption")
    if config_id:
        rows = [r for r in rows if r.get("retention_config_id") == config_id]
    return rows


# ========== 迟到更正流程 ==========
def open_retention_correction(
    retention_config_id: str,
    reason: str,
    curve_version: str = "",
) -> dict[str, Any]:
    """登记一次影响基准群的迟到更正。更正单创建后即阻断对应曲线输入继续生效。"""
    if curve_version:
        curve = get_curve_version(curve_version)
    else:
        curve = get_active_curve(retention_config_id)
    if curve is None:
        raise ValueError(f"Retention curve not found for config: {retention_config_id}")
    if curve["retention_config_id"] != retention_config_id:
        raise ValueError("曲线版本与留存配置不匹配")

    # 同一曲线已有未完成更正时不重复开单
    if _open_corrections_for_curve(curve["curve_fingerprint"]):
        raise ValueError("该曲线已存在未完成的留存更正")

    correction_id = _next_record_id("retention_correction", "RC-FIX")
    record = {
        "correction_id": correction_id,
        "retention_config_id": retention_config_id,
        "affected_curve_version": curve["curve_version"],
        "affected_curve_fingerprint": curve["curve_fingerprint"],
        "affected_batches": [dict(b) for b in curve.get("cohort_batches", [])],
        "reason": reason,
        "status": "更正中",
        "created_at": _now(),
        "completed_at": "",
        "replacement_curve_version": "",
        "replacement_curve_fingerprint": "",
        "rolled_back_at": "",
    }
    store.insert("retention_correction", record)
    return record


def complete_retention_correction(correction_id: str) -> dict[str, Any]:
    """完成更正：重算留存产生替代曲线版本，解除对旧曲线的阻断。"""
    correction = store.find_by_field(
        "retention_correction", "correction_id", correction_id
    )
    if correction is None:
        raise ValueError(f"Retention correction not found: {correction_id}")
    if correction["status"] != "更正中":
        raise ValueError(f"更正单 {correction_id} 当前状态为 {correction['status']}，无法完成")

    # 迟到更正并入：递增数据修订号，使重算的基准群与曲线指纹发生变化
    config = store.find_by_field(
        "retention_calc", "config_id", correction["retention_config_id"]
    )
    if config is not None:
        next_revision = int(config.get("data_revision", 0)) + 1
        store.update("retention_calc", config["id"], {"data_revision": next_revision})
        config["data_revision"] = next_revision

    # 重算生成新的曲线版本（新指纹），作为更正后的可核验替代输入
    new_result = calculate_retention(correction["retention_config_id"])
    correction.update({
        "status": "已完成",
        "completed_at": _now(),
        "replacement_curve_version": new_result.curve_version,
        "replacement_curve_fingerprint": new_result.curve_fingerprint,
    })
    return correction


def rollback_retention_correction(correction_id: str) -> dict[str, Any]:
    """回滚更正：更正单作废，恢复到更正前整份可核验的曲线数据，阻断解除。"""
    correction = store.find_by_field(
        "retention_correction", "correction_id", correction_id
    )
    if correction is None:
        raise ValueError(f"Retention correction not found: {correction_id}")
    if correction["status"] != "更正中":
        raise ValueError(f"更正单 {correction_id} 当前状态为 {correction['status']}，无法回滚")

    # 恢复旧曲线为生效曲线（整份历史版本仍在 retention_curve 中可核验）
    old_curve = get_curve_version(correction["affected_curve_version"])
    if old_curve is not None:
        old_curve["status"] = "生效中"
        config = store.find_by_field(
            "retention_calc", "config_id", correction["retention_config_id"]
        )
        if config is not None:
            store.update("retention_calc", config["id"], {
                "active_curve_version": old_curve["curve_version"],
                "active_curve_fingerprint": old_curve["curve_fingerprint"],
            })
            config["active_curve_version"] = old_curve["curve_version"]
            config["active_curve_fingerprint"] = old_curve["curve_fingerprint"]

    correction.update({
        "status": "已回滚",
        "rolled_back_at": _now(),
    })
    return correction


def list_corrections(config_id: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows("retention_correction")
    if config_id:
        rows = [r for r in rows if r.get("retention_config_id") == config_id]
    return rows
