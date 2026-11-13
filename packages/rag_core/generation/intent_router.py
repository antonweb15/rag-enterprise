"""Intent Router module (`core/soul.md` §2.3, `core/architecture.md` §2 p.4, Stage F).

Classifies user queries into:
- ``IntentType.RAG_SEARCH`` (documents search);
- ``IntentType.CHIT_CHAT`` (greetings, thanks, small talk);
- ``IntentType.META_COMMAND`` (system info, commands, source listings).
"""

from __future__ import annotations

import json
import re
from typing import Any

from rag_core.contracts.enums import IntentType
from rag_core.contracts.interfaces import BaseIntentRouter, BaseLLM
from rag_core.contracts.settings import Settings

INTENT_ROUTER_SYSTEM_PROMPT = """You are the user-intent classifier of a corporate bot.
Determine the message category and return the result strictly in JSON format:
{{"intent": "<category>", "reason": "<brief_reason>"}}

Possible categories:
1. "rag_search" — searching company docs (regulations, guidelines, pricing, facts).
2. "chit_chat" — greeting, thanks, farewell, small talk ("Hi", "Thanks", "Who are you?").
3. "meta_command" — bot help, available sources, system status ("/help", "What can you do?").

USER MESSAGE:
{user_query}"""

# Deterministic fast-path regex patterns
_META_COMMAND_RE = re.compile(
    r"^(?:/[a-zA-Z0-9_\-]+|какие документы(?: загружены)?|"
    r"что ты умеешь\??|список источников|what can you do\??|help|list sources)$",
    re.IGNORECASE,
)
_CHIT_CHAT_RE = re.compile(
    r"^(?:привет|здравствуй(?:те)?|добрый (?:день|вечер|утро)|"
    r"спасибо(?: большое)?|благодарю|пока|до свидания|"
    r"hello|hi|hey|thanks|thank you|goodbye|bye)[!.]*$",
    re.IGNORECASE,
)


class IntentRouter(BaseIntentRouter):
    """Classifies user queries to decide whether to hit RAG, chit-chat or return meta info."""

    def __init__(self, llm: BaseLLM, settings: Settings | None = None) -> None:
        self._llm = llm
        self._settings = settings or Settings()

    async def route_intent(self, query: str) -> IntentType:
        trimmed = query.strip()
        if not trimmed:
            return IntentType.CHIT_CHAT

        # 1. Deterministic fast heuristics for obvious commands / small-talk
        if _META_COMMAND_RE.match(trimmed):
            return IntentType.META_COMMAND
        if _CHIT_CHAT_RE.match(trimmed):
            return IntentType.CHIT_CHAT

        # 2. LLM-based classification with strict JSON output format
        prompt = INTENT_ROUTER_SYSTEM_PROMPT.format(user_query=trimmed)
        try:
            raw_res = await self._llm.generate(
                prompt=prompt,
                response_format="json_object",
                temperature=0.0,
                max_tokens=100,
            )
            intent = self._parse_intent_json(raw_res)
            if intent is not None:
                return intent
        except Exception:  # noqa: BLE001
            # Fail-safe: low-confidence/failure falls back to RAG search
            pass

        return IntentType.RAG_SEARCH

    @staticmethod
    def _parse_intent_json(raw_text: str) -> IntentType | None:
        """Robustly extracts JSON object and maps the 'intent' field."""
        cleaned = raw_text.strip()
        # Remove markdown codeblock wrappers if present
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            cleaned = cleaned.strip()

        # Find the first JSON object block if surrounded by extraneous text
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)

        try:
            parsed: dict[str, Any] = json.loads(cleaned)
            intent_val = str(parsed.get("intent", "")).strip().lower()
            if intent_val in (IntentType.RAG_SEARCH.value, "rag_search"):
                return IntentType.RAG_SEARCH
            if intent_val in (IntentType.CHIT_CHAT.value, "chit_chat"):
                return IntentType.CHIT_CHAT
            if intent_val in (IntentType.META_COMMAND.value, "meta_command", "meta"):
                return IntentType.META_COMMAND
        except Exception:  # noqa: BLE001
            return None
        return None
