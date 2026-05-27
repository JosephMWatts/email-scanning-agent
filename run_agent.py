#!/usr/bin/env python3
"""Agent entry point — the section C rules engine, end to end.

Connects through the IMAP adapter, reads the sender rules from the Obsidian
vault, archives mail whose sender has an archive rule, and writes the unruled
candidates to a hand-reviewable queue in the vault.

This is a composition root, one of the entry points where the concrete
ImapSource and MarkdownVault adapters get named — criterion E1. The agent
runtime in agent.py receives a MailSource and a VaultStore by injection and
never imports these.
"""

import os
from datetime import datetime

from dotenv import load_dotenv

import agent
import harness
from mail.imap_source import ImapSource
from vault.markdown_vault import MarkdownVault

SCOPE = {"since_days": 3, "folder": "INBOX"}

AGENT_ID = "email-scanner-flow-a"
AGENT_VERSION = "1.0.0"


def main() -> None:
    load_dotenv()
    vault_path = os.environ["VAULT_PATH"]

    source = ImapSource()
    vault = MarkdownVault(vault_path)

    input_summary = f"{SCOPE['folder']}, since_days={SCOPE['since_days']}"

    started_at = datetime.now().astimezone()
    try:
        result = agent.run(source, vault, SCOPE)
    except Exception as e:
        harness.write_run_log(
            vault_path=vault_path,
            agent_id=AGENT_ID,
            agent_version=AGENT_VERSION,
            started_at=started_at,
            completed_at=datetime.now().astimezone(),
            status="failed",
            trigger="manual",
            input_summary=input_summary,
            output_summary="",
            output_paths=[],
            error=str(e),
            parent_run_id=None,
        )
        raise
    completed_at = datetime.now().astimezone()

    output_summary = (
        f"{result['archived']} archived, {result['kept']} kept, "
        f"{result['queued']} queued"
    )
    harness.write_run_log(
        vault_path=vault_path,
        agent_id=AGENT_ID,
        agent_version=AGENT_VERSION,
        started_at=started_at,
        completed_at=completed_at,
        status="success",
        trigger="manual",
        input_summary=input_summary,
        output_summary=output_summary,
        output_paths=[result["review_queue_path"]],
        parent_run_id=None,
    )

    print(f"Scanned {result['fetched']} message(s) for scope {SCOPE}")
    print(f"Archived: {result['archived']}")
    print(f"Kept:     {result['kept']}")
    print(f"Queued:   {result['queued']}")
    print(f"Review queue written to: {result['review_queue_path']}")


if __name__ == "__main__":
    main()
