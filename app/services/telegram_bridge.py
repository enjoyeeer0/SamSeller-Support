import logging
import time
from datetime import datetime
from typing import Callable

from telebot import TeleBot

from app.models import PendingReply
from app.stores.client_store import ClientStore
from app.stores.reply_store import ReplyStore
from app.utils.text import compact, escape_html


class TelegramBridge:
    def __init__(self, token: str, admin_chat_id: int) -> None:
        self.bot = TeleBot(token, parse_mode="HTML")
        self.admin_chat_id = admin_chat_id
        self.reply_store = ReplyStore()
        self.client_store = ClientStore()
        self._send_to_funpay: Callable[[int | str, str, str | None], object] | None = None

        @self.bot.message_handler(func=lambda m: True, content_types=["text"])
        def on_text(message):
            self._handle_text(message)

    def set_sender(self, sender: Callable[[int | str, str, str | None], object]) -> None:
        self._send_to_funpay = sender

    def send_funpay_notification(
        self,
        chat_id: int | str,
        chat_name: str | None,
        author: str | None,
        author_id: int | None,
        text: str | None,
    ) -> None:
        ctx = self.client_store.upsert(chat_id, chat_name, author, author_id, text)

        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        safe_text = escape_html(text, fallback="[empty or non-text message]")
        safe_author = escape_html(author)
        safe_chat = escape_html(chat_name)

        payload = (
            "<b>New FunPay message</b>\n"
            f"Client code: <code>{ctx.code}</code>\n"
            f"Time: <code>{created_at}</code>\n"
            f"Client: <b>{safe_author}</b>\n"
            f"Client ID: <code>{author_id if author_id is not None else 'unknown'}</code>\n"
            f"Chat: <code>{safe_chat}</code>\n"
            f"Chat ID: <code>{chat_id}</code>\n\n"
            f"<code>{safe_text}</code>"
        )

        sent = self.bot.send_message(self.admin_chat_id, payload)
        self.reply_store.set(
            sent.message_id,
            PendingReply(chat_id=chat_id, chat_name=chat_name, client_code=ctx.code),
        )

    def _handle_text(self, message) -> None:
        if message.chat.id != self.admin_chat_id:
            return

        text = (message.text or "").strip()

        if text == "/help":
            self._send_help(message)
            return
        if text == "/clients":
            self._send_clients(message)
            return
        if text.lower().startswith("/to "):
            self._handle_send_to_code(message, text)
            return

        if not message.reply_to_message:
            self.bot.reply_to(
                message,
                "Reply to a notification, or use /clients and /to CODE your message.",
            )
            return

        link = self.reply_store.get(message.reply_to_message.message_id)
        if not link:
            self.bot.reply_to(message, "Cannot find linked FunPay chat for this reply.")
            return

        self._send_to_funpay_message(message, link.chat_id, link.chat_name, link.client_code)

    def _handle_send_to_code(self, message, full_text: str) -> None:
        parts = full_text.split(maxsplit=2)
        if len(parts) < 3:
            self.bot.reply_to(message, "Usage: /to C001 your message")
            return

        code = parts[1].upper()
        user_text = parts[2].strip()
        if not user_text:
            self.bot.reply_to(message, "Message cannot be empty.")
            return

        ctx = self.client_store.get_by_code(code)
        if not ctx:
            self.bot.reply_to(message, f"Client code {code} not found. Use /clients.")
            return

        self._send_to_funpay_message(message, ctx.chat_id, ctx.chat_name, ctx.code, user_text)

    def _send_to_funpay_message(
        self,
        message,
        chat_id: int | str,
        chat_name: str | None,
        client_code: str,
        override_text: str | None = None,
    ) -> None:
        if self._send_to_funpay is None:
            self.bot.reply_to(message, "FunPay sender is not initialized.")
            return

        text = (override_text if override_text is not None else (message.text or "")).strip()
        if not text:
            self.bot.reply_to(message, "Cannot send empty message.")
            return

        response = self._send_to_funpay(chat_id, text, chat_name)
        if response:
            self.bot.reply_to(message, f"Sent to FunPay ({client_code}).")
        else:
            self.bot.reply_to(message, f"Failed to send to FunPay ({client_code}).")

    def _send_help(self, message) -> None:
        text = (
            "Commands:\n"
            "<code>/help</code> - show help\n"
            "<code>/clients</code> - list recent clients with short codes\n"
            "<code>/to C001 your text</code> - send message to client by code\n\n"
            "You can also reply directly to a notification message."
        )
        self.bot.reply_to(message, text)

    def _send_clients(self, message) -> None:
        clients = self.client_store.list_clients(limit=20)
        if not clients:
            self.bot.reply_to(message, "No clients yet.")
            return

        lines = ["<b>Recent clients</b>"]
        for client in clients:
            when = client.last_message_at.strftime("%H:%M:%S")
            author = escape_html(client.author)
            preview = escape_html(compact(client.last_message_text, max_len=50), fallback="-")
            lines.append(
                f"<code>{client.code}</code> | <b>{author}</b> | {when} | <code>{preview}</code>"
            )

        self.bot.reply_to(message, "\n".join(lines))

    def run_polling_forever(self) -> None:
        while True:
            try:
                self.bot.infinity_polling(timeout=30, long_polling_timeout=30)
            except Exception:
                logging.exception("Telegram polling crashed. Restarting in 3 seconds.")
                time.sleep(3)
