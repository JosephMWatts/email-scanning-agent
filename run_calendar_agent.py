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

import os
from datetime import datetime

from dotenv import load_dotenv

import agent
import harness
from cal.eventkit_writer import EventKitWriter
from llm.claude_extractor import ClaudeMeetingExtractor
from mail.imap_source import ImapSource
from vault.markdown_vault import MarkdownVault

SCOPE = {"since_days": 3, "folder": "INBOX"}

# Above this meeting-intent confidence, a conflict-free event is auto-created;
# at or below it the event is routed to the propose-only queue instead. Tunable
# after observing a week of runs.
CONFIDENCE_THRESHOLD = 0.80

AGENT_ID = "calendar-agent"
AGENT_VERSION = "1.0.0"


def main() -> None:
    load_dotenv()
    vault_path = os.environ["VAULT_PATH"]
    # Which calendar receives auto-created events. Required, no silent default:
    # writing to the wrong calendar is worse than failing loud at startup.
    target_calendar_id = os.environ["CALENDAR_ID"]
    # Read at startup so failure and success paths see the same value. Default
    # "manual" preserves today's hand-fired cadence; a future launchd plist
    # would set TRIGGER=scheduled in EnvironmentVariables to flip this.
    trigger = os.environ.get("TRIGGER", "manual")

    source = ImapSource()
    writer = EventKitWriter()
    llm = ClaudeMeetingExtractor(calendar_id=target_calendar_id)
    sink = MarkdownVault(vault_path)

    input_summary = f"{SCOPE['folder']}, since_days={SCOPE['since_days']}"

    started_at = datetime.now().astimezone()
    try:
        result = agent.run_calendar(
            source,
            writer,
            llm,
            SCOPE,
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

    harness.write_run_log(
        vault_path=vault_path,
        agent_id=AGENT_ID,
        agent_version=AGENT_VERSION,
        started_at=started_at,
        completed_at=completed_at,
        status="success",
        trigger=trigger,
        input_summary=input_summary,
        output_summary=output_summary,
        output_paths=output_paths,
        parent_run_id=None,  # V1: standalone run — see the failed-path comment.
        model_id=result["model_id"],
        token_cost_input=result["token_cost_input"],
        token_cost_output=result["token_cost_output"],
    )

    print(f"Scanned {result['fetched']} message(s) for scope {SCOPE}")
    print(f"Created:   {result['created']}")
    print(f"Proposed:  {result['proposed']}")
    print(f"Duplicate: {result['duplicate']}")
    print(f"Skipped:   {result['skipped']}")
    print(f"Failed:    {result['failed']}")
    if result["proposal_path"]:
        print(f"Proposals written to: {result['proposal_path']}")


if __name__ == "__main__":
    main()
