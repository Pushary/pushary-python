"""Offline integration checks: real workflow/SDK, simulated HTTP/effect, fresh workers."""
import asyncio
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
from workflows import Context
from workflows.events import HumanResponseEvent
from review import CustomerReview, encoded

ACTION = {"order_id": "order_1", "amount_cents": 4800, "draft_version": 1}


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


async def worker(command, db, scenario=""):
    arguments = dict(ACTION)
    customer = "wrong" if scenario == "customer" else "customer_1"
    if scenario == "arguments":
        arguments["amount_cents"] = 4900
    async def refund(action):
        """Simulate a refund. Args: order_id: Order. amount_cents: EUR cents. draft_version: Immutable version."""
        assert action == ACTION
        if scenario == "exception":
            raise RuntimeError("action failed before receipt")
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO effects VALUES('refund_order_1_v1')")
        if scenario == "crash":
            os._exit(17)  # A real killed worker after committing the simulated business receipt.
        return "simulated refund"

    review = CustomerReview(db, PusharyServer("pk_simulated.sk_not_a_real_key"),
                            tenant="tenant_1", customer=customer, operation="refund_order_1_v1",
                            action=arguments, execute=refund,
                            code_version="changed" if scenario == "version" else "refund-v1")
    with patch("pushary.client.urllib.request.urlopen", side_effect=lambda *a, **kw: transport(db, *a, **kw)):
        if scenario == "bypass":
            row = review.row()
            handler = review.run(ctx=Context.from_dict(review, json.loads(row["snapshot"])))
            await handler.send_event(HumanResponseEvent(response="yes", operation=review.key, grant=review.key))
            assert (await handler)["approved"] is False
            result = {"phase": "bypass-denied"}
        elif scenario == "http-error":
            with patch("pushary.client.urllib.request.urlopen", side_effect=OSError("offline")):
                result = await review.resume_review()
        else:
            result = await review.start_review() if command == "start" else await review.resume_review()
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
        self.assertIs(first["output"]["approved"], True)

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
        self.answer(question=f"Approve action? {encoded(ACTION)}", status="unknown")
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

    def test_action_exception_requires_reconciliation(self):
        self.answer()
        self.run_worker("resume", "exception", ok=False)
        self.assertEqual(self.run_worker("resume")["phase"], "uncertain")
        self.assertEqual(self.effects(), 0)

    def test_lost_create_response_recovers_same_decision(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE reviews SET decision_id=NULL")
        self.run_worker("start")
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM decisions").fetchone()[0], 1)
        self.answer()
        self.run_worker("resume")
        self.assertEqual(self.effects(), 1)

    def test_duplicate_create(self):
        self.run_worker("start")
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM decisions").fetchone()[0], 1)
        self.assertEqual(self.effects(), 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("start", "resume"):
        asyncio.run(worker(*sys.argv[1:]))
    else:
        unittest.main()
