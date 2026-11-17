import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import app
from conftest import (HOME_ID, MODULE_ID, ROOM_ID, STATION_ID, FakeHttp, FakeSsm,
                      load, make_config)

ROME = ZoneInfo("Europe/Rome")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def rome(*args):
    return datetime(*args, tzinfo=ROME).astimezone(timezone.utc)


@pytest.fixture
def home():
    return app.find_home(load("homesdata"), HOME_ID)


# --------------------------------------------------------------------------- #
# Scheduled setpoint
# --------------------------------------------------------------------------- #
class TestScheduledSetpoint:
    def test_normal_slot(self, home):
        # Monday 10:00 -> comfort slot (from 07:00)
        assert app.scheduled_setpoint(home, ROOM_ID, rome(2026, 11, 16, 10, 0)) == 20.0
        # Monday 23:30 -> night slot (from 23:00)
        assert app.scheduled_setpoint(home, ROOM_ID, rome(2026, 11, 16, 23, 30)) == 17.0

    def test_exact_slot_boundary(self, home):
        assert app.scheduled_setpoint(home, ROOM_ID, rome(2026, 11, 16, 7, 0)) == 20.0
        assert app.scheduled_setpoint(home, ROOM_ID, rome(2026, 11, 16, 6, 59)) == 17.0

    def test_week_start(self, home):
        # Monday 00:00 exactly -> slot with m_offset 0
        assert app.scheduled_setpoint(home, ROOM_ID, rome(2026, 11, 16, 0, 0)) == 17.0

    def test_sunday_to_monday_wrap(self, home):
        sched = app.selected_schedule(home)
        # Remove the m_offset 0 entry: early Monday must still use Sunday's last slot.
        sched["timetable"] = [s for s in sched["timetable"] if s["m_offset"] != 0]
        sched["timetable"][-1]["zone_id"] = 4  # Sunday 23:00 -> Eco (18.5)
        assert app.scheduled_setpoint(home, ROOM_ID, rome(2026, 11, 16, 3, 0)) == 18.5
        # Late Sunday falls in the same slot
        assert app.scheduled_setpoint(home, ROOM_ID, rome(2026, 11, 22, 23, 45)) == 18.5

    def test_ignores_unselected_and_cooling_schedules(self, home):
        assert app.selected_schedule(home)["id"] == "therm1"

    def test_dst_spring_forward_europe_rome(self, home):
        # 2026-03-29 (Sunday): 07:30Z = 09:30 CEST -> comfort (Sunday from 09:00).
        # A fixed UTC+1 offset would give 08:30 -> night.
        assert app.scheduled_setpoint(home, ROOM_ID, utc(2026, 3, 29, 7, 30)) == 20.0

    def test_dst_fall_back_europe_rome(self, home):
        # 2026-10-25 (Sunday): 07:30Z = 08:30 CET -> still night.
        # A fixed UTC+2 offset would give 09:30 -> comfort.
        assert app.scheduled_setpoint(home, ROOM_ID, utc(2026, 10, 25, 7, 30)) == 17.0
        assert app.scheduled_setpoint(home, ROOM_ID, utc(2026, 10, 25, 8, 0)) == 20.0

    def test_unknown_room(self, home):
        assert app.scheduled_setpoint(home, "nope", rome(2026, 11, 16, 10, 0)) is None


# --------------------------------------------------------------------------- #
# Offset / rounding
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    (20.0, 20.0), (20.24, 20.0), (20.25, 20.5), (20.74, 20.5), (20.75, 21.0), (21.3, 21.5),
])
def test_round_half(value, expected):
    assert app.round_half(value) == expected


@pytest.mark.parametrize("raw,expected", [(-1.0, 0.0), (1.7, 1.7), (5.2, 3.0)])
def test_clamp_offset(raw, expected):
    assert app.clamp(raw, 0.0, 3.0) == expected


# --------------------------------------------------------------------------- #
# decide()
# --------------------------------------------------------------------------- #
NOW = int(rome(2026, 11, 16, 10, 0).timestamp())


def room(**kw):
    r = {"id": ROOM_ID, "reachable": True, "therm_measured_temperature": 22.0,
         "therm_setpoint_temperature": 20.0, "therm_setpoint_mode": "schedule",
         "therm_setpoint_end_time": 0}
    r.update(kw)
    return r


