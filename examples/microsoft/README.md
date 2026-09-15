# Customer phone approvals with Microsoft Agent Framework

Pause an order action, ask the customer on their phone, stop the worker, and
restore the native checkpoint after approval. This example uses Microsoft Agent
Framework's `Content` function approvals, `ctx.request_info`, `@response_handler`,
and `FileCheckpointStorage`, with the existing Pushary Python SDK for delivery
and one-use execution permits.

It protects one application-owned action in a custom workflow. It does not
automatically intercept every tool in an arbitrary Agent or add a new framework
package. An upstream agent can propose the order, but the application owns the
recipient and routes the actual effect through this workflow.

## Install

Tested with Python 3.12, `agent-framework-core==1.18.0`, and `pushary==2.1.1`.
The example is source in the public Python SDK repository, not part of its wheel.

```sh
git clone https://github.com/Pushary/pushary-python.git
cd pushary-python
python -m venv .venv
. .venv/bin/activate
pip install -r examples/microsoft/requirements.txt
python examples/microsoft/check_review.py
```

The check needs no account, phone, model key, or live network requests. It uses the
real Microsoft runtime and published Pushary SDK, simulating only the HTTP
boundary and business effect. It covers fresh-process resumption, rejection,
expiry, wrong caller, changed arguments/revision/checkpoint, forged native
approval, duplicate and concurrent resumes, lost responses, and uncertain effects.

## Run a customer phone decision

Use a [Pushary Partner account](https://pushary.com/sign-up?from=agent&plan=partner&utm_source=github&utm_medium=oss-adapter&utm_campaign=pushary-microsoft-agent-framework&utm_content=python-partner-start)
and an enrolled test customer's external ID. Enrollment uses the existing
`PusharyServer.enroll(external_id)` flow. Keep the full key in `PUSHARY_API_KEY`
on the server; it must belong to the authenticated tenant and should be bound
to the intended customer. Never let a model choose the tenant, recipient or key.

```sh
# Export PUSHARY_API_KEY securely, then use an enrolled test customer's ID.
python examples/microsoft/run.py start ./order-review YOUR_TEST_CUSTOMER_ID
# The worker has exited. Answer the request on the customer's phone.
python examples/microsoft/run.py resume ./order-review YOUR_TEST_CUSTOMER_ID
```

The start command claims a new private operation directory, saves the native
checkpoint and immutable operation record, then opens the decision. Approval
after resume creates a local SQLite order record in `order-review/orders.db`.
Denial does not. No shipment or payment is made, and no model API is called.
Use a fresh directory for an independent operation. Phone delivery is runnable
with your account; the automated checks do not claim to test live delivery.

## What enforces the decision

1. The native workflow emits a function-approval request and pauses. No effect
   runs in this step. The exact pending checkpoint ID is saved before delivery.
2. Pushary's decision binds the tenant, recipient, run, call, action, target,
   exact parameters, deadline, code revision, dependency versions and checkpoint
   hash. Retries preserve the same ID and API payload, including its fixed
   one-hour requested lifetime.
3. Resume verifies the trusted caller, checkpoint hash and native request. The
   shared SDK accepts an affirmative decision and consumes a durable one-use
   permit before continuing the workflow. Transport failure is never permission.
4. A one-use, process-local grant ties the native approval response to that
   successful permit consumption. A fabricated `approved=True` response cannot
   execute the handler. A restored worker has no grant until it consumes a permit.
5. The handler checks the original deadline again and receives a stable business
   idempotency key. The SDK records success or failure after the effect.

The grant is not restored as executor state. A response-entry checkpoint may
contain an old response, but it cannot recreate the corresponding in-memory
grant or spend an already consumed permit.

## Use it in your application

Import `start` and `resume` from [review.py](review.py). Obtain tenant, customer and
operation identity from your authenticated application. Keep state directories
private and look them up through that authenticated operation; never load paths
or checkpoint content supplied by a model or public request.

`start` requires a new directory and a deadline within the next hour. Parameters
must be 1–31 flat scalar facts with bounded names/values, and the complete approval
question must fit 500 characters. Complex actions should expose a concise,
complete operation summary; do not silently drop effect-relevant arguments.

`resume(directory, effect, tenant_id=..., external_id=..., run_id=...)` runs the
effect only with a verified, permit-backed approval. The effect receives
`(parameters, idempotency_key)` and must implement business idempotency. Do not
also register an unguarded version of the effect with the agent. This example is
synchronous at its public boundary; async servers can run it in a worker thread.

The SDK currently returns a `blocked` reason for pending and terminal refusals.
It leaves the original native checkpoint pending; it does not turn every refusal
into a native rejection response. A trusted application can re-enter the same
operation after the customer answers. Do not let a model manufacture new run IDs
or extend deadlines to retry a refusal. A final refusal remains final in Pushary.

Changing the native request is rejected. Changing other bound metadata requires
fresh approval. Changed code/dependencies require a new operation and revision:
bump `REVISION` when modifying the handler's meaning. The local operation deadline
can shorten the server decision's one-hour lifetime.

## Crashes and retries

The remote permit is consumed before the native continuation, so duplicate
workers and restarts cannot execute the same approval twice. A crash after
consumption can leave the business result uncertain; reconcile it with the order
service. Never automatically open a replacement approval and replay the effect.
This guarantees at-most-once permit consumption, not exactly-once business effects.

A crash during initial checkpoint preparation leaves an incomplete directory
and fails closed on resume. Inspect it before deliberately starting another
operation. Completed results are returned to the caller, not cached by this
example; repeated resume returns a spent-permit refusal.

## Framework references

- [Native human-in-the-loop requests and checkpoint restoration](https://learn.microsoft.com/en-us/agent-framework/workflows/human-in-the-loop)
- [Function approval content and responses](https://learn.microsoft.com/en-us/agent-framework/agents/tools/tool-approval)

Maintained by Pushary under this repository's MIT license. Report issues in
[pushary-python](https://github.com/Pushary/pushary-python/issues).
