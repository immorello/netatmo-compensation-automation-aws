# Netatmo TRV Compensator — specifica per l'implementazione

## Obiettivo
Una valvola termostatica Netatmo (NRV) è montata in un punto caldo (nicchia sotto il davanzale, accanto a un termosifone in ghisa) e misura una temperatura più alta di quella reale della stanza. Nella stessa stanza c'è il modulo interno di una stazione meteo Netatmo, che misura la temperatura corretta.

Serve una funzione serverless che ogni 10 minuti:
1. legge la temperatura della valvola e quella del modulo;
2. calcola lo scarto;
3. imposta sulla valvola un setpoint compensato, così che la stanza raggiunga la temperatura del programma Netatmo misurata dal modulo.

Se la funzione si ferma, la stanza deve tornare da sola al programma Netatmo standard. Questo è il requisito di sicurezza principale.

## Deliverable
- `template.yaml` (AWS SAM)
- `src/app.py`: handler Lambda in Python 3.12. Solo stdlib (`urllib.request`, `json`, `zoneinfo`) e `boto3` (già incluso nel runtime). Nessuna dipendenza esterna.
- `tests/`: unit test pytest con fixture JSON delle risposte API
- `scripts/bootstrap_tokens.sh`: crea i parametri SSM iniziali
- `README.md`: setup OAuth, bootstrap, `sam build && sam deploy --guided`, dry run, rollback

## Architettura
- **EventBridge Scheduler** (`AWS::Scheduler::Schedule`), `rate(10 minutes)`, target la Lambda. Il ruolo IAM dello scheduler deve permettere `lambda:InvokeFunction`.
- **Lambda**: arm64, 256 MB, timeout 30 s, **fuori dalla VPC** (deve raggiungere `api.netatmo.com` senza NAT). Reserved concurrency = 1, per evitare esecuzioni parallele che si contendono il refresh token.
- **SSM Parameter Store**, prefisso configurabile (default `/netatmo-trv`):
  - `/netatmo-trv/client_id`: String
  - `/netatmo-trv/client_secret`: SecureString
  - `/netatmo-trv/refresh_token`: SecureString, **ruota a ogni refresh**
  - `/netatmo-trv/state`: String JSON, stato della funzione (vedi sotto)
- **IMPORTANTE:** CloudFormation non può creare parametri SecureString. Il template **non** deve crearli. Li crea `scripts/bootstrap_tokens.sh` con `aws ssm put-parameter --type SecureString --overwrite`. Il template può creare solo `/state` come String, oppure lo crea la Lambda al primo giro.
- Usare la chiave KMS di default `aws/ssm`, senza CMK.
- IAM della Lambda (least privilege): `ssm:GetParameter` e `ssm:PutParameter` solo su `arn:aws:ssm:${Region}:${Account}:parameter/netatmo-trv/*`, oltre ai permessi standard di logging.
- **Allarme CloudWatch** su `Errors >= 1` per 3 periodi consecutivi da 10 minuti, con notifica su un topic SNS e sottoscrizione email. L'email è un parametro opzionale: se è vuoto, allarme e SNS non vengono creati (Condition).

## Parametri del template
| Nome | Default | Note |
|---|---|---|
| `HomeId` | — | da `homesdata` |
| `RoomId` | — | stanza della valvola |
| `StationDeviceId` | — | MAC della stazione principale (`70:ee:50:...`) |
| `IndoorModuleId` | `""` | se valorizzato, usa questo modulo aggiuntivo (NAModule4); se vuoto, usa la temperatura della stazione principale (NAMain) |
| `MaxOffset` | `3.0` | correzione massima in °C |
| `MinOffset` | `0.0` | non correggere mai al ribasso |
| `Hysteresis` | `0.5` | variazione minima del setpoint per giustificare una scrittura |
| `ManualDurationMinutes` | `40` | durata del setpoint manuale (dead-man switch) |
| `RenewBeforeMinutes` | `15` | rinnova l'`endtime` quando mancano meno di N minuti |
| `MaxSensorAgeMinutes` | `30` | oltre questa età il dato del modulo è considerato vecchio |
| `DryRun` | `true` | **default true**: logga le decisioni senza scrivere sulla valvola |
| `ParameterPrefix` | `/netatmo-trv` | |
| `AlarmEmail` | `""` | opzionale |

## API Netatmo
Base URL `https://api.netatmo.com`. Scope: `read_thermostat write_thermostat read_station`.

**Prima di scrivere il codice, verifica nomi di campi e parametri sulla documentazione ufficiale (dev.netatmo.com, sezioni Energy e Weather).** I nomi qui sotto sono quelli attesi, ma vanno confermati. Non inventare campi.

