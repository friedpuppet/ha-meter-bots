"""Scenarios against the scripted bots (no Home Assistant needed)."""

from __future__ import annotations

import pytest

from custom_components.meter_bots import scenarios
from custom_components.meter_bots.dialog import Dialog, DialogError, Reply

from .fakes import ADDRESS, YASNO_ACCOUNT, FakeChat, FakeKomunalka, FakeYasno, fast_dialog

READINGS = {"1000001": 222.017, "1000002": 123.2, "1000003": 1008.107, "1000004": 355.96}


async def _komunalka(bot: FakeKomunalka, readings=READINGS, **kwargs):
    kwargs.setdefault("address", None)
    kwargs.setdefault("dry_run", False)
    kwargs.setdefault("max_increase", 50)
    return await scenarios.komunalka(fast_dialog(bot), readings, **kwargs)


async def test_komunalka_submits_every_item_of_each_serial():
    bot = FakeKomunalka()
    results = await _komunalka(bot)
    assert bot.submitted == {
        "1000001 - ГАРЯЧА ВОДА": "222.01",
        "1000002 - ГАРЯЧА ВОДА": "123.2",
        "1000002 - ВОДОВІДВЕДЕННЯ ГАРЯЧОЇ ВОДИ": "123.2",
        "1000001 - ВОДОВІДВЕДЕННЯ ГАРЯЧОЇ ВОДИ": "222.01",
        "1000003 - ХОЛОДНА ВОДА": "1008.1",
        "1000004 - ХОЛОДНА ВОДА": "355.96",
    }
    assert [r["status"] for r in results] == ["submitted"] * 6
    assert results[0]["previous"] == 208.0
    assert bot.shared  # the share-phone prompt was answered


async def test_komunalka_dry_run_sends_no_numbers():
    bot = FakeKomunalka()
    results = await _komunalka(bot, dry_run=True)
    assert bot.submitted == {}
    assert {r["status"] for r in results} == {"dry_run"}
    assert not any(s.replace(".", "").isdigit() for s in bot.sent)


async def test_komunalka_rejects_lower_and_far_higher_values():
    bot = FakeKomunalka()
    results = await _komunalka(bot, {"1000001": 200.0, "1000004": 500.0, "1000003": 1008.107})
    by_meter = {r["meter"]: r for r in results}
    assert by_meter["1000001 - ГАРЯЧА ВОДА"]["status"] == "rejected"
    assert "менший" in by_meter["1000001 - ГАРЯЧА ВОДА"]["reason"]
    assert by_meter["1000004 - ХОЛОДНА ВОДА"]["status"] == "rejected"
    assert by_meter["1000003 - ХОЛОДНА ВОДА"]["status"] == "submitted"
    assert by_meter["1000002 - ГАРЯЧА ВОДА"]["status"] == "no_reading"
    assert bot.submitted == {"1000003 - ХОЛОДНА ВОДА": "1008.1"}


async def test_komunalka_unknown_serial_is_reported():
    bot = FakeKomunalka()
    results = await _komunalka(bot, {"999": 1.0})
    assert {"meter": "999", "value": 1.0, "status": "not_found"} in results
    assert bot.submitted == {}


async def test_komunalka_back_without_button_goes_the_long_way():
    bot = FakeKomunalka(back_on_input=False)
    results = await _komunalka(bot, dry_run=True)
    assert {r["status"] for r in results} == {"dry_run"}


async def test_komunalka_address_must_be_unambiguous():
    assert scenarios.komunalka_address(["Додати адресу", ADDRESS, "⬆️Додому"], None) == ADDRESS
    assert scenarios.komunalka_address(["Додати адресу", ADDRESS, "1 ІНША 2"], "тестова") == ADDRESS
    with pytest.raises(DialogError):
        scenarios.komunalka_address(["Додати адресу", ADDRESS, "1 ІНША 2"], None)


