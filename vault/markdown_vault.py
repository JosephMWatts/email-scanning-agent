# vault/markdown_vault.py — Obsidian-vault adapter.
#
# Implements the VaultStore contract over a local filesystem path that is an
# Obsidian vault root. Scans land as one reviewable markdown file per run in an
# "Email Scans" subfolder. The store only writes; it never reads or deletes.

import datetime
import os
import sys
from typing import Optional

from vault.base import (
    CalendarProposalRow,
    DigestRow,
    ReviewQueue,
    ReviewQueueRow,
    VaultStore,
)

# Subfolder, relative to the vault root, that collects scan digests.
_SCANS_FOLDER = "Email Scans"

# Subfolder, relative to the vault root, holding the agent's governance state.
_AGENT_FOLDER = "Email Agent"

# Rule-store file, relative to _AGENT_FOLDER, mapping senders to a rule.
_SENDER_RULES_FILE = "Sender Rules.md"

# Rolling review-queue file, relative to _AGENT_FOLDER, for unruled candidates.
_REVIEW_QUEUE_FILE = "Review Queue.md"

# Subfolder, relative to the vault root, holding the calendar agent's output.
_CALENDAR_FOLDER = "Calendar Agent"

# Rolling propose-only file, relative to _CALENDAR_FOLDER, for events the
# operator must approve before they are created.
_PROPOSALS_FILE = "Proposed Events.md"


