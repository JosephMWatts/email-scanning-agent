# vault/base.py — the vault-write contract. No joseph_vault specifics here.

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from mail.base import MailMessage


@dataclass
class DigestRow:
    message: MailMessage
    summary: str


@dataclass
class ReviewQueueRow:
    sender: str            # full address, e.g. news@stripe.com
    subject: str
    date: str              # ISO 8601 date
    summary: str
    your_call: str         # "" accepts the proposed action; "keep" overrides it


@dataclass
class ReviewQueue:
    is_ready: bool                 # top-of-file "Reviewed, process this queue" box is checked
    rows: list[ReviewQueueRow]


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
    def append_rule(self, sender: str, rule: str, source: str) -> None:
        """Append one exact-match sender rule to the sender-rules store,
        creating the store on first call. The "added" date is stamped by the
        implementation, not passed in. rule must be in the V1 two-action
        vocabulary {"archive", "keep"}; anything else raises ValueError. This
        is the governance-write half of Flow B, criterion C3."""

    @abstractmethod
    def read_sender_rules(self) -> dict[str, str]:
        """Return a mapping of full sender address to rule, where the rule is
        "archive" or "keep". This is the governance-read half of the
        bidirectional vault connection, criterion E3."""

    @abstractmethod
    def read_review_queue(self) -> ReviewQueue:
        """Read back the hand-reviewed queue written by write_review_queue.
        is_ready reflects the top-of-file "- [ ] Reviewed, process this queue"
        checkbox: True only when checked. Each row carries the human's verdict
        in your_call — "" means accept the proposed action, "keep" overrides it.
        This is the action-read half of the bidirectional vault connection,
        criterion C3."""

    @abstractmethod
    def rotate_review_queue(self) -> Optional[str]:
        """Retire the canonical review queue off the active path so the next
        Flow A run starts from a clean slate without clobbering the operator's
        signed-off queue. Return the new identifier of the rotated file on
        success, or None if there is no queue to rotate (clean no-op, not an
        error). Raises on rotation failure — target collision or I/O error —
        which must not be swallowed. This is the queue-retire half of the
        lifecycle, called by Flow B after successful processing."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release handles. A filesystem store has nothing to close; kept for symmetry."""
