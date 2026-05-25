# mail/imap_source.py — IMAP adapter, personal lane.
#
# Implements the MailSource contract over Gmail IMAP using imaplib (stdlib)
# and a Google App Password loaded from the local .env file. The adapter
# archives and labels only; it exposes no path to delete mail (criterion A3).

import email
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, parseaddr

from dotenv import load_dotenv

from mail.base import MailMessage, MailSource

# Gmail's stable, account-wide message identifier. Survives across sessions,
# unlike per-folder IMAP UIDs, so it is the opaque handle we hand out.
_MSGID_RE = re.compile(rb"X-GM-MSGID\s+(\d+)")

# Date format IMAP SEARCH expects, e.g. 25-May-2026.
_IMAP_DATE = "%d-%b-%Y"

# Tags whose text content is markup, not readable body, and must be dropped.
_HTML_SKIP_TAGS = {"script", "style", "head", "title"}


class _HtmlTextExtractor(HTMLParser):
    """Collect visible text from an HTML body, skipping script/style markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in _HTML_SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return " ".join("".join(self._chunks).split())


class ImapSource(MailSource):
    """Gmail IMAP implementation of the mail-read seam."""

    def __init__(self):
        self._conn = None
        self._address = None

    # --- lifecycle -------------------------------------------------------

    def connect(self) -> None:
        """Open and authenticate. Credentials load from .env via python-dotenv."""
        load_dotenv()
        self._address = os.environ["GMAIL_ADDRESS"]
        password = os.environ["GMAIL_APP_PASSWORD"]
        host = os.environ.get("IMAP_HOST", "imap.gmail.com")
        port = int(os.environ.get("IMAP_PORT", "993"))

        self._conn = imaplib.IMAP4_SSL(host, port)
        self._conn.login(self._address, password)

    def disconnect(self) -> None:
        """Close the connection cleanly."""
        if self._conn is None:
            return
        try:
            if self._conn.state == "SELECTED":
                self._conn.close()
            self._conn.logout()
        finally:
            self._conn = None

    # --- read ------------------------------------------------------------

    def fetch(self, scope: dict) -> list[MailMessage]:
        """Return messages matching scope, e.g. {'since_days': 7, 'folder': 'INBOX'}.

        Reads with BODY.PEEK so scanning never marks mail as \\Seen.
        """
        folder = scope.get("folder", "INBOX")
        since_days = scope.get("since_days", 7)

        # readonly so a read scan cannot mutate the mailbox.
        self._conn.select(self._quote(folder), readonly=True)

        since = (datetime.now() - timedelta(days=since_days)).strftime(_IMAP_DATE)
        typ, data = self._conn.uid("SEARCH", None, "SINCE", since)
        if typ != "OK":
            raise RuntimeError(f"IMAP SEARCH failed: {typ}")

        uids = data[0].split()
        messages = []
        for uid in uids:
            messages.append(self._fetch_one(uid))
        return messages

    def _fetch_one(self, uid: bytes) -> MailMessage:
        typ, data = self._conn.uid("FETCH", uid, "(X-GM-MSGID BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"IMAP FETCH failed for uid {uid!r}: {typ}")

        info, raw = data[0]
        match = _MSGID_RE.search(info)
        if not match:
            raise RuntimeError(f"no X-GM-MSGID in FETCH response for uid {uid!r}")
        message_id = match.group(1).decode()

        msg = email.message_from_bytes(raw)
        return MailMessage(
            message_id=message_id,
            sender=parseaddr(msg.get("From", ""))[1],
            subject=self._decode(msg.get("Subject", "")),
            date=self._parse_date(msg.get("Date")),
            has_unsubscribe=msg.get("List-Unsubscribe") is not None,
            body_text=self._body_text(msg),
        )

    # --- write (non-destructive) ----------------------------------------

    def archive(self, message_id: str) -> None:
        """Remove from inbox by dropping the \\Inbox label. Survives in All Mail."""
        uid = self._resolve(message_id)
        typ, _ = self._conn.uid("STORE", uid, "-X-GM-LABELS", r"(\Inbox)")
        if typ != "OK":
            raise RuntimeError(f"IMAP STORE failed archiving {message_id}: {typ}")

    def apply_label(self, message_id: str, label: str) -> None:
        """Tag a message, e.g. 'proposed-archive'. Gmail creates the label if new."""
        uid = self._resolve(message_id)
        typ, _ = self._conn.uid("STORE", uid, "+X-GM-LABELS", '("%s")' % label)
        if typ != "OK":
            raise RuntimeError(f"IMAP STORE failed labeling {message_id}: {typ}")

    def _resolve(self, message_id: str) -> bytes:
        """Map a Gmail message id to a UID in All Mail, which holds every message."""
        self._conn.select(self._all_mail_folder())
        typ, data = self._conn.uid("SEARCH", None, "X-GM-MSGID", message_id)
        if typ != "OK" or not data[0].split():
            raise RuntimeError(f"could not resolve message_id {message_id}")
        return data[0].split()[0]

    # --- helpers ---------------------------------------------------------

    def _all_mail_folder(self) -> str:
        """Find the mailbox flagged \\All, falling back to the English default."""
        typ, lines = self._conn.list()
        if typ == "OK":
            for line in lines:
                if line and rb"\All" in line:
                    # Mailbox name is the quoted segment at the end of the line.
                    name = line.decode().split(' "/" ')[-1].strip().strip('"')
                    return self._quote(name)
        return self._quote("[Gmail]/All Mail")

    @staticmethod
    def _quote(folder: str) -> str:
        return '"%s"' % folder

    @staticmethod
    def _decode(value: str) -> str:
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @staticmethod
    def _parse_date(raw) -> datetime:
        if not raw:
            return datetime.now(timezone.utc)
        try:
            return parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return datetime.now(timezone.utc)

    @staticmethod
    def _body_text(msg) -> str:
        """Return the body as plain text. Prefer text/plain; for HTML-only mail
        fall back to readable text stripped from the text/html part."""
        if msg.is_multipart():
            html_part = None
            for part in msg.walk():
                if "attachment" in str(part.get("Content-Disposition", "")):
                    continue
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    return ImapSource._decode_payload(part)
                if ctype == "text/html" and html_part is None:
                    html_part = part
            if html_part is not None:
                return ImapSource._html_to_text(ImapSource._decode_payload(html_part))
            return ""
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            return ImapSource._decode_payload(msg)
        if ctype == "text/html":
            return ImapSource._html_to_text(ImapSource._decode_payload(msg))
        return ""

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Strip tags to readable text using the stdlib html.parser, so no new
        dependency is needed. Drops script/style content and collapses runs."""
        parser = _HtmlTextExtractor()
        try:
            parser.feed(html)
        except Exception:
            return ""
        return parser.text()

    @staticmethod
    def _decode_payload(part) -> str:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
