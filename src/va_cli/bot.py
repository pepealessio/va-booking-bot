from __future__ import annotations

import asyncio
import logging
import os
import sys

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from .client import VirginActiveClient
from .config import Config
from .store import BookingStore

logger = logging.getLogger("va-bot")


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    booking_id = query.data.removeprefix("cancel:")

    config = Config.from_env()
    store = BookingStore(config.state_dir / "bookings.json")
    record = store.load(booking_id)

    if record is None:
        await query.edit_message_text("❌ This booking no longer exists.")
        return

    def _cancel() -> None:
        with VirginActiveClient(config, verbose=False) as client:
            client.cancel(record.token, approve=lambda _: True)

    try:
        await asyncio.to_thread(_cancel)
    except Exception as e:
        logger.exception("cancel failed for booking %s", booking_id)
        await query.edit_message_text(
            f"⚠️ Cancel failed: {e}\n\nTry again from the CLI: `va cancel {record.token}`",
            parse_mode="Markdown",
        )
        return

    store.delete(booking_id)
    try:
        await query.edit_message_text(
            f"❌ Booking cancelled for {record.class_desc}",
            reply_markup=None,
        )
    except Exception:
        logger.warning("could not update message for cancelled booking %s", booking_id)


def run_bot() -> None:
    token = os.environ.get("VA_NOTIFY_TOKEN")
    if not token:
        print("error: VA_NOTIFY_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    app = Application.builder().token(token.strip()).build()
    app.add_handler(CallbackQueryHandler(handle_cancel, pattern=r"^cancel:"))
    logger.info("starting bot polling")
    app.run_polling()
