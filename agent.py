# agent.py — the rules-engine runtime, section C.
#
# Classification logic plus the orchestration that drives a scan end to end:
# fetch, read rules, classify, archive the ruled, queue the unruled for review.
# It stays adapter-agnostic (criterion E1): it speaks only the MailSource and
# VaultStore contracts and the MailMessage shape, never a concrete adapter. The
# composition root (run_agent.py) injects the concrete ones.

from dataclasses import dataclass, field
from datetime import datetime
import sys
from typing import Optional, Protocol

from cal.base import CalendarEvent, CalendarWriter
from mail.base import MailMessage, MailSource
from vault.base import CalendarProposalRow, DigestRow, ProposalsSink, VaultStore

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


def run_flow_b(vault: VaultStore) -> dict:
    """Drive Flow B: process the operator-approved review queue into sender
    rules, then retire the queue.

    Reads the queue through the VaultStore seam (criterion E1) — no concrete
    adapter or path knowledge here. ``vault`` is connected here and disconnected
    in a finally, mirroring Flow A, so a failure mid-run still releases handles.
    If the queue is not marked ready, this is a clean no-op: the operator has
    not signed off, so nothing is written or rotated. When ready, each row's
    ``your_call`` verdict becomes a sender rule — blank means "yes, archive it"
    since Phase 1 only queues archive candidates (Lesson 44) — appended via
    ``append_rule``. Unrecognized verdicts are skipped and logged, not guessed.
    After the loop the queue is rotated off the active path so the next scan
    starts clean. Vault errors (read failure, append ValueError/RuntimeError,
    rotate FileExistsError) propagate: they are integrity signals the operator
    should see."""
    vault.connect()
    try:
        queue = vault.read_review_queue()

        if not queue.is_ready:
            print("Flow B: queue not ready, no-op", file=sys.stderr)
            return {
                "is_ready": False,
                "rows_processed": 0,
                "rows_skipped": 0,
                "rules_appended": [],
                "rotated_to": None,
            }

        rules_appended: list[tuple[str, str]] = []
        skipped = 0
        for row in queue.rows:
            verdict = row.your_call.strip().lower()
            if verdict in ("", "archive"):
                # Blank accepts the proposed default; Phase 1 only queues
                # archive candidates, so "yes" means archive (Lesson 44).
                rule = "archive"
            elif verdict == "keep":
                rule = "keep"
            else:
                print(
                    f"Flow B: skipping row for {row.sender}: unrecognized "
                    f"your_call value {row.your_call!r}, expected blank, "
                    f"'archive', or 'keep'",
                    file=sys.stderr,
                )
                skipped += 1
                continue

            vault.append_rule(
                sender=row.sender, rule=rule, source="phase 1 approval"
            )
            rules_appended.append((row.sender, rule))

        rotated_to = vault.rotate_review_queue()

        print(
            f"Flow B: ready=True, processed={len(rules_appended)}, "
            f"skipped={skipped}, rotated_to={rotated_to}",
            file=sys.stderr,
        )

        return {
            "is_ready": True,
            "rows_processed": len(rules_appended),
            "rows_skipped": skipped,
            "rules_appended": rules_appended,
            "rotated_to": rotated_to,
        }
    finally:
        vault.disconnect()


# --- A6: calendar runtime (meeting-intent → calendar write) ------------------
#
# Section A6. Same adapter-agnostic posture as run()/run_flow_b() (criterion
# E1): speaks only the MailSource, CalendarWriter, MeetingExtractor and
# ProposalsSink contracts, never a concrete adapter. The composition root
# (run_calendar_agent.py) injects the concrete ImapSource, EventKitWriter,
# ClaudeMeetingExtractor and MarkdownVault.


@dataclass
class MeetingIntent:
    """The decide-step's structured output for one message. has_meeting False
    means 'no calendar-worthy event here' and the runtime skips the message.
    start/end are timezone-aware when has_meeting is True."""

    has_meeting: bool
    title: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    all_day: bool = False
    location: Optional[str] = None
    url: Optional[str] = None
    attendees: list[str] = field(default_factory=list)
    confidence_score: float = 0.0   # the extractor's own 0.0–1.0 intent confidence


@dataclass
class CalendarOutcome:
    """Per-message result after gating and the write attempt. The post-action
    analogue of Classification's buckets: every fetched message lands in exactly
    one disposition."""

    message: MailMessage
    intent: MeetingIntent
    disposition: str                # "created" | "proposed" | "conflict" | "skipped" | "failed"
    event_id: Optional[str] = None  # set when disposition == "created"
    conflict_titles: list[str] = field(default_factory=list)
    error: Optional[str] = None     # set when disposition == "failed"


class MeetingExtractor(Protocol):
    """The decide-step seam. run_calendar speaks only this contract; the
    concrete ClaudeMeetingExtractor (llm/) is injected by the composition root
    (criterion E1)."""

    model_id: str

    def extract(self, message: MailMessage) -> MeetingIntent:
        """Return the meeting-intent extracted from one message."""
        ...

    def usage(self) -> tuple[int, int]:
        """Cumulative (input_tokens, output_tokens) across the run, for the
        harness v2 token-cost fields."""
        ...


