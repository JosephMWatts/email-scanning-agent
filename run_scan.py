#!/usr/bin/env python3
"""Scan entry point — the vault-write slice, closing criterion B3.

Connects through the IMAP adapter, fetches a scope, summarizes each message,
and writes one reviewable markdown digest into an Obsidian vault. It reads mail
and writes the vault; it does not archive or label.

This is a composition root, one of the entry points where the concrete
ImapSource and MarkdownVault adapters get named — criterion E1. The agent
runtime receives a MailSource and a VaultStore by injection and never imports
these.
"""

import os
from datetime import datetime

from dotenv import load_dotenv

from agent import summarize
from mail.imap_source import ImapSource
from vault.base import DigestRow
from vault.markdown_vault import MarkdownVault

SCOPE = {"since_days": 3, "folder": "INBOX"}


def main() -> None:
    load_dotenv()
    vault_path = os.environ["VAULT_PATH"]

    source = ImapSource()
    vault = MarkdownVault(vault_path)

    source.connect()
    vault.connect()
    try:
        messages = source.fetch(SCOPE)
        rows = [
            DigestRow(message=msg, summary=summarize(msg.body_text))
            for msg in messages
        ]
        run_meta = {
            "timestamp": datetime.now(),
            "scope": f"last {SCOPE['since_days']} days",
            "messages": len(messages),
        }
        digest_path = vault.write_scan_digest(rows, run_meta)
    finally:
        vault.disconnect()
        source.disconnect()

    print(f"Scanned {len(messages)} message(s) for scope {SCOPE}")
    print(f"Digest written to: {digest_path}")


if __name__ == "__main__":
    main()
