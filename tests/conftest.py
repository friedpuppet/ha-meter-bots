"""Fixtures: a fake Telegram session that hands out the scripted bots, and a fake Telethon client for login."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError
from telethon.tl.types import auth

from homeassistant.core import HomeAssistant

from custom_components.meter_bots.client import AuthError
from custom_components.meter_bots.const import (
    BOT_USERNAMES,
    CONF_API_HASH,
    CONF_API_ID,
    CONF_PHONE,
    CONF_SESSION,
    CONF_YASNO_ACCOUNT,
    DOMAIN,
)
from custom_components.meter_bots.dialog import Dialog

from .fakes import YASNO_ACCOUNT, FakeKomunalka, FakeYasno

PHONE = "+380501234567"
USER_ID = 123456789
DATA = {CONF_API_ID: 12345, CONF_API_HASH: "hash", CONF_PHONE: PHONE, CONF_SESSION: "session-string"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_retry_pauses():
    """The sites are retried at once in tests; the number of tries stays as in production."""
    with patch("custom_components.meter_bots.RETRY_DELAYS", (0, 0)):
        yield


class FakeSession:
    """Replaces ``TelegramSession``; ``authorized`` is flipped by tests."""

    authorized = True

    def __init__(self, bots: dict[str, Any]) -> None:
        self.bots = bots
        self.opened: list[tuple[str, int, str]] = []

    def __call__(self, session: str, api_id: int, api_hash: str) -> FakeSession:
        self.opened.append((session, api_id, api_hash))
        return self

    async def __aenter__(self) -> FakeSession:
        if not self.authorized:
            raise AuthError("session is not authorized")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def chat(self, username: str) -> Any:
        return self.bots[username]


@pytest.fixture
def bots():
    """The scripted bots, behind a patched session; dialogs don't wait."""
    fake = FakeSession({BOT_USERNAMES["komunalka"]: FakeKomunalka(), BOT_USERNAMES["yasno"]: FakeYasno()})
    original = Dialog.__init__

    def quick(self, chat, **kwargs):
        async def no_sleep(_):
            return None

        original(self, chat, timeout=1, quiet=0, poll=0, sleep=no_sleep)

    with (
        patch("custom_components.meter_bots.TelegramSession", fake),
        patch.object(Dialog, "__init__", quick),
    ):
        yield fake


def make_entry(**options: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Telegram Тест",
        unique_id=str(USER_ID),
        data=dict(DATA),
        options={CONF_YASNO_ACCOUNT: YASNO_ACCOUNT, **options},
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


class FakeClient:
    """Telethon client for the config flow: code 12345 logs in, 22222 needs the password «secret»."""

    def __init__(self) -> None:
        self.connected = False
        self.signed_in = False
        self.codes_sent = 0
        self.session = SimpleNamespace(save=lambda: "new-session")

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def send_code_request(self, phone: str) -> Any:
        self.codes_sent += 1
        return auth.SentCode(type=auth.SentCodeTypeApp(length=5), phone_code_hash="hash123")

    async def sign_in(self, phone=None, code=None, *, password=None, phone_code_hash=None) -> None:
        if password is not None:
            if password != "secret":
                from telethon.errors import PasswordHashInvalidError

                raise PasswordHashInvalidError(request=None)
            self.signed_in = True
            return
        assert phone_code_hash == "hash123"
        if code == "22222":
            raise SessionPasswordNeededError(request=None)
        if code != "12345":
            raise PhoneCodeInvalidError(request=None)
        self.signed_in = True

    async def qr_login(self) -> FakeQR:
        self.qr = FakeQR(self)
        return self.qr

    async def get_me(self) -> Any:
        assert self.signed_in
        return SimpleNamespace(id=USER_ID, first_name="Тест", last_name=None, phone=PHONE[1:])


class FakeQR:
    """Telethon QRLogin: the test resolves ``scanned`` (or lets it time out / ask for the password)."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.tokens = 1
        self.outcome: asyncio.Future = asyncio.get_running_loop().create_future()

    @property
    def url(self) -> str:
        return f"tg://login?token=t{self.tokens}"

    async def recreate(self) -> None:
        self.tokens += 1
        self.outcome = asyncio.get_running_loop().create_future()

    async def wait(self) -> Any:
        result = await self.outcome
        if isinstance(result, BaseException):
            raise result
        self.client.signed_in = True
        return result


@pytest.fixture
def login_client():
    client = FakeClient()
    with patch("custom_components.meter_bots.config_flow.make_client", return_value=client):
        yield client