async def test_unexpected_answer_stops_before_sending():
    class Broken(FakeKomunalka):
        def answer(self, text):
            if text in self.items:
                self.bot_says("Технічні роботи, спробуйте пізніше")
            else:
                super().answer(text)

    bot = Broken()
    with pytest.raises(DialogError, match="запит показу"):
        await _komunalka(bot)
    assert bot.submitted == {}


async def test_yasno_submits_whole_kwh():
    bot = FakeYasno()
    results = await scenarios.yasno(
        fast_dialog(bot), 15006.6, 7514.2, account=YASNO_ACCOUNT, dry_run=False, max_increase=3000
    )
    assert bot.submitted == ["15006 7514"]
    assert [r["status"] for r in results] == ["submitted", "submitted"]
    assert results[0]["balance"] == 3032.64


async def test_yasno_dry_run_stops_at_the_prompt():
    bot = FakeYasno()
    results = await scenarios.yasno(
        fast_dialog(bot), 15006, 7514, account=YASNO_ACCOUNT, dry_run=True, max_increase=3000
    )
    assert bot.submitted == []
    assert bot.sent == [
        "<click 🏠 Головне меню>",
        "<click 🏠 Головне меню>",
        "<click 📊 Передати показання>",
        f"<click {FakeYasno.ACCOUNT_BUTTON}>",
        "<click 🏠 Головне меню>",
    ]
    assert [r["previous"] for r in results] == [15000, 7514]
    assert {r["status"] for r in results} == {"dry_run"}


async def test_yasno_rejects_lower_than_previous():
    bot = FakeYasno(day=30000)
    results = await scenarios.yasno(
        fast_dialog(bot), 15006, 7514, account=YASNO_ACCOUNT, dry_run=False, max_increase=3000
    )
    assert bot.submitted == []
    assert results[0]["status"] == "rejected"
    assert results[1]["status"] == "no_reading"


async def test_dialog_waits_for_edits_and_records_transcript():
    class Editing(FakeChat):
        polls = 0

        async def replies(self, after_id):
            self.polls += 1
            text = "Зачекайте…" if self.polls < 3 else "Готово"
            return [Reply(id=after_id + 1, text=text, edited=float(self.polls >= 3))]

        def answer(self, text):
            pass

    # The bot edits its answer on the third poll; the dialog waits until it stays unchanged for `quiet`
    clock = iter(range(100))
    dialog = Dialog(Editing(), timeout=30, quiet=2, poll=0, sleep=lambda _: _noop(), clock=lambda: next(clock))
    assert await dialog.say("привіт") == "Готово"
    assert dialog.transcript == [{"from": "me", "text": "привіт"}, {"from": "bot", "text": "Готово"}]


async def test_click_returns_only_new_or_edited_messages():
    class Editing(FakeChat):
        def answer(self, text):
            # the bot edits the message with the button instead of sending a new one
            i, out, reply = self.messages[-1]
            self.messages[-1] = (i, out, Reply(id=reply.id, text="Меню", inline=["Б"], edited=1.0))

    chat = Editing()
    chat.bot_says("Старе", inline=["А"])
    dialog = fast_dialog(chat)
    await dialog.refresh()
    assert await dialog.press("А") == "Меню"
    assert dialog.inline == ["Б"]


async def test_dialog_times_out_without_answer():
    class Silent(FakeChat):
        def answer(self, text):
            pass

    clock = iter(range(100))
    dialog = Dialog(Silent(), timeout=3, quiet=0, poll=0, sleep=lambda _: _noop(), clock=lambda: next(clock))
    with pytest.raises(DialogError, match="не відповів"):
        await dialog.say("привіт")


async def _noop():
    return None


def test_format_m3_rounds_down():
    assert scenarios.format_m3(1008.107) == "1008.1"
    assert scenarios.format_m3(222.019) == "222.01"
    assert scenarios.format_m3(123.2) == "123.2"
    assert scenarios.format_m3(355.96) == "355.96"
    assert scenarios.format_m3(100.0) == "100"
