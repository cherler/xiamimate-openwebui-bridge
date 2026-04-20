"""
title: XiaMimate Bridge Manifold
author: GitHub Copilot
date: 2026-04-14
version: 0.2.0
description: Open WebUI manifold that exposes the single XiaMimate agent model with /workflow routing.
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
3. 需要商品数据时，先调用 resolve_candidates 拿到 candidate_asins，再调用 candidate_pool_stats / candidate_pool_trends / candidate_pool_weak_forecast / top_asin_drilldown / category_benchmark。
4. 当 top_asin_drilldown 返回空结果或用户提供了本地数据库可能没有的 ASIN 时，使用 keepa_asin_lookup 直连 Keepa API 查询实时数据。
4. 如果工具尚未返回数据，只能给出分析框架、验证路径和风险提醒，明确标注为待验证。
5. 输出尽量围绕结论、证据、风险、下一步动作。
6. 每个结论标注数据来源类型：知识库 / 推理 / 工具数据。

工具调用规则：
- 当你决定调用工具时，直接输出工具调用指令，不要在工具调用之前添加任何文字（如"好的，我来帮你…"等）。
- 等工具返回结果后，再给出分析和回答。
- 如果需要同时调用多个工具，可以连续输出多个工具调用。
- 后续工具如果依赖上一步输出参数，不要在同一轮猜测这些参数；先等待上一步工具结果。

可用工具概览：
- search_knowledge_base: 检索跨境电商知识库（平台规则、运营指南、市场洞察）
- web_search: 联网搜索最新外部信息并返回总结（平台动态、行业新闻、竞争情报、消费者趋势）
- resolve_candidates: 解析候选 ASIN 池
- candidate_pool_stats: 候选池描述统计
- candidate_pool_trends: 候选池趋势诊断
- candidate_pool_weak_forecast: 弱信号预测标记
- top_asin_drilldown: 头部 ASIN 下钻
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
    "category_benchmark",
    "keepa_asin_lookup",
}

COMMAND_TO_MODE = {
    "/agent": "agent",
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
    "kb_retrieve": 2,
    "product_api_call": 2,
    "web_search": 2,
}

TOOL_BILLING_EVENT = {
    "search_knowledge_base": "kb_retrieve",
    "web_search": "web_search",
    "resolve_candidates": "product_api_call",
    "candidate_pool_stats": "product_api_call",
    "candidate_pool_trends": "product_api_call",
    "candidate_pool_weak_forecast": "product_api_call",
    "top_asin_drilldown": "product_api_call",
    "category_benchmark": "product_api_call",
    "keepa_asin_lookup": "product_api_call",
}

WORKFLOW_SUGGESTION_PROMPTS = [
    {
        "title": ["/workflow 示例", "宠物自动喂食器在 TikTok 美国市场的前景"],
        "content": "/workflow 帮我调研一下宠物自动喂食器在 TikTok 美国市场的前景",
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
        CHAT_BACKEND_TIMEOUT: int = 30
        CHAT_BACKEND_SERVICE_SECRET: str = ""
        CHAT_BACKEND_SERVICE_NAME: str = "open-webui-pipeline"
        AGENT_OPENAI_MODEL: str = "MiniMax-M2.7-highspeed"
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
                "CHAT_BACKEND_TIMEOUT": int(os.getenv("CHAT_BACKEND_TIMEOUT", "30")),
                "CHAT_BACKEND_SERVICE_SECRET": os.getenv("CHAT_BACKEND_SERVICE_SECRET", ""),
                "CHAT_BACKEND_SERVICE_NAME": os.getenv("CHAT_BACKEND_SERVICE_NAME", "open-webui-pipeline"),
                "AGENT_OPENAI_MODEL": os.getenv("AGENT_OPENAI_MODEL", "MiniMax-M2.7-highspeed"),
                "XIAMIMATE_MODEL_PREFIX": os.getenv("XIAMIMATE_MODEL_PREFIX", "xiamimate"),
            }
        )
        self.pipelines = [
            {
                "id": "agent",
                "name": "Agent",
                "info": {
                    "meta": {
                        "description": "虾米选品的智能体模式，支持 /workflow 调用 Dify Chatflow，并在长流程中显示进度。",
                        "capabilities": {
                            "status_updates": True,
                        },
                        "suggestion_prompts": WORKFLOW_SUGGESTION_PROMPTS,
                    }
                },
            },
        ]

    async def on_startup(self):
        print("on_startup:xiamimate")

    async def on_shutdown(self):
        print("on_shutdown:xiamimate")

    async def on_valves_updated(self):
        self.id = self.valves.XIAMIMATE_MODEL_PREFIX

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

    def _get_provider(self) -> ProviderStrategy:
        """Resolve the LLM provider strategy based on the configured model name."""
        return get_provider(self.valves.AGENT_OPENAI_MODEL)

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: List[dict],
        body: dict,
    ) -> Union[str, dict, Iterator[bytes]]:
        response_model = "%s.agent" % self.id
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
        if mode == "web":
            return self._run_web_search(query=normalized_user_message, body=body, model=response_model)
        if mode in {"agent", "tool"}:
            return self._run_agent(messages=normalized_messages, body=body, model=response_model, mode=mode)

        return self._chat_response(content="未识别的 XiaMimate 模式。请使用 Agent。", model=response_model)

    def _run_workflow(self, query: str, body: dict, model: str) -> Union[dict, Iterator[bytes]]:
        return self._run_dify_chatflow(
            query=query,
            body=body,
            model=model,
            event_type="workflow_run",
            charge_description="Workflow 请求",
            run_path="/internal/provider/dify-workflow/run",
            run_stream_path="/internal/provider/dify-workflow/run-stream",
            mode_tag="workflow",
            guidance=(
                "请在 /workflow 后直接写出调研需求，例如：\n"
                "/workflow 帮我调研一下宠物自动喂食器在 TikTok 美国市场的前景"
            ),
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
            )

        try:
            response = self._chat_backend_request(
                method="POST",
                path=run_path,
                body={
                    "query": query,
                    "user": billing_context["user_id"],
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
            return self._chat_response(content=answer, model=model)

        return self._chat_response(content=json.dumps(response, ensure_ascii=False, indent=2), model=model)

    def _run_agent(
        self,
        messages: List[dict],
        body: dict,
        model: str,
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
                billing_context=billing_context,
                mode=mode,
            )

        try:
            answer = self._run_agent_loop(messages=messages, body=body, billing_context=billing_context, mode=mode)
        except RuntimeError as exc:
            return self._error_text(str(exc))

        return self._chat_response(content=answer, model=model)

    def _run_agent_stream(
        self,
        messages: List[dict],
        body: dict,
        model: str,
        billing_context: dict,
        mode: str,
    ) -> Iterator[bytes]:
        response_id = "%s-%s" % (model, uuid.uuid4())
        created = int(time.time())
        conversation = deepcopy(messages or [])
        answer_started = False
        used_tools = False
        reasoning_open = False

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
                payload = self._prepare_agent_payload(messages=conversation, body=body, mode=mode)
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
                    response = self._post_agent_payload(payload)
                except RuntimeError as exc:
                    self._refund_billing_event(
                        billing_context=billing_context,
                        charge=minimax_charge,
                        description="LLM 请求失败，已退款",
                        meta={"mode": "agent", "stream": True, "error": str(exc)[:500]},
                    )
                    raise

                content = self._extract_assistant_content(response)
                tool_calls = self._extract_tool_calls(content)

                if not tool_calls:
                    cleaned = self._clean_agent_content(content)
                    if cleaned:
                        final_answer = cleaned
                    elif self._agent_stream_contains_internal_markup(content):
                        final_answer = "已完成分析，但未生成可展示的结果，请重试。"
                    else:
                        final_answer = str(content or "").strip()
                    status_line = "正在生成最终答复" if round_index == 0 else "工具执行完成，正在生成最终答复"
                    for chunk in emit_reasoning_chunks(self._format_agent_progress(status_line)):
                        yield chunk

                    close_chunk = close_reasoning_chunk()
                    if close_chunk is not None:
                        yield close_chunk

                    if used_tools or self._agent_stream_contains_internal_markup(content):
                        for chunk in self._split_text(final_answer):
                            answer_started = True
                            yield emit_text_chunk(chunk)
                    else:
                        for chunk in self._stream_agent_final_answer_chunks(payload=payload, fallback_content=final_answer):
                            answer_started = True
                            yield emit_text_chunk(chunk)
                    break

                used_tools = True
                conversation.append({"role": "assistant", "content": content})

                tool_names = ", ".join(tool_call["name"] for tool_call in tool_calls)
                for chunk in emit_reasoning_chunks(self._format_agent_progress("正在调用工具: %s" % tool_names)):
                    yield chunk

                tool_results = []
                for tool_call in tool_calls:
                    result = self._execute_tool_call(tool_call, billing_context)
                    tool_results.append(
                        TOOL_RESULT_TEMPLATE.format(
                            tool_name=tool_call["name"],
                            arguments=json.dumps(tool_call.get("parameters") or {}, ensure_ascii=False),
                            result=result,
                        )
                    )
                    tool_status = "失败" if self._tool_result_has_error(result) else "完成"
                    for chunk in emit_reasoning_chunks(
                        self._format_agent_progress("工具 %s 已%s" % (tool_call["name"], tool_status))
                    ):
                        yield chunk

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

        mode = requested_mode or command_mode or (model_mode if model_mode in {"agent", "tool", "workflow"} else "agent")
        query = (command_query if command_mode else (user_message or last_user_text or "")).strip()
        return mode, query, command_mode is not None

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
            lines.append("- 计费提示：一次 Workflow 固定按 8 积分计费，内部检索和工具调用会保留记录，但当前不会重复额外收费。")
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
            lines.append("- 常见计价：LLM 请求 1 积分/次，知识库检索 2 积分/次，商品 API 检索 2 积分/次，网络搜索 2 积分/次，Workflow 8 积分/次。")
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
            "workflow_run": "Workflow 请求",
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
            "Dify workflow run": "一次 Workflow 固定计费 8 积分，内部步骤只保留审计记录，不重复收费。",
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
                "workflow_run": "一次 Workflow 固定计费 8 积分，内部检索和工具调用只保留记录，不重复收费。",
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
    ) -> Iterator[bytes]:
        response_id = "%s-%s" % (model, uuid.uuid4())
        created = int(time.time())
        finished_nodes = set()
        answer_started = False
        answer_chunks: List[str] = []
        emitted_progress = set()
        reasoning_open = False

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
                    final_answer = "".join(answer_chunks).strip() or "执行已完成，但未返回可展示的结果。"
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

    def _prepare_agent_payload(self, messages: List[dict], body: dict, mode: str = "agent") -> dict:
        provider = self._get_provider()
        payload = provider.filter_payload(body)
        payload["model"] = self.valves.AGENT_OPENAI_MODEL
        payload["messages"] = self._inject_agent_system_prompt(messages, mode=mode)

        user_value = payload.get("user")
        if isinstance(user_value, dict):
            payload["user"] = self._user_id(body)

        return payload

    def _run_agent_loop(
        self,
        messages: List[dict],
        body: dict,
        billing_context: dict,
        mode: str = "agent",
    ) -> str:
        conversation = deepcopy(messages or [])

        for _ in range(6):
            payload = self._prepare_agent_payload(messages=conversation, body=body, mode=mode)
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
                response = self._post_agent_payload(payload)
            except RuntimeError as exc:
                self._refund_billing_event(
                    billing_context=billing_context,
                    charge=minimax_charge,
                    description="LLM 请求失败，已退款",
                    meta={"mode": "agent", "error": str(exc)[:500]},
                )
                raise
            content = self._extract_assistant_content(response)
            tool_calls = self._extract_tool_calls(content)

            if not tool_calls:
                cleaned = self._clean_agent_content(content)
                if cleaned:
                    return cleaned
                if self._agent_stream_contains_internal_markup(content):
                    return "已完成分析，但未生成可展示的结果，请重试。"
                return str(content or "").strip()

            conversation.append({"role": "assistant", "content": content})

            tool_results = []
            for tool_call in tool_calls:
                result = self._execute_tool_call(tool_call, billing_context)
                tool_results.append(
                    TOOL_RESULT_TEMPLATE.format(
                        tool_name=tool_call["name"],
                        arguments=json.dumps(tool_call.get("parameters") or {}, ensure_ascii=False),
                        result=result,
                    )
                )

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

    def _post_agent_payload(self, payload: dict) -> dict:
        provider = self._get_provider()
        return self._chat_backend_request(
            method="POST",
            path=provider.chat_completions_path(),
            body={"payload": payload},
            internal=True,
            timeout=self.valves.DIFY_REQUEST_TIMEOUT,
        )

    def _stream_agent_final_answer_chunks(self, payload: dict, fallback_content: str) -> Iterator[str]:
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        streamed = False
        stream_state = {"pending": "", "in_think": False, "blocked": False}
        emitted_parts: List[str] = []
        fallback_text = self._clean_agent_content(fallback_content or "已完成，但未返回可展示的结果。")

        try:
            with self._chat_backend_stream_request(
                path=self._get_provider().chat_completions_stream_path(),
                body={"payload": stream_payload},
            ) as response:
                response.raise_for_status()
                for event in self._iter_sse_events(response):
                    chunk = self._extract_openai_stream_delta_text(event)
                    if not chunk:
                        continue
                    cleaned_chunk = self._consume_agent_stream_text(stream_state, chunk)
                    if not cleaned_chunk:
                        continue
                    streamed = True
                    emitted_parts.append(cleaned_chunk)
                    yield cleaned_chunk
        except RuntimeError as exc:
            print("xiamimate.agent final stream failed", str(exc))

        flushed_chunk = self._flush_agent_stream_text(stream_state)
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

    def _consume_agent_stream_text(self, state: dict, chunk: str) -> str:
        if state.get("blocked"):
            return ""

        pending = "%s%s" % (state.get("pending") or "", chunk or "")
        if self._agent_stream_contains_internal_markup(pending):
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

    def _flush_agent_stream_text(self, state: dict) -> str:
        if state.get("blocked"):
            state["pending"] = ""
            return ""
        if state.get("in_think"):
            state["pending"] = ""
            return ""
        pending = str(state.get("pending") or "")
        state["pending"] = ""
        return self._clean_agent_content(pending)

    def _extract_assistant_content(self, response: dict) -> str:
        choices = response.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    def _extract_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""

        calls.extend(self._extract_markdown_tool_calls(text))
        calls.extend(self._extract_wrapped_json_tool_calls(text))
        calls.extend(self._extract_pipe_json_tool_calls(text))
        calls.extend(self._extract_hash_arrow_tool_calls(text))
        calls.extend(self._extract_colon_args_tool_calls(text))
        calls.extend(self._extract_function_style_tool_calls(text))

        # ── Provider-specific tool call formats ──
        provider = self._get_provider()
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
        tool_name_pattern = "|".join(re.escape(name) for name in sorted(ALLOWED_AGENT_TOOLS))
        pattern = re.compile(r"\b(%s)\s*\((.*?)\)" % tool_name_pattern, flags=re.DOTALL)

        for tool_name, args_text in pattern.findall(text):
            parsed = self._parse_function_style_tool_call(tool_name, args_text)
            if parsed:
                calls.append(parsed)

        return calls

    def _extract_minimax_invoke_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""

        pattern = re.compile(
            r'<invoke name="([^"]+)"\s*>?\s*(.*?)\s*</invoke>',
            flags=re.DOTALL,
        )

        for tool_name, block in pattern.findall(text):
            params = {}
            for key, value in re.findall(r'<?parameter name="([^"]+)">(.*?)</parameter>', block, flags=re.DOTALL):
                params[key] = value.strip()
            parsed = self._normalize_tool_call(name=tool_name, parameters=params)
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
        tool_name = (name or "").strip()
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

    def _execute_tool_call(self, tool_call: Dict[str, Any], billing_context: dict) -> str:
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
        if len(result_text) > 12000:
            return "%s\n\n[结果已截断，原始长度 %d 字符]" % (result_text[:12000], len(result_text))
        return result_text

    def _agent_stream_contains_internal_markup(self, text: str) -> bool:
        return self._get_provider().has_internal_markup(text)

    def _strip_agent_internal_markup(self, content: str) -> str:
        return self._get_provider().strip_internal_markup(content)

    def _clean_agent_content(self, content: str) -> str:
        return self._strip_agent_internal_markup(content)

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
