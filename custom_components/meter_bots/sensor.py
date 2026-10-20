"""Per bot: when readings last went through, and how the last attempt ended."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import MeterBotsConfigEntry
from .const import BOT_YASNO, BOTS, RESULTS
from .entity import MeterBotsEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeterBotsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[SensorEntity] = []
    for bot in BOTS:
        entities += [LastSubmissionSensor(entry, bot), LastAttemptSensor(entry, bot)]
    async_add_entities(entities)

    # One sensor per meter the bots have shown a reading for; new ones appear after a run
    runtime = entry.runtime_data
    added: set[tuple[str, str]] = set()

    @callback
    def add_supplier_sensors() -> None:
        new = [
            SupplierReadingSensor(entry, bot, key)
            for bot, readings in runtime.persisted.data.get("supplier", {}).items()
            for key in readings
            if (bot, key) not in added
        ]
        added.update((e.bot, e.key) for e in new)
        if new:
            async_add_entities(new)

    add_supplier_sensors()
    entry.async_on_unload(runtime.async_add_listener(add_supplier_sensors))


class _BotSensor(MeterBotsEntity, SensorEntity):
    _record_key: str

    def __init__(self, entry: MeterBotsConfigEntry, bot: str, key: str) -> None:
        super().__init__(entry, f"{bot}_{key}")
        self._runtime = entry.runtime_data
        self._bot = bot

    @property
    def _record(self) -> dict[str, Any]:
        return self._runtime.persisted.data.get(self._bot, {}).get(self._record_key) or {}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = self._record
        return {"at": record.get("at"), "error": record.get("error"), "results": record.get("results")}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._runtime.async_add_listener(self._on_change))

    @callback
    def _on_change(self) -> None:
        self.async_write_ha_state()


class LastSubmissionSensor(_BotSensor):
    """Time of the last real submission that went through."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:send-check-outline"
    _record_key = "success"

    def __init__(self, entry: MeterBotsConfigEntry, bot: str) -> None:
        super().__init__(entry, bot, "last_submission")

    @property
    def native_value(self) -> datetime | None:
        at = self._record.get("at")
        return dt_util.parse_datetime(at) if at else None


class LastAttemptSensor(_BotSensor):
    """ok / failed / dry_run of the last run, with the bot's answers as attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = RESULTS
    _attr_icon = "mdi:message-processing-outline"
    _record_key = "attempt"

    def __init__(self, entry: MeterBotsConfigEntry, bot: str) -> None:
        super().__init__(entry, bot, "last_attempt")

    @property
    def native_value(self) -> str | None:
        return self._record.get("result")


class SupplierReadingSensor(MeterBotsEntity, SensorEntity):
    """The reading the supplier's bot shows as its own (the last one it accepted), not the one we sent."""

    _attr_icon = "mdi:office-building-outline"

    def __init__(self, entry: MeterBotsConfigEntry, bot: str, key: str) -> None:
        super().__init__(entry, f"{bot}_supplier_{key}")
        self._attr_translation_key = None
        self._runtime = entry.runtime_data
        self.bot = bot
        self.key = key
        if bot == BOT_YASNO:
            self._attr_name = f"YASNO {key}"
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        else:
            self._attr_name = f"Комуналка {key}"
            self._attr_device_class = SensorDeviceClass.WATER
            self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS

    @property
    def _reading(self) -> dict[str, Any]:
        return self._runtime.persisted.data.get("supplier", {}).get(self.bot, {}).get(self.key) or {}

    @property
    def native_value(self) -> float | None:
        items: dict[str, float] = self._reading.get("items") or {}
        # Hot water is billed twice (supply and drainage); the supply one is the meter's reading
        for service, value in items.items():
            if not service.upper().startswith("ВОДОВІДВЕДЕННЯ"):
                return value
        return next(iter(items.values()), None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reading = self._reading
        return {"date": reading.get("date"), "checked_at": reading.get("at"), "items": reading.get("items")}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._runtime.async_add_listener(self.async_write_ha_state))
