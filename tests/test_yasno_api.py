"""YASNO's web API: the client, the submission order (API first, bot as fallback) and the address in the options."""

from __future__ import annotations

from http import HTTPStatus

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.meter_bots import yasno_api
from custom_components.meter_bots.const import (
    CONF_YASNO_ACCOUNT,
    CONF_YASNO_APARTMENT,
    CONF_YASNO_CITY,
    CONF_YASNO_HOUSE,
    CONF_YASNO_INTERNAL_ID,
    CONF_YASNO_STREET,
    DOMAIN,
)
from custom_components.meter_bots.yasno_api import YasnoRejected, YasnoUnavailable

from .conftest import make_entry, setup_entry
from .fakes import YASNO_ACCOUNT

BASE = yasno_api.BASE
INTERNAL_ID = 1000
ACCOUNT_URL = f"{BASE}/public/users/me/accounts/b2c/{INTERNAL_ID}"
JSON = {"content-type": "application/json; charset=utf-8"}
PROBLEM = {"content-type": "application/problem+json"}
DEBT = {
    "balance": 1000.5,
    "lastMeterReadings": {
        "owner": "Client",
        "meteringReadings": [{"zone": "Day", "value": 15000.0}, {"zone": "Night", "value": 7514.0}],
        "createdOn": "2026-10-22T00:00:00+00:00",
    },
}
ALREADY = {
    "title": "ModelValidationException",
    "status": 400,
    "detail": "На зазначену дату вже є показання",
    "code": "ValidationError",
}
ATTEMPT = "sensor.telegram_test_yasno_last_attempt"


def mock_account(aioclient_mock: AiohttpClientMocker, *, post_status=HTTPStatus.OK, post_json=None, **post) -> None:
    aioclient_mock.get(f"{ACCOUNT_URL}/debt", json=DEBT, headers=JSON)
    if post_json is None and not post:
        post_json = {"feeId": "2026-09-30T21:00:00+00:00", "balance": 1250.5, "isEnrolledLater": False}
    if post_json is not None:
        post.setdefault("json", post_json)
        post.setdefault("headers", PROBLEM if post_status != HTTPStatus.OK else JSON)
    aioclient_mock.post(f"{ACCOUNT_URL}/meter-readings", status=post_status, **post)


def mock_address(aioclient_mock: AiohttpClientMocker, masked: str = "10******001") -> None:
    aioclient_mock.get(
        f"{BASE}/public/addresses/cities",
        json={
            "totalCount": 2,
            "items": [{"id": 1, "value": "м. Київ  Київська обл."}, {"id": 2, "value": "с. Київське  Тестова обл."}],
        },
        headers=JSON,
    )
    aioclient_mock.get(
        f"{BASE}/public/addresses/cities/1/streets",
        json={"totalCount": 1, "items": [{"id": 10, "value": "вул. Тестова"}]},
        headers=JSON,
    )
    aioclient_mock.get(
        f"{BASE}/public/users/me/accounts/by-address",
        json={"accountId": INTERNAL_ID, "accountNumberMasked": masked, "region": "Kyiv"},
        headers=JSON,
    )


def posts(aioclient_mock: AiohttpClientMocker) -> list:
    return [c for c in aioclient_mock.mock_calls if c[0].lower() == "post"]


async def _run(hass: HomeAssistant, **kwargs):
    kwargs.setdefault("dry_run", False)
    kwargs.setdefault("max_increase", 3000)
    api = yasno_api.YasnoApi(async_get_clientsession(hass))
    return await yasno_api.yasno(api, INTERNAL_ID, 15006.6, 7514.2, **kwargs)


# --- the client ---------------------------------------------------------------------------------------------


