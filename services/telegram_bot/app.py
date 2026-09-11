"""FastAPI webhook surface for the classification bot."""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from telegram import Bot, BotCommand, BotCommandScopeChat, MenuButtonCommands

from .callbacks import BOT_COMMANDS
from .config import load_settings
from .service import ClassificationService

logger = logging.getLogger(__name__)

settings = load_settings()


async def configure_webhook(bot, configured_settings):
    base_url = configured_settings.public_base_url
    if not base_url:
        return None
    if not base_url.startswith("https://"):
        raise RuntimeError("TELEGRAM_BOT_PUBLIC_BASE_URL must use HTTPS")
    if not configured_settings.webhook_secret:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required for webhook registration")
    webhook_url = f"{base_url}/telegram/webhook"
    configured = await bot.set_webhook(
        url=webhook_url,
        secret_token=configured_settings.webhook_secret,
        allowed_updates=["message", "callback_query"],
    )
    if not configured:
        raise RuntimeError("Telegram rejected setWebhook")
    return webhook_url


async def configure_command_menu(bot, configured_settings):
    commands = [BotCommand(command, description) for command, description in BOT_COMMANDS]
    for chat_id in sorted(configured_settings.allowed_chat_ids):
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=chat_id))
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonCommands(),
        )


@asynccontextmanager
async def lifespan(app):
    bot = Bot(token=settings.bot_token)
    try:
        await bot.initialize()
    except Exception as exc:
        # Placeholder/unreachable credentials must not block local bring-up;
        # API calls will surface the real error when Telegram is contacted.
        logger.warning("bot.initialize() failed (%s); continuing uninitialized", exc)
    service = ClassificationService(settings)
    service.bot = bot
    app.state.bot = bot
    app.state.service = service
    app.state.webhook_url = await configure_webhook(bot, settings)
    try:
        await configure_command_menu(bot, settings)
    except Exception as exc:
        logger.warning("Telegram command menu registration failed (%s)", exc)
    if not settings.auth_configured:
        logger.warning(
            "TELEGRAM_ALLOWED_CHAT_IDS / TELEGRAM_ALLOWED_USER_IDS are not both set; "
            "all commands will be rejected (fail closed)."
        )
    logger.info(
        "telegram bot webhook service started on port %s (webhook=%s)",
        settings.listen_port,
        app.state.webhook_url or "manual",
    )
    yield
    await bot.shutdown()


app = FastAPI(title="finpay-telegram-bot", lifespan=lifespan)


@app.get("/healthz")
async def healthz(request: Request):
    return {
        "status": "ok",
        "auth_configured": settings.auth_configured,
        "webhook_configured": bool(request.app.state.webhook_url),
        "webhook_url": request.app.state.webhook_url,
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not settings.webhook_secret or not hmac.compare_digest(header, settings.webhook_secret):
        raise HTTPException(status_code=403, detail="forbidden")
    payload = await request.json()
    service = request.app.state.service
    await service.handle_webhook_update(payload)
    return {"ok": True}
