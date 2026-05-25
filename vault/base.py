# vault/base.py — the vault-write contract. No joseph_vault specifics here.

from abc import ABC, abstractmethod
from dataclasses import dataclass
from mail.base import MailMessage


@dataclass
class DigestRow:
    message: MailMessage
    summary: str


class VaultStore(ABC):

    @abstractmethod
    def connect(self) -> None:
        """Verify the destination path exists and is writable. Fail loud if not."""

    @abstractmethod
    def write_scan_digest(self, rows: list[DigestRow], run_meta: dict) -> str:
        """Write the scan's extraction output as one reviewable markdown file.
        Return the path written. Each row bundles a message with its summary;
        run_meta carries only scan-level metadata — timestamp, scope, message
        count — and no per-message data."""

    @abstractmethod
    def write_review_queue(self, rows: list[DigestRow], run_meta: dict) -> str:
        """Write the unruled candidates as a hand-reviewable markdown file and
        return the path written. Each row bundles a message with its summary,
        reusing the existing DigestRow shape. run_meta carries scan-level
        metadata only."""

    @abstractmethod
    def read_sender_rules(self) -> dict[str, str]:
        """Return a mapping of full sender address to rule, where the rule is
        "archive" or "keep". This is the governance-read half of the
        bidirectional vault connection, criterion E3."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release handles. A filesystem store has nothing to close; kept for symmetry."""
