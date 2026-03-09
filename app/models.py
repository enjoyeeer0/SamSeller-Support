from dataclasses import dataclass, field
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
    needs_reply: bool = False
    history: list["ClientMessage"] = field(default_factory=list)


@dataclass
class ClientMessage:
    direction: str
    text: str
    created_at: datetime
