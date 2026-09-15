"""Dify's native Human Input API, with an application-owned protected effect."""
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pushary.adapters import AdapterKernel, decision_fingerprint, derive_parameters

REVISION = "dify-order-v1"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward a credential or replay a POST to another URL.


def request(base, path, body=None):
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("DIFY_API_URL must be a trusted HTTPS API base URL")
    req = Request(base.rstrip("/") + path,
                  data=None if body is None else json.dumps(body).encode(),
                  headers={"Authorization": "Bearer " + os.environ["DIFY_API_KEY"],
                           "Content-Type": "application/json",
                           "User-Agent": "Pushary-Dify-Example/1.0"})
    try:
        return build_opener(NoRedirect).open(req, timeout=30)
    except HTTPError as error:
        # A form token is a secret. Do not include the URL/body in logs or receipts.
        raise RuntimeError(f"Dify HTTP {error.code}; reconcile before retrying a submission") from None
    except (URLError, TimeoutError):
        raise RuntimeError("Dify transport failed; a submitted request may have succeeded") from None


def api(base, path, body=None):
    with request(base, path, body) as response:
        return json.load(response)


def events(response):
    """Bounded SSE records; comments and unrelated fields are ignored."""
    data, size = [], 0
    for raw in response:
        size += len(raw)
        if size > 65536:
            raise ValueError("Dify event exceeds the example's 64 KiB limit")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield json.loads("\n".join(data))
            data, size = [], 0
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        raise ValueError("Dify stream ended in an incomplete event")


def question_for(operation):
    return (f"Approve {operation['action']} for {operation['target']}? "
            + json.dumps(operation["parameters"], sort_keys=True, ensure_ascii=False))


def save(folder, name, value):
    temporary = folder / (name + ".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(folder / name)


def validate_form(form, question):
    if (form.get("form_content") != question or form.get("inputs") != []
            or form.get("resolved_default_values") != {}
            or form.get("user_actions") != [
                {"id": "approve", "title": "Approve", "button_style": "primary"},
                {"id": "reject", "title": "Reject", "button_style": "default"}]):
        raise ValueError("The native form does not match the reviewed approval-only template")
    if type(form.get("expiration_time")) is not int or form["expiration_time"] <= time.time():
        raise ValueError("The native form has no active bounded deadline")


def start(folder, *, base_url, workflow_id, node_id, tenant_id, external_id,
          action, target, parameters, expires_at):
    """Start once from trusted application values; never automatically restart a run."""
    operation = dict(base_url=base_url, workflow_id=workflow_id, node_id=node_id,
                     tenant_id=tenant_id, external_id=external_id, action=action,
                     target=target, parameters=parameters, expires_at=expires_at, revision=REVISION)
    for name in ("workflow_id", "node_id", "tenant_id", "external_id", "action", "target"):
        value = operation[name]
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,100}", value):
            raise ValueError(f"Invalid {name}")
    facts = derive_parameters(parameters)
    if facts is None or len(facts) > 31 or "pushary_binding" in facts:
        raise ValueError("Use 1–31 bounded flat parameters; pushary_binding is reserved")
    operation["parameters"] = facts
    question = question_for(operation)
    if len(question.encode("utf-16-le")) // 2 > 500:
        raise ValueError("The entire action must fit the 500-character phone question")
    if type(expires_at) is not int or not time.time() < expires_at <= time.time() + 3600:
        raise ValueError("Choose a deadline within the next hour")
    # Namespace Dify's end user across tenants. Never accept identity from a model.
    operation["dify_user"] = decision_fingerprint([tenant_id, external_id])
    folder = Path(folder)
    folder.mkdir(mode=0o700)
    save(folder, "operation.json", operation)
    pending = None
    with request(base_url, "/workflows/run", {
        "inputs": {"question": question}, "user": operation["dify_user"], "response_mode": "streaming"
    }) as response:
        if "text/event-stream" not in response.headers.get("Content-Type", ""):
            raise ValueError("Expected native streaming workflow events")
        for event in events(response):
            if event.get("event") == "human_input_required":
                if pending is not None or event["data"]["node_id"] != node_id:
                    raise ValueError("Expected exactly one configured Human Input node")
                token = event["data"].get("form_token")
                if not isinstance(token, str) or not token or len(token) > 1024:
                    raise ValueError("Human Input requires Web App delivery and a form token")
                pending = dict(run_id=event["workflow_run_id"], token=token)
            if event.get("event") == "workflow_paused":
                if not pending or event["workflow_run_id"] != pending["run_id"]:
                    raise ValueError("The pending form does not belong to this paused run")
                break
        else:
            raise ValueError("Workflow did not pause; reconcile this run before starting another")
    run = api(base_url, "/workflows/run/" + quote(pending["run_id"], safe=""))
    if run["workflow_id"] != workflow_id or run["status"] != "paused":
        raise ValueError("Unexpected workflow version or run state")
    form = api(base_url, "/form/human_input/" + quote(pending["token"], safe=""))
    validate_form(form, question)
    pending.update(form=form, operation_hash=decision_fingerprint(operation))
    save(folder, "pending.json", pending)


