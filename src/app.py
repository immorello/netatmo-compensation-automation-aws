"""Netatmo TRV compensator.

Every run reads the valve (NRV) temperature and the reference temperature of a
Netatmo weather-station module in the same room, then sets a compensated manual
setpoint on the valve with a short ``endtime``. If this function stops running,
the manual setpoint expires by itself and the room goes back to the Netatmo
schedule (dead-man switch).

Only stdlib + boto3 (bundled with the Lambda runtime).
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

API_BASE = "https://api.netatmo.com"
HTTP_TIMEOUT_S = 10
RETRY_BACKOFF_S = 1.0

SP_MIN = 7.0
SP_MAX = 30.0
RELEASE_OFFSET_THRESHOLD = 0.25
# Tolerances used to recognise a manual setpoint written by this function.
SP_MATCH_TOLERANCE = 0.05
ENDTIME_MATCH_TOLERANCE_S = 90

MINUTES_PER_WEEK = 7 * 24 * 60


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    home_id: str
    room_id: str
    station_device_id: str
    indoor_module_id: str = ""
    max_offset: float = 3.0
    min_offset: float = 0.0
    hysteresis: float = 0.5
    manual_duration_minutes: int = 40
    renew_before_minutes: int = 15
    max_sensor_age_minutes: int = 30
    dry_run: bool = True
    parameter_prefix: str = "/netatmo-trv"

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Config":
        e = os.environ if env is None else env
        return cls(
            home_id=e["HOME_ID"],
            room_id=e["ROOM_ID"],
            station_device_id=e["STATION_DEVICE_ID"],
            indoor_module_id=e.get("INDOOR_MODULE_ID", "").strip(),
            max_offset=float(e.get("MAX_OFFSET", "3.0")),
            min_offset=float(e.get("MIN_OFFSET", "0.0")),
            hysteresis=float(e.get("HYSTERESIS", "0.5")),
            manual_duration_minutes=int(e.get("MANUAL_DURATION_MINUTES", "40")),
            renew_before_minutes=int(e.get("RENEW_BEFORE_MINUTES", "15")),
            max_sensor_age_minutes=int(e.get("MAX_SENSOR_AGE_MINUTES", "30")),
            # Anything other than an explicit "false" keeps dry run on.
            dry_run=e.get("DRY_RUN", "true").strip().lower() != "false",
            parameter_prefix=e.get("PARAMETER_PREFIX", "/netatmo-trv").rstrip("/"),
        )

    def param(self, name: str) -> str:
        return f"{self.parameter_prefix}/{name}"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class NetatmoError(Exception):
    """Any failure talking to the Netatmo API."""


class InvalidGrantError(NetatmoError):
    """The refresh token was rejected: it must be regenerated (see README)."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
HttpFn = Callable[[str, str, Optional[dict], dict], tuple]


