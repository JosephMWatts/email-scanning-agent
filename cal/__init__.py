# cal/__init__.py — the calendar-write seam.
#
# Folder named `cal/` rather than `calendar/` to avoid shadowing Python's stdlib
# `calendar` module. The seam role is calendar-write; the short name preserves
# Joseph's terse seam convention (mail/, vault/) while sidestepping the shadow.
#
# Public surface:
#   - CalendarWriter (abstract base class)
#   - CalendarEvent, CalendarRef, EventResult, ConflictReport (dataclasses)
#   - EventKitWriter (concrete, personal-lane Cadillac, macOS via che-ical-mcp)
#   - GoogleAPIWriter (stub, corporate-port / headless / cross-platform)
#   - MicrosoftGraphWriter (stub, corporate-port for Exchange Online)
