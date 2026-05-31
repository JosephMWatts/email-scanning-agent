# tests/test_dedup.py — Deliverable 1 (cross-run propose-only dedup).
#
# Covers the six scoped cases:
#   a. _proposal_row carries the source email id.
#   b. Round-trip: write then read returns exactly the written ids.
#   c. Absent proposals file → empty set.
#   d. Skip-on-hit: a pre-seeded id yields disposition="duplicate", the LLM is
#      never asked to extract, and no proposal row is written for that id.
#   e. Control: an unseen id with a real meeting proposes/creates normally.
#   f. Self-pruning union: a prior id re-fetched this run is retained; a prior
#      id not re-fetched is dropped.

from datetime import datetime, timedelta, timezone

import agent
from agent import CalendarOutcome, MeetingIntent
from cal.base import ConflictReport, EventResult
from mail.base import MailMessage, MailSource
from cal.base import CalendarWriter
from vault.base import CalendarProposalRow
from vault.markdown_vault import MarkdownVault

UTC = timezone.utc


def _msg(message_id: str, subject: str = "Subj") -> MailMessage:
    return MailMessage(
        message_id=message_id,
        sender="someone@example.com",
        subject=subject,
        date=datetime(2026, 5, 31, 9, 0, tzinfo=UTC),
        has_unsubscribe=False,
        body_text="body",
    )


# --- fakes for the run_calendar seams ------------------------------------


class FakeSource(MailSource):
    """A MailSource that hands back a fixed message list."""

    def __init__(self, messages):
        self._messages = messages

    def connect(self) -> None:
        pass

    def fetch(self, scope):
        return list(self._messages)

    def archive(self, message_id):
        pass

    def apply_label(self, message_id, label):
        pass

    def disconnect(self) -> None:
        pass


class FakeWriter(CalendarWriter):
    """A conflict-free calendar writer that records what it creates."""

    def __init__(self):
        self.created = []

    def connect(self) -> None:
        pass

    def list_calendars(self):
        return []

    def check_conflicts(self, event):
        return ConflictReport(conflicts=[])

    def create_event(self, event):
        self.created.append(event)
        return EventResult(event_id=f"evt-{len(self.created)}", status="created")

    def update_event(self, event_id, event):
        return EventResult(event_id=event_id, status="updated")

    def delete_event(self, event_id):
        return EventResult(event_id=event_id, status="deleted")

    def disconnect(self) -> None:
        pass


class FakeExtractor:
    """Maps message_id → MeetingIntent and records every id it is asked about,
    so a test can assert the extract call was (or was not) made."""

    model_id = "fake-model"

    def __init__(self, intents):
        self._intents = intents
        self.calls = []

    def extract(self, message):
        self.calls.append(message.message_id)
        return self._intents[message.message_id]

    def usage(self):
        return (0, 0)


def _run(source, writer, llm, sink, threshold=0.8):
    return agent.run_calendar(
        source=source,
        writer=writer,
        llm=llm,
        scope={"folder": "INBOX", "since_days": 3},
        confidence_threshold=threshold,
        target_calendar_id="cal-1",
        proposals_sink=sink,
    )


# --- a. _proposal_row carries the id -------------------------------------


def test_proposal_row_carries_source_email_id():
    msg = _msg("<a@x>")
    outcome = CalendarOutcome(
        message=msg,
        intent=MeetingIntent(has_meeting=True, title="T", confidence_score=0.5),
        disposition="proposed",
    )
    row = agent._proposal_row(outcome)
    assert row.source_email_id == "<a@x>"


# --- b. round-trip write then read ---------------------------------------


def test_round_trip_returns_exact_ids(tmp_path):
    vault = MarkdownVault(str(tmp_path))
    ids = {"<a@x>", "<b@y>", "<c@z>"}
    run_meta = {"timestamp": datetime(2026, 5, 31, 9, 0), "scope": {}, "count": 0}
    vault.write_calendar_proposals([], run_meta, proposed_email_ids=ids)
    assert vault.read_proposed_email_ids() == ids


