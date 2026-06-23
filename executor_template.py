"""Minimal inline executor template — copy and modify.

Place in ~/.hermes/inline_executors/<name>.py
Add a matching tool entry in ~/.hermes/inline_tools.yaml
Restart the gateway to load.

The register() function is called by _discover_inline_executors() during
adapter connect(). It must call router.register_executor(name, factory).

The factory is called as factory(tool_config, bot) and must return an
InlineExecutor instance.

execute() must return a List of Telegram InlineQueryResult objects.
The adapter passes this list directly to iq.answer() — the executor owns
the full result lifecycle, including stub placeholders for slow operations.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from gateway.platforms.telegram_inline_router import InlineExecutor

try:
    from telegram import (
        InlineQueryResultArticle,
        InlineQueryResultCachedAudio,
        InputTextMessageContent,
    )
except ImportError:
    InlineQueryResultArticle = Any  # type: ignore[misc,assignment]
    InlineQueryResultCachedAudio = Any  # type: ignore[misc,assignment]
    InputTextMessageContent = Any  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


class TemplateExecutor(InlineExecutor):
    """Template executor — replace with your logic."""

    def __init__(self, tool_config: Dict[str, Any], bot: Any) -> None:
        self._config = tool_config
        self._bot = bot

    @staticmethod
    def get_stub(query: str) -> Any:
        """Return an instant placeholder result for slow operations.

        Call execute() in the background; return [get_stub(query)] immediately
        so Telegram sees a result within the 6.5 s deadline.
        """
        return InlineQueryResultArticle(
            id="stub",
            title="⏳ Searching...",
            description="Retype to see results",
            input_message_content=InputTextMessageContent(message_text=query[:256]),
        )

    async def execute(self, user_id: int, query: str) -> List[Any]:
        # ── Authorization (defense-in-depth) ──
        # Auth is enforced at the adapter level for most use cases.
        # Executors accessing sensitive data may add a second check here.
        allowed = {
            int(x) for x in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")
            if x.strip().isdigit()
        }
        if allowed and user_id not in allowed:
            raise PermissionError("Not authorized")

        # ── Your logic here ──
        search_term = query.lstrip("#").strip()

        # ── Return a list of InlineQueryResult objects ──
        # Text result:
        return [
            InlineQueryResultArticle(
                id="result",
                title=f"Result: {search_term[:60]}",
                description="Short description shown in inline picker",
                input_message_content=InputTextMessageContent(
                    message_text=f"Result for: {search_term}",
                ),
            )
        ]

        # Audio result (requires staging a file to get a file_id first):
        # return [
        #     InlineQueryResultCachedAudio(
        #         id="audio_result",
        #         audio_file_id="telegram_file_id_from_staged_upload",
        #         caption="",
        #     )
        # ]


def register(router) -> None:
    """Register this executor with the TelegramInlineRouter."""
    router.register_executor(
        "template_executor",  # must match the executor field in inline_tools.yaml
        lambda tool_config, bot: TemplateExecutor(tool_config, bot),
    )
    logger.info("[template_executor] registered")
