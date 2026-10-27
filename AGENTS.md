# meter-bots — Meter Bots (Home Assistant integration)

Subproject of `homeassistant`. Custom integration **`meter_bots`**: sends meter readings to the suppliers'
Telegram bots from the user's own Telegram account (Telethon user session). Behaviour: `README.md`. The readings
themselves come from `../utility-meters/` (two-zone electricity, water from photos); read its `AGENTS.md`.

## Shared Home Assistant context

Read `../AGENTS.md` (host, SSH, API token, backups) and `../energy-tg-bot/AGENTS.md` (the sibling integration this
one mirrors: layout, release, HACS via WS).

## Why bots

The user first rejected the suppliers' web APIs (short sessions, re-login all the time; Київводоканал's login has
reCAPTCHA) and Portmone (YASNO stopped taking readings through payment services). Viber has no user API.
YASNO turned out to have an anonymous web API (below), which is now the main way for YASNO; Комуналка is
still bot-only.

## YASNO web API (since 0.3.0, main way for YASNO; the bot is the fallback)

Found from the user's browser HAR files (2026-10-22 logged in, 2026-10-22 and 2026-10-27 anonymous) and checked
with curl: yasno.ua's «передати показники» works **without login, cookies or captcha**. Base
`https://app.yasno.ua/api/account-service`, headers `x-platform: Web`, `origin: https://yasno.ua`. Paths contain
`users/me` but need no user.

- Address → YASNO's **internal account id** (a short number, not the особовий рахунок; the user's real values
  are in `../AGENTS.md`): `GET /public/addresses/cities?query=` → `{items:[{id,value:"м. Київ  Київська обл."}]}`;
  `GET /public/addresses/cities/{city}/streets?query=` (needs `limit=100&offset=0`, or `items` is empty);
  `GET /public/users/me/accounts/by-address?cityId&streetId&houseNumber&apartmentNumber` →
  `{accountId, accountNumberMasked:"10******001"}`; an unknown address is 404 `EntryNotFound`. Done **once**, in
  the options flow; the mask is checked against the особовий рахунок and `yasno_internal_id` is stored.
- Each submission is two requests: `GET /public/users/me/accounts/b2c/{id}/debt` (`balance`,
  `lastMeterReadings{meteringReadings[{zone Day/Night,value}],createdOn}`, used as `previous` for
  `check_value`) and `POST …/b2c/{id}/meter-readings` `{"meteringReadings":[{"value":D,"zone":"Day"},{…"Night"}]}`
  → 200 `{feeId,balance,isEnrolledLater}`. A second reading the same day: 400 `application/problem+json`
  `code ValidationError`, `detail` «На зазначену дату вже є показання». It accepted a reading on the 27th, so the
  API doesn't enforce YASNO's end-of-month window.
- Order in `async_submit` (`via: auto`): API; on `YasnoUnavailable` (network, timeout, 5xx, 403, HTML, odd JSON)
  the bot, with the reason in the record's `api_error` and `via: telegram`. `YasnoRejected` (400/404 with
  `detail`) and `check_value` refusals don't go to the bot: it would refuse too. `via: api|telegram` forces one
  way. Without an address in the options YASNO goes through the bot as before. Known gap: a POST that went through
  but whose answer was lost (timeout) falls back to the bot, which then hits «вже є показання» and reports failed.
- yasno.ua itself runs Imperva bot protection (a JS token script on yasno.ua); `app.yasno.ua` didn't require it
  as of 2026-10-27. If it starts to, the API answers 403/HTML and the bot takes over.

## Bots (from the user's chats exported 2026-10-19)

- **@komunalkaua_bot «Комуналка»** (ЦКС/ГЕРЦ) covers all the water: per serial it lists «ХОЛОДНА ВОДА»,
  «ГАРЯЧА ВОДА» and «ВОДОВІДВЕДЕННЯ ГАРЯЧОЇ ВОДИ» (6 items for the 4 meters). So @vodokanalkiev_bot isn't needed.
  Flow: `/start` → (share phone if asked) → «🏠Адреси» → the address button → «✅Передати показання» →
  «Вибiр лiчильника» → `<serial> - <service>` → prompt with «Показники на дату: …\n<previous>\nВведіть поточні
  показання…» (previous with a decimal comma) → number `XXXXXXX.XX` → «Показники додано успiшно.» → «⬅️Повернутися».
  The bot asked for the phone again after «⬆️Додому». Texts mix Cyrillic і and Latin i.