# --- c. absent file → empty set ------------------------------------------


def test_absent_file_yields_empty_set(tmp_path):
    vault = MarkdownVault(str(tmp_path))
    assert vault.read_proposed_email_ids() == set()


# --- d. skip-on-hit -------------------------------------------------------


def test_skip_on_hit_marks_duplicate_and_skips_extract(tmp_path):
    vault = MarkdownVault(str(tmp_path))
    dup_id = "<dup@x>"
    # Seed the proposals file so dup_id is already-proposed.
    seed_meta = {"timestamp": datetime(2026, 5, 31, 8, 0), "scope": {}, "count": 0}
    vault.write_calendar_proposals([], seed_meta, proposed_email_ids={dup_id})

    source = FakeSource([_msg(dup_id, subject="Lunch")])
    writer = FakeWriter()
    # An intent is registered, but extract must never be reached for the dup.
    llm = FakeExtractor(
        {dup_id: MeetingIntent(has_meeting=True, title="Lunch", confidence_score=0.9)}
    )

    result = _run(source, writer, llm, vault)

    assert llm.calls == []                 # extract never called for the dup
    assert result["duplicate"] == 1
    assert result["proposed"] == 0
    assert result["created"] == 0
    assert writer.created == []            # nothing created
    assert result["proposal_path"] is None  # no proposal rows → write skipped
    # The seeded id survives; no new proposal row was written for it.
    assert vault.read_proposed_email_ids() == {dup_id}


# --- e. control: new id flows through normally ---------------------------


def test_new_id_with_meeting_creates_normally(tmp_path):
    vault = MarkdownVault(str(tmp_path))
    new_id = "<new@x>"
    start = datetime(2026, 6, 2, 15, 0, tzinfo=UTC)
    source = FakeSource([_msg(new_id, subject="Sync")])
    writer = FakeWriter()
    llm = FakeExtractor(
        {
            new_id: MeetingIntent(
                has_meeting=True,
                title="Sync",
                start=start,
                end=start + timedelta(hours=1),
                confidence_score=0.95,
            )
        }
    )

    result = _run(source, writer, llm, vault)

    assert llm.calls == [new_id]           # extract was reached
    assert result["duplicate"] == 0
    assert result["created"] == 1
    assert len(writer.created) == 1
    assert writer.created[0].source_email_id == new_id


# --- f. self-pruning union ------------------------------------------------


def test_self_pruning_union_retains_refetched_drops_absent(tmp_path):
    vault = MarkdownVault(str(tmp_path))
    refetched = "<prior-refetched@x>"
    gone = "<prior-gone@x>"
    fresh = "<fresh@x>"

    # Prior run proposed two ids.
    seed_meta = {"timestamp": datetime(2026, 5, 31, 8, 0), "scope": {}, "count": 0}
    vault.write_calendar_proposals(
        [], seed_meta, proposed_email_ids={refetched, gone}
    )

    # This run re-fetches `refetched` (dup) and a `fresh` low-confidence meeting
    # that proposes — forcing a write. `gone` is not re-fetched.
    source = FakeSource([_msg(refetched), _msg(fresh, subject="Maybe")])
    writer = FakeWriter()
    llm = FakeExtractor(
        {
            fresh: MeetingIntent(
                has_meeting=True, title="Maybe", confidence_score=0.10
            )
        }
    )

    result = _run(source, writer, llm, vault)

    assert result["duplicate"] == 1        # refetched skipped before extract
    assert result["proposed"] == 1         # fresh below threshold → proposed
    assert llm.calls == [fresh]            # dup never extracted
    # Union: fresh (proposed this run) ∪ refetched (prior ∩ fetched). gone drops.
    assert vault.read_proposed_email_ids() == {refetched, fresh}
