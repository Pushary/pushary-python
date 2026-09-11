"""Webhook signature verification.

When a human answers a decision, Pushary can POST the result to your
``callback_url``. The request carries an ``X-Pushary-Signature`` header, which is
an HMAC-SHA256 hex digest computed over the raw request body using your webhook
secret. Verify it before you trust the payload.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict, Optional, Union

SIGNATURE_HEADER = "X-Pushary-Signature"
"""Name of the header that carries the signature on inbound webhooks."""


def _to_bytes(value: Union[str, bytes]) -> bytes:
    """Encode a value to bytes with utf-8, leaving bytes untouched."""

    if isinstance(value, bytes):
        return value
    return value.encode("utf-8")


def verify_webhook_signature(
    raw_body: Union[str, bytes],
    signature: Optional[str],
    secret: str,
) -> bool:
    """Return True only if ``signature`` is a valid signature for ``raw_body``.

    Compute ``hmac.new(secret, raw_body, sha256).hexdigest()`` and compare it to
    the ``X-Pushary-Signature`` header in constant time. ``raw_body`` must be the
    exact bytes you received, not a re-serialized copy, because any change to the
    JSON (key order, spacing) changes the digest.

    Returns False when either ``signature`` or ``secret`` is empty, or when the
    lengths do not match, before doing the constant-time compare.
    """

    if not signature or not secret:
        return False

    expected = hmac.new(_to_bytes(secret), _to_bytes(raw_body), hashlib.sha256).hexdigest()
    expected_bytes = expected.encode("ascii")
    provided_bytes = _to_bytes(signature)

    if len(expected_bytes) != len(provided_bytes):
        return False

    return hmac.compare_digest(expected_bytes, provided_bytes)


def parse_decision_callback(raw_body: Union[str, bytes]) -> Optional[Dict[str, Any]]:
    """Parse a verified decision callback body into a dict, or None if it is not one.

    Verify the signature FIRST with ``verify_webhook_signature`` -- this does no
    verification. Read ``answer`` (canonical; ``value`` is an alias) keyed by
    ``correlationId``; ``context`` echoes what you passed at create time, so a
    stateless resume can read your own state off it without a correlationId map.
    """

    try:
        parsed = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    correlation_id = parsed.get("correlationId")
    answer = parsed.get("answer")
    if not isinstance(correlation_id, str) or not isinstance(answer, str):
        return None
    value = parsed.get("value")
    answered_at = parsed.get("answeredAt")
    result: Dict[str, Any] = {
        "correlationId": correlation_id,
        "answer": answer,
        "value": value if isinstance(value, str) else answer,
        "answeredAt": answered_at if isinstance(answered_at, str) else "",
    }
    context = parsed.get("context")
    if isinstance(context, str):
        result["context"] = context
    return result
