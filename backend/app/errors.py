"""Domain errors. The API layer maps these to HTTP codes; the MCP layer turns
them into readable tool errors so an agent can correct itself."""

from __future__ import annotations


class RoomError(Exception):
    """Base class for expected, user-correctable failures."""

    status_code = 400
    code = "room_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFound(RoomError):
    status_code = 404
    code = "not_found"


class Forbidden(RoomError):
    status_code = 403
    code = "forbidden"


class RoomExpired(RoomError):
    status_code = 409
    code = "room_expired"


class GuardrailBlocked(RoomError):
    """A safeguard (turn budget, cooldown, consecutive-turn cap) refused the action."""

    status_code = 429
    code = "guardrail_blocked"


class ConfigError(RoomError):
    status_code = 503
    code = "not_configured"
