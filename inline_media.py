"""Inline executor: download audio or video via yt-dlp.

Registered as two executors — ``inline_media_audio`` and ``inline_media_video`` —
both backed by the same class, parameterized by ``tool_config["media_type"]``.
The classifier selects which executor to call based on the query.

Two-phase UX (downloads take 15–45 s, well over the 6.5 s adapter deadline):
  First query  → stub "Downloading..." returned immediately, background dl starts.
  Repeat query → InlineQueryResultCachedAudio / InlineQueryResultCachedVideo once
                 ready, or stub again if still in-flight, or error article on failure.

Cache keys are scoped by media type so the same query can have independent
audio and video entries.
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
        InlineQueryResultCachedVideo,
        InputTextMessageContent,
    )
except ImportError:
    InlineQueryResultArticle = Any  # type: ignore[misc,assignment]
    InlineQueryResultCachedAudio = Any  # type: ignore[misc,assignment]
    InlineQueryResultCachedVideo = Any  # type: ignore[misc,assignment]
    InputTextMessageContent = Any  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)

_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov")

# Module-level result cache — keyed by "<media_type>:<query.lower()>"
_cache: Dict[str, Dict[str, Any]] = {}


class InlineMediaExecutor(InlineExecutor):
    """Download audio or video and return a cached Telegram inline result."""

    def __init__(self, tool_config: Dict[str, Any], bot: Any) -> None:
        self._config = tool_config
        self._bot = bot
        self._media_type: str = tool_config.get("media_type", "audio")
        staging_env = tool_config.get("staging_chat_env", "TELEGRAM_HOME_CHANNEL")
        self._staging_chat: Optional[str] = os.environ.get(staging_env)

    def get_stub(self, query: str) -> Any:
        label = "video" if self._media_type == "video" else "audio"
        return InlineQueryResultArticle(
            id=f"downloading_{self._media_type}",
            title=f"Downloading {label}...",
            description=f"Wait ~20 s then type again: {query[:60]}",
            input_message_content=InputTextMessageContent(
                message_text=f"Downloading {label}: {query[:80]}",
            ),
        )

    async def execute(self, user_id: int, query: str) -> List[Any]:
        key = f"{self._media_type}:{query.lower()}"
        entry = _cache.get(key)

        if entry:
            status = entry.get("status")
            if status == "ready":
                if self._media_type == "video":
                    return [
                        InlineQueryResultCachedVideo(
                            id=key[:64],
                            video_file_id=entry["file_id"],
                            title=entry.get("title", query[:60]),
                        )
                    ]
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
                        id=f"failed_{self._media_type}",
                        title="Download failed — try again",
                        description=err,
                        input_message_content=InputTextMessageContent(
                            message_text=f"Download failed: {err}",
                        ),
                    )
                ]

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
            await self._run_download(
                container, container_out_dir, search_query, timeout, self._media_type
            )

            staging = self._staging_chat or ""
            if not staging:
                logger.warning(
                    "[inline_media] no staging chat configured for %r", key[:60]
                )
                _cache[key] = {"status": "failed", "error": "no staging chat configured"}
                return

            if self._media_type == "video":
                files = [
                    f for f in os.listdir(host_out_dir)
                    if f.lower().endswith(_VIDEO_EXTENSIONS)
                ]
                if not files:
                    raise RuntimeError("Downloader produced no video file")
                file_path = os.path.join(host_out_dir, files[0])
                _, title = self._split_stem(files[0].rsplit(".", 1)[0])
                with open(file_path, "rb") as fh:
                    msg = await self._bot.send_video(
                        chat_id=staging,
                        video=fh,
                        caption=title,
                        disable_notification=True,
                    )
                _cache[key] = {
                    "status": "ready",
                    "file_id": msg.video.file_id,
                    "title": title,
                }
            else:
                mp3s = [f for f in os.listdir(host_out_dir) if f.endswith(".mp3")]
                if not mp3s:
                    raise RuntimeError("Downloader produced no audio file")
                file_path = os.path.join(host_out_dir, mp3s[0])
                performer, title = self._split_stem(mp3s[0][:-4])
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

            logger.info("[inline_media] cached %s file_id for %r", self._media_type, key[:60])

        except Exception as exc:
            logger.warning("[inline_media] %s download failed for %r: %s", self._media_type, key[:60], exc)
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
    async def _run_download(
        container: str, out_dir: str, query: str, timeout_sec: int, media_type: str
    ) -> None:
        if media_type == "video":
            flags = (
                '-f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best" '
                "--merge-output-format mp4 "
            )
        else:
            flags = (
                "--cookies /mnt/hermes_home/youtube_cookies.txt "
                "-f bestaudio --extract-audio --audio-format mp3 --audio-quality 0 "
            )
        bash = (
            f"mkdir -p {out_dir} && "
            f"python3 -m yt_dlp "
            f"{flags}"
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
        "inline_media_audio",
        lambda tool_config, bot: InlineMediaExecutor(tool_config, bot),
    )
    router.register_executor(
        "inline_media_video",
        lambda tool_config, bot: InlineMediaExecutor(tool_config, bot),
    )
    logger.info("[inline_media] audio + video executors registered")
