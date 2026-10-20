"""The user's Telegram account (Telethon), connected only for the length of one submission."""

from __future__ import annotations

import logging
from types import TracebackType

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    BotResponseTimeoutError,
    SessionRevokedError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession
from telethon.tl import types

from .dialog import DialogError, Reply

_LOGGER = logging.getLogger(__name__)

# Telethon logs every reconnect at INFO
logging.getLogger("telethon").setLevel(logging.WARNING)


class AuthError(Exception):
    """The stored session no longer logs in (revoked in Telegram → Devices, or expired)."""


def make_client(session: str, api_id: int, api_hash: str, *, receive_updates: bool = False) -> TelegramClient:
    # receive_updates=False: we only poll the bot chats we write to, never the account's other chats.
    # QR login needs updates (Telegram announces the scan with UpdateLoginToken).
    return TelegramClient(StringSession(session), api_id, api_hash, receive_updates=receive_updates)


def to_reply(message: types.Message) -> Reply:
    markup = message.reply_markup
    buttons: list[str] = []
    inline: list[str] = []
    if isinstance(markup, types.ReplyKeyboardMarkup):
        buttons = [b.text for row in markup.rows for b in row.buttons]
    elif isinstance(markup, types.ReplyInlineMarkup):
        inline = [b.text for row in markup.rows for b in row.buttons]
    edited = message.edit_date.timestamp() if message.edit_date else None
    return Reply(
        id=message.id,
        text=message.message or "",
        buttons=buttons,
        inline=inline,
        hides_keyboard=isinstance(markup, types.ReplyKeyboardHide),
        edited=edited,
    )


class TelethonChat:
    """``BotChat`` over Telethon."""

    def __init__(self, client: TelegramClient, peer: types.TypeInputPeer) -> None:
        self._client = client
        self._peer = peer

    async def send(self, text: str) -> int:
        return (await self._client.send_message(self._peer, text)).id

    async def share_contact(self) -> int:
        me = await self._client.get_me()
        contact = types.InputMediaContact(
            phone_number=me.phone, first_name=me.first_name or "", last_name=me.last_name or "", vcard=""
        )
        return (await self._client.send_file(self._peer, contact)).id

    async def replies(self, after_id: int) -> list[Reply]:
        messages = await self._client.get_messages(self._peer, limit=20, min_id=after_id)
        return [to_reply(m) for m in reversed(messages) if not m.out]

    async def recent(self, limit: int) -> list[Reply]:
        messages = await self._client.get_messages(self._peer, limit=limit)
        return [to_reply(m) for m in reversed(messages) if not m.out]

    async def click(self, message_id: int, text: str) -> None:
        message = await self._client.get_messages(self._peer, ids=message_id)
        if message is None:
            raise DialogError(f"повідомлення бота {message_id} з кнопкою «{text}» зникло")
        try:
            await message.click(text=text)
        except BotResponseTimeoutError:
            pass  # the press got through; the bot just didn't answer the callback itself


class TelegramSession:
    """``async with TelegramSession(...) as tg: chat = await tg.chat("Yasnoonlinebot")``."""

    def __init__(self, session: str, api_id: int, api_hash: str) -> None:
        self._client = make_client(session, api_id, api_hash)

    async def __aenter__(self) -> TelegramSession:
        await self._client.connect()
        try:
            authorized = await self._client.is_user_authorized()
        except (AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedError) as err:
            await self._client.disconnect()
            raise AuthError(str(err)) from err
        if not authorized:
            await self._client.disconnect()
            raise AuthError("session is not authorized")
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._client.disconnect()

    async def chat(self, username: str) -> TelethonChat:
        return TelethonChat(self._client, await self._client.get_input_entity(username))
