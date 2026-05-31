# llm/claude_extractor.py — ClaudeMeetingExtractor.
#
# Concrete MeetingExtractor over the Anthropic SDK. One message per extract()
# call, forced tool-call for structured MeetingIntent output (no prose parsing),
# and prompt caching on the static system prompt so the extraction instructions
# are billed once per 5-minute window rather than once per message.
#
# Satisfies the MeetingExtractor Protocol in agent.py structurally — no
# inheritance. The composition root injects an instance into agent.run_calendar.

from __future__ import annotations

from datetime import datetime

import anthropic

from agent import ExtractionError, MeetingIntent
from mail.base import MailMessage

DEFAULT_MODEL = "claude-sonnet-4-6"

# Cap the body text sent to the model. Meeting details live near the top of an
# email; sending multi-megabyte newsletter HTML would only burn tokens.
_MAX_BODY_CHARS = 6000

_SYSTEM_PROMPT = (
    "You extract calendar meeting-intent from a single email. "
    "Decide whether the email proposes, confirms, or invites the recipient to a "
    "specific meeting with a determinable time. "
    "Marketing 'events', webinars the recipient has not registered for, and "
    "vague 'let's catch up sometime' messages are NOT meetings — set "
    "has_meeting=false for those. "
    "When has_meeting is true, return timezone-aware ISO 8601 start and end "
    "timestamps; if the email gives a start but no duration, assume 60 minutes. "
    "Set confidence_score to your calibrated probability (0.0-1.0) that this is "
    "a genuine, actionable meeting the recipient should have on their calendar. "
    "Always call the record_meeting_intent tool; never answer in prose."
)

_TOOL = {
    "name": "record_meeting_intent",
    "description": "Record the structured meeting-intent extracted from the email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "has_meeting": {
                "type": "boolean",
                "description": "True only for a genuine, time-determinable meeting.",
            },
            "title": {"type": ["string", "null"]},
            "start": {
                "type": ["string", "null"],
                "description": "Timezone-aware ISO 8601 start, or null.",
            },
            "end": {
                "type": ["string", "null"],
                "description": "Timezone-aware ISO 8601 end, or null.",
            },
            "all_day": {"type": "boolean"},
            "location": {"type": ["string", "null"]},
            "url": {
                "type": ["string", "null"],
                "description": "Meeting join URL (Zoom/Meet/Teams), or null.",
            },
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Attendee email addresses.",
            },
            "confidence_score": {
                "type": "number",
                "description": "Calibrated 0.0-1.0 meeting-intent confidence.",
            },
        },
        "required": ["has_meeting", "confidence_score"],
    },
}


class ClaudeMeetingExtractor:
    """Anthropic-backed MeetingExtractor.

    calendar_id is accepted for forward-compatibility (future availability-aware
    prompting) and is not sent to the model today. The Anthropic client reads
    ANTHROPIC_API_KEY from the environment unless one is injected (tests)."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        calendar_id: str | None = None,
        client: "anthropic.Anthropic | None" = None,
    ) -> None:
        self.model_id = model
        self._calendar_id = calendar_id
        self._client = client or anthropic.Anthropic()
        self._input_tokens = 0
        self._output_tokens = 0

    def usage(self) -> tuple[int, int]:
        """Cumulative (input_tokens, output_tokens) across every extract call,
        with cache-creation and cache-read input folded into the input total for
        honest billing accounting."""
        return (self._input_tokens, self._output_tokens)

    def extract(self, message: MailMessage) -> MeetingIntent:
        # Translate the SDK's curated transient faults into the neutral
        # ExtractionError so run_calendar can capture-don't-raise without
        # importing anthropic (criterion E1). Permanent faults propagate raw.
        try:
            response = self._client.messages.create(
                model=self.model_id,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "record_meeting_intent"},
                messages=[
                    {"role": "user", "content": self._render_email(message)}
                ],
            )
        except anthropic.APIConnectionError as e:
            # Network down, DNS failure, client-side timeout (APITimeoutError is
            # a subclass). No status_code; always recoverable next run.
            raise ExtractionError(
                f"transient API connection error: {e}"
            ) from e
        except anthropic.APIStatusError as e:
            # 429 rate-limited and any 5xx (500-599, which includes 529
            # overloaded) are recoverable next run, so capture for retry.
            # Everything else — 400 bad-request, 401/403 auth, 404, 422 — is a
            # config or code fault that propagates raw so one error aborts the
            # run loudly instead of marking every message failed. Matched by
            # numeric status_code, not class: OverloadedError (529) is not
            # exported at the anthropic top level in the pinned SDK.
            if e.status_code == 429 or e.status_code >= 500:
                raise ExtractionError(
                    f"transient API status {e.status_code}: {e}"
                ) from e
            raise
        # Usage is accumulated after a successful call and before parsing, so a
        # parse-time defect still bills the tokens the call really spent.
        self._accumulate_usage(response.usage)
        try:
            return self._intent_from_payload(self._tool_payload(response))
        except (RuntimeError, ValueError) as e:
            # The model returned no record_meeting_intent tool call, or a
            # timestamp datetime.fromisoformat can't parse. A single garbage
            # extraction is captured, not raised — same posture as a transient
            # fault, so one bad response doesn't abort the batch.
            raise ExtractionError(f"model-output defect: {e}") from e

    # --- internals ----------------------------------------------------------

    def _render_email(self, message: MailMessage) -> str:
        body = message.body_text[:_MAX_BODY_CHARS]
        return (
            f"From: {message.sender}\n"
            f"Subject: {message.subject}\n"
            f"Date: {message.date.isoformat()}\n\n"
            f"{body}"
        )

    def _accumulate_usage(self, usage) -> None:
        self._input_tokens += (
            usage.input_tokens
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        )
        self._output_tokens += usage.output_tokens

    @staticmethod
    def _tool_payload(response) -> dict:
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_meeting_intent":
                return block.input
        raise RuntimeError(
            "model did not return the record_meeting_intent tool call"
        )

    @staticmethod
    def _intent_from_payload(payload: dict) -> MeetingIntent:
        start = payload.get("start")
        end = payload.get("end")
        return MeetingIntent(
            has_meeting=bool(payload.get("has_meeting", False)),
            title=payload.get("title"),
            start=datetime.fromisoformat(start) if start else None,
            end=datetime.fromisoformat(end) if end else None,
            all_day=bool(payload.get("all_day", False)),
            location=payload.get("location"),
            url=payload.get("url"),
            attendees=payload.get("attendees") or [],
            confidence_score=float(payload.get("confidence_score", 0.0)),
        )
