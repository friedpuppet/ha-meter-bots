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
# YASNO's web API: the address is looked up once (options flow) to get YASNO's internal account id
CONF_YASNO_CITY = "yasno_city"
CONF_YASNO_STREET = "yasno_street"
CONF_YASNO_HOUSE = "yasno_house"
CONF_YASNO_APARTMENT = "yasno_apartment"
CONF_YASNO_INTERNAL_ID = "yasno_internal_id"
DEFAULT_YASNO_CITY = "Київ"
# Water without the bot: Київводоканал's public form (by the ЦКС account) and Київтеплоенерго's cabinet
CONF_KVK_ACCOUNT = "kvk_account"
CONF_KTE_EMAIL = "kte_email"
CONF_KTE_PASSWORD = "kte_password"

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
ATTR_VIA = "via"

# How a submission goes: the suppliers' web APIs first, the Telegram bot if an API gives no usable answer
VIA_AUTO = "auto"
VIA_API = "api"
VIA_TELEGRAM = "telegram"
VIAS = [VIA_AUTO, VIA_API, VIA_TELEGRAM]

# Who bills a Комуналка item: hot water itself is Київтеплоенерго's, cold water and all drainage Київводоканал's
SUPPLIER_KVK = "kvk"
SUPPLIER_KTE = "kte"

RESULT_OK = "ok"
RESULT_FAILED = "failed"
RESULT_DRY_RUN = "dry_run"
RESULTS = [RESULT_OK, RESULT_FAILED, RESULT_DRY_RUN]
