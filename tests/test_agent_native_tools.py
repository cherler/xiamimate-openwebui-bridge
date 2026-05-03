from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))

import xiamimate  # noqa: E402


class FakeAgentTools:
    def resolve_candidates(
        self,
        product_query: str,
        marketplace: str = "US",
        query_aliases: str = "",
        category_hints: str = "",
        recall_mode: str = "keyword",
        category_id: int | None = None,
        category_path: str = "",
        include_descendants: bool = True,
        min_pool_size: int = 8,
        target_pool_size: int = 20,
        expand_if_small: bool = False,
        max_candidates: int = 30,
    ) -> str:
        """Resolve candidate ASIN pool."""
        return ""

    def category_resolve(
        self,
        category_query: str = "",
        category_path: str = "",
        marketplace: str = "US",
        max_matches: int = 10,
    ) -> str:
        """Resolve product category IDs and local coverage."""
        return ""

    def asin_history_timeseries(
        self,
        asins: str,
        marketplace: str = "US",
        window_days: int = 30,
        metrics: str = "review_count",
    ) -> str:
        """Load ASIN history timeseries."""
        return ""

    def candidate_pool_stats(
        self,
        candidate_asins: str = "",
        candidate_pool_id: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        product_query: str = "",
    ) -> str:
        """Get candidate pool stats."""
        return ""

    def category_benchmark(
        self,
        candidate_asins: str = "",
        candidate_pool_id: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        benchmark_category_id: int | None = None,
        benchmark_category_path: str = "",
        benchmark_level: str = "auto",
        include_descendants: bool = True,
        product_query: str = "",
    ) -> str:
        """Compare candidate pool against a benchmark category."""
        return ""

    def expand_candidates(
        self,
        product_query: str = "",
        marketplace: str = "US",
        recall_mode: str = "hybrid",
        category_id: int | None = None,
        category_path: str = "",
        include_descendants: bool = True,
        target_asin_count: int = 20,
        min_pool_size: int = 8,
        priority: str = "interactive_normal",
        requested_by_session_id: str = "",
        idempotency_key: str = "",
        notes: str = "",
    ) -> str:
        """Queue candidate expansion."""
        return ""

    def candidate_expansion_status(
        self,
        job_id: str = "",
        marketplace: str = "US",
        statuses: str = "queued,waiting_token",
        limit: int = 20,
    ) -> str:
        """Query candidate expansion status."""
        return ""


class TextToolOnlyProvider:
    allowed_params = {"messages", "stream", "temperature"}

    def filter_payload(self, body: dict) -> dict:
        return {key: value for key, value in body.items() if key in self.allowed_params}

    def has_internal_markup(self, text: str) -> bool:
        return "$tool_calls" in str(text or "").lower()

    def strip_internal_markup(self, content: str) -> str:
        return str(content or "")

    def extract_provider_tool_calls(self, content: str) -> list[dict]:
        return []

    def supports_streaming_final_answer(self) -> bool:
        return True


