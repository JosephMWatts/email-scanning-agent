# agent.py — the rules-engine runtime, section C.
#
# Classification logic plus the orchestration that drives a scan end to end:
# fetch, read rules, classify, archive the ruled, queue the unruled for review.
# It stays adapter-agnostic (criterion E1): it speaks only the MailSource and
# VaultStore contracts and the MailMessage shape, never a concrete adapter. The
# composition root (run_agent.py) injects the concrete ones.

from dataclasses import dataclass, field
from datetime import datetime

from mail.base import MailMessage, MailSource
from vault.base import DigestRow, VaultStore

# Target length of the per-message one-line summary, in characters.
_SUMMARY_LEN = 160


def summarize(body_text: str) -> str:
    """Collapse whitespace and truncate the body to a one-line summary."""
    collapsed = " ".join(body_text.split())
    if len(collapsed) <= _SUMMARY_LEN:
        return collapsed
    return collapsed[: _SUMMARY_LEN - 1].rstrip() + "…"


@dataclass
class Classification:
    """The three buckets a scan's candidates sort into."""

    to_archive: list[MailMessage] = field(default_factory=list)  # sender has an archive rule
    to_keep: list[MailMessage] = field(default_factory=list)     # sender has a keep rule
    to_queue: list[MailMessage] = field(default_factory=list)    # no rule; queue as proposed-archive


def classify_candidates(
    messages: list[MailMessage], rules: dict[str, str]
) -> Classification:
    """Sort archive candidates by their sender's rule, without I/O.

    Only messages whose ``has_unsubscribe`` is True are candidates; anything
    else is not the agent's to touch and is skipped entirely. Each candidate's
    sender is lowercased before lookup — the rules dict keys are already
    lowercased, so matching is case-insensitive. A ``keep`` rule routes to
    ``to_keep``, an ``archive`` rule to ``to_archive``, and no rule to
    ``to_queue``. Neither the input list nor the messages are mutated; a fresh
    Classification is returned."""
    result = Classification()
    for message in messages:
        if not message.has_unsubscribe:
            continue
        rule = rules.get(message.sender.lower())
        if rule == "keep":
            result.to_keep.append(message)
        elif rule == "archive":
            result.to_archive.append(message)
        else:
            result.to_queue.append(message)
    return result


def run(source: MailSource, vault: VaultStore, scope: dict) -> dict:
    """Drive one full scan: fetch the scope, read sender rules, classify, archive
    the ruled candidates, and write the unruled ones to the review queue.

    ``source`` and ``vault`` are the abstract contracts, injected by the
    composition root (criterion E1). Both are connected here and disconnected in
    a finally, so a failure mid-run still releases handles. An archive call that
    raises propagates — deliberate fail-fast for V1. Returns the run's counts and
    the review-queue path."""
    source.connect()
    vault.connect()
    try:
        messages = source.fetch(scope)
        rules = vault.read_sender_rules()
        classified = classify_candidates(messages, rules)

        for message in classified.to_archive:
            source.archive(message.message_id)

        rows = [
            DigestRow(message=msg, summary=summarize(msg.body_text))
            for msg in classified.to_queue
        ]
        run_meta = {"timestamp": datetime.now()}
        queue_path = vault.write_review_queue(rows, run_meta)
    finally:
        vault.disconnect()
        source.disconnect()

    return {
        "fetched": len(messages),
        "archived": len(classified.to_archive),
        "kept": len(classified.to_keep),
        "queued": len(classified.to_queue),
        "review_queue_path": queue_path,
    }
