import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from telebot import TeleBot
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.models import PendingReply
from app.stores.client_store import ClientStore
from app.stores.reply_store import ReplyStore
from app.utils.text import compact, escape_html


@dataclass
class AdminSession:
    active_dialog_chat_id: int | str | None = None
    active_dialog_chat_name: str | None = None
    active_dialog_client_code: str | None = None
    ui_message_ids: list[int] = field(default_factory=list)


class TelegramBridge:
    def __init__(self, token: str, admin_chat_ids: int | list[int] | tuple[int, ...]) -> None:
        self.bot = TeleBot(token, parse_mode="HTML")
        self.admin_chat_ids = self._normalize_admin_ids(admin_chat_ids)
        self.admin_chat_id = self.admin_chat_ids[0]
        self.reply_store = ReplyStore()
        self.client_store = ClientStore()
        self._send_to_funpay: Callable[[int | str, str, str | None], object] | None = None
        self._lock = threading.Lock()
        self._admin_sessions: dict[int, AdminSession] = {
            admin_id: AdminSession() for admin_id in self.admin_chat_ids
        }
        self._dedupe: dict[tuple[str, str], float] = {}
        self._last_forward_ts_by_chat: dict[str, float] = {}
        self._recent_outgoing: dict[str, tuple[float, str]] = {}
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

        @self.bot.callback_query_handler(func=lambda c: True)
        def on_callback(callback):
            self._handle_callback(callback)

        logging.info("TelegramBridge инициализирован для admin_chat_ids=%s", self.admin_chat_ids)

    @staticmethod
    def _normalize_admin_ids(raw_ids: int | list[int] | tuple[int, ...]) -> tuple[int, ...]:
        if isinstance(raw_ids, int):
            items = [raw_ids]
        else:
            items = [int(x) for x in raw_ids]

        cleaned: list[int] = []
        for value in items:
            if value <= 0:
                continue
            if value not in cleaned:
                cleaned.append(value)

        if not cleaned:
            raise ValueError("At least one valid admin chat id is required")
        if len(cleaned) > 3:
            cleaned = cleaned[:3]
        return tuple(cleaned)

    def _is_admin_chat(self, chat_id: int) -> bool:
        return int(chat_id) in self.admin_chat_ids

    def _get_admin_session(self, admin_chat_id: int) -> AdminSession:
        admin_chat_id = int(admin_chat_id)
        with self._lock:
            session = self._admin_sessions.get(admin_chat_id)
            if session is None:
                session = AdminSession()
                self._admin_sessions[admin_chat_id] = session
            return session

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
            logging.info("Пропущено эхо исходящего сообщения chat_id=%s", chat_id)
            return

        now_ts = time.time()
        chat_key = self._chat_key(chat_id)
        key = (chat_key, safe_text_plain)
        with self._lock:
            if now_ts - self._dedupe.get(key, 0.0) < 30.0:
                self._suppressed_duplicate_count += 1
                logging.info("Пропущен дубликат входящего сообщения chat_id=%s", chat_id)
                return
            if now_ts - self._last_forward_ts_by_chat.get(chat_key, 0.0) < 2.5:
                self._suppressed_chat_cooldown_count += 1
                logging.info("Пропущено сообщение по cooldown chat_id=%s", chat_id)
                return
            self._dedupe[key] = now_ts
            self._last_forward_ts_by_chat[chat_key] = now_ts

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
        safe_chat_name = escape_html(chat_name, fallback="-")
        chat_url = self._build_funpay_chat_url(chat_id)

        payload = (
            "<b>Новое сообщение FunPay</b>\n"
            f"Код клиента: <code>{ctx.code}</code>\n"
            f"Ник: <b>{safe_author}</b>\n"
            f"Заказ/чат: <code>{safe_chat_name}</code>\n"
            f"Время: <code>{created_at}</code>\n"
            f"Chat ID: <code>{chat_id}</code>\n"
            f"Ссылка: <a href=\"{chat_url}\">Открыть чат</a>\n\n"
            "<b>Текст:</b>\n"
            f"<code>{safe_text}</code>"
        )

        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton(text=f"Диалог {ctx.code}", callback_data=f"dialog:{ctx.code}"),
            InlineKeyboardButton(text="История 24ч", callback_data=f"history24:{ctx.code}"),
        )

        for admin_chat_id in self.admin_chat_ids:
            try:
                sent = self.bot.send_message(admin_chat_id, payload, reply_markup=markup)
                self.reply_store.set(
                    sent.message_id,
                    PendingReply(chat_id=chat_id, chat_name=chat_name, client_code=ctx.code),
                )
            except Exception:
                logging.exception(
                    "Не удалось отправить входящее уведомление в Telegram admin чат id=%s",
                    admin_chat_id,
                )

        with self._lock:
            self._forwarded_count += 1
        logging.info("Входящее сообщение отправлено в Telegram chat_id=%s code=%s", chat_id, ctx.code)

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
        admin_chat_id = int(message.chat.id)
        if not self._is_admin_chat(admin_chat_id):
            logging.warning("Игнор входящего Telegram message из не-админ чата id=%s", message.chat.id)
            return

        text = (message.text or "").strip()
        if not text:
            return

        if text.startswith("/"):
            self._cleanup_ui_messages(admin_chat_id)
            self._delete_user_command_message(message)

        if text in {"/start", "/help"}:
            logging.info("Команда /help от admin")
            self._send_help(message)
            return
        if text == "/status":
            logging.info("Команда /status от admin")
            self._send_status(message)
            return
        if text == "/clients":
            logging.info("Команда /clients от admin")
            self._send_clients(message)
            return
        if text == "/unanswered":
            logging.info("Команда /unanswered от admin")
            self._send_unanswered(message)
            return
        if text.lower().startswith("/history"):
            logging.info("Команда /history от admin: %s", text)
            self._handle_history(message, text)
            return
        if text.lower().startswith("/dialog"):
            logging.info("Команда /dialog от admin: %s", text)
            self._handle_dialog(message, text)
            return
        if text.lower().startswith("/to "):
            logging.info("Команда /to от admin")
            self._handle_send_to_code(message, text)
            return

        target_chat_id: int | str | None = None
        target_chat_name: str | None = None
        reply_text = text

        reply_to = message.reply_to_message
        if reply_to:
            link = self.reply_store.get(reply_to.message_id)
            if link:
                target_chat_id = link.chat_id
                target_chat_name = link.chat_name

        if target_chat_id is None:
            session = self._get_admin_session(admin_chat_id)
            if session.active_dialog_chat_id is not None:
                target_chat_id = session.active_dialog_chat_id
                target_chat_name = session.active_dialog_chat_name
                reply_text = text
            else:
                self._reply_ui(
                    message,
                    "Выберите клиента: /clients -> /dialog C001, или ответьте реплаем на уведомление клиента.",
                )
                return

        if target_chat_id is None:
            self._reply_ui(message, "Некорректные данные для ответа.")
            return
        if not reply_text:
            self._reply_ui(message, "Текст сообщения не может быть пустым.")
            return

        self._send_to_funpay_message(
            message,
            target_chat_id,
            target_chat_name,
            client_code="-",
            override_text=reply_text,
            admin_chat_id=admin_chat_id,
        )

    def _handle_send_to_code(self, message, full_text: str) -> None:
        parts = full_text.split(maxsplit=2)
        if len(parts) < 3:
            self._reply_ui(message, "Формат: /to C001 ваш_текст")
            return

        code = parts[1].upper()
        user_text = parts[2].strip()
        if not user_text:
            self._reply_ui(message, "Текст сообщения не может быть пустым.")
            return

        ctx = self.client_store.get_by_code(code)
        if not ctx:
            self._reply_ui(message, f"Код клиента {code} не найден. Используйте /clients.")
            return

        self._send_to_funpay_message(message, ctx.chat_id, ctx.chat_name, ctx.code, user_text)

    def _handle_dialog(self, message, full_text: str) -> None:
        admin_chat_id = int(message.chat.id)
        session = self._get_admin_session(admin_chat_id)
        parts = full_text.split(maxsplit=1)
        if len(parts) == 1:
            if session.active_dialog_chat_id is None:
                self._reply_ui(message, "Диалог не выбран. Формат: /dialog C001 или /dialog off")
                return
            active_code = escape_html(session.active_dialog_client_code, fallback="-")
            active_chat_id = escape_html(str(session.active_dialog_chat_id), fallback="-")
            self._reply_ui(message, f"Активный диалог: <code>{active_code}</code> (chat_id: <code>{active_chat_id}</code>)")
            return

        arg = parts[1].strip()
        if arg.lower() in {"off", "none", "stop", "clear"}:
            session.active_dialog_chat_id = None
            session.active_dialog_chat_name = None
            session.active_dialog_client_code = None
            self._reply_ui(message, "Режим диалога выключен.")
            logging.info("Режим диалога выключен admin_chat_id=%s", admin_chat_id)
            return

        code = arg.upper()
        ctx = self.client_store.get_by_code(code)
        if not ctx:
            self._reply_ui(message, f"Код клиента {code} не найден. Используйте /clients.")
            return

        session.active_dialog_chat_id = ctx.chat_id
        session.active_dialog_chat_name = ctx.chat_name
        session.active_dialog_client_code = ctx.code
        self._reply_ui(
            message,
            f"Диалог включен: <code>{ctx.code}</code>. Теперь можно писать без /to и без реплая.\n"
            "Отключить: <code>/dialog off</code>",
        )
        logging.info(
            "Режим диалога включен admin_chat_id=%s code=%s chat_id=%s",
            admin_chat_id,
            ctx.code,
            ctx.chat_id,
        )

    def _handle_history(self, message, full_text: str) -> None:
        parts = full_text.split()
        if len(parts) < 2:
            self._reply_ui(message, "Формат: /history C001 [N]")
            return

        code = parts[1].upper()
        limit = 10
        if len(parts) >= 3:
            try:
                limit = int(parts[2])
            except Exception:
                self._reply_ui(message, "N должен быть числом.")
                return
        limit = max(1, min(limit, 50))

        self._send_history_for_code(message, code=code, limit=limit, within_hours=24)

    def _send_history_for_code(self, message, code: str, limit: int, within_hours: int) -> None:
        ctx = self.client_store.get_by_code(code)
        if not ctx:
            self._reply_ui(message, f"Код клиента {code} не найден. Используйте /clients.")
            return

        history = self.client_store.list_history(ctx.chat_id, limit=limit, within_hours=within_hours)
        if not history:
            self._reply_ui(message, f"История для {code} за последние {within_hours}ч пустая.")
            return

        lines = [
            f"<b>История {escape_html(code)} за {within_hours}ч</b> | Chat ID: <code>{escape_html(str(ctx.chat_id))}</code>"
        ]
        for item in history:
            ts = item.created_at.strftime("%d.%m %H:%M:%S")
            direction = "IN" if item.direction == "in" else "OUT"
            preview = escape_html(compact(item.text, max_len=140), fallback="-")
            lines.append(f"<code>{ts}</code> <b>{direction}</b> | <code>{preview}</code>")

        self._reply_ui(message, "\n".join(lines))

    def _send_to_funpay_message(
        self,
        message,
        chat_id: int | str,
        chat_name: str | None,
        client_code: str,
        override_text: str | None = None,
        admin_chat_id: int | None = None,
    ) -> None:
        if admin_chat_id is None:
            admin_chat_id = int(getattr(getattr(message, "chat", None), "id", self.admin_chat_id))

        if self._send_to_funpay is None:
            self._reply_ui(message, "Модуль отправки в FunPay не инициализирован.")
            return

        text = (override_text if override_text is not None else (message.text or "")).strip()
        if not text:
            self._reply_ui(message, "Нельзя отправить пустое сообщение.")
            return

        response = self._send_to_funpay(chat_id, text, chat_name)
        if response:
            self.note_outgoing_message(chat_id, text)
            self.client_store.add_outgoing(chat_id=chat_id, message_text=text, chat_name=chat_name)
            ctx = self.client_store.get_by_chat_id(chat_id)
            if ctx:
                session = self._get_admin_session(admin_chat_id)
                session.active_dialog_chat_id = ctx.chat_id
                session.active_dialog_chat_name = ctx.chat_name
                session.active_dialog_client_code = ctx.code
            client_label = ctx.code if ctx else "клиент"
            self._reply_ui(message, f"✅ Ваш ответ этому клиенту успешно отправлен ({client_label}).")
            logging.info("Сообщение отправлено в FunPay chat_id=%s code=%s", chat_id, client_label)
        else:
            self._reply_ui(message, "❌ Ошибка доставки")
            logging.error("Ошибка отправки в FunPay chat_id=%s", chat_id)

    def note_outgoing_message(self, chat_id: int | str, text: str) -> None:
        with self._lock:
            self._recent_outgoing[self._chat_key(chat_id)] = (time.time(), (text or "").strip())

    def was_recent_outgoing_message(self, chat_id: int | str, text: str, window_seconds: float = 20.0) -> bool:
        with self._lock:
            cached = self._recent_outgoing.get(self._chat_key(chat_id))
        if not cached:
            return False
        ts, cached_text = cached
        if time.time() - ts > window_seconds:
            return False
        return cached_text == (text or "").strip()

    @staticmethod
    def _chat_key(chat_id: int | str) -> str:
        return str(chat_id).strip()

    def _handle_callback(self, callback) -> None:
        try:
            admin_chat_id = int(callback.message.chat.id)
            if not self._is_admin_chat(admin_chat_id):
                self.bot.answer_callback_query(callback.id, "Недостаточно прав")
                return

            data = (callback.data or "").strip()
            if data.startswith("dialog:"):
                code = data.split(":", 1)[1].upper()
                ctx = self.client_store.get_by_code(code)
                if not ctx:
                    self.bot.answer_callback_query(callback.id, "Клиент не найден")
                    return
                session = self._get_admin_session(admin_chat_id)
                session.active_dialog_chat_id = ctx.chat_id
                session.active_dialog_chat_name = ctx.chat_name
                session.active_dialog_client_code = ctx.code
                self.bot.answer_callback_query(callback.id, f"Диалог {ctx.code} включен")
                self._reply_ui(
                    callback.message,
                    f"Диалог включен: <code>{ctx.code}</code>. Можно писать без команд.",
                )
                return

            if data.startswith("history24:"):
                code = data.split(":", 1)[1].upper()
                self.bot.answer_callback_query(callback.id)
                self._send_history_for_code(callback.message, code, limit=20, within_hours=24)
                return

            self.bot.answer_callback_query(callback.id)
        except Exception:
            logging.exception("Ошибка обработки callback")

    def _main_keyboard(self) -> ReplyKeyboardMarkup:
        return self._main_keyboard_for_admin(self.admin_chat_id)

    def _main_keyboard_for_admin(self, admin_chat_id: int) -> ReplyKeyboardMarkup:
        session = self._get_admin_session(admin_chat_id)
        keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.row(KeyboardButton("/clients"), KeyboardButton("/status"))
        keyboard.row(KeyboardButton("/unanswered"), KeyboardButton("/help"))
        if session.active_dialog_chat_id is not None:
            keyboard.row(KeyboardButton("/dialog off"))
        return keyboard

    def _delete_user_command_message(self, message) -> None:
        try:
            self.bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            # Not critical: may fail due to Telegram restrictions.
            pass

    def _cleanup_ui_messages(self, admin_chat_id: int) -> None:
        session = self._get_admin_session(admin_chat_id)
        if not session.ui_message_ids:
            return
        stale_ids = list(session.ui_message_ids)
        session.ui_message_ids.clear()
        for msg_id in stale_ids:
            try:
                self.bot.delete_message(admin_chat_id, msg_id)
            except Exception:
                continue

    def _track_ui_message(self, admin_chat_id: int, message_id: int | None) -> None:
        if not message_id:
            return
        session = self._get_admin_session(admin_chat_id)
        session.ui_message_ids.append(int(message_id))
        if len(session.ui_message_ids) > 40:
            session.ui_message_ids = session.ui_message_ids[-40:]

    def _reply_ui(self, message, text: str) -> None:
        reply_to_message_id = getattr(message, "message_id", None)
        chat_id = getattr(getattr(message, "chat", None), "id", self.admin_chat_id)
        admin_chat_id = int(chat_id)

        try:
            # Keep reply threading when possible, but do not fail if the source
            # message is already deleted or unavailable.
            sent = self.bot.send_message(
                chat_id,
                text,
                reply_markup=self._main_keyboard_for_admin(admin_chat_id),
                reply_to_message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )
        except ApiTelegramException as exc:
            if "message to be replied not found" in str(exc).lower():
                logging.warning(
                    "_reply_ui fallback without reply: replied message not found (chat_id=%s)",
                    chat_id,
                )
                sent = self.bot.send_message(
                    chat_id,
                    text,
                    reply_markup=self._main_keyboard_for_admin(admin_chat_id),
                )
            else:
                raise

        self._track_ui_message(admin_chat_id, getattr(sent, "message_id", None))

    def _send_help(self, message) -> None:
        text = (
            "Команды:\n"
            "<code>/help</code> - помощь\n"
            "<code>/dialog C001</code> - включить режим диалога\n"
            "<code>/dialog off</code> - выключить режим диалога\n"
            "<code>/status</code> - состояние бота\n"
            "<code>/clients</code> - клиенты за последние 24 часа\n"
            "<code>/unanswered</code> - клиенты без ответа\n"
            "<code>/history C001 [N]</code> - история за последние 24 часа\n"
            "<code>/to C001 ваш_текст</code> - отправка по коду клиента\n\n"
            "Также можно отвечать реплаем на уведомление."
        )
        self._reply_ui(message, text)

    @staticmethod
    def _build_funpay_chat_url(chat_id: int | str) -> str:
        return f"https://funpay.com/chat/?node={chat_id}"

    def _send_status(self, message) -> None:
        status_text = self._build_status_text()
        self._reply_ui(message, status_text)

    def _send_clients(self, message) -> None:
        clients = self.client_store.list_clients(limit=20, within_hours=24)
        if not clients:
            self._reply_ui(message, "За последние 24 часа клиентов нет.")
            return

        lines = ["<b>Клиенты за последние 24 часа</b>"]
        for client in clients:
            when = client.last_message_at.strftime("%H:%M:%S")
            author = escape_html(client.author)
            preview = escape_html(compact(client.last_message_text, max_len=50), fallback="-")
            lines.append(
                f"<code>{client.code}</code> | <b>{author}</b> | {when} | <code>{preview}</code>"
            )

        self._reply_ui(message, "\n".join(lines))

    def _send_unanswered(self, message) -> None:
        clients = self.client_store.list_unanswered(limit=30, within_hours=24)
        if not clients:
            self._reply_ui(message, "За последние 24 часа нет клиентов без ответа.")
            return

        lines = ["<b>Клиенты без ответа (24ч)</b>"]
        for client in clients:
            when = client.last_message_at.strftime("%H:%M:%S")
            author = escape_html(client.author)
            preview = escape_html(compact(client.last_message_text, max_len=60), fallback="-")
            lines.append(f"<code>{client.code}</code> | <b>{author}</b> | {when} | <code>{preview}</code>")
        self._reply_ui(message, "\n".join(lines))

    def report_listener_error(self, error_text: str) -> None:
        now = time.time()
        with self._lock:
            self._listener_healthy = False
            self._listener_last_error = error_text.strip() or "Неизвестная ошибка"
            self._listener_last_error_ts = now
        logging.error("Listener error: %s", error_text)

    def report_listener_recovered(self) -> None:
        now = time.time()
        with self._lock:
            was_unhealthy = not self._listener_healthy
            self._listener_healthy = True
            self._listener_last_recovered_ts = now
        if was_unhealthy:
            self._send_text_to_admin("✅ Listener восстановлен после ошибки.")
            logging.info("Listener recovered")

    def _send_text_to_admin(self, text: str, admin_chat_id: int | None = None) -> None:
        targets = [admin_chat_id] if admin_chat_id is not None else list(self.admin_chat_ids)
        for target in targets:
            try:
                self.bot.send_message(int(target), text)
            except Exception:
                logging.exception("Не удалось отправить служебное сообщение в Telegram admin чат id=%s", target)

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
            self.client_store.cleanup_stale()
            status = self._build_status_text()
            self._send_text_to_admin(f"💓 Пинг здоровья\n\n{status}")

    def run_polling_forever(self) -> None:
        while True:
            try:
                self.bot.infinity_polling(timeout=30, long_polling_timeout=30)
            except Exception:
                logging.exception("Ошибка polling Telegram. Перезапуск через 3 секунды.")
                time.sleep(3)
