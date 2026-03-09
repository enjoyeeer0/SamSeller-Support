import logging
import time
from typing import Any

from FunPayAPI import Account, Runner, events

from app.services.telegram_bridge import TelegramBridge


class FunPayListener:
    def __init__(self, golden_key: str, tg_bridge: TelegramBridge) -> None:
        self.account = Account(golden_key, requests_timeout=25).get()
        # Safe mode: avoid internal bulk history fetches that may fail on some chats.
        self.runner = Runner(self.account, disable_message_requests=True)
        self.tg = tg_bridge
        self._last_seen_text: dict[int, str] = {}

    def _get_latest_chat_message(self, chat_id: int | str, chat_name: str | None) -> Any | None:
        """Returns the latest message object for the chat or None if unavailable."""
        try:
            history = self.account.get_chat_history(chat_id=chat_id, interlocutor_username=chat_name)
        except Exception:
            return None
        if not history:
            return None
        return history[-1]

    def _was_last_message_delivered(
        self,
        chat_id: int | str,
        text: str,
        chat_name: str | None,
        baseline_last_message_id: int | None,
    ) -> bool:
        """Checks whether the most recent chat message is our just-sent text."""
        last_message = self._get_latest_chat_message(chat_id, chat_name)
        if last_message is None:
            return False

        last_id = getattr(last_message, "id", None)
        if baseline_last_message_id is not None and isinstance(last_id, int):
            if last_id <= baseline_last_message_id:
                return False

        last_text = (getattr(last_message, "text", "") or "").strip()
        if last_text != (text or "").strip():
            return False

        return getattr(last_message, "author_id", None) == self.account.id

    def send_message(self, chat_id: int | str, text: str, chat_name: str | None):
        baseline_last_message_id = None
        baseline_message = self._get_latest_chat_message(chat_id, chat_name)
        if baseline_message is not None:
            baseline_last_message_id = getattr(baseline_message, "id", None)

        for attempt in range(1, 4):
            try:
                return self.account.send_message(chat_id=chat_id, text=text, chat_name=chat_name)
            except Exception as exc:
                if self._was_last_message_delivered(chat_id, text, chat_name, baseline_last_message_id):
                    logging.warning(
                        "Отправка в чат FunPay %s с ошибкой '%s', но сообщение уже доставлено. Повтор не нужен.",
                        chat_id,
                        exc,
                    )
                    return True
                logging.warning(
                    "Попытка %s/3: не удалось отправить сообщение в чат FunPay %s: %s",
                    attempt,
                    chat_id,
                    exc,
                )
                try:
                    # Refresh session before next attempt.
                    self.account.get(update_phpsessid=True)
                except Exception:
                    pass
                time.sleep(1.0 * attempt)
        logging.error("Не удалось отправить сообщение в чат FunPay %s после 3 попыток", chat_id)
        return None

    def run_forever(self) -> None:
        while True:
            try:
                for event in self.runner.listen(requests_delay=3, ignore_exceptions=True):
                    self.tg.report_listener_recovered()
                    if event.type is events.EventTypes.NEW_MESSAGE and isinstance(event, events.NewMessageEvent):
                        self._handle_new_message(event)
                        continue
                    if event.type is events.EventTypes.LAST_CHAT_MESSAGE_CHANGED and isinstance(
                        event, events.LastChatMessageChangedEvent
                    ):
                        self._handle_last_message_changed(event)
            except Exception:
                error_text = "Ошибка слушателя FunPay. Перезапуск через 3 секунды."
                logging.exception(error_text)
                self.tg.report_listener_error(error_text)
                time.sleep(3)
                self.account.get(update_phpsessid=True)
                self.runner = Runner(self.account, disable_message_requests=True)

    def _handle_new_message(self, event: events.NewMessageEvent) -> None:
        msg = event.message

        # Skip own/bot/system messages. We only want client incoming messages.
        if msg.author_id in (0, self.account.id):
            return
        if getattr(msg, "by_bot", False):
            return

        self.tg.send_funpay_notification(
            chat_id=msg.chat_id,
            chat_name=msg.chat_name,
            author=msg.author,
            author_id=msg.author_id,
            text=msg.text,
        )

    def _handle_last_message_changed(self, event: events.LastChatMessageChangedEvent) -> None:
        chat = event.chat
        text = str(chat).strip()
        if not text:
            return

        # Deduplicate identical last messages for the same chat.
        if self._last_seen_text.get(chat.id) == text:
            return
        self._last_seen_text[chat.id] = text

        # Ignore messages likely sent by our own bot.
        if getattr(chat, "last_by_bot", False) or getattr(chat, "last_by_vertex", False):
            return

        self.tg.send_funpay_notification(
            chat_id=chat.id,
            chat_name=chat.name,
            author=chat.name,
            author_id=None,
            text=text,
        )
