"""Decisions resource: create, poll, answer, and cancel human-in-the-loop
decisions, plus manage the webhook secret.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from ._util import is_approved

RequestFn = Callable[..., Dict[str, Any]]

# One long-poll window. The server caps GET ?wait at 55s, so ask() loops windows
# of this size until the decision resolves or the caller deadline passes.
_POLL_WINDOW_SECONDS = 50
_DEFAULT_ASK_TIMEOUT_SECONDS = 55.0

class DecisionsResource:
    """Ask a specific end-user to approve something, then resume on their answer.

    Obtain an instance from ``PusharyServer(...).decisions``. Every method
    returns the parsed JSON body as a ``dict`` and raises ``PusharyError`` on a
    non-2xx response.
    """

    def __init__(self, request: RequestFn) -> None:
        self._request = request

    def create(
        self,
        question: str,
        *,
        type: str = "confirm",
        options: Optional[List[str]] = None,
        external_id: Optional[str] = None,
        email: Optional[str] = None,
        callback_url: Optional[str] = None,
        agent_name: Optional[str] = None,
        context: Optional[str] = None,
        placeholder: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_target: Optional[str] = None,
        actor: Optional[str] = None,
        environment: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        presentation: Optional[Dict[str, Any]] = None,
        expires_in_seconds: Optional[int] = None,
        wait: Optional[bool] = None,
        timeout_seconds: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        powered_by: Optional[bool] = None,
        require_reachable: Optional[bool] = None,
        request_timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Create a decision and ask one end-user to approve it.

        Defaults to async (returns immediately with a decisionId); pass
        wait=True to block up to ~55s for a fast answer. Always pass
        idempotency_key so a retried call does not ask the same human twice.

        ``type`` is "confirm" (yes/no), "select" (needs ``options``), or "input"
        (free text). ``external_id`` targets the specific end-user to notify.
        ``email``, when set and Slack is connected, DMs that specific person instead
        of the shared channel. ``callback_url`` receives the signed webhook when the
        human answers.
        ``expires_in_seconds`` sets how long the decision stays open.
        ``timeout_seconds`` bounds the server-side wait when ``wait`` is True.
        ``require_reachable``, when True, makes this raise (HTTP 409) instead of
        opening a decision for an end-user with no enrolled device or push
        subscription, so an unreachable user fails fast rather than silently.

        Returns a dict with ``decisionId``, ``pollUrl``, ``decisionPageUrl``, the
        current ``status``, and (for a decision addressed to an end-user)
        ``reachable`` / ``reachableChannels`` / ``deviceCount``.
        """

        # Wire keys are camelCase even though the arguments are snake_case, and
        # only keys with a value are sent so unset options stay absent.
        body: Dict[str, Any] = {"question": question, "type": type}
        optional = {
            "options": options,
            "externalId": external_id,
            "email": email,
            "callbackUrl": callback_url,
            "agentName": agent_name,
            "context": context,
            "placeholder": placeholder,
            "toolName": tool_name,
            "toolTarget": tool_target,
            "actor": actor,
            "environment": environment,
            "parameters": parameters,
            "presentation": presentation,
            "expiresInSeconds": expires_in_seconds,
            "wait": wait,
            "timeoutSeconds": timeout_seconds,
            "idempotencyKey": idempotency_key,
            "poweredBy": powered_by,
            "requireReachable": require_reachable,
        }
        for key, value in optional.items():
            if value is not None:
                body[key] = value
        return self._request("POST", "/decisions", body=body, **({"timeout_seconds": request_timeout_seconds} if request_timeout_seconds is not None else {}))

    def ask(
        self,
        question: str,
        *,
        type: str = "confirm",
        options: Optional[List[str]] = None,
        external_id: Optional[str] = None,
        email: Optional[str] = None,
        agent_name: Optional[str] = None,
        context: Optional[str] = None,
        placeholder: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_target: Optional[str] = None,
        actor: Optional[str] = None,
        environment: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        presentation: Optional[Dict[str, Any]] = None,
        expires_in_seconds: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        powered_by: Optional[bool] = None,
        callback_url: Optional[str] = None,
        require_reachable: Optional[bool] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Create a decision and block until the human answers or the deadline passes.

        This is the one call most agents need. It creates the decision and polls
        durably until the person answers or ``timeout_seconds`` (default 55,
        serverless-safe) elapses. The decision stays answerable for its full
        lifetime, so an ``{"answered": False}`` return can still be resolved later
        via ``get`` or a ``callback_url``.

        Idempotency is caller intent, never inferred from content: without an
        ``idempotency_key`` a fresh unique key is used per call, so two asks with
        the same text never collapse into one silent auto-approval. Pass your own
        stable ``idempotency_key`` (tied to your operation id) only when you WANT a
        retried call to dedupe.

        Returns a dict with ``decisionId``, ``status``, ``answered``, ``value``,
        ``type``, and a fail-closed ``approved`` flag (true only when answered and,
        for a confirm, affirmative).
        """

        budget = max(0.0, _DEFAULT_ASK_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds))
        deadline = time.monotonic() + budget
        key = idempotency_key or uuid.uuid4().hex
        created = self.create(
            question,
            type=type,
            options=options,
            external_id=external_id,
            email=email,
            agent_name=agent_name,
            context=context,
            placeholder=placeholder,
            tool_name=tool_name,
            tool_target=tool_target,
            actor=actor,
            environment=environment,
            parameters=parameters,
            presentation=presentation,
            expires_in_seconds=expires_in_seconds,
            powered_by=powered_by,
            callback_url=callback_url,
            require_reachable=require_reachable,
            idempotency_key=key,
            wait=False,
            request_timeout_seconds=budget or 65.0,
        )

        decision_id = created.get("decisionId")
        status = created.get("status", "pending")
        answered = bool(created.get("answered"))
        value = created.get("value")
        resolved_type = created.get("type") or type

        while status == "pending" and decision_id and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            wait_seconds = max(1, min(_POLL_WINDOW_SECONDS, int(remaining) + 1))
            try:
                polled = self.get(decision_id, wait=wait_seconds, request_timeout_seconds=remaining)
            except TimeoutError:
                break
            status = polled.get("status", status)
            answered = bool(polled.get("answered"))
            value = polled.get("value")
            resolved_type = polled.get("type") or resolved_type

        return {
            "decisionId": decision_id,
            "status": status,
            "answered": answered,
            "value": value,
            "type": resolved_type,
            "approved": is_approved(status, resolved_type, value),
            # Surfaced from the create response so a caller can tell an unanswered
            # decision that reached nobody (reachable=False) from a real decline.
            "reachable": created.get("reachable"),
            "reachableChannels": created.get("reachableChannels"),
            "deviceCount": created.get("deviceCount"),
        }

    def get(self, decision_id: str, *, wait: Optional[int] = None, request_timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Fetch the current state of a decision.

        Pass wait=N to long-poll for up to N seconds (capped server-side around
        55s) so the call returns as soon as the human answers instead of on the
        next poll. Returns a dict with ``status``, ``answered``, and ``value``.
        """

        params = {"wait": wait} if wait is not None else None
        return self._request("GET", f"/decisions/{_seg(decision_id)}", params=params, **({"timeout_seconds": request_timeout_seconds} if request_timeout_seconds is not None else {}))

    def answer(self, decision_id: str, answer: str) -> Dict[str, Any]:
        """Record an answer for a decision on the human's behalf.

        Useful when your own surface (not the Pushary decision page) collected
        the answer. Any waiting ``get`` or ``create`` call resolves immediately.
        """

        return self._request("POST", f"/decisions/{_seg(decision_id)}", body={"answer": answer})

    def cancel(self, decision_id: str) -> Dict[str, Any]:
        """Cancel a still-open decision so it can no longer be answered."""

        return self._request("DELETE", f"/decisions/{_seg(decision_id)}")

    def get_webhook_secret(self) -> Dict[str, Any]:
        """Return the current webhook secret used to sign decision callbacks."""

        return self._request("GET", "/webhook-secret")

    def rotate_webhook_secret(self) -> Dict[str, Any]:
        """Rotate the webhook secret and return the new value.

        The previous secret stops verifying once rotated, so update every
        receiver before you rely on the new one.
        """

        return self._request("POST", "/webhook-secret")


def _seg(value: str) -> str:
    """Percent-encode a single path segment so an id cannot alter the route."""

    return quote(str(value), safe="")
