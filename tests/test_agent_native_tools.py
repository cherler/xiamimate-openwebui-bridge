from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipelines"))

import xiamimate  # noqa: E402
import xiamimate_mode_router  # noqa: E402


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

    def candidate_pool_slice(
        self,
        candidate_asins: str = "",
        candidate_pool_id: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        brand_include: str = "",
        title_keywords: str = "",
        material_keywords: str = "",
        price_min: float = None,
        price_max: float = None,
        sort_by: str = "sales_window_sum",
        top_n: int = 3,
        product_query: str = "",
    ) -> str:
        """Slice candidate pool by brand, title, material, or price range."""
        return ""

    def asin_review_insights(
        self,
        candidate_asins: str = "",
        candidate_pool_id: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        max_asins: int = 10,
        product_query: str = "",
    ) -> str:
        """Fetch review insight provider status."""
        return ""

    def amazon_keyword_demand(
        self,
        keywords: str = "",
        product_query: str = "",
        marketplace: str = "US",
    ) -> str:
        """Fetch Amazon keyword demand provider status."""
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
        referral_fee_rate: float | None = None,
        coupon_discount_rate: float | None = None,
        return_rate: float | None = None,
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

    def test_mode_router_redirects_stale_agent_model_to_default_explicit_id(self) -> None:
        router = xiamimate_mode_router.Pipeline()
        router.valves.default_profile = "minimax"
        body = {
            "model": "xiamimate.agent",
            "messages": [{"role": "user", "content": "/agent 分析挂脖风扇"}],
        }
        result = asyncio.run(router.inlet(body))
        self.assertEqual(result["model"], "xiamimate.agent-minimax")
        self.assertEqual(result["xiamimate_mode"], "agent")

    def test_mode_router_preserves_explicit_profile_selection(self) -> None:
        router = xiamimate_mode_router.Pipeline()
        router.valves.default_profile = "minimax"
        body = {
            "model": "xiamimate.agent-deepseek",
            "messages": [{"role": "user", "content": "/report 标准报告"}],
        }
        result = asyncio.run(router.inlet(body))
        self.assertEqual(result["model"], "xiamimate.agent-deepseek")
        self.assertEqual(result["xiamimate_mode"], "report")

    def test_default_profile_uses_explicit_pipeline_id_and_label(self) -> None:
        pipe = self.make_pipeline()
        pipe.valves.AGENT_MODEL_DEFAULT_PROFILE = "minimax"
        pipe.pipelines = pipe._build_agent_pipelines()

        self.assertEqual([entry["id"] for entry in pipe.pipelines], ["agent-minimax", "agent-deepseek"])
        self.assertEqual([entry["name"] for entry in pipe.pipelines], ["Agent · MiniMax M2.7", "Agent · DeepSeek V4 Pro"])
        self.assertTrue(all(entry["description"] == xiamimate.AGENT_MODEL_DESCRIPTION for entry in pipe.pipelines))
        self.assertTrue(all(entry["info"]["meta"]["description"] == xiamimate.AGENT_MODEL_DESCRIPTION for entry in pipe.pipelines))

    def test_legacy_agent_alias_resolves_to_default_explicit_pipeline_id(self) -> None:
        pipe = self.make_pipeline()
        pipe.valves.AGENT_MODEL_DEFAULT_PROFILE = "minimax"
        pipe.pipelines = pipe._build_agent_pipelines()

        self.assertEqual(pipe._resolve_agent_profile(model_id="xiamimate.agent", body={"model": "xiamimate.agent"}), "minimax")
        self.assertEqual(pipe._response_model_for_profile("minimax", "xiamimate.agent"), "xiamimate.agent-minimax")

    def test_openwebui_internal_task_bypasses_billing_and_agent_tools(self) -> None:
        pipe = self.make_pipeline()
        observed_payloads = []
        pipe._charge_billing_event = lambda **kwargs: (_ for _ in ()).throw(AssertionError("internal task must not bill"))
        pipe._ensure_billing_context = lambda body: (_ for _ in ()).throw(AssertionError("internal task must not exchange billing context"))
        pipe._post_agent_payload = lambda payload, model_name: observed_payloads.append(payload) or {
            "choices": [{"message": {"content": "新手选品提示词指南"}}]
        }

        response = pipe.pipe(
            user_message="",
            model_id="xiamimate.agent",
            messages=[{"role": "user", "content": "/help 新手卖家第一次使用虾米选品"}],
            body={
                "model": "xiamimate.agent",
                "metadata": {"task": "title_generation"},
                "messages": [{"role": "user", "content": "/help 新手卖家第一次使用虾米选品"}],
                "tools": [{"type": "function", "function": {"name": "web_search"}}],
                "tool_choice": "auto",
            },
        )

        self.assertEqual(response["choices"][0]["message"]["content"], "新手选品提示词指南")
        self.assertEqual(len(observed_payloads), 1)
        self.assertNotIn("tools", observed_payloads[0])
        self.assertNotIn("tool_choice", observed_payloads[0])

    def test_points_command_is_free_and_uses_account_overview(self) -> None:
        pipe = self.make_pipeline()
        requested_paths = []
        pipe._charge_billing_event = lambda **kwargs: (_ for _ in ()).throw(AssertionError("/points must not bill"))

        def chat_backend_request(method: str, path: str, **kwargs) -> dict:
            requested_paths.append(path)
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/v1/me/account-overview")
            return {
                "user": {"user_id": "user-1", "display_name": "testuser", "email": "test@example.com"},
                "points_account": {"balance_points": 16505},
                "balance_breakdown": {"subscription_balance_points": 15543, "recharge_balance_points": 3, "other_balance_points": 959},
                "usage_summary": {},
                "recent_ledger": [],
            }

        pipe._chat_backend_request = chat_backend_request
        response = pipe.pipe(
            user_message="/points",
            model_id="xiamimate.agent",
            messages=[{"role": "user", "content": "/points"}],
            body={"model": "xiamimate.agent", "stream": False, "user": {"id": "user-1", "email": "test@example.com"}},
        )

        self.assertEqual(requested_paths, ["/v1/me/account-overview"])
        content = response["choices"][0]["message"]["content"]
        self.assertIn("积分余额", content)
        self.assertIn("16505", content)

    def test_native_tool_calls_are_accepted_as_planner_compat_actions(self) -> None:
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
        self.assertNotIn("tools", observed_payloads[0])
        self.assertNotIn("tool_choice", observed_payloads[0])
        next_planner_context = json.loads(observed_payloads[1]["messages"][-1]["content"])
        self.assertEqual(next_planner_context["already_observed_tools"], ["resolve_candidates"])
        self.assertEqual(next_planner_context["previous_tool_observations"][0]["tool_name"], "resolve_candidates")
        self.assertIn("candidate_asins", next_planner_context["previous_tool_observations"][0]["result"])

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

    def test_tool_web_search_alias_redirects_to_web_route(self) -> None:
        pipe = self.make_pipeline()
        observed_calls = []

        def run_web_search(query: str, body: dict, model: str, model_name: str = "") -> dict:
            observed_calls.append({"query": query, "model": model, "model_name": model_name})
            return pipe._chat_response(content="web result", model=model)

        pipe._run_web_search = run_web_search
        response = pipe.pipe(
            user_message="/tool 请调用 web_search，搜索最近 30 天 portable fan overseas market trend，并返回搜索摘要和来源。",
            model_id="xiamimate.agent-minimax",
            messages=[
                {
                    "role": "user",
                    "content": "/tool 请调用 web_search，搜索最近 30 天 portable fan overseas market trend，并返回搜索摘要和来源。",
                }
            ],
            body={"model": "xiamimate.agent-minimax", "stream": False, "messages": []},
        )

        self.assertEqual(response["choices"][0]["message"]["content"], "web result")
        self.assertEqual(len(observed_calls), 1)
        self.assertEqual(observed_calls[0]["query"], "搜索最近 30 天 portable fan overseas market trend，并返回搜索摘要和来源。")
        self.assertEqual(observed_calls[0]["model"], "xiamimate.agent-minimax")
        self.assertEqual(observed_calls[0]["model_name"], "MiniMax-M2.7-highspeed")

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
        self.assertNotIn("tool_choice", observed_payloads[0])
        self.assertNotIn("tools", observed_payloads[0])

    def test_legacy_text_tool_markup_is_ignored_by_planner_path(self) -> None:
        pipe = self.make_pipeline()
        text_only_provider = TextToolOnlyProvider()
        pipe._get_provider = lambda model_name=None: text_only_provider
        observed_payloads = []

        def post_agent_payload(payload: dict, model_name: str) -> dict:
            observed_payloads.append(copy.deepcopy(payload))
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '$TOOL_CALLS = [{"name":"resolve_candidates","arguments":{"product_query":"humidifier"}}]',
                            "reasoning_content": "legacy text markup should not drive the planner path",
                        }
                    }
                ]
            }

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            raise AssertionError("legacy text markup should not be executed in planner harness mode")

        pipe._post_agent_payload = post_agent_payload
        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[{"role": "user", "content": "兼容旧文本工具调用。"}],
            body={},
            billing_context={"api_key": "test"},
            model_name="legacy-text-tools",
            mode="tool",
        )

        self.assertEqual(answer, "未生成可展示的结果。")
        self.assertEqual(len(observed_payloads), 1)

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
        self.assertIn("工具证据来源", compacted["instruction"])
        self.assertIn("每卡包含机会理由", compacted["instruction"])
        self.assertIn("精简排名表", compacted["instruction"])
        self.assertIn("中文翻译", compacted["instruction"])
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
        executed_tools = []
        pipe._classify_agent_scene = lambda messages, mode="agent": "theme_analysis"

        def plan_agent_next_steps(**kwargs) -> dict:
            observed = kwargs.get("tool_observations") or []
            if observed:
                return {
                    "scene": "theme_analysis",
                    "answer_ready": True,
                    "final_answer": "car vacuum 主题分析结果",
                    "reasoning_summary": "已有候选池证据，无需再做机会发现。",
                    "steps": [],
                    "stop_reason": "已拿到候选池。",
                }
            return {
                "scene": "theme_analysis",
                "answer_ready": False,
                "final_answer": "",
                "reasoning_summary": "显式主题分析应先建候选池。",
                "steps": [
                    {
                        "tool_call": {
                            "name": "opportunity_discovery",
                            "parameters": {"marketplace": "US", "limit": 10},
                        },
                        "goal": "发现机会",
                        "required": True,
                    },
                    {
                        "tool_call": {
                            "name": "resolve_candidates",
                            "parameters": {"product_query": "car vacuum", "marketplace": "US"},
                        },
                        "goal": "建立候选池",
                        "required": True,
                    },
                ],
                "stop_reason": "拿到候选池后继续分析。",
            }

        pipe._plan_agent_next_steps = plan_agent_next_steps
        pipe._synthesize_planner_executor_answer = lambda **kwargs: "car vacuum 主题分析结果"

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            executed_tools.append(tool_call["name"])
            return json.dumps({"success": True, "data": {"candidate_asins": ["B001"]}}, ensure_ascii=False)

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

    def test_theme_analysis_repairs_missing_product_query_for_resolve_candidates_step(self) -> None:
        pipe = self.make_pipeline()
        executed_calls = []

        def plan_agent_next_steps(**kwargs) -> dict:
            return {
                "scene": "theme_analysis",
                "answer_ready": False,
                "final_answer": "",
                "reasoning_summary": "先解析候选池。",
                "steps": [
                    {
                        "tool_call": {
                            "name": "resolve_candidates",
                            "parameters": {"marketplace": "US"},
                        },
                        "goal": "建立候选池",
                        "required": True,
                    }
                ],
                "stop_reason": "拿到候选池后再决定后续工具。",
            }

        pipe._plan_agent_next_steps = plan_agent_next_steps
        pipe._synthesize_planner_executor_answer = lambda **kwargs: "humidifier 候选池已成功解析。"

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            executed_calls.append(copy.deepcopy(tool_call))
            return json.dumps(
                {
                    "success": True,
                    "data": {"candidate_pool_id": "pool-1", "candidate_asins": ["B001"]},
                },
                ensure_ascii=False,
            )

        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[
                {
                    "role": "user",
                    "content": "/tool 请用原生工具帮我拆解 humidifier 在 Amazon 美国站的选品验证路径：先解析候选池，再说明应该继续看 stats、trends、benchmark、top ASIN 还是补池。",
                }
            ],
            body={},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="agent",
        )

        self.assertEqual(len(executed_calls), 1)
        self.assertEqual(executed_calls[0]["name"], "resolve_candidates")
        self.assertEqual(executed_calls[0]["parameters"]["product_query"], "humidifier")
        self.assertEqual(executed_calls[0]["parameters"]["marketplace"], "US")
        self.assertIn("humidifier 候选池已成功解析", answer)

    def test_explicit_resolve_candidates_tool_request_repairs_required_product_query(self) -> None:
        pipe = self.make_pipeline()
        executed_calls = []

        def plan_agent_next_steps(**kwargs) -> dict:
            observed = kwargs.get("tool_observations") or []
            if observed:
                return {
                    "scene": "general_agent",
                    "answer_ready": True,
                    "final_answer": "候选池解析完成。",
                    "reasoning_summary": "已有工具证据。",
                    "steps": [],
                    "stop_reason": "done",
                }
            return {
                "scene": "general_agent",
                "answer_ready": False,
                "final_answer": "",
                "reasoning_summary": "用户显式请求工具。",
                "steps": [
                    {
                        "tool_call": {"name": "resolve_candidates", "parameters": {"marketplace": "US"}},
                        "goal": "解析候选池",
                        "required": True,
                    }
                ],
                "stop_reason": "tool first",
            }

        pipe._plan_agent_next_steps = plan_agent_next_steps

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            executed_calls.append(copy.deepcopy(tool_call))
            return json.dumps({"success": True, "data": {"candidate_pool_id": "pool-1"}}, ensure_ascii=False)

        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[
                {
                    "role": "user",
                    "content": "/tool 请调用 resolve_candidates，解析 humidifier 在 Amazon 美国站的候选池，marketplace=US，recall_mode=keyword，max_candidates=8，并说明 pool_quality 是否足以继续分析。",
                }
            ],
            body={},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="tool",
        )

        self.assertEqual(answer, "候选池解析完成。")
        self.assertEqual(len(executed_calls), 1)
        self.assertEqual(executed_calls[0]["name"], "resolve_candidates")
        self.assertEqual(executed_calls[0]["parameters"]["product_query"], "humidifier")
        self.assertEqual(executed_calls[0]["parameters"]["recall_mode"], "keyword")
        self.assertEqual(executed_calls[0]["parameters"]["max_candidates"], 8)

    def test_explicit_category_resolve_request_bypasses_unrelated_planner_step(self) -> None:
        pipe = self.make_pipeline()
        executed_tools = []

        def plan_agent_next_steps(**kwargs) -> dict:
            observed = kwargs.get("tool_observations") or []
            if observed:
                return {
                    "scene": "general_agent",
                    "answer_ready": True,
                    "final_answer": "Humidifiers 类目解析完成。",
                    "reasoning_summary": "已有 category_resolve 证据。",
                    "steps": [],
                    "stop_reason": "done",
                }
            return {
                "scene": "general_agent",
                "answer_ready": False,
                "final_answer": "",
                "reasoning_summary": "planner 错误地先查知识库。",
                "steps": [
                    {
                        "tool_call": {"name": "search_knowledge_base", "parameters": {"query": "Humidifiers 类目"}},
                        "goal": "查知识库",
                        "required": True,
                    }
                ],
                "stop_reason": "tool first",
            }

        pipe._plan_agent_next_steps = plan_agent_next_steps

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            executed_tools.append(copy.deepcopy(tool_call))
            return json.dumps({"success": True, "data": {"category_id": 17685839011}}, ensure_ascii=False)

        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[
                {
                    "role": "user",
                    "content": "/tool 请调用 category_resolve，把 Humidifiers 解析成 Amazon/Keepa 美国站稳定类目 ID，并返回本地覆盖度。",
                }
            ],
            body={},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="tool",
        )

        self.assertEqual(answer, "Humidifiers 类目解析完成。")
        self.assertEqual([call["name"] for call in executed_tools], ["category_resolve"])
        self.assertEqual(executed_tools[0]["parameters"]["category_query"], "Humidifiers")
        self.assertEqual(executed_tools[0]["parameters"]["marketplace"], "US")

    def test_explicit_tool_request_overrides_planner_tool_unavailable_answer(self) -> None:
        pipe = self.make_pipeline()
        executed_tools = []

        def plan_agent_next_steps(**kwargs) -> dict:
            observed = kwargs.get("tool_observations") or []
            if observed:
                return {
                    "scene": "theme_analysis",
                    "answer_ready": True,
                    "final_answer": "Humidifiers 类目解析完成。",
                    "reasoning_summary": "已有 category_resolve 证据。",
                    "steps": [],
                    "stop_reason": "done",
                }
            return {
                "scene": "budget_analysis",
                "answer_ready": True,
                "final_answer": "category_resolve 不在当前可用的 allowed_tools 列表中。",
                "reasoning_summary": "planner 误判了工具目录。",
                "steps": [],
                "stop_reason": "tool unavailable",
            }

        pipe._plan_agent_next_steps = plan_agent_next_steps

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            executed_tools.append(copy.deepcopy(tool_call))
            return json.dumps({"success": True, "data": {"category_id": 17685839011}}, ensure_ascii=False)

        pipe._execute_tool_call = execute_tool_call
        answer = pipe._run_agent_loop(
            messages=[
                {
                    "role": "user",
                    "content": "/tool 请调用 category_resolve，把 Humidifiers 解析成 Amazon/Keepa 美国站稳定类目 ID，并返回本地覆盖度。",
                }
            ],
            body={},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="tool",
        )

        self.assertEqual(answer, "Humidifiers 类目解析完成。")
        self.assertEqual([call["name"] for call in executed_tools], ["category_resolve"])
        self.assertEqual(executed_tools[0]["parameters"]["category_query"], "Humidifiers")

    def test_react_action_protocol_executes_one_tool_per_round(self) -> None:
        pipe = self.make_pipeline()
        pipe._classify_agent_scene = lambda messages, mode="agent": "general_agent"
        observed_payloads = []
        executed_tools = []

        def post_agent_payload(payload: dict, model_name: str) -> dict:
            observed_payloads.append(copy.deepcopy(payload))
            if len(observed_payloads) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "scene": "general_agent",
                                        "reasoning_summary": "需要先查知识库获取证据。",
                                        "action": {
                                            "type": "tool",
                                            "tool": {
                                                "tool_name": "search_knowledge_base",
                                                "goal": "检索提示词知识",
                                                "parameters": {"query": "新手卖家提示词", "top_k": 3},
                                            },
                                            "final_answer": "",
                                        },
                                        "stop_reason": "拿到观察后再决定是否作答。",
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "scene": "general_agent",
                                    "reasoning_summary": "已有足够证据。",
                                    "action": {"type": "final", "final_answer": "新手提示词建议已整理。"},
                                    "stop_reason": "done",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            executed_tools.append(copy.deepcopy(tool_call))
            return json.dumps({"success": True, "documents": [{"title": "提示词指南"}]}, ensure_ascii=False)

        pipe._post_agent_payload = post_agent_payload
        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[{"role": "user", "content": "给新手卖家 5 条提示词"}],
            body={},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="agent",
        )

        self.assertEqual(answer, "新手提示词建议已整理。")
        self.assertEqual([call["name"] for call in executed_tools], ["search_knowledge_base"])
        self.assertEqual(executed_tools[0]["parameters"]["query"], "新手卖家提示词")

    def test_agent_harness_owns_tool_registry_and_scene_policy(self) -> None:
        pipe = self.make_pipeline()

        self.assertIs(xiamimate.TOOL_LAYER_REGISTRY, xiamimate.agent_harness.TOOL_LAYER_REGISTRY)
        self.assertIs(xiamimate.SCENE_TOOL_POLICY, xiamimate.agent_harness.SCENE_TOOL_POLICY)
        self.assertIs(xiamimate.ALLOWED_AGENT_TOOLS, xiamimate.agent_harness.ALLOWED_AGENT_TOOLS)
        self.assertIn("resolve_candidates", pipe.agent_harness.planner_allowed_tool_names("theme_analysis", "agent"))
        self.assertNotIn("web_search", pipe.agent_harness.planner_allowed_tool_names("foundation_qa", "tool"))

    def test_agent_trace_is_included_in_synthesis_context(self) -> None:
        pipe = self.make_pipeline()
        trace = pipe.agent_harness.new_trace(mode="tool", scene="theme_analysis")
        trace.record("planner_action", scene="theme_analysis", action_type="tool", tool_name="resolve_candidates")
        trace.record("observation", scene="theme_analysis", tool_name="resolve_candidates", status="ok")

        payload = pipe._prepare_planner_executor_synthesis_payload(
            messages=[{"role": "user", "content": "分析 humidifier"}],
            body={},
            model_name="deepseek-v4-pro",
            planner_notes=[{"scene": "theme_analysis", "planned_tools": ["resolve_candidates"]}],
            tool_observations=[
                {
                    "tool_name": "resolve_candidates",
                    "arguments": {"product_query": "humidifier"},
                    "llm_result": "candidate_pool_id=pool-1",
                }
            ],
            agent_trace=trace,
        )
        context = json.loads(payload["messages"][-1]["content"])

        self.assertEqual(context["trace"][0]["trace_id"], trace.trace_id)
        self.assertEqual(context["trace"][-1]["event_type"], "observation")
        self.assertEqual(context["tool_observations"][0]["tool_name"], "resolve_candidates")

    def test_agent_trace_jsonl_sink_writes_compact_record(self) -> None:
        pipe = self.make_pipeline()
        trace = pipe.agent_harness.new_trace(mode="tool", scene="theme_analysis")
        trace.record("planner_action", scene="theme_analysis", action_type="tool", tool_name="resolve_candidates")
        trace.record("observation", scene="theme_analysis", tool_name="resolve_candidates", status="ok")

        with tempfile.TemporaryDirectory() as tmp_dir:
            trace_path = Path(tmp_dir) / "agent-trace.jsonl"
            record = pipe.agent_harness.write_trace(trace, str(trace_path), extra={"tool_count": 1})
            lines = trace_path.read_text(encoding="utf-8").splitlines()

        self.assertIsNotNone(record)
        self.assertEqual(len(lines), 1)
        persisted = json.loads(lines[0])
        self.assertEqual(persisted["trace_id"], trace.trace_id)
        self.assertEqual(persisted["tool_count"], 1)
        self.assertEqual(persisted["event_count"], 2)
        self.assertEqual(persisted["events"][-1]["event_type"], "observation")

    def test_run_agent_loop_persists_trace_when_sink_enabled(self) -> None:
        pipe = self.make_pipeline()
        pipe._classify_agent_scene = lambda messages, mode="agent": "foundation_qa"
        pipe._post_agent_payload = lambda payload, model_name: {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "scene": "foundation_qa",
                                "reasoning_summary": "基础问题可直接回答。",
                                "action": {"type": "final", "final_answer": "新手可以先用三个问题开始选品。"},
                                "stop_reason": "done",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            trace_path = Path(tmp_dir) / "agent-trace.jsonl"
            pipe.valves.AGENT_TRACE_SINK_PATH = str(trace_path)
            answer = pipe._run_agent_loop(
                messages=[{"role": "user", "content": "新手怎么开始选品？"}],
                body={},
                billing_context={"api_key": "test"},
                model_name="deepseek-v4-pro",
                mode="agent",
                charge_llm=False,
            )
            persisted = json.loads(trace_path.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(answer, "新手可以先用三个问题开始选品。")
        self.assertEqual(persisted["status"], "finished")
        self.assertFalse(persisted["stream"])
        self.assertEqual(persisted["planner_note_count"], 1)
        self.assertEqual([event["event_type"] for event in persisted["events"]], ["intent", "planner_action", "final"])

    def test_react_runner_records_loop_events_and_observations(self) -> None:
        pipe = self.make_pipeline()
        runner = pipe.agent_harness.new_react_runner(mode="tool", scene="theme_analysis")
        plan = {
            "action_type": "tool",
            "reasoning_summary": "需要候选池证据。",
            "steps": [{"tool_call": {"name": "resolve_candidates", "parameters": {"product_query": "humidifier"}}}],
        }

        runner.start(max_rounds=2)
        note = runner.plan_note("theme_analysis", plan)
        runner.validation("theme_analysis", plan["steps"])
        runner.observation(
            "theme_analysis",
            "resolve_candidates",
            "ok",
            observation={"tool_name": "resolve_candidates", "arguments": {"product_query": "humidifier"}, "llm_result": "ok"},
            cache_key=("resolve_candidates", "{}"),
        )
        runner.final("theme_analysis", status="planner_final")

        self.assertEqual(note["planned_tools"], ["resolve_candidates"])
        self.assertEqual(runner.observation_store.names(), ["resolve_candidates"])
        self.assertIn(("resolve_candidates", "{}"), runner.observation_store.tool_result_cache)
        self.assertEqual([event["event_type"] for event in runner.events], ["intent", "planner_action", "validation", "observation", "final"])

    def test_harness_repairs_required_arguments_via_dependency_injection(self) -> None:
        pipe = self.make_pipeline()
        repaired = pipe.agent_harness.repair_tool_call_required_arguments(
            {"name": "resolve_candidates", "parameters": {"marketplace": "US"}},
            lambda tool_name, parameters: {"product_query": "humidifier"} if tool_name == "resolve_candidates" else {},
            pipe._normalize_tool_call,
        )

        self.assertIsNotNone(repaired)
        self.assertEqual(repaired["name"], "resolve_candidates")
        self.assertEqual(repaired["parameters"]["product_query"], "humidifier")
        self.assertEqual(repaired["parameters"]["marketplace"], "US")

    def test_budget_calculator_is_single_execution_in_budget_scene(self) -> None:
        pipe = self.make_pipeline()
        steps = [
            {
                "tool_call": {
                    "name": "launch_budget_calculator",
                    "parameters": {"product_theme": "humidifier", "marketplace": "US"},
                }
            }
        ]
        observations = [
            {
                "tool_name": "launch_budget_calculator",
                "arguments": {"product_theme": "humidifier", "marketplace": "US"},
                "llm_result": "预算已测算。",
            }
        ]

        filtered = pipe.agent_harness.filter_redundant_planner_steps(steps, "budget_analysis", observations)

        self.assertEqual(filtered, [])

    def test_legacy_planner_steps_are_limited_to_single_react_action(self) -> None:
        pipe = self.make_pipeline()
        pipe._classify_agent_scene = lambda messages, mode="agent": "general_agent"
        observed_payloads = []
        executed_tools = []

        def post_agent_payload(payload: dict, model_name: str) -> dict:
            observed_payloads.append(copy.deepcopy(payload))
            if len(observed_payloads) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "scene": "general_agent",
                                        "answer_ready": False,
                                        "final_answer": "",
                                        "reasoning_summary": "旧 planner 返回了多个步骤，但 harness 只能执行一个下一步动作。",
                                        "steps": [
                                            {"tool_name": "search_knowledge_base", "parameters": {"query": "提示词"}, "goal": "查知识库"},
                                            {"tool_name": "web_search", "parameters": {"query": "Amazon seller prompt"}, "goal": "联网补充"},
                                        ],
                                        "stop_reason": "legacy",
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "scene": "general_agent",
                                    "reasoning_summary": "已有第一步观察。",
                                    "action": {"type": "final", "final_answer": "基于第一步观察作答。"},
                                    "stop_reason": "done",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

        def execute_tool_call(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            executed_tools.append(tool_call["name"])
            return json.dumps({"success": True}, ensure_ascii=False)

        pipe._post_agent_payload = post_agent_payload
        pipe._execute_tool_call = execute_tool_call

        answer = pipe._run_agent_loop(
            messages=[{"role": "user", "content": "给新手卖家 5 条提示词"}],
            body={},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="agent",
        )

        self.assertEqual(answer, "基于第一步观察作答。")
        self.assertEqual(executed_tools, ["search_knowledge_base"])

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

    def test_opportunity_top5_request_preflights_discovery_limit(self) -> None:
        pipe = self.make_pipeline()
        messages = [{"role": "user", "content": "使用机会发现模块，输出top5的机会卡片，并对每个卡片的机会进行分析解说"}]
        steps = [
            {
                "tool_call": {
                    "name": "opportunity_discovery",
                    "parameters": {"marketplace": "US", "limit": 10},
                }
            }
        ]

        repaired = pipe._repair_planner_steps_required_arguments(steps, messages, {})

        self.assertEqual(repaired[0]["tool_call"]["parameters"]["limit"], 5)
        contract = pipe._answer_contract_from_messages(messages)
        self.assertEqual(contract["requested_count"], 5)
        self.assertEqual(contract["entity_type"], "opportunity_card")
        self.assertIn("每张卡片", " ".join(contract["must_include"]))

    def test_opportunity_discovery_preflight_defaults_marketplace(self) -> None:
        pipe = self.make_pipeline()
        messages = [{"role": "user", "content": "使用机会发现模块，输出top5的机会卡片"}]
        steps = [
            {
                "tool_call": {
                    "name": "opportunity_discovery",
                    "parameters": {"marketplace": "", "limit": 10},
                }
            }
        ]

        repaired = pipe._repair_planner_steps_required_arguments(steps, messages, {})

        self.assertEqual(repaired[0]["tool_call"]["parameters"]["marketplace"], "US")
        self.assertEqual(repaired[0]["tool_call"]["parameters"]["limit"], 5)

    def test_tool_preflight_clamps_top_asin_top_n_to_schema_limit(self) -> None:
        pipe = self.make_pipeline()
        messages = [{"role": "user", "content": "提供 SUNLU/Creality 在候选池中的具体 ASIN 列表"}]
        steps = [
            {
                "tool_call": {
                    "name": "top_asin_drilldown",
                    "parameters": {"candidate_pool_id": "11111111-1111-4111-8111-111111111111", "top_n": 30},
                }
            }
        ]

        repaired = pipe._repair_planner_steps_required_arguments(steps, messages, {})

        self.assertEqual(repaired[0]["tool_call"]["parameters"]["top_n"], 20)

    def test_stage_b_c_tools_are_registered_for_theme_analysis(self) -> None:
        pipe = self.make_pipeline()

        allowed_tools = set(pipe._planner_allowed_tool_names("theme_analysis"))

        self.assertIn("candidate_pool_slice", allowed_tools)
        # asin_review_insights / amazon_keyword_demand 已从 agent 可见 registry 中下线
        self.assertNotIn("asin_review_insights", allowed_tools)
        self.assertNotIn("amazon_keyword_demand", allowed_tools)

    def test_candidate_pool_slice_aliases_and_top_n_preflight(self) -> None:
        pipe = self.make_pipeline()
        messages = [{"role": "user", "content": "从候选池里看 SUNLU/Creality 的 top30 ASIN"}]
        steps = [
            {
                "tool_call": pipe._normalize_tool_call(
                    "candidate_pool_slice",
                    {
                        "pool_id": "11111111-1111-4111-8111-111111111111",
                        "brands": ["SUNLU", "Creality"],
                        "material": "PLA",
                        "min_price": 20,
                        "max_price": 50,
                        "top_n": 30,
                    },
                )
            }
        ]

        repaired = pipe._repair_planner_steps_required_arguments(steps, messages, {})
        params = repaired[0]["tool_call"]["parameters"]

        self.assertEqual(params["candidate_pool_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(params["brand_include"], ["SUNLU", "Creality"])
        self.assertEqual(params["material_keywords"], "PLA")
        self.assertEqual(params["price_min"], 20)
        self.assertEqual(params["price_max"], 50)
        self.assertEqual(params["top_n"], 20)

    def test_amazon_keyword_demand_tool_is_downlisted(self) -> None:
        pipe = self.make_pipeline()

        normalized = pipe._normalize_tool_call(
            "amazon_keyword_demand",
            {"keyword_list": "carbon fiber PLA, matte PLA", "market": "amazon.com"},
        )

        # 已从 agent 可见工具中下线；planner 不应再被允许调用，规范化返回为 None 表示拒绝。
        self.assertIsNone(normalized)

    def test_report_followup_questions_are_not_annotated_with_provider_notes(self) -> None:
        pipe = self.make_pipeline()
        answer = """# Amazon 美国 3D Printing Filament 深度选品分析报告

## 下一步可复制追问

3. **竞品下钻：** Creality / SUNLU / eSUN 三家的 1kg PLA 基础款评论结构（评分分布 + 关键词）是什么样的？

4. **差异化机会矩阵：** 碳纤维增强 PLA、哑光 PLA、丝绸 PLA 这三个细分材质在 Amazon 上的月搜索量分别是多少？头部 ASIN 的月销量区间和评论量分布？
"""

        rendered = pipe._prepare_workflow_answer(answer)

        # 两个工具已真实下线：报告尾部不再被注入能力边界/provider 提示，原文标题保持不变。
        self.assertIn("下一步可复制追问", rendered)
        self.assertNotIn("能力边界提示", rendered)
        self.assertNotIn("评论文本分析 provider", rendered)
        self.assertNotIn("关键词量 provider", rendered)
        self.assertNotIn("需评论文本 provider", rendered)
        self.assertNotIn("需关键词量 provider", rendered)

    def test_report_followup_direct_markers_are_left_untouched(self) -> None:
        pipe = self.make_pipeline()
        answer = """## 下一步验证问题

5. ✅ 评论质量分析：Top 5 ASIN 的 1-3 星差评集中在哪些痛点？
6. 可直接执行：Power Strips 在 Amazon 的月搜索量是多少？
"""

        rendered = pipe._prepare_workflow_answer(answer)

        # 不再改写为 provider_required 标记；原有标记保留。
        self.assertNotIn("需评论文本 provider：", rendered)
        self.assertNotIn("需关键词量 provider：", rendered)
        self.assertNotIn("provider_required", rendered)
        self.assertIn("评论质量分析", rendered)
        self.assertIn("Power Strips", rendered)

    def test_agent_grader_flags_opportunity_table_without_card_fields(self) -> None:
        pipe = self.make_pipeline()
        user_text = "使用机会发现模块，输出top5的机会卡片，并对每个卡片的机会进行分析解说"
        answer = """## 机会发现结果

市场: US | 平台: Amazon | 实际返回机会数: 5

| 排名 | 机会 | 机会得分 |
|---:|---|---:|
| 1 | Power Strips | 86.69 |
| 2 | Windshield Sunshades | 80.70 |
| 3 | Women's Pants | 80.64 |
| 4 | Men's Button-Down Shirts | 76.36 |
| 5 | Tumblers | 76.25 |
"""

        result = pipe.agent_harness.grade_answer(user_text=user_text, answer_text=answer)

        self.assertEqual(result["status"], "partial")
        self.assertIn("opportunity_card_analysis_fields", result["failures"])
        checks = {check["name"]: check for check in result["checks"]}
        self.assertTrue(checks["opportunity_requested_count"]["passed"])
        self.assertTrue(checks["opportunity_no_extra_items"]["passed"])

    def test_agent_grader_flags_provider_required_followup_as_direct(self) -> None:
        pipe = self.make_pipeline()
        answer = """## 下一步验证问题（已按当前工具能力标注）

5. 可直接执行：评论质量分析：Top 5 ASIN 的 1-3 星差评集中在哪些痛点？
"""

        result = pipe.agent_harness.grade_answer(answer_text=answer)

        self.assertEqual(result["status"], "fail")
        self.assertIn("review_provider_boundary", result["failures"])

    def test_agent_grader_accepts_review_provider_boundary(self) -> None:
        pipe = self.make_pipeline()
        answer = "当前缺 review_text_provider；asin_review_insights 返回 provider_required，不能生成真实评论关键词。"
        observations = [
            {
                "tool_name": "asin_review_insights",
                "raw_result": json.dumps({"success": True, "provider_required": True, "missing_capability": "review_text_provider"}),
            }
        ]

        result = pipe.agent_harness.grade_answer(answer_text=answer, tool_observations=observations)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["score"], 1.0)

    def test_agent_grader_does_not_treat_generic_que_as_boundary(self) -> None:
        pipe = self.make_pipeline()
        answer = "1. 评论关键词：✅可直接执行 — 销量缺口分析"
        observations = [
            {
                "tool_name": "asin_review_insights",
                "raw_result": json.dumps({"success": True, "provider_required": True, "missing_capability": "review_text_provider"}),
            }
        ]

        result = pipe.agent_harness.grade_answer(answer_text=answer, tool_observations=observations)

        self.assertEqual(result["status"], "fail")
        self.assertIn("review_provider_boundary", result["failures"])

    def test_tool_layer_registry_exposes_machine_readable_boundaries(self) -> None:
        registry = xiamimate.agent_harness.TOOL_LAYER_REGISTRY
        slice_meta = registry["candidate_pool_slice"]
        self.assertFalse(slice_meta["requires_provider"])
        self.assertIn("rating_distribution", slice_meta["provides"])
        # asin_review_insights / amazon_keyword_demand 已从 registry 中下线，不应再被读到。
        self.assertNotIn("asin_review_insights", registry)
        self.assertNotIn("amazon_keyword_demand", registry)

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

    def test_opportunity_contract_fallback_renders_analysis_cards(self) -> None:
        pipe = self.make_pipeline()
        user_text = "使用机会发现模块，输出top2的机会卡片，并对每个卡片的机会进行分析解说"
        raw_result = json.dumps(
            {
                "opportunity_count": 2,
                "opportunity_cards_text": "| 排名 | 机会主题 | 得分 |\n| --- | --- | --- |\n| #1 | Power Strips | 86.69 |\n| #2 | Tumblers | 76.25 |",
                "opportunities_for_llm": [
                    {
                        "rank": 1,
                        "title": "Power Strips",
                        "title_zh": "排插",
                        "category_id": 11,
                        "category_path": "Electronics > Power Strips",
                        "opportunity_score": 86.69,
                        "personalized_opportunity_score": 84.12,
                        "sales_window_sum": 930110.88,
                        "trend_momentum_display": "销量 +175.42% / 趋势 近期趋势缺失",
                        "candidate_count": 62,
                        "row_count": 611,
                        "confidence": "medium",
                        "metric_explanations": {
                            "opportunity_score": {
                                "components": {
                                    "demand_score": {"score": 92.3, "weight": 0.2, "weighted_points": 18.46},
                                    "trend_score": {"score": 88.0, "weight": 0.2, "weighted_points": 17.6},
                                    "competition_headroom_score": {"score": 70.0, "weight": 0.15, "weighted_points": 10.5},
                                    "price_fit_score": {"score": 68.0, "weight": 0.15, "weighted_points": 10.2},
                                }
                            }
                        },
                        "next_action": {"request": {"product_query": "Power Strips", "category_id": 11, "category_path": "Electronics > Power Strips"}},
                    },
                    {
                        "rank": 2,
                        "title": "Tumblers",
                        "title_zh": "真空保温杯",
                        "category_path": "Home & Kitchen > Tumblers",
                        "opportunity_score": 76.25,
                        "candidate_count": 32,
                        "row_count": 342,
                    },
                ],
            },
            ensure_ascii=False,
        )
        observation = pipe._build_tool_observation({"name": "opportunity_discovery", "parameters": {"marketplace": "US", "limit": 2}}, raw_result)

        answer = pipe._fallback_opportunity_answer_if_needed(
            "## 机会发现结果\n\n| 排名 | 机会 |\n|---:|---|\n| 1 | Power Strips |\n| 2 | Tumblers |",
            [observation],
            answer_contract=pipe.agent_harness.answer_contract_from_text(user_text),
        )

        self.assertIn("### 机会 1：排插（Power Strips）", answer)
        self.assertIn("### 机会 2：真空保温杯（Tumblers）", answer)
        self.assertIn("机会理由", answer)
        self.assertIn("关键证据", answer)
        self.assertIn("得分推导", answer)
        self.assertIn("需求 92.3×20%=18.46", answer)
        self.assertIn("趋势 88×20%=17.6", answer)
        self.assertIn("风险/证据边界", answer)
        self.assertIn("下一步验证", answer)
        self.assertIn("| 排名 | 机会主题 | 类目路径 | 机会得分 |", answer)
        self.assertIn("| 1 | 排插（Power Strips） |", answer)
        grade = pipe.agent_harness.grade_answer(user_text=user_text, answer_text=answer)
        self.assertEqual(grade["status"], "pass")
        self.assertNotIn("估算区间", answer)

    def test_build_tool_observation_enriches_opportunity_titles_via_llm(self) -> None:
        pipe = self.make_pipeline()
        pipe.valves.AGENT_TITLE_TRANSLATOR_MODEL = "deepseek-v4-pro"
        calls: List[List[str]] = []

        def fake_post_agent_payload(payload: dict, model_name: str) -> dict:
            user_text = payload["messages"][-1]["content"]
            calls.append([line[2:] for line in user_text.splitlines() if line.startswith("- ")])
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"translations": {"Power Strips": "排插", "Tumblers": "真空保温杯"}},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

        pipe._post_agent_payload = fake_post_agent_payload
        raw_result = json.dumps(
            {
                "opportunity_count": 2,
                "opportunities_for_llm": [
                    {"rank": 1, "title": "Power Strips", "category_path": "Electronics > Power Strips"},
                    {"rank": 2, "title": "Tumblers", "category_path": "Home & Kitchen > Tumblers"},
                ],
            },
            ensure_ascii=False,
        )
        observation = pipe._build_tool_observation(
            {"name": "opportunity_discovery", "parameters": {"marketplace": "US", "limit": 2}},
            raw_result,
        )
        self.assertEqual(observation.get("title_translations"), {"Power Strips": "排插", "Tumblers": "真空保温杯"})
        enriched_payload = json.loads(observation["raw_result"])
        titles_zh = [item.get("title_zh") for item in enriched_payload["opportunities_for_llm"]]
        self.assertEqual(titles_zh, ["排插", "真空保温杯"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ["Power Strips", "Tumblers"])

        # Second call with overlapping + new titles should only ask LLM for the uncached one.
        raw_result_2 = json.dumps(
            {
                "opportunity_count": 2,
                "opportunities_for_llm": [
                    {"rank": 1, "title": "Power Strips"},
                    {"rank": 2, "title": "Goggles"},
                ],
            },
            ensure_ascii=False,
        )

        def fake_post_agent_payload_2(payload: dict, model_name: str) -> dict:
            calls.append([line[2:] for line in payload["messages"][-1]["content"].splitlines() if line.startswith("- ")])
            return {"choices": [{"message": {"content": json.dumps({"translations": {"Goggles": "泳镜"}}, ensure_ascii=False)}}]}

        pipe._post_agent_payload = fake_post_agent_payload_2
        observation_2 = pipe._build_tool_observation(
            {"name": "opportunity_discovery", "parameters": {}},
            raw_result_2,
        )
        self.assertEqual(observation_2.get("title_translations"), {"Power Strips": "排插", "Goggles": "泳镜"})
        self.assertEqual(calls[1], ["Goggles"])

    def test_build_tool_observation_skips_translation_when_model_not_configured(self) -> None:
        pipe = self.make_pipeline()
        called = {"value": False}

        def fail_post(*args, **kwargs):
            called["value"] = True
            raise AssertionError("translator must not be invoked when model is unset")

        pipe._post_agent_payload = fail_post
        raw_result = json.dumps(
            {"opportunity_count": 1, "opportunities_for_llm": [{"rank": 1, "title": "Power Strips"}]},
            ensure_ascii=False,
        )
        observation = pipe._build_tool_observation(
            {"name": "opportunity_discovery", "parameters": {}}, raw_result
        )
        self.assertFalse(called["value"])
        self.assertNotIn("title_translations", observation)
        self.assertNotIn("title_zh", observation["raw_result"])

    def test_translate_opportunity_titles_retries_once_on_failure(self) -> None:
        pipe = self.make_pipeline()
        pipe.valves.AGENT_TITLE_TRANSLATOR_MODEL = "deepseek-v4-flash"
        attempts = {"count": 0}

        def flaky_post(payload: dict, model_name: str) -> dict:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("simulated read timeout")
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"translations": {"Power Strips": "排插"}}, ensure_ascii=False
                            )
                        }
                    }
                ]
            }

        pipe._post_agent_payload = flaky_post
        result = pipe._translate_opportunity_titles(["Power Strips"])
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(result, {"Power Strips": "排插"})
        self.assertEqual(pipe._opportunity_title_zh_cache.get("Power Strips"), "排插")

    def test_translate_opportunity_titles_does_not_cache_failure(self) -> None:
        pipe = self.make_pipeline()
        pipe.valves.AGENT_TITLE_TRANSLATOR_MODEL = "deepseek-v4-flash"
        attempts = {"count": 0}

        def always_fail(payload: dict, model_name: str) -> dict:
            attempts["count"] += 1
            raise RuntimeError("simulated read timeout")

        pipe._post_agent_payload = always_fail
        result = pipe._translate_opportunity_titles(["Power Strips"])
        self.assertEqual(result, {})
        self.assertEqual(attempts["count"], 2)
        self.assertNotIn("Power Strips", pipe._opportunity_title_zh_cache)

        def recover(payload: dict, model_name: str) -> dict:
            attempts["count"] += 1
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"translations": {"Power Strips": "排插"}}, ensure_ascii=False
                            )
                        }
                    }
                ]
            }

        pipe._post_agent_payload = recover
        result_2 = pipe._translate_opportunity_titles(["Power Strips"])
        self.assertEqual(result_2, {"Power Strips": "排插"})
        self.assertEqual(pipe._opportunity_title_zh_cache.get("Power Strips"), "排插")

    def test_report_query_resolves_opportunity_reference_from_markdown_table(self) -> None:
        pipe = self.make_pipeline()
        messages = [
            {
                "role": "assistant",
                "content": "| 排名 | 机会主题 | 类目路径 |\n| --- | --- | --- |\n| #6 | Power Strips | Electronics > Power |\n| #7 | Air Fresheners | Home & Kitchen > Home Fragrance |",
            },
            {"role": "user", "content": "/report quick 机会编号7，分析一下"},
        ]

        query = pipe._resolve_report_query_from_context("机会编号7，分析一下", messages)

        self.assertIsNotNone(query)
        self.assertIn("Air Fresheners", query)
        self.assertIn("Home & Kitchen > Home Fragrance", query)
        self.assertNotIn("机会编号7", query)

    def test_report_query_resolves_opportunity_reference_from_card_heading(self) -> None:
        pipe = self.make_pipeline()
        messages = [
            {
                "role": "assistant",
                "content": """下面是本次机会发现返回的机会卡片。

### 机会 1：Power Strips
- 机会理由：机会得分 86.69，排序靠前；细分类目为 Electronics > Power Strips
- 下一步验证：用 resolve_candidates 分析 `Power Strips`；category_id=172282；category_path=Electronics > Accessories & Supplies > Power Strips & Surge Protectors > Power Strips，再进入 candidate_pool_stats。

### 机会 2：Tumblers
- 机会理由：机会得分 76.25。
""",
            },
            {"role": "user", "content": "/report deep 请基于上一步机会发现中的机会编号 1，生成深度分析报告"},
        ]

        query = pipe._resolve_report_query_from_context("请基于上一步机会发现中的机会编号 1，生成深度分析报告", messages)

        self.assertIsNotNone(query)
        self.assertIn("Power Strips", query)
        self.assertIn("类目路径", query)
        self.assertIn("Power Strips & Surge Protectors", query)

    def test_report_query_prefers_opportunity_next_action_request(self) -> None:
        pipe = self.make_pipeline()
        payload = {
            "opportunities_for_llm": [
                {
                    "rank": 7,
                    "title": "Air Fresheners",
                    "category_path": "Fallback Category",
                    "next_action": {"request": {"query": "car air freshener", "category_path": "Automotive > Interior Accessories"}},
                }
            ]
        }
        messages = [
            {"role": "assistant", "content": "```json\n%s\n```" % json.dumps(payload, ensure_ascii=False)},
            {"role": "user", "content": "/report quick 机会编号7，分析一下"},
        ]

        query = pipe._resolve_report_query_from_context("机会编号7，分析一下", messages)

        self.assertIsNotNone(query)
        self.assertIn("car air freshener", query)
        self.assertIn("Automotive > Interior Accessories", query)
        self.assertNotIn("Fallback Category", query)

    def test_report_query_returns_none_for_unresolved_opportunity_reference(self) -> None:
        pipe = self.make_pipeline()

        query = pipe._resolve_report_query_from_context("机会编号7，分析一下", [{"role": "assistant", "content": "没有机会列表"}])

        self.assertIsNone(query)

    def test_report_query_resolves_short_bare_rank_for_standard_profile(self) -> None:
        pipe = self.make_pipeline()
        messages = [
            {
                "role": "assistant",
                "content": "| 排名 | 机会主题 | 类目路径 |\n| --- | --- | --- |\n| #7 | Air Fresheners | Home & Kitchen > Home Fragrance |",
            },
            {"role": "user", "content": "/report standard 7"},
        ]
        profile, normalized_query = pipe._parse_report_profile("standard 7")

        query = pipe._resolve_report_query_from_context(normalized_query, messages)

        self.assertEqual("standard", profile)
        self.assertIsNotNone(query)
        self.assertIn("Air Fresheners", query)
        self.assertIn("Home & Kitchen > Home Fragrance", query)

    def test_report_query_resolves_short_bare_rank_with_action_words(self) -> None:
        pipe = self.make_pipeline()
        messages = [
            {
                "role": "assistant",
                "content": "| 排名 | 机会主题 | 类目路径 |\n| --- | --- | --- |\n| #7 | Air Fresheners | Home & Kitchen > Home Fragrance |",
            }
        ]

        query = pipe._resolve_report_query_from_context("7 分析一下", messages)

        self.assertIsNotNone(query)
        self.assertIn("Air Fresheners", query)
        self.assertNotIn("补充要求", query)

    def test_report_query_does_not_treat_numeric_units_as_opportunity_rank(self) -> None:
        pipe = self.make_pipeline()
        messages = [
            {
                "role": "assistant",
                "content": "| 排名 | 机会主题 | 类目路径 |\n| --- | --- | --- |\n| #7 | Air Fresheners | Home & Kitchen > Home Fragrance |",
            }
        ]

        query = pipe._resolve_report_query_from_context("7天趋势", messages)

        self.assertEqual("7天趋势", query)

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
            {
                "product": "Women's Pants",
                "price": 22.94,
                "landed_cost": 8.3,
                "commission_rate": "15%",
                "coupon_rate": 5,
                "refund_rate": "0.08",
                "ad_budget": 600,
                "units": 300,
            },
        )

        self.assertIsNotNone(normalized)
        params = normalized["parameters"]
        self.assertEqual(params["product_theme"], "Women's Pants")
        self.assertEqual(params["selling_price"], 22.94)
        self.assertEqual(params["landed_cost_per_unit"], 8.3)
        self.assertAlmostEqual(params["referral_fee_rate"], 0.15)
        self.assertAlmostEqual(params["coupon_discount_rate"], 0.05)
        self.assertAlmostEqual(params["return_rate"], 0.08)
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