async def test_submits_whole_kwh(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_account(aioclient_mock)
    results = await _run(hass)
    assert posts(aioclient_mock)[0][2] == {
        "meteringReadings": [{"value": 15006, "zone": "Day"}, {"value": 7514, "zone": "Night"}]
    }
    assert [r["status"] for r in results] == ["submitted", "submitted"]
    assert [r["previous"] for r in results] == [15000.0, 7514.0]
    assert results[0]["previous_date"] == "2026-10-22"
    assert results[0]["balance"] == 1250.5


async def test_dry_run_reads_only(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_account(aioclient_mock)
    results = await _run(hass, dry_run=True)
    assert posts(aioclient_mock) == []
    assert {r["status"] for r in results} == {"dry_run"}
    assert results[0]["balance"] == 1000.5


async def test_lower_than_previous_is_not_sent(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_account(aioclient_mock)
    api = yasno_api.YasnoApi(async_get_clientsession(hass))
    results = await yasno_api.yasno(api, INTERNAL_ID, 14000, 7514, dry_run=False, max_increase=3000)
    assert posts(aioclient_mock) == []
    assert [r["status"] for r in results] == ["rejected", "no_reading"]


async def test_validation_error_is_a_refusal(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_account(aioclient_mock, post_status=HTTPStatus.BAD_REQUEST, post_json=ALREADY)
    with pytest.raises(YasnoRejected, match="вже є показання"):
        await _run(hass)


@pytest.mark.parametrize(
    "post",
    [
        {"post_status": HTTPStatus.SERVICE_UNAVAILABLE, "post_json": {"error": "busy"}},
        {
            "post_status": HTTPStatus.FORBIDDEN,
            "text": "<html>Incapsula</html>",
            "headers": {"content-type": "text/html"},
        },
        {"post_status": HTTPStatus.OK, "text": "<html>challenge</html>", "headers": {"content-type": "text/html"}},
        {"exc": aiohttp.ClientConnectionError("down")},
        {"exc": TimeoutError()},
    ],
    ids=["5xx", "403-html", "200-html", "connection", "timeout"],
)
async def test_no_usable_answer(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, post):
    mock_account(aioclient_mock, **post)
    with pytest.raises(YasnoUnavailable):
        await _run(hass)


async def test_find_account_by_address(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_address(aioclient_mock)
    api = yasno_api.YasnoApi(async_get_clientsession(hass))
    found = await api.find_account("Київ", "тестова", "1", "1")
    assert found["accountId"] == INTERNAL_ID
    by_address = next(c for c in aioclient_mock.mock_calls if "by-address" in str(c[1]))
    assert by_address[1].query == {"cityId": "1", "streetId": "10", "houseNumber": "1", "apartmentNumber": "1"}
    # the street search needs limit/offset, or YASNO answers with an empty list
    street = next(c for c in aioclient_mock.mock_calls if "streets" in str(c[1]))
    assert street[1].query["limit"] == "100"


async def test_find_account_unknown_address(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(
        f"{BASE}/public/addresses/cities",
        json={"items": [{"id": 1, "value": "м. Київ  Київська обл."}]},
        headers=JSON,
    )
    aioclient_mock.get(f"{BASE}/public/addresses/cities/1/streets", json={"items": []}, headers=JSON)
    api = yasno_api.YasnoApi(async_get_clientsession(hass))
    with pytest.raises(YasnoRejected, match="вулицю"):
        await api.find_account("Київ", "Невідома", "1", "1")


def test_pick_prefers_exact_name_and_refuses_ambiguity():
    items = [{"id": 1, "value": "м. Київ  Київська обл."}, {"id": 2, "value": "с. Київське  Тестова обл."}]
    assert yasno_api.pick(items, "київ", "місто")["id"] == 1
    assert yasno_api.pick(items, "київськ", "місто")["id"] == 2
    with pytest.raises(YasnoRejected, match="кілька"):
        yasno_api.pick(items + [{"id": 3, "value": "с. Київське  Інша обл."}], "київськ", "місто")


def test_mask_matches():
    assert yasno_api.mask_matches("10******001", " 10000000001 ")
    assert not yasno_api.mask_matches("10******002", "10000000001")
    assert not yasno_api.mask_matches("10*****001", "10000000001")


# --- the service: API first, bot as fallback ------------------------------------------------------------------


async def _submit(hass: HomeAssistant, **data):
    data = {"bot": "yasno", "readings": {"day": 15006, "night": 7514}, **data}
    return await hass.services.async_call(DOMAIN, "submit", data, blocking=True, return_response=True)


def api_entry():
    return make_entry(**{CONF_YASNO_INTERNAL_ID: INTERNAL_ID})


async def test_api_is_the_main_way(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_account(aioclient_mock)
    await setup_entry(hass, api_entry())
    response = await _submit(hass)
    assert response["result"] == "ok"
    assert response["via"] == "api"
    assert response["api_error"] is None
    assert bots.opened == []  # Telegram wasn't touched
    assert hass.states.get(ATTEMPT).attributes["via"] == "api"
    await hass.async_block_till_done()
    assert hass.states.get("sensor.telegram_test_yasno_day").state == "15000.0"


async def test_refusal_does_not_go_to_the_bot(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_account(aioclient_mock, post_status=HTTPStatus.BAD_REQUEST, post_json=ALREADY)
    await setup_entry(hass, api_entry())
    response = await _submit(hass)
    assert response["result"] == "failed"
    assert "вже є показання" in response["error"]
    assert bots.opened == []


async def test_bot_is_the_fallback(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_account(aioclient_mock, post_status=HTTPStatus.SERVICE_UNAVAILABLE, post_json={"error": "busy"})
    await setup_entry(hass, api_entry())
    response = await _submit(hass)
    assert response["result"] == "ok"
    assert response["via"] == "telegram"
    assert "503" in response["api_error"]
    assert bots.bots["Yasnoonlinebot"].submitted == ["15006 7514"]
    assert hass.states.get(ATTEMPT).attributes["api_error"] == response["api_error"]


async def test_via_api_has_no_fallback(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_account(aioclient_mock, exc=aiohttp.ClientConnectionError("down"))
    await setup_entry(hass, api_entry())
    response = await _submit(hass, via="api")
    assert response["result"] == "failed"
    assert response["via"] == "api"
    assert bots.opened == []


async def test_via_telegram_skips_the_api(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    await setup_entry(hass, api_entry())
    response = await _submit(hass, via="telegram", dry_run=True)
    assert response["result"] == "dry_run"
    assert response["via"] == "telegram"
    assert aioclient_mock.call_count == 0


async def test_without_address_only_the_bot(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    await setup_entry(hass, make_entry())
    response = await _submit(hass)
    assert response["via"] == "telegram"
    assert aioclient_mock.call_count == 0
    with pytest.raises(ServiceValidationError):
        await _submit(hass, via="api")


# --- options: the address gives the internal id ---------------------------------------------------------------

ADDRESS = {CONF_YASNO_STREET: "Тестова", CONF_YASNO_HOUSE: "1", CONF_YASNO_APARTMENT: "1"}


async def _options(hass: HomeAssistant, entry, user_input):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(result["flow_id"], user_input)


async def test_options_find_internal_id(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_address(aioclient_mock)
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await _options(hass, entry, {CONF_YASNO_ACCOUNT: YASNO_ACCOUNT, **ADDRESS})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_YASNO_ACCOUNT: YASNO_ACCOUNT,
        CONF_YASNO_CITY: "Київ",
        **ADDRESS,
        CONF_YASNO_INTERNAL_ID: INTERNAL_ID,
    }
    # Saved again unchanged: no new lookup
    calls = aioclient_mock.call_count
    await hass.async_block_till_done()
    result = await _options(hass, entry, {CONF_YASNO_ACCOUNT: YASNO_ACCOUNT, CONF_YASNO_CITY: "Київ", **ADDRESS})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_YASNO_INTERNAL_ID] == INTERNAL_ID
    assert aioclient_mock.call_count == calls


async def test_options_account_must_match(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_address(aioclient_mock, masked="10******999")
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await _options(hass, entry, {CONF_YASNO_ACCOUNT: YASNO_ACCOUNT, **ADDRESS})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_YASNO_ACCOUNT: "yasno_account_mismatch"}


async def test_options_incomplete_address(hass: HomeAssistant, bots):
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await _options(hass, entry, {CONF_YASNO_ACCOUNT: YASNO_ACCOUNT, CONF_YASNO_STREET: "Тестова"})
    assert result["errors"] == {"base": "yasno_address_incomplete"}


async def test_options_clearing_address_drops_internal_id(hass: HomeAssistant, bots):
    entry = api_entry()
    await setup_entry(hass, entry)
    result = await _options(hass, entry, {CONF_YASNO_ACCOUNT: YASNO_ACCOUNT})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_YASNO_INTERNAL_ID not in entry.options
