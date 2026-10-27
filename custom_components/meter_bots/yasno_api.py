"""YASNO's web API (the one yasno.ua uses without logging in): read and send readings.

Checked 2026-10-27: no login, no cookies, no captcha. YASNO knows the account by its internal id (a short number,
not the особовий рахунок). The id is looked up by address once, when the options are saved; a submission is then
two requests: the last readings and the POST of whole kWh per zone. One reading a day: a second one is refused
with 400 «На зазначену дату вже є показання».
"""

from __future__ import annotations

import math
import re
from typing import Any

import aiohttp

from . import scenarios

BASE = "https://app.yasno.ua/api/account-service"
HEADERS = {"accept": "application/json", "origin": "https://yasno.ua", "x-platform": "Web"}
TIMEOUT = aiohttp.ClientTimeout(total=30)
ZONES = {"day": "Day", "night": "Night"}


class YasnoError(Exception):
    """Base of the API's errors."""


class YasnoRejected(YasnoError):
    """YASNO understood the request and said no (validation, no such address). The bot would say the same."""


class YasnoUnavailable(YasnoError):
    """No usable answer: network, timeout, 5xx, a bot-protection page, unexpected JSON. The bot may still work."""


class YasnoApi:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self._session.request(method, BASE + path, headers=HEADERS, timeout=TIMEOUT, **kwargs) as resp:
                is_json = "json" in (resp.content_type or "")
                body = await resp.json(content_type=None) if is_json else None
                if resp.status in (400, 404) and isinstance(body, dict) and body.get("detail"):
                    raise YasnoRejected(body["detail"])
                if resp.status != 200 or not is_json:
                    raise YasnoUnavailable(f"HTTP {resp.status} {resp.content_type or ''}".strip())
                return body
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise YasnoUnavailable(f"{type(err).__name__}: {err}") from err

    async def _search(self, path: str, query: str) -> list[Any]:
        # Without limit/offset the search answers with an empty list
        body = await self._request("GET", path, params={"query": query, "limit": 100, "offset": 0})
        try:
            return list(body["items"])
        except (KeyError, TypeError) as err:
            raise YasnoUnavailable(f"unexpected answer to {path}: {body}") from err

    async def find_account(self, city: str, street: str, house: str, apartment: str) -> dict[str, Any]:
        """``{"accountId": <internal id>, "accountNumberMasked": "10******001", …}`` for the address."""
        city_item = pick(await self._search("/public/addresses/cities", city), city, "населений пункт")
        street_item = pick(
            await self._search(f"/public/addresses/cities/{city_item['id']}/streets", street), street, "вулицю"
        )
        body = await self._request(
            "GET",
            "/public/users/me/accounts/by-address",
            params={
                "cityId": city_item["id"],
                "streetId": street_item["id"],
                "houseNumber": house,
                "apartmentNumber": apartment,
            },
        )
        if not isinstance(body, dict) or "accountId" not in body:
            raise YasnoUnavailable(f"unexpected answer for the address: {body}")
        return body

    async def debt(self, internal_id: int) -> dict[str, Any]:
        """Balance and the last accepted readings (``lastMeterReadings``)."""
        body = await self._request("GET", f"/public/users/me/accounts/b2c/{internal_id}/debt")
        if not isinstance(body, dict):
            raise YasnoUnavailable(f"unexpected answer for the account: {body}")
        return body

    async def send(self, internal_id: int, day: int, night: int) -> dict[str, Any]:
        body = await self._request(
            "POST",
            f"/public/users/me/accounts/b2c/{internal_id}/meter-readings",
            json={"meteringReadings": [{"value": day, "zone": "Day"}, {"value": night, "zone": "Night"}]},
        )
        if not isinstance(body, dict):
            raise YasnoUnavailable(f"unexpected answer to the reading: {body}")
        return body


def _name(value: str) -> str:
    """«м. Київ  Київська обл.» → «київ», «вул. Тестова» → «тестова»."""
    return re.sub(r"^\S+\.\s*", "", value.split("  ")[0]).strip().casefold()


def pick(items: list[dict[str, Any]], query: str, what: str) -> dict[str, Any]:
    """The one search result meant by ``query``: an exact name, else the only one containing it."""
    wanted = query.strip().casefold()
    for candidates in (
        [i for i in items if _name(i["value"]) == wanted],
        [i for i in items if wanted in _name(i["value"])],
    ):
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            raise YasnoRejected(f"«{query}» — кілька варіантів: {', '.join(i['value'] for i in candidates)}")
    raise YasnoRejected(f"YASNO не знає {what} «{query}»")


def mask_matches(masked: str, account: str) -> bool:
    """«10******001» against the особовий рахунок «10000000001»: same length, the shown digits equal."""
    account = account.strip()
    return len(masked) == len(account) and all(m in ("*", a) for m, a in zip(masked, account, strict=True))


async def yasno(
    api: YasnoApi,
    internal_id: int,
    day: float,
    night: float,
    *,
    dry_run: bool,
    max_increase: float,
    this_month: str | None = None,
) -> list[dict[str, Any]]:
    """Same checks and result format as ``scenarios.yasno``: read the previous readings, check, send.

    If YASNO's last readings are these very ones from ``this_month``, nothing is sent: a repeated run is a no-op.
    """
    values = {"day": math.floor(day), "night": math.floor(night)}
    results: list[dict[str, Any]] = [{"meter": zone, "value": v} for zone, v in values.items()]

    debt = await api.debt(internal_id)
    last = debt.get("lastMeterReadings") or {}
    previous = {r.get("zone"): r.get("value") for r in last.get("meteringReadings") or []}
    for result in results:
        result["previous"] = previous.get(ZONES[result["meter"]])
        if last.get("createdOn"):
            result["previous_date"] = str(last["createdOn"])[:10]
        if problem := scenarios.check_value(result["value"], result["previous"], max_increase):
            result.update(status=scenarios.STATUS_REJECTED, reason=problem)

    if not any(r.get("status") for r in results) and all(
        scenarios.already_there(r["value"], r["previous"], r.get("previous_date"), this_month) for r in results
    ):
        for result in results:
            result["status"] = scenarios.STATUS_ALREADY
        return results

    if dry_run or any(r.get("status") for r in results):
        for result in results:
            result.setdefault("status", scenarios.STATUS_DRY_RUN if dry_run else scenarios.STATUS_NO_READING)
        if "balance" in debt:
            results[0]["balance"] = debt["balance"]
        return results

    answer = await api.send(internal_id, values["day"], values["night"])
    for result in results:
        result["status"] = scenarios.STATUS_SUBMITTED
    if "balance" in answer:
        results[0]["balance"] = answer["balance"]
    return results
