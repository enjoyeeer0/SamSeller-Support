import threading

from app.models import PendingReply


class ReplyStore:
    """Stores mapping Telegram notification message -> FunPay chat."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._storage: dict[int, PendingReply] = {}

    def set(self, telegram_message_id: int, value: PendingReply) -> None:
        with self._lock:
            self._storage[telegram_message_id] = value

    def get(self, telegram_message_id: int) -> PendingReply | None:
        with self._lock:
            return self._storage.get(telegram_message_id)
