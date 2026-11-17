#!/usr/bin/env bash
# Print the Netatmo ids needed by template.yaml (HomeId, RoomId, StationDeviceId,
# IndoorModuleId). Read-only: it only calls homesdata and getstationsdata.
#
# Usage: scripts/find_ids.sh
# Needs ACCESS_TOKEN in .env (or in the environment): the access token shown by
# the Token generator on dev.netatmo.com. It stops working once the Lambda has
# refreshed the tokens, so run this before the first deploy (or generate a new one).
set -euo pipefail

source "$(dirname "$0")/_env.sh"

if [[ -z "${ACCESS_TOKEN:-}" ]]; then
  echo "ACCESS_TOKEN is missing: set it in .env (Token generator on dev.netatmo.com)" >&2
  exit 1
fi

fetch() {
  curl -sS -m 15 -H "Authorization: Bearer $ACCESS_TOKEN" "https://api.netatmo.com/api/$1"
}

HOMES="$(fetch homesdata)"
STATIONS="$(fetch getstationsdata)"

HOMES="$HOMES" STATIONS="$STATIONS" python3 - <<'EOF'
import json, os, sys

homes = json.loads(os.environ["HOMES"])
stations = json.loads(os.environ["STATIONS"])
for name, d in (("homesdata", homes), ("getstationsdata", stations)):
    if "error" in d:
        sys.exit(f"{name}: {d['error']}")

print("== Energy (homesdata) ==")
for h in homes["body"]["homes"]:
    print(f"\nHomeId           {h['id']}   ({h.get('name')}, {h.get('timezone')}, mode={h.get('therm_mode')})")
    mods = {m["id"]: m for m in h.get("modules", [])}
    for r in h.get("rooms", []):
        devs = ", ".join(f"{mods.get(i, {}).get('type', '?')}" for i in r.get("module_ids", []))
        print(f"  RoomId         {r['id']:<12} {r.get('name') or '(senza nome)':<20} [{devs}]")
    for s in h.get("schedules", []):
        if s.get("type", "therm") == "therm":
            print(f"  Programma      {s.get('name')}{'  <- attivo' if s.get('selected') else ''}")

print("\n== Weather (getstationsdata) ==")
for dev in stations["body"]["devices"]:
    t = (dev.get("dashboard_data") or {}).get("Temperature")
    print(f"\nStationDeviceId  {dev['_id']}   ({dev.get('station_name') or dev.get('module_name')}, {t} °C)")
    for m in dev.get("modules", []):
        t = (m.get("dashboard_data") or {}).get("Temperature")
        hint = "  <- IndoorModuleId" if m.get("type") == "NAModule4" else ""
        print(f"  module         {m['_id']}   {m.get('type'):<10} {m.get('module_name')}, {t} °C{hint}")

print("\nRoomId: la stanza con la valvola (NRV) da compensare.")
print("IndoorModuleId: lascialo vuoto se il riferimento è la stazione principale (NAMain).")
EOF
