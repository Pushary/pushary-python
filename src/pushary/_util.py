"""Shared helpers for the two-call agent flow: a stable idempotency key and a
fail-closed approval check. Mirrors the TypeScript @pushary/server helpers.
"""

from __future__ import annotations

import hashlib
from typing import Optional, Sequence

_AFFIRMATIVE = frozenset(
    {"yes", "y", "approve", "approved", "ok", "okay", "confirm", "accept", "true"}
)


def deterministic_key(parts: Sequence[str]) -> str:
    """A collision-safe idempotency key that is STABLE across process restarts.

    Unlike Python's builtin hash(), this does not change between runs, so a
    retried create reuses the same key and never asks the same human twice. Pass
    the parts that make the ask unique (for example [external_id, step_id, question]).
    Parts are joined with a NUL separator so ["a", "bc"] and ["ab", "c"] never collide.
    """

    joined = "\x00".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:40]


def is_approved(status: str, type_: Optional[str], value: Optional[str]) -> bool:
    """Fail-closed approval check.

    A ``confirm`` decision is approved only on an affirmative answer. A
    ``select`` or ``input`` decision counts as approved once it is answered.
    Anything not answered (pending, expired, or cancelled) is not approved.
    """

    if status != "answered":
        return False
    if type_ and type_ != "confirm":
        return True
    return value is not None and value.strip().lower() in _AFFIRMATIVE
