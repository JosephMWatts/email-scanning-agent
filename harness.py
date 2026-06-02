"""Harness telemetry — run-log writer plus fetch-window recovery for Joseph's
agent fleet.

Fleet-portable. write_run_log emits a single run-log per agent run to
<vault>/Agent Runs/ per the Harness Pattern v1 schema. read_last_success and
compute_fetch_window (with the WindowResult it returns) read those same run-logs
back to drive idempotent state-based recovery: an agent computes "what hasn't
been processed yet" as last_success → now instead of a fixed window. All of it
stays decoupled from any one agent: imports nothing from a specific agent's
mail/ or vault/ layers and takes everything it needs as arguments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from math import ceil

SCHEMA_VERSION = 3
HOST = "personal"

# --- fetch-window recovery (Option C dynamic window) -------------------------
#
# A scheduled agent computes its fetch window from the latest successful run's
# completed_at instead of a fixed since_days. These bound that computation.

# Largest catch-up window, in days. If the host was off for a long stretch the
# window clamps here; the overage surfaces in the run-log rather than fetching
# an unbounded backlog.
MAX_WINDOW_DAYS = 7

# Fallback window, in days, when there is no successful watermark to read.
FIRST_RUN_DAYS = 1

# A recovered window wider than this many hours flags a possible missed
# scheduled fire — visible during run-log review. Daily fire + 1h slack.
GAP_CHECK_HOURS = 25


def _yaml_quote(value: str) -> str:
    """Double-quoted YAML scalar with the minimal escapes the schema needs."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _yaml_list(items: list[str]) -> str:
    """Render a block list, or inline [] when empty, matching the schema."""
    if not items:
        return " []"
    return "\n" + "\n".join(f"  - {_yaml_quote(item)}" for item in items)


def _vault_relative(path: str, vault_path: str) -> str:
    """Path relative to the vault root. An entry outside the vault keeps its
    ../ form — a diagnostic signal that an artifact landed off-vault."""
    return os.path.relpath(path, vault_path)


