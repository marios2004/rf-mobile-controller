"""
Evil Crow RF2 Controller — h-RAT HTTP API wrapper
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import ECRF_HOST, MOD_CODES, PRESET_CODES, CAPTURES_DIR

try:
    import websocket as _ws_mod
    _HAS_WS = True
except ImportError:
    _HAS_WS = False


class EvilCrowController:
    """Evil Crow RF2 control via h-RAT HTTP + WebSocket API."""

    def __init__(self, host=None, on_event=None):
        self.host = host or ECRF_HOST
        self.base = f"http://{self.host}"
        self.on_event = on_event
        self.connected = False
        self.ws = None
        self._ws_thread = None
        self._lock = threading.Lock()

        self.captured_signal = None
        self.current_freq = 433.92
        self.current_mod = "OOK"
        self.current_preset = "AM650"
        self.active_signal_file = None
        self._capture_acknowledged = False
        self._ws_shutdown = False

        os.makedirs(CAPTURES_DIR, exist_ok=True)

    def _emit(self, event_type, data):
        if self.on_event:
            self.on_event(event_type, data)

    def _get(self, path, timeout=3):
        url = f"{self.base}{path}"
        try:
            resp = urllib.request.urlopen(url, timeout=timeout)
            body = resp.read().decode("utf-8", errors="replace").strip()
            return True, body
        except Exception as exc:
            return False, str(exc)

    def _post(self, path, data, timeout=30, headers=None):
        headers = headers or {}
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers=headers,
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            body = resp.read().decode("utf-8", errors="replace").strip()
            return True, body
        except Exception as exc:
            return False, str(exc)

    def _send_raw_via_file(self, cleaned_rawdata):
        """Fallback replay path: save raw signal to h-RAT SD slot then TXSD."""
        self._apply("MOD", MOD_CODES.get(self.current_mod.upper(), "2"))
        self._apply("PRESET", PRESET_CODES.get(self.current_preset.upper(), "2"))
        self._apply("TXRAD", "2")
        token = int(time.time())
        encoded_data = urllib.parse.quote(cleaned_rawdata)
        # Some h-RAT builds persist saved signal files with a strict extension.
        # Try both common names to find one that the firmware accepts.
        filename_candidates = [
            f"ecrf_tmp_{token}.sub",
            f"ecrf_tmp_{token}.raw",
            f"ecrf_tmp_{token}.rawdata",
        ]

        for filename in filename_candidates:
            print(f"[ECRF] send_raw: trying SAVESIGNAL+TXSD fallback filename={filename}")
            self._apply("REPEAT", "5")

            # Try POST first for long payloads to avoid URL size limits.
            save_data = urllib.parse.urlencode({
                "FILENAME": filename,
                "RAWDATA": cleaned_rawdata,
            }).encode()
            ok, details = self._post(
                "/SAVESIGNAL",
                save_data,
                timeout=45,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if not ok:
                print(f"[ECRF] send_raw: SAVESIGNAL POST failed for {filename}: {details}")

                # Fallback to original GET endpoint variant.
                ok_get, details_get = self._get(
                    f"/SAVESIGNAL?FILENAME={filename}&RAWDATA={encoded_data}",
                    timeout=45,
                )
                if not ok_get:
                    print(f"[ECRF] send_raw: SAVESIGNAL GET failed for {filename}: {details_get}")
                    continue
                ok = True
                details = details_get

            print(f"[ECRF] send_raw: SAVESIGNAL accepted for {filename}: {details[:120]}")

            ok_apply, details = self._apply("CURRENT", filename)
            if not ok_apply:
                print(f"[ECRF] send_raw: CURRENT failed for file {filename}: {details}")
                continue

            time.sleep(0.25)
            ok_tx, details = self._get("/TXSD", timeout=45)
            if not ok_tx:
                print(f"[ECRF] send_raw: TXSD failed for {filename}: {details}")
                continue

            print(f"[ECRF] send_raw: SAVESIGNAL+TXSD succeeded with {filename}: {details[:120]}")
            return True, "ok"

        return False, "fallback SAVESIGNAL+TXSD failed for all extensions"

    def _save_sub_signal(self, rawdata, base_name):
        """Save signal in Flipper Zero .sub format using the given base filename
        (without extension), so it pairs 1:1 with the .rawdata sibling.

        h-RAT's .sub parser fails on a single oversized RAW_Data line and on
        comma separators (Flipper's reference format uses spaces and wraps
        long captures across multiple RAW_Data lines, ~512 samples each).
        We also strip leading/trailing noise via _select_single_burst so the
        decoder isn't fed garbage that triggers "No sample were parsed".
        """
        freq_hz = int(float(self.current_freq) * 1_000_000)
        preset = f"FuriHalSubGhzPresetOok{self.current_preset.replace('AM', '')}Async"
        if "FSK" in self.current_preset.upper():
            preset = f"FuriHalSubGhzPreset{self.current_preset.replace(' ', '')}"

        # Drop pre/post noise so the parser sees a single clean burst.
        trimmed = self._select_single_burst(rawdata)
        try:
            samples = [int(x) for x in trimmed.strip().split()]
        except Exception:
            samples = [int(x) for x in rawdata.strip().split() if x.lstrip('-').isdigit()]

        # h-RAT TXSD parser is sensitive to very long RAW payloads. Keep one
        # representative frame window (roughly the size of known-good captures).
        if len(samples) > 1200:
            cut_idx = None
            scan_end = min(len(samples), 1600)
            for i in range(220, scan_end):
                if samples[i] < -8000:
                    cut_idx = i + 1
                    break
            if cut_idx is not None:
                samples = samples[:cut_idx]
            if len(samples) > 1200:
                samples = samples[:1200]

        filename = f"{base_name}.sub"
        path = os.path.join(CAPTURES_DIR, filename)
        chunk = 512  # Flipper RAW chunk size used by reference SubGhz tools.
        with open(path, "w") as f:
            f.write("Filetype: Flipper SubGhz RAW File\n")
            f.write("Version: 1\n")
            f.write(f"Frequency: {freq_hz}\n")
            f.write(f"Preset: {preset}\n")
            f.write("Protocol: RAW\n")
            for i in range(0, len(samples), chunk):
                f.write("RAW_Data: ")
                f.write(" ".join(str(x) for x in samples[i:i + chunk]))
                f.write("\n")
        return filename, path

    def _select_single_burst(self, rawdata):
        """Keep only the strongest valid burst to avoid sending noise tails."""
        try:
            values = [int(x) for x in rawdata.strip().split()]
        except:
            return rawdata

        if len(values) <= 20:
            return rawdata

        bursts = []
        start = None
        for idx, val in enumerate(values):
            abs_val = abs(val)
            # Accept normal pulse widths in either sign, but only accept long
            # inter-frame gaps as NEGATIVE timings (h-RAT RAW convention).
            is_valid = (50 <= abs_val <= 2000) or (val < 0 and 10000 <= abs_val <= 30000)
            if is_valid:
                if start is None:
                    start = idx
            else:
                if start is not None and (idx - start) > 20:
                    bursts.append((start, idx))
                start = None

        if start is not None and (len(values) - start) > 20:
            bursts.append((start, len(values)))

        if not bursts:
            return rawdata

        # Use the longest valid burst.
        burst_start, burst_end = max(bursts, key=lambda rng: rng[1] - rng[0])
        selected = values[burst_start:burst_end]
        print(f"[ECRF] send_raw: selecting single burst {len(values)} -> {len(selected)} values")
        return " ".join(str(x) for x in selected)

    def _play_signal_file(self, filename):
        """Replay a file using CURRENT + TXSD."""
        if not filename:
            return {"status": "error", "error": "No signal file to replay"}

        path = os.path.join(CAPTURES_DIR, filename)
        if not os.path.exists(path):
            return {"status": "error", "error": "Signal file not found"}

        play_file = filename
        if filename.endswith(".rawdata"):
            # h-RAT TXSD is more reliable with .sub files.
            # Keep rawdata as fallback for older firmware compatibility.
            candidate_sub = filename.rsplit(".rawdata", 1)[0] + ".sub"
            candidate_sub_path = os.path.join(CAPTURES_DIR, candidate_sub)
            if os.path.exists(candidate_sub_path):
                play_file = candidate_sub
                path = candidate_sub_path
                print(f"[ECRF] Replaying via sub companion file: {play_file}")
            else:
                print(f"[ECRF] No .sub companion for {filename}; using rawdata directly")

        self._apply("MOD", MOD_CODES.get(self.current_mod.upper(), "2"))
        self._apply("PRESET", PRESET_CODES.get(self.current_preset.upper(), "2"))
        self._apply("TXRAD", "2")

        ok, details = self._apply("CURRENT", play_file)
        if not ok:
            return {"status": "error", "error": f"CURRENT failed ({play_file}): {details}"}

        time.sleep(0.25)
        ok_tx, details = self._get("/TXSD", timeout=45)
        if not ok_tx:
            return {"status": "error", "error": f"TXSD failed ({play_file}): {details}"}

        return {"status": "ok", "method": "file_txsd", "file": play_file, "details": details}

    def _apply(self, key, value):
        return self._get(f"/APPLY?P1={key}&P2={value}")

    def check_connection(self):
        try:
            ok, resp = self._get("/", timeout=2)
            self.connected = ok
            if not ok:
                print(f"[ECRF] check_connection: failed - {resp}")
            return ok
        except Exception as e:
            print(f"[ECRF] check_connection exception: {e}")
            self.connected = False
            return False

    def connect_websocket(self):
        """Connect WebSocket for live signal capture."""
        if not _HAS_WS:
            return False
        self._ws_shutdown = False

        def on_message(ws, msg):
            preview = msg[:120].replace("\n", " ")
            print(f"[ECRF-WS] {preview} (len={len(msg)})")
            if "->" not in msg:
                return
            mtype, payload = msg.split("->", 1)

            if mtype == "SIGNAL":
                parts = payload.split("|")
                print(f"[ECRF-WS] SIGNAL parts: {len(parts)}")

                # h-RAT format: ID|Freq|Preset|RSSI|RAWDATA|Sample|BIN|...|FILENAME|...
                # parts[4] is the raw timing data (may start with '-' or positive number)
                rawdata = None
                if len(parts) >= 5 and parts[4]:
                    candidate = parts[4].strip()
                    # Raw timing data has many space-separated signed integers.
                    if len(candidate) > 30 and ' ' in candidate and '-' in candidate:
                        rawdata = candidate

                # Fallback: scan other parts for timing data.
                if not rawdata and len(parts) >= 5:
                    for part in parts[4:]:
                        if part and len(part) > 30 and ' ' in part and '-' in part:
                            digits_and_signs = sum(1 for c in part if c.isdigit() or c in ' -')
                            if digits_and_signs / max(len(part), 1) > 0.9:
                                rawdata = part.strip()
                                break
                
                if rawdata:
                    self.captured_signal = rawdata
                    freq = parts[1] if len(parts) > 1 else "?"
                    rssi = parts[3] if len(parts) > 3 else "?"
                    print(f"[ECRF-WS] Captured signal, len={len(self.captured_signal)}")

                    self._emit("ecrf_signal_captured", {
                        "rawdata": self.captured_signal,
                        "freq": freq,
                        "rssi": rssi,
                        "length": len(self.captured_signal),
                    })

                    self._save_signal(self.captured_signal)
                else:
                    print(f"[ECRF-WS] SIGNAL received but no raw timing data found")

            elif mtype == "TXRAW" or mtype == "RAW":
                # Raw timing data response
                if payload and len(payload) > 50 and '-' in payload[:20]:
                    print(f"[ECRF-WS] Got raw data via {mtype}, len={len(payload)}")
                    self.captured_signal = payload.strip()
                    self._save_signal(self.captured_signal)
                    self._emit("ecrf_signal_captured", {
                        "rawdata": self.captured_signal,
                        "freq": self.current_freq,
                        "length": len(self.captured_signal),
                    })

            elif mtype == "SUCCESS":
                self._emit("ecrf_status", {"type": mtype, "message": payload})
                # Mark that h-RAT acknowledged the capture; the SIGNAL frame
                # carrying RAW data will arrive on its own shortly.
                if "signal" in payload.lower() and "received" in payload.lower():
                    print("[ECRF-WS] h-RAT acknowledged capture; waiting for SIGNAL frame...")
                    self._capture_acknowledged = True
            
            elif mtype in ("ERROR", "WARNING", "INFO"):
                self._emit("ecrf_status", {"type": mtype, "message": payload})

        def on_error(ws, err):
            print(f"[ECRF-WS] Error: {err}")

        def on_close(ws, code, reason):
            print(f"[ECRF-WS] Closed: code={code}, reason={reason}")
            # Auto-reconnect after brief delay so a transient close (e.g. h-RAT
            # closing the socket on an unexpected frame) doesn't permanently
            # disable live SIGNAL capture.
            if not self._ws_shutdown:
                def _reconnect():
                    time.sleep(1.5)
                    if not self._ws_shutdown:
                        print("[ECRF-WS] Reconnecting...")
                        self.connect_websocket()
                threading.Thread(target=_reconnect, daemon=True).start()

        try:
            self.ws = _ws_mod.WebSocketApp(
                f"ws://{self.host}/ws",
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            self._ws_thread = threading.Thread(
                target=self.ws.run_forever,
                kwargs={"ping_interval": 30},
                daemon=True,
            )
            self._ws_thread.start()
            time.sleep(0.5)
            print("[OK] Evil Crow WebSocket connected")
            return True
        except Exception as e:
            print(f"[--] Evil Crow WebSocket failed: {e}")
            return False

    def close_websocket(self):
        self._ws_shutdown = True
        if self.ws:
            self.ws.close()

    def _clean_signal(self, rawdata):
        """Clean signal by extracting valid OOK timing bursts."""
        try:
            values = [int(x) for x in rawdata.strip().split()]
        except:
            return rawdata
        
        if len(values) < 20:
            return rawdata
        
        # Find bursts of valid OOK timing (values between -2000 and 2000 microseconds)
        # except for gaps which can be -20000 to -10000 (inter-burst gaps)
        cleaned = []
        in_burst = False
        burst_start = 0
        
        for i, val in enumerate(values):
            abs_val = abs(val)

            # Valid OOK timing: 50-2000us, or NEGATIVE long gap: 10000-25000us.
            # Positive large values are noise/artifacts for h-RAT parser.
            is_valid = (50 <= abs_val <= 2000) or (val < 0 and 10000 <= abs_val <= 25000)
            
            if is_valid:
                if not in_burst:
                    burst_start = i
                    in_burst = True
            else:
                if in_burst and i - burst_start > 20:
                    # Save this burst if it's long enough
                    cleaned.extend(values[burst_start:i])
                in_burst = False
        
        # Don't forget the last burst
        if in_burst and len(values) - burst_start > 20:
            cleaned.extend(values[burst_start:])
        
        if len(cleaned) > 50:
            # Add trailing 0 if not present
            if cleaned[-1] != 0:
                cleaned.append(0)
            print(f"[ECRF] Cleaned signal: {len(values)} -> {len(cleaned)} values")
            return ' '.join(str(x) for x in cleaned)
        
        return rawdata
    
    def _save_signal(self, rawdata):
        # Clean the signal first
        cleaned = self._clean_signal(rawdata)
        
        ts = time.strftime("%Y%m%d_%H%M%S")
        base_name = f"ecrf_{ts}_{self.current_freq}MHz"
        filename = f"{base_name}.rawdata"
        path = os.path.join(CAPTURES_DIR, filename)
        with open(path, "w") as f:
            f.write(f"# Evil Crow Capture\n")
            f.write(f"# Freq: {self.current_freq} MHz\n")
            f.write(f"# Modulation: {self.current_mod}\n")
            f.write(f"# Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(cleaned + "\n")
        
        # Also update captured_signal with cleaned version
        self.captured_signal = cleaned
        try:
            sub_name, _ = self._save_sub_signal(cleaned, base_name)
            self.active_signal_file = sub_name
            print(f"[ECRF] Saved .sub signal: {sub_name}")
        except Exception as exc:
            print(f"[ECRF] WARN: failed to save .sub signal: {exc}")

        return path

    def set_frequency(self, freq_mhz):
        ok, _ = self._apply("SETFREQ", str(freq_mhz))
        if ok:
            self.current_freq = freq_mhz
        self._emit("ecrf_freq_changed", {"freq_mhz": freq_mhz, "success": ok})
        return {"status": "ok" if ok else "error", "freq_mhz": freq_mhz}

    def set_modulation(self, mod):
        code = MOD_CODES.get(mod.upper(), "2")
        ok, _ = self._apply("MOD", code)
        if ok:
            self.current_mod = mod.upper()
        return {"status": "ok" if ok else "error", "modulation": mod}

    def set_preset(self, preset):
        code = PRESET_CODES.get(preset.upper(), "2")
        ok, _ = self._apply("PRESET", code)
        if ok:
            self.current_preset = preset.upper()
        return {"status": "ok" if ok else "error", "preset": preset}

    def start_record(self):
        if not self.connected and not self.check_connection():
            return {"status": "error", "error": "Evil Crow not reachable"}
        # Clear any stale capture so replay always targets the latest session.
        self.captured_signal = None
        self.active_signal_file = None
        self._capture_acknowledged = False
        ok, _ = self._apply("RECORD", "true")
        if ok:
            self._record_started_at = time.time()
            self._emit("ecrf_recording", {"active": True})
        return {"status": "ok" if ok else "error", "recording": ok}

    def stop_record(self):
        # Guarantee a minimum ~2s recording window so h-RAT actually has time
        # to listen for a burst before we ask it to stop. Without this, a fast
        # stop click produces SUCCESS but never a SIGNAL frame.
        started = getattr(self, "_record_started_at", None)
        if started is not None:
            elapsed = time.time() - started
            if elapsed < 2.0:
                wait = 2.0 - elapsed
                print(f"[ECRF] stop_record: enforcing {wait:.1f}s minimum record window")
                time.sleep(wait)

        ok, _ = self._apply("RECORD", "false")
        self._emit("ecrf_recording", {"active": False})

        # Keep any already-captured signal; give firmware/WS more time to settle.
        time.sleep(3)

        # Try to fetch raw data via HTTP first (this is the path that actually
        # works with h-RAT BETA_v3.3.1; the WS SIGNAL frame is unreliable).
        print("[ECRF] Fetching signal via HTTP (capture stopped)...")
        self._fetch_raw_http()

        # If HTTP didn't work, wait for WebSocket as a fallback.
        if not self.captured_signal:
            print("[ECRF] Waiting for WebSocket signal data...")
            for i in range(8):
                time.sleep(0.5)
                if self.captured_signal:
                    print(f"[ECRF] Signal captured via WebSocket")
                    break

        if not self.captured_signal:
            print(
                "[ECRF] WARNING: No signal captured by h-RAT capture path.\n"
                "[ECRF] If your Evil Crow is running custom firmware, it may not expose "
                "the h-RAT /RAW capture path. Use that firmware's native RAW_RX/RAW_TX flow."
            )
        
        return {"status": "ok" if ok else "error", "recording": False, 
                "signal_captured": self.captured_signal is not None}
    
    def _fetch_raw_http(self):
        """Try to fetch raw signal data via HTTP from h-RAT."""
        import re
        
        # Method 1: Try /RAW endpoint and parse HTML for textarea/input content
        try:
            ok, html = self._get("/RAW", timeout=5)
            if ok and html:
                print(f"[ECRF] Got /RAW page, len={len(html)}")
                
                # Look for raw data in textarea
                textarea_match = re.search(r'<textarea[^>]*>([^<]+)</textarea>', html, re.IGNORECASE)
                if textarea_match:
                    content = textarea_match.group(1).strip()
                    if len(content) > 50 and '-' in content[:30]:
                        print(f"[ECRF] Found raw data in textarea, len={len(content)}")
                        self.captured_signal = content
                        self._save_signal(self.captured_signal)
                        return True
                
                # Look for raw data in input value
                input_match = re.search(r'value=["\']([^"\']*-\d+[^"\']*)["\']', html)
                if input_match:
                    content = input_match.group(1).strip()
                    if len(content) > 50:
                        print(f"[ECRF] Found raw data in input, len={len(content)}")
                        self.captured_signal = content
                        self._save_signal(self.captured_signal)
                        return True
                
                # Look for timing pattern anywhere in HTML
                match = re.search(r'(-?\d+\s+){20,}', html)
                if match:
                    rawdata = match.group(0).strip()
                    if len(rawdata) > 100:
                        print(f"[ECRF] Found raw data pattern, len={len(rawdata)}")
                        self.captured_signal = rawdata
                        self._save_signal(self.captured_signal)
                        return True
                        
        except Exception as e:
            print(f"[ECRF] /RAW parse failed: {e}")
        
        # NOTE: Don't poke the WebSocket here. h-RAT closes the WS on
        # unrecognized text frames (e.g. "RAW"), which kills our live
        # SIGNAL listener for the rest of the session. We rely on the
        # WS push from h-RAT instead.
        return False
    
    

    def send_raw(self, rawdata):
        """Send raw timing data for transmission."""
        cleaned = self._select_single_burst(self._clean_signal(rawdata))
        raw_len = len(cleaned)
        print(f"[ECRF] send_raw: payload_len={raw_len} chars")

        # Keep h-RAT on TX module 2 for direct raw replay
        self._apply("TXRAD", "2")

        encoded = urllib.parse.quote(cleaned)
        url = f"/SENDRAW?P1={encoded}"
        ok = False

        # If payload is long, skip inline /SENDRAW and use SD+TXSD directly.
        if len(url) <= 8000 and len(cleaned) <= 3000:
            # Prefer GET first for parity with h-RAT UI behavior.
            # If URL is large, use POST to avoid URL length limits.
            print(f"[ECRF] send_raw: trying GET /SENDRAW with timeout=30s")
            ok, details = self._get(url, timeout=30)
            if not ok:
                print(f"[ECRF] send_raw GET failed: {details}")
        else:
            print("[ECRF] send_raw: long payload, using SAVESIGNAL+TXSD first")
            ok, details = self._send_raw_via_file(cleaned)
            if ok:
                return {
                    "status": "ok",
                    "length": raw_len,
                    "method": "sendraw_file",
                    "details": details,
                }

            # For short payloads only, keep a last-chance /SENDRAW POST attempt.
            if len(cleaned) <= 3000:
                print(f"[ECRF] send_raw: payload too long for GET, trying POST /SENDRAW")
                ok, details = self._post(
                    "/SENDRAW",
                    urllib.parse.urlencode({"P1": cleaned}).encode(),
                    timeout=45,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if not ok:
                    print(f"[ECRF] send_raw POST failed: {details}")

            if not ok and len(cleaned) <= 3000:
                # Fallback for firmwares that expect payload set through APPLY first.
                print("[ECRF] send_raw: trying APPLY+SENDRAW fallback")
                ok_apply, apply_details = self._post(
                    "/APPLY",
                    urllib.parse.urlencode({"P1": "RAWDATA", "P2": cleaned}).encode(),
                    timeout=45,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if not ok_apply:
                    print(f"[ECRF] send_raw APPLY fallback failed: {apply_details}")
                    if details:
                        details = apply_details
                else:
                    print("[ECRF] send_raw: raw data applied, now triggering SENDRAW")
                    ok, details = self._get("/SENDRAW", timeout=45)
                    if not ok:
                        print(f"[ECRF] send_raw fallback SENDRAW failed: {details}")
                    else:
                        print("[ECRF] send_raw: fallback SENDRAW succeeded")

        # Final fallback if inline path not attempted or failed.
        if not ok:
            print("[ECRF] send_raw: trying SAVESIGNAL+TXSD fallback")
            ok, details = self._send_raw_via_file(cleaned)
            if ok:
                print("[ECRF] send_raw: fallback via SAVESIGNAL+TXSD succeeded")
            else:
                print(f"[ECRF] send_raw: SAVESIGNAL+TXSD fallback failed: {details}")

        if not ok and len(cleaned) > 0:
            # As absolute last resort, try legacy rawTX command path.
            print("[ECRF] send_raw: trying legacy TX fallback")
            for cmd in ["TX", "TRANSMIT", "REPLAY", "SENDLAST"]:
                ok_tx, resp = self._apply(cmd, "1")
                if ok_tx and "error" not in (resp.lower() if resp else ""):
                    print(f"[ECRF] send_raw legacy {cmd} succeeded: {resp[:50]}")
                    return {"status": "ok", "length": raw_len, "method": f"legacy_{cmd}"}

        return {
            "status": "ok" if ok else "error",
            "length": raw_len,
            "method": "sendraw",
            **({"error": details} if not ok else {}),
        }

        self._emit("ecrf_transmitted", {"success": ok, "length": len(rawdata)})
        return {
            "status": "ok" if ok else "error",
            "length": raw_len,
            "method": "sendraw",
            **({"error": details} if not ok else {}),
        }

    def replay_last(self, filename=None):
        """Replay the last captured signal."""
        if filename:
            self.active_signal_file = filename

        if self.active_signal_file:
            print(f"[ECRF] Replaying loaded signal file: {self.active_signal_file}")
            file_result = self._play_signal_file(self.active_signal_file)
            if file_result.get("status") == "ok":
                self._emit("ecrf_transmitted", {"success": True, "method": "file_txsd"})
                return file_result
            print(f"[ECRF] File replay failed: {file_result.get('error')}")

        if not self.captured_signal:
            return {
                "status": "error",
                "error": (
                    "No signal loaded for replay.\n"
                    "If you are using custom Evil Crow firmware, this bridge cannot "
                    "replay h-RAT raw timing directly. Record via h-RAT RECORD/STOP or "
                    "use the firmware's native raw capture/replay path (RAW_RX / RAW_TX)."
                ),
            }
        # Prefer explicit payload replay; TX-only replay may not resend the last
        # captured timings after a load from file.
        print(f"[ECRF] Replaying from stored captured signal ({len(self.captured_signal)} chars)...")
        send_result = self.send_raw(self.captured_signal)
        if send_result.get("status") == "ok":
            self._emit("ecrf_transmitted", {"success": True, "method": "sendraw"})
            return {"status": "ok", "method": "sendraw"}

        # Fallback to firmware's legacy replay commands.
        for cmd in ["TX", "TRANSMIT", "REPLAY", "SENDLAST"]:
            print(f"[ECRF] Trying h-RAT {cmd} fallback...")
            ok, resp = self._apply(cmd, "1")
            if ok and "error" not in resp.lower():
                print(f"[ECRF] h-RAT {cmd} command succeeded: {resp[:50]}")
                self._emit("ecrf_transmitted", {"success": True, "method": f"hrat_{cmd}"})
                return {"status": "ok", "method": f"hrat_{cmd}"}
        
        # Fallback to sending stored signal
        return {"status": "error", "error": f"Replay failed: {send_result.get('error', 'unknown')}"}

    def configure_for_capture(self, freq_mhz, mod="OOK", preset="AM650"):
        """Configure Evil Crow for signal capture."""
        print(f"[ECRF] configure_for_capture: freq={freq_mhz}, mod={mod}, preset={preset}")
        if not self.check_connection():
            print(f"[ECRF] check_connection failed")
            return {"status": "error", "error": "Evil Crow not reachable"}
        
        r1 = self.set_frequency(freq_mhz)
        if r1.get("status") != "ok":
            return {"status": "error", "error": "Failed to set frequency"}
        
        r2 = self.set_modulation(mod)
        if r2.get("status") != "ok":
            return {"status": "error", "error": "Failed to set modulation"}
        
        r3 = self.set_preset(preset)
        if r3.get("status") != "ok":
            return {"status": "error", "error": "Failed to set preset"}
        
        self._apply("RXRAD", "2")
        self._apply("TXRAD", "2")

        return {"status": "ok", "freq": freq_mhz, "mod": mod, "preset": preset}

    def start_jammer(self, freq_mhz, power="12"):
        """Start jamming on specified frequency using HTTP API."""
        print(f"[ECRF] Starting jammer at {freq_mhz} MHz")
        
        # Set frequency for module 1 (h-RAT expects MHz)
        ok1, r1 = self._apply("JAMFRQ1", str(freq_mhz))
        print(f"[ECRF] JAMFRQ1={freq_mhz}: {ok1}")
        time.sleep(0.2)
        
        # Set power for module 1
        ok2, r2 = self._apply("JAMPWR1", power)
        print(f"[ECRF] JAMPWR1={power}: {ok2}")
        time.sleep(0.2)
        
        # Start jammer
        ok3, r3 = self._apply("JAMMER", "true")
        print(f"[ECRF] JAMMER=true: {ok3}")
        
        if ok3:
            print("[ECRF] Jammer started successfully")
            return {"status": "ok", "freq": freq_mhz}
        else:
            print(f"[ECRF] Jammer failed: {r3}")
            return {"status": "error", "error": r3}

    def stop_jammer(self):
        """Stop jamming."""
        print("[ECRF] Stopping jammer")
        ok, resp = self._apply("JAMMER", "false")
        print(f"[ECRF] JAMMER=false: {ok}")
        return {"status": "ok" if ok else "error"}

    def get_status(self):
        return {
            "connected": self.connected,
            "freq_mhz": self.current_freq,
            "modulation": self.current_mod,
            "has_signal": self.captured_signal is not None,
            "signal_length": len(self.captured_signal) if self.captured_signal else 0,
        }

    def save_current_signal(self, name=""):
        """Save the current captured signal with optional custom name."""
        # Try to fetch signal if not already captured
        if not self.captured_signal:
            print("[ECRF] No signal in memory, trying to fetch from h-RAT...")
            self._fetch_raw_http()
        
        if not self.captured_signal:
            return {"status": "error", "error": "No signal to save"}
        
        ts = time.strftime("%Y%m%d_%H%M%S")
        
        if name:
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
            base_name = f"ecrf_{safe_name}_{ts}"
        else:
            base_name = f"ecrf_{ts}_{self.current_freq}MHz"

        filename = f"{base_name}.rawdata"
        path = os.path.join(CAPTURES_DIR, filename)
        
        # Clean the signal before saving
        cleaned = self._clean_signal(self.captured_signal)
        
        with open(path, "w") as f:
            f.write(f"# Evil Crow Capture\n")
            f.write(f"# Name: {name or 'Unnamed'}\n")
            f.write(f"# Freq: {self.current_freq} MHz\n")
            f.write(f"# Modulation: {self.current_mod}\n")
            f.write(f"# Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(cleaned + "\n")

        sub_filename = None
        try:
            sub_filename, _ = self._save_sub_signal(cleaned, base_name)
            self.active_signal_file = sub_filename
            print(f"[ECRF] Saved .sub companion: {sub_filename}")
        except Exception as exc:
            print(f"[ECRF] WARN: failed to save .sub companion: {exc}")

        return {"status": "ok", "filename": filename, "sub": sub_filename, "path": path}

    def list_signals(self, include_sub=False):
        """List Evil Crow captured signals.

        Args:
            include_sub: When True, include .sub files as well.
        """
        signals = []
        if include_sub:
            valid_extensions = (".rawdata", ".sub")
        else:
            valid_extensions = (".rawdata",)
            allowed_prefixes = ("ecrf_", "auto_")

        if os.path.exists(CAPTURES_DIR):
            for f in sorted(os.listdir(CAPTURES_DIR), reverse=True):
                if not f.endswith(valid_extensions):
                    continue
                if (not include_sub) and not f.startswith(allowed_prefixes):
                    continue
                
                path = os.path.join(CAPTURES_DIR, f)
                signals.append({
                    "name": f,
                    "path": path,
                    "size_bytes": os.path.getsize(path),
                    "created": time.ctime(os.path.getctime(path)),
                })
        return {"signals": signals}

    def list_all_signals(self):
        """List all captured signals (Evil Crow + Auto-capture)."""
        signals = []
        if os.path.exists(CAPTURES_DIR):
            for f in sorted(os.listdir(CAPTURES_DIR), reverse=True):
                if not f.endswith(".rawdata"):
                    continue
                
                path = os.path.join(CAPTURES_DIR, f)
                info = {
                    "name": f,
                    "path": path,
                    "size_bytes": os.path.getsize(path),
                    "created": time.ctime(os.path.getctime(path)),
                }
                
                # Parse file to get metadata
                try:
                    with open(path) as file:
                        for line in file:
                            line = line.strip()
                            if line.startswith("# Name:"):
                                info["display_name"] = line.replace("# Name:", "").strip()
                            elif line.startswith("# Freq:"):
                                info["freq"] = line.replace("# Freq:", "").replace("MHz", "").strip()
                            elif line.startswith("# Modulation:"):
                                info["mod"] = line.replace("# Modulation:", "").strip()
                            elif not line.startswith("#"):
                                break
                except Exception:
                    pass
                
                # Determine type
                if f.startswith("ecrf_"):
                    info["type"] = "Evil Crow"
                elif f.startswith("auto_"):
                    info["type"] = "Auto-capture"
                else:
                    info["type"] = "Other"
                
                # Create display name if not set
                if "display_name" not in info or info["display_name"] == "Unnamed":
                    if f.startswith("ecrf_"):
                        info["display_name"] = f.replace("ecrf_", "").replace(".rawdata", "")
                    elif f.startswith("auto_"):
                        info["display_name"] = f.replace("auto_", "").replace(".rawdata", "")
                    else:
                        info["display_name"] = f.replace(".rawdata", "")
                
                signals.append(info)
        
        return {"status": "ok", "signals": signals}

    def _extract_signal_from_sub(self, path):
        """Extract RAW_Data payload from a Flipper .sub file."""
        raw_parts = []
        try:
            with open(path) as file:
                for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("RAW_Data:"):
                        payload = line.split(":", 1)[1].strip()
                        if payload:
                            raw_parts.append(payload)
                        continue
                    if not line.startswith("#") and not raw_parts:
                        raw_parts.append(line)
        except Exception:
            return ""

        return " ".join(raw_parts).replace(",", " ")

    def load_signal(self, filename):
        """Load a signal from file."""
        path = os.path.join(CAPTURES_DIR, filename)
        if not os.path.exists(path):
            return {"error": "File not found"}

        if filename.endswith(".sub"):
            rawdata = self._extract_signal_from_sub(path)
        else:
            rawdata = ""
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    rawdata = line
                    break

        if rawdata:
            self.captured_signal = rawdata
            self.active_signal_file = filename
            return {"status": "ok", "length": len(rawdata)}
        return {"error": "No signal data in file"}
