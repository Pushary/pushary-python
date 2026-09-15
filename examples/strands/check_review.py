"""Offline integration checks: real SDKs, simulated model/HTTP/effect, fresh workers."""
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pushary import PusharyServer
from strands import Agent, Snapshot, tool
from strands.models import Model
from review import CustomerReview, encoded

ACTION = {"order_id": "order_1", "amount_cents": 4800, "draft_version": 1}


class ScriptedModel(Model):
    def __init__(self, start=False):
        self.start = start

    def get_config(self):
        return {"model_id": "offline-scripted"}

    def update_config(self, **kwargs):
        pass

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        yield {"messageStart": {"role": "assistant"}}
        if self.start:
            self.start = False
            yield {"contentBlockStart": {"start": {"toolUse": {"name": "refund", "toolUseId": "call_1"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": encoded(ACTION)}}}}
            stop = "tool_use"
        else:
            assert any("toolResult" in c for m in messages for c in m["content"]), "Must resume original call"
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "Reviewed."}}}
            stop = "end_turn"
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": stop}}


def transport(db, request, **kwargs):
    if request.method == "POST":
        body = json.loads(request.data)
        assert body["externalId"] == "customer_1" and body["type"] == "confirm"
        assert body["wait"] is False and body["requireReachable"] is True
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT OR IGNORE INTO decisions VALUES(?,?)",
                         (body["idempotencyKey"], encoded(dict(body, decisionId="decision_1", status="pending",
                                                              answered=False, value=None))))
            response = json.loads(conn.execute("SELECT body FROM decisions").fetchone()[0])
    else:
        with sqlite3.connect(db) as conn:
            response = json.loads(conn.execute("SELECT body FROM decisions").fetchone()[0])
    return io.BytesIO(encoded(response).encode())


def worker(command, db, scenario=""):
    arguments = dict(ACTION)
    customer = "wrong" if scenario == "customer" else "customer_1"
    if scenario == "arguments":
        arguments["amount_cents"] = 4900
    review = CustomerReview(db, PusharyServer("pk_simulated.sk_not_a_real_key"),
                            tenant="tenant_1", customer=customer, operation="refund_order_1_v1",
                            tool_name="refund", arguments=arguments, code_version="changed" if scenario == "version" else "refund-v1")

    @tool
    def refund(order_id: str, amount_cents: int, draft_version: int) -> str:
        """Simulate a refund. Args: order_id: Order. amount_cents: EUR cents. draft_version: Immutable version."""
        assert dict(order_id=order_id, amount_cents=amount_cents, draft_version=draft_version) == ACTION
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO effects VALUES('refund_order_1_v1')")
        if scenario == "crash":
            os._exit(17)  # A real killed worker after committing the simulated business receipt.
        return "simulated refund"

    agent = Agent(model=ScriptedModel(command == "start"), tools=[refund], hooks=[review], callback_handler=None)
    with patch("pushary.client.urllib.request.urlopen", side_effect=lambda *a, **kw: transport(db, *a, **kw)):
        if scenario == "bypass":
            row = review.row()
            agent.load_snapshot(Snapshot.from_dict(json.loads(row["snapshot"])))
            interruption = json.loads(row["interruption"])
            agent([{"interruptResponse": {"interruptId": interruption["id"], "response": True}}])
            result = {"phase": "bypass-denied"}
        elif scenario == "http-error":
            with patch("pushary.client.urllib.request.urlopen", side_effect=OSError("offline")):
                result = review.resume(agent)
        else:
            result = review.start(agent, "Propose the saved refund") if command == "start" else review.resume(agent)
    print(encoded(result))


class ReviewChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "reviews.sqlite")
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE decisions(key TEXT PRIMARY KEY,body TEXT)")
            conn.execute("CREATE TABLE effects(operation TEXT PRIMARY KEY)")
        self.run_worker("start")
        self.assertEqual(self.effects(), 0)

    def tearDown(self):
        self.tmp.cleanup()

    def run_worker(self, command, scenario="", ok=True):
        result = subprocess.run([sys.executable, __file__, command, self.db, scenario],
                                capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def answer(self, **values):
        with sqlite3.connect(self.db) as conn:
            body = json.loads(conn.execute("SELECT body FROM decisions").fetchone()[0])
            body.update(status="answered", answered=True, value="yes")
            body.update(values)
            conn.execute("UPDATE decisions SET body=?", (encoded(body),))

    def effects(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT count(*) FROM effects").fetchone()[0]

    def test_restart_approval_and_duplicate(self):
        self.answer()
        first = self.run_worker("resume")
        self.assertEqual(first, self.run_worker("resume"))
        self.assertEqual(self.effects(), 1)
        self.assertEqual(first["output"]["stop_reason"], "end_turn")

    def test_pending_denied_expired_cancelled(self):
        self.assertEqual(self.run_worker("resume")["phase"], "pending")
        for status, answered, value in [("answered", True, "no"), ("expired", False, None),
                                        ("cancelled", False, None), ("answered", False, "yes")]:
            with self.subTest(status=status, answered=answered):
                self.answer(status=status, answered=answered, value=value)
                self.run_worker("resume")
                self.assertEqual(self.effects(), 0)
                # Restore a paused reference for the next independent decision case.
                with sqlite3.connect(self.db) as conn:
                    conn.execute("UPDATE reviews SET phase='pending',output=NULL")

    def test_wrong_customer_arguments_and_context(self):
        self.answer()
        self.run_worker("resume", "customer", ok=False)
        self.run_worker("resume", "arguments", ok=False)
        self.run_worker("resume", "version", ok=False)
        self.answer(context="changed")
        self.run_worker("resume", ok=False)
        self.assertEqual(self.effects(), 0)

    def test_nonconfirm_and_wrong_recipient(self):
        self.answer(type="input")
        self.run_worker("resume", ok=False)
        self.answer(type="confirm", externalId="someone_else")
        self.run_worker("resume", ok=False)
        self.assertEqual(self.effects(), 0)

    def test_changed_snapshot(self):
        self.answer()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE reviews SET snapshot=snapshot || ' '")
        self.run_worker("resume", ok=False)
        self.assertEqual(self.effects(), 0)

    def test_retained_decision_uses_exact_binding(self):
        self.answer(externalId=None)
        self.run_worker("resume")
        self.assertEqual(self.effects(), 1)

    def test_changed_question_and_unknown_state(self):
        self.answer(question="Approve something else?")
        self.run_worker("resume", ok=False)
        self.answer(question=f"Approve refund? {encoded(ACTION)}", status="unknown")
        self.run_worker("resume", ok=False)
        self.assertEqual(self.effects(), 0)

    def test_changed_interruption(self):
        self.answer()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE reviews SET interruption=interruption || ' '")
        self.run_worker("resume", ok=False)
        self.assertEqual(self.effects(), 0)

    def test_direct_native_resume_cannot_bypass_review(self):
        self.run_worker("resume", "bypass")
        self.assertEqual(self.effects(), 0)

    def test_http_failure_does_not_consume_approval(self):
        self.answer()
        self.run_worker("resume", "http-error", ok=False)
        self.assertEqual(self.effects(), 0)
        self.run_worker("resume")
        self.assertEqual(self.effects(), 1)

    def test_concurrent_resumes(self):
        self.answer()
        processes = [subprocess.Popen([sys.executable, __file__, "resume", self.db],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for p in processes:
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err)
            self.assertIn(json.loads(out)["phase"], ("resuming", "complete"))
        self.assertEqual(self.effects(), 1)

    def test_uncertain_effect_never_replayed(self):
        self.answer()
        self.run_worker("resume", "crash", ok=False)
        self.assertEqual(self.run_worker("resume")["phase"], "resuming")
        self.assertEqual(self.effects(), 1)

    def test_duplicate_create(self):
        self.run_worker("start")
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM decisions").fetchone()[0], 1)
        self.assertEqual(self.effects(), 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("start", "resume"):
        worker(*sys.argv[1:])
    else:
        unittest.main()
