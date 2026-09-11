"""The Pushary server client.

Zero dependencies: the transport is stdlib ``urllib.request`` and JSON is parsed
with stdlib ``json``. Construct once with your full API key and reuse the frozen
instance across calls.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode

from .decisions import DecisionsResource
from .keys import KeysResource
from .errors import PusharyError

DEFAULT_BASE_URL = "https://pushary.com/api/v1/server"

# The create call and get(wait=N) can hold the connection open while a human
# answers. The server caps that wait around 55s, so the socket timeout sits a
# little above it to leave room for the round trip without hanging forever.
DEFAULT_TIMEOUT_SECONDS = 65.0

_SETTINGS_URL = "https://pushary.com/dashboard/settings"


class PusharyServer:
    """Client for the Pushary human-in-the-loop decisions API.

    Args:
        api_key: The full API key in the form ``pk_xxx.sk_xxx``.
        base_url: Override the API base URL. Defaults to the public endpoint.

    Raises:
        ValueError: If the API key is empty or is missing the secret half.
    """

    def __init__(self, api_key: str, base_url: Optional[str] = None) -> None:
        self._validate_api_key(api_key)

        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        self._timeout = DEFAULT_TIMEOUT_SECONDS
        self.decisions = DecisionsResource(self._request)
        self.keys = KeysResource(self._request)

    def evaluate_authorization(
        self,
        tool_name: str,
        *,
        tool_target: Optional[str] = None,
        actor: Optional[str] = None,
        environment: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        external_id: Optional[str] = None,
        question: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """What the site's policy says about one action, with nobody paged.

        The raw boundary behind :meth:`authorize`. Returns ``verdict``
        ("allow", "deny" or "requires_human"), ``policy``, ``reason`` and
        ``authorizationId``. No decision is opened, so a caller that owns its own
        escalation can tell "policy deferred" apart from "the ask failed", which
        :meth:`authorize` folds together. Requires the Partner plan.
        """

        # Built once and sent to both this call and any ask that follows, so the
        # evaluation and the decision describe the same action.
        subject: Dict[str, Any] = {
            "toolName": tool_name,
            "toolTarget": tool_target,
            "actor": actor,
            "environment": environment,
            "parameters": parameters,
            "externalId": external_id,
            "question": question,
            "agentName": agent_name,
        }
        body = {key: value for key, value in subject.items() if value is not None}
        return self._request("POST", "/authorize", body=body)

    def authorize(
        self,
        tool_name: str,
        *,
        tool_target: Optional[str] = None,
        actor: Optional[str] = None,
        environment: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        external_id: Optional[str] = None,
        question: Optional[str] = None,
        agent_name: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        expires_in_seconds: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        require_reachable: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Ask policy whether one action may proceed, and a person only if policy defers.

        ``allow`` and ``deny`` come back without anyone being paged. A rule has to
        NAME the action for that to happen: the all-agents "*" rule is never
        selected here, so a wildcard written for a team's own coding agents cannot
        silently authorize a business action. Anything no rule names goes to a
        person.

        Returns a dict with ``approved`` (fail-closed: False for a denial and for
        an unanswered ask), ``resolvedBy`` ("policy" or "human"), ``policy``,
        ``reason``, ``decisionId`` and ``authorizationId``. Requires the Partner
        plan.
        """

        evaluation = self.evaluate_authorization(
            tool_name,
            tool_target=tool_target,
            actor=actor,
            environment=environment,
            parameters=parameters,
            external_id=external_id,
            question=question,
            agent_name=agent_name,
        )
        verdict = evaluation.get("verdict")

        if verdict != "requires_human":
            return {
                "approved": verdict == "allow",
                "resolvedBy": "policy",
                "policy": evaluation.get("policy"),
                "reason": evaluation.get("reason"),
                "decisionId": None,
                "authorizationId": evaluation.get("authorizationId"),
            }

        target = f" {tool_target}" if tool_target else ""
        answer = self.decisions.ask(
            question or f"Approve {tool_name}{target}?",
            type="confirm",
            external_id=external_id,
            agent_name=agent_name,
            tool_name=tool_name,
            tool_target=tool_target,
            actor=actor,
            environment=environment,
            parameters=parameters,
            expires_in_seconds=expires_in_seconds,
            idempotency_key=idempotency_key,
            require_reachable=require_reachable,
            timeout_seconds=timeout_seconds,
        )
        approved = bool(answer.get("approved"))
        if approved:
            reason = "A person approved it."
        elif answer.get("answered"):
            reason = "A person denied it."
        else:
            reason = "Nobody answered, so this was not approved."
        return {
            "approved": approved,
            "resolvedBy": "human",
            "policy": evaluation.get("policy"),
            "reason": reason,
            "decisionId": answer.get("decisionId"),
            "authorizationId": None,
        }

    def enroll(self, external_id: str) -> Dict[str, Any]:
        """Connect one of your end-users' phones (keyless, no account for them).

        Returns a dict with a single-use ``universalLink`` to show the user; one
        tap turns on approvals. Call once per end-user and cache the enrollment,
        not the link, which is single-use and expires. Requires the Partner plan.
        """

        return self._request("POST", "/enroll", body={"externalId": external_id})

    def consume_authorization(
        self,
        authorization_id: str,
        tool_name: str,
        *,
        tool_target: Optional[str] = None,
        actor: Optional[str] = None,
        environment: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        external_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Spend one settled authorization, at most once.

        Re-states the action about to run. That is the point: the server digests
        what you claim and what it recorded, and refuses unless they are the same
        action. An amount changed between the approval and the execution does not
        fail a check that could be skipped — it simply has no permit.

        Returns ``permitId``, ``authority`` and ``expiresAt``. Raises
        :class:`PusharyError` on a refusal, whose ``body["refusal"]`` names it.
        Requires the Partner plan.
        """

        subject: Dict[str, Any] = {
            "authorizationId": authorization_id,
            "toolName": tool_name,
            "toolTarget": tool_target,
            "actor": actor,
            "environment": environment,
            "parameters": parameters,
            "externalId": external_id,
        }
        body = {key: value for key, value in subject.items() if value is not None}
        return self._request("POST", "/authorizations/consume", body=body)

    def record_execution(
        self,
        permit_id: str,
        outcome: str,
        *,
        summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Say what the authorized action did. ``outcome`` is "succeeded" or "failed".

        The evidence half. "Was this allowed" and "did this happen" are different
        questions, and only the second answers a customer asking where their
        refund is. The first report wins; a late duplicate cannot rewrite it.
        """

        body: Dict[str, Any] = {"outcome": outcome}
        if summary is not None:
            body["summary"] = summary
        return self._request(
            "POST", f"/authorizations/{quote(permit_id, safe='')}/receipt", body=body
        )

    @staticmethod
    def _validate_api_key(api_key: str) -> None:
        if not api_key:
            raise ValueError(
                f"API key is required. Get your API key from {_SETTINGS_URL}"
            )
        if "." not in api_key:
            raise ValueError(
                "Invalid API key format. Use the full API key (pk_xxx.sk_xxx). "
                f"Get your API key from {_SETTINGS_URL}"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        url = self._base_url + path
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url = f"{url}?{urlencode(query)}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method, headers=dict(self._headers)
        )

        try:
            with urllib.request.urlopen(request, timeout=min(self._timeout, timeout_seconds) if timeout_seconds is not None else self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._error_from_http(exc) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise exc.reason from exc
            raise PusharyError(f"Request to Pushary failed: {exc.reason}") from exc

        return self._parse_json(raw)

    @staticmethod
    def _error_from_http(exc: urllib.error.HTTPError) -> PusharyError:
        status = exc.code
        message: Optional[str] = None
        body: Optional[Dict[str, Any]] = None
        try:
            parsed = json.loads(exc.read().decode("utf-8"))
            if isinstance(parsed, dict):
                body = parsed
                error = parsed.get("error")
                fallback = parsed.get("message")
                message = error if isinstance(error, str) else None
                if message is None and isinstance(fallback, str):
                    message = fallback
        except Exception:
            message = None
        return PusharyError(message or f"HTTP {status}", status=status, body=body)

    @staticmethod
    def _parse_json(raw: bytes) -> Dict[str, Any]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {"data": parsed}