- **@Yasnoonlinebot YASNO** (seen live 2026-10-19): every menu is **inline buttons**. Typed text outside a prompt
  gets «Я вас не розумію, скористайтеся кнопками меню». «🏠 Головне меню» may first bring a rating survey
  («Оцініть від 1 до 5», with «🏠 Головне меню» again). Main menu «Вас вітає електронний помічник YASNO!…» →
  «📊 Передати показання» → «оберіть особовий рахунок» with a button `<account> Київ, вул., …` → prompt
  «Зонність лічильника: 2 / Останні активні показання на DD.MM.YYYY / День: 15000 / Ніч: 7514 / Введіть показання
  ДЕНЬ пробіл НІЧ (всі цифри до коми).» → typed `15006 7514` → «Показники успішно внесено, оновлено баланс: …».
  The "last active" readings lag: on 2026-10-19 the user had submitted 15006 that day and the bot still showed 15000.
  YASNO's window: last 2 days of the month or first 3 of the next. ЦКС's window: from the 5th.
- Komunalka live: «⬅️Повернутися» from the number prompt goes back to the **meter list**; after a successful
  submission it goes to the address menu. «Показники на дату» shows the last *billed* reading, not one submitted the
  same day (208 shown after 222.01 was submitted).

## Login

Telegram answered `SentCodeTypeApp` with no `next_type` and never delivered the code (3 tries; a new api_id). **QR
login** works: menu step «Сканувати QR-код телефоном», `client.qr_login()` on a client with `receive_updates=True`
(the scan arrives as `UpdateLoginToken`), shown with HA's `QrCodeSelector`. The token lives ~30 s; Submit re-creates
it when expired.

## Supplier readings (0.2.0)

Every run (dry runs too) stores the reading each bot shows as its own (`previous`) in `.storage/meter_bots.<entry>`
under `supplier`, and a sensor per meter appears: `sensor.<device>_yasno_{day,night}` (kWh) and
`sensor.<device>_komunalka_<serial>` (m³; state = the supply item, attribute `items` has hot-water drainage too,
`date` = the date the bot gives). These are the supplier's last accepted readings, which lag behind what was sent.

## Status

- 2026-10-19: code + tests (`uv run pytest -q`, scripted fake bots in `tests/fakes.py`). Version 0.1.0. Not yet
  run against the real bots, not yet on GitHub.
- 2026-10-19: copied straight into HA `custom_components/meter_bots` (no HACS yet; deploy = tar over SSH + `ha core
  restart`). The user logged in by QR; the live entry id and its options are in `../AGENTS.md` (kept out of this
  public repo). Sensors are named after the entry title («Telegram <name>»):
  `sensor.telegram_<name>_{yasno,komunalka}_last_{submission,attempt}`.
- Dry runs against the real bots pass for both (all 6 Komunalka items, YASNO with previous 15000/7514). No real
  submission has been made by the integration yet. Schedule: `script.lichylnyky_peredaty_pokazy` + automations in
  `../utility-meters/` (first real runs: water 2026-10-25, YASNO 2026-10-30).
- 2026-10-20: public at https://github.com/friedpuppet/ha-meter-bots, release v0.2.0, installed on HA through HACS
  (custom repository, HACS id `1426650776`; the earlier copy is in `/config/meter_bots-copy.bak-20261020-072909.tgz`).
  **Updating**: release (bump `manifest.json` + `pyproject.toml`), then WS `hacs/repository/download` with
  `repository: "1426650776"`, `version: "vX.Y.Z"`, then `ha core check` + `ha core restart`. Push with the PAT
  `~/.config/github/token-grid-load-shedding` (extraheader, as in the sibling repos).
- **The repo is public: no personal data in it.** Tests, README and strings use made-up serials (1000001–1000004),
  account 10000000001, Telegram id 123456789 and a «Тестова» address. Real ones belong in `../AGENTS.md` only. The
  history was restarted on publishing; the old local branch `private-history` has the real data, never push it.
