"""The shared kernel behind every Pushary framework adapter.

Imported as ``pushary.adapters`` so a new adapter is a thin binding — map the
framework's context onto these shapes, map the result back — instead of another
copy of the same ~150 lines. Nothing here imports a framework.

The public names mirror ``@pushary/server/adapters`` on the TypeScript side, in
Python's idiom, so a rule that holds in one language holds in the other.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Callable, Dict, List, Optional

from ._util import deterministic_key, is_approved
from .client import PusharyServer
from .errors import PusharyError
from .webhook import parse_decision_callback, verify_webhook_signature

__all__ = [
    "AdapterKernel",
    "ApprovalAsk",
    "ApprovalDecision",
    "Protect",
    "ProtectResult",
    "default_protect_question",
    "DENIED_BY_POLICY",
    "DENIED_REFUSED",
    "DENIED_UNANSWERED",
    "REFUSED_REPLAY",
    "UNBOUND_APPROVAL",
    "UNCONFIRMED_PERMIT",
    "denied_by_policy",
    "refused_permit",
    "derive_parameters",
    "decision_fingerprint",
    "describe_answer",
    "idempotency_key",
    "is_affirmative",
    "render_approval_question",
    "resolve_pushary_callback",
]

_DEFAULT_NODE = "ask-human"
_MAX_INPUT_CHARS = 300

DENIED_UNANSWERED = (
    "No answer from the approver, so this was denied. Do not retry the same action."
)
DENIED_REFUSED = "The approver denied this action."
DENIED_BY_POLICY = "Denied by policy. Do not retry the same action."

_PARAMETERS_MAX_KEYS = 32
_PARAMETER_KEY_MAX_LENGTH = 64
_PARAMETER_VALUE_MAX_LENGTH = 200


def denied_by_policy(evaluation: Dict[str, Any]) -> str:
    """A policy denial, as the model reads it.

    The server already words the reason ("Denied by policy rule refund.create.");
    the retry instruction is added here so every denial this gate returns ends the
    same way, whoever settled it.
    """

    reason = evaluation.get("reason")
    if isinstance(reason, str) and reason:
        return f"{reason} Do not retry the same action."
    return DENIED_BY_POLICY


def derive_parameters(tool_input: Any) -> Optional[Dict[str, Any]]:
    """Bounded facts for policy, derived from a tool's own input.

    All or nothing, deliberately. The bounds mirror what the API accepts, so a
    partial object is always representable, and that is exactly the danger: a rule
    reading ``amount >= 500`` stops denying the moment ``amount`` goes missing, so
    dropping one entry can only ever move a verdict toward allow. Sending nothing
    instead leaves the rule's parameter absent, which escalates to a person.

    The same bounds also mean this never turns a working tool call into a rejected
    one: an input this cannot carry is simply not sent.
    """

    if not isinstance(tool_input, dict):
        return None
    if not tool_input or len(tool_input) > _PARAMETERS_MAX_KEYS:
        return None

    parameters: Dict[str, Any] = {}
    for key, value in tool_input.items():
        if not isinstance(key, str) or not key or len(key) > _PARAMETER_KEY_MAX_LENGTH:
            return None
        # bool before int: bool is an int subclass, and a rule reading a flag and a
        # rule reading a threshold are not the same rule.
        if isinstance(value, bool):
            parameters[key] = value
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
                return None
            parameters[key] = value
        elif isinstance(value, str):
            if len(value) > _PARAMETER_VALUE_MAX_LENGTH:
                return None
            parameters[key] = value
        else:
            return None
    return parameters


def idempotency_key(external_id: str, node: str, question: str) -> str:
    """Fresh identity for an independent ask; never infer approval reuse from text."""
    return uuid.uuid4().hex


def _canonical_json(value: Any) -> str:
    def validate(item: Any) -> None:
        if item is None or type(item) in (str, bool, int, float):
            return
        if type(item) is list:
            for child in item:
                validate(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                validate(child)
            return
        raise ValueError("Approval identity requires JSON values and string object keys.")

    try:
        validate(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("Approval identity requires finite, non-circular JSON data.") from error


def _gate_key(ask: "ApprovalAsk", facts: Optional[Dict[str, Any]]) -> str:
    if not isinstance(ask.call_id, str) or not ask.call_id.strip():
        raise ValueError("Approval identity requires a nonempty tool call ID.")
    return decision_fingerprint({
        "version": 2,
        "session_id": ask.session_id,
        "call_id": ask.call_id,
        "tool_name": ask.tool_name,
        "external_id": ask.external_id,
        "tool_target": ask.tool_target,
        "actor": ask.actor,
        "environment": ask.environment,
        "question": ask.question,
        "input": ask.input,
        "parameters": facts,
        "presentation": ask.presentation,
    })


def decision_fingerprint(value: Any) -> str:
    """Stable JSON identity; rejects values that cannot identify an exact request."""
    return deterministic_key([_canonical_json(value)])


def is_affirmative(answer: Optional[str]) -> bool:
    """Fail-closed yes/no check for a confirm answer."""

    return is_approved("answered", "confirm", answer)


def describe_answer(type: str, result: Dict[str, Any]) -> str:
    """Turn a decision outcome into an unambiguous instruction for the model."""

    if not result.get("answered"):
        return (
            f"No answer (status: {result.get('status')}). "
            "Treat this as NOT approved and do not proceed."
        )
    if type == "confirm":
        return (
            "The human approved. You may proceed."
            if result.get("approved")
            else "The human declined. Do not proceed."
        )
    return f"The human answered: {result.get('value') or ''}"


def resolve_pushary_callback(
    raw_body: Any, signature: Optional[str], secret: str
) -> Optional[Dict[str, Any]]:
    """Verify a callback signature and parse it, or return None.

    The ``answer`` is what you feed back into the framework's resume seam.
    """

    if not verify_webhook_signature(raw_body, signature, secret):
        return None
    callback = parse_decision_callback(raw_body)
    if not callback:
        return None
    return {
        "correlationId": callback.get("correlationId"),
        "answer": callback.get("answer"),
        "value": callback.get("value"),
        "approved": is_affirmative(callback.get("answer")),
        "context": callback.get("context"),
        "answeredAt": callback.get("answeredAt"),
    }


def render_approval_question(tool_name: str, tool_input: Any) -> str:
    """The default question: the tool name plus a truncated view of its input.

    The approver sees what they are approving without a wall of JSON on a lock
    screen.
    """

    if tool_input is None:
        return f"Approve {tool_name}?"
    if isinstance(tool_input, str):
        rendered = tool_input
    else:
        try:
            rendered = json.dumps(tool_input, default=str)
        except (TypeError, ValueError):
            # A cycle, or something json cannot reach even via default=str. repr is
            # still more use to the approver than nothing, and a gate that cannot
            # render its input still has to ask.
            rendered = str(tool_input)
    summary = (
        f"{rendered[:_MAX_INPUT_CHARS]}..." if len(rendered) > _MAX_INPUT_CHARS else rendered
    )
    return f"Approve {tool_name}? {summary}"


class ApprovalAsk:
    """One gated tool call, resolved down to what Pushary needs.

    The adapter owns the mapping from its framework's context, so per-call
    resolvers stay typed against the framework's own shapes.
    """

    __slots__ = (
        "tool_name",
        "call_id",
        "session_id",
        "question",
        "external_id",
        "tool_target",
        "actor",
        "environment",
        "input",
        "parameters",
        "presentation",
    )

    def __init__(
        self,
        *,
        tool_name: str,
        call_id: str,
        session_id: str,
        question: str,
        external_id: str,
        tool_target: Optional[str] = None,
        actor: Optional[str] = None,
        environment: Optional[str] = None,
        input: Any = None,
        parameters: Optional[Dict[str, Any]] = None,
        presentation: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.tool_name = tool_name
        self.call_id = call_id
        self.session_id = session_id
        self.question = question
        self.external_id = external_id
        #: What the tool acts on, e.g. an order id. Narrows which rule governs.
        self.tool_target = tool_target
        #: Whose authority the action claims, e.g. ``user:u_44``. Not the approver.
        self.actor = actor
        #: Which deployment the action runs against, e.g. ``production``.
        self.environment = environment
        #: The tool's raw input. Bounded scalars are derived from it so a rule can
        #: decide on the arguments and not only on the name. Already rendered into
        #: the default question, so passing it exposes nothing new.
        self.input = input
        #: Explicit facts for policy, used instead of deriving them from ``input``.
        self.parameters = parameters
        self.presentation = presentation


class ApprovalDecision:
    """What a gate answers. A denial always carries a reason the model can read.

    An approval also carries ``authorization``: the id that settled it and the
    exact subject it was recorded against. Carried rather than rebuilt, because
    only the gate knows what it actually sent — failed legacy policy evaluation
    retains a minimal ask — and a caller that rebuilt it would send
    a subject the record does not hold and be refused for its own approval.
    ``None`` only from a server too old to name the authorization.
    """

    __slots__ = ("approved", "reason", "authorization")

    def __init__(
        self,
        approved: bool,
        reason: Optional[str] = None,
        authorization: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.approved = approved
        self.reason = reason
        self.authorization = authorization

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ApprovalDecision(approved={self.approved!r}, reason={self.reason!r})"


ApprovalGate = Callable[[ApprovalAsk], ApprovalDecision]
Protect = Callable[..., "ProtectResult"]


def _binding(
    authorization_id: Optional[str],
    ask: "ApprovalAsk",
    *,
    tool_target: Optional[str] = None,
    actor: Optional[str] = None,
    environment: Optional[str] = None,
    parameters: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """The subject an authorization was recorded against, or None.

    Only the fields that were actually sent. A field claimed here that the record
    does not hold makes the two describe different actions, and the consume
    refuses — for an action that really was approved.
    """

    if not authorization_id:
        return None
    binding: Dict[str, Any] = {
        "authorization_id": authorization_id,
        "tool_name": ask.tool_name,
        "external_id": ask.external_id,
        "tool_target": tool_target,
        "actor": actor,
        "environment": environment,
        "parameters": parameters,
    }
    return {key: value for key, value in binding.items() if value is not None}


UNBOUND_APPROVAL = (
    "This was approved, but the approval could not be bound to a single "
    "execution, so it was not run. Do not retry the same action."
)

UNCONFIRMED_PERMIT = (
    "This was approved, but it could not be confirmed as still unspent, so it "
    "was not run. Ask again rather than retrying this one."
)

REFUSED_REPLAY = (
    "This authorization was already used. The action has run once and must not "
    "run again."
)


def refused_permit(error: PusharyError) -> Optional[str]:
    """A permit refusal as the model reads it, or None if this was not a refusal.

    A refusal is an answer: the server looked, and said no. A transport error is
    neither, and must not be dressed up as one — a caller reading "already
    consumed" stops, and a caller reading a network error may ask again. Only a
    body the server actually sent is read as a refusal.
    """

    refusal = error.body.get("refusal")
    if refusal not in ("not_authorized", "action_mismatch", "expired", "already_consumed"):
        return None
    if refusal == "already_consumed":
        return REFUSED_REPLAY
    return f"{error.message} Do not retry the same action."


def default_protect_question(action: str, target: Optional[str] = None) -> str:
    """What the human is shown when the caller names no question.

    The action and its target, not a rendering of the facts: a protected action
    is written by hand, so its name is already business language
    ("refund.create order_4471") in a way a tool's raw input never is.
    """

    return f"Approve {action} {target}?" if target else f"Approve {action}?"


class ProtectResult:
    """What a protected action did.

    A value rather than a raised exception, because "policy said no" is an
    ordinary outcome an agent has to read and report. An error raised by the
    action itself is NOT caught: that is the caller's failure, about their side
    effect, and swallowing it here would turn a failed refund into a silent
    success.
    """

    __slots__ = ("ok", "result", "reason")

    def __init__(
        self, ok: bool, result: Any = None, reason: Optional[str] = None
    ) -> None:
        self.ok = ok
        self.result = result
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ProtectResult(ok={self.ok!r}, result={self.result!r}, reason={self.reason!r})"


class AdapterKernel:
    """The calls an adapter makes, bound to one framework's name.

    ``helpers_label`` names the adapter in the missing-key error, so the message
    points at the helpers the caller is actually using ("the LangGraph helpers").

    ```python
    kernel = AdapterKernel("the Acme helpers")
    ask_human = kernel.ask_human
    ```
    """

    __slots__ = ("_helpers_label",)

    def __init__(self, helpers_label: str) -> None:
        self._helpers_label = helpers_label

    def client(
        self, api_key: Optional[str] = None, base_url: Optional[str] = None
    ) -> PusharyServer:
        """Build a client from an explicit key or ``PUSHARY_API_KEY``."""

        key = api_key or os.environ.get("PUSHARY_API_KEY")
        if not key:
            raise ValueError(
                f"Pushary: set PUSHARY_API_KEY or pass api_key=... to {self._helpers_label}."
            )
        return PusharyServer(api_key=key, base_url=base_url)

    def require_external_id(self, external_id: Optional[str]) -> str:
        """The end-user to ask, or a clear error naming these helpers."""

        if not isinstance(external_id, str) or not external_id.strip():
            raise ValueError(
                f"Pushary: no end-user to ask. Pass external_id=... to {self._helpers_label}, "
                "or run user-scoped auth so the framework supplies one."
            )
        if len(external_id.encode("utf-16-le")) > 512:
            raise ValueError("Pushary: external_id must not exceed 256 UTF-16 characters.")
        return external_id

    def connect(
        self,
        external_id: str,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> str:
        """Connect one end-user's phone (keyless). Returns a single-use link."""

        return self.client(api_key, base_url).enroll(self.require_external_id(external_id))["universalLink"]

    def ask_human(
        self,
        question: str,
        *,
        external_id: str,
        idempotency_key: Optional[str] = None,
        type: str = "confirm",
        options: Optional[List[str]] = None,
        node: str = _DEFAULT_NODE,
        context: Optional[str] = None,
        agent_name: Optional[str] = None,
        placeholder: Optional[str] = None,
        tool_target: Optional[str] = None,
        actor: Optional[str] = None,
        environment: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        presentation: Optional[Dict[str, Any]] = None,
        expires_in_seconds: Optional[int] = None,
        require_reachable: Optional[bool] = None,
        callback_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Blocking ask: create a decision and poll durably until it resolves.

        Returns the decision dict (``decisionId``, ``status``, ``answered``,
        ``value``, ``type``, fail-closed ``approved``). Each call creates a fresh
        decision unless the caller supplies an operation-specific idempotency_key.
        """

        return self.client(api_key, base_url).decisions.ask(
            question,
            type=type,
            options=options,
            external_id=self.require_external_id(external_id),
            context=context,
            agent_name=agent_name,
            placeholder=placeholder,
            tool_target=tool_target,
            actor=actor,
            environment=environment,
            parameters=parameters,
            presentation=presentation,
            expires_in_seconds=expires_in_seconds,
            require_reachable=require_reachable,
            callback_url=callback_url,
            timeout_seconds=timeout_seconds,
            # The node/step name doubles as the action label. The default is not
            # sent: grouping every unnamed ask across every framework under one
            # bucket would mine into a suggestion to auto-approve "ask-human",
            # which spans unrelated questions.
            tool_name=node if node != _DEFAULT_NODE else None,
            idempotency_key=idempotency_key or uuid.uuid4().hex,
        )

    def create_durable_decision(
        self,
        question: str,
        *,
        external_id: str,
        idempotency_key: Optional[str] = None,
        callback_url: Optional[str] = None,
        type: str = "confirm",
        options: Optional[List[str]] = None,
        node: str = _DEFAULT_NODE,
        context: Optional[str] = None,
        agent_name: Optional[str] = None,
        placeholder: Optional[str] = None,
        tool_target: Optional[str] = None,
        actor: Optional[str] = None,
        environment: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        presentation: Optional[Dict[str, Any]] = None,
        expires_in_seconds: Optional[int] = None,
        require_reachable: Optional[bool] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Durable create: open a decision with a ``callback_url`` and return at once.

        For a framework that parks its run and resumes on the webhook. Requires
        an explicit idempotency_key tied to the run and step.
        """

        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("Pushary: durable decisions require an idempotency_key tied to the run and step.")
        created = self.client(api_key, base_url).decisions.create(
            question,
            type=type,
            options=options,
            external_id=self.require_external_id(external_id),
            context=context,
            agent_name=agent_name,
            placeholder=placeholder,
            tool_target=tool_target,
            actor=actor,
            environment=environment,
            parameters=parameters,
            presentation=presentation,
            expires_in_seconds=expires_in_seconds,
            require_reachable=require_reachable,
            callback_url=callback_url,
            tool_name=node if node != _DEFAULT_NODE else None,
            idempotency_key=idempotency_key,
            wait=False,
        )
        return {
            "decisionId": created.get("decisionId"),
            "correlationId": created.get("decisionId"),
            "status": created.get("status"),
            "reachable": created.get("reachable"),
            "reachableChannels": created.get("reachableChannels"),
            "deviceCount": created.get("deviceCount"),
        }

    def create_gate(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        agent_name: Optional[str] = None,
        expires_in_seconds: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        require_reachable: Optional[bool] = None,
        policy: bool = True,
    ) -> ApprovalGate:
        """Build a request-time approval gate.

        Asks the site's policy first and a person only when policy defers. The
        client is created here, so a missing key raises where the gate is defined
        rather than on the first tool call. Fails closed: anything short of an
        explicit yes denies.

        Set ``policy=False`` to restore the always-ask gate. A site whose rules
        were written for its own coding agents, and which happens to name a tool
        this gate also sits on, would otherwise start resolving it automatically.

        The verdict is the server's. Nothing here evaluates a rule, so the two
        languages cannot disagree about what a policy means.
        """

        gate_client = self.client(api_key, base_url)

        def evaluate(
            ask: ApprovalAsk, facts: Optional[Dict[str, Any]]
        ) -> Optional[Dict[str, Any]]:
            """The verdict, or None if there is no usable one.

            Every failure is None rather than a raise. This route is entitled to
            the Partner plan and is newer than the published adapters, so a caller
            that cannot reach it has to keep working exactly as it did before
            policy existed, which is by asking a person. None is never allow.
            """

            if not policy:
                return None
            try:
                return gate_client.evaluate_authorization(
                    ask.tool_name,
                    tool_target=ask.tool_target,
                    actor=ask.actor,
                    environment=ask.environment,
                    parameters=facts,
                    external_id=ask.external_id,
                    question=ask.question,
                    agent_name=agent_name,
                )
            except Exception:
                # Deliberately broad. The guarantee is that asking policy can never
                # break a call that worked before policy existed, and every way this
                # can fail ends at the same safe answer: ask a person.
                return None

        def gate(ask: ApprovalAsk) -> ApprovalDecision:
            # Derived once. The evaluation and the ask that may follow have to
            # describe the same action, and computing it twice is two chances for
            # them not to.
            facts = (
                ask.parameters
                if ask.parameters is not None
                else derive_parameters(ask.input)
            )
            self.require_external_id(ask.external_id)
            key = _gate_key(ask, facts)
            evaluation = evaluate(ask, facts)
            verdict = evaluation.get("verdict") if evaluation else None
            if verdict == "allow":
                authorization_id = (evaluation or {}).get("authorizationId")
                if not authorization_id:
                    return ApprovalDecision(True)
                # Exactly the subject POST /authorize recorded, which is the whole
                # of what it was sent.
                return ApprovalDecision(
                    True,
                    authorization=_binding(
                        authorization_id,
                        ask,
                        tool_target=ask.tool_target,
                        actor=ask.actor,
                        environment=ask.environment,
                        parameters=facts,
                    ),
                )
            if verdict == "deny":
                return ApprovalDecision(False, denied_by_policy(evaluation or {}))

            subject: Dict[str, Any] = {}
            if evaluation is not None or not policy:
                subject = {
                    "tool_target": ask.tool_target,
                    "actor": ask.actor,
                    "environment": ask.environment,
                    "parameters": facts,
                    "presentation": ask.presentation,
                }

            result = gate_client.decisions.ask(
                ask.question,
                type="confirm",
                external_id=ask.external_id,
                agent_name=agent_name,
                expires_in_seconds=expires_in_seconds,
                timeout_seconds=timeout_seconds,
                require_reachable=require_reachable,
                # The gated tool, recorded on the decision so it can be grouped
                # and matched later instead of surviving only as prose.
                tool_name=ask.tool_name,
                idempotency_key=key,
                **subject,
            )
            if result.get("approved"):
                return ApprovalDecision(
                    True,
                    authorization=_binding(
                        result.get("decisionId"),
                        ask,
                        tool_target=subject.get("tool_target"),
                        actor=subject.get("actor"),
                        environment=subject.get("environment"),
                        parameters=subject.get("parameters"),
                    ),
                )
            reason = DENIED_REFUSED if result.get("answered") else DENIED_UNANSWERED
            return ApprovalDecision(False, reason)

        return gate

    def create_protect(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        agent_name: Optional[str] = None,
        expires_in_seconds: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        require_reachable: Optional[bool] = None,
        policy: bool = True,
    ) -> Protect:
        """Build a protector: authorization, escalation and execution in one call.

        A factory for the same reason :meth:`create_gate` is, and it mirrors
        ``kernel.protect(config)`` on the TypeScript side. The gate, and with it
        the client, is built HERE, so a missing key raises where the protector is
        defined rather than on the first refund somebody tries to issue, and one
        client is reused across every action instead of being rebuilt per call.

        The whole of it is :meth:`create_gate` plus "run it if the answer was
        yes". There is no second policy engine, no durable runtime and no new
        abstraction under it.

        At most once. The decision is idempotent, so a replay of the same
        ``run_id`` + ``call_id`` + action resolves to the same approval instead of
        asking twice, and the approval is then spent against a durable permit
        before the side effect runs. A retry, a concurrent worker and a resumed
        run all reach the same permit and exactly one of them proceeds; the rest
        are refused with a reason that tells the model to stop.

        ``run`` is called only after the permit is spent, never before, so a crash
        mid-action leaves a permit with no receipt — which is visible — rather
        than an action that runs twice, which is not recoverable.
        """

        permit_client = self.client(api_key, base_url)

        def report(permit_id: str, outcome: str, summary: Optional[str] = None) -> None:
            """Record what the action did, and never fail the caller for it.

            The action already happened. Turning a successful refund into an error
            because the evidence could not be written would invite the caller to
            issue it again, which is the outcome this whole path prevents. A
            permit left running is the honest record, and it is queryable.
            """

            try:
                permit_client.record_execution(permit_id, outcome, summary=summary)
            except Exception:
                pass

        gate = self.create_gate(
            api_key=api_key,
            base_url=base_url,
            agent_name=agent_name,
            expires_in_seconds=expires_in_seconds,
            timeout_seconds=timeout_seconds,
            require_reachable=require_reachable,
            policy=policy,
        )

        def protect(
            action: str,
            run: Callable[[], Any],
            *,
            external_id: str,
            call_id: str,
            run_id: str,
            target: Optional[str] = None,
            actor: Optional[str] = None,
            environment: Optional[str] = None,
            facts: Optional[Dict[str, Any]] = None,
            question: Optional[str] = None,
        ) -> ProtectResult:
            """Authorize one action, then run it only if the answer was yes.

            ``facts`` are passed as given rather than derived, because a protected
            action is written by hand and the author knows which of its arguments
            matter.
            """

            decision = gate(
                ApprovalAsk(
                    tool_name=action,
                    call_id=call_id,
                    session_id=run_id,
                    question=question or default_protect_question(action, target),
                    external_id=self.require_external_id(external_id),
                    tool_target=target,
                    actor=actor,
                    environment=environment,
                    parameters=facts,
                )
            )
            if not decision.approved:
                return ProtectResult(False, reason=decision.reason)
            if not decision.authorization:
                return ProtectResult(False, reason=UNBOUND_APPROVAL)

            binding = decision.authorization
            try:
                # Spent before the side effect, never after. A permit consumed
                # after a successful run would leave the window this exists to
                # close: the crash between the two loses the record that it ran.
                permit = permit_client.consume_authorization(
                    binding["authorization_id"],
                    binding["tool_name"],
                    tool_target=binding.get("tool_target"),
                    actor=binding.get("actor"),
                    environment=binding.get("environment"),
                    parameters=binding.get("parameters"),
                    external_id=binding.get("external_id"),
                )
            except PusharyError as error:
                refusal = refused_permit(error)
                # A transport failure is not a refusal, and it is not permission
                # either. Not running is the only safe answer: an unissued refund
                # can be asked for again, a duplicate one cannot be taken back.
                return ProtectResult(False, reason=refusal or UNCONFIRMED_PERMIT)
            except Exception:
                return ProtectResult(False, reason=UNCONFIRMED_PERMIT)

            permit_id = permit.get("permitId")
            if not permit_id:
                return ProtectResult(False, reason=UNCONFIRMED_PERMIT)

            try:
                result = run()
            except Exception as error:
                # Recorded and re-raised, not swallowed. An error from the side
                # effect is the caller's, and reporting it as ok=False would be
                # indistinguishable from a refusal to an agent reading the result.
                report(permit_id, "failed", str(error) or "unknown error")
                raise
            report(permit_id, "succeeded")
            return ProtectResult(True, result=result)

        return protect