def run_decide(cfg=None, *, home_mode="schedule", room_status=None, state=None, target=20.0,
               t_module=20.5, module_time=NOW - 120):
    log = {}
    d = app.decide(cfg or make_config(), home_mode=home_mode, room=room_status or room(),
                   state=state or {}, target=target, t_module=t_module,
                   module_time=module_time, now_ts=NOW, log=log)
    return d, log


class TestDecide:
    def test_write_from_schedule(self):
        d, log = run_decide()  # offset 1.5 -> 21.5
        assert d.action == "write"
        assert d.new_sp == 21.5
        assert d.endtime == NOW + 40 * 60
        assert log["offset"] == 1.5

    def test_offset_clamped_to_max_and_setpoint_range(self):
        d, _ = run_decide(room_status=room(therm_measured_temperature=30.0), target=28.5)
        assert d.new_sp == 30.0  # 28.5 + 3.0 clamped to 30

    def test_negative_offset_clamped_to_zero(self):
        d, log = run_decide(room_status=room(therm_measured_temperature=19.0))
        assert log["offset"] == 0.0
        assert d.label == "skip:offset_negligible"

    @pytest.mark.parametrize("mode", ["away", "hg", "off"])
    def test_home_mode_not_schedule(self, mode):
        d, _ = run_decide(home_mode=mode)
        assert d.label == f"skip:home_mode_{mode}"

    def test_module_stale(self):
        d, log = run_decide(module_time=NOW - 31 * 60)
        assert d.label == "skip:module_stale"
        assert log["module_age_min"] == 31.0

    def test_module_fresh_at_limit(self):
        d, _ = run_decide(module_time=NOW - 30 * 60)
        assert d.action == "write"

    def test_module_missing(self):
        d, _ = run_decide(t_module=None, module_time=None)
        assert d.label == "skip:module_missing"

    def test_user_override_respected(self):
        # Manual set from the app: does not match our last write.
        state = {"last_setpoint": 21.5, "last_endtime": NOW + 1800}
        r = room(therm_setpoint_mode="manual", therm_setpoint_temperature=23.0,
                 therm_setpoint_end_time=NOW + 3 * 3600)
        d, log = run_decide(room_status=r, state=state)
        assert d.label == "skip:user_override"
        assert log["ours"] is False

    def test_user_override_without_state(self):
        r = room(therm_setpoint_mode="manual", therm_setpoint_temperature=21.5,
                 therm_setpoint_end_time=NOW + 1800)
        assert run_decide(room_status=r, state={})[0].label == "skip:user_override"

    def test_user_changed_only_endtime(self):
        state = {"last_setpoint": 21.5, "last_endtime": NOW + 1800}
        r = room(therm_setpoint_mode="manual", therm_setpoint_temperature=21.5,
                 therm_setpoint_end_time=NOW + 7200)
        assert run_decide(room_status=r, state=state)[0].label == "skip:user_override"

    def test_boost_respected(self):
        assert run_decide(room_status=room(therm_setpoint_mode="max"))[0].label == "skip:room_mode_max"

    def _ours(self, sp=21.5, remaining_min=30, **kw):
        end = NOW + remaining_min * 60
        state = {"last_setpoint": sp, "last_endtime": end}
        r = room(therm_setpoint_mode="manual", therm_setpoint_temperature=sp,
                 therm_setpoint_end_time=end, **kw)
        return r, state

    def test_our_manual_within_hysteresis_skips(self):
        r, state = self._ours(sp=21.5)  # new_sp = 21.5
        d, log = run_decide(room_status=r, state=state)
        assert log["ours"] is True
        assert d.label == "skip:within_hysteresis"

    def test_our_manual_hysteresis_exceeded_writes(self):
        r, state = self._ours(sp=21.0)  # new_sp 21.5, diff 0.5 >= 0.5
        d, _ = run_decide(room_status=r, state=state)
        assert d.action == "write" and d.new_sp == 21.5

    def test_hysteresis_larger_than_change_skips(self):
        r, state = self._ours(sp=21.0)
        d, _ = run_decide(make_config(hysteresis=1.0), room_status=r, state=state)
        assert d.label == "skip:within_hysteresis"

    def test_endtime_renewal(self):
        r, state = self._ours(sp=21.5, remaining_min=14)
        d, _ = run_decide(room_status=r, state=state)
        assert d.action == "write"
        assert d.endtime == NOW + 40 * 60

    def test_endtime_not_yet_due(self):
        r, state = self._ours(sp=21.5, remaining_min=16)
        assert run_decide(room_status=r, state=state)[0].label == "skip:within_hysteresis"

    def test_release_when_offset_vanishes(self):
        r, state = self._ours(sp=21.5, therm_measured_temperature=20.6)
        d, _ = run_decide(room_status=r, state=state, t_module=20.5)  # offset 0.1
        assert d.action == "release"

    def test_valve_unreachable(self):
        d, _ = run_decide(room_status=room(reachable=False))
        assert d.label == "skip:valve_unreachable"


