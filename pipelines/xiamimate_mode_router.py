"""
title: XiaMimate Slash Router
author: GitHub Copilot
date: 2026-04-14
version: 0.1.0
description: Filter pipeline that routes /agent /report /wf /workflow onto the single XiaMimate agent model.
"""

import os
from typing import List, Optional, Tuple

from pydantic import BaseModel


COMMAND_TO_MODE = {
    "/agent": "agent",
    "/report": "report",
    "/tool": "tool",
    "/web": "web",
    "/wf": "workflow",
    "/workflow": "workflow",
}


class Pipeline:
    class Valves(BaseModel):
        pipelines: List[str] = ["*"]
        priority: int = 0
        model_prefix: str = "xiamimate"

    def __init__(self):
        self.type = "filter"
        self.name = "XiaMimate Slash Router"
        self.valves = self.Valves(
            **{
                "pipelines": ["*"],
                "priority": 0,
                "model_prefix": os.getenv("XIAMIMATE_MODEL_PREFIX", "xiamimate"),
            }
        )

    async def on_startup(self):
        print("on_startup:xiamimate_mode_router")

    async def on_shutdown(self):
        print("on_shutdown:xiamimate_mode_router")

    async def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
        if body.get("title"):
            return body

        messages = body.get("messages") or []
        last_user_message = self._find_last_user_message(messages)
        if last_user_message is None:
            return body

        current_text = self._read_message_text(last_user_message)
        command, remainder = self._parse_command(current_text)
        if not command:
            return body

        target_mode = COMMAND_TO_MODE[command]
        body["model"] = "%s.agent" % self.valves.model_prefix
        body["xiamimate_mode"] = target_mode
        self._apply_mode_features(body, target_mode)
        self._write_message_text(last_user_message, remainder)
        body["messages"] = messages
        return body

    def _find_last_user_message(self, messages: List[dict]) -> Optional[dict]:
        for message in reversed(messages):
            if message.get("role") == "user":
                return message
        return None

    def _read_message_text(self, message: dict) -> str:
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

    def _write_message_text(self, message: dict, replacement: str) -> None:
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = replacement
            return
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    item["text"] = replacement
                    return
            content.insert(0, {"type": "text", "text": replacement})
            message["content"] = content
            return
        message["content"] = replacement

    def _parse_command(self, text: str) -> Tuple[Optional[str], str]:
        stripped = (text or "").lstrip()
        if not stripped.startswith("/"):
            return None, text

        first_token, _, remainder = stripped.partition(" ")
        normalized = first_token.lower().rstrip("：:")
        if normalized not in COMMAND_TO_MODE:
            return None, text

        return normalized, remainder.strip()

    def _apply_mode_features(self, body: dict, mode: str) -> None:
        features = body.get("features")
        if not isinstance(features, dict):
            features = {}
            body["features"] = features

        features["web_search"] = False

    def _fallback_prompt(self, mode: str) -> str:
        prompts = {
            "agent": "请分析这个商品主题，并在需要时调用工具。",
            "tool": "请只调用必要工具，不要联网搜索。",
            "web": "请基于联网搜索结果给出总结。",
            "workflow": "",
        }
        return prompts.get(mode, "请继续。")