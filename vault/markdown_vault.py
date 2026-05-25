# vault/markdown_vault.py — Obsidian-vault adapter.
#
# Implements the VaultStore contract over a local filesystem path that is an
# Obsidian vault root. Scans land as one reviewable markdown file per run in an
# "Email Scans" subfolder. The store only writes; it never reads or deletes.

import os

from vault.base import DigestRow, VaultStore

# Subfolder, relative to the vault root, that collects scan digests.
_SCANS_FOLDER = "Email Scans"

# Subfolder, relative to the vault root, holding the agent's governance state.
_AGENT_FOLDER = "Email Agent"

# Rule-store file, relative to _AGENT_FOLDER, mapping senders to a rule.
_SENDER_RULES_FILE = "Sender Rules.md"


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

    @staticmethod
    def _cell(text: str) -> str:
        """Collapse all whitespace and escape pipes so a table row never breaks."""
        collapsed = " ".join(str(text).split())
        return collapsed.replace("|", r"\|")
