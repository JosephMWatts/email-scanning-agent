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

from dotenv import load_dotenv

import agent
from mail.imap_source import ImapSource
from vault.markdown_vault import MarkdownVault

SCOPE = {"since_days": 3, "folder": "INBOX"}


def main() -> None:
    load_dotenv()
    vault_path = os.environ["VAULT_PATH"]

    source = ImapSource()
    vault = MarkdownVault(vault_path)

    result = agent.run(source, vault, SCOPE)

    print(f"Scanned {result['fetched']} message(s) for scope {SCOPE}")
    print(f"Archived: {result['archived']}")
    print(f"Kept:     {result['kept']}")
    print(f"Queued:   {result['queued']}")
    print(f"Review queue written to: {result['review_queue_path']}")


if __name__ == "__main__":
    main()