def write_run_log(
    vault_path: str,
    agent_id: str,
    agent_version: str,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    trigger: str,
    input_summary: str,
    output_summary: str,
    output_paths: list[str],
    error: str | None = None,
    parent_run_id: str | None = None,
    tags: list[str] | None = None,
    notes: str = "",
    model_id: str | None = None,
    token_cost_input: int | None = None,
    token_cost_output: int | None = None,
    token_cost_input_base: int | None = None,
    token_cost_input_cache_read: int | None = None,
    token_cost_input_cache_write: int | None = None,
    token_cost_thinking: int | None = None,
    batch: bool = False,
) -> str:
    """Write a harness run-log to <vault_path>/Agent Runs/. Returns the path written."""
    tags = tags or []

    slug = agent_id.replace("-", "")
    run_id = f"{started_at:%Y-%m-%d-%H%M}-{slug}"
    # duration keeps full precision; only the displayed timestamps are truncated.
    duration_ms = int((completed_at - started_at).total_seconds() * 1000)
    started_display = started_at.replace(microsecond=0)
    completed_display = completed_at.replace(microsecond=0)

    rel_paths = [_vault_relative(p, vault_path) for p in output_paths]

    runs_dir = os.path.join(vault_path, "Agent Runs")
    os.makedirs(runs_dir, exist_ok=True)
    out_path = os.path.join(runs_dir, f"{started_at:%Y-%m-%d %H%M} {agent_id}.md")

    error_field = "null" if error is None else _yaml_quote(error)
    parent_field = "null" if parent_run_id is None else _yaml_quote(parent_run_id)
    model_field = "null" if model_id is None else _yaml_quote(model_id)

    # Schema 3: when any cache-aware input breakdown field is supplied, the
    # emitted token_cost_input is their sum so legacy readers still see a single
    # input total; otherwise it is whatever the caller passed verbatim.
    _input_breakdown = (
        token_cost_input_base,
        token_cost_input_cache_read,
        token_cost_input_cache_write,
    )
    if any(v is not None for v in _input_breakdown):
        effective_token_cost_input: int | None = sum(v or 0 for v in _input_breakdown)
    else:
        effective_token_cost_input = token_cost_input

    def _int_or_null(value: int | None) -> str:
        return "null" if value is None else str(value)

    token_in_field = _int_or_null(effective_token_cost_input)
    token_out_field = _int_or_null(token_cost_output)
    token_in_base_field = _int_or_null(token_cost_input_base)
    token_in_cache_read_field = _int_or_null(token_cost_input_cache_read)
    token_in_cache_write_field = _int_or_null(token_cost_input_cache_write)
    token_thinking_field = _int_or_null(token_cost_thinking)
    batch_field = "true" if batch else "false"

    frontmatter = (
        "---\n"
        f"schema_version: {SCHEMA_VERSION}\n"
        f"agent_id: {agent_id}\n"
        f"agent_version: {agent_version}\n"
        f"model_id: {model_field}\n"
        f"run_id: {run_id}\n"
        f"parent_run_id: {parent_field}\n"
        f"host: {HOST}\n"
        f"started_at: {started_display.isoformat()}\n"
        f"completed_at: {completed_display.isoformat()}\n"
        f"duration_ms: {duration_ms}\n"
        f"status: {status}\n"
        f"trigger: {trigger}\n"
        f"input_summary: {_yaml_quote(input_summary)}\n"
        f"output_summary: {_yaml_quote(output_summary)}\n"
        f"output_paths:{_yaml_list(rel_paths)}\n"
        f"token_cost_input: {token_in_field}\n"
        f"token_cost_output: {token_out_field}\n"
        f"token_cost_input_base: {token_in_base_field}\n"
        f"token_cost_input_cache_read: {token_in_cache_read_field}\n"
        f"token_cost_input_cache_write: {token_in_cache_write_field}\n"
        f"token_cost_thinking: {token_thinking_field}\n"
        f"batch: {batch_field}\n"
        f"tags:{_yaml_list(tags)}\n"
        f"error: {error_field}\n"
        f"notes: {_yaml_quote(notes)}\n"
        "---\n"
    )

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(frontmatter)

    return out_path


@dataclass
class WindowResult:
    """The computed fetch window for one run, plus the provenance the run-log
    records. since_days is the only field the fetch seam consumes; the rest
    describe how it was derived so run-log review can see the reasoning."""

    since_days: int
    recovered_hours: float | None  # last_success → now, in hours; None when not dynamic
    capped: bool                   # raw window exceeded max_days and was clamped
    first_run: bool                # no watermark; fell back to first_run_days
    override: bool                 # operator --since-days took precedence
    gap: bool                      # recovered window exceeded gap_check_hours