def _event_from_intent(
    intent: MeetingIntent, message: MailMessage, calendar_id: str
) -> CalendarEvent:
    """Build the transport-agnostic CalendarEvent from an extracted intent,
    carrying the provenance back to the triggering email."""
    return CalendarEvent(
        title=intent.title or message.subject,
        start=intent.start,
        end=intent.end,
        calendar_id=calendar_id,
        all_day=intent.all_day,
        location=intent.location,
        url=intent.url,
        attendees=intent.attendees,
        confidence_score=intent.confidence_score,
        source_email_id=message.message_id,
        source_email_subject=message.subject,
    )


def _format_proposed_time(intent: MeetingIntent) -> str:
    """Human-readable start–end for the proposal row."""
    if intent.all_day:
        return (
            intent.start.strftime("%Y-%m-%d") + " (all day)"
            if intent.start
            else "all day"
        )
    if intent.start and intent.end:
        return (
            intent.start.strftime("%Y-%m-%d %H:%M")
            + "–"
            + intent.end.strftime("%H:%M")
        )
    if intent.start:
        return intent.start.strftime("%Y-%m-%d %H:%M")
    return "unspecified"


def _proposal_row(outcome: CalendarOutcome) -> CalendarProposalRow:
    """Translate a proposed/conflict outcome into a reviewable proposal row."""
    intent = outcome.intent
    return CalendarProposalRow(
        subject=intent.title or outcome.message.subject,
        proposed_time=_format_proposed_time(intent),
        confidence=intent.confidence_score,
        conflict="; ".join(outcome.conflict_titles),
        source_sender=outcome.message.sender,
        source_subject=outcome.message.subject,
    )


def run_calendar(
    source: MailSource,
    writer: CalendarWriter,
    llm: MeetingExtractor,
    scope: dict,
    confidence_threshold: float,
    target_calendar_id: str,
    proposals_sink: ProposalsSink,
) -> dict:
    """Drive one calendar run: fetch the scope, extract meeting-intent per
    message via the injected LLM, gate on confidence and conflicts, create the
    high-confidence conflict-free events, and route the rest to propose-only.

    ``source``, ``writer``, ``llm`` and ``proposals_sink`` are abstract
    contracts injected by the composition root (criterion E1). ``source`` and
    ``writer`` are connected here and disconnected in a finally — mirroring
    run()/run_flow_b() — so a failure mid-run still releases handles.

    Gating policy, in order: a message with no meeting-intent is skipped; one
    below ``confidence_threshold`` is proposed, never auto-created; one whose
    extracted start time is null is proposed (the tool-call schema permits a
    null start even when has_meeting is True, so this is caught defensively
    rather than trusting the prompt); one that conflicts with an existing event
    is proposed with the conflicting titles attached, never written over
    (propose-only-on-conflict). Only a high-confidence, timed, conflict-free
    event is created.

    Unlike run()'s fail-fast archive loop, a per-event create_event failure is
    captured as a "failed" outcome rather than raised: one malformed event must
    not abort the whole batch. Connect/fetch failures still propagate.

    Returns the run's counts, the created-event ids, the proposal-file path (or
    None), and the model/token fields the harness v2 schema records."""
    source.connect()
    writer.connect()
    try:
        messages = source.fetch(scope)

        outcomes: list[CalendarOutcome] = []
        for message in messages:
            intent = llm.extract(message)

            if not intent.has_meeting:
                outcomes.append(CalendarOutcome(message, intent, "skipped"))
                continue

            if intent.confidence_score < confidence_threshold:
                # Below the bar: surface for the operator, never auto-create.
                outcomes.append(CalendarOutcome(message, intent, "proposed"))
                continue

            if intent.start is None:
                # The tool-call schema permits a null start even when
                # has_meeting is True; the system prompt alone is not a
                # guarantee. A timeless event can't be created or conflict-
                # checked, so surface it for the operator instead.
                outcomes.append(CalendarOutcome(message, intent, "proposed"))
                continue

            event = _event_from_intent(intent, message, target_calendar_id)
            report = writer.check_conflicts(event)
            if report.conflicts:
                # Propose-only-on-conflict: never write over an existing event.
                outcomes.append(
                    CalendarOutcome(
                        message,
                        intent,
                        "conflict",
                        conflict_titles=[c.title for c in report.conflicts],
                    )
                )
                continue

            result = writer.create_event(event)
            if result.status == "created":
                outcomes.append(
                    CalendarOutcome(
                        message, intent, "created", event_id=result.event_id
                    )
                )
            else:
                # Captured, not raised — one bad event must not abort the batch.
                outcomes.append(
                    CalendarOutcome(
                        message, intent, "failed", error=result.error
                    )
                )

        proposal_rows = [
            _proposal_row(o)
            for o in outcomes
            if o.disposition in ("proposed", "conflict")
        ]
        proposal_path: Optional[str] = None
        if proposal_rows:
            run_meta = {
                "timestamp": datetime.now(),
                "scope": scope,
                "count": len(proposal_rows),
            }
            proposal_path = proposals_sink.write_calendar_proposals(
                proposal_rows, run_meta
            )
    finally:
        writer.disconnect()
        source.disconnect()

    created = [o for o in outcomes if o.disposition == "created"]
    in_tokens, out_tokens = llm.usage()
    return {
        "fetched": len(messages),
        "created": len(created),
        "proposed": len(proposal_rows),
        "skipped": sum(o.disposition == "skipped" for o in outcomes),
        "failed": sum(o.disposition == "failed" for o in outcomes),
        "created_event_ids": [o.event_id for o in created],
        "proposal_path": proposal_path,
        "model_id": llm.model_id,
        "token_cost_input": in_tokens,
        "token_cost_output": out_tokens,
    }
