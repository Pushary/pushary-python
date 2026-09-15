"""One durable LlamaIndex workflow action, reviewed by its authorized customer."""
from __future__ import annotations

import asyncio
from importlib.metadata import version
import json
import sqlite3
import secrets

from pushary import PusharyServer
from pushary.adapters import decision_fingerprint, derive_parameters
from workflows import Context, Workflow, step
from workflows.events import HumanResponseEvent, InputRequiredEvent, StartEvent, StopEvent


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class CustomerReview(Workflow):
    """Reusable single-action workflow. `execute` must enforce business idempotency.

    Supply identity and action from authenticated application state, never a model.
    `start_review` snapshots at native input; `resume_review` verifies and claims it.
    """

    def __init__(self, database, client: PusharyServer, *, tenant, customer, operation,
                 action, code_version, execute, expires_in_seconds=3600):
        super().__init__(timeout=60)
        for value in (tenant, customer, operation, code_version):
            if not isinstance(value, str) or not value or len(value) > 200:
                raise ValueError("Nonempty bounded application identity required")
        if derive_parameters(action) is None:
            raise ValueError("The complete action must be bounded flat JSON")
        self.question = "Approve action? " + encoded(action)
        if len(self.question) > 500 or type(expires_in_seconds) is not int or not 60 <= expires_in_seconds <= 86400:
            raise ValueError("Question or expiry exceeds API bounds")
        self.binding = encoded(dict(tenant=tenant, customer=customer, operation=operation,
                                    action=action, code_version=code_version, expires=expires_in_seconds,
                                    workflows=version("llama-index-workflows"), pushary=version("pushary")))
        self.key = decision_fingerprint([tenant, operation])
        self.client, self.execute_action = client, execute
        # ponytail: one SQLite file coordinates one host; use a shared transactional DB across hosts.
        self.db = sqlite3.connect(database, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS reviews (
            key TEXT PRIMARY KEY, binding TEXT NOT NULL, phase TEXT NOT NULL,
            snapshot TEXT, fingerprint TEXT, decision_id TEXT, output TEXT)""")
        self.db.commit()
        self._grant = None

    @step
    async def request_input(self, ev: StartEvent) -> InputRequiredEvent:
        return InputRequiredEvent(prefix=self.question, operation=self.key)

    @step
    async def execute_reviewed(self, ev: HumanResponseEvent) -> StopEvent:
        # A caller sending a native event directly cannot mint this one-use grant.
        permitted = ev.response == "yes" and self._grant is not None and ev.get("grant") == self._grant and ev.get("operation") == self.key
        self._grant = None
        if not permitted:
            return StopEvent(result={"approved": False})
        result = await self.execute_action(json.loads(self.binding)["action"])
        return StopEvent(result={"approved": True, "receipt": result})

    def row(self):
        row = self.db.execute("SELECT * FROM reviews WHERE key=?", (self.key,)).fetchone()
        if row is None or row["binding"] != self.binding:
            raise ValueError("Unknown operation or changed customer/action/code")
        if row["snapshot"] and row["fingerprint"] != decision_fingerprint([self.binding, row["snapshot"]]):
            raise ValueError("Saved workflow changed")
        return row

    async def start_review(self):
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO reviews(key,binding,phase) VALUES(?,?,'starting')",
                                       (self.key, self.binding)).rowcount
        if not inserted:
            return await self.request_decision()
        handler = self.run()
        try:
            async for event in handler.stream_events():
                if isinstance(event, InputRequiredEvent):
                    if event.prefix != self.question or event.get("operation") != self.key:
                        raise ValueError("Unexpected input request")
                    snapshot = encoded(handler.ctx.to_dict())
                    fingerprint = decision_fingerprint([self.binding, snapshot])
                    with self.db:
                        self.db.execute("UPDATE reviews SET phase='pending',snapshot=?,fingerprint=? WHERE key=?",
                                        (snapshot, fingerprint, self.key))
                    break
            else:
                raise ValueError("Workflow did not request review")
        except BaseException:
            with self.db:
                self.db.execute("UPDATE reviews SET phase='uncertain' WHERE key=?", (self.key,))
            raise
        finally:
            await handler.cancel_run()
        return await self.request_decision()

    async def request_decision(self):
        row = self.row()
        if row["phase"] != "pending" or row["decision_id"]:
            return {"phase": row["phase"], "decision_id": row["decision_id"]}
        b = json.loads(self.binding)
        decision = await asyncio.to_thread(
            self.client.decisions.create, self.question,
            external_id=b["customer"], type="confirm", wait=False,
            context="llamaindex:" + row["fingerprint"], require_reachable=True,
            expires_in_seconds=b["expires"], idempotency_key="llamaindex-" + row["fingerprint"])
        decision_id = decision.get("decisionId")
        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("Missing decision ID")
        with self.db:
            self.db.execute("UPDATE reviews SET decision_id=? WHERE key=? AND decision_id IS NULL",
                            (decision_id, self.key))
        if self.row()["decision_id"] != decision_id:
            raise ValueError("Conflicting decision ID")
        return {"phase": "pending", "decision_id": decision_id}

    async def resume_review(self):
        row = self.row()
        if row["phase"] == "complete":
            return {"phase": "complete", "output": json.loads(row["output"])}
        if row["phase"] != "pending":
            return {"phase": row["phase"]}
        if not row["decision_id"]:
            await self.request_decision()
            row = self.row()
        decision = await asyncio.to_thread(self.client.decisions.get, row["decision_id"])
        b = json.loads(self.binding)
        if (decision.get("decisionId") != row["decision_id"]
                or decision.get("context") != "llamaindex:" + row["fingerprint"]
                or decision.get("externalId") not in (None, b["customer"])
                or decision.get("question") != self.question or decision.get("type") != "confirm"):
            raise ValueError("Decision binding mismatch")
        if decision.get("status") == "pending":
            return {"phase": "pending"}
        if decision.get("status") not in ("answered", "expired", "cancelled"):
            raise ValueError("Unknown decision state")
        approved = decision["status"] == "answered" and decision.get("answered") is True and decision.get("value") == "yes"
        with self.db:
            claimed = self.db.execute("UPDATE reviews SET phase='resuming' WHERE key=? AND phase='pending'", (self.key,)).rowcount
        if not claimed:
            return {"phase": self.row()["phase"]}
        handler = None
        try:
            ctx = Context.from_dict(self, json.loads(row["snapshot"]))
            self._grant = secrets.token_urlsafe(32) if approved else None
            handler = self.run(ctx=ctx)
            await handler.send_event(HumanResponseEvent(response="yes" if approved else "no", operation=self.key, grant=self._grant))
            output = await handler
            with self.db:
                self.db.execute("UPDATE reviews SET phase='complete',output=? WHERE key=?", (encoded(output), self.key))
            return {"phase": "complete", "output": output}
        except BaseException:
            with self.db:
                self.db.execute("UPDATE reviews SET phase='uncertain' WHERE key=?", (self.key,))
            raise
        finally:
            self._grant = None
            if handler is not None and not handler.done():
                await handler.cancel_run()
