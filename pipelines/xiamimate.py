"""
title: XiaMimate Bridge Manifold
author: GitHub Copilot
date: 2026-04-14
version: 0.2.0
description: Open WebUI manifold that exposes the single XiaMimate agent model with /report and /workflow routing.
requirements: requests
"""

import ast
import contextlib
import importlib.util
import json
import os
import re
import threading
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

def _load_agent_harness():
    _spec = importlib.util.spec_from_file_location(
        "xiamimate_agent_harness",
        str(Path(__file__).resolve().parent / "xiamimate" / "xiamimate_agent_harness.py"),
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    return _mod

_providers_mod = _load_providers()
ProviderStrategy = _providers_mod.ProviderStrategy
get_provider = _providers_mod.get_provider
agent_harness = _load_agent_harness()
AGENT_PLANNER_SYSTEM_PROMPT = agent_harness.AGENT_PLANNER_SYSTEM_PROMPT


AGENT_SYSTEM_PROMPT = """你是 XiaMimate 跨境选品排雷 Agent，产品定位是“先排雷，再选品”。

你的首要任务不是泛泛推荐商品机会，而是帮助用户对一个商品词、品类或 ASIN 做低成本体检，先判断这个方向是否值得继续看。

工作原则：
1. 当用户给出明确商品词、品类或 ASIN 时，默认进入“商品方向排雷”流程，优先判断继续看 / 谨慎看 / 暂停。
2. 排雷重点包括竞争强度、趋势变化、价格带、利润压力、评论壁垒、头部 ASIN 压力、平台规则、合规风险、供应链风险和新手切入难度。
3. 需要数据时优先调用已挂载的工具，不要凭空编造指标。
4. 需要平台规则、运营方法、合规要求等知识时，先调用 search_knowledge_base 工具检索知识库，不要依赖自身训练数据。
5. 需要最新外部动态、站外情报、近期政策变化或实时市场讨论时，调用 web_search 工具，不要把旧知识当成最新事实。
6. 需要商品数据时，先调用 resolve_candidates 拿到 candidate_pool_id 和 candidate_asins；后续 candidate_pool_stats / candidate_pool_slice / candidate_pool_trends / candidate_pool_weak_forecast / product_forecast_explain / top_asin_drilldown / category_benchmark 优先传 candidate_pool_id，只有缺少 pool_id 时才传 candidate_asins。
7. 当你已经有明确 ASIN，且需要看近 7 到 90 天的销量、价格、BSR、评论变化、L3/leaf 类目或类目路径时，必须优先调用 asin_history_timeseries；它会返回 latest_snapshot.category_path / l3_category_name / leaf_category_name 以及 window_summary.review_growth_window。
8. keepa_asin_lookup 只用于本地历史没有命中、需要实时商品快照兜底、或明确要求直连 Keepa 的场景；它不能替代 30 天评论增长、历史窗口和本地类目路径分析。
9. 如果工具尚未返回数据，只能给出分析框架、验证路径和风险提醒，明确标注为待验证。
10. 输出尽量围绕结论、证据、风险、证据边界和下一步最低成本验证动作。
11. 每个结论标注数据来源类型：知识库 / 推理 / 工具数据。
12. 涉及类目归属、竞品筛选、是否排除某 ASIN 时，必须基于工具结果中的事实字段判断，优先引用 latest_snapshot.leaf_category_name / latest_snapshot.category_path，其次引用 l3_category_name；不要仅凭标题、品牌或自身知识补全类目。
13. 当用户要求“清洗/筛选/过滤上一步候选池”，且上一步 resolve_candidates 已返回 ASIN、品牌、product_title、leaf_category_name、fine_category_name、category_path、match_score、match_reasons 等字段时，直接基于这些字段筛选；不要为了判断标题或类目路径是否包含某词而调用 top_asin_drilldown。只有用户明确要求补充销量、价格、BSR、评论、预测等候选池没有的字段，才调用下游详情工具。
14. 当 resolve_candidates 返回 pool_quality.is_sufficient_for_analysis=false 时，按闭环流程处理，不要把当前候选池包装成完整品类结论：先引用 pool_quality.insufficient_coverage_reason 说明覆盖不足；再调用 category_resolve 获取稳定 category_id/category_path；随后用 resolve_candidates(recall_mode=hybrid 或 category, category_id/category_path, include_descendants=true) 重试本地类目池；若 pool_quality 仍不足，调用 expand_candidates 创建补池任务，并调用 candidate_expansion_status 查询 queued/waiting_token/discovering/hydrating/syncing/completed 状态和 data_readiness。只有补池 job 的 data_readiness.analysis_ready=true，或本地池已 sufficient 且 stats/trends 有有效数值时，才继续 category_benchmark、candidate_pool_stats、top_asin_drilldown 和强结论；如果 status=completed 但 data_readiness.readiness_status=history_hydration_pending 或 serving_sync_pending，要说明“ASIN 已补入但分析特征未就绪”，建议等待 hydrate/serving sync 或只给待验证框架，不要把空 stats 当成市场事实。
   - 选品验证路径是自适应的，不是固定的“stats→trends→benchmark→drilldown”流水线，也没有“必须全部通过才算完整”的关卡。某一个维度数据不足，只代表该维度暂时待校准，不要据此宣布“当前选品验证路径还不完整”，更不要把“换关键词”当成唯一收尾。
   - 具体到 category_benchmark：当返回 benchmark_is_precise=false，或 pool_representativeness 偏低、uncategorized_asin_count 较多（锚点只代表候选池里少数 ASIN、多数 ASIN 还没解析出本地 L3 类目）时，这只是“类目基准”这一个维度的证据边界。应照常给出 candidate_pool_stats 盘面、candidate_pool_slice 切片自我对标、top_asin_drilldown 头部下钻、forecast 等其它维度能支撑的结论，只把基准维度标注为“待校准/仅供参考”。
   - 下一步建议要按实际成因从多条路径里自由选择，而不是套模板：补池/类目特征未就绪→建议等待 hydrate/serving sync 后重查；锚点代表性低但池子本身相关→用候选池内部切片和头部下钻做自我对标；召回明显混入无关品→建议用更精准的类目或关键词重建候选池；确属长尾→说明并转向可用替代信号。换关键词只是其中一个可选项，不是默认答复。
15. 只有当用户还没有给出明确商品主题/关键词/ASIN、在问“找机会/发现机会/某大类下有哪些细分方向/不知道分析什么”时，才调用 opportunity_discovery 输出机会卡片；机会卡片是继续分析入口，按机会编号深入分析时，沿用 opportunities_for_llm 中该编号的 next_action.request 继续调用 resolve_candidates，并以返回的 rank/title/category_path 作为上下文。若用户已经给出明确主题（例如“评估 car vacuum 在 Temu 美国站的机会”“分析 humidifier 在 Amazon US 是否值得做”），不要调用 opportunity_discovery，即使用户句子里出现“机会”二字，也应走商品方向排雷：先 resolve_candidates 拿到候选池，再按当前问题和已返回数据自由编排 candidate_pool_stats / candidate_pool_trends / category_benchmark / top_asin_drilldown 等工具（不必固定顺序、也不必全部调用），并按需要补充 search_knowledge_base 或 web_search。机会发现最终答复必须以逐机会卡片作为主答案；当用户要求 topN/机会卡片/逐卡分析时，最终答复结构固定为：① 一句话总览（市场/平台/返回机会数）；② 一张精简排名表（Markdown 表格，表头 `| 排名 | 机会主题 | 类目路径 | 机会得分 |`，行数等于用户请求数量，类目路径过长可省中间层但保留 leaf）；③ 然后用 `### 机会 N：<名称>` 模板逐卡展开，每张卡片固定包含"机会理由 / 关键证据 / 风险或证据边界 / 下一步验证"，只展示用户请求数量，不得只返回工具表格、也不得只给逐卡解说而省略排名表。排名表中的"机会主题"列和每张卡片标题都必须使用『中文翻译（English 原文）』双语形式（例如『真空保温杯（Tumblers）』『车窗遮阳板（Windshield Sunshades）』），中文翻译需准确反映品类含义，不可省略；原文本身是中文或专有名词则保留原文不加括号。payload.opportunity_cards_text 中的总览表、字段解释和公式明细只能作为证据来源参考，不得替代主答案；不要丢列、改数值或补未返回的数值；同时遵守 payload.llm_summary_guidance/display_rules：保留同名主题隐藏提示；有 personalized_opportunity_score 时保留个性化分或说明排序口径；趋势展示优先使用 trend_momentum_display/trend_signal_status，不能把趋势缺失或近期为 0 简化成普通 -100%；next_action.requires_category_resolve=true 时必须提醒先 category_resolve 再做类目召回。
16. 当用户明确询问销量预测、未来增长或“为什么模型看好/看空”时，优先调用 product_forecast_explain；不要把 candidate_pool_weak_forecast 的弱信号包装成正式模型预测。
17. 不要把“用户问法”写成固定流程；按 tool_contract.capability 选择能回答问题的工具，按 evidence_contract 区分 tool_fact、derived_metric、default_assumption、hypothesis。涉及启动资金、盈亏平衡、单件利润、预算周期等计算时，调用 launch_budget_calculator，让工具产出公式和数值；最终答复可以自由组织，但必须把明确事实、计算结果、默认假设和商业判断分开。
18. 当用户询问品牌内 top ASIN、材质细分 top ASIN、价格区间细分 top ASIN、评分/评论数量分布时，优先调用 candidate_pool_slice（价格区间用 price_min/price_max 参数，按 ASIN 最新成交价过滤）。禁止编造当前数据源不存在的指标：评论文本关键词/差评原因/评论质量、Amazon 关键词月搜索量等都不要凭空给数值，也不要用 Google Trends / 反推伪装成月搜索量。不要主动声明“工具限制/能力缺口”，也不要罗列缺哪些工具或 provider；只有当用户明确点名要这类数据时，才用一句话说明它不在当前数据范围内，并自然转向可用的评分/评论数量分布、销量、BSR、趋势指数等替代信号，其余情况完全不要提及。
19. 不要默认把自己描述为泛选品聊天助手、全流程替代型选品工具或低价完整报告工具；如果用户问你是什么，回答为“商品方向排雷、Keepa 数据中文解读和新手选品体检”。
20. 工具结果是经过内部精简/截断后的中间产物。其中出现的 `compaction_note`、`original_chars`、`result_format`、`result_digest`、`<internal_only…>`、`…[omitted N chars]…`、`...(truncated)`、`[结果已截断]` 等都是内部格式标记，绝对不能复述给用户，也不能据此对用户说“数据因压缩/缓存/截断未能展示”“缓存结果未能完整返回”“完整数据被截断”之类的话。如果某个具体数值在当前结果里确实没有，就直接省略该点、或改用其它已返回字段，或在必要时重新调用对应工具，而不是向用户描述内部压缩/缓存状态。用户应当只看到结论与证据，而不是工具结果的存储/压缩细节。

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
- category_resolve: 解析商品类目名称或完整类目路径，返回 category_id 与本地覆盖度
- expand_candidates: 创建 Keepa 补池任务，不在请求路径直接消耗 token
- candidate_expansion_status: 查询 Keepa 补池任务状态、token 等待状态，以及 data_readiness 分析数据就绪度
- opportunity_discovery: 发现空白机会或大类细分机会；仅用于用户尚未给出明确商品主题/关键词/ASIN 的场景
- opportunity_discovery_job: 按机会发现 job_id 回取完整机会卡片和结构化证据
- candidate_pool_stats: 候选池描述统计，优先使用 resolve_candidates 返回的 candidate_pool_id
- candidate_pool_slice: 候选池品牌/标题/材质/价格区间切片，返回切片 top ASIN 和评分/评论/销量/价格分布
- candidate_pool_trends: 候选池趋势诊断
- candidate_pool_weak_forecast: 弱信号预测标记
- product_forecast_explain: 商品销量模型预测与自动解释，调用正式 Theme API forecast explainability 路由
- launch_budget_calculator: 启动资金、单件经济模型、盈亏平衡与多场景预算计算
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

HELP_SYSTEM_PROMPT = """你是 XiaMimate 的客服帮助助手，专门处理 /help 模式问题。

工作规则：
1. 必须先调用 customer_help_search 工具检索客服知识库，再基于检索结果回答；不要直接依赖自身记忆回答。
2. 只允许使用 customer_help_search 这一个工具，不要调用别的工具。
3. 不要把检索原文整段照抄给用户，不要输出文件名、相关度、分块编号，除非用户明确要求来源。
4. 回答结构优先是：直接回答用户问题；如果合适，再补 1 到 3 条下一步建议；只有在知识里确实有现成示例时，才精选最相关的 1 到 3 条可复制示例。
5. 多意图问题要合并整理，去重后输出，不要把不同分块的重复内容机械堆叠。
6. 如果知识不足以支持回答，要明确说“当前客服知识库里没有足够信息”，并建议用户换一个更具体的问法。
7. 保持客服口吻，简洁、明确、可执行，不要展示内部检索过程。
"""

WEB_SEARCH_ANALYSIS_SYSTEM_PROMPT = """你是 XiaMimate 的 /web 联网情报分析助手。

工作规则：
1. 用户不需要原始搜索结果列表；你必须基于给定的 Tavily 外部检索证据，直接回答用户问题。
2. 不要堆砌 URL，不要逐条复述搜索结果。只在最后的“参考来源”里列出 3 到 5 个关键来源标题和域名。
3. 对平台政策、卖家影响、市场动态类问题，优先输出：结论摘要、变化/事件分组、卖家影响排序、建议动作、证据边界。
4. 如果证据之间相关性弱、时间不满足用户窗口、或没有官方来源，必须明确写在证据边界里。
5. 只能使用输入中的外部检索证据和用户问题做分析，不要编造未出现的事实。
6. 用中文回答，结论前置，措辞专业但不要写成搜索引擎结果页。
"""

TOOL_RESULT_TEMPLATE = """以下是工具执行结果，请基于这些结果继续回答用户原问题。

工具名: {tool_name}
参数: {arguments}
结果:
{result}

如果信息已经足够，请直接给出最终答案。
如果仍需继续调用工具，只调用真正必要的工具，不要重复调用相同工具。"""

AGENT_SYNTHESIS_SYSTEM_PROMPT = """你是 XiaMimate 的 Answer Synthesizer。

职责：
1. 严格基于用户问题、planner 摘要和已返回的工具结果作答，不得再调用工具。
2. 结论前置，避免冗长铺垫。
3. 基础知识或新手问题优先直接、清楚、专业地回答；不要硬套选品分析框架。
4. 涉及工具证据时，区分工具事实、推理判断和证据边界。
5. 若工具证据不足，明确说明还缺什么，不要假装结论已被验证。
6. 不要输出内部 JSON、tool_call、planner 字段或控制标记。
7. 如果上下文包含 answer_contract，必须按其中 requested_count、answer_shape、must_include 和 must_not_include 组织最终答复。
8. 如果上下文包含 followup_actionability_policy，报告尾部追问只保留当前数据可支撑的问题；禁止编造当前数据源不存在的指标（如评论文本关键词、Amazon 月搜索量），也不要主动罗列“工具限制/能力缺口”或点名缺哪个 provider。
9. 当 answer_contract.entity_type=opportunity_card 时，最终答复必须采用以下结构作为主答案，不要只输出排名表：
   - 先给一段一句话总览（市场/平台/返回机会数）；
   - 紧接一张精简排名表（Markdown 表格），表头固定为 `| 排名 | 机会主题 | 类目路径 | 机会得分 |`，行数等于用户请求数量；机会主题列必须使用「中文翻译（English 原文）」双语格式；类目路径如果过长可省略中间层，但保留 leaf 类目；缺数据时填 `当前工具未返回该细节`；
   - 然后再用 `### 机会 N：<名称>` 模板逐卡展开，其下依次给出 `机会理由`、`关键证据`、`风险/证据边界`、`下一步验证`。
   `<名称>` 同样使用「中文翻译（English 原文）」双语格式（示例：「真空保温杯（Tumblers）」「车窗遮阳板（Windshield Sunshades）」「手机壳套装（Case & Cover Bundles）」）；中文翻译需准确反映品类含义，不可省略中文；如果原文本身已是中文或属于专有名词、无对应中文（如 ASIN、品牌名），则保留原文不加括号。没有对应工具字段时写"当前工具未返回该细节"，不要编造。
10. 当任一工具结果包含 provider_status=provider_required 或 missing_capability 时，必须显式说明缺失的 provider，并复述该结果中 available_alternatives 至少一条作为下一步替代验证路径，不要把 provider_required 包装成普通结论。
11. 上下文里的工具结果是经过内部精简/截断后的中间产物。`compaction_note`、`original_chars`、`result_format`、`result_digest`、`<internal_only…>`、`…[omitted N chars]…`、`...(truncated)`、`[结果已截断]` 等都是内部格式标记，绝对不能出现在答复里，也不能据此对用户说“数据因压缩/缓存/截断未能展示”“缓存结果未能完整返回”“完整数据在压缩后被截断”之类的话。某个数值若当前结果里确实没有，就省略该点或改用其它已返回字段，不要描述内部存储/压缩状态；用户只应看到结论与证据。
"""

TOOL_LAYER_REGISTRY = agent_harness.TOOL_LAYER_REGISTRY
SCENE_TOOL_POLICY = agent_harness.SCENE_TOOL_POLICY
ALLOWED_AGENT_TOOLS = agent_harness.ALLOWED_AGENT_TOOLS

COMMAND_TO_MODE = {
    "/agent": "agent",
    "/help": "help",
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

REPORT_GATE_REFUND_REASONS = {
    "input_guard_failed",
    "candidate_pool_quality_insufficient",
    "core_evidence_guard_failed",
    "formal_report_not_generated",
}

REPORT_REFUND_VISIBILITY_NOTE = (
    "退款流水可在 账户管理 -> 完整账单 查看；需要单独核对时，可切换到“退款”筛选，"
    "对应记录会展示退款原因、变动后余额和积分来源。"
)

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
    "candidate_pool_slice": "product_api_call",
    "candidate_pool_trends": "product_api_call",
    "candidate_pool_weak_forecast": "product_api_call",
    "top_asin_drilldown": "product_api_call",
    "asin_history_timeseries": "product_api_call",
    "asin_review_insights": "product_api_call",
    "amazon_keyword_demand": "product_api_call",
    "category_benchmark": "product_api_call",
    "keepa_asin_lookup": "product_api_call",
}

WORKFLOW_SUGGESTION_PROMPTS = [
    {
        "title": ["/help 示例", "新手卖家提示词"],
        "content": "/help 新手卖家第一次使用虾米选品，给我 5 条可以直接复制的提示词，并说明分别适合什么场景。",
    },
    {
        "title": ["/tool 示例", "humidifier 商品方向排雷"],
        "content": "/tool 请先给 humidifier 在 Amazon 美国站做商品方向排雷：解析候选池后，优先判断样本覆盖、竞争强度、价格带、评论壁垒和是否需要补池。",
    },
    {
        "title": ["/report quick", "pet hair remover 红黄绿判断"],
        "content": "/report quick 请给 pet hair remover 在 Amazon 美国站做新手排雷体检，先输出绿灯/黄灯/红灯判断，再列出 3 个最关键风险指标。",
    },
    {
        "title": ["/web 示例", "带电小家电合规排雷"],
        "content": "/web 请搜索最近 30 天 Amazon 美国站带电小家电上架、认证和召回相关风险，并按新手卖家的排雷优先级排序。",
    },
]

AGENT_MODEL_DESCRIPTION = (
    "虾米选品（XiaMimate）主打“先排雷，再选品”。输入商品词或 ASIN 后，"
    "系统会结合 Keepa 商品数据、趋势信号、平台知识、联网查询和商品预测算法，"
    "先帮用户看竞争强度、趋势变化、价格带、评论壁垒和风险边界，低成本判断这个商品方向是否值得继续看。"
)

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
        AGENT_MAX_TOOL_ROUNDS: int = 12
        AGENT_TRACE_SINK_PATH: str = ""
        AGENT_PLANNER_MODEL: str = ""
        AGENT_PLANNER_HEARTBEAT_SECONDS: int = 5
        AGENT_PLANNER_OBSERVATION_LIMIT: int = 4
        AGENT_PLANNER_BASE_URL: str = ""
        AGENT_PLANNER_API_KEY: str = ""
        AGENT_TITLE_TRANSLATOR_MODEL: str = ""
        AGENT_TITLE_TRANSLATION_TIMEOUT: int = 20
        AGENT_TITLE_TRANSLATOR_BASE_URL: str = ""
        AGENT_TITLE_TRANSLATOR_API_KEY: str = ""
        # 翻译副线优先用非推理模式：DeepSeek v4 系列在 extra_body/请求体里通过
        # {"thinking": {"type": "disabled"}} 关闭思维链，避免推理模型把 token 全放进
        # reasoning_content 而 content 为空导致译文解析失败。
        AGENT_TITLE_TRANSLATOR_DISABLE_THINKING: bool = True
        AGENT_OPPORTUNITY_BYPASS_SYNTHESIS: bool = True
        HELP_FAST_TOP_K: int = 4
        HELP_CACHE_TTL_SECONDS: int = 900
        HELP_CACHE_MAX_ENTRIES: int = 128
        XIAMIMATE_MODEL_PREFIX: str = "xiamimate"

    def __init__(self):
        self.type = "manifold"
        self.id = os.getenv("XIAMIMATE_MODEL_PREFIX", "xiamimate")
        self.name = "XiaMimate: "
        self._last_chat_id = None
        self.agent_harness = agent_harness.AgentHarness()
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
                "AGENT_MAX_TOOL_ROUNDS": int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "12")),
                "AGENT_TRACE_SINK_PATH": os.getenv("AGENT_TRACE_SINK_PATH", ""),
                "AGENT_PLANNER_MODEL": os.getenv("AGENT_PLANNER_MODEL", ""),
                "AGENT_PLANNER_HEARTBEAT_SECONDS": int(os.getenv("AGENT_PLANNER_HEARTBEAT_SECONDS", "5") or 0),
                "AGENT_PLANNER_OBSERVATION_LIMIT": int(os.getenv("AGENT_PLANNER_OBSERVATION_LIMIT", "4") or 4),
                "AGENT_PLANNER_BASE_URL": os.getenv("AGENT_PLANNER_BASE_URL", ""),
                "AGENT_PLANNER_API_KEY": os.getenv("AGENT_PLANNER_API_KEY", ""),
                "AGENT_TITLE_TRANSLATOR_MODEL": os.getenv("AGENT_TITLE_TRANSLATOR_MODEL", ""),
                "AGENT_TITLE_TRANSLATION_TIMEOUT": int(os.getenv("AGENT_TITLE_TRANSLATION_TIMEOUT", "20") or 20),
                "AGENT_TITLE_TRANSLATOR_BASE_URL": os.getenv("AGENT_TITLE_TRANSLATOR_BASE_URL", ""),
                "AGENT_TITLE_TRANSLATOR_API_KEY": os.getenv("AGENT_TITLE_TRANSLATOR_API_KEY", ""),
                "AGENT_TITLE_TRANSLATOR_DISABLE_THINKING": (os.getenv("AGENT_TITLE_TRANSLATOR_DISABLE_THINKING", "true").strip().lower() not in {"0", "false", "no", "off", ""}),
                "AGENT_OPPORTUNITY_BYPASS_SYNTHESIS": (os.getenv("AGENT_OPPORTUNITY_BYPASS_SYNTHESIS", "true").strip().lower() not in {"0", "false", "no", "off", ""}),
                "HELP_FAST_TOP_K": int(os.getenv("HELP_FAST_TOP_K", "4")),
                "HELP_CACHE_TTL_SECONDS": int(os.getenv("HELP_CACHE_TTL_SECONDS", "900")),
                "HELP_CACHE_MAX_ENTRIES": int(os.getenv("HELP_CACHE_MAX_ENTRIES", "128")),
                "XIAMIMATE_MODEL_PREFIX": os.getenv("XIAMIMATE_MODEL_PREFIX", "xiamimate"),
            }
        )
        self._help_answer_cache: Dict[str, dict] = {}
        self._opportunity_title_zh_cache: Dict[str, str] = {}
        self.pipelines = self._build_agent_pipelines()
    def _ensure_agent_harness(self, body: dict):
        # mode_router 的 inlet 会把 metadata.chat_id 复制到 body['chat_id']，
        # 这里依然兼容直接出现在 body 根的情况。
        metadata = body.get("metadata") if isinstance(body, dict) else None
        chat_id = str(body.get("chat_id") or "").strip()
        if not chat_id and isinstance(metadata, dict):
            chat_id = str(metadata.get("chat_id") or "").strip()
        chat_id = chat_id or None
        if chat_id != self._last_chat_id or self.agent_harness is None:
            try:
                self.agent_harness = agent_harness.AgentHarness(chat_id=chat_id)
            except TypeError:
                # 兼容旧构造
                self.agent_harness = agent_harness.AgentHarness()
            self._last_chat_id = chat_id
        if self.agent_harness is None:
            self.agent_harness = agent_harness.AgentHarness()
        return self.agent_harness

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
            description = AGENT_MODEL_DESCRIPTION
            pipelines.append(
                {
                    "id": pipeline_id,
                    "name": "Agent · %s" % label,
                    "description": description,
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
        if effective_model_id.endswith(".agent") or effective_model_id == "agent":
            return self._default_agent_profile()
        for profile in self._configured_agent_profiles():
            explicit_pipeline_id = "agent-%s" % profile
            if effective_model_id.endswith(".%s" % explicit_pipeline_id) or effective_model_id == explicit_pipeline_id:
                return profile

        return self._default_agent_profile()

    def _response_model_for_profile(self, profile: str, requested_model_id: str) -> str:
        return "%s.%s" % (self.id, self._pipeline_id_for_profile(profile))

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
        if self._is_openwebui_internal_task_request(body):
            return self._run_openwebui_internal_task(body=body, model=response_model, model_name=agent_model_name)

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
        if mode == "help":
            return self._run_help(
                query=normalized_user_message,
                body=body,
                model=response_model,
                model_name=agent_model_name,
            )
        if mode == "report":
            return self._run_report(query=normalized_user_message, body=body, model=response_model)
        if mode == "web":
            return self._run_web_search(query=normalized_user_message, body=body, model=response_model, model_name=agent_model_name)
        if mode == "tool" and self._explicit_tool_name_from_text(normalized_user_message) == "web_search":
            return self._run_web_search(
                query=self._web_search_query_from_tool_alias(normalized_user_message),
                body=body,
                model=response_model,
                model_name=agent_model_name,
            )
        if mode in {"agent", "tool"}:
            self._ensure_agent_harness(body)
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

    def _run_help(self, query: str, body: dict, model: str, model_name: str) -> Union[dict, Iterator[bytes]]:
        normalized_query = (query or "").strip()
        if not normalized_query:
            content = (
                "请在 /help 后面写出你要查询的帮助主题，例如：\n"
                "/help 新手卖家第一次使用虾米选品，给我 5 条可以直接复制的提示词。\n"
                "/help /report quick、standard、deep、research 分别适合什么场景？\n"
                "/help 更省积分的提问方式有哪些？"
            )
        else:
            if body.get("stream"):
                return self._run_help_fast_stream(
                    query=normalized_query,
                    body=body,
                    model=model,
                    model_name=model_name,
                )

            try:
                answer = self._run_help_fast(query=normalized_query, body=body, model_name=model_name)
                content = str(answer or "").strip() or "当前客服知识库里没有足够信息，请换一个更具体的问法再试。"
            except Exception as exc:
                content = "客服知识库检索失败：%s" % str(exc)[:2000]

        if body.get("stream"):
            return self._stream_text_response(content=content, model=model)
        return self._chat_response(content=content, model=model)

    def _run_report(self, query: str, body: dict, model: str) -> Union[dict, Iterator[bytes]]:
        profile, normalized_query = self._parse_report_profile(query)
        resolved_query = self._resolve_report_query_from_context(normalized_query, body.get("messages") or [])
        if resolved_query is None:
            message = self._report_opportunity_reference_guidance(normalized_query, profile=profile)
            if body.get("stream"):
                return self._stream_text_response(content=message, model=model)
            return self._chat_response(content=message, model=model)
        return self._run_report_profile(
            query=resolved_query,
            body=body,
            model=model,
            profile=profile,
            mode_tag="report",
            guidance=(
                "请在 /report 后直接写出调研需求，可选档位为 quick / standard / deep / research，例如：\n"
                "/report standard 帮我调研一下宠物自动喂食器在 TikTok 美国市场的前景"
            ),
        )

    def _run_help_fast(self, query: str, body: dict, model_name: str) -> str:
        cached_answer = self._get_cached_help_answer(query=query, model_name=model_name)
        if cached_answer:
            return cached_answer

        curated_answer = self._curated_newbie_prompt_help_answer(query=query)
        if curated_answer:
            self._store_cached_help_answer(query=query, model_name=model_name, answer=curated_answer)
            return curated_answer

        knowledge_result = self._retrieve_customer_help_knowledge(query=query)
        answer = self._synthesize_help_answer(
            query=query,
            knowledge_result=knowledge_result,
            body=body,
            model_name=model_name,
        )
        self._store_cached_help_answer(query=query, model_name=model_name, answer=answer)
        return answer

    def _run_help_fast_stream(self, query: str, body: dict, model: str, model_name: str) -> Iterator[bytes]:
        response_id = "%s-%s" % (model, uuid.uuid4())
        created = int(time.time())
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
            cached_answer = self._get_cached_help_answer(query=query, model_name=model_name)
            if cached_answer:
                for chunk in self._split_text(cached_answer):
                    yield emit_text_chunk(chunk)
                yield self._stream_stop_chunk(response_id=response_id, created=created, model=model)
                yield b"data: [DONE]\n\n"
                return

            curated_answer = self._curated_newbie_prompt_help_answer(query=query)
            if curated_answer:
                self._store_cached_help_answer(query=query, model_name=model_name, answer=curated_answer)
                for chunk in self._split_text(curated_answer):
                    yield emit_text_chunk(chunk)
                yield self._stream_stop_chunk(response_id=response_id, created=created, model=model)
                yield b"data: [DONE]\n\n"
                return

            for chunk in emit_reasoning_chunks(self._format_agent_progress("正在识别帮助主题", percent=10)):
                yield chunk

            for chunk in emit_reasoning_chunks(self._format_agent_progress("正在检索客服知识库", percent=35)):
                yield chunk
            knowledge_result = self._retrieve_customer_help_knowledge(query=query)

            for chunk in emit_reasoning_chunks(self._format_agent_progress("知识库结果已就绪，正在整理最终答复", percent=80)):
                yield chunk
            answer = self._synthesize_help_answer(
                query=query,
                knowledge_result=knowledge_result,
                body=body,
                model_name=model_name,
            )
            self._store_cached_help_answer(query=query, model_name=model_name, answer=answer)

            for chunk in emit_reasoning_chunks(self._format_agent_progress("最终答复已生成", percent=100)):
                yield chunk
            close_chunk = close_reasoning_chunk()
            if close_chunk is not None:
                yield close_chunk
            for chunk in self._split_text(answer):
                yield emit_text_chunk(chunk)
        except Exception as exc:
            close_chunk = close_reasoning_chunk()
            if close_chunk is not None:
                yield close_chunk
            yield emit_text_chunk("客服知识库检索失败：%s" % str(exc)[:2000])

        yield self._stream_stop_chunk(response_id=response_id, created=created, model=model)
        yield b"data: [DONE]\n\n"

    def _retrieve_customer_help_knowledge(self, query: str) -> str:
        if self.agent_tools is None or not hasattr(self.agent_tools, "customer_help_search"):
            raise RuntimeError("customer_help_search 工具未加载")
        return str(self.agent_tools.customer_help_search(query=query, top_k=self._help_fast_top_k()) or "").strip()

    def _curated_newbie_prompt_help_answer(self, query: str) -> str:
        normalized_query = re.sub(r"\s+", " ", str(query or "").strip().lower())
        if not normalized_query:
            return ""
        if "新手" not in normalized_query and "第一次" not in normalized_query:
            return ""
        if "提示词" not in normalized_query and "prompt" not in normalized_query:
            return ""
        if "5" not in normalized_query and "五" not in normalized_query:
            return ""

        return """好的！以下是为新手卖家精选的 5 条可直接复制的排雷提示词，按从轻到重的使用顺序排列：

### 1. 商品方向排雷（/tool）

```
/tool 请先给 humidifier 在 Amazon 美国站做商品方向排雷：解析候选池后，优先判断样本覆盖、竞争强度、价格带、评论壁垒和是否需要补池。
```

适合场景：你心里已经有一个大致品类方向（比如 humidifier），但还不确定这个方向有没有明显坑。这条提示词会先做低成本体检，而不是一上来跑完整长报告。

### 2. 红黄绿快速判断（/report quick）

```
/report quick 请给 pet hair remover 在 Amazon 美国站做新手排雷体检，先输出绿灯/黄灯/红灯判断，再列出 3 个最关键风险指标。
```

适合场景：你有好几个候选品类，想快速筛掉不适合新手继续投入的方向。quick 报告要先给结论，再告诉你最大的不确定性在哪里。

### 3. ASIN 体检（/report standard）

```
/report standard 请体检 ASIN B0GG8YFVV1 在 Amazon 美国站是否值得新手跟进，重点看销量/价格/BSR/评论变化、头部压力和主要风险。
```

适合场景：你已经看到一个竞品或样品，想判断能不能继续研究。standard 更适合围绕明确 ASIN 或明确商品词做体检，而不是泛泛找机会。

### 4. 合规风险排雷（/web）

```
/web 请搜索最近 30 天 Amazon 美国站带电小家电上架、认证和召回相关风险，并按新手卖家的排雷优先级排序。
```

适合场景：你的商品可能涉及认证、侵权、召回、平台限制或运输风险。先用 /web 查最新外部信息，再决定是否继续测款。

### 5. 查询更多提示词模板（/help）

```
/help 我是新手卖家，正在做 Amazon 美国站选品，给我推荐适合我当前阶段的提示词。
```

适合场景：上面的 4 条用完之后，你还想要更多针对自己情况的提示词。直接告诉 /help 你的身份和阶段，系统会从知识库里匹配最相关的示例，不用自己从零想 prompt。

使用建议：新手建议按 1 -> 2 -> 3 的顺序推进，先用 /tool 做商品方向排雷，再用 /report quick 拿红黄绿初判，方向或 ASIN 明确后再上 /report standard 体检；/web 则用来补认证、召回、政策和站外风险。"""

    def _synthesize_help_answer(self, query: str, knowledge_result: str, body: dict, model_name: str) -> str:
        if not knowledge_result:
            return "当前客服知识库里没有足够信息，请换一个更具体的问法再试。"

        if "未找到与" in knowledge_result and "客服知识库内容" in knowledge_result:
            return "当前客服知识库里没有找到足够相关的内容。请换一个更具体的问法，例如只问价格规则、/report 计费、提示词示例或新手上手步骤。"

        synthesis_messages = [
            {"role": "user", "content": query},
            {
                "role": "user",
                "content": (
                    "下面是 customer_help_search 检索到的客服知识库结果。不要再调用任何工具，"
                    "只能基于这些结果直接回答用户问题。\n\n"
                    "%s"
                )
                % knowledge_result,
            },
        ]
        payload = self._prepare_agent_payload(messages=synthesis_messages, body=body, mode="help", model_name=model_name)
        payload["stream"] = False
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        response = self._post_agent_payload(payload, model_name=model_name)
        content = self._clean_agent_content(self._extract_assistant_content(response), model_name=model_name)
        if content:
            return content
        return self._fallback_help_answer_from_tool_observations([{"llm_result": knowledge_result}])

    def _help_fast_top_k(self) -> int:
        try:
            return max(1, min(8, int(self.valves.HELP_FAST_TOP_K)))
        except Exception:
            return 4

    def _help_cache_ttl_seconds(self) -> int:
        try:
            return max(30, min(86400, int(self.valves.HELP_CACHE_TTL_SECONDS)))
        except Exception:
            return 900

    def _help_cache_max_entries(self) -> int:
        try:
            return max(8, min(1024, int(self.valves.HELP_CACHE_MAX_ENTRIES)))
        except Exception:
            return 128

    def _normalize_help_cache_key(self, query: str, model_name: str) -> str:
        normalized_query = re.sub(r"\s+", " ", str(query or "").strip().lower())
        normalized_model_name = str(model_name or "").strip().lower()
        return "%s::%s" % (normalized_model_name, normalized_query)

    def _prune_help_answer_cache(self) -> None:
        now = time.time()
        ttl_seconds = self._help_cache_ttl_seconds()
        self._help_answer_cache = {
            key: value
            for key, value in self._help_answer_cache.items()
            if now - float(value.get("created_at") or 0) <= ttl_seconds
        }
        max_entries = self._help_cache_max_entries()
        if len(self._help_answer_cache) <= max_entries:
            return
        sorted_items = sorted(
            self._help_answer_cache.items(),
            key=lambda item: float((item[1] or {}).get("created_at") or 0),
            reverse=True,
        )
        self._help_answer_cache = dict(sorted_items[:max_entries])

    def _get_cached_help_answer(self, query: str, model_name: str) -> str:
        self._prune_help_answer_cache()
        cache_key = self._normalize_help_cache_key(query=query, model_name=model_name)
        cached = self._help_answer_cache.get(cache_key) or {}
        return str(cached.get("answer") or "").strip()

    def _store_cached_help_answer(self, query: str, model_name: str, answer: str) -> None:
        normalized_answer = str(answer or "").strip()
        if not normalized_answer:
            return
        cache_key = self._normalize_help_cache_key(query=query, model_name=model_name)
        self._help_answer_cache[cache_key] = {
            "answer": normalized_answer,
            "created_at": time.time(),
        }
        self._prune_help_answer_cache()

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
        if not self._report_profile_is_available(profile):
            message = self._report_profile_unavailable_message(profile)
            if body.get("stream"):
                return self._stream_text_response(content=message, model=model)
            return self._chat_response(content=message, model=model)

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

    def _report_profile_is_available(self, profile: str) -> bool:
        normalized = str(profile or "").strip().lower()
        if normalized != "research":
            return True
        return bool((os.getenv("DIFY_REPORT_RESEARCH_APP_API_KEY") or "").strip())

    def _report_profile_unavailable_message(self, profile: str) -> str:
        normalized = str(profile or "").strip().lower()
        if normalized == "research":
            return "当前 /report research 功能待开发，暂未上线。本次不会扣除积分。"
        return "当前所选报告档位暂不可用。本次不会扣除积分。"

    def _run_web_search(self, query: str, body: dict, model: str, model_name: str = "") -> Union[dict, Iterator[bytes]]:
        query = (query or "").strip()
        guidance = (
            "请在 /web 后直接写出要联网搜索的问题，例如：\n"
            "/web 帮我搜索并总结 2026 年 TikTok Shop 美国站最新入驻政策变化"
        )
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
            web_charge = self._charge_billing_event(
                billing_context=billing_context,
                event_type="web_search",
                description="网络搜索",
                meta={"mode": "web", "provider": "tavily", "stream": bool(body.get("stream")), "query_preview": query[:200]},
            )
        except RuntimeError as exc:
            message = self._error_text(str(exc))
            if body.get("stream"):
                return self._stream_text_response(content=message, model=model)
            return self._chat_response(content=message, model=model)

        if body.get("stream"):
            return self._run_tavily_web_search_stream(
                query=query,
                body=body,
                model=model,
                model_name=model_name,
                billing_context=billing_context,
                web_charge=web_charge,
            )

        try:
            response = self._run_tavily_web_search_request(query=query, body=body, billing_context=billing_context)
            answer = self._run_tavily_web_search_analysis(query=query, search_response=response, body=body, model_name=model_name)
        except RuntimeError as exc:
            self._refund_billing_event(
                billing_context=billing_context,
                charge=web_charge,
                description="网络搜索失败，已退款",
                meta={"mode": "web", "provider": "tavily", "error": str(exc)[:500]},
            )
            return self._chat_response(content=self._error_text(str(exc)), model=model)

        return self._chat_response(content=answer, model=model)

    def _run_tavily_web_search_request(self, query: str, body: dict, billing_context: dict) -> dict:
        return self._chat_backend_request(
            method="POST",
            path="/internal/provider/web-search/tavily",
            body={
                "query": query,
                "user": billing_context["user_id"],
                "search_mode": self._body_context_value(body, "search_mode") or "auto",
                "target_platform": self._body_context_value(body, "target_platform"),
                "target_market": self._body_context_value(body, "target_market") or self._body_context_value(body, "marketplace"),
                "max_results": 5,
                "include_answer": True,
                "time_range": self._body_context_value(body, "time_range") or "month",
            },
            internal=True,
            timeout=self.valves.DIFY_REQUEST_TIMEOUT,
        )

    def _run_tavily_web_search_analysis(self, query: str, search_response: dict, body: dict, model_name: str) -> str:
        evidence_text = self._format_tavily_web_search_evidence(search_response)
        if not evidence_text:
            return "没有拿到可用于分析的外部搜索证据，请换一个更具体的问题再试。"

        provider = self._get_provider(model_name)
        payload = provider.filter_payload(body)
        payload["model"] = model_name or self._model_name_for_profile(self._default_agent_profile())
        payload["stream"] = False
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        payload["messages"] = [
            {"role": "system", "content": WEB_SEARCH_ANALYSIS_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "当前日期: %s\n\n"
                    "用户问题:\n%s\n\n"
                    "Tavily 外部检索证据:\n%s\n\n"
                    "请不要输出原始搜索结果列表；请综合分析并按卖家影响排序。"
                )
                % (datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"), query, evidence_text),
            },
        ]
        user_value = payload.get("user")
        if isinstance(user_value, dict):
            payload["user"] = self._user_id(body)

        response = self._post_agent_payload(payload, model_name=model_name)
        answer = self._clean_agent_content(self._extract_assistant_content(response), model_name=model_name)
        if answer:
            return answer
        return self._format_tavily_web_search_answer(search_response)

    def _format_tavily_web_search_evidence(self, response: dict) -> str:
        if not isinstance(response, dict):
            return str(response or "").strip()
        compact = {
            "provider": response.get("provider"),
            "capability": response.get("capability"),
            "query": response.get("query"),
            "query_plan": response.get("query_plan") or {},
            "answer": response.get("answer"),
            "result_text": response.get("result_text"),
            "source_meta": response.get("source_meta") or {},
            "degradation_status": response.get("degradation_status"),
            "degradation_reason": response.get("degradation_reason"),
            "results": response.get("results") or [],
        }
        return json.dumps(
            self._compact_json_value(compact, max_depth=5, max_items=10, max_scalar_items=50, max_string=1200),
            ensure_ascii=False,
            indent=2,
        )

    def _format_tavily_web_search_answer(self, response: dict) -> str:
        if not isinstance(response, dict):
            return str(response or "")
        result_text = str(response.get("result_text") or "").strip()
        if result_text:
            return result_text
        answer = str(response.get("answer") or "").strip()
        if answer:
            return answer
        return json.dumps(response, ensure_ascii=False, indent=2)

    def _run_tavily_web_search_stream(
        self,
        query: str,
        body: dict,
        model: str,
        model_name: str,
        billing_context: dict,
        web_charge: dict,
    ) -> Iterator[bytes]:
        response_id = "%s-%s" % (model, uuid.uuid4())
        created = int(time.time())
        answer_started = False

        def emit_text_chunk(content: str) -> bytes:
            return self._stream_content_chunk(response_id=response_id, created=created, model=model, content=content)

        try:
            yield self._stream_reasoning_open_chunk(response_id=response_id, created=created, model=model)
            yield self._stream_reasoning_text_chunk(
                response_id=response_id,
                created=created,
                model=model,
                content=self._format_dify_progress("web", 20, "正在直连 Tavily 搜索网络信息"),
            )
            response = self._run_tavily_web_search_request(query=query, body=body, billing_context=billing_context)
            yield self._stream_reasoning_text_chunk(
                response_id=response_id,
                created=created,
                model=model,
                content=self._format_dify_progress("web", 70, "Tavily 搜索完成，正在生成分析结论"),
            )
            answer = self._run_tavily_web_search_analysis(query=query, search_response=response, body=body, model_name=model_name)
            yield self._stream_reasoning_text_chunk(
                response_id=response_id,
                created=created,
                model=model,
                content=self._format_dify_progress("web", 100, "分析结论已生成"),
            )
            yield self._stream_reasoning_close_chunk(response_id=response_id, created=created, model=model)
            answer_started = True
            for chunk in self._split_text(answer):
                yield emit_text_chunk(chunk)
        except RuntimeError as exc:
            if not answer_started:
                self._refund_billing_event(
                    billing_context=billing_context,
                    charge=web_charge,
                    description="网络搜索失败，已退款",
                    meta={"mode": "web", "provider": "tavily", "error": str(exc)[:500]},
                )
            yield self._stream_reasoning_close_chunk(response_id=response_id, created=created, model=model)
            yield emit_text_chunk("\n" + self._error_text(str(exc)))

        yield self._stream_stop_chunk(response_id=response_id, created=created, model=model)
        yield b"data: [DONE]\n\n"

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
        report_profile = str((request_payload or {}).get("profile") or "").strip()
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
                    "report_profile": report_profile,
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
                gate_refund_description="%s门控失败，已退款" % charge_description,
                report_profile=report_profile,
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
            if mode_tag == "report":
                self._refund_report_gate_failure(
                    billing_context=billing_context,
                    charge=flow_charge,
                    answer_text=answer,
                    report_profile=report_profile,
                    description="%s门控失败，已退款" % charge_description,
                    mode_tag=mode_tag,
                )
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

        agent_body = dict(body or {})
        memory_profile = self._build_agent_memory_profile_context(
            messages=messages,
            body=agent_body,
            billing_context=billing_context,
            mode=mode,
        )
        if memory_profile:
            agent_body["_xiamimate_memory_profile"] = memory_profile

        if agent_body.get("stream"):
            return self._run_agent_stream(
                messages=messages,
                body=agent_body,
                model=model,
                model_name=model_name,
                billing_context=billing_context,
                mode=mode,
            )

        try:
            answer = self._run_agent_loop(
                messages=messages,
                body=agent_body,
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
        source_messages = deepcopy(messages or [])
        answer_started = False
        used_tools = False
        reasoning_open = False
        scene = self._classify_agent_scene(source_messages, mode=mode)
        react_runner = self.agent_harness.new_react_runner(mode=mode, scene=scene)
        agent_trace = react_runner.trace
        trace_status = "finished"
        final_answer_for_grade = ""
        tool_store = react_runner.observation_store
        tool_observations: List[dict] = tool_store.observations
        tool_result_cache: Dict[Tuple[str, str], dict] = tool_store.tool_result_cache
        planner_notes: List[dict] = react_runner.planner_notes
        max_rounds = min(self._agent_max_tool_rounds(), int(self._scene_policy(scene, mode).get("max_rounds") or 1))
        react_runner.start(max_rounds=max_rounds)

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
            for chunk in emit_reasoning_chunks(self._format_agent_progress("正在分析问题", percent=10)):
                yield chunk

            for round_index in range(max_rounds):
                for chunk in emit_reasoning_chunks(self._format_agent_progress("正在规划执行路径", percent=25)):
                    yield chunk
                try:
                    planner_kwargs = dict(
                        messages=source_messages,
                        body=body,
                        model_name=model_name,
                        mode=mode,
                        scene=scene,
                        tool_observations=tool_observations,
                        remaining_rounds=max_rounds - round_index,
                    )
                    heartbeat_interval = max(0, int(self.valves.AGENT_PLANNER_HEARTBEAT_SECONDS or 0))
                    if heartbeat_interval <= 0:
                        plan = self._plan_agent_next_steps(**planner_kwargs)
                    else:
                        result_holder: dict = {}

                        def _planner_runner() -> None:
                            try:
                                result_holder["plan"] = self._plan_agent_next_steps(**planner_kwargs)
                            except BaseException as plan_exc:  # noqa: BLE001 - re-raise on main thread
                                result_holder["exc"] = plan_exc

                        planner_thread = threading.Thread(target=_planner_runner, name="xm-planner", daemon=True)
                        planner_thread.start()
                        heartbeat_start = time.monotonic()
                        next_tick = heartbeat_start + heartbeat_interval
                        while planner_thread.is_alive():
                            now = time.monotonic()
                            wait_for = max(0.05, next_tick - now)
                            planner_thread.join(timeout=wait_for)
                            if not planner_thread.is_alive():
                                break
                            elapsed = int(time.monotonic() - heartbeat_start)
                            for chunk in emit_reasoning_chunks(
                                self._format_agent_progress(f"正在规划执行路径 (已等待 {elapsed}s)", percent=25)
                            ):
                                yield chunk
                            next_tick = time.monotonic() + heartbeat_interval
                        planner_thread.join()
                        if "exc" in result_holder:
                            raise result_holder["exc"]
                        plan = result_holder.get("plan") or {}
                except RuntimeError as exc:
                    if tool_observations:
                        final_answer = self._fallback_answer_from_tool_observations(tool_observations, error=str(exc))
                        for chunk in emit_reasoning_chunks(self._format_agent_progress("模型整理失败，返回工具结果摘要", percent=100)):
                            yield chunk
                        close_chunk = close_reasoning_chunk()
                        if close_chunk is not None:
                            yield close_chunk
                        for chunk in self._split_text(final_answer):
                            answer_started = True
                            yield emit_text_chunk(chunk)
                        break
                    raise

                scene = str(plan.get("scene") or scene or "general_agent").strip() or "general_agent"
                explicit_tool_name = ""
                if not tool_observations:
                    explicit_tool_name = self._explicit_tool_name_from_text(self._extract_last_user_text(source_messages))
                    if explicit_tool_name:
                        scene = self._scene_for_explicit_tool(explicit_tool_name, scene)
                react_runner.plan_note(scene, plan)

                if not explicit_tool_name and plan.get("answer_ready") and str(plan.get("final_answer") or "").strip():
                    if mode == "agent" and not used_tools:
                        self._charge_standalone_llm_request(
                            billing_context=billing_context,
                            payload={"model": model_name, "messages": source_messages},
                            mode=mode,
                            stream=True,
                        )
                    final_answer = self._fallback_opportunity_answer_if_needed(
                        str(plan.get("final_answer") or "").strip(),
                        tool_observations,
                        answer_contract=self._answer_contract_from_messages(source_messages),
                    )
                    final_answer_for_grade = final_answer
                    react_runner.final(scene, status="planner_final")
                    for chunk in emit_reasoning_chunks(self._format_agent_progress("Planner 已确认可直接作答，正在生成最终答复", percent=100)):
                        yield chunk

                    close_chunk = close_reasoning_chunk()
                    if close_chunk is not None:
                        yield close_chunk

                    for chunk in self._split_text(final_answer):
                        answer_started = True
                        yield emit_text_chunk(chunk)
                    break

                steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
                if explicit_tool_name and not tool_observations:
                    matching_explicit_steps = [
                        step
                        for step in steps
                        if str(((step or {}).get("tool_call") or {}).get("name") or "").strip() == explicit_tool_name
                    ]
                    if matching_explicit_steps:
                        steps = [matching_explicit_steps[0]]
                    else:
                        explicit_step = self._explicit_tool_request_step(source_messages, body, scene, mode=mode)
                        if explicit_step is not None:
                            steps = [explicit_step]
                if plan.get("planner_protocol") != "native_tool_calls" and not (explicit_tool_name and explicit_tool_name != "resolve_candidates"):
                    steps = self._enforce_theme_resolve_first_step(steps, scene, source_messages, body, tool_observations)
                steps = self._repair_planner_steps_required_arguments(steps, source_messages, body)
                steps = self._filter_redundant_planner_steps(steps, scene, tool_observations)
                react_runner.validation(scene, steps)
                if not steps:
                    if not tool_observations and str(plan.get("action_type") or "").strip() in {"", "none"}:
                        final_answer = self._fallback_answer_from_tool_observations(tool_observations)
                        final_answer_for_grade = final_answer
                        for chunk in emit_reasoning_chunks(self._format_agent_progress("未生成可执行工具或可见答复，返回兜底提示", percent=100)):
                            yield chunk
                        close_chunk = close_reasoning_chunk()
                        if close_chunk is not None:
                            yield close_chunk
                        for chunk in self._split_text(final_answer):
                            answer_started = True
                            yield emit_text_chunk(chunk)
                        break
                    final_answer = self._synthesize_planner_executor_answer(
                        messages=source_messages,
                        body=body,
                        model_name=model_name,
                        planner_notes=planner_notes,
                        tool_observations=tool_observations,
                        agent_trace=agent_trace,
                    )
                    final_answer_for_grade = final_answer
                    if mode == "agent" and not used_tools:
                        self._charge_standalone_llm_request(
                            billing_context=billing_context,
                            payload={"model": model_name, "messages": source_messages},
                            mode=mode,
                            stream=True,
                        )
                    for chunk in emit_reasoning_chunks(self._format_agent_progress("无需继续执行工具，正在生成最终答复", percent=100)):
                        yield chunk
                    close_chunk = close_reasoning_chunk()
                    if close_chunk is not None:
                        yield close_chunk
                    for chunk in self._split_text(final_answer):
                        answer_started = True
                        yield emit_text_chunk(chunk)
                    break

                used_tools = True
                tool_names = ", ".join(
                    str(((step or {}).get("tool_call") or {}).get("name") or "").strip()
                    for step in steps
                    if isinstance(step, dict)
                )
                for chunk in emit_reasoning_chunks(self._format_agent_progress("Planner 已生成执行计划: %s" % tool_names, percent=40)):
                    yield chunk

                executed_any = False
                for step_index, step in enumerate(steps, start=1):
                    tool_call = self._attach_internal_tool_context((step or {}).get("tool_call") or {}, body)
                    tool_name = str(tool_call.get("name") or "").strip() or "unknown_tool"
                    cached_observation = self._cached_tool_observation_for_call(
                        tool_call=tool_call,
                        tool_result_cache=tool_result_cache,
                        tool_observations=tool_observations,
                    )
                    if cached_observation is not None:
                        executed_any = True
                        for chunk in emit_reasoning_chunks(
                            self._format_agent_progress(
                                "步骤 %d/%d：工具 %s 复用已有结果" % (step_index, len(steps), tool_name),
                                percent=65,
                            )
                        ):
                            yield chunk
                        continue

                    for chunk in emit_reasoning_chunks(
                        self._format_agent_progress(
                            "正在执行步骤 %d/%d：%s" % (step_index, len(steps), tool_name),
                            percent=55,
                        )
                    ):
                        yield chunk

                    public_tool_call = self._strip_internal_tool_context(tool_call)
                    result = self._execute_tool_call(tool_call, billing_context, truncate=False)
                    observation = self._build_tool_observation(tool_call=public_tool_call, result=result)
                    executed_any = True
                    tool_status = "失败" if self._tool_result_has_error(result) else "完成"
                    react_runner.observation(
                        scene,
                        tool_name,
                        "error" if self._tool_result_has_error(result) else "ok",
                        observation=observation,
                        cache_key=self._tool_call_cache_key(tool_call),
                    )
                    for chunk in emit_reasoning_chunks(
                        self._format_agent_progress(
                            "步骤 %d/%d：工具 %s 已%s" % (step_index, len(steps), tool_name, tool_status),
                            percent=75,
                        )
                    ):
                        yield chunk

                if executed_any:
                    for chunk in emit_reasoning_chunks(self._format_agent_progress("本轮执行完成，正在判断是否需要补强", percent=85)):
                        yield chunk

            if not answer_started:
                final_answer = self._synthesize_planner_executor_answer(
                    messages=source_messages,
                    body=body,
                    model_name=model_name,
                    planner_notes=planner_notes,
                    tool_observations=tool_observations,
                    agent_trace=agent_trace,
                    limit_reached=bool(tool_observations),
                )
                final_answer_for_grade = final_answer
                if mode == "agent" and not used_tools:
                    self._charge_standalone_llm_request(
                        billing_context=billing_context,
                        payload={"model": model_name, "messages": source_messages},
                        mode=mode,
                        stream=True,
                    )
                for chunk in emit_reasoning_chunks(self._format_agent_progress("执行轮次结束，正在基于已有证据生成最终答复", percent=100)):
                    yield chunk
                close_chunk = close_reasoning_chunk()
                if close_chunk is not None:
                    yield close_chunk
                for chunk in self._split_text(final_answer):
                    answer_started = True
                    yield emit_text_chunk(chunk)
        except RuntimeError as exc:
            trace_status = "error"
            close_chunk = close_reasoning_chunk()
            if close_chunk is not None:
                yield close_chunk
            yield emit_text_chunk("\n" + self._error_text(str(exc)))

        trace_extra = {"tool_count": len(tool_observations), "planner_note_count": len(planner_notes), "stream": True}
        grader_result = self._grade_agent_answer_for_trace(source_messages, final_answer_for_grade, tool_observations, agent_trace)
        if grader_result.get("status") != "skipped":
            trace_extra["grader_result"] = grader_result
        self._persist_agent_trace(
            agent_trace,
            status=trace_status,
            extra=trace_extra,
        )
        close_chunk = close_reasoning_chunk()
        if close_chunk is not None:
            yield close_chunk
        yield self._stream_stop_chunk(response_id=response_id, created=created, model=model)
        yield b"data: [DONE]\n\n"

    def _persist_agent_trace(self, agent_trace: Optional[Any], status: str = "finished", extra: Optional[dict] = None) -> None:
        sink_path = str(getattr(self.valves, "AGENT_TRACE_SINK_PATH", "") or "").strip()
        if not sink_path or agent_trace is None:
            return
        try:
            self.agent_harness.write_trace(agent_trace, sink_path, status=status, extra=extra or {})
        except Exception as exc:
            print("xiamimate.agent failed to persist trace", str(exc)[:300])

    def _grade_agent_answer_for_trace(
        self,
        messages: List[dict],
        answer_text: str,
        tool_observations: List[dict],
        agent_trace: Optional[Any] = None,
    ) -> dict:
        try:
            result = self.agent_harness.grade_answer(
                user_text=self._extract_last_user_text(messages),
                answer_text=answer_text,
                answer_contract=self._answer_contract_from_messages(messages),
                tool_observations=tool_observations,
            )
        except Exception as exc:  # noqa: BLE001
            result = {"grader": "deterministic_v1", "status": "error", "score": 0.0, "checks": [], "failures": [str(exc)[:160]]}
        if agent_trace is not None and result.get("status") != "skipped":
            with contextlib.suppress(Exception):
                agent_trace.record(
                    "grader_result",
                    status=str(result.get("status") or ""),
                    score=result.get("score"),
                    failures=", ".join(result.get("failures") or []),
                )
        return result

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

        mode = requested_mode or command_mode or (model_mode if model_mode in {"agent", "tool", "workflow", "report", "help"} else "agent")
        query = (command_query if command_mode else (user_message or last_user_text or "")).strip()
        return mode, query, command_mode is not None

    def _openwebui_internal_task_name(self, body: dict) -> str:
        if not isinstance(body, dict):
            return ""

        candidates = []
        metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        extra_body = body.get("extra_body") if isinstance(body.get("extra_body"), dict) else {}
        for container in (metadata, extra_body, body):
            for key in ("task", "task_type", "task_name"):
                value = container.get(key) if isinstance(container, dict) else None
                if value not in (None, "", [], {}):
                    candidates.append(value)

        for value in candidates:
            normalized = str(value or "").strip().lower()
            if normalized:
                return normalized
        return ""

    def _is_openwebui_internal_task_request(self, body: dict) -> bool:
        return bool(self._openwebui_internal_task_name(body))

    def _run_openwebui_internal_task(self, body: dict, model: str, model_name: str) -> Union[dict, Iterator[bytes]]:
        task_name = self._openwebui_internal_task_name(body)
        if task_name == "function_calling":
            content = "[]"
        else:
            try:
                payload = self._prepare_openwebui_internal_task_payload(body=body, model_name=model_name)
                response = self._post_agent_payload(payload, model_name=model_name)
                content = self._clean_agent_content(self._extract_assistant_content(response), model_name=model_name)
            except RuntimeError as exc:
                print("xiamimate openwebui internal task failed", task_name, str(exc)[:500])
                content = ""

        if body.get("stream"):
            return self._stream_text_response(content=content, model=model)
        return self._chat_response(content=content, model=model)

    def _prepare_openwebui_internal_task_payload(self, body: dict, model_name: str) -> dict:
        provider = self._get_provider(model_name)
        payload = provider.filter_payload(body)
        payload["model"] = model_name or self._model_name_for_profile(self._default_agent_profile())
        payload["stream"] = False
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        if not isinstance(payload.get("messages"), list):
            payload["messages"] = deepcopy(body.get("messages") or [])
        user_value = payload.get("user")
        if isinstance(user_value, dict):
            payload["user"] = self._user_id(body)
        return payload

    def _parse_report_profile(self, text: str) -> Tuple[str, str]:
        stripped = (text or "").strip()
        if not stripped:
            return "standard", ""

        first_token, _, remainder = stripped.partition(" ")
        normalized = first_token.lower().strip()
        if normalized in REPORT_PROFILE_EVENT_TYPES:
            return normalized, remainder.strip()
        return "standard", stripped

    def _resolve_report_query_from_context(self, query: str, messages: List[dict]) -> Optional[str]:
        normalized_query = (query or "").strip()
        rank = self._extract_opportunity_reference_rank(normalized_query)
        if rank is None:
            rank = self._extract_short_bare_opportunity_rank(normalized_query)
        if rank is None:
            return normalized_query

        opportunity = self._find_referenced_opportunity(rank, messages)
        if not opportunity:
            return None

        report_query = self._report_query_from_opportunity(opportunity)
        if not report_query:
            return None

        suffix = self._strip_opportunity_reference_words(normalized_query)
        if suffix:
            return "%s。补充要求：%s" % (report_query, suffix)
        return report_query

    def _extract_opportunity_reference_rank(self, text: str) -> Optional[int]:
        normalized_text = str(text or "")
        patterns = (
            r"机会\s*(?:编号|#|＃)?\s*(\d{1,3})",
            r"(?:编号|#|＃)\s*(\d{1,3})",
        )
        for pattern in patterns:
            match = re.search(pattern, normalized_text, flags=re.IGNORECASE)
            if not match:
                continue
            try:
                rank = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if rank > 0:
                return rank
        return None

    def _extract_short_bare_opportunity_rank(self, text: str) -> Optional[int]:
        normalized_text = str(text or "").strip()
        match = re.fullmatch(r"(\d{1,3})(?:\s*[\.、\)）:：]\s*|\s+)?(.*)", normalized_text, flags=re.DOTALL)
        if not match:
            return None
        try:
            rank = int(match.group(1))
        except (TypeError, ValueError):
            return None
        if rank <= 0:
            return None

        suffix = str(match.group(2) or "").strip()
        compact_suffix = re.sub(r"[，。,.；;：:\s]+", "", suffix)
        if not compact_suffix:
            return rank
        if re.search(r"\d", compact_suffix):
            return None
        if len(compact_suffix) > 12:
            return None
        if re.match(r"^(天|日|周|月|年|页|万|元|美元|美金|个|件|款|次|%|％)", compact_suffix):
            return None
        return rank

    def _find_referenced_opportunity(self, rank: int, messages: List[dict]) -> Optional[dict]:
        for message in reversed(messages or []):
            if message.get("role") != "assistant":
                continue
            text = self._extract_message_text(message)
            if not text:
                continue

            opportunity = self._find_opportunity_in_structured_text(rank, text)
            if opportunity:
                return opportunity

            opportunity = self._find_opportunity_in_markdown_table(rank, text)
            if opportunity:
                return opportunity

            opportunity = self._find_opportunity_in_card_heading(rank, text)
            if opportunity:
                return opportunity

            opportunity = self._find_opportunity_in_numbered_text(rank, text)
            if opportunity:
                return opportunity
        return None

    def _find_opportunity_in_structured_text(self, rank: int, text: str) -> Optional[dict]:
        for json_text in self._iter_json_blocks(text):
            try:
                payload = json.loads(json_text)
            except ValueError:
                continue
            opportunity = self._find_opportunity_in_json_value(rank, payload)
            if opportunity:
                return opportunity
        return None

    def _iter_json_blocks(self, text: str) -> Iterator[str]:
        for match in re.finditer(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", str(text or ""), flags=re.IGNORECASE | re.DOTALL):
            yield match.group(1)
        stripped = str(text or "").strip()
        if stripped.startswith("{") or stripped.startswith("["):
            yield stripped

    def _find_opportunity_in_json_value(self, rank: int, value: Any) -> Optional[dict]:
        if isinstance(value, list):
            for item in value:
                opportunity = self._find_opportunity_in_json_value(rank, item)
                if opportunity:
                    return opportunity
            return None

        if not isinstance(value, dict):
            return None

        raw_rank = value.get("rank") or value.get("opportunity_rank") or value.get("index") or value.get("number")
        try:
            value_rank = int(str(raw_rank).lstrip("#＃"))
        except (TypeError, ValueError):
            value_rank = None
        if value_rank == rank:
            return value

        for key in ("opportunities_for_llm", "opportunities", "items", "data", "payload", "result"):
            child = value.get(key)
            if isinstance(child, str):
                child_payload = self._load_tool_json_payload(child)
                if child_payload is None:
                    continue
                child = child_payload
            opportunity = self._find_opportunity_in_json_value(rank, child)
            if opportunity:
                return opportunity
        return None

    def _find_opportunity_in_markdown_table(self, rank: int, text: str) -> Optional[dict]:
        header_cells: List[str] = []
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line.startswith("|") or "|" not in line[1:]:
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if not cells or all(re.fullmatch(r"[-: ]+", cell or "") for cell in cells):
                continue
            if any("排名" in cell or "机会" in cell or "类目" in cell for cell in cells):
                header_cells = cells
                continue

            rank_index = self._markdown_table_rank_index(cells, header_cells)
            if rank_index is None:
                continue
            cell_rank = self._parse_rank_cell(cells[rank_index])
            if cell_rank != rank:
                continue

            title_index = self._markdown_table_column_index(header_cells, ("机会主题", "主题", "标题", "品类", "产品"), default=rank_index + 1)
            category_index = self._markdown_table_column_index(header_cells, ("类目路径", "类目", "category"), default=None)
            return {
                "rank": rank,
                "title": cells[title_index].strip() if title_index is not None and title_index < len(cells) else "",
                "category_path": cells[category_index].strip() if category_index is not None and category_index < len(cells) else "",
            }
        return None

    def _markdown_table_rank_index(self, cells: List[str], header_cells: List[str]) -> Optional[int]:
        if header_cells:
            for index, header in enumerate(header_cells):
                if "排名" in header or "编号" in header or header.strip().lower() in {"rank", "#"}:
                    return index if index < len(cells) else None
        for index, cell in enumerate(cells[:3]):
            if self._parse_rank_cell(cell) is not None:
                return index
        return None

    def _markdown_table_column_index(self, header_cells: List[str], names: Tuple[str, ...], default: Optional[int]) -> Optional[int]:
        for index, header in enumerate(header_cells or []):
            normalized_header = header.strip().lower()
            if any(name.lower() in normalized_header for name in names):
                return index
        return default

    def _parse_rank_cell(self, cell: str) -> Optional[int]:
        match = re.search(r"(?:#|＃)?\s*(\d{1,3})", str(cell or ""))
        if not match:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    def _find_opportunity_in_card_heading(self, rank: int, text: str) -> Optional[dict]:
        pattern = re.compile(
            r"^\s*#{1,4}\s*机会\s*(?:编号\s*)?(?:#|＃)?%d\s*[：:]\s*(.+?)\s*$" % rank,
            flags=re.MULTILINE,
        )
        match = pattern.search(str(text or ""))
        if not match:
            return None
        title = match.group(1).strip()
        block_end = len(text)
        next_heading = re.search(r"^\s*#{1,4}\s*机会\s*(?:编号\s*)?(?:#|＃)?\d{1,3}\s*[：:]", text[match.end() :], flags=re.MULTILINE)
        if next_heading:
            block_end = match.end() + next_heading.start()
        block = text[match.end() : block_end]
        category_id = ""
        category_path = ""
        category_id_match = re.search(r"category_id\s*=\s*([0-9]+)", block)
        if category_id_match:
            category_id = category_id_match.group(1).strip()
        category_path_match = re.search(r"category_path\s*=\s*([^，。\n]+)", block)
        if category_path_match:
            category_path = category_path_match.group(1).strip()
        if not category_path:
            category_path_match = re.search(r"细分类目为\s*([^\n；]+)", block)
            if category_path_match:
                category_path = category_path_match.group(1).strip()
        return {"rank": rank, "title": title, "category_id": category_id, "category_path": category_path}

    def _find_opportunity_in_numbered_text(self, rank: int, text: str) -> Optional[dict]:
        pattern = re.compile(
            r"^\s*(?:机会\s*)?(?:编号\s*)?(?:#|＃)?%d[\.、\)）:：\s]+(.+)$" % rank,
            flags=re.MULTILINE,
        )
        match = pattern.search(str(text or ""))
        if not match:
            return None
        title = re.split(r"[|｜]", match.group(1).strip(), maxsplit=1)[0].strip()
        return {"rank": rank, "title": title, "category_path": ""}

    def _report_query_from_opportunity(self, opportunity: dict) -> str:
        if not isinstance(opportunity, dict):
            return ""
        next_action = opportunity.get("next_action") if isinstance(opportunity.get("next_action"), dict) else {}
        request_payload = next_action.get("request") if isinstance(next_action.get("request"), dict) else {}
        title = str(
            request_payload.get("product_query")
            or request_payload.get("query")
            or opportunity.get("title")
            or opportunity.get("opportunity_title")
            or ""
        ).strip()
        category_path = str(request_payload.get("category_path") or opportunity.get("category_path") or "").strip()
        category_id = str(request_payload.get("category_id") or opportunity.get("category_id") or "").strip()
        parts = []
        if title:
            parts.append(title)
        if category_path:
            parts.append("类目路径：%s" % category_path)
        elif category_id:
            parts.append("类目ID：%s" % category_id)
        return "；".join(parts).strip()

    def _strip_opportunity_reference_words(self, query: str) -> str:
        text = re.sub(r"机会\s*(?:编号|#|＃)?\s*\d{1,3}", "", str(query or ""), flags=re.IGNORECASE)
        text = re.sub(r"(?:编号|#|＃)\s*\d{1,3}", "", text, flags=re.IGNORECASE)
        if self._extract_short_bare_opportunity_rank(text) is not None:
            text = re.sub(r"^\s*\d{1,3}(?:\s*[\.、\)）:：]\s*|\s+)?", "", text, count=1)
        text = re.sub(r"\b(?:quick|standard|deep|research)\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"[，。,.；;：:\s]+", " ", text).strip()
        removable = {"请", "帮我", "帮忙", "使用", "用", "分析", "分析一下", "一下", "这个", "这条", "机会", "报告"}
        tokens = [token for token in text.split() if token not in removable]
        return " ".join(tokens).strip()

    def _report_opportunity_reference_guidance(self, query: str, profile: str) -> str:
        profile_text = str(profile or "quick").strip() or "quick"
        return (
            "我识别到你想分析上文的 `%s`，但当前对话里没有找到对应的机会卡片明细。\n\n"
            "请把机会主题放到 `/report %s` 后面，例如：\n\n"
            "`/report %s Power Strips，分析一下`\n\n"
            "或者先让 /agent 重新展示机会列表，再按完整命令继续。"
        ) % ((query or "").strip(), profile_text, profile_text)

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
            "MiniMax agent request": "仅未触发工具的独立 LLM 请求计费。",
            "MiniMax agent request failed": "独立 LLM 请求失败，系统已自动退款。",
            "LLM 请求": "仅未触发工具的独立 LLM 请求计费。",
            "LLM 请求失败，已退款": "独立 LLM 请求失败，系统已自动退款。",
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
                "llm_request": "仅未触发工具的独立 LLM 请求计费。",
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
            "candidate_pool_slice": "候选池切片",
            "candidate_pool_trends": "候选池趋势",
            "candidate_pool_weak_forecast": "弱信号预测",
            "product_forecast_explain": "模型预测解释",
            "top_asin_drilldown": "头部 ASIN 深挖",
            "opportunity_discovery": "机会发现",
            "asin_history_timeseries": "ASIN 历史时序",
            "asin_review_insights": "评论关键词洞察",
            "amazon_keyword_demand": "Amazon 关键词需求",
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
        gate_refund_description: str,
        report_profile: str,
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
        refund_applied = False

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
                            if mode_tag == "report" and buffered_answer and not refund_applied:
                                refund_applied = self._refund_report_gate_failure(
                                    billing_context=billing_context,
                                    charge=flow_charge,
                                    answer_text=buffered_answer,
                                    report_profile=report_profile,
                                    description=gate_refund_description,
                                    mode_tag="%s_stream" % mode_tag,
                                )
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
                    if mode_tag == "report" and raw_answer and not refund_applied:
                        refund_applied = self._refund_report_gate_failure(
                            billing_context=billing_context,
                            charge=flow_charge,
                            answer_text=raw_answer,
                            report_profile=report_profile,
                            description=gate_refund_description,
                            mode_tag="%s_stream" % mode_tag,
                        )
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
            if not answer_started and not refund_applied:
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
            if not answer_started and not refund_applied:
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

    def _format_agent_progress(self, description: str, percent: Optional[int] = None) -> str:
        if percent is None:
            return "⏳ /agent · %s\n" % description

        normalized_percent = max(0, min(100, int(percent)))
        total_slots = 10
        filled = max(0, min(total_slots, round((normalized_percent / 100) * total_slots)))
        bar = "#" * filled + "." * (total_slots - filled)
        return "⏳ /agent 进度 [%s] %d%% · %s\n" % (bar, normalized_percent, description)

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
        answer_text = self._strip_outer_markdown_fence(answer_text)
        visible_text, payload = self._extract_structured_workflow_payload(answer_text)
        if not payload:
            return self._annotate_report_followup_actionability(answer_text), None

        payload = self._augment_asin_history_payload(payload)
        payload = self._augment_selection_report_payload(payload, fallback_summary=visible_text)
        rendered = self._render_structured_workflow_payload(payload, fallback_summary=visible_text)
        rendered = self._append_report_refund_visibility_note(rendered, payload)
        rendered = self._annotate_report_followup_actionability(rendered)
        payload_comment = self._build_structured_payload_comment(payload)
        if rendered:
            return rendered.rstrip(), payload_comment
        if visible_text:
            visible_text = self._annotate_report_followup_actionability(visible_text)
            return self._append_report_refund_visibility_note(visible_text, payload).rstrip(), payload_comment
        return "", payload_comment

    def _annotate_report_followup_actionability(self, text: str) -> str:
        # 已下线评论文本/关键词量 provider 的对外声明：报告尾部不再注入“能力边界提示”，
        # 也不再把追问改写成“需 provider”。反编造由 prompt 静默约束负责，此处保持原文不变。
        return str(text or "")

    def _append_report_refund_visibility_note(self, text: str, payload: dict) -> str:
        rendered = str(text or "").rstrip()
        if not isinstance(payload, dict):
            return rendered
        if str(payload.get("schema_version") or "").strip() != "xm.report-delivery.v1":
            return rendered
        if str(payload.get("delivery_status") or "").strip() != "gated_failed":
            return rendered
        if self._coerce_optional_bool(payload.get("refund_recommended")) is not True:
            return rendered
        if REPORT_REFUND_VISIBILITY_NOTE in rendered:
            return rendered
        note = "\n\n> %s" % REPORT_REFUND_VISIBILITY_NOTE
        return (rendered + note).strip() if rendered else REPORT_REFUND_VISIBILITY_NOTE

    def _strip_outer_markdown_fence(self, answer_text: str) -> str:
        text = str(answer_text or "")
        match = re.match(
            r"\A\s*```[ \t]*(?:markdown|md)[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\s*\Z",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return text
        return match.group("body")

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

    def _coerce_optional_bool(self, value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        normalized = str(value or "").strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
        return None

    def _report_gate_refund_meta(self, answer_text: str, report_profile: str) -> Optional[dict]:
        _visible_text, payload = self._extract_structured_workflow_payload(answer_text)
        if not isinstance(payload, dict):
            return None

        if str(payload.get("schema_version") or "").strip() != "xm.report-delivery.v1":
            return None

        payload_profile = str(payload.get("report_profile") or "").strip()
        if payload_profile and report_profile and payload_profile != report_profile:
            return None

        delivery_status = str(payload.get("delivery_status") or "").strip()
        refund_reason = str(payload.get("refund_reason") or "").strip()
        report_generated = self._coerce_optional_bool(payload.get("report_generated"))
        refund_recommended = self._coerce_optional_bool(payload.get("refund_recommended"))
        if delivery_status != "gated_failed":
            return None
        if report_generated is not False or refund_recommended is not True:
            return None
        if refund_reason not in REPORT_GATE_REFUND_REASONS:
            return None

        refund_meta = {
            "schema_version": "xm.report-delivery.v1",
            "report_profile": payload_profile or report_profile,
            "delivery_status": delivery_status,
            "refund_reason": refund_reason,
        }
        for key in ("gate_stage", "failure_category"):
            value = str(payload.get(key) or "").strip()
            if value:
                refund_meta[key] = value
        return refund_meta

    def _refund_report_gate_failure(
        self,
        billing_context: dict,
        charge: dict,
        answer_text: str,
        report_profile: str,
        description: str,
        mode_tag: str,
    ) -> bool:
        refund_meta = self._report_gate_refund_meta(answer_text, report_profile=report_profile)
        if not refund_meta:
            return False
        refund_meta["mode"] = mode_tag
        self._refund_billing_event(
            billing_context=billing_context,
            charge=charge,
            description=description,
            meta=refund_meta,
        )
        return True

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

    def _build_agent_tool_definitions(self, mode: str = "agent") -> List[dict]:
        definitions = []
        if self.agent_tools is None:
            return definitions
        allowed_tools = {"customer_help_search"} if mode == "help" else ALLOWED_AGENT_TOOLS
        for tool_name in sorted(allowed_tools):
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
        payload["messages"] = self._inject_agent_system_prompt(
            messages,
            mode=mode,
            memory_profile=body.get("_xiamimate_memory_profile") if isinstance(body, dict) else None,
        )
        if "tools" in getattr(provider, "allowed_params", set()):
            payload["tools"] = self._build_agent_tool_definitions(mode=mode)
            payload.setdefault("tool_choice", "auto")

        user_value = payload.get("user")
        if isinstance(user_value, dict):
            payload["user"] = self._user_id(body)

        return payload

    def _agent_max_tool_rounds(self) -> int:
        try:
            return max(1, min(50, int(self.valves.AGENT_MAX_TOOL_ROUNDS)))
        except Exception:
            return 12

    def _scene_policy(self, scene: str, mode: str = "agent") -> dict:
        return self.agent_harness.scene_policy(scene, mode)

    def _classify_agent_scene(self, messages: List[dict], mode: str = "agent") -> str:
        text = self._extract_last_user_text(messages)
        if mode == "help":
            return "foundation_qa"
        if self._looks_like_budget_analysis_request(text):
            return "budget_analysis"
        if self._looks_like_foundation_question(text):
            return "foundation_qa"
        if self._looks_like_blank_opportunity_discovery_request(text):
            return "blank_opportunity_discovery"
        if self._looks_like_asin_specific_request(text):
            return "asin_specific_analysis"
        if self._looks_like_explicit_theme_analysis_request(text):
            return "theme_analysis"
        return "general_agent"

    def _looks_like_foundation_question(self, text: str) -> bool:
        content = str(text or "").strip().lower()
        if not content:
            return False
        patterns = (
            r"跨境电商.*是什么",
            r"什么是.*跨境电商",
            r"(?:新手|入门|小白).{0,12}(?:提示词|怎么问|怎么用|怎么开始|教程|指南|步骤)",
            r"提示词",
            r"(?:怎么用|如何使用|使用方法|介绍一下|是什么|什么意思|有什么区别)",
            r"(?:虾米选品|选品工具).{0,12}(?:怎么用|如何用|是什么|介绍)",
        )
        if any(re.search(pattern, content, flags=re.IGNORECASE) for pattern in patterns):
            return True
        if self._looks_like_explicit_theme_analysis_request(content):
            return False
        return False

    def _looks_like_budget_analysis_request(self, text: str) -> bool:
        content = str(text or "").strip().lower()
        if not content:
            return False
        patterns = (
            r"(?:启动资金|预算|盈亏平衡|回本|利润|毛利|净利|单件利润)",
            r"(?:fba|佣金|广告费|备货|成本核算)",
        )
        return any(re.search(pattern, content, flags=re.IGNORECASE) for pattern in patterns)

    def _looks_like_asin_specific_request(self, text: str) -> bool:
        content = str(text or "").strip().upper()
        if not content:
            return False
        return bool(re.search(r"\bB0[A-Z0-9]{8}\b", content))

    def _planner_allowed_tool_names(self, scene: str, mode: str = "agent") -> List[str]:
        return self.agent_harness.planner_allowed_tool_names(scene, mode)

    def _planner_tool_catalog(self, scene: str, mode: str = "agent") -> List[dict]:
        return self.agent_harness.planner_tool_catalog(scene, mode, tool_label=self._tool_name_label)

    def _planner_observation_context(self, tool_observations: List[dict], limit: Optional[int] = None) -> List[dict]:
        effective_limit = int(limit) if limit is not None else max(1, int(self.valves.AGENT_PLANNER_OBSERVATION_LIMIT or 4))
        return self.agent_harness.planner_observation_context(
            tool_observations,
            lambda text, budget: self._truncate_text_for_llm(text, budget=int(budget or 12000)),
            limit=effective_limit,
        )

    def _synthesis_observation_context(self, tool_observations: List[dict], limit: Optional[int] = None) -> List[dict]:
        # 合成阶段（生成用户最终答复）必须保留所有工具结果中的数值，
        # 不能像 planner 那样把历史 observation 压成 head+tail 摘要，
        # 否则像 candidate_pool_stats 这类“非最后一步”的统计数值会丢失。
        effective_limit = int(limit) if limit is not None else 8
        return self.agent_harness.planner_observation_context(
            tool_observations,
            lambda text, budget: self._truncate_text_for_llm(text, budget=int(budget or 16000)),
            limit=effective_limit,
            digest_older=False,
            newest_budget=16000,
            older_budget=12000,
        )

    def _observed_tool_names(self, tool_observations: List[dict]) -> List[str]:
        return self.agent_harness.observed_tool_names(tool_observations)

    def _scene_single_execution_tools(self, scene: str) -> set:
        return self.agent_harness.scene_single_execution_tools(scene)

    def _filter_redundant_planner_steps(self, steps: List[dict], scene: str, tool_observations: List[dict]) -> List[dict]:
        return self.agent_harness.filter_redundant_planner_steps(steps, scene, tool_observations)

    def _infer_theme_product_query(self, messages: List[dict]) -> str:
        patterns = (
            r"(?:评估|分析|判断|研究|拆解|看看|看一下)\s+([^，。！？\n]{2,90}?)\s+在\s+",
            r"^\s*([A-Za-z][A-Za-z0-9 &+\-/]{1,70})\s+在\s+",
            r"(?:评估|分析|判断|研究|拆解)\s+([A-Za-z][A-Za-z0-9 &+\-/]{1,70})",
        )
        user_texts: List[str] = []
        for msg in reversed(messages or []):
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "").lower() != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_part = str(part.get("text") or "").strip()
                        if text_part:
                            user_texts.append(text_part)
            else:
                text_part = str(content or "").strip()
                if text_part:
                    user_texts.append(text_part)
            if len(user_texts) >= 6:
                break
        for text in user_texts:
            for pattern in patterns:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    return str(match.group(1) or "").strip(" ：:，,。")[:120]
        # 兜底 1：从"裸商品查询"里抽取主题。像"挂脖风扇top3的asin"/"加湿器候选池"/"车载吸尘器销量怎么样"
        # 这类没有"评估/分析 X 在"句式的提问，上面的窄模式都匹配不到，会导致 resolve_candidates 缺
        # product_query 被 _repair_theme_resolve_step 判空丢弃，整条计划被清空（validation=empty）。
        # 这里在回退到 session 旧主题之前，先尝试剥掉前导动词与尾部分析性修饰，取出核心商品词。
        for text in user_texts:
            bare = self._extract_bare_product_query(text)
            if bare:
                return bare
        snapshot = self._session_snapshot()
        fallback = str(snapshot.get("last_product_query") or "").strip()
        return fallback[:120] if fallback else ""

    @staticmethod
    def _extract_bare_product_query(text: str) -> str:
        """从裸商品提问里抽取核心商品主题词，例如：
        "挂脖风扇top3的asin" -> "挂脖风扇"；"加湿器候选池" -> "加湿器"；"车载吸尘器销量怎么样" -> "车载吸尘器"。

        仅做轻量启发式：去掉前导命令/动词/礼貌词，去掉尾部 top-N / asin / 候选池 / 销量 等分析性修饰，
        剩余 1~40 字的短词才视为有效主题；抽不出就返回空串，交给上层继续走 session 兜底。
        """
        t = str(text or "").strip()
        if not t:
            return ""
        # 去掉前导命令、礼貌词与动词
        t = re.sub(r"^\s*/(?:agent|tool|web|report|help|wf|workflow)\b\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"^(?:请|帮我|麻烦|给我|我想|我要|想|要|看看|看一下|帮忙)+\s*", "", t)
        t = re.sub(r"^(?:分析|评估|判断|研究|拆解|查询|查|看|获取|返回|列出|找|搜索|解析)\s*(?:下|一下)?\s*", "", t)
        # 去掉尾部分析性修饰（top-N / 排名 / asin / 候选池 / 销量 / 怎么样 等）
        t = re.sub(r"\s*的?\s*(?:top\s*\d+|前\s*\d+\s*名?|top|排名|榜单).*$", "", t, flags=re.IGNORECASE)
        t = re.sub(
            r"\s*的?\s*(?:asin|候选池|候选|销量|数据|评分|评论|趋势|机会|怎么样|如何|详情|信息|分析).*$",
            "",
            t,
            flags=re.IGNORECASE,
        )
        t = t.strip(" \t，,。.；;：:、!！?？的了呢吗")
        if 1 <= len(t) <= 40:
            return t[:120]
        return ""


    def _session_snapshot(self) -> Dict[str, Any]:
        try:
            return self.agent_harness.session_snapshot()
        except AttributeError:
            return {}

    def _explicit_tool_name_from_text(self, text: str) -> str:
        return self.agent_harness.explicit_tool_name_from_text(text)

    def _web_search_query_from_tool_alias(self, text: str) -> str:
        query = str(text or "").strip()
        query = re.sub(
            r"^(?:请|帮我)?\s*(?:调用|执行|运行|使用)?\s*(?:原生)?\s*(?:工具\s*)?web[_-]?search\s*[，,:：]?\s*",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip()
        return query or str(text or "").strip()

    def _extract_explicit_tool_subject(self, text: str, tool_name: str) -> str:
        return agent_harness.extract_explicit_tool_subject(text, tool_name)

    def _infer_tool_required_arguments(self, tool_name: str, messages: List[dict], body: dict, parameters: Dict[str, Any]) -> Dict[str, Any]:
        normalized_tool = str(tool_name or "").strip()
        latest_text = self._extract_last_user_text(messages)
        inferred: Dict[str, Any] = self._extract_explicit_tool_parameters_from_text(latest_text, normalized_tool)
        explicit_subject = self._extract_explicit_tool_subject(latest_text, normalized_tool)

        if normalized_tool == "resolve_candidates":
            product_query = str(parameters.get("product_query") or "").strip()
            if not product_query:
                product_query = self._infer_theme_product_query(messages) or explicit_subject
            if product_query:
                inferred["product_query"] = product_query
            if not str(parameters.get("marketplace") or "").strip():
                inferred["marketplace"] = self._infer_theme_marketplace(messages, body)
            if not str(parameters.get("recall_mode") or "").strip():
                inferred["recall_mode"] = "keyword"

        elif normalized_tool == "category_resolve":
            category_query = str(parameters.get("category_query") or "").strip()
            if not category_query:
                category_query = explicit_subject
            if category_query:
                inferred["category_query"] = category_query
            if not str(parameters.get("marketplace") or "").strip():
                inferred["marketplace"] = self._infer_theme_marketplace(messages, body)

        elif normalized_tool == "expand_candidates":
            product_query = str(parameters.get("product_query") or "").strip()
            if not product_query:
                product_query = self._infer_theme_product_query(messages) or explicit_subject
            if product_query:
                inferred["product_query"] = product_query
            if not str(parameters.get("marketplace") or "").strip():
                inferred["marketplace"] = self._infer_theme_marketplace(messages, body)

        elif normalized_tool == "launch_budget_calculator":
            product_theme = str(parameters.get("product_theme") or "").strip()
            if not product_theme:
                product_theme = self._infer_theme_product_query(messages) or explicit_subject
            if product_theme:
                inferred["product_theme"] = product_theme
            if not str(parameters.get("marketplace") or "").strip():
                inferred["marketplace"] = self._infer_theme_marketplace(messages, body)

        return inferred

    def _extract_explicit_tool_parameters_from_text(self, text: str, tool_name: str) -> Dict[str, Any]:
        return self.agent_harness.extract_explicit_tool_parameters_from_text(text, tool_name, self._normalize_tool_call)

    def _tool_call_has_required_arguments(self, tool_call: Dict[str, Any]) -> bool:
        return agent_harness.tool_call_has_required_arguments(tool_call)

    def _repair_tool_call_required_arguments(
        self,
        tool_call: Dict[str, Any],
        messages: List[dict],
        body: dict,
    ) -> Optional[Dict[str, Any]]:
        return self.agent_harness.repair_tool_call_required_arguments(
            tool_call,
            lambda tool_name, parameters: self._infer_tool_required_arguments(tool_name, messages, body, parameters),
            self._normalize_tool_call,
        )

    def _repair_planner_steps_required_arguments(self, steps: List[dict], messages: List[dict], body: dict) -> List[dict]:
        repaired_steps: List[dict] = []
        answer_contract = self._answer_contract_from_messages(messages)
        for step in steps or []:
            repaired_call = self._repair_tool_call_required_arguments((step or {}).get("tool_call") or {}, messages, body)
            if repaired_call is None:
                continue
            repaired_call = self.agent_harness.preflight_tool_call(repaired_call, answer_contract=answer_contract)
            repaired_step = dict(step or {})
            repaired_step["tool_call"] = repaired_call
            repaired_steps.append(repaired_step)
        return repaired_steps

    def _answer_contract_from_messages(self, messages: List[dict]) -> dict:
        latest_text = self._extract_last_user_text(messages)
        try:
            return self.agent_harness.answer_contract_from_text(latest_text)
        except AttributeError:
            return {}

    def _explicit_tool_request_step(self, messages: List[dict], body: dict, scene: str, mode: str = "agent") -> Optional[dict]:
        latest_text = self._extract_last_user_text(messages)
        tool_name = self._explicit_tool_name_from_text(latest_text)
        if not tool_name:
            return None
        initial_call = self._normalize_tool_call(name=tool_name, parameters={})
        if initial_call is None or not self._tool_call_allowed_for_scene(initial_call, scene, mode):
            return None
        repaired_call = self._repair_tool_call_required_arguments(initial_call, messages, body)
        if repaired_call is None:
            return None
        repaired_call = self.agent_harness.preflight_tool_call(
            repaired_call,
            answer_contract=self._answer_contract_from_messages(messages),
        )
        return {"tool_call": repaired_call, "goal": "按用户显式请求直接调用目标工具", "required": True}

    def _scene_for_explicit_tool(self, tool_name: str, current_scene: str) -> str:
        return self.agent_harness.scene_for_explicit_tool(tool_name, current_scene)

    def _infer_theme_marketplace(self, messages: List[dict], body: dict) -> str:
        explicit = self._body_context_value(body, "marketplace") or self._body_context_value(body, "target_market")
        if explicit:
            return self._normalize_marketplace_value(explicit)
        text = self._extract_last_user_text(messages).lower()
        if "amazon us" in text or "amazon 美国" in text or "美国站" in text or "美国市场" in text:
            return "US"
        return "US"

    def _build_theme_resolve_step(self, messages: List[dict], body: dict) -> Optional[dict]:
        product_query = self._infer_theme_product_query(messages)
        if not product_query:
            product_query = self._extract_explicit_tool_subject(self._extract_last_user_text(messages), "resolve_candidates")
        if not product_query:
            return None
        tool_call = self._normalize_tool_call(
            name="resolve_candidates",
            parameters={
                "product_query": product_query,
                "marketplace": self._infer_theme_marketplace(messages, body),
                "recall_mode": "keyword",
                "max_candidates": 30,
            },
        )
        if tool_call is None:
            return None
        return {"tool_call": tool_call, "goal": "先建立候选池，作为后续主题分析的稳定证据锚点", "required": True}

    def _repair_theme_resolve_step(self, step: dict, messages: List[dict], body: dict) -> Optional[dict]:
        tool_call = (step or {}).get("tool_call") or {}
        if str(tool_call.get("name") or "").strip() != "resolve_candidates":
            return step if isinstance(step, dict) else None

        repaired_call = self._repair_tool_call_required_arguments(tool_call, messages, body)
        if repaired_call is not None:
            repaired = dict(step or {})
            repaired["tool_call"] = repaired_call
            return repaired

        fallback_step = self._build_theme_resolve_step(messages, body)
        if fallback_step is None:
            return step if isinstance(step, dict) else None

        repaired = dict(step or {})
        repaired["tool_call"] = fallback_step["tool_call"]
        if not str(repaired.get("goal") or "").strip():
            repaired["goal"] = fallback_step.get("goal") or ""
        return repaired

    def _enforce_theme_resolve_first_step(
        self,
        steps: List[dict],
        scene: str,
        messages: List[dict],
        body: dict,
        tool_observations: List[dict],
    ) -> List[dict]:
        if scene != "theme_analysis" or "resolve_candidates" in set(self._observed_tool_names(tool_observations)):
            return steps
        session_snapshot = self._session_snapshot() or {}
        existing_pool = session_snapshot.get("last_candidate_pool") if isinstance(session_snapshot, dict) else None
        if isinstance(existing_pool, dict) and existing_pool.get("pool_id"):
            return steps
        for step in steps or []:
            if str(((step or {}).get("tool_call") or {}).get("name") or "").strip() == "resolve_candidates":
                repaired_step = self._repair_theme_resolve_step(step, messages, body)
                return [repaired_step] if repaired_step is not None else []
        fallback_step = self._build_theme_resolve_step(messages, body)
        return [fallback_step] if fallback_step is not None else steps

    def _prepare_agent_planner_payload(
        self,
        *,
        messages: List[dict],
        body: dict,
        mode: str,
        model_name: str,
        scene: str,
        tool_observations: List[dict],
        remaining_rounds: int,
    ) -> dict:
        # planner 单独走轻量模型时，filter_payload 仍用 agent provider 以兼容字段过滤，
        # 但 payload.model 改成 planner_model_name，让 chat-backend 路由到轻量模型。
        planner_model_name = str(self.valves.AGENT_PLANNER_MODEL or "").strip() or model_name or self._model_name_for_profile(self._default_agent_profile())
        provider = self._get_provider(planner_model_name)
        payload = provider.filter_payload(body)
        payload["model"] = planner_model_name
        payload["stream"] = False
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        planner_messages = deepcopy(messages or [])
        planner_messages.insert(0, {"role": "system", "content": AGENT_PLANNER_SYSTEM_PROMPT})
        self._insert_agent_memory_profile_message(
            planner_messages,
            body.get("_xiamimate_memory_profile") if isinstance(body, dict) else None,
        )
        session_snapshot = self._session_snapshot() or {}
        observed_tools = list(self._observed_tool_names(tool_observations))
        # 跨轮已持久化的 prerequisite 工具（候选池/类目）必须以 already_observed 形式告诉 planner，
        # 否则 planner 在新一轮请求里看不到本次 tool_observations，会重新规划 resolve_candidates。
        executed_signatures: list[str] = []
        if isinstance(session_snapshot, dict):
            pool = session_snapshot.get("last_candidate_pool") if isinstance(session_snapshot.get("last_candidate_pool"), dict) else None
            if pool and pool.get("pool_id") and "resolve_candidates" not in observed_tools:
                observed_tools.append("resolve_candidates")
            if (session_snapshot.get("last_category_id") or session_snapshot.get("last_category_path")) and "category_resolve" not in observed_tools:
                observed_tools.append("category_resolve")
            for entry in session_snapshot.get("recent_tool_calls") or []:
                tn = (entry or {}).get("tool_name")
                fp = (entry or {}).get("params_fingerprint")
                if tn and fp:
                    # 状态轮询型工具（如 candidate_expansion_status）不进入 executed 签名，
                    # 否则 planner 会按"匹配已执行签名就不重复执行"的规则直接复述旧 summary，
                    # 导致已 completed 的扩池任务仍被报成 queued。这类工具每轮都应真实重查。
                    if tn in agent_harness.STATUS_POLL_REFRESH_TOOLS:
                        continue
                    # 超出新鲜窗口的旧签名不再作为"已执行、勿重复"提示喂给 planner，
                    # 否则 planner 会复述过期 summary；放行后让它可按需重新取最新数据。
                    ts = (entry or {}).get("recorded_at")
                    if isinstance(ts, (int, float)) and (
                        time.time() - float(ts)
                    ) > agent_harness.CROSS_TURN_DEDUP_FRESHNESS_SECONDS:
                        continue
                    executed_signatures.append(f"{tn}::{fp}")
        planner_messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "current_date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
                        "mode": mode,
                        "scene_hint": scene,
                        "scene_policy": self._scene_policy(scene, mode),
                        "allowed_tools": self._planner_tool_catalog(scene, mode),
                        "already_observed_tools": observed_tools,
                        "executed_tool_signatures": executed_signatures,
                        "previous_tool_observations": self._planner_observation_context(tool_observations),
                        "session_memory": session_snapshot,
                        "remaining_rounds": remaining_rounds,
                        "planner_note": (
                            "session_memory 是同一对话之前已成功执行的工具产物（候选池/类目/各分析工具的最近结果），可直接复用。"
                            "通用跨轮去重规则：如果你要规划的工具调用 (tool_name + 参数) 完全匹配 executed_tool_signatures 中已有的签名，"
                            "请不要重复执行——直接从 session_memory.recent_tool_calls 取既有 summary，"
                            "或者基于已有产物给出 final_answer。"
                            "仅当用户明确要求『重新跑/刷新/换 window/换 marketplace 等不同参数』时才允许用不同入参重跑同名工具。"
                            "对于 resolve_candidates / category_resolve 这类只负责获取候选池/类目句柄的 prerequisite 工具，"
                            "只要 session_memory.last_candidate_pool.pool_id 或 last_category_id 已存在，就视为已完成，禁止再次调用。"
                            "但 candidate_expansion_status 等状态查询类工具属于例外：后台任务状态会随时间变化（queued→discovering→hydrating→completed），"
                            "只要用户在询问补池/任务进度或数据是否就绪，就必须重新调用该工具获取最新 status 与 data_readiness，"
                            "绝不能复述 session_memory 里上一轮的旧状态摘要。"
                            "【缓存标记】session_memory.recent_tool_calls 里的每条结果都带 cached=true 和 cached_age_seconds，"
                            "表示它是之前轮次缓存的结果、不是本轮实时重查。当底层数据可能已经变化时（最典型：扩池/补池任务刚 completed，"
                            "候选池新增了 ASIN，但 candidate_pool_stats / candidate_pool_trends / category_benchmark / top_asin_drilldown "
                            "等工具入参只含 pool_id、签名没变，会被去重直接复述补池前的旧统计），这些缓存结果就已过期、不可直接采信。"
                            "【强制刷新权限】此时请在该工具调用的 parameters 里显式加上 force_refresh=true，"
                            "即可绕过跨轮去重、强制真实重新调用工具拿最新结果（该标记只控制刷新，不会传给工具本身）。"
                            "判断准则：只要你怀疑缓存结果可能已过期（数据源刚发生变更、用户明确要求最新/刷新、补池刚完成等），就加 force_refresh=true 重查；"
                            "若数据稳定且 cached_age_seconds 不大，可继续复用缓存以节省调用。"
                            "如果已有工具足够回答，请返回 action.type=final 和 final_answer。"
                            "如果需要继续执行新的工具，只返回一个 action.type=tool 的下一步动作，不要一次性规划完整路线。"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            }
        )
        payload["messages"] = planner_messages

        user_value = payload.get("user")
        if isinstance(user_value, dict):
            payload["user"] = self._user_id(body)
        return payload

    def _extract_json_value_from_text(self, value: Any) -> Optional[Any]:
        if isinstance(value, (dict, list)):
            return value

        text = str(value or "").strip()
        if not text:
            return None

        def _try_load(raw: str):
            raw = raw.strip()
            if not raw:
                return None
            for attempt in (raw, raw.replace("\r\n", "\n")):
                try:
                    return json.loads(attempt)
                except ValueError:
                    pass
            # 容忍字符串值中未转义的换行/制表符（LLM 经常这么干）
            try:
                return json.loads(raw, strict=False)
            except ValueError:
                return None

        loaded = _try_load(text)
        if loaded is not None:
            return loaded

        fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
        if fenced_match:
            loaded = _try_load(fenced_match.group(1))
            if loaded is not None:
                return loaded

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            loaded = _try_load(text[start : end + 1])
            if loaded is not None:
                return loaded

        # 最后兜底：如果文本明显是 planner JSON（包含 action/final_answer 关键字），
        # 用正则提取 final_answer 字符串，避免把原始 JSON 当作最终回答吐给用户。
        if re.search(r'"action"\s*:|"final_answer"\s*:', text):
            m = re.search(r'"final_answer"\s*:\s*"((?:\\.|[^"\\])*)"', text, flags=re.DOTALL)
            if m:
                try:
                    final_answer = json.loads('"' + m.group(1) + '"')
                except ValueError:
                    final_answer = m.group(1).encode("utf-8", "ignore").decode("unicode_escape", "ignore")
                return {"action": {"type": "final", "final_answer": final_answer}}
        return None

    def _tool_call_allowed_for_scene(self, tool_call: Dict[str, Any], scene: str, mode: str = "agent") -> bool:
        return self.agent_harness.tool_call_allowed_for_scene(tool_call, scene, mode)

    def _normalize_planner_step(self, step: dict, scene: str, mode: str = "agent") -> Optional[dict]:
        if not isinstance(step, dict):
            return None
        tool_call = step.get("tool_call") if isinstance(step.get("tool_call"), dict) else {}
        tool_name = str(step.get("tool_name") or step.get("name") or tool_call.get("name") or "").strip()
        raw_parameters = step.get("parameters") if isinstance(step.get("parameters"), dict) else tool_call.get("parameters")
        if not isinstance(raw_parameters, dict):
            raw_parameters = {}
        normalized_call = self._normalize_tool_call(name=tool_name, parameters=raw_parameters)
        if normalized_call is None or not self._tool_call_allowed_for_scene(normalized_call, scene, mode):
            return None
        return {
            "tool_call": normalized_call,
            "goal": str(step.get("goal") or "").strip(),
            "required": bool(step.get("required", True)),
        }

    def _normalize_planner_action(self, action: Any, scene: str, mode: str = "agent") -> Tuple[bool, str, List[dict], str]:
        return self.agent_harness.normalize_planner_action(action, scene, mode, self._normalize_planner_step)

    def _normalize_planner_plan(self, plan_payload: Any, scene: str, mode: str = "agent") -> dict:
        return self.agent_harness.normalize_planner_plan(plan_payload, scene, mode, self._normalize_planner_step)

    def _plan_agent_next_steps(
        self,
        *,
        messages: List[dict],
        body: dict,
        model_name: str,
        mode: str,
        scene: str,
        tool_observations: List[dict],
        remaining_rounds: int,
    ) -> dict:
        payload = self._prepare_agent_planner_payload(
            messages=messages,
            body=body,
            mode=mode,
            model_name=model_name,
            scene=scene,
            tool_observations=tool_observations,
            remaining_rounds=remaining_rounds,
        )
        planner_model_name = str(payload.get("model") or "").strip() or model_name
        planner_base_url = (self.valves.AGENT_PLANNER_BASE_URL or "").strip()
        planner_api_key = (self.valves.AGENT_PLANNER_API_KEY or "").strip()
        if planner_base_url and planner_api_key and self.valves.AGENT_PLANNER_MODEL:
            response = self._post_llm_direct(
                base_url=planner_base_url,
                api_key=planner_api_key,
                payload=payload,
                timeout=self.valves.DIFY_REQUEST_TIMEOUT,
            )
        else:
            response = self._post_agent_payload(payload, model_name=planner_model_name)
        native_tool_calls = self._filter_tool_calls_for_user_intent(self._extract_response_tool_calls(response), messages)
        if native_tool_calls:
            steps = []
            for tool_call in native_tool_calls[:1]:
                if self._tool_call_allowed_for_scene(tool_call, scene, mode):
                    steps.append({"tool_call": tool_call, "goal": "执行模型返回的原生工具调用", "required": True})
            return {
                "scene": scene,
                "answer_ready": False,
                "final_answer": "",
                "reasoning_summary": "模型返回了原生工具调用，已交由 harness 校验执行。",
                "stop_reason": "等待工具 observation 后再继续决策。",
                "action_type": "tool" if steps else "none",
                "planner_protocol": "native_tool_calls",
                "steps": steps,
            }
        content = self._clean_agent_content(self._extract_assistant_content(response), model_name=model_name)
        plan_payload = self._extract_json_value_from_text(content)
        if plan_payload is None and content.strip():
            # 防止模型返回一段看似 planner JSON 但解析失败的文本被当成 final_answer 吐给用户。
            # 这种情况应该走后续的 synthesis 路径，由 _synthesize_planner_executor_answer
            # 重新基于已有观察生成 markdown，或在无观察时走兑底提示。
            if self._looks_like_planner_json(content):
                return {
                    "scene": scene,
                    "answer_ready": False,
                    "final_answer": "",
                    "reasoning_summary": "检测到模型返回 planner JSON 但未能解析，转交后续综合生成。",
                    "stop_reason": "planner_json_leak_guard",
                    "action_type": "none",
                    "planner_protocol": "leak_guard",
                    "steps": [],
                }
            return {
                "scene": scene,
                "answer_ready": True,
                "final_answer": content.strip(),
                "reasoning_summary": "模型返回了可见文本，按最终回答处理。",
                "stop_reason": "done",
                "action_type": "final",
                "planner_protocol": "text_final",
                "steps": [],
            }
        normalized = self._normalize_planner_plan(plan_payload, scene=scene, mode=mode)
        # 二次护栏：如果 normalize 出来的 final_answer 本身看似 planner JSON（比如模型把整段
        # planner JSON 塞到 action.final_answer 字段里），也应该推到 synthesis 路径。
        if normalized.get("answer_ready") and self._looks_like_planner_json(str(normalized.get("final_answer") or "")):
            return {
                "scene": normalized.get("scene") or scene,
                "answer_ready": False,
                "final_answer": "",
                "reasoning_summary": "检测到 final_answer 字段依然是 planner JSON，转交后续综合生成。",
                "stop_reason": "planner_json_leak_guard",
                "action_type": "none",
                "planner_protocol": "leak_guard",
                "steps": [],
            }
        return normalized

    @staticmethod
    def _looks_like_planner_json(text: str) -> bool:
        """Heuristic：判断一段文本是否 “是 planner/agent 内部 JSON 而不是用户可见的 markdown 回答”。

        模型偶尔会把 planner 调度 JSON 当成文本返回（尤其在 final synthesis 阶段当上下
        文较长、提示词包含 planner 模板时）。如果被当作最终回答转发到 SSE，用户
        会看到一大段 {"scene":"...","action":{...}}。这里用轻量标记检测拦下，让外层转入
        synthesis / fallback 路径重新生成可读回答。
        """

        if not isinstance(text, str):
            return False
        stripped = text.strip()
        if not stripped or len(stripped) < 40:
            return False
        if not (stripped.startswith("{") or stripped.startswith("```")):
            return False
        sample = stripped[:4000]
        marker_groups = (
            ('"action"', ('"tool"', '"tool_name"', '"final_answer"', '"type"')),
            ('"scene"', ('"reasoning_summary"', '"action"', '"stop_reason"')),
            ('"answer_ready"', ('"final_answer"', '"steps"', '"action_type"')),
            ('"planner_protocol"', ('"steps"', '"action_type"')),
        )
        for primary, secondaries in marker_groups:
            if primary in sample and any(s in sample for s in secondaries):
                return True
        return False

    def _prepare_planner_executor_synthesis_payload(
        self,
        *,
        messages: List[dict],
        body: dict,
        model_name: str,
        planner_notes: List[dict],
        tool_observations: List[dict],
        agent_trace: Optional[Any] = None,
        limit_reached: bool = False,
    ) -> dict:
        provider = self._get_provider(model_name)
        payload = provider.filter_payload(body)
        payload["model"] = model_name or self._model_name_for_profile(self._default_agent_profile())
        payload["stream"] = False
        payload.pop("tools", None)
        payload.pop("tool_choice", None)

        synthesis_messages = deepcopy(messages or [])
        synthesis_messages.insert(0, {"role": "system", "content": AGENT_SYNTHESIS_SYSTEM_PROMPT})
        self._insert_agent_memory_profile_message(
            synthesis_messages,
            body.get("_xiamimate_memory_profile") if isinstance(body, dict) else None,
        )
        synthesis_context = self.agent_harness.synthesis_context(
            planner_notes,
            tool_observations,
            self._synthesis_observation_context,
            trace=agent_trace,
            limit_reached=limit_reached,
            answer_contract=self._answer_contract_from_messages(messages),
            followup_actionability_policy=self.agent_harness.followup_actionability_policy(),
        )
        synthesis_messages.append(
            {
                "role": "user",
                "content": json.dumps(synthesis_context, ensure_ascii=False, indent=2),
            }
        )
        payload["messages"] = synthesis_messages

        user_value = payload.get("user")
        if isinstance(user_value, dict):
            payload["user"] = self._user_id(body)
        return payload

    def _synthesize_planner_executor_answer(
        self,
        *,
        messages: List[dict],
        body: dict,
        model_name: str,
        planner_notes: List[dict],
        tool_observations: List[dict],
        agent_trace: Optional[Any] = None,
        limit_reached: bool = False,
    ) -> str:
        bypass = self._maybe_bypass_synthesis_with_rendered_opportunity_cards(
            messages=messages,
            tool_observations=tool_observations,
        )
        if bypass:
            return bypass
        payload = self._prepare_planner_executor_synthesis_payload(
            messages=messages,
            body=body,
            model_name=model_name,
            planner_notes=planner_notes,
            tool_observations=tool_observations,
            agent_trace=agent_trace,
            limit_reached=limit_reached,
        )
        response = self._post_agent_payload(payload, model_name=model_name)
        content = self._clean_agent_content(self._extract_assistant_content(response), model_name=model_name)
        # 合成阶段也可能返回 planner JSON（模型误把 synthesis prompt 当成了另一轮 planner）。
        # 这里统一拦下，避免将调度 JSON 作为最终回答吐给用户。
        if content and self._looks_like_planner_json(content):
            content = ""
        if content:
            return self._fallback_opportunity_answer_if_needed(
                content,
                tool_observations,
                answer_contract=self._answer_contract_from_messages(messages),
            )
        return self._fallback_answer_from_tool_observations(tool_observations)

    def _planner_plan_note(self, scene: str, plan: dict, trace: Optional[Any] = None) -> dict:
        return self.agent_harness.planner_plan_note(scene, plan, trace=trace)

    def _prepare_agent_final_synthesis_payload(
        self,
        *,
        conversation: List[dict],
        body: dict,
        mode: str,
        model_name: str,
    ) -> dict:
        synthesis_messages = deepcopy(conversation or [])
        synthesis_messages.append(
            {
                "role": "user",
                "content": (
                    "工具调用预算已用完。请停止调用任何工具，必须仅基于上面已经返回的工具结果，"
                    "直接回答用户的原始问题。\n"
                    "要求：结论前置；把工具事实、推理判断和证据边界分开；如果证据不足，明确说明哪些结论待验证；"
                    "不要输出内部工具调用标记，不要要求继续调用工具。"
                ),
            }
        )
        payload = self._prepare_agent_payload(messages=synthesis_messages, body=body, mode=mode, model_name=model_name)
        payload["stream"] = False
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        return payload

    def _final_answer_after_tool_round_limit(
        self,
        *,
        conversation: List[dict],
        body: dict,
        model_name: str,
        mode: str,
        tool_observations: List[dict],
    ) -> str:
        limit_message = "Agent 工具调用轮次达到上限，已停止继续调用工具并基于现有证据生成答复。"
        if not tool_observations:
            raise RuntimeError(limit_message)
        try:
            payload = self._prepare_agent_final_synthesis_payload(
                conversation=conversation,
                body=body,
                mode=mode,
                model_name=model_name,
            )
            response = self._post_agent_payload(payload, model_name=model_name)
            content = self._clean_agent_content(self._extract_assistant_content(response), model_name=model_name)
            if content:
                return self._fallback_opportunity_answer_if_needed(content, tool_observations)
        except RuntimeError as exc:
            limit_message = "%s 最终整理模型调用失败: %s" % (limit_message, str(exc))
        return self._fallback_answer_from_tool_observations(tool_observations, error=limit_message)

    def _provider_uses_native_tool_calls(self, model_name: str) -> bool:
        provider = self._get_provider(model_name)
        return "tools" in getattr(provider, "allowed_params", set())

    def _tool_call_cache_key(self, tool_call: Dict[str, Any]) -> Tuple[str, str]:
        name = str(tool_call.get("name") or "").strip()
        parameters = tool_call.get("parameters") if isinstance(tool_call.get("parameters"), dict) else {}
        return name, json.dumps(parameters, sort_keys=True, ensure_ascii=False, default=str)

    def _latest_tool_observation(self, tool_observations: List[dict], tool_name: str) -> Optional[dict]:
        normalized_name = str(tool_name or "").strip()
        for observation in reversed(tool_observations or []):
            if str(observation.get("tool_name") or "").strip() == normalized_name:
                return observation
        return None

    def _cached_tool_observation_for_call(
        self,
        *,
        tool_call: Dict[str, Any],
        tool_result_cache: Dict[Tuple[str, str], dict],
        tool_observations: List[dict],
    ) -> Optional[dict]:
        cache_key = self._tool_call_cache_key(tool_call)
        if cache_key in tool_result_cache:
            return tool_result_cache[cache_key]
        if str(tool_call.get("name") or "").strip() == "opportunity_discovery":
            return self._latest_tool_observation(tool_observations, "opportunity_discovery")
        return None

    def _filter_tool_calls_for_user_intent(self, tool_calls: List[Dict[str, Any]], messages: List[dict]) -> List[Dict[str, Any]]:
        if not tool_calls:
            return []
        if not any(str(call.get("name") or "").strip() == "opportunity_discovery" for call in tool_calls):
            return tool_calls
        if not self._looks_like_explicit_theme_analysis_request(self._extract_last_user_text(messages)):
            return tool_calls

        product_analysis_calls = [call for call in tool_calls if str(call.get("name") or "").strip() != "opportunity_discovery"]
        if any(self._is_product_theme_tool_call(call) for call in product_analysis_calls):
            return product_analysis_calls
        return tool_calls

    def _filter_assistant_message_tool_calls(self, assistant_message: dict, tool_calls: List[Dict[str, Any]]) -> dict:
        raw_tool_calls = assistant_message.get("tool_calls") if isinstance(assistant_message, dict) else None
        if not isinstance(raw_tool_calls, list):
            return assistant_message

        keep_ids = {
            str(call.get("tool_call_id"))
            for call in tool_calls or []
            if call.get("tool_call_id") not in (None, "")
        }
        updated = deepcopy(assistant_message)
        if not keep_ids:
            updated.pop("tool_calls", None)
            return updated
        filtered = [raw_call for raw_call in raw_tool_calls if str(raw_call.get("id") or "") in keep_ids]
        if filtered:
            updated["tool_calls"] = filtered
        else:
            updated.pop("tool_calls", None)
        return updated

    def _looks_like_blank_opportunity_discovery_request(self, text: str) -> bool:
        content = str(text or "").strip()
        if not content:
            return False
        blank_discovery_patterns = (
            r"不知道.*(?:选什么|分析什么|做什么)",
            r"(?:帮我|给我)?(?:找|发现|挖掘|推荐).{0,18}(?:机会|方向|品类|类目|关键词|商品|产品|选品)",
            r"(?:帮我|给我|先)?(?:选择|挑选|选).{0,18}(?:一个|几款|几种)?(?:商品|产品|选品)",
            r"(?:哪些|有什么|有哪些).{0,16}(?:机会|方向|细分|品类|类目|商品|产品)",
            r"(?:空白机会|机会发现|细分方向|商品推荐|产品推荐|选品推荐)",
        )
        return any(re.search(pattern, content, flags=re.IGNORECASE) for pattern in blank_discovery_patterns)

    def _looks_like_explicit_theme_analysis_request(self, text: str) -> bool:
        content = str(text or "").strip()
        if not content:
            return False

        lowered = content.lower()
        blank_discovery_patterns = (
            r"不知道.*(?:选什么|分析什么|做什么)",
            r"(?:帮我|给我)?(?:找|发现|挖掘|推荐).{0,12}(?:机会|方向|品类|类目|关键词)",
            r"(?:哪些|有什么|有哪些).{0,12}(?:机会|方向|细分)",
            r"(?:空白机会|机会发现|细分方向)",
        )
        if any(re.search(pattern, content, flags=re.IGNORECASE) for pattern in blank_discovery_patterns):
            return False

        explicit_patterns = (
            r"(?:评估|分析|判断|研究|看看|看一下|拆解)\s+[^，。！？\n]{2,90}?\s+在\s+[^，。！？\n]{2,80}?(?:机会|是否|值不值得|可行|空间|市场)",
            r"\b[a-z][a-z0-9][a-z0-9 &+\-/]{1,70}\b\s+在\s+[^，。！？\n]{2,80}?(?:机会|是否|值不值得|可行|空间|市场)",
            r"(?:评估|分析|判断|研究|拆解)\s+\b[a-z][a-z0-9][a-z0-9 &+\-/]{1,70}\b",
        )
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in explicit_patterns):
            return True

        return False

    def _is_product_theme_tool_call(self, tool_call: Dict[str, Any]) -> bool:
        name = str(tool_call.get("name") or "").strip()
        parameters = tool_call.get("parameters") if isinstance(tool_call.get("parameters"), dict) else {}
        product_theme_tools = {
            "resolve_candidates",
            "candidate_pool_stats",
            "candidate_pool_slice",
            "candidate_pool_trends",
            "candidate_pool_weak_forecast",
            "product_forecast_explain",
            "top_asin_drilldown",
            "asin_history_timeseries",
            "asin_review_insights",
            "amazon_keyword_demand",
            "category_benchmark",
            "launch_budget_calculator",
        }
        if name not in product_theme_tools:
            return False
        evidence_keys = {
            "product_query",
            "product_theme",
            "candidate_pool_id",
            "candidate_asins",
            "asins",
            "category_id",
            "category_path",
            "benchmark_category_id",
            "benchmark_category_path",
        }
        return any(parameters.get(key) not in (None, "", [], {}) for key in evidence_keys)

    def _attach_internal_tool_context(self, tool_call: Dict[str, Any], body: dict) -> Dict[str, Any]:
        if str(tool_call.get("name") or "").strip() != "opportunity_discovery":
            return tool_call
        memory_profile = body.get("_xiamimate_memory_profile") if isinstance(body, dict) else None
        if not isinstance(memory_profile, dict) or not memory_profile:
            return tool_call
        updated = deepcopy(tool_call)
        parameters = dict(updated.get("parameters") or {})
        parameters["_memory_profile"] = memory_profile
        updated["parameters"] = parameters
        return updated

    def _strip_internal_tool_context(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        updated = deepcopy(tool_call)
        parameters = {
            key: value
            for key, value in dict(updated.get("parameters") or {}).items()
            if not str(key).startswith("_")
        }
        updated["parameters"] = parameters
        return updated

    def _run_agent_loop(
        self,
        messages: List[dict],
        body: dict,
        billing_context: dict,
        model_name: str,
        mode: str = "agent",
        charge_llm: bool = True,
    ) -> str:
        source_messages = deepcopy(messages or [])
        used_tools = False
        scene = self._classify_agent_scene(source_messages, mode=mode)
        react_runner = self.agent_harness.new_react_runner(mode=mode, scene=scene)
        agent_trace = react_runner.trace
        tool_store = react_runner.observation_store
        tool_observations: List[dict] = tool_store.observations
        tool_result_cache: Dict[Tuple[str, str], dict] = tool_store.tool_result_cache
        planner_notes: List[dict] = react_runner.planner_notes
        max_rounds = min(self._agent_max_tool_rounds(), int(self._scene_policy(scene, mode).get("max_rounds") or 1))
        react_runner.start(max_rounds=max_rounds)

        def persist_trace(status: str = "finished", answer_text: str = "") -> None:
            trace_extra = {"tool_count": len(tool_observations), "planner_note_count": len(planner_notes), "stream": False}
            grader_result = self._grade_agent_answer_for_trace(source_messages, answer_text, tool_observations, agent_trace)
            if grader_result.get("status") != "skipped":
                trace_extra["grader_result"] = grader_result
            self._persist_agent_trace(
                agent_trace,
                status=status,
                extra=trace_extra,
            )

        for round_index in range(max_rounds):
            try:
                plan = self._plan_agent_next_steps(
                    messages=source_messages,
                    body=body,
                    model_name=model_name,
                    mode=mode,
                    scene=scene,
                    tool_observations=tool_observations,
                    remaining_rounds=max_rounds - round_index,
                )
            except RuntimeError as exc:
                if tool_observations:
                    answer = self._fallback_answer_from_tool_observations(tool_observations, error=str(exc))
                    persist_trace(status="error", answer_text=answer)
                    return answer
                persist_trace(status="error")
                raise

            scene = str(plan.get("scene") or scene or "general_agent").strip() or "general_agent"
            explicit_tool_name = ""
            if not tool_observations:
                explicit_tool_name = self._explicit_tool_name_from_text(self._extract_last_user_text(source_messages))
                if explicit_tool_name:
                    scene = self._scene_for_explicit_tool(explicit_tool_name, scene)
            react_runner.plan_note(scene, plan)

            if not explicit_tool_name and plan.get("answer_ready") and str(plan.get("final_answer") or "").strip():
                if charge_llm and mode == "agent" and not used_tools:
                    self._charge_standalone_llm_request(
                        billing_context=billing_context,
                        payload={"model": model_name, "messages": source_messages},
                        mode=mode,
                        stream=False,
                    )
                react_runner.final(scene, status="planner_final")
                answer = self._fallback_opportunity_answer_if_needed(
                    str(plan.get("final_answer") or "").strip(),
                    tool_observations,
                    answer_contract=self._answer_contract_from_messages(source_messages),
                )
                persist_trace(answer_text=answer)
                return answer

            steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
            if explicit_tool_name and not tool_observations:
                matching_explicit_steps = [
                    step
                    for step in steps
                    if str(((step or {}).get("tool_call") or {}).get("name") or "").strip() == explicit_tool_name
                ]
                if matching_explicit_steps:
                    steps = [matching_explicit_steps[0]]
                else:
                    explicit_step = self._explicit_tool_request_step(source_messages, body, scene, mode=mode)
                    if explicit_step is not None:
                        steps = [explicit_step]
            if plan.get("planner_protocol") != "native_tool_calls" and not (explicit_tool_name and explicit_tool_name != "resolve_candidates"):
                steps = self._enforce_theme_resolve_first_step(steps, scene, source_messages, body, tool_observations)
            steps = self._repair_planner_steps_required_arguments(steps, source_messages, body)
            steps = self._filter_redundant_planner_steps(steps, scene, tool_observations)
            react_runner.validation(scene, steps)
            if not steps:
                if not tool_observations and str(plan.get("action_type") or "").strip() in {"", "none"}:
                    answer = self._fallback_answer_from_tool_observations(tool_observations)
                    persist_trace(answer_text=answer)
                    return answer
                opportunity_fallback = self._fallback_opportunity_answer_from_observations(tool_observations)
                if opportunity_fallback:
                    persist_trace(answer_text=opportunity_fallback)
                    return opportunity_fallback
                answer = self._synthesize_planner_executor_answer(
                    messages=source_messages,
                    body=body,
                    model_name=model_name,
                    planner_notes=planner_notes,
                    tool_observations=tool_observations,
                    agent_trace=agent_trace,
                )
                if charge_llm and mode == "agent" and not used_tools:
                    self._charge_standalone_llm_request(
                        billing_context=billing_context,
                        payload={"model": model_name, "messages": source_messages},
                        mode=mode,
                        stream=False,
                    )
                persist_trace(answer_text=answer)
                return answer

            used_tools = True
            executed_any = False
            for step in steps:
                tool_call = self._attach_internal_tool_context((step or {}).get("tool_call") or {}, body)
                cached_observation = self._cached_tool_observation_for_call(
                    tool_call=tool_call,
                    tool_result_cache=tool_result_cache,
                    tool_observations=tool_observations,
                )
                if cached_observation is not None:
                    executed_any = True
                    continue

                public_tool_call = self._strip_internal_tool_context(tool_call)
                result = self._execute_tool_call(tool_call, billing_context, truncate=False)
                observation = self._build_tool_observation(tool_call=public_tool_call, result=result)
                react_runner.observation(
                    scene,
                    str(tool_call.get("name") or "").strip(),
                    "error" if self._tool_result_has_error(result) else "ok",
                    observation=observation,
                    cache_key=self._tool_call_cache_key(tool_call),
                )
                executed_any = True

            if not executed_any:
                break

        if tool_observations:
            answer = self._synthesize_planner_executor_answer(
                messages=source_messages,
                body=body,
                model_name=model_name,
                planner_notes=planner_notes,
                tool_observations=tool_observations,
                agent_trace=agent_trace,
                limit_reached=True,
            )
            persist_trace(answer_text=answer)
            return answer

        fallback_answer = self._synthesize_planner_executor_answer(
            messages=source_messages,
            body=body,
            model_name=model_name,
            planner_notes=planner_notes,
            tool_observations=tool_observations,
            agent_trace=agent_trace,
            limit_reached=True,
        )
        if charge_llm and mode == "agent" and not used_tools:
            self._charge_standalone_llm_request(
                billing_context=billing_context,
                payload={"model": model_name, "messages": source_messages},
                mode=mode,
                stream=False,
            )
        persist_trace(answer_text=fallback_answer)
        return fallback_answer

    def _charge_standalone_llm_request(
        self,
        billing_context: dict,
        payload: dict,
        mode: str,
        stream: bool,
    ) -> dict:
        return self._charge_billing_event(
            billing_context=billing_context,
            event_type="llm_request",
            description="LLM 请求",
            meta={
                "mode": mode,
                "model": payload.get("model"),
                "message_count": len(payload.get("messages") or []),
                "stream": bool(stream),
                "billing_scope": "standalone_llm_only",
            },
        )

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

    def _build_agent_memory_profile_context(
        self,
        *,
        messages: List[dict],
        body: dict,
        billing_context: dict,
        mode: str,
    ) -> Optional[dict]:
        if mode not in {"agent", "tool"}:
            return None
        query = (self._extract_last_user_text(messages) or "").strip()
        if not query:
            return None
        try:
            data = self._chat_backend_request(
                method="POST",
                path="/internal/provider/memory-profile/build",
                body={
                    "user_id": billing_context.get("user_id") or self._user_id(body),
                    "query": query,
                    "target_platform": self._body_context_value(body, "target_platform"),
                    "target_market": self._body_context_value(body, "target_market") or self._body_context_value(body, "marketplace"),
                    "report_profile": "research",
                },
                internal=True,
                timeout=12,
            )
        except RuntimeError as exc:
            print("xiamimate memory profile build failed", str(exc)[:500])
            return None
        return self._compact_memory_profile_context(data)

    def _body_context_value(self, body: dict, key: str) -> str:
        if not isinstance(body, dict):
            return ""
        candidates = [body.get(key)]
        metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        candidates.append(metadata.get(key))
        extra = body.get("extra_body") if isinstance(body.get("extra_body"), dict) else {}
        candidates.append(extra.get(key))
        for value in candidates:
            text = str(value or "").strip()
            if text:
                return text[:120]
        return ""

    def _compact_memory_profile_context(self, data: dict) -> dict:
        if not isinstance(data, dict):
            return {}
        keys = [
            "summary_version",
            "user_identity_summary",
            "role_hint",
            "market_focus",
            "preferred_platforms",
            "preferred_price_band",
            "risk_preference",
            "decision_style",
            "hard_constraints",
            "recent_topics",
            "memory_confidence",
            "evidence_sources",
            "confidence_digest",
        ]
        compact = self._copy_keys(data, keys)
        if "summary_version" not in compact:
            compact["summary_version"] = "memory_profile_v1"
        return self._compact_json_value(
            compact,
            max_depth=4,
            max_items=8,
            max_scalar_items=32,
            max_string=240,
        )

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

    def _post_llm_direct(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict,
        timeout: int,
    ) -> dict:
        # 直连 OpenAI 兼容 /chat/completions，绕开 chat-backend 的单模型绑定。
        # 用于 planner / title translator 这类小副线，允许在不污染主 agent 配置的前提下
        # 切换到独立的轻量模型。
        normalized = (base_url or "").rstrip("/")
        url = normalized if normalized.endswith("/chat/completions") else f"{normalized}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = requests.post(url, headers=headers, json=payload, timeout=max(1, int(timeout)))
        response.raise_for_status()
        return response.json()

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
                "mode": "recall_mode",
                "recall": "recall_mode",
                "categoryId": "category_id",
                "category_id": "category_id",
                "path": "category_path",
                "full_path": "category_path",
                "descendants": "include_descendants",
                "include_child_categories": "include_descendants",
                "min_candidates": "min_pool_size",
                "target_candidates": "target_pool_size",
                "market": "marketplace",
                "target_market": "marketplace",
                "target_market_norm": "marketplace",
            },
            "category_resolve": {
                "query": "category_query",
                "category": "category_query",
                "category_name": "category_query",
                "path": "category_path",
                "full_path": "category_path",
                "market": "marketplace",
                "target_market": "marketplace",
                "target_market_norm": "marketplace",
                "top_k": "max_matches",
                "top_n": "max_matches",
                "max_results": "max_matches",
            },
            "expand_candidates": {
                "query": "product_query",
                "category": "product_query",
                "keywords": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "mode": "recall_mode",
                "recall": "recall_mode",
                "categoryId": "category_id",
                "category_id": "category_id",
                "path": "category_path",
                "full_path": "category_path",
                "descendants": "include_descendants",
                "include_child_categories": "include_descendants",
                "target_candidates": "target_asin_count",
                "target_pool_size": "target_asin_count",
                "min_candidates": "min_pool_size",
                "market": "marketplace",
                "target_market": "marketplace",
                "target_market_norm": "marketplace",
                "session_id": "requested_by_session_id",
                "request_id": "idempotency_key",
            },
            "candidate_expansion_status": {
                "id": "job_id",
                "job": "job_id",
                "jobId": "job_id",
                "market": "marketplace",
                "target_market": "marketplace",
                "target_market_norm": "marketplace",
                "status": "statuses",
                "state": "statuses",
                "max_results": "limit",
                "top_k": "limit",
            },
            "opportunity_discovery": {
                "category": "category_path",
                "category_id": "category_id",
                "categoryId": "category_id",
                "path": "category_path",
                "full_path": "category_path",
                "market": "marketplace",
                "target_market": "marketplace",
                "target_market_norm": "marketplace",
                "top_k": "limit",
                "top_n": "limit",
                "max_results": "limit",
                "confidence": "min_data_confidence",
                "min_confidence": "min_data_confidence",
                "descendants": "include_descendants",
                "include_child_categories": "include_descendants",
            },
            "candidate_pool_stats": {
                "pool_id": "candidate_pool_id",
                "candidatePoolId": "candidate_pool_id",
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
            "candidate_pool_slice": {
                "pool_id": "candidate_pool_id",
                "candidatePoolId": "candidate_pool_id",
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
                "query": "product_query",
                "category": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "market": "marketplace",
                "brand": "brand_include",
                "brands": "brand_include",
                "brand_names": "brand_include",
                "title_keyword": "title_keywords",
                "title_keyword_include": "title_keywords",
                "keyword": "title_keywords",
                "keywords": "title_keywords",
                "material": "material_keywords",
                "materials": "material_keywords",
                "material_keyword": "material_keywords",
                "min_price": "price_min",
                "price_floor": "price_min",
                "price_low": "price_min",
                "price_from": "price_min",
                "max_price": "price_max",
                "price_ceiling": "price_max",
                "price_high": "price_max",
                "price_to": "price_max",
                "top_k": "top_n",
                "max_results": "top_n",
            },
            "candidate_pool_trends": {
                "pool_id": "candidate_pool_id",
                "candidatePoolId": "candidate_pool_id",
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
                "pool_id": "candidate_pool_id",
                "candidatePoolId": "candidate_pool_id",
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
            "product_forecast_explain": {
                "pool_id": "candidate_pool_id",
                "candidatePoolId": "candidate_pool_id",
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
                "query": "product_query",
                "category": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "market": "marketplace",
                "top_k": "top_n",
                "max_results": "top_n",
            },
            "launch_budget_calculator": {
                "query": "product_theme",
                "category": "product_theme",
                "product": "product_theme",
                "product_query": "product_theme",
                "theme": "product_theme",
                "market": "marketplace",
                "target_market": "marketplace",
                "price": "selling_price",
                "sellingPrice": "selling_price",
                "unit_cost": "unit_product_cost",
                "product_cost": "unit_product_cost",
                "landed_cost": "landed_cost_per_unit",
                "landed_cost_per_unit": "landed_cost_per_unit",
                "shipping": "inbound_shipping_per_unit",
                "inbound_shipping": "inbound_shipping_per_unit",
                "duty": "duty_per_unit",
                "referral_rate": "referral_fee_rate",
                "commission_rate": "referral_fee_rate",
                "coupon_rate": "coupon_discount_rate",
                "promo_rate": "coupon_discount_rate",
                "refund_rate": "return_rate",
                "returns_rate": "return_rate",
                "ad_budget": "monthly_ad_budget",
                "monthly_ads": "monthly_ad_budget",
                "units": "launch_units",
                "inventory_units": "launch_units",
                "months": "launch_months",
                "runway_months": "launch_months",
            },
            "top_asin_drilldown": {
                "pool_id": "candidate_pool_id",
                "candidatePoolId": "candidate_pool_id",
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
            "asin_review_insights": {
                "pool_id": "candidate_pool_id",
                "candidatePoolId": "candidate_pool_id",
                "asin": "candidate_asins",
                "asins": "candidate_asins",
                "asin_list": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "query": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "market": "marketplace",
                "top_k": "max_asins",
                "top_n": "max_asins",
                "max_results": "max_asins",
            },
            "amazon_keyword_demand": {
                "query": "product_query",
                "product": "product_query",
                "product_keyword": "product_query",
                "keyword": "keywords",
                "keyword_list": "keywords",
                "search_terms": "keywords",
                "market": "marketplace",
                "target_market": "marketplace",
                "target_market_norm": "marketplace",
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
                "pool_id": "candidate_pool_id",
                "candidatePoolId": "candidate_pool_id",
                "asins": "candidate_asins",
                "candidate_list": "candidate_asins",
                "candidate_pool": "candidate_asins",
                "asin_list": "candidate_asins",
                "query": "product_query",
                "category": "product_query",
                "category_id": "benchmark_category_id",
                "categoryId": "benchmark_category_id",
                "benchmark_category": "benchmark_category_path",
                "category_path": "benchmark_category_path",
                "path": "benchmark_category_path",
                "full_path": "benchmark_category_path",
                "level": "benchmark_level",
                "descendants": "include_descendants",
                "include_child_categories": "include_descendants",
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
        if tool_name == "launch_budget_calculator":
            for rate_param in ("referral_fee_rate", "coupon_discount_rate", "return_rate"):
                if rate_param in filtered_params:
                    filtered_params[rate_param] = self._normalize_fraction_rate_value(filtered_params[rate_param])

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

    def _normalize_fraction_rate_value(self, value: Any) -> Any:
        if value in (None, ""):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return value
            has_percent = "%" in text or "percent" in text.lower()
            numeric_text = re.sub(r"[^0-9.\-]+", "", text)
            if not numeric_text:
                return value
            try:
                number = float(numeric_text)
            except ValueError:
                return value
            if has_percent or number > 1:
                return number / 100.0
            return number
        if isinstance(value, (int, float)) and value > 1:
            return float(value) / 100.0
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
        # 防御性剥离：force_refresh 等是去重控制标记（通常已在 filter 阶段移除），
        # 绝不能作为关键字参数传给真实工具方法，否则 method(**parameters) 会因未知参数报 TypeError。
        if isinstance(parameters, dict) and any(
            k in parameters for k in ("force_refresh", "_force_refresh", "_refresh", "refresh")
        ):
            parameters = {
                k: v
                for k, v in parameters.items()
                if k not in ("force_refresh", "_force_refresh", "_refresh", "refresh")
            }
        public_parameters = {
            key: value
            for key, value in dict(parameters or {}).items()
            if not str(key).startswith("_")
        }
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
                        "parameters": public_parameters,
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
            return "%s\n\n<internal_only 已截断，仅保留前 12000/%d 字符；这是内部格式标记，勿向用户复述压缩/截断状态>" % (
                result_text[:12000],
                len(result_text),
            )
        return result_text

    def _build_tool_observation(self, tool_call: Dict[str, Any], result: str) -> dict:
        tool_name = str(tool_call.get("name") or "")
        raw_text = str(result or "")
        translations: Dict[str, str] = {}
        if tool_name.strip() == "opportunity_discovery" and raw_text and not self._tool_result_has_error(raw_text):
            enriched, translations = self._enrich_opportunity_discovery_raw_result(raw_text)
            if enriched is not None:
                raw_text = enriched
        observation = {
            "tool_name": tool_name,
            "arguments": tool_call.get("parameters") or {},
            "raw_result": raw_text,
            "llm_result": self._format_tool_result_for_llm(tool_name=tool_name, result=raw_text),
        }
        if translations:
            observation["title_translations"] = translations
        try:
            if not self._tool_result_has_error(observation["raw_result"]):
                self.agent_harness.after_tool_observation(
                    tool_call=tool_call,
                    result=observation["raw_result"],
                    compact_result=observation["llm_result"],
                )
        except Exception as exc:  # noqa: BLE001
            print("xiamimate.agent session-context update failed:", repr(exc))
        return observation

    def _format_tool_result_for_llm(self, tool_name: str, result: str, budget: int = 14000) -> str:
        result_text = str(result or "").strip()
        if not result_text:
            return "工具返回为空。"
        if self._tool_result_has_error(result_text):
            return result_text[:budget]

        payload = self._load_tool_json_payload(result_text)
        if payload is None:
            return self._truncate_text_for_llm(result_text, budget=budget)

        if str(tool_name or "").strip() == "opportunity_discovery":
            return self._format_opportunity_discovery_result_for_llm(payload=payload, original_chars=len(result_text), budget=budget)

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
            "compaction_note": "internal_only: 这是工具结果的内部精简视图。直接使用其中已展示的数值与计数作答；切勿向用户复述压缩/截断/缓存等内部状态，也不要因为这里看不到某个字段就对用户说“数据因压缩未能展示/未能完整返回”。如确需缺失字段，请改用其它已返回字段或省略该点。",
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

    def _format_opportunity_discovery_result_for_llm(self, payload: dict, original_chars: int, budget: int) -> str:
        opportunity_payload = self._extract_opportunity_payload(payload)
        opportunity_count = self._opportunity_count(opportunity_payload)
        cards_text = str(opportunity_payload.get("opportunity_cards_text") or "").strip()

        compact_payload = self._copy_keys(
            opportunity_payload,
            [
                "instruction",
                "opportunity_discovery_job_id",
                "opportunity_discovery_job",
                "result_ref",
                "opportunity_count",
                "opportunity_cards_text",
                "opportunities_for_llm",
                "field_formula_details",
                "llm_summary_guidance",
                "metric_definitions",
                "display_rules",
                "tool_contract",
                "evidence_contract",
                "diagnostics",
                "notes",
                "meta",
            ],
        )
        compact_payload["opportunity_count"] = opportunity_count
        if cards_text:
            compact_payload["opportunity_cards_text"] = cards_text
        if "opportunities_for_llm" in compact_payload:
            compact_payload["opportunities_for_llm"] = self._compact_opportunities_for_llm(
                compact_payload.get("opportunities_for_llm"), max_items=12, max_string=180
            )
        if "metric_definitions" in compact_payload:
            compact_payload["metric_definitions"] = self._compact_json_value(
                compact_payload["metric_definitions"],
                max_depth=4,
                max_items=12,
                max_scalar_items=30,
                max_string=360,
            )

        envelope = {
            "tool_name": "opportunity_discovery",
            "result_format": "opportunity_evidence_block",
            "original_chars": original_chars,
            "instruction": (
                "opportunity_cards_text 是机会发现的工具证据来源，不应替代主答案。"
                "opportunities_for_llm 包含可继续分析的结构化机会入口。"
                "最终答复可自行组织标题、摘要和解读；如果用户明确要求 topN 机会卡片或逐卡分析，应按用户请求数量裁剪并重组为卡片式解读。"
                "主答案结构固定为：① 一句话总览（市场/平台/返回机会数）；② 一张精简排名表（Markdown 表格，表头 `| 排名 | 机会主题 | 类目路径 | 机会得分 |`，行数等于用户请求数量，类目路径过长可省中间层但保留 leaf）；③ 然后按 `### 机会 N：<名称>` 模板逐卡展开，每卡包含机会理由、关键证据、风险/证据边界和下一步验证。"
                "不要展示超过用户请求数量的机会，不要丢列、改数值或补未返回的数值，不得省略排名表也不得只给排名表而省略逐卡解说；同时必须保留 llm_summary_guidance/display_rules 中要求的同名隐藏提示、个性化分、趋势状态和 category_resolve 前置提醒。"
                "排名表中的『机会主题』列和每张卡片标题都必须使用『中文翻译（English 原文）』双语形式（例如『真空保温杯（Tumblers）』『车载吸尘器（Car Vacuum）』）；中文翻译需准确反映品类含义，不可省略或仅保留英文。若原文本身已是中文或专有名词，则只保留原文。"
            ),
            "payload": compact_payload,
        }
        rendered = json.dumps(envelope, ensure_ascii=False, indent=2)
        if len(rendered) <= budget:
            return rendered

        compact_payload.pop("diagnostics", None)
        compact_payload.pop("meta", None)
        compact_payload.pop("notes", None)
        rendered = json.dumps(envelope, ensure_ascii=False, indent=2)
        if len(rendered) <= budget:
            return rendered

        compact_payload.pop("tool_contract", None)
        compact_payload.pop("evidence_contract", None)
        rendered = json.dumps(envelope, ensure_ascii=False, indent=2)
        if len(rendered) <= budget:
            return rendered

        if "opportunities_for_llm" in compact_payload:
            compact_payload["opportunities_for_llm"] = self._compact_opportunities_for_llm(
                compact_payload.get("opportunities_for_llm"), max_items=10, max_string=90
            )
        rendered = json.dumps(envelope, ensure_ascii=False, indent=2)
        if len(rendered) <= budget:
            return rendered

        if cards_text:
            compact_payload["opportunity_cards_text"] = self._trim_opportunity_cards_text(
                cards_text,
                budget=max(1200, budget - 3200),
            )
            compact_payload["overflow_note"] = "机会表文本过长，当前内容保留工具返回文本的前段和结构化机会入口。"
        rendered = json.dumps(envelope, ensure_ascii=False, indent=2)
        if len(rendered) <= budget:
            return rendered

        compact_payload.pop("metric_definitions", None)
        compact_payload.pop("display_rules", None)
        rendered = json.dumps(envelope, ensure_ascii=False, indent=2)
        if len(rendered) <= budget:
            return rendered

        if "opportunities_for_llm" in compact_payload:
            compact_payload["opportunities_for_llm"] = self._compact_opportunities_for_llm(
                compact_payload.get("opportunities_for_llm"), max_items=10, max_string=60
            )
        rendered = json.dumps(envelope, ensure_ascii=False, indent=2)
        if len(rendered) <= budget:
            return rendered

        rendered = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= budget:
            return rendered

        if cards_text:
            compact_payload["opportunity_cards_text"] = self._trim_opportunity_cards_text(cards_text, budget=1600)
        rendered = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= budget:
            return rendered

        compact_payload.pop("opportunity_cards_text", None)
        compact_payload["overflow_note"] = "机会表文本过长，本次压缩保留可执行 opportunities_for_llm。"
        rendered = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= budget:
            return rendered

        compact_payload.pop("opportunities_for_llm", None)
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))

    def _extract_opportunity_payload(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {}
        if any(key in payload for key in ("opportunity_cards_text", "opportunities_for_llm", "opportunities")):
            return payload
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if data is not None:
            if any(key in data for key in ("opportunity_cards_text", "opportunities_for_llm", "opportunities")):
                return data
            result = data.get("result")
            nested = self._load_tool_json_payload(result) if isinstance(result, str) else None
            if nested is not None:
                return self._extract_opportunity_payload(nested)
        result = payload.get("result")
        nested = self._load_tool_json_payload(result) if isinstance(result, str) else None
        if nested is not None:
            return self._extract_opportunity_payload(nested)
        return payload

    def _opportunity_count(self, payload: dict) -> int:
        raw_count = payload.get("opportunity_count") if isinstance(payload, dict) else None
        try:
            return int(raw_count)
        except (TypeError, ValueError):
            pass
        for key in ("opportunities_for_llm", "opportunities"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, list):
                return len(value)
        return 0

    def _compact_opportunities_for_llm(self, opportunities: Any, *, max_items: int, max_string: int) -> Any:
        if not isinstance(opportunities, list):
            return opportunities
        compact_items = []
        for item in opportunities[:max_items]:
            if not isinstance(item, dict):
                compact_items.append(self._compact_leaf_value(item, max_string=max_string))
                continue
            compact_items.append(
                self._compact_json_value(
                    self._copy_keys(
                        item,
                        [
                            "rank",
                            "opportunity_id",
                            "title",
                            "title_zh",
                            "source",
                            "category_id",
                            "category_path",
                            "candidate_pool_id",
                            "opportunity_score",
                            "sales_window_sum",
                            "candidate_count",
                            "row_count",
                            "sales_momentum_pct",
                            "trend_momentum_pct",
                            "offer_count_avg",
                            "data_confidence",
                            "next_action",
                            "metric_explanations",
                        ],
                    ),
                    max_depth=4,
                    max_items=8,
                    max_scalar_items=18,
                    max_string=max_string,
                )
            )
        if len(opportunities) > max_items:
            compact_items.append({"_omitted_items": len(opportunities) - max_items, "_total_items": len(opportunities)})
        return compact_items

    def _trim_opportunity_cards_text(self, text: str, budget: int) -> str:
        clean_text = str(text or "").strip()
        if len(clean_text) <= budget:
            return clean_text
        lines = clean_text.splitlines()
        kept_lines: List[str] = []
        used = 0
        for line in lines:
            next_used = used + len(line) + 1
            if next_used > budget:
                break
            kept_lines.append(line)
            used = next_used
        kept_lines.append("[后续工具文本过长已省略]")
        return "\n".join(kept_lines)

    def _fallback_opportunity_answer_if_needed(
        self,
        answer: str,
        tool_observations: List[dict],
        answer_contract: Optional[dict] = None,
    ) -> str:
        if (
            not self._answer_has_invalid_opportunity_expansion(answer)
            and self._opportunity_answer_matches_observations(answer, tool_observations)
            and self._opportunity_answer_satisfies_contract(answer, answer_contract, tool_observations)
        ):
            return answer
        fallback = self._fallback_opportunity_answer_from_observations(tool_observations, answer_contract=answer_contract)
        return fallback or answer

    def _opportunity_answer_satisfies_contract(
        self,
        answer: str,
        answer_contract: Optional[dict],
        tool_observations: List[dict],
    ) -> bool:
        if not answer_contract or answer_contract.get("entity_type") != "opportunity_card":
            return True
        if not any(str(observation.get("tool_name") or "") == "opportunity_discovery" for observation in tool_observations or []):
            return True
        result = self.agent_harness.grade_answer(
            answer_text=answer,
            answer_contract=answer_contract,
            tool_observations=tool_observations,
        )
        return str(result.get("status") or "") in {"pass", "skipped"}

    def _opportunity_answer_matches_observations(self, answer: str, tool_observations: List[dict]) -> bool:
        titles = self._opportunity_titles_from_observations(tool_observations)
        if not titles:
            return True
        answer_text = str(answer or "").lower()
        matched_count = 0
        for title in titles[:10]:
            normalized_title = str(title or "").strip().lower()
            if len(normalized_title) < 3:
                continue
            if normalized_title in answer_text:
                matched_count += 1
        return matched_count >= min(3, max(1, len(titles)))

    def _maybe_bypass_synthesis_with_rendered_opportunity_cards(
        self,
        *,
        messages: List[dict],
        tool_observations: List[dict],
    ) -> str:
        # 机会卡片场景下，最后一轮 opportunity_discovery 的结构化输出（含 title_zh）
        # 已经由 renderer 拼装成符合 answer_contract 的最终答复，再过一次 synthesis
        # LLM 既慢又有改写丢列/丢中文的风险。这里只做结构判断，不做内容打分：
        # ① 总开关开启；② answer_contract 是 opportunity_card；③ 最后一个工具调用
        # 就是 opportunity_discovery；④ 该 observation 携带成功的结构化 payload
        # （opportunity_cards_text 非空 + opportunities_for_llm 非空）。任一不满足
        # 即回退到原 synthesis 路径。
        if not bool(getattr(self.valves, "AGENT_OPPORTUNITY_BYPASS_SYNTHESIS", True)):
            return ""
        contract = self._answer_contract_from_messages(messages)
        if not isinstance(contract, dict) or contract.get("entity_type") != "opportunity_card":
            return ""
        if not tool_observations:
            return ""
        last_observation = tool_observations[-1] or {}
        if str(last_observation.get("tool_name") or "") != "opportunity_discovery":
            return ""
        payload = None
        for result_key in ("raw_result", "llm_result"):
            payload = self._load_tool_json_payload(last_observation.get(result_key))
            if payload is not None:
                break
        if not isinstance(payload, dict):
            return ""
        if payload.get("success") is False:
            return ""
        opportunity_payload = self._extract_opportunity_payload(
            payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        )
        if not isinstance(opportunity_payload, dict):
            return ""
        cards_text = str(opportunity_payload.get("opportunity_cards_text") or "").strip()
        if len(cards_text) < 200:
            return ""
        items = opportunity_payload.get("opportunities_for_llm")
        if not isinstance(items, list) or not items:
            return ""
        rendered = self._render_opportunity_cards_from_payload(
            opportunity_payload, last_observation, answer_contract=contract
        )
        return rendered or ""

    def _opportunity_titles_from_observations(self, tool_observations: List[dict]) -> List[str]:
        for observation in reversed(tool_observations or []):
            if str(observation.get("tool_name") or "") != "opportunity_discovery":
                continue
            for result_key in ("raw_result", "llm_result"):
                payload = self._load_tool_json_payload(observation.get(result_key))
                if payload is None:
                    continue
                opportunity_payload = self._extract_opportunity_payload(payload.get("payload") if isinstance(payload.get("payload"), dict) else payload)
                titles: List[str] = []
                for key in ("opportunities_for_llm", "opportunities"):
                    items = opportunity_payload.get(key) if isinstance(opportunity_payload, dict) else None
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if isinstance(item, dict) and str(item.get("title") or "").strip():
                            titles.append(str(item.get("title")).strip())
                return titles
        return []

    def _answer_has_invalid_opportunity_expansion(self, answer: str) -> bool:
        text = str(answer or "")
        if not text:
            return False
        markers = ("待补全", "待补充", "估算区间", "基于机会得分分布", "结果压缩截断")
        return any(marker in text for marker in markers)

    def _fallback_opportunity_answer_from_observations(
        self,
        tool_observations: List[dict],
        answer_contract: Optional[dict] = None,
    ) -> str:
        for observation in reversed(tool_observations or []):
            if str(observation.get("tool_name") or "") != "opportunity_discovery":
                continue
            for result_key in ("llm_result", "raw_result"):
                payload = self._load_tool_json_payload(observation.get(result_key))
                if payload is None:
                    continue
                opportunity_payload = self._extract_opportunity_payload(payload.get("payload") if isinstance(payload.get("payload"), dict) else payload)
                cards_text = str(opportunity_payload.get("opportunity_cards_text") or "").strip()
                if not cards_text:
                    continue
                structured_answer = self._render_opportunity_cards_from_payload(opportunity_payload, observation, answer_contract=answer_contract)
                if structured_answer:
                    return structured_answer
                opportunity_count = self._opportunity_count(opportunity_payload)
                lines = [
                    "下面是本次机会发现返回的机会卡片。",
                    "",
                    cards_text,
                ]
                if opportunity_count:
                    lines.extend(
                        [
                            "",
                            "当前可继续分析的机会编号共有 %d 个。" % opportunity_count,
                        ]
                    )
                return "\n".join(lines)
        return ""

    def _render_opportunity_cards_from_payload(
        self,
        opportunity_payload: dict,
        observation: dict,
        answer_contract: Optional[dict] = None,
    ) -> str:
        opportunities = opportunity_payload.get("opportunities_for_llm")
        if not isinstance(opportunities, list) or not opportunities:
            opportunities = opportunity_payload.get("opportunities")
        if not isinstance(opportunities, list) or not opportunities:
            return ""
        requested_count = self._requested_opportunity_count(observation, opportunity_payload, answer_contract)
        selected = [item for item in opportunities if isinstance(item, dict)][:requested_count]
        if not selected:
            return ""
        total_count = self._opportunity_count(opportunity_payload) or len(selected)
        marketplace = self._opportunity_marketplace(observation, opportunity_payload)
        translations = (observation or {}).get("title_translations") if isinstance(observation, dict) else None
        if not isinstance(translations, dict):
            translations = None
        lines = [
            "下面是本次机会发现返回的机会卡片。",
            "",
            "市场: %s | 平台: Amazon | 实际返回机会数: %d" % (marketplace, len(selected)),
            "",
            "| 排名 | 机会主题 | 类目路径 | 机会得分 |",
            "| ---: | --- | --- | ---: |",
        ]
        for index, item in enumerate(selected, start=1):
            rank = item.get("rank") or index
            display_title = self._opportunity_bilingual_title(item, translations)
            if not display_title:
                display_title = "机会 %d" % index
            short_path = self._opportunity_short_category_path(self._first_present(item, "category_path", "category", "leaf_category_name"))
            score_value = self._first_present(item, "opportunity_score", "score", "personalized_opportunity_score")
            score_text = self._format_metric_value(score_value) if score_value is not None else "—"
            lines.append("| %s | %s | %s | %s |" % (rank, display_title, short_path or "—", score_text))
        lines.append("")
        for index, item in enumerate(selected, start=1):
            rank = item.get("rank") or index
            display_title = self._opportunity_bilingual_title(item, translations)
            if not display_title:
                display_title = "机会 %d" % index
            lines.extend(
                [
                    "### 机会 %s：%s" % (rank, display_title),
                    "- 机会理由：%s" % self._opportunity_reason(item),
                    "- 关键证据：%s" % self._opportunity_evidence(item),
                    "- 风险/证据边界：%s" % self._opportunity_boundary(item),
                    "- 下一步验证：%s" % self._opportunity_next_step(item),
                    "",
                ]
            )
        lines.append("当前可继续分析的机会编号共有 %d 个。" % total_count)
        return "\n".join(lines).strip()

    @staticmethod
    def _has_cjk(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")

    @staticmethod
    def _opportunity_item_title(item: Any) -> str:
        if not isinstance(item, dict):
            return ""
        for key in ("title", "opportunity", "name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _collect_opportunity_titles(self, parsed: Any) -> List[str]:
        seen: List[str] = []
        if not isinstance(parsed, dict):
            return seen
        inner = parsed.get("payload") if isinstance(parsed.get("payload"), dict) else parsed
        opp_payload = self._extract_opportunity_payload(inner) if isinstance(inner, dict) else None
        if not isinstance(opp_payload, dict):
            return seen
        for key in ("opportunities_for_llm", "opportunities"):
            items = opp_payload.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                title = self._opportunity_item_title(item)
                if title and title not in seen and not self._has_cjk(title):
                    seen.append(title)
        return seen

    def _apply_opportunity_title_translations(self, parsed: Any, translations: Dict[str, str]) -> bool:
        if not translations or not isinstance(parsed, dict):
            return False
        inner = parsed.get("payload") if isinstance(parsed.get("payload"), dict) else parsed
        opp_payload = self._extract_opportunity_payload(inner) if isinstance(inner, dict) else None
        if not isinstance(opp_payload, dict):
            return False
        mutated = False
        for key in ("opportunities_for_llm", "opportunities"):
            items = opp_payload.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("title_zh"):
                    continue
                title = self._opportunity_item_title(item)
                zh = (translations.get(title) or "").strip()
                if zh and self._has_cjk(zh):
                    item["title_zh"] = zh
                    mutated = True
        return mutated

    def _enrich_opportunity_discovery_raw_result(self, raw_text: str) -> "tuple[Optional[str], Dict[str, str]]":
        parsed = self._load_tool_json_payload(raw_text)
        if not isinstance(parsed, dict):
            return None, {}
        titles = self._collect_opportunity_titles(parsed)
        if not titles:
            return None, {}
        translations = self._translate_opportunity_titles(titles)
        if not translations:
            return None, {}
        if not self._apply_opportunity_title_translations(parsed, translations):
            return None, translations
        try:
            return json.dumps(parsed, ensure_ascii=False), translations
        except (TypeError, ValueError):
            return None, translations

    def _translate_opportunity_titles(self, titles: List[str]) -> Dict[str, str]:
        cache = self._opportunity_title_zh_cache
        out: Dict[str, str] = {}
        pending: List[str] = []
        for title in titles:
            if not title or self._has_cjk(title):
                continue
            if title in cache:
                if cache[title]:
                    out[title] = cache[title]
                continue
            pending.append(title)
        if not pending:
            return out
        # 跨境调用 DeepSeek 偶发 ReadTimeout/ConnectionError；做一次重试，并且失败时
        # 不写空串到缓存，避免一次抖动就把这批 title 永久标记为"无翻译"。
        fetched: Dict[str, str] = {}
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                fetched = self._call_opportunity_title_translator(pending)
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                print(
                    "xiamimate.agent opportunity title translation failed (attempt %d/2):" % (attempt + 1),
                    repr(exc),
                )
        if last_exc is not None:
            return out
        applied = 0
        for title in pending:
            zh = str(fetched.get(title) or "").strip()
            if zh and not self._has_cjk(zh):
                zh = ""
            # 只缓存成功结果；空串不写入缓存，让下一次查询可以重试。
            if zh:
                cache[title] = zh
                out[title] = zh
                applied += 1
        if applied == 0:
            # 翻译调用本身没抛异常，但一条有效译文都没拿到（常见于推理模型 content 为空 /
            # 解析失败 / 模型漏键）。显式告警，区别于上面的异常路径，便于线上排查。
            print(
                "xiamimate.agent opportunity title translation returned empty:",
                "pending=%d fetched_keys=%d sample=%r"
                % (len(pending), len(fetched), pending[:3]),
            )
        return out

    def _call_opportunity_title_translator(self, titles: List[str]) -> Dict[str, str]:
        model_name = (self.valves.AGENT_TITLE_TRANSLATOR_MODEL or self.valves.AGENT_PLANNER_MODEL or "").strip()
        if not model_name or not titles:
            return {}
        system_prompt = (
            "你是跨境电商数据助手的术语翻译模块。"
            "将给定的英文 Amazon 细分类目/商品主题翻译为简洁的中文译名（4 字以内最佳），"
            "保留约定俗成的中文叫法；无法翻译时给空字符串。仅输出 JSON。"
        )
        user_prompt = (
            "请将下列英文条目翻译为简洁中文，输出严格 JSON：\n"
            "{\"translations\": {\"<英文原文>\": \"<中文译名或空字符串>\", ...}}\n"
            "约束：\n"
            "1) 键必须完全等于输入条目（保留大小写和复数形式）；\n"
            "2) 值只能是中文，不要包含括号、英文、解释；\n"
            "3) 不要新增条目，也不要省略条目。\n"
            "输入条目：\n- " + "\n- ".join(titles)
        )
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        # 翻译副线优先关闭思维链：DeepSeek v4 系列（deepseek-v4-flash 等）默认 thinking=enabled，
        # 偶发把全部 token 放进 reasoning_content 而 content 为空，导致译文解析失败。
        # 关闭后用非推理模式直出 JSON，速度更快、空 content 概率更低。
        if getattr(self.valves, "AGENT_TITLE_TRANSLATOR_DISABLE_THINKING", True):
            payload["thinking"] = {"type": "disabled"}
        translator_base_url = (self.valves.AGENT_TITLE_TRANSLATOR_BASE_URL or "").strip()
        translator_api_key = (self.valves.AGENT_TITLE_TRANSLATOR_API_KEY or "").strip()
        if translator_base_url and translator_api_key:
            data = self._post_llm_direct(
                base_url=translator_base_url,
                api_key=translator_api_key,
                payload=payload,
                timeout=self.valves.AGENT_TITLE_TRANSLATION_TIMEOUT,
            )
        else:
            data = self._post_agent_payload(payload, model_name=model_name)
        text = ""
        finish_reason = ""
        reasoning_text = ""
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            finish_reason = str(first.get("finish_reason") or "")
            message = first.get("message") if isinstance(first.get("message"), dict) else None
            if isinstance(message, dict):
                text = str(message.get("content") or "")
                reasoning_text = str(message.get("reasoning_content") or "")
        # content 为空时回退到 reasoning_content：推理模型偶发把可解析的 JSON 留在思维链字段里。
        primary = text if text.strip() else reasoning_text
        if not primary.strip():
            print(
                "xiamimate.agent title translator empty content:",
                "model=%s finish_reason=%s has_reasoning=%s titles=%d"
                % (model_name, finish_reason or "?", bool(reasoning_text.strip()), len(titles)),
            )
            return {}
        # 解析加固：剥离 ```json 围栏 / <think> 块 / 抽取首个 JSON 对象（复用通用提取器）。
        parsed = self._extract_json_value_from_text(primary)
        if not isinstance(parsed, dict):
            print(
                "xiamimate.agent title translator non-json content:",
                "model=%s finish_reason=%s preview=%r"
                % (model_name, finish_reason or "?", primary.strip()[:200]),
            )
            return {}
        block = parsed.get("translations") if isinstance(parsed, dict) else None
        if not isinstance(block, dict):
            print(
                "xiamimate.agent title translator missing 'translations' key:",
                "model=%s top_keys=%s"
                % (model_name, list(parsed.keys())[:8] if isinstance(parsed, dict) else None),
            )
            return {}
        return {str(k): str(v) for k, v in block.items() if isinstance(k, str)}

    def _opportunity_bilingual_title(self, item_or_title: Any, translations: Optional[Dict[str, str]] = None) -> str:
        if isinstance(item_or_title, dict):
            raw = self._opportunity_item_title(item_or_title)
            zh = str(item_or_title.get("title_zh") or "").strip()
        else:
            raw = str(item_or_title or "").strip()
            zh = ""
        if not raw:
            return ""
        if self._has_cjk(raw):
            return raw
        if not zh and translations:
            zh = str(translations.get(raw) or "").strip()
        if zh and self._has_cjk(zh) and zh.lower() != raw.lower():
            return "%s（%s）" % (zh, raw)
        return raw

    @staticmethod
    def _opportunity_short_category_path(path: Any, max_segments: int = 3) -> str:
        text = str(path or "").strip()
        if not text:
            return ""
        segments = [seg.strip() for seg in text.split(">") if seg.strip()]
        if len(segments) <= max_segments:
            return " > ".join(segments)
        return " > ".join([segments[0], "…", segments[-2], segments[-1]])

    def _requested_opportunity_count(self, observation: dict, opportunity_payload: dict, answer_contract: Optional[dict]) -> int:
        for value in ((answer_contract or {}).get("requested_count"), ((observation or {}).get("arguments") or {}).get("limit"), opportunity_payload.get("opportunity_count")):
            with contextlib.suppress(TypeError, ValueError):
                count = int(value)
                if count > 0:
                    return min(30, count)
        return 5

    def _opportunity_marketplace(self, observation: dict, opportunity_payload: dict) -> str:
        value = ((observation or {}).get("arguments") or {}).get("marketplace") or opportunity_payload.get("marketplace") or opportunity_payload.get("market") or "US"
        return str(value or "US").upper()

    def _opportunity_reason(self, item: dict) -> str:
        for key in ("opportunity_reason", "reason", "why", "summary"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        parts = []
        score = self._first_present(item, "opportunity_score", "score", "personalized_opportunity_score")
        if score is not None:
            parts.append("机会得分 %s，排序靠前" % self._format_metric_value(score))
        sales = self._first_present(item, "sales_window_sum", "window_sales_sum", "estimated_sales_window", "sales_estimate")
        if sales is not None:
            parts.append("窗口销量估算 %s" % self._format_metric_value(sales))
        category = self._first_present(item, "category_path", "category", "leaf_category_name")
        if category:
            parts.append("细分类目为 %s" % category)
        return "；".join(parts) if parts else "当前工具未返回可展开的机会理由，仅可依据该机会进入后续候选池验证。"

    def _opportunity_evidence(self, item: dict) -> str:
        parts = []
        score = self._first_present(item, "opportunity_score", "score")
        personalized = self._first_present(item, "personalized_opportunity_score", "personalized_score")
        score_part = ""
        if score is not None:
            score_part = "机会得分 %s" % self._format_metric_value(score)
            if personalized is not None:
                score_part += "（个性化分 %s）" % self._format_metric_value(personalized)
            derivation = self._opportunity_score_derivation(item)
            if derivation:
                score_part += "，得分推导：%s" % derivation
        elif personalized is not None:
            score_part = "个性化分 %s" % self._format_metric_value(personalized)
        if score_part:
            parts.append(score_part)
        sales = self._first_present(item, "sales_window_sum", "window_sales_sum", "estimated_daily_sales_sum", "estimated_sales_window")
        if sales is not None:
            parts.append("窗口销量估算 %s" % self._format_metric_value(sales))
        trend = self._first_present(item, "trend_momentum_display", "trend_signal_status", "trend_growth_display", "sales_growth_display")
        if trend:
            parts.append("增长/趋势 %s" % trend)
        competition = self._first_present(item, "competition_offer", "competition_offer_avg", "avg_offer_count", "new_offer_count")
        if competition is not None:
            parts.append("竞争 Offer %s" % self._format_metric_value(competition))
        sample_bits = []
        for label, key in (("ASIN", "candidate_count"), ("日数据行", "row_count")):
            value = item.get(key)
            if value is not None:
                sample_bits.append("%s=%s" % (label, self._format_metric_value(value)))
        if sample_bits:
            parts.append("样本 " + ", ".join(sample_bits))
        return "；".join(parts) if parts else "当前工具未返回结构化证据字段，请以机会编号继续做候选池验证。"

    def _opportunity_score_derivation(self, item: dict, max_components: int = 4) -> str:
        explanations = item.get("metric_explanations") if isinstance(item, dict) else None
        if not isinstance(explanations, dict):
            return ""
        score_explanation = explanations.get("opportunity_score")
        if not isinstance(score_explanation, dict):
            return ""
        components = score_explanation.get("components")
        if not isinstance(components, dict) or not components:
            return ""

        rendered = []
        for key, detail in components.items():
            if not isinstance(detail, dict):
                continue
            score = self._coerce_float(detail.get("score"))
            weight = self._coerce_float(detail.get("weight"))
            weighted = self._coerce_float(detail.get("weighted_points"))
            if weighted is None and score is not None and weight is not None:
                weighted = round(score * weight, 2)
            if score is None or weight is None or weighted is None:
                continue
            rendered.append(
                {
                    "label": self._opportunity_score_component_label(str(key)),
                    "score": score,
                    "weight": weight,
                    "weighted": weighted,
                }
            )
        if not rendered:
            return ""
        rendered.sort(key=lambda x: x.get("weighted", 0.0), reverse=True)
        pieces = []
        for component in rendered[: max(1, int(max_components or 4))]:
            pieces.append(
                "%s %s×%s=%s"
                % (
                    component["label"],
                    self._format_metric_value(component["score"]),
                    self._format_weight_percent(component["weight"]),
                    self._format_metric_value(component["weighted"]),
                )
            )
        return "；".join(pieces)

    def _opportunity_score_component_label(self, key: str) -> str:
        mapping = {
            "demand_score": "需求",
            "trend_score": "趋势",
            "competition_headroom_score": "竞争空间",
            "price_fit_score": "价格适配",
            "forecast_growth_score": "预测增长",
            "coverage_gap_score": "覆盖差距",
            "evidence_quality_score": "证据质量",
        }
        normalized = str(key or "").strip().lower()
        return mapping.get(normalized, str(key or "分项"))

    def _format_weight_percent(self, weight: Any) -> str:
        numeric = self._coerce_float(weight)
        if numeric is None:
            return str(weight)
        pct = numeric * 100.0
        return ("%.2f" % pct).rstrip("0").rstrip(".") + "%"

    def _coerce_float(self, value: Any) -> Optional[float]:
        if value in (None, "", [], {}):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _opportunity_boundary(self, item: dict) -> str:
        for key in ("risk", "risks", "boundary", "evidence_boundary", "confidence_note"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        parts = []
        confidence = self._first_present(item, "confidence", "data_confidence")
        if confidence:
            parts.append("置信度 %s" % confidence)
        if item.get("candidate_count") is not None or item.get("row_count") is not None:
            parts.append("当前结论只覆盖工具返回的候选样本和窗口期")
        if self._first_present(item, "trend_momentum_display", "trend_signal_status") in (None, ""):
            parts.append("趋势细节可能缺失，需要后续趋势/候选池工具补证")
        return "；".join(parts) if parts else "当前工具未返回风险细节；需通过候选池统计、趋势和竞品下钻补证。"

    def _opportunity_next_step(self, item: dict) -> str:
        next_action = item.get("next_action") if isinstance(item.get("next_action"), dict) else {}
        request = next_action.get("request") if isinstance(next_action.get("request"), dict) else {}
        product_query = request.get("product_query") or item.get("title") or item.get("opportunity")
        category_id = request.get("category_id") or item.get("category_id")
        category_path = request.get("category_path") or item.get("category_path")
        if next_action.get("requires_category_resolve"):
            return "先调用 category_resolve 确认稳定类目，再基于类目召回候选池。"
        details = []
        if product_query:
            details.append("用 resolve_candidates 分析 `%s`" % product_query)
        if category_id:
            details.append("category_id=%s" % category_id)
        if category_path:
            details.append("category_path=%s" % category_path)
        if details:
            return "；".join(details) + "，再进入 candidate_pool_stats / candidate_pool_slice / trend 验证。"
        return "复制该机会编号做 `/report deep`，或先 resolve_candidates 建立候选池后继续验证。"

    def _first_present(self, item: dict, *keys: str) -> Any:
        for key in keys:
            value = item.get(key)
            if value not in (None, "", [], {}):
                return value
        return None

    def _format_metric_value(self, value: Any) -> str:
        if isinstance(value, float):
            return ("%.2f" % value).rstrip("0").rstrip(".")
        return str(value)

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
                "candidate_pool_id",
                "candidate_pool_version",
                "candidate_pool_lineage",
                "candidate_pool_persistence",
                "raw_product_query",
                "recall_mode",
                "category_constraint",
                "category_scope_applied",
                "expand_if_small",
                "normalized_query",
                "candidate_count",
                "candidate_total_before_truncate",
                "candidate_total_before_semantic_category_anchor",
                "pool_quality",
                "semantic_fine_category_anchor_applied",
                "semantic_category_anchor_applied",
                "candidate_sql_prefilter_count",
                "candidate_sql_prefilter_limit",
                "candidate_sql_prefilter_truncated",
                "truncated",
                "query_phrases",
                "query_tokens",
                "required_product_terms",
                "effective_required_product_terms",
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
            "candidate_pool_id": "stable reference for downstream pool tools in this persisted resolve_candidates result",
            "candidate_asins": "full ranked ASIN pool for downstream tools such as candidate_pool_stats/trends/weak_forecast",
            "candidate_items": "budgeted visible details for reasoning/filtering; omitted details do not remove ASINs from the pool",
        }
        if len(candidate_items) > candidate_item_limit:
            compact_data["candidate_items"].append({"_omitted_items": len(candidate_items) - candidate_item_limit, "_total_items": len(candidate_items)})

        for contract_key in ("tool_contract", "evidence_contract"):
            if contract_key in data:
                compact_data[contract_key] = self._compact_json_value(
                    data[contract_key],
                    max_depth=4,
                    max_items=8,
                    max_scalar_items=20,
                    max_string=240,
                )

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

        for contract_key in ("tool_contract", "evidence_contract"):
            if contract_key in data:
                compact_data[contract_key] = self._compact_json_value(
                    data[contract_key],
                    max_depth=4,
                    max_items=8,
                    max_scalar_items=20,
                    max_string=240,
                )

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
        return "%s\n\n<internal_only 已截断，仅保留前 %d/%d 字符；这是内部格式标记，勿向用户复述压缩/截断状态>" % (
            text[:budget],
            budget,
            len(text),
        )

    def _fallback_answer_from_tool_observations(self, tool_observations: List[dict], error: str = "") -> str:
        opportunity_fallback = self._fallback_opportunity_answer_from_observations(tool_observations)
        if opportunity_fallback:
            return opportunity_fallback
        if not tool_observations:
            return "未生成可展示的结果。"
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

    def _inject_agent_system_prompt(self, messages: List[dict], mode: str = "agent", memory_profile: Optional[dict] = None) -> List[dict]:
        clean_messages = deepcopy(messages or [])
        system_prompt = self._agent_system_prompt_for_mode(mode)
        if clean_messages:
            first_message = clean_messages[0]
            if first_message.get("role") == "system":
                if first_message.get("content") == system_prompt:
                    self._insert_agent_memory_profile_message(clean_messages, memory_profile)
                    return clean_messages
                if first_message.get("content") in {AGENT_SYSTEM_PROMPT, TOOL_ONLY_SYSTEM_PROMPT}:
                    first_message["content"] = system_prompt
                    self._insert_agent_memory_profile_message(clean_messages, memory_profile)
                    return clean_messages
        clean_messages.insert(0, {"role": "system", "content": system_prompt})
        self._insert_agent_memory_profile_message(clean_messages, memory_profile)
        return clean_messages

    def _insert_agent_memory_profile_message(self, messages: List[dict], memory_profile: Optional[dict]) -> None:
        if not isinstance(memory_profile, dict) or not memory_profile:
            return
        messages[:] = [
            message
            for message in messages
            if not (
                message.get("role") == "system"
                and isinstance(message.get("content"), str)
                and message.get("content", "").startswith("XiaMimate memory_profile_context:")
            )
        ]
        content = "XiaMimate memory_profile_context:\n%s" % json.dumps(
            {
                "usage": "User preference context for personalization and explanation only; it is not market evidence and must not override tool facts.",
                "profile": memory_profile,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        insert_index = 1 if messages and messages[0].get("role") == "system" else 0
        messages.insert(insert_index, {"role": "system", "content": content})

    def _agent_system_prompt_for_mode(self, mode: str) -> str:
        if mode == "tool":
            return TOOL_ONLY_SYSTEM_PROMPT
        if mode == "help":
            return HELP_SYSTEM_PROMPT
        return AGENT_SYSTEM_PROMPT

    def _fallback_help_answer_from_tool_observations(self, tool_observations: List[dict], error: str = "") -> str:
        latest_result = str((tool_observations[-1] or {}).get("llm_result") or "").strip() if tool_observations else ""
        if latest_result.startswith("客服知识库检索失败"):
            message = latest_result[:2000]
        elif "未找到" in latest_result:
            message = "当前客服知识库里没有找到足够相关的内容。请换一个更具体的问法，例如只问价格规则、/report 计费、提示词示例或新手上手步骤。"
        else:
            message = "客服知识库已经检索到相关内容，但模型整理最终答复时失败了。请重试一次，或把问题拆得更具体一些再问。"
        if error:
            message = "%s\n\n备注：%s" % (message, str(error).strip()[:300])
        return message

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
