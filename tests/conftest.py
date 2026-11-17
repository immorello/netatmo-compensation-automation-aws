import copy
import json
import pathlib
import sys
import urllib.parse

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ROOM_ID = "2746182631"
HOME_ID = "5c810xxxxxxxxxxxxxxxxxxx"
STATION_ID = "70:ee:50:aa:bb:cc"
MODULE_ID = "03:00:00:aa:bb:cc"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def fixtures():
    return {n: load(n) for n in ("homesdata", "homestatus", "getstationsdata", "token")}


def make_config(**overrides):
    base = dict(home_id=HOME_ID, room_id=ROOM_ID, station_device_id=STATION_ID,
                indoor_module_id=MODULE_ID, dry_run=False)
    base.update(overrides)
    return app.Config(**base)


class ParameterNotFound(Exception):
    pass


class FakeSsm:
    """Minimal boto3 SSM stand-in that records calls into a shared event log."""

    def __init__(self, events, prefix="/netatmo-trv", state=None, fail_put=False):
        self.events = events
        self.fail_put = fail_put
        self.params = {
            f"{prefix}/client_id": "cid",
            f"{prefix}/client_secret": "csecret",
            f"{prefix}/refresh_token": "RT-old",
        }
        if state is not None:
            self.params[f"{prefix}/state"] = json.dumps(state)

    def get_parameter(self, Name, WithDecryption=False):
        self.events.append(("ssm.get", Name))
        if Name not in self.params:
            raise ParameterNotFound(Name)
        return {"Parameter": {"Name": Name, "Value": self.params[Name]}}

    def put_parameter(self, Name, Value, Type, Overwrite):
        self.events.append(("ssm.put", Name, Type))
        if self.fail_put:
            raise RuntimeError("AccessDenied")
        self.params[Name] = Value


class FakeHttp:
    """Routes Netatmo requests to fixture payloads and records them."""

    def __init__(self, events, payloads):
        self.events = events
        self.payloads = copy.deepcopy(payloads)
        self.posts = []
        self.overrides = {}  # path -> list of (status, body) to return first

    def __call__(self, method, url, form, headers):
        path = urllib.parse.urlparse(url).path
        self.events.append(("http", method, path))
        if path != "/oauth2/token":
            assert headers.get("Authorization") == "Bearer AT-new-xxxx"
        if self.overrides.get(path):
            return self.overrides[path].pop(0)
        if path == "/oauth2/token":
            return 200, json.dumps(self.payloads["token"])
        if path == "/api/homesdata":
            return 200, json.dumps(self.payloads["homesdata"])
        if path == "/api/homestatus":
            return 200, json.dumps(self.payloads["homestatus"])
        if path == "/api/getstationsdata":
            return 200, json.dumps(self.payloads["getstationsdata"])
        if path == "/api/setroomthermpoint":
            self.posts.append(dict(form))
            return 200, json.dumps({"status": "ok", "time_server": 0})
        return 404, json.dumps({"error": {"code": 404, "message": "not found"}})
