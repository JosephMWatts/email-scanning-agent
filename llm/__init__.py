# llm/ — the meeting-intent decide-step seam.
#
# The MeetingExtractor Protocol that agent.run_calendar depends on lives in
# agent.py (next to the runtime that consumes it). This package provides the
# concrete implementation the composition root (run_calendar_agent.py) injects.
#
# Public surface:
#   - ClaudeMeetingExtractor (concrete, anthropic SDK, forced tool-call)

from llm.claude_extractor import ClaudeMeetingExtractor

__all__ = ["ClaudeMeetingExtractor"]
