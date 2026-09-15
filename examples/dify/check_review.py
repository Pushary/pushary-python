"""Real Dify graph engine and Pushary SDK; both hosted HTTP services are simulated."""
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import UUID
import yaml

from graphon.entities.graph_init_params import GraphInitParams
from graphon.graph import Graph
from graphon.graph_engine import GraphEngine
from graphon.graph_engine.command_channels import InMemoryChannel
from graphon.nodes.start.start_node import StartNode
from graphon.nodes.start.entities import StartNodeData
from graphon.nodes.human_input.human_input_node import HumanInputNode
from graphon.nodes.human_input.entities import HumanInputNodeData, PauseRequested, Completed
from graphon.nodes.end.end_node import EndNode
from graphon.nodes.end.entities import EndNodeData
from graphon.runtime import GraphRuntimeState, VariablePool
from graphon.variables.factory import build_segment
from pushary.errors import PusharyError
import review


def native(snapshot=None, action=None, question=""):
    document = yaml.safe_load(Path(__file__).with_name("workflow.yml").read_text())
    nodes = {node["id"]: node["data"] for node in document["workflow"]["graph"]["nodes"]}
    edges = {(edge["source"], edge["sourceHandle"], edge["target"])
             for edge in document["workflow"]["graph"]["edges"]}
    assert edges == {("start", "source", "review"), ("review", "approve", "approved"),
                     ("review", "reject", "stopped"), ("review", "__timeout", "stopped")}
    assert {node["type"] for node in nodes.values()} == {"start", "human-input", "end"}
    delivery, = nodes["review"]["delivery_methods"]
    UUID(delivery["id"])  # Dify validates delivery IDs outside Graphon.
    assert delivery == {"id": delivery["id"], "type": "webapp", "enabled": True}
    assert nodes["stopped"]["outputs"] == [{"variable": "stopped_question",
        "value_type": "string", "value_selector": ["start", "question"]}]
    output_names = [o["variable"] for n in nodes.values() for o in n.get("outputs", [])]
    assert len(output_names) == len(set(output_names))
    state = (GraphRuntimeState.from_snapshot(snapshot) if snapshot else
             GraphRuntimeState(variable_pool=VariablePool(), start_at=time.perf_counter()))
    params = GraphInitParams(workflow_id="version-1", graph_config={"nodes": [], "edges": []},
                             run_context={"workflow_run_id": "run-1"}, call_depth=0)
    kw = dict(graph_init_params=params, graph_runtime_state=state)
    state.variable_pool.add(["start", "question"], question)
    start = StartNode(node_id="start", data=StartNodeData.model_validate(nodes["start"]), **kw)
    human = HumanInputNode(node_id="review", data=HumanInputNodeData.model_validate(nodes["review"]), **kw,
        hitl_callback=lambda ctx: (Completed(action, {}, {"__action_id": build_segment(action),
                                  "__rendered_content": build_segment(question)}) if action else PauseRequested("form-1")))
    end = EndNode(node_id="approved", data=EndNodeData.model_validate(nodes["approved"]), **kw)
    graph = (Graph.new().add_root(start).add_node(human)
             .add_node(end, from_node_id="review", source_handle="approve").build())
    events = list(GraphEngine(workflow_id="version-1", graph=graph, graph_runtime_state=state,
                              command_channel=InMemoryChannel()).run())
    expected = "GraphRunSucceededEvent" if action else "GraphRunPausedEvent"
    assert type(events[-1]).__name__ == expected
    return state.dumps(), dict(state.outputs)


def db():
    return sqlite3.connect(os.environ["CHECK_DB"], timeout=10)


class Stream(io.BytesIO):
    headers = {"Content-Type": "text/event-stream"}


