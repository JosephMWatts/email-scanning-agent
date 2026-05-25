# agent.py — the rules-engine runtime, section C.
#
# Pure classification logic, no I/O. Given the candidates a scan turned up and
# the sender rules read from the vault, it sorts each candidate into archive,
# keep, or review-queue. It stays adapter-agnostic (criterion E1): it knows the
# MailMessage shape and nothing about how mail or rules are fetched or written.

from dataclasses import dataclass, field

from mail.base import MailMessage


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
