"""Scripted stand-ins for the suppliers' bots, built from real conversations (2026-10-19)."""

from __future__ import annotations

from typing import Any

from custom_components.meter_bots.dialog import Dialog, Reply

ADDRESS = "1 ВУЛИЦЯ ТЕСТОВА 1"
KOMUNALKA_ITEMS = {
    "1000001 - ГАРЯЧА ВОДА": 208.0,
    "1000002 - ГАРЯЧА ВОДА": 120.8,
    "1000002 - ВОДОВІДВЕДЕННЯ ГАРЯЧОЇ ВОДИ": 122.3,
    "1000001 - ВОДОВІДВЕДЕННЯ ГАРЯЧОЇ ВОДИ": 216.7,
    "1000003 - ХОЛОДНА ВОДА": 1003.4,
    "1000004 - ХОЛОДНА ВОДА": 353.8,
}
YASNO_ACCOUNT = "10000000001"


class FakeChat:
    """``BotChat`` whose bot answers synchronously from ``answer``."""

    def __init__(self) -> None:
        self._next_id = 100
        self.messages: list[tuple[int, bool, Reply | str]] = []  # (id, out, reply-or-text)
        self.sent: list[str] = []

    def _add(self, out: bool, item: Reply | str) -> int:
        self._next_id += 1
        self.messages.append((self._next_id, out, item))
        return self._next_id

    def bot_says(self, text: str, buttons: list[str] | None = None, inline: list[str] | None = None) -> None:
        reply_id = self._next_id + 1
        self._add(False, Reply(id=reply_id, text=text, buttons=buttons or [], inline=inline or []))

    async def send(self, text: str) -> int:
        self.sent.append(text)
        sent_id = self._add(True, text)
        self.answer(text)
        return sent_id

    async def share_contact(self) -> int:
        self.sent.append("<contact>")
        sent_id = self._add(True, "<contact>")
        self.answer("<contact>")
        return sent_id

    async def replies(self, after_id: int) -> list[Reply]:
        return [m for i, out, m in self.messages if i > after_id and not out]  # type: ignore[misc]

    async def recent(self, limit: int) -> list[Reply]:
        return [m for _, out, m in self.messages if not out][-limit:]  # type: ignore[misc]

    async def click(self, message_id: int, text: str) -> None:
        self.sent.append(f"<click {text}>")
        self.answer(f"<click {text}>")

    def answer(self, text: str) -> None:
        raise NotImplementedError


class FakeKomunalka(FakeChat):
    def __init__(self, items: dict[str, float] | None = None, *, back_on_input: bool = True) -> None:
        super().__init__()
        self.items = dict(items or KOMUNALKA_ITEMS)
        self.shared = False
        self.state = "start"
        self.back_on_input = back_on_input
        self.submitted: dict[str, str] = {}

    def _menu(self) -> None:
        self.state = "menu"
        self.bot_says(
            "Чатбот для передачі показників приладів обліку та оплат за комунальні послуги. Оберiть необхiдний пункт меню.",
            ["🏠Адреси", "💳Оплата", "ℹ️Довідка"],
        )

    def _address(self) -> None:
        self.state = "address"
        self.bot_says(ADDRESS, ["✅Передати показання", "📈Історія показань", "⬅️Повернутися"])

    def answer(self, text: str) -> None:
        if text == "/start":
            if self.shared:
                self._menu()
            else:
                self.bot_says("Для початку роботи натисніть кнопку Поділитися номером.", ["Поділитися номером"])
        elif text == "<contact>":
            self.shared = True
            self._menu()
        elif text == "🏠Адреси" and self.state == "menu":
            self.state = "addresses"
            self.bot_says("Тут ви можете додати свою адресу", ["Додати адресу", ADDRESS, "⬆️Додому"])
        elif text == ADDRESS and self.state == "addresses":
            self._address()
        elif text == "✅Передати показання" and self.state == "address":
            self.state = "meters"
            self.bot_says("Вибiр лiчильника", [*self.items, "⬅️Повернутися"])
        elif text in self.items and self.state == "meters":
            self.state = f"input:{text}"
            previous = f"{self.items[text]:g}".replace(".", ",")
            self.bot_says(
                f"КИЇВ, ВУЛИЦЯ ТЕСТОВА Б1 КВ1\nПоказники на дату: 2026-10-19\n{previous}\n"
                "Введіть поточні показання лічильника:\nПриклад: ХХХХХХХ.XX (тiльки цифри)",
                ["⬅️Повернутися"] if self.back_on_input else [],
            )
        elif text == "⬅️Повернутися" and self.state.startswith("input:"):
            self.state = "meters"
            self.bot_says("Вибiр лiчильника", [*self.items, "⬅️Повернутися"])
        elif text == "⬅️Повернутися" and self.state in ("meters", "done"):
            self._address()
        elif self.state.startswith("input:"):
            item = self.state[6:]
            self.submitted[item] = text
            self.state = "done"
            self.bot_says(f"Показники додано успiшно.\n{item}\nНовi показання лічильника: {text}", ["⬅️Повернутися"])
        else:
            self.bot_says("Оберiть пункт меню", ["🏠Адреси"])


