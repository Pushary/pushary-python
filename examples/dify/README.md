# Customer phone approvals for Dify Human Input workflows

Pause an order review in Dify, ask the customer on their phone through Pushary,
then submit the native Human Input form and release the order in your backend.
The runnable demo writes a local SQLite order; it does not charge or ship anything.

This is an application-side example using the published `pushary` SDK and Dify's
Service API. It is not a Dify Marketplace plugin or an official Dify integration.
The business effect belongs in the protected backend callback, **not** in a Dify
HTTP or tool node: another valid native form submission can resume the graph.

## What runs where

1. Your backend starts the published workflow with a complete, trusted order question.
2. Dify pauses at its native Human Input node. The backend saves the run ID, form
   token, definition, workflow version, customer identity, parameters and deadline.
3. Pushary sends the bound approval to that customer's enrolled phone. The worker
   exits while the decision is pending; Dify retains the paused workflow.
4. A new worker reads the same operation and asks the SDK for its current decision.
   An affirmative answer must yield a matching, unspent execution permit.
5. Inside that permit-protected callback, the worker submits the native form,
   checks the resumed run's identity and output, then performs the backend effect.

A Dify form response is not itself a Pushary permit. A denial, unanswered decision,
changed form, wrong customer, expired deadline, failed workflow or uncertain permit
does not run the effect. Refused decisions leave Dify paused until its timeout;
this example does not submit a synthetic rejection on behalf of the customer.

## Configure the Dify workflow

API contracts were checked against **Dify 1.17.1**. Its native graph engine is
`graphon==0.7.0`. You need a published **Workflow** app and its server-side app API
key. No LLM node or model API key is needed for this example.

Import [workflow.yml](workflow.yml) in Dify Studio using **Import DSL file**, then
review the following configuration before publishing. The file contains no keys,
plugins, model nodes or business effects. Its Human Input node ID is `review`.

| Node | Configuration |
| --- | --- |
| User Input / Start | One required paragraph input named `question`, maximum 500 characters. |
| Human Input | Form body consists only of the Start node's `question` variable. No input fields. Enable **Web App** delivery, not email-only delivery. Set timeout to one hour. |
| Human Input actions | `approve` / **Approve** / primary button, then `reject` / **Reject** / default button. These are exact IDs, titles and order. |
| End on approve | Output `action` from Human Input's `__action_id`; output `question` from its `__rendered_content`. No additional outputs. |
| End on reject and timeout | Return `stopped_question` from Start's `question`; no business action. |

Connect Start → Human Input. Keep **all** business effects out of this graph,
including before the Human Input node. Disable automatic retries for the backend
effect. The minimal graph is intentionally approval-only; adapt a larger workflow
only after reviewing all paths that can reach a side effect.

