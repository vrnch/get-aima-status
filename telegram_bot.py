import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from aima_service import (
    AimaError,
    FormData,
    request_mfa,
    start_browser,
    stop_browser,
    submit_form,
)

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)

SESSION_TTL = timedelta(minutes=30)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def tip_url() -> str:
    return os.getenv("BUYMEACOFFEE_URL", "").strip()


def identity_label(flow: str) -> str:
    return "passport number" if flow == "passport" else "residence permit number"


def format_confirmation(flow: str, data: dict) -> str:
    return (
        "Please confirm your data:\n\n"
        f"Name: {data['full_name']}\n"
        f"Birth date: {data['birth_date']}\n"
        f"Country: {data['country_iso_code']}\n"
        f"{identity_label(flow).capitalize()}: {data['identity_number']}\n"
        f"Email: {data['email']}\n\n"
        "Submit this request?"
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Confirm",
                    callback_data="confirm:yes",
                ),
                InlineKeyboardButton(
                    "Edit",
                    callback_data="confirm:edit",
                ),
            ]
        ]
    )


def edit_keyboard(flow: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Name", callback_data="edit:full_name")],
            [InlineKeyboardButton("Birth date", callback_data="edit:birth_date")],
            [InlineKeyboardButton("Country", callback_data="edit:country")],
            [
                InlineKeyboardButton(
                    identity_label(flow).capitalize(),
                    callback_data="edit:identity_number",
                )
            ],
            [InlineKeyboardButton("Email", callback_data="edit:email")],
            [InlineKeyboardButton("Back", callback_data="edit:back")],
        ]
    )


EDIT_PROMPTS = {
    "full_name": "Enter your full name:",
    "birth_date": "Enter your birth date as YYYY-MM-DD:",
    "country": "Enter your two-letter country code, for example PT or UA:",
    "identity_number": None,  # filled from flow
    "email": "Enter your email address:",
}


async def submit_confirmed_request(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    flow: str,
    data: dict,
) -> None:
    await _store(context).save(user_id, chat_id, flow, "submitting", data)
    await context.bot.send_message(
        chat_id,
        "Submitting your request. This may take a moment…",
    )
    form = FormData(
        identity_type=flow,
        identity_number=data["identity_number"],
        birth_date=data["birth_date"],
        country_iso_code=data["country_iso_code"],
        full_name=data["full_name"],
        email=data["email"],
    )
    try:
        tracking_token = await asyncio.to_thread(
            submit_form, form, data["mfa_code"]
        )
    except Exception as error:
        LOGGER.exception("Submission failed for Telegram user %s", user_id)
        reason = str(error) if isinstance(error, AimaError) else "Browser error"
        is_recaptcha = "recaptcha" in reason.lower()
        retry_data = dict(data)
        retry_data.pop("mfa_code", None)
        if is_recaptcha:
            await _store(context).save(
                user_id, chat_id, flow, "confirm", retry_data
            )
            await context.bot.send_message(
                chat_id,
                f"Submission failed:\n{reason}\n\n"
                "Google scored this attempt as automated. Your data is still "
                "saved — tap Confirm to request a new email code and try again. "
                "Keep the Chrome window in the foreground.\n\n"
                + format_confirmation(flow, retry_data),
                reply_markup=confirm_keyboard(),
            )
            return

        await _store(context).delete(user_id)
        await context.bot.send_message(
            chat_id,
            f"Submission failed:\n{reason}\n\n"
            "The temporary data was deleted. Send /start to try again.",
        )
        return

    await _store(context).delete(user_id)
    lines = ["Request submitted successfully."]
    if tracking_token:
        lines.append(
            f"Tracking: https://contactenos.aima.gov.pt/tracking/{tracking_token}"
        )
    lines.append("Your temporary data was deleted.")
    await context.bot.send_message(chat_id, "\n\n".join(lines))

    donate = tip_url()
    if donate:
        await asyncio.sleep(2)
        await context.bot.send_message(
            chat_id,
            "✨☕ If this helped, you can leave a small voluntary tip "
            f"(about €1-2) here:\n{donate}",
        )


