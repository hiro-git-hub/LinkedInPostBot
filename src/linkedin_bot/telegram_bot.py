"""Long-Polling-Bot: schickt Entwürfe zur Freigabe und startet die geplanten Läufe.

Kein Webhook -> keine öffentliche URL nötig, läuft lokal wie im Container.
"""

import asyncio
import logging
import os
import secrets
from datetime import time
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from linkedin_bot.config import AppConfig
from linkedin_bot.errors import describe_error
from linkedin_bot.healthcheck import HEARTBEAT_INTERVAL_SECONDS, write_heartbeat
from linkedin_bot.integrations.linkedin import LinkedInClient, LinkedInError, code_from_callback, params_from_url
from linkedin_bot.nodes.approval import PLACEHOLDER, VARIANT_LABELS
from linkedin_bot.nodes.hashtags import hashtag_line
from linkedin_bot.runtime import (
    Runtime, Step, current_draft, is_awaiting_approval, outcome_message, pending_steps, render_header,
    render_variant, resume, start_run,
)
from linkedin_bot.state import VARIANTS, Decision

log = logging.getLogger(__name__)

# PTB zählt Wochentage ab Sonntag = 0.
WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
TELEGRAM_LIMIT = 4096

VARIANT_KEYBOARD = [
    [("✅ Diese Version freigeben", "approve")],
    [("✏️ Bearbeiten", "edit"), ("🔁 Überarbeiten lassen", "revise")],
]
# Aktionen, für die wir erst noch Text vom Autor brauchen
ASK_FOR_TEXT = {
    "edit": "Schick mir den kompletten neuen Text der {label}-Version als nächste Nachricht "
            "(Hashtags am Ende werden übernommen).",
    "revise": "Was soll an der {label}-Version anders werden? Schick mir dein Feedback als nächste Nachricht.",
}
PROGRESS = {
    "new_topic": "⏳ Nehme ein anderes Thema und recherchiere neu …",
    "image": "🎨 Erzeuge ein Bild – dauert etwa eine Minute …",
}


def _button(label: str, action: str, variant: str, thread_id: str) -> InlineKeyboardButton:
    # callback_data max. 64 Byte: "new_topic|humor|2026-09-28-61390f" passt.
    return InlineKeyboardButton(label, callback_data=f"{action}|{variant}|{thread_id}")


def control_keyboard(thread_id: str, has_image: bool) -> InlineKeyboardMarkup:
    image_row = ([_button("🔄 Neues Bild", "image", "", thread_id), _button("🗑️ Ohne Bild", "no_image", "", thread_id)]
                 if has_image else [_button("🖼️ Bild erzeugen", "image", "", thread_id)])
    return InlineKeyboardMarkup([
        image_row,
        [_button("🔀 Anderes Thema", "new_topic", "", thread_id), _button("❌ Verwerfen", "reject", "", thread_id)],
    ])


def variant_keyboard(thread_id: str, variant: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button(label, action, variant, thread_id) for label, action in row] for row in VARIANT_KEYBOARD
    ])


