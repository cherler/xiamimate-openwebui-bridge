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
from inspect import signature
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

import requests
from pydantic import BaseModel


AGENT_SYSTEM_PROMPT = """你是 XiaMimate 商品主题分析 Agent。

工作原则：
1. 需要数据时优先调用已挂载的工具，不要凭空编造指标。
2. 需要平台规则、运营方法、合规要求等知识时，先调用 search_knowledge_base 工具检索知识库，不要依赖自身训练数据。
3. 需要商品数据时，使用工具链：resolve_candidates -> candidate_pool_stats / candidate_pool_trends / candidate_pool_weak_forecast / top_asin_drilldown / category_benchmark。
4. 如果工具尚未返回数据，只能给出分析框架、验证路径和风险提醒，明确标注为待验证。
5. 输出尽量围绕结论、证据、风险、下一步动作。
6. 每个结论标注数据来源类型：知识库 / 推理 / 工具数据。

工具调用规则：
- 当你决定调用工具时，直接输出工具调用指令，不要在工具调用之前添加任何文字（如"好的，我来帮你…"等）。
- 等工具返回结果后，再给出分析和回答。
- 如果需要同时调用多个工具，可以连续输出多个工具调用。

可用工具概览：
- search_knowledge_base: 检索跨境电商知识库（平台规则、运营指南、市场洞察）
- resolve_candidates: 解析候选 ASIN 池
- candidate_pool_stats: 候选池描述统计
- candidate_pool_trends: 候选池趋势诊断
- candidate_pool_weak_forecast: 弱信号预测标记
- top_asin_drilldown: 头部 ASIN 下钻
- category_benchmark: 类目基准对比
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
    "resolve_candidates",
    "candidate_pool_stats",
    "candidate_pool_trends",
    "candidate_pool_weak_forecast",
    "top_asin_drilldown",
    "category_benchmark",
}

COMMAND_TO_MODE = {
    "/agent": "agent",
    "/wf": "workflow",
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
    "minimax_request": 1,
    "dify_workflow_run": 8,
    "dify_knowledge_retrieve": 1,
    "theme_api_call": 2,
    "tavily_search": 2,
}

TOOL_BILLING_EVENT = {
    "search_knowledge_base": "dify_knowledge_retrieve",
    "resolve_candidates": "theme_api_call",
    "candidate_pool_stats": "theme_api_call",
    "candidate_pool_trends": "theme_api_call",
    "candidate_pool_weak_forecast": "theme_api_call",
    "top_asin_drilldown": "theme_api_call",
    "category_benchmark": "theme_api_call",
}

WORKFLOW_SUGGESTION_PROMPTS = [
    {
        "title": ["/workflow 示例", "宠物自动喂食器在 TikTok 美国市场的前景"],
        "content": "/workflow 帮我调研一下宠物自动喂食器在 TikTok 美国市场的前景",
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

        if mode == "workflow":
            return self._run_workflow(query=normalized_user_message, body=body, model=response_model)
        if mode == "agent":
            return self._run_agent(messages=normalized_messages, body=body, model=response_model)

        return self._chat_response(content="未识别的 XiaMimate 模式。请使用 Agent。", model=response_model)

    def _run_workflow(self, query: str, body: dict, model: str) -> Union[dict, Iterator[bytes]]:
        query = (query or "").strip()
        if not query:
            guidance = (
                "请在 /workflow 后直接写出调研需求，例如：\n"
                "/workflow 帮我调研一下宠物自动喂食器在 TikTok 美国市场的前景"
            )
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
            workflow_charge = self._charge_billing_event(
                billing_context=billing_context,
                event_type="dify_workflow_run",
                description="Dify workflow run",
                meta={
                    "mode": "workflow",
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
            return self._run_workflow_stream(
                query=query,
                body=body,
                model=model,
                billing_context=billing_context,
                workflow_charge=workflow_charge,
            )

        try:
            response = self._chat_backend_request(
                method="POST",
                path="/internal/provider/dify-workflow/run",
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
                charge=workflow_charge,
                description="Dify workflow request failed",
                meta={"mode": "workflow", "error": str(exc)[:500]},
            )
            return self._chat_response(content=self._error_text(str(exc)), model=model)

        answer = self._extract_workflow_answer(response)
        if answer:
            return self._chat_response(content=answer, model=model)

        return self._chat_response(content=json.dumps(response, ensure_ascii=False, indent=2), model=model)

    def _run_agent(self, messages: List[dict], body: dict, model: str) -> Union[dict, Iterator[bytes], str]:
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
            )

        try:
            answer = self._run_agent_loop(messages=messages, body=body, billing_context=billing_context)
        except RuntimeError as exc:
            return self._error_text(str(exc))

        return self._chat_response(content=answer, model=model)

    def _run_agent_stream(
        self,
        messages: List[dict],
        body: dict,
        model: str,
        billing_context: dict,
    ) -> Iterator[bytes]:
        response_id = "%s-%s" % (model, uuid.uuid4())
        created = int(time.time())
        conversation = deepcopy(messages or [])
        answer_started = False

        def emit_text_chunk(content: str) -> bytes:
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

        try:
            yield emit_text_chunk(self._format_agent_progress("正在分析问题"))

            for round_index in range(6):
                payload = self._prepare_agent_payload(messages=conversation, body=body)
                payload["stream"] = False
                minimax_charge = self._charge_billing_event(
                    billing_context=billing_context,
                    event_type="minimax_request",
                    description="MiniMax agent request",
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
                        description="MiniMax agent request failed",
                        meta={"mode": "agent", "stream": True, "error": str(exc)[:500]},
                    )
                    raise

                content = self._extract_assistant_content(response)
                tool_calls = self._extract_tool_calls(content)

                if not tool_calls:
                    final_answer = self._clean_agent_content(content) or str(content or "").strip()
                    status_line = "正在生成最终答复" if round_index == 0 else "工具执行完成，正在生成最终答复"
                    yield emit_text_chunk(self._format_agent_progress(status_line))
                    yield emit_text_chunk("\n---\n\n")

                    for chunk in self._stream_agent_final_answer_chunks(payload=payload, fallback_content=final_answer):
                        answer_started = True
                        yield emit_text_chunk(chunk)
                    break

                conversation.append({"role": "assistant", "content": content})

                tool_names = ", ".join(tool_call["name"] for tool_call in tool_calls)
                yield emit_text_chunk(self._format_agent_progress("正在调用工具: %s" % tool_names))

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
                    yield emit_text_chunk(
                        self._format_agent_progress("工具 %s 已%s" % (tool_call["name"], tool_status))
                    )

                conversation.append({"role": "user", "content": "\n\n".join(tool_results)})

            if not answer_started:
                raise RuntimeError("Agent 工具调用轮次超过上限，已中止。")
        except RuntimeError as exc:
            yield emit_text_chunk("\n" + self._error_text(str(exc)))

        yield self._sse_chunk(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
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

        mode = requested_mode or command_mode or (model_mode if model_mode in {"agent", "workflow"} else "agent")
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
            content = self._format_account_overview(command=command, overview=overview)
        except RuntimeError as exc:
            content = self._error_text(str(exc))

        if body.get("stream"):
            return self._stream_text_response(content=content, model=model)
        return self._chat_response(content=content, model=model)

    def _format_account_overview(self, command: str, overview: dict) -> str:
        user = overview.get("user") or {}
        points_account = overview.get("points_account") or {}
        usage_summary = overview.get("usage_summary") or {}
        usage_by_type = overview.get("usage_by_type_30d") or []
        recent_ledger = overview.get("recent_ledger") or []
        subscriptions = overview.get("subscriptions") or []
        daily_quota = overview.get("daily_quota_state") or {}
        point_cost_by_event = overview.get("point_cost_by_event") or {}

        display_name = str(user.get("display_name") or user.get("user_id") or "当前用户")
        user_id = str(user.get("user_id") or "")
        balance_points = int(points_account.get("balance_points") or 0)
        plan_tier = str(overview.get("plan_tier") or user.get("plan_tier") or "unknown")

        lines = [f"用户: {display_name}"]
        if user_id:
            lines.append(f"User ID: {user_id}")

        if command in {"overview", "points"}:
            lines.extend(
                [
                    f"当前积分余额: {balance_points}",
                    f"累计赠送积分: {int(points_account.get('lifetime_granted_points') or 0)}",
                    f"累计购买积分: {int(points_account.get('lifetime_purchased_points') or 0)}",
                    f"累计消费积分: {int(points_account.get('lifetime_spent_points') or 0)}",
                ]
            )
            if daily_quota:
                quota_points = int(daily_quota.get("quota_points") or 0)
                consumed_points = int(daily_quota.get("consumed_points") or 0)
                lines.append(
                    f"Guest 当日配额: {max(0, quota_points - consumed_points)}/{quota_points} (日期 {daily_quota.get('quota_date')})"
                )
            if recent_ledger:
                lines.append("")
                lines.append("最近账本:")
                for row in recent_ledger[:6]:
                    delta = int(row.get("points_delta") or 0)
                    sign = "+" if delta >= 0 else ""
                    lines.append(
                        f"- {row.get('created_at')} | {row.get('entry_type')} | {sign}{delta} | 余额 {row.get('balance_after_points')} | {row.get('description') or row.get('event_type') or ''}"
                    )

        if command in {"overview", "usage"}:
            lines.append("")
            lines.append("使用汇总:")
            lines.append(f"- 1 天内 units: {usage_summary.get('units_1d', 0)}")
            lines.append(f"- 7 天内 units: {usage_summary.get('units_7d', 0)}")
            lines.append(f"- 30 天内 units: {usage_summary.get('units_30d', 0)}")
            lines.append(f"- 30 天内事件数: {usage_summary.get('event_count_30d', 0)}")
            if usage_by_type:
                lines.append("- 30 天内按事件类型:")
                for row in usage_by_type[:8]:
                    lines.append(f"  {row.get('event_type')}: {row.get('total_units')}")

        if command in {"overview", "plan"}:
            lines.append("")
            lines.append(f"当前套餐: {plan_tier}")
            entitlements = overview.get("entitlements") or {}
            if entitlements:
                lines.append(f"套餐权益: {json.dumps(entitlements, ensure_ascii=False)}")
            if subscriptions:
                latest = subscriptions[0]
                lines.append(
                    f"订阅状态: {latest.get('status')} | package={latest.get('package_code')} | monthly_points={latest.get('monthly_points')}"
                )
            if point_cost_by_event:
                lines.append("当前计价:")
                for event_type, points in point_cost_by_event.items():
                    lines.append(f"- {event_type}: {points} 积分/单位")

        lines.append("")
        lines.append("可用命令: /me  /points  /usage  /plan")
        return "\n".join(lines)

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

    def _run_workflow_stream(
        self,
        query: str,
        body: dict,
        model: str,
        billing_context: dict,
        workflow_charge: dict,
    ) -> Iterator[bytes]:
        response_id = "%s-%s" % (model, uuid.uuid4())
        created = int(time.time())
        finished_nodes = set()
        answer_started = False
        answer_chunks: List[str] = []
        emitted_progress = set()

        def emit_text_chunk(content: str) -> bytes:
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

        try:
            with self._chat_backend_stream_request(
                path="/internal/provider/dify-workflow/run-stream",
                body={
                    "query": query,
                    "user": billing_context["user_id"],
                },
            ) as response:
                response.raise_for_status()

                start_line = self._format_workflow_progress(5, "工作流已启动，正在解析需求")
                emitted_progress.add(start_line)
                yield emit_text_chunk(start_line)

                for event in self._iter_sse_events(response):
                    event_type = str(event.get("event") or "").strip()
                    data = event.get("data") if isinstance(event.get("data"), dict) else {}

                    if event_type == "workflow_started":
                        stage_line = self._format_workflow_progress(10, "已连接 Dify Chatflow，开始执行节点")
                        if stage_line not in emitted_progress:
                            emitted_progress.add(stage_line)
                            yield emit_text_chunk(stage_line)
                        continue

                    if event_type == "node_finished":
                        node_id = str(data.get("node_id") or "").strip()
                        if not node_id or node_id in finished_nodes:
                            continue
                        finished_nodes.add(node_id)
                        stage_label = WORKFLOW_NODE_LABELS.get(node_id) or str(data.get("title") or node_id)
                        percent = self._workflow_progress_percent(len(finished_nodes))
                        stage_line = self._format_workflow_progress(percent, "%s 完成" % stage_label)
                        if stage_line not in emitted_progress:
                            emitted_progress.add(stage_line)
                            yield emit_text_chunk(stage_line)
                        continue

                    if event_type in {"message", "agent_message", "text_chunk"}:
                        chunk = self._extract_workflow_answer(event)
                        if not chunk:
                            continue
                        if not answer_started:
                            answer_started = True
                            yield emit_text_chunk(self._format_workflow_progress(95, "工作流执行完成，正在整理最终报告"))
                            yield emit_text_chunk("\n---\n\n")
                        answer_chunks.append(chunk)
                        yield emit_text_chunk(chunk)
                        continue

                    if event_type == "workflow_finished":
                        final_answer = self._extract_workflow_answer(event)
                        if final_answer and not answer_started:
                            answer_started = True
                            yield emit_text_chunk(self._format_workflow_progress(95, "工作流执行完成，正在整理最终报告"))
                            yield emit_text_chunk("\n---\n\n")
                            for chunk in self._split_text(final_answer):
                                answer_chunks.append(chunk)
                                yield emit_text_chunk(chunk)
                        continue

                    if event_type == "error":
                        error_text = self._extract_workflow_error(event)
                        raise RuntimeError(error_text or "Dify Chatflow 返回错误事件。")

                if not answer_started:
                    final_answer = "".join(answer_chunks).strip() or "工作流已完成，但未返回可展示的结果。"
                    yield emit_text_chunk(self._format_workflow_progress(95, "工作流执行完成，正在整理最终报告"))
                    yield emit_text_chunk("\n---\n\n")
                    for chunk in self._split_text(final_answer):
                        yield emit_text_chunk(chunk)

        except requests.RequestException as exc:
            if not answer_started:
                self._refund_billing_event(
                    billing_context=billing_context,
                    charge=workflow_charge,
                    description="Dify workflow stream request failed",
                    meta={"mode": "workflow_stream", "error": str(exc)[:500]},
                )
            detail = self._error_text(str(exc))
            yield emit_text_chunk("\n" + detail)
        except RuntimeError as exc:
            if not answer_started:
                self._refund_billing_event(
                    billing_context=billing_context,
                    charge=workflow_charge,
                    description="Dify workflow stream runtime failed",
                    meta={"mode": "workflow_stream", "error": str(exc)[:500]},
                )
            yield emit_text_chunk("\n" + self._error_text(str(exc)))

        yield self._sse_chunk(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
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
        percent = 8 + int((finished_count * 84) / max(1, WORKFLOW_ESTIMATED_STEPS))
        return max(8, min(92, percent))

    def _format_workflow_progress(self, percent: int, description: str) -> str:
        total_slots = 10
        filled = max(0, min(total_slots, round((percent / 100) * total_slots)))
        bar = "#" * filled + "." * (total_slots - filled)
        return "⏳ /workflow 进度 [%s] %d%% · %s\n" % (bar, percent, description)

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

    def _prepare_agent_payload(self, messages: List[dict], body: dict) -> dict:
        # Only forward parameters that MiniMax API actually supports.
        # Exclude tools/tool_choice (MiniMax uses text-based tool calling),
        # stream_options, metadata, reasoning_effort, etc. which cause errors.
        allowed_params = {
            "messages",
            "temperature",
            "top_p",
            "n",
            "stream",
            "stop",
            "max_tokens",
            "max_completion_tokens",
            "presence_penalty",
            "frequency_penalty",
            "logit_bias",
            "user",
            "response_format",
            "seed",
        }
        payload = {key: value for key, value in body.items() if key in allowed_params}
        payload["model"] = self.valves.AGENT_OPENAI_MODEL
        payload["messages"] = self._inject_agent_system_prompt(messages)

        user_value = payload.get("user")
        if isinstance(user_value, dict):
            payload["user"] = self._user_id(body)

        return payload

    def _run_agent_loop(self, messages: List[dict], body: dict, billing_context: dict) -> str:
        conversation = deepcopy(messages or [])

        for _ in range(6):
            payload = self._prepare_agent_payload(messages=conversation, body=body)
            payload["stream"] = False
            minimax_charge = self._charge_billing_event(
                billing_context=billing_context,
                event_type="minimax_request",
                description="MiniMax agent request",
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
                    description="MiniMax agent request failed",
                    meta={"mode": "agent", "error": str(exc)[:500]},
                )
                raise
            content = self._extract_assistant_content(response)
            tool_calls = self._extract_tool_calls(content)

            if not tool_calls:
                cleaned = self._clean_agent_content(content)
                return cleaned or str(content or "").strip()

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
            "CHAT_BACKEND_BASE_URL 未配置。",
            "CHAT_BACKEND_SERVICE_SECRET 未配置。",
        )
        return any(normalized.startswith(prefix) for prefix in error_prefixes)

    def _post_agent_payload(self, payload: dict) -> dict:
        return self._chat_backend_request(
            method="POST",
            path="/internal/provider/minimax/chat-completions",
            body={"payload": payload},
            internal=True,
            timeout=self.valves.DIFY_REQUEST_TIMEOUT,
        )

    def _stream_agent_final_answer_chunks(self, payload: dict, fallback_content: str) -> Iterator[str]:
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        streamed = False

        try:
            with self._chat_backend_stream_request(
                path="/internal/provider/minimax/chat-completions/stream",
                body={"payload": stream_payload},
            ) as response:
                response.raise_for_status()
                for event in self._iter_sse_events(response):
                    chunk = self._extract_openai_stream_delta_text(event)
                    if not chunk:
                        continue
                    streamed = True
                    yield chunk
        except RuntimeError as exc:
            print("xiamimate.agent final stream failed", str(exc))

        if streamed:
            return

        final_text = fallback_content or "已完成，但未返回可展示的结果。"
        for chunk in self._split_text(final_text):
            yield chunk

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

        for match in re.findall(r"<minimax:tool_call>\s*(.*?)\s*</minimax:tool_call>", text, flags=re.DOTALL):
            calls.extend(self._extract_minimax_invoke_tool_calls(match))

        for match in re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", text, flags=re.DOTALL):
            parsed = self._parse_tool_call_block(match)
            if parsed:
                calls.append(parsed)

        for match in re.findall(r"\[TOOL_CALL\]\s*(.*?)\s*\[/TOOL_CALL\]", text, flags=re.DOTALL):
            parsed = self._parse_bracket_tool_call(match)
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
            parsed = self._normalize_tool_call(name=tool_name, parameters=self._parse_dash_parameters(args_block))
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
            parsed = self._normalize_tool_call(name=tool_name, parameters=self._parse_dash_parameters(args_block))
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
        return self._parse_json_tool_call(raw_text) or self._parse_inline_tool_call(raw_text)

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

        args = self._parse_dash_parameters(raw_text)

        return self._normalize_tool_call(name=tool_match.group(1), parameters=args)

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
                "market": "marketplace",
            },
            "candidate_pool_trends": {
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
                "market": "marketplace",
            },
            "candidate_pool_weak_forecast": {
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
                "market": "marketplace",
            },
            "top_asin_drilldown": {
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
                "market": "marketplace",
            },
            "category_benchmark": {
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
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
                    description="Tool call: %s" % tool_name,
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
                    description="Tool call failed: %s" % tool_name,
                    meta={"tool_name": tool_name, "error": str(exc)[:500]},
                )
            return "工具 %s 执行失败: %s" % (tool_name, str(exc))

        result_text = str(result or "")
        if tool_charge is not None and self._tool_result_has_error(result_text):
            self._refund_billing_event(
                billing_context=billing_context,
                charge=tool_charge,
                description="Tool call returned error: %s" % tool_name,
                meta={"tool_name": tool_name, "result_preview": result_text[:500]},
            )
        if len(result_text) > 12000:
            return "%s\n\n[结果已截断，原始长度 %d 字符]" % (result_text[:12000], len(result_text))
        return result_text

    def _clean_agent_content(self, content: str) -> str:
        text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL)
        text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL)
        text = re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", "", text, flags=re.DOTALL)
        text = re.sub(r"<minimax:tool_call>.*?</minimax:tool_call>", "", text, flags=re.DOTALL)
        text = re.sub(r"\$PARAMS\s*=\s*\{.*?\}\s*[A-Za-z_][A-Za-z0-9_]*\(\$PARAMS\)", "", text, flags=re.DOTALL)
        text = re.sub(r'<invoke name="[^"]+">.*?</invoke>', "", text, flags=re.DOTALL)
        return text.strip()

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
            yield self._sse_chunk(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": chunk},
                            "finish_reason": None,
                        }
                    ],
                }
            )

        if not emitted:
            yield self._sse_chunk(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None,
                        }
                    ],
                }
            )

        yield self._sse_chunk(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        yield b"data: [DONE]\n\n"

    def _split_text(self, content: str, chunk_size: int = 800) -> List[str]:
        text = content or ""
        if not text:
            return []
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

    def _sse_chunk(self, payload: dict) -> bytes:
        return ("data: %s\n\n" % json.dumps(payload, ensure_ascii=False)).encode("utf-8")

    def _inject_agent_system_prompt(self, messages: List[dict]) -> List[dict]:
        clean_messages = deepcopy(messages or [])
        if clean_messages:
            first_message = clean_messages[0]
            if first_message.get("role") == "system" and first_message.get("content") == AGENT_SYSTEM_PROMPT:
                return clean_messages
        clean_messages.insert(0, {"role": "system", "content": AGENT_SYSTEM_PROMPT})
        return clean_messages

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
