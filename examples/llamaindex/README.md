# Customer approval with LlamaIndex Workflows

Pause an order release, ask its customer on their phone, and resume in a new
process. `CustomerReview` is a reusable, single-action LlamaIndex workflow using
native `InputRequiredEvent`, `HumanResponseEvent` and `Context` snapshots. The
existing Pushary Python SDK delivers and reads the decision.

The protected action runs only after the saved customer decision is verified and
its continuation is atomically claimed. Sending `HumanResponseEvent(response="yes")`
directly cannot authorize the action.

## Install and verify

Requires Python 3.12. Tested with `llama-index-workflows==2.23.3` and `pushary==2.1.1`.
The workflow library is also the runtime underlying LlamaIndex agent workflows;
this integration wraps one application-owned action, not every tool in an agent.

```sh
git clone https://github.com/Pushary/pushary-python.git
cd pushary-python/examples/llamaindex
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python check_review.py -v
```

The check uses the real workflow runtime and Pushary SDK, with simulated HTTP and
business effects. Separate workers prove approval, denial, expiry, cancellation,
restart, duplicate/concurrent resume, changed customer/action/code/snapshot,
missing API availability, direct-event bypass, and a killed worker after its effect.
This is not evidence of a physical phone delivery. No model or API key is needed.

## Try the phone workflow

1. [Create a Pushary Partner integration](https://pushary.com/sign-up?from=agent&plan=partner&utm_source=github&utm_medium=oss-adapter&utm_campaign=pushary-llamaindex&utm_content=partner-start).
2. Set `PUSHARY_API_KEY` from your server-side secret environment.
3. [Enroll your test customer](../../README.md#two-calls-to-add-human-in-the-loop)
   and open the returned `universalLink` on their phone. Set `PUSHARY_EXTERNAL_ID`
   to that exact application customer ID.

```sh
python run.py start
# Process exits with phase=pending and a decision_id. No release is recorded.
# Answer on the enrolled phone, then start a different process:
python run.py resume
# Safe to repeat: returns the recorded result without rerunning the action.
```

The demo records an approved order release in a local SQLite `releases` table.
It never ships an order or moves money and requires no model provider. Keep the
same `REVIEW_DATABASE` (default `reviews.sqlite`) and environment across commands.
The fixed demo operation deliberately runs once; use a new trusted operation and
immutable draft for a genuinely new request instead of deleting old receipts.

## Integrate an application action

Copy `review.py` alongside your application and supply an async action:

```python
from pushary import PusharyServer
from review import CustomerReview

review = CustomerReview(
    database_path,
    PusharyServer(server_api_key),
    tenant=authenticated_tenant_id,
    customer=order.customer_id,
    operation=order.release_operation_id,
    action={"order_id": order.id, "draft_version": order.version},
    code_version="order-release-v1",
    execute=release_order_idempotently,
)
result = await review.start_review()
# Later, instantiate the same workflow in a new process, then:
result = await review.resume_review()
```

`release_order_idempotently(action)` must recheck the current business version and
use the operation's durable receipt or the external service's idempotency key.
Connect this reviewed action to your agent orchestration without exposing another
tool that performs the same action without review. The model must not select the
recipient, operation identity, or approved action. Load those from authenticated,
authorized application state.

Include every consequential field in `action`: currency for money, destination
for messages, and immutable content versions. Inputs must be bounded, flat JSON
that fits the API's 500-character review question. Large actions need a dedicated
application review surface; this helper rejects them rather than truncating.
Never include secrets in review text.

## Native persistence and enforcement

The workflow follows LlamaIndex's recommended
[two-step human-input pattern](https://developers.llamaindex.ai/python/llamaagents/workflows/human_in_the_loop/).
The first step emits the review request. The driver saves `handler.ctx.to_dict()`
and cancels the original handler; there is no live process waiting on the phone.
Resume rebuilds `Context.from_dict(...)` and sends a native response into the saved
workflow only after reading the authoritative Pushary decision.

The saved binding includes tenant, customer, operation, full action, expiry, code
and SDK versions, and the complete native context. Repeated creates reuse one
idempotency key. The fetched ID, binding context, question and confirm type must
match. A conflicting recipient is rejected; if the API omits `externalId`, the
exact customer remains bound in the context fingerprint. Only an answered confirm
whose value is `yes` creates an unpredictable one-use grant sent only to the restored native handler.
The response event alone is not a grant.

## Recovery boundaries

- One durable SQLite file coordinates workers on one host. Use your existing
  shared transactional database for multiple hosts. Protect the file: a binding
  hash detects mismatches, not malicious rewrites by someone controlling storage.
- Keep the same Pushary workspace and API environment. Keep workflow code, SDK
  versions, and action implementation identical across
  pause/resume. Bump `code_version` when behavior changes and drain pending work
  on its original version. Workflow instance attributes are recreated, not serialized.
- `pending` may be polled again; API failures raise without consuming the review.
  A verified webhook can wake a worker, but must never supply the approval itself.
- `starting`, `resuming`, and `uncertain` require inspecting durable business
  receipts before recovery. Never automatically reset a claim after a timeout.
  A killed worker may have committed an effect before it saved the final output.
- Native execution has a 60-second timeout after resumption. The phone can answer
  later within the separate decision expiry (one hour by default). Long actions
  should enqueue durable application work with its own receipt.
- `complete` includes denial: inspect `output.approved` and the business receipt.
  Exceptions, cancellation and unrecordable action results leave an uncertain
  phase. An approval is not a guarantee of exactly-once external execution.

Maintained by [Pushary](https://github.com/Pushary). Contact: business@pushary.com.
This integration is maintained outside the LlamaIndex repository; a documentation
submission is separate from official acceptance or endorsement.
