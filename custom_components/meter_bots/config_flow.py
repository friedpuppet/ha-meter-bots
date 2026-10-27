"""Log in to the user's Telegram account (phone → code → 2FA password) and store the session."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

from telethon import TelegramClient
from telethon.tl.custom import QRLogin
from telethon.tl.types import auth as auth_types
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import make_client
from .const import (
    CONF_API_HASH,
    CONF_API_ID,
    CONF_CODE,
    CONF_KOMUNALKA_ADDRESS,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_SESSION,
    CONF_YASNO_ACCOUNT,
    CONF_YASNO_APARTMENT,
    CONF_YASNO_CITY,
    CONF_YASNO_HOUSE,
    CONF_YASNO_INTERNAL_ID,
    CONF_YASNO_STREET,
    DEFAULT_YASNO_CITY,
    DOMAIN,
)
from .yasno_api import YasnoApi, YasnoRejected, YasnoUnavailable, mask_matches

_LOGGER = logging.getLogger(__name__)

# Where Telegram says it sent the code (auth.SentCode.type)
DELIVERY = {
    "SentCodeTypeApp": "в застосунок Telegram, чат «Telegram»",
    "SentCodeTypeSms": "SMS",
    "SentCodeTypeSmsWord": "SMS (слово)",
    "SentCodeTypeSmsPhrase": "SMS (фраза)",
    "SentCodeTypeFirebaseSms": "SMS",
    "SentCodeTypeFragmentSms": "SMS через Fragment",
    "SentCodeTypeCall": "голосовим дзвінком",
    "SentCodeTypeFlashCall": "дзвінком (код — останні цифри номера)",
    "SentCodeTypeMissedCall": "пропущеним дзвінком (код — останні цифри номера)",
    "SentCodeTypeEmailCode": "на пошту, прив'язану для входу",
}

PASSWORD = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))


YASNO_ADDRESS = (CONF_YASNO_STREET, CONF_YASNO_HOUSE, CONF_YASNO_APARTMENT)


def _options_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    keys = (CONF_YASNO_ACCOUNT, CONF_YASNO_CITY, *YASNO_ADDRESS, CONF_KOMUNALKA_ADDRESS)
    suggested = {CONF_YASNO_CITY: DEFAULT_YASNO_CITY, **defaults}
    return vol.Schema(
        {
            vol.Optional(key, description={"suggested_value": suggested.get(key)}): selector.TextSelector()
            for key in keys
        }
    )


class MeterBotsConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per Telegram account."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: TelegramClient | None = None
        self._api_id = 0
        self._api_hash = ""
        self._phone = ""
        self._phone_code_hash = ""
        self._delivery = ""
        self._qr: QRLogin | None = None
        self._qr_task: asyncio.Future | None = None

    async def _disconnect(self) -> None:
        if self._qr_task is not None:
            self._qr_task.cancel()
            self._qr_task = None
        self._qr = None
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def _send_code(self, errors: dict[str, str]) -> None:
        await self._disconnect()
        self._client = make_client("", self._api_id, self._api_hash)
        try:
            await self._client.connect()
            sent = await self._client.send_code_request(self._phone)
        except ApiIdInvalidError:
            errors[CONF_API_ID] = "invalid_api_id"
        except PhoneNumberInvalidError:
            errors[CONF_PHONE] = "invalid_phone"
        except FloodWaitError as err:
            _LOGGER.warning("Telegram asks to wait %s s before the next code", err.seconds)
            errors["base"] = "flood_wait"
        except OSError:
            errors["base"] = "cannot_connect"
        else:
            _LOGGER.info("Telegram login code: %s", sent.to_dict())
            if isinstance(sent, auth_types.SentCodePaymentRequired):
                errors["base"] = "payment_required"
            elif isinstance(sent.type, auth_types.SentCodeTypeSetUpEmailRequired):
                errors["base"] = "email_required"
            else:
                self._phone_code_hash = sent.phone_code_hash
                kind = type(sent.type).__name__
                self._delivery = DELIVERY.get(kind, kind)
                return
        await self._disconnect()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._api_id = int(user_input[CONF_API_ID])
            self._api_hash = user_input[CONF_API_HASH].strip()
            self._phone = user_input[CONF_PHONE].replace(" ", "")
            return await self.async_step_method()
        return self._user_form(errors, user_input or {})

    def _user_form(self, errors: dict[str, str], defaults: Mapping[str, Any]) -> ConfigFlowResult:
        schema = vol.Schema(
            {
                vol.Required(CONF_API_ID, default=defaults.get(CONF_API_ID, vol.UNDEFINED)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_API_HASH): PASSWORD,
                vol.Required(CONF_PHONE, default=defaults.get(CONF_PHONE, vol.UNDEFINED)): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEL)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_method(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """QR (scan with the phone) or a login code sent to the app."""
        return self.async_show_menu(step_id="method", menu_options=["qr", "send_code"])

    async def async_step_send_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        await self._send_code(errors)
        if errors:
            return self._user_form(errors, {CONF_API_ID: self._api_id, CONF_PHONE: self._phone})
        return await self.async_step_code()

    async def _new_qr(self, errors: dict[str, str]) -> None:
        """Fresh login token (valid ~30 s) and a task waiting for the phone to scan it."""
        if self._qr_task is not None:
            self._qr_task.cancel()
        try:
            if self._qr is None:
                await self._disconnect()
                self._client = make_client("", self._api_id, self._api_hash, receive_updates=True)
                await self._client.connect()
                self._qr = await self._client.qr_login()
            else:
                await self._qr.recreate()
        except ApiIdInvalidError:
            errors["base"] = "invalid_api_id"
            await self._disconnect()
            return
        except OSError:
            errors["base"] = "cannot_connect"
            await self._disconnect()
            return
        self._qr_task = self.hass.async_create_background_task(self._qr.wait(), f"{DOMAIN} QR login")

    async def async_step_qr(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None or self._qr_task is None:
            await self._new_qr(errors)
        else:
            # The phone may still be talking to Telegram when Submit is pressed
            await asyncio.wait({self._qr_task}, timeout=10)
            if self._qr_task.done():
                task, self._qr_task = self._qr_task, None
                try:
                    task.result()
                except SessionPasswordNeededError:
                    return await self.async_step_password()
                except TimeoutError:
                    errors["base"] = "qr_expired"
                    await self._new_qr(errors)
                else:
                    return await self._async_finish()
            else:
                errors["base"] = "qr_not_scanned"
        if self._qr is None:
            return self._user_form(errors, {CONF_API_ID: self._api_id, CONF_PHONE: self._phone})
        schema = vol.Schema(
            {
                vol.Optional("qr"): selector.QrCodeSelector(
                    selector.QrCodeSelectorConfig(
                        data=self._qr.url, scale=6, error_correction_level=selector.QrErrorCorrectionLevel.QUARTILE
                    )
                )
            }
        )
        return self.async_show_form(step_id="qr", data_schema=schema, errors=errors)

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and self._client is not None:
            try:
                await self._client.sign_in(
                    self._phone, user_input[CONF_CODE].strip(), phone_code_hash=self._phone_code_hash
                )
            except SessionPasswordNeededError:
                return await self.async_step_password()
            except PhoneCodeInvalidError:
                errors[CONF_CODE] = "invalid_code"
            except PhoneCodeExpiredError:
                errors[CONF_CODE] = "code_expired"
                await self._send_code(errors)
            else:
                return await self._async_finish()
        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required(CONF_CODE): selector.TextSelector()}),
            errors=errors,
            description_placeholders={"phone": self._phone, "delivery": self._delivery},
        )

    async def async_step_password(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and self._client is not None:
            try:
                await self._client.sign_in(password=user_input[CONF_PASSWORD])
            except PasswordHashInvalidError:
                errors[CONF_PASSWORD] = "invalid_password"
            else:
                return await self._async_finish()
        return self.async_show_form(
            step_id="password", data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD}), errors=errors
        )

    async def _async_finish(self) -> ConfigFlowResult:
        assert self._client is not None
        me = await self._client.get_me()
        session = self._client.session.save()
        await self._disconnect()
        data = {
            CONF_API_ID: self._api_id,
            CONF_API_HASH: self._api_hash,
            CONF_PHONE: self._phone,
            CONF_SESSION: session,
        }
        await self.async_set_unique_id(str(me.id))
        if self.source == "reauth":
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        self._abort_if_unique_id_configured()
        name = " ".join(p for p in (me.first_name, me.last_name) if p) or self._phone
        return self.async_create_entry(title=f"Telegram {name}", data=data, options={})

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._api_id = entry_data[CONF_API_ID]
        self._api_hash = entry_data[CONF_API_HASH]
        self._phone = entry_data[CONF_PHONE]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_method()
        return self.async_show_form(step_id="reauth_confirm", description_placeholders={"phone": self._phone})

    def async_remove(self) -> None:
        if self._client is not None:
            self.hass.async_create_task(self._disconnect())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MeterBotsOptionsFlow()


class MeterBotsOptionsFlow(OptionsFlow):
    """Account and address the scenarios pick in the bots; the YASNO address gives the API's internal id."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"detail": ""}
        if user_input is not None:
            options: dict[str, Any] = {k: v.strip() for k, v in user_input.items() if v and v.strip()}
            if any(k in options for k in YASNO_ADDRESS):
                await self._async_yasno_internal_id(options, errors, placeholders)
            if not errors:
                return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(user_input or self.config_entry.options),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_yasno_internal_id(
        self, options: dict[str, Any], errors: dict[str, str], placeholders: dict[str, str]
    ) -> None:
        """Look the address up once and keep YASNO's internal id; the особовий рахунок must match its mask."""
        old = self.config_entry.options
        if not all(k in options for k in YASNO_ADDRESS):
            errors["base"] = "yasno_address_incomplete"
            return
        if CONF_YASNO_ACCOUNT not in options:
            errors[CONF_YASNO_ACCOUNT] = "yasno_account_required"
            return
        options.setdefault(CONF_YASNO_CITY, DEFAULT_YASNO_CITY)
        lookup = (CONF_YASNO_ACCOUNT, CONF_YASNO_CITY, *YASNO_ADDRESS)
        if old.get(CONF_YASNO_INTERNAL_ID) and all(old.get(k) == options[k] for k in lookup):
            options[CONF_YASNO_INTERNAL_ID] = old[CONF_YASNO_INTERNAL_ID]
            return
        api = YasnoApi(async_get_clientsession(self.hass))
        try:
            found = await api.find_account(*(options[k] for k in (CONF_YASNO_CITY, *YASNO_ADDRESS)))
        except YasnoRejected as err:
            errors["base"] = "yasno_address_not_found"
            placeholders["detail"] = f"\n\n{err}"
            return
        except YasnoUnavailable as err:
            errors["base"] = "yasno_cannot_connect"
            placeholders["detail"] = f"\n\n{err}"
            return
        masked = str(found.get("accountNumberMasked", ""))
        if not mask_matches(masked, options[CONF_YASNO_ACCOUNT]):
            errors[CONF_YASNO_ACCOUNT] = "yasno_account_mismatch"
            placeholders["detail"] = f"\n\nYASNO: {masked}"
            return
        options[CONF_YASNO_INTERNAL_ID] = found["accountId"]