def _read_frontmatter_field(path: str, field: str) -> str | None:
    """Return the value of a top-level `field:` line from a run-log's leading
    frontmatter block, or None if absent. Scans only the first `---`-fenced
    block so a stray match in the body is ignored. Never raises on I/O — an
    unreadable file yields None so the caller can skip it."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None

    if not lines or lines[0].strip() != "---":
        return None

    prefix = f"{field}:"
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return None


def read_last_success(vault_path: str, agent_id: str) -> datetime | None:
    """Return the completed_at of the most recent successful run-log for
    agent_id, or None if there is no successful watermark to read.

    Reads the run-logs write_run_log writes to <vault_path>/Agent Runs/. The
    filename leads with the run's timestamp and ends with " {agent_id}.md", so
    filenames are prefiltered by that suffix (cheap, and it keeps a shared runs
    directory's other agents out of the scan) and sorted descending — newest
    first. The first file carrying `status: success` with a parseable
    `completed_at:` wins.

    Defensive throughout: a missing directory yields None; a file missing
    status/completed_at, or whose completed_at will not parse, is skipped rather
    than raised. A window-read failure must never abort the run — worst case the
    caller falls back to the first-run window."""
    runs_dir = os.path.join(vault_path, "Agent Runs")
    if not os.path.isdir(runs_dir):
        return None

    suffix = f" {agent_id}.md"
    names = [n for n in os.listdir(runs_dir) if n.endswith(suffix)]
    # Filename leads with the run timestamp, so lexical descending == newest first.
    for name in sorted(names, reverse=True):
        path = os.path.join(runs_dir, name)
        if _read_frontmatter_field(path, "status") != "success":
            continue
        raw = _read_frontmatter_field(path, "completed_at")
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            continue
    return None


def count_failed_runs(vault_path: str, agent_id: str) -> int:
    """Return how many run-logs for agent_id carry `status: failed`. Used to
    distinguish genuine genesis (no logs at all) from an all-failed history when
    there is no successful watermark, so the latter can be surfaced loudly.
    Mirrors read_last_success's defensive posture: missing dir → 0."""
    runs_dir = os.path.join(vault_path, "Agent Runs")
    if not os.path.isdir(runs_dir):
        return 0

    suffix = f" {agent_id}.md"
    failed = 0
    for name in os.listdir(runs_dir):
        if not name.endswith(suffix):
            continue
        path = os.path.join(runs_dir, name)
        if _read_frontmatter_field(path, "status") == "failed":
            failed += 1
    return failed


def compute_run_status(progressed: int, failed: int) -> str:
    """Map a batch's per-message outcome to the run-log status that governs
    watermark advance. Pure: no I/O.

    read_last_success only treats a `status: success` run as a watermark, so the
    status returned here decides whether the dynamic fetch window rolls forward
    past the messages this run saw. Returns "failed" only when failures occurred
    and nothing progressed — every extract attempt failed — so the watermark is
    held and the next run re-fetches the same window for retry; cross-run dedup
    absorbs the re-proposed successes and conflict detection protects already-
    created events. A single failure amid real progress still returns "success":
    one transient blip is tolerated, and the operator sees the failed count in
    output_summary. All-zeros (a clean fetch with nothing to do) is "success" —
    there is no failure to hold the cursor for.

    `progressed` counts messages that completed an extract this run (created +
    proposed + skipped); duplicates are excluded — a dedup-skip needs no
    reprocessing but must not mask a co-occurring failure from this guard. The
    yellow-to-green redundancy carry-over from the Deliverable 1.5 gap-check: a
    degraded run stays visible instead of silently advancing the cursor."""
    if failed > 0 and progressed == 0:
        return "failed"
    return "success"


def compute_fetch_window(
    last_success: datetime | None,
    now: datetime,
    *,
    override_days: int | None = None,
    max_days: int = MAX_WINDOW_DAYS,
    first_run_days: int = FIRST_RUN_DAYS,
    gap_check_hours: float = GAP_CHECK_HOURS,
) -> WindowResult:
    """Compute the fetch window from the last successful watermark. Pure: no I/O.

    Precedence:
      1. override_days set → operator catch-up wins; dynamic logic is ignored
         and the gap-check is suppressed (the operator chose the window).
      2. last_success is None → no watermark; fall back to first_run_days.
      3. otherwise → since_days is ceil(last_success → now in days), clamped to
         max_days, and flagged as a gap when the recovered span exceeds
         gap_check_hours.

    The IMAP fetch seam is date-granular, so the precise interval collapses to a
    whole-day count at the boundary; ceil rounds up so the window never
    undershoots — re-fetched overlap is absorbed by cross-run dedup. The
    gap-check uses strict greater-than: exactly gap_check_hours is not a gap."""
    if override_days is not None:
        return WindowResult(
            since_days=override_days,
            recovered_hours=None,
            capped=False,
            first_run=False,
            override=True,
            gap=False,
        )

    if last_success is None:
        return WindowResult(
            since_days=first_run_days,
            recovered_hours=None,
            capped=False,
            first_run=True,
            override=False,
            gap=False,
        )

    recovered_hours = (now - last_success).total_seconds() / 3600
    since_days_raw = ceil((now - last_success).total_seconds() / 86400)
    since_days = min(since_days_raw, max_days)
    return WindowResult(
        since_days=since_days,
        recovered_hours=recovered_hours,
        capped=since_days_raw > max_days,
        first_run=False,
        override=False,
        gap=recovered_hours > gap_check_hours,
    )
