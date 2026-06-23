"""Inline executor: search and repost Aineko's outbound messages.

Registered as ``inline_repost`` — matches the executor name in inline_tools.yaml.
Searches the Hermes state DB's ``messages_fts_trigram`` table for assistant
messages matching the query, scoped to the N most recent sessions.

Session scope (configurable via ``session_window`` in inline_tools.yaml,
default 5): only messages from the N most recent non-archived sessions that
contain at least one assistant message are searched. This keeps results
current and avoids surfacing months-old context.

Authorization (defense-in-depth):
    The framework's inline query handler has no user-allowlist gate.
    This executor enforces ``TELEGRAM_ALLOWED_USERS`` independently —
    if the env var is set and the querying user is not in it, the executor
    raises ``PermissionError`` and the user sees a "failed" cache entry.

Usage:
    Type ``@inlinebot #<search>`` in any Telegram chat.
    The ``#`` prefix routes to this executor via inline_tools.yaml.
    First query → "Searching..." placeholder.
    Second query → inline article result with the matched message text.
    Tap to send the message content to the current chat.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from gateway.platforms.telegram_inline_router import InlineExecutor

logger = logging.getLogger(__name__)

_state_db_path: Path | None = None


def _get_state_db_path() -> Path:
    global _state_db_path
    if _state_db_path is not None:
        return _state_db_path
    try:
        from hermes_constants import get_hermes_home
        _state_db_path = get_hermes_home() / "state.db"
    except ImportError:
        _state_db_path = Path(
            os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        ) / "state.db"
    return _state_db_path


def _get_allowed_users() -> set[int]:
    raw = os.getenv("TELEGRAM_ALLOWED_USERS", "").strip()
    if not raw:
        return set()
    return {int(uid.strip()) for uid in raw.split(",") if uid.strip().isdigit()}


def _recent_session_ids(conn: sqlite3.Connection, n: int) -> List[str]:
    """Return the IDs of the N most recent non-archived sessions that have
    at least one active assistant text message."""
    rows = conn.execute(
        """
        SELECT s.id
        FROM sessions s
        WHERE s.archived = 0
          AND EXISTS (
              SELECT 1 FROM messages m
              WHERE m.session_id = s.id
                AND m.role = 'assistant'
                AND m.tool_call_id IS NULL
                AND m.active = 1
          )
        ORDER BY s.started_at DESC
        LIMIT ?
        """,
        (n,),
    ).fetchall()
    return [r[0] for r in rows]


def _search_messages(
    query: str,
    session_ids: List[str],
    limit: int = 1,
) -> List[Dict[str, Any]]:
    """Search assistant messages via FTS5 trigram index, restricted to the
    given session IDs."""
    db_path = _get_state_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"State DB not found at {db_path}")

    search_term = query.lstrip("#").strip()
    if len(search_term) < 3:
        raise ValueError("Need at least 3 characters after '#' to search")

    escaped = search_term.replace('"', '""')
    placeholders = ",".join("?" * len(session_ids))

    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        rows = conn.execute(
            f"""
            SELECT m.id, m.content, m.timestamp, m.session_id
            FROM messages_fts_trigram f
            JOIN messages m ON m.id = f.rowid
            WHERE f.content MATCH ?
              AND m.role = 'assistant'
              AND m.tool_call_id IS NULL
              AND m.active = 1
              AND m.session_id IN ({placeholders})
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (f'"{escaped}"', *session_ids, limit),
        ).fetchall()
        return [
            {"id": r[0], "content": r[1], "timestamp": r[2], "session_id": r[3]}
            for r in rows
        ]
    finally:
        conn.close()


class InlineRepostExecutor(InlineExecutor):
    """Search Aineko's outbound messages and return the best match for repost."""

    # How many recent sessions to search (overridable via tool config)
    DEFAULT_SESSION_WINDOW = 5

    def __init__(self, tool_config: Dict[str, Any], bot: Any) -> None:
        self._config = tool_config
        self._bot = bot
        self._session_window: int = int(
            tool_config.get("session_window", self.DEFAULT_SESSION_WINDOW)
        )

    async def execute(self, user_id: int, query: str) -> Dict[str, Any]:
        allowed = _get_allowed_users()
        if allowed and user_id not in allowed:
            logger.warning("[inline_repost] denied user %d", user_id)
            raise PermissionError("Not authorized")

        db_path = _get_state_db_path()
        if not db_path.exists():
            raise FileNotFoundError(f"State DB not found at {db_path}")

        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            session_ids = _recent_session_ids(conn, self._session_window)
        finally:
            conn.close()

        if not session_ids:
            raise ValueError("No sessions found in state DB")

        results = _search_messages(query, session_ids, limit=1)
        if not results:
            raise ValueError(
                f"No messages found matching '{query.lstrip('#')}' "
                f"in the last {len(session_ids)} session(s)"
            )

        msg = results[0]
        content = msg["content"]

        if len(content) > 4096:
            content = content[:4090] + "\n[…]"

        # First non-empty, non-table, non-code line as title
        title = ""
        for line in content.split("\n"):
            s = line.strip()
            if s and not s.startswith("|") and not s.startswith("```"):
                title = s[:80]
                break
        if not title:
            title = f"Message #{msg['id']}"

        logger.info(
            "[inline_repost] user %d reposting msg #%d session=%s (query=%r)",
            user_id, msg["id"], msg["session_id"][:16], query[:60],
        )

        return {
            "media_type": "text",
            "text": content,
            "title": title,
            "description": f"Repost · msg #{msg['id']}",
            "message_id": msg["id"],
        }


def register(router) -> None:
    router.register_executor(
        "inline_repost",
        lambda tool_config, bot: InlineRepostExecutor(tool_config, bot),
    )
    logger.info("[inline_repost] executor registered")