1. `POST /oauth2/token` con `grant_type=refresh_token`, `refresh_token`, `client_id`, `client_secret`. La risposta contiene un nuovo `access_token` e un **nuovo `refresh_token`**: salvalo in SSM **subito**, prima di qualsiasi altra chiamata. Se il salvataggio fallisce, solleva un'eccezione.
2. `GET /api/homesdata?home_id=...`: `timezone` della casa, programmi (`schedules[]` con `selected`, `timetable[]` con `zone_id` e `m_offset` in minuti da lunedì 00:00, `zones[]` con `rooms[].therm_setpoint_temperature`) e `therm_mode` della casa.
3. `GET /api/homestatus?home_id=...`: per la stanza `RoomId` legge `therm_measured_temperature`, `therm_setpoint_temperature`, `therm_setpoint_mode`, `therm_setpoint_end_time`.
4. `GET /api/getstationsdata?device_id=...`: `dashboard_data.Temperature` e `dashboard_data.time_utc` della stazione, oppure del modulo in `modules[]` con `_id == IndoorModuleId`.
5. `POST /api/setroomthermpoint` con `home_id`, `room_id`, `mode=manual`, `temp`, `endtime` (epoch). Per rilasciare la stanza: `mode=home`.

Usa l'header `Authorization: Bearer`, timeout HTTP di 10 s e un solo retry con backoff sugli errori 5xx o di rete.

## Logica per ogni esecuzione
1. Refresh del token e salvataggio immediato del nuovo refresh token.
2. Lettura di `homesdata`, `homestatus` e `getstationsdata`.
3. **Condizioni di uscita senza scrivere** (con log del motivo):
   - la modalità della casa non è `schedule` (away, frostguard, off);
   - il dato del modulo è più vecchio di `MaxSensorAgeMinutes`, oppure manca;
   - **override utente**: la stanza è in `manual` ma setpoint ed `endtime` non coincidono con l'ultima scrittura salvata in `/state`. Qualcuno l'ha regolata a mano o con l'app, quindi la si rispetta finché non scade.
4. `target` = setpoint del programma attivo per `RoomId` nell'istante corrente, calcolato nella timezone della casa. Va trovato lo slot della `timetable` con il più grande `m_offset` ≤ minuti trascorsi da lunedì 00:00, gestendo il passaggio dalla domenica al lunedì.
5. `offset = clamp(T_valvola − T_modulo, MinOffset, MaxOffset)`.
6. `new_sp = round_to_0.5(target + offset)`, poi limitato all'intervallo `[7, 30]`.
7. Decisione:
   - se `offset < 0.25` e la stanza è in un nostro manual: rilascia con `mode=home`;
   - altrimenti, se `|new_sp − setpoint_attuale| >= Hysteresis`, oppure la stanza non è in un nostro manual, oppure l'`endtime` scade entro `RenewBeforeMinutes`: scrive `manual`, `new_sp`, `endtime = now + ManualDurationMinutes`;
   - altrimenti non fa nulla.
8. Dopo ogni scrittura riuscita aggiorna `/state` con `{last_setpoint, last_endtime, last_write_utc}`.
9. Con `DryRun=true` esegue tutto tranne `setroomthermpoint` e la scrittura di `/state`, e logga la decisione.

Per qualsiasi errore delle API: logga, **non scrivere** e solleva l'eccezione, così che l'allarme scatti. Il setpoint manuale scade da solo e la stanza torna al programma.

## Logging
Un log JSON strutturato per ogni esecuzione con: `t_valve`, `t_module`, `module_age_min`, `target`, `offset`, `current_sp`, `room_mode`, `decision` (`write` | `release` | `skip:<motivo>`), `new_sp`, `dry_run`. **Non loggare mai token o secret.**

## Test (pytest, senza chiamate di rete)
- Calcolo del setpoint programmato: slot normale, inizio settimana, passaggio domenica → lunedì, timezone Europe/Rome con cambio ora legale.
- Clamp dell'offset e arrotondamento a 0,5.
- Riconoscimento dell'override utente rispetto a un manual scritto dalla funzione.
- Isteresi e rinnovo dell'`endtime`.
- Dato del modulo troppo vecchio → skip.
- Modalità della casa diversa da schedule → skip.
- Rotazione del refresh token: il nuovo token viene salvato prima delle altre chiamate (mock di SSM).

## Bootstrap OAuth (da documentare nel README)
1. Su dev.netatmo.com crea un'app per ottenere `client_id` e `client_secret`.
2. Genera il primo `refresh_token` con il *Token generator* della pagina dell'app, selezionando gli scope `read_thermostat`, `write_thermostat`, `read_station`.
3. Esegui `scripts/bootstrap_tokens.sh <client_id> <client_secret> <refresh_token> [prefix] [region]`.
4. `sam build && sam deploy --guided` con `DryRun=true`. Controlla i log per 1–2 giorni, poi rifai il deploy con `DryRun=false`.
5. Se il refresh token si invalida (errore `invalid_grant`), rigeneralo e ripeti il passo 3.

## Fuori scope
Più stanze, dashboard, persistenza dello storico, VPC, CMK KMS.
