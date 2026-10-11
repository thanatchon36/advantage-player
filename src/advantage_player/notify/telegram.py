"""Telegram: async, throttled alerts that never block trading, plus risk-reducing commands.

- notify() only enqueues (bounded; oldest dropped when full) — trading never waits on HTTP.
- One sender task: ≥ min_interval_s between messages (group limit ≈ 20/min); a backlog is
  coalesced into one message.
- `tables` alerts are debounced (one per debounce window, latest wins).
- Commands are accepted ONLY from TELEGRAM_CHAT_ID; everything else is ignored and logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)
API = "https://api.telegram.org"
MAX_LEN = 4000
Handler = Callable[[list[str]], Awaitable[str]]


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        min_interval_s: float = 3.0,
        debounce_s: float = 1800,
        max_queue: int = 200,
        client: httpx.AsyncClient | None = None,
        api: str = API,
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.min_interval_s = min_interval_s
        self.debounce_s = debounce_s
        self.queue: deque[str] = deque(maxlen=max_queue)
        self._wake = asyncio.Event()
        self._http = client or httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0))
        self._api = f"{api}/bot{token}"
        self.commands: dict[str, Handler] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._offset = 0
        self._last_tables_sent = 0.0
        self._pending_tables: str | None = None
        self.sent: list[str] = []  # last messages (for tests / status)
        self.rejected_chats: int = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    # ---- outbound --------------------------------------------------------------------------
    def notify(self, kind: str, text: str) -> None:
        if not self.enabled:
            return
        if kind == "tables":
            now = time.monotonic()
            if now - self._last_tables_sent < self.debounce_s:
                self._pending_tables = text  # latest wins; flushed by the sender loop
                return
            self._last_tables_sent = now
        self.queue.append(text)
        self._wake.set()

    async def _send(self, text: str) -> bool:
        for chunk in [text[i : i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [""]:
            try:
                r = await self._http.post(
                    f"{self._api}/sendMessage",
                    data={
                        "chat_id": self.chat_id,
                        "text": chunk,
                        "disable_web_page_preview": "true",
                    },
                )
                if r.status_code == 429:
                    retry = float(r.json().get("parameters", {}).get("retry_after", 5))
                    await asyncio.sleep(retry)
                    return False
                if r.status_code >= 400:
                    log.warning("telegram_send_failed", status=r.status_code, body=r.text[:200])
                    return r.status_code < 500  # 4xx won't succeed on retry → drop
            except httpx.HTTPError as e:
                log.warning("telegram_send_error", error=repr(e))
                return False
        self.sent = [*self.sent[-49:], text]
        return True

    async def _sender(self) -> None:
        while True:
            if not self.queue:
                if self._pending_tables and (
                    time.monotonic() - self._last_tables_sent >= self.debounce_s
                ):
                    self.queue.append(self._pending_tables)
                    self._pending_tables = None
                    self._last_tables_sent = time.monotonic()
                    continue
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=30)
                continue
            batch = [self.queue.popleft()]
            while self.queue and sum(len(x) for x in batch) < MAX_LEN - 500:
                batch.append(self.queue.popleft())  # coalesce a backlog
            text = "\n\n".join(batch)
            if not await self._send(text):
                self.queue.appendleft(text)
            await asyncio.sleep(self.min_interval_s)

    # ---- inbound commands ------------------------------------------------------------------
    async def handle_update(self, upd: dict[str, Any]) -> str | None:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = str(msg.get("text") or "").strip()
        if not text.startswith("/"):
            return None
        if chat != self.chat_id:
            self.rejected_chats += 1
            log.warning("telegram_command_rejected", chat=chat)
            return None
        parts = text.split()
        cmd = parts[0][1:].split("@", 1)[0].lower()
        h = self.commands.get(cmd)
        if h is None:
            reply = "commands: " + " ".join(f"/{c}" for c in sorted(self.commands))
        else:
            try:
                reply = await h(parts[1:])
            except Exception as e:
                log.exception("telegram_command_error", cmd=cmd)
                reply = f"error: {e!r}"
        self.queue.appendleft(reply)  # replies jump the queue
        self._wake.set()
        return reply

    async def _poller(self) -> None:
        backoff = 1.0
        while True:
            try:
                r = await self._http.get(
                    f"{self._api}/getUpdates",
                    params={
                        "offset": self._offset,
                        "timeout": 25,
                        "allowed_updates": '["message"]',
                    },
                )
                data = r.json()
                for upd in data.get("result", []):
                    self._offset = int(upd["update_id"]) + 1
                    await self.handle_update(upd)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram_poll_error", error=repr(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def start(self, poll_commands: bool = True) -> None:
        if not self.enabled:
            return
        self._tasks.append(asyncio.create_task(self._sender(), name="tg-send"))
        if poll_commands:
            self._tasks.append(asyncio.create_task(self._poller(), name="tg-poll"))

    async def stop(self, flush_s: float = 5.0) -> None:
        deadline = time.monotonic() + flush_s
        while self.queue and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._http.aclose()
