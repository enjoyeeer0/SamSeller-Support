import logging
import json
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
        logging.info("FunPayListener инициализирован. account_id=%s", getattr(self.account, "id", None))

    @staticmethod
    def _is_last_chat_message_from_me(chat: Any, my_account_id: int) -> bool:
        possible_author_ids = [
            getattr(chat, "last_message_author_id", None),
            getattr(chat, "last_message_sender_id", None),
            getattr(chat, "author_id", None),
        ]
        for value in possible_author_ids:
            try:
                if value is not None and int(value) == my_account_id:
                    return True
            except Exception:
                continue
        return False

    def _get_latest_chat_message(self, chat_id: int | str, chat_name: str | None) -> Any | None:
        """Returns the latest message object for the chat or None if unavailable."""
        histories = []
        try:
            histories.append(self.account.get_chat_history(chat_id=chat_id, interlocutor_username=chat_name))
        except Exception:
            pass
        try:
            histories.append(self.account.get_chat_history(chat_id=chat_id))
        except Exception:
            pass

        for history in histories:
            if not history:
                continue
            # Some API calls may return trailing None entries.
            for item in reversed(history):
                if item is not None:
                    return item
        return None

    def _send_message_runner(self, chat_id: int | str, text: str) -> bool:
        safe_text = (text or "").strip()
        if not safe_text:
            return False

        payload = {
            "objects": json.dumps(
                [
                    {
                        "type": "chat_node",
                        "id": int(chat_id),
                        "tag": "0",
                        "data": {"node": int(chat_id), "last_message": -1, "content": ""},
                    }
                ],
                ensure_ascii=False,
            ),
            "request": json.dumps(
                {"action": "chat_message", "data": {"node": int(chat_id), "content": safe_text}},
                ensure_ascii=False,
            ),
            "csrf_token": self.account.csrf_token,
        }

        self.account.method(
            "post",
            "runner/",
            headers={
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "x-requested-with": "XMLHttpRequest",
            },
            payload=payload,
            raise_not_200=True,
        )
        return True

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

        author_id = getattr(last_message, "author_id", None)
        if author_id == self.account.id:
            return True

        # Fallback: some edge cases do not expose author_id correctly,
        # but message id advanced and text already matches sent content.
        return baseline_last_message_id is not None and isinstance(last_id, int) and last_id > baseline_last_message_id

    def send_message(self, chat_id: int | str, text: str, chat_name: str | None):
        logging.info("Отправка в FunPay chat_id=%s text_len=%s", chat_id, len((text or "").strip()))
        baseline_last_message_id = None
        baseline_message = self._get_latest_chat_message(chat_id, chat_name)
        if baseline_message is not None:
            baseline_last_message_id = getattr(baseline_message, "id", None)

        # Primary path: low-level runner send is significantly more stable for some chats.
        try:
            return self._send_message_runner(chat_id=chat_id, text=text)
        except Exception as exc:
            logging.warning("Runner send failed for chat_id=%s: %s. Fallback to account.send_message()", chat_id, exc)

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
        logging.info("Запуск цикла FunPay listener")
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
            logging.info("Пропуск NEW_MESSAGE от себя/системы chat_id=%s", getattr(msg, "chat_id", None))
            return
        if getattr(msg, "by_bot", False):
            logging.info("Пропуск NEW_MESSAGE by_bot chat_id=%s", getattr(msg, "chat_id", None))
            return
        if self.tg.was_recent_outgoing_message(getattr(msg, "chat_id", 0), getattr(msg, "text", "") or ""):
            logging.info("Пропуск NEW_MESSAGE как эхо исходящего chat_id=%s", getattr(msg, "chat_id", None))
            return

        logging.info("NEW_MESSAGE из FunPay chat_id=%s", getattr(msg, "chat_id", None))

        self.tg.send_funpay_notification(
            chat_id=msg.chat_id,
            chat_name=msg.chat_name,
            author=msg.author,
            author_id=msg.author_id,
            text=msg.text,
        )

    def _handle_last_message_changed(self, event: events.LastChatMessageChangedEvent) -> None:
        chat = event.chat
        chat_id = int(getattr(chat, "id", 0) or 0)
        text = (getattr(chat, "last_message_text", "") or "").strip()
        if not chat_id or not text:
            return

        # Deduplicate identical last messages for the same chat.
        if self._last_seen_text.get(chat_id) == text:
            logging.info("Пропуск LAST_CHAT_MESSAGE_CHANGED дубликат chat_id=%s", chat_id)
            return
        self._last_seen_text[chat_id] = text
        if len(self._last_seen_text) > 5000:
            # Keep map bounded for long-running processes.
            for _ in range(500):
                self._last_seen_text.pop(next(iter(self._last_seen_text)), None)

        if self._is_last_chat_message_from_me(chat, int(getattr(self.account, "id", 0) or 0)):
            logging.info("Пропуск LAST_CHAT_MESSAGE_CHANGED от своего аккаунта chat_id=%s", chat_id)
            return
        if self.tg.was_recent_outgoing_message(chat_id, text):
            logging.info("Пропуск LAST_CHAT_MESSAGE_CHANGED как эхо исходящего chat_id=%s", chat_id)
            return

        # Ignore messages likely sent by our own bot.
        if getattr(chat, "last_by_bot", False) or getattr(chat, "last_by_vertex", False):
            logging.info("Пропуск LAST_CHAT_MESSAGE_CHANGED by_bot/by_vertex chat_id=%s", chat_id)
            return

        logging.info("LAST_CHAT_MESSAGE_CHANGED из FunPay chat_id=%s", chat_id)

        chat_name = (
            str(getattr(chat, "name", "") or "").strip()
            or str(getattr(chat, "title", "") or "").strip()
            or str(getattr(chat, "username", "") or "").strip()
        )

        self.tg.send_funpay_notification(
            chat_id=chat_id,
            chat_name=chat_name,
            author=chat_name,
            author_id=None,
            text=text,
        )
