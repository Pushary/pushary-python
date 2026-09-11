"""Tests for the shared adapter kernel. Mirrors packages/server/src/adapters.test.ts,
so a rule proved in one language is proved in the other.
"""

import hashlib
import hmac
import json
import os
import unittest

from pushary import adapters
from pushary.errors import PusharyError
from pushary.adapters import (
    AdapterKernel,
    ApprovalAsk,
    default_protect_question,
    derive_parameters,
    decision_fingerprint,
    describe_answer,
    idempotency_key,
    is_affirmative,
    render_approval_question,
    resolve_pushary_callback,
)


SECRET = "whsec_test"


def sign(body: str) -> str:
    return hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


class FakeDecisions:
    def __init__(self, ask_result=None, create_result=None):
        self.ask_calls = []
        self.create_calls = []
        self._ask_result = ask_result or {}
        self._create_result = create_result or {}

    def ask(self, question, **kwargs):
        self.ask_calls.append({"question": question, **kwargs})
        return self._ask_result

    def create(self, question, **kwargs):
        self.create_calls.append({"question": question, **kwargs})
        return self._create_result


def evaluated(**overrides):
    return {
        "verdict": "requires_human",
        "policy": None,
        "reason": "No policy rule names this action, so a person decides.",
        "authorizationId": None,
        **overrides,
    }


def consumed(**overrides):
    return {
        "permitId": "permit_1",
        "authority": {"kind": "policy", "rulePattern": "refund.create"},
        "expiresAt": "2026-08-22T13:00:00.000Z",
        **overrides,
    }


