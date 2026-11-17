# Netatmo TRV Compensator

Automatically corrects a **Netatmo smart radiator valve** (NRV) that reads the
wrong temperature because it is mounted in a hot spot, for example in a recess
under the windowsill next to a cast-iron radiator.

It uses a **Netatmo weather station** in the same room as the reference. Every
10 minutes it compares the two temperatures and sets a compensated setpoint on
the valve, so the room actually reaches the temperature of your Netatmo
schedule.

It runs on AWS as a serverless function (AWS SAM) and costs next to nothing.
An optional web dashboard shows the current status and the schedule.

---

## Contents

- [The problem](#the-problem)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Getting started, step by step](#getting-started-step-by-step)
- [Dashboard](#dashboard)
- [Day-to-day use](#day-to-day-use)
- [Configuration parameters](#configuration-parameters)
- [Costs](#costs)
- [Project structure](#project-structure)
- [Development and tests](#development-and-tests)
- [Limitations and technical notes](#limitations-and-technical-notes)

---

## The problem

A Netatmo valve regulates the radiator based on the temperature it measures
itself. When it is mounted close to a heat source:

1. the radiator heats up and the valve reads, say, 22 °C;
2. the room is actually at 20 °C, but the schedule asks for 21 °C;
3. the valve thinks the target is already reached and **closes too early**;
4. the room stays colder than scheduled.

The calibration in the Netatmo app ("True Temperature") applies a fixed offset.
Here, however, the offset varies a lot: it is large when the radiator is hot
and almost zero when it is cold. A dynamic correction is needed.

## How it works

Every 10 minutes the function:

1. reads from Netatmo the valve temperature, the weather station temperature
   and the active heating schedule;
2. computes the offset and the corrected setpoint:
   ```
   offset = clamp(T_valve − T_station, MinOffset, MaxOffset)    # default 0…3 °C
   new_sp = clamp(round_to_0.5(scheduled_target + offset), 7, 30)
   ```
3. sets a **manual** setpoint of `new_sp` on the valve, valid for **40 minutes**,
   and renews it on every run while it is needed;
4. puts the room back on the schedule once the offset disappears.

Example: the schedule asks for 20 °C, the valve reads 22.4 °C and the station
20.1 °C. The offset is 2.3, so the function sets 22.5 °C on the valve. The
valve keeps heating until the room, as measured by the station, really is
around 20 °C.

### Safety: it switches itself off

The manual setpoint always expires shortly. **If the function stops** (error,
expired token, suspended AWS account…), the valve goes back to the standard
Netatmo schedule on its own within 40 minutes. It is never left stuck on a
wrong value.

The function also **leaves the valve alone** when:
- the home is not in schedule mode (away, frost guard, off);
- you changed the temperature yourself from the app or on the valve: your manual setpoint is respected until it expires;
- the room is in boost mode;
- the weather station reading is stale (older than 30 minutes) or missing;
- any Netatmo API call fails.

### Architecture

```
EventBridge Scheduler (every 10 min) ──► Lambda ──► Netatmo API
                                           │
                      SSM Parameter Store ◄┤  credentials, refresh token (rotated every run), state
                                           │
                  S3 (optional) ◄──────────┘  status.json, history.json, schedule.json
                     ▲
CloudFront + login ──┘  dashboard  ── "Read now" ──► Lambda (Function URL)
```

- **Lambda** on Python 3.12, arm64, standard library + boto3 only, no external dependencies.
- **Concurrency 1**: two runs can never overlap and race for the refresh token.
- **Email alarm** (optional) if the function keeps failing for 30 minutes.

## Requirements

**Netatmo hardware**
- at least one Netatmo smart radiator valve (NRV), set up in the Netatmo Energy app;
- a Netatmo weather station (NAMain), or one of its additional indoor modules
  (NAModule4), **in the same room** as the valve.

**Accounts**
- an account on [dev.netatmo.com](https://dev.netatmo.com), using the same credentials as the Netatmo app;
- an AWS account.

**On your machine**
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), configured with a profile;
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html);
- Python 3.12 or later, `bash` and `curl`.

## Getting started, step by step

### 1. Create the Netatmo app and get the tokens

1. Go to <https://dev.netatmo.com>, then **My apps** → **Create**. Fill in the
   form: for personal use any values will do.
2. Write down the **client ID** and **client secret**.
3. On the app page, open the **Token generator**, select the scopes
   `read_thermostat`, `write_thermostat` and `read_station`, then generate the token.
4. Copy both the **access token** and the **refresh token**.

> The refresh token changes every time it is used. From now on, do not use it
> anywhere else (for example in the documentation's "Try it"), or the one you
> copied will no longer be valid.

### 2. Create the `.env` file

```bash
cp .env.example .env
```

Fill in `.env` with the values from step 1 (plus `ACCESS_TOKEN=...`, used only
by step 3) and with your AWS profile and region. The file is in `.gitignore`
and must never be published.

### 3. Find your home's IDs

```bash
scripts/find_ids.sh
```

The script only reads data and shows:
- `HomeId`: your home;
- `RoomId`: the rooms, with the devices assigned to each. Pick the room with the valve to compensate;
- `StationDeviceId`: the MAC address of the weather station (`70:ee:50:...`);
- `IndoorModuleId`: any additional indoor module. Leave it empty if the reference is the main station.

Run it **before** the first deploy: after the Lambda's first run, the access
token from the Token generator is no longer valid.

### 4. Upload the credentials to AWS

```bash
scripts/bootstrap_tokens.sh
```

The script reads the credentials and the AWS profile from `.env`, shows you the
target account and **asks for confirmation** before writing anything. It
creates in SSM Parameter Store:

| Parameter | Type |
|---|---|
| `/netatmo-trv/client_id` | String |
| `/netatmo-trv/client_secret` | SecureString (AWS managed key `aws/ssm`) |
| `/netatmo-trv/refresh_token` | SecureString. The Lambda rewrites it on every run |
| `/netatmo-trv/state` | String JSON: the function's last write |

The script creates these parameters instead of the template because
CloudFormation cannot create SecureString parameters.

### 5. Deploy in dry-run mode

The first time, use the guided deploy:

```bash
sam build
sam deploy --guided --profile <your-profile>
```

Answer as follows:
- **Stack Name:** `netatmo-trv-compensator`. **Region:** the same as in step 4.
- **HomeId, RoomId, StationDeviceId, IndoorModuleId:** the values from step 3.
- **DryRun:** keep `true`.
- **AlarmEmail:** your email address if you want to be notified of errors. Leave it empty to skip the alarm.
- **DashboardUser / DashboardPassword:** see [Dashboard](#dashboard). Leave the password empty to skip the dashboard.
- **Confirm changes / Allow IAM role creation:** `Y`. **Disable rollback:** `N`.
- **Save arguments to configuration file:** `Y`.

Alternatively, copy `samconfig.example.toml` to `samconfig.toml`, fill it in
and run `sam build && sam deploy`.

If you entered an email address, confirm the subscription from the AWS message
("Subscription Confirmation").

### 6. Check the dry run for 1–2 days

In **dry run** the function does everything except write to the valve: it
reads, computes, decides and logs the decision.

```bash
sam logs --stack-name netatmo-trv-compensator --tail --profile <your-profile>
```

Every 10 minutes a line like this appears:

```json
{"decision": "write", "dry_run": true, "t_valve": 22.4, "t_module": 20.1,
 "offset": 2.3, "target": 20.0, "current_sp": 20, "new_sp": 22.5,
 "module_age_min": 4.0, "room_mode": "schedule", "heating_power_request": 0, ...}
```

What to check:
- `target` must match the scheduled temperature at that time;
- `t_module` and `t_valve` must match what you see in the Netatmo apps;
- when the radiator is hot, `offset` should rise and `decision` should switch to `write`;
- there must be no lines with `"decision": "error"`.

If you enabled the dashboard, it is easier to check there.

### 7. Go live

In `samconfig.toml` change `DryRun=true` to `DryRun=false`, then:

```bash
sam build && sam deploy
```

From now on the function really writes to the valve. In the Netatmo app you
will see the room in "manual" mode with a temperature slightly above the
scheduled one: that is the expected behaviour.

## Dashboard

A private website protected by username and password, reachable at an address
like `https://xxxx.cloudfront.net/`. It has two pages. The user interface is
in Italian.

**Status** (*Stato*)
- valve and station temperature, offset, target, current and computed setpoint;
- the latest decision, explained in plain words;
- all rooms: temperature, setpoint, mode and heat request. Under each room, its
  valves and thermostats with battery, signal strength and reachability;
- whether the boiler is on;
- a chart of the last 24 hours;
- the **Read now** button (*Leggi ora*), which immediately runs a full cycle,
  for example to compare the values with the app.

**Schedule** (*Programma*)
- the current time slot and its temperature;
- the table of slots (Comfort, Night, Eco…) with the temperature for each room;
- the week split into slots, day by day.

**Setup**
1. During `sam deploy --guided`, set **DashboardUser** and
   **DashboardPassword** (at least 12 characters, no `:`). They are `NoEcho`
   parameters, so they are not saved to `samconfig.toml`.
2. Upload the site:
   ```bash
   scripts/deploy_dashboard.sh
   ```
   The script prints the dashboard URL. Run it again whenever you change the
   files in `dashboard/`.

**Notes**
- The **Read now** button runs the same logic as the scheduled run, dry run
  included. If a run is already in progress, the page asks you to try again in
  a few seconds.
- The username and password are stored, encoded, in the CloudFront Function's
  code. Anyone with access to your AWS account can read them (CloudFront →
  Functions → `<stack>-basic-auth`). Use a dedicated password.
- A dashboard problem never stops the compensation.

## Day-to-day use

**Changing the temperature from the app.** Go ahead as usual: the function
notices that the manual setpoint is not its own and respects it until it
expires. The same goes for boost, away and frost guard.

**Changing the schedule.** Edit it in the Netatmo app as usual: it is read
again on every run.

**Turning the compensation off:**

| What you want | How |
|---|---|
| Go back to observing without acting | `DryRun=true` in `samconfig.toml`, then `sam deploy` |
| Stop it right away | `aws lambda put-function-concurrency --function-name <name> --reserved-concurrent-executions 0` (the next `sam deploy` re-enables it) |
| Remove everything | `sam delete`, then delete the `/netatmo-trv/*` parameters from SSM |

In every case the valve goes back to the schedule within 40 minutes. To make it
go back immediately, set the room back to "schedule" in the app.

**Expired token (`InvalidGrantError` in the logs or an alarm email).** The
refresh token is no longer valid, for example because it was revoked or the
Netatmo app was modified. Generate new tokens with the Token generator, update
`.env` and run `scripts/bootstrap_tokens.sh` again. The function recovers by
itself on the next run.

**Meaning of `decision` in the logs:**

| Value | Meaning |
|---|---|
| `write` | sets (or renews) the compensated setpoint |
| `release` | the offset is gone: puts the room back on the schedule |
| `skip:offset_negligible` | no correction needed |
| `skip:within_hysteresis` | the current setpoint is already right |
| `skip:user_override` | there is a manual setpoint set by you: it is respected |
| `skip:home_mode_<m>` | the home is in away, frost guard or off mode |
| `skip:room_mode_<m>` | the room is in boost or another mode |
| `skip:module_stale` / `skip:module_missing` | the station reading is stale or missing |
| `skip:valve_unreachable` | the valve is not responding |
| `skip:no_scheduled_setpoint` | the schedule has no temperature for the room |
| `error` | error: nothing is written and the alarm counts the failure |

## Configuration parameters

Set them in `samconfig.toml` (`parameter_overrides`) or during the guided deploy.

| Parameter | Default | Description |
|---|---|---|
| `HomeId` | — | home ID |
| `RoomId` | — | room of the valve to compensate |
| `StationDeviceId` | — | MAC address of the main weather station |
| `IndoorModuleId` | empty | additional indoor module (NAModule4); empty = main station |
| `MaxOffset` | `3.0` | maximum correction in °C |
| `MinOffset` | `0.0` | minimum correction; with 0 it never corrects downwards |
| `Hysteresis` | `0.5` | minimum setpoint change that justifies a new write |
| `ManualDurationMinutes` | `40` | duration of the manual setpoint, i.e. the maximum time before the fail-safe kicks in |
| `RenewBeforeMinutes` | `15` | renew the manual setpoint when fewer than N minutes are left |
| `MaxSensorAgeMinutes` | `30` | station readings older than this are ignored |
| `DryRun` | `true` | `true` = observe and log without writing to the valve |
| `ParameterPrefix` | `/netatmo-trv` | prefix of the SSM parameters |
| `AlarmEmail` | empty | email for the alarm; empty = no alarm |
| `DashboardUser` | `admin` | dashboard username |
| `DashboardPassword` | empty | dashboard password; empty = no dashboard |

## Costs

With the intended use (one run every 10 minutes, one person looking at the
dashboard) the cost is **a few cents per month**:

| Service | Approximate cost |
|---|---|
| Lambda, EventBridge Scheduler, SSM (standard parameters) | free tier |
| CloudWatch Logs (30-day retention), alarm, SNS | free tier / cents |
| S3 (about 13,000 writes per month for the dashboard) | less than €0.10 |
| CloudFront and the login function | free tier |

No custom domain is needed: the free `*.cloudfront.net` address is used.

## Project structure

```
├── template.yaml            # AWS SAM infrastructure (Lambda, scheduler, alarm, dashboard)
├── samconfig.example.toml   # example deploy configuration (copy to samconfig.toml)
├── .env.example             # example credentials and local settings (copy to .env)
├── src/app.py               # the function: Netatmo API, compensation logic, handler
├── dashboard/               # static site: index.html (Status), schedule.html (Schedule)
├── scripts/
│   ├── find_ids.sh          # finds the Netatmo IDs (read-only)
│   ├── bootstrap_tokens.sh  # uploads the credentials to SSM
│   └── deploy_dashboard.sh  # uploads the site to S3
├── tests/                   # pytest unit tests, with JSON fixtures of the Netatmo API
└── netatmo-trv-compensator-spec.md   # original specification (Italian)
```

`.env` and `samconfig.toml` contain credentials and personal data (home IDs,
email, AWS profile). They are in `.gitignore`: do not publish them.

## Development and tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests make no network calls and cover:
- the schedule slot computation: start of the week, Sunday → Monday wrap-around,
  daylight saving time changes in Europe/Rome;
- offset, rounding, hysteresis and expiry renewal;
- detection of manual setpoints set by the user;
- the skip conditions (home not in schedule mode, stale station reading…);
- refresh token rotation, with the new token saved before any other call;
- dry run, retries, no secrets in the logs, dashboard data and requests.

To validate the template: `sam validate --lint`.

## Limitations and technical notes

- **One room** per installation. To compensate several rooms, deploy one stack
  per room with different `ParameterPrefix` values.
- **Temperature and setpoint are per room, not per valve.** The Netatmo API
  does not expose the temperature of a single valve. In a room with two valves,
  both receive the same setpoint, computed on the room temperature.
- **Netatmo's anticipation is lost.** While the room is in the compensated
  manual mode, the valve does not pre-heat before a comfort slot. When the slot
  changes, the function catches up within 10 minutes.
- **Heat request and boiler.** By raising the setpoint, the compensated valve
  requests heat for longer. If the boiler is controlled by the Netatmo
  thermostat, it stays on longer, and rooms without a valve (for example the
  thermostat's room) get a little warmer.
- **Netatmo API.** Field names were checked against real responses. The active
  heating schedule (type `therm`) is used. Each call has a 10 s timeout and a
  single retry on 5xx and network errors.
- **Recognising "our" manual setpoint.** The function compares the room's
  setpoint and expiry with the last write saved in `/state` (tolerances
  ±0.05 °C and ±90 s).
- **Least privilege.** The Lambda can only read and write `/netatmo-trv/*` in
  SSM and `data/*` in the dashboard bucket. It uses the AWS managed KMS key
  `aws/ssm`, with no customer-managed keys.

## License

[MIT](LICENSE)
