"""Київтеплоенерго's personal cabinet (kte.kmda.gov.ua/my-cabinet, Drupal 7): send hot-water readings.

Checked 2026-10-27. The guest form has an image captcha, the cabinet doesn't: log in with e-mail and password,
open the account's «Передача показань лічильників» form and post it. There is no JSON, only HTML forms. The
site's firewall answers 403 «Web Application Firewall» to clients that don't look like a browser, so the
requests carry a browser User-Agent. The session cookie lives about two hours; every run logs in again.

Each meter's history page lists every reading with its status («Зараховано»), including those sent through
@komunalkaua_bot («ГЕРЦ онлайн»); its newest accepted one is the previous reading.
"""

from __future__ import annotations

import html
import re
from typing import Any

import aiohttp

from . import scenarios

BASE = "https://kte.kmda.gov.ua/my-cabinet/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9",
}
TIMEOUT = aiohttp.ClientTimeout(total=30)
SERVICE = "ГАРЯЧА ВОДА"  # as @komunalkaua_bot names Київтеплоенерго's item
DONE = "Дякуємо за внесення показників"

LOGIN_FORM = re.compile(r'<form[^>]*id="user-login-form".*?</form>', re.S)
SEND_FORM = re.compile(r'<form[^>]*id="send-counters-user-new-form".*?</form>', re.S)
ACCOUNT = re.compile(r"send_counters/send_current_new/(\d+)")
INPUT = re.compile(r"<input\b[^>]*>", re.S)
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
ROW = re.compile(r"<tr\b.*?</tr>", re.S)
CELL = re.compile(r"<td\b.*?</td>", re.S)
COUNTER_FIELD = re.compile(r'name="(counter_last_(\d+)_\d+)"')
ERROR = re.compile(r'<div[^>]*class="[^"]*messages[^"]*error[^"]*"[^>]*>(.*?)</div>', re.S)


class KteError(Exception):
    """Base of the cabinet's errors."""


class KteRejected(KteError):
    """The cabinet refused the readings. The bot would most likely refuse too."""


class KteUnavailable(KteError):
    """No usable answer: network, firewall, login failed, a page we don't recognise. The bot may still work."""


def text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b.*?</\1>", "", fragment, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def inputs(form: str) -> dict[str, str]:
    """name → value of the form's inputs, as a browser would post them (no buttons)."""
    fields: dict[str, str] = {}
    for tag in INPUT.findall(form):
        attrs = dict(ATTR.findall(tag))
        if "name" in attrs and attrs.get("type") not in ("submit", "button"):
            fields[attrs["name"]] = html.unescape(attrs.get("value", ""))
    return fields


def number(value: str) -> float | None:
    found = re.match(r"\s*(\d+(?:[.,]\d+)?)", value)
    return float(found.group(1).replace(",", ".")) if found else None


