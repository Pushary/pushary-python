# pushary

Human-in-the-loop decisions for AI products, in Python. Create a decision, ask a
specific end-user to approve it, and resume on their answer via webhook or poll.

This is the Python counterpart of the `@pushary/server` SDK. It is zero
dependency (Python standard library only) and targets Python 3.9 and newer.

## Installation

```bash
pip install pushary
```

## Strands Agents customer approvals

[Pause a Strands tool call, request customer approval, and resume after a worker restart](examples/strands/README.md).
The reusable hook uses native interrupts and snapshots with this SDK. Its runnable
checks cover changed actions, denial, retries, concurrent workers, and crash recovery.

## API key

The SDK needs your full API key (`pk_xxx.sk_xxx`), which includes the secret
half. Never expose it in client-side code. Get your key from your
[Pushary dashboard](https://pushary.com/docs/agents/api-key).

```python
import os
from pushary import PusharyServer

pushary = PusharyServer(api_key=os.environ["PUSHARY_API_KEY"])
```

## Two calls to add human-in-the-loop

Connect an end-user's phone once, then ask them whenever your agent needs a human.
Requires the Partner plan.

```python
# 1. Connect an end-user's phone (keyless, no account for them). Show the link.
enrolled = pushary.enroll("user_123")
# Render enrolled["universalLink"] as a button or QR. One tap turns on approvals.

# 2. Ask that person and block until they answer. Fail-closed approved flag.
decision = pushary.decisions.ask(
    question="Issue a $50 refund?",
    external_id="user_123",
    type="confirm",  # confirm | select | input
)
if decision["approved"]:
    issue_refund()
```

`ask` creates a fresh decision per call and polls
durably until the human answers or the deadline passes (default 55 seconds). `approved` is true only when the person actually said yes, so a
declined, expired, or unanswered decision safely blocks the action. For longer
waits or your own resume logic, use `create` plus a webhook or `get` below.

## Human-in-the-loop decisions

A decision asks one of your end-users to approve something and then lets your
product resume once they answer. Use it for the moments where a human should be
in the loop: releasing funds, sending an outbound message, running a
destructive action, or confirming an AI-proposed change.

The flow has three parts:

1. Create a decision and notify the end-user.
2. Resume when they answer, either by handling the webhook or by polling.
3. Optionally answer or cancel on their behalf from your own surface.

### Create a decision

By default `create` is async: it returns right away with a `decisionId` and a
`pollUrl`, which suits serverless functions that cannot hold a request open
while a human decides. Pass `wait=True` to block for up to about 55 seconds in
case the human answers quickly.

Always pass `idempotency_key` so a retried call does not ask the same human
twice.

```python
decision = pushary.decisions.create(
    "Approve the $4,200 payout to Acme Corp?",
    type="confirm",
    external_id="user_123",
    agent_name="Billing Agent",
    context="Invoice INV-8842, net-30, first payout to this vendor.",
    callback_url="https://your-app.com/webhooks/pushary",
    expires_in_seconds=3600,
    wait=True,
    timeout_seconds=50,
    idempotency_key="payout-INV-8842",
)

if decision["answered"]:
    print("Resolved fast:", decision["value"])
else:
    print("Still pending, poll:", decision["pollUrl"])
```

For a multiple choice decision, pass `type="select"` with at least two
`options`. For free text, pass `type="input"` and an optional `placeholder`.

```python
decision = pushary.decisions.create(
    "Which shipping speed should we book?",
    type="select",
    options=["Standard", "Express", "Overnight"],
    external_id="user_123",
)
```

### Poll for the answer

If you did not wait, or the wait window closed before the human answered, poll
for the outcome. Pass `wait=N` to long-poll for up to N seconds so the call
returns as soon as they answer rather than on your next loop.

```python
result = pushary.decisions.get(decision["decisionId"], wait=50)

if result["status"] == "answered":
    print("Answer:", result["value"])
elif result["status"] == "expired":
    print("The decision expired before anyone answered.")
```

### Answer or cancel on their behalf

If your own interface collected the answer, record it so any waiting call
resolves. Cancel a decision to close it when it is no longer needed.

```python
pushary.decisions.answer(decision["decisionId"], "yes")

pushary.decisions.cancel(decision["decisionId"])
```

## Webhooks

When the end-user answers, Pushary POSTs the result to your `callback_url`. The
request carries an `X-Pushary-Signature` header, an HMAC-SHA256 hex digest of
the raw request body signed with your webhook secret. Verify it against the raw
bytes you received, before parsing the JSON, so a change to spacing or key order
cannot slip past the check.

Fetch or rotate the secret from the SDK:

```python
secret = pushary.decisions.get_webhook_secret()
rotated = pushary.decisions.rotate_webhook_secret()
```

A Flask handler that verifies the signature and resumes your work:

```python
import os
from flask import Flask, request, abort
from pushary import verify_webhook_signature, parse_decision_callback

app = Flask(__name__)
WEBHOOK_SECRET = os.environ["PUSHARY_WEBHOOK_SECRET"]

@app.post("/webhooks/pushary")
def pushary_webhook():
    signature = request.headers.get("X-Pushary-Signature")
    if not verify_webhook_signature(request.data, signature, WEBHOOK_SECRET):
        abort(401)

    payload = parse_decision_callback(request.data)
    if payload is None:
        abort(400)
    decision_id = payload["correlationId"]
    answer = payload.get("value")
    # Resume your work now that the human has answered.
    return "", 204
```

`request.data` is the raw request body Flask captured, which is exactly what the
signature was computed over. Do not re-serialize the parsed JSON before
verifying.

## Errors

Every method returns the parsed JSON body as a `dict`. A non-2xx response raises
`PusharyError`, which carries the HTTP `status` and the reason the server
reported.

```python
from pushary import PusharyError

try:
    pushary.decisions.get("does-not-exist")
except PusharyError as error:
    print(error.status, error.message)
```

## Security

- Keep your API key and webhook secret in environment variables.
- Rotate the webhook secret from the dashboard or via
  `rotate_webhook_secret()` if it is ever exposed.
- API keys are site-scoped, so a key can only reach its own decisions.

## License

MIT. See `LICENSE`.

## Retries and timeouts

Pass an `idempotency_key` tied to your run and step when retrying one operation. Without it, each blocking ask creates a fresh decision. Shared adapter durable creates require an explicit key and fail before sending if it is absent.

The local wait budget starts before creation; each HTTP operation receives the remaining timeout. Python uses urllib socket timeouts, which bound blocking I/O rather than guaranteeing an exact wall-clock deadline during a continuously streaming response. A creation timeout raises; retry using the same key to recover. A polling timeout returns the known pending decision for later retrieval. Zero retains create-only behavior.

For select/input decisions, `approved` means answered, not permission for an action. Use a confirm question for approval gates and inspect actual values for other question types.

### Upgrading to 2.0

Upgrade `pushary-langgraph`, `pushary-openai-agents` or `pushary-crewai` to 0.3 alongside this SDK. Durable helpers require an explicit `idempotency_key` tied to the run and step; independent blocking calls no longer deduplicate by question text. Published LangGraph 0.2.0, CrewAI 0.2.0 and OpenAI Agents 0.2.1 declare `pushary>=1.4.0` without an upper bound. An old adapter pin can therefore install core 2.0; LangGraph 0.2.0 cannot provide the required durable key and will fail closed. Upgrade the adapter and core together. Temporarily pinning `pushary<2` preserves the old behavior, including its approval-identity defect; it is not a fix.

### Upgrading to 2.1

Shared approval gates now bind decision identity to the customer, session, tool call, full JSON input, question, target, actor, environment, parameters, and presentation. Exact retries keep their key regardless of object property order. Changed arguments or recipients create a new review. A tool call ID is required; a session ID may be empty when the framework supplies globally unique call IDs. Non-JSON inputs, non-string object keys, cycles, and non-finite numbers raise before policy evaluation or decision creation. Framework adapters must supply raw `ApprovalAsk.input`, not only a shortened question or derived parameters.

These keys intentionally differ from 2.0. Do not replay completed operations during upgrade or silently migrate an old approval onto a new key. Drain pending runs on their original version, or explicitly request fresh review while preserving your application's operation idempotency. `policy=False` now records the full subject and presentation on the human decision; a failed legacy policy endpoint retains the prior minimal fallback for compatibility.

`decisions.create` and `decisions.ask` accept `presentation`, using the existing server-validated shape with `label`, `effect`, optional `risk`, and `changes` that reference keys in `parameters`. Shared `ask_human` and `create_durable_decision` helpers forward that field plus target, actor, environment, parameters, placeholder, expiry, reachability, and callback settings. Parameter values remain the source of truth for display; presentation changes name a parameter rather than carrying a second copy of its value.