async def request_mfa_for_session(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    flow: str,
    data: dict,
    *,
    retry_step: str = "confirm",
) -> None:
    await _store(context).save(user_id, chat_id, flow, "requesting_mfa", data)
    await context.bot.send_message(
        chat_id,
        "Requesting your verification code. This may take a moment…",
    )
    try:
        await asyncio.to_thread(request_mfa, data["email"])
    except Exception as error:
        LOGGER.exception("MFA request failed for Telegram user %s", user_id)
        await _store(context).save(user_id, chat_id, flow, retry_step, data)
        reason = str(error) if isinstance(error, AimaError) else "Browser error"
        if retry_step == "confirm":
            await context.bot.send_message(
                chat_id,
                f"Could not request the code:\n{reason}\n\n"
                + format_confirmation(flow, data),
                reply_markup=confirm_keyboard(),
            )
        else:
            await context.bot.send_message(
                chat_id,
                f"Could not request the code:\n{reason}\n\n"
                "Send /start to try again.",
            )
        return

    await _store(context).save(user_id, chat_id, flow, "mfa", data)
    await context.bot.send_message(
        chat_id,
        "Check your email and enter the six-digit verification code:",
    )


async def confirm_submit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if query is None or user is None or chat is None:
        return

    await query.answer()
    session = await _store(context).get(user.id)
    if session is None or session["step"] != "confirm":
        await query.edit_message_text("No pending confirmation. Send /start.")
        return

    decision = query.data.split(":", 1)[1]
    if decision == "edit":
        await _store(context).save(
            user.id,
            chat.id,
            session["flow"],
            "edit_menu",
            dict(session["data"]),
        )
        await query.edit_message_text(
            "What do you want to edit?",
            reply_markup=edit_keyboard(session["flow"]),
        )
        return

    await query.edit_message_text("Confirmed.")
    await request_mfa_for_session(
        context,
        user.id,
        chat.id,
        session["flow"],
        dict(session["data"]),
    )


async def choose_edit_field(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if query is None or user is None or chat is None:
        return

    await query.answer()
    session = await _store(context).get(user.id)
    if session is None or session["step"] != "edit_menu":
        await query.edit_message_text("No pending edit. Send /start.")
        return

    field = query.data.split(":", 1)[1]
    data = dict(session["data"])
    if field == "back":
        await _store(context).save(
            user.id, chat.id, session["flow"], "confirm", data
        )
        await query.edit_message_text(
            format_confirmation(session["flow"], data),
            reply_markup=confirm_keyboard(),
        )
        return

    await _store(context).save(
        user.id,
        chat.id,
        session["flow"],
        f"edit_{field}",
        data,
    )
    prompt = EDIT_PROMPTS[field]
    if field == "identity_number":
        prompt = f"Enter your {identity_label(session['flow'])}:"
    await query.edit_message_text(prompt)


def parse_supabase_timestamp(value: str) -> datetime:
    # Python 3.9 fromisoformat rejects fractional seconds that aren't exactly
    # 0/3/6 digits, e.g. "...25.09323+00:00" from Postgres/Supabase.
    normalized = value.replace("Z", "+00:00")
    match = re.fullmatch(
        r"(.*T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2})",
        normalized,
    )
    if match:
        base, fraction, offset = match.groups()
        micros = (fraction or ".")[1:].ljust(6, "0")[:6]
        normalized = f"{base}.{micros}{offset}" if micros else f"{base}{offset}"
    return datetime.fromisoformat(normalized)


class SessionStore:
    def __init__(self, url: str, service_role_key: str) -> None:
        base = url.strip().rstrip("/")
        if base.endswith("/rest/v1"):
            base = base[: -len("/rest/v1")]
        self.endpoint = f"{base}/rest/v1/aima_bot_sessions"
        headers = {
            "apikey": service_role_key,
            "Content-Type": "application/json",
        }
        # Legacy service_role keys are JWTs and may also be sent as Bearer
        # tokens. New sb_secret_* keys must only be sent via the apikey header.
        if not service_role_key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {service_role_key}"
        self.client = httpx.AsyncClient(
            headers=headers,
            timeout=20,
        )

    async def save(
        self,
        user_id: int,
        chat_id: int,
        flow: str,
        step: str,
        data: dict,
    ) -> None:
        expires_at = datetime.now(timezone.utc) + SESSION_TTL
        response = await self.client.post(
            self.endpoint,
            params={"on_conflict": "telegram_user_id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json={
                "telegram_user_id": user_id,
                "chat_id": chat_id,
                "flow": flow,
                "step": step,
                "data": data,
                "expires_at": expires_at.isoformat(),
            },
        )
        response.raise_for_status()

    async def get(self, user_id: int) -> Optional[dict]:
        response = await self.client.get(
            self.endpoint,
            params={
                "telegram_user_id": f"eq.{user_id}",
                "select": "chat_id,flow,step,data,expires_at",
            },
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return None

        session = rows[0]
        expires_at = parse_supabase_timestamp(session["expires_at"])
        if expires_at <= datetime.now(timezone.utc):
            await self.delete(user_id)
            return None
        return session

    async def delete(self, user_id: int) -> None:
        response = await self.client.delete(
            self.endpoint,
            params={"telegram_user_id": f"eq.{user_id}"},
        )
        response.raise_for_status()

    async def cleanup_expired(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        response = await self.client.delete(
            self.endpoint,
            params={"expires_at": f"lt.{now}"},
        )
        response.raise_for_status()

    async def close(self) -> None:
        await self.client.aclose()


def _store(context: ContextTypes.DEFAULT_TYPE) -> SessionStore:
    return context.application.bot_data["session_store"]


async def _delete_sensitive_input(update: Update) -> None:
    if update.message is None:
        return
    try:
        await update.message.delete()
    except Exception:
        # Deletion is best-effort and may depend on Telegram permissions.
        pass


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None or update.message is None:
        return

    await _store(context).delete(user.id)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("I agree and continue", callback_data="consent")]]
    )
    await update.message.reply_text(
        "This bot will send the details you provide to AIMA on your behalf. "
        "By continuing, you authorize their use for this request.\n\n"
        "Your answers are kept temporarily for up to 30 minutes and are "
        "deleted after submission or /cancel. Telegram may still retain "
        "messages under its own privacy policy.",
        reply_markup=keyboard,
    )


