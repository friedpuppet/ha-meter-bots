"""The submit service, the sensors and re-authentication."""

from __future__ import annotations

import pytest

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.meter_bots.const import DOMAIN

from .conftest import make_entry, setup_entry

KOMUNALKA_ATTEMPT = "sensor.telegram_test_komunalka_last_attempt"
KOMUNALKA_SUBMISSION = "sensor.telegram_test_komunalka_last_submission"
YASNO_ATTEMPT = "sensor.telegram_test_yasno_last_attempt"


async def _submit(hass: HomeAssistant, **data):
    return await hass.services.async_call(DOMAIN, "submit", data, blocking=True, return_response=True)


async def test_komunalka_submit_updates_sensors(hass: HomeAssistant, bots):
    await setup_entry(hass, make_entry())
    response = await _submit(hass, bot="komunalka", readings={1000001: 222.017, "1000003": 1008.107})

    assert response["result"] == "ok"
    assert [r["status"] for r in response["results"]].count("submitted") == 3
    assert response["transcript"][0] == {"from": "me", "text": "/start"}
    assert hass.states.get(KOMUNALKA_ATTEMPT).state == "ok"
    assert hass.states.get(KOMUNALKA_SUBMISSION).state not in ("unknown", "unavailable")


async def test_dry_run_is_not_a_submission(hass: HomeAssistant, bots):
    await setup_entry(hass, make_entry())
    response = await _submit(hass, bot="komunalka", readings={"1000001": 222.0}, dry_run=True)
    assert response["result"] == "dry_run"
    assert hass.states.get(KOMUNALKA_ATTEMPT).state == "dry_run"
    assert hass.states.get(KOMUNALKA_SUBMISSION).state == "unknown"


async def test_rejected_value_fails_the_run(hass: HomeAssistant, bots):
    await setup_entry(hass, make_entry())
    response = await _submit(hass, bot="komunalka", readings={"1000001": 1.0})
    assert response["result"] == "failed"
    assert hass.states.get(KOMUNALKA_ATTEMPT).state == "failed"


async def test_yasno_needs_day_and_night_and_account(hass: HomeAssistant, bots):
    await setup_entry(hass, make_entry())
    with pytest.raises(ServiceValidationError):
        await _submit(hass, bot="yasno", readings={"total": 22520})
    response = await _submit(hass, bot="yasno", readings={"day": 15006, "night": 7514})
    assert response["result"] == "ok"
    assert hass.states.get(YASNO_ATTEMPT).state == "ok"


async def test_yasno_without_account(hass: HomeAssistant, bots):
    await setup_entry(hass, make_entry(yasno_account=""))
    with pytest.raises(ServiceValidationError):
        await _submit(hass, bot="yasno", readings={"day": 1, "night": 1})


async def test_dead_session_starts_reauth(hass: HomeAssistant, bots):
    bots.authorized = False
    entry = make_entry()
    await setup_entry(hass, entry)
    with pytest.raises(HomeAssistantError, match="увійдіть знову"):
        await _submit(hass, bot="komunalka", readings={"1000001": 222.0})
    await hass.async_block_till_done()
    # The failure shows on the dashboard too, not only as the service's error
    attempt = hass.states.get(KOMUNALKA_ATTEMPT)
    assert attempt.state == "failed"
    assert "увійдіть знову" in attempt.attributes["error"]
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_state_survives_reload(hass: HomeAssistant, bots):
    entry = make_entry()
    await setup_entry(hass, entry)
    await _submit(hass, bot="komunalka", readings={"1000001": 222.017})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(KOMUNALKA_ATTEMPT).state == "ok"


async def test_supplier_readings_become_sensors(hass: HomeAssistant, bots):
    entry = make_entry()
    await setup_entry(hass, entry)
    await _submit(hass, bot="komunalka", readings={"1000001": 222.0}, dry_run=True)
    await _submit(hass, bot="yasno", readings={"day": 15006, "night": 7514}, dry_run=True)
    await hass.async_block_till_done()

    hot = hass.states.get("sensor.telegram_test_komunalka_1000001")
    assert hot.state == "208.0"
    assert hot.attributes["items"] == {"ГАРЯЧА ВОДА": 208.0, "ВОДОВІДВЕДЕННЯ ГАРЯЧОЇ ВОДИ": 216.7}
    assert hot.attributes["date"] == "2026-10-19"
    assert hass.states.get("sensor.telegram_test_yasno_day").state == "15000.0"
    assert hass.states.get("sensor.telegram_test_yasno_night").attributes["date"] == "2026-10-19"

    # They come back after a reload, from storage
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.telegram_test_komunalka_1000001").state == "208.0"


async def test_retryable_tells_a_failure_from_a_refusal(hass: HomeAssistant, bots):
    await setup_entry(hass, make_entry())
    response = await _submit(hass, bot="komunalka", readings={"1000001": 1.0})  # lower than the bot's
    assert response["result"] == "failed"
    assert response["retryable"] is False
    response = await _submit(hass, bot="komunalka", readings={"1000001": 222.0})
    assert response["retryable"] is False  # ok
    bots.bots["komunalkaua_bot"].answer = lambda text: None  # the bot stops answering
    response = await _submit(hass, bot="komunalka", readings={"1000001": 222.0})
    assert response["result"] == "failed"
    assert response["retryable"] is True
    assert hass.states.get(KOMUNALKA_ATTEMPT).attributes["retryable"] is True
