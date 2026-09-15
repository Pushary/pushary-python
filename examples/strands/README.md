# Customer approval for Strands Agents tool calls

Pause an order workflow, get customer approval on their phone, and resume the
original tool call in a new worker. Pushary delivers and records the decision;
[Strands native interrupts](https://strandsagents.com/docs/user-guide/concepts/interrupts/)
enforce the pause. A model message saying “approved” cannot release the tool.

This is a reusable Python integration using the existing `pushary` SDK, with a
small order demonstration. It does not require another adapter package.

## Install and check without credentials

Requires Python 3.12. Tested with `strands-agents==1.55.1` and `pushary==2.1.1`.

```sh
git clone https://github.com/Pushary/pushary-python.git
cd pushary-python/examples/strands
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python check_review.py -v
```

The check uses the released SDKs, native hooks, interrupts, snapshots and tool
execution. It substitutes model output, HTTP responses and the business effect.
Separate processes prove restart, duplicate/concurrent resume, denial, expiry,
cancellation, changed customer/action/code/snapshot, unavailable API, and a killed
worker after its effect. It also tries to bypass the approval with a direct native
resume. These are offline checks, not evidence of a real phone delivery.

## Run the phone demonstration

1. [Start a Pushary Partner integration](https://pushary.com/sign-up?from=agent&plan=partner&utm_source=github&utm_medium=oss-adapter&utm_campaign=pushary-strands&utm_content=partner-start).
2. Store your server API key in `PUSHARY_API_KEY`. Keep it server-side.
3. Enroll your test customer using `PusharyServer(...).enroll(external_id)` and
   open the returned `universalLink` on their phone. Set `PUSHARY_EXTERNAL_ID`
   to that exact application's customer ID. See the [enrollment guide](../../README.md#two-calls-to-add-human-in-the-loop).
4. Configure AWS credentials with access to the Strands default Bedrock model,
   following [Strands quickstart](https://strandsagents.com/docs/user-guide/quickstart/overview/).
   Model usage and Pushary plan limits apply.

```sh
python run.py start
# Process exits with phase=pending and a decision_id. No release is recorded.
# Approve or deny on the enrolled phone, then launch a fresh process:
python run.py resume
# Repeat resume safely: completed output is returned without rerunning the tool.
```

`run.py` records an approved release in the local `releases` SQLite table; it does
not ship an order or move money. The fixed demo operation deliberately runs once.
Use a different trusted operation and immutable draft for a genuinely new request,
rather than deleting its history to retry a completed action.

## Use the hook in your application

Copy `review.py` alongside your application and register the hook on the agent:

```python
review = CustomerReview(
    database, pushary_client,
    tenant=authenticated_tenant_id,
    customer=order.customer_id,
    operation=order.release_operation_id,
    tool_name="release_order",
    arguments={"order_id": order.id, "draft_version": order.version},
    code_version="order-release-v1",
)
agent = Agent(tools=[release_order], hooks=[review], callback_handler=None)
result = review.start(agent, prompt)
# Later, in a new worker with the same code, identities and database:
result = review.resume(agent)
```

The application authorizes the tenant and customer and loads the complete action
from trusted state. The model cannot choose the recipient, amount or approval
identity. Include every consequential field, including currency for money and an
immutable content version for messages. Flat JSON inputs must fit the 500-character
question limit; larger actions need an application-specific review surface, not
silent truncation. Do not include secrets in the review question.

The native [session snapshot](https://strandsagents.com/docs/user-guide/concepts/agents/session-management/)
is saved before requesting the decision. The binding includes the customer, action,
operation, code and SDK versions, original interruption and complete snapshot.
Creation retries reuse one key. Resume fetches the authoritative decision with the
server SDK and verifies its ID, binding context, question and confirm type. The API
may omit `externalId` for retained decisions; the exact customer remains bound in
the saved context fingerprint. A conflicting nonempty recipient is rejected.
Only an answered confirm with value `yes` grants one exact native tool call.

## Operational boundaries

- Use one durable, access-controlled SQLite file for workers on the same host.
  For multiple hosts, move these records and the atomic claim to your existing
  transactional database. The hash detects mismatches; it does not authenticate
  a database writable by an attacker. Protect snapshots as sensitive application data.
- Keep the same tool definitions, hooks, SDK versions and application code across
  pause/resume. Increase `code_version` when executable behavior changes and drain
  old reviews on their original code. Never transfer an old grant to a new action.
- `pending` is safe to schedule again. API failures raise and preserve the pending
  record. `starting`, `resuming`, or `uncertain` require inspecting durable business
  receipts before recovery; never automatically reset these phases. A crashed
  worker may have committed an effect before it could save completion.
- The business tool still needs its own idempotency key/transaction and must
  recheck current business validity before an irreversible action. A local claim
  cannot atomically commit an external payment. `run.py` demonstrates a unique
  operation receipt only for its local effect.
- Only the named tool is protected. Expose no alternative tool that performs the
  same effect. Other hooks must not change approved inputs after this hook.
  This example supports one protected action per operation, not a multi-agent
  approval scheduler. Any additional native interruption in the returned output
  stays unresolved and needs its own authorized workflow.
- `complete` means the native continuation returned, not that the action succeeded
  or was approved. Inspect `output.message`, `output.stop_reason` and business
  receipts. Denial and expiry cancel the call and still let the agent finish.
- Poll from a job scheduler, or use a verified webhook only as a wake-up signal;
  resume always reads the decision again. Do not trust a browser's `approved` flag.

Maintained by [Pushary](https://github.com/Pushary). Questions: business@pushary.com.
See [Strands' integration catalog](https://strandsagents.com/integrations/) for the
upstream discovery channel; a catalog submission is separate from acceptance.
