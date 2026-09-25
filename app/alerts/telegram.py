"""
VFP: Delivery of a ready message to Telegram — one queue, every recipient, with the Bot API's own pace respected.
Changes when: the Bot API changes or the terminal needs another chat.
Anti-goal:
1. Deciding what to send or how it reads — that is core/alerts.py and core/telegram_message.py.
2. Blocking the terminal on the network: sending is a queue, never awaited by the engine.
3. A chat that refuses messages silencing the others — each recipient is delivered to on its own.
4. Reporting a Telegram failure through Telegram — a broken channel would announce its own breakage forever.
5. The token in a log, an error or the journal: it lives in the credential store and never leaves this module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from app.core.telegram_message import Message
from app.system.tls import client_session

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MIN_INTERVAL_S = 1.1
"""Telegram throttles a single chat at about one message a second; the queue keeps under that by itself."""
MAX_ATTEMPTS = 5
QUEUE_LIMIT = 200


@dataclass(frozen=True, slots=True)
class Post:
    message: Message
    key: str | None = None
    """Feed key of the gap, so the sender can remember which message announced it."""


@dataclass(frozen=True, slots=True)
class Amend:
    sent: tuple[tuple[str, int], ...]
    """(чат, id сообщения) — то же дополнение уходит в каждый чат, где сообщение было."""
    text: str


class TelegramSender:
    def __init__(
        self,
        token: str | None,
        chats: Sequence[str],
        session_factory: Callable[..., Any] = client_session,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._token = token
        self._chats = [str(chat) for chat in chats if str(chat).strip()]
        self._session_factory = session_factory
        self._sleep = sleep
        self._clock = clock
        self._queue: asyncio.Queue[Post | Amend] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._last_sent = 0.0
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.last_error: str | None = None
        self.message_ids: dict[str, tuple[tuple[str, int], ...]] = {}
        self._session: Any | None = None

    @property
    def dry_run(self) -> bool:
        """No token: the terminal still writes every message to the log, so it can be read before a bot exists."""
        return not self._token

    def post(self, message: Message, key: str | None = None) -> None:
        self._enqueue(Post(message, key))

    def amend(self, sent: tuple[tuple[str, int], ...], text: str) -> None:
        self._enqueue(Amend(tuple(sent), text))

    def _enqueue(self, item: Post | Amend) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # A queue this long means Telegram has been unreachable for minutes; the newest gaps matter more.
            self.dropped += 1
            log.warning("telegram queue is full, message dropped")

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "dry_run": self.dry_run,
            "chats": len(self._chats),
            "queued": self._queue.qsize(),
            "sent": self.sent,
            "failed": self.failed,
            "dropped": self.dropped,
            "last_error": self.last_error,
        }

    async def run(self) -> None:
        # One session for the whole run: a message every few seconds should not open a new connection each time.
        self._session = None if self.dry_run else self._session_factory()
        try:
            await self._loop()
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def _loop(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._pace()
                await self._deliver(item)
            except Exception as exc:  # a failed message must not stop the queue
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("telegram delivery failed: %s", self.last_error)
            finally:
                self._queue.task_done()

    async def _pace(self) -> None:
        wait = MIN_INTERVAL_S - (self._clock() - self._last_sent)
        if wait > 0:
            await self._sleep(wait)
        self._last_sent = self._clock()

    async def _deliver(self, item: Post | Amend) -> None:
        if isinstance(item, Post):
            delivered: list[tuple[str, int]] = []
            for chat in self._chats:
                payload: dict[str, Any] = {
                    "chat_id": chat,
                    "text": item.message.text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                if item.message.buttons:
                    payload["reply_markup"] = {
                        "inline_keyboard": [[{"text": text, "url": url} for text, url in item.message.buttons]]
                    }
                result = await self._one(chat, "sendMessage", payload)
                if result:
                    delivered.append((chat, int(result["message_id"])))
            if item.key and delivered:
                self.message_ids[item.key] = tuple(delivered)
        else:
            for chat, message_id in item.sent:
                await self._one(
                    chat,
                    "editMessageText",
                    {
                        "chat_id": chat,
                        "message_id": message_id,
                        "text": item.text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )

    async def _one(self, chat: str, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """One recipient: its refusal is counted and logged, and the next recipient still gets the message."""
        try:
            result = await self._call(method, payload)
            self.sent += 1
            return result
        except Exception as exc:
            self.failed += 1
            self.last_error = f"{chat}: {type(exc).__name__}: {exc}"
            log.warning("telegram delivery to %s failed: %s", chat, self.last_error)
            return None

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if self.dry_run:
            log.info("[telegram] %s\n%s", method, payload.get("text", ""))
            return None
        url = f"{API}/bot{self._token}/{method}"
        session = self._session if self._session is not None else self._session_factory()
        for attempt in range(MAX_ATTEMPTS):
            async with session.post(url, json=payload) as response:
                body = await response.json(content_type=None)
                if response.status == 200 and body.get("ok"):
                    return body.get("result")
                retry_after = float((body.get("parameters") or {}).get("retry_after", 0))
                # 429 says exactly how long to wait; everything else gets a growing pause.
                if retry_after <= 0 and response.status < 500:
                    raise RuntimeError(f"{method} refused: {body.get('description', response.status)}")
            await self._sleep(retry_after or 2**attempt)
        raise RuntimeError(f"{method} gave up after {MAX_ATTEMPTS} attempts")
