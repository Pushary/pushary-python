"""One application-owned action: native Microsoft checkpoint + Pushary permit."""
import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
import secrets
import time

from agent_framework import (
    Content, Executor, FileCheckpointStorage, WorkflowBuilder, WorkflowContext,
    handler, response_handler,
)
from pushary.adapters import AdapterKernel, decision_fingerprint, derive_parameters

REVISION = "order-review-v1"


def versions():
    return {name: importlib.metadata.version(name) for name in ("agent-framework-core", "pushary")}


def question_for(operation):
    return (f"Approve {operation['action']} for {operation['target']}? "
            + json.dumps(operation["parameters"], sort_keys=True, ensure_ascii=False))


def approval_request(operation):
    return Content.from_function_approval_request(
        operation["call_id"], Content.from_function_call(
            operation["call_id"], operation["action"], arguments=operation["parameters"]
        )
    )


class OrderReview(Executor):
    def __init__(self, operation, effect, binding):
        super().__init__(id="customer-review")
        self.operation, self.effect, self.binding = operation, effect, binding
        self.grant = None  # Deliberately absent from on_checkpoint_save's state.

    @handler
    async def ask(self, request: Content, ctx: WorkflowContext[None, dict]):
        await ctx.request_info(request, Content, request_id=self.operation["call_id"])

    @response_handler
    async def answer(self, original_request: Content, response: Content, ctx: WorkflowContext[None, dict]):
        expected = approval_request(self.operation)
        if (decision_fingerprint(original_request.to_dict()) != decision_fingerprint(expected.to_dict())
                or response.type != "function_approval_response" or response.approved is not True
                or response.id != expected.id or response.function_call is None
                or response.function_call.to_dict() != expected.function_call.to_dict()
                or not self.grant or (response.additional_properties or {}).get("pushary_grant") != self.grant):
            raise ValueError("No matching, permit-backed approval for this action")
        self.grant = None
        if time.time() >= self.operation["expires_at"]:
            raise TimeoutError("Operation expired before execution")
        result = self.effect(self.operation["parameters"].copy(), self.binding)
        await ctx.yield_output(result)


def workflow(folder, executor):
    storage = FileCheckpointStorage(Path(folder) / "checkpoints")
    instance = WorkflowBuilder(start_executor=executor, name="pushary-order-review",
                               checkpoint_storage=storage).build()
    return instance, storage


def start(folder, *, tenant_id, external_id, run_id, call_id, action, target, parameters, expires_at):
    """Persist once before delivery. An existing operation directory is never reused."""
    operation = dict(tenant_id=tenant_id, external_id=external_id, run_id=run_id,
                     call_id=call_id, action=action, target=target, parameters=parameters,
                     expires_at=expires_at, revision=REVISION, versions=versions())
    for name, limit in dict(tenant_id=120, external_id=256, run_id=200, call_id=200,
                            action=100, target=80).items():
        value = operation[name]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"Invalid {name}")
    facts = derive_parameters(parameters)
    if facts is None or len(facts) > 31 or "pushary_binding" in facts:
        raise ValueError("Use 1–31 bounded flat JSON parameters; pushary_binding is reserved")
    operation["parameters"] = facts
    question = question_for(operation)
    if len(question.encode("utf-16-le")) // 2 > 500:
        raise ValueError("The complete approval question must fit 500 characters")
    if type(expires_at) is not int or not time.time() < expires_at <= time.time() + 3600:
        raise ValueError("Persist a deadline within the next hour")
    folder = Path(folder)
    folder.mkdir(mode=0o700)

    async def pause():
        instance, storage = workflow(folder, OrderReview(operation, None, None))
        events = await instance.run(approval_request(operation))
        requests = [event for event in events if event.type == "request_info"]
        if len(requests) != 1 or requests[0].request_id != call_id:
            raise RuntimeError("Expected exactly one native approval request")
        checkpoint = await storage.get_latest(workflow_name="pushary-order-review")
        return checkpoint.checkpoint_id

    checkpoint_id = asyncio.run(pause())
    checkpoint_bytes = (folder / "checkpoints" / f"{checkpoint_id}.json").read_bytes()
    saved = dict(operation=operation, checkpoint_id=checkpoint_id,
                 checkpoint_hash=hashlib.sha256(checkpoint_bytes).hexdigest(), question=question)
    temporary = folder / "operation.tmp"
    temporary.write_text(json.dumps(saved))
    temporary.replace(folder / "operation.json")


def resume(folder, effect, *, tenant_id, external_id, run_id):
    """Re-enter the exact checkpoint; SDK transport and permits remain shared."""
    folder = Path(folder)
    saved = json.loads((folder / "operation.json").read_text())
    operation = saved["operation"]
    if saved["question"] != question_for(operation):
        raise ValueError("Approval question does not describe the saved action")
    for name, expected in dict(tenant_id=tenant_id, external_id=external_id, run_id=run_id,
                               revision=REVISION, versions=versions()).items():
        if operation[name] != expected:
            raise ValueError(f"Saved operation {name} does not match the trusted caller")
    if time.time() >= operation["expires_at"]:
        return {"blocked": "Operation expired"}
    # Read before native deserialization; only application-owned state is accepted.
    checkpoint_path = folder / "checkpoints" / f"{saved['checkpoint_id']}.json"
    if checkpoint_path.parent.resolve() != (folder / "checkpoints").resolve():
        raise ValueError("Invalid checkpoint path")
    if hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() != saved["checkpoint_hash"]:
        raise ValueError("Checkpoint changed since the approval was prepared")
    binding = decision_fingerprint(saved)
    executor = OrderReview(operation, effect, binding)
    instance, storage = workflow(folder, executor)

    async def pending_request():
        checkpoint = await storage.load(saved["checkpoint_id"])
        event = checkpoint.pending_request_info_events[operation["call_id"]]
        if event.data.to_dict() != approval_request(operation).to_dict():
            raise ValueError("Checkpoint action does not match the saved operation")
        return event.data

    request = asyncio.run(pending_request())

    def continue_workflow():
        executor.grant = secrets.token_urlsafe(32)
        response = request.to_function_approval_response(True)
        response.additional_properties["pushary_grant"] = executor.grant
        async def run():
            result = await instance.run(checkpoint_id=saved["checkpoint_id"],
                                        responses={operation["call_id"]: response})
            outputs = result.get_outputs()
            if len(outputs) != 1:
                raise RuntimeError("The resumed workflow did not finish its protected action")
            return outputs[0]
        try:
            return asyncio.run(run())
        finally:
            executor.grant = None

    protect = AdapterKernel("the Microsoft checkpoint example").create_protect(
        policy=False, timeout_seconds=0, expires_in_seconds=3600,
    )
    result = protect(operation["action"], continue_workflow,
                     external_id=external_id, run_id=run_id, call_id=operation["call_id"],
                     target=operation["target"], actor=tenant_id, question=saved["question"],
                     facts={**operation["parameters"], "pushary_binding": binding})
    return {"result": result.result} if result.ok else {"blocked": result.reason}