class SessionContextStoreTests(unittest.TestCase):
    def _store(self):
        return xiamimate.agent_harness.SessionContextStore(ttl_seconds=3600, max_sessions=3, max_tool_results=2)

    def test_update_and_get_roundtrip(self) -> None:
        store = self._store()
        store.update("chat-a", {"last_product_query": "humidifier", "last_marketplace": "US"})
        snapshot = store.get("chat-a")
        self.assertEqual(snapshot["last_product_query"], "humidifier")
        self.assertEqual(snapshot["last_marketplace"], "US")
        self.assertNotIn("_touched_at", snapshot)

    def test_get_empty_for_unknown_or_blank_chat_id(self) -> None:
        store = self._store()
        self.assertEqual(store.get(None), {})
        self.assertEqual(store.get(""), {})
        self.assertEqual(store.get("never-set"), {})

    def test_update_ignores_none_values_and_underscore_keys(self) -> None:
        store = self._store()
        store.update("c1", {"last_product_query": "humidifier", "_internal": "x", "last_marketplace": None})
        snapshot = store.get("c1")
        self.assertEqual(snapshot.get("last_product_query"), "humidifier")
        self.assertNotIn("last_marketplace", snapshot)
        self.assertNotIn("_internal", snapshot)

    def test_record_tool_result_truncates_and_evicts(self) -> None:
        store = xiamimate.agent_harness.SessionContextStore(max_tool_result_chars=10, max_tool_results=2)
        store.record_tool_result("c1", "resolve_candidates", "x" * 50, parameters={"product_query": "humidifier"})
        store.record_tool_result("c1", "candidate_pool_stats", "yy", parameters={"candidate_pool_id": "p1"})
        store.record_tool_result("c1", "candidate_pool_trends", "zz", parameters={"candidate_pool_id": "p1"})
        snapshot = store.get("c1")
        calls = snapshot["last_tool_calls"]
        tool_names = [entry["tool_name"] for entry in calls.values()]
        self.assertEqual(tool_names, ["candidate_pool_stats", "candidate_pool_trends"])
        first_entry = next(iter(calls.values()))
        self.assertTrue(first_entry["summary"])
        self.assertTrue(first_entry.get("params_fingerprint"))

    def test_record_tool_result_dedups_by_fingerprint(self) -> None:
        store = xiamimate.agent_harness.SessionContextStore(max_tool_results=4)
        # 同名工具不同参数 -> 视为不同条目
        store.record_tool_result("c1", "candidate_pool_trends", "win30", parameters={"candidate_pool_id": "p1", "window_days": 30})
        store.record_tool_result("c1", "candidate_pool_trends", "win90", parameters={"candidate_pool_id": "p1", "window_days": 90})
        # 同名工具同参数 -> 覆盖最新
        store.record_tool_result("c1", "candidate_pool_trends", "win30b", parameters={"candidate_pool_id": "p1", "window_days": 30})
        snapshot = store.get("c1")
        calls = snapshot["last_tool_calls"]
        self.assertEqual(len(calls), 2)

    def test_lru_evicts_oldest_session(self) -> None:
        store = xiamimate.agent_harness.SessionContextStore(max_sessions=2)
        store.update("a", {"last_product_query": "q1"})
        store.update("b", {"last_product_query": "q2"})
        store.update("c", {"last_product_query": "q3"})
        self.assertEqual(store.get("a"), {})
        self.assertEqual(store.get("b").get("last_product_query"), "q2")
        self.assertEqual(store.get("c").get("last_product_query"), "q3")

    def test_clear_removes_session(self) -> None:
        store = self._store()
        store.update("c1", {"last_product_query": "humidifier"})
        store.clear("c1")
        self.assertEqual(store.get("c1"), {})


