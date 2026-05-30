# cal/microsoft_graph_writer.py — MicrosoftGraphWriter.
#
# Stub. Build when the deployment context requires it.
#
# Why stubbed today: A6 personal lane uses EventKitWriter to write Exchange
# calendars through the macOS Calendar.app bridge IF the operator has an
# Exchange account configured there. MicrosoftGraphWriter is the corporate-
# port / headless escape hatch — built when the deployment context lacks
# macOS (Linux server, container runtime) AND the corporate calendar backend
# is Exchange Online or Office 365.
#
# Alternative integration path worth evaluating before custom REST work:
# Joseph's plugin registry already includes `plugin:productivity:ms365`
# which exposes Microsoft 365 via MCP. A future MicrosoftGraphWriter could
# call that MCP server via subprocess rather than implementing direct REST,
# mirroring the EventKitWriter → che-ical pattern.
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


class MicrosoftGraphWriter(CalendarWriter):
    """Stub. Implementation path (when needed):

    Dependencies (REST-direct path):
      - `msal` (Microsoft Authentication Library)
      - `requests` (or `httpx`) for REST calls
      - OR `microsoft-kiota-abstractions` + the generated Graph SDK

    Dependencies (MCP-shim path):
      - Existing `plugin:productivity:ms365` MCP server, invoked via
        subprocess analogous to EventKitWriter → che-ical.

    Auth:
      - Personal-lane port: OAuth 2.0 with `Calendars.ReadWrite` scope
        minimum; `Calendars.ReadWrite.Shared` for shared/delegated calendars.
        Token storage via `msal` SerializableTokenCache.
      - Corporate-port: app-only auth (service principal) requires Azure AD
        admin consent for `Calendars.ReadWrite` application permission. Token
        storage via corporate secret manager.

    Endpoints (REST-direct):
      - GET /me/calendars for list_calendars
      - POST /me/calendar/getSchedule for check_conflicts
      - POST /me/events for create_event
      - PATCH /me/events/{id} for update_event
      - DELETE /me/events/{id} for delete_event

    Calendar ID mapping:
      - Graph uses opaque calendar IDs. Resolve by displayName at startup
        and cache the mapping for the session.

    Tenant admin gate:
      - For corporate-port, the tenant admin must enable the API permission
        scopes in Azure AD before this writer functions. Document this
        prerequisite in the corporate provisioning runbook.
    """

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "MicrosoftGraphWriter is a stub. See module docstring for "
            "implementation path. Personal-lane A6 uses EventKitWriter via "
            "macOS Calendar.app if an Exchange account is configured there."
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