class KteCabinet:
    """One logged-in visit; ``session`` must have its own cookie jar."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _page(self, path: str, data: dict[str, str] | None = None) -> str:
        method = "POST" if data is not None else "GET"
        try:
            async with self._session.request(method, BASE + path, data=data, headers=HEADERS, timeout=TIMEOUT) as resp:
                body = await resp.text()
                # Drupal answers 403 «Немає доступу» with the login form to a visitor who isn't logged in
                if resp.status not in (200, 403) or "Web Application Firewall" in body[:2000]:
                    raise KteUnavailable(f"HTTP {resp.status}{' firewall' if 'Firewall' in body[:2000] else ''}")
                return body
        except (aiohttp.ClientError, TimeoutError, UnicodeDecodeError) as err:
            raise KteUnavailable(f"{type(err).__name__}: {err}") from err

    async def login(self, email: str, password: str) -> list[str]:
        """Log in; the ids of the cabinet's accounts (as in ``send_current_new/<id>``)."""
        page = await self._page("userhome_new")
        if not (form := LOGIN_FORM.search(page)):
            raise KteUnavailable("немає форми входу")
        fields = inputs(form.group(0))
        fields.update(name=email, **{"pass": password, "op": "Увійти"})
        page = await self._page("userhome_new?destination=userhome_new", fields)
        if accounts := sorted(set(ACCOUNT.findall(page))):
            return accounts
        if error := ERROR.search(page):
            raise KteUnavailable(f"вхід не вдався: {text(error.group(1))}")
        raise KteUnavailable("вхід не вдався: у кабінеті немає особових рахунків")

    async def form(self, account: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """The send form's fields and its meters: ``serial``, ``field``, ``counter`` id, ``previous``."""
        page = await self._page(f"send_counters/send_current_new/{account}")
        if not (found := SEND_FORM.search(page)):
            raise KteUnavailable(f"немає форми передачі для рахунку {account}")
        form = found.group(0)
        meters = []
        for row in ROW.findall(form):
            if not (field := COUNTER_FIELD.search(row)):
                continue
            cells = [text(c) for c in CELL.findall(row)]
            # № ліч. | Місце | Послуга | Одиниця | Попередні («208 Історія лічильника») | Поточні | Повірка
            meters.append(
                {
                    "serial": cells[0],
                    "field": field.group(1),
                    "counter": field.group(2),
                    "previous": number(cells[4]) if len(cells) > 4 else None,
                }
            )
        if not meters:
            raise KteUnavailable(f"у формі рахунку {account} немає лічильників")
        return inputs(form), meters

    async def last_accepted(self, account: str, counter: str) -> tuple[float, str] | None:
        """The newest «Зараховано» reading in the meter's history, with its date."""
        page = await self._page(f"send_counters/history_counters/{account}/{counter}")
        for row in ROW.findall(page):
            cells = [text(c) for c in CELL.findall(row)]
            # Дата + статус | t Режим | Поточні | Тип | Джерело | Створення запису | Коментар
            accepted = len(cells) >= 3 and "Зараховано" in cells[0] and "Не зараховано" not in cells[0]
            if accepted and (value := number(cells[2])) is not None:
                return value, cells[0][:10]
        return None

    async def send(self, account: str, fields: dict[str, str]) -> None:
        page = await self._page(f"send_counters/send_current_new/{account}", fields)
        if DONE in page:
            return
        if error := ERROR.search(page):
            raise KteRejected(text(error.group(1)))
        raise KteUnavailable("кабінет не підтвердив передачу")


async def kte(
    cabinet: KteCabinet,
    email: str,
    password: str,
    readings: dict[str, float],
    *,
    dry_run: bool,
    max_increase: float,
    this_month: str | None = None,
) -> list[dict[str, Any]]:
    """Same checks and result format as ``scenarios.komunalka``, for the meters Київтеплоенерго lists.

    A meter whose history already has this reading accepted in ``this_month`` isn't sent again.
    """
    results: list[dict[str, Any]] = []
    for account in await cabinet.login(email, password):
        fields, meters = await cabinet.form(account)
        changed = False
        for meter in meters:
            name = f"{meter['serial']} - {SERVICE}"
            if meter["serial"] not in readings:
                results.append({"meter": name, "status": scenarios.STATUS_NO_READING})
                continue
            value = float(scenarios.format_m3(readings[meter["serial"]]))
            result: dict[str, Any] = {"meter": name, "previous": meter["previous"], "value": value}
            if last := await cabinet.last_accepted(account, meter["counter"]):
                result["previous"], result["previous_date"] = last
            results.append(result)
            if problem := scenarios.check_value(value, result["previous"], max_increase):
                result.update(status=scenarios.STATUS_REJECTED, reason=problem)
            elif scenarios.already_there(value, result["previous"], result.get("previous_date"), this_month):
                result["status"] = scenarios.STATUS_ALREADY
            elif dry_run:
                result["status"] = scenarios.STATUS_DRY_RUN
            else:
                fields[meter["field"]] = scenarios.format_m3(value)
                result["status"] = scenarios.STATUS_SUBMITTED
                changed = True
        if changed:
            await cabinet.send(account, fields)
    return results
