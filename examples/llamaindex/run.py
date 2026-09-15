"""Phone demo: record an approved order release locally; never charge or ship."""
import asyncio
import os
import sqlite3
import sys

from pushary import PusharyServer
from review import CustomerReview, encoded

if len(sys.argv) != 2 or sys.argv[1] not in ("start", "resume"):
    raise SystemExit("Usage: python run.py start|resume")

# In an application, load these from the authenticated tenant and immutable order.
# Never take the customer, operation identity, or approved action from the model.
database = os.environ.get("REVIEW_DATABASE", "reviews.sqlite")
customer = os.environ["PUSHARY_EXTERNAL_ID"]
action = {"order_id": "demo_order_1", "draft_version": 1}
operation = "release_demo_order_1_v1"


async def release_order(proposed):
    """Record a local demonstration order release after customer approval.

    The proposed action must match the trusted order and immutable version.
    """
    if proposed != action:
        raise ValueError("Changed order")
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS releases(operation TEXT PRIMARY KEY, receipt TEXT)")
        conn.execute("INSERT OR IGNORE INTO releases VALUES(?,?)", (operation, encoded(action)))
        return conn.execute("SELECT receipt FROM releases WHERE operation=?", (operation,)).fetchone()[0]


async def main():
    review = CustomerReview(
        database, PusharyServer(os.environ["PUSHARY_API_KEY"]),
        tenant="phone-demo", customer=customer, operation=operation,
        action=action, code_version="order-demo-v1", execute=release_order,
    )
    result = await review.start_review() if sys.argv[1] == "start" else await review.resume_review()
    print(encoded(result))


asyncio.run(main())
