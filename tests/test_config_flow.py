"""Telegram login: code, two-step password, re-authentication, options."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import SessionPasswordNeededError

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.meter_bots.const import (
    CONF_API_HASH,
    CONF_API_ID,
    CONF_CODE,
    CONF_KOMUNALKA_ADDRESS,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_SESSION,
    CONF_YASNO_ACCOUNT,
    DOMAIN,
)

from .conftest import DATA, PHONE, USER_ID, make_entry, setup_entry

USER_INPUT = {CONF_API_ID: 12345, CONF_API_HASH: " hash ", CONF_PHONE: "+380 50 123 4567"}


async def _to_code(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "send_code"})
    assert result["step_id"] == "code"
    assert result["description_placeholders"]["delivery"].startswith("в застосунок")
    return result


async def _to_qr(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "qr"})
    assert result["step_id"] == "qr"
    return result


async def test_login_with_qr(hass: HomeAssistant, login_client):
    result = await _to_qr(hass)
    login_client.qr.outcome.set_result(SimpleNamespace(id=USER_ID))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SESSION] == "new-session"


async def test_qr_expired_shows_a_new_one(hass: HomeAssistant, login_client):
    result = await _to_qr(hass)
    login_client.qr.outcome.set_exception(TimeoutError())
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "qr_expired"}
    assert login_client.qr.tokens == 2
    login_client.qr.outcome.set_exception(SessionPasswordNeededError(request=None))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "password"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "secret"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_qr_not_scanned_yet(hass: HomeAssistant, login_client):
    result = await _to_qr(hass)
    with patch("custom_components.meter_bots.config_flow.asyncio.wait", AsyncMock()):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "qr_not_scanned"}
    assert login_client.qr.tokens == 1


async def test_login_with_code(hass: HomeAssistant, login_client):
    result = await _to_code(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: "00000"})
    assert result["errors"] == {CONF_CODE: "invalid_code"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: " 12345 "})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Telegram Тест"
    assert result["data"] == {CONF_API_ID: 12345, CONF_API_HASH: "hash", CONF_PHONE: PHONE, CONF_SESSION: "new-session"}
    assert result["result"].unique_id == str(USER_ID)
    assert not login_client.connected


async def test_login_with_two_step_password(hass: HomeAssistant, login_client):
    result = await _to_code(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: "22222"})
    assert result["step_id"] == "password"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "wrong"})
    assert result["errors"] == {CONF_PASSWORD: "invalid_password"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "secret"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_same_account_twice_aborts(hass: HomeAssistant, login_client):
    make_entry().add_to_hass(hass)
    result = await _to_code(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: "12345"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_replaces_session(hass: HomeAssistant, login_client):
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "send_code"})
    assert result["step_id"] == "code"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: "12345"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_SESSION] == "new-session"
    assert entry.data[CONF_PHONE] == DATA[CONF_PHONE]


async def test_options(hass: HomeAssistant, bots):
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_YASNO_ACCOUNT: " 123 ", CONF_KOMUNALKA_ADDRESS: ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_YASNO_ACCOUNT: "123"}
