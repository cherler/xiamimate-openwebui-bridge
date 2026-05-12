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

    def opportunity_discovery(
        self,
        marketplace: str = "US",
        category_id: int | None = None,
        category_path: str = "",
        limit: int = 10,
        _memory_profile: dict | None = None,
    ) -> str:
        """Discover product opportunity cards."""
        return ""

    def opportunity_discovery_job(
        self,
        job_id: str = "",
        marketplace: str = "US",
        include_result: bool = True,
        limit: int = 20,
    ) -> str:
        """Retrieve stored opportunity discovery job evidence."""
        return ""

    def launch_budget_calculator(
        self,
        product_theme: str = "",
        marketplace: str = "US",
        selling_price: float | None = None,
        unit_product_cost: float | None = None,
        landed_cost_per_unit: float | None = None,
        monthly_ad_budget: float | None = None,
        launch_units: int | None = None,
        launch_months: int | None = None,
    ) -> str:
        """Calculate deterministic launch budget and break-even scenarios."""
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
        self.assertTrue(any(tool["function"]["name"] == "opportunity_discovery_job" for tool in observed_payloads[0]["tools"]))
        self.assertTrue(any(tool["function"]["name"] == "launch_budget_calculator" for tool in observed_payloads[0]["tools"]))
        resolve_schema = next(
            tool for tool in observed_payloads[0]["tools"] if tool["function"]["name"] == "resolve_candidates"
        )["function"]["parameters"]["properties"]
        opportunity_schema = next(
            tool for tool in observed_payloads[0]["tools"] if tool["function"]["name"] == "opportunity_discovery"
        )["function"]["parameters"]["properties"]
        self.assertIn("recall_mode", resolve_schema)
        self.assertIn("category_id", resolve_schema)
        self.assertIn("category_path", resolve_schema)
        self.assertNotIn("query", opportunity_schema)
        self.assertNotIn("_memory_profile", opportunity_schema)
        self.assertIn("category_id", opportunity_schema)
        self.assertIn("category_path", opportunity_schema)
        self.assertIn("leaf_category_name / latest_snapshot.category_path", observed_payloads[0]["messages"][0]["content"])
        self.assertIn("pool_quality.is_sufficient_for_analysis=false", observed_payloads[0]["messages"][0]["content"])
        self.assertIn("candidate_pool_id", observed_payloads[0]["messages"][0]["content"])
        self.assertIn("candidate_expansion_status", observed_payloads[0]["messages"][0]["content"])
        self.assertIn("opportunity_discovery_job", observed_payloads[0]["messages"][0]["content"])
        self.assertIn("next_action.request", observed_payloads[0]["messages"][0]["content"])
        self.assertIn("rank/title/category_path", observed_payloads[0]["messages"][0]["content"])
        self.assertIn("tool_contract.capability", observed_payloads[0]["messages"][0]["content"])
        self.assertIn("launch_budget_calculator", observed_payloads[0]["messages"][0]["content"])

        second_messages = observed_payloads[1]["messages"]
        assistant_message = next(message for message in second_messages if message.get("role") == "assistant")
        tool_message = next(message for message in second_messages if message.get("role") == "tool")
        self.assertEqual(assistant_message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertIn("compacted_json", tool_message["content"])
        self.assertIn("candidate_asins", tool_message["content"])
        self.assertFalse(any("以下是工具执行结果" in str(message.get("content") or "") for message in second_messages))

    def test_web_route_uses_tavily_provider(self) -> None:
        pipe = self.make_pipeline()
        observed_requests = []
        observed_payloads = []

        pipe.valves.CHAT_BACKEND_SERVICE_SECRET = "test-secret"
        pipe._ensure_billing_context = lambda body: {"user_id": "user-1", "api_key": "test"}

        def chat_backend_request(**kwargs):
            observed_requests.append(copy.deepcopy(kwargs))
            return {
                "provider": "tavily",
                "query": "TikTok Shop US policy updates",
                "result_text": "Tavily result text",
                "results": [{"title": "Policy update", "domain": "seller-us.tiktok.com", "url": "https://example.com"}],
            }

        def post_agent_payload(payload: dict, model_name: str) -> dict:
            observed_payloads.append(copy.deepcopy(payload))
            return {"choices": [{"message": {"role": "assistant", "content": "分析结论：卖家应关注入驻和履约政策变化。"}}]}

        pipe._chat_backend_request = chat_backend_request
        pipe._post_agent_payload = post_agent_payload

        response = pipe._run_web_search(
            query="TikTok Shop US policy updates",
            body={"stream": False, "user": {"id": "user-1"}},
            model="xiamimate.agent",
        )

        self.assertEqual(response["choices"][0]["message"]["content"], "分析结论：卖家应关注入驻和履约政策变化。")
        self.assertEqual(observed_requests[0]["path"], "/internal/provider/web-search/tavily")
        self.assertEqual(observed_requests[0]["body"]["search_mode"], "auto")
        self.assertEqual(observed_requests[0]["body"]["time_range"], "month")
        self.assertNotIn("dify-web-search", json.dumps(observed_requests[0], ensure_ascii=False))
        self.assertIn("Tavily 外部检索证据", observed_payloads[0]["messages"][1]["content"])
        self.assertIn("请不要输出原始搜索结果列表", observed_payloads[0]["messages"][1]["content"])

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

    def test_opportunity_discovery_preserves_cards_text_for_llm(self) -> None:
        pipe = self.make_pipeline()
        cards_text = "\n".join(
            [
                "### 完整10大机会卡片",
                "| 排名 | 机会主题 | 得分 | 类目路径 | 窗口销量估算 | 样本ASIN数 | 日数据行数 | 销量增长 | 趋势增长 | 竞争Offer | 置信度 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            + [
                "| #%d | 真实机会%d | %.2f | Home & Kitchen > Storage > Box %d | %d | %d | %d | %d%% | %d%% | %.2f | medium |"
                % (index, index, 0.9 - index * 0.01, index, 1000 + index, 10 + index, 30 + index, index * 2, index * 3, 1.2 + index / 10)
                for index in range(1, 11)
            ]
            + [
                "",
                "字段解释：",
                "- 样本ASIN数：进入本次机会评分的真实 ASIN 数。",
                "- 竞争Offer：Keepa offer_count 的窗口均值，表示同一 ASIN 下的在售报价/卖家报价数量。",
            ]
        )
        payload = {
            "instruction": "将 opportunity_cards_text 作为工具证据块展示。",
            "opportunity_discovery_job_id": "odisc_test",
            "result_ref": {"type": "opportunity_discovery_job", "job_id": "odisc_test"},
            "opportunity_count": 10,
            "opportunity_cards_text": cards_text,
            "opportunities_for_llm": [
                {
                    "rank": index,
                    "title": "真实机会%d" % index,
                    "category_id": 1000 + index,
                    "category_path": "Home & Kitchen > Storage > Box %d" % index,
                    "opportunity_score": 0.9 - index * 0.01,
                    "candidate_count": 10 + index,
                    "row_count": 30 + index,
                    "next_action": {
                        "type": "analyze_theme",
                        "request": {
                            "product_query": "真实机会%d" % index,
                            "marketplace": "US",
                            "category_id": 1000 + index,
                            "category_path": "Home & Kitchen > Storage > Box %d" % index,
                            "recall_mode": "category",
                            "include_descendants": True,
                        },
                    },
                    "large_unused_field": "drop-me" * 200,
                }
                for index in range(1, 11)
            ],
            "metric_definitions": {"sales_window_sum": "窗口销量估算不是美元金额。"},
            "diagnostics": {"trace": "x" * 2000},
        }

        rendered = pipe._format_tool_result_for_llm("opportunity_discovery", json.dumps(payload, ensure_ascii=False), budget=7000)
        compacted = json.loads(rendered)

        self.assertEqual(compacted["result_format"], "opportunity_evidence_block")
        self.assertIn("工具证据块", compacted["instruction"])
        self.assertIn("不要把证据表改写成平铺列表", compacted["instruction"])
        self.assertEqual(compacted["payload"]["opportunity_discovery_job_id"], "odisc_test")
        self.assertEqual(compacted["payload"]["result_ref"]["job_id"], "odisc_test")
        self.assertEqual(compacted["payload"]["opportunity_count"], 10)
        self.assertIn("完整10大机会卡片", compacted["payload"]["opportunity_cards_text"])
        self.assertIn("| #10 | 真实机会10", compacted["payload"]["opportunity_cards_text"])
        self.assertIn("样本ASIN数", compacted["payload"]["opportunity_cards_text"])
        self.assertIn("竞争Offer", compacted["payload"]["opportunity_cards_text"])
        self.assertIn("真实机会3", rendered)
        self.assertEqual(compacted["payload"]["opportunities_for_llm"][2]["next_action"]["request"]["category_id"], 1003)
        self.assertEqual(compacted["payload"]["opportunities_for_llm"][2]["next_action"]["request"]["recall_mode"], "category")
        self.assertEqual(compacted["payload"]["opportunities_for_llm"][2]["next_action"]["request"]["category_path"], "Home & Kitchen > Storage > Box 3")
        self.assertNotIn("待补全", rendered)
        self.assertNotIn("估算区间", rendered)
        self.assertNotIn("结果已压缩截断", rendered)
        self.assertNotIn("large_unused_field", rendered)
        self.assertLessEqual(len(rendered), 7000)

    def test_repeated_opportunity_discovery_reuses_first_result_even_with_changed_args(self) -> None:
        pipe = self.make_pipeline()
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
                                        "name": "opportunity_discovery",
                                        "arguments": json.dumps({"query": "找机会", "marketplace": "US", "limit": 3}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {
                                        "name": "opportunity_discovery",
                                        "arguments": json.dumps({"marketplace": "US", "limit": 10}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        ]
        execute_count = {"value": 0}

        def post_agent_payload(payload: dict, model_name: str) -> dict:
            return responses.pop(0)

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            execute_count["value"] += 1
            return json.dumps(
                {
                    "success": True,
                    "opportunity_count": 1,
                    "opportunity_cards_text": "## 机会发现结果\n\n| 排名 | 机会主题 |\n|---:|---|\n| 1 | Women's Pants |",
                    "opportunities_for_llm": [{"rank": 1, "title": "Women's Pants"}],
                },
                ensure_ascii=False,
            )

        pipe._post_agent_payload = post_agent_payload
        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[{"role": "user", "content": "机会发现"}],
            body={},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="agent",
        )

        self.assertEqual(execute_count["value"], 1)
        self.assertIn("Women's Pants", answer)
        self.assertNotIn("不要补齐", answer)

    def test_explicit_theme_request_drops_opportunity_discovery_when_resolve_is_present(self) -> None:
        pipe = self.make_pipeline()
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
                                        "name": "opportunity_discovery",
                                        "arguments": json.dumps({"marketplace": "US", "limit": 10}),
                                    },
                                },
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {
                                        "name": "resolve_candidates",
                                        "arguments": json.dumps({"product_query": "car vacuum", "marketplace": "US"}),
                                    },
                                },
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "car vacuum 主题分析结果"}}]},
        ]
        executed_tools = []

        def post_agent_payload(payload: dict, model_name: str) -> dict:
            return responses.pop(0)

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            executed_tools.append(tool_call["name"])
            return json.dumps({"success": True, "data": {"candidate_asins": ["B001"]}}, ensure_ascii=False)

        pipe._post_agent_payload = post_agent_payload
        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[{"role": "user", "content": "请从用户需求、价格带、竞争强度和差异化空间四个角度，评估 car vacuum 在 Temu 美国站的机会"}],
            body={},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="agent",
        )

        self.assertEqual(executed_tools, ["resolve_candidates"])
        self.assertIn("car vacuum 主题分析结果", answer)

    def test_agent_round_limit_forces_final_synthesis_instead_of_error(self) -> None:
        pipe = self.make_pipeline()
        pipe.valves.AGENT_MAX_TOOL_ROUNDS = 2
        observed_payloads = []
        executed_tools = []

        def post_agent_payload(payload: dict, model_name: str) -> dict:
            observed_payloads.append(copy.deepcopy(payload))
            if len(observed_payloads) <= 2:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_%d" % len(observed_payloads),
                                        "type": "function",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": json.dumps({"query": "Temu car vacuum policy %d" % len(observed_payloads)}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            self.assertNotIn("tools", payload)
            self.assertIn("工具调用预算已用完", payload["messages"][-1]["content"])
            return {"choices": [{"message": {"role": "assistant", "content": "基于已有证据的最终分析。"}}]}

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            executed_tools.append(tool_call["name"])
            return json.dumps({"success": True, "summary": "tool evidence"}, ensure_ascii=False)

        pipe._post_agent_payload = post_agent_payload
        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[{"role": "user", "content": "评估 car vacuum 在 Temu 美国站的机会"}],
            body={},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="agent",
        )

        self.assertEqual(executed_tools, ["web_search", "web_search"])
        self.assertEqual(answer, "基于已有证据的最终分析。")

    def test_opportunity_discovery_drops_query_parameter_from_llm_call(self) -> None:
        pipe = self.make_pipeline()

        normalized = pipe._normalize_tool_call(
            "opportunity_discovery",
            {"query": "机会发现", "keyword": "找机会", "market": "US", "limit": 5},
        )

        self.assertIsNotNone(normalized)
        self.assertNotIn("query", normalized["parameters"])
        self.assertEqual(normalized["parameters"]["marketplace"], "US")
        self.assertEqual(normalized["parameters"]["limit"], 5)

    def test_invalid_opportunity_expansion_falls_back_to_real_cards(self) -> None:
        pipe = self.make_pipeline()
        raw_result = json.dumps(
            {
                "opportunity_count": 2,
                "opportunity_cards_text": "| 排名 | 机会主题 | 类目路径 |\n| --- | --- | --- |\n| #1 | 真实机会1 | Home & Kitchen |\n| #2 | 真实机会2 | Sports & Outdoors |",
                "opportunities_for_llm": [
                    {"rank": 1, "title": "真实机会1", "category_id": 11, "category_path": "Home & Kitchen"},
                    {"rank": 2, "title": "真实机会2", "category_id": 22, "category_path": "Sports & Outdoors"},
                ],
            },
            ensure_ascii=False,
        )
        observation = pipe._build_tool_observation({"name": "opportunity_discovery", "parameters": {}}, raw_result)

        answer = pipe._fallback_opportunity_answer_if_needed(
            "#3~#10 详情因结果压缩截断，基于机会得分分布提供估算区间。",
            [observation],
        )

        self.assertIn("真实机会1", answer)
        self.assertIn("真实机会2", answer)
        self.assertIn("本次机会发现返回的机会卡片", answer)
        self.assertIn("当前可继续分析的机会编号共有 2 个", answer)
        self.assertNotIn("估算区间", answer)

    def test_ungrounded_opportunity_answer_falls_back_to_real_cards(self) -> None:
        pipe = self.make_pipeline()
        raw_result = json.dumps(
            {
                "opportunity_count": 3,
                "opportunity_cards_text": "| 排名 | 机会主题 | 类目路径 |\n| --- | --- | --- |\n| #1 | 真实机会A | Home & Kitchen |\n| #2 | 真实机会B | Sports & Outdoors |\n| #3 | 真实机会C | Beauty |",
                "opportunities_for_llm": [
                    {"rank": 1, "title": "真实机会A", "category_id": 11},
                    {"rank": 2, "title": "真实机会B", "category_id": 22},
                    {"rank": 3, "title": "真实机会C", "category_id": 33},
                ],
            },
            ensure_ascii=False,
        )
        observation = pipe._build_tool_observation({"name": "opportunity_discovery", "parameters": {}}, raw_result)

        answer = pipe._fallback_opportunity_answer_if_needed(
            "## 机会发现结果\n\n1. 口腔护理\n2. 宠物用品\n3. 智能家居",
            [observation],
        )

        self.assertIn("真实机会A", answer)
        self.assertIn("真实机会B", answer)
        self.assertIn("真实机会C", answer)
        self.assertNotIn("口腔护理", answer)

    def test_opportunity_fallback_uses_raw_result_when_llm_result_is_overflow_only(self) -> None:
        pipe = self.make_pipeline()
        raw_result = json.dumps(
            {
                "success": True,
                "data": {
                    "opportunity_count": 1,
                    "opportunity_cards_text": "## 机会发现结果\n\n| 排名 | 机会主题 |\n|---:|---|\n| 1 | Women's Pants |",
                    "opportunities_for_llm": [{"rank": 1, "title": "Women's Pants"}],
                },
            },
            ensure_ascii=False,
        )
        overflow_only = json.dumps(
            {
                "tool_name": "opportunity_discovery",
                "payload": {
                    "opportunity_count": 1,
                    "overflow_note": "机会表文本过长，本次压缩保留可执行 opportunities_for_llm。",
                },
            },
            ensure_ascii=False,
        )

        answer = pipe._fallback_answer_from_tool_observations(
            [
                {
                    "tool_name": "opportunity_discovery",
                    "arguments": {"marketplace": "US", "limit": 10},
                    "llm_result": overflow_only,
                    "raw_result": raw_result,
                }
            ],
            error="upstream failed",
        )

        self.assertIn("Women's Pants", answer)
        self.assertIn("下面是本次机会发现返回的机会卡片", answer)
        self.assertNotIn("工具已经执行完成", answer)

    def test_memory_profile_context_is_compacted_and_injected(self) -> None:
        pipe = self.make_pipeline()
        payloads = []

        def chat_backend_request(method: str, path: str, body: dict | None = None, **kwargs) -> dict:
            payloads.append({"method": method, "path": path, "body": body, "kwargs": kwargs})
            return {
                "summary_version": "memory_profile_v1",
                "user_identity_summary": "verified user focused on US Amazon",
                "role_hint": "subscriber_user",
                "market_focus": ["US"],
                "preferred_platforms": ["amazon"],
                "risk_preference": "evidence_first",
                "decision_style": "evidence_first",
                "hard_constraints": ["避免侵权和重货"],
                "recent_topics": ["women pants"],
                "memory_confidence": {"market_focus": "high"},
                "evidence_sources": {"recent_messages": 3},
                "confidence_digest": "主要字段已有可用置信度。",
                "full_payload": {"large": "drop-me" * 500},
            }

        pipe._chat_backend_request = chat_backend_request

        profile = pipe._build_agent_memory_profile_context(
            messages=[{"role": "user", "content": "帮我找机会"}],
            body={"metadata": {"target_market": "US"}},
            billing_context={"user_id": "user_1"},
            mode="agent",
        )
        messages = pipe._inject_agent_system_prompt(
            [{"role": "user", "content": "帮我找机会"}],
            mode="agent",
            memory_profile=profile,
        )

        self.assertEqual(payloads[0]["path"], "/internal/provider/memory-profile/build")
        self.assertEqual(payloads[0]["body"]["user_id"], "user_1")
        self.assertEqual(payloads[0]["body"]["target_market"], "US")
        memory_message = messages[1]
        self.assertEqual(memory_message["role"], "system")
        self.assertIn("memory_profile_context", memory_message["content"])
        self.assertIn("women pants", memory_message["content"])
        self.assertNotIn("full_payload", memory_message["content"])

    def test_opportunity_discovery_receives_memory_profile_as_internal_context(self) -> None:
        pipe = self.make_pipeline()
        tool_call = {"name": "opportunity_discovery", "parameters": {"marketplace": "US", "limit": 5}}
        body = {"_xiamimate_memory_profile": {"recent_topics": ["women pants"], "risk_preference": "evidence_first"}}

        internal_call = pipe._attach_internal_tool_context(tool_call, body)
        public_call = pipe._strip_internal_tool_context(internal_call)

        self.assertIn("_memory_profile", internal_call["parameters"])
        self.assertNotIn("_memory_profile", public_call["parameters"])
        self.assertEqual(public_call["parameters"], {"marketplace": "US", "limit": 5})

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

    def test_launch_budget_calculator_aliases_are_normalized(self) -> None:
        pipe = self.make_pipeline()

        normalized = pipe._normalize_tool_call(
            "launch_budget_calculator",
            {"product": "Women's Pants", "price": 22.94, "landed_cost": 8.3, "ad_budget": 600, "units": 300},
        )

        self.assertIsNotNone(normalized)
        params = normalized["parameters"]
        self.assertEqual(params["product_theme"], "Women's Pants")
        self.assertEqual(params["selling_price"], 22.94)
        self.assertEqual(params["landed_cost_per_unit"], 8.3)
        self.assertEqual(params["monthly_ad_budget"], 600)
        self.assertEqual(params["launch_units"], 300)

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
                "tool_contract": {"capability": "asin_history_analysis"},
                "evidence_contract": {
                    "evidence_ledger": [
                        {
                            "evidence_id": "asin_history:B08FB31NH5:30d",
                            "allowed_claim_strength": "tool_fact",
                        }
                    ]
                },
            },
        }

        rendered = pipe._format_tool_result_for_llm("asin_history_timeseries", json.dumps(payload), budget=4000)
        compacted = json.loads(rendered)
        item = compacted["payload"]["data"]["items"][0]

        self.assertEqual(item["latest_snapshot"]["leaf_category_name"], "Hand Mixers")
        self.assertIn("Mixers > Hand Mixers", item["latest_snapshot"]["category_path"])
        self.assertEqual(item["window_summary"]["review_growth_window"], 84)
        self.assertEqual(compacted["payload"]["data"]["tool_contract"]["capability"], "asin_history_analysis")
        self.assertEqual(compacted["payload"]["data"]["evidence_contract"]["evidence_ledger"][0]["evidence_id"], "asin_history:B08FB31NH5:30d")
        self.assertTrue(item["series"]["_compacted_series"])
        self.assertLessEqual(len(rendered), 4000)


if __name__ == "__main__":
    unittest.main()