class MarkdownVault(VaultStore):
    """Filesystem implementation of the vault-write seam, one digest per scan."""

    def __init__(self, root: str):
        self._root = root

    # --- lifecycle -------------------------------------------------------

    def connect(self) -> None:
        """Verify the vault root exists and is writable. Fail loud if not."""
        if not os.path.isdir(self._root):
            raise RuntimeError(
                f"vault root does not exist or is not a directory: {self._root}"
            )
        if not os.access(self._root, os.W_OK):
            raise RuntimeError(f"vault root is not writable: {self._root}")

    def disconnect(self) -> None:
        """A filesystem store has nothing to close; kept for symmetry."""
        pass

    # --- governance read -------------------------------------------------

    def read_sender_rules(self) -> dict[str, str]:
        """Return a mapping of full sender address to rule ("archive" or
        "keep"), parsed from <vault root>/Email Agent/Sender Rules.md.

        Strictly read-only: an absent rule store is a valid state and yields
        an empty dict — no rules yet, every candidate treated as unknown. The
        file is never created or written here. Sender addresses are lowercased
        so later matching is case-insensitive. Rows whose rule cell is neither
        "archive" nor "keep" are skipped. When a sender appears more than once
        with conflicting rules, "keep" wins over "archive" (criterion C5)."""
        path = os.path.join(self._root, _AGENT_FOLDER, _SENDER_RULES_FILE)
        if not os.path.isfile(path):
            return {}

        rules: dict[str, str] = {}
        with open(path, "r", encoding="utf-8") as fh:
            in_section = False  # seen the "# Sender rules" heading yet?
            header_seen = False  # consumed the table's header row yet?
            for line in fh:
                stripped = line.strip()
                if not in_section:
                    # Skip frontmatter and anything before the heading.
                    if stripped.lower() == "# sender rules":
                        in_section = True
                    continue
                if not stripped.startswith("|"):
                    # Blank line or prose between heading and table; ignore.
                    continue

                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if not header_seen:
                    # First table row is the column header; carries no rule.
                    header_seen = True
                    continue
                if set("".join(cells)) <= {"-", ":"}:
                    # The |---| separator row: no sender or rule.
                    continue
                if len(cells) < 2:
                    continue

                # Four columns — sender, rule, added, source — read first two.
                sender = cells[0].lower()
                rule = cells[1].lower()
                if rule not in ("archive", "keep"):
                    continue
                # "keep" wins over "archive" on conflict; never downgrade.
                if rules.get(sender) == "keep":
                    continue
                rules[sender] = rule

        return rules

    def read_review_queue(self) -> ReviewQueue:
        """Read back the hand-reviewed queue from <vault root>/Email Agent/
        Review Queue.md.

        An absent file is a clean no-op, not an error: it means no queue has
        been written yet, so return an empty, not-ready queue. is_ready is True
        only when the top-of-file checkbox reads "- [x] Reviewed, process this
        queue" ([X] accepted too), searched within the first 20 lines. Rows are
        the pipe lines following the "| Sender |" header, up to the first
        non-pipe line or EOF; each yields sender, subject, date, summary and
        your_call. Malformed rows — fewer than five columns — are skipped and
        logged to stderr for debugging. This is the action-read half of the
        bidirectional vault connection, criterion C3."""
        path = os.path.join(self._root, _AGENT_FOLDER, _REVIEW_QUEUE_FILE)
        if not os.path.isfile(path):
            return ReviewQueue(is_ready=False, rows=[])

        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()

        # The "Reviewed, process this queue" checkbox lives near the top; only
        # an explicitly checked box marks the queue ready to process.
        is_ready = False
        for line in lines[:20]:
            if line.strip().lower() == "- [x] reviewed, process this queue":
                is_ready = True
                break

        rows: list[ReviewQueueRow] = []
        in_table = False  # passed the "| Sender |" header row yet?
        for num, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not in_table:
                # Parse the row to find the header by first-cell value, so any
                # column padding (Advanced Tables, manual alignment) still matches.
                if stripped.startswith("|"):
                    cells = [c.strip() for c in stripped.strip("|").split("|")]
                    if cells and cells[0].lower() == "sender":
                        in_table = True
                continue
            if not stripped.startswith("|"):
                # First non-pipe line ends the table.
                break

            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if set("".join(cells)) <= {"-", ":"}:
                # The |---| separator row carries no verdict.
                continue
            if len(cells) < 5:
                print(
                    f"{path}: line {num}: skipping malformed review-queue row "
                    f"({len(cells)} columns, expected 5)",
                    file=sys.stderr,
                )
                continue

            sender, subject, date, summary, your_call = cells[:5]
            rows.append(
                ReviewQueueRow(
                    sender=sender,
                    subject=subject,
                    date=date,
                    summary=summary,
                    your_call=your_call,  # already stripped; "" if blank
                )
            )

        return ReviewQueue(is_ready=is_ready, rows=rows)

    def read_proposed_email_ids(self) -> set[str]:
        """Return the set of source email ids already proposed, parsed from the
        `proposed_email_ids:` frontmatter block list in <vault root>/Calendar
        Agent/Proposed Events.md.

        Strictly read-only: an absent proposals file is a valid empty state and
        yields an empty set — no file is created or written here, mirroring the
        absent-file no-op pattern of read_review_queue. Only the leading
        frontmatter block (between the first two `---` fences) is scanned; a
        stray `proposed_email_ids:` in the body is ignored. The key may be an
        empty inline list (`proposed_email_ids: []`) or a block list of `-`
        items; each item is unquoted on read so it round-trips what
        write_calendar_proposals emitted."""
        path = os.path.join(self._root, _CALENDAR_FOLDER, _PROPOSALS_FILE)
        if not os.path.isfile(path):
            return set()

        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()

        # Bound the scan to the leading frontmatter block: the first `---` opens
        # it, the next `---` closes it. No opening fence → no frontmatter.
        if not lines or lines[0].strip() != "---":
            return set()
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is None:
            return set()

        ids: set[str] = set()
        in_block = False  # consuming `- ` items under proposed_email_ids:?
        for line in lines[1:end]:
            stripped = line.strip()
            if not in_block:
                if stripped.startswith("proposed_email_ids:"):
                    rest = stripped[len("proposed_email_ids:"):].strip()
                    if rest and rest != "[]":
                        # Inline form, e.g. proposed_email_ids: [a, b].
                        inner = rest.strip("[]")
                        for part in inner.split(","):
                            val = self._unquote(part.strip())
                            if val:
                                ids.add(val)
                    elif not rest:
                        # Block form: the `- ` items follow on later lines.
                        in_block = True
                continue
            if stripped.startswith("-"):
                val = self._unquote(stripped[1:].strip())
                if val:
                    ids.add(val)
            else:
                # First non-item line ends the block list.
                break

        return ids

    # --- write -----------------------------------------------------------

    def write_scan_digest(self, rows: list[DigestRow], run_meta: dict) -> str:
        """Write the scan's extraction output as one markdown file. Return its
        absolute path. Each row bundles a message with its summary."""
        scans_dir = os.path.join(self._root, _SCANS_FOLDER)
        os.makedirs(scans_dir, exist_ok=True)

        ts = run_meta["timestamp"]
        filename = ts.strftime("%Y-%m-%d %H%M") + " Email Scan.md"
        path = os.path.join(scans_dir, filename)

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self._render(rows, run_meta))
        return os.path.abspath(path)

    def write_review_queue(self, rows: list[DigestRow], run_meta: dict) -> str:
        """Write the unruled candidates as a single rolling, hand-reviewable
        markdown file. Return its absolute path. Overwrites on each call."""
        agent_dir = os.path.join(self._root, _AGENT_FOLDER)
        os.makedirs(agent_dir, exist_ok=True)
        path = os.path.join(agent_dir, _REVIEW_QUEUE_FILE)

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self._render_queue(rows, run_meta))
        return os.path.abspath(path)

    def write_calendar_proposals(
        self,
        rows: list[CalendarProposalRow],
        run_meta: dict,
        proposed_email_ids: set[str],
    ) -> str:
        """Write the calendar agent's propose-only events as a single rolling,
        hand-reviewable markdown file under <vault root>/Calendar Agent/. Return
        its absolute path. Overwrites on each call, mirroring write_review_queue.
        Satisfies the ProposalsSink seam (criterion E1).

        proposed_email_ids is the file-level dedup set the runtime hands in: the
        self-pruning union of source email ids this file should be treated as
        having already proposed. It is persisted as a frontmatter block list
        (file-level, not per-row) and read back by read_proposed_email_ids."""
        cal_dir = os.path.join(self._root, _CALENDAR_FOLDER)
        os.makedirs(cal_dir, exist_ok=True)
        path = os.path.join(cal_dir, _PROPOSALS_FILE)

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self._render_proposals(rows, run_meta, proposed_email_ids))
        return os.path.abspath(path)

    def append_rule(self, sender: str, rule: str, source: str) -> None:
        """Append one exact-match rule to <vault root>/Email Agent/Sender
        Rules.md, creating the file on first call. The "added" date is stamped
        here as today's ISO date, not passed in.

        Append-only by design (Lesson 44): rows land at the bottom of the
        table with no duplicate detection, no sender-uniqueness check, and no
        enforced sort. rule must be "archive" or "keep" — anything else raises
        ValueError. An existing file with no recognizable "| Sender |" table
        header raises RuntimeError rather than risk silent corruption."""
        if rule not in ("archive", "keep"):
            raise ValueError(
                f'rule must be "archive" or "keep", got {rule!r}'
            )

        added = datetime.date.today().isoformat()
        row = "| {} | {} | {} | {} |".format(
            self._cell(sender),
            self._cell(rule),
            self._cell(added),
            self._cell(source),
        )

        agent_dir = os.path.join(self._root, _AGENT_FOLDER)
        path = os.path.join(agent_dir, _SENDER_RULES_FILE)

        if not os.path.isfile(path):
            os.makedirs(agent_dir, exist_ok=True)
            lines = [
                "---",
                "type: sender-rules",
                "schema_version: 1",
                "---",
                "",
                "# Sender rules",
                "",
                "| Sender | Rule | Added | Source |",
                "|---|---|---|---|",
                row,
            ]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            return

        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()

        # Locate the table header, then the last contiguous pipe line below it;
        # the new row is inserted right after that so existing rows, the
        # heading, and frontmatter are left untouched.
        header_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if cells and cells[0].lower() == "sender":
                    header_idx = i
                    break
        if header_idx is None:
            raise RuntimeError(
                f"no recognizable '| Sender |' table header in {path}"
            )

        insert_at = header_idx
        for i in range(header_idx + 1, len(lines)):
            if lines[i].strip().startswith("|"):
                insert_at = i
            else:
                break
        lines.insert(insert_at + 1, row)

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def rotate_review_queue(self) -> Optional[str]:
        """Rename <vault root>/Email Agent/Review Queue.md aside to
        Review Queue.processed.<TS>.md (TS = YYYY-MM-DD-HHMM) so the next scan
        writes a fresh queue. Return the rotated file's absolute path, or None
        if there is no queue to rotate. A target collision — two rotations in
        the same minute — raises FileExistsError rather than overwrite the
        earlier signed-off queue."""
        agent_dir = os.path.join(self._root, _AGENT_FOLDER)
        source = os.path.join(agent_dir, _REVIEW_QUEUE_FILE)
        if not os.path.isfile(source):
            return None

        ts = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
        target = os.path.join(agent_dir, f"Review Queue.processed.{ts}.md")
        if os.path.exists(target):
            raise FileExistsError(
                f"cannot rotate {source}: target already exists: {target}"
            )

        os.rename(source, target)
        return os.path.abspath(target)

    # --- rendering -------------------------------------------------------

    def _render(self, rows: list[DigestRow], run_meta: dict) -> str:
        ts = run_meta["timestamp"]
        created = ts.strftime("%Y-%m-%d %H:%M")
        count = run_meta["messages"]
        scope = run_meta["scope"]

        lines = [
            "---",
            "type: email-scan",
            f"created: {created}",
            f"messages: {count}",
            f"scope: {scope}",
            "---",
            "",
            "# Email scan digest",
            "",
            f"Scanned {created} · {count} messages · scope: {scope}",
            "",
            "| Sender | Subject | Date | Unsub | Summary |",
            "|---|---|---|---|---|",
        ]
        for row in rows:
            msg = row.message
            lines.append(
                "| {} | {} | {} | {} | {} |".format(
                    self._cell(msg.sender),
                    self._cell(msg.subject),
                    self._cell(msg.date.strftime("%b %d")),
                    "yes" if msg.has_unsubscribe else "no",
                    self._cell(row.summary),
                )
            )
        return "\n".join(lines) + "\n"

    def _render_queue(self, rows: list[DigestRow], run_meta: dict) -> str:
        ts = run_meta["timestamp"]
        created = ts.strftime("%Y-%m-%d %H:%M")
        count = len(rows)

        lines = [
            "---",
            "type: email-agent-review-queue",
            f"created: {created}",
            f"messages: {count}",
            "---",
            "",
            "# Review queue",
            "",
            f"Generated {created} · {count} messages to review",
            "",
            "- [ ] Reviewed, process this queue",
            "",
            "| Sender | Subject | Date | Summary | Your call |",
            "|---|---|---|---|---|",
        ]
        for row in rows:
            msg = row.message
            lines.append(
                "| {} | {} | {} | {} | {} |".format(
                    self._cell(msg.sender),
                    self._cell(msg.subject),
                    self._cell(msg.date.strftime("%Y-%m-%d")),
                    self._cell(row.summary),
                    "",
                )
            )
        return "\n".join(lines) + "\n"

    def _render_proposals(
        self,
        rows: list[CalendarProposalRow],
        run_meta: dict,
        proposed_email_ids: set[str],
    ) -> str:
        ts = run_meta["timestamp"]
        created = ts.strftime("%Y-%m-%d %H:%M")
        count = len(rows)

        lines = [
            "---",
            "type: calendar-agent-proposals",
            f"created: {created}",
            f"events: {count}",
        ]
        # File-level dedup set as a frontmatter block list. Sorted for stable,
        # diff-friendly output; quoted so message ids round-trip verbatim.
        if proposed_email_ids:
            lines.append("proposed_email_ids:")
            lines.extend(
                f"- {self._quote(eid)}" for eid in sorted(proposed_email_ids)
            )
        else:
            lines.append("proposed_email_ids: []")
        lines += [
            "---",
            "",
            "# Proposed events",
            "",
            f"Generated {created} · {count} proposed event(s) to review",
            "",
            "Check the box to approve; a future run creates the approved events.",
            "",
            "| Create? | Subject | Proposed time | Confidence | Conflict "
            "| Source sender | Source subject |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in rows:
            lines.append(
                "| {} | {} | {} | {} | {} | {} | {} |".format(
                    "[ ]",
                    self._cell(row.subject),
                    self._cell(row.proposed_time),
                    self._cell(f"{row.confidence:.2f}"),
                    self._cell(row.conflict),
                    self._cell(row.source_sender),
                    self._cell(row.source_subject),
                )
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _cell(text: str) -> str:
        """Collapse all whitespace and escape pipes so a table row never breaks."""
        collapsed = " ".join(str(text).split())
        return collapsed.replace("|", r"\|")

    @staticmethod
    def _quote(text: str) -> str:
        """Double-quote a frontmatter scalar with minimal escaping so message
        ids (which carry `<`, `>`, `@`, `:`) round-trip as YAML plain text."""
        escaped = str(text).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _unquote(text: str) -> str:
        """Inverse of _quote: strip surrounding double quotes and unescape. A
        bare, unquoted value passes through unchanged so hand-edited files still
        parse."""
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return text
