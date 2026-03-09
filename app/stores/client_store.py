import threading
from datetime import datetime

from app.models import ClientContext


class ClientStore:
    """Tracks known clients and assigns a short stable code per chat."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self._by_chat_id: dict[int | str, ClientContext] = {}
        self._by_code: dict[str, int | str] = {}

    def upsert(
        self,
        chat_id: int | str,
        chat_name: str | None,
        author: str | None,
        author_id: int | None,
        message_text: str | None,
    ) -> ClientContext:
        with self._lock:
            existing = self._by_chat_id.get(chat_id)
            if existing:
                existing.chat_name = chat_name
                existing.author = author
                existing.author_id = author_id
                existing.last_message_text = message_text
                existing.last_message_at = datetime.now()
                return existing

            self._counter += 1
            code = f"C{self._counter:03d}"
            ctx = ClientContext(
                code=code,
                chat_id=chat_id,
                chat_name=chat_name,
                author=author,
                author_id=author_id,
                last_message_at=datetime.now(),
                last_message_text=message_text,
            )
            self._by_chat_id[chat_id] = ctx
            self._by_code[code] = chat_id
            return ctx

    def get_by_code(self, code: str) -> ClientContext | None:
        with self._lock:
            chat_id = self._by_code.get(code.upper())
            if chat_id is None:
                return None
            return self._by_chat_id.get(chat_id)

    def list_clients(self, limit: int = 20) -> list[ClientContext]:
        with self._lock:
            clients = sorted(self._by_chat_id.values(), key=lambda c: c.last_message_at, reverse=True)
            return clients[:limit]
