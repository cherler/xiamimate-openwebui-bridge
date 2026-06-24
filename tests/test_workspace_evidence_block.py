from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))

import xiamimate  # noqa: E402


class EvidenceBlockTests(unittest.TestCase):
    def make_pipeline(self) -> "xiamimate.Pipeline":
        return xiamimate.Pipeline()

    WORKSPACE_PAYLOAD = {
        "theme_key": "blender",
        "title": "便携榨汁机",
        "brief": {"product_theme": "blender"},
        "evidence": {"trend_series": [1, 2, 3]},
    }
    UPSERT_DATA = {
        "workspace_id": "ws_1",
        "evidence_charts": [
            {"kind": "trend", "title": "趋势", "svg_url": "https://x.test/portal/api/evidence/chart/tok.svg"},
            {"kind": "competition", "title": "竞争度", "svg_url": "https://x.test/portal/api/evidence/chart/tok2.svg"},
        ],
        "workspace_url": "https://x.test/portal/workspace?id=ws_1",
    }

    def test_flag_off_returns_answer_unchanged(self) -> None:
        pipe = self.make_pipeline()
        pipe.valves.WORKSPACE_EVIDENCE_ENABLED = False
        out = pipe._maybe_render_evidence_block("原始答案", {"workspace_payload": self.WORKSPACE_PAYLOAD}, {"user": "u1"})
        self.assertEqual(out, "原始答案")

    def test_no_payload_returns_answer_unchanged(self) -> None:
        pipe = self.make_pipeline()
        pipe.valves.WORKSPACE_EVIDENCE_ENABLED = True
        out = pipe._maybe_render_evidence_block("原始答案", {"report_payload": {}}, {"user": "u1"})
        self.assertEqual(out, "原始答案")

    def test_upsert_failure_falls_back_to_plain_answer(self) -> None:
        pipe = self.make_pipeline()
        pipe.valves.WORKSPACE_EVIDENCE_ENABLED = True

        def boom(*args, **kwargs):
            raise RuntimeError("backend down")

        pipe._chat_backend_request = boom
        out = pipe._maybe_render_evidence_block(
            "原始答案", {"workspace_payload": self.WORKSPACE_PAYLOAD}, {"user": "u1"}
        )
        self.assertEqual(out, "原始答案")

    def test_evidence_block_prepended_on_success(self) -> None:
        pipe = self.make_pipeline()
        pipe.valves.WORKSPACE_EVIDENCE_ENABLED = True
        captured = {}

        def fake_request(method, path, body=None, internal=False, timeout=None, **kwargs):
            captured["path"] = path
            captured["body"] = body
            captured["internal"] = internal
            return self.UPSERT_DATA

        pipe._chat_backend_request = fake_request
        out = pipe._maybe_render_evidence_block(
            "原始答案", {"workspace_payload": self.WORKSPACE_PAYLOAD}, {"user": "u1"}
        )
        self.assertIn("![趋势](https://x.test/portal/api/evidence/chart/tok.svg)", out)
        self.assertIn("打开商品工作台", out)
        self.assertTrue(out.rstrip().endswith("原始答案"))
        self.assertEqual(captured["path"], "/internal/workspace/upsert-from-analysis")
        self.assertTrue(captured["internal"])
        self.assertEqual(captured["body"]["theme_key"], "blender")
        self.assertEqual(captured["body"]["user_id"], "u1")

    def test_extract_workspace_payload_nested(self) -> None:
        pipe = self.make_pipeline()
        nested = {"report_payload": {"workspace_payload": self.WORKSPACE_PAYLOAD}}
        self.assertEqual(pipe._extract_workspace_payload(nested), self.WORKSPACE_PAYLOAD)
        self.assertIsNone(pipe._extract_workspace_payload({"report_payload": {}}))

    def test_build_evidence_block_markdown_empty_when_no_charts(self) -> None:
        pipe = self.make_pipeline()
        self.assertEqual(pipe._build_evidence_block_markdown({"evidence_charts": []}), "")

    # --- 端到端连接层：从选品报告合成 workspace_payload ---

    SELECTION_REPORT = {
        "source_context": {"product_query": "挂脖风扇", "marketplace": "US"},
        "coverage_status": {"forecast_type": "candidate", "overall_status": "ok"},
        "data_tables": [
            {
                "table_id": "forecast_top_asins",
                "columns": [
                    {"key": "predicted_weekly_sales_w1"},
                    {"key": "predicted_weekly_sales_w4"},
                ],
                "rows": [
                    {"predicted_weekly_sales_w1": 100, "predicted_weekly_sales_w4": 180},
                    {"predicted_weekly_sales_w1": 50, "predicted_weekly_sales_w4": 90},
                ],
            }
        ],
        "raw_endpoint_results": {
            "candidate_pool_weak_forecast": {
                "risk_flags": ["供给集中度偏高", {"name": "评论壁垒", "severity": "high"}]
            }
        },
    }

    def test_synthesize_workspace_payload_from_selection_report(self) -> None:
        pipe = self.make_pipeline()
        wp = pipe._build_workspace_payload_from_report(self.SELECTION_REPORT, {"user": "u1"})
        self.assertIsNotNone(wp)
        self.assertEqual(wp["theme_key"], "挂脖风扇")
        self.assertEqual(wp["title"], "挂脖风扇")
        evidence = wp["evidence"]
        # 趋势：按周聚合 Top ASIN 预测周销量 -> [w1_sum, w4_sum]
        self.assertEqual(evidence["trend_series"], [150.0, 270.0])
        # 风险灯：真实 risk_flags 归整为 [{name, level}]
        names = [r["name"] for r in evidence["risk_lights"]]
        self.assertIn("供给集中度偏高", names)
        self.assertIn("评论壁垒", names)
        levels = {r["name"]: r["level"] for r in evidence["risk_lights"]}
        self.assertEqual(levels["评论壁垒"], "bad")

    def test_synthesize_returns_none_when_not_selection_report(self) -> None:
        pipe = self.make_pipeline()
        self.assertIsNone(pipe._build_workspace_payload_from_report({"foo": "bar"}, {"user": "u1"}))

    def test_evidence_block_end_to_end_via_synthesis(self) -> None:
        pipe = self.make_pipeline()
        pipe.valves.WORKSPACE_EVIDENCE_ENABLED = True
        captured = {}

        def fake_request(method, path, body=None, internal=False, timeout=None, **kwargs):
            captured["body"] = body
            return {
                "evidence_charts": [
                    {"kind": "trend", "title": "趋势", "svg_url": "https://x.test/portal/api/evidence/chart/t.svg"}
                ],
                "workspace_url": "https://x.test/portal/workspace?id=ws_9",
            }

        pipe._chat_backend_request = fake_request
        out = pipe._maybe_render_evidence_block("原始答案", self.SELECTION_REPORT, {"user": "u1"})
        self.assertIn("![趋势]", out)
        self.assertTrue(out.rstrip().endswith("原始答案"))
        self.assertEqual(captured["body"]["theme_key"], "挂脖风扇")
        self.assertEqual(captured["body"]["evidence"]["trend_series"], [150.0, 270.0])


if __name__ == "__main__":
    unittest.main()
