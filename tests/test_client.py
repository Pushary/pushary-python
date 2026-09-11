"""Tests for the client and decisions resource. No network: the transport is
either a recording stub or a patched urlopen.
"""

import io
import json
import unittest
import urllib.error
from unittest import mock

from pushary import PusharyError, PusharyServer
from pushary.client import DEFAULT_BASE_URL
from pushary.decisions import DecisionsResource


VALID_KEY = "pk_live_abc.sk_live_xyz"


class Recorder:
    """Stand-in for the client request function that records every call."""

    def __init__(self, result=None):
        self.calls = []
        self.result = {} if result is None else result

    def __call__(self, method, path, *, body=None, params=None, timeout_seconds=None):
        self.calls.append(
            {"method": method, "path": path, "body": body, "params": params}
        )
        return self.result

    @property
    def last(self):
        return self.calls[-1]


class ApiKeyValidationTests(unittest.TestCase):
    def test_empty_key_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            PusharyServer(api_key="")
        self.assertIn("dashboard/settings", str(ctx.exception))

    def test_key_without_dot_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            PusharyServer(api_key="pk_live_only")
        self.assertIn("pk_xxx.sk_xxx", str(ctx.exception))

    def test_valid_key_constructs(self):
        client = PusharyServer(api_key=VALID_KEY)
        self.assertIsInstance(client.decisions, DecisionsResource)

    def test_default_and_custom_base_url(self):
        default_client = PusharyServer(api_key=VALID_KEY)
        self.assertEqual(default_client._base_url, DEFAULT_BASE_URL)

        custom = PusharyServer(api_key=VALID_KEY, base_url="https://example.com/api/")
        self.assertEqual(custom._base_url, "https://example.com/api")

    def test_authorization_header_carries_full_key(self):
        client = PusharyServer(api_key=VALID_KEY)
        self.assertEqual(client._headers["Authorization"], f"Bearer {VALID_KEY}")
        self.assertEqual(client._headers["Content-Type"], "application/json")