class FakeClient:
    def __init__(
        self,
        decisions=None,
        enroll_result=None,
        evaluation=None,
        raises=None,
        consume_result=None,
        consume_raises=None,
        receipt_raises=None,
    ):
        self.decisions = decisions or FakeDecisions()
        self._enroll_result = enroll_result or {}
        self._evaluation = evaluated() if evaluation is None else evaluation
        self._raises = raises
        self._consume_result = consumed() if consume_result is None else consume_result
        self._consume_raises = consume_raises
        self._receipt_raises = receipt_raises
        self.enroll_calls = []
        self.evaluate_calls = []
        self.consume_calls = []
        self.receipt_calls = []

    def enroll(self, external_id):
        self.enroll_calls.append(external_id)
        return self._enroll_result

    def evaluate_authorization(self, tool_name, **kwargs):
        self.evaluate_calls.append({"toolName": tool_name, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._evaluation

    def consume_authorization(self, authorization_id, tool_name, **kwargs):
        self.consume_calls.append(
            {"authorizationId": authorization_id, "toolName": tool_name, **kwargs}
        )
        if self._consume_raises is not None:
            raise self._consume_raises
        return self._consume_result

    def record_execution(self, permit_id, outcome, summary=None):
        self.receipt_calls.append(
            {"permitId": permit_id, "outcome": outcome, "summary": summary}
        )
        if self._receipt_raises is not None:
            raise self._receipt_raises
        return {"permitId": permit_id, "executionState": outcome}


class WithFakeClient:
    """Patch the kernel's client constructor to a FakeClient for one test."""

    def __init__(self, client):
        self.client = client
        self._orig = None
        self._orig_key = None

    def __enter__(self):
        self._orig = adapters.PusharyServer
        adapters.PusharyServer = lambda **kwargs: self.client
        self._orig_key = os.environ.get("PUSHARY_API_KEY")
        os.environ["PUSHARY_API_KEY"] = "pk_test.sk_test"
        return self.client

    def __exit__(self, *exc):
        adapters.PusharyServer = self._orig
        if self._orig_key is None:
            os.environ.pop("PUSHARY_API_KEY", None)
        else:
            os.environ["PUSHARY_API_KEY"] = self._orig_key


kernel = AdapterKernel("the Acme helpers")


class ClientTests(unittest.TestCase):
    def test_names_the_calling_adapter_when_no_key_is_configured(self):
        saved = os.environ.pop("PUSHARY_API_KEY", None)
        try:
            with self.assertRaises(ValueError) as ctx:
                kernel.client()
            self.assertIn("the Acme helpers", str(ctx.exception))
        finally:
            if saved is not None:
                os.environ["PUSHARY_API_KEY"] = saved

    def test_names_the_calling_adapter_when_there_is_no_end_user(self):
        with self.assertRaises(ValueError) as ctx:
            kernel.require_external_id(None)
        self.assertIn("no end-user to ask", str(ctx.exception))
        self.assertIn("the Acme helpers", str(ctx.exception))
        self.assertEqual(kernel.require_external_id("user_1"), "user_1")
        self.assertEqual(kernel.require_external_id(" user_1 "), " user_1 ")
        with self.assertRaisesRegex(ValueError, "256"):
            kernel.require_external_id("x" * 257)
        with self.assertRaisesRegex(ValueError, "256"):
            kernel.require_external_id("🙂" * 129)


class AskHumanTests(unittest.TestCase):
    def test_sends_a_re_run_safe_idempotency_key(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        with WithFakeClient(FakeClient(decisions=decisions)):
            kernel.ask_human("Refund?", external_id="user_1", node="approval", idempotency_key="operation-1")
        self.assertEqual(
            decisions.ask_calls[0]["idempotency_key"],
            "operation-1",
        )

    def test_keys_two_nodes_apart(self):
        self.assertNotEqual(
            idempotency_key("u", "one", "Refund?"), idempotency_key("u", "two", "Refund?")
        )


class DurableDecisionTests(unittest.TestCase):
    def test_opens_without_waiting_and_echoes_the_id_as_correlation_id(self):
        decisions = FakeDecisions(create_result={"decisionId": "d9", "status": "pending"})
        with WithFakeClient(FakeClient(decisions=decisions)):
            created = kernel.create_durable_decision(
                "Ship it?", external_id="user_1", idempotency_key="operation-1", callback_url="https://agent.example.com/hook"
            )
        self.assertEqual(decisions.create_calls[0]["wait"], False)
        self.assertEqual(decisions.create_calls[0]["callback_url"], "https://agent.example.com/hook")
        self.assertEqual(created["decisionId"], "d9")
        self.assertEqual(created["correlationId"], "d9")


class ActionLabelTests(unittest.TestCase):
    def test_named_node_becomes_the_label_and_the_default_does_not(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        with WithFakeClient(FakeClient(decisions=decisions)):
            kernel.ask_human("Refund?", external_id="user_1", node="refund.create")
            kernel.ask_human("Refund?", external_id="user_1")
        self.assertEqual(decisions.ask_calls[0]["tool_name"], "refund.create")
        self.assertIsNone(decisions.ask_calls[1]["tool_name"])

    def test_durable_create_carries_the_same_label(self):
        decisions = FakeDecisions(create_result={"decisionId": "d9", "status": "pending"})
        with WithFakeClient(FakeClient(decisions=decisions)):
            kernel.create_durable_decision("Ship?", external_id="user_1", node="deploy.run", idempotency_key="operation-1")
        self.assertEqual(decisions.create_calls[0]["tool_name"], "deploy.run")


class GateTests(unittest.TestCase):
    def _ask(self):
        return ApprovalAsk(
            tool_name="issue_refund",
            call_id="call_1",
            session_id="sess_1",
            question="Approve?",
            external_id="user_1",
        )

    def test_approves_on_an_affirmative_answer(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        with WithFakeClient(FakeClient(decisions=decisions)):
            decision = kernel.create_gate()(self._ask())
        self.assertTrue(decision.approved)
        self.assertIsNone(decision.reason)
        self.assertEqual(decisions.ask_calls[0]["type"], "confirm")

    def test_fails_closed_when_nobody_answers(self):
        decisions = FakeDecisions(ask_result={"answered": False, "approved": False, "value": None})
        with WithFakeClient(FakeClient(decisions=decisions)):
            decision = kernel.create_gate()(self._ask())
        self.assertFalse(decision.approved)
        self.assertIn("No answer", decision.reason)

    def test_distinguishes_a_refusal_from_silence(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": False, "value": "no"})
        with WithFakeClient(FakeClient(decisions=decisions)):
            decision = kernel.create_gate()(self._ask())
        self.assertFalse(decision.approved)
        self.assertIn("denied this action", decision.reason)

    def test_labels_the_decision_with_the_gated_tool(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        with WithFakeClient(FakeClient(decisions=decisions)):
            kernel.create_gate()(self._ask())
        self.assertEqual(decisions.ask_calls[0]["tool_name"], "issue_refund")

    def test_keys_on_session_call_and_tool(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        with WithFakeClient(FakeClient(decisions=decisions)):
            gate = kernel.create_gate()
            gate(self._ask())
            gate(self._ask())
            gate(
                ApprovalAsk(
                    tool_name="issue_refund",
                    call_id="call_2",
                    session_id="sess_1",
                    question="Approve?",
                    external_id="user_1",
                )
            )
        keys = [call["idempotency_key"] for call in decisions.ask_calls]
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[0], keys[2])

    def test_allows_without_opening_a_decision_or_paging_anyone(self):
        decisions = FakeDecisions()
        client = FakeClient(
            decisions=decisions,
            evaluation=evaluated(verdict="allow", policy="issue_refund", authorizationId="authz_1"),
        )
        with WithFakeClient(client):
            decision = kernel.create_gate()(self._ask())
        self.assertTrue(decision.approved)
        self.assertEqual(decisions.ask_calls, [])

    def test_denies_with_the_rule_and_never_asks(self):
        decisions = FakeDecisions()
        client = FakeClient(
            decisions=decisions,
            evaluation=evaluated(
                verdict="deny",
                policy="issue_refund",
                reason="Denied by policy rule issue_refund.",
            ),
        )
        with WithFakeClient(client):
            decision = kernel.create_gate()(self._ask())
        self.assertFalse(decision.approved)
        self.assertEqual(
            decision.reason,
            "Denied by policy rule issue_refund. Do not retry the same action.",
        )
        self.assertEqual(decisions.ask_calls, [])

    def test_asks_a_person_when_the_site_cannot_evaluate_policy(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        client = FakeClient(decisions=decisions, raises=RuntimeError("no plan"))
        with WithFakeClient(client):
            decision = kernel.create_gate()(
                ApprovalAsk(
                    tool_name="issue_refund",
                    call_id="call_1",
                    session_id="sess_1",
                    question="Approve?",
                    external_id="user_1",
                    tool_target="order_9",
                    input={"amount": 4800},
                )
            )
        self.assertTrue(decision.approved)
        # The pre-policy ask shape: a failed evaluation proves nothing about what
        # this call would accept.
        self.assertEqual(decisions.ask_calls[0]["tool_name"], "issue_refund")
        self.assertNotIn("tool_target", decisions.ask_calls[0])
        self.assertNotIn("parameters", decisions.ask_calls[0])

    def test_never_asks_policy_when_the_gate_opts_out(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        client = FakeClient(decisions=decisions)
        with WithFakeClient(client):
            kernel.create_gate(policy=False)(self._ask())
        self.assertEqual(client.evaluate_calls, [])
        self.assertEqual(len(decisions.ask_calls), 1)

    def test_gives_policy_the_action_arguments_and_carries_them_into_the_ask(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        client = FakeClient(decisions=decisions)
        with WithFakeClient(client):
            kernel.create_gate()(
                ApprovalAsk(
                    tool_name="issue_refund",
                    call_id="call_1",
                    session_id="sess_1",
                    question="Approve?",
                    external_id="user_1",
                    tool_target="order_9",
                    input={"amount": 4800, "currency": "EUR"},
                )
            )
        self.assertEqual(
            client.evaluate_calls[0]["parameters"], {"amount": 4800, "currency": "EUR"}
        )
        self.assertEqual(client.evaluate_calls[0]["tool_target"], "order_9")
        self.assertEqual(
            decisions.ask_calls[0]["parameters"], {"amount": 4800, "currency": "EUR"}
        )

    def test_binds_the_approval_to_the_subject_the_ask_recorded(self):
        # The evaluation failed here, so the ask sent only the action and the
        # recipient. Claiming the rest would describe an action the record does
        # not hold, and the consume would refuse a real approval.
        decisions = FakeDecisions(
            ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"}
        )
        client = FakeClient(decisions=decisions, raises=RuntimeError("policy is down"))
        with WithFakeClient(client):
            decision = kernel.create_gate()(
                ApprovalAsk(
                    tool_name="issue_refund",
                    call_id="call_1",
                    session_id="sess_1",
                    question="Approve?",
                    external_id="user_1",
                    tool_target="order_9",
                )
            )
        self.assertTrue(decision.approved)
        self.assertEqual(
            decision.authorization,
            {
                "authorization_id": "d1",
                "tool_name": "issue_refund",
                "external_id": "user_1",
            },
        )

    def test_prefers_stated_facts_over_derived_ones(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        client = FakeClient(decisions=decisions)
        with WithFakeClient(client):
            kernel.create_gate()(
                ApprovalAsk(
                    tool_name="issue_refund",
                    call_id="call_1",
                    session_id="sess_1",
                    question="Approve?",
                    external_id="user_1",
                    input={"amount": 1},
                    parameters={"amount": 4800},
                )
            )
        self.assertEqual(client.evaluate_calls[0]["parameters"], {"amount": 4800})

    def test_refuses_at_construction_when_no_key_is_configured(self):
        saved = os.environ.pop("PUSHARY_API_KEY", None)
        try:
            with self.assertRaises(ValueError):
                kernel.create_gate()
        finally:
            if saved is not None:
                os.environ["PUSHARY_API_KEY"] = saved


class ProtectTests(unittest.TestCase):
    ACTION = dict(
        action="refund.create",
        target="order_4471",
        external_id="user_1",
        call_id="call_1",
        run_id="run_1",
    )

    def test_runs_the_action_when_a_rule_allows_it(self):
        decisions = FakeDecisions()
        client = FakeClient(
            decisions=decisions,
            evaluation=evaluated(verdict="allow", policy="refund.create", authorizationId="authz_1"),
        )
        ran = []
        with WithFakeClient(client):
            outcome = kernel.create_protect()(run=lambda: ran.append(1) or "refunded", **self.ACTION)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result, "refunded")
        self.assertEqual(len(ran), 1)
        self.assertEqual(decisions.ask_calls, [])

    def test_does_not_run_the_action_on_a_policy_denial(self):
        client = FakeClient(
            evaluation=evaluated(
                verdict="deny",
                policy="refund.create",
                reason="Denied by policy rule refund.create.",
            ),
        )
        ran = []
        with WithFakeClient(client):
            outcome = kernel.create_protect()(run=lambda: ran.append(1), **self.ACTION)
        self.assertFalse(outcome.ok)
        self.assertIn("Denied by policy rule refund.create.", outcome.reason)
        self.assertEqual(ran, [])

    def test_runs_the_action_when_a_person_approves(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        with WithFakeClient(FakeClient(decisions=decisions)):
            outcome = kernel.create_protect()(run=lambda: "refunded", **self.ACTION)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result, "refunded")

    def test_does_not_run_the_action_when_nobody_answers(self):
        decisions = FakeDecisions(ask_result={"answered": False, "approved": False, "value": None})
        ran = []
        with WithFakeClient(FakeClient(decisions=decisions)):
            outcome = kernel.create_protect()(run=lambda: ran.append(1), **self.ACTION)
        self.assertFalse(outcome.ok)
        self.assertEqual(ran, [])

    def test_gives_policy_the_action_its_target_and_its_facts(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        client = FakeClient(decisions=decisions)
        with WithFakeClient(client):
            kernel.create_protect()(
                run=lambda: None,
                actor="user:u_44",
                environment="production",
                facts={"amount": 4800, "currency": "EUR"},
                **self.ACTION,
            )
        call = client.evaluate_calls[0]
        self.assertEqual(call["toolName"], "refund.create")
        self.assertEqual(call["tool_target"], "order_4471")
        self.assertEqual(call["actor"], "user:u_44")
        self.assertEqual(call["environment"], "production")
        self.assertEqual(call["parameters"], {"amount": 4800, "currency": "EUR"})

    def test_asks_a_readable_question_without_one_being_written(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        with WithFakeClient(FakeClient(decisions=decisions)):
            kernel.create_protect()(run=lambda: None, **self.ACTION)
        self.assertEqual(decisions.ask_calls[0]["question"], "Approve refund.create order_4471?")

    def test_lets_the_actions_own_failure_through(self):
        # An agent reading ok=False cannot tell a denied refund from a refund that
        # was authorized and then failed, and those want opposite next moves.
        client = FakeClient(evaluation=evaluated(verdict="allow", policy="refund.create", authorizationId="authz_1"))

        def boom():
            raise RuntimeError("stripe is down")

        with WithFakeClient(client):
            with self.assertRaises(RuntimeError):
                kernel.create_protect()(run=boom, **self.ACTION)

    def test_spends_the_authorization_before_the_action_runs(self):
        client = FakeClient(
            evaluation=evaluated(
                verdict="allow", policy="refund.create", authorizationId="authz_1"
            )
        )
        order = []
        with WithFakeClient(client):
            kernel.create_protect()(run=lambda: order.append("ran"), **self.ACTION)
        # A permit spent after the run would leave the window this exists to
        # close: the crash between the two loses the record that it ran.
        self.assertEqual(len(client.consume_calls), 1)
        self.assertEqual(order, ["ran"])
        self.assertEqual(len(client.receipt_calls), 1)

    def test_restates_the_action_it_is_about_to_run(self):
        client = FakeClient(
            evaluation=evaluated(
                verdict="allow", policy="refund.create", authorizationId="authz_1"
            )
        )
        with WithFakeClient(client):
            kernel.create_protect()(
                run=lambda: None,
                actor="user:u_44",
                environment="production",
                facts={"amount": 4800},
                **self.ACTION,
            )
        self.assertEqual(
            client.consume_calls[0],
            {
                "authorizationId": "authz_1",
                "toolName": "refund.create",
                "tool_target": "order_4471",
                "actor": "user:u_44",
                "environment": "production",
                "parameters": {"amount": 4800},
                "external_id": "user_1",
            },
        )

    def test_does_not_run_the_action_when_the_authorization_was_already_spent(self):
        client = FakeClient(
            evaluation=evaluated(
                verdict="allow", policy="refund.create", authorizationId="authz_1"
            ),
            consume_raises=PusharyError(
                "The authorization was already consumed",
                status=409,
                body={"refusal": "already_consumed", "executionState": "succeeded"},
            ),
        )
        ran = []
        with WithFakeClient(client):
            outcome = kernel.create_protect()(run=lambda: ran.append(1), **self.ACTION)
        self.assertEqual(ran, [])
        self.assertFalse(outcome.ok)
        # Worded to stop the model, not to invite a retry: something already ran it.
        self.assertIn("must not run again", outcome.reason)
        self.assertEqual(client.receipt_calls, [])

    def test_does_not_run_the_action_when_the_subject_no_longer_matches(self):
        client = FakeClient(
            evaluation=evaluated(
                verdict="allow", policy="refund.create", authorizationId="authz_1"
            ),
            consume_raises=PusharyError(
                "The action does not match the action that was authorized",
                status=409,
                body={"refusal": "action_mismatch"},
            ),
        )
        ran = []
        with WithFakeClient(client):
            outcome = kernel.create_protect()(run=lambda: ran.append(1), **self.ACTION)
        self.assertEqual(ran, [])
        self.assertIn("does not match", outcome.reason)

    def test_does_not_run_the_action_when_the_permit_could_not_be_confirmed(self):
        # A transport failure is not a refusal, and it is not permission either.
        client = FakeClient(
            evaluation=evaluated(
                verdict="allow", policy="refund.create", authorizationId="authz_1"
            ),
            consume_raises=PusharyError("Request to Pushary failed: timed out"),
        )
        ran = []
        with WithFakeClient(client):
            outcome = kernel.create_protect()(run=lambda: ran.append(1), **self.ACTION)
        self.assertEqual(ran, [])
        self.assertIn("could not be confirmed", outcome.reason)

    def test_does_not_run_an_approval_the_server_would_not_bind(self):
        client = FakeClient(
            evaluation=evaluated(verdict="allow", policy="refund.create")
        )
        ran = []
        with WithFakeClient(client):
            outcome = kernel.create_protect()(run=lambda: ran.append(1), **self.ACTION)
        self.assertEqual(ran, [])
        self.assertIn("could not be bound", outcome.reason)
        self.assertEqual(client.consume_calls, [])

    def test_records_what_the_action_did(self):
        client = FakeClient(
            evaluation=evaluated(
                verdict="allow", policy="refund.create", authorizationId="authz_1"
            )
        )
        with WithFakeClient(client):
            kernel.create_protect()(run=lambda: "refunded", **self.ACTION)
        self.assertEqual(
            client.receipt_calls[0],
            {"permitId": "permit_1", "outcome": "succeeded", "summary": None},
        )

    def test_records_a_failure_and_still_lets_the_error_through(self):
        client = FakeClient(
            evaluation=evaluated(
                verdict="allow", policy="refund.create", authorizationId="authz_1"
            )
        )

        def boom():
            raise RuntimeError("stripe is down")

        with WithFakeClient(client):
            with self.assertRaises(RuntimeError):
                kernel.create_protect()(run=boom, **self.ACTION)
        self.assertEqual(client.receipt_calls[0]["outcome"], "failed")
        self.assertEqual(client.receipt_calls[0]["summary"], "stripe is down")

    def test_does_not_turn_a_completed_action_into_an_error(self):
        # The action already happened. Failing the caller here would invite them
        # to issue it again, which is the outcome this whole path prevents.
        client = FakeClient(
            evaluation=evaluated(
                verdict="allow", policy="refund.create", authorizationId="authz_1"
            ),
            receipt_raises=PusharyError("HTTP 500", status=500),
        )
        with WithFakeClient(client):
            outcome = kernel.create_protect()(run=lambda: "refunded", **self.ACTION)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result, "refunded")

    def test_refuses_at_construction_when_no_key_is_configured(self):
        # The point of the factory shape: the missing key raises where the
        # protector is defined, not on the first refund somebody tries to issue.
        saved = os.environ.pop("PUSHARY_API_KEY", None)
        try:
            with self.assertRaises(ValueError):
                kernel.create_protect()
        finally:
            if saved is not None:
                os.environ["PUSHARY_API_KEY"] = saved

    def test_reuses_one_client_across_actions(self):
        decisions = FakeDecisions(ask_result={"answered": True, "approved": True, "value": "yes", "decisionId": "d1"})
        client = FakeClient(decisions=decisions)
        with WithFakeClient(client):
            protect = kernel.create_protect()
            protect(run=lambda: None, **self.ACTION)
            protect(run=lambda: None, **{**self.ACTION, "call_id": "call_2"})
        self.assertEqual(len(client.evaluate_calls), 2)

    def test_default_question_omits_an_absent_target(self):
        self.assertEqual(default_protect_question("refund.create"), "Approve refund.create?")
        self.assertEqual(
            default_protect_question("refund.create", "order_1"),
            "Approve refund.create order_1?",
        )


class DeriveParametersTests(unittest.TestCase):
    def test_carries_scalars_through_unchanged(self):
        self.assertEqual(
            derive_parameters({"amount": 4800, "currency": "EUR", "urgent": True}),
            {"amount": 4800, "currency": "EUR", "urgent": True},
        )

    def test_is_all_or_nothing(self):
        # A rule reading `amount >= 500` would stop denying if `amount` alone survived.
        self.assertIsNone(derive_parameters({"amount": 4800, "meta": {"nested": True}}))
        self.assertIsNone(derive_parameters({"amount": 4800, "tags": ["a"]}))
        self.assertIsNone(derive_parameters({"amount": float("nan")}))
        self.assertIsNone(derive_parameters({"amount": float("inf")}))
        self.assertIsNone(derive_parameters({"amount": 4800, "note": "x" * 201}))
        self.assertIsNone(derive_parameters({"k" * 65: 1}))
        self.assertIsNone(derive_parameters({f"k{i}": i for i in range(33)}))

    def test_has_nothing_to_derive_from_a_non_mapping(self):
        self.assertIsNone(derive_parameters(None))
        self.assertIsNone(derive_parameters("refund"))
        self.assertIsNone(derive_parameters([1, 2]))
        self.assertIsNone(derive_parameters({}))


class RenderApprovalQuestionTests(unittest.TestCase):
    def test_shows_the_tool_and_its_input(self):
        question = render_approval_question("issue_refund", {"amount": 480})
        self.assertIn("issue_refund", question)
        self.assertIn("480", question)

    def test_truncates_a_large_input(self):
        question = render_approval_question("issue_refund", {"blob": "x" * 1000})
        self.assertLess(len(question), 400)
        self.assertIn("...", question)

    def test_handles_an_input_the_tool_did_not_supply(self):
        self.assertEqual(render_approval_question("delete_all", None), "Approve delete_all?")

    def test_still_asks_when_the_input_has_a_cycle(self):
        circular = {"name": "loop"}
        circular["self"] = circular
        question = render_approval_question("issue_refund", circular)
        self.assertIn("issue_refund", question)

    def test_falls_back_to_str_for_an_unserialisable_input(self):
        class Opaque:
            def __repr__(self):
                return "<opaque>"

        self.assertIn("opaque", render_approval_question("t", Opaque()))


class DescribeAnswerTests(unittest.TestCase):
    def test_tells_the_model_not_to_proceed_without_an_answer(self):
        text = describe_answer("confirm", {"answered": False, "status": "expired"})
        self.assertIn("NOT approved", text)

    def test_reports_the_value_for_select(self):
        self.assertIn("B", describe_answer("select", {"answered": True, "value": "B"}))


class IsAffirmativeTests(unittest.TestCase):
    def test_is_fail_closed(self):
        self.assertTrue(is_affirmative("yes"))
        self.assertTrue(is_affirmative("Approve"))
        self.assertFalse(is_affirmative("no"))
        self.assertFalse(is_affirmative(None))


class ResolveCallbackTests(unittest.TestCase):
    BODY = json.dumps(
        {
            "event": "decision.answered",
            "correlationId": "d1",
            "answer": "yes",
            "value": "yes",
            "answeredAt": "2026-08-17T00:00:00.000Z",
        }
    )

    def test_parses_a_correctly_signed_callback(self):
        parsed = resolve_pushary_callback(self.BODY, sign(self.BODY), SECRET)
        self.assertEqual(parsed["correlationId"], "d1")
        self.assertTrue(parsed["approved"])

    def test_rejects_a_forged_signature(self):
        self.assertIsNone(resolve_pushary_callback(self.BODY, "deadbeef", SECRET))


if __name__ == "__main__":
    unittest.main()

class OperationIdentityTests(unittest.TestCase):
    def test_independent_asks_do_not_infer_identity_from_question(self):
        self.assertNotEqual(idempotency_key('u', 'refund', 'Approve?'),
                            idempotency_key('u', 'refund', 'Approve?'))

    def test_explicit_key_and_durable_requirement(self):
        kernel = AdapterKernel('audit')
        fake = FakeClient()
        with WithFakeClient(fake):
            kernel.ask_human('Approve?', external_id='u', idempotency_key='order-1')
        self.assertEqual(fake.decisions.ask_calls[0]['idempotency_key'], 'order-1')
        with WithFakeClient(fake), self.assertRaisesRegex(ValueError, 'idempotency_key'):
            kernel.create_durable_decision('Approve?', external_id='u')
        self.assertEqual(fake.decisions.create_calls, [])


class ExactGateIdentityTests(unittest.TestCase):
    def ask(self, **overrides):
        fields = {
            "tool_name": "refund", "call_id": "call_1", "session_id": "session_1",
            "question": "Approve refund?", "external_id": "customer_1",
            "input": {"order": {"id": "order_1", "items": [1, 2]}},
            "parameters": {"amount": 10}, "tool_target": "order_1",
            "actor": "support", "environment": "production",
            "presentation": {"label": "Refund", "effect": "Return funds"},
        }
        fields.update(overrides)
        return ApprovalAsk(**fields)

    def test_exact_retries_ignore_object_order_and_changed_subjects_get_new_keys(self):
        decisions = FakeDecisions()
        client = FakeClient(decisions=decisions)
        with WithFakeClient(client):
            gate = kernel.create_gate(policy=False)
            gate(self.ask())
            gate(self.ask(input={"order": {"items": [1, 2], "id": "order_1"}}))
            for changed in (
                {"external_id": "customer_2"}, {"call_id": "call_2"},
                {"session_id": "session_2"}, {"tool_name": "payment"},
                {"tool_target": "order_2"}, {"actor": "customer"},
                {"environment": "test"}, {"question": "Approve revised refund?"},
                {"input": {"order": {"id": "order_1", "items": [2, 1]}}},
                {"parameters": {"amount": 20}},
                {"presentation": {"label": "Refund", "effect": "Revised terms"}},
            ):
                gate(self.ask(**changed))
        keys = [call["idempotency_key"] for call in decisions.ask_calls]
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(len(set(keys)), len(keys) - 1)

    def test_full_input_is_bound_even_when_policy_parameters_cannot_be_derived(self):
        client = FakeClient()
        with WithFakeClient(client):
            gate = kernel.create_gate(policy=False)
            gate(self.ask(parameters=None, input={"items": [{"quantity": 1}]}))
            gate(self.ask(parameters=None, input={"items": [{"quantity": 2}]}))
        calls = client.decisions.ask_calls
        self.assertIsNone(calls[0]["parameters"])
        self.assertIsNone(calls[1]["parameters"])
        self.assertNotEqual(calls[0]["idempotency_key"], calls[1]["idempotency_key"])

    def test_non_json_input_fails_before_policy_can_allow(self):
        circular = []
        circular.append(circular)
        for value in ({1: "one"}, ("tuple",), {"set"}, object(), float("nan"), float("inf"), circular):
            client = FakeClient(evaluation=evaluated(verdict="allow"))
            with self.subTest(value=type(value).__name__), WithFakeClient(client):
                with self.assertRaises(ValueError):
                    kernel.create_gate()(self.ask(input=value))
            self.assertEqual(client.evaluate_calls, [])
            self.assertEqual(client.decisions.ask_calls, [])

    def test_call_id_is_required_but_empty_session_is_valid(self):
        client = FakeClient()
        with WithFakeClient(client):
            gate = kernel.create_gate()
            for call_id in (None, "", " "):
                with self.assertRaisesRegex(ValueError, "tool call ID"):
                    gate(self.ask(call_id=call_id))
            self.assertEqual(client.evaluate_calls, [])
            gate(self.ask(session_id=""))
            gate(self.ask(session_id=""))
        calls = client.decisions.ask_calls
        self.assertEqual(calls[0]["idempotency_key"], calls[1]["idempotency_key"])

    def test_explicit_human_gate_preserves_subject_and_bound_authorization(self):
        client = FakeClient(decisions=FakeDecisions(ask_result={"approved": True, "decisionId": "d1"}))
        with WithFakeClient(client):
            decision = kernel.create_gate(policy=False)(self.ask())
        self.assertEqual(client.evaluate_calls, [])
        sent = client.decisions.ask_calls[0]
        for key in ("tool_target", "actor", "environment", "parameters", "presentation"):
            self.assertEqual(sent[key], getattr(self.ask(), key))
        self.assertEqual(decision.authorization["tool_target"], "order_1")
        self.assertEqual(decision.authorization["parameters"], {"amount": 10})

    def test_fingerprint_preserves_json_types_and_escaped_boundaries(self):
        self.assertEqual(decision_fingerprint({"a": 1, "b": 2}), decision_fingerprint({"b": 2, "a": 1}))
        values = [{"v": True}, {"v": 1}, {"v": "1"}, ["a\x00b", "c"], ["a", "b\x00c"]]
        self.assertEqual(len({decision_fingerprint(value) for value in values}), len(values))


class ReviewTransportTests(unittest.TestCase):
    def test_shared_helpers_forward_review_and_delivery_fields(self):
        fields = {
            "tool_target": "order_1", "actor": "support", "environment": "production",
            "parameters": {"amount": 10}, "presentation": {"label": "Refund", "effect": "Return funds"},
            "placeholder": "Explain your choice", "expires_in_seconds": 3600,
            "require_reachable": True, "callback_url": "https://app.example.com/hook",
        }
        client = FakeClient()
        with WithFakeClient(client):
            kernel.ask_human("Approve?", external_id="customer_1", **fields)
            kernel.create_durable_decision("Approve?", external_id="customer_1", idempotency_key="operation_1", **fields)
        for sent in (client.decisions.ask_calls[0], client.decisions.create_calls[0]):
            for key, value in fields.items():
                self.assertEqual(sent[key], value)
