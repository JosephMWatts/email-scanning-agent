# tests/test_retry.py — Deliverable 2 (LLM retry hardening).
#
# Covers the four scoped concerns:
#   1. A per-message extract failure is captured as a "failed" outcome and the
#      batch continues — one transient fault does not abort the run.
#   2. The disposition counts partition exactly: created/proposed/skipped/
#      duplicate/failed each land in their own bucket, one per message.
#   3. The extractor seam translates the curated transients into ExtractionError
#      (529 overloaded, connection error, missing tool call) and lets permanent
#      faults (401 auth) propagate raw. A post-response defect still bills the
#      tokens it spent.
#   4. harness.compute_run_status maps a batch outcome to the run-log status that
#      governs watermark advance, and a "failed" run does not advance the
#      read_last_success watermark.

from datetime import datetime, timedelta, timezone

import anthropic
import httpx
import pytest

import agent
import harness
from agent import ExtractionError, MeetingIntent
from cal.base import CalendarWriter, ConflictReport, EventResult
from llm.claude_extractor import ClaudeMeetingExtractor
from mail.base import MailMessage, MailSource

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


def _meeting(title: str) -> MeetingIntent:
    """A high-confidence, timed, conflict-free intent — creates under FakeWriter."""
    start = datetime(2026, 6, 2, 15, 0, tzinfo=UTC)
    return MeetingIntent(
        has_meeting=True,
        title=title,
        start=start,
        end=start + timedelta(hours=1),
        confidence_score=0.95,
    )


# --- fakes for the run_calendar seams (mirrors tests/test_dedup.py) -------


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


class FakeSink:
    """Structural ProposalsSink. read_proposed_email_ids returns a seeded set so
    a test can stage a duplicate without a real vault; write records its args."""

    def __init__(self, seeded=None):
        self._seeded = set(seeded or set())
        self.written = None

    def read_proposed_email_ids(self):
        return set(self._seeded)

    def write_calendar_proposals(self, rows, run_meta, proposed_email_ids):
        self.written = (list(rows), set(proposed_email_ids))
        return "/fake/Proposed Events.md"


class FakeExtractor:
    """Maps message_id → MeetingIntent OR an Exception to raise, and records
    every id it is asked about so a test can assert the batch kept going."""

    model_id = "fake-model"

    def __init__(self, intents):
        self._intents = intents
        self.calls = []

    def extract(self, message):
        self.calls.append(message.message_id)
        value = self._intents[message.message_id]
        if isinstance(value, Exception):
            raise value
        return value

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


# --- 1. extract failure captured, batch continues -------------------------


def test_extract_failure_captured_batch_continues():
    a, b, c = "<a@x>", "<b@x>", "<c@x>"
    source = FakeSource([_msg(a), _msg(b), _msg(c)])
    writer = FakeWriter()
    # The middle message raises a transient fault the extractor would have
    # wrapped as ExtractionError; the runtime must capture it and carry on.
    llm = FakeExtractor(
        {
            a: _meeting("A"),
            b: ExtractionError("transient API status 529: overloaded"),
            c: _meeting("C"),
        }
    )

    result = _run(source, writer, llm, FakeSink())

    assert llm.calls == [a, b, c]   # all three attempted; b did not abort
    assert result["failed"] == 1
    assert result["created"] == 2
    assert len(writer.created) == 2
    assert result["fetched"] == 3


# --- 2. disposition counts partition exactly ------------------------------


def test_failed_counts_partition_exactly():
    created_id = "<cr@x>"
    proposed_id = "<pr@x>"
    skipped_id = "<sk@x>"
    dup_id = "<du@x>"
    failed_id = "<fa@x>"

    source = FakeSource(
        [
            _msg(created_id),
            _msg(proposed_id),
            _msg(skipped_id),
            _msg(dup_id),
            _msg(failed_id),
        ]
    )
    writer = FakeWriter()
    llm = FakeExtractor(
        {
            created_id: _meeting("Create"),
            # Below threshold → proposed, never auto-created.
            proposed_id: MeetingIntent(
                has_meeting=True, title="Maybe", confidence_score=0.10
            ),
            # No meeting → skipped.
            skipped_id: MeetingIntent(has_meeting=False),
            # Transient fault → failed.
            failed_id: ExtractionError(
                "transient API connection error: net down"
            ),
            # dup_id intentionally absent: it must never reach extract.
        }
    )
    # Seed dup_id so it is already-proposed → "duplicate" before the paid call.
    sink = FakeSink(seeded={dup_id})

    result = _run(source, writer, llm, sink)

    assert result["created"] == 1
    assert result["proposed"] == 1
    assert result["skipped"] == 1
    assert result["duplicate"] == 1
    assert result["failed"] == 1
    assert result["fetched"] == 5
    assert dup_id not in llm.calls   # dedup skip happens before extract
    assert failed_id in llm.calls    # the failure was a real extract attempt


