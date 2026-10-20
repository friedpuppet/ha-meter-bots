"""Talking to a chat bot the way a person does: send a message, wait until the bot has finished answering."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import re
import time
from typing import Any, Protocol


@dataclass
class Reply:
    """One message from the bot, as it looks right now (bots edit their messages)."""

    id: int
    text: str
    buttons: list[str] = field(default_factory=list)  # reply keyboard, flattened
    inline: list[str] = field(default_factory=list)  # inline buttons, flattened
    hides_keyboard: bool = False
    edited: float | None = None


class BotChat(Protocol):
    """A private chat with one bot, seen from the user's account."""

    async def send(self, text: str) -> int:
        """Send a text; return the message id."""

    async def share_contact(self) -> int:
        """Send the account's own phone number as a contact (the "share my number" button)."""

    async def replies(self, after_id: int) -> list[Reply]:
        """The bot's messages newer than ``after_id``, oldest first."""

    async def recent(self, limit: int) -> list[Reply]:
        """The bot's last ``limit`` messages, oldest first."""

    async def click(self, message_id: int, text: str) -> None:
        """Press the inline button ``text`` under the bot's message ``message_id``."""


class DialogError(Exception):
    """The bot didn't answer, or answered something the scenario doesn't expect."""


class Dialog:
    """Sends messages, waits for the bot to go quiet, keeps the current keyboard and a transcript."""

    def __init__(
        self,
        chat: BotChat,
        *,
        timeout: float = 30.0,
        quiet: float = 2.0,
        poll: float = 1.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._chat = chat
        self._timeout = timeout
        self._quiet = quiet
        self._poll = poll
        self._sleep = sleep
        self._clock = clock
        self.keyboard: list[str] = []
        self.inline: list[str] = []
        self.inline_message: int | None = None
        self.transcript: list[dict[str, Any]] = []
        self._known: dict[int, tuple[float | None, str]] = {}  # message id -> (edited, text) as last seen

    async def say(self, text: str) -> str:
        """Send ``text``; return everything the bot answered, joined by blank lines."""
        self.transcript.append({"from": "me", "text": text})
        return await self._wait(await self._chat.send(text))

    async def share_contact(self) -> str:
        self.transcript.append({"from": "me", "text": "<contact>"})
        return await self._wait(await self._chat.share_contact())

    async def refresh(self) -> None:
        """Pick up the keyboards of the bot's last messages without sending anything."""
        self._take(await self._chat.recent(5), earlier=True)

    async def click(self, text: str) -> str:
        """Press an inline button; the bot usually edits that message or sends new ones."""
        if self.inline_message is None:
            raise DialogError(f"немає inline-кнопки «{text}»")
        self.transcript.append({"from": "me", "click": text})
        clicked = self.inline_message
        before = dict(self._known)
        await self._chat.click(clicked, text)
        return await self._wait(clicked - 1, unchanged=before)

    async def press(self, *words: str) -> str:
        """A reply-keyboard button (sent as text) or an inline button (clicked), whichever has ``words``."""
        if found := self.find_button(*words):
            return await self.say(found)
        if found := self.find_inline(*words):
            return await self.click(found)
        raise DialogError(f"немає кнопки «{' '.join(words)}» серед {self.keyboard} / {self.inline}")

    async def _wait(self, sent_id: int, unchanged: dict[int, tuple[float | None, str]] | None = None) -> str:
        start = self._clock()
        seen: list[tuple[int, float | None, str]] | None = None
        seen_at = start
        while True:
            replies = await self._chat.replies(sent_id)
            if unchanged:
                replies = [r for r in replies if unchanged.get(r.id) != (r.edited, r.text)]
            now = self._clock()
            signature = [(r.id, r.edited, r.text) for r in replies]
            if signature != seen:
                seen, seen_at = signature, now
            elif replies and now - seen_at >= self._quiet:
                break
            if now - start >= self._timeout:
                if not replies:
                    raise DialogError("бот не відповів")
                break
            await self._sleep(self._poll)

        self._take(replies)
        return "\n\n".join(r.text for r in replies if r.text)

    def _take(self, replies: list[Reply], *, earlier: bool = False) -> None:
        for reply in replies:
            self._known[reply.id] = (reply.edited, reply.text)
            if reply.buttons:
                self.keyboard = reply.buttons
            elif reply.hides_keyboard:
                self.keyboard = []
            if reply.inline:
                self.inline, self.inline_message = reply.inline, reply.id
            entry: dict[str, Any] = {"from": "bot", "text": reply.text}
            if earlier:
                entry["earlier"] = True
            if reply.buttons:
                entry["buttons"] = reply.buttons
            if reply.inline:
                entry["inline"] = reply.inline
            self.transcript.append(entry)

    def button(self, *words: str) -> str:
        """The keyboard button containing all ``words`` (case-insensitive)."""
        found = self.find_button(*words)
        if found is None:
            raise DialogError(f"немає кнопки «{' '.join(words)}» серед {self.keyboard}")
        return found

    def find_button(self, *words: str) -> str | None:
        return _find(self.keyboard, words)

    def find_inline(self, *words: str) -> str | None:
        return _find(self.inline, words)


def _find(buttons: list[str], words: tuple[str, ...]) -> str | None:
    for text in buttons:
        if all(w.casefold() in text.casefold() for w in words):
            return text
    return None


def expect(text: str, pattern: str, what: str) -> re.Match[str]:
    """Search ``pattern`` in the bot's answer or stop the scenario."""
    match = re.search(pattern, text, re.IGNORECASE)
    if match is None:
        raise DialogError(f"очікувалось «{what}», бот відповів: {text!r}")
    return match
