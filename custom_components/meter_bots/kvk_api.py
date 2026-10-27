"""Київводоканал's public «надати показники» form (kvkmeters.ssmt.com.ua): read and send water readings.

Checked 2026-10-27: no login, no cookies. The page shows a reCAPTCHA, but only to enable its search button; the
token is never sent. The account is the ЦКС особовий рахунок from the bill. Київводоканал bills cold water
and the drainage of hot water, so both kinds of meter are listed; hot water itself is Київтеплоенерго's
(``kte_api``). A submission is two requests: the meters with their last accepted readings, then the changed ones.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from . import scenarios

BASE = "https://kvkmeters.ssmt.com.ua/warehouse/api/warehouse/external/extension/"
PROVIDER = "999"  # Київводоканал in this system
HEADERS = {"accept": "application/json", "content-type": "application/json"}
TIMEOUT = aiohttp.ClientTimeout(total=30)
# Item names as @komunalkaua_bot shows them, so the supplier sensors don't care which way a reading came
SERVICES = {"Холодна вода": "ХОЛОДНА ВОДА", "Гаряча вода": "ВОДОВІДВЕДЕННЯ ГАРЯЧОЇ ВОДИ"}


class KvkError(Exception):
    """Base of the API's errors."""


class KvkRejected(KvkError):
    """Київводоканал understood the request and said no. The bot would say the same."""


class KvkUnavailable(KvkError):
    """No usable answer: network, timeout, 5xx, a firewall page, unexpected JSON. The bot may still work."""


class KvkApi:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            async with self._session.post(BASE + path, json=payload, headers=HEADERS, timeout=TIMEOUT) as resp:
                is_json = "json" in (resp.content_type or "")
                body = await resp.json(content_type=None) if is_json else await resp.text()
                if resp.status in (400, 404, 409, 422) and is_json:
                    raise KvkRejected(_message(body))
                if resp.status != 200 or not is_json:
                    raise KvkUnavailable(f"HTTP {resp.status} {resp.content_type or ''}".strip())
                return body
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise KvkUnavailable(f"{type(err).__name__}: {err}") from err

    async def counters(self, account: str) -> list[dict[str, Any]]:
        """The account's meters, each with ``counterNumber``, ``endFactor`` (last accepted reading) and more."""
        body = await self._post("counter/search", {"serviceProviderCode": PROVIDER, "code": account})
        if not isinstance(body, list):
            raise KvkUnavailable(f"unexpected answer for the account: {str(body)[:200]}")
        if not body:
            raise KvkRejected(f"Київводоканал не знає особовий рахунок {account}")
        return body

    async def send(self, counters: list[dict[str, Any]]) -> Any:
        """Save the meters as the form does: the objects from ``counters`` with ``endFactor`` changed."""
        return await self._post(
            "doc/cfs",
            {
                "counters": counters,
                "owningWaterHeater": False,
                "serviceProviderCode": PROVIDER,
                "feedbackContactInfo": {},
            },
        )


def _message(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "detail", "error", "title"):
            if body.get(key):
                return str(body[key])
    return str(body)[:200]


def item(counter: dict[str, Any]) -> str:
    """«1000004 - ХОЛОДНА ВОДА»: the meter as the bot names it."""
    kind = (counter.get("counterType") or {}).get("name") or ""
    return f"{counter.get('counterNumber')} - {SERVICES.get(kind, kind.upper())}"


async def kvk(
    api: KvkApi,
    account: str,
    readings: dict[str, float],
    *,
    dry_run: bool,
    max_increase: float,
) -> list[dict[str, Any]]:
    """Same checks and result format as ``scenarios.komunalka``, for the meters Київводоканал lists.

    Serials it doesn't list are left out, not reported: the other supplier may have them.
    """
    counters = await api.counters(account)
    results: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for counter in counters:
        serial = str(counter.get("counterNumber"))
        if serial not in readings:
            results.append({"meter": item(counter), "status": scenarios.STATUS_NO_READING})
            continue
        value = float(scenarios.format_m3(readings[serial]))
        previous = counter.get("endFactor")
        result: dict[str, Any] = {"meter": item(counter), "previous": previous, "value": value}
        if counter.get("dateFrom"):
            result["previous_date"] = str(counter["dateFrom"])[:10]
        results.append(result)
        if problem := scenarios.check_value(value, previous, max_increase):
            result.update(status=scenarios.STATUS_REJECTED, reason=problem)
        elif dry_run:
            result["status"] = scenarios.STATUS_DRY_RUN
        else:
            changed.append({**counter, "endFactor": value, "modified": True})
            result["status"] = scenarios.STATUS_SUBMITTED

    if changed:
        await api.send(changed)
    return results
