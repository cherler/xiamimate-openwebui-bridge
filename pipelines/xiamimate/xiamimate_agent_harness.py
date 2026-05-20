from __future__ import annotations

import json
import re
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


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
"""

TOOL_REQUIRED_ARGUMENTS = {
    "resolve_candidates": ["product_query"],
    "category_resolve": ["category_query"],
    "candidate_pool_stats": ["candidate_pool_id", "candidate_asins", "product_query"],
    "candidate_pool_trends": ["candidate_pool_id", "candidate_asins", "product_query"],
    "category_benchmark": ["candidate_pool_id", "candidate_asins", "product_query", "benchmark_category_id", "benchmark_category_path"],
    "top_asin_drilldown": ["asin", "asins", "candidate_pool_id"],
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
}

BOOLEAN_EXPLICIT_ARGUMENTS = {"include_descendants", "expand_if_small", "include_result"}


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
        "max_rounds": 3,
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


def planner_observation_context(
    tool_observations: List[dict],
    truncate_text: Callable[[str, int], str],
    limit: int = 6,
) -> List[dict]:
    items: List[dict] = []
    for observation in (tool_observations or [])[-max(1, limit) :]:
        items.append(
            {
                "tool_name": str(observation.get("tool_name") or "").strip(),
                "arguments": observation.get("arguments") or {},
                "result": truncate_text(str(observation.get("llm_result") or ""), 2400),
            }
        )
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
                    if key in {"trace_id", "event_type", "elapsed_ms", "mode", "scene", "tool_name", "action_type", "status", "repair_applied"}
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
    ) -> dict:
        instruction = "不要再调用工具；只基于这些证据回答用户原问题。"
        if limit_reached:
            instruction = "工具调用预算已用完。请停止调用任何工具，必须仅基于这些证据回答用户原问题。"
        context = {
            "planner_notes": planner_notes[-4:],
            "tool_observations": observation_context(tool_observations, 8),
            "instruction": instruction,
        }
        if trace is not None:
            context["trace"] = trace.compact(limit=16)
        return context


class AgentHarness:
    def __init__(self, registry: Optional[ToolRegistry] = None):
        self.registry = registry or ToolRegistry()
        self.synthesis_runner = SynthesisRunner()

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
    ) -> dict:
        return self.synthesis_runner.build_context(
            planner_notes,
            tool_observations,
            observation_context,
            trace=trace,
            limit_reached=limit_reached,
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
        if not single_execution_tools:
            return steps
        already_seen = set(self.observed_tool_names(tool_observations))
        kept: List[dict] = []
        planned_seen: set[str] = set()
        for step in steps:
            tool_name = str(((step or {}).get("tool_call") or {}).get("name") or "").strip()
            if tool_name in single_execution_tools and (tool_name in already_seen or tool_name in planned_seen):
                continue
            kept.append(step)
            if tool_name in single_execution_tools:
                planned_seen.add(tool_name)
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
        parameters = tool_call.get("parameters") if isinstance(tool_call.get("parameters"), dict) else {}
        inferred = infer_required_arguments(tool_name, parameters) or {}
        if not inferred and tool_call_has_required_arguments(tool_call):
            return tool_call
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
