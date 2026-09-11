"""Typed shapes for the decision payloads.

The client methods return plain ``dict`` objects parsed straight from the API
response. These ``TypedDict`` definitions describe the keys those dicts carry so
editors and type checkers can help, without forcing any runtime conversion.
"""

from __future__ import annotations

from typing import List, Optional

try:  # TypedDict and Literal live in typing on Python 3.9+
    from typing import Literal, TypedDict
except ImportError:  # pragma: no cover - defensive for very old runtimes
    from typing_extensions import Literal, TypedDict  # type: ignore


DecisionType = Literal["confirm", "select", "input"]
"""How the human answers: a yes/no confirm, a fixed set of options, or free text."""

DecisionStatus = Literal["pending", "answered", "expired", "cancelled"]
"""Lifecycle state of a decision."""


class CreateDecisionResponse(TypedDict, total=False):
    """Shape returned by ``decisions.create``.

    Without ``wait`` the API returns immediately with ``status`` "pending" and
    ``answered`` False. With ``wait`` it may already carry the resolved
    ``value`` when the human answered inside the window, or a ``hint`` telling
    you to poll ``pollUrl`` if it did not.
    """

    decisionId: str
    question: str
    type: DecisionType
    decisionPageUrl: str
    pollUrl: str
    expiresInSeconds: int
    status: DecisionStatus
    answered: bool
    value: Optional[str]
    hint: str


class Decision(TypedDict, total=False):
    """Shape returned by ``decisions.get``, the durable view of one decision."""

    decisionId: str
    status: DecisionStatus
    answered: bool
    value: Optional[str]
    type: DecisionType
    question: str
    options: Optional[List[str]]
    externalId: Optional[str]
    createdAt: str
    answeredAt: Optional[str]
    expiresAt: Optional[str]


class CancelDecisionResponse(TypedDict, total=False):
    """Shape returned by ``decisions.cancel``."""

    decisionId: str
    cancelled: bool
    status: DecisionStatus


class WebhookSecretResponse(TypedDict, total=False):
    """Shape returned by ``decisions.get_webhook_secret`` and
    ``decisions.rotate_webhook_secret``.
    """

    secret: str
