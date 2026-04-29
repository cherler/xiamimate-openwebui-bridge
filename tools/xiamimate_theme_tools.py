"""
title: XiaMimate Theme Tools
author: GitHub Copilot
date: 2026-04-14
version: 0.2.0
description: Native Open WebUI tool wrappers for XiaMimate theme_api, Dify knowledge base retrieval, and Dify web search.
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

    def web_search(
        self,
        query: str,
    ) -> str:
        """Search the live web through the XiaMimate Dify web-search chatflow and return a summarized result.

        Use this tool when you need recent external information such as platform policy updates, market news, competitor moves, creator trends, consumer demand signals, or other time-sensitive cross-border e-commerce intelligence.

        :param query: The web search query. Be specific about platform, market, and topic.
        :return: Final summarized result from the Dify web-search app.
        """
        return self._proxy_chatflow_result(
            path="/internal/provider/dify-web-search/run",
            payload={"query": query, "user": self.service_name},
            error_prefix="网络搜索失败",
        )

    def resolve_candidates(
        self,
        product_query: str,
        marketplace: str = "US",
        query_aliases: str = "",
        category_hints: str = "",
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
                "price_min": price_min,
                "price_max": price_max,
                "max_candidates": max_candidates,
                "active_only": active_only,
            },
        )

    def candidate_pool_stats(
        self,
        candidate_asins: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        product_query: str = "",
    ) -> str:
        """Get descriptive statistics for a resolved candidate pool.

        :param candidate_asins: CSV string of candidate ASINs.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for the pool metrics.
        :param product_query: Optional product query fallback when candidate_asins is not yet available.
        :return: JSON response from theme_api.
        """
        resolved_candidate_asins = self._ensure_candidate_asins(
            candidate_asins=candidate_asins,
            marketplace=marketplace,
            product_query=product_query,
        )
        if isinstance(resolved_candidate_asins, str):
            return resolved_candidate_asins

        return self._request(
            "/api/product-theme/candidate-pool-stats",
            {
                "candidate_asins": resolved_candidate_asins,
                "marketplace": marketplace,
                "window_days": window_days,
            },
        )

    def candidate_pool_trends(
        self,
        candidate_asins: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        product_query: str = "",
    ) -> str:
        """Get trend diagnostics for a candidate pool.

        :param candidate_asins: CSV string of candidate ASINs.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for trend calculations.
        :param product_query: Optional product query fallback when candidate_asins is not yet available.
        :return: JSON response from theme_api.
        """
        resolved_candidate_asins = self._ensure_candidate_asins(
            candidate_asins=candidate_asins,
            marketplace=marketplace,
            product_query=product_query,
        )
        if isinstance(resolved_candidate_asins, str):
            return resolved_candidate_asins

        return self._request(
            "/api/product-theme/candidate-pool-trends",
            {
                "candidate_asins": resolved_candidate_asins,
                "marketplace": marketplace,
                "window_days": window_days,
            },
        )

    def candidate_pool_weak_forecast(
        self,
        candidate_asins: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        top_n: int = 5,
        product_query: str = "",
    ) -> str:
        """Get weak-signal forecast markers for a candidate pool.

        :param candidate_asins: CSV string of candidate ASINs.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for forecast features.
        :param top_n: Number of top opportunity or risk signals to keep.
        :param product_query: Optional product query fallback when candidate_asins is not yet available.
        :return: JSON response from theme_api.
        """
        resolved_candidate_asins = self._ensure_candidate_asins(
            candidate_asins=candidate_asins,
            marketplace=marketplace,
            product_query=product_query,
        )
        if isinstance(resolved_candidate_asins, str):
            return resolved_candidate_asins

        return self._request(
            "/api/product-theme/candidate-pool-weak-forecast",
            {
                "candidate_asins": resolved_candidate_asins,
                "marketplace": marketplace,
                "window_days": window_days,
                "top_n": top_n,
            },
        )

    def top_asin_drilldown(
        self,
        candidate_asins: str = "",
        marketplace: str = "US",
        window_days: int = 30,
        top_n: Optional[int] = None,
        product_query: str = "",
    ) -> str:
        """Inspect the strongest ASINs in a candidate pool.

        :param candidate_asins: CSV string of candidate ASINs.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for the drilldown.
        :param top_n: Optional limit for the number of ASINs returned.
        :param product_query: Optional product query fallback when candidate_asins is not yet available.
        :return: JSON response from theme_api.
        """
        resolved_candidate_asins = self._ensure_candidate_asins(
            candidate_asins=candidate_asins,
            marketplace=marketplace,
            product_query=product_query,
        )
        if isinstance(resolved_candidate_asins, str):
            return resolved_candidate_asins

        payload = {
            "candidate_asins": resolved_candidate_asins,
            "marketplace": marketplace,
            "window_days": window_days,
        }
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
        marketplace: str = "US",
        window_days: int = 30,
        product_query: str = "",
    ) -> str:
        """Compare a candidate pool against its benchmark category.

        :param candidate_asins: CSV string of candidate ASINs.
        :param marketplace: Marketplace code such as US, UK, DE, or JP.
        :param window_days: Lookback window for the benchmark snapshot.
        :param product_query: Optional product query fallback when candidate_asins is not yet available.
        :return: JSON response from theme_api.
        """
        resolved_candidate_asins = self._ensure_candidate_asins(
            candidate_asins=candidate_asins,
            marketplace=marketplace,
            product_query=product_query,
        )
        if isinstance(resolved_candidate_asins, str):
            return resolved_candidate_asins

        return self._request(
            "/api/product-theme/category-benchmark",
            {
                "candidate_asins": resolved_candidate_asins,
                "marketplace": marketplace,
                "window_days": window_days,
            },
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
            return "缺少 candidate_asins；请先调用 resolve_candidates，或传入 product_query/category。"

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