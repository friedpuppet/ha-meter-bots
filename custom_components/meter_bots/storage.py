"""Per-entry persisted state: the last attempt per bot."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

SAVE_DELAY = 1


class Persisted:
    """A plain dict backed by a ``Store``."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry_id}")
        self.data: dict[str, Any] = {}

    async def async_load(self) -> None:
        self.data = await self._store.async_load() or {}

    def async_schedule_save(self) -> None:
        self._store.async_delay_save(lambda: self.data, SAVE_DELAY)

    async def async_flush(self) -> None:
        await self._store.async_save(self.data)
