"""Error type raised by the Pushary SDK."""

from __future__ import annotations

from typing import Any, Dict, Optional


class PusharyError(Exception):
    """Raised when the Pushary API returns a non-2xx response.

    Carries the HTTP ``status`` and the reason the server reported (its
    ``error`` or ``message`` field), so callers can classify the failure
    instead of parsing a bare status code.

    ``body`` is the parsed response, for the endpoints that answer with a
    machine-readable field beside the sentence. A permit refusal names which
    refusal it was, and "the authorization was already spent" and "the network
    was down" want opposite next moves from the caller.
    """

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body or {}

    def __repr__(self) -> str:
        return f"PusharyError(status={self.status!r}, message={self.message!r})"
