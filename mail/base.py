# mail/base.py — the mail-read contract. No backend logic here.

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class MailMessage:
    message_id: str        # opaque handle; the adapter knows how to resolve it
    sender: str            # full address, e.g. news@stripe.com
    subject: str
    date: datetime
    has_unsubscribe: bool   # List-Unsubscribe header present, yes or no
    body_text: str         # plain-text body, raw; the runtime decides on summary


class MailSource(ABC):

    @abstractmethod
    def connect(self) -> None:
        """Open and authenticate. Credentials load from the secret file."""

    @abstractmethod
    def fetch(self, scope: dict) -> list[MailMessage]:
        """Return messages matching scope, e.g. {'since_days': 7, 'folder': 'INBOX'}."""

    @abstractmethod
    def archive(self, message_id: str) -> None:
        """Remove from inbox. Non-destructive. The message survives in All Mail."""

    @abstractmethod
    def apply_label(self, message_id: str, label: str) -> None:
        """Tag a message, e.g. 'proposed-archive'."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection cleanly."""