class DecisionsResourceTests(unittest.TestCase):
    def test_presentation_reaches_the_wire_from_create_and_ask(self):
        recorder = Recorder({"decisionId": "d1", "status": "pending", "answered": False})
        presentation = {
            "label": "Refund", "effect": "Return funds",
            "changes": [{"parameter": "amount", "label": "Amount", "format": {"kind": "quantity"}}],
        }
        decisions = DecisionsResource(recorder)
        decisions.create("Approve?", parameters={"amount": 10}, presentation=presentation)
        decisions.ask("Approve?", parameters={"amount": 10}, presentation=presentation, timeout_seconds=0)
        for call in recorder.calls:
            self.assertEqual(call["body"]["parameters"], {"amount": 10})
            self.assertEqual(call["body"]["presentation"], presentation)

    def test_create_maps_snake_to_camel_and_omits_none(self):
        recorder = Recorder({"decisionId": "d_1", "status": "pending"})
        decisions = DecisionsResource(recorder)

        decisions.create(
            "Approve payout?",
            type="confirm",
            external_id="user_123",
            callback_url="https://app.test/hook",
            agent_name="Billing Agent",
            context="Invoice INV-1",
            placeholder=None,
            expires_in_seconds=3600,
            wait=True,
            timeout_seconds=50,
            idempotency_key="idem-1",
            powered_by=False,
        )

        call = recorder.last
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["path"], "/decisions")
        self.assertEqual(
            call["body"],
            {
                "question": "Approve payout?",
                "type": "confirm",
                "externalId": "user_123",
                "callbackUrl": "https://app.test/hook",
                "agentName": "Billing Agent",
                "context": "Invoice INV-1",
                "expiresInSeconds": 3600,
                "wait": True,
                "timeoutSeconds": 50,
                "idempotencyKey": "idem-1",
                "poweredBy": False,
            },
        )
        self.assertNotIn("placeholder", call["body"])

    def test_create_minimal_only_sends_question_and_type(self):
        recorder = Recorder()
        DecisionsResource(recorder).create("Ship it?")
        self.assertEqual(
            recorder.last["body"], {"question": "Ship it?", "type": "confirm"}
        )

    def test_create_select_includes_options(self):
        recorder = Recorder()
        DecisionsResource(recorder).create(
            "Pick one", type="select", options=["A", "B"]
        )
        self.assertEqual(recorder.last["body"]["options"], ["A", "B"])

    def test_get_without_wait_sends_no_params(self):
        recorder = Recorder()
        DecisionsResource(recorder).get("d_1")
        self.assertEqual(recorder.last["method"], "GET")
        self.assertEqual(recorder.last["path"], "/decisions/d_1")
        self.assertIsNone(recorder.last["params"])

    def test_get_with_wait_sends_param(self):
        recorder = Recorder()
        DecisionsResource(recorder).get("d_1", wait=50)
        self.assertEqual(recorder.last["params"], {"wait": 50})

    def test_answer_posts_body(self):
        recorder = Recorder()
        DecisionsResource(recorder).answer("d_1", "yes")
        self.assertEqual(recorder.last["method"], "POST")
        self.assertEqual(recorder.last["path"], "/decisions/d_1")
        self.assertEqual(recorder.last["body"], {"answer": "yes"})

    def test_cancel_deletes(self):
        recorder = Recorder()
        DecisionsResource(recorder).cancel("d_1")
        self.assertEqual(recorder.last["method"], "DELETE")
        self.assertEqual(recorder.last["path"], "/decisions/d_1")

    def test_decision_id_is_percent_encoded(self):
        recorder = Recorder()
        DecisionsResource(recorder).get("a/b?c")
        self.assertEqual(recorder.last["path"], "/decisions/a%2Fb%3Fc")

    def test_webhook_secret_methods(self):
        recorder = Recorder()
        resource = DecisionsResource(recorder)

        resource.get_webhook_secret()
        self.assertEqual(recorder.last["method"], "GET")
        self.assertEqual(recorder.last["path"], "/webhook-secret")

        resource.rotate_webhook_secret()
        self.assertEqual(recorder.last["method"], "POST")
        self.assertEqual(recorder.last["path"], "/webhook-secret")

    def test_create_forwards_require_reachable(self):
        recorder = Recorder({"decisionId": "d_1", "status": "pending"})
        DecisionsResource(recorder).create(
            "Ship it?", external_id="user_9", require_reachable=True
        )
        self.assertEqual(recorder.last["body"]["requireReachable"], True)

    def test_create_omits_require_reachable_when_unset(self):
        recorder = Recorder()
        DecisionsResource(recorder).create("Ship it?")
        self.assertNotIn("requireReachable", recorder.last["body"])

    def test_ask_surfaces_reachability_from_create(self):
        recorder = Recorder(
            {
                "decisionId": "d_1",
                "status": "answered",
                "answered": True,
                "value": "yes",
                "type": "confirm",
                "reachable": True,
                "reachableChannels": 2,
                "deviceCount": 3,
            }
        )
        result = DecisionsResource(recorder).ask("Ship it?", external_id="user_9")
        self.assertTrue(result["approved"])
        self.assertTrue(result["reachable"])
        self.assertEqual(result["reachableChannels"], 2)
        self.assertEqual(result["deviceCount"], 3)


class KeysResourceTests(unittest.TestCase):
    def test_issue_posts_external_id(self):
        recorder = Recorder({"apiKey": "pk_x.secret", "keyPrefix": "pk_x"})
        PusharyServer(api_key=VALID_KEY)  # smoke: keys attribute exists
        from pushary.keys import KeysResource

        KeysResource(recorder).issue("user_9", name="Session A", expires_in_seconds=3600)
        self.assertEqual(recorder.last["method"], "POST")
        self.assertEqual(recorder.last["path"], "/keys")
        self.assertEqual(
            recorder.last["body"],
            {"externalId": "user_9", "name": "Session A", "expiresInSeconds": 3600},
        )

    def test_issue_minimal_only_sends_external_id(self):
        from pushary.keys import KeysResource

        recorder = Recorder()
        KeysResource(recorder).issue("user_9")
        self.assertEqual(recorder.last["body"], {"externalId": "user_9"})

    def test_list_returns_keys_array(self):
        from pushary.keys import KeysResource

        recorder = Recorder({"keys": [{"keyPrefix": "pk_x"}]})
        keys = KeysResource(recorder).list()
        self.assertEqual(recorder.last["method"], "GET")
        self.assertEqual(recorder.last["path"], "/keys")
        self.assertEqual(keys, [{"keyPrefix": "pk_x"}])

    def test_list_tolerates_missing_keys(self):
        from pushary.keys import KeysResource

        self.assertEqual(KeysResource(Recorder({})).list(), [])

    def test_revoke_deletes_encoded_prefix(self):
        from pushary.keys import KeysResource

        recorder = Recorder({"keyPrefix": "pk_x", "revoked": True})
        KeysResource(recorder).revoke("pk_x/y")
        self.assertEqual(recorder.last["method"], "DELETE")
        self.assertEqual(recorder.last["path"], "/keys/pk_x%2Fy")

    def test_client_exposes_keys_resource(self):
        from pushary.keys import KeysResource

        client = PusharyServer(api_key=VALID_KEY)
        self.assertIsInstance(client.keys, KeysResource)


