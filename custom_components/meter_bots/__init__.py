"""Meter Bots: send meter readings to suppliers' Telegram bots from the user's own account."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import kte_api, kvk_api, scenarios, yasno_api
from .client import AuthError, TelegramSession
from .const import (
    ATTR_BOT,
    ATTR_DRY_RUN,
    ATTR_FALLBACK,
    ATTR_MAX_INCREASE,
    ATTR_READINGS,
    ATTR_VIA,
    BOT_USERNAMES,
    BOT_YASNO,
    BOTS,
    CONF_API_HASH,
    CONF_API_ID,
    CONF_KOMUNALKA_ADDRESS,
    CONF_KTE_EMAIL,
    CONF_KTE_PASSWORD,
    CONF_KVK_ACCOUNT,
    CONF_SESSION,
    CONF_YASNO_ACCOUNT,
    CONF_YASNO_INTERNAL_ID,
    DEFAULT_MAX_INCREASE,
    DOMAIN,
    RESULT_DRY_RUN,
    RESULT_FAILED,
    RESULT_OK,
    RESULT_PENDING,
    SERVICE_SUBMIT,
    SUPPLIER_KTE,
    SUPPLIER_KVK,
    VIA_API,
    VIA_AUTO,
    VIA_TELEGRAM,
    VIAS,
)
from .dialog import Dialog, DialogError
from .storage import Persisted

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SUBMIT_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Required(ATTR_BOT): vol.In(BOTS),
        vol.Required(ATTR_READINGS): {cv.string: vol.Coerce(float)},
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
        vol.Optional(ATTR_MAX_INCREASE): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional(ATTR_VIA, default=VIA_AUTO): vol.In(VIAS),
        vol.Optional(ATTR_FALLBACK, default=True): cv.boolean,
    }
)

FAILED_STATUSES = (scenarios.STATUS_REJECTED, scenarios.STATUS_NOT_FOUND)


@dataclass
class RuntimeData:
    persisted: Persisted
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    listeners: list[Callable[[], None]] = field(default_factory=list)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    @callback
    def notify(self) -> None:
        for listener in list(self.listeners):
            listener()


type MeterBotsConfigEntry = ConfigEntry[RuntimeData]


def _entry_for(hass: HomeAssistant, entry_id: str | None) -> MeterBotsConfigEntry:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    if len(entries) != 1:
        raise ServiceValidationError("Вкажіть config_entry_id: налаштовано не один Telegram-акаунт або жодного")
    return entries[0]


# A site that gave no usable answer is asked again after these pauses (s) before the bot takes over
RETRY_DELAYS = (10, 30)

SUPPLIER_NAMES = {BOT_YASNO: "YASNO", SUPPLIER_KVK: "Київводоканал", SUPPLIER_KTE: "Київтеплоенерго"}
API_REJECTED = (yasno_api.YasnoRejected, kvk_api.KvkRejected, kte_api.KteRejected)
API_UNAVAILABLE = (yasno_api.YasnoUnavailable, kvk_api.KvkUnavailable, kte_api.KteUnavailable)


async def _yasno_api(hass: HomeAssistant, options: Mapping[str, Any], readings: dict[str, float], **kwargs: Any):
    api = yasno_api.YasnoApi(async_get_clientsession(hass))
    return await yasno_api.yasno(api, options[CONF_YASNO_INTERNAL_ID], readings["day"], readings["night"], **kwargs)


async def _kvk_api(hass: HomeAssistant, options: Mapping[str, Any], readings: dict[str, float], **kwargs: Any):
    api = kvk_api.KvkApi(async_get_clientsession(hass))
    return await kvk_api.kvk(api, str(options[CONF_KVK_ACCOUNT]), readings, **kwargs)


async def _kte_api(hass: HomeAssistant, options: Mapping[str, Any], readings: dict[str, float], **kwargs: Any):
    # The cabinet keeps the login in a cookie: a session of its own, closed after the run
    session = async_create_clientsession(hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())
    try:
        cabinet = kte_api.KteCabinet(session)
        return await kte_api.kte(cabinet, options[CONF_KTE_EMAIL], options[CONF_KTE_PASSWORD], readings, **kwargs)
    finally:
        await session.close()


async def _with_retries(name: str, run: Callable[..., Any], *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """``run`` again after each of ``RETRY_DELAYS`` while it gives no usable answer; a refusal isn't retried.

    Safe to repeat: the APIs skip what the supplier already has (``STATUS_ALREADY``), so a run whose answer was
    lost after the supplier took the readings doesn't send them twice.
    """
    for delay in RETRY_DELAYS:
        try:
            return await run(*args, **kwargs)
        except API_UNAVAILABLE as err:
            _LOGGER.info("%s gave no usable answer (%s); trying again in %s s", name, err, delay)
            await asyncio.sleep(delay)
    try:
        return await run(*args, **kwargs)
    except API_UNAVAILABLE as err:
        if not RETRY_DELAYS:
            raise
        raise type(err)(f"{err} (спроб: {len(RETRY_DELAYS) + 1})") from err


def _unknown_serials(results: list[dict[str, Any]], readings: dict[str, float]) -> list[dict[str, Any]]:
    """One «not_found» per serial no supplier listed; the APIs leave them out, the bot reports its own."""

    def serial(result: dict[str, Any]) -> str:
        match = scenarios.KOMUNALKA_METER.match(result["meter"])
        return match.group(1) if match else result["meter"]

    found = {serial(r) for r in results if r.get("status") != scenarios.STATUS_NOT_FOUND}
    kept = [r for r in results if r.get("status") != scenarios.STATUS_NOT_FOUND]
    missing = [s for s in readings if s not in found]
    return kept + [{"meter": s, "value": readings[s], "status": scenarios.STATUS_NOT_FOUND} for s in missing]


async def async_submit(
    hass: HomeAssistant,
    entry: MeterBotsConfigEntry,
    bot: str,
    readings: dict[str, float],
    *,
    dry_run: bool = False,
    max_increase: float | None = None,
    via: str = VIA_AUTO,
    fallback: bool = True,
) -> dict[str, Any]:
    """Run one supplier's submission; record and return what happened.

    YASNO goes through its web API when the address is set up. Water (``komunalka``) goes to Київводоканал's
    public form and Київтеплоенерго's cabinet, each when set up. The Telegram bot takes whatever has no
    API set up, or whose API gave no usable answer, or everything when ``via`` asks for the bot. A refusal by an
    API is final: the bot would refuse too. With ``fallback`` off, a site that gave no usable answer isn't handed
    to the bot: its items wait for a later run (result ``pending``, ``retryable``), which the automations make.
    """
    if max_increase is None:
        max_increase = DEFAULT_MAX_INCREASE[bot]
    options = entry.options
    if bot == BOT_YASNO:
        if set(readings) != {"day", "night"}:
            raise ServiceValidationError("Для YASNO потрібні покази day і night")
        if via == VIA_API and not options.get(CONF_YASNO_INTERNAL_ID):
            raise ServiceValidationError("Вкажіть адресу YASNO в налаштуваннях інтеграції")
        if (via == VIA_TELEGRAM or not options.get(CONF_YASNO_INTERNAL_ID)) and not options.get(CONF_YASNO_ACCOUNT):
            raise ServiceValidationError("Вкажіть особовий рахунок YASNO в налаштуваннях інтеграції")
        apis = {BOT_YASNO: _yasno_api} if options.get(CONF_YASNO_INTERNAL_ID) else {}
        everything = {BOT_YASNO}
    else:
        apis = {}
        if options.get(CONF_KVK_ACCOUNT):
            apis[SUPPLIER_KVK] = _kvk_api
        if options.get(CONF_KTE_EMAIL) and options.get(CONF_KTE_PASSWORD):
            apis[SUPPLIER_KTE] = _kte_api
        if via == VIA_API and not apis:
            raise ServiceValidationError(
                "Вкажіть особовий рахунок Київводоканалу або вхід у кабінет Київтеплоенерго в налаштуваннях"
            )
        everything = {SUPPLIER_KVK, SUPPLIER_KTE}
    if via == VIA_TELEGRAM:
        apis = {}

    runtime = entry.runtime_data
    errors: list[str] = []
    api_errors: list[str] = []
    pending: list[str] = []  # sites that didn't answer and wait for a later run
    results: list[dict[str, Any]] = []
    dialog: Dialog | None = None
    for_bot = everything - set(apis)
    answered = False  # some API answered (readings or a refusal)
    refused = False  # a supplier said no: running again won't help
    auth_error: AuthError | None = None
    async with runtime.lock:
        for supplier, run in apis.items():
            name = SUPPLIER_NAMES[supplier]
            try:
                got = await _with_retries(
                    name,
                    run,
                    hass,
                    options,
                    readings,
                    dry_run=dry_run,
                    max_increase=max_increase,
                    this_month=dt_util.now().strftime("%Y-%m"),
                )
                results += [{**r, "via": VIA_API, "supplier": supplier} for r in got]
                answered = True
            except API_REJECTED as err:
                errors.append(f"{name}: {err}")
                answered = True
                refused = True
            except Exception as err:  # API_UNAVAILABLE, or a page we failed to read: the bot may still work
                if isinstance(err, API_UNAVAILABLE):
                    reason = str(err)
                    _LOGGER.warning("%s API gave no usable answer: %s", name, reason)
                else:
                    reason = f"{type(err).__name__}: {err}"
                    _LOGGER.exception("%s API failed", name)
                if via == VIA_API:
                    errors.append(f"API {name}: {reason}")
                elif bot == BOT_YASNO and not options.get(CONF_YASNO_ACCOUNT):
                    errors.append(f"API {name}: {reason}; для бота не вказано особовий рахунок")
                elif not fallback:
                    pending.append(f"{name}: {reason}")
                else:
                    api_errors.append(f"{name}: {reason}")
                    for_bot.add(supplier)
        if for_bot and via != VIA_API:
            got = []
            try:
                async with TelegramSession(
                    entry.data[CONF_SESSION], entry.data[CONF_API_ID], entry.data[CONF_API_HASH]
                ) as tg:
                    dialog = Dialog(await tg.chat(BOT_USERNAMES[bot]))
                    if bot == BOT_YASNO:
                        got = await scenarios.yasno(
                            dialog,
                            readings["day"],
                            readings["night"],
                            account=str(options[CONF_YASNO_ACCOUNT]),
                            dry_run=dry_run,
                            max_increase=max_increase,
                        )
                    else:
                        got = await scenarios.komunalka(
                            dialog,
                            readings,
                            address=options.get(CONF_KOMUNALKA_ADDRESS) or None,
                            dry_run=dry_run,
                            max_increase=max_increase,
                            suppliers=None if for_bot == everything else for_bot,
                        )
            except AuthError as err:
                entry.async_start_reauth(hass)
                auth_error = err
                errors.append(f"Telegram-сесія більше не діє, увійдіть знову: {err}")
            except DialogError as err:
                errors.append(str(err))
            except Exception as err:  # Telegram/network errors: report them like a bot problem
                _LOGGER.exception("Submitting to %s failed", bot)
                errors.append(f"{type(err).__name__}: {err}")
            results += [{**r, "via": VIA_TELEGRAM, "supplier": _bot_supplier(bot, r)} for r in got]
    if bot != BOT_YASNO and not errors and not pending:  # «not found» only when every list of meters came in
        results = _unknown_serials(results, readings)

    used = VIA_API if not dialog else f"{VIA_API}+{VIA_TELEGRAM}" if answered else VIA_TELEGRAM
    error = "; ".join(errors) or None
    api_error = "; ".join(api_errors) or None

    refused = refused or any(r.get("status") in FAILED_STATUSES for r in results)
    failed = error is not None or refused
    outcome = RESULT_FAILED if failed else RESULT_PENDING if pending else RESULT_DRY_RUN if dry_run else RESULT_OK
    record = {
        "at": dt_util.utcnow().isoformat(),
        "result": outcome,
        "error": error,
        "results": results,
        "via": used,
        "api_error": api_error,
        "pending": "; ".join(pending) or None,
        # A technical failure (network, a site or bot down) or a site left for later: worth running again.
        # Not a refused value.
        "retryable": (failed and not refused) or bool(pending),
    }
    _remember_supplier_readings(runtime.persisted.data, bot, results, record["at"])
    last = runtime.persisted.data.setdefault(bot, {})
    last["attempt"] = record
    if outcome == RESULT_OK:
        last["success"] = record
    runtime.persisted.async_schedule_save()
    runtime.notify()
    if failed:
        _LOGGER.warning("Submitting to %s: %s %s", bot, error or "", results)
    if auth_error:
        raise HomeAssistantError(errors[-1]) from auth_error
    return {**record, "transcript": dialog.transcript if dialog else []}


def _bot_supplier(bot: str, result: dict[str, Any]) -> str | None:
    if bot == BOT_YASNO:
        return BOT_YASNO
    if scenarios.KOMUNALKA_METER.match(result["meter"]):
        return scenarios.komunalka_supplier(result["meter"])
    return None


def _remember_supplier_readings(data: dict[str, Any], bot: str, results: list[dict[str, Any]], at: str) -> None:
    """Keep the readings the bot showed as its own (``previous``): YASNO by zone, Комуналка by serial and service."""
    store = data.setdefault("supplier", {}).setdefault(bot, {})
    for result in results:
        if result.get("previous") is None:
            continue
        if bot == BOT_YASNO:
            key, item = result["meter"], result["meter"]
        elif match := scenarios.KOMUNALKA_METER.match(result["meter"]):
            key, item = match.group(1), match.group(2)
        else:
            continue
        reading = store.setdefault(key, {"items": {}})
        reading["items"][item] = result["previous"]
        reading["date"] = result.get("previous_date")
        reading["at"] = at


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def submit(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for(hass, call.data.get("config_entry_id"))
        return await async_submit(
            hass,
            entry,
            call.data[ATTR_BOT],
            call.data[ATTR_READINGS],
            dry_run=call.data[ATTR_DRY_RUN],
            max_increase=call.data.get(ATTR_MAX_INCREASE),
            via=call.data[ATTR_VIA],
            fallback=call.data[ATTR_FALLBACK],
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SUBMIT, submit, schema=SUBMIT_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MeterBotsConfigEntry) -> bool:
    persisted = Persisted(hass, entry.entry_id)
    await persisted.async_load()
    entry.runtime_data = RuntimeData(persisted)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MeterBotsConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.persisted.async_flush()
    return unloaded


async def _async_entry_updated(hass: HomeAssistant, entry: MeterBotsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
