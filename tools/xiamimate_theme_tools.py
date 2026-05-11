"""
title: XiaMimate Theme Tools
author: GitHub Copilot
date: 2026-04-14
version: 0.2.0
description: Native Open WebUI tool wrappers for XiaMimate theme_api, Dify knowledge base retrieval, and Tavily web search.
requirements: requests
"""

import json
import os
from typing import Iterable, List, Optional

import requests


INTERNAL_SERVICE_SECRET_HEADER_NAME = "X-Internal-Service-Secret"
INTERNAL_SERVICE_NAME_HEADER_NAME = "X-Internal-Service-Name"


class Tools:
    def __init__(self):
        self.chat_backend_base_url = (os.getenv("CHAT_BACKEND_BASE_URL") or "").rstrip("/")
        self.timeout = int(os.getenv("CHAT_BACKEND_TIMEOUT") or "30")
        self.service_secret = os.getenv("CHAT_BACKEND_SERVICE_SECRET") or ""
        self.service_name = os.getenv("CHAT_BACKEND_SERVICE_NAME") or "open-webui-pipeline"

    def search_knowledge_base(
        self,
        query: str,
        top_k: int = 5,
    ) -> str:
        """Search the cross-border e-commerce knowledge base for platform rules, operational guides, and market insights about TikTok Shop, Temu, and Amazon.

        Use this tool when answering questions about platform policies, seller requirements, category regulations, logistics, compliance, operational best practices, or any domain knowledge that is not numerical product data.

        :param query: The search query describing what knowledge you need. Be specific, e.g. "TikTok Shop US seller onboarding requirements" or "Temu semi-managed vs fully-managed differences".
        :param top_k: Number of top relevant snippets to return (default 5).
        :return: Formatted knowledge snippets with source attribution.
        """
        return self._proxy_result(
            path="/internal/provider/dify-dataset/retrieve",
            payload={"query": query, "top_k": top_k},
            error_prefix="知识库检索失败",
        )

    def customer_help_search(
        self,
        query: str,
        top_k: int = 5,
    ) -> str:
        """Search the customer-help knowledge base for usage guidance, pricing rules, prompt examples, and customer-service standard answers.

        Use this tool only for /help-style questions such as prompt suggestions, pricing and points rules, onboarding guidance, account-related FAQ, and customer-facing product explanations.

        :param query: The customer-help query to search for.
        :param top_k: Number of top relevant snippets to return (default 5).
        :return: Formatted customer-help snippets with source attribution.
        """
        return self._proxy_result(
            path="/internal/provider/dify-customer-help/retrieve",
            payload={"query": query, "top_k": top_k},
            error_prefix="客服知识库检索失败",
        )

    def web_search(
        self,
        query: str,
    ) -> str:
        """Search the live web through XiaMimate's direct Tavily web-search provider and return a summarized result.

        Use this tool when you need recent external information such as platform policy updates, market news, competitor moves, creator trends, consumer demand signals, or other time-sensitive cross-border e-commerce intelligence.

        :param query: The web search query. Be specific about platform, market, and topic.
        :return: Search summary and sources from Tavily.
        """
        return self._proxy_tavily_result(
            path="/internal/provider/web-search/tavily",
            payload={"query": query, "user": self.service_name, "search_mode": "auto", "max_results": 5, "include_answer": True},
            error_prefix="网络搜索失败",
        )

    def resolve_candidates(
        self,
        product_query: str,
        marketplace: str = "US",
        query_aliases: str = "",
        category_hints: str = "",
        recall_mode: str = "keyword",
        category_id: Optional[int] = None,
        category_path: str = "",
        include_descendants: bool = True,
        min_pool_size: int = 8,
        target_pool_size: int = 20,
        expand_if_small: bool = False,
        price_min: Optional[float] = None,
        price_max: Optional[float] = None,
        max_candidates: int = 30,
        active_only: bool = True,
    ) -> str:
        """Resolve a candidate ASIN pool for a product theme.

        :param product_query: Product keyword or theme to analyze.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param query_aliases: Optional CSV string of query aliases.
        :param category_hints: Optional CSV string of category hints.
        :param recall_mode: Recall strategy: keyword, hybrid, category, or asin_seed_expand.
        :param category_id: Optional Keepa category ID to constrain recall.
        :param category_path: Optional category path to constrain recall.
        :param include_descendants: Whether category_id/category_path constraints include descendant categories.
        :param min_pool_size: Minimum pure candidate pool size for analysis confidence.
        :param target_pool_size: Target candidate pool size before expansion is recommended.
        :param expand_if_small: Planning signal that the caller wants expansion if the local pool is too small.
        :param price_min: Optional lower bound for current price.
        :param price_max: Optional upper bound for current price.
        :param max_candidates: Maximum number of ASINs to return.
        :param active_only: Whether to keep only active candidates.
        :return: JSON response from theme_api.
        """
        return self._request(
            "/api/product-theme/resolve-candidates",
            {
                "product_query": product_query,
                "marketplace": marketplace,
                "query_aliases": self._normalize_csv(query_aliases),
                "category_hints": self._normalize_csv(category_hints),
                "recall_mode": recall_mode,
                "category_id": category_id,
                "category_path": category_path,
                "include_descendants": include_descendants,
                "min_pool_size": min_pool_size,
                "target_pool_size": target_pool_size,
                "expand_if_small": expand_if_small,
                "price_min": price_min,
                "price_max": price_max,
                "max_candidates": max_candidates,
                "active_only": active_only,
            },
        )

    def category_resolve(
        self,
        category_query: str = "",
        category_path: str = "",
        marketplace: str = "US",
        max_matches: int = 10,
    ) -> str:
        """Resolve an Amazon/Keepa category name or full category path to candidate category IDs and local data coverage.

        Use this tool when a candidate pool is too small or when you need a stable category_id/category_path before benchmark or pool expansion.

        :param category_query: Category keyword or leaf name, e.g. "Humidifiers".
        :param category_path: Optional full category path, e.g. "Home & Kitchen > Heating, Cooling & Air Quality > Humidifiers".
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param max_matches: Maximum category matches to return.
        :return: JSON response from theme_api.
        """
        return self._request(
            "/api/product-theme/category-resolve",
            {
                "category_query": category_query,
                "category_path": category_path,
                "marketplace": marketplace,
                "max_matches": max_matches,
            },
        )

    def expand_candidates(
        self,
        product_query: str = "",
        marketplace: str = "US",
        recall_mode: str = "hybrid",
        category_id: Optional[int] = None,
        category_path: str = "",
        include_descendants: bool = True,
        target_asin_count: int = 20,
        min_pool_size: int = 8,
        priority: str = "interactive_normal",
        requested_by_session_id: str = "",
        idempotency_key: str = "",
        notes: str = "",
    ) -> str:
        """Queue a Keepa candidate expansion job without consuming Keepa tokens in the request path.

        Use after resolve_candidates returns low pool_quality or after category_resolve identifies a stable category_id/category_path.

        :param product_query: Product keyword or theme to expand.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param recall_mode: Recall strategy to use after expansion, usually hybrid or category.
        :param category_id: Optional Keepa category ID to expand.
        :param category_path: Optional category path to expand.
        :param include_descendants: Whether category expansion includes descendants.
        :param target_asin_count: Target ASIN count after expansion.
        :param min_pool_size: Minimum pool size before analysis is allowed.
        :param priority: Job priority: interactive_high, interactive_normal, background_high, or background_low.
        :param requested_by_session_id: Optional chat/session ID for traceability.
        :param idempotency_key: Optional idempotency key to avoid duplicate jobs.
        :param notes: Optional human-readable request note.
        :return: JSON response from theme_api.
        """
        return self._request(
            "/api/product-theme/expand-candidates",
            {
                "product_query": product_query,
                "marketplace": marketplace,
                "recall_mode": recall_mode,
                "category_id": category_id,
                "category_path": category_path,
                "include_descendants": include_descendants,
                "target_asin_count": target_asin_count,
                "min_pool_size": min_pool_size,
                "source": "agent_interactive",
                "priority": priority,
                "requested_by_session_id": requested_by_session_id,
                "idempotency_key": idempotency_key,
                "notes": notes,
            },
        )

    def candidate_expansion_status(
        self,
        job_id: str = "",
        marketplace: str = "US",
        statuses: str = "queued,waiting_token,discovering,hydrating,syncing",
        limit: int = 20,
    ) -> str:
        """Query queued or running Keepa candidate expansion jobs and analysis data readiness.

        The response includes data_readiness for each job. Treat status=completed as ASIN registry visibility only; use data_readiness.analysis_ready before running stats, benchmark, drilldown, or forecast analysis.

        :param job_id: Optional specific expansion job ID.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param statuses: CSV statuses to list when job_id is empty.
        :param limit: Maximum jobs to return.
        :return: JSON response from theme_api, including data_readiness.readiness_status and analysis_ready.
        """
        return self._request(
            "/api/product-theme/candidate-expansion-status",
            {
                "job_id": job_id,
                "marketplace": marketplace,
                "statuses": self._normalize_csv(statuses),
                "limit": limit,
            },
        )

    def opportunity_discovery(
        self,
        marketplace: str = "US",
        platform: str = "Amazon",
        category_id: Optional[int] = None,
        category_path: str = "",
        limit: int = 10,
        window_days: int = 30,
        min_data_confidence: str = "low",
        include_expandable: bool = True,
        include_descendants: bool = True,
        _memory_profile: Optional[dict] = None,
    ) -> str:
        """Discover product opportunity cards before a user has selected a specific product theme.

        Use this tool only for spontaneous blank opportunity discovery or broad category-scoped discovery when the user has not provided a concrete product keyword, product theme, or ASIN. Do not use it for explicit theme-analysis requests such as "evaluate car vacuum in Temu US"; in those cases call resolve_candidates and the downstream theme-analysis tools instead.

        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param platform: Platform name. The MVP currently supports Amazon.
        :param category_id: Optional Keepa category ID to scope discovery.
        :param category_path: Optional category path to scope discovery, e.g. "Home & Kitchen".
        :param limit: Maximum opportunity cards to return.
        :param window_days: Lookback window for local evidence.
        :param min_data_confidence: low, medium, or high.
        :param include_expandable: Whether low-coverage opportunities can still be returned with expansion suggestions.
        :param include_descendants: Whether category constraints include descendants.
        :return: JSON response from theme_api.
        """
        return self._request(
            "/api/product-theme/opportunity-discovery",
            {
                "marketplace": marketplace,
                "platform": platform,
                "category_id": category_id,
                "category_path": str(category_path or "").strip() or None,
                "limit": limit,
                "window_days": window_days,
                "min_data_confidence": min_data_confidence,
                "include_expandable": include_expandable,
                "include_descendants": include_descendants,
                "memory_profile": _memory_profile if isinstance(_memory_profile, dict) and _memory_profile else None,
            },
        )

    def opportunity_discovery_job(
        self,
        job_id: str = "",
        marketplace: str = "US",
        include_result: bool = True,
        limit: int = 20,
    ) -> str:
        """Retrieve stored opportunity discovery evidence by job ID.

        Use this when an opportunity discovery response provides opportunity_discovery_job_id and the agent needs the full cards or structured opportunities without relying on compressed context.

        :param job_id: Opportunity discovery job ID returned by opportunity_discovery.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param include_result: Whether to include the full result payload.
        :param limit: Maximum recent jobs to list when job_id is empty.
        :return: JSON response from theme_api.
        """
        return self._request(
            "/api/product-theme/opportunity-discovery-job",
            {
                "job_id": str(job_id or "").strip() or None,
                "marketplace": marketplace,
                "include_result": include_result,
                "limit": limit,
            },
        )

    def candidate_pool_stats(
        self,
        candidate_asins: str = "",
        candidate_pool_id: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        product_query: str = "",
    ) -> str:
        """Get descriptive statistics for a resolved candidate pool.

        :param candidate_asins: CSV string of candidate ASINs.
        :param candidate_pool_id: Persisted candidate_pool_id returned by resolve_candidates.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for the pool metrics.
        :param product_query: Optional product query fallback when candidate_asins is not yet available.
        :return: JSON response from theme_api.
        """
        payload = {
            "candidate_pool_id": str(candidate_pool_id or "").strip() or None,
            "marketplace": marketplace,
            "window_days": window_days,
        }
        if not payload["candidate_pool_id"]:
            resolved_candidate_asins = self._ensure_candidate_asins(
                candidate_asins=candidate_asins,
                marketplace=marketplace,
                product_query=product_query,
            )
            if isinstance(resolved_candidate_asins, str):
                return resolved_candidate_asins
            payload["candidate_asins"] = resolved_candidate_asins

        return self._request(
            "/api/product-theme/candidate-pool-stats",
            payload,
        )

    def candidate_pool_trends(
        self,
        candidate_asins: str = "",
        candidate_pool_id: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        product_query: str = "",
    ) -> str:
        """Get trend diagnostics for a candidate pool.

        :param candidate_asins: CSV string of candidate ASINs.
        :param candidate_pool_id: Persisted candidate_pool_id returned by resolve_candidates.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for trend calculations.
        :param product_query: Optional product query fallback when candidate_asins is not yet available.
        :return: JSON response from theme_api.
        """
        payload = {
            "candidate_pool_id": str(candidate_pool_id or "").strip() or None,
            "marketplace": marketplace,
            "window_days": window_days,
        }
        if not payload["candidate_pool_id"]:
            resolved_candidate_asins = self._ensure_candidate_asins(
                candidate_asins=candidate_asins,
                marketplace=marketplace,
                product_query=product_query,
            )
            if isinstance(resolved_candidate_asins, str):
                return resolved_candidate_asins
            payload["candidate_asins"] = resolved_candidate_asins

        return self._request(
            "/api/product-theme/candidate-pool-trends",
            payload,
        )

    def candidate_pool_weak_forecast(
        self,
        candidate_asins: str = "",
        candidate_pool_id: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        top_n: int = 5,
        product_query: str = "",
    ) -> str:
        """Get weak-signal forecast markers for a candidate pool.

        :param candidate_asins: CSV string of candidate ASINs.
        :param candidate_pool_id: Persisted candidate_pool_id returned by resolve_candidates.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for forecast features.
        :param top_n: Number of top opportunity or risk signals to keep.
        :param product_query: Optional product query fallback when candidate_asins is not yet available.
        :return: JSON response from theme_api.
        """
        payload = {
            "candidate_pool_id": str(candidate_pool_id or "").strip() or None,
            "marketplace": marketplace,
            "window_days": window_days,
            "top_n": top_n,
        }
        if not payload["candidate_pool_id"]:
            resolved_candidate_asins = self._ensure_candidate_asins(
                candidate_asins=candidate_asins,
                marketplace=marketplace,
                product_query=product_query,
            )
            if isinstance(resolved_candidate_asins, str):
                return resolved_candidate_asins
            payload["candidate_asins"] = resolved_candidate_asins

        return self._request(
            "/api/product-theme/candidate-pool-weak-forecast",
            payload,
        )

    def product_forecast_explain(
        self,
        candidate_asins: str = "",
        candidate_pool_id: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        top_n: int = 10,
        product_query: str = "",
    ) -> str:
        """Return trained sales forecast fields and explainability summaries for top ASINs.

        Use this formal Theme API tool when users ask for model forecast, future sales, or why the model is bullish/bearish. It returns trained sales_forecast fields plus explainability fields such as primary_driver_label, top_feature_contributions, and driver_summary_text.

        :param candidate_asins: CSV string of candidate ASINs.
        :param candidate_pool_id: Persisted candidate_pool_id returned by resolve_candidates.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for the drilldown context.
        :param top_n: Maximum number of top ASIN forecast explanations to return.
        :param product_query: Optional product query fallback when candidate_asins is not yet available.
        :return: JSON response from theme_api with model forecast coverage and ASIN-level explainability.
        """
        payload = {
            "candidate_pool_id": str(candidate_pool_id or "").strip() or None,
            "marketplace": marketplace,
            "window_days": window_days,
            "top_n": top_n,
        }
        if not payload["candidate_pool_id"]:
            resolved_candidate_asins = self._ensure_candidate_asins(
                candidate_asins=candidate_asins,
                marketplace=marketplace,
                product_query=product_query,
            )
            if isinstance(resolved_candidate_asins, str):
                return resolved_candidate_asins
            payload["candidate_asins"] = resolved_candidate_asins

        return self._request("/api/product-theme/product-forecast-explain", payload)

    def launch_budget_calculator(
        self,
        product_theme: str = "",
        marketplace: str = "US",
        selling_price: Optional[float] = None,
        unit_product_cost: Optional[float] = None,
        landed_cost_per_unit: Optional[float] = None,
        packaging_cost: Optional[float] = None,
        inbound_shipping_per_unit: Optional[float] = None,
        duty_per_unit: Optional[float] = None,
        fba_fee: Optional[float] = None,
        referral_fee_rate: Optional[float] = None,
        coupon_discount_rate: Optional[float] = None,
        return_rate: Optional[float] = None,
        fixed_startup_cost: Optional[float] = None,
        monthly_fixed_cost: Optional[float] = None,
        monthly_ad_budget: Optional[float] = None,
        launch_units: Optional[int] = None,
        launch_months: Optional[int] = None,
    ) -> str:
        """Calculate deterministic launch budget, unit economics, and break-even scenarios from explicit assumptions.

        Use this tool whenever users ask about startup capital, launch budget, break-even units, operating runway, or product unit economics. Pass observed product price or user-provided cost assumptions when available; otherwise the tool marks defaults as default_assumption so the final answer can separate facts, assumptions, and hypotheses.

        :param product_theme: Optional product theme or category being planned.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param selling_price: Planned selling price per unit.
        :param unit_product_cost: Supplier product cost per unit.
        :param landed_cost_per_unit: Optional all-in landed cost per unit. If provided, it overrides product+packaging+inbound+duty component sum.
        :param packaging_cost: Packaging cost per unit.
        :param inbound_shipping_per_unit: Inbound freight or first-mile cost per unit.
        :param duty_per_unit: Duty/customs cost per unit.
        :param fba_fee: FBA fulfillment fee per sold unit.
        :param referral_fee_rate: Platform referral fee rate, e.g. 0.15 for 15%.
        :param coupon_discount_rate: Planned coupon/promo reserve rate, e.g. 0.10.
        :param return_rate: Return/refund reserve rate, e.g. 0.08.
        :param fixed_startup_cost: One-time fixed setup cost.
        :param monthly_fixed_cost: Recurring fixed operating cost excluding ads.
        :param monthly_ad_budget: Monthly ad budget during launch.
        :param launch_units: Initial launch inventory units.
        :param launch_months: Planning runway in months.
        :return: JSON response with assumptions, formulas, unit economics, break-even, and launch budget scenarios.
        """
        return self._request(
            "/api/product-theme/launch-budget-calculator",
            {
                "product_theme": str(product_theme or "").strip() or None,
                "marketplace": marketplace,
                "selling_price": selling_price,
                "unit_product_cost": unit_product_cost,
                "landed_cost_per_unit": landed_cost_per_unit,
                "packaging_cost": packaging_cost,
                "inbound_shipping_per_unit": inbound_shipping_per_unit,
                "duty_per_unit": duty_per_unit,
                "fba_fee": fba_fee,
                "referral_fee_rate": referral_fee_rate,
                "coupon_discount_rate": coupon_discount_rate,
                "return_rate": return_rate,
                "fixed_startup_cost": fixed_startup_cost,
                "monthly_fixed_cost": monthly_fixed_cost,
                "monthly_ad_budget": monthly_ad_budget,
                "launch_units": launch_units,
                "launch_months": launch_months,
            },
        )

    def top_asin_drilldown(
        self,
        candidate_asins: str = "",
        candidate_pool_id: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        top_n: Optional[int] = None,
        product_query: str = "",
    ) -> str:
        """Inspect the strongest ASINs in a candidate pool.

        :param candidate_asins: CSV string of candidate ASINs.
        :param candidate_pool_id: Persisted candidate_pool_id returned by resolve_candidates.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for the drilldown.
        :param top_n: Optional limit for the number of ASINs returned.
        :param product_query: Optional product query fallback when candidate_asins is not yet available.
        :return: JSON response from theme_api.
        """
        payload = {
            "candidate_pool_id": str(candidate_pool_id or "").strip() or None,
            "marketplace": marketplace,
            "window_days": window_days,
        }
        if not payload["candidate_pool_id"]:
            resolved_candidate_asins = self._ensure_candidate_asins(
                candidate_asins=candidate_asins,
                marketplace=marketplace,
                product_query=product_query,
            )
            if isinstance(resolved_candidate_asins, str):
                return resolved_candidate_asins
            payload["candidate_asins"] = resolved_candidate_asins
        if top_n is not None:
            payload["top_n"] = top_n
        return self._request("/api/product-theme/top-asin-drilldown", payload)

    def asin_history_timeseries(
        self,
        asins: str = "",
        marketplace: str = "US",
        window_days: int = 90,
        interval: str = "day",
        metrics: str = "",
        product_query: str = "",
    ) -> str:
        """Get ASIN-level historical time series from the local theme store, with optional Keepa latest-snapshot fallback.

        Use this tool when you already have one or more specific ASINs and need daily or weekly history for sales, price, BSR, or review trends.

        :param asins: CSV string of ASINs. If omitted, product_query can be used to resolve a candidate pool first.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for history extraction. Current online limit is 90 days.
        :param interval: day or week.
        :param metrics: Optional CSV string such as estimated_daily_sales,effective_price,bsr,review_count.
        :param product_query: Optional product query fallback when asins is not yet available.
        :return: JSON response from theme_api.
        """
        resolved_asins = self._ensure_candidate_asins(
            candidate_asins=asins,
            marketplace=marketplace,
            product_query=product_query,
        )
        if isinstance(resolved_asins, str):
            return resolved_asins

        return self._request(
            "/api/product-theme/asin-history-timeseries",
            {
                "asins": resolved_asins,
                "marketplace": marketplace,
                "window_days": window_days,
                "interval": interval,
                "metrics": self._normalize_csv(metrics),
            },
        )

    def category_benchmark(
        self,
        candidate_asins: str = "",
        candidate_pool_id: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        benchmark_category_id: Optional[int] = None,
        benchmark_category_path: str = "",
        benchmark_level: str = "auto",
        include_descendants: bool = True,
        product_query: str = "",
    ) -> str:
        """Compare a candidate pool against its benchmark category.

        :param candidate_asins: CSV string of candidate ASINs.
        :param candidate_pool_id: Persisted candidate_pool_id returned by resolve_candidates.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for the benchmark snapshot.
        :param benchmark_category_id: Optional explicit Keepa category ID to use as benchmark anchor.
        :param benchmark_category_path: Optional explicit category path to use as benchmark anchor.
        :param benchmark_level: Anchor level: auto, leaf, fine, l3, l2, l1, or root.
        :param include_descendants: Whether benchmark aggregation includes child categories.
        :param product_query: Optional product query fallback when candidate_asins is not yet available.
        :return: JSON response from theme_api.
        """
        payload = {
            "candidate_pool_id": str(candidate_pool_id or "").strip() or None,
            "marketplace": marketplace,
            "window_days": window_days,
            "benchmark_category_id": benchmark_category_id,
            "benchmark_category_path": benchmark_category_path,
            "benchmark_level": benchmark_level,
            "include_descendants": include_descendants,
        }
        if not payload["candidate_pool_id"]:
            resolved_candidate_asins = self._ensure_candidate_asins(
                candidate_asins=candidate_asins,
                marketplace=marketplace,
                product_query=product_query,
            )
            if isinstance(resolved_candidate_asins, str):
                return resolved_candidate_asins
            payload["candidate_asins"] = resolved_candidate_asins

        return self._request(
            "/api/product-theme/category-benchmark",
            payload,
        )

    def keepa_asin_lookup(
        self,
        asins: str,
        marketplace: str = "US",
    ) -> str:
        """Look up ASIN product details directly from the Keepa API when the local database does not contain the ASIN data. Returns product info in the same format as top_asin_drilldown.

        Use this tool when top_asin_drilldown returns empty results or when you need real-time data for specific ASINs that may not be in the local database.

        :param asins: CSV string of ASINs to look up (max 20).
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :return: JSON response with ASIN product details from Keepa.
        """
        return self._request(
            "/api/product-theme/keepa-asin-lookup",
            {
                "asins": self._normalize_csv(asins),
                "marketplace": marketplace,
            },
        )

    def _normalize_csv(self, value) -> List[str]:
        if value is None:
            return []

        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return self._flatten_csv_items(parsed)
            return [item.strip() for item in stripped.split(",") if item.strip()]

        if isinstance(value, dict):
            nested_values = (
                value.get("candidate_asins")
                or value.get("asins")
                or value.get("items")
                or value.get("candidates")
                or value.get("data")
            )
            if nested_values is not None:
                return self._normalize_csv(nested_values)

        if isinstance(value, (list, tuple, set)):
            return self._flatten_csv_items(value)

        return [str(value).strip()] if str(value).strip() else []

    def _flatten_csv_items(self, values: Iterable) -> List[str]:
        items: List[str] = []
        for value in values:
            if isinstance(value, dict):
                text = str(
                    value.get("asin")
                    or value.get("code")
                    or value.get("id")
                    or ""
                ).strip()
            else:
                text = str(value).strip()
            if text:
                items.append(text)
        return items

    def _ensure_candidate_asins(self, candidate_asins, marketplace: str, product_query: str):
        normalized_candidate_asins = self._normalize_csv(candidate_asins)
        if normalized_candidate_asins:
            return normalized_candidate_asins

        normalized_product_query = str(product_query or "").strip()
        if not normalized_product_query:
            return "缺少 candidate_asins 或 candidate_pool_id；请先调用 resolve_candidates，或传入 product_query/category。"

        resolved = self.resolve_candidates(
            product_query=normalized_product_query,
            marketplace=marketplace,
        )
        if resolved.startswith("theme_api 请求失败") or resolved.startswith("工具 ") or resolved.startswith("CHAT_BACKEND_"):
            return resolved

        try:
            payload = json.loads(resolved)
        except ValueError:
            return "候选池解析结果不是合法 JSON，无法提取 candidate_asins。"

        resolved_candidate_asins = self._extract_candidate_asins(payload)
        if not resolved_candidate_asins:
            return "候选池解析结果缺少 candidate_asins，无法继续执行下游工具。"

        return resolved_candidate_asins

    def _extract_candidate_asins(self, payload: dict) -> List[str]:
        candidates = self._normalize_csv(payload.get("candidate_asins"))
        if candidates:
            return candidates

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        candidates = self._normalize_csv(data.get("candidate_asins"))
        if candidates:
            return candidates

        return self._normalize_csv(data.get("candidate_items"))

    def _request(self, path: str, payload: dict) -> str:
        operation = path.rsplit("/", 1)[-1].replace("-", "_")
        return self._proxy_result(
            path="/internal/provider/theme-api/%s" % operation,
            payload={"payload": payload},
            error_prefix="theme_api 请求失败",
        )

    def _proxy_result(self, path: str, payload: dict, error_prefix: str) -> str:
        if not self.chat_backend_base_url:
            return "CHAT_BACKEND_BASE_URL 未配置。"
        if not self.service_secret:
            return "CHAT_BACKEND_SERVICE_SECRET 未配置。"

        response = None
        try:
            response = requests.post(
                "%s%s" % (self.chat_backend_base_url, path),
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    INTERNAL_SERVICE_SECRET_HEADER_NAME: self.service_secret,
                    INTERNAL_SERVICE_NAME_HEADER_NAME: self.service_name,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            wrapper = response.json()
            if wrapper.get("success") is not True:
                detail = str(wrapper.get("message") or "")
                if detail.startswith(error_prefix):
                    return detail[:4000]
                return "%s:\n%s" % (error_prefix, detail[:4000])
            return str((wrapper.get("data") or {}).get("result") or "")
        except requests.RequestException as exc:
            detail = response.text if response is not None else str(exc)
            if detail.startswith(error_prefix):
                return detail[:4000]
            return "%s:\n%s" % (error_prefix, detail[:4000])

    def _proxy_chatflow_result(self, path: str, payload: dict, error_prefix: str) -> str:
        if not self.chat_backend_base_url:
            return "CHAT_BACKEND_BASE_URL 未配置。"
        if not self.service_secret:
            return "CHAT_BACKEND_SERVICE_SECRET 未配置。"

        response = None
        try:
            response = requests.post(
                "%s%s" % (self.chat_backend_base_url, path),
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    INTERNAL_SERVICE_SECRET_HEADER_NAME: self.service_secret,
                    INTERNAL_SERVICE_NAME_HEADER_NAME: self.service_name,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            wrapper = response.json()
            if wrapper.get("success") is not True:
                detail = str(wrapper.get("message") or "")
                if detail.startswith(error_prefix):
                    return detail[:4000]
                return "%s:\n%s" % (error_prefix, detail[:4000])

            data = wrapper.get("data") or {}
            for key in ("answer", "text", "content"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value

            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            for key in ("answer", "text", "content"):
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    return value

            outputs = nested.get("outputs") if isinstance(nested.get("outputs"), dict) else {}
            for key in ("answer", "text", "result", "output"):
                value = outputs.get(key)
                if isinstance(value, str) and value.strip():
                    return value

            return json.dumps(data, ensure_ascii=False, indent=2)
        except requests.RequestException as exc:
            detail = response.text if response is not None else str(exc)
            if detail.startswith(error_prefix):
                return detail[:4000]
            return "%s:\n%s" % (error_prefix, detail[:4000])

    def _proxy_tavily_result(self, path: str, payload: dict, error_prefix: str) -> str:
        if not self.chat_backend_base_url:
            return "CHAT_BACKEND_BASE_URL 未配置。"
        if not self.service_secret:
            return "CHAT_BACKEND_SERVICE_SECRET 未配置。"

        response = None
        try:
            response = requests.post(
                "%s%s" % (self.chat_backend_base_url, path),
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    INTERNAL_SERVICE_SECRET_HEADER_NAME: self.service_secret,
                    INTERNAL_SERVICE_NAME_HEADER_NAME: self.service_name,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            wrapper = response.json()
            if wrapper.get("success") is not True:
                detail = str(wrapper.get("message") or "")
                if detail.startswith(error_prefix):
                    return detail[:4000]
                return "%s:\n%s" % (error_prefix, detail[:4000])

            data = wrapper.get("data") or {}
            for key in ("result_text", "answer", "text", "content"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return json.dumps(data, ensure_ascii=False, indent=2)
        except requests.RequestException as exc:
            detail = response.text if response is not None else str(exc)
            if detail.startswith(error_prefix):
                return detail[:4000]
            return "%s:\n%s" % (error_prefix, detail[:4000])