"""Reusable, single-action Strands customer review using the released Pushary SDK."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from importlib.metadata import version

from pushary import PusharyServer
from strands import Agent, Snapshot
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class CustomerReview(HookProvider):
    """One trusted business operation, one protected call, one native snapshot.

    All workers for this operation must use the same database and identity.
    Application code owns authorization, scheduling and side-effect idempotency.
    """

    def __init__(self, database, client: PusharyServer, *, tenant, customer, operation,
                 tool_name, arguments, code_version, expires_in_seconds=3600):
        for value in (tenant, customer, operation, tool_name, code_version):
            if not isinstance(value, str) or not value or len(value) > 200:
                raise ValueError("Nonempty bounded application identifiers required")
        if not isinstance(arguments, dict) or not 1 <= len(arguments) <= 16:
            raise ValueError("Supply the complete flat action")
        for key, value in arguments.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValueError("Invalid action field")
            if type(value) not in (str, int, float, bool) or len(str(value)) > 200:
                raise ValueError("Action fields must be bounded JSON primitives")
        self.question = f"Approve {tool_name}? {encoded(arguments)}"
        if len(self.question) > 500 or type(expires_in_seconds) is not int or not 60 <= expires_in_seconds <= 86400:
            raise ValueError("Action or expiry exceeds bounds")
        self.binding = encoded(dict(tenant=tenant, customer=customer, operation=operation,
                                    tool=tool_name, arguments=arguments, code_version=code_version,
                                    strands=version("strands-agents"), pushary=version("pushary"),
                                    expires=expires_in_seconds))
        self.key = digest(encoded([tenant, operation]))
        self.client = client
        self.db = sqlite3.connect(database, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS reviews (
            key TEXT PRIMARY KEY, binding TEXT NOT NULL, phase TEXT NOT NULL,
            snapshot TEXT, fingerprint TEXT, interruption TEXT, decision_id TEXT, output TEXT)""")
        self.db.commit()
        self._permit = None

    def register_hooks(self, registry: HookRegistry, **kwargs):
        registry.add_callback(BeforeToolCallEvent, self.before_tool)

    def before_tool(self, event: BeforeToolCallEvent):
        binding = json.loads(self.binding)
        if event.tool_use["name"] != binding["tool"]:
            return
        if encoded(event.tool_use["input"]) != encoded(binding["arguments"]):
            event.cancel_tool = "The action differs from the trusted proposal"
            return
        # Binding the grant to the complete call prevents a second call reusing it.
        call = encoded(event.tool_use)
        response = event.interrupt("pushary.customer-review", reason=json.loads(call))
        permitted = response is True and self._permit == call
        self._permit = None
        if not permitted:
            event.cancel_tool = "No verified customer approval"

    def row(self):
        row = self.db.execute("SELECT * FROM reviews WHERE key=?", (self.key,)).fetchone()
        if row is None or row["binding"] != self.binding:
            raise ValueError("Unknown operation or changed customer/action/code version")
        if row["snapshot"] and row["fingerprint"] != digest(self.binding + row["snapshot"] + row["interruption"]):
            raise ValueError("Saved state changed")
        return row

    def start(self, agent: Agent, prompt):
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO reviews(key,binding,phase) VALUES(?,?,'starting')",
                                       (self.key, self.binding)).rowcount
        if not inserted:
            return self.request()
        try:
            result = agent(prompt)
            if result.stop_reason != "interrupt" or len(result.interrupts) != 1:
                raise ValueError("Exactly one protected interruption is required")
            interruption = result.interrupts[0]
            binding = json.loads(self.binding)
            if (interruption.name != "pushary.customer-review"
                    or interruption.reason.get("name") != binding["tool"]
                    or encoded(interruption.reason.get("input")) != encoded(binding["arguments"])):
                raise ValueError("Unexpected interruption")
            snapshot = encoded(agent.take_snapshot(preset="session", include=["system_prompt"]).to_dict())
            saved_interruption = encoded(interruption.to_dict())
            fingerprint = digest(self.binding + snapshot + saved_interruption)
            with self.db:
                self.db.execute("""UPDATE reviews SET phase='pending',snapshot=?,fingerprint=?,interruption=?
                    WHERE key=?""", (snapshot, fingerprint, saved_interruption, self.key))
        except BaseException:
            with self.db:
                self.db.execute("UPDATE reviews SET phase='uncertain' WHERE key=?", (self.key,))
            raise
        return self.request()

    def request(self):
        row = self.row()
        if row["phase"] != "pending" or row["decision_id"]:
            return {"phase": row["phase"], "decision_id": row["decision_id"]}
        b = json.loads(self.binding)
        decision = self.client.decisions.create(
            question=self.question,
            external_id=b["customer"], type="confirm", wait=False,
            context="strands:" + row["fingerprint"], tool_name=b["tool"],
            expires_in_seconds=b["expires"], require_reachable=True,
            idempotency_key="strands-" + row["fingerprint"])
        decision_id = decision.get("decisionId")
        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("Missing decision ID")
        with self.db:
            self.db.execute("UPDATE reviews SET decision_id=? WHERE key=? AND decision_id IS NULL",
                            (decision_id, self.key))
        if self.row()["decision_id"] != decision_id:
            raise ValueError("Conflicting decision identity")
        return {"phase": "pending", "decision_id": decision_id}

    def resume(self, agent: Agent):
        row = self.row()
        if row["phase"] == "complete":
            return {"phase": "complete", "output": json.loads(row["output"])}
        if row["phase"] != "pending":
            return {"phase": row["phase"]}
        if not row["decision_id"]:
            self.request()
            row = self.row()
        decision = self.client.decisions.get(row["decision_id"])
        b = json.loads(self.binding)
        if (decision.get("decisionId") != row["decision_id"]
                or decision.get("externalId") not in (None, b["customer"])
                or decision.get("context") != "strands:" + row["fingerprint"]
                or decision.get("type") != "confirm"
                or decision.get("question") != self.question):
            raise ValueError("Decision binding mismatch")
        if decision.get("status") == "pending":
            return {"phase": "pending"}
        if decision.get("status") not in ("answered", "expired", "cancelled"):
            raise ValueError("Unknown decision state")
        approved = (decision["status"] == "answered" and decision.get("answered") is True
                    and decision.get("value") == "yes")
        # ponytail: one SQLite file coordinates local workers; use a shared transactional DB across hosts.
        with self.db:
            claimed = self.db.execute("UPDATE reviews SET phase='resuming' WHERE key=? AND phase='pending'",
                                      (self.key,)).rowcount
        if not claimed:
            return {"phase": self.row()["phase"]}
        try:
            agent.load_snapshot(Snapshot.from_dict(json.loads(row["snapshot"])))
            interruption = json.loads(row["interruption"])
            self._permit = encoded(interruption["reason"]) if approved else None
            result = agent([{"interruptResponse": {"interruptId": interruption["id"], "response": approved}}])
            output = {"message": result.message, "stop_reason": result.stop_reason,
                      "interrupts": [i.to_dict() for i in (result.interrupts or [])],
                      "snapshot": agent.take_snapshot(preset="session", include=["system_prompt"]).to_dict()}
            with self.db:
                self.db.execute("UPDATE reviews SET phase='complete',output=? WHERE key=?",
                                (encoded(output), self.key))
            return {"phase": "complete", "output": output}
        except BaseException:
            with self.db:
                self.db.execute("UPDATE reviews SET phase='uncertain' WHERE key=?", (self.key,))
            raise
        finally:
            self._permit = None
