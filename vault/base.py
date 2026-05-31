# vault/base.py — the vault-write contract. No joseph_vault specifics here.

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Protocol
from mail.base import MailMessage


@dataclass
class DigestRow:
    message: MailMessage
    summary: str


@dataclass
class CalendarProposalRow:
    """One propose-only calendar event awaiting operator approval. Carries
    enough provenance for the operator to decide without opening the email."""

    subject: str           # proposed event title
    proposed_time: str     # human-readable start–end (or all-day)
    confidence: float      # 0.0–1.0, the extractor's meeting-intent confidence
    conflict: str          # conflicting event titles, "; "-joined; "" if clear
    source_sender: str     # the email that triggered the proposal
    source_subject: str    # that email's subject line
    source_email_id: str   # that email's opaque message id; the dedup key


class ProposalsSink(Protocol):
    """Write-only seam for the calendar agent's propose-only output. Kept
    separate from VaultStore so the email-scan contract stays free of calendar
    concerns; MarkdownVault structurally satisfies both (criterion E1)."""

    def write_calendar_proposals(
        self,
        rows: list["CalendarProposalRow"],
        run_meta: dict,
        proposed_email_ids: set[str],
    ) -> str:
        """Write the propose-only calendar events as one hand-reviewable
        markdown file and return the absolute path written. run_meta carries
        run-level metadata only — timestamp, scope, event count.

        proposed_email_ids is the file-level dedup set persisted in frontmatter:
        every source email id this file should be treated as having already
        proposed. The runtime owns the self-pruning union it passes here; the
        sink only records it verbatim and reads it back via
        read_proposed_email_ids."""
        ...

    def read_proposed_email_ids(self) -> set[str]:
        """Return the set of source email ids already proposed in a prior run,
        parsed from the proposals file's frontmatter. An absent file is a valid
        empty state and yields an empty set — strictly read-only, never created
        here. This is the dedup-read half of the propose-only seam."""
        ...


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