class AgentNativeToolTests(unittest.TestCase):
    def make_pipeline(self) -> xiamimate.Pipeline:
        pipe = xiamimate.Pipeline()
        pipe.agent_tools = FakeAgentTools()
        pipe._charge_billing_event = lambda **kwargs: {"points_charged": 0}
        pipe._refund_billing_event = lambda **kwargs: None
        return pipe

    def test_native_tool_calls_are_returned_as_role_tool_messages_before_final_answer(self) -> None:
        pipe = self.make_pipeline()
        observed_payloads = []
        responses = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "resolve_candidates",
                                        "arguments": json.dumps(
                                            {"product_query": "humidifier", "marketplace": "US", "max_candidates": 3},
                                            ensure_ascii=False,
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "最终答案: B001 是候选 ASIN。"}}]},
        ]

        def post_agent_payload(payload: dict, model_name: str) -> dict:
            observed_payloads.append(copy.deepcopy(payload))
            return responses.pop(0)

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            self.assertFalse(truncate)
            self.assertEqual(tool_call["name"], "resolve_candidates")
            return json.dumps(
                {
                    "success": True,
                    "data": {
                        "candidate_asins": ["B001"],
                        "candidate_items": [
                            {
                                "asin": "B001",
                                "brand": "DREO",
                                "category_path": "Home & Kitchen > Humidifiers",
                                "match_score": 99.5,
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )

        pipe._post_agent_payload = post_agent_payload
        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[{"role": "user", "content": "请调用 resolve_candidates 获取 humidifier 候选 ASIN。"}],
            body={},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="tool",
        )

        self.assertIn("最终答案", answer)
        self.assertEqual(len(observed_payloads), 2)
        self.assertEqual(observed_payloads[0]["tool_choice"], "auto")
        self.assertTrue(any(tool["function"]["name"] == "resolve_candidates" for tool in observed_payloads[0]["tools"]))
        self.assertTrue(any(tool["function"]["name"] == "category_resolve" for tool in observed_payloads[0]["tools"]))
        self.assertTrue(any(tool["function"]["name"] == "expand_candidates" for tool in observed_payloads[0]["tools"]))
        self.assertTrue(any(tool["function"]["name"] == "candidate_expansion_status" for tool in observed_payloads[0]["tools"]))
        resolve_schema = next(
            tool for tool in observed_payloads[0]["tools"] if tool["function"]["name"] == "resolve_candidates"
        )["function"]["parameters"]["properties"]
        self.assertIn("recall_mode", resolve_schema)
        self.assertIn("category_id", resolve_schema)
        self.assertIn("category_path", resolve_schema)
        self.assertIn("leaf_category_name / latest_snapshot.category_path", observed_payloads[0]["messages"][0]["content"])
        self.assertIn("pool_quality.is_sufficient_for_analysis=false", observed_payloads[0]["messages"][0]["content"])
        self.assertIn("candidate_pool_id", observed_payloads[0]["messages"][0]["content"])
        self.assertIn("candidate_expansion_status", observed_payloads[0]["messages"][0]["content"])

        second_messages = observed_payloads[1]["messages"]
        assistant_message = next(message for message in second_messages if message.get("role") == "assistant")
        tool_message = next(message for message in second_messages if message.get("role") == "tool")
        self.assertEqual(assistant_message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertIn("compacted_json", tool_message["content"])
        self.assertIn("candidate_asins", tool_message["content"])
        self.assertFalse(any("以下是工具执行结果" in str(message.get("content") or "") for message in second_messages))

    def test_native_capable_provider_does_not_execute_text_tool_markup(self) -> None:
        pipe = self.make_pipeline()
        observed_payloads = []

        def post_agent_payload(payload: dict, model_name: str) -> dict:
            observed_payloads.append(copy.deepcopy(payload))
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '$TOOL_CALLS = [{"name":"web_search","arguments":{}}]\n$ABORT_CONTROLLER = null',
                            "reasoning_content": "native-capable provider should not use text tool fallback",
                        }
                    }
                ]
            }

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            raise AssertionError("text tool markup should not be executed when native tools are available")

        pipe._post_agent_payload = post_agent_payload
        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[{"role": "user", "content": "查一下补池状态。"}],
            body={},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="tool",
        )

        self.assertIn("未生成可展示的结果", answer)
        self.assertEqual(len(observed_payloads), 1)
        self.assertEqual(observed_payloads[0]["tool_choice"], "auto")

    def test_legacy_text_tool_path_preserves_reasoning_content(self) -> None:
        pipe = self.make_pipeline()
        text_only_provider = TextToolOnlyProvider()
        pipe._get_provider = lambda model_name=None: text_only_provider
        observed_payloads = []
        responses = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '$TOOL_CALLS = [{"name":"resolve_candidates","arguments":{"product_query":"humidifier"}}]',
                            "reasoning_content": "must be sent back on the next request",
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "最终答复。"}}]},
        ]

        def post_agent_payload(payload: dict, model_name: str) -> dict:
            observed_payloads.append(copy.deepcopy(payload))
            return responses.pop(0)

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            self.assertEqual(tool_call["name"], "resolve_candidates")
            return json.dumps({"success": True, "data": {"candidate_asins": ["B001"]}}, ensure_ascii=False)

        pipe._post_agent_payload = post_agent_payload
        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[{"role": "user", "content": "兼容旧文本工具调用。"}],
            body={},
            billing_context={"api_key": "test"},
            model_name="legacy-text-tools",
            mode="tool",
        )

        self.assertEqual(answer, "最终答复。")
        assistant_message = next(message for message in observed_payloads[1]["messages"] if message.get("role") == "assistant")
        self.assertEqual(assistant_message["reasoning_content"], "must be sent back on the next request")

    def test_resolve_candidates_compaction_prioritizes_candidate_identity_fields(self) -> None:
        pipe = self.make_pipeline()
        payload = {
            "success": True,
            "message": "candidate pool resolved",
            "data": {
                "marketplace": "US",
                "recall_mode": "hybrid",
                "candidate_pool_id": "11111111-1111-4111-8111-111111111111",
                "candidate_pool_version": 1,
                "category_constraint": {
                    "applied": True,
                    "category_id": 12345,
                    "category_path": "Home & Kitchen > Heating, Cooling & Air Quality > Humidifiers",
                },
                "normalized_query": "humidifier",
                "candidate_count": 20,
                "pool_quality": {
                    "candidate_count": 20,
                    "is_sufficient_for_analysis": True,
                    "should_expand_pool": False,
                    "dominant_leaf_category": "Humidifiers",
                    "dominant_leaf_share": 1.0,
                    "insufficient_coverage_reason": None,
                },
                "candidate_asins": [f"B{i:09d}" for i in range(20)],
                "query_normalization": {
                    "pipeline_mode": "rules_simple_english->rules_simple_english",
                    "pipeline_llm_used": False,
                },
                "ranking_policy": {
                    "primary_sort": ["match_score", "business_priority"],
                    "matched_fields": ["product_title", "category_path", "keywords"],
                },
                "timing_ms": {"query_normalization": 0, "domain_candidate_fetch": 15, "scoring_and_sorting": 4},
                "candidate_items": [
                    {
                        "asin": f"B{i:09d}",
                        "brand": f"Brand{i}",
                        "product_title": "Humidifier " + ("x" * 200),
                        "category_path": "Home & Kitchen > Heating, Cooling & Air Quality > Humidifiers",
                        "match_score": 100 - i,
                        "unused_payload": "drop-me" * 200,
                    }
                    for i in range(20)
                ],
            },
        }

        rendered = pipe._format_tool_result_for_llm("resolve_candidates", json.dumps(payload), budget=5000)
        compacted = json.loads(rendered)
        item = compacted["payload"]["data"]["candidate_items"][0]

        self.assertEqual(item["asin"], "B000000000")
        self.assertEqual(item["brand"], "Brand0")
        self.assertIn("Humidifiers", item["category_path"])
        self.assertIn("match_score", item)
        self.assertEqual(compacted["payload"]["data"]["ranking_policy"]["primary_sort"][0], "match_score")
        self.assertEqual(compacted["payload"]["data"]["timing_ms"]["query_normalization"], 0)
        self.assertEqual(compacted["payload"]["data"]["candidate_pool_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(compacted["payload"]["data"]["recall_mode"], "hybrid")
        self.assertEqual(compacted["payload"]["data"]["category_constraint"]["category_id"], 12345)
        self.assertTrue(compacted["payload"]["data"]["pool_quality"]["is_sufficient_for_analysis"])
        self.assertEqual(compacted["payload"]["data"]["pool_quality"]["dominant_leaf_category"], "Humidifiers")
        self.assertFalse(compacted["payload"]["data"]["query_normalization"]["pipeline_llm_used"])
        self.assertIn("full ranked ASIN pool", compacted["payload"]["data"]["candidate_pool_contract"]["candidate_asins"])
        self.assertIn("stable reference", compacted["payload"]["data"]["candidate_pool_contract"]["candidate_pool_id"])
        self.assertNotIn("unused_payload", rendered)
        self.assertLessEqual(len(rendered), 5000)

    def test_resolve_candidates_aliases_support_category_recall_controls(self) -> None:
        pipe = self.make_pipeline()

        normalized = pipe._normalize_tool_call(
            "resolve_candidates",
            {
                "query": "humidifier",
                "mode": "hybrid",
                "categoryId": 12345,
                "path": "Home & Kitchen > Humidifiers",
                "descendants": True,
                "min_candidates": 6,
                "target_candidates": 18,
                "market": "US",
            },
        )

        self.assertIsNotNone(normalized)
        params = normalized["parameters"]
        self.assertEqual(params["product_query"], "humidifier")
        self.assertEqual(params["recall_mode"], "hybrid")
        self.assertEqual(params["category_id"], 12345)
        self.assertEqual(params["category_path"], "Home & Kitchen > Humidifiers")
        self.assertTrue(params["include_descendants"])
        self.assertEqual(params["min_pool_size"], 6)
        self.assertEqual(params["target_pool_size"], 18)
        self.assertEqual(params["marketplace"], "US")

    def test_category_benchmark_aliases_support_explicit_anchor(self) -> None:
        pipe = self.make_pipeline()

        normalized = pipe._normalize_tool_call(
            "category_benchmark",
            {
                "asins": "B000000001,B000000002",
                "categoryId": 12345,
                "full_path": "Home & Kitchen > Humidifiers",
                "level": "leaf",
                "descendants": False,
                "market": "US",
            },
        )

        self.assertIsNotNone(normalized)
        params = normalized["parameters"]
        self.assertEqual(params["candidate_asins"], "B000000001,B000000002")
        self.assertEqual(params["benchmark_category_id"], 12345)
        self.assertEqual(params["benchmark_category_path"], "Home & Kitchen > Humidifiers")
        self.assertEqual(params["benchmark_level"], "leaf")
        self.assertFalse(params["include_descendants"])
        self.assertEqual(params["marketplace"], "US")

    def test_downstream_pool_tools_accept_candidate_pool_id_alias(self) -> None:
        pipe = self.make_pipeline()

        normalized = pipe._normalize_tool_call(
            "candidate_pool_stats",
            {
                "pool_id": "11111111-1111-4111-8111-111111111111",
                "market": "US",
                "window_days": 30,
            },
        )

        self.assertIsNotNone(normalized)
        params = normalized["parameters"]
        self.assertEqual(params["candidate_pool_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(params["marketplace"], "US")

    def test_expansion_tool_aliases_support_category_job_controls(self) -> None:
        pipe = self.make_pipeline()

        normalized = pipe._normalize_tool_call(
            "expand_candidates",
            {
                "query": "humidifier",
                "categoryId": 12345,
                "full_path": "Home & Kitchen > Humidifiers",
                "target_candidates": 20,
                "min_candidates": 8,
                "request_id": "req-1",
                "market": "US",
            },
        )

        self.assertIsNotNone(normalized)
        params = normalized["parameters"]
        self.assertEqual(params["product_query"], "humidifier")
        self.assertEqual(params["category_id"], 12345)
        self.assertEqual(params["category_path"], "Home & Kitchen > Humidifiers")
        self.assertEqual(params["target_asin_count"], 20)
        self.assertEqual(params["min_pool_size"], 8)
        self.assertEqual(params["idempotency_key"], "req-1")
        self.assertEqual(params["marketplace"], "US")

    def test_expansion_status_aliases_support_job_id(self) -> None:
        pipe = self.make_pipeline()

        normalized = pipe._normalize_tool_call(
            "candidate_expansion_status",
            {"jobId": "kexp_1", "status": "queued,waiting_token", "top_k": 3},
        )

        self.assertIsNotNone(normalized)
        params = normalized["parameters"]
        self.assertEqual(params["job_id"], "kexp_1")
        self.assertEqual(params["statuses"], "queued,waiting_token")
        self.assertEqual(params["limit"], 3)

    def test_tool_wrapper_extracts_nested_candidate_asins(self) -> None:
        tools = xiamimate.Pipeline()._load_agent_tools()
        payload = {
            "success": True,
            "data": {
                "candidate_asins": ["B000000001", "B000000002"],
                "candidate_items": [{"asin": "B000000003"}],
            },
        }

        self.assertEqual(tools._extract_candidate_asins(payload), ["B000000001", "B000000002"])

    def test_asin_history_compaction_keeps_category_facts_and_window_summary(self) -> None:
        pipe = self.make_pipeline()
        payload = {
            "success": True,
            "message": "asin history timeseries loaded",
            "data": {
                "marketplace": "US",
                "window_days": 30,
                "items": [
                    {
                        "asin": "B08FB31NH5",
                        "history_status": "ready",
                        "series": [{"date": f"2026-04-{day:02d}", "review_count": 3700 + day} for day in range(1, 31)],
                        "latest_snapshot": {
                            "asin": "B08FB31NH5",
                            "product_title": "WEPSEN Hand Mixer",
                            "brand": "WEPSEN",
                            "category_path": "Home & Kitchen > Kitchen & Dining > Small Appliances > Mixers > Hand Mixers",
                            "l3_category_name": "Small Appliances",
                            "leaf_category_name": "Hand Mixers",
                            "review_count": 3796,
                        },
                        "window_summary": {"review_growth_window": 84, "series_row_count": 30, "coverage_ratio": 1.0},
                    }
                ],
            },
        }

        rendered = pipe._format_tool_result_for_llm("asin_history_timeseries", json.dumps(payload), budget=4000)
        compacted = json.loads(rendered)
        item = compacted["payload"]["data"]["items"][0]

        self.assertEqual(item["latest_snapshot"]["leaf_category_name"], "Hand Mixers")
        self.assertIn("Mixers > Hand Mixers", item["latest_snapshot"]["category_path"])
        self.assertEqual(item["window_summary"]["review_growth_window"], 84)
        self.assertTrue(item["series"]["_compacted_series"])
        self.assertLessEqual(len(rendered), 4000)


if __name__ == "__main__":
    unittest.main()