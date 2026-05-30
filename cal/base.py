# cal/base.py — the calendar-write contract. No backend logic here.
#
# Three-writer seam, keyed by TRANSPORT mechanism (not by destination service):
#
#   - EventKitWriter (eventkit_writer.py) — macOS-native via the vendored
#     che-ical-mcp binary called as a subprocess. Handles every account
#     macOS Calendar.app sees: iCloud, Google (via CalDAV bridge), Exchange
#     (via CalDAV bridge), subscribed calendars. Personal-lane Cadillac.
#
#   - GoogleAPIWriter (google_api_writer.py) — cross-platform via
#     google-api-python-client. Stub today. Implementation path documented
#     for headless / Linux / corporate-port contexts where EventKit is
#     unavailable (i.e., not macOS).
#
#   - MicrosoftGraphWriter (microsoft_graph_writer.py) — Microsoft Graph
#     REST API. Stub today. Implementation path documented for Exchange
#     Online corporate contexts where EventKit is unavailable.
#
# Why transport-keyed rather than service-keyed: EventKit on macOS bridges
# iCloud + Google + Exchange + any CalDAV source into one local API, so a
# service-keyed seam (ICloudWriter / GoogleCalendarWriter / OutlookWriter)
# would force redundant per-service writers when one macOS-native writer
# already handles all three. The corporate-port / headless contexts need
# separate per-service writers because EventKit isn't available there.
# Transport-keyed cleanly splits the personal-lane shortcut from the
# corporate-port escape hatches. See:
#   joseph_vault/Development Program/Office Assistant Fleet Roadmap.md
#
# Forward-compatibility note (Roadmap Phase 2 — Wshobson Automated Sync):
# This interface is one source-of-truth surface the future cross-CLI
# Automated Sync Script will consume to generate per-CLI documentation
# artifacts. Keep method signatures, docstrings, and dataclass shapes
# stable; treat changes here the same way you treat changes to harness.py.

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class CalendarRef:
    """Identifies a target calendar within a writer's source domain.

    For EventKitWriter, id is the EventKit calendar identifier (UUID).
    For GoogleAPIWriter, id is the Google Calendar ID ('primary' or email).
    For MicrosoftGraphWriter, id is the Microsoft Graph calendar id.
    """
    id: str
    title: str          # human-readable name, e.g., "Joe Personal"
    source: str         # writer-specific source label, e.g., "iCloud", "Google"
    writable: bool      # True if writes are accepted; subscription/holiday calendars are False


@dataclass
class CalendarEvent:
    """Transport-agnostic event shape. Writers translate to their backend's schema.

    calendar_id is the stable UUID returned by the writer's list_calendars().
    Writers handle UUID-to-backend-identifier translation internally
    (EventKitWriter caches UUID → name + source for che-ical's calendar_name
    parameter; other writers use their backend's native ID format).
    """
    title: str
    start: datetime              # timezone-aware
    end: datetime                # timezone-aware
    calendar_id: str             # stable UUID from list_calendars()
    all_day: bool = False        # True for all-day events; start/end still passed, flag drives semantic
    location: Optional[str] = None
    notes: Optional[str] = None
    url: Optional[str] = None    # meeting URL (Zoom, Meet, Teams) — rendered as clickable join link
    attendees: list[str] = field(default_factory=list)   # email addresses
    confidence_score: Optional[float] = None             # runtime metadata: LLM's confidence in the
                                                         # meeting-intent extraction (0.0-1.0). Writers
                                                         # ignore this; runtime uses it for auto-create
                                                         # vs propose-only decisions.
    source_email_id: Optional[str] = None                # provenance: the email that triggered this event
    source_email_subject: Optional[str] = None           # provenance: subject line


@dataclass
class EventResult:
    """Writer return type. event_id is the backend-assigned identifier for the
    created or updated event; None on failure (with error set)."""
    event_id: Optional[str]
    status: str                  # "created", "updated", "deleted", "failed"
    error: Optional[str] = None


@dataclass
class ConflictReport:
    """Returned by check_conflicts. Empty conflicts list means no conflict."""
    conflicts: list[CalendarEvent]


class CalendarWriter(ABC):
    """Abstract contract every concrete calendar writer implements.

    Three concrete implementations live next to this file, distinguished by
    TRANSPORT (not by destination service). See module docstring for the
    transport-keyed-vs-service-keyed rationale.

    Lifecycle: connect() once at runtime start, then any number of
    list_calendars / check_conflicts / create_event / update_event /
    delete_event calls, then disconnect() at runtime shutdown.
    """

    @abstractmethod
    def connect(self) -> None:
        """Open and authenticate. Verify the backend is reachable and the
        configured calendars exist. Fail loud if not. Idempotent — safe to
        call multiple times."""

    @abstractmethod
    def list_calendars(self) -> list[CalendarRef]:
        """Return all calendars the writer can see. The runtime uses this to
        validate calendar_id targets and to surface choices to the operator."""

    @abstractmethod
    def check_conflicts(self, event: CalendarEvent) -> ConflictReport:
        """Look for existing events that overlap event.start..event.end on the
        target calendar. Returns ConflictReport with the overlapping events
        (empty list means clear). The runtime owns the conflict-decision
        policy; this method only surfaces conflicts."""

    @abstractmethod
    def create_event(self, event: CalendarEvent) -> EventResult:
        """Create the event in the target backend. Returns EventResult with the
        backend-assigned event_id on success; with error set and event_id=None
        on failure. Does NOT call check_conflicts internally."""

    @abstractmethod
    def update_event(self, event_id: str, event: CalendarEvent) -> EventResult:
        """Replace an existing event by id. EventResult.status == 'updated' on
        success. Used when the runtime detects a meeting-intent revision in a
        follow-up email and updates rather than recreates."""

    @abstractmethod
    def delete_event(self, event_id: str) -> EventResult:
        """Delete an event by id. EventResult.status == 'deleted' on success."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release handles. Subprocess-backed writers have nothing to close;
        OAuth-token writers may flush state."""
