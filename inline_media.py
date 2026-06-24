"""Inline executor: download audio from Spotify/YouTube/SoundCloud/Apple Music URLs.

Registered as ``inline_media`` — matches the executor name in inline_tools.yaml.
Downloads audio via the hermes-agent Docker container, stages it to a Telegram
chat to obtain a file_id, then returns an InlineQueryResultCachedAudio.

Two-phase UX (download takes 15–25 s, well over the 6.5 s adapter deadline):
  First query → returns stub "Downloading..." article immediately, starts background dl.
  Repeat same query → returns InlineQueryResultCachedAudio once ready, or stub again
  if still in-flight, or an error article if the download failed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import uuid
from typing import Any, Dict, List, Optional

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

# Module-level result cache — keyed by query.lower()
_cache: Dict[str, Dict[str, Any]] = {}


class InlineMediaExecutor(InlineExecutor):
    """Download audio and return an InlineQueryResultCachedAudio."""

    def __init__(self, tool_config: Dict[str, Any], bot: Any) -> None:
        self._config = tool_config
        self._bot = bot
        staging_env = tool_config.get("staging_chat_env", "TELEGRAM_HOME_CHANNEL")
        self._staging_chat: Optional[str] = os.environ.get(staging_env)

    @staticmethod
    def get_stub(query: str) -> Any:
        return InlineQueryResultArticle(
            id="downloading",
            title="Downloading...",
            description=f"Wait ~15 s then type again: {query[:60]}",
            input_message_content=InputTextMessageContent(
                message_text=f"Downloading: {query[:80]}",
            ),
        )

    async def execute(self, user_id: int, query: str) -> List[Any]:
        key = query.lower()
        entry = _cache.get(key)

        if entry:
            status = entry.get("status")
            if status == "ready":
                return [
                    InlineQueryResultCachedAudio(
                        id=key[:64],
                        audio_file_id=entry["file_id"],
                        caption="",
                    )
                ]
            if status == "downloading":
                return [self.get_stub(query)]
            if status == "failed":
                err = entry.get("error", "unknown error")[:60]
                _cache.pop(key, None)  # evict so next query retries
                return [
                    InlineQueryResultArticle(
                        id="failed",
                        title="Download failed — try again",
                        description=err,
                        input_message_content=InputTextMessageContent(
                            message_text=f"Download failed: {err}",
                        ),
                    )
                ]

        # Not cached — kick off background download and return stub immediately
        _cache[key] = {"status": "downloading"}
        asyncio.ensure_future(self._download_and_cache(query, key))
        return [self.get_stub(query)]

    async def _download_and_cache(self, query: str, key: str) -> None:
        host_out_dir: Optional[str] = None
        try:
            container = await self._find_container()
            search_query = self._resolve_spotify(query)

            dl_id = uuid.uuid4().hex[:12]
            hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
            host_out_dir = os.path.join(hermes_home, "inline_dl", dl_id)
            container_out_dir = f"/mnt/hermes_home/inline_dl/{dl_id}"
            os.makedirs(host_out_dir, exist_ok=True)

            timeout = self._config.get("timeout_sec", 25)
            await self._run_download(container, container_out_dir, search_query, timeout)

            mp3s = [f for f in os.listdir(host_out_dir) if f.endswith(".mp3")]
            if not mp3s:
                raise RuntimeError("Downloader produced no output file")

            file_path = os.path.join(host_out_dir, mp3s[0])
            performer, title = self._split_stem(mp3s[0][:-4])

            staging = self._staging_chat or ""
            if not staging:
                logger.warning("[inline_media] no staging chat configured — cannot stage file_id for %r", key[:60])
                _cache[key] = {"status": "failed", "error": "no staging chat configured"}
                return

            with open(file_path, "rb") as fh:
                msg = await self._bot.send_audio(
                    chat_id=staging,
                    audio=fh,
                    title=title,
                    performer=performer,
                    disable_notification=True,
                )

            _cache[key] = {
                "status": "ready",
                "file_id": msg.audio.file_id,
                "title": title,
                "performer": performer,
            }
            logger.info("[inline_media] cached file_id for %r", key[:60])

        except Exception as exc:
            logger.warning("[inline_media] download failed for %r: %s", key[:60], exc)
            _cache[key] = {"status": "failed", "error": str(exc)[:200]}
        finally:
            if host_out_dir:
                shutil.rmtree(host_out_dir, ignore_errors=True)

    @staticmethod
    async def _find_container() -> str:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps",
            "--filter", "label=hermes-agent=1",
            "--filter", "label=hermes-profile=default",
            "--format", "{{.Names}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        names = out.decode().strip().splitlines()
        if not names:
            raise RuntimeError("hermes agent container (label=hermes-agent=1) not found")
        return names[0]

    @staticmethod
    def _resolve_spotify(query: str) -> str:
        if "spotify.com/track/" not in query:
            return query
        m = re.search(r"/track/([A-Za-z0-9]+)", query)
        if not m:
            return query
        try:
            import urllib.request as _urlreq
            req = _urlreq.Request(
                f"https://open.spotify.com/track/{m.group(1)}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with _urlreq.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            og = re.search(r'property="og:title"[^>]*content="([^"]+)"', html)
            if og:
                return og.group(1)
        except Exception:
            pass
        return query

    @staticmethod
    async def _run_download(container: str, out_dir: str, query: str, timeout_sec: int) -> None:
        bash = (
            f"mkdir -p {out_dir} && "
            f"python3 -m yt_dlp "
            f"--cookies /mnt/hermes_home/youtube_cookies.txt "
            f"-f bestaudio --extract-audio --audio-format mp3 --audio-quality 0 "
            f"--no-playlist "
            f"-o '{out_dir}/%(uploader)s - %(title)s.%(ext)s' "
            f'"ytsearch1:$YTDLP_QUERY"'
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-e", f"YTDLP_QUERY={query}",
            container, "bash", "-c", bash,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec + 60)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Download timed out")

        if proc.returncode not in (0, 1):
            err = (stderr or b"").decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(err or "yt-dlp returned non-zero exit code")

    @staticmethod
    def _split_stem(stem: str):
        if " - " in stem:
            performer, title = stem.split(" - ", 1)
            return performer.strip(), title.strip()
        return "", stem


def register(router) -> None:
    router.register_executor(
        "inline_media",
        lambda tool_config, bot: InlineMediaExecutor(tool_config, bot),
    )
    logger.info("[inline_media] executor registered")