def urllib_http(method: str, url: str, form: Optional[dict], headers: dict) -> tuple:
    """Perform one HTTP request. Returns (status, body_text).

    HTTP error statuses are returned, not raised; network errors raise URLError.
    """
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers))
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded;charset=UTF-8")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _error_message(body: str) -> str:
    """Extract a short error description from a Netatmo error body (never contains secrets)."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return body[:200]
    err = parsed.get("error")
    if isinstance(err, dict):
        return f"{err.get('code')}: {err.get('message')}"
    if isinstance(err, str):
        return err
    return body[:200]


class NetatmoClient:
    def __init__(
        self,
        http: HttpFn = urllib_http,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = API_BASE,
    ):
        self._http = http
        self._sleep = sleep
        self._base = base_url
        self._access_token: Optional[str] = None

    # -- low level -------------------------------------------------------- #
    def _request(self, method: str, path: str, form: Optional[dict] = None,
                 query: Optional[dict] = None, auth: bool = True) -> dict:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json"}
        if auth:
            if not self._access_token:
                raise NetatmoError("access token not available")
            headers["Authorization"] = f"Bearer {self._access_token}"

        last_exc: Optional[Exception] = None
        for attempt in range(2):  # one retry on 5xx / network errors
            if attempt:
                self._sleep(RETRY_BACKOFF_S)
            try:
                status, body = self._http(method, url, form, headers)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = NetatmoError(f"{method} {path}: network error: {exc}")
                continue
            if status >= 500:
                last_exc = NetatmoError(f"{method} {path}: HTTP {status}: {_error_message(body)}")
                continue
            if status >= 400:
                msg = _error_message(body)
                if path == "/oauth2/token" and "invalid_grant" in msg:
                    raise InvalidGrantError(
                        "refresh token rejected (invalid_grant): regenerate it and "
                        "rerun scripts/bootstrap_tokens.sh"
                    )
                raise NetatmoError(f"{method} {path}: HTTP {status}: {msg}")
            try:
                return json.loads(body)
            except ValueError as exc:
                raise NetatmoError(f"{method} {path}: invalid JSON response") from exc
        assert last_exc is not None
        raise last_exc

    # -- API -------------------------------------------------------------- #
    def refresh(self, client_id: str, client_secret: str, refresh_token: str) -> dict:
        data = self._request(
            "POST", "/oauth2/token", auth=False,
            form={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        if not data.get("access_token") or not data.get("refresh_token"):
            raise NetatmoError("token response without access_token/refresh_token")
        self._access_token = data["access_token"]
        return data

    def homesdata(self, home_id: str) -> dict:
        return self._request("GET", "/api/homesdata", query={"home_id": home_id})

    def homestatus(self, home_id: str) -> dict:
        return self._request("GET", "/api/homestatus", query={"home_id": home_id})

    def getstationsdata(self, device_id: str) -> dict:
        return self._request("GET", "/api/getstationsdata", query={"device_id": device_id})

    def set_room_manual(self, home_id: str, room_id: str, temp: float, endtime: int) -> dict:
        return self._check_ok(self._request(
            "POST", "/api/setroomthermpoint",
            form={"home_id": home_id, "room_id": room_id, "mode": "manual",
                  "temp": f"{temp:.1f}", "endtime": str(int(endtime))},
        ))

    def release_room(self, home_id: str, room_id: str) -> dict:
        return self._check_ok(self._request(
            "POST", "/api/setroomthermpoint",
            form={"home_id": home_id, "room_id": room_id, "mode": "home"},
        ))

    @staticmethod
    def _check_ok(resp: dict) -> dict:
        if resp.get("status") != "ok":
            raise NetatmoError(f"setroomthermpoint: unexpected response status {resp.get('status')!r}")
        return resp


# --------------------------------------------------------------------------- #
# SSM
# --------------------------------------------------------------------------- #
class SsmStore:
    def __init__(self, client: Any, config: Config):
        self._ssm = client
        self._cfg = config

    def get(self, name: str, decrypt: bool = False) -> str:
        resp = self._ssm.get_parameter(Name=self._cfg.param(name), WithDecryption=decrypt)
        return resp["Parameter"]["Value"]

    def put_refresh_token(self, token: str) -> None:
        # Any exception propagates: we must not proceed if the rotated token is lost.
        self._ssm.put_parameter(
            Name=self._cfg.param("refresh_token"), Value=token,
            Type="SecureString", Overwrite=True,
        )

    def get_state(self) -> dict:
        try:
            raw = self.get("state")
        except Exception as exc:  # ParameterNotFound on first run
            if type(exc).__name__ == "ParameterNotFound" or "ParameterNotFound" in str(exc):
                return {}
            raise
        try:
            state = json.loads(raw)
        except ValueError:
            return {}
        return state if isinstance(state, dict) else {}

    def put_state(self, state: dict) -> None:
        self._ssm.put_parameter(
            Name=self._cfg.param("state"), Value=json.dumps(state, sort_keys=True),
            Type="String", Overwrite=True,
        )


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #
def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def round_half(value: float) -> float:
    """Round to the nearest 0.5 (halves rounded up, no banker's rounding)."""
    return math.floor(value * 2 + 0.5) / 2


def minutes_since_monday(now_utc: datetime, tz_name: str) -> int:
    local = now_utc.astimezone(ZoneInfo(tz_name))
    return local.weekday() * 1440 + local.hour * 60 + local.minute


def find_home(homesdata: dict, home_id: str) -> dict:
    for home in homesdata.get("body", {}).get("homes", []):
        if home.get("id") == home_id:
            return home
    raise NetatmoError(f"home {home_id} not found in homesdata")


def selected_schedule(home: dict) -> Optional[dict]:
    for sched in home.get("schedules", []):
        # Homes can have several schedule types (therm, cooling, event...).
        if sched.get("selected") and sched.get("type", "therm") == "therm":
            return sched
    return None


def scheduled_setpoint(home: dict, room_id: str, now_utc: datetime) -> Optional[float]:
    """Setpoint of the active schedule slot for ``room_id`` at ``now_utc``.

    The active slot is the timetable entry with the largest ``m_offset`` <= the
    minutes elapsed since Monday 00:00 in the home's timezone. If no entry
    qualifies (first entry after Monday 00:00), the last slot of the week -
    Sunday's - is still active.
    """
    sched = selected_schedule(home)
    if not sched or not sched.get("timetable"):
        return None
    now_min = minutes_since_monday(now_utc, home["timezone"])
    slots = sorted(sched["timetable"], key=lambda s: s["m_offset"])
    active = None
    for slot in slots:
        if slot["m_offset"] <= now_min:
            active = slot
        else:
            break
    if active is None:
        active = slots[-1]  # wrap Sunday -> Monday

    zone = next((z for z in sched.get("zones", []) if z.get("id") == active["zone_id"]), None)
    if zone is None:
        return None
    for room in zone.get("rooms", []):
        if str(room.get("id")) == room_id and room.get("therm_setpoint_temperature") is not None:
            return float(room["therm_setpoint_temperature"])
    # Older schedules expose per-room temperatures as rooms_temp[].
    for room in zone.get("rooms_temp", []):
        if str(room.get("room_id")) == room_id and room.get("temp") is not None:
            return float(room["temp"])
    return None


def find_room_status(homestatus: dict, room_id: str) -> dict:
    for room in homestatus.get("body", {}).get("home", {}).get("rooms", []):
        if str(room.get("id")) == room_id:
            return room
    raise NetatmoError(f"room {room_id} not found in homestatus")


def heating_overview(home: dict, homestatus: dict) -> dict:
    """Diagnostics: heat request of every room (by name) and boiler status of the thermostat(s)."""
    names = {str(r.get("id")): r.get("name") or str(r.get("id")) for r in home.get("rooms", [])}
    body = homestatus.get("body", {}).get("home", {})
    requests = {names.get(str(r.get("id")), str(r.get("id"))): r.get("heating_power_request")
                for r in body.get("rooms", []) if r.get("heating_power_request") is not None}
    boiler = [m.get("boiler_status") for m in body.get("modules", []) if "boiler_status" in m]
    return {"rooms_power_request": requests,
            "boiler_status": boiler[0] if len(boiler) == 1 else (boiler or None)}


HEATING_DEVICE_TYPES = ("NRV", "NATherm1", "OTM")  # valves and thermostats


def room_devices(home: dict, homestatus: dict) -> dict:
    """room_id -> [valve/thermostat status]. The API only exposes battery,
    signal and reachability per device; temperature and setpoint are per room."""
    meta = {m.get("id"): m for m in home.get("modules", [])}
    status = {m.get("id"): m for m in homestatus.get("body", {}).get("home", {}).get("modules", [])}
    out: dict = {}
    for room in home.get("rooms", []):
        devices = []
        for mid in room.get("module_ids", []):
            m = {**meta.get(mid, {}), **status.get(mid, {})}
            if m.get("type") not in HEATING_DEVICE_TYPES:
                continue
            devices.append({
                "id": mid,
                "type": m.get("type"),
                "name": meta.get(mid, {}).get("name") or None,
                "reachable": m.get("reachable"),
                "battery_state": m.get("battery_state"),
                "battery_level": m.get("battery_level"),
                "rf_strength": m.get("rf_strength"),
            })
        if devices:
            out[str(room.get("id"))] = devices
    return out


def rooms_overview(home: dict, homestatus: dict, own_room_id: str) -> list:
    """Diagnostics: temperature, setpoint and mode of every heated room, plus its devices."""
    names = {str(r.get("id")): r.get("name") for r in home.get("rooms", [])}
    devices = room_devices(home, homestatus)
    rooms = []
    for r in homestatus.get("body", {}).get("home", {}).get("rooms", []):
        rid = str(r.get("id"))
        if r.get("therm_measured_temperature") is None and r.get("therm_setpoint_temperature") is None:
            continue  # e.g. a room holding only the outdoor module
        rooms.append({
            "id": rid,
            "name": names.get(rid) or rid,
            "t": r.get("therm_measured_temperature"),
            "sp": r.get("therm_setpoint_temperature"),
            "mode": r.get("therm_setpoint_mode"),
            "end_time": r.get("therm_setpoint_end_time"),
            "power": r.get("heating_power_request"),
            "reachable": r.get("reachable"),
            "open_window": r.get("open_window"),
            "compensated": rid == own_room_id,
            "devices": devices.get(rid, []),
        })
    return rooms


def schedule_summary(home: dict) -> Optional[dict]:
    """The active heating schedule in a dashboard-friendly shape (room names instead of ids)."""
    sched = selected_schedule(home)
    if not sched:
        return None
    names = {str(r.get("id")): r.get("name") or str(r.get("id")) for r in home.get("rooms", [])}
    zones = []
    for z in sched.get("zones", []):
        temps = {}
        for r in z.get("rooms", []):
            if r.get("therm_setpoint_temperature") is not None:
                temps[names.get(str(r.get("id")), str(r.get("id")))] = r["therm_setpoint_temperature"]
        for r in z.get("rooms_temp", []):
            if r.get("temp") is not None:
                temps.setdefault(names.get(str(r.get("room_id")), str(r.get("room_id"))), r["temp"])
        zones.append({"id": z.get("id"), "name": z.get("name"), "type": z.get("type"), "rooms": temps})
    return {
        "id": sched.get("id"),
        "name": sched.get("name"),
        "timezone": home.get("timezone"),
        "hg_temp": sched.get("hg_temp"),
        "away_temp": sched.get("away_temp"),
        "zones": zones,
        "timetable": sorted(({"zone_id": t.get("zone_id"), "m_offset": t.get("m_offset")}
                             for t in sched.get("timetable", [])), key=lambda t: t["m_offset"]),
    }


def reference_reading(stations: dict, device_id: str, module_id: str) -> tuple:
    """Return (temperature, time_utc) for the station or the indoor module, or (None, None)."""
    for dev in stations.get("body", {}).get("devices", []):
        if dev.get("_id") != device_id:
            continue
        source = dev
        if module_id:
            source = next((m for m in dev.get("modules", []) if m.get("_id") == module_id), None)
            if source is None:
                return None, None
        dash = source.get("dashboard_data") or {}
        return dash.get("Temperature"), dash.get("time_utc")
    return None, None


def is_our_manual(room: dict, state: dict) -> bool:
    if room.get("therm_setpoint_mode") != "manual":
        return False
    last_sp = state.get("last_setpoint")
    last_end = state.get("last_endtime")
    if last_sp is None or last_end is None:
        return False
    sp = room.get("therm_setpoint_temperature")
    end = room.get("therm_setpoint_end_time")
    if sp is None or end is None:
        return False
    return (abs(float(sp) - float(last_sp)) <= SP_MATCH_TOLERANCE
            and abs(int(end) - int(last_end)) <= ENDTIME_MATCH_TOLERANCE_S)


@dataclass
class Decision:
    action: str  # "write" | "release" | "skip"
    reason: str = ""
    new_sp: Optional[float] = None
    endtime: Optional[int] = None

    @property
    def label(self) -> str:
        return f"skip:{self.reason}" if self.action == "skip" else self.action


def decide(cfg: Config, *, home_mode: Optional[str], room: dict, state: dict,
           target: Optional[float], t_module: Optional[float], module_time: Optional[int],
           now_ts: int, log: dict) -> Decision:
    """Pure decision function. Fills ``log`` with the computed values."""
    t_valve = room.get("therm_measured_temperature")
    room_mode = room.get("therm_setpoint_mode")
    current_sp = room.get("therm_setpoint_temperature")
    log.update(t_valve=t_valve, t_module=t_module, room_mode=room_mode,
               current_sp=current_sp, target=target, home_mode=home_mode)

    if home_mode != "schedule":
        return Decision("skip", f"home_mode_{home_mode}")

    if t_module is None or module_time is None:
        return Decision("skip", "module_missing")
    age_min = (now_ts - int(module_time)) / 60.0
    log["module_age_min"] = round(age_min, 1)
    if age_min > cfg.max_sensor_age_minutes:
        return Decision("skip", "module_stale")

    if t_valve is None or room.get("reachable") is False:
        return Decision("skip", "valve_unreachable")

    ours = is_our_manual(room, state)
    log["ours"] = ours
    if room_mode == "manual" and not ours:
        return Decision("skip", "user_override")
    if room_mode not in ("manual", "schedule", "home"):
        # max (boost), off, hg, away at room level: someone else's choice.
        return Decision("skip", f"room_mode_{room_mode}")

    if target is None:
        return Decision("skip", "no_scheduled_setpoint")

    offset = clamp(float(t_valve) - float(t_module), cfg.min_offset, cfg.max_offset)
    log["offset"] = round(offset, 2)
    new_sp = clamp(round_half(target + offset), SP_MIN, SP_MAX)
    log["new_sp"] = new_sp

    if offset < RELEASE_OFFSET_THRESHOLD:
        if ours:
            return Decision("release", new_sp=None)
        # Room already follows the schedule: a manual at target+0 would be pointless.
        return Decision("skip", "offset_negligible", new_sp=new_sp)

    endtime = now_ts + cfg.manual_duration_minutes * 60
    if not ours:
        return Decision("write", new_sp=new_sp, endtime=endtime)
    if current_sp is None or abs(new_sp - float(current_sp)) >= cfg.hysteresis:
        return Decision("write", new_sp=new_sp, endtime=endtime)
    remaining_s = int(room.get("therm_setpoint_end_time") or 0) - now_ts
    log["endtime_remaining_min"] = round(remaining_s / 60.0, 1)
    if remaining_s < cfg.renew_before_minutes * 60:
        return Decision("write", new_sp=new_sp, endtime=endtime)
    return Decision("skip", "within_hysteresis", new_sp=new_sp)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(cfg: Config, store: SsmStore, api: NetatmoClient, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    log: dict = {"dry_run": cfg.dry_run, "home_id": cfg.home_id, "room_id": cfg.room_id,
                 "ts": now_ts, "time_utc": now.isoformat()}

    # 1. Token refresh, then persist the rotated refresh token before anything else.
    client_id = store.get("client_id")
    client_secret = store.get("client_secret", decrypt=True)
    refresh_token = store.get("refresh_token", decrypt=True)
    tokens = api.refresh(client_id, client_secret, refresh_token)
    if tokens["refresh_token"] != refresh_token:
        store.put_refresh_token(tokens["refresh_token"])

    # 2. Reads.
    home = find_home(api.homesdata(cfg.home_id), cfg.home_id)
    status = api.homestatus(cfg.home_id)
    stations = api.getstationsdata(cfg.station_device_id)

    room = find_room_status(status, cfg.room_id)
    log.update(heating_overview(home, status))
    log["rooms"] = rooms_overview(home, status, cfg.room_id)
    log["schedule"] = schedule_summary(home)  # popped before logging, saved to S3 separately
    log["heating_power_request"] = room.get("heating_power_request")
    home_mode = home.get("therm_mode") or status.get("body", {}).get("home", {}).get("therm_mode")
    target = scheduled_setpoint(home, cfg.room_id, now)
    t_module, module_time = reference_reading(stations, cfg.station_device_id, cfg.indoor_module_id)
    state = store.get_state()

    decision = decide(cfg, home_mode=home_mode, room=room, state=state, target=target,
                      t_module=t_module, module_time=module_time, now_ts=now_ts, log=log)
    log["decision"] = decision.label
    log.setdefault("new_sp", decision.new_sp)

    # 3. Writes (skipped in dry run).
    if not cfg.dry_run:
        if decision.action == "write":
            api.set_room_manual(cfg.home_id, cfg.room_id, decision.new_sp, decision.endtime)
            store.put_state({"last_setpoint": decision.new_sp, "last_endtime": decision.endtime,
                             "last_write_utc": now.isoformat()})
        elif decision.action == "release":
            api.release_room(cfg.home_id, cfg.room_id)
            store.put_state({"last_setpoint": None, "last_endtime": None,
                             "last_write_utc": now.isoformat(), "last_action": "release"})
    if decision.endtime:
        log["endtime"] = decision.endtime
    return log


def _emit(record: dict) -> None:
    print(json.dumps(record, sort_keys=True, default=str))


# --------------------------------------------------------------------------- #
# Dashboard status (S3) - diagnostics only, never affects the valve logic
# --------------------------------------------------------------------------- #
HISTORY_KEEP_S = 24 * 3600
HISTORY_FIELDS = ("ts", "trigger", "decision", "t_valve", "t_module", "target", "offset",
                  "new_sp", "current_sp", "room_mode", "heating_power_request", "boiler_status")


class StatusStore:
    """Writes data/status.json (last run), data/history.json (last 24 h) and
    data/schedule.json (active heating schedule) to S3."""

    def __init__(self, s3: Any, bucket: str):
        self._s3 = s3
        self._bucket = bucket

    def _get_json(self, key: str, default):
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            if "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc):
                return default
            raise
        try:
            return json.loads(obj["Body"].read())
        except ValueError:
            return default

    def _put_json(self, key: str, data) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=key, ContentType="application/json",
                            CacheControl="no-store",
                            Body=json.dumps(data, sort_keys=True, default=str).encode())

    def save(self, record: dict, schedule: Optional[dict] = None) -> None:
        self._put_json("data/status.json", record)
        if schedule:
            self._put_json("data/schedule.json", dict(schedule, updated_ts=record.get("ts")))
        history = self._get_json("data/history.json", [])
        if not isinstance(history, list):
            history = []
        history.append({k: record.get(k) for k in HISTORY_FIELDS})
        cutoff = int(record.get("ts") or time.time()) - HISTORY_KEEP_S
        history = [h for h in history if isinstance(h, dict) and (h.get("ts") or 0) >= cutoff]
        self._put_json("data/history.json", history)


def _execute(trigger: str) -> tuple:
    """Run once. Returns (record, exception or None). Never raises."""
    import boto3  # imported lazily so unit tests do not need boto3

    cfg = Config.from_env()
    store = SsmStore(boto3.client("ssm"), cfg)
    error: Optional[Exception] = None
    try:
        record = run(cfg, store, NetatmoClient())
    except Exception as exc:
        # No write happened past the failing call; the manual setpoint (if any)
        # expires by itself.
        error = exc
        now = datetime.now(timezone.utc)
        record = {"decision": "error", "error_type": type(exc).__name__, "error": str(exc),
                  "dry_run": cfg.dry_run, "ts": int(now.timestamp()), "time_utc": now.isoformat()}
    record["trigger"] = trigger
    schedule = record.pop("schedule", None)
    _emit(record)

    bucket = os.environ.get("DASHBOARD_BUCKET", "")
    if bucket:
        try:
            StatusStore(boto3.client("s3"), bucket).save(record, schedule)
        except Exception as exc:  # the dashboard must never break the compensator
            _emit({"warning": "status_write_failed", "error_type": type(exc).__name__,
                   "error": str(exc)})
    return record, error


def _http_response(status: int, body: dict) -> dict:
    return {"statusCode": status,
            "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(body, sort_keys=True, default=str)}


def lambda_handler(event, context):
    # Function URL invocation (dashboard button, via CloudFront).
    if isinstance(event, dict) and "http" in (event.get("requestContext") or {}):
        method = event["requestContext"]["http"].get("method")
        path = event.get("rawPath", "")
        if path != "/api/run":
            return _http_response(404, {"error": "not found"})
        if method != "POST":
            return _http_response(405, {"error": "method not allowed"})
        record, error = _execute("manual")
        return _http_response(502 if error else 200, record)

    # Scheduled invocation: re-raise errors so the Errors metric / alarm fires.
    record, error = _execute("schedule")
    if error:
        raise error
    return record
