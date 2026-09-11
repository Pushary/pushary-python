"""Pushary Python SDK.

Human-in-the-loop decisions for AI products: create a decision, ask a specific
end-user to approve it, and resume on their answer via webhook or poll. Zero
runtime dependencies, stdlib only.
"""

from __future__ import annotations

from ._types import (
    CancelDecisionResponse,
    CreateDecisionResponse,
    Decision,
    DecisionStatus,
    DecisionType,
    WebhookSecretResponse,
)
from ._util import deterministic_key, is_approved
from .client import PusharyServer
from .decisions import DecisionsResource
from .keys import KeysResource
from .errors import PusharyError
from .webhook import SIGNATURE_HEADER, parse_decision_callback, verify_webhook_signature

__version__ = "2.1.1"

__all__ = [
    "PusharyServer",
    "DecisionsResource",
    "KeysResource",
    "PusharyError",
    "verify_webhook_signature",
    "parse_decision_callback",
    "SIGNATURE_HEADER",
    "deterministic_key",
    "is_approved",
    "Decision",
    "DecisionType",
    "DecisionStatus",
    "CreateDecisionResponse",
    "CancelDecisionResponse",
    "WebhookSecretResponse",
    "__version__",
]
