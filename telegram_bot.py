import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from aima_service import AimaError, FormData, request_mfa, submit_form


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)

SESSION_TTL = timedelta(minutes=30)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SessionStore:
    def __init__(self, url: str, service_role_key: str) -> None:
        self.endpoint = (
            f"{url.rstrip('/')}/rest/v1/aima_bot_sessions"
        )
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
        expires_at = datetime.fromisoformat(
            session["expires_at"].replace("Z", "+00:00")
        )
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
        label = (
            "passport number"
            if session["flow"] == "passport"
            else "residence permit number"
        )
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
            "requesting_mfa",
            data,
        )
        await _delete_sensitive_input(update)
        await context.bot.send_message(
            chat.id,
            "Requesting your verification code. This may take a moment…",
        )
        try:
            await asyncio.to_thread(request_mfa, text)
        except Exception as error:
            LOGGER.exception("MFA request failed for Telegram user %s", user.id)
            await _store(context).save(
                user.id,
                chat.id,
                session["flow"],
                "email",
                data,
            )
            reason = str(error) if isinstance(error, AimaError) else "Browser error"
            await context.bot.send_message(
                chat.id,
                f"Could not request the code: {reason}\n"
                "Check the email and send it again, or use /cancel.",
            )
            return

        await _store(context).save(
            user.id,
            chat.id,
            session["flow"],
            "mfa",
            data,
        )
        await context.bot.send_message(
            chat.id,
            "Check your email and enter the six-digit verification code:",
        )
        return

    if step == "mfa":
        if not re.fullmatch(r"\d{6}", text):
            await message.reply_text("Enter exactly six digits.")
            return
        await _store(context).save(
            user.id,
            chat.id,
            session["flow"],
            "submitting",
            data,
        )
        await _delete_sensitive_input(update)
        await context.bot.send_message(
            chat.id,
            "Submitting your request. This may take a moment…",
        )
        form = FormData(
            identity_type=session["flow"],
            identity_number=data["identity_number"],
            birth_date=data["birth_date"],
            country_iso_code=data["country_iso_code"],
            full_name=data["full_name"],
            email=data["email"],
        )
        try:
            await asyncio.to_thread(submit_form, form, text)
        except Exception as error:
            LOGGER.exception("Submission failed for Telegram user %s", user.id)
            await _store(context).delete(user.id)
            reason = str(error) if isinstance(error, AimaError) else "Browser error"
            await context.bot.send_message(
                chat.id,
                f"Submission failed: {reason}\n"
                "The temporary data was deleted. Send /start to try again.",
            )
            return

        await _store(context).delete(user.id)
        await context.bot.send_message(
            chat.id,
            "Request submitted successfully. Your temporary data was deleted.",
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


async def post_shutdown(application: Application) -> None:
    await application.bot_data["session_store"].close()


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
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer)
    )
    application.add_error_handler(on_error)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
