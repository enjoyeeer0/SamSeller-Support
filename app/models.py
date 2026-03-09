from dataclasses import dataclass
from datetime import datetime


@dataclass
class PendingReply:
    chat_id: int | str
    chat_name: str | None
    client_code: str


@dataclass
class ClientContext:
    code: str
    chat_id: int | str
    chat_name: str | None
    author: str | None
    author_id: int | None
    last_message_at: datetime
    last_message_text: str | None
