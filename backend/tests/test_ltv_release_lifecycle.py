"""LTV 校准发布与留存更正闭环测试。

覆盖需求场景：
- 预测明细按版本登记留存曲线与入组批次，留存计算登记校准采用记录；
- 留存更正未完成时，预测明细与曲线两项输入不能分开生效（发布被阻断）；
- 回滚回到上一份整份可核验数据，且可沿留存采用记录复算；
- 同一预测周期两个版本的生效时间、关联曲线、受影响用户估值均可核对。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services import retention_calc as rc
from app.services import lifetime_value as ltv


class LtvReleaseLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        store._artifacts.clear()
        store._compute_cache.clear()
        store._snapshots.clear()
        for table in (
            "retention_curve",
            "retention_adoption",
            "retention_correction",
            "ltv_forecast_detail",
            "ltv_release",
        ):
            store._tables[table] = []

    # ---------- 曲线版本与采用登记 ----------
    def test_retention_calc_freezes_curve_version_and_batches(self) -> None:
        result = rc.calculate_retention("RC-001")
        self.assertTrue(result.curve_version.startswith("RC-001-curve-"))
        self.assertEqual(len(result.curve_fingerprint), 16)
        # 入组批次：整体基准群 + 各分段维度批次
        self.assertGreaterEqual(len(result.cohort_batches), 2)
        curve = rc.get_curve_version(result.curve_version)
        self.assertIsNotNone(curve)
        self.assertEqual(curve["curve_fingerprint"], result.curve_fingerprint)

    def test_build_detail_registers_adoption_with_curve_and_batches(self) -> None:
        detail = ltv.build_forecast_detail("LTV-001", "RC-001", cycle="2026-11")
        # 明细内嵌当时曲线、版本与入组批次
        self.assertTrue(detail["detail_id"].startswith("LTV-001-D"))
        self.assertTrue(detail["curve_version"])
        self.assertEqual(detail["status"], "待发布")
        self.assertGreater(len(detail["cohort_batches"]), 0)
        self.assertIn("retention_rates", detail["retention_snapshot"])
        # 逐用户估值带留存调整值
        self.assertIn("retention_adjusted_value", detail["predicted_values"][0])

        # 留存侧登记了校准采用记录，且回指该明细
        adoptions = rc.list_adoptions("RC-001")
        self.assertEqual(len(adoptions), 1)
        adoption = adoptions[0]
        self.assertEqual(adoption["curve_version"], detail["curve_version"])
        self.assertEqual(adoption["curve_fingerprint"], detail["curve_fingerprint"])
        self.assertEqual(adoption["source_detail_id"], detail["detail_id"])
        self.assertEqual(adoption["adopted_by_model"], "LTV-001")
        self.assertEqual(
            len(adoption["cohort_batches"]), len(detail["cohort_batches"])
        )

    # ---------- 发布与原子绑定 ----------
    def test_publish_atomically_binds_detail_and_curve(self) -> None:
        detail = ltv.build_forecast_detail("LTV-001", "RC-001", cycle="2026-11")
        release = ltv.publish_forecast(detail["detail_id"], effective_from="2026-11-20T00:00:00")

        self.assertEqual(release["status"], "已发布")
        self.assertEqual(release["detail_id"], detail["detail_id"])
        self.assertEqual(release["curve_version"], detail["curve_version"])
        self.assertEqual(release["curve_fingerprint"], detail["curve_fingerprint"])
        self.assertEqual(release["adoption_id"], detail["adoption_id"])
        self.assertEqual(release["effective_from"], "2026-11-20T00:00:00")
        # 明细与模型状态联动
        self.assertEqual(ltv.get_forecast_detail(detail["detail_id"])["status"], "已发布")
        model = store.find_by_field("lifetime_value", "model_id", "LTV-001")
        self.assertEqual(model["status"], "已发布")
        self.assertEqual(model["active_release_id"], release["release_id"])

    def test_cannot_publish_twice(self) -> None:
        detail = ltv.build_forecast_detail("LTV-001", "RC-001")
        ltv.publish_forecast(detail["detail_id"])
        with self.assertRaises(ValueError):
            ltv.publish_forecast(detail["detail_id"])

    def test_tampered_detail_fails_fingerprint_check(self) -> None:
        detail = ltv.build_forecast_detail("LTV-001", "RC-001")
        # 篡改某个用户估值但不改指纹 -> 发布必须被拒绝
        detail["predicted_values"][0]["retention_adjusted_value"] = 999999.0
        with self.assertRaises(ValueError):
            ltv.publish_forecast(detail["detail_id"])

    # ---------- 迟到更正阻断两项输入分开生效 ----------
    def test_open_correction_blocks_new_publish_until_completed(self) -> None:
        # 第一版先发布成功
        detail_v1 = ltv.build_forecast_detail("LTV-001", "RC-001", cycle="2026-11")
        release_v1 = ltv.publish_forecast(
            detail_v1["detail_id"], effective_from="2026-11-18T00:00:00"
        )

        # 留存任务收到影响基准群的迟到更正
        correction = rc.open_retention_correction(
            "RC-001", reason="基准群入组事件迟到，需重算留存"
        )
        self.assertEqual(correction["status"], "更正中")
        self.assertEqual(
            correction["affected_curve_fingerprint"], detail_v1["curve_fingerprint"]
        )
        self.assertGreater(len(correction["affected_batches"]), 0)

        # 更正未完成：新明细仍可构建（采用登记会被阻断）-> 两项输入不能分开生效
        with self.assertRaises(ValueError):
            ltv.build_forecast_detail("LTV-001", "RC-001", cycle="2026-11")

        # 直接构造一份引用被阻断曲线的待发布明细：发布环节同样必须拒绝
        blocked_detail = dict(detail_v1)
        blocked_detail["detail_id"] = "LTV-001-D99"
        blocked_detail["status"] = "待发布"
        store.insert("ltv_forecast_detail", blocked_detail)
        with self.assertRaises(ValueError):
            ltv.publish_forecast("LTV-001-D99")
        # 当前生效结果仍指向第一版，未被分开生效的输入污染
        self.assertEqual(ltv.get_active_release("LTV-001")["release_id"],
                         release_v1["release_id"])

        # 完成更正：重算产生替代曲线版本（新指纹），阻断解除
        completed = rc.complete_retention_correction(correction["correction_id"])
        self.assertEqual(completed["status"], "已完成")
        self.assertNotEqual(
            completed["replacement_curve_fingerprint"],
            correction["affected_curve_fingerprint"],
        )

        # 更正完成后可基于新曲线构建并发布第二版
        detail_v2 = ltv.build_forecast_detail("LTV-001", "RC-001", cycle="2026-11")
        self.assertNotEqual(detail_v2["curve_fingerprint"], detail_v1["curve_fingerprint"])
        release_v2 = ltv.publish_forecast(
            detail_v2["detail_id"], effective_from="2026-11-20T00:00:00"
        )

        # 同一周期两版本的生效时间、关联曲线、替换关系均可核对
        self.assertEqual(release_v2["cycle"], release_v1["cycle"])
        self.assertEqual(release_v2["replaced_release_id"], release_v1["release_id"])
        self.assertNotEqual(release_v2["effective_from"], release_v1["effective_from"])
        self.assertNotEqual(release_v2["curve_version"], release_v1["curve_version"])
        # 旧发布被收口失效
        old = store.find_by_field("ltv_release", "release_id", release_v1["release_id"])
        self.assertEqual(old["status"], "已失效")
        self.assertTrue(old["effective_to"])
        # 当前生效指向第二版
        self.assertEqual(
            ltv.get_active_release("LTV-001")["release_id"], release_v2["release_id"]
        )

    def test_correction_rollback_restores_whole_verifiable_curve(self) -> None:
        rc.calculate_retention("RC-001")
        active_before = rc.get_active_curve("RC-001")
        correction = rc.open_retention_correction("RC-001", reason="误报更正")
        # 更正中不能采用
        with self.assertRaises(ValueError):
            rc.register_retention_adoption("RC-001", "LTV-001")

        rolled_back = rc.rollback_retention_correction(correction["correction_id"])
        self.assertEqual(rolled_back["status"], "已回滚")
        # 回滚后生效曲线仍是更正前那一份（指纹一致，整份可核验）
        active_after = rc.get_active_curve("RC-001")
        self.assertEqual(
            active_after["curve_fingerprint"], active_before["curve_fingerprint"]
        )
        # 阻断解除，可再次采用
        adoption = rc.register_retention_adoption("RC-001", "LTV-001")
        self.assertEqual(adoption["curve_version"], active_before["curve_version"])

    def test_duplicate_open_correction_rejected(self) -> None:
        rc.calculate_retention("RC-001")
        rc.open_retention_correction("RC-001", reason="第一次")
        with self.assertRaises(ValueError):
            rc.open_retention_correction("RC-001", reason="重复开单")

    # ---------- 发布回滚与沿记录复算 ----------
    def test_release_rollback_restores_previous_verifiable_result(self) -> None:
        detail_v1 = ltv.build_forecast_detail("LTV-001", "RC-001", cycle="2026-11")
        release_v1 = ltv.publish_forecast(detail_v1["detail_id"])

        # 第二版（模拟一次留存更正后的新曲线）
        correction = rc.open_retention_correction("RC-001", reason="迟到数据")
        rc.complete_retention_correction(correction["correction_id"])
        detail_v2 = ltv.build_forecast_detail("LTV-001", "RC-001", cycle="2026-11")
        release_v2 = ltv.publish_forecast(detail_v2["detail_id"])
        self.assertEqual(
            ltv.get_active_release("LTV-001")["release_id"], release_v2["release_id"]
        )

        # 回滚第二版 -> 回到第一份整份数据
        outcome = ltv.rollback_release(release_v2["release_id"], reason="复核不通过")
        self.assertEqual(outcome["rolled_back_release"]["status"], "已回滚")
        self.assertEqual(outcome["restored_release"]["release_id"],
                         release_v1["release_id"])
        self.assertEqual(
            ltv.get_active_release("LTV-001")["release_id"], release_v1["release_id"]
        )
        self.assertEqual(
            ltv.get_forecast_detail(detail_v1["detail_id"])["status"], "已发布"
        )
        self.assertEqual(
            ltv.get_forecast_detail(detail_v2["detail_id"])["status"], "已回滚"
        )

    def test_recompute_from_detail_uses_snapshot_and_matches_fingerprint(self) -> None:
        detail_v1 = ltv.build_forecast_detail("LTV-001", "RC-001", cycle="2026-11")
        ltv.publish_forecast(detail_v1["detail_id"])

        # 留存后续被更正：当前曲线已不是发布当时的曲线
        correction = rc.open_retention_correction("RC-001", reason="迟到数据")
        rc.complete_retention_correction(correction["correction_id"])
        current_curve = rc.get_active_curve("RC-001")
        self.assertNotEqual(current_curve["curve_fingerprint"],
                            detail_v1["curve_fingerprint"])

        # 沿明细内嵌的留存快照与采用记录复算，结果与登记指纹一致
        recomputed = ltv.recompute_from_detail(detail_v1["detail_id"])
        self.assertTrue(recomputed["fingerprint_matches"])
        self.assertEqual(recomputed["curve_fingerprint"], detail_v1["curve_fingerprint"])
        self.assertEqual(recomputed["adoption_id"], detail_v1["adoption_id"])
        self.assertEqual(
            recomputed["recomputed_adjusted_avg_ltv"],
            detail_v1["adjusted_avg_ltv"],
        )

    def test_two_versions_user_level_valuations_are_traceable(self) -> None:
        """两个版本受影响的用户估值均可核对：同一用户在两版明细中都可追溯。"""
        detail_v1 = ltv.build_forecast_detail("LTV-001", "RC-001", cycle="2026-11")
        ltv.publish_forecast(detail_v1["detail_id"])

        correction = rc.open_retention_correction("RC-001", reason="基准群迟到更正")
        rc.complete_retention_correction(correction["correction_id"])
        detail_v2 = ltv.build_forecast_detail("LTV-001", "RC-001", cycle="2026-11")
        ltv.publish_forecast(detail_v2["detail_id"])

        user_id = detail_v1["predicted_values"][0]["user_id"]
        v1_user = next(u for u in detail_v1["predicted_values"] if u["user_id"] == user_id)
        v2_user = next(u for u in detail_v2["predicted_values"] if u["user_id"] == user_id)
        # 两版都保留同一用户的估值，且各自绑定不同曲线指纹
        self.assertIn("retention_adjusted_value", v1_user)
        self.assertIn("retention_adjusted_value", v2_user)
        self.assertNotEqual(detail_v1["curve_fingerprint"], detail_v2["curve_fingerprint"])
        # 发布单可反查两版的明细与曲线
        releases = ltv.list_releases("LTV-001")
        self.assertEqual(len(releases), 2)
        bound_fingerprints = {r["curve_fingerprint"] for r in releases}
        self.assertEqual(
            bound_fingerprints,
            {detail_v1["curve_fingerprint"], detail_v2["curve_fingerprint"]},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
