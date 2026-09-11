"""Tests for webhook signature verification. No network."""

import hashlib
import hmac
import unittest

from pushary import (
    SIGNATURE_HEADER,
    parse_decision_callback,
    verify_webhook_signature,
)


SECRET = "whsec_test_secret"


def sign(raw_body, secret=SECRET):
    body = raw_body if isinstance(raw_body, bytes) else raw_body.encode("utf-8")
    key = secret if isinstance(secret, bytes) else secret.encode("utf-8")
    return hmac.new(key, body, hashlib.sha256).hexdigest()


class VerifyWebhookSignatureTests(unittest.TestCase):
    def test_valid_signature_str_body(self):
        body = '{"decisionId":"d_1","value":"yes"}'
        self.assertTrue(verify_webhook_signature(body, sign(body), SECRET))

    def test_valid_signature_bytes_body(self):
        body = b'{"decisionId":"d_1","value":"yes"}'
        self.assertTrue(verify_webhook_signature(body, sign(body), SECRET))

    def test_str_and_bytes_body_agree(self):
        text = '{"a":1}'
        self.assertEqual(sign(text), sign(text.encode("utf-8")))

    def test_tampered_body_fails(self):
        body = '{"value":"yes"}'
        signature = sign(body)
        tampered = '{"value":"no"}'
        self.assertFalse(verify_webhook_signature(tampered, signature, SECRET))

    def test_wrong_secret_fails(self):
        body = '{"value":"yes"}'
        signature = sign(body, "another_secret")
        self.assertFalse(verify_webhook_signature(body, signature, SECRET))

    def test_missing_signature_returns_false(self):
        body = '{"value":"yes"}'
        self.assertFalse(verify_webhook_signature(body, None, SECRET))
        self.assertFalse(verify_webhook_signature(body, "", SECRET))

    def test_missing_secret_returns_false(self):
        body = '{"value":"yes"}'
        self.assertFalse(verify_webhook_signature(body, sign(body), ""))

    def test_length_mismatch_returns_false(self):
        body = '{"value":"yes"}'
        self.assertFalse(verify_webhook_signature(body, "abc123", SECRET))

    def test_non_ascii_signature_does_not_raise(self):
        body = '{"value":"yes"}'
        # A malicious header with multibyte characters must be rejected, not
        # raise, so it can never crash the receiver.
        self.assertFalse(verify_webhook_signature(body, "é" * 64, SECRET))

    def test_signature_header_constant(self):
        self.assertEqual(SIGNATURE_HEADER, "X-Pushary-Signature")


class ParseDecisionCallbackTests(unittest.TestCase):
    def test_parses_full_payload(self):
        body = (
            '{"correlationId":"c_1","answer":"yes","value":"yes",'
            '"answeredAt":"2026-07-17T00:00:00Z","context":"run_42"}'
        )
        parsed = parse_decision_callback(body)
        self.assertEqual(parsed["correlationId"], "c_1")
        self.assertEqual(parsed["answer"], "yes")
        self.assertEqual(parsed["value"], "yes")
        self.assertEqual(parsed["context"], "run_42")

    def test_value_defaults_to_answer_when_absent(self):
        parsed = parse_decision_callback('{"correlationId":"c_1","answer":"no"}')
        self.assertEqual(parsed["value"], "no")
        self.assertNotIn("context", parsed)

    def test_rejects_missing_fields(self):
        self.assertIsNone(parse_decision_callback('{"answer":"yes"}'))
        self.assertIsNone(parse_decision_callback('{"correlationId":"c_1"}'))

    def test_rejects_non_json(self):
        self.assertIsNone(parse_decision_callback("not json"))
        self.assertIsNone(parse_decision_callback("[1,2,3]"))


if __name__ == "__main__":
    unittest.main()
