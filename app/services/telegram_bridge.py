import logging
import threading
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
        self._lock = threading.Lock()
        self._dedupe: dict[tuple[int | str, str], float] = {}
        self._last_forward_ts_by_chat: dict[int | str, float] = {}
        self._recent_outgoing: dict[int | str, tuple[float, str]] = {}
        self._forwarded_count = 0
        self._suppressed_duplicate_count = 0
        self._suppressed_chat_cooldown_count = 0
        self._suppressed_recent_outgoing_count = 0
        self._suppressed_filtered_count = 0

        self._listener_healthy = True
        self._listener_last_error: str | None = None
        self._listener_last_error_ts: float | None = None
        self._listener_last_recovered_ts: float | None = None
        self._health_ping_interval_seconds = 15 * 60

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
        safe_text_plain = (text or "").strip()
        if not safe_text_plain:
            return
        if not self._should_forward_client_text(safe_text_plain):
            with self._lock:
                self._suppressed_filtered_count += 1
            return
        if self.was_recent_outgoing_message(chat_id, safe_text_plain):
            with self._lock:
                self._suppressed_recent_outgoing_count += 1
            return

        now_ts = time.time()
        key = (chat_id, safe_text_plain)
        with self._lock:
            if now_ts - self._dedupe.get(key, 0.0) < 30.0:
                self._suppressed_duplicate_count += 1
                return
            if now_ts - self._last_forward_ts_by_chat.get(chat_id, 0.0) < 2.5:
                self._suppressed_chat_cooldown_count += 1
                return
            self._dedupe[key] = now_ts
            self._last_forward_ts_by_chat[chat_id] = now_ts

            # Cleanup stale anti-spam entries.
            stale_dedupe = [k for k, ts in self._dedupe.items() if now_ts - ts > 300.0]
            for k in stale_dedupe:
                self._dedupe.pop(k, None)
            stale_chat = [cid for cid, ts in self._last_forward_ts_by_chat.items() if now_ts - ts > 300.0]
            for cid in stale_chat:
                self._last_forward_ts_by_chat.pop(cid, None)

        ctx = self.client_store.upsert(chat_id, chat_name, author, author_id, text)

        created_at = datetime.now().strftime("%d.%m %H:%M:%S")
        safe_text = escape_html(self._limit_text(text), fallback="[пусто]")
        safe_author = escape_html(author)

        payload = (
            f"Код: <code>{ctx.code}</code>\n"
            f"Ник: <b>{safe_author}</b>\n"
            f"Дата: <code>{created_at}</code>\n"
            f"Chat ID: <code>{chat_id}</code>\n\n"
            f"<code>{safe_text}</code>"
        )

        sent = self.bot.send_message(self.admin_chat_id, payload)
        self.reply_store.set(
            sent.message_id,
            PendingReply(chat_id=chat_id, chat_name=chat_name, client_code=ctx.code),
        )
        with self._lock:
            self._forwarded_count += 1

    @staticmethod
    def _should_forward_client_text(text: str) -> bool:
        normalized = (text or "").strip()
        lower = normalized.lower()
        if not normalized:
            return False
        if lower.startswith("/"):
            return False

        blocked_prefixes = (
            "ваши активные аренды:",
            "коды steam guard:",
            "нет активных аренд для получения steam guard",
            "у вас сейчас нет активных аренд",
            "продавец вызван",
        )
        return not lower.startswith(blocked_prefixes)

    @staticmethod
    def _limit_text(text: str | None, limit: int = 600) -> str | None:
        if text is None:
            return None
        normalized = "\n".join(line.rstrip() for line in str(text).splitlines()).strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."

    def _handle_text(self, message) -> None:
        if message.chat.id != self.admin_chat_id:
            return

        text = (message.text or "").strip()

        if text == "/help":
            self._send_help(message)
            return
        if text == "/status":
            self._send_status(message)
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
                "Ответьте реплаем на уведомление или используйте /clients и /to КОД ваш_текст.",
            )
            return

        link = self.reply_store.get(message.reply_to_message.message_id)
        if not link:
            self.bot.reply_to(message, "Не удалось найти связанный чат FunPay для этого реплая.")
            return

        self._send_to_funpay_message(message, link.chat_id, link.chat_name, link.client_code)

    def _handle_send_to_code(self, message, full_text: str) -> None:
        parts = full_text.split(maxsplit=2)
        if len(parts) < 3:
            self.bot.reply_to(message, "Формат: /to C001 ваш_текст")
            return

        code = parts[1].upper()
        user_text = parts[2].strip()
        if not user_text:
            self.bot.reply_to(message, "Текст сообщения не может быть пустым.")
            return

        ctx = self.client_store.get_by_code(code)
        if not ctx:
            self.bot.reply_to(message, f"Код клиента {code} не найден. Используйте /clients.")
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
            self.bot.reply_to(message, "Модуль отправки в FunPay не инициализирован.")
            return

        text = (override_text if override_text is not None else (message.text or "")).strip()
        if not text:
            self.bot.reply_to(message, "Нельзя отправить пустое сообщение.")
            return

        response = self._send_to_funpay(chat_id, text, chat_name)
        if response:
            self.note_outgoing_message(chat_id, text)
            self.bot.reply_to(message, "✅ Доставлено")
        else:
            self.bot.reply_to(message, "❌ Ошибка доставки")

    def note_outgoing_message(self, chat_id: int | str, text: str) -> None:
        with self._lock:
            self._recent_outgoing[chat_id] = (time.time(), (text or "").strip())

    def was_recent_outgoing_message(self, chat_id: int | str, text: str, window_seconds: float = 20.0) -> bool:
        with self._lock:
            cached = self._recent_outgoing.get(chat_id)
        if not cached:
            return False
        ts, cached_text = cached
        if time.time() - ts > window_seconds:
            return False
        return cached_text == (text or "").strip()

    def _send_help(self, message) -> None:
        text = (
            "Команды:\n"
            "<code>/help</code> - помощь\n"
            "<code>/status</code> - состояние бота\n"
            "<code>/clients</code> - список последних клиентов\n"
            "<code>/to C001 ваш_текст</code> - отправка по коду клиента\n\n"
            "Также можно отвечать реплаем на уведомление."
        )
        self.bot.reply_to(message, text)

    def _send_status(self, message) -> None:
        status_text = self._build_status_text()
        self.bot.reply_to(message, status_text)

    def _send_clients(self, message) -> None:
        clients = self.client_store.list_clients(limit=20)
        if not clients:
            self.bot.reply_to(message, "Пока нет клиентов.")
            return

        lines = ["<b>Последние клиенты</b>"]
        for client in clients:
            when = client.last_message_at.strftime("%H:%M:%S")
            author = escape_html(client.author)
            preview = escape_html(compact(client.last_message_text, max_len=50), fallback="-")
            lines.append(
                f"<code>{client.code}</code> | <b>{author}</b> | {when} | <code>{preview}</code>"
            )

        self.bot.reply_to(message, "\n".join(lines))

    def report_listener_error(self, error_text: str) -> None:
        now = time.time()
        with self._lock:
            self._listener_healthy = False
            self._listener_last_error = error_text.strip() or "Неизвестная ошибка"
            self._listener_last_error_ts = now

    def report_listener_recovered(self) -> None:
        now = time.time()
        with self._lock:
            was_unhealthy = not self._listener_healthy
            self._listener_healthy = True
            self._listener_last_recovered_ts = now
        if was_unhealthy:
            self._send_text_to_admin("✅ Listener восстановлен после ошибки.")

    def _build_status_text(self) -> str:
        with self._lock:
            listener_healthy = self._listener_healthy
            last_error = self._listener_last_error
            last_error_ts = self._listener_last_error_ts
            last_recovered_ts = self._listener_last_recovered_ts
            forwarded = self._forwarded_count
            suppressed_duplicate = self._suppressed_duplicate_count
            suppressed_chat_cooldown = self._suppressed_chat_cooldown_count
            suppressed_recent_outgoing = self._suppressed_recent_outgoing_count
            suppressed_filtered = self._suppressed_filtered_count

        def fmt_ts(value: float | None) -> str:
            if value is None:
                return "-"
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")

        listener_state = "OK" if listener_healthy else "ОШИБКА"
        text = (
            "📊 Статус\n"
            f"Слушатель: <b>{listener_state}</b>\n"
            f"Последняя ошибка: <code>{escape_html(last_error, fallback='-')}</code>\n"
            f"Время ошибки: <code>{fmt_ts(last_error_ts)}</code>\n"
            f"Время восстановления: <code>{fmt_ts(last_recovered_ts)}</code>\n\n"
            f"Форвардов в Telegram: <code>{forwarded}</code>\n"
            f"Подавлено дублей: <code>{suppressed_duplicate}</code>\n"
            f"Подавлено flood cooldown: <code>{suppressed_chat_cooldown}</code>\n"
            f"Подавлено как эхо исходящих: <code>{suppressed_recent_outgoing}</code>\n"
            f"Подавлено фильтрами: <code>{suppressed_filtered}</code>"
        )
        return text

    def run_health_ping_forever(self) -> None:
        while True:
            time.sleep(self._health_ping_interval_seconds)
            status = self._build_status_text()
            self._send_text_to_admin(f"💓 Пинг здоровья\n\n{status}")

    def run_polling_forever(self) -> None:
        while True:
            try:
                self.bot.infinity_polling(timeout=30, long_polling_timeout=30)
            except Exception:
                logging.exception("Ошибка polling Telegram. Перезапуск через 3 секунды.")
                time.sleep(3)