async def accept_consent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Passport",
                    callback_data="flow:passport",
                ),
                InlineKeyboardButton(
                    "Residence permit",
                    callback_data="flow:residence",
                ),
            ]
        ]
    )
    await query.edit_message_text(
        "Choose the document you will use:",
        reply_markup=keyboard,
    )


async def choose_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if query is None or user is None or chat is None:
        return

    await query.answer()
    flow = query.data.split(":", 1)[1]
    if flow not in {"passport", "residence"}:
        return

    await _store(context).save(
        user.id,
        chat.id,
        flow,
        "full_name",
        {},
    )
    await query.edit_message_text("Enter your full name:")


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    await _store(context).delete(user.id)
    await update.message.reply_text(
        "Cancelled. Your temporary session was deleted."
    )


async def _save_and_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: dict,
    step: str,
    data: dict,
    prompt: str,
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None or update.message is None:
        return
    await _store(context).save(
        user.id,
        chat.id,
        session["flow"],
        step,
        data,
    )
    await _delete_sensitive_input(update)
    await context.bot.send_message(chat.id, prompt)


async def handle_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    if user is None or chat is None or message is None or not message.text:
        return

    session = await _store(context).get(user.id)
    if session is None:
        await message.reply_text("No active request. Send /start first.")
        return
    if session["chat_id"] != chat.id:
        await message.reply_text("Continue in the chat where you started.")
        return

    text = message.text.strip()
    step = session["step"]
    data = dict(session["data"])

    if step == "full_name":
        if len(text) < 2:
            await message.reply_text("Enter a valid full name.")
            return
        data["full_name"] = text
        await _save_and_prompt(
            update,
            context,
            session,
            "birth_date",
            data,
            "Enter your birth date as YYYY-MM-DD:",
        )
        return

    if step == "birth_date":
        try:
            parsed_date = date.fromisoformat(text)
            if parsed_date >= date.today():
                raise ValueError
        except ValueError:
            await message.reply_text(
                "Invalid date. Use YYYY-MM-DD, for example 1990-01-31."
            )
            return
        data["birth_date"] = text
        await _save_and_prompt(
            update,
            context,
            session,
            "country",
            data,
            "Enter your two-letter country code, for example PT or UA:",
        )
        return

    if step == "country":
        country = text.upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            await message.reply_text("Enter exactly two letters.")
            return
        data["country_iso_code"] = country
        label = identity_label(session["flow"])
        await _save_and_prompt(
            update,
            context,
            session,
            "identity_number",
            data,
            f"Enter your {label}:",
        )
        return

    if step == "identity_number":
        if len(text) < 3:
            await message.reply_text("Enter a valid document number.")
            return
        data["identity_number"] = text
        await _save_and_prompt(
            update,
            context,
            session,
            "email",
            data,
            "Enter your email address:",
        )
        return

    if step == "email":
        if not EMAIL_PATTERN.fullmatch(text):
            await message.reply_text("Enter a valid email address.")
            return
        data["email"] = text
        await _store(context).save(
            user.id,
            chat.id,
            session["flow"],
            "confirm",
            data,
        )
        await _delete_sensitive_input(update)
        await context.bot.send_message(
            chat.id,
            format_confirmation(session["flow"], data),
            reply_markup=confirm_keyboard(),
        )
        return

    if step == "mfa":
        if not re.fullmatch(r"\d{6}", text):
            await message.reply_text("Enter exactly six digits.")
            return
        data["mfa_code"] = text
        await _delete_sensitive_input(update)
        await submit_confirmed_request(
            context,
            user.id,
            chat.id,
            session["flow"],
            data,
        )
        return

    if step.startswith("edit_"):
        field = step[len("edit_"):]
        if field == "full_name":
            if len(text) < 2:
                await message.reply_text("Enter a valid full name.")
                return
            data["full_name"] = text
        elif field == "birth_date":
            try:
                parsed_date = date.fromisoformat(text)
                if parsed_date >= date.today():
                    raise ValueError
            except ValueError:
                await message.reply_text(
                    "Invalid date. Use YYYY-MM-DD, for example 1990-01-31."
                )
                return
            data["birth_date"] = text
        elif field == "country":
            country = text.upper()
            if not re.fullmatch(r"[A-Z]{2}", country):
                await message.reply_text("Enter exactly two letters.")
                return
            data["country_iso_code"] = country
        elif field == "identity_number":
            if len(text) < 3:
                await message.reply_text("Enter a valid document number.")
                return
            data["identity_number"] = text
        elif field == "email":
            if not EMAIL_PATTERN.fullmatch(text):
                await message.reply_text("Enter a valid email address.")
                return
            data["email"] = text
        else:
            await message.reply_text("Unknown field. Send /start.")
            return

        await _store(context).save(
            user.id, chat.id, session["flow"], "confirm", data
        )
        await _delete_sensitive_input(update)
        await context.bot.send_message(
            chat.id,
            format_confirmation(session["flow"], data),
            reply_markup=confirm_keyboard(),
        )
        return

    await message.reply_text(
        "Your request is currently being processed. Please wait."
    )