def dify_request(base, path, body=None):
    assert path == "/workflows/run" and body["response_mode"] == "streaming"
    snapshot, _ = native()
    with db() as conn:
        conn.execute("insert into workflow values (?,?,?,?,?)",
                     ("run-1", snapshot, "paused", body["inputs"]["question"], body["user"]))
    events = [dict(event="human_input_required", workflow_run_id="run-1",
                   data={"node_id": "review", "form_token": "secret-form-token"}),
              dict(event="workflow_paused", workflow_run_id="run-1")]
    return Stream("".join("data: " + json.dumps(e) + "\n\n" for e in events).encode())


def dify_api(base, path, body=None):
    with db() as conn:
        run_id, snapshot, status, question, user = conn.execute("select * from workflow").fetchone()
        if path == "/workflows/run/run-1":
            return dict(id=run_id, workflow_id="version-1", status=status,
                        outputs={"action": "approve", "question": question} if status == "succeeded" else {})
        assert path == "/form/human_input/secret-form-token"
        if status != "paused":
            raise RuntimeError("Dify HTTP 412; form is one-shot")
        if body is None:
            return dict(form_content=question, inputs=[], resolved_default_values={},
                        user_actions=[{"id": "approve", "title": "Approve", "button_style": "primary"},
                                      {"id": "reject", "title": "Reject", "button_style": "default"}],
                        expiration_time=int(os.environ["CHECK_EXPIRY"]))
        assert body == {"action": "approve", "inputs": {}, "user": user}
        assert conn.execute("select count(*) from permits").fetchone()[0] == 1
        snapshot, outputs = native(snapshot, "approve", question)
        assert outputs == {"action": "approve", "question": question}
        conn.execute("update workflow set snapshot=?,status='succeeded'", (snapshot,))
        conn.commit()
        if os.environ.get("CHECK_SUBMIT_LOST"):
            raise TimeoutError("Native submission response lost")
        return {}


def pushary_http(self, method, path, *, body=None, **kwargs):
    with db() as conn:
        if path == "/decisions":
            key = body["idempotencyKey"]
            conn.execute("insert or ignore into decisions values (?,?,'pending')", (key, json.dumps(body)))
            original, status = conn.execute("select body,status from decisions where id=?", (key,)).fetchone()
            assert json.loads(original) == body
            return dict(decisionId=key, status="answered" if status in ("yes", "no") else status,
                        answered=status in ("yes", "no"), type="confirm", value=status)
        if path == "/authorizations/consume":
            original, status = conn.execute("select body,status from decisions where id=?",
                                            (body["authorizationId"],)).fetchone()
            assert status == "yes"
            for key in ("externalId", "toolName", "toolTarget", "actor", "parameters"):
                assert json.loads(original)[key] == body[key]
            try:
                conn.execute("insert into permits values (?,'running')", (body["authorizationId"],))
            except sqlite3.IntegrityError:
                raise PusharyError("Replay", 409, {"refusal": "already_consumed"})
            conn.commit()
            if os.environ.get("CHECK_PERMIT_LOST"):
                raise TimeoutError("Permit response lost")
            if os.environ.get("CHECK_EXPIRE"):
                patch("review.time.time", return_value=time.time() + 7200).start()
            return {"permitId": body["authorizationId"]}
        if path.endswith("/receipt"):
            conn.execute("update permits set outcome=?", (body["outcome"],))
            return {}
        raise AssertionError(path)


def effect(parameters, binding):
    with db() as conn:
        assert conn.execute("select status from workflow").fetchone()[0] == "succeeded"
        assert conn.execute("select count(*) from permits").fetchone()[0] == 1
        conn.execute("insert into effects values (?)", (binding,))
    if os.environ.get("CHECK_CRASH"):
        os._exit(23)
    return parameters


def worker(mode, folder, config):
    with patch("review.request", dify_request), patch("review.api", dify_api), patch(
            "pushary.client.PusharyServer._request", pushary_http):
        if mode == "start":
            review.start(folder, **config)
            return "paused"
        return review.resume(folder, effect, **{key: config[key] for key in
            ("base_url", "workflow_id", "tenant_id", "external_id")})


