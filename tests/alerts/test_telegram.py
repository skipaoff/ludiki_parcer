"""Delivery to Telegram: one chat, its pace, its refusals — and never the token in an error."""

import asyncio
from types import SimpleNamespace

from app.alerts.telegram import TelegramSender
from app.core.telegram_message import Message

TOKEN = "123456:AAH-secret-bot-token"


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, calls, answers):
        self._calls = calls
        self._answers = answers
        self.closed = False

    def post(self, url, json):
        self._calls.append((url, json))
        answer = self._answers.pop(0) if self._answers else (200, {"ok": True, "result": {"message_id": 777}})
        return FakeResponse(*answer)

    async def close(self):
        self.closed = True


def sender(answers=None, token=TOKEN):
    calls: list = []
    waits: list[float] = []
    clock = SimpleNamespace(now=1000.0)

    async def sleep(seconds):
        waits.append(seconds)
        clock.now += seconds

    session = FakeSession(calls, list(answers or []))
    made = TelegramSender(
        token,
        chat_id="-100500",
        session_factory=lambda: session,
        sleep=sleep,
        clock=lambda: clock.now,
    )
    return made, calls, waits


async def drain(made: TelegramSender):
    worker = asyncio.ensure_future(made.run())
    await made._queue.join()
    worker.cancel()


async def test_a_gap_message_goes_to_the_chat_with_its_buttons_and_the_id_is_remembered():
    made, calls, _ = sender()
    made.post(Message(text="<b>SOL</b>", buttons=(("GATE ↗", "https://gate.example"),)), key="pair")

    await drain(made)

    url, payload = calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload["chat_id"] == "-100500" and payload["parse_mode"] == "HTML"
    assert payload["reply_markup"]["inline_keyboard"] == [[{"text": "GATE ↗", "url": "https://gate.example"}]]
    assert made.message_ids["pair"] == 777 and made.sent == 1


async def test_the_outcome_edits_the_message_that_announced_the_gap():
    made, calls, _ = sender()
    made.amend(777, "текст\n\n✓ прожила 7 мин")

    await drain(made)

    url, payload = calls[0]
    assert url.endswith("/editMessageText")
    assert payload["message_id"] == 777 and payload["text"].endswith("✓ прожила 7 мин")


async def test_too_many_messages_waits_exactly_as_long_as_telegram_asks():
    made, calls, waits = sender(answers=[(429, {"ok": False, "parameters": {"retry_after": 7}}), (200, {"ok": True, "result": {"message_id": 5}})])
    made.post(Message(text="раз"))

    await drain(made)

    assert 7 in waits and len(calls) == 2 and made.sent == 1


async def test_a_refusal_that_is_not_about_pace_is_not_retried_forever():
    made, calls, _ = sender(answers=[(400, {"ok": False, "description": "chat not found"})])
    made.post(Message(text="раз"))

    await drain(made)

    assert len(calls) == 1 and made.failed == 1
    assert "chat not found" in (made.last_error or "") and TOKEN not in (made.last_error or "")


async def test_without_a_token_messages_go_to_the_log_instead_of_the_network():
    made, calls, _ = sender(token=None)
    assert made.dry_run
    made.post(Message(text="раз"))

    await drain(made)

    assert calls == [] and made.sent == 1


async def test_a_queue_that_cannot_drain_drops_instead_of_growing_without_end():
    made, _, _ = sender()
    for index in range(250):
        made.post(Message(text=str(index)))

    assert made.dropped > 0 and made._queue.qsize() <= 200
