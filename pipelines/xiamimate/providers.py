"""
LLM provider strategy: per-model parameter whitelist, API routing, tool-call
parsing and internal-markup cleanup.

Usage inside Pipeline:

    from xiamimate.providers import get_provider

    provider = get_provider(model_name)      # returns ProviderStrategy
    payload  = provider.filter_payload(body)  # allowed params only
    path     = provider.chat_completions_path()
    stream   = provider.chat_completions_stream_path()
    calls    = provider.extract_tool_calls(content)
    cleaned  = provider.strip_internal_markup(content)
    has_mark = provider.has_internal_markup(content)
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Base strategy
# ---------------------------------------------------------------------------

class ProviderStrategy:
    """Abstract provider strategy.  Subclasses override only what differs."""

    name: str = "base"

    # ── param whitelist ──

    allowed_params: Set[str] = {
        "messages", "temperature", "top_p", "stream", "stop",
        "max_tokens", "presence_penalty", "frequency_penalty", "user",
    }

    def filter_payload(self, body: dict) -> dict:
        """Return a copy of *body* containing only provider-supported keys."""
        return {k: v for k, v in body.items() if k in self.allowed_params}

    # ── API routing ──

    def chat_completions_path(self) -> str:
        raise NotImplementedError

    def chat_completions_stream_path(self) -> str:
        raise NotImplementedError

    # ── tool-call extraction ──

    def extract_provider_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        """Return provider-specific tool calls (if any).

        Generic formats (markdown, JSON, pipe, etc.) are handled by the
        pipeline itself – providers only need to supply *their own* custom
        format here.
        """
        return []

    # ── internal-markup detection / cleanup ──

    _common_markers = (
        "<tool_call>",
        "</tool_call>",
        "[tool_call]",
        "[/tool_call]",
        "$tool_call$",
        "$end$",
        "<tool_response>",
        "</tool_response>",
        "<invoke name=",
        "$params =",
    )

    def _provider_markers(self) -> tuple:
        """Extra markers specific to this provider."""
        return ()

    def internal_markers(self) -> tuple:
        return self._common_markers + self._provider_markers()

    def has_internal_markup(self, text: str) -> bool:
        normalized = (text or "").lower()
        return any(m in normalized for m in self.internal_markers())

    def strip_internal_markup(self, content: str) -> str:
        text = content or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"\$TOOL_CALL\$.*?\$END\$", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<tool_response>.*?</tool_response>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"\$PARAMS\s*=\s*\{.*?\}\s*[A-Za-z_][A-Za-z0-9_]*\(\$PARAMS\)", "", text, flags=re.DOTALL)
        text = re.sub(r'<invoke name="[^"]+">.*?</invoke>', "", text, flags=re.DOTALL | re.IGNORECASE)
        text = self._strip_provider_markup(text)
        text = re.sub(r"^[^\n]{0,4}/agent\s*·[^\n]*\n?", "", text, flags=re.MULTILINE)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _strip_provider_markup(self, text: str) -> str:
        """Override in subclasses for provider-specific regex cleanup."""
        return text


# ---------------------------------------------------------------------------
# MiniMax
# ---------------------------------------------------------------------------

class MiniMaxProvider(ProviderStrategy):
    """MiniMax (MiniMax-M2.7-highspeed etc.)

    - Does NOT support: response_format, max_completion_tokens, logit_bias,
      seed, n (>1), tools, tool_choice, stream_options.
    - Uses custom XML tool-call format: ``<minimax:tool_call><invoke …>``
    """

    name = "minimax"

    allowed_params: Set[str] = {
        "messages", "temperature", "top_p", "stream", "stop",
        "max_tokens", "presence_penalty", "frequency_penalty", "user",
    }

    def chat_completions_path(self) -> str:
        return "/internal/provider/minimax/chat-completions"

    def chat_completions_stream_path(self) -> str:
        return "/internal/provider/minimax/chat-completions/stream"

    # ── MiniMax XML tool calls ──

    _invoke_pattern = re.compile(
        r'<invoke name="([^"]+)"\s*>?\s*(.*?)\s*</invoke>',
        flags=re.DOTALL,
    )
    _param_pattern = re.compile(
        r'<?parameter name="([^"]+)">(.*?)</parameter>',
        flags=re.DOTALL,
    )
    _minimax_block = re.compile(
        r"<minimax:tool_call>\s*(.*?)\s*</minimax:tool_call>",
        flags=re.DOTALL,
    )

    def extract_provider_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        text = content or ""
        for block in self._minimax_block.findall(text):
            for tool_name, param_block in self._invoke_pattern.findall(block):
                params = {}
                for key, value in self._param_pattern.findall(param_block):
                    params[key] = value.strip()
                calls.append({"name": tool_name.strip(), "parameters": params})
        return calls

    # ── markup ──

    def _provider_markers(self) -> tuple:
        return (
            "minimax:tool_call",
            "<minimax:tool_call>",
            "</minimax:tool_call>",
        )

    def _strip_provider_markup(self, text: str) -> str:
        text = re.sub(r"<minimax:tool_call>.*?</minimax:tool_call>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(
            r"minimax:tool_call\s*.*?(?=(?:minimax:tool_call|<tool_response>|</tool_call>|</minimax:tool_call>|$))",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        return text


# ---------------------------------------------------------------------------
# OpenAI-compatible (GPT-4o / GPT-5 / DeepSeek / etc.)
# ---------------------------------------------------------------------------

class OpenAIProvider(ProviderStrategy):
    """Standard OpenAI-compatible provider.

    Supports native function-calling via ``tools`` / ``tool_choice`` as well
    as the broader parameter surface (response_format, seed, logit_bias …).
    """

    name = "openai"

    allowed_params: Set[str] = {
        "messages", "temperature", "top_p", "stream", "stop",
        "max_tokens", "max_completion_tokens",
        "presence_penalty", "frequency_penalty",
        "user", "seed", "n",
        "response_format", "logit_bias",
        "tools", "tool_choice", "stream_options",
    }

    def chat_completions_path(self) -> str:
        return "/internal/provider/openai/chat-completions"

    def chat_completions_stream_path(self) -> str:
        return "/internal/provider/openai/chat-completions/stream"

    # OpenAI native tool_calls come in a structured field, not embedded text.
    # No custom text-based parsing needed.

    def _provider_markers(self) -> tuple:
        return ()

    def _strip_provider_markup(self, text: str) -> str:
        return text


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_PROVIDERS: Dict[str, ProviderStrategy] = {}

def _build_registry() -> Dict[str, ProviderStrategy]:
    minimax = MiniMaxProvider()
    openai = OpenAIProvider()
    return {
        "minimax": minimax,
        "openai": openai,
    }

_PROVIDERS = _build_registry()

# Model-name → provider mapping (prefix match, case-insensitive).
_MODEL_PREFIX_MAP = [
    ("minimax", "minimax"),
    ("gpt-", "openai"),
    ("gpt4", "openai"),
    ("gpt5", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("deepseek", "openai"),
    ("claude", "openai"),
    ("qwen", "openai"),
]

_DEFAULT_PROVIDER_KEY = "minimax"


def get_provider(model_name: str) -> ProviderStrategy:
    """Resolve a provider strategy by model name (case-insensitive prefix match)."""
    lower = (model_name or "").strip().lower()
    for prefix, key in _MODEL_PREFIX_MAP:
        if lower.startswith(prefix):
            return _PROVIDERS[key]
    return _PROVIDERS[_DEFAULT_PROVIDER_KEY]
