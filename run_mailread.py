#!/usr/bin/env python3
"""Minimal mail-read entry point — the mail-read slice of Lesson 41.

Connects through the IMAP adapter, fetches a small scope (last 3 days of
INBOX), and prints each message as a structured MailMessage record. It reads
only; it does not archive, label, or write anything.

This is the composition root: it is allowed to name the concrete adapter.
The agent runtime (agent.py, not built in this slice) will instead receive a
MailSource by injection and never import imap_source.py — criterion E1.
"""

from mail.imap_source import ImapSource

SCOPE = {"since_days": 3, "folder": "INBOX"}


def main() -> None:
    source = ImapSource()
    source.connect()
    try:
        messages = source.fetch(SCOPE)
    finally:
        source.disconnect()

    print(f"Fetched {len(messages)} message(s) for scope {SCOPE}\n")
    for msg in messages:
        body_preview = " ".join(msg.body_text.split())[:200]
        print("-" * 72)
        print(f"message_id      : {msg.message_id}")
        print(f"sender          : {msg.sender}")
        print(f"subject         : {msg.subject}")
        print(f"date            : {msg.date}")
        print(f"has_unsubscribe : {msg.has_unsubscribe}")
        print(f"body_text       : {body_preview}")
    print("-" * 72)


if __name__ == "__main__":
    main()