async def on_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    LOGGER.exception("Unhandled Telegram bot error", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot_data["session_store"].cleanup_expired()
    await asyncio.to_thread(start_browser)
    LOGGER.info("Persistent AIMA browser session is ready.")


async def post_shutdown(application: Application) -> None:
    await asyncio.to_thread(stop_browser)
    await application.bot_data["session_store"].close()
    LOGGER.info("Persistent AIMA browser session was closed.")


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    supabase_key = os.getenv("SUPABASE_SECRET_KEY")
    if not supabase_key:
        supabase_key = require_env("SUPABASE_SERVICE_ROLE_KEY")
    store = SessionStore(
        require_env("SUPABASE_URL"),
        supabase_key,
    )
    application = (
        Application.builder()
        .token(require_env("TELEGRAM_BOT_TOKEN"))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["session_store"] = store
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(
        CallbackQueryHandler(accept_consent, pattern=r"^consent$")
    )
    application.add_handler(
        CallbackQueryHandler(choose_flow, pattern=r"^flow:(passport|residence)$")
    )
    application.add_handler(
        CallbackQueryHandler(confirm_submit, pattern=r"^confirm:(yes|edit)$")
    )
    application.add_handler(
        CallbackQueryHandler(
            choose_edit_field,
            pattern=r"^edit:(full_name|birth_date|country|identity_number|email|back)$",
        )
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer)
    )
    application.add_error_handler(on_error)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
