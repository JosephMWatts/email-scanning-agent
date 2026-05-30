# cal/google_api_writer.py — GoogleAPIWriter.
#
# Stub. Build when the deployment context requires it.
#
# Why stubbed today: A6 personal lane uses EventKitWriter to write Google
# calendars through the macOS Calendar.app CalDAV bridge. GoogleAPIWriter is
# the corporate-port / headless / cross-platform escape hatch — built when
# the deployment context lacks macOS (Linux server, corporate Workspace
# replica, container runtime).
#
# Reference: joseph_vault/Development Program/Office Assistant Fleet Roadmap.md
# (Phase 1 — transport-keyed writers, 2026-05-30 Saturday build session).

from __future__ import annotations

from cal.base import (
    CalendarEvent,
    CalendarRef,
    CalendarWriter,
    ConflictReport,
    EventResult,
)


class GoogleAPIWriter(CalendarWriter):
    """Stub. Implementation path (when needed):

    Dependencies:
      - `google-api-python-client` (the Google REST client)
      - `google-auth-oauthlib` (OAuth 2.0 flow helpers)

    Auth:
      - Personal-lane port: OAuth 2.0 installed-app flow with
        `https://www.googleapis.com/auth/calendar` scope.
        Token cached at `~/.config/email-scanning-agent/google_oauth.json`
        or via the `keyring` library for keychain storage.
      - Corporate-port: service account with delegated domain-wide authority,
        OR app-only OAuth with admin consent. Token storage via corporate
        secret manager (AWS Secrets Manager, GCP Secret Manager, HashiCorp
        Vault, or Azure Key Vault).

    Calendar ID mapping:
      - Google Calendar API uses 'primary' for the primary calendar, or the
        full email address for shared/secondary calendars.

    Conflict checking:
      - events.list with timeMin/timeMax and singleEvents=True for recurring
        expansion. Google's recurring-event semantics differ from EventKit's;
        translate carefully.

    Endpoints:
      - GET /users/me/calendarList for list_calendars
      - GET /calendars/{calendarId}/events?timeMin=&timeMax= for check_conflicts
      - POST /calendars/{calendarId}/events for create_event
      - PATCH /calendars/{calendarId}/events/{eventId} for update_event
      - DELETE /calendars/{calendarId}/events/{eventId} for delete_event
    """

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "GoogleAPIWriter is a stub. See module docstring for implementation "
            "path. Personal-lane A6 uses EventKitWriter via the macOS "
            "Calendar.app CalDAV bridge to reach Google calendars."
        )

    def connect(self) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def list_calendars(self) -> list[CalendarRef]:
        raise NotImplementedError

    def check_conflicts(self, event: CalendarEvent) -> ConflictReport:
        raise NotImplementedError

    def create_event(self, event: CalendarEvent) -> EventResult:
        raise NotImplementedError

    def update_event(self, event_id: str, event: CalendarEvent) -> EventResult:
        raise NotImplementedError

    def delete_event(self, event_id: str) -> EventResult:
        raise NotImplementedError
