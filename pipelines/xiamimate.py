"""
title: XiaMimate Bridge Manifold
author: GitHub Copilot
date: 2026-04-14
version: 0.2.0
description: Open WebUI manifold that exposes the single XiaMimate agent model with /report and /workflow routing.
requirements: requests
"""

import ast
import importlib.util
import json
import os
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from inspect import signature
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

import requests
from pydantic import BaseModel

# Load providers module directly by file path to avoid __init__.py package
# shadowing the xiamimate.py pipeline file in the pipelines loader.
def _load_providers():
    _spec = importlib.util.spec_from_file_location(
        "xiamimate_providers",
        str(Path(__file__).resolve().parent / "xiamimate" / "providers.py"),
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    return _mod

_providers_mod = _load_providers()
ProviderStrategy = _providers_mod.ProviderStrategy
get_provider = _providers_mod.get_provider


AGENT_SYSTEM_PROMPT = """你是 XiaMimate 商品主题分析 Agent。

工作原则：
1. 需要数据时优先调用已挂载的工具，不要凭空编造指标。
2. 需要平台规则、运营方法、合规要求等知识时，先调用 search_knowledge_base 工具检索知识库，不要依赖自身训练数据。
3. 需要最新外部动态、站外情报、近期政策变化或实时市场讨论时，调用 web_search 工具，不要把旧知识当成最新事实。
3. 需要商品数据时，先调用 resolve_candidates 拿到 candidate_asins；后续是否调用 candidate_pool_stats / candidate_pool_trends / candidate_pool_weak_forecast / top_asin_drilldown / asin_history_timeseries / category_benchmark，取决于用户是否需要候选池统计、趋势、销量/评论时序或详情下钻。
4. 当你已经有明确 ASIN，且需要看近 7 到 90 天的销量、价格、BSR、评论变化、L3/leaf 类目或类目路径时，必须优先调用 asin_history_timeseries；它会返回 latest_snapshot.category_path / l3_category_name / leaf_category_name 以及 window_summary.review_growth_window。
5. keepa_asin_lookup 只用于本地历史没有命中、需要实时商品快照兜底、或明确要求直连 Keepa 的场景；它不能替代 30 天评论增长、历史窗口和本地类目路径分析。
6. 如果工具尚未返回数据，只能给出分析框架、验证路径和风险提醒，明确标注为待验证。
7. 输出尽量围绕结论、证据、风险、下一步动作。
8. 每个结论标注数据来源类型：知识库 / 推理 / 工具数据。
9. 涉及类目归属、竞品筛选、是否排除某 ASIN 时，必须基于工具结果中的事实字段判断，优先引用 latest_snapshot.leaf_category_name / latest_snapshot.category_path，其次引用 l3_category_name；不要仅凭标题、品牌或自身知识补全类目。
10. 当用户要求“清洗/筛选/过滤上一步候选池”，且上一步 resolve_candidates 已返回 ASIN、品牌、product_title、leaf_category_name、fine_category_name、category_path、match_score、match_reasons 等字段时，直接基于这些字段筛选；不要为了判断标题或类目路径是否包含某词而调用 top_asin_drilldown。只有用户明确要求补充销量、价格、BSR、评论、预测等候选池没有的字段，才调用下游详情工具。

工具调用规则：
- 当你决定调用工具时，直接输出工具调用指令，不要在工具调用之前添加任何文字（如"好的，我来帮你…"等）。
- 等工具返回结果后，再给出分析和回答。
- 如果需要同时调用多个工具，可以连续输出多个工具调用。
- 后续工具如果依赖上一步输出参数，不要在同一轮猜测这些参数；先等待上一步工具结果。
- 工具调用控制字段（如 $TOOL_CALLS、$ABORT_CONTROLLER、<tools>、<tool_call>）永远不能作为最终答复展示给用户。
- 不要声称“未展开的 ASIN 可能包含更多核心商品”。如果工具结果只展示了部分候选字段，只能说“当前可见字段不足”；如果已有 candidate_items，则按已有 candidate_items 排序和筛选。

可用工具概览：
- search_knowledge_base: 检索跨境电商知识库（平台规则、运营指南、市场洞察）
- web_search: 联网搜索最新外部信息并返回总结（平台动态、行业新闻、竞争情报、消费者趋势）
- resolve_candidates: 解析候选 ASIN 池
- candidate_pool_stats: 候选池描述统计
- candidate_pool_trends: 候选池趋势诊断
- candidate_pool_weak_forecast: 弱信号预测标记
- top_asin_drilldown: 头部 ASIN 下钻
- asin_history_timeseries: 指定 ASIN 的历史时序
- category_benchmark: 类目基准对比
- keepa_asin_lookup: 直连 Keepa API 查询 ASIN 商品详情（当本地数据库没有相关 ASIN 时使用）
"""

TOOL_ONLY_SYSTEM_PROMPT = AGENT_SYSTEM_PROMPT + """

/tool 模式附加规则：
- 只允许依赖当前已挂载工具完成分析，不要联网搜索，不要假设存在外部网页结果。
- 如果工具还未返回证据，就明确写成“待工具验证”，不要把推测写成事实。
- 优先给出下一步所需的最小工具调用，再基于工具结果汇总结论。
"""

TOOL_RESULT_TEMPLATE = """以下是工具执行结果，请基于这些结果继续回答用户原问题。

工具名: {tool_name}
参数: {arguments}
结果:
{result}

如果信息已经足够，请直接给出最终答案。
如果仍需继续调用工具，只调用真正必要的工具，不要重复调用相同工具。"""

ALLOWED_AGENT_TOOLS = {
    "search_knowledge_base",
    "web_search",
    "resolve_candidates",
    "candidate_pool_stats",
    "candidate_pool_trends",
    "candidate_pool_weak_forecast",
    "top_asin_drilldown",
    "asin_history_timeseries",
    "category_benchmark",
    "keepa_asin_lookup",
}

COMMAND_TO_MODE = {
    "/agent": "agent",
    "/report": "report",
    "/web": "web",
    "/wf": "workflow",
    "/tool": "tool",
    "/workflow": "workflow",
}

ACCOUNT_COMMANDS = {
    "/me": "overview",
    "/points": "points",
    "/balance": "points",
    "/usage": "usage",
    "/plan": "plan",
}

USER_ID_HEADER_NAME = "X-User-Id"
USER_EMAIL_HEADER_NAME = "X-User-Email"
USER_NAME_HEADER_NAME = "X-User-Name"
INTERNAL_SERVICE_SECRET_HEADER_NAME = "X-Internal-Service-Secret"
INTERNAL_SERVICE_NAME_HEADER_NAME = "X-Internal-Service-Name"
IDEMPOTENCY_KEY_HEADER_NAME = "Idempotency-Key"

POINT_COST_BY_EVENT = {
    "llm_request": 1,
    "workflow_run": 8,
    "report_quick_run": 8,
    "report_standard_run": 16,
    "report_deep_run": 24,
    "report_research_run": 32,
    "kb_retrieve": 2,
    "product_api_call": 2,
    "web_search": 2,
}

REPORT_PROFILE_EVENT_TYPES = {
    "quick": "report_quick_run",
    "standard": "report_standard_run",
    "deep": "report_deep_run",
    "research": "report_research_run",
}

REPORT_PROFILE_LABELS = {
    "quick": "快速报告",
    "standard": "标准报告",
    "deep": "深度报告",
    "research": "研究报告",
}

STRUCTURED_PAYLOAD_START = "<!-- XM_PAYLOAD_START"
STRUCTURED_PAYLOAD_END = "XM_PAYLOAD_END -->"

CHART_PANEL_GROUP_LABELS = {
    "overview": "机会总览",
    "internal_evidence": "站内证据",
    "market_trend": "市场趋势",
    "risk_exclusion": "风险排除",
    "supplier": "供应承接",
    "diagnostic": "诊断补证",
}

CHART_PANEL_GROUP_ORDER = [
    "overview",
    "internal_evidence",
    "market_trend",
    "risk_exclusion",
    "supplier",
    "diagnostic",
]

CHART_PANEL_TIER_LABELS = {
    "primary": "关键图表",
    "supporting": "支撑图表",
    "diagnostic": "诊断图表",
}

DEFAULT_CHART_PANEL_META = {
    "forecast_top_asins_sales": {"panel_group": "overview", "panel_tier": "primary", "display_priority": 10, "evidence_layer": "internal"},
    "forecast_top_asins_chart": {"panel_group": "overview", "panel_tier": "primary", "display_priority": 10, "evidence_layer": "internal"},
    "top_asin_w1_w4_compare": {"panel_group": "overview", "panel_tier": "primary", "display_priority": 20, "evidence_layer": "internal"},
    "candidate_vs_benchmark_compare": {"panel_group": "overview", "panel_tier": "supporting", "display_priority": 30, "evidence_layer": "internal"},
    "forecast_top_asins_growth": {"panel_group": "overview", "panel_tier": "supporting", "display_priority": 35, "evidence_layer": "internal"},
    "forecast_top_asins_growth_chart": {"panel_group": "overview", "panel_tier": "supporting", "display_priority": 36, "evidence_layer": "internal"},
    "top_asin_drilldown_chart": {"panel_group": "internal_evidence", "panel_tier": "supporting", "display_priority": 45, "evidence_layer": "internal"},
    "forecast_driver_distribution": {"panel_group": "internal_evidence", "panel_tier": "supporting", "display_priority": 50, "evidence_layer": "internal"},
    "asin_sales_trend_line": {"panel_group": "internal_evidence", "panel_tier": "supporting", "display_priority": 60, "evidence_layer": "internal"},
    "asin_price_trend_line": {"panel_group": "internal_evidence", "panel_tier": "supporting", "display_priority": 61, "evidence_layer": "internal"},
    "asin_bsr_trend_line": {"panel_group": "internal_evidence", "panel_tier": "supporting", "display_priority": 62, "evidence_layer": "internal"},
    "asin_review_growth_trend_line": {"panel_group": "internal_evidence", "panel_tier": "supporting", "display_priority": 63, "evidence_layer": "internal"},
    "asin_stability_scorecard": {"panel_group": "diagnostic", "panel_tier": "diagnostic", "display_priority": 80, "evidence_layer": "internal"},
    "weak_signal_score_rank": {"panel_group": "diagnostic", "panel_tier": "diagnostic", "display_priority": 81, "evidence_layer": "internal"},
    "weak_signal_momentum_compare": {"panel_group": "diagnostic", "panel_tier": "diagnostic", "display_priority": 82, "evidence_layer": "internal"},
}

TOOL_BILLING_EVENT = {
    "search_knowledge_base": "kb_retrieve",
    "web_search": "web_search",
    "resolve_candidates": "product_api_call",
    "candidate_pool_stats": "product_api_call",
    "candidate_pool_trends": "product_api_call",
    "candidate_pool_weak_forecast": "product_api_call",
    "top_asin_drilldown": "product_api_call",
    "asin_history_timeseries": "product_api_call",
    "category_benchmark": "product_api_call",
    "keepa_asin_lookup": "product_api_call",
}

WORKFLOW_SUGGESTION_PROMPTS = [
    {
        "title": ["/report 示例", "宠物自动喂食器在 TikTok 美国市场的前景"],
        "content": "/report standard 帮我调研一下宠物自动喂食器在 TikTok 美国市场的前景",
    },
    {
        "title": ["/web 示例", "TikTok Shop 美国站最新政策"],
        "content": "/web 帮我搜索并总结 2026 年 TikTok Shop 美国站最新入驻与合规政策变化",
    },
    {
        "title": ["工具调研示例", "portable blender 在 Amazon 美国市场是否值得做"],
        "content": "帮我分析 portable blender 在 Amazon 美国市场是否值得做，并给出建议调用的工具和验证路径。",
    },
    {
        "title": ["规则问答示例", "TikTok Shop 美国站注册规则"],
        "content": "TikTok Shop 美国站的注册规则和资料要求是什么？",
    },
]

WORKFLOW_NODE_LABELS = {
    "n02_normalize": "商品归一化",
    "n03_parse_normalized_intent": "参数解析",
    "n04_resolve_candidates": "候选池解析",
    "n04b_parse_resolve_response": "候选池结果整理",
    "n06_candidate_pool_stats": "候选池统计",
    "n06b_parse_candidate_pool_stats_response": "候选池统计整理",
    "n07_category_benchmark": "类目基准对比",
    "n07b_parse_category_benchmark_response": "类目基准整理",
    "n08_candidate_pool_trends": "趋势诊断",
    "n08b_parse_candidate_pool_trends_response": "趋势结果整理",
    "n09_candidate_pool_weak_forecast": "弱信号预测",
    "n09b_parse_candidate_pool_weak_forecast_response": "弱信号结果整理",
    "n10_select_top_asins": "头部 ASIN 筛选",
    "n11_top_asin_drilldown": "头部 ASIN 深挖",
    "n11b_parse_top_asin_drilldown_response": "头部 ASIN 结果整理",
    "n12a_build_knowledge_query": "知识检索参数构建",
    "n12_route_primary_platform_kb": "平台知识路由",
    "n13_tiktok_primary_knowledge": "TikTok 知识检索",
    "n13_temu_primary_knowledge": "Temu 知识检索",
    "n13_amazon_primary_knowledge": "Amazon 知识检索",
    "n13_aggregate_kb_results": "知识结果聚合",
    "n13b_parse_kb_result": "知识结果整理",
    "n14_analysis": "分析底稿生成",
    "n15_presentation": "最终报告生成",
}

WORKFLOW_ESTIMATED_STEPS = 14

class Pipeline:
    class Valves(BaseModel):
        DIFY_REQUEST_TIMEOUT: int = 180
        CHAT_BACKEND_BASE_URL: str = "http://chat-backend:8200"
        CHAT_BACKEND_TIMEOUT: int = 180
        CHAT_BACKEND_SERVICE_SECRET: str = ""
        CHAT_BACKEND_SERVICE_NAME: str = "open-webui-pipeline"
        AGENT_OPENAI_MODEL: str = "deepseek-v4-pro"
        AGENT_ANTHROPIC_MODEL: str = "MiniMax-M2.7-highspeed"
        AGENT_MODEL_DEFAULT_PROFILE: str = "deepseek"
        AGENT_MODEL_PROFILES: str = "deepseek,minimax"
        AGENT_MODEL_DEEPSEEK_LABEL: str = "DeepSeek V4 Pro"
        AGENT_MODEL_MINIMAX_LABEL: str = "MiniMax M2.7"
        XIAMIMATE_MODEL_PREFIX: str = "xiamimate"

    def __init__(self):
        self.type = "manifold"
        self.id = os.getenv("XIAMIMATE_MODEL_PREFIX", "xiamimate")
        self.name = "XiaMimate: "
        self.agent_tools = self._load_agent_tools()
        self.valves = self.Valves(
            **{
                "DIFY_REQUEST_TIMEOUT": int(os.getenv("DIFY_REQUEST_TIMEOUT", "180")),
                "CHAT_BACKEND_BASE_URL": os.getenv("CHAT_BACKEND_BASE_URL", "http://chat-backend:8200"),
                "CHAT_BACKEND_TIMEOUT": int(os.getenv("CHAT_BACKEND_TIMEOUT", "180")),
                "CHAT_BACKEND_SERVICE_SECRET": os.getenv("CHAT_BACKEND_SERVICE_SECRET", ""),
                "CHAT_BACKEND_SERVICE_NAME": os.getenv("CHAT_BACKEND_SERVICE_NAME", "open-webui-pipeline"),
                "AGENT_OPENAI_MODEL": os.getenv("AGENT_OPENAI_MODEL", "deepseek-v4-pro"),
                "AGENT_ANTHROPIC_MODEL": os.getenv("AGENT_ANTHROPIC_MODEL", "MiniMax-M2.7-highspeed"),
                "AGENT_MODEL_DEFAULT_PROFILE": os.getenv("AGENT_MODEL_DEFAULT_PROFILE", "deepseek"),
                "AGENT_MODEL_PROFILES": os.getenv("AGENT_MODEL_PROFILES", "deepseek,minimax"),
                "AGENT_MODEL_DEEPSEEK_LABEL": os.getenv("AGENT_MODEL_DEEPSEEK_LABEL", "DeepSeek V4 Pro"),
                "AGENT_MODEL_MINIMAX_LABEL": os.getenv("AGENT_MODEL_MINIMAX_LABEL", "MiniMax M2.7"),
                "XIAMIMATE_MODEL_PREFIX": os.getenv("XIAMIMATE_MODEL_PREFIX", "xiamimate"),
            }
        )
        self.pipelines = self._build_agent_pipelines()

    async def on_startup(self):
        print("on_startup:xiamimate")

    async def on_shutdown(self):
        print("on_shutdown:xiamimate")

    async def on_valves_updated(self):
        self.id = self.valves.XIAMIMATE_MODEL_PREFIX
        self.pipelines = self._build_agent_pipelines()

    def _load_agent_tools(self):
        tools_path = Path(__file__).resolve().parent.parent / "tools" / "xiamimate_theme_tools.py"
        if not tools_path.exists():
            print("xiamimate.agent tools file not found", str(tools_path))
            return None

        spec = importlib.util.spec_from_file_location("xiamimate_theme_tools", tools_path)
        if spec is None or spec.loader is None:
            print("xiamimate.agent failed to create tools module spec", str(tools_path))
            return None

        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.Tools()
        except Exception as exc:
            print("xiamimate.agent failed to load tools", str(exc))
            return None

    def _default_agent_profile(self) -> str:
        normalized = str(self.valves.AGENT_MODEL_DEFAULT_PROFILE or "").strip().lower()
        return normalized if normalized in {"deepseek", "minimax"} else "deepseek"

    def _configured_agent_profiles(self) -> List[str]:
        profiles = []
        for raw_value in str(self.valves.AGENT_MODEL_PROFILES or "").split(","):
            profile = raw_value.strip().lower()
            if profile not in {"deepseek", "minimax"} or profile in profiles:
                continue
            profiles.append(profile)
        if not profiles:
            profiles.append(self._default_agent_profile())
        return profiles

    def _model_name_for_profile(self, profile: str) -> str:
        normalized = str(profile or "").strip().lower()
        if normalized == "minimax":
            return str(self.valves.AGENT_ANTHROPIC_MODEL or "").strip() or "MiniMax-M2.7-highspeed"
        return str(self.valves.AGENT_OPENAI_MODEL or "").strip() or "deepseek-v4-pro"

    def _label_for_profile(self, profile: str) -> str:
        normalized = str(profile or "").strip().lower()
        if normalized == "minimax":
            return str(self.valves.AGENT_MODEL_MINIMAX_LABEL or "MiniMax M2.7").strip() or "MiniMax M2.7"
        return str(self.valves.AGENT_MODEL_DEEPSEEK_LABEL or "DeepSeek V4 Pro").strip() or "DeepSeek V4 Pro"

    def _pipeline_id_for_profile(self, profile: str) -> str:
        normalized = str(profile or "").strip().lower()
        if normalized == self._default_agent_profile():
            return "agent"
        return "agent-%s" % normalized

    def _build_agent_pipelines(self) -> List[dict]:
        pipelines: List[dict] = []
        seen_ids = set()
        profiles = self._configured_agent_profiles()

        ordered_profiles = [self._default_agent_profile()] + [profile for profile in profiles if profile != self._default_agent_profile()]
        for profile in ordered_profiles:
            if profile not in profiles:
                continue
            pipeline_id = self._pipeline_id_for_profile(profile)
            if pipeline_id in seen_ids:
                continue
            seen_ids.add(pipeline_id)
            label = self._label_for_profile(profile)
            description = "虾米选品的智能体模式，支持 /report 报告编排，并兼容 /workflow 旧入口。当前模型：%s。" % label
            pipelines.append(
                {
                    "id": pipeline_id,
                    "name": "Agent" if pipeline_id == "agent" else "Agent · %s" % label,
                    "info": {
                        "meta": {
                            "description": description,
                            "capabilities": {
                                "status_updates": True,
                            },
                            "suggestion_prompts": WORKFLOW_SUGGESTION_PROMPTS,
                            "xiamimate_profile": profile,
                            "xiamimate_model_name": self._model_name_for_profile(profile),
                        }
                    },
                }
            )
        return pipelines

    def _resolve_agent_profile(self, model_id: str, body: dict) -> str:
        override = str(
            body.get("xiamimate_model_profile")
            or body.get("xiamimate_agent_profile")
            or ((body.get("metadata") or {}).get("xiamimate_model_profile") if isinstance(body.get("metadata"), dict) else "")
            or ""
        ).strip().lower()
        if override in self._configured_agent_profiles():
            return override

        effective_model_id = str(body.get("model") or model_id or "").strip().lower()
        for profile in self._configured_agent_profiles():
            explicit_pipeline_id = "agent-%s" % profile
            if effective_model_id.endswith(".%s" % explicit_pipeline_id) or effective_model_id == explicit_pipeline_id:
                return profile

        return self._default_agent_profile()

    def _response_model_for_profile(self, profile: str, requested_model_id: str) -> str:
        effective_model_id = str(requested_model_id or "").strip().lower()
        explicit_pipeline_id = "agent-%s" % str(profile or "").strip().lower()
        selected_pipeline_id = explicit_pipeline_id if effective_model_id.endswith(".%s" % explicit_pipeline_id) or effective_model_id == explicit_pipeline_id else self._pipeline_id_for_profile(profile)
        return "%s.%s" % (self.id, selected_pipeline_id)

    def _get_provider(self, model_name: Optional[str] = None) -> ProviderStrategy:
        """Resolve the LLM provider strategy based on the selected model name."""
        return get_provider(model_name or self._model_name_for_profile(self._default_agent_profile()))

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: List[dict],
        body: dict,
    ) -> Union[str, dict, Iterator[bytes]]:
        agent_profile = self._resolve_agent_profile(model_id=model_id, body=body)
        agent_model_name = self._model_name_for_profile(agent_profile)
        response_model = self._response_model_for_profile(agent_profile, str(body.get("model") or model_id or ""))
        account_command = self._parse_account_command(self._extract_last_user_text(messages) or (user_message or ""))
        if account_command is not None:
            return self._handle_account_command(command=account_command, body=body, model=response_model)

        mode, normalized_user_message, command_used = self._resolve_mode(
            user_message=user_message,
            model_id=model_id,
            messages=messages,
            body=body,
        )
        normalized_messages = (
            self._rewrite_last_user_message(messages, normalized_user_message) if command_used else messages
        )

        self._disable_web_search_feature(body)

        if mode == "workflow":
            return self._run_workflow(query=normalized_user_message, body=body, model=response_model)
        if mode == "report":
            return self._run_report(query=normalized_user_message, body=body, model=response_model)
        if mode == "web":
            return self._run_web_search(query=normalized_user_message, body=body, model=response_model)
        if mode in {"agent", "tool"}:
            return self._run_agent(
                messages=normalized_messages,
                body=body,
                model=response_model,
                mode=mode,
                model_name=agent_model_name,
            )

        return self._chat_response(content="未识别的 XiaMimate 模式。请使用 Agent。", model=response_model)

    def _run_workflow(self, query: str, body: dict, model: str) -> Union[dict, Iterator[bytes]]:
        return self._run_report_profile(
            query=query,
            body=body,
            model=model,
            profile="standard",
            mode_tag="workflow",
            guidance=(
                "请在 /workflow 后直接写出调研需求。当前 /workflow 会兼容走标准报告，例如：\n"
                "/workflow 帮我调研一下宠物自动喂食器在 TikTok 美国市场的前景"
            ),
        )

    def _run_report(self, query: str, body: dict, model: str) -> Union[dict, Iterator[bytes]]:
        profile, normalized_query = self._parse_report_profile(query)
        return self._run_report_profile(
            query=normalized_query,
            body=body,
            model=model,
            profile=profile,
            mode_tag="report",
            guidance=(
                "请在 /report 后直接写出调研需求，可选档位为 quick / standard / deep / research，例如：\n"
                "/report standard 帮我调研一下宠物自动喂食器在 TikTok 美国市场的前景"
            ),
        )

    def _run_report_profile(
        self,
        query: str,
        body: dict,
        model: str,
        *,
        profile: str,
        mode_tag: str,
        guidance: str,
    ) -> Union[dict, Iterator[bytes]]:
        report_label = REPORT_PROFILE_LABELS[profile]
        return self._run_dify_chatflow(
            query=query,
            body=body,
            model=model,
            event_type=REPORT_PROFILE_EVENT_TYPES[profile],
            charge_description=report_label,
            run_path="/internal/provider/report/run",
            run_stream_path="/internal/provider/report/run-stream",
            mode_tag=mode_tag,
            guidance=guidance,
            request_payload={"profile": profile},
        )

    def _run_web_search(self, query: str, body: dict, model: str) -> Union[dict, Iterator[bytes]]:
        return self._run_dify_chatflow(
            query=query,
            body=body,
            model=model,
            event_type="web_search",
            charge_description="网络搜索",
            run_path="/internal/provider/dify-web-search/run",
            run_stream_path="/internal/provider/dify-web-search/run-stream",
            mode_tag="web",
            guidance=(
                "请在 /web 后直接写出要联网搜索的问题，例如：\n"
                "/web 帮我搜索并总结 2026 年 TikTok Shop 美国站最新入驻政策变化"
            ),
        )

    def _run_dify_chatflow(
        self,
        query: str,
        body: dict,
        model: str,
        *,
        event_type: str,
        charge_description: str,
        run_path: str,
        run_stream_path: str,
        mode_tag: str,
        guidance: str,
        request_payload: Optional[dict] = None,
    ) -> Union[dict, Iterator[bytes]]:
        query = (query or "").strip()
        if not query:
            if body.get("stream"):
                return self._stream_text_response(content=guidance, model=model)
            return self._chat_response(content=guidance, model=model)

        if not self.valves.CHAT_BACKEND_SERVICE_SECRET:
            message = "CHAT_BACKEND_SERVICE_SECRET 未配置。"
            if body.get("stream"):
                return self._stream_text_response(content=message, model=model)
            return self._chat_response(content=message, model=model)

        try:
            billing_context = self._ensure_billing_context(body)
            flow_charge = self._charge_billing_event(
                billing_context=billing_context,
                event_type=event_type,
                description=charge_description,
                meta={
                    "mode": mode_tag,
                    "report_profile": (request_payload or {}).get("profile"),
                    "stream": bool(body.get("stream")),
                    "query_preview": query[:200],
                },
            )
        except RuntimeError as exc:
            message = self._error_text(str(exc))
            if body.get("stream"):
                return self._stream_text_response(content=message, model=model)
            return self._chat_response(content=message, model=model)

        if body.get("stream"):
            return self._run_dify_chatflow_stream(
                query=query,
                body=body,
                model=model,
                billing_context=billing_context,
                flow_charge=flow_charge,
                run_stream_path=run_stream_path,
                mode_tag=mode_tag,
                refund_description="%s失败，已退款" % charge_description,
                request_payload=request_payload,
            )

        try:
            response = self._chat_backend_request(
                method="POST",
                path=run_path,
                body={
                    "query": query,
                    "user": billing_context["user_id"],
                    **(request_payload or {}),
                },
                internal=True,
                timeout=self.valves.DIFY_REQUEST_TIMEOUT,
            )
        except RuntimeError as exc:
            self._refund_billing_event(
                billing_context=billing_context,
                charge=flow_charge,
                description="%s失败，已退款" % charge_description,
                meta={"mode": mode_tag, "error": str(exc)[:500]},
            )
            return self._chat_response(content=self._error_text(str(exc)), model=model)

        answer = self._extract_workflow_answer(response)
        if answer:
            return self._chat_response(content=self._prepare_workflow_answer(answer), model=model)

        return self._chat_response(content=json.dumps(response, ensure_ascii=False, indent=2), model=model)

    def _run_agent(
        self,
        messages: List[dict],
        body: dict,
        model: str,
        model_name: str,
        mode: str = "agent",
    ) -> Union[dict, Iterator[bytes], str]:
        if not self.valves.CHAT_BACKEND_SERVICE_SECRET:
            return "CHAT_BACKEND_SERVICE_SECRET 未配置。"

        try:
            billing_context = self._ensure_billing_context(body)
        except RuntimeError as exc:
            return self._error_text(str(exc))

        if body.get("stream"):
            return self._run_agent_stream(
                messages=messages,
                body=body,
                model=model,
                model_name=model_name,
                billing_context=billing_context,
                mode=mode,
            )

        try:
            answer = self._run_agent_loop(
                messages=messages,
                body=body,
                billing_context=billing_context,
                mode=mode,
                model_name=model_name,
            )
        except RuntimeError as exc:
            return self._error_text(str(exc))

        return self._chat_response(content=answer, model=model)

    def _run_agent_stream(
        self,
        messages: List[dict],
        body: dict,
        model: str,
        model_name: str,
        billing_context: dict,
        mode: str,
    ) -> Iterator[bytes]:
        response_id = "%s-%s" % (model, uuid.uuid4())
        created = int(time.time())
        conversation = deepcopy(messages or [])
        answer_started = False
        used_tools = False
        reasoning_open = False
        tool_observations: List[dict] = []

        def emit_text_chunk(content: str) -> bytes:
            return self._stream_content_chunk(
                response_id=response_id,
                created=created,
                model=model,
                content=content,
            )

        def emit_reasoning_chunks(content: str) -> List[bytes]:
            nonlocal reasoning_open

            chunks: List[bytes] = []
            if not reasoning_open:
                reasoning_open = True
                chunks.append(
                    self._stream_reasoning_open_chunk(
                        response_id=response_id,
                        created=created,
                        model=model,
                    )
                )
            if content:
                chunks.append(
                    self._stream_reasoning_text_chunk(
                        response_id=response_id,
                        created=created,
                        model=model,
                        content=content,
                    )
                )
            return chunks

        def close_reasoning_chunk() -> Optional[bytes]:
            nonlocal reasoning_open

            if not reasoning_open:
                return None
            reasoning_open = False
            return self._stream_reasoning_close_chunk(
                response_id=response_id,
                created=created,
                model=model,
            )

        try:
            for chunk in emit_reasoning_chunks(self._format_agent_progress("正在分析问题")):
                yield chunk

            for round_index in range(6):
                payload = self._prepare_agent_payload(messages=conversation, body=body, mode=mode, model_name=model_name)
                payload["stream"] = False
                minimax_charge = self._charge_billing_event(
                    billing_context=billing_context,
                    event_type="llm_request",
                    description="LLM 请求",
                    meta={
                        "mode": "agent",
                        "model": payload.get("model"),
                        "message_count": len(payload.get("messages") or []),
                        "stream": True,
                    },
                )
                try:
                    response = self._post_agent_payload(payload, model_name=model_name)
                except RuntimeError as exc:
                    self._refund_billing_event(
                        billing_context=billing_context,
                        charge=minimax_charge,
                        description="LLM 请求失败，已退款",
                        meta={"mode": "agent", "stream": True, "error": str(exc)[:500]},
                    )
                    if tool_observations:
                        final_answer = self._fallback_answer_from_tool_observations(tool_observations, error=str(exc))
                        for chunk in emit_reasoning_chunks(self._format_agent_progress("模型整理失败，返回工具结果摘要")):
                            yield chunk
                        close_chunk = close_reasoning_chunk()
                        if close_chunk is not None:
                            yield close_chunk
                        for chunk in self._split_text(final_answer):
                            answer_started = True
                            yield emit_text_chunk(chunk)
                        break
                    raise

                content = self._extract_assistant_content(response)
                assistant_message = self._extract_assistant_message(response)
                native_tool_calls = self._extract_response_tool_calls(response)
                text_tool_calls = [] if native_tool_calls else self._extract_tool_calls(content, model_name=model_name)
                tool_calls = native_tool_calls or text_tool_calls

                if not tool_calls:
                    cleaned = self._clean_agent_content(content, model_name=model_name)
                    if cleaned:
                        final_answer = cleaned
                    elif self._agent_stream_contains_internal_markup(content, model_name=model_name):
                        final_answer = "已完成分析，但未生成可展示的结果，请重试。"
                    else:
                        final_answer = str(content or "").strip()
                    status_line = "正在生成最终答复" if round_index == 0 else "工具执行完成，正在生成最终答复"
                    for chunk in emit_reasoning_chunks(self._format_agent_progress(status_line)):
                        yield chunk

                    close_chunk = close_reasoning_chunk()
                    if close_chunk is not None:
                        yield close_chunk

                    if used_tools or self._agent_stream_contains_internal_markup(content, model_name=model_name):
                        for chunk in self._split_text(final_answer):
                            answer_started = True
                            yield emit_text_chunk(chunk)
                    else:
                        for chunk in self._stream_agent_final_answer_chunks(
                            payload=payload,
                            fallback_content=final_answer,
                            model_name=model_name,
                        ):
                            answer_started = True
                            yield emit_text_chunk(chunk)
                    break

                used_tools = True
                conversation.append(assistant_message if native_tool_calls else {"role": "assistant", "content": content})

                tool_names = ", ".join(tool_call["name"] for tool_call in tool_calls)
                for chunk in emit_reasoning_chunks(self._format_agent_progress("正在调用工具: %s" % tool_names)):
                    yield chunk

                tool_results = []
                for tool_call in tool_calls:
                    result = self._execute_tool_call(tool_call, billing_context, truncate=False)
                    observation = self._build_tool_observation(tool_call=tool_call, result=result)
                    tool_observations.append(observation)
                    if native_tool_calls and tool_call.get("tool_call_id"):
                        conversation.append(
                            {
                                "role": "tool",
                                "tool_call_id": str(tool_call.get("tool_call_id")),
                                "content": observation["llm_result"],
                            }
                        )
                    else:
                        tool_results.append(
                            TOOL_RESULT_TEMPLATE.format(
                                tool_name=tool_call["name"],
                                arguments=json.dumps(tool_call.get("parameters") or {}, ensure_ascii=False),
                                result=observation["llm_result"],
                            )
                        )
                    tool_status = "失败" if self._tool_result_has_error(result) else "完成"
                    for chunk in emit_reasoning_chunks(
                        self._format_agent_progress("工具 %s 已%s" % (tool_call["name"], tool_status))
                    ):
                        yield chunk

                if tool_results:
                    conversation.append({"role": "user", "content": "\n\n".join(tool_results)})

            if not answer_started:
                raise RuntimeError("Agent 工具调用轮次超过上限，已中止。")
        except RuntimeError as exc:
            close_chunk = close_reasoning_chunk()
            if close_chunk is not None:
                yield close_chunk
            yield emit_text_chunk("\n" + self._error_text(str(exc)))

        close_chunk = close_reasoning_chunk()
        if close_chunk is not None:
            yield close_chunk
        yield self._stream_stop_chunk(response_id=response_id, created=created, model=model)
        yield b"data: [DONE]\n\n"

    def _resolve_mode(
        self,
        user_message: str,
        model_id: str,
        messages: List[dict],
        body: dict,
    ) -> Tuple[str, str, bool]:
        last_user_text = self._extract_last_user_text(messages) or (user_message or "")
        command_mode, command_query = self._parse_mode_command(last_user_text)
        requested_mode = str(body.get("xiamimate_mode") or "").strip().lower()
        effective_model = str(body.get("model", model_id) or model_id or "")
        model_mode = effective_model.split(".")[-1].lower() if effective_model else ""

        mode = requested_mode or command_mode or (model_mode if model_mode in {"agent", "tool", "workflow", "report"} else "agent")
        query = (command_query if command_mode else (user_message or last_user_text or "")).strip()
        return mode, query, command_mode is not None

    def _parse_report_profile(self, text: str) -> Tuple[str, str]:
        stripped = (text or "").strip()
        if not stripped:
            return "standard", ""

        first_token, _, remainder = stripped.partition(" ")
        normalized = first_token.lower().strip()
        if normalized in REPORT_PROFILE_EVENT_TYPES:
            return normalized, remainder.strip()
        return "standard", stripped

    def _parse_mode_command(self, text: str) -> Tuple[Optional[str], str]:
        stripped = (text or "").lstrip()
        if not stripped.startswith("/"):
            return None, text or ""

        first_token, _, remainder = stripped.partition(" ")
        normalized = first_token.lower().rstrip("：:")
        if normalized not in COMMAND_TO_MODE:
            return None, text or ""

        return COMMAND_TO_MODE[normalized], remainder.strip()

    def _parse_account_command(self, text: str) -> Optional[str]:
        stripped = (text or "").lstrip()
        if not stripped.startswith("/"):
            return None

        first_token, _, _ = stripped.partition(" ")
        normalized = first_token.lower().rstrip("：:")
        return ACCOUNT_COMMANDS.get(normalized)

    def _load_tool_json_payload(self, result: Any) -> Optional[dict]:
        if isinstance(result, dict):
            return result

        text = str(result or "").strip()
        if not text:
            return None

        try:
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else None
        except ValueError:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _handle_account_command(self, command: str, body: dict, model: str) -> Union[dict, Iterator[bytes], str]:
        try:
            overview = self._chat_backend_request(
                method="GET",
                path="/v1/me/account-overview",
                headers=self._chat_backend_user_headers(body),
            )
            portal_url = "/portal"
            content = self._format_account_overview(command=command, overview=overview, portal_url=portal_url)
        except RuntimeError as exc:
            content = self._error_text(str(exc))

        if body.get("stream"):
            return self._stream_text_response(content=content, model=model)
        return self._chat_response(content=content, model=model)

    def _format_account_overview(self, command: str, overview: dict, portal_url: str = "") -> str:
        user = overview.get("user") or {}
        points_account = overview.get("points_account") or {}
        balance_breakdown = overview.get("balance_breakdown") or {}
        usage_summary = overview.get("usage_summary") or {}
        usage_by_type = overview.get("usage_by_type_30d") or []
        recent_ledger = overview.get("recent_ledger") or []
        subscriptions = overview.get("subscriptions") or []
        daily_quota = overview.get("daily_quota_state") or {}
        point_cost_by_event = overview.get("point_cost_by_event") or {}
        event_display = overview.get("event_pricing_display") or {}

        display_name = str(user.get("display_name") or user.get("user_id") or "当前用户")
        email = str(user.get("email") or "")
        balance_points = int(points_account.get("balance_points") or 0)
        plan_tier = str(overview.get("plan_tier") or user.get("plan_tier") or "unknown")
        event_count_30d = int(usage_summary.get("event_count_30d") or 0)

        lines: List[str] = []

        if portal_url:
            lines.append("🔗 **账户门户**")
            lines.extend(
                self._render_markdown_table(
                    headers=["功能", "链接"],
                    rows=[["账户详情", f"[打开账户门户查看详细消费记录]({portal_url})"]],
                )
            )
            lines.append("")

        lines.append("👤 **用户信息**")
        user_rows = [["用户", display_name]]
        if email:
            user_rows.append(["邮箱", email])
        user_rows.append(["套餐", self._plan_tier_label(plan_tier)])
        if command in {"overview", "points"}:
            user_rows.append(["积分余额", f"{balance_points} 点"])
        if command in {"overview", "usage"}:
            user_rows.append(["近30天使用", f"{event_count_30d} 次"])
        lines.extend(self._render_markdown_table(headers=["字段", "内容"], rows=user_rows))

        if command in {"overview", "points"}:
            lines.append("")
            lines.append("💰 **积分概览**")
            points_rows = [
                ["当前积分余额", str(balance_points)],
                ["月包余额", str(int(balance_breakdown.get("subscription_balance_points") or 0))],
                ["充值包余额", str(int(balance_breakdown.get("recharge_balance_points") or 0))],
                ["其他赠送余额", str(int(balance_breakdown.get("other_balance_points") or 0))],
                ["累计赠送积分", str(int(points_account.get("lifetime_granted_points") or 0))],
                ["累计购买积分", str(int(points_account.get("lifetime_purchased_points") or 0))],
                ["累计消费积分", str(int(points_account.get("lifetime_spent_points") or 0))],
            ]
            if daily_quota:
                quota_points = int(daily_quota.get("quota_points") or 0)
                consumed_points = int(daily_quota.get("consumed_points") or 0)
                points_rows.append(
                    [
                        "Guest 当日配额",
                        "%s/%s（日期 %s）"
                        % (max(0, quota_points - consumed_points), quota_points, daily_quota.get("quota_date") or "-"),
                    ]
                )
            lines.extend(self._render_markdown_table(headers=["项目", "数值"], rows=points_rows))
            lines.append("")
            lines.append("- 扣减规则：%s" % self._consumption_policy_text(balance_breakdown))
            lines.append("- 扣减顺序：%s" % self._consumption_priority_text(balance_breakdown))
            lines.append("- 计费提示：/report 按档位计费；/workflow 当前兼容走标准报告语义，内部检索和工具调用会保留记录，但不会重复额外收费。")
            if recent_ledger:
                lines.append("")
                lines.append("🧾 **最近账本**")
                ledger_rows = []
                for row in recent_ledger[:6]:
                    delta = int(row.get("points_delta") or 0)
                    sign = "+" if delta >= 0 else ""
                    ledger_rows.append(
                        [
                            self._format_beijing_time(row.get("created_at")),
                            self._ledger_item_label(row),
                            "%s%s" % (sign, delta),
                            self._ledger_source_summary_label(row),
                            str(row.get("balance_after_points") or 0),
                            self._ledger_description_label(row),
                        ]
                    )
                lines.extend(
                    self._render_markdown_table(
                        headers=["时间", "消费项目", "变动", "扣减来源", "余额", "说明"],
                        rows=ledger_rows,
                    )
                )

        if command in {"overview", "usage"}:
            lines.append("")
            lines.append("📈 **使用汇总（按计费单位统计）**")
            lines.extend(
                self._render_markdown_table(
                    headers=["周期", "数值"],
                    rows=[
                        ["1 天内总用量", str(usage_summary.get("units_1d", 0))],
                        ["7 天内总用量", str(usage_summary.get("units_7d", 0))],
                        ["30 天内总用量", str(usage_summary.get("units_30d", 0))],
                        ["30 天内事件数", str(usage_summary.get("event_count_30d", 0))],
                    ],
                )
            )
            lines.append("")
            lines.append("- 说明：这里按计费单位统计使用量，不直接等于人民币金额。实际扣费优先消耗月包积分，月包不足时再扣充值包积分。")
            lines.append("- 常见计价：快速报告 8 积分/次，标准报告 16 积分/次，深度报告 24 积分/次，研究报告 32 积分/次，知识库检索 2 积分/次，商品 API 检索 2 积分/次，网络搜索 2 积分/次。")
            if usage_by_type:
                lines.append("")
                lines.append("- 30 天内按事件类型：")
                usage_rows = []
                for row in usage_by_type[:8]:
                    event_type = str(row.get("event_type") or "")
                    label = self._event_type_label(event_type, event_display)
                    usage_rows.append([label, str(row.get("total_units") or 0)])
                lines.extend(self._render_markdown_table(headers=["行为", "30天总用量"], rows=usage_rows))

        if command in {"overview", "plan"}:
            lines.append("")
            lines.append("📦 **套餐与计价**")
            plan_rows = [["当前套餐", self._plan_tier_label(plan_tier)]]
            entitlements = overview.get("entitlements") or {}
            if entitlements:
                plan_rows.append(["套餐权益", self._plan_entitlements_label(entitlements)])
            if subscriptions:
                latest = subscriptions[0]
                plan_rows.append(
                    [
                        "订阅状态",
                        "状态：%s；套餐：%s；月度积分：%s"
                        % (
                            latest.get("status") or "-",
                            latest.get("package_code") or "-",
                            latest.get("monthly_points") or "-",
                        ),
                    ]
                )
            lines.extend(self._render_markdown_table(headers=["项目", "说明"], rows=plan_rows))
            if point_cost_by_event:
                lines.append("")
                lines.append("- 当前计价：")
                pricing_rows = []
                for event_type, points in point_cost_by_event.items():
                    label = self._event_type_label(event_type, event_display)
                    pricing_rows.append([label, "%s 积分/次" % points])
                lines.extend(self._render_markdown_table(headers=["行为", "价格"], rows=pricing_rows))

        lines.append("")
        lines.append("🧭 **可用命令**")
        lines.extend(
            self._render_markdown_table(
                headers=["命令", "说明"],
                rows=[
                    ["/me", "查看账户综合概览（用户信息、积分、使用量、套餐）"],
                    ["/points", "查看积分余额与近期账本明细"],
                    ["/usage", "查看近 30 天使用汇总与按事件类型统计"],
                    ["/plan", "查看当前套餐、权益与计价表"],
                ],
            )
        )
        return "\n".join(lines)

    def _render_markdown_table(self, headers: List[str], rows: List[List[str]]) -> List[str]:
        if not rows:
            return []
        table_lines = ["| %s |" % " | ".join(headers), "| %s |" % " | ".join(["---"] * len(headers))]
        for row in rows:
            table_lines.append("| %s |" % " | ".join(self._escape_table_cell(cell) for cell in row))
        return table_lines

    def _escape_table_cell(self, value: Any) -> str:
        text = str(value or "-")
        return text.replace("|", "\\|").replace("\n", "<br>")

    def _format_beijing_time(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "-"
        try:
            normalized = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return text

    def _plan_tier_label(self, value: str) -> str:
        mapping = {
            "free": "免费版",
            "guest": "访客版",
            "standard": "标准版",
            "pro": "专业版",
            "admin": "管理员",
        }
        return mapping.get((value or "").strip().lower(), value or "-")

    def _event_type_label(self, event_type: str, event_display: dict) -> str:
        alias_mapping = {
            "llm_request": "LLM 请求",
            "minimax_request": "LLM 请求",
            "workflow_run": "Workflow 兼容入口",
            "report_quick_run": "快速报告",
            "report_standard_run": "标准报告",
            "report_deep_run": "深度报告",
            "report_research_run": "研究报告",
            "kb_retrieve": "知识库检索",
            "dify_knowledge_retrieve": "知识库检索",
            "product_api_call": "商品 API 检索",
            "web_search": "网络搜索",
        }
        if event_type in event_display:
            return str(event_display.get(event_type) or event_type)
        return alias_mapping.get(event_type, event_type or "-")

    def _balance_source_label(self, source: Any) -> str:
        mapping = {
            "subscription": "月包积分",
            "recharge": "充值包积分",
            "other": "其他赠送积分",
        }
        normalized = str(source or "").strip().lower()
        return mapping.get(normalized, "其他赠送积分")

    def _consumption_policy_text(self, balance_breakdown: dict) -> str:
        return str(
            balance_breakdown.get("consumption_policy_text")
            or "消费时优先扣减月包积分；月包不足时再扣充值包积分；充值包积分永久有效。"
        )

    def _consumption_priority_text(self, balance_breakdown: dict) -> str:
        priority = balance_breakdown.get("consumption_priority") or ["subscription", "recharge", "other"]
        labels = [self._balance_source_label(item) for item in priority]
        return " -> ".join(labels)

    def _ledger_item_label(self, row: dict) -> str:
        entry_type = str(row.get("entry_type") or "").strip().lower()
        if entry_type == "consume":
            return self._event_type_label(str(row.get("event_type") or ""), {})
        return self._ledger_entry_type_label(row.get("entry_type"), row.get("event_type"))

    def _ledger_source_summary_label(self, row: dict) -> str:
        meta = row.get("meta_json") or {}
        allocations = meta.get("balance_source_allocations") or []
        totals: Dict[str, int] = {}
        for item in allocations:
            source = str((item or {}).get("source") or "other").strip().lower() or "other"
            points = int((item or {}).get("points") or 0)
            if points <= 0:
                continue
            totals[source] = totals.get(source, 0) + points
        entry_type = str(row.get("entry_type") or "").strip().lower()
        if not totals and entry_type == "subscription_expire":
            expired_points = abs(int(row.get("points_delta") or 0))
            if expired_points > 0:
                totals["subscription"] = expired_points
        if not totals:
            return "按系统默认顺序扣减"
        parts = []
        for source in ["subscription", "recharge", "other"]:
            points = totals.get(source, 0)
            if points > 0:
                parts.append("%s %s" % (self._balance_source_label(source), points))
        return "；".join(parts)

    def _ledger_entry_type_label(self, entry_type: Any, event_type: Any) -> str:
        entry_text = str(entry_type or "").strip().lower()
        mapping = {
            "consume": "消费",
            "refund": "退款",
            "grant": "赠送",
            "signup_gift": "注册赠送",
            "admin_grant": "后台加积分",
            "subscription_grant": "订阅发放",
        }
        if entry_text in mapping:
            return mapping[entry_text]
        if entry_text:
            return mapping.get(entry_text, entry_text)
        return self._event_type_label(str(event_type or ""), {})

    def _ledger_description_label(self, row: dict) -> str:
        description = str(row.get("description") or "").strip()
        entry_type = str(row.get("entry_type") or "").strip().lower()
        event_type = str(row.get("event_type") or "").strip().lower()
        known_descriptions = {
            "MiniMax agent request": "按 LLM 请求次数计费。",
            "MiniMax agent request failed": "LLM 请求失败，系统已自动退款。",
            "快速报告": "一次快速报告计费，内部步骤只保留审计记录，不重复收费。",
            "标准报告": "一次标准报告计费，内部步骤只保留审计记录，不重复收费。",
            "深度报告": "一次深度报告计费，内部步骤只保留审计记录，不重复收费。",
            "研究报告": "一次研究报告计费，内部步骤只保留审计记录，不重复收费。",
            "Dify workflow run": "兼容旧入口 /workflow 当前按标准报告语义执行，内部步骤只保留审计记录，不重复收费。",
            "Dify workflow request failed": "Workflow 请求失败，系统已自动退款。",
            "网络搜索": "按网络搜索次数计费。",
            "网络搜索失败，已退款": "网络搜索失败，系统已自动退款。",
            "expired subscription points removed": "当前订阅周期结束后，未使用完的月包积分会自动清零。",
        }
        if description in known_descriptions:
            return known_descriptions[description]
        if description.startswith("Tool call: "):
            tool_name = description.replace("Tool call: ", "", 1).strip()
            return "工具调用：%s" % self._tool_name_label(tool_name)
        if description.startswith("Tool call failed: "):
            tool_name = description.replace("Tool call failed: ", "", 1).strip()
            return "工具调用失败：%s" % self._tool_name_label(tool_name)
        if description.startswith("Tool call returned error: "):
            tool_name = description.replace("Tool call returned error: ", "", 1).strip()
            return "工具调用异常：%s" % self._tool_name_label(tool_name)
        if description:
            return description
        if entry_type == "consume":
            usage_mapping = {
                "workflow_run": "兼容旧入口 /workflow 的历史计费记录。",
                "report_quick_run": "一次快速报告计费，内部检索和工具调用只保留记录，不重复收费。",
                "report_standard_run": "一次标准报告计费，内部检索和工具调用只保留记录，不重复收费。",
                "report_deep_run": "一次深度报告计费，内部检索和工具调用只保留记录，不重复收费。",
                "report_research_run": "一次研究报告计费，内部检索和工具调用只保留记录，不重复收费。",
                "llm_request": "按 LLM 请求次数计费。",
                "kb_retrieve": "按知识库检索次数计费。",
                "dify_knowledge_retrieve": "按知识库检索次数计费。",
                "product_api_call": "按商品 API 检索次数计费。",
                "web_search": "按网络搜索次数计费。",
            }
            if event_type in usage_mapping:
                return usage_mapping[event_type]
        if entry_type == "subscription_expire":
            return "当前订阅周期结束后，未使用完的月包积分会自动清零。"
        return self._event_type_label(event_type, {})

    def _tool_name_label(self, tool_name: str) -> str:
        mapping = {
            "search_knowledge_base": "知识库检索",
            "web_search": "网络搜索",
            "resolve_candidates": "候选池解析",
            "candidate_pool_stats": "候选池统计",
            "candidate_pool_trends": "候选池趋势",
            "candidate_pool_weak_forecast": "弱信号预测",
            "top_asin_drilldown": "头部 ASIN 深挖",
            "asin_history_timeseries": "ASIN 历史时序",
            "category_benchmark": "类目基准对比",
            "keepa_asin_lookup": "Keepa ASIN 查询",
        }
        return mapping.get(tool_name, tool_name or "-")

    def _plan_entitlements_label(self, entitlements: dict) -> str:
        if not isinstance(entitlements, dict) or not entitlements:
            return "-"
        parts: List[str] = []
        daily_theme_runs = entitlements.get("daily_theme_runs")
        if daily_theme_runs is not None:
            parts.append("每日主题分析次数 %s 次" % daily_theme_runs)
        retention_days = entitlements.get("history_retention_days")
        if retention_days is not None:
            parts.append("历史记录保留 %s 天" % retention_days)
        daily_points = entitlements.get("daily_points")
        if daily_points is not None:
            parts.append("每日积分额度 %s 点" % daily_points)
        return "；".join(parts) if parts else json.dumps(entitlements, ensure_ascii=False)

    def _extract_last_user_text(self, messages: List[dict]) -> str:
        for message in reversed(messages or []):
            if message.get("role") == "user":
                return self._extract_message_text(message)
        return ""

    def _extract_message_text(self, message: dict) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
            return "\n".join(texts)
        return ""

    def _rewrite_last_user_message(self, messages: List[dict], replacement: str) -> List[dict]:
        normalized_messages = deepcopy(messages or [])
        for message in reversed(normalized_messages):
            if message.get("role") != "user":
                continue

            content = message.get("content")
            if isinstance(content, str):
                message["content"] = replacement
                return normalized_messages
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        item["text"] = replacement
                        return normalized_messages
                content.insert(0, {"type": "text", "text": replacement})
                message["content"] = content
                return normalized_messages

            message["content"] = replacement
            return normalized_messages

        return normalized_messages

    def _run_dify_chatflow_stream(
        self,
        query: str,
        body: dict,
        model: str,
        billing_context: dict,
        flow_charge: dict,
        run_stream_path: str,
        mode_tag: str,
        refund_description: str,
        request_payload: Optional[dict] = None,
    ) -> Iterator[bytes]:
        response_id = "%s-%s" % (model, uuid.uuid4())
        created = int(time.time())
        finished_nodes = set()
        answer_started = False
        answer_chunks: List[str] = []
        emitted_progress = set()
        reasoning_open = False
        buffered_answer_mode = mode_tag in {"workflow", "report"}

        def emit_text_chunk(content: str) -> bytes:
            return self._stream_content_chunk(
                response_id=response_id,
                created=created,
                model=model,
                content=content,
            )

        def emit_reasoning_chunks(content: str) -> List[bytes]:
            nonlocal reasoning_open

            chunks: List[bytes] = []
            if not reasoning_open:
                reasoning_open = True
                chunks.append(
                    self._stream_reasoning_open_chunk(
                        response_id=response_id,
                        created=created,
                        model=model,
                    )
                )
            if content:
                chunks.append(
                    self._stream_reasoning_text_chunk(
                        response_id=response_id,
                        created=created,
                        model=model,
                        content=content,
                    )
                )
            return chunks

        def close_reasoning_chunk() -> Optional[bytes]:
            nonlocal reasoning_open

            if not reasoning_open:
                return None
            reasoning_open = False
            return self._stream_reasoning_close_chunk(
                response_id=response_id,
                created=created,
                model=model,
            )

        try:
            with self._chat_backend_stream_request(
                path=run_stream_path,
                body={
                    "query": query,
                    "user": billing_context["user_id"],
                    **(request_payload or {}),
                },
            ) as response:
                response.raise_for_status()

                start_line = self._format_dify_progress(mode_tag, 5, "已启动，正在解析需求")
                emitted_progress.add(start_line)
                for rc in emit_reasoning_chunks(start_line):
                    yield rc

                for event in self._iter_sse_events(response):
                    event_type = str(event.get("event") or "").strip()
                    data = event.get("data") if isinstance(event.get("data"), dict) else {}

                    if event_type == "workflow_started":
                        stage_line = self._format_dify_progress(mode_tag, 10, "已连接 Dify Chatflow，开始执行节点")
                        if stage_line not in emitted_progress:
                            emitted_progress.add(stage_line)
                            for rc in emit_reasoning_chunks(stage_line):
                                yield rc
                        continue

                    if event_type == "node_finished":
                        node_id = str(data.get("node_id") or "").strip()
                        if not node_id or node_id in finished_nodes:
                            continue
                        finished_nodes.add(node_id)
                        stage_label = WORKFLOW_NODE_LABELS.get(node_id) or str(data.get("title") or node_id)
                        percent = self._workflow_progress_percent(len(finished_nodes))
                        stage_line = self._format_dify_progress(mode_tag, percent, "%s 完成" % stage_label)
                        if stage_line not in emitted_progress:
                            emitted_progress.add(stage_line)
                            for rc in emit_reasoning_chunks(stage_line):
                                yield rc
                        continue

                    if event_type in {"message", "agent_message", "text_chunk"}:
                        answer_text = self._extract_workflow_answer(event)
                        if not answer_text:
                            continue
                        if buffered_answer_mode:
                            answer_chunks.append(answer_text)
                            continue
                        if not answer_started:
                            answer_started = True
                            final_stage = self._format_dify_progress(mode_tag, 100, "执行完成，正在整理最终结果")
                            if final_stage not in emitted_progress:
                                emitted_progress.add(final_stage)
                                for rc in emit_reasoning_chunks(final_stage):
                                    yield rc
                            close_chunk = close_reasoning_chunk()
                            if close_chunk is not None:
                                yield close_chunk
                        answer_chunks.append(answer_text)
                        yield emit_text_chunk(answer_text)
                        continue

                    if event_type == "workflow_finished":
                        final_answer = self._extract_workflow_answer(event)
                        if buffered_answer_mode:
                            buffered_answer = final_answer or "".join(answer_chunks).strip()
                            prepared_answer, _payload_comment = self._prepare_workflow_answer_parts(buffered_answer) if buffered_answer else ("", None)
                            if prepared_answer and not answer_started:
                                answer_started = True
                                final_stage = self._format_dify_progress(mode_tag, 100, "执行完成，正在整理最终结果")
                                if final_stage not in emitted_progress:
                                    emitted_progress.add(final_stage)
                                    for rc in emit_reasoning_chunks(final_stage):
                                        yield rc
                                close_chunk = close_reasoning_chunk()
                                if close_chunk is not None:
                                    yield close_chunk
                                if prepared_answer:
                                    for text_chunk in self._split_text(prepared_answer):
                                        answer_chunks.append(text_chunk)
                                        yield emit_text_chunk(text_chunk)
                            continue
                        if final_answer and not answer_started:
                            answer_started = True
                            final_stage = self._format_dify_progress(mode_tag, 100, "执行完成，正在整理最终结果")
                            if final_stage not in emitted_progress:
                                emitted_progress.add(final_stage)
                                for rc in emit_reasoning_chunks(final_stage):
                                    yield rc
                            close_chunk = close_reasoning_chunk()
                            if close_chunk is not None:
                                yield close_chunk
                            for text_chunk in self._split_text(final_answer):
                                answer_chunks.append(text_chunk)
                                yield emit_text_chunk(text_chunk)
                        continue

                    if event_type == "error":
                        error_text = self._extract_workflow_error(event)
                        raise RuntimeError(error_text or "Dify Chatflow 返回错误事件。")

                if not answer_started:
                    raw_answer = "".join(answer_chunks).strip() or "执行已完成，但未返回可展示的结果。"
                    final_answer, _payload_comment = self._prepare_workflow_answer_parts(raw_answer) if buffered_answer_mode else (raw_answer, None)
                    final_stage = self._format_dify_progress(mode_tag, 100, "执行完成，正在整理最终结果")
                    if final_stage not in emitted_progress:
                        emitted_progress.add(final_stage)
                        for rc in emit_reasoning_chunks(final_stage):
                            yield rc
                    close_chunk = close_reasoning_chunk()
                    if close_chunk is not None:
                        yield close_chunk
                    for text_chunk in self._split_text(final_answer):
                        yield emit_text_chunk(text_chunk)

        except requests.RequestException as exc:
            if not answer_started:
                self._refund_billing_event(
                    billing_context=billing_context,
                    charge=flow_charge,
                    description=refund_description,
                    meta={"mode": "%s_stream" % mode_tag, "error": str(exc)[:500]},
                )
            close_chunk = close_reasoning_chunk()
            if close_chunk is not None:
                yield close_chunk
            detail = self._error_text(str(exc))
            yield emit_text_chunk("\n" + detail)
        except RuntimeError as exc:
            if not answer_started:
                self._refund_billing_event(
                    billing_context=billing_context,
                    charge=flow_charge,
                    description=refund_description,
                    meta={"mode": "%s_stream" % mode_tag, "error": str(exc)[:500]},
                )
            close_chunk = close_reasoning_chunk()
            if close_chunk is not None:
                yield close_chunk
            yield emit_text_chunk("\n" + self._error_text(str(exc)))

        close_chunk = close_reasoning_chunk()
        if close_chunk is not None:
            yield close_chunk
        yield self._stream_stop_chunk(response_id=response_id, created=created, model=model)
        yield b"data: [DONE]\n\n"

    def _iter_sse_events(self, response: requests.Response) -> Iterator[dict]:
        for raw_line in response.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue

            line = raw_line.strip()
            if not line or not line.startswith("data:"):
                continue

            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue

            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue

            if isinstance(event, dict):
                yield event

    def _extract_openai_stream_delta_text(self, payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""

        choices = payload.get("choices")
        if not isinstance(choices, list):
            return ""

        parts: List[str] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
        return "".join(parts)

    def _format_agent_progress(self, description: str) -> str:
        return "⏳ /agent · %s\n" % description

    def _workflow_progress_percent(self, finished_count: int) -> int:
        percent = 8 + int((finished_count * 88) / max(1, WORKFLOW_ESTIMATED_STEPS))
        return max(8, min(98, percent))

    def _format_dify_progress(self, mode_tag: str, percent: int, description: str) -> str:
        total_slots = 10
        filled = max(0, min(total_slots, round((percent / 100) * total_slots)))
        bar = "#" * filled + "." * (total_slots - filled)
        return "⏳ /%s 进度 [%s] %d%% · %s\n" % (mode_tag, bar, percent, description)

    def _extract_workflow_answer(self, payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""

        for key in ("answer", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for key in ("answer", "text", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value

        outputs = data.get("outputs") if isinstance(data.get("outputs"), dict) else {}
        for key in ("answer", "text", "result", "output"):
            value = outputs.get(key)
            if isinstance(value, str) and value.strip():
                return value

        for value in outputs.values():
            if isinstance(value, str) and value.strip():
                return value

        return ""

    def _extract_workflow_error(self, payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""

        for key in ("message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for key in ("error", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value

        return ""

    def _prepare_workflow_answer(self, answer_text: str) -> str:
        rendered, _payload_comment = self._prepare_workflow_answer_parts(answer_text)
        return rendered

    def _prepare_workflow_answer_parts(self, answer_text: str) -> Tuple[str, Optional[str]]:
        visible_text, payload = self._extract_structured_workflow_payload(answer_text)
        if not payload:
            return answer_text, None

        payload = self._augment_asin_history_payload(payload)
        payload = self._augment_selection_report_payload(payload, fallback_summary=visible_text)
        rendered = self._render_structured_workflow_payload(payload, fallback_summary=visible_text)
        payload_comment = self._build_structured_payload_comment(payload)
        if rendered:
            return rendered.rstrip(), payload_comment
        if visible_text:
            return visible_text.rstrip(), payload_comment
        return "", payload_comment

    def _extract_structured_workflow_payload(self, answer_text: str) -> Tuple[str, Optional[dict]]:
        text = str(answer_text or "")
        start = text.find(STRUCTURED_PAYLOAD_START)
        if start == -1:
            return text, None

        end = text.find(STRUCTURED_PAYLOAD_END, start)
        if end == -1:
            return text, None

        block_start = text.find("\n", start)
        if block_start == -1:
            return text, None

        payload_text = text[block_start:end].strip()
        visible_text = (text[:start] + text[end + len(STRUCTURED_PAYLOAD_END) :]).strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return visible_text or text, None
        if not isinstance(payload, dict):
            return visible_text or text, None
        return visible_text or text[:start].strip(), payload

    def _build_structured_payload_comment(self, payload: dict) -> str:
        return "%s\n%s\n%s" % (
            STRUCTURED_PAYLOAD_START,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            STRUCTURED_PAYLOAD_END,
        )

    def _augment_selection_report_payload(self, payload: dict, fallback_summary: str = "") -> dict:
        if not isinstance(payload, dict):
            return payload
        if self._is_standard_report_payload(payload):
            return payload
        if not self._is_selection_report_payload(payload):
            return payload

        augmented = deepcopy(payload)
        bundle = self._build_selection_report_bundle(augmented, fallback_summary=fallback_summary)
        if not bundle:
            return augmented

        decision_markdown = str(bundle.get("decision_markdown") or "").strip()
        report_payload = bundle.get("report_payload") or {}
        augmented["schema_version"] = str(report_payload.get("schema_version") or "xm.selection-report.v1")
        augmented["decision_debug"] = bundle.get("decision_debug") or {}
        augmented["report_payload"] = report_payload
        augmented["render_profiles"] = ["full_report", "executive_brief", "chat_compact", "dashboard_cards"]
        augmented["section_registry"] = {
            "overview": report_payload.get("overview") or {},
            "decision": report_payload.get("decision") or {},
            "market_attractiveness": report_payload.get("market_attractiveness") or {},
            "competition_structure": report_payload.get("competition_structure") or {},
            "opportunity_drivers": report_payload.get("opportunity_drivers") or {},
            "asin_case_evidence": report_payload.get("asin_case_evidence") or {},
            "risks_and_counterevidence": report_payload.get("risks_and_counterevidence") or {},
            "recommended_actions": report_payload.get("recommended_actions") or {},
            "data_boundary": report_payload.get("data_boundary") or {},
        }
        if decision_markdown:
            augmented["selection_decision_markdown"] = decision_markdown
        return augmented

    def _augment_asin_history_payload(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return payload

        raw = self._ensure_structured_dict(payload.get("raw_endpoint_results"))
        asin_history_payload = self._ensure_structured_dict(raw.get("asin_history_timeseries"))
        if not asin_history_payload:
            return payload

        augmented = deepcopy(payload)
        data_tables = augmented.get("data_tables") if isinstance(augmented.get("data_tables"), list) else []
        chart_intents = augmented.get("chart_intents") if isinstance(augmented.get("chart_intents"), list) else []

        asin_history_tables = self._build_asin_history_data_tables(asin_history_payload)
        asin_history_intents = self._build_asin_history_chart_intents(asin_history_tables)
        augmented["data_tables"] = self._merge_structured_tables(data_tables, asin_history_tables)
        augmented["chart_intents"] = self._merge_chart_intents(chart_intents, asin_history_intents)
        return augmented

    def _is_selection_report_payload(self, payload: dict) -> bool:
        coverage_status = payload.get("coverage_status")
        data_tables = payload.get("data_tables")
        if not isinstance(coverage_status, dict) and not isinstance(data_tables, list):
            return False

        table_ids = set()
        for table in data_tables or []:
            if not isinstance(table, dict):
                continue
            table_id = str(table.get("table_id") or "").strip()
            if table_id:
                table_ids.add(table_id)

        expected_table_ids = {
            "forecast_top_asins",
            "top_asin_forecast_compare",
            "top_asin_drilldown_forecast",
            "candidate_vs_benchmark",
            "forecast_driver_distribution",
            "weak_signal_top_asins",
        }
        if table_ids & expected_table_ids:
            return True

        chart_intents = payload.get("chart_intents")
        if isinstance(chart_intents, list):
            for intent in chart_intents:
                if not isinstance(intent, dict):
                    continue
                intent_id = str(intent.get("intent_id") or "").strip()
                if intent_id in {
                    "forecast_top_asins_sales",
                    "top_asin_w1_w4_compare",
                    "candidate_vs_benchmark_compare",
                }:
                    return True

        return any(
            key in (coverage_status or {})
            for key in ["overall_status", "forecast_status", "coverage_ratio", "forecast_type", "candidate_asin_count"]
        )

    def _is_standard_report_payload(self, payload: dict) -> bool:
        if not isinstance(payload, dict):
            return False
        source_context = self._ensure_structured_dict(payload.get("source_context"))
        report_profile = str(source_context.get("report_profile") or payload.get("report_profile") or "").strip().lower()
        if report_profile != "standard":
            return False
        channel = str(source_context.get("channel") or payload.get("channel") or payload.get("producer") or "").strip().lower()
        return channel in {"", "workflow", "report"}

    def _standard_report_chart_payload(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return payload

        allowed_chart_ids = {"candidate_vs_benchmark_compare"}
        suppressed_table_ids = {"forecast_top_asins", "top_asin_drilldown_forecast"}
        filtered = deepcopy(payload)

        chart_specs = filtered.get("chart_specs")
        if isinstance(chart_specs, list):
            filtered["chart_specs"] = [
                spec
                for spec in chart_specs
                if isinstance(spec, dict) and str(spec.get("chart_id") or "").strip() in allowed_chart_ids
            ]

        data_tables = filtered.get("data_tables")
        if isinstance(data_tables, list):
            filtered["data_tables"] = [
                table
                for table in data_tables
                if not (
                    isinstance(table, dict)
                    and str(table.get("table_id") or "").strip() in suppressed_table_ids
                )
            ]

        return filtered

    def _build_selection_report_bundle(self, payload: dict, fallback_summary: str = "") -> Optional[dict]:
        features = self._extract_selection_report_features(payload)
        if not self._selection_features_have_signal(features):
            return None

        scores = {
            "demand_strength_score": self._score_selection_demand_strength(features),
            "competition_headroom_score": self._score_selection_competition_headroom(features),
            "opportunity_driver_score": self._score_selection_opportunity_driver(features),
            "risk_severity_score": self._score_selection_risk_severity(features),
            "evidence_completeness_score": self._score_selection_evidence_completeness(features),
        }
        scores["cross_endpoint_consistency_score"] = self._score_selection_cross_endpoint_consistency(features)
        scores["coverage_quality_score"] = self._score_selection_coverage_quality(features)
        scores["chart_support_ratio_score"] = self._score_selection_chart_support_ratio(features)

        recommendation_level, recommendation_score, gates = self._compute_selection_recommendation(scores, features)
        confidence_level, confidence_score = self._compute_selection_confidence(scores)
        primary_chart_intent_id = self._pick_selection_primary_chart_intent_id(features)
        core_reasons = self._build_selection_core_reasons(
            features,
            scores,
            recommendation_level,
            confidence_level,
        )
        decision_basis = list(core_reasons)
        blocking_risk = self._build_selection_blocking_risk(features, scores, gates)
        validation_focus = self._build_selection_validation_focus(features, scores)
        next_actions = self._build_selection_next_actions(recommendation_level, features, scores)
        summary_markdown = str(payload.get("summary_markdown") or fallback_summary or "").strip()

        report_payload = {
            "schema_version": "xm.selection-report.v1",
            "report_meta": self._build_selection_report_meta(payload),
            "overview": {
                "recommendation_level": recommendation_level,
                "confidence_level": confidence_level,
                "core_reasons": core_reasons,
                "summary_markdown": summary_markdown,
            },
            "decision": {
                "judgement_summary": "当前建议：%s；信心：%s。" % (recommendation_level, confidence_level),
                "decision_basis": decision_basis,
                "blocking_risk": blocking_risk,
                "primary_chart_intent_id": primary_chart_intent_id,
            },
            "market_attractiveness": {
                "predicted_sales_w4_total": features.get("predicted_sales_w4_total"),
                "top20_predicted_sales_w4_share": features.get("top20_predicted_sales_w4_share"),
                "top_asin_count_with_forecast": features.get("covered_asin_count"),
                "trend_stage": features.get("trend_stage"),
                "trend_wow": features.get("trend_wow"),
                "keyword_coverage_ratio": features.get("keyword_coverage_ratio"),
            },
            "competition_structure": {
                "candidate_vs_benchmark_rows": features.get("candidate_vs_benchmark_rows") or [],
                "avg_price_gap": features.get("avg_price_gap"),
                "avg_review_gap": features.get("avg_review_gap"),
                "avg_monthly_sold_gap": features.get("avg_monthly_sold_gap"),
                "median_offer_gap": features.get("median_offer_gap"),
            },
            "opportunity_drivers": {
                "forecast_type": features.get("forecast_type"),
                "driver_distribution_rows": features.get("forecast_driver_rows") or [],
                "weak_signal_rows": features.get("weak_signal_rows") or [],
            },
            "asin_case_evidence": self._build_selection_asin_case_evidence(payload),
            "risks_and_counterevidence": {
                "coverage_status": payload.get("coverage_status") or {},
                "risk_flags": features.get("risk_flags") or [],
                "missing_asin_count": features.get("missing_asin_count"),
                "sales_forecast_meta_status": features.get("top_asin_status"),
                "suppressed_chart_ids": self._collect_selection_suppressed_chart_ids(payload),
                "suppression_reasons": self._collect_selection_suppression_reasons(payload),
                "summary_paragraph": blocking_risk,
            },
            "recommended_actions": {
                "validation_focus": validation_focus,
                "next_actions": next_actions,
                "stop_condition": self._build_selection_stop_condition(recommendation_level, features, scores),
                "revisit_condition": self._build_selection_revisit_condition(features),
            },
            "data_boundary": {
                "data_sources": self._build_selection_data_sources(payload),
                "forecast_type": features.get("forecast_type"),
                "coverage_ratio": features.get("coverage_ratio"),
                "window_days": self._build_selection_window_days(payload),
                "suppressed_charts": self._build_selection_suppressed_charts(payload),
                "data_notes": self._build_selection_data_notes(payload, features),
            },
            "support_matrix": {
                "overview": self._selection_support_level(recommendation_score),
                "decision": self._selection_support_level(confidence_score),
                "market_attractiveness": self._selection_support_level(scores["demand_strength_score"]),
                "competition_structure": self._selection_support_level(scores["competition_headroom_score"]),
                "opportunity_drivers": self._selection_support_level(scores["opportunity_driver_score"]),
                "risks_and_counterevidence": self._selection_support_level(100.0 - scores["risk_severity_score"]),
                "recommended_actions": self._selection_support_level(scores["evidence_completeness_score"]),
                "data_boundary": self._selection_support_level(scores["coverage_quality_score"]),
            },
            "notes": [],
        }
        decision_debug = {
            "scores": scores,
            "recommendation_score": recommendation_score,
            "confidence_score": confidence_score,
            "gates": gates,
            "features": self._build_selection_debug_features(features),
        }
        return {
            "decision_debug": decision_debug,
            "report_payload": report_payload,
            "decision_markdown": self._render_selection_decision_markdown(
                recommendation_level=recommendation_level,
                recommendation_score=recommendation_score,
                confidence_level=confidence_level,
                confidence_score=confidence_score,
                core_reasons=core_reasons,
                validation_focus=validation_focus,
                next_actions=next_actions,
                blocking_risk=blocking_risk,
            ),
        }

    def _selection_features_have_signal(self, features: dict) -> bool:
        return any(
            [
                features.get("forecast_top_asins_rows"),
                features.get("top_asin_forecast_compare_rows"),
                features.get("candidate_vs_benchmark_rows"),
                features.get("forecast_driver_rows"),
                features.get("weak_signal_rows"),
                features.get("overall_status"),
                features.get("coverage_ratio"),
            ]
        )

    def _extract_selection_report_features(self, payload: dict) -> dict:
        raw = self._ensure_structured_dict(payload.get("raw_endpoint_results"))
        coverage = self._ensure_structured_dict(payload.get("coverage_status"))
        data_tables = payload.get("data_tables") if isinstance(payload.get("data_tables"), list) else []
        chart_intents = payload.get("chart_intents") if isinstance(payload.get("chart_intents"), list) else []

        candidate_pool_stats = self._ensure_structured_dict(raw.get("candidate_pool_stats"))
        sales_forecast = self._ensure_structured_dict(candidate_pool_stats.get("sales_forecast"))
        trends = self._ensure_structured_dict(raw.get("candidate_pool_trends"))
        weak_forecast = self._ensure_structured_dict(raw.get("candidate_pool_weak_forecast"))
        drilldown = self._ensure_structured_dict(raw.get("top_asin_drilldown"))
        drilldown_meta = self._ensure_structured_dict(drilldown.get("sales_forecast_meta"))

        forecast_top_asins_rows = self._selection_table_rows(data_tables, "forecast_top_asins")
        top_asin_forecast_compare_rows = self._selection_table_rows(data_tables, "top_asin_forecast_compare")
        if not top_asin_forecast_compare_rows:
            top_asin_forecast_compare_rows = self._selection_table_rows(data_tables, "top_asin_drilldown_forecast")
        candidate_vs_benchmark_rows = self._selection_table_rows(data_tables, "candidate_vs_benchmark")
        forecast_driver_rows = self._selection_table_rows(data_tables, "forecast_driver_distribution")
        weak_signal_rows = self._selection_table_rows(data_tables, "weak_signal_top_asins")

        predicted_sales_w4_total = self._selection_first_number(
            coverage.get("predicted_sales_w4_total"),
            sales_forecast.get("predicted_sales_w4_total"),
            self._selection_sum_field(forecast_top_asins_rows, ["predicted_weekly_sales_w4", "预测周销量W4"]),
        )
        top20_predicted_sales_w4_share = self._selection_first_number(
            coverage.get("top20_predicted_sales_w4_share"),
            sales_forecast.get("top20_predicted_sales_w4_share"),
        )
        trend_stage = str(
            coverage.get("trend_stage")
            or trends.get("trend_stage")
            or ""
        ).strip()
        trend_wow = self._selection_first_number(coverage.get("trend_wow"), trends.get("trend_wow"))
        keyword_coverage_ratio = self._selection_first_number(
            coverage.get("keyword_coverage_ratio"),
            trends.get("keyword_coverage_ratio"),
        )
        covered_asin_count = int(
            self._selection_first_number(
                coverage.get("covered_asin_count"),
                sales_forecast.get("covered_asin_count"),
                len(forecast_top_asins_rows) or None,
            )
            or 0
        )
        candidate_asin_count = int(
            self._selection_first_number(
                coverage.get("candidate_asin_count"),
                sales_forecast.get("candidate_asin_count"),
                covered_asin_count or None,
            )
            or 0
        )
        missing_asin_count = int(
            self._selection_first_number(
                coverage.get("missing_asin_count"),
                sales_forecast.get("missing_asin_count"),
                max(candidate_asin_count - covered_asin_count, 0) if candidate_asin_count else None,
            )
            or 0
        )

        return {
            "forecast_type": str(coverage.get("forecast_type") or weak_forecast.get("forecast_type") or "none").strip(),
            "overall_status": str(coverage.get("overall_status") or coverage.get("candidate_pool") or coverage.get("forecast_status") or "").strip(),
            "forecast_status": str(coverage.get("forecast_status") or coverage.get("candidate_pool") or sales_forecast.get("status") or "").strip(),
            "top_asin_status": str(coverage.get("top_asin_status") or coverage.get("top_asin") or drilldown_meta.get("status") or "").strip(),
            "coverage_ratio": self._selection_first_number(coverage.get("coverage_ratio"), sales_forecast.get("coverage_ratio")) or 0.0,
            "candidate_asin_count": candidate_asin_count,
            "covered_asin_count": covered_asin_count,
            "missing_asin_count": missing_asin_count,
            "predicted_sales_w4_total": predicted_sales_w4_total,
            "top20_predicted_sales_w4_share": top20_predicted_sales_w4_share,
            "trend_stage": trend_stage,
            "trend_wow": trend_wow,
            "keyword_coverage_ratio": keyword_coverage_ratio,
            "risk_flags": self._ensure_structured_list(weak_forecast.get("risk_flags") or coverage.get("risk_flags")),
            "forecast_top_asins_rows": forecast_top_asins_rows,
            "top_asin_forecast_compare_rows": top_asin_forecast_compare_rows,
            "candidate_vs_benchmark_rows": candidate_vs_benchmark_rows,
            "forecast_driver_rows": forecast_driver_rows,
            "weak_signal_rows": weak_signal_rows,
            "has_forecast_top_asins_sales": self._selection_intent_ready(
                chart_intents,
                "forecast_top_asins_sales",
                fallback_rows=forecast_top_asins_rows,
                value_fields=["predicted_weekly_sales_w4", "预测周销量W4"],
                min_rows=3,
            ),
            "has_top_asin_w1_w4_compare": self._selection_intent_ready(
                chart_intents,
                "top_asin_w1_w4_compare",
                fallback_rows=top_asin_forecast_compare_rows,
                value_fields=["predicted_weekly_sales_w1", "predicted_weekly_sales_w4", "预测周销量W1", "预测周销量W4"],
                min_rows=2,
            ),
            "has_candidate_vs_benchmark_compare": self._selection_intent_ready(
                chart_intents,
                "candidate_vs_benchmark_compare",
                fallback_rows=candidate_vs_benchmark_rows,
                value_fields=["gap_pct"],
                min_rows=3,
            ),
            "has_forecast_top_asins_growth": self._selection_intent_ready(
                chart_intents,
                "forecast_top_asins_growth",
                fallback_rows=forecast_top_asins_rows,
                value_fields=["predicted_growth_delta_w4_minus_w1", "W4-W1 增量"],
                min_rows=2,
            ),
            "has_forecast_driver_distribution": self._selection_intent_ready(
                chart_intents,
                "forecast_driver_distribution",
                fallback_rows=forecast_driver_rows,
                value_fields=["driver_share", "share_pct", "占比"],
                min_rows=2,
            ),
            "has_weak_signal_score_rank": self._selection_intent_ready(
                chart_intents,
                "weak_signal_score_rank",
                fallback_rows=weak_signal_rows,
                value_fields=["weak_signal_score", "score", "弱信号分数"],
                min_rows=2,
            ),
            "has_weak_signal_momentum_compare": self._selection_intent_ready(
                chart_intents,
                "weak_signal_momentum_compare",
                fallback_rows=weak_signal_rows,
                value_fields=["momentum", "momentum_score", "动量"],
                min_rows=2,
            ),
            "suppressed_primary_chart": self._selection_intent_suppressed(chart_intents, "forecast_top_asins_sales")
            and self._selection_intent_suppressed(chart_intents, "top_asin_w1_w4_compare")
            and self._selection_intent_suppressed(chart_intents, "candidate_vs_benchmark_compare"),
            "primary_chart_suppression_reason": self._selection_intent_suppression_reason(chart_intents, "forecast_top_asins_sales")
            or self._selection_intent_suppression_reason(chart_intents, "top_asin_w1_w4_compare")
            or self._selection_intent_suppression_reason(chart_intents, "candidate_vs_benchmark_compare"),
            "avg_price_gap": self._selection_pick_metric_gap(candidate_vs_benchmark_rows, "avg_price"),
            "avg_review_gap": self._selection_pick_metric_gap(candidate_vs_benchmark_rows, "avg_review_count"),
            "avg_monthly_sold_gap": self._selection_pick_metric_gap(candidate_vs_benchmark_rows, "avg_monthly_sold"),
            "median_offer_gap": self._selection_pick_metric_gap(candidate_vs_benchmark_rows, "median_offer_count"),
        }

    def _ensure_structured_dict(self, value: Any) -> dict:
        return value if isinstance(value, dict) else {}

    def _ensure_structured_list(self, value: Any) -> list:
        return value if isinstance(value, list) else []

    def _selection_table_rows(self, data_tables: list, table_id: str) -> List[dict]:
        for table in data_tables:
            if not isinstance(table, dict):
                continue
            if str(table.get("table_id") or "").strip() != table_id:
                continue
            return self._structured_table_rows_as_dicts(table)
        return []

    def _selection_first_number(self, *values: Any) -> Optional[float]:
        for value in values:
            number = self._safe_structured_number(value)
            if number is not None:
                return number
        return None

    def _selection_sum_field(self, rows: List[dict], field_names: List[str]) -> Optional[float]:
        total = 0.0
        found = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field_name in field_names:
                number = self._safe_structured_number(row.get(field_name))
                if number is None:
                    continue
                total += number
                found = True
                break
        if not found:
            return None
        return round(total, 2)

    def _selection_intent_by_id(self, chart_intents: list, intent_id: str) -> dict:
        for intent in chart_intents:
            if not isinstance(intent, dict):
                continue
            if str(intent.get("intent_id") or "").strip() == intent_id:
                return intent
        return {}

    def _selection_intent_ready(
        self,
        chart_intents: list,
        intent_id: str,
        *,
        fallback_rows: Optional[List[dict]] = None,
        value_fields: Optional[List[str]] = None,
        min_rows: int = 1,
    ) -> bool:
        intent = self._selection_intent_by_id(chart_intents, intent_id)
        if intent and str(intent.get("status") or "").strip() != "ready":
            return False
        rows = fallback_rows or []
        if intent and not rows and not value_fields:
            return True
        if len(rows) < min_rows:
            return False
        if not value_fields:
            return True
        return self._selection_count_non_zero(rows, value_fields) >= min_rows

    def _selection_intent_suppressed(self, chart_intents: list, intent_id: str) -> bool:
        intent = self._selection_intent_by_id(chart_intents, intent_id)
        return str(intent.get("status") or "").strip() == "suppressed"

    def _selection_intent_suppression_reason(self, chart_intents: list, intent_id: str) -> str:
        intent = self._selection_intent_by_id(chart_intents, intent_id)
        return str(intent.get("suppression_reason") or "").strip()

    def _selection_count_non_zero(self, rows: List[dict], field_names: List[str]) -> int:
        count = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field_name in field_names:
                number = self._safe_structured_number(row.get(field_name))
                if number is not None and abs(number) > 1e-9:
                    count += 1
                    break
        return count

    def _selection_count_distinct_non_zero(self, rows: List[dict], field_names: List[str]) -> int:
        values = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field_name in field_names:
                number = self._safe_structured_number(row.get(field_name))
                if number is not None and abs(number) > 1e-9:
                    values.add(round(number, 4))
                    break
        return len(values)

    def _selection_pick_metric_gap(self, rows: List[dict], metric_name: str) -> Optional[float]:
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("metric") or "").strip() != metric_name:
                continue
            return self._safe_structured_number(row.get("gap_pct"))
        return None

    def _selection_clamp(self, number: float, low: float = 0.0, high: float = 100.0) -> float:
        if number < low:
            return low
        if number > high:
            return high
        return number

    def _score_selection_demand_strength(self, features: dict) -> float:
        score = 50.0
        if not features.get("has_forecast_top_asins_sales"):
            score = min(score, 45.0)
        else:
            score += 15.0

        covered_asin_count = int(features.get("covered_asin_count") or 0)
        if covered_asin_count >= 5:
            score += 10.0
        elif covered_asin_count < 3:
            score -= 15.0

        if features.get("has_top_asin_w1_w4_compare"):
            score += 10.0

        top_share = features.get("top20_predicted_sales_w4_share")
        if top_share is not None:
            if top_share >= 0.85:
                score -= 10.0
            elif top_share <= 0.55:
                score += 5.0

        trend_stage = str(features.get("trend_stage") or "")
        if trend_stage == "rising":
            score += 5.0
        elif trend_stage == "cooling":
            score -= 5.0
        return round(self._selection_clamp(score), 2)

    def _score_selection_competition_headroom(self, features: dict) -> float:
        score = 50.0
        if not features.get("has_candidate_vs_benchmark_compare"):
            return 45.0

        review_gap = features.get("avg_review_gap")
        offer_gap = features.get("median_offer_gap")
        price_gap = features.get("avg_price_gap")

        if review_gap is not None and review_gap > 0.35:
            score -= 12.0
        elif review_gap is not None and review_gap < -0.15:
            score += 8.0

        if offer_gap is not None and offer_gap > 0.25:
            score -= 12.0
        elif offer_gap is not None and offer_gap < -0.1:
            score += 6.0

        if price_gap is not None and -0.12 <= price_gap <= 0.18:
            score += 4.0
        elif price_gap is not None and price_gap > 0.4:
            score -= 6.0
        return round(self._selection_clamp(score), 2)

    def _score_selection_opportunity_driver(self, features: dict) -> float:
        score = 45.0
        forecast_type = str(features.get("forecast_type") or "none")
        if features.get("has_forecast_top_asins_growth"):
            score += 18.0
        if features.get("has_forecast_driver_distribution"):
            score += 15.0
        if features.get("has_weak_signal_score_rank"):
            score += 8.0
        if features.get("has_weak_signal_momentum_compare"):
            score += 6.0
        if forecast_type.startswith("heuristic") and not features.get("has_forecast_top_asins_growth"):
            score = min(score, 55.0)
        if len(self._ensure_structured_list(features.get("forecast_driver_rows"))) >= 2:
            score += 5.0
        return round(self._selection_clamp(score), 2)

    def _score_selection_risk_severity(self, features: dict) -> float:
        score = 30.0
        overall_status = str(features.get("overall_status") or "")
        forecast_status = str(features.get("forecast_status") or "")
        top_asin_status = str(features.get("top_asin_status") or "")

        if overall_status == "missing_domain_model" or forecast_status == "missing_domain_model" or top_asin_status == "missing_domain_model":
            score = max(score, 75.0)
        if overall_status == "unavailable" or forecast_status == "unavailable":
            score = max(score, 80.0)
        if forecast_status == "partial_coverage":
            score += min(20.0, float(features.get("missing_asin_count") or 0) * 2.0)
        if features.get("suppressed_primary_chart"):
            score += 10.0
        if features.get("forecast_type") == "heuristic_v1":
            score += 12.0
        risk_flags = self._ensure_structured_list(features.get("risk_flags"))
        score += min(12.0, float(len(risk_flags)) * 4.0)
        return round(self._selection_clamp(score), 2)

    def _score_selection_evidence_completeness(self, features: dict) -> float:
        score = 20.0
        if features.get("has_forecast_top_asins_sales"):
            score += 25.0
        if features.get("has_candidate_vs_benchmark_compare"):
            score += 20.0
        if features.get("has_top_asin_w1_w4_compare"):
            score += 20.0
        if float(features.get("coverage_ratio") or 0.0) >= 0.7:
            score += 10.0
        if features.get("overall_status") in {"ready", "partial_coverage"}:
            score += 10.0
        if features.get("forecast_type") == "heuristic_v1" and not features.get("has_forecast_top_asins_sales"):
            score = min(score, 45.0)
        return round(self._selection_clamp(score), 2)

    def _score_selection_cross_endpoint_consistency(self, features: dict) -> float:
        score = 60.0
        trend_stage = str(features.get("trend_stage") or "")
        if features.get("has_forecast_top_asins_sales") and features.get("has_top_asin_w1_w4_compare"):
            score += 15.0
        if features.get("has_candidate_vs_benchmark_compare") and (
            features.get("avg_review_gap") is not None or features.get("avg_monthly_sold_gap") is not None
        ):
            score += 10.0
        if trend_stage == "rising" and features.get("has_forecast_top_asins_sales"):
            score += 8.0
        elif trend_stage == "cooling" and features.get("has_forecast_top_asins_sales"):
            score -= 8.0
        if features.get("forecast_type") == "heuristic_v1" and features.get("has_forecast_top_asins_sales"):
            score -= 10.0
        return round(self._selection_clamp(score), 2)

    def _score_selection_coverage_quality(self, features: dict) -> float:
        score = 50.0
        overall_status = str(features.get("overall_status") or "")
        coverage_ratio = float(features.get("coverage_ratio") or 0.0)
        if overall_status == "ready":
            score = 80.0
        elif overall_status == "partial_coverage":
            score = 65.0
        elif overall_status in {"missing_domain_model", "unavailable"}:
            score = 35.0
        score += min(10.0, coverage_ratio * 10.0)
        return round(self._selection_clamp(score), 2)

    def _score_selection_chart_support_ratio(self, features: dict) -> float:
        ready_count = 0
        for key in [
            "has_forecast_top_asins_sales",
            "has_candidate_vs_benchmark_compare",
            "has_forecast_driver_distribution",
        ]:
            if features.get(key):
                ready_count += 1
        if ready_count == 3:
            return 90.0
        if ready_count == 2:
            return 70.0
        if ready_count == 1:
            return 50.0
        if features.get("has_weak_signal_score_rank") or features.get("has_weak_signal_momentum_compare"):
            return 35.0
        return 20.0

    def _compute_selection_recommendation(self, scores: dict, features: dict) -> Tuple[str, float, dict]:
        gates = {
            "block_enter_by_low_evidence": scores["evidence_completeness_score"] < 50.0,
            "block_enter_by_heuristic_only": features.get("forecast_type") == "heuristic_v1" and not features.get("has_forecast_top_asins_sales"),
            "block_enter_by_high_risk": scores["risk_severity_score"] >= 75.0,
            "block_enter_by_low_headroom": scores["competition_headroom_score"] < 40.0,
            "block_enter_by_status": features.get("overall_status") in {"missing_domain_model", "unavailable"},
            "force_pause_by_extreme_risk": scores["risk_severity_score"] >= 85.0,
            "force_pause_by_weak_demand_and_driver": scores["demand_strength_score"] < 40.0 and scores["opportunity_driver_score"] < 45.0,
            "force_pause_by_market_lock": scores["competition_headroom_score"] < 35.0 and scores["risk_severity_score"] >= 70.0,
            "force_pause_by_no_real_evidence": features.get("forecast_type") == "heuristic_v1"
            and not features.get("has_candidate_vs_benchmark_compare")
            and features.get("suppressed_primary_chart"),
        }
        if any(
            [
                gates["force_pause_by_extreme_risk"],
                gates["force_pause_by_weak_demand_and_driver"],
                gates["force_pause_by_market_lock"],
                gates["force_pause_by_no_real_evidence"],
            ]
        ):
            return "暂缓进入", 0.0, gates

        recommendation_score = (
            0.30 * scores["demand_strength_score"]
            + 0.20 * scores["competition_headroom_score"]
            + 0.20 * scores["opportunity_driver_score"]
            + 0.15 * scores["evidence_completeness_score"]
            + 0.15 * (100.0 - scores["risk_severity_score"])
        )
        recommendation_score = round(self._selection_clamp(recommendation_score), 2)

        if recommendation_score >= 72.0 and not any(
            [
                gates["block_enter_by_low_evidence"],
                gates["block_enter_by_heuristic_only"],
                gates["block_enter_by_high_risk"],
                gates["block_enter_by_low_headroom"],
                gates["block_enter_by_status"],
            ]
        ):
            return "建议进入", recommendation_score, gates
        if recommendation_score >= 50.0:
            return "谨慎验证", recommendation_score, gates
        return "暂缓进入", recommendation_score, gates

    def _compute_selection_confidence(self, scores: dict) -> Tuple[str, float]:
        confidence_score = (
            0.35 * scores["evidence_completeness_score"]
            + 0.25 * scores["cross_endpoint_consistency_score"]
            + 0.25 * scores["coverage_quality_score"]
            + 0.15 * scores["chart_support_ratio_score"]
        )
        confidence_score = round(self._selection_clamp(confidence_score), 2)
        if confidence_score >= 75.0:
            return "高", confidence_score
        if confidence_score >= 55.0:
            return "中", confidence_score
        return "低", confidence_score

    def _pick_selection_primary_chart_intent_id(self, features: dict) -> str:
        if features.get("has_forecast_top_asins_sales"):
            return "forecast_top_asins_sales"
        if features.get("has_top_asin_w1_w4_compare"):
            return "top_asin_w1_w4_compare"
        if features.get("has_candidate_vs_benchmark_compare"):
            return "candidate_vs_benchmark_compare"
        return ""

    def _build_selection_core_reasons(
        self,
        features: dict,
        scores: dict,
        recommendation_level: str,
        confidence_level: str,
    ) -> List[str]:
        reasons: List[str] = []
        if features.get("has_forecast_top_asins_sales"):
            if scores["demand_strength_score"] >= 65.0:
                reasons.append("候选池已形成可比较的 Top ASIN 预测销量分化，需求侧信号偏强。")
            else:
                reasons.append("候选池已有预测销量证据，但需求强度仍未达到强进入阈值。")
        else:
            reasons.append("当前缺少稳定的 Top ASIN 预测销量主图，需求判断需要保持保守。")

        if features.get("has_candidate_vs_benchmark_compare"):
            review_gap = features.get("avg_review_gap")
            offer_gap = features.get("median_offer_gap")
            if (review_gap is not None and review_gap < -0.15) or (offer_gap is not None and offer_gap < -0.1):
                reasons.append("候选池在评论量或供给压力上未明显高于类目基准，仍有一定切入空间。")
            elif (review_gap is not None and review_gap > 0.35) or (offer_gap is not None and offer_gap > 0.25):
                reasons.append("候选池相对类目基准已经偏拥挤，竞争空间明显收窄。")
            else:
                reasons.append("候选池与类目基准处于可比区间，竞争结构需要结合更多证据判断。")
        else:
            reasons.append("当前缺少候选池与类目 benchmark 的对照图，竞争空间判断仍偏保守。")

        if recommendation_level == "暂缓进入":
            reasons.append("风险或证据完整度尚未过线，现阶段更适合先补证而不是直接进入。")
        elif confidence_level == "低":
            reasons.append("当前信心水位偏低，结论更适合作为方向筛选而不是直接立项。")
        else:
            reasons.append("覆盖率、图表支持度和多源一致性达到第一轮选品判断的可用水位。")
        return reasons[:3]

    def _build_selection_blocking_risk(self, features: dict, scores: dict, gates: dict) -> str:
        if gates.get("force_pause_by_extreme_risk"):
            return "当前风险分数过高，建议先暂停进入并排查数据覆盖与异常风险信号。"
        if gates.get("block_enter_by_status"):
            return "当前 domain 或预测状态不可用，结论不应当被当作进入依据。"
        if gates.get("block_enter_by_low_evidence"):
            return "证据完整度不足，当前更适合先补齐主图和关键样本后再判断。"
        if features.get("primary_chart_suppression_reason"):
            return "关键主图被抑制：%s" % features.get("primary_chart_suppression_reason")
        if scores.get("risk_severity_score", 0.0) >= 70.0:
            return "风险分数偏高，需要优先排查覆盖缺口、弱信号风险和启发式预测限制。"
        return "当前没有单一极端阻断项，但仍需按验证优先级逐步补证。"

    def _build_selection_validation_focus(self, features: dict, scores: dict) -> str:
        weakest = min(
            [
                ("需求强度", scores.get("demand_strength_score", 0.0)),
                ("竞争空间", scores.get("competition_headroom_score", 0.0)),
                ("机会驱动", scores.get("opportunity_driver_score", 0.0)),
                ("证据完整度", scores.get("evidence_completeness_score", 0.0)),
            ],
            key=lambda item: item[1],
        )[0]
        if weakest == "需求强度":
            return "优先验证头部 ASIN 的预测销量分化和趋势是否真实存在。"
        if weakest == "竞争空间":
            return "优先验证候选池相对类目基准的评论、供给和价格差距。"
        if weakest == "机会驱动":
            return "优先验证增长驱动和弱信号是否能形成可复制的切入点。"
        if float(features.get("coverage_ratio") or 0.0) < 0.7:
            return "优先补齐缺失 ASIN 和覆盖不足样本，再做更强结论。"
        return "优先补强主图支持和关键对照证据，再决定是否进入执行。"

    def _build_selection_next_actions(self, recommendation_level: str, features: dict, scores: dict) -> List[str]:
        if recommendation_level == "建议进入":
            return [
                "优先复核头部 ASIN 的增长驱动是否可复制到目标切口。",
                "继续验证评论和供给压力较低的细分方向，避免直接追头部同质化款。",
                "补充知识库或 /web 的规则与合规核验，降低执行阶段的不确定性。",
            ]
        if recommendation_level == "谨慎验证":
            return [
                "先补齐缺失样本或缺失主图，再重新评估 recommendation 和 confidence。",
                "重点复核类目 benchmark 差距以及 Top ASIN 的 W1/W4 变化。",
                "把当前结果视为筛选线索，不要直接进入重投入执行。",
            ]
        return [
            "先不要进入执行阶段，先定位阻断风险来自 coverage、竞争还是风险信号。",
            "如果当前主要依赖 heuristic 或缺少 benchmark 对照，先补证后再复评。",
            "若风险分数和竞争压力持续偏高，考虑切换商品方向。",
        ]

    def _build_selection_stop_condition(self, recommendation_level: str, features: dict, scores: dict) -> str:
        if recommendation_level == "建议进入":
            return "如果后续 benchmark 对照显示评论/供给压力显著高于类目，或风险分数升至 75 以上，则停止推进。"
        if recommendation_level == "谨慎验证":
            return "如果补证后 evidence 仍低于 50，或风险分数继续升高，则停止当前方向。"
        return "在 coverage、主图支持和 benchmark 对照仍未改善前，不进入执行阶段。"

    def _build_selection_revisit_condition(self, features: dict) -> str:
        if float(features.get("coverage_ratio") or 0.0) < 0.7:
            return "当 coverage_ratio 提升到 0.7 以上，并补齐主图证据后再复评。"
        if features.get("forecast_type") == "heuristic_v1":
            return "当可用 trained forecast 或真实 benchmark 对照补齐后再复评。"
        return "当趋势、benchmark 对照和头部 ASIN 下钻证据同时稳定后再复评。"

    def _build_selection_report_meta(self, payload: dict) -> dict:
        raw = self._ensure_structured_dict(payload.get("raw_endpoint_results"))
        source_context = self._ensure_structured_dict(payload.get("source_context"))
        product_query = (
            source_context.get("product_query")
            or payload.get("product_query")
            or payload.get("normalized_product_query")
            or ""
        )
        marketplace = source_context.get("marketplace") or payload.get("marketplace") or ""
        domain = source_context.get("domain") or payload.get("domain")
        candidate_asins = source_context.get("candidate_asins") or payload.get("candidate_asins") or []
        source_tools = [key for key, value in raw.items() if value]
        return {
            "product_query": product_query,
            "marketplace": marketplace,
            "domain": domain,
            "candidate_asins": candidate_asins if isinstance(candidate_asins, list) else [],
            "source_channel": source_context.get("channel") or payload.get("producer") or "report",
            "source_tools": sorted(source_tools),
        }

    def _build_selection_data_sources(self, payload: dict) -> List[str]:
        raw = self._ensure_structured_dict(payload.get("raw_endpoint_results"))
        sources = [key for key, value in raw.items() if value]
        if not sources and payload.get("data_tables"):
            sources.append("standardized_data_tables")
        return sorted(set(str(item) for item in sources if item))

    def _build_selection_window_days(self, payload: dict) -> Optional[int]:
        for key in ["window_days", "analysis_window_days"]:
            number = self._safe_structured_number(payload.get(key))
            if number is not None:
                return int(number)
        raw = self._ensure_structured_dict(payload.get("raw_endpoint_results"))
        for result in raw.values():
            if not isinstance(result, dict):
                continue
            number = self._safe_structured_number(result.get("window_days"))
            if number is not None:
                return int(number)
        return None

    def _collect_selection_suppressed_chart_ids(self, payload: dict) -> List[str]:
        chart_intents = payload.get("chart_intents") if isinstance(payload.get("chart_intents"), list) else []
        return [
            str(intent.get("intent_id") or "").strip()
            for intent in chart_intents
            if isinstance(intent, dict) and str(intent.get("status") or "").strip() == "suppressed"
        ]

    def _collect_selection_suppression_reasons(self, payload: dict) -> List[str]:
        chart_intents = payload.get("chart_intents") if isinstance(payload.get("chart_intents"), list) else []
        reasons: List[str] = []
        for intent in chart_intents:
            if not isinstance(intent, dict):
                continue
            if str(intent.get("status") or "").strip() != "suppressed":
                continue
            reason = str(intent.get("suppression_reason") or "").strip()
            if reason:
                reasons.append(reason)
        return reasons

    def _build_selection_suppressed_charts(self, payload: dict) -> List[dict]:
        chart_intents = payload.get("chart_intents") if isinstance(payload.get("chart_intents"), list) else []
        suppressed = []
        for intent in chart_intents:
            if not isinstance(intent, dict):
                continue
            if str(intent.get("status") or "").strip() != "suppressed":
                continue
            suppressed.append(
                {
                    "intent_id": str(intent.get("intent_id") or "").strip(),
                    "title": str(intent.get("title") or "").strip(),
                    "reason": str(intent.get("suppression_reason") or "").strip(),
                }
            )
        return suppressed

    def _build_selection_data_notes(self, payload: dict, features: dict) -> List[str]:
        notes: List[str] = []
        coverage = self._ensure_structured_dict(payload.get("coverage_status"))
        raw_notes = coverage.get("notes")
        if isinstance(raw_notes, list):
            notes.extend(str(item) for item in raw_notes if str(item).strip())
        if features.get("forecast_type") == "heuristic_v1":
            notes.append("当前结果包含 heuristic 预测成分，建议降低进入性结论的强度。")
        if float(features.get("coverage_ratio") or 0.0) < 0.7:
            notes.append("当前样本覆盖率不足 0.7，结论更适合做方向筛选而不是直接立项。")
        return notes

    def _build_selection_debug_features(self, features: dict) -> dict:
        keys = [
            "forecast_type",
            "overall_status",
            "forecast_status",
            "top_asin_status",
            "coverage_ratio",
            "candidate_asin_count",
            "covered_asin_count",
            "missing_asin_count",
            "predicted_sales_w4_total",
            "top20_predicted_sales_w4_share",
            "trend_stage",
            "trend_wow",
            "keyword_coverage_ratio",
            "avg_price_gap",
            "avg_review_gap",
            "avg_monthly_sold_gap",
            "median_offer_gap",
            "has_forecast_top_asins_sales",
            "has_top_asin_w1_w4_compare",
            "has_candidate_vs_benchmark_compare",
            "has_forecast_top_asins_growth",
            "has_forecast_driver_distribution",
            "has_weak_signal_score_rank",
            "has_weak_signal_momentum_compare",
            "suppressed_primary_chart",
            "primary_chart_suppression_reason",
        ]
        debug = {key: features.get(key) for key in keys}
        debug["forecast_top_asins_row_count"] = len(self._ensure_structured_list(features.get("forecast_top_asins_rows")))
        debug["top_asin_forecast_compare_row_count"] = len(self._ensure_structured_list(features.get("top_asin_forecast_compare_rows")))
        debug["candidate_vs_benchmark_row_count"] = len(self._ensure_structured_list(features.get("candidate_vs_benchmark_rows")))
        debug["forecast_driver_row_count"] = len(self._ensure_structured_list(features.get("forecast_driver_rows")))
        debug["weak_signal_row_count"] = len(self._ensure_structured_list(features.get("weak_signal_rows")))
        debug["risk_flags"] = self._ensure_structured_list(features.get("risk_flags"))
        return debug

    def _selection_support_level(self, score: float) -> str:
        if score >= 75.0:
            return "strong"
        if score >= 55.0:
            return "partial"
        return "weak"

    def _merge_structured_tables(self, existing_tables: List[dict], new_tables: List[dict]) -> List[dict]:
        merged: List[dict] = []
        new_table_ids = {
            str(table.get("table_id") or "").strip()
            for table in new_tables
            if isinstance(table, dict) and str(table.get("table_id") or "").strip()
        }
        for table in existing_tables:
            if not isinstance(table, dict):
                continue
            table_id = str(table.get("table_id") or "").strip()
            if table_id and table_id in new_table_ids:
                continue
            merged.append(table)
        merged.extend(table for table in new_tables if isinstance(table, dict))
        return merged

    def _merge_chart_intents(self, existing_intents: List[dict], new_intents: List[dict]) -> List[dict]:
        merged: List[dict] = []
        new_intent_ids = {
            str(intent.get("intent_id") or "").strip()
            for intent in new_intents
            if isinstance(intent, dict) and str(intent.get("intent_id") or "").strip()
        }
        for intent in existing_intents:
            if not isinstance(intent, dict):
                continue
            intent_id = str(intent.get("intent_id") or "").strip()
            if intent_id and intent_id in new_intent_ids:
                continue
            merged.append(intent)
        merged.extend(intent for intent in new_intents if isinstance(intent, dict))
        return merged

    def _build_asin_history_data_tables(self, history_payload: dict) -> List[dict]:
        sales_rows: List[dict] = []
        price_rows: List[dict] = []
        bsr_rows: List[dict] = []
        review_rows: List[dict] = []
        summary_rows: List[dict] = []

        for item in self._ensure_structured_list(history_payload.get("items")):
            if not isinstance(item, dict):
                continue
            asin = str(item.get("asin") or "").strip()
            if not asin:
                continue
            series = self._ensure_structured_list(item.get("series"))
            summary = self._ensure_structured_dict(item.get("window_summary"))
            history_status = str(item.get("history_status") or "").strip()

            for row in series:
                if not isinstance(row, dict):
                    continue
                date = row.get("date")
                base_row = {"asin": asin, "date": date}
                if row.get("iso_year_week"):
                    base_row["iso_year_week"] = row.get("iso_year_week")
                sales_rows.append({**base_row, "estimated_daily_sales": row.get("estimated_daily_sales")})
                price_rows.append({**base_row, "effective_price": row.get("effective_price")})
                bsr_rows.append({**base_row, "bsr": row.get("bsr")})
                review_rows.append({**base_row, "review_count": row.get("review_count")})

            summary_rows.append(
                {
                    "asin": asin,
                    "sales_trend_direction": self._compute_asin_sales_trend_direction(series),
                    "price_stability_score": self._compute_asin_price_stability_score(series),
                    "review_growth_delta": summary.get("review_growth_window"),
                    "bsr_improvement_ratio": self._compute_asin_bsr_improvement_ratio(series),
                    "history_status": history_status,
                }
            )

        return [
            self._make_structured_table(
                "asin_history_sales_series",
                "ASIN 历史销量时序",
                ["asin", "date", "estimated_daily_sales"],
                sales_rows,
                semantic_type="trend_summary",
                grain="asin_date",
            ),
            self._make_structured_table(
                "asin_history_price_series",
                "ASIN 历史价格时序",
                ["asin", "date", "effective_price"],
                price_rows,
                semantic_type="trend_summary",
                grain="asin_date",
            ),
            self._make_structured_table(
                "asin_history_bsr_series",
                "ASIN 历史 BSR 时序",
                ["asin", "date", "bsr"],
                bsr_rows,
                semantic_type="trend_summary",
                grain="asin_date",
            ),
            self._make_structured_table(
                "asin_history_review_series",
                "ASIN 历史评论时序",
                ["asin", "date", "review_count"],
                review_rows,
                semantic_type="trend_summary",
                grain="asin_date",
            ),
            self._make_structured_table(
                "asin_history_stability_summary",
                "ASIN 历史稳定性摘要",
                ["asin", "sales_trend_direction", "price_stability_score", "review_growth_delta", "bsr_improvement_ratio", "history_status"],
                summary_rows,
                semantic_type="diagnostic_metrics",
                grain="asin",
            ),
        ]

    def _make_structured_table(
        self,
        table_id: str,
        title: str,
        columns: List[str],
        rows: List[dict],
        *,
        semantic_type: str,
        grain: str,
    ) -> dict:
        return {
            "table_id": table_id,
            "title": title,
            "semantic_type": semantic_type,
            "grain": grain,
            "columns": columns,
            "rows": rows,
        }

    def _build_asin_history_chart_intents(self, data_tables: List[dict]) -> List[dict]:
        sales_rows = self._selection_table_rows(data_tables, "asin_history_sales_series")
        price_rows = self._selection_table_rows(data_tables, "asin_history_price_series")
        bsr_rows = self._selection_table_rows(data_tables, "asin_history_bsr_series")
        review_rows = self._selection_table_rows(data_tables, "asin_history_review_series")
        summary_rows = self._selection_table_rows(data_tables, "asin_history_stability_summary")

        return [
            self._build_asin_history_line_intent(
                intent_id="asin_sales_trend_line",
                title="单 ASIN 历史销量趋势",
                dataset_ref="asin_history_sales_series",
                rows=sales_rows,
                value_field="estimated_daily_sales",
                value_title="估算日销量",
            ),
            self._build_asin_history_line_intent(
                intent_id="asin_price_trend_line",
                title="单 ASIN 历史价格趋势",
                dataset_ref="asin_history_price_series",
                rows=price_rows,
                value_field="effective_price",
                value_title="价格",
            ),
            self._build_asin_history_line_intent(
                intent_id="asin_bsr_trend_line",
                title="单 ASIN 历史 BSR 趋势",
                dataset_ref="asin_history_bsr_series",
                rows=bsr_rows,
                value_field="bsr",
                value_title="BSR",
            ),
            self._build_asin_history_line_intent(
                intent_id="asin_review_growth_trend_line",
                title="单 ASIN 历史评论趋势",
                dataset_ref="asin_history_review_series",
                rows=review_rows,
                value_field="review_count",
                value_title="评论数",
            ),
            self._build_asin_history_scorecard_intent(summary_rows),
        ]

    def _build_asin_history_line_intent(
        self,
        *,
        intent_id: str,
        title: str,
        dataset_ref: str,
        rows: List[dict],
        value_field: str,
        value_title: str,
    ) -> dict:
        non_null_count = sum(1 for row in rows if isinstance(row, dict) and row.get(value_field) not in (None, ""))
        status = "ready" if len(rows) >= 8 and non_null_count >= 5 else "suppressed"
        suppression_reason = None if status == "ready" else "history points are insufficient for a stable line chart"
        return {
            "intent_id": intent_id,
            "status": status,
            "priority": 70,
            "title": title,
            "question": title,
            "chart_family": "line_trend",
            "dataset_ref": dataset_ref,
            "roles": {
                "x_field": "date",
                "series_field": "asin",
                "value_field": value_field,
            },
            "semantics": {
                "value_semantic": value_title,
                "comparison_mode": "trend",
            },
            "guardrails": {
                "min_rows": 8,
                "min_non_null_points": 5,
                "prefer_single_asin": True,
            },
            "suppression_reason": suppression_reason,
        }

    def _build_asin_history_scorecard_intent(self, rows: List[dict]) -> dict:
        ready_rows = [row for row in rows if isinstance(row, dict) and str(row.get("history_status") or "") == "ready"]
        return {
            "intent_id": "asin_stability_scorecard",
            "status": "ready" if ready_rows else "suppressed",
            "priority": 60,
            "title": "单 ASIN 历史稳定性摘要",
            "question": "哪些 ASIN 的历史表现更稳定？",
            "chart_family": "scorecard_only",
            "dataset_ref": "asin_history_stability_summary",
            "roles": {"label_field": "asin"},
            "semantics": {"comparison_mode": "scorecard"},
            "guardrails": {"min_rows": 1},
            "suppression_reason": None if ready_rows else "no ASIN has ready local history summary",
        }

    def _compute_asin_sales_trend_direction(self, series: List[dict]) -> str:
        first_value, last_value = self._first_last_numeric(series, "estimated_daily_sales")
        if first_value is None or last_value is None:
            return "unknown"
        if last_value > first_value * 1.08:
            return "rising"
        if last_value < first_value * 0.92:
            return "cooling"
        return "stable"

    def _compute_asin_price_stability_score(self, series: List[dict]) -> Optional[float]:
        values = [self._safe_structured_number(row.get("effective_price")) for row in series if isinstance(row, dict)]
        values = [value for value in values if value is not None and value > 0]
        if len(values) < 2:
            return None
        avg_value = sum(values) / len(values)
        if avg_value <= 0:
            return None
        variation = (max(values) - min(values)) / avg_value
        score = 100.0 - min(100.0, variation * 100.0)
        return round(score, 2)

    def _compute_asin_bsr_improvement_ratio(self, series: List[dict]) -> Optional[float]:
        first_value, last_value = self._first_last_numeric(series, "bsr")
        if first_value is None or last_value is None or first_value <= 0:
            return None
        return round((first_value - last_value) / first_value, 4)

    def _first_last_numeric(self, series: List[dict], field_name: str) -> Tuple[Optional[float], Optional[float]]:
        first_value: Optional[float] = None
        last_value: Optional[float] = None
        for row in series:
            if not isinstance(row, dict):
                continue
            value = self._safe_structured_number(row.get(field_name))
            if value is None:
                continue
            if first_value is None:
                first_value = value
            last_value = value
        return first_value, last_value

    def _build_selection_asin_case_evidence(self, payload: dict) -> dict:
        data_tables = payload.get("data_tables") if isinstance(payload.get("data_tables"), list) else []
        chart_intents = payload.get("chart_intents") if isinstance(payload.get("chart_intents"), list) else []
        summary_rows = self._selection_table_rows(data_tables, "asin_history_stability_summary")
        sales_rows = self._selection_table_rows(data_tables, "asin_history_sales_series")
        if not summary_rows and not sales_rows:
            return {}

        focus_asins: List[str] = []
        for row in summary_rows or sales_rows:
            if not isinstance(row, dict):
                continue
            asin = str(row.get("asin") or "").strip()
            if asin and asin not in focus_asins:
                focus_asins.append(asin)
            if len(focus_asins) >= 3:
                break

        chart_ids = {
            "asin_sales_trend_line",
            "asin_price_trend_line",
            "asin_bsr_trend_line",
            "asin_review_growth_trend_line",
            "asin_stability_scorecard",
        }
        ready_chart_intents = [
            str(intent.get("intent_id") or "").strip()
            for intent in chart_intents
            if isinstance(intent, dict)
            and str(intent.get("intent_id") or "").strip() in chart_ids
            and str(intent.get("status") or "").strip() == "ready"
        ]

        return {
            "focus_asins": focus_asins,
            "data_tables": [
                table_id
                for table_id in [
                    "asin_history_sales_series",
                    "asin_history_price_series",
                    "asin_history_bsr_series",
                    "asin_history_review_series",
                    "asin_history_stability_summary",
                ]
                if self._selection_table_rows(data_tables, table_id)
            ],
            "chart_intents": ready_chart_intents,
            "summary_paragraph": "已补充 ASIN 级历史时序证据，可用来解释单点机会是否具备持续性、价格是否稳定以及评论是否仍在增长。",
        }

    def _render_selection_decision_markdown(
        self,
        *,
        recommendation_level: str,
        recommendation_score: float,
        confidence_level: str,
        confidence_score: float,
        core_reasons: List[str],
        validation_focus: str,
        next_actions: List[str],
        blocking_risk: str,
    ) -> str:
        lines = [
            "## 决策摘要",
            "- Recommendation: %s（%.1f/100）" % (recommendation_level, recommendation_score),
            "- Confidence: %s（%.1f/100）" % (confidence_level, confidence_score),
            "- 当前最大阻断项: %s" % blocking_risk,
            "- 当前优先验证点: %s" % validation_focus,
        ]
        if core_reasons:
            lines.append("")
            lines.append("### 核心依据")
            for reason in core_reasons:
                lines.append("- %s" % reason)
        if next_actions:
            lines.append("")
            lines.append("### 下一步动作")
            for action in next_actions[:3]:
                lines.append("- %s" % action)
        return "\n".join(lines).strip()

    def _render_structured_workflow_payload(self, payload: dict, fallback_summary: str = "") -> str:
        rendered_markdown = payload.get("rendered_markdown")
        if isinstance(rendered_markdown, str) and rendered_markdown.strip():
            return rendered_markdown.strip()

        if self._is_standard_report_payload(payload):
            return self._render_standard_report_payload(payload, fallback_summary=fallback_summary)

        sections: List[str] = []

        selection_decision_markdown = payload.get("selection_decision_markdown")
        if isinstance(selection_decision_markdown, str) and selection_decision_markdown.strip():
            sections.append(selection_decision_markdown.strip())

        report_payload = payload.get("report_payload")
        report_section = self._render_selection_report_payload(report_payload)
        if report_section:
            sections.append(report_section)

        summary_markdown = payload.get("summary_markdown")
        if isinstance(summary_markdown, str) and summary_markdown.strip():
            sections.append(summary_markdown.strip())
        elif fallback_summary.strip():
            sections.append(fallback_summary.strip())

        coverage_status = payload.get("coverage_status")
        coverage_section = self._render_structured_coverage_status(coverage_status)
        if coverage_section:
            sections.append(coverage_section)

        chart_panels_section = self._render_structured_chart_panels(payload)
        if chart_panels_section:
            sections.append(chart_panels_section)

        data_tables = payload.get("data_tables")
        if isinstance(data_tables, list):
            for table in data_tables:
                table_markdown = self._render_structured_data_table(table)
                if table_markdown:
                    sections.append(table_markdown)

        return "\n\n".join(section for section in sections if section).strip()

    def _render_standard_report_payload(self, payload: dict, fallback_summary: str = "") -> str:
        sections: List[str] = []

        summary_markdown = payload.get("summary_markdown")
        if isinstance(summary_markdown, str) and summary_markdown.strip():
            sections.append(summary_markdown.strip())
        elif fallback_summary.strip():
            sections.append(fallback_summary.strip())

        chart_panels_section = self._render_structured_chart_panels(
            self._standard_report_chart_payload(payload),
            include_suppressed=False,
        )
        if chart_panels_section:
            sections.append(chart_panels_section)

        return "\n\n".join(section for section in sections if section).strip()

    def _render_structured_chart_panels(self, payload: dict, *, include_suppressed: bool = True) -> str:
        chart_specs = self._collect_structured_chart_specs(payload)
        suppressed_charts = self._build_selection_suppressed_charts(payload) if include_suppressed else []
        if not chart_specs and not suppressed_charts:
            return ""

        chart_intents = payload.get("chart_intents") if isinstance(payload.get("chart_intents"), list) else []
        intent_by_id = {
            str(intent.get("intent_id") or "").strip(): intent
            for intent in chart_intents
            if isinstance(intent, dict) and str(intent.get("intent_id") or "").strip()
        }

        entries = [
            self._build_chart_panel_entry(chart_spec, intent_by_id)
            for chart_spec in chart_specs
        ]
        entries = [entry for entry in entries if entry]
        entries.sort(
            key=lambda item: (
                0 if item["panel_tier"] == "primary" else 1 if item["panel_tier"] == "supporting" else 2,
                item["display_priority"],
                item["title"],
            )
        )

        primary_entries = [entry for entry in entries if entry["panel_tier"] == "primary"]
        supporting_entries = [entry for entry in entries if entry["panel_tier"] == "supporting"]
        diagnostic_entries = [entry for entry in entries if entry["panel_tier"] == "diagnostic"]

        if len(primary_entries) > 2:
            supporting_entries = primary_entries[2:] + supporting_entries
            primary_entries = primary_entries[:2]

        sections: List[str] = []
        primary_section = self._render_chart_panel_tier_section(
            title=CHART_PANEL_TIER_LABELS["primary"],
            entries=primary_entries,
            collapsed=False,
        )
        if primary_section:
            sections.append(primary_section)

        supporting_section = self._render_chart_panel_tier_section(
            title=CHART_PANEL_TIER_LABELS["supporting"],
            entries=supporting_entries,
            collapsed=False,
        )
        if supporting_section:
            sections.append(supporting_section)

        diagnostic_notes = self._render_suppressed_chart_notes(suppressed_charts)
        diagnostic_section = self._render_chart_panel_tier_section(
            title=CHART_PANEL_TIER_LABELS["diagnostic"],
            entries=diagnostic_entries,
            collapsed=True,
            notes=diagnostic_notes,
        )
        if diagnostic_section:
            sections.append(diagnostic_section)

        return "\n\n".join(section for section in sections if section).strip()

    def _collect_structured_chart_specs(self, payload: dict) -> List[dict]:
        if not isinstance(payload, dict):
            return []

        collected: List[dict] = []
        rendered_chart_ids: set = set()
        chart_specs = payload.get("chart_specs") if isinstance(payload.get("chart_specs"), list) else []
        for chart_spec in chart_specs:
            if not isinstance(chart_spec, dict):
                continue
            if not self._structured_chart_spec_has_signal(chart_spec):
                continue
            chart_id = str(chart_spec.get("chart_id") or "").strip()
            if chart_id:
                rendered_chart_ids.add(chart_id)
            collected.append(chart_spec)

        collected.extend(self._synthesize_structured_chart_specs(payload, rendered_chart_ids))
        return collected

    def _structured_chart_spec_values(self, chart_spec: dict) -> List[dict]:
        spec = chart_spec.get("spec") if isinstance(chart_spec, dict) else None
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except Exception:
                return []
        if not isinstance(spec, dict):
            return []
        data = spec.get("data")
        if not isinstance(data, dict):
            return []
        values = data.get("values")
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, dict)]

    def _structured_chart_spec_has_signal(self, chart_spec: dict) -> bool:
        chart_id = str(chart_spec.get("chart_id") or "").strip()
        if chart_id not in {
            "forecast_top_asins_sales",
            "forecast_top_asins_chart",
            "forecast_top_asins_growth",
            "forecast_top_asins_growth_chart",
            "top_asin_w1_w4_compare",
            "top_asin_drilldown_chart",
        }:
            return True

        values = self._structured_chart_spec_values(chart_spec)
        if not values:
            return False
        if chart_id in {"forecast_top_asins_sales", "forecast_top_asins_chart"}:
            return (
                self._selection_count_non_zero(values, ["predicted_weekly_sales_w4", "预测周销量W4"]) >= 2
                and self._selection_count_distinct_non_zero(values, ["predicted_weekly_sales_w4", "预测周销量W4"]) >= 2
            )
        if chart_id in {"forecast_top_asins_growth", "forecast_top_asins_growth_chart"}:
            return (
                self._selection_count_non_zero(values, ["growth_delta", "predicted_growth_delta_w4_minus_w1", "W4-W1 增量"]) >= 2
                and self._selection_count_distinct_non_zero(values, ["growth_delta", "predicted_growth_delta_w4_minus_w1", "W4-W1 增量"]) >= 2
            )
        return self._selection_count_non_zero(
            values,
            ["predicted_weekly_sales_w1", "predicted_weekly_sales_w4", "预测周销量W1", "预测周销量W4"],
        ) >= 2

    def _build_chart_panel_entry(self, chart_spec: dict, intent_by_id: Dict[str, dict]) -> Optional[dict]:
        if not isinstance(chart_spec, dict):
            return None

        chart_id = str(chart_spec.get("chart_id") or "").strip()
        intent = intent_by_id.get(chart_id, {})
        default_meta = DEFAULT_CHART_PANEL_META.get(chart_id, {})
        panel_group = str(
            chart_spec.get("panel_group")
            or intent.get("panel_group")
            or default_meta.get("panel_group")
            or ("diagnostic" if "weak_signal" in chart_id else "overview")
        ).strip() or "overview"
        panel_tier = str(
            chart_spec.get("panel_tier")
            or intent.get("panel_tier")
            or default_meta.get("panel_tier")
            or ("diagnostic" if panel_group == "diagnostic" else "supporting")
        ).strip() or "supporting"
        display_priority = int(
            self._safe_structured_number(
                chart_spec.get("display_priority")
                or intent.get("display_priority")
                or chart_spec.get("priority")
                or intent.get("priority")
                or default_meta.get("display_priority")
                or 999
            )
            or 999
        )
        evidence_layer = str(
            chart_spec.get("evidence_layer")
            or intent.get("evidence_layer")
            or default_meta.get("evidence_layer")
            or "internal"
        ).strip() or "internal"
        chart_markdown = self._render_structured_chart_spec(chart_spec, heading_level=4, evidence_layer=evidence_layer)
        if not chart_markdown:
            return None
        return {
            "chart_id": chart_id,
            "title": str(chart_spec.get("title") or chart_id or "图表").strip(),
            "panel_group": panel_group,
            "panel_tier": panel_tier,
            "display_priority": display_priority,
            "evidence_layer": evidence_layer,
            "markdown": chart_markdown,
        }

    def _render_chart_panel_tier_section(
        self,
        *,
        title: str,
        entries: List[dict],
        collapsed: bool,
        notes: str = "",
    ) -> str:
        parts: List[str] = []
        if notes:
            parts.append(notes)
        if entries:
            grouped_entries = self._group_chart_panel_entries(entries)
            for group_key in CHART_PANEL_GROUP_ORDER:
                group_entries = grouped_entries.get(group_key) or []
                if not group_entries:
                    continue
                parts.append(self._render_chart_panel_group(group_key, group_entries))
        if not parts:
            return ""

        body = "\n\n".join(part for part in parts if part).strip()
        if not body:
            return ""
        if not collapsed:
            return "## %s\n\n%s" % (title, body)
        return "## %s\n\n<details>\n<summary>展开查看诊断图表与抑制说明</summary>\n\n%s\n\n</details>" % (title, body)

    def _group_chart_panel_entries(self, entries: List[dict]) -> Dict[str, List[dict]]:
        grouped: Dict[str, List[dict]] = {}
        for entry in entries:
            group_key = str(entry.get("panel_group") or "overview").strip() or "overview"
            grouped.setdefault(group_key, []).append(entry)
        for group_entries in grouped.values():
            group_entries.sort(key=lambda item: (item.get("display_priority") or 999, item.get("title") or ""))
        return grouped

    def _render_chart_panel_group(self, group_key: str, entries: List[dict]) -> str:
        if not entries:
            return ""
        label = CHART_PANEL_GROUP_LABELS.get(group_key, group_key)
        visible_entries = entries[:4]
        overflow_entries = entries[4:]
        lines = ["### %s" % label]
        lines.extend(entry.get("markdown") or "" for entry in visible_entries if entry.get("markdown"))
        if overflow_entries:
            overflow_body = "\n\n".join(entry.get("markdown") or "" for entry in overflow_entries if entry.get("markdown")).strip()
            if overflow_body:
                lines.append(
                    "<details>\n<summary>展开查看更多 %s 图表（%d 张）</summary>\n\n%s\n\n</details>"
                    % (label, len(overflow_entries), overflow_body)
                )
        return "\n\n".join(part for part in lines if part).strip()

    def _render_suppressed_chart_notes(self, suppressed_charts: List[dict]) -> str:
        if not suppressed_charts:
            return ""
        lines = ["### 主图抑制与补证说明"]
        for item in suppressed_charts[:8]:
            title = str(item.get("title") or item.get("intent_id") or "图表").strip()
            reason = str(item.get("reason") or "未返回抑制原因").strip()
            lines.append("- %s：%s" % (title, reason))
        return "\n".join(lines).strip()

    def _render_selection_report_payload(self, report_payload: Any) -> str:
        if not isinstance(report_payload, dict):
            return ""
        if str(report_payload.get("schema_version") or "").strip() != "xm.selection-report.v1":
            return ""

        sections: List[str] = []

        overview = report_payload.get("overview")
        if isinstance(overview, dict):
            lines = ["## 结论总览"]
            recommendation_level = str(overview.get("recommendation_level") or "").strip()
            confidence_level = str(overview.get("confidence_level") or "").strip()
            if recommendation_level:
                lines.append("- 当前建议: %s" % recommendation_level)
            if confidence_level:
                lines.append("- 当前信心: %s" % confidence_level)
            core_reasons = overview.get("core_reasons")
            if isinstance(core_reasons, list):
                for reason in core_reasons[:3]:
                    text = str(reason or "").strip()
                    if text:
                        lines.append("- %s" % text)
            if len(lines) > 1:
                sections.append("\n".join(lines))

        decision = report_payload.get("decision")
        if isinstance(decision, dict):
            lines = ["## 决策判断"]
            judgement_summary = str(decision.get("judgement_summary") or "").strip()
            if judgement_summary:
                lines.append(judgement_summary)
            decision_basis = decision.get("decision_basis")
            if isinstance(decision_basis, list):
                for reason in decision_basis[:3]:
                    text = str(reason or "").strip()
                    if text:
                        lines.append("- %s" % text)
            blocking_risk = str(decision.get("blocking_risk") or "").strip()
            if blocking_risk:
                lines.append("- 最大阻断项: %s" % blocking_risk)
            if len(lines) > 1:
                sections.append("\n".join(lines))

        market_attractiveness = report_payload.get("market_attractiveness")
        if isinstance(market_attractiveness, dict):
            lines = ["## 市场吸引力"]
            metric_labels = [
                ("predicted_sales_w4_total", "预测 W4 总销量"),
                ("top20_predicted_sales_w4_share", "Top20 集中度"),
                ("top_asin_count_with_forecast", "有效预测样本数"),
                ("trend_stage", "趋势阶段"),
                ("trend_wow", "趋势周环比"),
                ("keyword_coverage_ratio", "趋势覆盖率"),
            ]
            for key, label in metric_labels:
                value = market_attractiveness.get(key)
                if value in (None, "", [], {}):
                    continue
                lines.append("- %s: %s" % (label, value))
            if len(lines) > 1:
                sections.append("\n".join(lines))

        competition_structure = report_payload.get("competition_structure")
        if isinstance(competition_structure, dict):
            lines = ["## 竞争结构"]
            metric_labels = [
                ("avg_price_gap", "平均价格差异"),
                ("avg_review_gap", "平均评论差异"),
                ("avg_monthly_sold_gap", "平均月销差异"),
                ("median_offer_gap", "中位供给差异"),
            ]
            for key, label in metric_labels:
                value = competition_structure.get(key)
                if value in (None, "", [], {}):
                    continue
                lines.append("- %s: %s" % (label, value))
            if len(lines) > 1:
                sections.append("\n".join(lines))

        opportunity_drivers = report_payload.get("opportunity_drivers")
        if isinstance(opportunity_drivers, dict):
            lines = ["## 机会驱动"]
            forecast_type = str(opportunity_drivers.get("forecast_type") or "").strip()
            if forecast_type:
                lines.append("- 预测类型: %s" % forecast_type)
            driver_rows = opportunity_drivers.get("driver_distribution_rows")
            if isinstance(driver_rows, list) and driver_rows:
                lines.append("- 驱动分布证据: %s 行" % len(driver_rows))
            weak_signal_rows = opportunity_drivers.get("weak_signal_rows")
            if isinstance(weak_signal_rows, list) and weak_signal_rows:
                lines.append("- 弱信号证据: %s 行" % len(weak_signal_rows))
            if len(lines) > 1:
                sections.append("\n".join(lines))

        asin_case_evidence = report_payload.get("asin_case_evidence")
        if isinstance(asin_case_evidence, dict) and asin_case_evidence:
            lines = ["## ASIN 案例证据"]
            focus_asins = asin_case_evidence.get("focus_asins")
            if isinstance(focus_asins, list) and focus_asins:
                lines.append("- 聚焦 ASIN: %s" % ", ".join(str(item) for item in focus_asins if str(item).strip()))
            chart_intents = asin_case_evidence.get("chart_intents")
            if isinstance(chart_intents, list) and chart_intents:
                lines.append("- 已挂接图表意图: %s" % ", ".join(str(item) for item in chart_intents if str(item).strip()))
            summary_paragraph = str(asin_case_evidence.get("summary_paragraph") or "").strip()
            if summary_paragraph:
                lines.append(summary_paragraph)
            if len(lines) > 1:
                sections.append("\n".join(lines))

        risks = report_payload.get("risks_and_counterevidence")
        if isinstance(risks, dict):
            lines = ["## 风险与反证"]
            risk_flags = risks.get("risk_flags")
            if isinstance(risk_flags, list):
                for flag in risk_flags[:5]:
                    text = str(flag or "").strip()
                    if text:
                        lines.append("- 风险标记: %s" % text)
            suppression_reasons = risks.get("suppression_reasons")
            if isinstance(suppression_reasons, list):
                for reason in suppression_reasons[:3]:
                    text = str(reason or "").strip()
                    if text:
                        lines.append("- 图表抑制原因: %s" % text)
            summary_paragraph = str(risks.get("summary_paragraph") or "").strip()
            if summary_paragraph:
                lines.append(summary_paragraph)
            if len(lines) > 1:
                sections.append("\n".join(lines))

        recommended_actions = report_payload.get("recommended_actions")
        if isinstance(recommended_actions, dict):
            lines = ["## 推荐动作"]
            validation_focus = str(recommended_actions.get("validation_focus") or "").strip()
            if validation_focus:
                lines.append("- 优先验证点: %s" % validation_focus)
            next_actions = recommended_actions.get("next_actions")
            if isinstance(next_actions, list):
                for action in next_actions[:3]:
                    text = str(action or "").strip()
                    if text:
                        lines.append("- %s" % text)
            stop_condition = str(recommended_actions.get("stop_condition") or "").strip()
            if stop_condition:
                lines.append("- 停止条件: %s" % stop_condition)
            revisit_condition = str(recommended_actions.get("revisit_condition") or "").strip()
            if revisit_condition:
                lines.append("- 复评条件: %s" % revisit_condition)
            if len(lines) > 1:
                sections.append("\n".join(lines))

        data_boundary = report_payload.get("data_boundary")
        if isinstance(data_boundary, dict):
            lines = ["## 数据边界"]
            data_sources = data_boundary.get("data_sources")
            if isinstance(data_sources, list) and data_sources:
                lines.append("- 数据来源: %s" % ", ".join(str(item) for item in data_sources if str(item).strip()))
            forecast_type = str(data_boundary.get("forecast_type") or "").strip()
            if forecast_type:
                lines.append("- 预测类型: %s" % forecast_type)
            coverage_ratio = data_boundary.get("coverage_ratio")
            if coverage_ratio not in (None, "", [], {}):
                lines.append("- 覆盖率: %s" % coverage_ratio)
            window_days = data_boundary.get("window_days")
            if window_days not in (None, "", [], {}):
                lines.append("- 分析窗口: %s 天" % window_days)
            data_notes = data_boundary.get("data_notes")
            if isinstance(data_notes, list):
                for note in data_notes[:3]:
                    text = str(note or "").strip()
                    if text:
                        lines.append("- %s" % text)
            if len(lines) > 1:
                sections.append("\n".join(lines))

        return "\n\n".join(section for section in sections if section).strip()

    def _render_structured_coverage_status(self, coverage_status: Any) -> str:
        if not isinstance(coverage_status, dict):
            return ""

        lines: List[str] = ["## 结构化状态"]
        candidate_pool_status = coverage_status.get("candidate_pool")
        if candidate_pool_status:
            lines.append("- 候选池预测覆盖: %s" % candidate_pool_status)
        top_asin_status = coverage_status.get("top_asin")
        if top_asin_status:
            lines.append("- Top ASIN 预测覆盖: %s" % top_asin_status)
        covered_domains = coverage_status.get("covered_domains")
        if isinstance(covered_domains, list) and covered_domains:
            lines.append("- 已覆盖 domain: %s" % ", ".join(str(item) for item in covered_domains))
        missing_domains = coverage_status.get("missing_domains")
        if isinstance(missing_domains, list) and missing_domains:
            lines.append("- 未覆盖 domain: %s" % ", ".join(str(item) for item in missing_domains))

        return "\n".join(lines) if len(lines) > 1 else ""

    def _render_structured_data_table(self, table: Any) -> str:
        if not isinstance(table, dict):
            return ""

        columns = table.get("columns")
        rows = table.get("rows")
        if not isinstance(columns, list) or not columns:
            return ""
        if not isinstance(rows, list) or not rows:
            return ""

        def normalize_cell(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)
            return str(value)

        title = str(table.get("title") or table.get("table_id") or "数据表").strip()
        header = "| %s |" % " | ".join(normalize_cell(column) for column in columns)
        separator = "| %s |" % " | ".join("---" for _ in columns)
        body = []
        for row in rows:
            if isinstance(row, dict):
                padded = [row.get(str(column), "") for column in columns]
            elif isinstance(row, list):
                padded = list(row[: len(columns)])
                if len(padded) < len(columns):
                    padded.extend([""] * (len(columns) - len(padded)))
            else:
                continue
            body.append("| %s |" % " | ".join(normalize_cell(cell) for cell in padded))
        if not body:
            return ""
        return "## %s\n%s\n%s\n%s" % (title, header, separator, "\n".join(body))

    def _structured_table_rows_as_dicts(self, table: Any) -> List[dict]:
        if not isinstance(table, dict):
            return []

        columns = table.get("columns")
        rows = table.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list):
            return []

        normalized_rows: List[dict] = []
        for row in rows:
            if isinstance(row, dict):
                normalized_rows.append({str(key): value for key, value in row.items()})
                continue
            if isinstance(row, list):
                normalized_rows.append(
                    {
                        str(column): row[index] if index < len(row) else ""
                        for index, column in enumerate(columns)
                    }
                )
        return normalized_rows

    def _safe_structured_number(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            value = stripped
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _structured_chart_domain_max(self, values: List[dict], field_names: List[str]) -> float:
        max_value = 0.0
        for item in values:
            if not isinstance(item, dict):
                continue
            for field_name in field_names:
                number = self._safe_structured_number(item.get(field_name))
                if number is None:
                    continue
                if number > max_value:
                    max_value = number
        if max_value <= 0:
            return 1.0
        return round(max_value * 1.15, 2)

    def _build_structured_bar_chart_spec(
        self,
        values: List[dict],
        y_field: str,
        y_title: str,
        tooltip: List[dict],
        color_field: Optional[str] = None,
    ) -> str:
        encoding: Dict[str, Any] = {
            "x": {"field": "asin", "type": "nominal", "sort": "-y", "axis": {"labelAngle": -20}},
            "y": {
                "field": y_field,
                "type": "quantitative",
                "title": y_title,
                "scale": {"domain": [0, self._structured_chart_domain_max(values, [y_field])]},
            },
            "tooltip": tooltip,
        }
        if color_field:
            encoding["color"] = {"field": color_field, "type": "nominal", "title": "方向"}

        return json.dumps(
            {
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "mark": {"type": "bar", "cornerRadiusTopLeft": 4, "cornerRadiusTopRight": 4},
                "data": {"values": values},
                "encoding": encoding,
            },
            ensure_ascii=False,
        )

    def _build_structured_line_chart_spec(
        self,
        values: List[dict],
        x_field: str,
        y_field: str,
        y_title: str,
        tooltip: List[dict],
        color_field: Optional[str] = None,
        x_type: str = "temporal",
    ) -> str:
        encoding: Dict[str, Any] = {
            "x": {"field": x_field, "type": x_type, "title": "日期" if x_field == "date" else x_field},
            "y": {
                "field": y_field,
                "type": "quantitative",
                "title": y_title,
            },
            "tooltip": tooltip,
        }
        if color_field:
            encoding["color"] = {"field": color_field, "type": "nominal", "title": "ASIN"}

        return json.dumps(
            {
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "data": {"values": values},
                "mark": {"type": "line", "point": True},
                "encoding": encoding,
            },
            ensure_ascii=False,
        )

    def _build_forecast_top_asins_sales_chart(self, rows: List[dict]) -> Optional[dict]:
        values = []
        for row in rows:
            asin = str(row.get("ASIN") or row.get("asin") or "").strip()
            if not asin:
                continue
            sales_w4 = self._safe_structured_number(row.get("预测周销量W4") or row.get("predicted_weekly_sales_w4"))
            if sales_w4 is None or abs(sales_w4) <= 1e-9:
                continue
            values.append(
                {
                    "asin": asin,
                    "predicted_weekly_sales_w4": sales_w4,
                    "growth_delta": self._safe_structured_number(row.get("W4-W1 增量") or row.get("predicted_growth_delta_w4_minus_w1")) or 0,
                    "direction": str(row.get("方向") or row.get("direction") or "").strip(),
                }
            )
        if len(values) < 2:
            return None
        if self._selection_count_distinct_non_zero(values, ["predicted_weekly_sales_w4"]) < 2:
            return None
        return {
            "chart_id": "forecast_top_asins_sales",
            "renderer": "vega-lite",
            "title": "候选池 Top ASIN 预测周销量对比",
            "spec": self._build_structured_bar_chart_spec(
                values=values,
                y_field="predicted_weekly_sales_w4",
                y_title="预测周销量W4",
                tooltip=[
                    {"field": "asin", "type": "nominal"},
                    {"field": "predicted_weekly_sales_w4", "type": "quantitative", "title": "预测周销量W4"},
                    {"field": "growth_delta", "type": "quantitative", "title": "W4-W1 增量"},
                    {"field": "direction", "type": "nominal", "title": "方向"},
                ],
                color_field="direction" if any(item.get("direction") for item in values) else None,
            ),
        }

    def _build_top_asin_compare_chart(self, rows: List[dict]) -> Optional[dict]:
        values = []
        for row in rows:
            asin = str(row.get("ASIN") or row.get("asin") or "").strip()
            if not asin:
                continue
            status = str(row.get("状态") or row.get("status") or "").strip()
            if status in {"missing_domain_model", "missing_asin_prediction"}:
                continue
            sales_w1 = self._safe_structured_number(row.get("预测周销量W1") or row.get("predicted_weekly_sales_w1"))
            sales_w4 = self._safe_structured_number(row.get("预测周销量W4") or row.get("predicted_weekly_sales_w4"))
            if sales_w1 is None and sales_w4 is None:
                continue
            if abs(sales_w1 or 0) <= 1e-9 and abs(sales_w4 or 0) <= 1e-9:
                continue
            values.append(
                {
                    "asin": asin,
                    "predicted_weekly_sales_w1": sales_w1 or 0,
                    "predicted_weekly_sales_w4": sales_w4 or 0,
                }
            )
        if len(values) < 2:
            return None
        return {
            "chart_id": "top_asin_w1_w4_compare",
            "renderer": "vega-lite",
            "title": "Top ASIN 下钻预测 W1 vs W4 对比",
            "spec": json.dumps(
                {
                    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                    "data": {"values": values},
                    "transform": [
                        {"fold": ["predicted_weekly_sales_w1", "predicted_weekly_sales_w4"], "as": ["week", "sales"]},
                        {"calculate": "datum.week === 'predicted_weekly_sales_w1' ? 'W1' : 'W4'", "as": "week_label"},
                    ],
                    "mark": {"type": "bar", "cornerRadiusTopLeft": 4, "cornerRadiusTopRight": 4},
                    "encoding": {
                        "x": {"field": "asin", "type": "nominal", "axis": {"labelAngle": -20}},
                        "xOffset": {"field": "week_label"},
                        "y": {
                            "field": "sales",
                            "type": "quantitative",
                            "title": "预测周销量",
                            "scale": {"domain": [0, self._structured_chart_domain_max(values, ["predicted_weekly_sales_w1", "predicted_weekly_sales_w4"])]},
                        },
                        "color": {"field": "week_label", "type": "nominal", "title": "预测周期"},
                        "tooltip": [
                            {"field": "asin", "type": "nominal"},
                            {"field": "week_label", "type": "nominal", "title": "预测周期"},
                            {"field": "sales", "type": "quantitative", "title": "预测周销量"},
                        ],
                    },
                },
                ensure_ascii=False,
            ),
        }

    def _build_candidate_vs_benchmark_compare_chart(self, rows: List[dict]) -> Optional[dict]:
        pair_values = []
        gap_values = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            metric = str(row.get("指标") or row.get("metric") or row.get("asin") or "").strip()
            if not metric:
                continue
            candidate_value = self._safe_structured_number(row.get("候选池") or row.get("candidate_value"))
            benchmark_value = self._safe_structured_number(row.get("类目整体") or row.get("benchmark_value"))
            gap_value = self._safe_structured_number(row.get("gap_pct") or row.get("差距比例") or row.get("gap"))
            if candidate_value is not None and benchmark_value is not None:
                pair_values.append({"metric": metric, "candidate": candidate_value, "benchmark": benchmark_value})
            elif gap_value is not None:
                gap_values.append({"metric": metric, "gap_pct": gap_value})
        if pair_values:
            return {
                "chart_id": "candidate_vs_benchmark_compare",
                "renderer": "vega-lite",
                "title": "候选池 vs 类目基准对比图",
                "spec": json.dumps(
                    {
                        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                        "data": {"values": pair_values},
                        "transform": [
                            {"fold": ["candidate", "benchmark"], "as": ["series", "value"]},
                            {"calculate": "datum.series === 'candidate' ? '候选池' : '类目整体'", "as": "series_label"},
                        ],
                        "mark": {"type": "bar", "cornerRadiusTopLeft": 4, "cornerRadiusTopRight": 4},
                        "encoding": {
                            "x": {"field": "metric", "type": "nominal", "axis": {"labelAngle": -20}, "title": "指标"},
                            "xOffset": {"field": "series_label"},
                            "y": {"field": "value", "type": "quantitative", "title": "指标值"},
                            "color": {"field": "series_label", "type": "nominal", "title": "对比对象"},
                            "tooltip": [
                                {"field": "metric", "type": "nominal", "title": "指标"},
                                {"field": "series_label", "type": "nominal", "title": "对比对象"},
                                {"field": "value", "type": "quantitative", "title": "指标值"},
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
            }
        if gap_values:
            return {
                "chart_id": "candidate_vs_benchmark_compare",
                "renderer": "vega-lite",
                "title": "候选池 vs 类目基准差距图",
                "spec": json.dumps(
                    {
                        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                        "data": {"values": gap_values},
                        "mark": {"type": "bar", "cornerRadiusTopLeft": 4, "cornerRadiusTopRight": 4},
                        "encoding": {
                            "x": {"field": "metric", "type": "nominal", "axis": {"labelAngle": -20}, "title": "指标"},
                            "y": {"field": "gap_pct", "type": "quantitative", "title": "差距比例"},
                            "tooltip": [
                                {"field": "metric", "type": "nominal", "title": "指标"},
                                {"field": "gap_pct", "type": "quantitative", "title": "差距比例"},
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
            }
        return None

    def _build_forecast_driver_distribution_chart(self, rows: List[dict]) -> Optional[dict]:
        values = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = str(row.get("driver_label") or row.get("主驱动") or row.get("feature") or row.get("driver") or "").strip()
            share = self._safe_structured_number(row.get("driver_share") or row.get("share_pct") or row.get("占比"))
            if not label or share is None:
                continue
            values.append({"driver": label, "driver_share": share})
        if len(values) < 2:
            return None
        return {
            "chart_id": "forecast_driver_distribution",
            "renderer": "vega-lite",
            "title": "机会驱动分布图",
            "spec": json.dumps(
                {
                    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                    "data": {"values": values},
                    "mark": {"type": "bar", "cornerRadiusTopLeft": 4, "cornerRadiusTopRight": 4},
                    "encoding": {
                        "x": {"field": "driver", "type": "nominal", "axis": {"labelAngle": -20}, "title": "驱动项"},
                        "y": {"field": "driver_share", "type": "quantitative", "title": "贡献占比"},
                        "tooltip": [
                            {"field": "driver", "type": "nominal", "title": "驱动项"},
                            {"field": "driver_share", "type": "quantitative", "title": "贡献占比"},
                        ],
                    },
                },
                ensure_ascii=False,
            ),
        }

    def _build_supplemental_growth_chart(self, rows: List[dict]) -> Optional[dict]:
        values = []
        for row in rows:
            asin = str(row.get("ASIN") or row.get("asin") or "").strip()
            if not asin:
                continue
            growth_delta = self._safe_structured_number(row.get("W4-W1 增量") or row.get("predicted_growth_delta_w4_minus_w1"))
            if growth_delta is None or abs(growth_delta) <= 1e-9:
                continue
            values.append(
                {
                    "asin": asin,
                    "growth_delta": growth_delta,
                    "predicted_weekly_sales_w4": self._safe_structured_number(row.get("预测周销量W4") or row.get("predicted_weekly_sales_w4")) or 0,
                    "direction": str(row.get("方向") or row.get("direction") or "").strip(),
                }
            )
        if len(values) < 2:
            return None
        if self._selection_count_distinct_non_zero(values, ["growth_delta"]) < 2:
            return None

        return {
            "chart_id": "forecast_top_asins_growth_chart",
            "renderer": "vega-lite",
            "title": "候选池 Top ASIN 预测增量分化",
            "spec": self._build_structured_bar_chart_spec(
                values=values,
                y_field="growth_delta",
                y_title="W4-W1 增量",
                tooltip=[
                    {"field": "asin", "type": "nominal"},
                    {"field": "growth_delta", "type": "quantitative", "title": "W4-W1 增量"},
                    {"field": "predicted_weekly_sales_w4", "type": "quantitative", "title": "预测周销量W4"},
                    {"field": "direction", "type": "nominal", "title": "方向"},
                ],
                color_field="direction",
            ),
        }

    def _build_supplemental_drilldown_chart(self, rows: List[dict]) -> Optional[dict]:
        values = []
        for row in rows:
            asin = str(row.get("ASIN") or row.get("asin") or "").strip()
            if not asin:
                continue
            status = str(row.get("状态") or row.get("status") or "").strip()
            if status in {"missing_domain_model", "missing_asin_prediction"}:
                continue
            sales_w1 = self._safe_structured_number(row.get("预测周销量W1") or row.get("predicted_weekly_sales_w1"))
            sales_w4 = self._safe_structured_number(row.get("预测周销量W4") or row.get("predicted_weekly_sales_w4"))
            if abs(sales_w1 or 0) <= 1e-9 and abs(sales_w4 or 0) <= 1e-9:
                continue
            values.append(
                {
                    "asin": asin,
                    "predicted_weekly_sales_w1": sales_w1 or 0,
                    "predicted_weekly_sales_w4": sales_w4 or 0,
                }
            )
        if len(values) < 2:
            return None

        return {
            "chart_id": "top_asin_drilldown_chart",
            "renderer": "vega-lite",
            "title": "Top ASIN 下钻预测 W1 vs W4 对比",
            "spec": json.dumps(
                {
                    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                    "data": {"values": values},
                    "transform": [
                        {"fold": ["predicted_weekly_sales_w1", "predicted_weekly_sales_w4"], "as": ["week", "sales"]},
                        {"calculate": "datum.week === 'predicted_weekly_sales_w1' ? 'W1' : 'W4'", "as": "week_label"},
                    ],
                    "mark": {"type": "bar", "cornerRadiusTopLeft": 4, "cornerRadiusTopRight": 4},
                    "encoding": {
                        "x": {"field": "asin", "type": "nominal", "axis": {"labelAngle": -20}},
                        "xOffset": {"field": "week_label"},
                        "y": {
                            "field": "sales",
                            "type": "quantitative",
                            "title": "预测周销量",
                            "scale": {
                                "domain": [
                                    0,
                                    self._structured_chart_domain_max(
                                        values,
                                        ["predicted_weekly_sales_w1", "predicted_weekly_sales_w4"],
                                    ),
                                ]
                            },
                        },
                        "color": {"field": "week_label", "type": "nominal", "title": "预测周期"},
                        "tooltip": [
                            {"field": "asin", "type": "nominal"},
                            {"field": "week_label", "type": "nominal", "title": "预测周期"},
                            {"field": "sales", "type": "quantitative", "title": "预测周销量"},
                        ],
                    },
                },
                ensure_ascii=False,
            ),
        }

    def _build_asin_history_line_chart(self, intent: dict, rows: List[dict]) -> Optional[dict]:
        if not isinstance(intent, dict) or not rows:
            return None
        roles = intent.get("roles") if isinstance(intent.get("roles"), dict) else {}
        x_field = str(roles.get("x_field") or "date").strip() or "date"
        series_field = str(roles.get("series_field") or "asin").strip() or "asin"
        value_field = str(roles.get("value_field") or "").strip()
        if not value_field:
            return None

        values: List[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get(value_field)
            if value in (None, ""):
                continue
            point = {
                series_field: row.get(series_field),
                x_field: row.get(x_field),
                value_field: self._safe_structured_number(value) if self._safe_structured_number(value) is not None else value,
            }
            if row.get("iso_year_week"):
                point["iso_year_week"] = row.get("iso_year_week")
            values.append(point)
        if len(values) < 5:
            return None

        unique_series = sorted({str(item.get(series_field) or "").strip() for item in values if str(item.get(series_field) or "").strip()})
        color_field = series_field if len(unique_series) > 1 else None
        value_title = str(((intent.get("semantics") or {}).get("value_semantic") if isinstance(intent.get("semantics"), dict) else "") or value_field).strip() or value_field
        x_type = "temporal" if x_field == "date" else "ordinal"
        return {
            "chart_id": str(intent.get("intent_id") or "").strip() or value_field,
            "renderer": "vega-lite",
            "title": str(intent.get("title") or intent.get("intent_id") or "图表").strip(),
            "spec": self._build_structured_line_chart_spec(
                values=values,
                x_field=x_field,
                y_field=value_field,
                y_title=value_title,
                tooltip=[
                    {"field": series_field, "type": "nominal", "title": "ASIN"},
                    {"field": x_field, "type": x_type, "title": "日期" if x_field == "date" else x_field},
                    {"field": value_field, "type": "quantitative", "title": value_title},
                ],
                color_field=color_field,
                x_type=x_type,
            ),
        }

    def _synthesize_structured_chart_specs(self, payload: dict, existing_chart_ids: set) -> List[dict]:
        if not isinstance(payload, dict):
            return []

        data_tables = payload.get("data_tables")
        if not isinstance(data_tables, list):
            return []

        standard_report = self._is_standard_report_payload(payload)
        chart_intents = payload.get("chart_intents") if isinstance(payload.get("chart_intents"), list) else []
        ready_intent_ids = {
            str(intent.get("intent_id") or "").strip()
            for intent in chart_intents
            if isinstance(intent, dict) and str(intent.get("status") or "").strip() == "ready"
        }

        forecast_rows: List[dict] = []
        drilldown_rows: List[dict] = []
        candidate_benchmark_rows: List[dict] = []
        forecast_driver_rows: List[dict] = []
        table_rows_by_id: Dict[str, List[dict]] = {}
        for table in data_tables:
            if not isinstance(table, dict):
                continue
            table_id = str(table.get("table_id") or "").strip()
            title = str(table.get("title") or "").strip()
            rows = self._structured_table_rows_as_dicts(table)
            if not rows:
                continue
            if table_id:
                table_rows_by_id[table_id] = rows
            if table_id == "forecast_top_asins" or "候选池预测 Top ASIN" in title:
                forecast_rows = rows
            elif table_id == "top_asin_drilldown_forecast" or "Top ASIN 预测下钻" in title:
                drilldown_rows = rows
            elif table_id in {"candidate_vs_benchmark", "candidate_pool_vs_l3"} or "候选池 vs" in title:
                candidate_benchmark_rows = rows
            elif table_id == "forecast_driver_distribution":
                forecast_driver_rows = rows

        charts: List[dict] = []
        if forecast_rows and not ({"forecast_top_asins_sales", "forecast_top_asins_chart"} & existing_chart_ids) and "forecast_top_asins_sales" in ready_intent_ids:
            primary_chart = self._build_forecast_top_asins_sales_chart(forecast_rows)
            if primary_chart:
                charts.append(primary_chart)

        if drilldown_rows and "top_asin_w1_w4_compare" not in existing_chart_ids and "top_asin_w1_w4_compare" in ready_intent_ids:
            compare_chart = self._build_top_asin_compare_chart(drilldown_rows)
            if compare_chart:
                charts.append(compare_chart)

        if candidate_benchmark_rows and "candidate_vs_benchmark_compare" not in existing_chart_ids and "candidate_vs_benchmark_compare" in ready_intent_ids:
            benchmark_chart = self._build_candidate_vs_benchmark_compare_chart(candidate_benchmark_rows)
            if benchmark_chart:
                charts.append(benchmark_chart)

        if forecast_driver_rows and "forecast_driver_distribution" not in existing_chart_ids and "forecast_driver_distribution" in ready_intent_ids:
            driver_chart = self._build_forecast_driver_distribution_chart(forecast_driver_rows)
            if driver_chart:
                charts.append(driver_chart)

        if not standard_report and forecast_rows and "forecast_top_asins_growth_chart" not in existing_chart_ids:
            growth_chart = self._build_supplemental_growth_chart(forecast_rows)
            if growth_chart:
                charts.append(growth_chart)

        if not standard_report and drilldown_rows and "top_asin_drilldown_chart" not in existing_chart_ids:
            drilldown_chart = self._build_supplemental_drilldown_chart(drilldown_rows)
            if drilldown_chart:
                charts.append(drilldown_chart)

        for intent in chart_intents:
            if not isinstance(intent, dict):
                continue
            if str(intent.get("status") or "").strip() != "ready":
                continue
            if str(intent.get("chart_family") or "").strip() != "line_trend":
                continue
            chart_id = str(intent.get("intent_id") or "").strip()
            if not chart_id or chart_id in existing_chart_ids:
                continue
            dataset_ref = str(intent.get("dataset_ref") or "").strip()
            rows = table_rows_by_id.get(dataset_ref) or []
            line_chart = self._build_asin_history_line_chart(intent, rows)
            if line_chart:
                charts.append(line_chart)

        return charts

    def _render_structured_chart_spec(
        self,
        chart_spec: Any,
        *,
        heading_level: int = 2,
        evidence_layer: str = "",
    ) -> str:
        if not isinstance(chart_spec, dict):
            return ""

        renderer = str(chart_spec.get("renderer") or "").strip()
        spec = chart_spec.get("spec")
        if not renderer or spec is None:
            return ""

        title = str(chart_spec.get("title") or chart_spec.get("chart_id") or "图表").strip()
        if evidence_layer == "external_paid_signal" and not title.startswith("外部付费信号 · "):
            title = "外部付费信号 · %s" % title
        if renderer not in {"vega", "vega-lite", "mermaid"}:
            return ""

        if isinstance(spec, str):
            spec_text = spec.strip()
        else:
            spec_text = json.dumps(spec, ensure_ascii=False, indent=2)
        if not spec_text:
            return ""

        level = max(1, int(heading_level or 2))
        return "%s %s\n```%s\n%s\n```" % ("#" * level, title, renderer, spec_text)

    def _build_agent_tool_definitions(self) -> List[dict]:
        definitions = []
        if self.agent_tools is None:
            return definitions
        for tool_name in sorted(ALLOWED_AGENT_TOOLS):
            method = getattr(self.agent_tools, tool_name, None)
            if method is None:
                continue
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": self._tool_description(method),
                        "parameters": self._tool_parameters_schema(method),
                    },
                }
            )
        return definitions

    def _tool_description(self, method: Any) -> str:
        doc = str(getattr(method, "__doc__", "") or "").strip()
        if not doc:
            return "XiaMimate tool."
        return re.split(r"\n\s*:param\s+", doc, maxsplit=1)[0].strip()

    def _tool_parameters_schema(self, method: Any) -> dict:
        doc = str(getattr(method, "__doc__", "") or "")
        param_descriptions = {
            name.strip(): desc.strip()
            for name, desc in re.findall(r":param\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^\n]+)", doc)
        }
        properties = {}
        required = []
        for param_name, param in signature(method).parameters.items():
            if param_name.startswith("_"):
                continue
            schema = self._json_schema_for_tool_param(param.default)
            description = param_descriptions.get(param_name)
            if description:
                schema["description"] = description
            properties[param_name] = schema
            if param.default is param.empty:
                required.append(param_name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def _json_schema_for_tool_param(self, default: Any) -> dict:
        if isinstance(default, bool):
            return {"type": "boolean"}
        if isinstance(default, int) and not isinstance(default, bool):
            return {"type": "integer"}
        if isinstance(default, float):
            return {"type": "number"}
        if default is None:
            return {"type": "string"}
        return {"type": "string"}

    def _prepare_agent_payload(self, messages: List[dict], body: dict, mode: str = "agent", model_name: str = "") -> dict:
        provider = self._get_provider(model_name)
        payload = provider.filter_payload(body)
        payload["model"] = model_name or self._model_name_for_profile(self._default_agent_profile())
        payload["messages"] = self._inject_agent_system_prompt(messages, mode=mode)
        if "tools" in getattr(provider, "allowed_params", set()):
            payload["tools"] = self._build_agent_tool_definitions()
            payload.setdefault("tool_choice", "auto")

        user_value = payload.get("user")
        if isinstance(user_value, dict):
            payload["user"] = self._user_id(body)

        return payload

    def _run_agent_loop(
        self,
        messages: List[dict],
        body: dict,
        billing_context: dict,
        model_name: str,
        mode: str = "agent",
    ) -> str:
        conversation = deepcopy(messages or [])
        tool_observations: List[dict] = []

        for _ in range(6):
            payload = self._prepare_agent_payload(messages=conversation, body=body, mode=mode, model_name=model_name)
            payload["stream"] = False
            minimax_charge = self._charge_billing_event(
                billing_context=billing_context,
                event_type="llm_request",
                description="LLM 请求",
                meta={
                    "mode": "agent",
                    "model": payload.get("model"),
                    "message_count": len(payload.get("messages") or []),
                },
            )
            try:
                response = self._post_agent_payload(payload, model_name=model_name)
            except RuntimeError as exc:
                self._refund_billing_event(
                    billing_context=billing_context,
                    charge=minimax_charge,
                    description="LLM 请求失败，已退款",
                    meta={"mode": "agent", "error": str(exc)[:500]},
                )
                if tool_observations:
                    return self._fallback_answer_from_tool_observations(tool_observations, error=str(exc))
                raise
            content = self._extract_assistant_content(response)
            assistant_message = self._extract_assistant_message(response)
            native_tool_calls = self._extract_response_tool_calls(response)
            text_tool_calls = [] if native_tool_calls else self._extract_tool_calls(content, model_name=model_name)
            tool_calls = native_tool_calls or text_tool_calls

            if not tool_calls:
                cleaned = self._clean_agent_content(content, model_name=model_name)
                if cleaned:
                    return cleaned
                if self._agent_stream_contains_internal_markup(content, model_name=model_name):
                    return "已完成分析，但未生成可展示的结果，请重试。"
                return str(content or "").strip()

            conversation.append(assistant_message if native_tool_calls else {"role": "assistant", "content": content})

            tool_results = []
            for tool_call in tool_calls:
                result = self._execute_tool_call(tool_call, billing_context, truncate=False)
                observation = self._build_tool_observation(tool_call=tool_call, result=result)
                tool_observations.append(observation)
                if native_tool_calls and tool_call.get("tool_call_id"):
                    conversation.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(tool_call.get("tool_call_id")),
                            "content": observation["llm_result"],
                        }
                    )
                else:
                    tool_results.append(
                        TOOL_RESULT_TEMPLATE.format(
                            tool_name=tool_call["name"],
                            arguments=json.dumps(tool_call.get("parameters") or {}, ensure_ascii=False),
                            result=observation["llm_result"],
                        )
                    )

            if tool_results:
                conversation.append({"role": "user", "content": "\n\n".join(tool_results)})

        raise RuntimeError("Agent 工具调用轮次超过上限，已中止。")

    def _ensure_billing_context(self, body: dict) -> dict:
        data = self._chat_backend_request(
            method="POST",
            path="/internal/identity/exchange-webui-user",
            body={
                "user_id": self._user_id(body),
                "email": self._user_email(body),
                "display_name": self._user_name(body),
            },
            internal=True,
        )
        api_key = ((data.get("api_key") or {}).get("api_key_raw") or "").strip()
        if not api_key:
            raise RuntimeError("chat_backend 未返回用户 API key。")
        return {
            "user_id": str(data.get("user_id") or self._user_id(body)).strip(),
            "api_key": api_key,
            "points_account": data.get("points_account") or {},
            "pricing_version": data.get("pricing_version") or "unknown",
            "point_cost_by_event": data.get("point_cost_by_event") or POINT_COST_BY_EVENT,
        }

    def _charge_billing_event(
        self,
        billing_context: dict,
        event_type: str,
        units: int = 1,
        description: str = "",
        meta: Optional[dict] = None,
    ) -> dict:
        reference_id = "bill_%s" % uuid.uuid4().hex
        data = self._chat_backend_request(
            method="POST",
            path="/internal/billing/charge-points",
            body={
                "api_key": billing_context["api_key"],
                "events": [
                    {
                        "event_type": event_type,
                        "units": units,
                        "reference_id": reference_id,
                        "description": description,
                        "meta": meta or {},
                    }
                ],
            },
            internal=True,
            idempotency_key=reference_id,
        )
        charges = data.get("charges") or []
        if not charges:
            raise RuntimeError("chat_backend 未返回扣费结果。")
        charge = charges[-1]
        points_account = charge.get("points_account")
        if isinstance(points_account, dict):
            billing_context["points_account"] = points_account
        return charge

    def _refund_billing_event(
        self,
        billing_context: dict,
        charge: dict,
        description: str,
        meta: Optional[dict] = None,
    ) -> None:
        points_charged = int(charge.get("points_charged") or 0)
        if points_charged <= 0:
            return

        reference_id = None
        ledger_entry = charge.get("ledger_entry")
        if isinstance(ledger_entry, dict):
            reference_id = ledger_entry.get("reference_id")

        try:
            refund_id = "refund_%s" % (reference_id or uuid.uuid4().hex)
            data = self._chat_backend_request(
                method="POST",
                path="/internal/billing/refund-points",
                body={
                    "api_key": billing_context["api_key"],
                    "event_type": charge.get("event_type") or "unknown",
                    "points": points_charged,
                    "units": int(charge.get("units") or 1),
                    "reference_id": reference_id,
                    "description": description,
                    "meta": meta or {},
                },
                internal=True,
                idempotency_key=refund_id,
            )
            points_account = data.get("points_account")
            if isinstance(points_account, dict):
                billing_context["points_account"] = points_account
        except RuntimeError as exc:
            print("xiamimate billing refund failed", str(exc))

    def _chat_backend_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        headers: Optional[dict] = None,
        internal: bool = False,
        idempotency_key: Optional[str] = None,
        timeout: Optional[Union[int, Tuple[int, int]]] = None,
    ) -> dict:
        url = "%s%s" % (self._base_chat_backend_url(), path)
        response = None
        request_headers = dict(headers or {})
        if internal:
            request_headers.update(self._chat_backend_internal_headers(idempotency_key=idempotency_key))
        if body is not None:
            request_headers["Content-Type"] = "application/json"

        try:
            request_kwargs = {
                "headers": request_headers,
                "timeout": timeout if timeout is not None else self.valves.CHAT_BACKEND_TIMEOUT,
            }
            if body is not None:
                request_kwargs["json"] = body
            response = requests.request(method=method.upper(), url=url, **request_kwargs)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            detail = str(exc)
            if response is not None:
                try:
                    payload = response.json()
                    detail = str(payload.get("message") or payload)
                except ValueError:
                    detail = response.text or detail
            raise RuntimeError(detail[:4000])
        except ValueError as exc:
            raise RuntimeError("chat_backend 返回了无法解析的 JSON: %s" % str(exc))

        if payload.get("success") is not True:
            raise RuntimeError(str(payload.get("message") or "chat_backend 请求失败")[:4000])
        return payload.get("data") or {}

    def _chat_backend_stream_request(self, path: str, body: dict) -> requests.Response:
        url = "%s%s" % (self._base_chat_backend_url(), path)
        response = None
        try:
            response = requests.post(
                url,
                json=body,
                headers=self._chat_backend_internal_headers(),
                timeout=(10, self.valves.DIFY_REQUEST_TIMEOUT),
                stream=True,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            detail = str(exc)
            if response is not None:
                try:
                    payload = response.json()
                    detail = str(payload.get("message") or payload)
                except ValueError:
                    detail = response.text or detail
            raise RuntimeError(detail[:4000])

    def _chat_backend_internal_headers(self, idempotency_key: Optional[str] = None) -> dict:
        if not self.valves.CHAT_BACKEND_SERVICE_SECRET:
            raise RuntimeError("CHAT_BACKEND_SERVICE_SECRET 未配置。")
        headers = {
            INTERNAL_SERVICE_SECRET_HEADER_NAME: self.valves.CHAT_BACKEND_SERVICE_SECRET,
            INTERNAL_SERVICE_NAME_HEADER_NAME: self.valves.CHAT_BACKEND_SERVICE_NAME,
        }
        if idempotency_key:
            headers[IDEMPOTENCY_KEY_HEADER_NAME] = idempotency_key
        return headers

    def _chat_backend_user_headers(self, body: dict) -> dict:
        return {
            USER_ID_HEADER_NAME: self._user_id(body),
            USER_EMAIL_HEADER_NAME: self._user_email(body),
            USER_NAME_HEADER_NAME: self._user_name(body),
        }

    def _tool_result_has_error(self, result_text: str) -> bool:
        normalized = (result_text or "").strip()
        error_prefixes = (
            "工具 ",
            "theme_api 请求失败:",
            "知识库检索失败:",
            "网络搜索失败:",
            "CHAT_BACKEND_BASE_URL 未配置。",
            "CHAT_BACKEND_SERVICE_SECRET 未配置。",
        )
        return any(normalized.startswith(prefix) for prefix in error_prefixes)

    def _post_agent_payload(self, payload: dict, model_name: str) -> dict:
        provider = self._get_provider(model_name)
        return self._chat_backend_request(
            method="POST",
            path=provider.chat_completions_path(),
            body={"payload": payload},
            internal=True,
            timeout=self.valves.DIFY_REQUEST_TIMEOUT,
        )

    def _stream_agent_final_answer_chunks(self, payload: dict, fallback_content: str, model_name: str) -> Iterator[str]:
        provider = self._get_provider(model_name)
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        streamed = False
        stream_state = {"pending": "", "in_think": False, "blocked": False}
        emitted_parts: List[str] = []
        fallback_text = self._clean_agent_content(fallback_content or "已完成，但未返回可展示的结果。", model_name=model_name)

        if not provider.supports_streaming_final_answer():
            for chunk in self._split_text(fallback_text):
                yield chunk
            return

        try:
            with self._chat_backend_stream_request(
                path=provider.chat_completions_stream_path(),
                body={"payload": stream_payload},
            ) as response:
                response.raise_for_status()
                for event in self._iter_sse_events(response):
                    chunk = self._extract_openai_stream_delta_text(event)
                    if not chunk:
                        continue
                    cleaned_chunk = self._consume_agent_stream_text(stream_state, chunk, model_name=model_name)
                    if not cleaned_chunk:
                        continue
                    streamed = True
                    emitted_parts.append(cleaned_chunk)
                    yield cleaned_chunk
        except RuntimeError as exc:
            print("xiamimate.agent final stream failed", str(exc))

        flushed_chunk = self._flush_agent_stream_text(stream_state, model_name=model_name)
        if flushed_chunk:
            streamed = True
            emitted_parts.append(flushed_chunk)
            yield flushed_chunk

        emitted_text = "".join(emitted_parts)
        if fallback_text and fallback_text.startswith(emitted_text):
            remainder = fallback_text[len(emitted_text) :]
            if remainder:
                for chunk in self._split_text(remainder):
                    yield chunk
            if streamed or emitted_text:
                return

        if streamed:
            return

        final_text = fallback_text or "已完成，但未返回可展示的结果。"
        for chunk in self._split_text(final_text):
            yield chunk

    def _consume_agent_stream_text(self, state: dict, chunk: str, model_name: str) -> str:
        if state.get("blocked"):
            return ""

        pending = "%s%s" % (state.get("pending") or "", chunk or "")
        if self._agent_stream_contains_internal_markup(pending, model_name=model_name):
            state["pending"] = ""
            state["in_think"] = False
            state["blocked"] = True
            return ""

        in_think = bool(state.get("in_think"))
        emitted: List[str] = []
        think_open = "<think>"
        think_close = "</think>"

        while pending:
            if in_think:
                close_index = pending.find(think_close)
                if close_index == -1:
                    reserve = len(think_close) - 1
                    pending = pending[-reserve:] if len(pending) > reserve else pending
                    break
                pending = pending[close_index + len(think_close) :]
                in_think = False
                continue

            open_index = pending.find(think_open)
            if open_index != -1:
                if open_index > 0:
                    emitted.append(pending[:open_index])
                pending = pending[open_index + len(think_open) :]
                in_think = True
                continue

            reserve = len(think_open) - 1
            if len(pending) > reserve:
                emitted.append(pending[:-reserve])
                pending = pending[-reserve:]
            break

        state["pending"] = pending
        state["in_think"] = in_think
        return "".join(emitted)

    def _flush_agent_stream_text(self, state: dict, model_name: str) -> str:
        if state.get("blocked"):
            state["pending"] = ""
            return ""
        if state.get("in_think"):
            state["pending"] = ""
            return ""
        pending = str(state.get("pending") or "")
        state["pending"] = ""
        return self._clean_agent_content(pending, model_name=model_name)

    def _extract_assistant_content(self, response: dict) -> str:
        choices = response.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    def _extract_assistant_message(self, response: dict) -> dict:
        choices = response.get("choices") or []
        message = choices[0].get("message") if choices and isinstance(choices[0].get("message"), dict) else {}
        assistant_message = {"role": "assistant", "content": str(message.get("content") or "")}
        for key in ("reasoning_content", "tool_calls"):
            value = message.get(key)
            if value not in (None, "", [], {}):
                assistant_message[key] = value
        return assistant_message

    def _extract_response_tool_calls(self, response: dict) -> List[Dict[str, Any]]:
        choices = response.get("choices") or []
        if not choices:
            return []
        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        parsed_calls: List[Dict[str, Any]] = []
        for raw_call in tool_calls:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
            arguments = function.get("arguments")
            params: Dict[str, Any] = {}
            if isinstance(arguments, str) and arguments.strip():
                try:
                    decoded = json.loads(arguments)
                    if isinstance(decoded, dict):
                        params = decoded
                except ValueError:
                    params = {}
            elif isinstance(arguments, dict):
                params = arguments
            normalized = self._normalize_tool_call(name=str(function.get("name") or raw_call.get("name") or ""), parameters=params)
            if normalized:
                if raw_call.get("id"):
                    normalized["tool_call_id"] = str(raw_call.get("id"))
                parsed_calls.append(normalized)
        return parsed_calls

    def _extract_tool_calls(self, content: str, model_name: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""

        calls.extend(self._extract_markdown_tool_calls(text))
        calls.extend(self._extract_wrapped_json_tool_calls(text))
        calls.extend(self._extract_pipe_json_tool_calls(text))
        calls.extend(self._extract_hash_arrow_tool_calls(text))
        calls.extend(self._extract_colon_args_tool_calls(text))
        calls.extend(self._extract_function_style_tool_calls(text))
        calls.extend(self._extract_xml_tool_calls(text))
        calls.extend(self._extract_tool_calls_variable(text))

        # ── Provider-specific tool call formats ──
        provider = self._get_provider(model_name)
        for pc in provider.extract_provider_tool_calls(text):
            normalized = self._normalize_tool_call(name=pc.get("name", ""), parameters=pc.get("parameters", {}))
            if normalized:
                calls.append(normalized)

        for match in re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", text, flags=re.DOTALL):
            parsed = self._parse_tool_call_block(match)
            if parsed:
                calls.append(parsed)

        for match in re.findall(r"\[TOOL_CALL\]\s*(.*?)\s*\[/TOOL_CALL\]", text, flags=re.DOTALL):
            parsed = self._parse_bracket_tool_call(match)
            if parsed:
                calls.append(parsed)

        for match in re.findall(r"\$TOOL_CALL\$\s*(.*?)\s*\$END\$", text, flags=re.DOTALL):
            parsed = self._parse_dollar_tool_call(match)
            if parsed:
                calls.append(parsed)

        for params_text, function_name in re.findall(
            r"\$PARAMS\s*=\s*(\{.*?\})\s*([A-Za-z_][A-Za-z0-9_]*)\(\$PARAMS\)",
            text,
            flags=re.DOTALL,
        ):
            parsed = self._parse_params_function_tool_call(function_name, params_text)
            if parsed:
                calls.append(parsed)

        for name, block in re.findall(r'<invoke name="([^"]+)">\s*(.*?)\s*</invoke>', text, flags=re.DOTALL):
            params = {}
            for key, value in re.findall(r'<parameter name="([^"]+)">(.*?)</parameter>', block, flags=re.DOTALL):
                params[key] = value.strip()
            parsed = self._normalize_tool_call(name=name, parameters=params)
            if parsed:
                calls.append(parsed)

        calls.extend(self._extract_inline_json_tool_calls(text))

        deduped: List[Dict[str, Any]] = []
        seen = set()
        for item in calls:
            identity = (item["name"], json.dumps(item.get("parameters") or {}, sort_keys=True, ensure_ascii=False))
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(item)
        return deduped

    def _extract_markdown_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""
        decoder = json.JSONDecoder()
        pattern = re.compile(
            r"\{\s*\*\*tool_name\*\*\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*\*\*tool_args\*\*\s*:\s*",
            flags=re.IGNORECASE,
        )

        for match in pattern.finditer(text):
            tool_name = match.group(1)
            json_start = text.find("{", match.end())
            if json_start < 0:
                continue
            try:
                params, _ = decoder.raw_decode(text[json_start:])
            except json.JSONDecodeError:
                continue
            parsed = self._normalize_tool_call(
                name=tool_name,
                parameters=params if isinstance(params, dict) else {},
            )
            if parsed:
                calls.append(parsed)

        return calls

    def _extract_wrapped_json_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""
        decoder = json.JSONDecoder()

        for match in re.finditer(r"\{", text):
            try:
                payload, _ = decoder.raw_decode(text[match.start() :])
            except json.JSONDecodeError:
                continue

            if not isinstance(payload, dict):
                continue

            tool_name = str(payload.get("tool") or payload.get("tool_name") or payload.get("name") or "").strip()
            if not tool_name:
                continue

            parameters = (
                payload.get("input")
                or payload.get("tool_args")
                or payload.get("arguments")
                or payload.get("parameters")
                or {}
            )
            parsed = self._normalize_tool_call(
                name=tool_name,
                parameters=parameters if isinstance(parameters, dict) else {},
            )
            if parsed:
                calls.append(parsed)

        return calls

    def _extract_pipe_json_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""
        decoder = json.JSONDecoder()

        for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*\|\s*\{", text):
            tool_name = match.group(1)
            json_start = text.find("{", match.start())
            if json_start < 0:
                continue
            try:
                params, _ = decoder.raw_decode(text[json_start:])
            except json.JSONDecodeError:
                continue
            parsed = self._normalize_tool_call(
                name=tool_name,
                parameters=params if isinstance(params, dict) else {},
            )
            if parsed:
                calls.append(parsed)

        return calls

    def _extract_inline_json_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        decoder = json.JSONDecoder()
        calls: List[Dict[str, Any]] = []
        text = content or ""

        for tool_name in ALLOWED_AGENT_TOOLS:
            pattern = r"%s\s*\(?\s*\{" % re.escape(tool_name)
            for match in re.finditer(pattern, text):
                json_start = text.find("{", match.start())
                if json_start < 0:
                    continue
                try:
                    params, _ = decoder.raw_decode(text[json_start:])
                except json.JSONDecodeError:
                    continue
                parsed = self._normalize_tool_call(
                    name=tool_name,
                    parameters=params if isinstance(params, dict) else {},
                )
                if parsed:
                    calls.append(parsed)

        return calls

    def _extract_hash_arrow_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""

        pattern = re.compile(
            r"tool_call\s*:\s*\{\s*tool\s*=>\s*['\"]([^'\"]+)['\"]\s*,\s*args\s*=>\s*\{(.*?)\}\s*\}",
            flags=re.DOTALL,
        )

        for tool_name, args_block in pattern.findall(text):
            parsed = self._normalize_tool_call(name=tool_name, parameters=self._parse_tool_parameter_block(args_block))
            if parsed:
                calls.append(parsed)

        return calls

    def _extract_colon_args_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""

        pattern = re.compile(
            r"\{\s*tool_call_id\s*:\s*[^,]+,\s*tool_name\s*:\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*,\s*args\s*:\s*\{(.*?)\}\s*\}",
            flags=re.DOTALL,
        )

        for tool_name, args_block in pattern.findall(text):
            parsed = self._normalize_tool_call(name=tool_name, parameters=self._parse_tool_parameter_block(args_block))
            if parsed:
                calls.append(parsed)

        return calls

    def _extract_function_style_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""
        tool_aliases = sorted(ALLOWED_AGENT_TOOLS | {name.replace("_", "-") for name in ALLOWED_AGENT_TOOLS}, key=len, reverse=True)
        tool_name_pattern = "|".join(re.escape(name) for name in tool_aliases)
        pattern = re.compile(r"\$?\b(%s)\s*\((.*?)\)" % tool_name_pattern, flags=re.DOTALL)

        for tool_name, args_text in pattern.findall(text):
            parsed = self._parse_function_style_tool_call(tool_name.replace("-", "_"), args_text)
            if parsed:
                calls.append(parsed)

        return calls

    def _extract_xml_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""
        decoder = json.JSONDecoder()
        pattern = re.compile(r'<tool\s+name=["\']([^"\']+)["\']\s*>\s*(.*?)\s*</tool>', flags=re.DOTALL | re.IGNORECASE)

        for tool_name, raw_params in pattern.findall(text):
            params: Dict[str, Any] = {}
            stripped = raw_params.strip()
            if stripped:
                try:
                    decoded, _ = decoder.raw_decode(stripped)
                    if isinstance(decoded, dict):
                        params = decoded
                except json.JSONDecodeError:
                    params = self._parse_tool_parameter_block(stripped)
            parsed = self._normalize_tool_call(name=tool_name, parameters=params)
            if parsed:
                calls.append(parsed)

        return calls

    def _extract_tool_calls_variable(self, content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""
        decoder = json.JSONDecoder()

        for match in re.finditer(r"\$TOOL_CALLS\s*=\s*", text, flags=re.IGNORECASE):
            array_start = text.find("[", match.end())
            if array_start < 0:
                continue
            try:
                payload, _ = decoder.raw_decode(text[array_start:])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, list):
                continue
            for raw_call in payload:
                if not isinstance(raw_call, dict):
                    continue
                tool_name = str(
                    raw_call.get("toolkit-name")
                    or raw_call.get("toolkit_name")
                    or raw_call.get("tool_name")
                    or raw_call.get("name")
                    or ""
                ).strip()
                params = raw_call.get("args") or raw_call.get("arguments") or raw_call.get("parameters") or {}
                parsed = self._normalize_tool_call(name=tool_name, parameters=params if isinstance(params, dict) else {})
                if parsed:
                    calls.append(parsed)

        return calls

    def _parse_tool_call_block(self, raw_text: str) -> Optional[Dict[str, Any]]:
        return self._parse_json_tool_call(raw_text) or self._parse_attr_tool_call(raw_text) or self._parse_inline_tool_call(raw_text)

    def _parse_attr_tool_call(self, raw_text: str) -> Optional[Dict[str, Any]]:
        text = (raw_text or "").strip()
        name_match = re.search(r'name\s*=\s*"([^"]+)"', text)
        if not name_match:
            return None
        tool_name = name_match.group(1).strip()

        params: Dict[str, Any] = {}
        params_match = re.search(r'(?:parameters|arguments)\s*=\s*"?\s*(\{.*\})', text, flags=re.DOTALL)
        if params_match:
            try:
                params = json.loads(params_match.group(1))
            except json.JSONDecodeError:
                pass
        return self._normalize_tool_call(name=tool_name, parameters=params if isinstance(params, dict) else {})

    def _parse_json_tool_call(self, raw_text: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(raw_text.strip())
        except json.JSONDecodeError:
            return None

        return self._normalize_tool_call(
            name=str(data.get("name") or "").strip(),
            parameters=data.get("parameters") or data.get("arguments") or {},
        )

    def _parse_inline_tool_call(self, raw_text: str) -> Optional[Dict[str, Any]]:
        text = (raw_text or "").strip()
        if not text:
            return None

        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$", text, flags=re.DOTALL)
        if not match:
            return None

        params: Dict[str, Any] = {}
        rest = match.group(2)
        for key, value in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"', rest):
            params[key] = value
        for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)='([^']*)'", rest):
            params.setdefault(key, value)
        for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=(-?\d+(?:\.\d+)?)", rest):
            params.setdefault(key, value)
        for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=(true|false)", rest, flags=re.IGNORECASE):
            params.setdefault(key, value)

        return self._normalize_tool_call(name=match.group(1), parameters=params)

    def _parse_bracket_tool_call(self, raw_text: str) -> Optional[Dict[str, Any]]:
        tool_match = re.search(r'tool\s*=>\s*["\']([^"\']+)["\']', raw_text)
        if not tool_match:
            return None

        args = self._extract_named_args_block(raw_text)

        return self._normalize_tool_call(name=tool_match.group(1), parameters=args)

    def _parse_dollar_tool_call(self, raw_text: str) -> Optional[Dict[str, Any]]:
        return self._parse_bracket_tool_call(raw_text)

    def _extract_named_args_block(self, raw_text: str) -> Dict[str, Any]:
        text = str(raw_text or "")
        args_match = re.search(r"args\s*(?:=>|:)\s*\{(.*?)\}", text, flags=re.DOTALL)
        args_source = args_match.group(1) if args_match else text
        return self._parse_tool_parameter_block(args_source)

    def _parse_tool_parameter_block(self, raw_text: str) -> Dict[str, Any]:
        args = self._parse_dash_parameters(raw_text)
        if args:
            return args

        parsed: Dict[str, Any] = {}
        for raw_line in str(raw_text or "").splitlines():
            line = raw_line.strip().rstrip(",")
            if not line or line in {"{", "}"} or line.startswith("#") or line.startswith("//"):
                continue

            separator = None
            for candidate in ("=>", "=", ":"):
                if candidate in line:
                    separator = candidate
                    break

            if separator is None:
                continue

            key, value = line.split(separator, 1)
            normalized_key = str(key).strip().strip("\"'")
            normalized_value = str(value).strip().rstrip(",")
            if not normalized_key:
                continue
            parsed[normalized_key] = self._parse_argument_token(normalized_value)

        return parsed

    def _parse_dash_parameters(self, raw_text: str) -> Dict[str, Any]:
        args: Dict[str, Any] = {}
        token_pattern = re.compile(
            r"--([a-zA-Z0-9_]+)\s+(\[[\s\S]*?\]|\{[\s\S]*?\}|\"[^\"]*\"|'[^']*'|[^\s}]+)",
            flags=re.DOTALL,
        )
        for key, value in token_pattern.findall(raw_text):
            args[key] = self._parse_argument_token(value)
        return args

    def _parse_function_style_tool_call(self, tool_name: str, args_text: str) -> Optional[Dict[str, Any]]:
        raw_call = "%s(%s)" % (tool_name, args_text)

        try:
            parsed = ast.parse(raw_call, mode="eval")
        except SyntaxError:
            return None

        expression = parsed.body
        if not isinstance(expression, ast.Call):
            return None

        parameters: Dict[str, Any] = {}
        for keyword in expression.keywords:
            if keyword.arg is None:
                continue
            parameters[keyword.arg] = self._parse_ast_argument_value(raw_call, keyword.value)

        return self._normalize_tool_call(name=tool_name, parameters=parameters)

    def _parse_ast_argument_value(self, raw_call: str, node: ast.AST) -> Any:
        try:
            return ast.literal_eval(node)
        except Exception:
            segment = ast.get_source_segment(raw_call, node)
            return segment.strip() if isinstance(segment, str) else None

    def _parse_argument_token(self, token: str) -> Any:
        text = str(token or "").strip()
        if not text:
            return ""

        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            return text[1:-1]

        if text[0] in "[{":
            try:
                return json.loads(text)
            except ValueError:
                return text

        return self._coerce_tool_value(text)

    def _parse_params_function_tool_call(self, function_name: str, params_text: str) -> Optional[Dict[str, Any]]:
        try:
            params = json.loads((params_text or "").strip())
        except json.JSONDecodeError:
            return None

        return self._normalize_tool_call(name=function_name, parameters=params if isinstance(params, dict) else {})

    def _normalize_tool_call(self, name: str, parameters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tool_name = (name or "").strip().replace("-", "_")
        if tool_name not in ALLOWED_AGENT_TOOLS:
            return None

        params = self._unwrap_tool_parameters(dict(parameters or {}))
        aliases = {
            "search_knowledge_base": {
                "question": "query",
                "keyword": "query",
                "keywords": "query",
                "queries": "query",
                "top_n": "top_k",
                "topk": "top_k",
                "top_k": "top_k",
                "max_results": "top_k",
                "doc_type": "ignored_doc_type",
                "type": "ignored_doc_type",
                "source": "ignored_doc_type",
                "priority": "ignored_priority",
            },
            "web_search": {
                "question": "query",
                "keyword": "query",
                "keywords": "query",
                "queries": "query",
                "search_query": "query",
            },
            "resolve_candidates": {
                "query": "product_query",
                "category": "product_query",
                "keywords": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "market": "marketplace",
                "target_market": "marketplace",
                "target_market_norm": "marketplace",
            },
            "candidate_pool_stats": {
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
                "query": "product_query",
                "category": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "market": "marketplace",
            },
            "candidate_pool_trends": {
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
                "query": "product_query",
                "category": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "market": "marketplace",
            },
            "candidate_pool_weak_forecast": {
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
                "query": "product_query",
                "category": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "market": "marketplace",
            },
            "top_asin_drilldown": {
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
                "query": "product_query",
                "category": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "market": "marketplace",
            },
            "asin_history_timeseries": {
                "asin": "asins",
                "as_i_n": "asins",
                "asin_code": "asins",
                "asin_codes": "asins",
                "asin_list": "asins",
                "candidate_asins": "asins",
                "candidate_list": "asins",
                "candidate_pool": "asins",
                "query": "product_query",
                "category": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "domain": "marketplace",
                "market": "marketplace",
            },
            "category_benchmark": {
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
                "query": "product_query",
                "category": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "market": "marketplace",
            },
            "keepa_asin_lookup": {
                "asin": "asins",
                "as_i_n": "asins",
                "asin_code": "asins",
                "asin_codes": "asins",
                "asin_list": "asins",
                "candidate_asins": "asins",
                "candidate_list": "asins",
                "domain": "marketplace",
                "market": "marketplace",
            },
        }

        normalized_params = {}
        for key, value in params.items():
            normalized_key = self._normalize_tool_parameter_key(key)
            mapped_key = aliases.get(tool_name, {}).get(normalized_key, normalized_key)
            if mapped_key in normalized_params and normalized_params[mapped_key] not in (None, "", [], {}):
                continue
            normalized_params[mapped_key] = value

        if tool_name == "search_knowledge_base" and isinstance(normalized_params.get("query"), list):
            normalized_params["query"] = " ".join(
                str(item).strip() for item in normalized_params["query"] if str(item).strip()
            )
        if tool_name == "resolve_candidates" and isinstance(normalized_params.get("product_query"), list):
            normalized_params["product_query"] = " ".join(
                str(item).strip() for item in normalized_params["product_query"] if str(item).strip()
            )

        method = getattr(self.agent_tools, tool_name, None) if self.agent_tools is not None else None
        if method is None:
            filtered = {
                key: self._coerce_tool_value(value)
                for key, value in normalized_params.items()
                if not key.startswith("ignored_")
            }
            return {"name": tool_name, "parameters": filtered}

        method_signature = signature(method)
        filtered_params = {}
        for param_name in method_signature.parameters:
            if param_name not in normalized_params:
                continue
            filtered_params[param_name] = self._coerce_tool_value(normalized_params[param_name])

        if "marketplace" in filtered_params:
            filtered_params["marketplace"] = self._normalize_marketplace_value(filtered_params["marketplace"])

        return {"name": tool_name, "parameters": filtered_params}

    def _unwrap_tool_parameters(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(parameters or {})
        wrapper_keys = {"input", "arguments", "parameters", "payload", "args", "kwargs", "tool_args"}

        while isinstance(params, dict) and len(params) == 1:
            key, value = next(iter(params.items()))
            if self._normalize_tool_parameter_key(key) not in wrapper_keys or not isinstance(value, dict):
                break
            params = dict(value)

        return params

    def _normalize_tool_parameter_key(self, key: Any) -> str:
        text = str(key or "").strip()
        if not text:
            return ""

        text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
        text = re.sub(r"[^A-Za-z0-9]+", "_", text)
        return text.strip("_").lower()

    def _normalize_marketplace_value(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text:
            return value

        compact = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
        direct_map = {
            "GB": "UK",
            "COM": "US",
            "AMAZON_COM": "US",
            "US_COM": "US",
        }
        known_codes = {"US", "UK", "DE", "FR", "JP", "CA", "IT", "ES", "IN", "MX", "BR", "AU"}

        if compact in direct_map:
            return direct_map[compact]
        if compact in known_codes:
            return compact

        suffix = compact.split("_")[-1] if compact else ""
        if suffix in direct_map:
            return direct_map[suffix]
        if suffix in known_codes:
            return suffix

        return value

    def _coerce_tool_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            nested_values = (
                value.get("candidate_asins")
                or value.get("asins")
                or value.get("items")
                or value.get("candidates")
                or value.get("data")
            )
            if nested_values is not None:
                return self._coerce_tool_value(nested_values)
            return value

        if isinstance(value, list):
            normalized_items = []
            for item in value:
                if isinstance(item, dict):
                    asin_like = item.get("asin") or item.get("code") or item.get("id")
                    normalized_items.append(asin_like if asin_like is not None else item)
                else:
                    normalized_items.append(item)
            return normalized_items

        if not isinstance(value, str):
            return value

        text = value.strip()
        lower = text.lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
        if re.fullmatch(r"-?\d+", text):
            try:
                return int(text)
            except ValueError:
                return text
        if re.fullmatch(r"-?\d+\.\d+", text):
            try:
                return float(text)
            except ValueError:
                return text
        return text

    def _execute_tool_call(self, tool_call: Dict[str, Any], billing_context: dict, truncate: bool = True) -> str:
        tool_name = tool_call["name"]
        parameters = tool_call.get("parameters") or {}
        method = getattr(self.agent_tools, tool_name, None) if self.agent_tools is not None else None

        if method is None or tool_name.startswith("_"):
            return "工具 %s 未加载或不可用。" % tool_name

        billing_event = TOOL_BILLING_EVENT.get(tool_name)
        tool_charge = None
        if billing_event is not None:
            try:
                tool_charge = self._charge_billing_event(
                    billing_context=billing_context,
                    event_type=billing_event,
                    description="工具调用：%s" % self._tool_name_label(tool_name),
                    meta={
                        "tool_name": tool_name,
                        "parameters": parameters,
                    },
                )
            except RuntimeError as exc:
                return "工具 %s 执行前积分校验失败: %s" % (tool_name, str(exc))

        try:
            result = method(**parameters)
        except Exception as exc:
            if tool_charge is not None:
                self._refund_billing_event(
                    billing_context=billing_context,
                    charge=tool_charge,
                    description="工具调用失败：%s" % self._tool_name_label(tool_name),
                    meta={"tool_name": tool_name, "error": str(exc)[:500]},
                )
            return "工具 %s 执行失败: %s" % (tool_name, str(exc))

        result_text = str(result or "")
        if tool_charge is not None and self._tool_result_has_error(result_text):
            self._refund_billing_event(
                billing_context=billing_context,
                charge=tool_charge,
                description="工具调用异常：%s" % self._tool_name_label(tool_name),
                meta={"tool_name": tool_name, "result_preview": result_text[:500]},
            )
        if truncate and len(result_text) > 12000:
            return "%s\n\n[结果已截断，原始长度 %d 字符]" % (result_text[:12000], len(result_text))
        return result_text

    def _build_tool_observation(self, tool_call: Dict[str, Any], result: str) -> dict:
        return {
            "tool_name": str(tool_call.get("name") or ""),
            "arguments": tool_call.get("parameters") or {},
            "raw_result": str(result or ""),
            "llm_result": self._format_tool_result_for_llm(tool_name=str(tool_call.get("name") or ""), result=result),
        }

    def _format_tool_result_for_llm(self, tool_name: str, result: str, budget: int = 9000) -> str:
        result_text = str(result or "").strip()
        if not result_text:
            return "工具返回为空。"
        if self._tool_result_has_error(result_text):
            return result_text[:budget]

        payload = self._load_tool_json_payload(result_text)
        if payload is None:
            return self._truncate_text_for_llm(result_text, budget=budget)

        compact_payload = self._compact_tool_payload_for_llm(
            tool_name=tool_name,
            payload=payload,
            max_depth=5,
            max_items=12,
            max_scalar_items=40,
            max_string=700,
        )
        envelope = {
            "tool_name": tool_name,
            "result_format": "compacted_json",
            "original_chars": len(result_text),
            "compaction_note": "Large arrays/strings may be shortened; use visible counts and omitted markers when reasoning.",
            "payload": compact_payload,
        }
        rendered = json.dumps(envelope, ensure_ascii=False, indent=2)
        if len(rendered) <= budget:
            return rendered

        compact_payload = self._compact_tool_payload_for_llm(
            tool_name=tool_name,
            payload=payload,
            max_depth=4,
            max_items=6,
            max_scalar_items=30,
            max_string=350,
        )
        envelope["payload"] = compact_payload
        rendered = json.dumps(envelope, ensure_ascii=False, indent=2)
        if len(rendered) <= budget:
            return rendered

        if str(tool_name or "") == "resolve_candidates":
            for item_limit, string_limit in ((6, 80), (6, 60), (6, 32), (6, 24), (4, 120), (2, 120), (1, 120)):
                compact_payload = self._compact_candidate_pool_payload(payload, max_items=item_limit, max_string=string_limit)
                envelope["payload"] = compact_payload
                rendered = json.dumps(envelope, ensure_ascii=False, indent=2)
                if len(rendered) <= budget:
                    return rendered

        return self._truncate_text_for_llm(rendered, budget=budget)

    def _compact_tool_payload_for_llm(
        self,
        tool_name: str,
        payload: dict,
        *,
        max_depth: int,
        max_items: int,
        max_scalar_items: int,
        max_string: int,
    ) -> Any:
        normalized_tool = str(tool_name or "").strip()
        if normalized_tool == "resolve_candidates":
            return self._compact_candidate_pool_payload(payload, max_items=max_items, max_string=max_string)
        if normalized_tool == "asin_history_timeseries":
            return self._compact_asin_history_payload(payload, max_items=max_items, max_string=max_string)
        return self._compact_json_value(
            payload,
            max_depth=max_depth,
            max_items=max_items,
            max_scalar_items=max_scalar_items,
            max_string=max_string,
        )

    def _compact_candidate_pool_payload(self, payload: dict, *, max_items: int, max_string: int) -> dict:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not data:
            return self._compact_json_value(payload, max_depth=4, max_items=max_items, max_scalar_items=30, max_string=max_string)

        compact_data = self._copy_keys(
            data,
            [
                "marketplace",
                "domain",
                "raw_product_query",
                "normalized_query",
                "candidate_count",
                "candidate_total_before_truncate",
                "candidate_total_before_semantic_category_anchor",
                "semantic_fine_category_anchor_applied",
                "semantic_category_anchor_applied",
                "candidate_sql_prefilter_count",
                "candidate_sql_prefilter_limit",
                "candidate_sql_prefilter_truncated",
                "truncated",
                "query_phrases",
                "query_tokens",
                "required_product_terms",
                "candidate_asins",
                "matched_categories",
                "matched_leaf_categories",
                "matched_fine_categories",
                "matched_root_categories",
            ],
        )

        matched_keywords = data.get("matched_keywords")
        if isinstance(matched_keywords, list):
            compact_data["matched_keywords"] = self._compact_json_value(
                matched_keywords,
                max_depth=2,
                max_items=10,
                max_scalar_items=10,
                max_string=80,
            )

        query_normalization = data.get("query_normalization")
        if isinstance(query_normalization, dict):
            compact_query_normalization = self._copy_keys(
                query_normalization,
                [
                    "mode",
                    "llm_used",
                    "pipeline_mode",
                    "pipeline_llm_used",
                    "llm_language",
                    "llm_confidence",
                    "llm_error",
                    "normalized_product_query",
                    "normalized_query_aliases",
                    "normalized_category_hints",
                ],
            )
            for stage_key in ["theme_extraction", "recall_normalization"]:
                stage = query_normalization.get(stage_key)
                if isinstance(stage, dict):
                    compact_query_normalization[stage_key] = self._copy_keys(
                        stage,
                        [
                            "mode",
                            "llm_used",
                            "llm_language",
                            "llm_confidence",
                            "llm_error",
                            "extracted_theme",
                            "normalized_product_query",
                            "extracted_query_aliases",
                            "normalized_query_aliases",
                            "extracted_category_hints",
                            "normalized_category_hints",
                        ],
                    )
            compact_data["query_normalization"] = self._compact_json_value(
                compact_query_normalization,
                max_depth=3,
                max_items=8,
                max_scalar_items=16,
                max_string=120,
            )

        ranking_policy = data.get("ranking_policy")
        if isinstance(ranking_policy, dict):
            compact_ranking_policy = self._copy_keys(
                ranking_policy,
                ["primary_sort", "match_score_components", "matched_fields", "note"],
            )
            if isinstance(compact_ranking_policy.get("note"), str):
                compact_ranking_policy["note"] = self._truncate_text_for_llm(compact_ranking_policy["note"], budget=140)
            compact_data["ranking_policy"] = self._compact_json_value(
                compact_ranking_policy,
                max_depth=3,
                max_items=8,
                max_scalar_items=16,
                max_string=140,
            )

        timing_ms = data.get("timing_ms")
        if isinstance(timing_ms, dict):
            compact_data["timing_ms"] = self._copy_keys(
                timing_ms,
                ["query_normalization", "domain_candidate_fetch", "scoring_and_sorting", "total"],
            )

        recall_notes = data.get("recall_notes")
        if isinstance(recall_notes, list):
            compact_data["recall_notes"] = self._compact_json_value(
                recall_notes,
                max_depth=2,
                max_items=3,
                max_scalar_items=3,
                max_string=160,
            )

        candidate_items = data.get("candidate_items") if isinstance(data.get("candidate_items"), list) else []
        candidate_item_limit = min(50, max(max_items, len(candidate_items))) if max_items >= 12 else min(max_items, len(candidate_items))
        compact_data["candidate_items"] = [
            self._compact_candidate_item(item, max_string=max_string)
            for item in candidate_items[:candidate_item_limit]
            if isinstance(item, dict)
        ]
        compact_data["candidate_items_visible_count"] = len(
            [item for item in compact_data["candidate_items"] if isinstance(item, dict) and item.get("asin")]
        )
        compact_data["candidate_pool_contract"] = {
            "candidate_asins": "full ranked ASIN pool for downstream tools such as candidate_pool_stats/trends/weak_forecast",
            "candidate_items": "budgeted visible details for reasoning/filtering; omitted details do not remove ASINs from the pool",
        }
        if len(candidate_items) > candidate_item_limit:
            compact_data["candidate_items"].append({"_omitted_items": len(candidate_items) - candidate_item_limit, "_total_items": len(candidate_items)})

        compact_payload = self._copy_keys(payload, ["success", "code", "message"])
        compact_payload["data"] = compact_data
        return compact_payload

    def _compact_candidate_item(self, item: dict, *, max_string: int) -> dict:
        candidate_string_limit = min(max_string, 160)
        compact_item = self._copy_keys(
            item,
            [
                "asin",
                "brand",
                "product_title",
                "category",
                "category_path",
                "root_category_name",
                "category_l2_name",
                "category_l3_name",
                "leaf_category_name",
                "fine_category_name",
                "current_price",
                "current_rating",
                "current_review_count",
                "current_bsr",
                "history_rows_30d",
                "latest_history_date",
                "sql_prefilter_score",
                "match_score",
                "match_reasons",
            ],
        )
        return self._compact_json_value(compact_item, max_depth=3, max_items=8, max_scalar_items=16, max_string=candidate_string_limit)

    def _compact_asin_history_payload(self, payload: dict, *, max_items: int, max_string: int) -> dict:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not data:
            return self._compact_json_value(payload, max_depth=4, max_items=max_items, max_scalar_items=30, max_string=max_string)

        compact_data = self._copy_keys(
            data,
            [
                "marketplace",
                "domain",
                "window_days",
                "interval",
                "metrics",
                "source_preference",
                "requested_asin_count",
                "local_history_hit_count",
                "missing_local_history_asin_count",
                "fallback_keepa_snapshot",
            ],
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        compact_data["items"] = [
            self._compact_asin_history_item(item, max_string=max_string)
            for item in items[:max_items]
            if isinstance(item, dict)
        ]
        if len(items) > max_items:
            compact_data["items"].append({"_omitted_items": len(items) - max_items, "_total_items": len(items)})

        compact_payload = self._copy_keys(payload, ["success", "code", "message"])
        compact_payload["data"] = compact_data
        return compact_payload

    def _compact_asin_history_item(self, item: dict, *, max_string: int) -> dict:
        latest_snapshot = item.get("latest_snapshot") if isinstance(item.get("latest_snapshot"), dict) else {}
        window_summary = item.get("window_summary") if isinstance(item.get("window_summary"), dict) else {}
        compact_item = self._copy_keys(item, ["asin", "history_status"])
        compact_item["latest_snapshot"] = self._compact_json_value(
            self._copy_keys(
                latest_snapshot,
                [
                    "asin",
                    "product_title",
                    "brand",
                    "category",
                    "category_path",
                    "l3_category_name",
                    "leaf_category_name",
                    "effective_price",
                    "rating",
                    "review_count",
                    "offer_count",
                    "bsr",
                    "estimated_daily_sales",
                    "latest_date",
                    "source",
                ],
            ),
            max_depth=3,
            max_items=8,
            max_scalar_items=16,
            max_string=max_string,
        )
        compact_item["window_summary"] = self._compact_json_value(
            self._copy_keys(
                window_summary,
                [
                    "sales_window_sum",
                    "sales_daily_avg",
                    "price_min_window",
                    "price_max_window",
                    "review_growth_window",
                    "offer_count_avg_window",
                    "bsr_avg_window",
                    "series_row_count",
                    "coverage_ratio",
                ],
            ),
            max_depth=2,
            max_items=8,
            max_scalar_items=16,
            max_string=max_string,
        )
        if isinstance(item.get("series"), list):
            compact_item["series"] = self._compact_series_items(item["series"], max_string=max_string)
        return compact_item

    def _copy_keys(self, source: dict, keys: List[str]) -> dict:
        return {key: source[key] for key in keys if key in source and source[key] not in (None, "", [], {})}

    def _compact_json_value(
        self,
        value: Any,
        *,
        max_depth: int,
        max_items: int,
        max_scalar_items: int,
        max_string: int,
        depth: int = 0,
    ) -> Any:
        if depth >= max_depth:
            return self._compact_leaf_value(value, max_string=max_string)

        if isinstance(value, dict):
            compacted = {}
            for key, nested_value in value.items():
                if str(key) == "series" and isinstance(nested_value, list):
                    compacted[str(key)] = self._compact_series_items(nested_value, max_string=max_string)
                    continue
                compacted[str(key)] = self._compact_json_value(
                    nested_value,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_scalar_items=max_scalar_items,
                    max_string=max_string,
                    depth=depth + 1,
                )
            return compacted

        if isinstance(value, list):
            item_limit = max_scalar_items if all(not isinstance(item, (dict, list)) for item in value) else max_items
            compacted_items = [
                self._compact_json_value(
                    item,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_scalar_items=max_scalar_items,
                    max_string=max_string,
                    depth=depth + 1,
                )
                for item in value[:item_limit]
            ]
            if len(value) > item_limit:
                compacted_items.append({"_omitted_items": len(value) - item_limit, "_total_items": len(value)})
            return compacted_items

        return self._compact_leaf_value(value, max_string=max_string)

    def _compact_series_items(self, items: List[Any], *, max_string: int) -> dict:
        first_items = items[:2]
        last_items = items[-2:] if len(items) > 2 else []
        return {
            "_compacted_series": True,
            "total_items": len(items),
            "first_items": [self._compact_leaf_value(item, max_string=max_string) for item in first_items],
            "last_items": [self._compact_leaf_value(item, max_string=max_string) for item in last_items],
        }

    def _compact_leaf_value(self, value: Any, *, max_string: int) -> Any:
        if isinstance(value, str) and len(value) > max_string:
            return "%s...[truncated %d chars]" % (value[:max_string], len(value) - max_string)
        return value

    def _truncate_text_for_llm(self, text: str, budget: int) -> str:
        if len(text) <= budget:
            return text
        return "%s\n\n[结果已压缩截断，原始长度 %d 字符]" % (text[:budget], len(text))

    def _fallback_answer_from_tool_observations(self, tool_observations: List[dict], error: str = "") -> str:
        lines = [
            "工具已经执行完成，但模型整理最终答复时失败；先返回工具结果摘要，方便继续分析。",
        ]
        if error:
            lines.append("失败原因：%s" % str(error).strip()[:500])
        for index, observation in enumerate(tool_observations[-3:], start=1):
            lines.extend(
                [
                    "",
                    "### 工具结果 %d：%s" % (index, observation.get("tool_name") or "unknown"),
                    "参数：`%s`" % json.dumps(observation.get("arguments") or {}, ensure_ascii=False),
                    "```json",
                    str(observation.get("llm_result") or "")[:9000],
                    "```",
                ]
            )
        return "\n".join(lines)

    def _agent_stream_contains_internal_markup(self, text: str, model_name: str) -> bool:
        return self._get_provider(model_name).has_internal_markup(text)

    def _strip_agent_internal_markup(self, content: str, model_name: str) -> str:
        return self._get_provider(model_name).strip_internal_markup(content)

    def _clean_agent_content(self, content: str, model_name: str) -> str:
        cleaned = self._strip_agent_internal_markup(content, model_name=model_name)
        return self._strip_agent_function_tool_markup(cleaned)

    def _strip_agent_function_tool_markup(self, content: str) -> str:
        text = content or ""
        tool_aliases = sorted(ALLOWED_AGENT_TOOLS | {name.replace("_", "-") for name in ALLOWED_AGENT_TOOLS}, key=len, reverse=True)
        tool_name_pattern = "|".join(re.escape(name) for name in tool_aliases)
        text = re.sub(r"\$TOOL_CALLS\s*=\s*\[.*?\](?=\s*\$ABORT_CONTROLLER|\s*\$TOOL_CALLS|\s*$)", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"\$ABORT_CONTROLLER\s*=\s*[^\n$]*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\$?\b(?:%s)\s*\([^)]*\)" % tool_name_pattern, "", text, flags=re.DOTALL)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _disable_web_search_feature(self, body: dict) -> None:
        features = body.get("features")
        if not isinstance(features, dict):
            features = {}
            body["features"] = features
        features["web_search"] = False

    def _chat_response(self, content: str, model: str) -> dict:
        return {
            "id": "%s-%s" % (model, uuid.uuid4()),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }

    def _stream_text_response(self, content: str, model: str) -> Iterator[bytes]:
        response_id = "%s-%s" % (model, uuid.uuid4())
        created = int(time.time())
        emitted = False

        for chunk in self._split_text(content):
            emitted = True
            yield self._stream_content_chunk(
                response_id=response_id,
                created=created,
                model=model,
                content=chunk,
            )

        if not emitted:
            yield self._stream_content_chunk(
                response_id=response_id,
                created=created,
                model=model,
                content="",
            )

        yield self._stream_stop_chunk(response_id=response_id, created=created, model=model)
        yield b"data: [DONE]\n\n"

    def _split_text(self, content: str, chunk_size: int = 800) -> List[str]:
        text = content or ""
        if not text:
            return []
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

    def _stream_content_chunk(self, response_id: str, created: int, model: str, content: str) -> bytes:
        return self._sse_chunk(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": content},
                        "finish_reason": None,
                    }
                ],
            }
        )

    def _stream_reasoning_open_chunk(self, response_id: str, created: int, model: str) -> bytes:
        return self._stream_content_chunk(
            response_id=response_id,
            created=created,
            model=model,
            content="<think>",
        )

    def _stream_reasoning_text_chunk(self, response_id: str, created: int, model: str, content: str) -> bytes:
        return self._stream_content_chunk(
            response_id=response_id,
            created=created,
            model=model,
            content=content,
        )

    def _stream_reasoning_close_chunk(self, response_id: str, created: int, model: str) -> bytes:
        return self._stream_content_chunk(
            response_id=response_id,
            created=created,
            model=model,
            content="</think>",
        )

    def _stream_stop_chunk(self, response_id: str, created: int, model: str) -> bytes:
        return self._sse_chunk(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )

    def _sse_chunk(self, payload: dict) -> bytes:
        return ("data: %s\n\n" % json.dumps(payload, ensure_ascii=False)).encode("utf-8")

    def _inject_agent_system_prompt(self, messages: List[dict], mode: str = "agent") -> List[dict]:
        clean_messages = deepcopy(messages or [])
        system_prompt = self._agent_system_prompt_for_mode(mode)
        if clean_messages:
            first_message = clean_messages[0]
            if first_message.get("role") == "system":
                if first_message.get("content") == system_prompt:
                    return clean_messages
                if first_message.get("content") in {AGENT_SYSTEM_PROMPT, TOOL_ONLY_SYSTEM_PROMPT}:
                    first_message["content"] = system_prompt
                    return clean_messages
        clean_messages.insert(0, {"role": "system", "content": system_prompt})
        return clean_messages

    def _agent_system_prompt_for_mode(self, mode: str) -> str:
        return TOOL_ONLY_SYSTEM_PROMPT if mode == "tool" else AGENT_SYSTEM_PROMPT

    def _user_id(self, body: dict) -> str:
        user = body.get("user")
        if isinstance(user, str) and user.strip():
            return user.strip()
        if isinstance(user, dict):
            for key in ("id", "email", "name"):
                value = user.get(key)
                if value:
                    return str(value)
        raise RuntimeError("Open WebUI 当前请求缺少已登录用户，已禁用共享 open-webui-user fallback。")

    def _user_email(self, body: dict) -> str:
        user = body.get("user")
        if isinstance(user, dict):
            value = user.get("email")
            if value:
                return str(value)
        return "%s@openwebui.local" % self._user_id(body)

    def _user_name(self, body: dict) -> str:
        user = body.get("user")
        if isinstance(user, dict):
            value = user.get("name")
            if value:
                return str(value)
        return self._user_id(body)

    def _base_chat_backend_url(self) -> str:
        return (self.valves.CHAT_BACKEND_BASE_URL or "").rstrip("/")

    def _error_text(self, detail: str) -> str:
        detail = (detail or "").strip()
        if not detail:
            return "请求失败。"
        return "请求失败:\n%s" % detail[:4000]
