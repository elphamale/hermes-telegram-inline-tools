"""Minimal inline executor template — copy and modify.

Place in ~/.hermes/inline_executors/<name>.py
Add a matching tool entry in ~/.hermes/inline_tools.yaml
Restart the gateway to load.

The register() function is called by _discover_inline_executors() during
adapter connect(). It must call router.register_executor(name, factory).

The factory is called as factory(tool_config, bot) and must return an
InlineExecutor instance.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from gateway.platforms.telegram_inline_router import InlineExecutor

logger = logging.getLogger(__name__)


class TemplateExecutor(InlineExecutor):
    """Template executor — replace with your logic."""

    def __init__(self, tool_config: Dict[str, Any], bot: Any) -> None:
        self._config = tool_config
        self._bot = bot

    async def execute(self, user_id: int, query: str) -> Dict[str, Any]:
        # ── Authorization (defense-in-depth) ──
        # The inline query handler has no user-allowlist gate.
        # Executors accessing sensitive data should enforce TELEGRAM_ALLOWED_USERS.
        allowed = {
            int(x) for x in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")
            if x.strip().isdigit()
        }
        if allowed and user_id not in allowed:
            raise PermissionError("Not authorized")

        # ── Your logic here ──
        # query is the full inline query text (e.g. "#search term")
        # Strip any prefix your match pattern uses:
        search_term = query.lstrip("#").strip()

        # ── Return a result dict ──
        # For text results:
        return {
            "media_type": "text",
            "text": f"Result for: {search_term}",
            "title": f"Result: {search_term[:60]}",
            "description": "Short description shown in inline picker",
        }

        # For audio results (requires staging a file to get a file_id):
        # return {
        #     "file_id": "telegram_file_id_from_staged_upload",
        #     "title": "Track Title",
        #     "performer": "Artist",
        # }


def register(router) -> None:
    """Register this executor with the TelegramInlineRouter."""
    router.register_executor(
        "template_executor",  # must match the executor field in inline_tools.yaml
        lambda tool_config, bot: TemplateExecutor(tool_config, bot),
    )
    logger.info("[template_executor] registered")
