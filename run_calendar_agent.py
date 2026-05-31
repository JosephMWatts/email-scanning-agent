#!/usr/bin/env python3
"""Calendar agent entry point — the A6 meeting-intent runtime, end to end.

Reads recent mail through the IMAP adapter, extracts meeting-intent with the
injected LLM, and writes high-confidence, conflict-free events to the calendar
through EventKit. Low-confidence or conflicting events are routed to a
propose-only queue in the vault for the operator to approve.

This is a composition root, one of the entry points where the concrete
ImapSource, EventKitWriter, ClaudeMeetingExtractor and MarkdownVault adapters
get named — criterion E1. The runtime in agent.run_calendar receives a
MailSource, CalendarWriter, MeetingExtractor and ProposalsSink by injection and
never imports these.
"""

import argparse
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

import agent
import harness
from cal.eventkit_writer import EventKitWriter
from llm.claude_extractor import ClaudeMeetingExtractor
from mail.imap_source import ImapSource
from vault.markdown_vault import MarkdownVault

# Mailbox folder is fixed; the fetch window is computed per run (Option C
# dynamic window) or set by the --since-days override.
FOLDER = "INBOX"

# Above this meeting-intent confidence, a conflict-free event is auto-created;
# at or below it the event is routed to the propose-only queue instead. Tunable
# after observing a week of runs.
CONFIDENCE_THRESHOLD = 0.80

AGENT_ID = "calendar-agent"
AGENT_VERSION = "1.0.0"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the A6 calendar agent over recent mail."
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Override the dynamic fetch window with a fixed N-day lookback. "
            "Takes precedence over the last_success → now computation; used for "
            "manual catch-up."
        ),
    )
    return parser.parse_args()


def _summarize_window(window: harness.WindowResult) -> str:
    """Window-aware input_summary, e.g. 'INBOX, since_days=2 (dynamic, recovered
    31.2h)'. Records how the window was derived for run-log review."""
    if window.override:
        mode = "override"
    elif window.first_run:
        mode = "first-run"
    else:
        mode = "dynamic"

    parts = [mode]
    if window.recovered_hours is not None:
        parts.append(f"recovered {window.recovered_hours:.1f}h")
    if window.capped:
        parts.append("capped at 7d")
    if window.gap:
        parts.append("gap")
    return f"{FOLDER}, since_days={window.since_days} ({', '.join(parts)})"


def _gap_check_note(
    window: harness.WindowResult, vault_path: str
) -> str:
    """The run-log notes string for window anomalies, or "" when there is
    nothing to flag. Suppressed on the operator-override path (DP3). The
    all-failed sentinel (DP5) is louder than a pure genesis first run: it means
    prior runs existed but none succeeded, so a 24h fallback could silently miss
    older mail those runs were meant to cover."""
    if window.override:
        return ""

    if window.first_run:
        failed = harness.count_failed_runs(vault_path, AGENT_ID)
        if failed > 0:
            return (
                f"gap-check: no successful watermark despite {failed} prior "
                f"failed attempts; fetched since_days={window.since_days} fallback"
            )
        # Pure genesis — zero prior logs of any status. Nothing to flag.
        return ""

    if window.gap:
        capped = ", capped at 7d" if window.capped else ""
        return (
            f"gap-check: recovered window {window.recovered_hours:.1f}h exceeds "
            f"{int(harness.GAP_CHECK_HOURS)}h threshold (possible missed "
            f"scheduled fire); fetched since_days={window.since_days}{capped}"
        )
    return ""


