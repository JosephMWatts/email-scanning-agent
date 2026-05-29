#!/usr/bin/env python3
"""Flow B entry point — process the operator-approved review queue.

Reads the hand-reviewed queue from the Obsidian vault, turns each approved row
into a sender rule, and retires the queue so the next scan starts clean —
criterion C3. Flow B never touches mail: it works only against the vault.

This is a composition root, one of the entry points where the concrete
MarkdownVault adapter gets named — criterion E1. The agent runtime in agent.py
receives a VaultStore by injection and never imports it.
"""

import os
from datetime import datetime

from dotenv import load_dotenv

import agent
import harness
from vault.markdown_vault import MarkdownVault

AGENT_ID = "email-scanner-flow-b"
AGENT_VERSION = "1.0.0"


def main() -> None:
    load_dotenv()
    vault_path = os.environ["VAULT_PATH"]
    trigger = os.environ.get("TRIGGER", "manual")

    vault = MarkdownVault(vault_path)

    started_at = datetime.now().astimezone()
    try:
        result = agent.run_flow_b(vault)
    except Exception as e:
        harness.write_run_log(
            vault_path=vault_path,
            agent_id=AGENT_ID,
            agent_version=AGENT_VERSION,
            started_at=started_at,
            completed_at=datetime.now().astimezone(),
            status="failed",
            trigger=trigger,
            input_summary="review queue",
            output_summary="",
            output_paths=[],
            error=str(e),
            parent_run_id=None,
        )
        raise
    completed_at = datetime.now().astimezone()

    if not result["is_ready"]:
        output_summary = "queue not ready, no-op"
        output_paths: list[str] = []
    else:
        output_summary = (
            f"{result['rows_processed']} rules appended, "
            f"{result['rows_skipped']} rows skipped"
        )
        output_paths = []
        if result["rules_appended"]:
            output_paths.append("Email Agent/Sender Rules.md")
        if result["rotated_to"]:
            output_paths.append(result["rotated_to"])

    harness.write_run_log(
        vault_path=vault_path,
        agent_id=AGENT_ID,
        agent_version=AGENT_VERSION,
        started_at=started_at,
        completed_at=completed_at,
        status="success",
        trigger=trigger,
        input_summary="review queue",
        output_summary=output_summary,
        output_paths=output_paths,
        parent_run_id=None,
    )

    if not result["is_ready"]:
        print("Flow B: queue not ready, no-op.")
        return

    print("Flow B run: queue ready")
    print(f"Processed: {result['rows_processed']}")
    print(f"Skipped:   {result['rows_skipped']}")
    print(f"Rotated:   {result['rotated_to']}")
    if result['rules_appended']:
        print("Rules appended:")
        for sender, rule in result['rules_appended']:
            print(f"  {sender} -> {rule}")


if __name__ == "__main__":
    main()