class AgentHarnessSessionMemoryTests(unittest.TestCase):
    def _harness(self, chat_id: str = "chat-test"):
        store = xiamimate.agent_harness.SessionContextStore()
        return xiamimate.agent_harness.AgentHarness(chat_id=chat_id, session_store=store), store

    def test_after_tool_observation_extracts_candidate_pool_from_resolve_candidates(self) -> None:
        harness, store = self._harness()
        payload = json.dumps(
            {
                "success": True,
                "data": {
                    "candidate_pool_id": "pool-xyz",
                    "candidate_asins": ["B001ABCD12", "B002EFGH34", "B003IJKL56"],
                    "candidate_pool_size": 3,
                    "leaf_categories": [{"category_id": 999, "category_path": "Home > Humidifiers"}],
                },
            }
        )
        harness.after_tool_observation(
            tool_call={"name": "resolve_candidates", "parameters": {"product_query": "humidifier", "marketplace": "US"}},
            result=payload,
            compact_result=payload[:200],
        )
        snapshot = harness.session_snapshot()
        self.assertEqual(snapshot["last_product_query"], "humidifier")
        self.assertEqual(snapshot["last_marketplace"], "US")
        pool = snapshot["last_candidate_pool"]
        self.assertEqual(pool["pool_id"], "pool-xyz")
        self.assertEqual(pool["size"], 3)
        self.assertEqual(pool["asins_total"], 3)
        self.assertIn("B001ABCD12", pool["asins_preview"])
        recent_tools = [item["tool_name"] for item in snapshot["recent_tool_results"]]
        self.assertIn("resolve_candidates", recent_tools)

    def test_after_tool_observation_extracts_category_handle(self) -> None:
        harness, _ = self._harness()
        payload = json.dumps(
            {"success": True, "data": {"category_id": 12345, "category_path": "Home > Humidifiers"}}
        )
        harness.after_tool_observation(
            tool_call={"name": "category_resolve", "parameters": {"category_query": "humidifier", "marketplace": "US"}},
            result=payload,
            compact_result=payload,
        )
        snapshot = harness.session_snapshot()
        self.assertEqual(str(snapshot["last_category_id"]), "12345")
        self.assertEqual(snapshot["last_category_path"], "Home > Humidifiers")

    def test_repair_fills_candidate_pool_from_session_for_followup_tool(self) -> None:
        harness, _ = self._harness()
        harness.after_tool_observation(
            tool_call={"name": "resolve_candidates", "parameters": {"product_query": "humidifier", "marketplace": "US"}},
            result=json.dumps(
                {"success": True, "data": {"candidate_pool_id": "pool-1", "candidate_asins": ["B001ABCD12", "B002EFGH34"]}}
            ),
            compact_result="ok",
        )
        repaired = harness.repair_tool_call_required_arguments(
            {"name": "candidate_pool_stats", "parameters": {}},
            lambda tool_name, parameters: {},
            lambda tool_name, parameters: {"name": tool_name, "parameters": parameters},
        )
        self.assertIsNotNone(repaired)
        params = repaired["parameters"]
        self.assertEqual(params.get("candidate_pool_id"), "pool-1")
        self.assertEqual(params.get("product_query"), "humidifier")
        self.assertEqual(params.get("marketplace"), "US")
        self.assertIn("B001ABCD12", str(params.get("candidate_asins", "")))


