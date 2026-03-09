import threading

from app.models import PendingReply


class ReplyStore:
    """Stores mapping Telegram notification message -> FunPay chat."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._storage: dict[int, PendingReply] = {}
        self._max_entries = 5000

    def set(self, telegram_message_id: int, value: PendingReply) -> None:
        with self._lock:
            self._storage[telegram_message_id] = value
            while len(self._storage) > self._max_entries:
                oldest_key = next(iter(self._storage))
                self._storage.pop(oldest_key, None)

    def get(self, telegram_message_id: int) -> PendingReply | None:
        with self._lock:
            return self._storage.get(telegram_message_id)