def main() -> None:
    load_dotenv()
    args = _parse_args()
    vault_path = os.environ["VAULT_PATH"]
    # Which calendar receives auto-created events. Required, no silent default:
    # writing to the wrong calendar is worse than failing loud at startup.
    target_calendar_id = os.environ["CALENDAR_ID"]
    # Read at startup so failure and success paths see the same value. Default
    # "manual" preserves today's hand-fired cadence; a future launchd plist
    # would set TRIGGER=scheduled in EnvironmentVariables to flip this.
    trigger = os.environ.get("TRIGGER", "manual")

    # Dynamic fetch window: read the latest successful watermark and compute
    # last_success → now, unless --since-days overrides it. now is tz-aware to
    # match the run-log's completed_at on subtraction.
    last_success = harness.read_last_success(vault_path, AGENT_ID)
    window = harness.compute_fetch_window(
        last_success,
        datetime.now().astimezone(),
        override_days=args.since_days,
    )
    scope = {"since_days": window.since_days, "folder": FOLDER}

    source = ImapSource()
    writer = EventKitWriter()
    llm = ClaudeMeetingExtractor(calendar_id=target_calendar_id)
    sink = MarkdownVault(vault_path)

    input_summary = _summarize_window(window)
    note = _gap_check_note(window, vault_path)
    if note:
        print(note, file=sys.stderr)

    started_at = datetime.now().astimezone()
    try:
        result = agent.run_calendar(
            source,
            writer,
            llm,
            scope,
            CONFIDENCE_THRESHOLD,
            target_calendar_id,
            sink,
        )
    except Exception as e:
        harness.write_run_log(
            vault_path=vault_path,
            agent_id=AGENT_ID,
            agent_version=AGENT_VERSION,
            started_at=started_at,
            completed_at=datetime.now().astimezone(),
            status="failed",
            trigger=trigger,
            input_summary=input_summary,
            output_summary="",
            output_paths=[],
            error=str(e),
            # parent_run_id is always None for V1: the calendar agent reads the
            # inbox independently — it is NOT a downstream of Flow A's output —
            # so there is no real parent-child run relationship to record. A
            # labeled handoff is the right V2 design once A6 actually consumes a
            # Flow A artifact rather than re-reading the inbox.
            parent_run_id=None,
            notes=note,
        )
        raise
    completed_at = datetime.now().astimezone()

    output_summary = (
        f"{result['created']} created, {result['proposed']} proposed, "
        f"{result['duplicate']} duplicate, {result['skipped']} skipped, "
        f"{result['failed']} failed"
    )
    # Real path from the orchestration result — no hardcoded vault-relative
    # literal. Empty list when nothing was proposed.
    output_paths = [result["proposal_path"]] if result["proposal_path"] else []

    # DP2(b) all-failed/zero-progress guard. run_calendar captures a per-message
    # extract failure as a "failed" outcome and returns normally, so a batch
    # where every extract failed lands here, not on the except path above.
    # Logging status="success" would let harness.read_last_success advance the
    # dynamic watermark past a window nothing was actually processed in,
    # silently dropping those messages. Hold the watermark by logging
    # status="failed" when failures occurred and nothing progressed; the next
    # run re-fetches the same window and cross-run dedup absorbs the re-proposed
    # successes. "progressed" excludes duplicates: a dedup-skip needs no retry
    # but must not mask a co-occurring failure. A single failure amid progress
    # still succeeds and the failed count stays visible in output_summary.
    progressed = result["created"] + result["proposed"] + result["skipped"]
    run_status = harness.compute_run_status(
        progressed=progressed, failed=result["failed"]
    )
    run_error = None
    if run_status == "failed":
        run_error = (
            f"all {result['failed']} extract attempt(s) failed with no "
            f"successful extraction; watermark held so the next run re-fetches "
            f"this window"
        )
        print(run_error, file=sys.stderr)

    harness.write_run_log(
        vault_path=vault_path,
        agent_id=AGENT_ID,
        agent_version=AGENT_VERSION,
        started_at=started_at,
        completed_at=completed_at,
        status=run_status,
        trigger=trigger,
        input_summary=input_summary,
        output_summary=output_summary,
        output_paths=output_paths,
        error=run_error,
        parent_run_id=None,  # V1: standalone run — see the failed-path comment.
        notes=note,
        model_id=result["model_id"],
        token_cost_input=result["token_cost_input"],
        token_cost_output=result["token_cost_output"],
    )

    print(f"Scanned {result['fetched']} message(s) for scope {scope}")
    print(f"Created:   {result['created']}")
    print(f"Proposed:  {result['proposed']}")
    print(f"Duplicate: {result['duplicate']}")
    print(f"Skipped:   {result['skipped']}")
    print(f"Failed:    {result['failed']}")
    if result["proposal_path"]:
        print(f"Proposals written to: {result['proposal_path']}")


if __name__ == "__main__":
    main()
