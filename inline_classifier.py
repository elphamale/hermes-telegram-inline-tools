"""Hermes gateway plugin: semantic classifier for Telegram inline queries.

Monkey-patches TelegramInlineRouter.dispatch to add two-stage routing:

  Stage 1 — Prefix match (O(1)):
    If any enabled tool declares a ``prefix:`` field and the query starts
    with that string, only those tools are dispatched — no model inference.

  Stage 2 — Embedding similarity (fastembed BAAI/bge-small-en-v1.5):
    For queries that don't match any prefix, encode the query and compare
    cosine similarity against each tool's ``description:`` embedding.
    Tools above SIMILARITY_THRESHOLD are dispatched.

  Fallback: if fastembed is unavailable or no tool clears the threshold,
  all non-prefix enabled tools are dispatched (fail-open).

Tool embeddings are built once at init and rebuilt whenever
~/.hermes/inline_tools.yaml mtime changes (stat-checked per dispatch).

Results from multiple executors are merged in FILO order (last-registered
tool's results appear first in the response list).

Bot-username filtering: tools that declare ``handles: [botname, ...]`` are
only dispatched when ``router.bot_username`` matches one of the listed names.
The adapter sets ``router.bot_username`` after ``app.initialize()``.

Install:
  mkdir -p ~/.hermes/plugins/inline-classifier
  cp inline_classifier.py ~/.hermes/plugins/inline-classifier/__init__.py
  Create ~/.hermes/plugins/inline-classifier/plugin.yaml:
    name: inline-classifier
    kind: standalone
    version: "1.0.0"
  hermes plugins enable inline-classifier
  Restart the gateway.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_REGISTRY_PATH = os.path.join(
    os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
    "inline_tools.yaml",
)
SIMILARITY_THRESHOLD = float(os.environ.get("INLINE_CLASSIFIER_THRESHOLD", "0.35"))
_MODEL_NAME = "BAAI/bge-small-en-v1.5"


# ---------------------------------------------------------------------------
# Classifier state (module-level singleton)
# ---------------------------------------------------------------------------

class _Classifier:
    def __init__(self) -> None:
        self._tools: List[Dict[str, Any]] = []
        self._non_prefix_tools: List[Dict[str, Any]] = []
        self._mtime: float = 0.0
        self._embeddings: Dict[str, Any] = {}  # tool_id -> np.ndarray
        self._model: Optional[Any] = None
        self._model_ok: bool = False
        self._init_done: bool = False

    def _try_init_model(self) -> None:
        if self._init_done:
            return
        self._init_done = True
        try:
            from fastembed import TextEmbedding
            t0 = time.perf_counter()
            self._model = TextEmbedding(model_name=_MODEL_NAME)
            logger.info("[inline_classifier] fastembed model loaded in %.1fs", time.perf_counter() - t0)
            self._model_ok = True
        except Exception as exc:
            logger.warning("[inline_classifier] fastembed unavailable — falling back to dispatch-all: %s", exc)
            self._model_ok = False

    def _load_yaml(self) -> None:
        try:
            import yaml
            with open(_REGISTRY_PATH) as fh:
                data = yaml.safe_load(fh) or {}
            self._tools = [t for t in data.get("tools", []) if t.get("enabled", False)]
            # Tools without a prefix: field are eligible for embedding-based routing.
            self._non_prefix_tools = [t for t in self._tools if not t.get("prefix")]
        except Exception as exc:
            logger.warning("[inline_classifier] YAML load failed: %s", exc)
            self._tools = []
            self._non_prefix_tools = []

    def _build_embeddings(self) -> None:
        if not self._model_ok or self._model is None:
            return
        t0 = time.perf_counter()
        self._embeddings = {}
        for tool in self._tools:
            desc = tool.get("description", "").strip()
            if not desc:
                continue
            try:
                import numpy as np
                vecs = list(self._model.embed([desc]))
                if vecs:
                    v = np.array(vecs[0], dtype=float)
                    norm = np.linalg.norm(v)
                    self._embeddings[tool["id"]] = v / norm if norm > 0 else v
            except Exception as exc:
                logger.warning("[inline_classifier] embed failed for %r: %s", tool.get("id"), exc)
        logger.info(
            "[inline_classifier] encoded %d tool embeddings in %.2fs",
            len(self._embeddings), time.perf_counter() - t0,
        )

    def maybe_reload(self) -> None:
        """Reload YAML + re-encode if mtime changed."""
        try:
            mtime = os.stat(_REGISTRY_PATH).st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        self._try_init_model()
        self._load_yaml()
        self._build_embeddings()

    def _prefix_tools(self, query: str) -> List[Dict[str, Any]]:
        """Return tools whose ``prefix:`` field matches the start of query."""
        matched = []
        for tool in self._tools:
            prefix = tool.get("prefix")
            if prefix and query.startswith(str(prefix)):
                matched.append(tool)
        return matched

    def _embedding_tools(self, query: str) -> List[Dict[str, Any]]:
        """Return non-prefix tools above similarity threshold, ordered by score desc.

        Prefix-keyed tools are never candidates here — they only match via _prefix_tools.
        """
        candidates = self._non_prefix_tools
        if not self._model_ok or self._model is None or not self._embeddings:
            return candidates  # fail-open: all non-prefix tools
        try:
            import numpy as np
            vecs = list(self._model.embed([query]))
            if not vecs:
                return candidates
            qv = np.array(vecs[0], dtype=float)
            norm = np.linalg.norm(qv)
            if norm > 0:
                qv = qv / norm
            scored = []
            for tool in candidates:
                tid = tool.get("id", "")
                if tid not in self._embeddings:
                    continue
                score = float(np.dot(qv, self._embeddings[tid]))
                if score >= SIMILARITY_THRESHOLD:
                    scored.append((score, tool))
            if not scored:
                return candidates  # fail-open: no strong match
            scored.sort(key=lambda x: x[0], reverse=True)
            return [t for _, t in scored]
        except Exception as exc:
            logger.warning("[inline_classifier] embedding inference failed: %s", exc)
            return candidates  # fail-open

    def select_tools(self, query: str, bot_username: Optional[str]) -> List[Dict[str, Any]]:
        """Return candidate tools for this query, filtered by bot_username."""
        prefix_candidates = self._prefix_tools(query)
        if prefix_candidates:
            candidates = prefix_candidates
        else:
            candidates = self._embedding_tools(query)

        if not bot_username:
            return candidates

        filtered = []
        for tool in candidates:
            handles = tool.get("handles")
            if not handles:
                filtered.append(tool)
            elif bot_username.lstrip("@").lower() in [h.lstrip("@").lower() for h in handles]:
                filtered.append(tool)
        return filtered if filtered else candidates  # fail-open if all filtered


_classifier = _Classifier()


# ---------------------------------------------------------------------------
# Patched dispatch
# ---------------------------------------------------------------------------

async def _classifier_dispatch(router_self: Any, user_id: int, query: str) -> List[Any]:
    """Replacement for TelegramInlineRouter.dispatch with classifier routing."""
    try:
        _classifier.maybe_reload()
    except Exception as exc:
        logger.warning("[inline_classifier] reload error: %s", exc)

    try:
        candidates = _classifier.select_tools(query, getattr(router_self, "bot_username", None))
    except Exception as exc:
        logger.warning("[inline_classifier] select_tools error: %s", exc)
        candidates = _classifier._non_prefix_tools or _classifier._tools  # fail-open

    if not candidates:
        # Fall back to the registry's own matcher
        try:
            tool = router_self._registry.match(query)
            candidates = [tool] if tool else []
        except Exception:
            candidates = []

    results: List[Any] = []
    # FILO: iterate in reverse so last-registered tool's results go last,
    # then reverse the final list so they appear first.
    for tool in reversed(candidates):
        executor_name = tool.get("executor", "")
        factory = router_self._executors.get(executor_name)
        if factory is None:
            continue
        try:
            executor = factory(tool, router_self._bot)
            timeout = tool.get("timeout_sec", 10)
            tool_results = await asyncio.wait_for(
                executor.execute(user_id, query),
                timeout=float(timeout),
            )
            results.extend(tool_results or [])
        except asyncio.TimeoutError:
            logger.warning(
                "[inline_classifier] executor %r timed out for query %r",
                executor_name, query[:60],
            )
        except Exception as exc:
            logger.error(
                "[inline_classifier] executor %r raised: %s",
                executor_name, exc,
            )

    results.reverse()  # FILO: last-registered first in output
    return results


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Patch TelegramInlineRouter.dispatch with the classifier at load time."""
    try:
        from gateway.platforms.telegram_inline_router import TelegramInlineRouter
    except ImportError as exc:
        logger.warning("[inline_classifier] TelegramInlineRouter not importable — skipping: %s", exc)
        return

    if getattr(TelegramInlineRouter, "_classifier_patched", False):
        logger.debug("[inline_classifier] dispatch already patched, skipping")
        return

    import types

    original_dispatch = TelegramInlineRouter.dispatch

    async def _patched_dispatch(self, user_id: int, query: str) -> List[Any]:
        return await _classifier_dispatch(self, user_id, query)

    TelegramInlineRouter.dispatch = _patched_dispatch
    TelegramInlineRouter._classifier_patched = True
    TelegramInlineRouter._original_dispatch = original_dispatch

    # Trigger initial load + encode on first call (lazy — bot may not be up yet)
    logger.info("[inline_classifier] dispatch patched; model will load on first query")