def _fake_response(payload):
    body = json.dumps(payload).encode("utf-8")

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _Resp()


class TransportTests(unittest.TestCase):
    def test_get_builds_url_and_returns_dict(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["timeout"] = timeout
            captured["auth"] = request.get_header("Authorization")
            return _fake_response({"decisionId": "d_1", "status": "answered", "value": "yes"})

        client = PusharyServer(api_key=VALID_KEY)
        with mock.patch("pushary.client.urllib.request.urlopen", fake_urlopen):
            result = client.decisions.get("d_1", wait=50)

        self.assertEqual(captured["url"], f"{DEFAULT_BASE_URL}/decisions/d_1?wait=50")
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["auth"], f"Bearer {VALID_KEY}")
        self.assertGreater(captured["timeout"], 55)
        self.assertEqual(result["value"], "yes")

    def test_post_sends_json_body(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["data"] = request.data
            captured["content_type"] = request.get_header("Content-type")
            return _fake_response({"decisionId": "d_1", "status": "pending"})

        client = PusharyServer(api_key=VALID_KEY)
        with mock.patch("pushary.client.urllib.request.urlopen", fake_urlopen):
            client.decisions.create("Ship it?", external_id="user_9")

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], f"{DEFAULT_BASE_URL}/decisions")
        self.assertEqual(captured["content_type"], "application/json")
        self.assertEqual(
            json.loads(captured["data"].decode("utf-8")),
            {"question": "Ship it?", "type": "confirm", "externalId": "user_9"},
        )

    def test_http_error_raises_pushary_error_with_status_and_reason(self):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                url=request.full_url,
                code=404,
                msg="Not Found",
                hdrs=None,
                fp=io.BytesIO(b'{"error":"Decision not found or expired"}'),
            )

        client = PusharyServer(api_key=VALID_KEY)
        with mock.patch("pushary.client.urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(PusharyError) as ctx:
                client.decisions.get("missing")

        error = ctx.exception
        self.assertEqual(error.status, 404)
        self.assertEqual(error.message, "Decision not found or expired")

    def test_http_error_without_body_falls_back_to_status(self):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                url=request.full_url,
                code=500,
                msg="Server Error",
                hdrs=None,
                fp=io.BytesIO(b""),
            )

        client = PusharyServer(api_key=VALID_KEY)
        with mock.patch("pushary.client.urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(PusharyError) as ctx:
                client.decisions.cancel("d_1")

        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(ctx.exception.message, "HTTP 500")

    def test_url_error_raises_pushary_error(self):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        client = PusharyServer(api_key=VALID_KEY)
        with mock.patch("pushary.client.urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(PusharyError):
                client.decisions.get("d_1")


if __name__ == "__main__":
    unittest.main()


class AuthorizeTests(unittest.TestCase):
    """Policy first, a person only when policy defers."""

    def _client(self, responses):
        client = PusharyServer(api_key=VALID_KEY)
        calls = []

        def fake(method, path, *, body=None, params=None, timeout_seconds=None):
            calls.append({"method": method, "path": path, "body": body})
            return responses[min(len(calls) - 1, len(responses) - 1)]

        client._request = fake
        client.decisions = DecisionsResource(fake)
        return client, calls

    def test_policy_allow_pages_nobody(self):
        client, calls = self._client(
            [{"verdict": "allow", "policy": "crm.read", "reason": "ok", "authorizationId": "a1"}]
        )
        result = client.authorize("crm.read", external_id="u1")
        self.assertTrue(result["approved"])
        self.assertEqual(result["resolvedBy"], "policy")
        self.assertEqual(result["authorizationId"], "a1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["path"], "/authorize")

    def test_policy_deny_pages_nobody(self):
        client, calls = self._client(
            [{"verdict": "deny", "policy": "infra.delete", "reason": "no", "authorizationId": "a2"}]
        )
        result = client.authorize("infra.delete", external_id="u1")
        self.assertFalse(result["approved"])
        self.assertEqual(result["resolvedBy"], "policy")
        self.assertEqual(len(calls), 1)

    def test_defers_to_a_person_and_labels_the_ask(self):
        client, calls = self._client(
            [
                {"verdict": "requires_human", "policy": None, "reason": "no rule", "authorizationId": None},
                {"decisionId": "d1", "status": "answered", "answered": True, "value": "yes", "type": "confirm"},
            ]
        )
        result = client.authorize("refund.create", tool_target="order_1", external_id="u1")
        self.assertTrue(result["approved"])
        self.assertEqual(result["resolvedBy"], "human")
        self.assertEqual(result["decisionId"], "d1")
        ask = calls[1]["body"]
        self.assertEqual(ask["toolName"], "refund.create")
        self.assertEqual(ask["question"], "Approve refund.create order_1?")

    def test_fails_closed_when_nobody_answers(self):
        client, _ = self._client(
            [
                {"verdict": "requires_human", "policy": None, "reason": "no rule", "authorizationId": None},
                {"decisionId": "d2", "status": "pending", "answered": False, "value": None, "type": "confirm"},
            ]
        )
        result = client.authorize("refund.create", external_id="u1", timeout_seconds=0)
        self.assertFalse(result["approved"])
        self.assertIn("Nobody answered", result["reason"])


class StructuredActionModelTests(unittest.TestCase):
    """actor / environment / parameters reach both calls, and only when sent."""

    def _client(self, responses):
        client = PusharyServer(api_key=VALID_KEY)
        calls = []

        def fake(method, path, *, body=None, params=None, timeout_seconds=None):
            calls.append({"method": method, "path": path, "body": body})
            return responses[min(len(calls) - 1, len(responses) - 1)]

        client._request = fake
        client.decisions = DecisionsResource(fake)
        return client, calls

    def test_subject_reaches_the_evaluation_and_the_ask(self):
        client, calls = self._client(
            [
                {"verdict": "requires_human", "policy": None, "reason": "no rule", "authorizationId": None},
                {"decisionId": "d1", "status": "answered", "answered": True, "value": "yes", "type": "confirm"},
            ]
        )
        client.authorize(
            "refund.create",
            tool_target="order_1",
            actor="user:u_44",
            environment="production",
            parameters={"amount": 480, "currency": "USD"},
            external_id="u1",
        )
        for call in calls:
            self.assertEqual(call["body"]["actor"], "user:u_44")
            self.assertEqual(call["body"]["environment"], "production")
            self.assertEqual(call["body"]["parameters"], {"amount": 480, "currency": "USD"})

    def test_absent_fields_are_not_sent(self):
        client, calls = self._client(
            [{"verdict": "allow", "policy": "crm.read", "reason": "ok", "authorizationId": "a1"}]
        )
        client.authorize("crm.read", external_id="u1")
        for key in ("actor", "environment", "parameters"):
            self.assertNotIn(key, calls[0]["body"])

    def test_decisions_create_carries_the_subject(self):
        recorder = Recorder({"decisionId": "d1", "status": "pending"})
        DecisionsResource(recorder).create(
            "Approve?",
            actor="team:billing",
            environment="staging",
            parameters={"amount": 12, "urgent": True},
        )
        body = recorder.last["body"]
        self.assertEqual(body["actor"], "team:billing")
        self.assertEqual(body["environment"], "staging")
        self.assertEqual(body["parameters"], {"amount": 12, "urgent": True})
