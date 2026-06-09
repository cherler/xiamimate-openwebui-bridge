from __future__ import annotations

import hashlib
import contextlib
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


def tool_call_fingerprint(parameters: Any) -> str:
    """生成参数的稳定指纹，用于跨轮判断"同一个工具是否已用相同入参执行过"。"""
    if parameters is None:
        return "none"
    if isinstance(parameters, str):
        canonical = parameters
    else:
        try:
            canonical = json.dumps(parameters, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            canonical = str(parameters)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def _summarize_parameters(parameters: Any, limit: int = 240) -> str:
    if parameters is None:
        return ""
    try:
        text = json.dumps(parameters, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(parameters)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


# 状态轮询型工具：返回的是后台任务/数据就绪的实时状态（如扩池任务从 queued→completed）。
# 这类工具的入参在多轮里几乎不变，但每次必须真实重查，绝不能被跨轮 (tool, params) 签名去重拦截，
# 否则会用首轮的 queued 旧 summary 反复复述，造成"任务一直排队"的假象。
# 仅在同一请求内按相同入参去重，跨轮一律放行重新执行。
STATUS_POLL_REFRESH_TOOLS: set[str] = {
    "candidate_expansion_status",
}

# 跨轮去重的"新鲜窗口"：同一 (tool, params) 只有在该窗口内才视为"刚跑过、可安全复用"。
# 一旦超过窗口，说明上次结果可能已过期（价格/榜单/机会/库存等会随时间变化），就放行重跑取最新数据，
# 避免把首轮旧摘要在长对话里反复当成最新结果复述。状态轮询型工具不受此约束（永远重查）。
# session 缓存 TTL 为 2 小时，这里取更短的新鲜窗口让长对话能自然刷新。
CROSS_TURN_DEDUP_FRESHNESS_SECONDS: float = 30 * 60

# 失败结果的文本特征：这些短语出现在结果开头通常代表工具未成功执行。
_TOOL_RESULT_FAILURE_PHRASES: Tuple[str, ...] = (
    "执行失败",
    "请求失败",
    "检索失败",
    "搜索失败",
    "调用失败",
    "未配置",
)


def tool_result_is_unusable(result: Any, compact_result: str = "") -> bool:
    """判断一次工具结果是否属于失败/错误，不应进入跨轮去重缓存。

    跨轮去重一旦记录某个 (tool, params) 签名，后续轮相同入参会被直接拦截并复述其 summary。
    若把失败/错误结果写进缓存，用户在后面几轮就会反复拿到"工具其实从未成功过"的错误旧结果。
    因此这里在记录边界做一道稳健判断：除了前缀型中文报错，还要识别结构化 JSON 错误标记。
    """
    head = str(compact_result or result or "").strip()[:200]
    if any(phrase in head for phrase in _TOOL_RESULT_FAILURE_PHRASES):
        return True
    payload = _coerce_payload(result)
    if isinstance(payload, dict):
        status = str(payload.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure"}:
            return True
        if payload.get("ok") is False or payload.get("success") is False:
            return True
        if payload.get("error") or payload.get("errors"):
            return True
    return False


def _coerce_bool_flag(value: Any) -> bool:
    """把 planner 可能给出的多种"真值"形式（true / "true" / 1 / "yes" 等）归一为布尔。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "on", "refresh"}
    return False


class SessionContextStore:
    """跨轮会话级结构化上下文。

    设计目标：解决 ReAct harness 在多轮对话中无法复用前轮工具结构化结果的问题。
    ObservationStore 每次 HTTP 请求重建，但同一 `chat_id` 的多轮对话需要复用：
    - 上一轮 resolve_candidates 生成的候选池 (pool_id / asins / size / leaf_categories)
    - 最近一次明确的 product_query / marketplace
    - 最近一次 category_resolve 出的 category_id / category_path
    - 各工具最近一次的 compact result（仅作为 planner 提示，不进入参数）

    进程内 TTL + LRU；bridge 重启后丢失（可接受，后续可外置 Redis）。
    """

    def __init__(
        self,
        ttl_seconds: int = 2 * 60 * 60,
        max_sessions: int = 512,
        max_tool_result_chars: int = 1200,
        max_tool_results: int = 8,
    ):
        self.ttl_seconds = max(60, int(ttl_seconds or 7200))
        self.max_sessions = max(1, int(max_sessions or 512))
        self.max_tool_result_chars = max(200, int(max_tool_result_chars or 1200))
        self.max_tool_results = max(1, int(max_tool_results or 8))
        self._sessions: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._lock = threading.RLock()

    def _evict_expired(self, now: float) -> None:
        expired = [key for key, entry in self._sessions.items() if now - entry.get("_touched_at", 0) > self.ttl_seconds]
        for key in expired:
            self._sessions.pop(key, None)

    def _evict_lru(self) -> None:
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)

    def get(self, chat_id: Optional[str]) -> Dict[str, Any]:
        key = str(chat_id or "").strip()
        if not key:
            return {}
        now = time.time()
        with self._lock:
            self._evict_expired(now)
            entry = self._sessions.get(key)
            if entry is None:
                return {}
            self._sessions.move_to_end(key)
            entry["_touched_at"] = now
            return {k: v for k, v in entry.items() if not k.startswith("_")}

    def update(self, chat_id: Optional[str], updates: Dict[str, Any]) -> None:
        key = str(chat_id or "").strip()
        if not key or not isinstance(updates, dict) or not updates:
            return
        now = time.time()
        with self._lock:
            self._evict_expired(now)
            entry = self._sessions.get(key)
            if entry is None:
                entry = {"_created_at": now}
                self._sessions[key] = entry
            else:
                self._sessions.move_to_end(key)
            for field_name, field_value in updates.items():
                if field_value is None or field_name.startswith("_"):
                    continue
                entry[field_name] = field_value
            entry["_touched_at"] = now
            self._evict_lru()

    def record_tool_result(
        self,
        chat_id: Optional[str],
        tool_name: str,
        compact_result: str,
        parameters: Any = None,
    ) -> None:
        key = str(chat_id or "").strip()
        normalized_tool = str(tool_name or "").strip()
        if not key or not normalized_tool:
            return
        truncated = str(compact_result or "")
        if len(truncated) > self.max_tool_result_chars:
            truncated = truncated[: self.max_tool_result_chars] + "...(truncated)"
        fingerprint = tool_call_fingerprint(parameters)
        params_preview = _summarize_parameters(parameters)
        composite_key = f"{normalized_tool}::{fingerprint}"
        now = time.time()
        with self._lock:
            self._evict_expired(now)
            entry = self._sessions.get(key)
            if entry is None:
                entry = {"_created_at": now}
                self._sessions[key] = entry
            else:
                self._sessions.move_to_end(key)
            tool_calls = entry.get("last_tool_calls")
            if not isinstance(tool_calls, OrderedDict):
                tool_calls = OrderedDict()
                entry["last_tool_calls"] = tool_calls
            if composite_key in tool_calls:
                tool_calls.move_to_end(composite_key)
            tool_calls[composite_key] = {
                "tool_name": normalized_tool,
                "params_fingerprint": fingerprint,
                "params_preview": params_preview,
                "summary": truncated,
                "recorded_at": now,
            }
            while len(tool_calls) > self.max_tool_results:
                tool_calls.popitem(last=False)
            entry["_touched_at"] = now
            self._evict_lru()

    def clear(self, chat_id: Optional[str]) -> None:
        key = str(chat_id or "").strip()
        if not key:
            return
        with self._lock:
            self._sessions.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()


SESSION_CONTEXT = SessionContextStore()



AGENT_PLANNER_SYSTEM_PROMPT = """你是 XiaMimate 的 ReAct Planner，负责在每一轮只决定下一步动作。

你的职责：
1. 先判断问题场景，再决定本轮是调用一个工具，还是给出最终答案。
2. 能直接回答时直接回答，不要为了显得完整而调用工具。
3. 需要工具时，只输出一个下一步 Action；工具 Observation 会进入下一轮，由你再决定后续动作。
4. 基础知识、入门指导、提示词建议、产品使用说明这类问题，禁止使用选品分析工具；优先直接回答，必要时才使用 customer_help_search、search_knowledge_base、web_search。
5. web_search 是增强可信度和时效性的补强工具，不是默认前置工具。只有当最新外部信息会实质影响结论、或用户明确要求联网/最新/外部依据时才使用。
6. 选品机会发现、商品主题分析、ASIN 分析、预算测算分别走各自工具层，不要跨层乱调。

你必须只输出一个 JSON 对象，不要输出 markdown，不要输出解释文字。

JSON 结构：
{
  "scene": "foundation_qa|blank_opportunity_discovery|theme_analysis|asin_specific_analysis|budget_analysis|general_agent",
  "reasoning_summary": "一句话说明为什么选择这个下一步动作",
  "action": {
    "type": "tool|final",
    "tool": {
      "tool_name": "工具名",
      "goal": "本步骤目标",
      "parameters": {"参数名": "参数值"}
    },
    "final_answer": "当 type=final 时给用户的最终答案；否则为空字符串"
  },
  "stop_reason": "本轮执行后何时可以停止继续调用工具"
}

约束：
- 每轮只能输出一个 action，不能输出多个工具步骤。
- action.type=tool 时，必须填写 action.tool.tool_name 和 action.tool.parameters，action.final_answer 必须为空。
- action.type=final 时，必须填写 action.final_answer，不得填写工具。
- 只可从给定 allowed_tools 中选择工具。
- 不要编造工具参数；拿不准时先选择前置工具，或直接回答并说明边界。
- 不要一次性规划完整路线；每次只选择基于当前 Observation 最必要的下一步。
- 当 session_memory.last_candidate_pool.pool_id 存在时，所有需要 candidate_pool_id 的工具必须原样复用该 pool_id；禁止凭空构造形如 "P-XXXX" 的 pool_id。
- 当 session_memory.last_candidate_pool 已经存在且本轮用户问题仍指向同一个商品主题/候选池时，禁止再次调用 resolve_candidates；改为直接使用已有 pool_id 进入下一步工具或直接作答。
- 当历史轮次已经在 executed_tool_signatures 中记录过相同 (tool, parameters) 调用，禁止再次调用该工具；改为基于 session_memory 直接给出 final 答案。
"""

TOOL_REQUIRED_ARGUMENTS = {
    "resolve_candidates": ["product_query"],
    "category_resolve": ["category_query"],
    "candidate_pool_stats": ["candidate_pool_id", "candidate_asins", "product_query"],
    "candidate_pool_slice": ["candidate_pool_id", "candidate_asins", "product_query"],
    "candidate_pool_trends": ["candidate_pool_id", "candidate_asins", "product_query"],
    "category_benchmark": ["candidate_pool_id", "candidate_asins", "product_query", "benchmark_category_id", "benchmark_category_path"],
    "top_asin_drilldown": ["asin", "asins", "candidate_pool_id", "product_query"],
    "asin_history_timeseries": ["asins"],
    "expand_candidates": ["product_query", "category_id", "category_path"],
    "candidate_expansion_status": ["job_id"],
    "launch_budget_calculator": ["product_theme"],
}

INTEGER_EXPLICIT_ARGUMENTS = {
    "max_candidates",
    "min_pool_size",
    "target_pool_size",
    "target_asin_count",
    "max_matches",
    "top_k",
    "limit",
    "window_days",
    "max_asins",
}

BOOLEAN_EXPLICIT_ARGUMENTS = {"include_descendants", "expand_if_small", "include_result"}

TOOL_NUMERIC_LIMITS = {
    "candidate_pool_weak_forecast": {"top_n": (1, 20)},
    "candidate_pool_slice": {"top_n": (1, 20)},
    "product_forecast_explain": {"top_n": (1, 20)},
    "top_asin_drilldown": {"top_n": (1, 20)},
}


TOOL_LAYER_REGISTRY = {
    "customer_help_search": {
        "layer": "foundation",
        "capability": "产品使用、入门指导、提示词示例、计费和客服 FAQ",
        "scene_tags": ["foundation_qa", "general_agent"],
    },
    "search_knowledge_base": {
        "layer": "foundation",
        "capability": "跨境平台规则、运营方法、合规要求、领域知识",
        "scene_tags": ["foundation_qa", "theme_analysis", "blank_opportunity_discovery", "general_agent"],
    },
    "web_search": {
        "layer": "foundation",
        "capability": "最新外部政策、新闻、市场动态、外部可信度补强",
        "scene_tags": ["foundation_qa", "theme_analysis", "blank_opportunity_discovery", "general_agent"],
    },
    "opportunity_discovery": {
        "layer": "discovery",
        "capability": "用户尚未给出具体商品时，发现机会卡片和后续分析入口",
        "scene_tags": ["blank_opportunity_discovery", "general_agent"],
    },
    "opportunity_discovery_job": {
        "layer": "discovery",
        "capability": "根据机会发现 job_id 回取历史结果或完整卡片",
        "scene_tags": ["blank_opportunity_discovery", "general_agent"],
    },
    "resolve_candidates": {
        "layer": "analysis",
        "capability": "将具体商品词或主题解析成候选 ASIN 池",
        "scene_tags": ["theme_analysis", "general_agent"],
    },
    "category_resolve": {
        "layer": "analysis",
        "capability": "解析稳定类目 ID/路径，为召回、benchmark、扩池做锚点",
        "scene_tags": ["theme_analysis", "general_agent"],
    },
    "candidate_pool_stats": {
        "layer": "analysis",
        "capability": "候选池基础统计盘面",
        "scene_tags": ["theme_analysis", "general_agent"],
    },
    "candidate_pool_slice": {
        "layer": "analysis",
        "capability": "按品牌、标题关键词或材质关键词切片候选池，返回切片内 top ASIN、评分/评论数量/销量分布；不能回答评论文本关键词或 Amazon 月搜索量",
        "scene_tags": ["theme_analysis", "general_agent"],
        "requires_provider": False,
        "provides": ["slice_top_asins", "rating_distribution", "review_count_distribution", "sales_window_sum", "top_brands"],
        "unsupported_claims": ["评论文本关键词", "低分原因", "Amazon 月搜索量"],
    },
    "candidate_pool_trends": {
        "layer": "analysis",
        "capability": "候选池趋势变化",
        "scene_tags": ["theme_analysis", "general_agent"],
    },
    "candidate_pool_weak_forecast": {
        "layer": "analysis",
        "capability": "候选池弱信号预测",
        "scene_tags": ["theme_analysis", "general_agent"],
    },
    "product_forecast_explain": {
        "layer": "analysis",
        "capability": "正式销量预测及其解释",
        "scene_tags": ["theme_analysis", "general_agent"],
    },
    "top_asin_drilldown": {
        "layer": "analysis",
        "capability": "头部 ASIN 下钻分析",
        "scene_tags": ["theme_analysis", "general_agent"],
    },
    "asin_history_timeseries": {
        "layer": "analysis",
        "capability": "指定 ASIN 的历史时序表现",
        "scene_tags": ["asin_specific_analysis", "general_agent"],
    },
    "category_benchmark": {
        "layer": "analysis",
        "capability": "候选池与类目基准对比",
        "scene_tags": ["theme_analysis", "general_agent"],
    },
    "keepa_asin_lookup": {
        "layer": "analysis",
        "capability": "本地没有历史数据时查 Keepa 实时快照",
        "scene_tags": ["asin_specific_analysis", "theme_analysis", "general_agent"],
    },
    "expand_candidates": {
        "layer": "expansion",
        "capability": "候选池不足时创建扩池任务",
        "scene_tags": ["theme_analysis", "general_agent"],
    },
    "candidate_expansion_status": {
        "layer": "expansion",
        "capability": "查询扩池任务和分析数据就绪状态",
        "scene_tags": ["theme_analysis", "general_agent"],
    },
    "launch_budget_calculator": {
        "layer": "business",
        "capability": "启动资金、单件利润、盈亏平衡测算",
        "scene_tags": ["budget_analysis", "general_agent"],
    },
}

SCENE_TOOL_POLICY = {
    "foundation_qa": {
        "label": "基础知识与新手指导",
        "allowed_layers": ["foundation"],
        "max_rounds": 2,
        "max_steps_per_round": 2,
    },
    "blank_opportunity_discovery": {
        "label": "空白机会发现",
        "allowed_layers": ["discovery", "foundation"],
        "max_rounds": 3,
        "max_steps_per_round": 2,
    },
    "theme_analysis": {
        "label": "商品主题分析",
        "allowed_layers": ["analysis", "expansion", "foundation", "business"],
        "max_rounds": 6,
        "max_steps_per_round": 2,
    },
    "asin_specific_analysis": {
        "label": "ASIN 定向分析",
        "allowed_layers": ["analysis", "foundation"],
        "max_rounds": 3,
        "max_steps_per_round": 2,
    },
    "budget_analysis": {
        "label": "预算与利润测算",
        "allowed_layers": ["business", "foundation"],
        "max_rounds": 2,
        "max_steps_per_round": 2,
    },
    "general_agent": {
        "label": "通用智能体",
        "allowed_layers": ["foundation", "discovery", "analysis", "expansion", "business"],
        "max_rounds": 3,
        "max_steps_per_round": 2,
    },
}

ALLOWED_AGENT_TOOLS = set(TOOL_LAYER_REGISTRY.keys())


def observed_tool_names(tool_observations: List[dict]) -> List[str]:
    names: List[str] = []
    for observation in tool_observations or []:
        tool_name = str((observation or {}).get("tool_name") or "").strip()
        if tool_name and tool_name not in names:
            names.append(tool_name)
    return names


def _digest_observation_result(text: str, head: int = 480, tail: int = 240) -> str:
    text = (text or "").strip()
    if len(text) <= head + tail + 32:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n\u2026[omitted {omitted} chars]\u2026\n\n{text[-tail:]}"


def planner_observation_context(
    tool_observations: List[dict],
    truncate_text: Callable[[str, int], str],
    limit: int = 6,
) -> List[dict]:
    window = (tool_observations or [])[-max(1, limit) :]
    items: List[dict] = []
    last_index = len(window) - 1
    for idx, observation in enumerate(window):
        result_text = str(observation.get("llm_result") or "")
        if idx == last_index:
            # Newest observation: keep full content (with a safety cap to avoid runaway payloads).
            result_payload = truncate_text(result_text, 12000)
            digest = False
        else:
            # Older observations: planner already saw them; compress to head+tail digest.
            result_payload = _digest_observation_result(result_text)
            digest = True
        item = {
            "tool_name": str(observation.get("tool_name") or "").strip(),
            "arguments": observation.get("arguments") or {},
            "result": result_payload,
        }
        if digest:
            item["result_digest"] = True
        items.append(item)
    return items


class ToolRegistry:
    def __init__(
        self,
        tool_layer_registry: Optional[Dict[str, Dict[str, Any]]] = None,
        scene_tool_policy: Optional[Dict[str, Dict[str, Any]]] = None,
        allowed_tools: Optional[set[str]] = None,
    ):
        self.tool_layer_registry = tool_layer_registry or TOOL_LAYER_REGISTRY
        self.scene_tool_policy = scene_tool_policy or SCENE_TOOL_POLICY
        self.allowed_tools = allowed_tools or ALLOWED_AGENT_TOOLS

    def scene_policy(self, scene: str, mode: str = "agent") -> dict:
        normalized_scene = str(scene or "general_agent").strip() or "general_agent"
        policy = deepcopy(self.scene_tool_policy.get(normalized_scene) or self.scene_tool_policy["general_agent"])
        if mode == "tool":
            policy["max_rounds"] = max(1, min(3, int(policy.get("max_rounds") or 2)))
        return policy

    def allowed_tool_names(self, scene: str, mode: str = "agent") -> List[str]:
        policy = self.scene_policy(scene, mode)
        allowed_layers = set(policy.get("allowed_layers") or [])
        names: List[str] = []
        for tool_name in sorted(self.allowed_tools):
            metadata = self.tool_layer_registry.get(tool_name) or {}
            layer = str(metadata.get("layer") or "").strip()
            if layer and layer not in allowed_layers:
                continue
            if mode == "tool" and tool_name == "web_search":
                continue
            names.append(tool_name)
        return names

    def tool_catalog(self, scene: str, mode: str = "agent", tool_label: Optional[Callable[[str], str]] = None) -> List[dict]:
        catalog: List[dict] = []
        for tool_name in self.allowed_tool_names(scene, mode):
            metadata = self.tool_layer_registry.get(tool_name) or {}
            fallback_label = tool_label(tool_name) if tool_label is not None else tool_name
            catalog.append(
                {
                    "tool_name": tool_name,
                    "layer": metadata.get("layer") or "general",
                    "capability": metadata.get("capability") or fallback_label,
                    "scene_tags": metadata.get("scene_tags") or [],
                }
            )
        return catalog

    def tool_allowed_for_scene(self, tool_call: Dict[str, Any], scene: str, mode: str = "agent") -> bool:
        tool_name = str((tool_call or {}).get("name") or "").strip()
        if tool_name not in self.allowed_tools:
            return False
        if mode == "tool" and tool_name == "web_search":
            return False
        metadata = self.tool_layer_registry.get(tool_name) or {}
        layer = str(metadata.get("layer") or "").strip()
        allowed_layers = set((self.scene_policy(scene, mode) or {}).get("allowed_layers") or [])
        if layer and allowed_layers and layer not in allowed_layers:
            return False
        return True

    def scene_for_explicit_tool(self, tool_name: str, current_scene: str) -> str:
        return scene_for_explicit_tool(tool_name, current_scene, self.tool_layer_registry, self.scene_tool_policy)

    def single_execution_tools(self, scene: str) -> set[str]:
        if scene == "theme_analysis":
            return {
                "resolve_candidates",
                "category_resolve",
                "candidate_pool_stats",
                "candidate_pool_slice",
                "candidate_pool_trends",
                "candidate_pool_weak_forecast",
                "top_asin_drilldown",
                "category_benchmark",
            }
        if scene == "blank_opportunity_discovery":
            return {"opportunity_discovery", "opportunity_discovery_job", "category_resolve"}
        if scene == "foundation_qa":
            return {"customer_help_search", "search_knowledge_base", "web_search"}
        if scene == "budget_analysis":
            return {"launch_budget_calculator"}
        return set()


class ObservationStore:
    def __init__(self, observations: Optional[List[dict]] = None):
        self.observations = observations if observations is not None else []
        self.tool_result_cache: Dict[Tuple[str, str], dict] = {}

    def append(self, observation: dict, cache_key: Optional[Tuple[str, str]] = None) -> None:
        self.observations.append(observation)
        if cache_key is not None:
            self.tool_result_cache[cache_key] = observation

    def names(self) -> List[str]:
        return observed_tool_names(self.observations)

    def context(self, truncate_text: Callable[[str, int], str], limit: int = 6) -> List[dict]:
        return planner_observation_context(self.observations, truncate_text, limit=limit)


class AgentTrace:
    def __init__(self, trace_id: str = "", mode: str = "agent", scene: str = "general_agent"):
        self.trace_id = trace_id or "agent-%s" % uuid.uuid4()
        self.mode = str(mode or "agent")
        self.scene = str(scene or "general_agent")
        self.started_at_ms = int(time.time() * 1000)
        self.events: List[dict] = []

    def record(self, event_type: str, **fields: Any) -> dict:
        event = {
            "trace_id": self.trace_id,
            "event_type": str(event_type or "").strip() or "event",
            "elapsed_ms": max(0, int(time.time() * 1000) - self.started_at_ms),
            "mode": self.mode,
            "scene": fields.pop("scene", self.scene),
        }
        event.update({key: value for key, value in fields.items() if value is not None})
        self.events.append(event)
        return event

    def compact(self, limit: int = 24) -> List[dict]:
        compacted: List[dict] = []
        for event in self.events[-max(1, limit) :]:
            compacted.append(
                {
                    key: value
                    for key, value in event.items()
                    if key in {"trace_id", "event_type", "elapsed_ms", "mode", "scene", "tool_name", "action_type", "status", "repair_applied", "score", "failures"}
                }
            )
        return compacted

    def to_record(self, status: str = "finished", extra: Optional[Dict[str, Any]] = None, compact_limit: int = 64) -> dict:
        finished_at_ms = int(time.time() * 1000)
        record = {
            "trace_id": self.trace_id,
            "mode": self.mode,
            "scene": self.scene,
            "status": str(status or "finished"),
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": finished_at_ms,
            "duration_ms": max(0, finished_at_ms - self.started_at_ms),
            "event_count": len(self.events),
            "events": self.compact(limit=compact_limit),
        }
        if isinstance(extra, dict):
            record.update({key: value for key, value in extra.items() if value is not None})
        return record


class AgentTraceJsonlSink:
    def __init__(self, path: str, compact_limit: int = 64):
        self.path = Path(str(path or "")).expanduser() if str(path or "").strip() else None
        self.compact_limit = max(1, int(compact_limit or 64))

    def enabled(self) -> bool:
        return self.path is not None

    def write(self, trace: AgentTrace, status: str = "finished", extra: Optional[Dict[str, Any]] = None) -> Optional[dict]:
        if self.path is None or trace is None:
            return None
        record = trace.to_record(status=status, extra=extra, compact_limit=self.compact_limit)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return record


class ReactRunner:
    def __init__(self, mode: str = "agent", scene: str = "general_agent"):
        self.mode = str(mode or "agent")
        self.scene = str(scene or "general_agent")
        self.trace = AgentTrace(mode=self.mode, scene=self.scene)
        self.observation_store = ObservationStore()
        self.planner_notes: List[dict] = []
        self.events: List[dict] = []

    def emit(self, event_type: str, **fields: Any) -> dict:
        event = self.trace.record(event_type, **fields)
        self.events.append(event)
        if event.get("scene"):
            self.scene = str(event.get("scene") or self.scene)
        return event

    def start(self, max_rounds: int) -> dict:
        return self.emit("intent", scene=self.scene, max_rounds=max_rounds)

    def plan_note(self, scene: str, plan: dict) -> dict:
        note = {
            "scene": scene,
            "action_type": str((plan or {}).get("action_type") or "").strip(),
            "reasoning_summary": str((plan or {}).get("reasoning_summary") or "").strip(),
            "stop_reason": str((plan or {}).get("stop_reason") or "").strip(),
            "planned_tools": self.step_tool_names((plan or {}).get("steps") or []),
            "trace_id": self.trace.trace_id,
        }
        self.planner_notes.append(note)
        self.emit("planner_action", scene=scene, action_type=note["action_type"], tool_name=", ".join(note["planned_tools"]))
        return note

    def validation(self, scene: str, steps: List[dict]) -> dict:
        return self.emit(
            "validation",
            scene=scene,
            status="ready" if steps else "empty",
            tool_name=", ".join(self.step_tool_names(steps)),
        )

    def observation(self, scene: str, tool_name: str, status: str, observation: Optional[dict] = None, cache_key: Optional[Tuple[str, str]] = None) -> dict:
        if observation is not None:
            self.observation_store.append(observation, cache_key=cache_key)
        return self.emit("observation", scene=scene, tool_name=str(tool_name or "").strip(), status=status)

    def final(self, scene: str, status: str = "final", action_type: str = "final") -> dict:
        return self.emit("final", scene=scene, action_type=action_type, status=status)

    def step_tool_names(self, steps: List[dict]) -> List[str]:
        names: List[str] = []
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            tool_name = str(((step or {}).get("tool_call") or {}).get("name") or "").strip()
            if tool_name:
                names.append(tool_name)
        return names


class SynthesisRunner:
    def build_context(
        self,
        planner_notes: List[dict],
        tool_observations: List[dict],
        observation_context: Callable[[List[dict], int], List[dict]],
        trace: Optional[AgentTrace] = None,
        limit_reached: bool = False,
        answer_contract: Optional[dict] = None,
        followup_actionability_policy: Optional[dict] = None,
    ) -> dict:
        instruction = "不要再调用工具；只基于这些证据回答用户原问题。"
        if limit_reached:
            instruction = "工具调用预算已用完。请停止调用任何工具，必须仅基于这些证据回答用户原问题。"
        context = {
            "planner_notes": planner_notes[-4:],
            "tool_observations": observation_context(tool_observations, 8),
            "instruction": instruction,
        }
        if answer_contract:
            context["answer_contract"] = answer_contract
        if followup_actionability_policy:
            context["followup_actionability_policy"] = followup_actionability_policy
        if trace is not None:
            context["trace"] = trace.compact(limit=16)
        return context


def extract_answer_contract_from_text(text: str) -> dict:
    content = str(text or "").strip()
    if not content:
        return {}
    normalized = content.lower().replace(" ", "")
    contract: Dict[str, Any] = {}

    top_match = re.search(r"top\s*([1-9][0-9]?)", content, flags=re.IGNORECASE)
    if not top_match:
        top_match = re.search(r"前\s*([1-9][0-9]?)\s*(?:个|名|条)?", content)
    if top_match:
        with contextlib.suppress(ValueError):
            contract["requested_count"] = max(1, min(30, int(top_match.group(1))))

    if "机会" in content and ("卡片" in content or "opportunity" in normalized):
        contract["entity_type"] = "opportunity_card"
    if "解说" in content or "解读" in content or "分析" in content:
        contract["answer_shape"] = "card_with_analysis"
    if "中文" in content or re.search(r"[\u4e00-\u9fff]", content):
        contract["language"] = "zh"

    if contract.get("entity_type") == "opportunity_card":
        contract.setdefault("requested_count", 5 if "top5" in normalized else None)
        contract["must_include"] = [
            "只展示用户请求数量的机会卡片",
            "逐卡展开前先给出一张精简排名表（列：排名/机会主题/类目路径/机会得分），表中主题同样使用中文翻译（English 原文）双语",
            "每张卡片包含机会理由、关键证据、主要风险或证据边界、下一步验证",
            "不要把工具默认返回数量当成用户请求数量",
            "卡片标题中的品类名使用「中文翻译（English 原文）」双语格式，例如「真空保温杯（Tumblers）」",
        ]
        contract["must_not_include"] = [
            "超过用户请求数量的机会排名",
            "缺少逐卡解说的裸工具表格",
            "只有逐卡解说但未给出排名表",
            "用未返回的数据补齐机会卡片",
            "只保留英文原文、未给出中文翻译的卡片标题",
        ]
    if contract.get("requested_count") is None:
        contract.pop("requested_count", None)
    return contract


def followup_actionability_policy() -> dict:
    return {
        "policy": "报告尾部追问必须按当前工具能力标注支持度；不可把缺少 provider 的问题写成可完整执行的复制追问。",
        "directly_supported": [
            "候选池销量、价格、评分、评论数量、BSR、品牌分布",
            "品牌/标题/材质关键词切片后的 top ASIN 和评分/评论数量/销量分布",
            "指定 ASIN 的价格、评分、评论数、销量估算和预测解释",
            "基于明确假设的利润/盈亏平衡测算",
        ],
        "partial_or_unsupported": [
            "评论关键词/低分原因需要 asin_review_insights 或评论文本 provider",
            "Amazon 月搜索量需要 ABA/Helium 10/Jungle Scout/关键词量 provider",
            "评论文本 provider 或关键词量 provider 未配置时，只能返回能力缺口和替代验证路径",
        ],
        "response_rule": "若问题包含评论关键词、月搜索量、品牌内 top3 等能力缺口，必须显式写出当前只能回答哪些部分，以及缺什么工具或外部数据。",
    }


class AgentGrader:
    version = "deterministic_v1"

    DIRECT_SUPPORT_MARKERS = (
        "✅",
        "可直接执行",
        "直接执行",
        "当前可直接",
        "可以直接",
        "可执行",
    )
    PROVIDER_BOUNDARY_MARKERS = (
        "provider",
        "外部",
        "能力缺口",
        "未配置",
        "未支持",
        "无法直接",
        "不能直接",
        "不得",
        "需要",
        "需 ",
        "需外部",
        "需评论文本",
        "需关键词量",
        "待补",
    )
    REVIEW_PROVIDER_TERMS = (
        "评论关键词",
        "差评关键词",
        "低分原因",
        "评论质量",
        "评论痛点",
        "差评集中",
        "1-3 星差评",
        "1–3 星差评",
        "1～3 星差评",
    )
    KEYWORD_DEMAND_TERMS = (
        "月搜索量",
        "amazon 搜索量",
        "Amazon 搜索量",
        "ABA",
        "Helium10",
        "JungleScout",
    )

    def grade(
        self,
        *,
        user_text: str = "",
        answer_text: str = "",
        answer_contract: Optional[dict] = None,
        tool_observations: Optional[List[dict]] = None,
    ) -> dict:
        checks: List[dict] = []
        if not str(answer_text or "").strip():
            return {"grader": self.version, "status": "skipped", "score": None, "checks": [], "failures": []}
        contract = answer_contract or extract_answer_contract_from_text(user_text)
        if contract.get("entity_type") == "opportunity_card":
            checks.extend(self._grade_opportunity_contract(str(answer_text or ""), contract))
        checks.extend(self._grade_provider_boundaries(str(answer_text or ""), tool_observations or []))

        if not checks:
            return {"grader": self.version, "status": "skipped", "score": None, "checks": [], "failures": []}

        total_weight = sum(float(check.get("weight") or 1.0) for check in checks)
        passed_weight = sum(float(check.get("weight") or 1.0) for check in checks if check.get("passed"))
        score = round(passed_weight / total_weight, 4) if total_weight else 1.0
        failures = [str(check.get("name") or "check") for check in checks if not check.get("passed")]
        status = "pass" if not failures else ("partial" if score >= 0.5 else "fail")
        return {"grader": self.version, "status": status, "score": score, "checks": checks, "failures": failures}

    def _grade_opportunity_contract(self, answer_text: str, contract: dict) -> List[dict]:
        requested_count = contract.get("requested_count")
        checks: List[dict] = []
        if requested_count:
            observed_count = self._observed_opportunity_count(answer_text)
            checks.append(
                self._check(
                    "opportunity_requested_count",
                    observed_count == int(requested_count),
                    0.35,
                    "requested=%s observed=%s" % (requested_count, observed_count if observed_count is not None else "unknown"),
                )
            )
            too_many = observed_count is not None and observed_count > int(requested_count)
            checks.append(
                self._check(
                    "opportunity_no_extra_items",
                    not too_many and "实际返回机会数: 10" not in answer_text and "实际返回机会数：10" not in answer_text,
                    0.2,
                    "answer must not expose more opportunities than requested",
                )
            )

        required_terms = ["机会理由", "关键证据", "下一步"]
        risk_ok = "风险" in answer_text or "证据边界" in answer_text or "边界" in answer_text
        missing = [term for term in required_terms if term not in answer_text]
        if not risk_ok:
            missing.append("风险/证据边界")
        checks.append(
            self._check(
                "opportunity_card_analysis_fields",
                not missing,
                0.45,
                "missing=%s" % (", ".join(missing) if missing else "none"),
            )
        )
        return checks

    def _grade_provider_boundaries(self, answer_text: str, tool_observations: List[dict]) -> List[dict]:
        checks: List[dict] = []
        review_conflicts = self._unsupported_direct_lines(answer_text, self.REVIEW_PROVIDER_TERMS)
        keyword_conflicts = self._unsupported_direct_lines(answer_text, self.KEYWORD_DEMAND_TERMS)
        if review_conflicts or self._provider_required_observed(tool_observations, "asin_review_insights"):
            checks.append(
                self._check(
                    "review_provider_boundary",
                    not review_conflicts and self._mentions_review_provider_boundary(answer_text),
                    0.5,
                    self._details_for_conflicts(review_conflicts, "review_text_provider boundary required"),
                )
            )
        if keyword_conflicts or self._provider_required_observed(tool_observations, "amazon_keyword_demand"):
            checks.append(
                self._check(
                    "keyword_demand_provider_boundary",
                    not keyword_conflicts and self._mentions_keyword_provider_boundary(answer_text),
                    0.5,
                    self._details_for_conflicts(keyword_conflicts, "keyword demand provider boundary required"),
                )
            )
        return checks

    def _unsupported_direct_lines(self, answer_text: str, terms: Tuple[str, ...]) -> List[str]:
        conflicts: List[str] = []
        for line in str(answer_text or "").splitlines():
            normalized = line.strip()
            if not normalized or not any(term in normalized for term in terms):
                continue
            if not any(marker in normalized for marker in self.DIRECT_SUPPORT_MARKERS):
                continue
            if any(marker in normalized for marker in self.PROVIDER_BOUNDARY_MARKERS):
                continue
            conflicts.append(normalized[:240])
        return conflicts

    def _observed_opportunity_count(self, answer_text: str) -> Optional[int]:
        match = re.search(r"实际返回机会数\s*[:：]\s*(\d+)", answer_text)
        if match:
            with contextlib.suppress(ValueError):
                return int(match.group(1))
        table_rows = re.findall(r"^\|\s*\d+\s*\|", answer_text, flags=re.MULTILINE)
        if table_rows:
            return len(table_rows)
        headings = re.findall(r"^#{1,4}\s*(?:机会\s*)?\d+[\.、：:]", answer_text, flags=re.MULTILINE)
        return len(headings) if headings else None

    def _provider_required_observed(self, tool_observations: List[dict], tool_name: str) -> bool:
        for observation in tool_observations or []:
            if not isinstance(observation, dict) or observation.get("tool_name") != tool_name:
                continue
            raw_result = str(observation.get("raw_result") or observation.get("llm_result") or "")
            if "provider_required" in raw_result or "missing_capability" in raw_result:
                return True
        return False

    def _mentions_review_provider_boundary(self, answer_text: str) -> bool:
        return "review_text_provider" in answer_text or "评论文本" in answer_text or "评论文本分析 provider" in answer_text

    def _mentions_keyword_provider_boundary(self, answer_text: str) -> bool:
        return "关键词量 provider" in answer_text or "amazon_keyword_demand" in answer_text or "ABA" in answer_text

    def _details_for_conflicts(self, conflicts: List[str], fallback: str) -> str:
        if not conflicts:
            return fallback
        return "conflicting_lines=%s" % " | ".join(conflicts[:3])

    def _check(self, name: str, passed: bool, weight: float, details: str = "") -> dict:
        return {"name": name, "passed": bool(passed), "weight": float(weight), "details": details}


def preflight_tool_call(tool_call: Dict[str, Any], answer_contract: Optional[dict] = None) -> Dict[str, Any]:
    if not isinstance(tool_call, dict):
        return tool_call
    tool_name = str(tool_call.get("name") or "").strip()
    parameters = dict(tool_call.get("parameters") or {}) if isinstance(tool_call.get("parameters"), dict) else {}

    limits = TOOL_NUMERIC_LIMITS.get(tool_name) or {}
    for param_name, (min_value, max_value) in limits.items():
        if param_name not in parameters or parameters.get(param_name) in (None, ""):
            continue
        with contextlib.suppress(TypeError, ValueError):
            raw_value = int(float(parameters[param_name]))
            parameters[param_name] = max(min_value, min(max_value, raw_value))

    contract = answer_contract or {}
    requested_count = contract.get("requested_count")
    if tool_name == "opportunity_discovery" and not str(parameters.get("marketplace") or "").strip():
        parameters["marketplace"] = "US"
    if tool_name == "opportunity_discovery" and contract.get("entity_type") == "opportunity_card" and requested_count:
        with contextlib.suppress(TypeError, ValueError):
            parameters["limit"] = max(1, min(30, int(requested_count)))

    updated = dict(tool_call)
    updated["parameters"] = parameters
    return updated


_CANDIDATE_POOL_HANDLES = {
    "candidate_pool_stats",
    "candidate_pool_slice",
    "candidate_pool_trends",
    "candidate_pool_weak_forecast",
    "category_benchmark",
    "top_asin_drilldown",
    "product_forecast_explain",
    "expand_candidates",
}


def _coerce_payload(result: Any) -> Optional[Dict[str, Any]]:
    if isinstance(result, dict):
        return result
    text = str(result or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    try:
        loaded = json.loads(text)
    except (ValueError, TypeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _normalize_asin_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        items = re.split(r"[\s,\u3001]+", value)
    elif isinstance(value, (list, tuple, set)):
        items: List[str] = []
        for entry in value:
            if isinstance(entry, dict):
                asin = str(entry.get("asin") or entry.get("ASIN") or "").strip()
                if asin:
                    items.append(asin)
            else:
                items.append(str(entry or "").strip())
    else:
        return []
    seen: List[str] = []
    seen_set: set[str] = set()
    for raw in items:
        candidate = raw.strip().upper()
        if not candidate or candidate in seen_set:
            continue
        if re.fullmatch(r"[A-Z0-9]{8,14}", candidate):
            seen.append(candidate)
            seen_set.add(candidate)
    return seen


def _extract_candidate_pool(payload: Dict[str, Any]) -> Dict[str, Any]:
    nested_keys = ("candidate_pool", "pool", "data", "result")
    sources: List[Dict[str, Any]] = [payload]
    for key in nested_keys:
        nested = payload.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    pool_id = ""
    asins: List[str] = []
    size: Optional[int] = None
    leaf_categories: Any = None
    for source in sources:
        if not pool_id:
            for pid_key in ("candidate_pool_id", "pool_id", "id"):
                value = source.get(pid_key)
                if value:
                    pool_id = str(value).strip()
                    break
        if not asins:
            for asin_key in ("candidate_asins", "asins", "asin_list"):
                normalized = _normalize_asin_list(source.get(asin_key))
                if normalized:
                    asins = normalized
                    break
        if size is None:
            for size_key in ("candidate_count", "pool_size", "size", "total"):
                raw_size = source.get(size_key)
                if isinstance(raw_size, (int, float)):
                    size = int(raw_size)
                    break
        if leaf_categories is None:
            leaf_categories = source.get("leaf_categories") or source.get("leaf_category_distribution")
    if size is None and asins:
        size = len(asins)
    if not (pool_id or asins or size):
        return {}
    pool: Dict[str, Any] = {}
    if pool_id:
        pool["pool_id"] = pool_id
    if asins:
        pool["asins"] = asins[:30]
    if size is not None:
        pool["size"] = size
    if leaf_categories:
        if isinstance(leaf_categories, list):
            pool["leaf_categories"] = leaf_categories[:5]
        else:
            pool["leaf_categories"] = leaf_categories
    return pool


def _extract_category_handle(payload: Dict[str, Any]) -> Dict[str, Any]:
    sources: List[Dict[str, Any]] = [payload]
    for key in ("data", "result", "category"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    category_id = ""
    category_path = ""
    for source in sources:
        if not category_id:
            for cid_key in ("category_id", "id", "leaf_category_id"):
                value = source.get(cid_key)
                if value:
                    category_id = str(value).strip()
                    break
        if not category_path:
            for path_key in ("category_path", "path", "leaf_category_path"):
                value = source.get(path_key)
                if value:
                    if isinstance(value, list):
                        category_path = " > ".join(str(item) for item in value if item)
                    else:
                        category_path = str(value).strip()
                    break
    handle: Dict[str, Any] = {}
    if category_id:
        handle["category_id"] = category_id
    if category_path:
        handle["category_path"] = category_path
    return handle


class AgentHarness:
    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        chat_id: Optional[str] = None,
        session_store: Optional[SessionContextStore] = None,
    ):
        self.registry = registry or ToolRegistry()
        self.synthesis_runner = SynthesisRunner()
        self.grader = AgentGrader()
        self.chat_id = (str(chat_id).strip() if chat_id else "") or None
        self.session_store = session_store or SESSION_CONTEXT

    def session_snapshot(self) -> Dict[str, Any]:
        if not self.chat_id:
            return {}
        snapshot = self.session_store.get(self.chat_id)
        if not snapshot:
            return {}
        compact: Dict[str, Any] = {}
        for key in ("last_product_query", "last_marketplace", "last_category_id", "last_category_path"):
            value = snapshot.get(key)
            if value:
                compact[key] = value
        pool = snapshot.get("last_candidate_pool")
        if isinstance(pool, dict) and pool:
            compact["last_candidate_pool"] = {
                "pool_id": pool.get("pool_id"),
                "size": pool.get("size"),
                "asins_preview": (pool.get("asins") or [])[:8],
                "asins_total": len(pool.get("asins") or []),
                "leaf_categories": pool.get("leaf_categories"),
            }
        tool_calls = snapshot.get("last_tool_calls")
        if tool_calls:
            now = time.time()
            recent = []
            for _composite_key, entry in list(tool_calls.items())[-6:]:
                if isinstance(entry, dict):
                    recorded_at = entry.get("recorded_at")
                    age_seconds = None
                    if isinstance(recorded_at, (int, float)):
                        age_seconds = max(0, int(now - recorded_at))
                    recent.append(
                        {
                            "tool_name": entry.get("tool_name"),
                            "params_fingerprint": entry.get("params_fingerprint"),
                            "params_preview": entry.get("params_preview"),
                            "summary": entry.get("summary"),
                            "recorded_at": recorded_at,
                            # 明确标注：这些是之前轮次缓存下来的结果，不是本轮实时重查。
                            # planner 可据此判断是否需要 force_refresh 重新取最新数据。
                            "cached": True,
                            "cached_age_seconds": age_seconds,
                        }
                    )
            if recent:
                compact["recent_tool_calls"] = recent
                # 兼容旧字段：保留 recent_tool_results（tool_name + summary）
                compact["recent_tool_results"] = [
                    {"tool_name": item["tool_name"], "summary": item.get("summary")}
                    for item in recent
                ]
        return compact

    def after_tool_observation(
        self,
        tool_call: Dict[str, Any],
        result: Any,
        compact_result: str = "",
    ) -> None:
        if not self.chat_id:
            return
        tool_name = str((tool_call or {}).get("name") or "").strip()
        if not tool_name:
            return
        parameters = (tool_call or {}).get("parameters") or {}
        updates: Dict[str, Any] = {}

        product_query = str(parameters.get("product_query") or "").strip()
        if product_query:
            updates["last_product_query"] = product_query
        marketplace = str(parameters.get("marketplace") or "").strip()
        if marketplace:
            updates["last_marketplace"] = marketplace.upper()

        payload = _coerce_payload(result)
        if payload is not None:
            if tool_name == "resolve_candidates":
                pool = _extract_candidate_pool(payload)
                if pool:
                    if not pool.get("pool_id") and product_query:
                        pool["product_query"] = product_query
                    updates["last_candidate_pool"] = pool
            if tool_name == "category_resolve":
                handle = _extract_category_handle(payload)
                if handle.get("category_id"):
                    updates["last_category_id"] = handle["category_id"]
                if handle.get("category_path"):
                    updates["last_category_path"] = handle["category_path"]

        if updates:
            self.session_store.update(self.chat_id, updates)
        # 失败/错误结果不得进入跨轮去重缓存：否则下一轮相同入参会被同签名去重拦截，
        # 直接复述这条失败摘要，用户会拿到"工具其实没成功过"的错误旧结果。
        if compact_result and not tool_result_is_unusable(result, compact_result):
            self.session_store.record_tool_result(self.chat_id, tool_name, compact_result, parameters=parameters)

    def new_observation_store(self) -> ObservationStore:
        return ObservationStore()

    def new_trace(self, mode: str = "agent", scene: str = "general_agent") -> AgentTrace:
        return AgentTrace(mode=mode, scene=scene)

    def new_react_runner(self, mode: str = "agent", scene: str = "general_agent") -> ReactRunner:
        return ReactRunner(mode=mode, scene=scene)

    def write_trace(
        self,
        trace: AgentTrace,
        path: str,
        status: str = "finished",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[dict]:
        return AgentTraceJsonlSink(path).write(trace, status=status, extra=extra)

    def synthesis_context(
        self,
        planner_notes: List[dict],
        tool_observations: List[dict],
        observation_context: Callable[[List[dict], int], List[dict]],
        trace: Optional[AgentTrace] = None,
        limit_reached: bool = False,
        answer_contract: Optional[dict] = None,
        followup_actionability_policy: Optional[dict] = None,
    ) -> dict:
        return self.synthesis_runner.build_context(
            planner_notes,
            tool_observations,
            observation_context,
            trace=trace,
            limit_reached=limit_reached,
            answer_contract=answer_contract,
            followup_actionability_policy=followup_actionability_policy,
        )

    def answer_contract_from_text(self, text: str) -> dict:
        return extract_answer_contract_from_text(text)

    def followup_actionability_policy(self) -> dict:
        return followup_actionability_policy()

    def preflight_tool_call(self, tool_call: Dict[str, Any], answer_contract: Optional[dict] = None) -> Dict[str, Any]:
        return preflight_tool_call(tool_call, answer_contract=answer_contract)

    def grade_answer(
        self,
        *,
        user_text: str = "",
        answer_text: str = "",
        answer_contract: Optional[dict] = None,
        tool_observations: Optional[List[dict]] = None,
    ) -> dict:
        return self.grader.grade(
            user_text=user_text,
            answer_text=answer_text,
            answer_contract=answer_contract,
            tool_observations=tool_observations,
        )

    def scene_policy(self, scene: str, mode: str = "agent") -> dict:
        return self.registry.scene_policy(scene, mode)

    def planner_allowed_tool_names(self, scene: str, mode: str = "agent") -> List[str]:
        return self.registry.allowed_tool_names(scene, mode)

    def planner_tool_catalog(self, scene: str, mode: str = "agent", tool_label: Optional[Callable[[str], str]] = None) -> List[dict]:
        return self.registry.tool_catalog(scene, mode, tool_label=tool_label)

    def planner_observation_context(
        self,
        tool_observations: List[dict],
        truncate_text: Callable[[str, int], str],
        limit: int = 6,
    ) -> List[dict]:
        return planner_observation_context(tool_observations, truncate_text, limit=limit)

    def observed_tool_names(self, tool_observations: List[dict]) -> List[str]:
        return observed_tool_names(tool_observations)

    def scene_single_execution_tools(self, scene: str) -> set[str]:
        return self.registry.single_execution_tools(scene)

    def filter_redundant_planner_steps(self, steps: List[dict], scene: str, tool_observations: List[dict]) -> List[dict]:
        if not steps:
            return []
        single_execution_tools = self.scene_single_execution_tools(scene)
        already_seen_names = set(self.observed_tool_names(tool_observations))

        # 通用跨轮去重：用 (tool_name, params_fingerprint) 作为唯一签名；
        # session 里已经执行过完全相同入参的工具调用一律跳过，参数不同则放行（例如 trends 不同 window_days）。
        session_signatures: set[str] = set()
        session_signature_recorded_at: dict[str, float] = {}
        session_prerequisite_tools: set[str] = set()
        snapshot = self.session_snapshot() or {}
        if isinstance(snapshot, dict):
            for entry in snapshot.get("recent_tool_calls") or []:
                tn = (entry or {}).get("tool_name")
                fp = (entry or {}).get("params_fingerprint")
                if tn and fp:
                    sig = f"{tn}::{fp}"
                    session_signatures.add(sig)
                    ts = (entry or {}).get("recorded_at")
                    if isinstance(ts, (int, float)):
                        session_signature_recorded_at[sig] = float(ts)
            # 结构性兜底：prerequisite 工具的产物已在 session，无论参数是否略有差异都不应再跑
            pool = snapshot.get("last_candidate_pool")
            if isinstance(pool, dict) and pool.get("pool_id"):
                session_prerequisite_tools.add("resolve_candidates")
            if snapshot.get("last_category_id") or snapshot.get("last_category_path"):
                session_prerequisite_tools.add("category_resolve")

        now = time.time()

        def _signature_is_fresh(sig: str) -> bool:
            # 无时间戳（历史数据）按新鲜处理，保持既有去重行为；有时间戳则按新鲜窗口判定。
            ts = session_signature_recorded_at.get(sig)
            if ts is None:
                return True
            return (now - ts) <= CROSS_TURN_DEDUP_FRESHNESS_SECONDS

        kept: List[dict] = []
        planned_signatures: set[str] = set()
        planned_names: set[str] = set()
        for step in steps:
            tool_call = (step or {}).get("tool_call") or {}
            tool_name = str(tool_call.get("name") or "").strip()
            if not tool_name:
                kept.append(step)
                continue
            # force_refresh：planner 显式要求绕过跨轮缓存、强制真实重查（例如扩池完成后候选池内容已变，
            # 而 candidate_pool_stats 等工具入参签名没变，旧统计会被当成最新复述）。
            # 该标记仅用于控制去重，绝不能传给真实工具调用，否则下游可能因未知参数报错——这里先剥离。
            parameters = tool_call.get("parameters")
            force_refresh = False
            if isinstance(parameters, dict):
                for flag_key in ("force_refresh", "_force_refresh", "_refresh", "refresh"):
                    if flag_key in parameters:
                        if _coerce_bool_flag(parameters.pop(flag_key)):
                            force_refresh = True
            fingerprint = tool_call_fingerprint(tool_call.get("parameters") or {})
            signature = f"{tool_name}::{fingerprint}"
            # 0) 状态轮询型工具：每轮都必须真实重查，仅在同一请求内去重，
            #    跨轮 session_signatures / prerequisite 句柄一律不拦截，避免复述 queued 旧状态。
            if tool_name in STATUS_POLL_REFRESH_TOOLS:
                if signature in planned_signatures:
                    continue
                kept.append(step)
                planned_signatures.add(signature)
                planned_names.add(tool_name)
                continue
            # 0.5) force_refresh：planner 主动要求刷新，跨轮一律放行真实重查，
            #      仅保留同一请求内去重（避免一轮里把同一刷新重复执行两次）。
            if force_refresh:
                if signature in planned_signatures:
                    continue
                kept.append(step)
                planned_signatures.add(signature)
                planned_names.add(tool_name)
                continue
            # 1) 跨轮同签名去重（通用）：仅在新鲜窗口内才拦截；
            #    超出新鲜窗口的旧签名放行重跑，避免长对话里复述已过期的旧结果。
            if signature in planned_signatures:
                continue
            if signature in session_signatures and _signature_is_fresh(signature):
                continue
            # 2) 跨轮 prerequisite 句柄兜底（候选池/类目）
            if tool_name in session_prerequisite_tools:
                continue
            # 3) 同一请求内 single-execution 工具按 tool_name 去重（保留原有语义）
            if tool_name in single_execution_tools and (tool_name in already_seen_names or tool_name in planned_names):
                continue
            kept.append(step)
            planned_signatures.add(signature)
            planned_names.add(tool_name)
        return kept

    def tool_call_allowed_for_scene(self, tool_call: Dict[str, Any], scene: str, mode: str = "agent") -> bool:
        return self.registry.tool_allowed_for_scene(tool_call, scene, mode)

    def normalize_planner_action(
        self,
        action: Any,
        scene: str,
        mode: str,
        normalize_planner_step: Callable[[dict, str, str], Optional[dict]],
    ) -> Tuple[bool, str, List[dict], str]:
        return normalize_planner_action(action, scene, mode, normalize_planner_step)

    def normalize_planner_plan(
        self,
        plan_payload: Any,
        scene: str,
        mode: str,
        normalize_planner_step: Callable[[dict, str, str], Optional[dict]],
    ) -> dict:
        return normalize_planner_plan(
            plan_payload,
            scene,
            mode,
            self.scene_policy,
            normalize_planner_step,
            self.registry.scene_tool_policy,
        )

    def scene_for_explicit_tool(self, tool_name: str, current_scene: str) -> str:
        return self.registry.scene_for_explicit_tool(tool_name, current_scene)

    def explicit_tool_name_from_text(self, text: str) -> str:
        return explicit_tool_name_from_text(text, self.registry.allowed_tools)

    def extract_explicit_tool_parameters_from_text(
        self,
        text: str,
        tool_name: str,
        normalize_tool_call: Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        return extract_explicit_tool_parameters_from_text(text, tool_name, self.registry.allowed_tools, normalize_tool_call)

    def planner_plan_note(self, scene: str, plan: dict, trace: Optional[AgentTrace] = None) -> dict:
        note = {
            "scene": scene,
            "action_type": str((plan or {}).get("action_type") or "").strip(),
            "reasoning_summary": str((plan or {}).get("reasoning_summary") or "").strip(),
            "stop_reason": str((plan or {}).get("stop_reason") or "").strip(),
            "planned_tools": [
                str(((step or {}).get("tool_call") or {}).get("name") or "").strip()
                for step in (plan or {}).get("steps") or []
                if isinstance(step, dict)
            ],
        }
        if trace is not None:
            note["trace_id"] = trace.trace_id
        return note

    def repair_tool_call_required_arguments(
        self,
        tool_call: Dict[str, Any],
        infer_required_arguments: Callable[[str, Dict[str, Any]], Dict[str, Any]],
        normalize_tool_call: Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(tool_call, dict):
            return None
        tool_name = str(tool_call.get("name") or "").strip()
        parameters = dict(tool_call.get("parameters") or {}) if isinstance(tool_call.get("parameters"), dict) else {}
        snapshot = self.session_snapshot() if self.chat_id else {}
        if snapshot:
            if tool_name in _CANDIDATE_POOL_HANDLES:
                pool = snapshot.get("last_candidate_pool") or {}
                if pool.get("pool_id") and not str(parameters.get("candidate_pool_id") or "").strip():
                    parameters["candidate_pool_id"] = pool["pool_id"]
                if not parameters.get("candidate_asins"):
                    asins_preview = pool.get("asins_preview") or []
                    if asins_preview:
                        parameters["candidate_asins"] = list(asins_preview)
            if not str(parameters.get("product_query") or "").strip() and snapshot.get("last_product_query"):
                parameters["product_query"] = snapshot["last_product_query"]
            if not str(parameters.get("marketplace") or "").strip() and snapshot.get("last_marketplace"):
                parameters["marketplace"] = snapshot["last_marketplace"]
            if not str(parameters.get("category_id") or "").strip() and snapshot.get("last_category_id"):
                parameters["category_id"] = snapshot["last_category_id"]
            if not str(parameters.get("category_path") or "").strip() and snapshot.get("last_category_path"):
                parameters["category_path"] = snapshot["last_category_path"]
        inferred = infer_required_arguments(tool_name, parameters) or {}
        if not inferred and tool_call_has_required_arguments({"name": tool_name, "parameters": parameters}):
            normalized_existing = normalize_tool_call(tool_name, parameters)
            return normalized_existing if normalized_existing is not None else {"name": tool_name, "parameters": parameters}
        repaired_parameters = dict(parameters)
        for argument_name, argument_value in inferred.items():
            if not str(repaired_parameters.get(argument_name) or "").strip():
                repaired_parameters[argument_name] = argument_value
        normalized_call = normalize_tool_call(tool_name, repaired_parameters)
        if normalized_call is None:
            return None
        return normalized_call if tool_call_has_required_arguments(normalized_call) else None


def explicit_tool_name_from_text(text: str, allowed_tools: set[str]) -> str:
    content = str(text or "")
    if not content:
        return ""
    has_explicit_tool_intent = bool(
        re.search(r"(^|\s)/tool\b|(?:请|帮我)?(?:调用|执行|运行|使用).{0,24}(?:工具|tool)?", content, flags=re.IGNORECASE)
    )
    if not has_explicit_tool_intent:
        return ""
    lower_content = content.lower().replace("-", "_")
    for tool_name in sorted(allowed_tools, key=len, reverse=True):
        if tool_name.lower() in lower_content:
            return tool_name
    return ""


def extract_explicit_tool_subject(text: str, tool_name: str) -> str:
    content = re.sub(r"^\s*/(?:agent|tool|help|web|report|wf|workflow)\b\s*", "", str(text or "").strip(), flags=re.IGNORECASE)
    if not content:
        return ""
    escaped_tool_name = re.escape(str(tool_name or "").replace("-", "_"))
    patterns = (
        rf"{escaped_tool_name}\s*[，,:：]?\s*(?:把|将|解析|获取|生成|召回|查询|分析)?\s*([^，。！？\n]{{2,100}}?)(?:\s+在\s+|\s+解析成|\s+解析为|\s+的|，|,|。|；|;|并|$)",
        r"(?:把|将)\s+([^，。！？\n]{2,100}?)\s+(?:解析成|解析为|转成|映射到)",
        r"(?:解析|获取|生成|召回|查询|分析)\s+([^，。！？\n]{2,100}?)(?:\s+在\s+|\s+解析成|\s+解析为|\s+的|，|,|。|；|;|并|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, content, flags=re.IGNORECASE)
        if not match:
            continue
        subject = str(match.group(1) or "").strip(" ：:，,。；;、")
        subject = re.sub(r"^(?:候选池|类目|商品|品类|关键词)\s*", "", subject, flags=re.IGNORECASE).strip(" ：:，,。；;、")
        subject = re.sub(r"\s+(?:in|on)\s+(?:amazon|keepa|temu).*$", "", subject, flags=re.IGNORECASE).strip()
        if subject and not subject.lower().startswith(("resolve_candidates", "category_resolve")):
            return subject[:120]
    return ""


def extract_explicit_tool_parameters_from_text(
    text: str,
    tool_name: str,
    allowed_tools: set[str],
    normalize_tool_call: Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]],
) -> Dict[str, Any]:
    content = str(text or "")
    if not content or not explicit_tool_name_from_text(content, allowed_tools):
        return {}

    raw_params: Dict[str, Any] = {}
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\s，,。；;]+)", content):
        key = str(match.group(1) or "").strip()
        value = str(match.group(2) or "").strip().strip("`'\"")
        if not key or value == "":
            continue
        lowered_key = key.lower()
        if lowered_key in INTEGER_EXPLICIT_ARGUMENTS:
            try:
                raw_params[key] = int(float(value))
            except ValueError:
                raw_params[key] = value
        elif lowered_key in BOOLEAN_EXPLICIT_ARGUMENTS:
            raw_params[key] = value.lower() in {"1", "true", "yes", "y", "是", "对"}
        else:
            raw_params[key] = value

    normalized = normalize_tool_call(tool_name, raw_params)
    return dict((normalized or {}).get("parameters") or {})


def tool_call_has_required_arguments(
    tool_call: Dict[str, Any],
    required_arguments: Dict[str, List[str]] = TOOL_REQUIRED_ARGUMENTS,
) -> bool:
    tool_name = str((tool_call or {}).get("name") or "").strip()
    parameters = (tool_call or {}).get("parameters") if isinstance((tool_call or {}).get("parameters"), dict) else {}
    required_groups = required_arguments.get(tool_name) or []
    if not required_groups:
        return True
    return any(str(parameters.get(required_name) or "").strip() for required_name in required_groups)


def scene_for_explicit_tool(
    tool_name: str,
    current_scene: str,
    tool_layer_registry: Dict[str, Dict[str, Any]],
    scene_tool_policy: Dict[str, Dict[str, Any]],
) -> str:
    normalized_tool = str(tool_name or "").strip()
    current = str(current_scene or "general_agent").strip() or "general_agent"
    metadata = tool_layer_registry.get(normalized_tool) or {}
    scene_tags = [str(scene_tag or "").strip() for scene_tag in metadata.get("scene_tags") or []]
    if current in scene_tags:
        return current
    for preferred_scene in (
        "theme_analysis",
        "asin_specific_analysis",
        "budget_analysis",
        "blank_opportunity_discovery",
        "foundation_qa",
        "general_agent",
    ):
        if preferred_scene in scene_tags and preferred_scene in scene_tool_policy:
            return preferred_scene
    return "general_agent"


def normalize_planner_action(
    action: Any,
    scene: str,
    mode: str,
    normalize_planner_step: Callable[[dict, str, str], Optional[dict]],
) -> Tuple[bool, str, List[dict], str]:
    if not isinstance(action, dict):
        return False, "", [], ""

    action_type = str(action.get("type") or action.get("action_type") or "").strip().lower()
    if action_type in {"final", "finish", "answer"}:
        final_answer = str(action.get("final_answer") or action.get("answer") or "").strip()
        return bool(final_answer), final_answer, [], "final"

    if action_type in {"tool", "call_tool", "action"} or action.get("tool") or action.get("tool_name"):
        tool_payload = action.get("tool") if isinstance(action.get("tool"), dict) else action
        raw_step = {
            "tool_name": tool_payload.get("tool_name") or tool_payload.get("name"),
            "parameters": tool_payload.get("parameters") or tool_payload.get("arguments") or action.get("arguments") or {},
            "goal": tool_payload.get("goal") or action.get("goal") or "",
            "required": True,
        }
        normalized_step = normalize_planner_step(raw_step, scene, mode)
        return False, "", ([normalized_step] if normalized_step is not None else []), "tool"

    return False, "", [], action_type


def normalize_planner_plan(
    plan_payload: Any,
    scene: str,
    mode: str,
    scene_policy: Callable[[str, str], dict],
    normalize_planner_step: Callable[[dict, str, str], Optional[dict]],
    scene_tool_policy: Dict[str, Dict[str, Any]],
) -> dict:
    data = plan_payload if isinstance(plan_payload, dict) else {}
    normalized_scene = str(data.get("scene") or scene or "general_agent").strip() or scene or "general_agent"
    if normalized_scene not in scene_tool_policy:
        normalized_scene = scene or "general_agent"

    steps: List[dict] = []
    answer_ready = False
    final_answer = ""
    action_type = ""
    action = data.get("action")
    if isinstance(action, dict):
        answer_ready, final_answer, steps, action_type = normalize_planner_action(
            action,
            normalized_scene,
            mode,
            normalize_planner_step,
        )

    if not steps and not answer_ready:
        raw_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        for raw_step in raw_steps[:1]:
            normalized_step = normalize_planner_step(raw_step, normalized_scene, mode)
            if normalized_step is not None:
                steps.append(normalized_step)
        if steps:
            action_type = "tool"

    if not final_answer:
        final_answer = str(data.get("final_answer") or "").strip()
    if not answer_ready:
        answer_ready = bool(data.get("answer_ready"))
    if answer_ready and not final_answer and steps:
        answer_ready = False
    if not answer_ready:
        final_answer = ""
    if answer_ready:
        steps = []

    return {
        "scene": normalized_scene,
        "answer_ready": answer_ready,
        "final_answer": final_answer,
        "reasoning_summary": str(data.get("reasoning_summary") or "").strip(),
        "stop_reason": str(data.get("stop_reason") or "").strip(),
        "action_type": action_type or ("final" if answer_ready else "tool" if steps else "none"),
        "steps": steps,
    }
