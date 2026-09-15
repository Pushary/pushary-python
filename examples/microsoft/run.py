"""A real phone decision with a local order effect; no model key is required."""
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from review import start, resume


def release_order(folder, parameters, binding):
    with sqlite3.connect(folder / "orders.db") as db:
        db.execute("create table if not exists released (id primary key, order_id)")
        db.execute("insert or ignore into released values (?,?)", (binding, parameters["order_id"]))
    return {"order_id": parameters["order_id"], "status": "released"}


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("start", "resume"):
        raise SystemExit("Usage: python run.py start|resume STATE_DIRECTORY TEST_CUSTOMER_ID")
    mode, folder, external_id = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    if mode == "start":
        run_id = uuid.uuid4().hex
        start(folder, tenant_id="demo-store", external_id=external_id, run_id=run_id,
              call_id="release-1", action="order.release", target="order-1",
              parameters={"order_id": "order-1", "amount_cents": 1900},
              expires_at=int(time.time()) + 3600)
    saved = json.loads((folder / "operation.json").read_text())
    print(resume(folder, lambda parameters, binding: release_order(folder, parameters, binding),
                 tenant_id="demo-store", external_id=external_id, run_id=saved["operation"]["run_id"]))
