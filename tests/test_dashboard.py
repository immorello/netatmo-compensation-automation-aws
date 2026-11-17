import io
import json
import sys
import types

import pytest

import app


class NoSuchKey(Exception):
    pass


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        assert CacheControl == "no-store"
        self.objects[Key] = Body


def test_status_store_writes_status_and_prunes_history():
    s3 = FakeS3()
    store = app.StatusStore(s3, "bucket")
    old = {"ts": 1_000_000, "decision": "write"}
    s3.objects["data/history.json"] = json.dumps([old]).encode()
    rec = {"ts": 1_000_000 + 24 * 3600 + 1, "decision": "skip:x", "t_valve": 22.0,
           "rooms_power_request": {"A": 1}}
    store.save(rec)
    assert json.loads(s3.objects["data/status.json"])["rooms_power_request"] == {"A": 1}
    hist = json.loads(s3.objects["data/history.json"])
    assert [h["ts"] for h in hist] == [rec["ts"]]  # old entry dropped
    assert set(hist[0]) == set(app.HISTORY_FIELDS)


def test_status_store_first_run_without_history():
    s3 = FakeS3()
    app.StatusStore(s3, "b").save({"ts": 10, "decision": "write"})
    assert len(json.loads(s3.objects["data/history.json"])) == 1


@pytest.fixture
def lambda_env(monkeypatch):
    s3 = FakeS3()
    fake_boto3 = types.SimpleNamespace(client=lambda name: s3 if name == "s3" else object())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    for k, v in {"HOME_ID": "h", "ROOM_ID": "r", "STATION_DEVICE_ID": "s",
                 "DASHBOARD_BUCKET": "bucket"}.items():
        monkeypatch.setenv(k, v)
    return s3


def http_event(method="POST", path="/api/run"):
    return {"rawPath": path, "requestContext": {"http": {"method": method}}}


def test_http_run_ok(lambda_env, monkeypatch, capsys):
    sched = {"name": "Inverno", "zones": [], "timetable": []}
    monkeypatch.setattr(app, "run", lambda *a, **k: {"ts": 5, "decision": "write", "schedule": sched})
    resp = app.lambda_handler(http_event(), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["decision"] == "write" and body["trigger"] == "manual"
    assert "schedule" not in body
    status = json.loads(lambda_env.objects["data/status.json"])
    assert status["trigger"] == "manual" and "schedule" not in status
    saved = json.loads(lambda_env.objects["data/schedule.json"])
    assert saved["name"] == "Inverno" and saved["updated_ts"] == 5
    assert "Inverno" not in capsys.readouterr().out  # schedule is not logged


def test_http_run_error_returns_502_without_raising(lambda_env, monkeypatch):
    def boom(*a, **k):
        raise app.NetatmoError("down")
    monkeypatch.setattr(app, "run", boom)
    resp = app.lambda_handler(http_event(), None)
    assert resp["statusCode"] == 502
    assert json.loads(resp["body"])["decision"] == "error"
    assert json.loads(lambda_env.objects["data/status.json"])["error"] == "down"


@pytest.mark.parametrize("method,path,code", [("GET", "/api/run", 405), ("POST", "/api/x", 404)])
def test_http_routing(lambda_env, monkeypatch, method, path, code):
    monkeypatch.setattr(app, "run", lambda *a, **k: pytest.fail("must not run"))
    assert app.lambda_handler(http_event(method, path), None)["statusCode"] == code


def test_scheduled_error_is_raised_and_status_saved(lambda_env, monkeypatch):
    def boom(*a, **k):
        raise app.NetatmoError("down")
    monkeypatch.setattr(app, "run", boom)
    with pytest.raises(app.NetatmoError):
        app.lambda_handler({}, None)
    assert json.loads(lambda_env.objects["data/status.json"])["trigger"] == "schedule"


def test_status_write_failure_does_not_break_run(lambda_env, monkeypatch):
    monkeypatch.setattr(app, "run", lambda *a, **k: {"ts": 5, "decision": "write"})
    def fail(**k):
        raise RuntimeError("AccessDenied")
    lambda_env.put_object = fail
    assert app.lambda_handler({}, None)["decision"] == "write"