def main():
    os.environ.update(PUSHARY_API_KEY="pk_offline.sk_offline", CHECK_EXPIRY=str(int(time.time()) + 3600))
    config = dict(base_url="https://dify.example/v1", workflow_id="version-1", node_id="review",
                  tenant_id="tenant-1", external_id="customer-1", action="order.release", target="order-1",
                  parameters={"order_id": "order-1", "amount_cents": 1900}, expires_at=int(os.environ["CHECK_EXPIRY"]))
    with tempfile.TemporaryDirectory() as temp:
        os.environ["CHECK_DB"] = str(Path(temp) / "api.db")
        with db() as conn:
            conn.executescript("create table workflow(id primary key,snapshot,status,question,user);"
                "create table decisions(id primary key,body,status);create table permits(id primary key,outcome);"
                "create table effects(id primary key);")
        def sql(query):
            with db() as conn:
                return conn.execute(query).fetchall()
        def spawn(mode, folder, c=config, **env):
            return subprocess.Popen([sys.executable, __file__, mode, str(folder), json.dumps(c)],
                env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def run(mode, folder, c=config, code=0, **env):
            proc = spawn(mode, folder, c, **env)
            output, error = proc.communicate(timeout=30)
            assert proc.returncode == code, error.decode()
            return json.loads(output) if code == 0 else None
        counter = 0
        def new():
            nonlocal counter
            counter += 1
            for table in ("workflow", "decisions", "permits", "effects"):
                sql(f"delete from {table}")
            folder = Path(temp) / str(counter)
            run("start", folder)
            assert "blocked" in run("resume", folder)
            assert sql("select status from workflow") == [("paused",)]
            assert not sql("select * from effects")
            return folder

        folder = new()
        run("start", folder, code=1)
        run("resume", folder)
        assert len(sql("select * from decisions")) == 1
        sql("update decisions set status='yes'")
        assert run("resume", folder)["result"] == config["parameters"]
        run("resume", folder, code=1)
        assert len(sql("select * from effects")) == 1
        for status in ("no", "expired", "cancelled"):
            folder = new(); sql(f"update decisions set status='{status}'")
            assert "blocked" in run("resume", folder)
            assert not sql("select * from effects")
        for key in ("base_url", "workflow_id", "tenant_id", "external_id"):
            folder = new(); sql("update decisions set status='yes'")
            run("resume", folder, {**config, key: "changed"}, code=1)
            assert not sql("select * from permits")
        folder = new(); sql("update decisions set status='yes'")
        saved = json.loads((folder / "operation.json").read_text())
        saved["parameters"]["amount_cents"] = 9999
        (folder / "operation.json").write_text(json.dumps(saved))
        run("resume", folder, code=1)
        assert not sql("select * from permits")
        folder = new(); sql("update workflow set status='succeeded'")  # Another native form submitter.
        run("resume", folder, code=1)
        assert not sql("select * from effects")
        for flag, code, effects in (("CHECK_PERMIT_LOST", 0, 0), ("CHECK_SUBMIT_LOST", 1, 0),
                                    ("CHECK_EXPIRE", 1, 0), ("CHECK_CRASH", 23, 1)):
            folder = new(); sql("update decisions set status='yes'")
            run("resume", folder, code=code, **{flag: "1"})
            assert len(sql("select * from effects")) == effects
            proc = spawn("resume", folder); proc.communicate(timeout=30)
            assert len(sql("select * from effects")) == effects
        folder = new(); sql("update decisions set status='yes'")
        processes = [spawn("resume", folder) for _ in range(2)]
        for proc in processes:
            proc.communicate(timeout=30)
        assert len(sql("select * from effects")) == 1
    assert list(review.events(io.BytesIO(b': ping\n\ndata: {"ok":\ndata: true}\n\n'))) == [{"ok": True}]
    print("PASS: native graph pause/restore, fresh-process SDK permit, bindings, refusals, races and uncertain outcomes")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(json.dumps(worker(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))))
    else:
        main()