# --------------------------------------------------------------------------- #
# Reference reading
# --------------------------------------------------------------------------- #
def test_reference_reading_module_and_main():
    st = load("getstationsdata")
    assert app.reference_reading(st, STATION_ID, MODULE_ID) == (20.1, 1763380450)
    assert app.reference_reading(st, STATION_ID, "") == (21.0, 1763380500)
    assert app.reference_reading(st, STATION_ID, "03:00:00:00:00:00") == (None, None)
    assert app.reference_reading(st, "70:ee:50:00:00:00", "") == (None, None)


# --------------------------------------------------------------------------- #
# End-to-end run() with mocked SSM and HTTP
# --------------------------------------------------------------------------- #
def make_run(fixtures, *, cfg=None, state=None, fail_put=False, now=None):
    events = []
    cfg = cfg or make_config()
    ssm = FakeSsm(events, state=state, fail_put=fail_put)
    http = FakeHttp(events, fixtures)
    api = app.NetatmoClient(http=http, sleep=lambda s: None)
    now = now or datetime.fromtimestamp(fixtures["getstationsdata"]["time_server"], timezone.utc)
    return events, ssm, http, (lambda: app.run(cfg, app.SsmStore(ssm, cfg), api, now=now))


@pytest.fixture
def monday_10(fixtures):
    """Fixtures aligned on Monday 2026-11-16 10:00 Rome, fresh module data."""
    now = rome(2026, 11, 16, 10, 0)
    fixtures["getstationsdata"]["time_server"] = int(now.timestamp())
    for m in fixtures["getstationsdata"]["body"]["devices"][0]["modules"]:
        m["dashboard_data"]["time_utc"] = int(now.timestamp()) - 300
    return fixtures


class TestRun:
    def test_refresh_token_saved_before_other_calls(self, monday_10):
        events, ssm, http, go = make_run(monday_10)
        go()
        put_idx = events.index(("ssm.put", "/netatmo-trv/refresh_token", "SecureString"))
        token_idx = events.index(("http", "POST", "/oauth2/token"))
        homes_idx = events.index(("http", "GET", "/api/homesdata"))
        assert token_idx < put_idx < homes_idx
        assert ssm.params["/netatmo-trv/refresh_token"] == "RT-new-xxxx"

    def test_refresh_token_save_failure_aborts(self, monday_10):
        events, _, http, go = make_run(monday_10, fail_put=True)
        with pytest.raises(RuntimeError):
            go()
        assert not any(e[0] == "http" and e[2] != "/oauth2/token" for e in events)

    def test_write_and_state_update(self, monday_10):
        events, ssm, http, go = make_run(monday_10)
        log = go()
        # valve 22.4, module 20.1 -> offset 2.3, target 20 -> 22.5
        assert log["decision"] == "write"
        assert log["new_sp"] == 22.5
        assert http.posts == [{"home_id": HOME_ID, "room_id": ROOM_ID, "mode": "manual",
                               "temp": "22.5", "endtime": str(log["endtime"])}]
        state = json.loads(ssm.params["/netatmo-trv/state"])
        assert state["last_setpoint"] == 22.5
        assert state["last_endtime"] == log["endtime"]
        assert "last_write_utc" in state

    def test_dry_run_writes_nothing_but_token(self, monday_10):
        events, ssm, http, go = make_run(monday_10, cfg=make_config(dry_run=True))
        log = go()
        assert log["decision"] == "write" and log["dry_run"] is True
        assert http.posts == []
        assert "/netatmo-trv/state" not in ssm.params
        assert ssm.params["/netatmo-trv/refresh_token"] == "RT-new-xxxx"

    def test_home_away_skips(self, monday_10):
        monday_10["homesdata"]["body"]["homes"][0]["therm_mode"] = "away"
        _, ssm, http, go = make_run(monday_10)
        assert go()["decision"] == "skip:home_mode_away"
        assert http.posts == [] and "/netatmo-trv/state" not in ssm.params

    def test_stale_module_skips(self, monday_10):
        mods = monday_10["getstationsdata"]["body"]["devices"][0]["modules"]
        mods[1]["dashboard_data"]["time_utc"] -= 3600
        _, _, http, go = make_run(monday_10)
        assert go()["decision"] == "skip:module_stale"
        assert http.posts == []

    def test_release(self, monday_10):
        now = rome(2026, 11, 16, 10, 0)
        end = int(now.timestamp()) + 1200
        r = monday_10["homestatus"]["body"]["home"]["rooms"][0]
        r.update(therm_setpoint_mode="manual", therm_setpoint_temperature=21.0,
                 therm_setpoint_end_time=end, therm_measured_temperature=20.2)
        _, ssm, http, go = make_run(monday_10, state={"last_setpoint": 21.0, "last_endtime": end})
        assert go()["decision"] == "release"
        assert http.posts == [{"home_id": HOME_ID, "room_id": ROOM_ID, "mode": "home"}]
        assert json.loads(ssm.params["/netatmo-trv/state"])["last_setpoint"] is None

    def test_retry_once_on_5xx(self, monday_10):
        _, _, http, go = make_run(monday_10)
        http.overrides["/api/homestatus"] = [(503, "busy")]
        assert go()["decision"] == "write"

    def test_api_error_raises_without_writing(self, monday_10):
        _, ssm, http, go = make_run(monday_10)
        http.overrides["/api/homestatus"] = [(503, "busy"), (502, "bad")]
        with pytest.raises(app.NetatmoError):
            go()
        assert http.posts == [] and "/netatmo-trv/state" not in ssm.params

    def test_invalid_grant(self, monday_10):
        _, _, http, go = make_run(monday_10)
        http.overrides["/oauth2/token"] = [(400, json.dumps({"error": "invalid_grant"}))]
        with pytest.raises(app.InvalidGrantError):
            go()

    def test_log_never_contains_secrets(self, monday_10, capsys, monkeypatch):
        _, _, _, go = make_run(monday_10)
        app._emit(go())
        out = capsys.readouterr().out
        for secret in ("RT-new", "RT-old", "AT-new", "csecret"):
            assert secret not in out
        rec = json.loads(out)
        for key in ("t_valve", "t_module", "module_age_min", "target", "offset", "current_sp",
                    "room_mode", "decision", "new_sp", "dry_run"):
            assert key in rec


