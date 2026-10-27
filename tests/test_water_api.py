"""Water without the bot: Київводоканал's public form, Київтеплоенерго's cabinet, the bot for what's left."""

from __future__ import annotations

from http import HTTPStatus

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.meter_bots import kte_api, kvk_api
from custom_components.meter_bots.const import CONF_KTE_EMAIL, CONF_KTE_PASSWORD, CONF_KVK_ACCOUNT, DOMAIN
from custom_components.meter_bots.kte_api import KteRejected, KteUnavailable
from custom_components.meter_bots.kvk_api import KvkRejected, KvkUnavailable

from .conftest import make_entry, setup_entry

KVK_ACCOUNT = "1000000000"
KTE_LOGIN = {CONF_KTE_EMAIL: "test@example.com", CONF_KTE_PASSWORD: "secret"}
JSON = {"content-type": "application/json"}
HTML = {"content-type": "text/html; charset=utf-8"}
ATTEMPT = "sensor.telegram_test_komunalka_last_attempt"

# Same meters as the fake bot (tests/fakes.py): 1000001/1000002 hot, 1000003/1000004 cold
READINGS = {"1000001": 222.017, "1000002": 123.36, "1000003": 1008.107, "1000004": 356.134}


def counter(id_: int, serial: str, kind: str, previous: float) -> dict:
    return {
        "id": id_,
        "counterNumber": serial,
        "beginFactor": previous - 1,
        "endFactor": previous,
        "dateFrom": "2026-10-25T08:12:04Z",
        "counterType": {"id": 2 if kind == "Холодна вода" else 3, "name": kind},
        "invoiceAccount": {"id": 1, "code": KVK_ACCOUNT},
    }


COUNTERS = [
    counter(1, "1000004", "Холодна вода", 353.8),
    counter(2, "1000003", "Холодна вода", 1003.4),
    counter(3, "1000002", "Гаряча вода", 122.3),
    counter(4, "1000001", "Гаряча вода", 216.7),
]

LOGIN_PAGE = """<html><title>Немає доступу | Київтеплоенерго</title>
<form action="/my-cabinet/userhome_new?destination=userhome_new" method="post" id="user-login-form">
<input type="text" name="name" value="" /><input type="password" name="pass" />
<input type="hidden" name="form_build_id" value="form-login" />
<input type="hidden" name="form_id" value="user_login_block" />
<button type="submit" name="op" value="Увійти">Увійти</button></form></html>"""
HOME_PAGE = '<html><a href="/my-cabinet/send_counters/send_current_new/77">Передача показань</a></html>'
LOGIN_ERROR = (
    '<html><div class="alert alert-block messages error">Невірна адреса електронної пошти або пароль.</div>'
    + LOGIN_PAGE
    + "</html>"
)


def kte_row(serial: str, place: str, counter_id: str, previous: str) -> str:
    return f"""<tr><td>{serial}</td><td>{place}</td><td>ГВ</td><td>м3</td>
<td>{previous} <a href="/my-cabinet/send_counters/history_counters/77/{counter_id.split("_")[0]}">Історія лічильника</a></td>
<td><input maxlength="11" placeholder="00000.0000" class="form-control form-text" type="text"
 id="edit-counter-last-{counter_id}" name="counter_last_{counter_id}" value="" size="60" />
<input type="hidden" name="h_is_avg_povirka[{counter_id}]" value="0" /></td><td>2028-02-27</td></tr>"""


FORM_PAGE = f"""<html><form action="/my-cabinet/send_counters/send_current_new/77" method="post"
 id="send-counters-user-new-form" accept-charset="UTF-8">
<input id="h-allow-send-big-counters" type="hidden" name="h_allow_send_big_counters" value="0" />
<input type="hidden" name="h_actionId" value="9" />
<table><tr><th>№ ліч.</th><th>Місце</th></tr>
{kte_row("1000001", "Ванна", "11_1", "208")}
{kte_row("1000002", "Кухня", "12_2", "120.8")}
</table>
<input type="hidden" name="form_build_id" value="form-send" />
<input type="hidden" name="form_token" value="token" />
<input type="hidden" name="form_id" value="send_counters_user_new_form" />
<button type="submit" id="edit-next" name="op" value="Передати показання">Передати показання</button>
</form></html>"""


def history(*rows: tuple[str, str]) -> str:
    cells = "".join(
        f"<tr><td>{date}</td><td>&gt; 50</td><td>{value}</td><td>Поточні</td><td>ОК абонента</td><td></td><td></td></tr>"
        for date, value in rows
    )
    return f"<html><table><tr><th>Дата</th><th>t Режим</th><th>Поточні</th></tr>{cells}</table></html>"