class AgentMultiTurnSessionMemoryTests(unittest.TestCase):
    def make_pipeline(self) -> xiamimate.Pipeline:
        pipe = xiamimate.Pipeline()
        pipe.agent_tools = FakeAgentTools()
        pipe._charge_billing_event = lambda **kwargs: {"points_charged": 0}
        pipe._refund_billing_event = lambda **kwargs: None
        return pipe

    def test_second_turn_reuses_candidate_pool_via_session_memory(self) -> None:
        pipe = self.make_pipeline()
        pipe._classify_agent_scene = lambda messages, mode="agent": "theme_analysis"

        # Turn 1: resolve_candidates establishes the pool.
        executed_turn1: list[dict] = []

        def plan_turn1(**kwargs) -> dict:
            return {
                "scene": "theme_analysis",
                "answer_ready": False,
                "final_answer": "",
                "reasoning_summary": "解析候选池",
                "steps": [
                    {
                        "tool_call": {
                            "name": "resolve_candidates",
                            "parameters": {"product_query": "humidifier", "marketplace": "US"},
                        },
                        "goal": "建立候选池",
                        "required": True,
                    }
                ],
                "stop_reason": "等候选池",
            }

        def exec_turn1(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            executed_turn1.append(copy.deepcopy(tool_call))
            return json.dumps(
                {
                    "success": True,
                    "data": {
                        "candidate_pool_id": "pool-shared",
                        "candidate_asins": ["B001ABCD12", "B002EFGH34", "B003IJKL56"],
                        "candidate_pool_size": 3,
                    },
                },
                ensure_ascii=False,
            )

        pipe._plan_agent_next_steps = plan_turn1
        pipe._execute_tool_call = exec_turn1
        pipe._synthesize_planner_executor_answer = lambda **kwargs: "候选池已建立。"

        pipe._ensure_agent_harness({"chat_id": "chat-multi-1"})
        pipe._run_agent_loop(
            messages=[{"role": "user", "content": "/tool 解析 humidifier 候选池"}],
            body={"chat_id": "chat-multi-1"},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="agent",
        )

        snapshot = pipe._session_snapshot()
        self.assertEqual(snapshot["last_candidate_pool"]["pool_id"], "pool-shared")
        self.assertEqual(snapshot["last_product_query"], "humidifier")

        # Turn 2: same chat_id, planner asks for candidate_pool_stats with empty params.
        executed_turn2: list[dict] = []
        plan_turn2_calls: list[int] = []

        def plan_turn2(**kwargs) -> dict:
            plan_turn2_calls.append(1)
            observed = kwargs.get("tool_observations") or []
            if observed:
                return {
                    "scene": "theme_analysis",
                    "answer_ready": True,
                    "final_answer": "pool stats 已分析。",
                    "reasoning_summary": "已得到 stats",
                    "steps": [],
                    "stop_reason": "done",
                }
            return {
                "scene": "theme_analysis",
                "answer_ready": False,
                "final_answer": "",
                "reasoning_summary": "继续查看 pool stats",
                "steps": [
                    {
                        "tool_call": {
                            "name": "candidate_pool_stats",
                            "parameters": {},
                        },
                        "goal": "看 pool stats",
                        "required": True,
                    }
                ],
                "stop_reason": "看完作答",
            }

        def exec_turn2(tool_call: dict, billing_context: dict, truncate: bool = True) -> str:
            executed_turn2.append(copy.deepcopy(tool_call))
            return json.dumps({"success": True, "data": {"avg_price": 35.0}}, ensure_ascii=False)

        pipe._plan_agent_next_steps = plan_turn2
        pipe._execute_tool_call = exec_turn2
        pipe._synthesize_planner_executor_answer = lambda **kwargs: "pool stats 已分析。"
        pipe._ensure_agent_harness({"chat_id": "chat-multi-1"})

        answer = pipe._run_agent_loop(
            messages=[{"role": "user", "content": "继续跑 candidate_pool_stats"}],
            body={"chat_id": "chat-multi-1"},
            billing_context={"api_key": "test"},
            model_name="deepseek-v4-pro",
            mode="agent",
        )

        self.assertEqual(len(executed_turn2), 1)
        self.assertEqual(executed_turn2[0]["name"], "candidate_pool_stats")
        params = executed_turn2[0]["parameters"]
        self.assertEqual(params.get("candidate_pool_id"), "pool-shared")
        self.assertEqual(params.get("product_query"), "humidifier")
        self.assertIn("B001ABCD12", str(params.get("candidate_asins", "")))
        self.assertIn("pool stats", answer)

    def test_planner_payload_includes_session_memory_snapshot(self) -> None:
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-session-mem"})
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "resolve_candidates", "parameters": {"product_query": "humidifier", "marketplace": "US"}},
            result=json.dumps(
                {"success": True, "data": {"candidate_pool_id": "pool-A", "candidate_asins": ["B001ABCD12"]}}
            ),
            compact_result="ok",
        )

        class _StubProvider:
            def filter_payload(self, body: dict) -> dict:
                return {}

        pipe._get_provider = lambda model_name: _StubProvider()
        pipe._insert_agent_memory_profile_message = lambda messages, profile: None

        payload = pipe._prepare_agent_planner_payload(
            messages=[{"role": "user", "content": "继续"}],
            body={"chat_id": "chat-session-mem"},
            mode="agent",
            model_name="deepseek-v4-pro",
            scene="theme_analysis",
            tool_observations=[],
            remaining_rounds=3,
        )
        last_user = next((m for m in reversed(payload["messages"]) if m.get("role") == "user"), None)
        self.assertIsNotNone(last_user)
        content = last_user["content"]
        self.assertIn("session_memory", content)
        self.assertIn("pool-A", content)
        # already_observed_tools should include resolve_candidates because the session
        # already has a candidate pool, even though this request has zero observations.
        planner_payload = json.loads(content)
        self.assertIn("resolve_candidates", planner_payload.get("already_observed_tools", []))

    def test_third_turn_drops_redundant_resolve_candidates_from_planner_steps(self) -> None:
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-third-turn"})
        # Seed session with a pool from a prior turn.
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "resolve_candidates", "parameters": {"product_query": "humidifier", "marketplace": "US"}},
            result=json.dumps(
                {"success": True, "data": {"candidate_pool_id": "pool-T3", "candidate_asins": ["B001ABCD12"]}}
            ),
            compact_result="ok",
        )

        # Planner emits resolve_candidates first (wrong) followed by trends/top_asin (correct).
        steps = [
            {"tool_call": {"name": "resolve_candidates", "parameters": {"product_query": "humidifier", "marketplace": "US"}}},
            {"tool_call": {"name": "candidate_pool_trends", "parameters": {"candidate_pool_id": "pool-T3"}}},
            {"tool_call": {"name": "top_asin_drilldown", "parameters": {"candidate_pool_id": "pool-T3"}}},
        ]
        filtered = pipe.agent_harness.filter_redundant_planner_steps(steps, "theme_analysis", [])
        kept_names = [s["tool_call"]["name"] for s in filtered]
        self.assertNotIn("resolve_candidates", kept_names)
        self.assertIn("candidate_pool_trends", kept_names)
        self.assertIn("top_asin_drilldown", kept_names)

    def test_cross_turn_dedup_by_fingerprint_for_any_analysis_tool(self) -> None:
        """通用机制：任何工具只要 (tool_name + 参数) 跨轮重复，executor 都应跳过；参数变了则放行。"""
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-generic-dedup"})
        # Seed: 上一轮已经跑过 trends 和 top_asin_drilldown
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "candidate_pool_trends", "parameters": {"candidate_pool_id": "p1", "window_days": 30}},
            result=json.dumps({"success": True, "data": {"trend_phase": "flat"}}),
            compact_result="trends-flat",
        )
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "top_asin_drilldown", "parameters": {"candidate_pool_id": "p1", "top_n": 5}},
            result=json.dumps({"success": True, "data": {"top_asins": []}}),
            compact_result="top-asin-summary",
        )

        # 第四轮 planner 又把同参数的 trends + top_asin_drilldown 拍上来，并新增了 weak_forecast
        steps = [
            {"tool_call": {"name": "candidate_pool_trends", "parameters": {"candidate_pool_id": "p1", "window_days": 30}}},  # dup
            {"tool_call": {"name": "top_asin_drilldown", "parameters": {"candidate_pool_id": "p1", "top_n": 5}}},  # dup
            {"tool_call": {"name": "candidate_pool_trends", "parameters": {"candidate_pool_id": "p1", "window_days": 90}}},  # 参数不同，应保留
            {"tool_call": {"name": "candidate_pool_weak_forecast", "parameters": {"candidate_pool_id": "p1"}}},  # 全新
        ]
        filtered = pipe.agent_harness.filter_redundant_planner_steps(steps, "theme_analysis", [])
        kept = [(s["tool_call"]["name"], s["tool_call"]["parameters"].get("window_days")) for s in filtered]
        # 重复的 (trends, 30) 与 (top_asin_drilldown, top_n=5) 应被丢掉
        self.assertNotIn(("candidate_pool_trends", 30), kept)
        # 同名但 window_days=90 必须保留
        self.assertIn(("candidate_pool_trends", 90), kept)
        # 全新工具必须保留
        self.assertIn("candidate_pool_weak_forecast", [n for n, _ in kept])
        # top_asin_drilldown 同参数应被丢掉
        self.assertNotIn("top_asin_drilldown", [n for n, _ in kept])

    def test_status_poll_tool_is_not_cross_turn_deduped(self) -> None:
        """状态轮询型工具（candidate_expansion_status）即使跨轮同参也必须重新执行，
        否则已 completed 的扩池任务会被复述成上一轮的 queued 旧状态。"""
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-status-poll"})
        # 上一轮已查过同样入参的扩池状态（当时返回 queued）
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "candidate_expansion_status", "parameters": {"marketplace": "US"}},
            result=json.dumps({"success": True, "data": {"jobs": [{"status": "queued"}]}}),
            compact_result="status-queued",
        )

        # 本轮 planner 又拍上同参数的状态查询；必须保留以便真实重查
        steps = [
            {"tool_call": {"name": "candidate_expansion_status", "parameters": {"marketplace": "US"}}},
        ]
        filtered = pipe.agent_harness.filter_redundant_planner_steps(steps, "theme_analysis", [])
        kept_names = [s["tool_call"]["name"] for s in filtered]
        self.assertIn("candidate_expansion_status", kept_names)

    def test_status_poll_tool_still_deduped_within_same_request(self) -> None:
        """同一请求内重复的同参状态查询仍应去重，避免一轮里查两次。"""
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-status-poll-intra"})
        steps = [
            {"tool_call": {"name": "candidate_expansion_status", "parameters": {"marketplace": "US"}}},
            {"tool_call": {"name": "candidate_expansion_status", "parameters": {"marketplace": "US"}}},
        ]
        filtered = pipe.agent_harness.filter_redundant_planner_steps(steps, "theme_analysis", [])
        kept_names = [s["tool_call"]["name"] for s in filtered]
        self.assertEqual(kept_names, ["candidate_expansion_status"])

    def test_failed_tool_result_is_not_cached_for_dedup(self) -> None:
        """失败/错误结果不得进入跨轮去重缓存：否则下一轮同参会被去重拦截、复述失败旧结果。
        本轮 planner 再次拍上同名同参工具时必须放行重试。"""
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-failed-cache"})
        params = {"candidate_pool_id": "p1", "window_days": 30}
        # 上一轮该工具返回结构化错误（非中文前缀报错），不应被记入去重缓存
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "candidate_pool_trends", "parameters": params},
            result=json.dumps({"status": "error", "error": "upstream timeout"}),
            compact_result=json.dumps({"status": "error", "error": "upstream timeout"}),
        )
        snapshot = pipe.agent_harness.session_snapshot() or {}
        recent = snapshot.get("recent_tool_calls") or []
        self.assertEqual(recent, [], "失败结果不应写入 recent_tool_calls")

        # 本轮 planner 重新拍上同参 trends，必须保留以便重试
        steps = [{"tool_call": {"name": "candidate_pool_trends", "parameters": params}}]
        filtered = pipe.agent_harness.filter_redundant_planner_steps(steps, "theme_analysis", [])
        kept_names = [s["tool_call"]["name"] for s in filtered]
        self.assertIn("candidate_pool_trends", kept_names)

    def test_prerequisite_resolve_candidates_runs_again_when_theme_changes(self) -> None:
        """上一轮已为某主题建池后，下一轮换了全新商品主题时，resolve_candidates 不能被
        prerequisite 句柄兜底静默删空；必须放行重新解析，否则用户拿到"工具执行结果为空"。"""
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-theme-switch"})
        # 上一轮：挂脖风扇建池成功
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "resolve_candidates", "parameters": {"product_query": "挂脖风扇", "marketplace": "US"}},
            result=json.dumps(
                {
                    "success": True,
                    "data": {
                        "candidate_pool_id": "pool-neckfan",
                        "candidate_asins": ["B0GSHZN1C3", "B0B1HQVNL4", "B0GLGCCYV4"],
                        "candidate_pool_size": 3,
                    },
                },
                ensure_ascii=False,
            ),
            compact_result="pool-neckfan",
        )
        snapshot = pipe.agent_harness.session_snapshot() or {}
        self.assertEqual(snapshot["last_candidate_pool"]["pool_id"], "pool-neckfan")

        # 下一轮：换成"加湿器"，planner 规划 resolve_candidates(product_query=加湿器)
        steps = [{"tool_call": {"name": "resolve_candidates", "parameters": {"product_query": "加湿器", "marketplace": "US"}}}]
        filtered = pipe.agent_harness.filter_redundant_planner_steps(steps, "theme_analysis", [])
        self.assertIn(
            "resolve_candidates",
            [s["tool_call"]["name"] for s in filtered],
            "换了新商品主题时 resolve_candidates 必须放行重跑",
        )

    def test_prerequisite_resolve_candidates_still_skipped_for_same_theme(self) -> None:
        """同一商品主题、候选池已在 session 时，仍按原有语义跳过重复 resolve_candidates。"""
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-theme-same"})
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "resolve_candidates", "parameters": {"product_query": "humidifier", "marketplace": "US"}},
            result=json.dumps(
                {
                    "success": True,
                    "data": {
                        "candidate_pool_id": "pool-humidifier",
                        "candidate_asins": ["B001ABCD12", "B002EFGH34"],
                        "candidate_pool_size": 2,
                    },
                },
                ensure_ascii=False,
            ),
            compact_result="pool-humidifier",
        )
        # 同主题（大小写/空白不同也算同主题）
        steps = [{"tool_call": {"name": "resolve_candidates", "parameters": {"product_query": " Humidifier ", "marketplace": "US"}}}]
        filtered = pipe.agent_harness.filter_redundant_planner_steps(steps, "theme_analysis", [])
        self.assertNotIn(
            "resolve_candidates",
            [s["tool_call"]["name"] for s in filtered],
            "同主题候选池已存在时仍应复用、跳过重复解析",
        )

    def test_bare_theme_query_infers_product_query(self) -> None:
        """裸查询（如"挂脖风扇top3的asin"）必须能推断出 product_query，否则
        planner 偶发漏填 product_query 时，resolve_candidates 会在 enforce 阶段被删空。"""
        pipe = self.make_pipeline()
        cases = [
            ("挂脖风扇top3的asin", "挂脖风扇"),
            ("加湿器top3的asin", "加湿器"),
            ("车载吸尘器销量怎么样", "车载吸尘器"),
            ("请给我加湿器的top3 asin", "加湿器"),
            ("neck fan top3 asin", "neck fan"),
        ]
        for text, expected in cases:
            messages = [{"role": "user", "content": text}]
            inferred = pipe._infer_theme_product_query(messages)
            self.assertEqual(inferred, expected, f"裸查询 {text!r} 应推断出 {expected!r}")

    def test_bare_theme_query_overrides_stale_session_product_query(self) -> None:
        """换主题的裸查询应优先于 session 里上一轮的旧 product_query，
        避免 enforce/repair 阶段把新主题 resolve_candidates 误填成旧主题。"""
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-bare-switch"})
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "resolve_candidates", "parameters": {"product_query": "挂脖风扇", "marketplace": "US"}},
            result=json.dumps(
                {"success": True, "data": {"candidate_pool_id": "pool-neckfan", "candidate_asins": ["B0GSHZN1C3"]}},
                ensure_ascii=False,
            ),
            compact_result="pool-neckfan",
        )
        messages = [{"role": "user", "content": "加湿器top3的asin"}]
        self.assertEqual(pipe._infer_theme_product_query(messages), "加湿器")

    def test_stale_cross_turn_signature_is_refreshed_after_freshness_window(self) -> None:
        """超出新鲜窗口的旧签名应放行重跑，避免长对话复述过期结果；窗口内仍去重。"""
        from xiamimate import agent_harness

        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-stale-refresh"})
        params = {"candidate_pool_id": "p1", "window_days": 30}
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "candidate_pool_trends", "parameters": params},
            result=json.dumps({"success": True, "data": {"trend_phase": "flat"}}),
            compact_result="trends-flat",
        )
        steps = [{"tool_call": {"name": "candidate_pool_trends", "parameters": params}}]

        # 窗口内：仍应去重
        fresh = pipe.agent_harness.filter_redundant_planner_steps(steps, "theme_analysis", [])
        self.assertNotIn("candidate_pool_trends", [s["tool_call"]["name"] for s in fresh])

        # 把记录时间手动回拨到新鲜窗口之外，模拟长对话过期
        store = pipe.agent_harness.session_store
        entry = store._sessions.get("chat-stale-refresh")
        for call in (entry.get("last_tool_calls") or {}).values():
            call["recorded_at"] = call["recorded_at"] - agent_harness.CROSS_TURN_DEDUP_FRESHNESS_SECONDS - 60

        stale = pipe.agent_harness.filter_redundant_planner_steps(steps, "theme_analysis", [])
        self.assertIn("candidate_pool_trends", [s["tool_call"]["name"] for s in stale])

    def test_force_refresh_bypasses_cross_turn_dedup_and_strips_flag(self) -> None:
        """planner 在 parameters 里加 force_refresh=true 时，即使跨轮同签名也应放行重跑，
        且该控制标记必须从入参中剥离，不能传给真实工具。"""
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-force-refresh"})
        params = {"candidate_pool_id": "p1"}
        # 上一轮已缓存 stats（补池前的旧统计）
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "candidate_pool_stats", "parameters": params},
            result=json.dumps({"success": True, "data": {"pool_size": 12}}),
            compact_result="stats-12",
        )

        # 不加 force_refresh：窗口内同签名被去重
        deduped = pipe.agent_harness.filter_redundant_planner_steps(
            [{"tool_call": {"name": "candidate_pool_stats", "parameters": dict(params)}}],
            "theme_analysis",
            [],
        )
        self.assertNotIn("candidate_pool_stats", [s["tool_call"]["name"] for s in deduped])

        # 加 force_refresh=true：放行重跑，且标记被剥离
        step = {"tool_call": {"name": "candidate_pool_stats", "parameters": {**params, "force_refresh": True}}}
        kept = pipe.agent_harness.filter_redundant_planner_steps([step], "theme_analysis", [])
        kept_names = [s["tool_call"]["name"] for s in kept]
        self.assertIn("candidate_pool_stats", kept_names)
        self.assertNotIn("force_refresh", kept[0]["tool_call"]["parameters"])
        self.assertEqual(kept[0]["tool_call"]["parameters"], params)

    def test_force_refresh_accepts_string_truthy_values(self) -> None:
        """force_refresh 兼容字符串 'true' 等真值写法。"""
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-force-refresh-str"})
        params = {"candidate_pool_id": "p1"}
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "candidate_pool_stats", "parameters": params},
            result=json.dumps({"success": True, "data": {"pool_size": 12}}),
            compact_result="stats-12",
        )
        step = {"tool_call": {"name": "candidate_pool_stats", "parameters": {**params, "force_refresh": "true"}}}
        kept = pipe.agent_harness.filter_redundant_planner_steps([step], "theme_analysis", [])
        self.assertIn("candidate_pool_stats", [s["tool_call"]["name"] for s in kept])
        self.assertNotIn("force_refresh", kept[0]["tool_call"]["parameters"])

    def test_force_refresh_still_deduped_within_same_request(self) -> None:
        """同一请求内重复的 force_refresh 同签名仍应去重，避免一轮里刷新两次。"""
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-force-refresh-intra"})
        steps = [
            {"tool_call": {"name": "candidate_pool_stats", "parameters": {"candidate_pool_id": "p1", "force_refresh": True}}},
            {"tool_call": {"name": "candidate_pool_stats", "parameters": {"candidate_pool_id": "p1", "force_refresh": True}}},
        ]
        kept = pipe.agent_harness.filter_redundant_planner_steps(steps, "theme_analysis", [])
        self.assertEqual([s["tool_call"]["name"] for s in kept], ["candidate_pool_stats"])

    def test_session_snapshot_labels_cached_results_with_age(self) -> None:
        """session_memory.recent_tool_calls 应标注 cached=true 与 cached_age_seconds，供 planner 判断是否刷新。"""
        pipe = self.make_pipeline()
        pipe._ensure_agent_harness({"chat_id": "chat-cache-label"})
        pipe.agent_harness.after_tool_observation(
            tool_call={"name": "candidate_pool_stats", "parameters": {"candidate_pool_id": "p1"}},
            result=json.dumps({"success": True, "data": {"pool_size": 12}}),
            compact_result="stats-12",
        )
        snapshot = pipe.agent_harness.session_snapshot() or {}
        recent = snapshot.get("recent_tool_calls") or []
        self.assertTrue(recent)
        self.assertTrue(recent[0].get("cached"))
        self.assertIsInstance(recent[0].get("cached_age_seconds"), int)


    """覆盖两件事：
    1. asin_review_insights / amazon_keyword_demand 已从 agent 可见 registry 和 scene policy 中下线。
    2. 当 planner LLM 把 planner 调度 JSON 当成文本返回时，harness 不会把这段 JSON 当 final_answer 吐给用户。
    """

    def _make_pipeline(self) -> xiamimate.Pipeline:
        pipe = xiamimate.Pipeline()
        pipe.agent_tools = FakeAgentTools()
        pipe._charge_billing_event = lambda **kwargs: {"points_charged": 0}
        pipe._refund_billing_event = lambda **kwargs: None
        return pipe

    def test_review_insights_and_keyword_demand_removed_from_agent_registry(self) -> None:
        from xiamimate import agent_harness

        self.assertNotIn("asin_review_insights", agent_harness.TOOL_LAYER_REGISTRY)
        self.assertNotIn("amazon_keyword_demand", agent_harness.TOOL_LAYER_REGISTRY)
        self.assertNotIn("asin_review_insights", agent_harness.ALLOWED_AGENT_TOOLS)
        self.assertNotIn("amazon_keyword_demand", agent_harness.ALLOWED_AGENT_TOOLS)
        self.assertNotIn("asin_review_insights", agent_harness.TOOL_REQUIRED_ARGUMENTS)
        self.assertNotIn("amazon_keyword_demand", agent_harness.TOOL_REQUIRED_ARGUMENTS)
        self.assertNotIn("asin_review_insights", agent_harness.TOOL_NUMERIC_LIMITS)

        # 各 scene 的 single_execution_tools 也不再包含这两个工具
        registry = agent_harness.ToolRegistry()
        for scene in agent_harness.SCENE_TOOL_POLICY:
            single = registry.single_execution_tools(scene)
            self.assertNotIn("asin_review_insights", single, scene)
            self.assertNotIn("amazon_keyword_demand", single, scene)

    def test_planner_json_leak_is_quarantined_in_plan_step(self) -> None:
        pipe = self._make_pipeline()
        leaked_planner_json = json.dumps(
            {
                "scene": "theme_analysis",
                "reasoning_summary": "已经拿到 stats，下一步切片",
                "action": {
                    "type": "tool",
                    "tool": {
                        "tool_name": "candidate_pool_slice",
                        "goal": "按价格段切片",
                        "parameters": {"candidate_pool_id": "pool-1", "top_n": 5},
                    },
                    "stop_reason": "切片完成后再判断是否继续",
                },
            },
            ensure_ascii=False,
        )
        # 把 _extract_assistant_content 输出的 content 假装"已经无法解析为 JSON"——
        # 用 _extract_json_value_from_text 返回 None 的替身来模拟解析失败
        pipe._extract_json_value_from_text = lambda value: None
        pipe._post_agent_payload = lambda payload, model_name: {
            "choices": [{"message": {"role": "assistant", "content": leaked_planner_json}}]
        }
        plan = pipe._plan_agent_next_steps(
            messages=[{"role": "user", "content": "继续切片"}],
            body={},
            model_name="minimax-m2",
            mode="agent",
            scene="theme_analysis",
            tool_observations=[{"tool_name": "candidate_pool_stats", "result": "{}"}],
            remaining_rounds=2,
        )
        self.assertFalse(plan["answer_ready"], plan)
        self.assertEqual(plan["action_type"], "none")
        self.assertEqual(plan["final_answer"], "")
        self.assertEqual(plan["steps"], [])
        self.assertEqual(plan.get("stop_reason"), "planner_json_leak_guard")

    def test_planner_json_inside_final_answer_field_is_also_quarantined(self) -> None:
        pipe = self._make_pipeline()
        # 模型把整段 planner JSON 塞进 action.final_answer 字符串里
        nested_leak = json.dumps(
            {
                "scene": "theme_analysis",
                "action": {
                    "type": "final",
                    "final_answer": json.dumps(
                        {
                            "scene": "theme_analysis",
                            "reasoning_summary": "...",
                            "action": {
                                "type": "tool",
                                "tool": {"tool_name": "candidate_pool_slice", "parameters": {}},
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            },
            ensure_ascii=False,
        )
        pipe._post_agent_payload = lambda payload, model_name: {
            "choices": [{"message": {"role": "assistant", "content": nested_leak}}]
        }
        plan = pipe._plan_agent_next_steps(
            messages=[{"role": "user", "content": "继续切片"}],
            body={},
            model_name="minimax-m2",
            mode="agent",
            scene="theme_analysis",
            tool_observations=[{"tool_name": "candidate_pool_stats", "result": "{}"}],
            remaining_rounds=2,
        )
        self.assertFalse(plan["answer_ready"], plan)
        self.assertEqual(plan["action_type"], "none")
        self.assertEqual(plan["final_answer"], "")
        self.assertEqual(plan.get("stop_reason"), "planner_json_leak_guard")

    def test_synthesize_planner_executor_answer_strips_planner_json_leak(self) -> None:
        pipe = self._make_pipeline()
        leaked = json.dumps(
            {
                "scene": "theme_analysis",
                "reasoning_summary": "fallback should kick in",
                "action": {"type": "tool", "tool": {"tool_name": "candidate_pool_slice", "parameters": {}}},
            },
            ensure_ascii=False,
        )
        pipe._post_agent_payload = lambda payload, model_name: {
            "choices": [{"message": {"role": "assistant", "content": leaked}}]
        }
        # 当 _post_agent_payload 返回 planner JSON 时，合成器不能把它当成最终答案；
        # 应该走 _fallback_answer_from_tool_observations 兜底。
        observations = [
            {
                "tool_name": "candidate_pool_stats",
                "result": json.dumps({"success": True, "data": {"pool_size": 12}}, ensure_ascii=False),
            }
        ]
        answer = pipe._synthesize_planner_executor_answer(
            messages=[{"role": "user", "content": "继续切片"}],
            body={},
            model_name="minimax-m2",
            planner_notes=[],
            tool_observations=observations,
        )
        self.assertNotIn('"scene"', answer)
        self.assertNotIn('"action"', answer)
        self.assertNotIn('"tool_name"', answer)

    def test_looks_like_planner_json_recognizes_typical_envelopes(self) -> None:
        pipe = self._make_pipeline()
        positives = [
            '{"scene":"theme_analysis","reasoning_summary":"x","action":{"type":"tool","tool":{"tool_name":"candidate_pool_slice","parameters":{}}}}',
            '{"action":{"type":"final","final_answer":"x"}}',
            '{"answer_ready":true,"final_answer":"x","steps":[],"action_type":"final"}',
            '```json\n{"scene":"theme_analysis","action":{"type":"tool","tool":{"tool_name":"x","parameters":{}}}}\n```',
        ]
        for sample in positives:
            self.assertTrue(pipe._looks_like_planner_json(sample), sample[:60])
        negatives = [
            "## 结论\n候选池规模 12，建议聚焦头部品牌。",
            "这是一段普通中文回答，无 JSON 结构。",
            "{ this is not json but starts with brace }",
            "",
        ]
        for sample in negatives:
            self.assertFalse(pipe._looks_like_planner_json(sample), sample[:60])


if __name__ == "__main__":
    unittest.main()