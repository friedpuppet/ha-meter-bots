"""Meter Bots: send meter readings to suppliers' Telegram bots from the user's own account."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import scenarios
from .client import AuthError, TelegramSession
from .const import (
    ATTR_BOT,
    ATTR_DRY_RUN,
    ATTR_MAX_INCREASE,
    ATTR_READINGS,
    BOT_USERNAMES,
    BOT_YASNO,
    BOTS,
    CONF_API_HASH,
    CONF_API_ID,
    CONF_KOMUNALKA_ADDRESS,
    CONF_SESSION,
    CONF_YASNO_ACCOUNT,
    DEFAULT_MAX_INCREASE,
    DOMAIN,
    RESULT_DRY_RUN,
    RESULT_FAILED,
    RESULT_OK,
    SERVICE_SUBMIT,
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


async def async_submit(
    hass: HomeAssistant,
    entry: MeterBotsConfigEntry,
    bot: str,
    readings: dict[str, float],
    *,
    dry_run: bool = False,
    max_increase: float | None = None,
) -> dict[str, Any]:
    """Run one bot's scenario; record and return what happened."""
    if max_increase is None:
        max_increase = DEFAULT_MAX_INCREASE[bot]
    if bot == BOT_YASNO:
        if set(readings) != {"day", "night"}:
            raise ServiceValidationError("Для YASNO потрібні покази day і night")
        account = entry.options.get(CONF_YASNO_ACCOUNT)
        if not account:
            raise ServiceValidationError("Вкажіть особовий рахунок YASNO в налаштуваннях інтеграції")

    runtime = entry.runtime_data
    error: str | None = None
    results: list[dict[str, Any]] = []
    dialog: Dialog | None = None
    async with runtime.lock:
        try:
            async with TelegramSession(
                entry.data[CONF_SESSION], entry.data[CONF_API_ID], entry.data[CONF_API_HASH]
            ) as tg:
                dialog = Dialog(await tg.chat(BOT_USERNAMES[bot]))
                if bot == BOT_YASNO:
                    results = await scenarios.yasno(
                        dialog,
                        readings["day"],
                        readings["night"],
                        account=str(account),
                        dry_run=dry_run,
                        max_increase=max_increase,
                    )
                else:
                    results = await scenarios.komunalka(
                        dialog,
                        readings,
                        address=entry.options.get(CONF_KOMUNALKA_ADDRESS) or None,
                        dry_run=dry_run,
                        max_increase=max_increase,
                    )
        except AuthError as err:
            entry.async_start_reauth(hass)
            raise HomeAssistantError(f"Telegram-сесія більше не діє, увійдіть знову: {err}") from err
        except DialogError as err:
            error = str(err)
        except Exception as err:  # Telegram/network errors: report them like a bot problem
            _LOGGER.exception("Submitting to %s failed", bot)
            error = f"{type(err).__name__}: {err}"

    failed = error is not None or any(r.get("status") in FAILED_STATUSES for r in results)
    outcome = RESULT_FAILED if failed else RESULT_DRY_RUN if dry_run else RESULT_OK
    record = {
        "at": dt_util.utcnow().isoformat(),
        "result": outcome,
        "error": error,
        "results": results,
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
    return {**record, "transcript": dialog.transcript if dialog else []}


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