def test_heating_overview_logged(monday_10):
    _, _, _, go = make_run(monday_10)
    log = go()
    assert log["rooms_power_request"] == {"Studio": 0, "Soggiorno": 75}
    assert log["boiler_status"] is True
    assert log["heating_power_request"] == 0


def test_rooms_overview_logged(monday_10):
    _, _, _, go = make_run(monday_10)
    rooms = {r["name"]: r for r in go()["rooms"]}
    assert set(rooms) == {"Studio", "Soggiorno"}
    assert rooms["Studio"]["compensated"] is True and rooms["Soggiorno"]["compensated"] is False
    assert rooms["Studio"]["t"] == 22.4 and rooms["Studio"]["sp"] == 20
    assert rooms["Soggiorno"]["power"] == 75 and rooms["Soggiorno"]["mode"] == "schedule"


def test_schedule_summary(home):
    s = app.schedule_summary(home)
    assert s["name"] == "Inverno" and s["timezone"] == "Europe/Rome"
    zones = {z["id"]: z for z in s["zones"]}
    assert zones[0]["rooms"] == {"Studio": 20, "Soggiorno": 21}
    assert zones[4]["rooms"] == {"Studio": 18.5}
    offs = [t["m_offset"] for t in s["timetable"]]
    assert offs == sorted(offs) and offs[0] == 0


def test_schedule_not_in_log(monday_10, capsys, monkeypatch):
    _, _, _, go = make_run(monday_10)
    rec = go()
    assert rec["schedule"]["name"] == "Inverno"  # returned by run(), popped by _execute()


def test_room_devices(monday_10):
    _, _, _, go = make_run(monday_10)
    rooms = {r["name"]: r for r in go()["rooms"]}
    studio = rooms["Studio"]["devices"]
    assert studio == [{"id": "09:00:00:aa:bb:cc", "type": "NRV", "name": "Valvola Studio",
                       "reachable": True, "battery_state": "full", "battery_level": 3020,
                       "rf_strength": 62}]
    salone = rooms["Soggiorno"]["devices"]
    assert [d["id"] for d in salone] == ["09:00:00:aa:bb:01", "09:00:00:aa:bb:02"]
    assert salone[0]["battery_state"] == "low" and salone[0]["rf_strength"] == 82
    assert salone[1]["name"] is None and salone[1]["reachable"] is False
    assert salone[1]["battery_state"] is None
