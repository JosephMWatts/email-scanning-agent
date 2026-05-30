# cal/eventkit_writer.py — EventKitWriter.
#
# Personal-lane Cadillac. Calls the vendored che-ical-mcp binary via subprocess
# in --cli mode. Handles every account macOS Calendar.app sees (iCloud, Google
# via CalDAV bridge, Exchange via CalDAV bridge, subscribed calendars).
#
# Operator prerequisites:
#   1. che-ical-mcp binary installed at the configured path
#      (default ~/bin/CheICalMCP), vetted per OSS Install Triangle protocol.
#   2. Calendar + Reminders TCC permissions granted to Terminal.app
#      (parent app of any process spawning the binary).
#   3. Source accounts (iCloud, Google, etc.) configured in
#      macOS System Settings → Internet Accounts.
#
# Schema translation notes:
#   - The CalendarWriter dataclass uses stable UUIDs (calendar_id) returned by
#     list_calendars(). che-ical's create/update/check tools want
#     calendar_name + calendar_source (the latter required when multiple
#     calendars share a name). This writer maintains a UUID → CalendarRef
#     cache (populated lazily on first list_calendars call) and translates at
#     the subprocess boundary.
#   - che-ical input field names: start_time, end_time, calendar_name,
#     calendar_source, all_day, url, location, notes, attendees, event_id.
#     Asymmetric with read responses (which use is_all_day, etc.).
#
# References:
#   - OSS Install Triangle: joseph_vault/Agentic Toolkit/OSS Install Triangle.md
#   - che-ical vetting report: joseph_vault/Agentic Toolkit/OSS Vetting Reports/2026-05-30 che-ical-mcp.md
#   - Kill-switch: same vetting report, Override section.

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from cal.base import (
    CalendarEvent,
    CalendarRef,
    CalendarWriter,
    ConflictReport,
    EventResult,
)

# Default binary location. Override via CHE_ICAL_PATH env var for corporate-port
# (e.g., /opt/<company>/bin/CheICalMCP) or alternate install locations.
_ENV_PATH_VAR = "CHE_ICAL_PATH"
DEFAULT_BINARY_PATH = Path("~/bin/CheICalMCP").expanduser()

# Subprocess invocation timeout. che-ical is local-only; any call taking longer
# than this indicates a TCC dialog block or a hung child process.
_SUBPROCESS_TIMEOUT_SEC = 30


class EventKitError(RuntimeError):
    """Raised when the che-ical subprocess returns a non-zero exit code,
    times out, or returns malformed JSON; also when an unknown calendar_id
    cannot be resolved against the cache."""