class ApprovalBot:
    def __init__(self, cfg: AppConfig, runtime: Runtime):
        self.cfg = cfg
        self.graph = runtime.graph
        self.repo = runtime.repo
        # Ohne Chat-ID läuft der Bot im Einrichtungsmodus: nur /start antwortet (mit der Chat-ID).
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.chat_id = int(chat_id) if chat_id else None
        # Wartet der Bot auf Text für edit/revise? Nur im Speicher – nach Neustart einfach erneut tippen.
        self.awaiting: tuple[str, str, str] | None = None  # (action, variant, thread_id)
        self.login_state: str | None = None  # CSRF-Schutz für /login
        self.lock = asyncio.Lock()  # ein Graph-Lauf zur Zeit; LLM-Kosten und Reihenfolge bleiben überschaubar

    # --- Graph ausführen und Ergebnis zustellen -------------------------------------------------

    async def advance(self, app: Application, fn, *args) -> None:
        async with self.lock:
            try:
                step: Step = await asyncio.to_thread(fn, self.graph, *args)
            except Exception as exc:
                log.exception("Graph-Lauf fehlgeschlagen")
                await app.bot.send_message(self.chat_id, f"💥 Lauf fehlgeschlagen.\n{describe_error(exc)}")
                return
        await self.deliver(app, step)

    async def deliver(self, app: Application, step: Step) -> None:
        if step.pending:
            pending = step.pending
            await app.bot.send_message(self.chat_id, render_header(pending)[:TELEGRAM_LIMIT],
                                       reply_markup=control_keyboard(step.thread_id, pending["has_image"]))
            if pending["has_image"]:
                image = await asyncio.to_thread(self.repo.get_image, step.thread_id)
                if image:
                    await app.bot.send_photo(self.chat_id, image, caption=f"🖼️ {pending['image_alt']}"[:1024])
            for variant in VARIANTS:
                if variant in pending["drafts"]:
                    await app.bot.send_message(self.chat_id, render_variant(pending, variant)[:TELEGRAM_LIMIT],
                                               reply_markup=variant_keyboard(step.thread_id, variant))
        else:
            await app.bot.send_message(self.chat_id, outcome_message(step.state)[:TELEGRAM_LIMIT])

    async def check_linkedin_login(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Täglich: rechtzeitig an die 60-Tage-Neuanmeldung erinnern."""
        if self.cfg.linkedin.dry_run:
            return
        auth = await asyncio.to_thread(self.repo.get_linkedin_auth)
        if auth is None:
            text = "🔑 Noch keine LinkedIn-Anmeldung – freigegebene Posts können nicht veröffentlicht werden."
        elif auth.expired:
            text = "🔑 Die LinkedIn-Anmeldung ist abgelaufen – Posts werden nicht veröffentlicht."
        elif auth.expires_within(self.cfg.linkedin.remind_days_before_expiry):
            text = f"🔑 Die LinkedIn-Anmeldung läuft am {auth.expires_at:%d.%m.%Y} ab."
        else:
            return
        await context.bot.send_message(self.chat_id, f"{text}\nNeu anmelden: /login")

    # --- Handler ----------------------------------------------------------------------------------

    def authorized(self, update: Update) -> bool:
        return self.chat_id is not None and update.effective_chat is not None and update.effective_chat.id == self.chat_id

    async def cmd_start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        # Absichtlich ohne Auth: So findet man beim Einrichten seine Chat-ID heraus.
        if self.chat_id is None:
            await update.message.reply_text(
                f"Deine Chat-ID: {update.effective_chat.id}\n"
                "Trag sie als TELEGRAM_CHAT_ID in die .env ein und starte den Bot neu."
            )
        elif self.authorized(update):
            await update.message.reply_text(
                "Bereit. /run startet sofort einen Lauf, /offen zeigt offene Entwürfe erneut, /login meldet bei LinkedIn an."
            )

    async def cmd_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        await update.message.reply_text("🔎 Sammle News, recherchiere und schreibe – das dauert ein paar Minuten …")
        context.application.create_task(self.advance(context.application, start_run))

    async def cmd_login(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """LinkedIn-Login ohne öffentliche URL: Redirect zeigt auf localhost, der Autor schickt die Adresse zurück."""
        if not self.authorized(update):
            return
        self.login_state = secrets.token_urlsafe(16)
        url = LinkedInClient(self.cfg.linkedin.api_version).authorize_url(self.login_state)
        await update.message.reply_text(
            "🔑 LinkedIn-Anmeldung:\n"
            "1. Link öffnen und bei LinkedIn zustimmen.\n"
            "2. Danach zeigt der Browser eine Fehlerseite (»localhost nicht erreichbar«) – das ist richtig so.\n"
            "3. Die komplette Adresse aus der Adresszeile kopieren und mir hier schicken (gültig 30 Minuten).\n\n"
            f"{url}",
            disable_web_page_preview=True,
        )

    async def finish_login(self, update: Update, text: str) -> None:
        state, self.login_state = self.login_state, None
        client = LinkedInClient(self.cfg.linkedin.api_version)
        try:
            code = code_from_callback(params_from_url(text), state)
            auth = await asyncio.to_thread(client.exchange_code, code)
        except LinkedInError as exc:
            await update.message.reply_text(f"❌ {exc}\nBitte /login erneut starten.")
            return
        await asyncio.to_thread(self.repo.save_linkedin_auth, auth)
        await update.message.reply_text(f"✅ Angemeldet als {auth.name}, gültig bis {auth.expires_at:%d.%m.%Y}.")

    async def cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        if not await self.resend_pending(context.application):
            await update.message.reply_text("Keine offenen Entwürfe.")

    async def resend_pending(self, app: Application) -> int:
        """Offene Entwürfe mit frischen Buttons erneut schicken (alte Nachrichten haben nach einem Klick keine mehr)."""
        self.awaiting = None
        steps = await asyncio.to_thread(pending_steps, self.graph)
        for step in steps:
            await self.deliver(app, step)
        return len(steps)

    async def resend_on_startup(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.resend_pending(context.application)

    async def scheduled_run(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.advance(context.application, start_run)

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.authorized(update):
            await query.answer()
            return
        action, variant, thread_id = query.data.split("|", 2)
        if not is_awaiting_approval(self.graph, thread_id):
            await query.answer("Dieser Entwurf ist nicht mehr offen.")
            await query.edit_message_reply_markup(None)
            return
        draft = current_draft(self.graph, thread_id, variant) if variant else ""
        if action == "approve" and PLACEHOLDER in draft:
            # Direkt als Popup abfangen, statt den ganzen Entwurf erneut zu schicken (der Graph prüft zusätzlich).
            await query.answer(
                "Im Text steht noch ein [EIGENE ERFAHRUNG: …]-Platzhalter.\n\n"
                "✏️ Bearbeiten: eigenen Text schicken\n"
                "🔁 Überarbeiten lassen: z.B. \"Platzhalter entfernen\"",
                show_alert=True,
            )
            return
        await query.answer()
        await query.edit_message_reply_markup(None)  # verhindert Doppelklicks

        if action in ASK_FOR_TEXT:
            self.awaiting = (action, variant, thread_id)
            await context.bot.send_message(self.chat_id, ASK_FOR_TEXT[action].format(label=VARIANT_LABELS[variant]))
            if action == "edit":
                # Reiner Post-Text inkl. Hashtags als eigene Nachricht – lässt sich in Telegram am Stück kopieren.
                tags = self.graph.get_state({"configurable": {"thread_id": thread_id}}).values.get("hashtags") or []
                full = f"{draft}\n\n{hashtag_line(tags)}" if tags else draft
                await context.bot.send_message(self.chat_id, full[:TELEGRAM_LIMIT])
            return
        if action in PROGRESS:
            await context.bot.send_message(self.chat_id, PROGRESS[action])
        decision = Decision(action=action, variant=variant) if variant else Decision(action=action)
        context.application.create_task(self.advance(context.application, resume, thread_id, decision))

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        if self.login_state and "code=" in update.message.text:
            await self.finish_login(update, update.message.text)
            return
        if self.awaiting is None:
            await update.message.reply_text(
                "Ich weiß gerade nicht, wofür dieser Text ist. Tippe zuerst unter dem Entwurf auf "
                "✏️ Bearbeiten oder 🔁 Überarbeiten lassen und schick ihn dann noch einmal. "
                "Keine Buttons mehr sichtbar? /offen schickt den Entwurf neu."
            )
            return
        action, variant, thread_id = self.awaiting
        self.awaiting = None
        if action == "revise":
            await update.message.reply_text(f"⏳ Überarbeite die {VARIANT_LABELS[variant]}-Version …")
        decision = Decision(action=action, variant=variant, text=update.message.text)
        context.application.create_task(self.advance(context.application, resume, thread_id, decision))

    async def heartbeat(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Für den Docker-Healthcheck: nur schreiben, wenn Telegram und Postgres wirklich antworten."""
        try:
            await context.bot.get_me()
            await asyncio.to_thread(self.repo.ping)
        except Exception as exc:
            log.warning("Heartbeat fehlgeschlagen: %s", exc)
            return
        write_heartbeat()

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if isinstance(context.error, NetworkError):
            # Kurze Netzaussetzer: PTB wiederholt das Polling selbst – kein Traceback nötig.
            log.warning("Telegram nicht erreichbar: %s", context.error)
            return
        log.error("Fehler im Bot", exc_info=context.error)

    # --- Start ------------------------------------------------------------------------------------

    def run(self) -> None:
        app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("run", self.cmd_run))
        app.add_handler(CommandHandler("offen", self.cmd_pending))
        app.add_handler(CommandHandler("login", self.cmd_login))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self.on_error)

        if self.chat_id is None:
            log.warning("TELEGRAM_CHAT_ID fehlt – Einrichtungsmodus: schick dem Bot /start, um deine Chat-ID zu erfahren.")
            app.run_polling(allowed_updates=Update.ALL_TYPES)
            return

        schedule = self.cfg.schedule
        tz = ZoneInfo(schedule.timezone)
        for run in schedule.runs:
            hour, minute = map(int, run.time.split(":"))
            app.job_queue.run_daily(self.scheduled_run, time=time(hour, minute, tzinfo=tz), days=(WEEKDAYS[run.day],))
        # Beim Start und dann täglich prüfen
        app.job_queue.run_once(self.check_linkedin_login, when=5)
        app.job_queue.run_once(self.resend_on_startup, when=3)
        app.job_queue.run_repeating(self.heartbeat, interval=HEARTBEAT_INTERVAL_SECONDS, first=5)
        app.job_queue.run_daily(self.check_linkedin_login, time=time(9, 0, tzinfo=tz))
        log.info("Bot läuft – geplante Läufe (%s): %s", schedule.timezone,
                 ", ".join(f"{r.day} {r.time}" for r in schedule.runs))
        app.run_polling(allowed_updates=Update.ALL_TYPES)