def resume(folder, effect, *, base_url, workflow_id, tenant_id, external_id):
    """Consume one permit, resume the native form, then execute one local effect."""
    folder = Path(folder)
    operation = json.loads((folder / "operation.json").read_text())
    pending = json.loads((folder / "pending.json").read_text())
    for name, expected in dict(base_url=base_url, workflow_id=workflow_id, tenant_id=tenant_id,
                               external_id=external_id, revision=REVISION).items():
        if operation[name] != expected:
            raise ValueError(f"Saved {name} differs from the trusted caller")
    if decision_fingerprint(operation) != pending["operation_hash"]:
        raise ValueError("Operation changed since the native pause")
    deadline = min(operation["expires_at"], pending["form"]["expiration_time"])
    if time.time() >= deadline:
        return {"blocked": "Approval expired"}
    question = question_for(operation)
    form_path = "/form/human_input/" + quote(pending["token"], safe="")
    form = api(base_url, form_path)
    validate_form(form, question)
    if form != pending["form"]:
        raise ValueError("The native form changed after it was bound")
    binding = decision_fingerprint(dict(operation=operation, pending=pending))

    def execute():
        if time.time() >= deadline:
            raise TimeoutError("Approval expired before native submission")
        api(base_url, form_path, {"action": "approve", "inputs": {}, "user": operation["dify_user"]})
        # Only read-only polling is repeated. A lost POST or uncertain effect is not retried.
        end = min(deadline, time.time() + 30)
        while time.time() < end:
            run = api(base_url, "/workflows/run/" + quote(pending["run_id"], safe=""))
            if run["id"] != pending["run_id"] or run["workflow_id"] != workflow_id:
                raise ValueError("Native run identity changed")
            if run["status"] == "succeeded":
                if run.get("error") or run.get("outputs") != {"action": "approve", "question": question}:
                    raise ValueError("Native output does not match the approved operation")
                if time.time() >= deadline:
                    raise TimeoutError("Approval expired before the business effect")
                return effect(operation["parameters"].copy(), binding)
            if run["status"] not in ("running", "paused"):
                raise RuntimeError("Native workflow did not succeed")
            time.sleep(0.25)
        raise TimeoutError("Native resume is uncertain; reconcile without replaying the effect")

    protect = AdapterKernel("the Dify Human Input example").create_protect(
        policy=False, timeout_seconds=0, expires_in_seconds=3600)
    result = protect(operation["action"], execute, external_id=external_id,
                     run_id=pending["run_id"], call_id=operation["node_id"],
                     target=operation["target"], actor=tenant_id, question=question,
                     facts={**operation["parameters"], "pushary_binding": binding})
    return {"result": result.result} if result.ok else {"blocked": result.reason}
