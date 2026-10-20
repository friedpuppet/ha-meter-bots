"""Constants for Meter Bots."""

DOMAIN = "meter_bots"

CONF_API_ID = "api_id"
CONF_API_HASH = "api_hash"
CONF_PHONE = "phone"
CONF_SESSION = "session"
CONF_CODE = "code"
CONF_PASSWORD = "password"
CONF_YASNO_ACCOUNT = "yasno_account"
CONF_KOMUNALKA_ADDRESS = "komunalka_address"

BOT_YASNO = "yasno"
BOT_KOMUNALKA = "komunalka"
BOTS = [BOT_YASNO, BOT_KOMUNALKA]

# The only chats this integration ever writes to.
BOT_USERNAMES = {
    BOT_YASNO: "Yasnoonlinebot",
    BOT_KOMUNALKA: "komunalkaua_bot",
}

# A new reading may exceed the bot's previous one by at most this much (kWh / m³).
DEFAULT_MAX_INCREASE = {
    BOT_YASNO: 3000,
    BOT_KOMUNALKA: 50,
}

SERVICE_SUBMIT = "submit"
ATTR_BOT = "bot"
ATTR_READINGS = "readings"
ATTR_DRY_RUN = "dry_run"
ATTR_MAX_INCREASE = "max_increase"

RESULT_OK = "ok"
RESULT_FAILED = "failed"
RESULT_DRY_RUN = "dry_run"
RESULTS = [RESULT_OK, RESULT_FAILED, RESULT_DRY_RUN]