DONE_PAGE = (
    '<html><div class="alert alert-block alert-success messages status">Дякуємо за внесення показників! '
    "Останнього тижня наступного місяця заходьте сюди знову!</div>" + FORM_PAGE + "</html>"
)


def mock_kvk(aioclient_mock: AiohttpClientMocker, *, counters=COUNTERS, **save) -> None:
    aioclient_mock.post(f"{kvk_api.BASE}counter/search", json=counters, headers=JSON)
    save.setdefault("json", {"id": 5})
    save.setdefault("headers", JSON)
    aioclient_mock.post(f"{kvk_api.BASE}doc/cfs", **save)


def mock_kte(aioclient_mock: AiohttpClientMocker, *, login=HOME_PAGE, done=DONE_PAGE, done_status=200) -> None:
    base = kte_api.BASE
    aioclient_mock.get(f"{base}userhome_new", status=HTTPStatus.FORBIDDEN, text=LOGIN_PAGE, headers=HTML)
    aioclient_mock.post(f"{base}userhome_new?destination=userhome_new", text=login, headers=HTML)
    aioclient_mock.get(f"{base}send_counters/send_current_new/77", text=FORM_PAGE, headers=HTML)
    aioclient_mock.get(
        f"{base}send_counters/history_counters/77/11",
        text=history(("2026-10-27 Не зараховано", "300"), ("2026-10-25 Зараховано", "222.01")),
        headers=HTML,
    )
    aioclient_mock.get(f"{base}send_counters/history_counters/77/12", text=history(), headers=HTML)
    aioclient_mock.post(f"{base}send_counters/send_current_new/77", status=done_status, text=done, headers=HTML)


def posts(aioclient_mock: AiohttpClientMocker, path: str) -> list:
    return [c for c in aioclient_mock.mock_calls if c[0].lower() == "post" and str(c[1]).endswith(path)]


# --- Київводоканал ---------------------------------------------------------------------------------------------


async def _kvk(hass: HomeAssistant, readings=READINGS, **kwargs):
    kwargs.setdefault("dry_run", False)
    kwargs.setdefault("max_increase", 50)
    return await kvk_api.kvk(kvk_api.KvkApi(async_get_clientsession(hass)), KVK_ACCOUNT, readings, **kwargs)


