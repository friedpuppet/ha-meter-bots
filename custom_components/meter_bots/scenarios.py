"""What to say to each bot. Every step checks the answer; anything unexpected stops the run.

Texts are matched loosely: the bots mix Cyrillic «і» with Latin «i» («Вибiр лiчильника»), and button
captions carry emoji that may change, so buttons are found by a word, not by their exact caption.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .const import SUPPLIER_KTE, SUPPLIER_KVK
from .dialog import Dialog, DialogError, expect

I = "[іi]"  # Cyrillic or Latin i

STATUS_SUBMITTED = "submitted"
STATUS_DRY_RUN = "dry_run"
STATUS_REJECTED = "rejected"  # our own check refused the value; nothing was sent for it
STATUS_NOT_FOUND = "not_found"  # no such meter in the bot
STATUS_NO_READING = "no_reading"  # the bot has the meter, we have no value for it
STATUS_ALREADY = "already_there"  # the supplier already has this very reading from this month; not sent again


def parse_number(text: str) -> float:
    return float(re.sub(r"\s", "", text).replace(",", "."))


def check_value(value: float, previous: float | None, max_increase: float) -> str | None:
    """Why ``value`` must not be sent, or None."""
    if value <= 0:
        return f"показ {value} не додатний"
    if previous is None:
        return None
    if value < previous - 0.01:
        return f"показ {value} менший за попередній {previous}"
    if value > previous + max_increase:
        return f"показ {value} більший за попередній {previous} на понад {max_increase}"
    return None


def already_there(value: float, previous: float | None, previous_date: str | None, this_month: str | None) -> bool:
    """The supplier's last accepted reading is this one and from this month (``YYYY-MM``): a repeat run skips it."""
    return (
        this_month is not None
        and previous is not None
        and abs(value - previous) < 0.005
        and (previous_date or "")[:7] == this_month
    )


def format_m3(value: float) -> str:
    """Two decimals at most, rounded down: never report water that hasn't flowed yet."""
    text = f"{math.floor(value * 100 + 1e-6) / 100:.2f}"
    return text.rstrip("0").rstrip(".")


# --- Комуналка (@komunalkaua_bot): water for Київводоканал and Київтеплоенерго ---------------------------

KOMUNALKA_SHARE_PHONE = r"Поділитися номером"
KOMUNALKA_MAIN_MENU = r"пункт меню"
KOMUNALKA_NAV = ("додати", "додому", "повернутися", "назад", "меню")
KOMUNALKA_METER = re.compile(r"^\s*(\d+)\s*-\s*(.+?)\s*$")
KOMUNALKA_PREVIOUS = re.compile(r"Показники на дату:[^\n]*\n\s*(\d[\d\s]*(?:[.,]\d+)?)\s*\n")


async def komunalka_home(dialog: Dialog) -> None:
    """/start, share the phone number if asked, end up in the main menu."""
    text = await dialog.say("/start")
    if re.search(KOMUNALKA_SHARE_PHONE, text, re.IGNORECASE):
        text = await dialog.share_contact()
    expect(text, KOMUNALKA_MAIN_MENU, "головне меню")


def komunalka_address(keyboard: list[str], wanted: str | None) -> str:
    candidates = [b for b in keyboard if not any(n in b.casefold() for n in KOMUNALKA_NAV)]
    if wanted:
        matching = [b for b in candidates if wanted.casefold() in b.casefold()]
        if len(matching) == 1:
            return matching[0]
        raise DialogError(f"адреса «{wanted}» не знайдена однозначно серед {candidates}")
    if len(candidates) == 1:
        return candidates[0]
    raise DialogError(f"кілька адрес або жодної: {candidates}; вкажіть адресу в налаштуваннях")


async def komunalka_back(dialog: Dialog, place: str) -> None:
    """Back one level with «Повернутися» if the bot offers it, otherwise the long way round to the address."""
    if back := dialog.find_button("Повернутися"):
        await dialog.say(back)
        return
    await komunalka_home(dialog)
    await dialog.say(dialog.button("Адреси"))
    await dialog.say(dialog.button(place))


def komunalka_supplier(item: str) -> str:
    """«1000001 - ГАРЯЧА ВОДА» → kte; cold water and every drainage item → kvk."""
    service = KOMUNALKA_METER.match(item).group(2).upper()
    return SUPPLIER_KTE if service.startswith("ГАРЯЧА") else SUPPLIER_KVK


