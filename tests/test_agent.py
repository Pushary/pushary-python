"""Tests for the two-call agent flow: enroll(), decisions.ask(), and the helpers."""

import unittest

from pushary import PusharyServer, deterministic_key, is_approved
from pushary.decisions import DecisionsResource


VALID_KEY = "pk_live_abc.sk_live_xyz"


class SeqRecorder:
    """Request stub that records every call and returns a queued result each time."""

    def __init__(self, results):
        self.calls = []
        self.results = list(results)
        self.i = 0

    def __call__(self, method, path, *, body=None, params=None, timeout_seconds=None):
        self.calls.append({"method": method, "path": path, "body": body, "params": params})
        result = self.results[min(self.i, len(self.results) - 1)]
        self.i += 1
        return result


class EnrollTests(unittest.TestCase):
    def test_enroll_posts_external_id(self):
        client = PusharyServer(api_key=VALID_KEY)
        rec = SeqRecorder([
            {
                "externalId": "user_9",
                "token": "tok",
                "deepLink": "pushary://enroll?token=tok",
                "universalLink": "https://pushary.com/e/tok",
                "expiresInSeconds": 900,
            }
        ])
        client._request = rec  # type: ignore[assignment]
        result = client.enroll("user_9")
        self.assertEqual(rec.last_method_path(), ("POST", "/enroll"))
        self.assertEqual(rec.calls[0]["body"], {"externalId": "user_9"})
        self.assertEqual(result["universalLink"], "https://pushary.com/e/tok")


class AskTests(unittest.TestCase):
    def test_ask_creates_async_then_polls_until_answered_and_is_approved(self):
        rec = SeqRecorder([
            {"decisionId": "d", "status": "pending", "answered": False, "type": "confirm"},
            {"decisionId": "d", "status": "answered", "answered": True, "value": "yes", "type": "confirm"},
        ])
        result = DecisionsResource(rec).ask("Ship it?", external_id="user_9", timeout_seconds=5)

        create, poll = rec.calls[0], rec.calls[1]
        self.assertEqual(create["method"], "POST")
        self.assertEqual(create["path"], "/decisions")
        self.assertEqual(create["body"]["wait"], False)
        self.assertEqual(create["body"]["externalId"], "user_9")
        self.assertTrue(create["body"]["idempotencyKey"])
        self.assertEqual(poll["method"], "GET")
        self.assertEqual(poll["path"], "/decisions/d")
        self.assertIn("wait", poll["params"])

        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["answered"])
        self.assertTrue(result["approved"])
        self.assertEqual(result["value"], "yes")

    def test_ask_is_fail_closed_on_decline(self):
        rec = SeqRecorder([
            {"decisionId": "d", "status": "pending", "answered": False, "type": "confirm"},
            {"decisionId": "d", "status": "answered", "answered": True, "value": "no", "type": "confirm"},
        ])
        result = DecisionsResource(rec).ask("Delete prod?", external_id="u", timeout_seconds=5)
        self.assertTrue(result["answered"])
        self.assertFalse(result["approved"])

    def test_ask_does_not_poll_when_timeout_is_zero(self):
        rec = SeqRecorder([
            {"decisionId": "d", "status": "pending", "answered": False, "type": "confirm"}
        ])
        result = DecisionsResource(rec).ask("q", external_id="u", timeout_seconds=0)
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["approved"])

    def test_ask_reuses_explicit_idempotency_key(self):
        rec = SeqRecorder([
            {"decisionId": "d", "status": "answered", "answered": True, "value": "yes", "type": "confirm"}
        ])
        DecisionsResource(rec).ask("q", external_id="u", idempotency_key="my-key", timeout_seconds=0)
        self.assertEqual(rec.calls[0]["body"]["idempotencyKey"], "my-key")

    def test_ask_generates_a_unique_key_per_call_when_omitted(self):
        # Two asks with identical text must not collapse into one silent auto-approval.
        rec1 = SeqRecorder([{"decisionId": "d", "status": "pending", "answered": False, "type": "confirm"}])
        rec2 = SeqRecorder([{"decisionId": "d", "status": "pending", "answered": False, "type": "confirm"}])
        DecisionsResource(rec1).ask("same", external_id="u", timeout_seconds=0)
        DecisionsResource(rec2).ask("same", external_id="u", timeout_seconds=0)
        k1 = rec1.calls[0]["body"]["idempotencyKey"]
        k2 = rec2.calls[0]["body"]["idempotencyKey"]
        self.assertTrue(k1 and k2)
        self.assertNotEqual(k1, k2)

    def test_ask_forwards_powered_by(self):
        rec = SeqRecorder([
            {"decisionId": "d", "status": "answered", "answered": True, "value": "yes", "type": "confirm"}
        ])
        DecisionsResource(rec).ask("q", external_id="u", powered_by=False, timeout_seconds=0)
        self.assertEqual(rec.calls[0]["body"]["poweredBy"], False)


class HelperTests(unittest.TestCase):
    def test_deterministic_key_stable_and_distinct(self):
        self.assertEqual(deterministic_key(["a", "b"]), deterministic_key(["a", "b"]))
        self.assertNotEqual(deterministic_key(["a", "b"]), deterministic_key(["a", "c"]))
        self.assertRegex(deterministic_key(["a", "b"]), r"^[0-9a-f]{40}$")

    def test_is_approved(self):
        self.assertTrue(is_approved("answered", "confirm", "yes"))
        self.assertFalse(is_approved("answered", "confirm", "no"))
        self.assertFalse(is_approved("expired", "confirm", None))
        self.assertTrue(is_approved("answered", "select", "A"))
        self.assertFalse(is_approved("cancelled", "input", None))


def _last_method_path(self):
    call = self.calls[-1]
    return (call["method"], call["path"])


SeqRecorder.last_method_path = _last_method_path


if __name__ == "__main__":
    unittest.main()

class DeadlineBudgetTests(unittest.TestCase):
    def test_creation_spends_the_wait_budget_and_bounds_transport(self):
        import time
        from pushary.decisions import DecisionsResource
        calls = []
        def request(method, path, **kwargs):
            calls.append(kwargs)
            time.sleep(0.02)
            return {'decisionId': 'd', 'status': 'pending', 'answered': False}
        result = DecisionsResource(request).ask('Approve?', timeout_seconds=0.01)
        self.assertLessEqual(calls[0].get('timeout_seconds', 65), 0.01)
        self.assertEqual(len(calls), 1)
        self.assertFalse(result['approved'])
