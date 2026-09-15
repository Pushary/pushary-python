"""A Dify workflow and real phone decision; the order effect is local SQLite only."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from review import start, resume


def release_order(folder, parameters, binding):
    with sqlite3.connect(folder / "orders.db") as db:
        db.execute("create table if not exists released (id primary key, order_id)")
        db.execute("insert or ignore into released values (?,?)", (binding, parameters["order_id"]))
    return {"order_id": parameters["order_id"], "status": "released"}


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
        print(json.dumps(resume(folder, lambda p, b: release_order(folder, p, b), **config)))
    except Exception:
        # Do not expose native form tokens, API keys or HTTP response bodies.
        raise SystemExit("Stopped safely. Inspect the trusted run and permit records; do not automatically restart.")
