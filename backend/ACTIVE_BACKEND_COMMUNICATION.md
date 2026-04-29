# Active Backend Communication (HackRF + Evil Crow)

This document explains how the **currently active** backend (`backend/server.py`) communicates with:

- **HackRF One** (CLI tools)
- **Evil Crow RF2 / h-RAT** (HTTP + WebSocket)
- **ESP8266 web bridge** (JSON over serial)

It reflects the current implementation in:

- `backend/server.py`
- `backend/hackrf_controller.py`
- `backend/evil_crow_controller.py`

---

## 1) End-to-End Control Path

Command path from phone UI to radio hardware:

1. Phone browser sends HTTP request to ESP8266 (`/api`).
2. ESP8266 forwards JSON command over USB serial to laptop backend.
3. `server.py` reads one JSON line, dispatches by `cmd`.
4. Backend executes controller logic (HackRF or Evil Crow).
5. Backend sends JSON response over serial.
6. ESP8266 returns response to browser.

Serial request/response shape:

```json
{"cmd":"ecrf_replay","params":{"file":"name.iq"},"id":42}
```

```json
{"status":"ok","method":"hackrf_iq","file":"name.iq","id":42}
```

---

## 2) Serial Layer (ESP8266 <-> Python Backend)

In `server.py`:

- `connect()` opens `serial.Serial(port, baud, timeout=1)`.
- Main loop reads `readline()` from serial.
- `_handle_line()` parses JSON into:
  - `cmd`
  - `params`
  - optional request `id`
- `_dispatch()` returns a dict response.
- `_send_json()` writes `json.dumps(obj) + "\n"` back to serial.

Error behavior:

- Invalid JSON line -> warning + ignore.
- Dispatch exceptions -> `{"status":"error","error":"..."}`.
- `id` is echoed back when present.

---

## 3) Command Routing in `server.py`

### Core routing groups

- System: `status`, `check_devices`
- HackRF scan/capture: `scan`, `scan_sync`, `scan_stop`, `capture`, `transmit`
- Rollback workflow: `rollback_*`
- Evil Crow group: `ecrf_*`
- Signal processor group: `monitor_*`, `decode_file`

### Important current behavior

`server.py` currently sets:

- `self.replay_backend = "hackrf"`

So Replay-tab style commands (`ecrf_record`, `ecrf_stop_record`, `ecrf_save`, `ecrf_load`, `ecrf_replay`, `ecrf_signals`, `ecrf_configure`) are routed through the HackRF replay path in `server.py`.

Evil Crow controller remains available for:

- direct ECRF commands (when backend path chooses ecrf)
- jamming (`ecrf_jam`, `ecrf_jam_stop`)
- low-level HTTP/WS support in `evil_crow_controller.py`

---

## 4) Active HackRF Communication Details

Backend talks to HackRF via subprocess calls to HackRF CLI tools.

## 4.1 Scan path

`hackrf_controller.py` uses:

- `hackrf_info` for device presence check
- `hackrf_sweep` for band scanning

Scan results are parsed to detect peaks and SNR values.

## 4.2 Capture path

`capture_signal()` runs:

```bash
hackrf_transfer -r <file> -f <hz> -s <sample_rate> -l <lna> -g <vga> -n <samples>
```

Current replay-capture path in `server.py` uses conservative gains:

- `lna_gain=20`
- `vga_gain=20`

to reduce clipping in recorded IQ.

`capture_signal()` returns:

- `file` (path to `.iq`)
- `has_signal` (simple signal detection result)
- metadata (`size_bytes`, `duration_ms`, `modulation`)

## 4.3 Replay path

`transmit_iq()` runs:

```bash
hackrf_transfer -t <file> -f <hz> -s <sample_rate> -a 1 -x <tx_gain>
```

Notes:

- `-a 1` enables RF amp.
- `-x` controls TX gain (0-47).

In active replay flow (`_transmit_hackrf_replay()` in `server.py`):

- backend builds/uses a short replay clip from source IQ (`_make_simple_replay_clip`)
- repeats transmission 3 times with short gap
- returns `tx_file` used for actual TX

---

## 5) Active Replay API Behavior (`ecrf_*` commands with HackRF backend)

Because `replay_backend == "hackrf"`, these commands behave as follows:

- `ecrf_configure`:
  - stores replay frequency/mod/preset fields in backend state
  - returns `{..., "backend":"hackrf"}`

- `ecrf_record`:
  - starts background HackRF IQ capture thread
  - returns `recording: true`

- `ecrf_stop_record`:
  - waits for capture thread result
  - stores `replay_last_capture_path`
  - returns `signal_captured`, `file`, `backend`

- `ecrf_save`:
  - saves a replay-ready `.iq` using user-provided filename
  - current rule: user name preserved (adds `.iq` only if missing)

- `ecrf_signals`:
  - lists replay `.iq` files from `CAPTURES_DIR` (filtered)

- `ecrf_load`:
  - validates file exists and selects active replay file

- `ecrf_replay`:
  - resolves target file
  - transmits using `_transmit_hackrf_replay()` (short clip + repeats)
  - returns `method: "hackrf_iq"`, `tx_file`, `repeats`

---

## 6) Evil Crow Communication Details (HTTP + WebSocket)

When backend uses Evil Crow path, communication is through h-RAT endpoints:

## 6.1 HTTP helpers

`evil_crow_controller.py`:

- `_get(path, timeout)` -> `urllib.request.urlopen`
- `_post(path, data, headers, timeout)`
- `_apply(key, value)` -> GET `/APPLY?P1=<key>&P2=<value>`

Used for:

- frequency/modulation/preset changes
- recording control
- raw send paths
- file-based TXSD fallbacks

## 6.2 WebSocket listener

`connect_websocket()` creates `WebSocketApp(ws://<host>/ws)` with callbacks:

- `on_message`:
  - parses `TYPE->payload`
  - handles `SIGNAL`, `TXRAW/RAW`, `SUCCESS`, `ERROR/WARNING/INFO`
  - updates `captured_signal`
  - emits backend events

- `on_close`:
  - auto-reconnect (unless explicit shutdown flag set)

## 6.3 Evil Crow replay/save internals

Main methods:

- `start_record()`
- `stop_record()`
- `replay_last()`
- `save_current_signal()`
- `send_raw()`

These include multiple h-RAT compatibility fallbacks:

- `/SENDRAW`
- `/SAVESIGNAL` + `/CURRENT` + `/TXSD`
- save `.rawdata` + `.sub` companions

---

## 7) Events Back to UI

Controllers call `_emit(event_type, data)`.

`server.py` forwards those as serial JSON events:

```json
{"event":"scan_progress","data":{...}}
```

ESP8266 forwards event updates to UI (SSE pattern in firmware).

---

## 8) Current Practical Summary

- **Transport to backend:** ESP8266 serial JSON.
- **Active replay path:** HackRF-based (`replay_backend = "hackrf"`).
- **HackRF control:** subprocess to `hackrf_sweep` / `hackrf_transfer`.
- **Evil Crow control:** HTTP + WebSocket support remains implemented and usable where routed.
- **Primary reliability strategy:** short replay clips, repeated TX, reduced capture gain, RF amp enabled on TX.

