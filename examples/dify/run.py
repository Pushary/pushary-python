"""A Dify workflow and real phone decision; the order effect is local SQLite only."""
from contextlib import closing
from functools import partial
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from pushary.adapters import derive_parameters

from review import start, resume


def order_evidence(parameters: dict[str, object], binding: str) -> tuple[str, str]:
    if derive_parameters(parameters) is None:
        raise ValueError("Order parameters must be bounded flat JSON")
    order_id = parameters.get("order_id")
    if not isinstance(order_id, str) or not order_id or not isinstance(binding, str) or not binding:
        raise ValueError("An order ID and operation binding are required")
    return order_id, json.dumps(parameters, sort_keys=True, allow_nan=False)


def read_order(db: sqlite3.Connection, parameters: dict[str, object],
               binding: str) -> dict[str, str] | None:
    order_id, evidence = order_evidence(parameters, binding)
    order = db.execute("select order_id from released where id=?", (binding,)).fetchone()
    receipt = db.execute("select parameters from release_receipts where id=?", (binding,)).fetchone()
    if order is None and receipt is None:
        return None
    if order != (order_id,) or receipt != (evidence,):
        raise ValueError("Order and receipt do not match the approved operation; reconcile manually")
    return {"order_id": order_id, "status": "released"}


def lookup_order(folder: Path, parameters: dict[str, object], binding: str) -> dict[str, str] | None:
    database = folder / "orders.db"
    if not database.exists():
        return None
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("begin")
        return read_order(db, parameters, binding)


def release_order(folder: Path, parameters: dict[str, object], binding: str) -> dict[str, str]:
    order_id, evidence = order_evidence(parameters, binding)
    with closing(sqlite3.connect(folder / "orders.db")) as db, db:
        db.execute("begin immediate")
        db.execute("create table if not exists released (id primary key, order_id)")
        db.execute("create table if not exists release_receipts (id primary key, parameters text not null)")
        existing = read_order(db, parameters, binding)
        if existing is not None:
            return existing
        db.execute("insert into released values (?,?)", (binding, order_id))
        db.execute("insert into release_receipts values (?,?)", (binding, evidence))
    return {"order_id": order_id, "status": "released"}


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("start", "resume"):
        raise SystemExit("Usage: python run.py start|resume STATE_DIRECTORY TEST_CUSTOMER_ID")
    mode, folder, customer = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    config = dict(base_url=os.environ["DIFY_API_URL"], workflow_id=os.environ["DIFY_WORKFLOW_ID"],
                  tenant_id="demo-store", external_id=customer)
    try:
        if mode == "start":
            start(folder, **config, node_id=os.environ["DIFY_NODE_ID"], action="order.release",
                  target="order-1", parameters={"order_id": "order-1", "amount_cents": 1900},
                  expires_at=int(time.time()) + 3600)
        print(json.dumps(resume(folder, partial(release_order, folder),
                                lookup=partial(lookup_order, folder), **config)))
    except Exception:
        # Do not expose native form tokens, API keys or HTTP response bodies.
        raise SystemExit("Stopped safely. Inspect the trusted run and permit records; do not automatically restart.")