class EventKitWriter(CalendarWriter):
    """che-ical-mcp subprocess wrapper. One subprocess invocation per tool call.

    Trade-off: subprocess startup adds ~50-100ms per call. Acceptable for the
    A6 use case (a few events per email scan). If batch volume grows beyond a
    few dozen events per second, refactor to MCP server mode (long-lived
    child process with stdio framing).

    Self-update is INTENTIONALLY never invoked from this writer. che-ical's
    --self-update flag exists in the binary but creates network egress to
    GitHub. Per OSS Install Triangle mitigations, updates are operator-
    initiated via a deliberate re-vetting cycle, not automatic.
    """

    def __init__(self, binary_path: Path | None = None) -> None:
        # Path resolution order: explicit arg → env var → default.
        if binary_path is not None:
            self.binary_path = Path(binary_path).expanduser()
        elif os.environ.get(_ENV_PATH_VAR):
            self.binary_path = Path(os.environ[_ENV_PATH_VAR]).expanduser()
        else:
            self.binary_path = DEFAULT_BINARY_PATH
        self._connected = False
        self._calendar_cache: dict[str, CalendarRef] = {}

    def connect(self) -> None:
        """Verify the binary exists and is executable. Defer permission and
        EventKit-handshake validation to the first tool call so connect()
        stays cheap and predictable."""
        if not self.binary_path.exists():
            raise EventKitError(
                f"che-ical binary not found at {self.binary_path}. "
                f"Install per OSS Install Triangle protocol "
                f"(joseph_vault/Agentic Toolkit/OSS Install Triangle.md). "
                f"Or override location via env var {_ENV_PATH_VAR}."
            )
        if not os.access(self.binary_path, os.X_OK):
            raise EventKitError(
                f"che-ical binary at {self.binary_path} is not executable. "
                f"Run: chmod +x {self.binary_path}"
            )
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        self._calendar_cache.clear()

    def list_calendars(self) -> list[CalendarRef]:
        """Fetch calendars from che-ical AND repopulate the UUID → CalendarRef
        cache. Subsequent _resolve_calendar calls use the cache."""
        raw = self._invoke("list_calendars", {})
        if not isinstance(raw, list):
            raise EventKitError(
                f"list_calendars returned non-list payload: {type(raw).__name__}"
            )
        refs: list[CalendarRef] = []
        for entry in raw:
            ref = CalendarRef(
                id=entry["id"],
                title=entry["title"],
                source=entry.get("source", "unknown"),
                writable=entry.get("allowsContentModifications", False),
            )
            refs.append(ref)
            self._calendar_cache[ref.id] = ref
        return refs

    def check_conflicts(self, event: CalendarEvent) -> ConflictReport:
        ref = self._resolve_calendar(event.calendar_id)
        args: dict[str, Any] = {
            "start_time": event.start.isoformat(),
            "end_time": event.end.isoformat(),
            "calendar_name": ref.title,
            "calendar_source": ref.source,
        }
        # NOTE (verification gate): che-ical's check_conflicts response shape
        # needs confirmation by a live call. Expected to be either a top-level
        # list of overlapping events or a dict envelope with a "conflicts" key.
        # Adjust the parse below once the live shape is observed.
        raw = self._invoke("check_conflicts", args)
        conflicts_raw = raw if isinstance(raw, list) else raw.get("conflicts", [])
        conflicts = [self._event_from_raw(c) for c in conflicts_raw]
        return ConflictReport(conflicts=conflicts)

    def create_event(self, event: CalendarEvent) -> EventResult:
        args = self._event_to_create_args(event)
        try:
            raw = self._invoke("create_event", args)
        except EventKitError as e:
            return EventResult(event_id=None, status="failed", error=str(e))
        event_id = raw.get("id") if isinstance(raw, dict) else None
        if not event_id:
            # NOTE (verification gate): exact response shape on success needs
            # live confirmation. che-ical may return {"id": "..."} or wrap it
            # in {"event": {"id": ...}} or similar. Adjust extraction here.
            return EventResult(
                event_id=None,
                status="failed",
                error=f"create_event returned no id; raw={raw!r}",
            )
        return EventResult(event_id=event_id, status="created")

    def update_event(self, event_id: str, event: CalendarEvent) -> EventResult:
        args = self._event_to_update_args(event)
        args["event_id"] = event_id
        try:
            raw = self._invoke("update_event", args)
        except EventKitError as e:
            return EventResult(event_id=event_id, status="failed", error=str(e))
        returned_id = raw.get("id", event_id) if isinstance(raw, dict) else event_id
        return EventResult(event_id=returned_id, status="updated")

    def delete_event(self, event_id: str) -> EventResult:
        try:
            self._invoke("delete_event", {"event_id": event_id})
        except EventKitError as e:
            return EventResult(event_id=event_id, status="failed", error=str(e))
        return EventResult(event_id=event_id, status="deleted")

    # --- internals ----------------------------------------------------------

    def _resolve_calendar(self, calendar_id: str) -> CalendarRef:
        """Translate a stable UUID to a CalendarRef (title + source) for
        che-ical's calendar_name + calendar_source parameters. Populates the
        cache on first miss by calling list_calendars."""
        if calendar_id not in self._calendar_cache:
            self.list_calendars()
        ref = self._calendar_cache.get(calendar_id)
        if ref is None:
            raise EventKitError(
                f"calendar_id {calendar_id!r} not found in EventKit. "
                f"Run list_calendars to see available calendars. "
                f"This usually means the calendar was renamed or removed."
            )
        return ref

    def _event_to_create_args(self, event: CalendarEvent) -> dict[str, Any]:
        """Translate CalendarEvent to che-ical create_event args. Per Server.swift
        schema, required fields are title, start_time, end_time, calendar_name."""
        ref = self._resolve_calendar(event.calendar_id)
        args: dict[str, Any] = {
            "title": event.title,
            "start_time": event.start.isoformat(),
            "end_time": event.end.isoformat(),
            "calendar_name": ref.title,
            "calendar_source": ref.source,
            "all_day": event.all_day,
        }
        if event.location:
            args["location"] = event.location
        if event.notes:
            args["notes"] = event.notes
        if event.url:
            args["url"] = event.url
        if event.attendees:
            args["attendees"] = event.attendees
        # confidence_score is runtime metadata; not sent to the backend.
        return args

    def _event_to_update_args(self, event: CalendarEvent) -> dict[str, Any]:
        """Translate CalendarEvent to che-ical update_event args. Only includes
        fields the operator actually wants to change; che-ical preserves
        unspecified fields. event_id is added by update_event caller."""
        args: dict[str, Any] = {
            "title": event.title,
            "start_time": event.start.isoformat(),
            "end_time": event.end.isoformat(),
            "all_day": event.all_day,
        }
        # For update, calendar_name is only needed if moving the event to a
        # different calendar. Including it preserves the move semantic; che-ical
        # treats same-calendar updates as no-ops on that field.
        ref = self._resolve_calendar(event.calendar_id)
        args["calendar_name"] = ref.title
        args["calendar_source"] = ref.source
        if event.location is not None:
            args["location"] = event.location
        if event.notes is not None:
            args["notes"] = event.notes
        if event.url is not None:
            args["url"] = event.url
        if event.attendees:
            args["attendees"] = event.attendees
        return args

    def _event_from_raw(self, raw: dict[str, Any]) -> CalendarEvent:
        """Inverse parse for tool responses (conflict lists, etc.). Handles the
        asymmetric is_all_day output field name vs the all_day input field."""
        return CalendarEvent(
            title=raw.get("title", ""),
            start=datetime.fromisoformat(raw.get("start_time") or raw["start_date"]),
            end=datetime.fromisoformat(raw.get("end_time") or raw["end_date"]),
            calendar_id=raw.get("calendar_id", ""),
            all_day=raw.get("is_all_day", False),
            location=raw.get("location"),
            notes=raw.get("notes"),
            url=raw.get("url"),
            attendees=raw.get("attendees", []),
        )

    def _invoke(self, tool: str, arguments: dict[str, Any]) -> Any:
        """One subprocess invocation. Returns parsed JSON or raises
        EventKitError. This is the single shell-out point — every tool call
        funnels through here."""
        if not self._connected:
            raise EventKitError("Call connect() before invoking tools.")
        payload = json.dumps({"tool": tool, "arguments": arguments})
        try:
            result = subprocess.run(
                [str(self.binary_path), "--cli"],
                input=payload,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_SEC,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise EventKitError(
                f"che-ical {tool} timed out after {_SUBPROCESS_TIMEOUT_SEC}s; "
                f"check for hung TCC dialog or zombie process"
            ) from e
        if result.returncode != 0:
            raise EventKitError(
                f"che-ical {tool} returned exit code {result.returncode}: "
                f"stderr={result.stderr.strip()[:500]}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise EventKitError(
                f"che-ical {tool} returned non-JSON stdout: "
                f"{result.stdout[:500]!r}"
            ) from e