class FakeYasno(FakeChat):
    """As seen live 2026-10-19: inline menus, a rating survey, the account as a button."""

    MENU = ["📊 Передати показання", "📄 Мої особові рахунки", "💳 Оплатити"]
    ACCOUNT_BUTTON = f"{YASNO_ACCOUNT} Київ, вул., Тестова, буд. 1, к. 1"

    def __init__(self, day: int = 15000, night: int = 7514) -> None:
        super().__init__()
        self.previous = (day, night)
        self.state = "idle"
        self.submitted: list[str] = []
        self.bot_says("Я вас не розумію, скористайтеся кнопками меню", inline=["💳 Сплатити", "🏠 Головне меню"])

    def answer(self, text: str) -> None:
        if text == "<click 🏠 Головне меню>" and self.state == "idle":
            self.state = "survey"
            self.bot_says(
                "Наскільки легко було користуватися нашим сервісом? Оцініть від 1 до 5",
                inline=["1 ⭐", "🏠 Головне меню"],
            )
        elif text == "<click 🏠 Головне меню>":
            self.state = "menu"
            self.bot_says(
                "Вас вітає електронний помічник YASNO! Оберіть послугу для подальшої роботи в боті!", inline=self.MENU
            )
        elif text == "<click 📊 Передати показання>" and self.state == "menu":
            self.state = "account"
            self.bot_says(
                "Для передачі показань оберіть особовий рахунок.",
                inline=[self.ACCOUNT_BUTTON, "➕ Додати особовий рахунок", "🏠 Головне меню"],
            )
        elif text == f"<click {self.ACCOUNT_BUTTON}>" and self.state == "account":
            self.state = "readings"
            day, night = self.previous
            self.bot_says(
                f"Особовий рахунок: {YASNO_ACCOUNT}\n\nЗонність лічильника: 2\nОстанні активні показання на 19.10.2026\n\n"
                f"День: {day}\nНіч: {night}\n\nВведіть показання ДЕНЬ пробіл НІЧ (всі цифри до коми).",
                inline=["⬅️ Назад", "🏠 Головне меню"],
            )
        elif self.state == "readings" and not text.startswith("<click"):
            self.submitted.append(text)
            self.state = "idle"
            self.bot_says(
                f"Показники успішно внесено, оновлено баланс: 3032.64\n\nОсобовий рахунок: {YASNO_ACCOUNT}",
                inline=["💳 Сплатити", "✏️ Редагувати суму", "🏠 Головне меню"],
            )
        else:
            self.bot_says("Я вас не розумію, скористайтеся кнопками меню", inline=["🏠 Головне меню"])


async def _no_sleep(_: float) -> None:
    return None


def fast_dialog(chat: Any) -> Dialog:
    return Dialog(chat, timeout=1, quiet=0, poll=0, sleep=_no_sleep)
