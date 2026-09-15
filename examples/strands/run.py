"""Phone demo: record an approved order release locally; never charge or ship."""
import os
import sqlite3
import sys

from pushary import PusharyServer
from strands import Agent, tool
from review import CustomerReview, encoded

if len(sys.argv) != 2 or sys.argv[1] not in ("start", "resume"):
    raise SystemExit("Usage: python run.py start|resume")

# In an application, load these from the authenticated tenant and immutable order.
# Never take the customer, operation identity, or approved action from the model.
database = os.environ.get("REVIEW_DATABASE", "reviews.sqlite")
customer = os.environ["PUSHARY_EXTERNAL_ID"]
action = {"order_id": "demo_order_1", "draft_version": 1}
operation = "release_demo_order_1_v1"
review = CustomerReview(
    database, PusharyServer(os.environ["PUSHARY_API_KEY"]),
    tenant="phone-demo", customer=customer, operation=operation,
    tool_name="release_order", arguments=action, code_version="order-demo-v1",
)


@tool
def release_order(order_id: str, draft_version: int) -> str:
    """Record a local demonstration order release after customer approval.

    Args:
        order_id: The exact proposed order ID.
        draft_version: Its immutable draft version.
    """
    if {"order_id": order_id, "draft_version": draft_version} != action:
        raise ValueError("Changed order")
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS releases(operation TEXT PRIMARY KEY, receipt TEXT)")
        conn.execute("INSERT OR IGNORE INTO releases VALUES(?,?)", (operation, encoded(action)))
        return conn.execute("SELECT receipt FROM releases WHERE operation=?", (operation,)).fetchone()[0]


# Uses Strands' default Bedrock model and your configured AWS credentials.
# Keep model configuration, tool definitions and hooks identical after restart.
agent = Agent(tools=[release_order], hooks=[review], callback_handler=None)
if sys.argv[1] == "start":
    result = review.start(agent, f"Call release_order exactly once with {encoded(action)}. Then stop.")
else:
    result = review.resume(agent)
print(encoded(result))