async def test_kvk_sends_the_changed_meters(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_kvk(aioclient_mock)
    results = await _kvk(hass)
    assert posts(aioclient_mock, "counter/search")[0][2] == {"serviceProviderCode": "999", "code": KVK_ACCOUNT}
    sent = posts(aioclient_mock, "doc/cfs")[0][2]
    assert sent["serviceProviderCode"] == "999"
    assert {c["counterNumber"]: c["endFactor"] for c in sent["counters"]} == {
        "1000004": 356.13,
        "1000003": 1008.1,
        "1000002": 123.36,
        "1000001": 222.01,
    }
    assert all(c["modified"] for c in sent["counters"])
    assert sent["counters"][0]["invoiceAccount"] == {"id": 1, "code": KVK_ACCOUNT}  # the rest as it came
    assert [r["meter"] for r in results] == [
        "1000004 - ХОЛОДНА ВОДА",
        "1000003 - ХОЛОДНА ВОДА",
        "1000002 - ВОДОВІДВЕДЕННЯ ГАРЯЧОЇ ВОДИ",
        "1000001 - ВОДОВІДВЕДЕННЯ ГАРЯЧОЇ ВОДИ",
    ]
    assert {r["status"] for r in results} == {"submitted"}
    assert results[0]["previous"] == 353.8
    assert results[0]["previous_date"] == "2026-10-25"


async def test_kvk_dry_run_and_checks(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_kvk(aioclient_mock)
    results = await _kvk(hass, {"1000004": 300.0, "1000003": 1008.1}, dry_run=True)
    assert posts(aioclient_mock, "doc/cfs") == []
    assert [r["status"] for r in results] == ["rejected", "dry_run", "no_reading", "no_reading"]

    results = await _kvk(hass, {"1000004": 300.0, "1000003": 1008.1})
    # The good one still goes; the low one doesn't
    assert [c["counterNumber"] for c in posts(aioclient_mock, "doc/cfs")[0][2]["counters"]] == ["1000003"]
    assert [r["status"] for r in results][:2] == ["rejected", "submitted"]


async def test_kvk_unknown_account(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_kvk(aioclient_mock, counters=[])
    with pytest.raises(KvkRejected, match="не знає"):
        await _kvk(hass)


@pytest.mark.parametrize(
    "save",
    [
        {"status": HTTPStatus.BAD_GATEWAY, "text": "<html>bad gateway</html>", "headers": HTML},
        {"status": HTTPStatus.FORBIDDEN, "text": "<html>WAF</html>", "headers": HTML},
        {"exc": aiohttp.ClientConnectionError("down")},
        {"exc": TimeoutError()},
    ],
    ids=["5xx", "403-html", "connection", "timeout"],
)
async def test_kvk_no_usable_answer(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, save):
    mock_kvk(aioclient_mock, **save)
    with pytest.raises(KvkUnavailable):
        await _kvk(hass)


async def test_kvk_refusal(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_kvk(aioclient_mock, status=HTTPStatus.BAD_REQUEST, json={"message": "Показник менший за попередній"})
    with pytest.raises(KvkRejected, match="менший"):
        await _kvk(hass)


# --- Київтеплоенерго --------------------------------------------------------------------------------------------


async def _kte(hass: HomeAssistant, readings=READINGS, **kwargs):
    kwargs.setdefault("dry_run", False)
    kwargs.setdefault("max_increase", 50)
    cabinet = kte_api.KteCabinet(async_get_clientsession(hass))
    return await kte_api.kte(cabinet, "test@example.com", "secret", readings, **kwargs)


async def test_kte_logs_in_and_posts_the_form(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_kte(aioclient_mock)
    results = await _kte(hass)

    login = posts(aioclient_mock, "userhome_new?destination=userhome_new")[0][2]
    assert login == {
        "name": "test@example.com",
        "pass": "secret",
        "form_build_id": "form-login",
        "form_id": "user_login_block",
        "op": "Увійти",
    }
    sent = posts(aioclient_mock, "send_current_new/77")[0][2]
    assert sent == {
        "h_allow_send_big_counters": "0",
        "h_actionId": "9",
        "counter_last_11_1": "222.01",
        "h_is_avg_povirka[11_1]": "0",
        "counter_last_12_2": "123.36",
        "h_is_avg_povirka[12_2]": "0",
        "form_build_id": "form-send",
        "form_token": "token",
        "form_id": "send_counters_user_new_form",
    }
    # A browser User-Agent, or the firewall answers 403
    assert "Mozilla" in aioclient_mock.mock_calls[0][3]["User-Agent"]
    assert results == [
        # previous: the newest accepted reading in the history, not the refused one above it
        {
            "meter": "1000001 - ГАРЯЧА ВОДА",
            "previous": 222.01,
            "previous_date": "2026-10-25",
            "value": 222.01,
            "status": "submitted",
        },
        # no history: the form's previous reading
        {"meter": "1000002 - ГАРЯЧА ВОДА", "previous": 120.8, "value": 123.36, "status": "submitted"},
    ]


async def test_kte_dry_run_posts_nothing(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_kte(aioclient_mock)
    results = await _kte(hass, {"1000001": 222.5}, dry_run=True)
    assert posts(aioclient_mock, "send_current_new/77") == []
    assert [r["status"] for r in results] == ["dry_run", "no_reading"]


async def test_kte_wrong_password(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_kte(aioclient_mock, login=LOGIN_ERROR)
    with pytest.raises(KteUnavailable, match="Невірна адреса"):
        await _kte(hass)


async def test_kte_form_error_is_a_refusal(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    error = '<html><div class="messages error">Поточні показання менші за попередні</div></html>'
    mock_kte(aioclient_mock, done=error)
    with pytest.raises(KteRejected, match="менші"):
        await _kte(hass)


async def test_kte_firewall(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(
        f"{kte_api.BASE}userhome_new",
        status=HTTPStatus.FORBIDDEN,
        text="<html><title>Web Application Firewall</title>This transfer is blocked.</html>",
        headers=HTML,
    )
    with pytest.raises(KteUnavailable, match="firewall"):
        await _kte(hass)


async def test_kte_no_confirmation(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    mock_kte(aioclient_mock, done=FORM_PAGE)
    with pytest.raises(KteUnavailable, match="не підтвердив"):
        await _kte(hass)


# --- the service ------------------------------------------------------------------------------------------------


async def _submit(hass: HomeAssistant, **data):
    data = {"bot": "komunalka", "readings": READINGS, **data}
    return await hass.services.async_call(DOMAIN, "submit", data, blocking=True, return_response=True)


def water_entry(**options):
    return make_entry(**{CONF_KVK_ACCOUNT: KVK_ACCOUNT, **KTE_LOGIN, **options})


async def test_both_apis_and_no_bot(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_kvk(aioclient_mock)
    mock_kte(aioclient_mock)
    await setup_entry(hass, water_entry())
    response = await _submit(hass)
    assert response["result"] == "ok"
    assert response["via"] == "api"
    assert bots.opened == []
    assert len(response["results"]) == 6
    assert {r["status"] for r in response["results"]} == {"submitted"}
    await hass.async_block_till_done()
    # The supplier sensors read the same as after the bot: the hot one shows the supply item
    hot = hass.states.get("sensor.telegram_test_komunalka_1000001")
    assert hot.state == "222.01"
    assert hot.attributes["items"] == {"ВОДОВІДВЕДЕННЯ ГАРЯЧОЇ ВОДИ": 216.7, "ГАРЯЧА ВОДА": 222.01}


async def test_bot_takes_only_what_failed(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_kvk(aioclient_mock)
    aioclient_mock.get(f"{kte_api.BASE}userhome_new", exc=aiohttp.ClientConnectionError("down"))
    await setup_entry(hass, water_entry())
    response = await _submit(hass)
    assert response["result"] == "ok"
    assert response["via"] == "api+telegram"
    assert response["api_error"].startswith("Київтеплоенерго: ClientConnectionError")
    # Only hot water itself went through the bot; cold water and drainage went to Київводоканал
    assert bots.bots["komunalkaua_bot"].submitted == {
        "1000001 - ГАРЯЧА ВОДА": "222.01",
        "1000002 - ГАРЯЧА ВОДА": "123.36",
    }
    assert hass.states.get(ATTEMPT).attributes["via"] == "api+telegram"


async def test_without_kte_login_hot_water_goes_to_the_bot(
    hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker
):
    mock_kvk(aioclient_mock)
    await setup_entry(hass, make_entry(**{CONF_KVK_ACCOUNT: KVK_ACCOUNT}))
    response = await _submit(hass, dry_run=True)
    assert response["result"] == "dry_run"
    assert response["via"] == "api+telegram"
    bot_items = [r["meter"] for r in response["results"] if "ГАРЯЧА ВОДА" == r["meter"].split(" - ")[1]]
    assert sorted(bot_items) == ["1000001 - ГАРЯЧА ВОДА", "1000002 - ГАРЯЧА ВОДА"]


async def test_refusal_does_not_go_to_the_bot(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_kvk(aioclient_mock, counters=[])
    mock_kte(aioclient_mock)
    await setup_entry(hass, water_entry())
    response = await _submit(hass)
    assert response["result"] == "failed"
    assert response["error"].startswith("Київводоканал: ")
    assert response["via"] == "api"
    assert bots.opened == []


async def test_unknown_serial_is_reported_once(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_kvk(aioclient_mock)
    mock_kte(aioclient_mock)
    await setup_entry(hass, water_entry())
    response = await _submit(hass, readings={**READINGS, "9999999": 1.0}, dry_run=True)
    assert response["result"] == "failed"
    assert [r for r in response["results"] if r["status"] == "not_found"] == [
        {"meter": "9999999", "value": 1.0, "status": "not_found"}
    ]


async def test_via_telegram_skips_the_apis(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    await setup_entry(hass, water_entry())
    response = await _submit(hass, via="telegram", dry_run=True)
    assert response["via"] == "telegram"
    assert aioclient_mock.call_count == 0
    assert len(response["results"]) == 6


async def test_via_api_needs_a_water_api(hass: HomeAssistant, bots):
    await setup_entry(hass, make_entry())
    with pytest.raises(ServiceValidationError):
        await _submit(hass, via="api")


# --- options ----------------------------------------------------------------------------------------------------


async def _options(hass: HomeAssistant, entry, user_input):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(result["flow_id"], user_input)


async def test_options_check_both(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_kvk(aioclient_mock)
    mock_kte(aioclient_mock)
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await _options(hass, entry, {CONF_KVK_ACCOUNT: KVK_ACCOUNT, **KTE_LOGIN})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_KVK_ACCOUNT] == KVK_ACCOUNT
    assert entry.options[CONF_KTE_PASSWORD] == "secret"
    # Saved again unchanged: nothing checked again
    calls = aioclient_mock.call_count
    await hass.async_block_till_done()
    result = await _options(hass, entry, dict(entry.options))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert aioclient_mock.call_count == calls


async def test_options_errors(hass: HomeAssistant, bots, aioclient_mock: AiohttpClientMocker):
    mock_kvk(aioclient_mock, counters=[])
    mock_kte(aioclient_mock, login=LOGIN_ERROR)
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await _options(hass, entry, {CONF_KVK_ACCOUNT: KVK_ACCOUNT})
    assert result["errors"] == {CONF_KVK_ACCOUNT: "kvk_account_not_found"}
    result = await _options(hass, entry, {CONF_KTE_EMAIL: "test@example.com"})
    assert result["errors"] == {"base": "kte_login_incomplete"}
    result = await _options(hass, entry, KTE_LOGIN)
    assert result["errors"] == {"base": "kte_login_failed"}
    assert "Невірна адреса" in result["description_placeholders"]["detail"]
