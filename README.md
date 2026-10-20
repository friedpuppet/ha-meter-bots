# Meter Bots

> Personal project, published only so it can be installed through HACS as a custom repository.
> Use it if it's useful to you, but there's no support and no promise of stability.

A Home Assistant integration that sends meter readings to Kyiv suppliers' Telegram bots **from your own
Telegram account**, the way you would by hand:

| Bot | What |
|---|---|
| [@komunalkaua_bot](https://t.me/komunalkaua_bot) «Комуналка» | water: cold, hot and hot-water drainage (Київводоканал, Київтеплоенерго) |
| [@Yasnoonlinebot](https://t.me/Yasnoonlinebot) YASNO | electricity, day and night |

A bot can't talk to another bot, so the integration logs in to your account (Telethon, Telegram's client API).
It writes only to these two chats. Every step checks the bot's answer; anything unexpected stops the run and
nothing is confirmed blindly. Readings are also checked against the previous value the bot shows.

## Setup

1. Submit once by hand in each bot: share your phone number, add the address / account there.
2. Create an API id and hash at [my.telegram.org](https://my.telegram.org) → API development tools.
3. Add the integration: API id, hash, phone, then the login code Telegram sends to your app (and the cloud
   password, if the account has one). In the options set the YASNO account number.

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

For `yasno`: `readings: {day: 15006, night: 7514}` (whole kWh).

Per bot there are two sensors: *last submission* (timestamp of the last real one that went through) and
*last attempt* (`ok` / `failed` / `dry_run`, details in the attributes). Scheduling is left to an automation.

## Development

```sh
uv sync
uv run pytest -q
```