# --- 3. extractor-seam translation ----------------------------------------


class _FakeMessages:
    def __init__(self, exc=None, response=None):
        self._exc = exc
        self._response = response

    def create(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeClient:
    """Stand-in for anthropic.Anthropic: messages.create raises or returns."""

    def __init__(self, exc=None, response=None):
        self.messages = _FakeMessages(exc=exc, response=response)


def _extractor(exc=None, response=None):
    return ClaudeMeetingExtractor(client=_FakeClient(exc=exc, response=response))


def _request():
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def test_transient_status_wrapped_as_extraction_error():
    # 529 overloaded: a >=500 status_code is captured for retry, not raised.
    err = anthropic.InternalServerError(
        message="overloaded",
        response=httpx.Response(529, request=_request()),
        body=None,
    )
    with pytest.raises(ExtractionError):
        _extractor(exc=err).extract(_msg("<a@x>"))


def test_connection_error_wrapped():
    err = anthropic.APIConnectionError(message="net down", request=_request())
    with pytest.raises(ExtractionError):
        _extractor(exc=err).extract(_msg("<a@x>"))


def test_permanent_error_propagates_raw():
    # 401 auth: a config fault propagates raw so the run aborts loudly rather
    # than marking every message "failed".
    err = anthropic.AuthenticationError(
        message="bad key",
        response=httpx.Response(401, request=_request()),
        body=None,
    )
    with pytest.raises(anthropic.AuthenticationError):
        _extractor(exc=err).extract(_msg("<a@x>"))


class _TextBlock:
    type = "text"   # not a record_meeting_intent tool_use block


class _Usage:
    input_tokens = 5
    output_tokens = 2
    # No cache_* attributes; _accumulate_usage reads them via getattr(..., 0).


class _ResponseNoTool:
    content = [_TextBlock()]
    usage = _Usage()


def test_missing_tool_call_wrapped_and_usage_billed():
    ex = _extractor(response=_ResponseNoTool())
    with pytest.raises(ExtractionError):
        ex.extract(_msg("<a@x>"))
    # Usage was accumulated after the successful call and before the parse
    # defect, so the tokens it really spent are still billed (LD3).
    assert ex.usage() == (5, 2)


# --- 4. compute_run_status matrix + watermark integration -----------------


def test_compute_run_status_all_failed_holds_watermark():
    # failed>0 and progressed==0 → "failed": read_last_success ignores it, so
    # the dynamic watermark does not advance and the next run re-fetches.
    assert harness.compute_run_status(progressed=0, failed=3) == "failed"


def test_compute_run_status_partial_failure_advances():
    assert harness.compute_run_status(progressed=2, failed=1) == "success"


def test_compute_run_status_clean_run_advances():
    assert harness.compute_run_status(progressed=5, failed=0) == "success"


def test_compute_run_status_empty_window_advances():
    # All-zeros: a clean fetch with nothing to do has no failure to hold for.
    assert harness.compute_run_status(progressed=0, failed=0) == "success"


def test_failed_status_does_not_advance_watermark(tmp_path):
    vault = str(tmp_path)
    earlier = datetime(2026, 5, 30, 7, 0)
    later = datetime(2026, 5, 31, 7, 0)

    # An earlier successful run sets the watermark.
    harness.write_run_log(
        vault_path=vault,
        agent_id="calendar-agent",
        agent_version="1.0.0",
        started_at=earlier,
        completed_at=earlier,
        status="success",
        trigger="manual",
        input_summary="",
        output_summary="",
        output_paths=[],
    )
    # A later run where every extract failed logs status="failed" (the guard).
    harness.write_run_log(
        vault_path=vault,
        agent_id="calendar-agent",
        agent_version="1.0.0",
        started_at=later,
        completed_at=later,
        status="failed",
        trigger="scheduled",
        input_summary="",
        output_summary="",
        output_paths=[],
        error="all 3 extract attempt(s) failed with no successful extraction",
    )

    # read_last_success returns the earlier success, not the later failed run:
    # the watermark is held, so the next fetch window still covers later's span.
    assert harness.read_last_success(vault, "calendar-agent") == earlier
