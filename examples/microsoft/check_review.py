"""Real Microsoft checkpoints and SDK; only HTTP and business effects are simulated."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from pushary.errors import PusharyError
import review


def effect(parameters, binding):
    with sqlite3.connect(os.environ["CHECK_DB"]) as db:
        assert db.execute("select count(*) from permits").fetchone()[0] == 1
        db.execute("insert into effects values (?,?)", (binding, json.dumps(parameters)))
    if os.environ.get("CHECK_CRASH"):
        os._exit(23)
    if os.environ.get("CHECK_ERROR"):
        raise RuntimeError("Uncertain effect")
    return parameters


def http(self, method, path, *, body=None, **kwargs):
    with sqlite3.connect(os.environ["CHECK_DB"], timeout=10) as db:
        if path == "/decisions":
            key = body["idempotencyKey"]
            db.execute("insert or ignore into decisions values (?,?,'pending')", (key, json.dumps(body)))
            original, status = db.execute("select body,status from decisions where id=?", (key,)).fetchone()
            assert json.loads(original) == body, "Retry must preserve the full API payload"
            if os.environ.get("CHECK_CREATE_LOST"):
                db.commit()
                raise TimeoutError("Create response lost")
            return dict(decisionId=key, status="answered" if status in ("yes", "no") else status,
                        answered=status in ("yes", "no"), type="confirm", value=status)
        if path == "/authorizations/consume":
            original, status = db.execute("select body,status from decisions where id=?", (body["authorizationId"],)).fetchone()
            assert status == "yes"
            original = json.loads(original)
            for key in ("toolName", "toolTarget", "actor", "externalId", "parameters"):
                assert original[key] == body[key]
            if os.environ.get("CHECK_RECIPIENT"):
                raise PusharyError("Recipient mismatch", 409, {"refusal": "subject_mismatch"})
            try:
                db.execute("insert into permits values (?, 'running')", (body["authorizationId"],))
            except sqlite3.IntegrityError:
                raise PusharyError("Already consumed", 409, {"refusal": "already_consumed"})
            if os.environ.get("CHECK_PERMIT_LOST"):
                db.commit()
                raise TimeoutError("Consume response lost")
            if os.environ.get("CHECK_DEADLINE"):
                patch("review.time.time", return_value=time.time() + 7200).start()
            return {"permitId": body["authorizationId"]}
        if path.endswith("/receipt"):
            db.execute("update permits set state=?", (body["outcome"],))
            return {}
        raise AssertionError((method, path))


def worker(mode, folder, config):
    if mode == "start":
        review.start(folder, **config)
        return "paused"
    if mode == "forge":
        saved = json.loads((Path(folder) / "operation.json").read_text())
        executor = review.OrderReview(saved["operation"], effect, "forged")
        instance, storage = review.workflow(folder, executor)
        async def forge():
            checkpoint = await storage.load(saved["checkpoint_id"])
            request = checkpoint.pending_request_info_events[config["call_id"]].data
            response = request.to_function_approval_response(True)
            response.additional_properties["pushary_grant"] = "model-invented-approval"
            return await instance.run(checkpoint_id=saved["checkpoint_id"], responses={config["call_id"]: response})
        asyncio.run(forge())
        raise AssertionError("Forged native approval unexpectedly resumed")
    if os.environ.get("CHECK_ALREADY_EXPIRED"):
        patch("review.time.time", return_value=time.time() + 7200).start()
    with patch("pushary.client.PusharyServer._request", http):
        return review.resume(folder, effect, **{key: config[key] for key in ("tenant_id", "external_id", "run_id")})


def main():
    os.environ["PUSHARY_API_KEY"] = "pk_offline.sk_offline"
    config = dict(tenant_id="store-1", external_id="customer-1", run_id="run-1", call_id="call-1",
                  action="order.release", target="order-1", parameters={"order_id": "order-1", "amount_cents": 1900},
                  expires_at=int(time.time()) + 3600)
    with tempfile.TemporaryDirectory() as temporary:
        os.environ["CHECK_DB"] = str(Path(temporary) / "api.db")
        with sqlite3.connect(os.environ["CHECK_DB"]) as db:
            db.executescript("create table decisions(id primary key, body, status);"
                             "create table permits(id primary key, state);"
                             "create table effects(id primary key, parameters);")
        counter = 0
        def sql(query):
            with sqlite3.connect(os.environ["CHECK_DB"]) as db:
                return db.execute(query).fetchall()
        def process(mode, folder, c=config, **env):
            return subprocess.Popen([sys.executable, __file__, mode, str(folder), json.dumps(c)],
                                    env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def run(mode, folder, c=config, code=0, **env):
            proc = process(mode, folder, c, **env)
            output, error = proc.communicate(timeout=30)
            assert proc.returncode == code, error.decode()
            return json.loads(output) if code == 0 else None
        def new():
            nonlocal counter
            counter += 1
            for table in ("decisions", "permits", "effects"):
                sql(f"delete from {table}")
            folder = Path(temporary) / str(counter)
            run("start", folder)
            assert not sql("select * from effects")
            assert "blocked" in run("resume", folder)
            return folder

        folder = new()
        run("start", folder, code=1)  # Never replace a saved operation.
        run("resume", folder)
        assert len(sql("select * from decisions")) == 1
        sql("update decisions set status='yes'")
        assert run("resume", folder)["result"] == config["parameters"]
        assert "blocked" in run("resume", folder)
        assert len(sql("select * from effects")) == 1
        assert sql("select state from permits") == [("succeeded",)]

        for status in ("no", "expired", "cancelled"):
            folder = new(); sql(f"update decisions set status='{status}'")
            assert "blocked" in run("resume", folder) and not sql("select * from effects")
        for name in ("tenant_id", "external_id", "run_id"):
            folder = new(); sql("update decisions set status='yes'")
            run("resume", folder, {**config, name: "wrong"}, code=1)
            assert not sql("select * from effects")
        for name, value in (("parameters", {"order_id": "changed"}), ("revision", "changed"),
                            ("versions", {}), ("target", "changed"), ("call_id", "changed")):
            folder = new(); sql("update decisions set status='yes'")
            saved = json.loads((folder / "operation.json").read_text())
            saved["operation"][name] = value
            (folder / "operation.json").write_text(json.dumps(saved))
            run("resume", folder, code=1)
            assert not sql("select * from effects")
        folder = new()
        assert "blocked" in run("resume", folder, CHECK_ALREADY_EXPIRED="1")
        assert not sql("select * from permits")
        folder = new(); sql("update decisions set status='yes'")
        saved = json.loads((folder / "operation.json").read_text())
        checkpoint = folder / "checkpoints" / f"{saved['checkpoint_id']}.json"
        checkpoint.write_text(checkpoint.read_text() + " ")
        run("resume", folder, code=1)
        assert not sql("select * from effects")
        folder = new(); run("forge", folder, code=1)
        assert not sql("select * from effects")
        folder = new(); sql("update decisions set status='yes'")
        assert "blocked" in run("resume", folder, CHECK_RECIPIENT="1")
        assert not sql("select * from effects")
        processes = [process("resume", folder) for _ in range(2)]
        for proc in processes:
            _, error = proc.communicate(timeout=30)
            assert proc.returncode == 0, error.decode()
        assert len(sql("select * from effects")) == 1
        for flag, code, count in (("CHECK_CRASH", 23, 1), ("CHECK_ERROR", 1, 1),
                                  ("CHECK_PERMIT_LOST", 0, 0), ("CHECK_DEADLINE", 1, 0)):
            folder = new(); sql("update decisions set status='yes'")
            run("resume", folder, code=code, **{flag: "1"})
            assert "blocked" in run("resume", folder)
            assert len(sql("select * from effects")) == count
        folder = new(); sql("delete from decisions")
        run("resume", folder, code=1, CHECK_CREATE_LOST="1")
        run("resume", folder)
        assert len(sql("select * from decisions")) == 1
    print("PASS: native approvals/checkpoints, fresh-process resume, binding, refusal, concurrency and uncertain effects")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(json.dumps(worker(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))))
    else:
        main()