Publish it. Set `DIFY_NODE_ID=review` for the supplied workflow (if you build your
own, export it to find the Human Input node's `id`). Read the published `workflow_id` from a test run's
`workflow_started` event or run detail for `DIFY_WORKFLOW_ID`. This identifies the
published **version**, not the application ID. A test run without a phone binding
must not perform a business effect; let its form expire.

The bridge starts the app's current published workflow and verifies its returned
version before contacting Pushary. Republish only after reviewing the new graph,
then update the configured version. A mismatch stops the bridge. It does not use
the version-specific execution endpoint, which Dify Cloud restricts on free plans.

## Run with a test phone

Requires Python 3.12+, a Dify app configured above, a Pushary **Partner** API key
and an enrolled test customer. Obtain keys through your own dashboards and inject
them into the process environment; do not put them in the workflow, repository,
shell history, URLs, or an agent prompt.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Inject DIFY_API_KEY and PUSHARY_API_KEY securely before running.
export DIFY_API_URL='https://api.dify.ai/v1'
export DIFY_WORKFLOW_ID='YOUR_REVIEWED_PUBLISHED_WORKFLOW_VERSION_ID'
export DIFY_NODE_ID='review'
.venv/bin/python run.py start /tmp/dify-order-1 TEST_CUSTOMER_ID
# Answer on that customer's phone, then use a new process:
.venv/bin/python run.py resume /tmp/dify-order-1 TEST_CUSTOMER_ID
```

`start` creates a new state directory once. `resume` reuses it and never starts a
second workflow. A pending result is reported as `blocked` with the SDK's reason.
After approval, the result contains the released local order. Inspect `orders.db`
inside the state directory: the business idempotency key is unique. Re-running
`resume` after submission stops because the native form is already closed.

For your backend, import `start` and `resume` from [review.py](review.py). Supply
tenant/customer IDs and the action from authenticated application state, never
model output. The CLI's `demo-store` tenant and test order are demonstration data.
Pass your business callback to `resume`; it receives a copy of the exact approved
parameters and a stable idempotency key. Your downstream service must also enforce
that key if it can retry internally.

## Trust and recovery limits

- The API base URL and both keys are trusted server configuration. HTTPS is required;
  redirects are refused. Dify app keys and form tokens never go to Pushary or the model.
- The state directory is private application storage (created with mode `0700`).
  Protect it from untrusted writes and retain it across workers. Hashes detect
  accidental changes; they are not signatures against someone who controls the disk.
- The complete question must fit 500 UTF-16 units; parameters must be bounded flat
  JSON. The example rejects forms with editable/file inputs and simultaneous Human
  Input nodes. It does not silently omit approval details or choose an approver.
- The decision binds the tenant, customer, run, node, workflow version, form, exact
  parameters, deadline and example revision. The native form can expire earlier than
  the one-hour Pushary request; both deadlines are checked before the effect.
- The SDK consumes the remote one-use permit **before** native submission. Dify
  also makes forms one-shot. A lost submission response, native resume taking over
  30 seconds, worker crash or uncertain business result needs operator reconciliation.
  The spent permit is not reset and the effect is not automatically retried.
- If initial workflow creation fails before the pause is saved, inspect Dify's run
  logs. Do not delete the state directory and blindly start another operation.
- Dify workspace administrators and anyone who controls your backend remain trusted.
  This example does not intercept arbitrary agent tools or secure a separate effect
  placed directly in Dify.

## Verification

```bash
.venv/bin/pip install -r requirements-check.txt
.venv/bin/python check_review.py
```

The checks use Dify's **real Graphon 0.7.0** Human Input node, graph engine and
serialized runtime snapshots, plus the published Pushary SDK. They run fresh
processes across pause/resume and cover pending, approval, denial, expiry,
cancellation, customer/version/parameter changes, duplicate workers, a competing
native submitter, lost permit/submission responses and a crash after the effect.

Both hosted HTTP services and the order service are simulated in these checks.
Dify Cloud import and a Studio pause → approve → resume smoke test were also
verified on 2026-09-15. The automated checks do **not** prove its database/Celery
delivery or live phone delivery. Run the test-phone procedure above against your deployment
before using this in production; no live-phone verification is claimed here.

## Source contracts and support

- [Dify Human Input API flow](https://docs.dify.ai/en/api-reference/guides/human-input-flow)
- [Dify 1.17.1 form API: app/tenant checks and one-shot submission](https://github.com/langgenius/dify/blob/1.17.1/api/controllers/service_api/app/human_input_form.py)
- [Dify 1.17.1 native completion contract](https://github.com/langgenius/dify/blob/1.17.1/api/core/workflow/nodes/human_input/callback.py)
- [Pushary Python SDK](https://github.com/Pushary/pushary-python)
- [Customer enrollment with `PusharyServer.enroll`](../../README.md#two-calls-to-add-human-in-the-loop)

For phone delivery, [start with Pushary Partner](https://pushary.com/sign-up?plan=partner&utm_source=github&utm_medium=oss-adapter&utm_campaign=pushary-dify&utm_content=python-partner-start).
Report example issues in [Pushary's public SDK repository](https://github.com/Pushary/pushary-python/issues);
business contact: business@pushary.com.
