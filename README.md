# Meter Bots

> Personal project, published only so it can be installed through HACS as a custom repository.
> Use it if it's useful to you, but there's no support and no promise of stability.

A Home Assistant integration that sends meter readings to Kyiv suppliers' Telegram bots **from your own
Telegram account**, the way you would by hand:

| Bot | What |
|---|---|
| [@komunalkaua_bot](https://t.me/komunalkaua_bot) «Комуналка» | water: cold, hot and hot-water drainage (Київводоканал, Київтеплоенерго) |
| [@Yasnoonlinebot](https://t.me/Yasnoonlinebot) YASNO | electricity, day and night |

The suppliers' own sites can take the readings too, and when set up in the options they are the main way; the bot
is used only for what they can't take:

| Site | What | Needs |
|---|---|---|
| YASNO's web API (the one yasno.ua uses) | electricity | the address, no login |
| Київводоканал's public «надати показники» form | cold water, hot-water drainage | the ЦКС account number from the bill, no login |
| Київтеплоенерго's personal cabinet (kte.kmda.gov.ua/my-cabinet) | hot water | the cabinet's e-mail and password |

Київтеплоенерго's site blocks clients that don't look like a browser, so the integration sends a browser
User-Agent there. Its guest form has a captcha; the cabinet doesn't, which is why the login is needed.

A bot can't talk to another bot, so the integration logs in to your account (Telethon, Telegram's client API).
It writes only to these two chats. Every step checks the bot's answer; anything unexpected stops the run and
nothing is confirmed blindly. Readings are also checked against the previous value the bot shows.

## Setup

1. Submit once by hand in each bot: share your phone number, add the address / account there.
2. Create an API id and hash at [my.telegram.org](https://my.telegram.org) → API development tools.
3. Add the integration: API id, hash, phone, then the login code Telegram sends to your app (and the cloud
   password, if the account has one). In the options set the YASNO account number, and for the web API the
   YASNO address (city, street, house, apartment, as on yasno.ua). The address is looked up once when you save;
   the account it finds must match the account number. For water set the Київводоканал account (the ЦКС
   особовий рахунок on the water bill) and the Київтеплоенерго cabinet login; both are checked when you save.

The session gives full access to the account and is stored in Home Assistant (and its backups). Revoke it any
time in Telegram → Settings → Devices; the integration then asks you to log in again.

## Service `meter_bots.submit`

```yaml
action: meter_bots.submit
data:
  bot: komunalka            # or yasno
  readings:                 # komunalka: by meter serial number, m³ (two decimals, rounded down)
    "1000001": 222.017
    "1000003": 1008.107
  dry_run: true             # go up to the number prompt and stop
response_variable: result   # result, per-meter statuses, the whole conversation
```

For `yasno`: `readings: {day: 15006, night: 7514}` (whole kWh). `via: auto` (default) tries the web APIs first
and the bot for what they didn't take: a supplier without an API set up, or whose API didn't answer usably (for
water only that supplier's items go to the bot). `via: api` / `via: telegram` use only that way. A refusal by an
API (e.g. a reading lower than the supplier's) doesn't go to the bot. The result has `via` (`api`, `telegram`
or `api+telegram`) and, after a fallback, `api_error`. `komunalka` stays the name of the water submission whichever
way it goes; item names and the supplier sensors are the same either way.

Per bot there are two sensors: *last submission* (timestamp of the last real one that went through) and
*last attempt* (`ok` / `failed` / `dry_run`, details in the attributes). Scheduling is left to an automation.

## Development

```sh
uv sync
uv run pytest -q
```