async def komunalka(
    dialog: Dialog,
    readings: dict[str, float],
    *,
    address: str | None,
    dry_run: bool,
    max_increase: float,
    suppliers: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Send every meter of the address whose serial number is in ``readings``.

    One serial can stand for several items (hot water and its drainage are billed separately), each gets
    the same value. ``suppliers`` limits the run to those suppliers' items (see ``komunalka_supplier``) when
    the others already went through their web APIs.
    """
    await komunalka_home(dialog)
    await dialog.say(dialog.button("Адреси"))
    place = komunalka_address(dialog.keyboard, address)
    await dialog.say(place)

    text = await dialog.say(dialog.button("Передати показання"))
    expect(text, f"Виб{I}р л{I}чильника", "вибір лічильника")
    items = [b for b in dialog.keyboard if KOMUNALKA_METER.match(b)]
    if not items:
        raise DialogError(f"бот не показав жодного лічильника: {dialog.keyboard}")

    results: list[dict[str, Any]] = []
    serials = {KOMUNALKA_METER.match(i).group(1) for i in items}
    for serial in readings:
        if serial not in serials:
            results.append({"meter": serial, "value": readings[serial], "status": STATUS_NOT_FOUND})

    for item in items:
        serial = KOMUNALKA_METER.match(item).group(1)
        if suppliers is not None and komunalka_supplier(item) not in suppliers:
            continue
        if serial not in readings:
            results.append({"meter": item, "status": STATUS_NO_READING})
            continue
        # «Повернутися» leads back to the meter list from the input prompt, but to the address menu after a submission
        if item not in dialog.keyboard:
            text = await dialog.say(dialog.button("Передати показання"))
            expect(text, f"Виб{I}р л{I}чильника", "вибір лічильника")
        if item not in dialog.keyboard:
            raise DialogError(f"лічильник «{item}» зник з меню: {dialog.keyboard}")

        text = await dialog.say(item)
        expect(text, "Введ[іi]ть поточн[іi] показання", "запит показу")
        found = KOMUNALKA_PREVIOUS.search(text)
        previous = parse_number(found.group(1)) if found else None
        value = float(format_m3(readings[serial]))
        result: dict[str, Any] = {"meter": item, "previous": previous, "value": value}
        if dated := re.search(r"Показники на дату:\s*(\d{4}-\d{2}-\d{2})", text):
            result["previous_date"] = dated.group(1)
        results.append(result)

        problem = check_value(value, previous, max_increase)
        if problem or dry_run:
            result["status"] = STATUS_REJECTED if problem else STATUS_DRY_RUN
            if problem:
                result["reason"] = problem
            # Leave the input prompt without sending a number
            await komunalka_back(dialog, place)
            continue

        text = await dialog.say(format_m3(value))
        expect(text, f"додано усп{I}шно", "підтвердження")
        result["status"] = STATUS_SUBMITTED
        result["reply"] = text
        await komunalka_back(dialog, place)
    return results


# --- YASNO (@Yasnoonlinebot): electricity, two zones ----------------------------------------------------

YASNO_DONE = r"успішно внесено"
YASNO_BALANCE = re.compile(r"баланс:\s*(-?[\d\s]+(?:[.,]\d+)?)")


async def yasno(
    dialog: Dialog,
    day: float,
    night: float,
    *,
    account: str,
    dry_run: bool,
    max_increase: float,
) -> list[dict[str, Any]]:
    """Main menu → «📊 Передати показання» → the account's button → "DAY NIGHT" in whole kWh.

    All menus are inline buttons. The prompt shows the last accepted readings («День: 15000\\nНіч: 7514»),
    which the new ones are checked against.
    """
    values = {"day": math.floor(day), "night": math.floor(night)}
    results: list[dict[str, Any]] = [{"meter": zone, "value": v} for zone, v in values.items()]

    await dialog.refresh()
    # «Головне меню» may first bring a rating survey («Оцініть від 1 до 5»), which has «Головне меню» again
    for _ in range(3):
        if dialog.find_button("Передати показання") or dialog.find_inline("Передати показання"):
            break
        await dialog.press("Головне меню")
    text = await dialog.press("Передати показання")
    if dialog.find_button(account) or dialog.find_inline(account):
        text = await dialog.press(account)
    else:
        expect(text, "рахун", "запит особового рахунку")
        text = await dialog.say(account)
    expect(text, "Введ[іi]ть показання", "запит показів")

    dated = re.search(r"показання на (\d{2})\.(\d{2})\.(\d{4})", text)
    for result, label in zip(results, ("День", "Ніч"), strict=True):
        found = re.search(rf"{label}:\s*(\d[\d\s]*)", text)
        result["previous"] = parse_number(found.group(1)) if found else None
        if dated:
            result["previous_date"] = f"{dated.group(3)}-{dated.group(2)}-{dated.group(1)}"
        if problem := check_value(result["value"], result["previous"], max_increase):
            result.update(status=STATUS_REJECTED, reason=problem)
    rejected = any(r.get("status") for r in results)

    if dry_run or rejected:
        for result in results:
            result.setdefault("status", STATUS_DRY_RUN if dry_run else STATUS_NO_READING)
        await dialog.press("Головне меню")
        return results

    text = await dialog.say(f"{values['day']} {values['night']}")
    expect(text, YASNO_DONE, "підтвердження")
    for result in results:
        result["status"] = STATUS_SUBMITTED
        result["reply"] = text
    if balance := YASNO_BALANCE.search(text):
        results[0]["balance"] = parse_number(balance.group(1))
    return results
