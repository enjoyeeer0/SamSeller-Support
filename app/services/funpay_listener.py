import logging
import time

from FunPayAPI import Account, Runner, events

from app.services.telegram_bridge import TelegramBridge


class FunPayListener:
    def __init__(self, golden_key: str, tg_bridge: TelegramBridge) -> None:
        self.account = Account(golden_key).get()
        self.runner = Runner(self.account)
        self.tg = tg_bridge

    def send_message(self, chat_id: int | str, text: str, chat_name: str | None):
        try:
            return self.account.send_message(chat_id=chat_id, text=text, chat_name=chat_name)
        except Exception:
            logging.exception("Failed to send message to FunPay chat %s", chat_id)
            return None

    def run_forever(self) -> None:
        while True:
            try:
                for event in self.runner.listen(requests_delay=3, ignore_exceptions=True):
                    if event.type is not events.EventTypes.NEW_MESSAGE:
                        continue
                    if not isinstance(event, events.NewMessageEvent):
                        continue
                    self._handle_new_message(event)
            except Exception:
                logging.exception("FunPay listener crashed. Re-initializing in 3 seconds.")
                time.sleep(3)
                self.account.get(update_phpsessid=True)
                self.runner = Runner(self.account)

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
