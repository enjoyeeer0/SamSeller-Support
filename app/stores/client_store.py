import threading
from datetime import datetime, timedelta

from app.models import ClientContext, ClientMessage


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
            self._cleanup_stale_locked()
            existing = self._by_chat_id.get(chat_id)
            if existing:
                existing.chat_name = chat_name
                existing.author = author
                existing.author_id = author_id
                existing.last_message_text = message_text
                existing.last_message_at = datetime.now()
                existing.needs_reply = True
                self._append_history(existing, "in", message_text)
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
                needs_reply=True,
            )
            self._append_history(ctx, "in", message_text)
            self._by_chat_id[chat_id] = ctx
            self._by_code[code] = chat_id
            return ctx

    def get_by_chat_id(self, chat_id: int | str) -> ClientContext | None:
        with self._lock:
            return self._by_chat_id.get(chat_id)

    def add_outgoing(self, chat_id: int | str, message_text: str | None, chat_name: str | None = None) -> ClientContext:
        with self._lock:
            self._cleanup_stale_locked()
            existing = self._by_chat_id.get(chat_id)
            if existing is None:
                self._counter += 1
                code = f"C{self._counter:03d}"
                existing = ClientContext(
                    code=code,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    author=chat_name,
                    author_id=None,
                    last_message_at=datetime.now(),
                    last_message_text=message_text,
                )
                self._by_chat_id[chat_id] = existing
                self._by_code[code] = chat_id
            else:
                if chat_name:
                    existing.chat_name = chat_name
                existing.last_message_text = message_text
                existing.last_message_at = datetime.now()
            existing.needs_reply = False

            self._append_history(existing, "out", message_text)
            return existing

    def list_history(self, chat_id: int | str, limit: int = 10, within_hours: int | None = None) -> list[ClientMessage]:
        with self._lock:
            ctx = self._by_chat_id.get(chat_id)
            if not ctx:
                return []
            if limit <= 0:
                return []
            items = ctx.history
            if within_hours is not None and within_hours > 0:
                cutoff = datetime.now() - timedelta(hours=within_hours)
                items = [i for i in items if i.created_at >= cutoff]
            return list(items[-limit:])

    def get_by_code(self, code: str) -> ClientContext | None:
        with self._lock:
            chat_id = self._by_code.get(code.upper())
            if chat_id is None:
                return None
            return self._by_chat_id.get(chat_id)

    def list_clients(self, limit: int = 20, within_hours: int | None = None) -> list[ClientContext]:
        with self._lock:
            clients = sorted(self._by_chat_id.values(), key=lambda c: c.last_message_at, reverse=True)
            if within_hours is not None and within_hours > 0:
                cutoff = datetime.now() - timedelta(hours=within_hours)
                clients = [c for c in clients if c.last_message_at >= cutoff]
            return clients[:limit]

    def list_unanswered(self, limit: int = 20, within_hours: int | None = None) -> list[ClientContext]:
        with self._lock:
            clients = [c for c in self._by_chat_id.values() if c.needs_reply]
            clients.sort(key=lambda c: c.last_message_at, reverse=True)
            if within_hours is not None and within_hours > 0:
                cutoff = datetime.now() - timedelta(hours=within_hours)
                clients = [c for c in clients if c.last_message_at >= cutoff]
            return clients[:limit]

    def cleanup_stale(self) -> None:
        with self._lock:
            self._cleanup_stale_locked()

    @staticmethod
    def _append_history(ctx: ClientContext, direction: str, message_text: str | None, max_items: int = 100) -> None:
        text = (message_text or "").strip()
        if not text:
            return
        ctx.history.append(ClientMessage(direction=direction, text=text, created_at=datetime.now()))
        if len(ctx.history) > max_items:
            ctx.history[:] = ctx.history[-max_items:]

    def _cleanup_stale_locked(self, max_age_days: int = 120, max_clients: int = 5000) -> None:
        cutoff = datetime.now() - timedelta(days=max_age_days)

        stale_chat_ids = [chat_id for chat_id, ctx in self._by_chat_id.items() if ctx.last_message_at < cutoff]
        for chat_id in stale_chat_ids:
            ctx = self._by_chat_id.pop(chat_id, None)
            if ctx:
                self._by_code.pop(ctx.code, None)

        if len(self._by_chat_id) <= max_clients:
            return

        clients_sorted = sorted(self._by_chat_id.values(), key=lambda c: c.last_message_at, reverse=True)
        keep_ids = {ctx.chat_id for ctx in clients_sorted[:max_clients]}
        drop_ids = [chat_id for chat_id in self._by_chat_id.keys() if chat_id not in keep_ids]
        for chat_id in drop_ids:
            ctx = self._by_chat_id.pop(chat_id, None)
            if ctx:
                self._by_code.pop(ctx.code, None)
