#!/usr/bin/env python3
"""
RF Mobile Controller — Backend Server

Listens on serial port for commands from ESP8266.
Controls HackRF, Evil Crow RF2, and signal processing.

Serial Protocol (JSON):
    Request:  {"cmd": "scan", "params": {"band": "315", "duration": 10}}
    Response: {"status": "ok", "data": {...}}
    Event:    {"event": "scan_progress", "data": {...}}

Usage:
    python3 server.py
    python3 server.py --port /dev/ttyUSB1 --baud 115200
"""

import argparse
import json
import os
import signal
import shutil
import sys
import threading
import time

import numpy as np
import serial

from config import ESP_SERIAL_PORT, ESP_BAUD_RATE, CAPTURES_DIR, HACKRF_SAMPLE_RATE
from hackrf_controller import HackRFController, RollbackAttack
from evil_crow_controller import EvilCrowController
from signal_processor import SignalProcessor


class RFMobileServer:
    """Main server handling serial communication with ESP8266."""

    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self.ser = None
        self.running = True
        self._lock = threading.Lock()

        self.hackrf = HackRFController(on_event=self._on_event)
        self.rollback = RollbackAttack(self.hackrf, on_event=self._on_event)
        self.ecrf = EvilCrowController(on_event=self._on_event)
        self.processor = SignalProcessor(on_event=self._on_event)

        # Replay tab backend: use HackRF path for robust record/save/replay.
        self.replay_backend = "hackrf"
        self.replay_freq_mhz = 433.92
        self.replay_mod = "OOK"
        self.replay_preset = "AM650"
        self.replay_record_started_at = None
        self.replay_last_capture_path = None
        self.replay_active_file = None
        self.replay_capture_thread = None
        self.replay_capture_result = None

        os.makedirs(CAPTURES_DIR, exist_ok=True)

    def _run_hackrf_replay_capture(self, freq_mhz, duration, session_name):
        # Use conservative RX gains for replay capture to avoid IQ clipping.
        # Clipped captures often replay badly even when signal detection says True.
        self.replay_capture_result = self.hackrf.capture_signal(
            freq_mhz,
            duration,
            session_name=session_name,
            lna_gain=20,
            vga_gain=20,
        )

    def _make_simple_replay_clip(self, path, output_path=None):
        """Create a short first-burst IQ clip for simple replay attacks."""
        try:
            raw = np.fromfile(path, dtype=np.int8)
            if len(raw) < 20000:
                return path

            i_data = raw[0::2].astype(np.float32)
            q_data = raw[1::2].astype(np.float32)
            mag = np.sqrt(i_data**2 + q_data**2)

            block = 400  # 0.2ms at 2Msps
            n_blocks = len(mag) // block
            if n_blocks < 20:
                return path

            m = mag[:n_blocks * block].reshape(-1, block).mean(axis=1)
            noise = float(np.percentile(m, 30))
            peak = float(np.percentile(m, 99.7))
            thr = noise + (peak - noise) * 0.45
            idx = np.where(m > thr)[0]
            if len(idx) == 0:
                return path

            first_sample = int(idx[0] * block)
            pre = int(0.010 * HACKRF_SAMPLE_RATE)      # 10ms before
            clip_len = int(0.180 * HACKRF_SAMPLE_RATE) # 180ms clip
            start = max(0, first_sample - pre)
            end = min(len(mag), start + clip_len)
            if end - start < int(0.040 * HACKRF_SAMPLE_RATE):
                return path

            simple_path = output_path or path.replace(".iq", "_simple.iq")
            raw[start * 2:end * 2].tofile(simple_path)
            print(f"[REPLAY] simple clip: {os.path.basename(path)} -> {os.path.basename(simple_path)} ({end-start} samples)")
            return simple_path
        except Exception as exc:
            print(f"[REPLAY] simple clip failed: {exc}")
            return path

    def _transmit_hackrf_replay(self, path, repeats=3, gap_s=0.15):
        """Replay IQ using a trimmed burst file for faster/cleaner TX."""
        tx_path = path
        if path.endswith(".iq"):
            # If already a short clip, send as-is (avoid generating extra files).
            try:
                if os.path.getsize(path) <= 1_000_000:
                    tx_path = path
                else:
                    tx_path = self._make_simple_replay_clip(path)
            except Exception:
                tx_path = self._make_simple_replay_clip(path)

        last = None
        for i in range(max(1, repeats)):
            last = self.hackrf.transmit_iq(tx_path, self.replay_freq_mhz, 47)
            if last.get("status") != "ok":
                return last, tx_path
            if i < repeats - 1:
                time.sleep(gap_s)
        return {"status": "ok"}, tx_path

    def _on_event(self, event_type, data):
        """Forward events to ESP8266 for real-time updates."""
        self._send_event(event_type, data)

    def _send_event(self, event_type, data):
        """Send event to ESP8266."""
        msg = {"event": event_type, "data": data}
        self._send_json(msg)

    def _send_response(self, response):
        """Send response to ESP8266."""
        self._send_json(response)

    def _send_json(self, obj):
        """Send JSON message over serial."""
        try:
            with self._lock:
                if self.ser and self.ser.is_open:
                    line = json.dumps(obj) + "\n"
                    self.ser.write(line.encode("utf-8"))
                    self.ser.flush()
        except Exception as exc:
            print(f"[WARN] Serial write error: {exc}")

    def connect(self):
        """Connect to serial port."""
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=1)
            time.sleep(1)
            print(f"[OK] Serial connected: {self.port} @ {self.baud}")
            return True
        except serial.SerialException as exc:
            print(f"[ERR] Serial connection failed: {exc}")
            return False

    def run(self):
        """Main loop — read commands from serial, execute, respond."""
        print(f"""
{'=' * 60}
  RF MOBILE CONTROLLER — Backend Server
{'=' * 60}
  Serial Port : {self.port}
  Baud Rate   : {self.baud}
  Captures    : {CAPTURES_DIR}
{'=' * 60}
  Waiting for commands from ESP8266...
  Press Ctrl+C to stop.
{'=' * 60}
""")

        if not self.connect():
            return

        print("[INFO] Checking device connections...")
        if self.hackrf.check_connection():
            print("[OK] HackRF One connected")
        else:
            print("[--] HackRF One not detected")

        if self.ecrf.check_connection():
            print(f"[OK] Evil Crow RF2 connected ({self.ecrf.host})")
            self.ecrf.connect_websocket()
        else:
            print("[--] Evil Crow RF2 not reachable")
            print("     Connect laptop WiFi to: Evil Crow RF v2")

        print()

        while self.running:
            try:
                if self.ser.in_waiting > 0:
                    line = self.ser.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        self._handle_line(line)
            except serial.SerialException:
                print("[WARN] Serial disconnected, attempting reconnect...")
                time.sleep(2)
                self.connect()
            except Exception as exc:
                print(f"[ERR] {exc}")

            time.sleep(0.01)

        self.cleanup()

    def _handle_line(self, line):
        """Parse and execute command from ESP8266."""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"[WARN] Invalid JSON: {line}")
            return

        cmd = msg.get("cmd", "")
        params = msg.get("params", {})
        req_id = msg.get("id")

        print(f"[CMD] {cmd} {params}")

        try:
            result = self._dispatch(cmd, params)
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}

        if req_id:
            result["id"] = req_id

        if cmd != "status":
            print(f"[RSP] {result}")
        self._send_response(result)

    def _dispatch(self, cmd, params):
        """Dispatch command to appropriate handler."""

        # ── System ────────────────────────────────────────────
        if cmd == "status":
            return {
                "status": "ok",
                "hackrf_connected": self.hackrf.connected,
                "ecrf_connected": self.ecrf.connected,
                "ecrf_freq": self.ecrf.current_freq,
                "ecrf_mod": self.ecrf.current_mod,
                "monitoring": self.processor.monitoring,
                "scanning": self.hackrf.scanning,
            }

        elif cmd == "check_devices":
            hackrf_ok = self.hackrf.check_connection()
            ecrf_ok = self.ecrf.check_connection()
            return {
                "status": "ok",
                "hackrf": hackrf_ok,
                "ecrf": ecrf_ok,
            }

        # ── HackRF Scanning ───────────────────────────────────
        elif cmd == "scan":
            band = params.get("band", "433")
            duration = params.get("duration", 10)
            thread = threading.Thread(
                target=self.hackrf.scan_frequency,
                args=(band, duration),
                daemon=True,
            )
            thread.start()
            return {"status": "ok", "message": f"Scanning {band} MHz band..."}

        elif cmd == "scan_sync":
            band = params.get("band", "433")
            duration = params.get("duration", 10)
            print(f"[SCAN] Starting sync scan of {band} MHz for {duration}s...")
            result = self.hackrf.scan_frequency_sync(band, duration)
            print(f"[SCAN] Complete. Found {len(result.get('peaks', []))} peaks")
            return result

        elif cmd == "scan_stop":
            return self.hackrf.stop_scan()

        elif cmd == "get_peaks":
            return {"status": "ok", "peaks": self.hackrf.last_peaks}

        # ── HackRF Capture ────────────────────────────────────
        elif cmd == "capture":
            freq = params.get("freq", 433.92)
            duration = params.get("duration", 5)
            name = params.get("name")
            thread = threading.Thread(
                target=self.hackrf.capture_signal,
                args=(freq, duration, name),
                daemon=True,
            )
            thread.start()
            return {"status": "ok", "message": f"Capturing at {freq} MHz..."}

        elif cmd == "transmit":
            filename = params.get("file")
            freq = params.get("freq", 433.92)
            gain = params.get("gain", 47)
            return self.hackrf.transmit_iq(filename, freq, gain)

        elif cmd == "list_captures":
            return self.hackrf.list_captures()

        # ── Rollback Attack ───────────────────────────────────
        elif cmd == "rollback_start":
            freq = params.get("freq", 315.07)
            count = params.get("count", 3)
            return self.rollback.start_session(freq, count)

        elif cmd == "rollback_capture":
            index = params.get("index", 1)
            duration = params.get("duration", 4)
            thread = threading.Thread(
                target=self.rollback.capture_signal,
                args=(index, duration),
                daemon=True,
            )
            thread.start()
            return {"status": "ok", "message": f"Capturing signal {index}..."}

        elif cmd == "rollback_replay":
            session = params.get("session")
            delay = params.get("delay", 0.5)
            gain = params.get("gain", 47)
            thread = threading.Thread(
                target=self.rollback.replay_sequence,
                args=(session, delay, gain),
                daemon=True,
            )
            thread.start()
            return {"status": "ok", "message": "Replaying rollback sequence..."}

        elif cmd == "rollback_sessions":
            return self.rollback.list_sessions()

        elif cmd == "rollback_rename":
            session = params.get("session")
            name = params.get("name")
            return self.rollback.rename_session(session, name)

        # ── Evil Crow ─────────────────────────────────────────
        elif cmd == "ecrf_connect":
            ok = self.ecrf.check_connection()
            if ok:
                self.ecrf.connect_websocket()
            return {"status": "ok" if ok else "error", "connected": ok}

        elif cmd == "ecrf_status":
            return {"status": "ok", "data": self.ecrf.get_status()}

        elif cmd == "ecrf_freq":
            freq = params.get("freq", 433.92)
            return self.ecrf.set_frequency(freq)

        elif cmd == "ecrf_mod":
            mod = params.get("mod", "OOK")
            return self.ecrf.set_modulation(mod)

        elif cmd == "ecrf_preset":
            preset = params.get("preset", "AM650")
            return self.ecrf.set_preset(preset)

        elif cmd == "ecrf_record":
            if self.replay_backend == "hackrf":
                self.replay_record_started_at = time.time()
                self.replay_active_file = None
                self.replay_last_capture_path = None
                self.replay_capture_result = None
                duration = int(params.get("duration", 6))
                duration = max(2, min(20, duration))
                session_name = time.strftime("hackrf_replay_%Y%m%d_%H%M%S")
                self.replay_capture_thread = threading.Thread(
                    target=self._run_hackrf_replay_capture,
                    args=(self.replay_freq_mhz, duration, session_name),
                    daemon=True,
                )
                self.replay_capture_thread.start()
                return {"status": "ok", "recording": True, "backend": "hackrf"}
            return self.ecrf.start_record()

        elif cmd == "ecrf_stop_record":
            if self.replay_backend == "hackrf":
                if self.replay_capture_thread and self.replay_capture_thread.is_alive():
                    self.replay_capture_thread.join(timeout=30)
                result = self.replay_capture_result or {}
                if result.get("status") != "ok":
                    return {
                        "status": "error",
                        "recording": False,
                        "signal_captured": False,
                        "error": result.get("error", "HackRF capture failed"),
                        "backend": "hackrf",
                    }
                self.replay_last_capture_path = result.get("file")
                self.replay_active_file = os.path.basename(self.replay_last_capture_path)
                return {
                    "status": "ok",
                    "recording": False,
                    "signal_captured": bool(result.get("has_signal", False)),
                    "file": self.replay_active_file,
                    "duration_s": int(max(0, round(time.time() - (self.replay_record_started_at or time.time())))),
                    "backend": "hackrf",
                }
            return self.ecrf.stop_record()

        elif cmd == "ecrf_replay":
            filename = params.get("file")
            if self.replay_backend == "hackrf":
                target = filename or self.replay_active_file
                if not target and self.replay_last_capture_path:
                    target = os.path.basename(self.replay_last_capture_path)
                if not target:
                    return {"status": "error", "error": "No HackRF capture loaded for replay"}
                if target.endswith((".sub", ".rawdata")):
                    return {"status": "error", "error": "Selected file is not HackRF IQ (.iq)"}
                path = target if os.path.isabs(target) else os.path.join(CAPTURES_DIR, target)
                tx, used_path = self._transmit_hackrf_replay(path, repeats=3, gap_s=0.15)
                if tx.get("status") == "ok":
                    self.replay_active_file = os.path.basename(path)
                    return {
                        "status": "ok",
                        "method": "hackrf_iq",
                        "file": self.replay_active_file,
                        "tx_file": os.path.basename(used_path),
                        "repeats": 3,
                    }
                return {"status": "error", "error": tx.get("error", "HackRF replay failed")}
            return self.ecrf.replay_last(filename)

        elif cmd == "ecrf_send":
            rawdata = params.get("rawdata", "")
            return self.ecrf.send_raw(rawdata)

        elif cmd == "ecrf_configure":
            freq = params.get("freq", 433.92)
            mod = params.get("mod", "OOK")
            preset = params.get("preset", "AM650")
            if self.replay_backend == "hackrf":
                self.replay_freq_mhz = float(freq)
                self.replay_mod = mod
                self.replay_preset = preset
                return {
                    "status": "ok",
                    "freq": self.replay_freq_mhz,
                    "mod": self.replay_mod,
                    "preset": self.replay_preset,
                    "backend": "hackrf",
                }
            return self.ecrf.configure_for_capture(freq, mod, preset)

        elif cmd == "ecrf_jam":
            freq = params.get("freq", 433.92)
            power = params.get("power", "12")
            return self.ecrf.start_jammer(freq, power)

        elif cmd == "ecrf_jam_stop":
            return self.ecrf.stop_jammer()

        elif cmd == "ecrf_signals":
            include_sub = str(params.get("include_sub", "false")).lower() in ("1", "true", "yes", "on")
            if self.replay_backend == "hackrf":
                signals = []
                if os.path.exists(CAPTURES_DIR):
                    for f in sorted(os.listdir(CAPTURES_DIR), reverse=True):
                        if not f.endswith(".iq"):
                            continue
                        if not (f.startswith("hackrf_replay_") or f.startswith("ecrf_")):
                            continue
                        path = os.path.join(CAPTURES_DIR, f)
                        signals.append({
                            "name": f,
                            "path": path,
                            "size_bytes": os.path.getsize(path),
                            "created": time.ctime(os.path.getctime(path)),
                        })
                return {"signals": signals}
            return self.ecrf.list_signals(include_sub=include_sub)

        elif cmd == "ecrf_save":
            name = params.get("name", "")
            if self.replay_backend == "hackrf":
                if not self.replay_last_capture_path or not os.path.exists(self.replay_last_capture_path):
                    return {"status": "error", "error": "No HackRF capture to save"}

                # Save exactly what the user typed (no timestamp / no prefixes).
                requested = str(name or "").strip()
                if not requested:
                    return {"status": "error", "error": "Please enter a name"}
                # Keep basename only for safety, but preserve typed filename.
                requested = os.path.basename(requested)
                filename = requested if requested.endswith(".iq") else f"{requested}.iq"
                dst = os.path.join(CAPTURES_DIR, filename)

                # Save a single short replay-ready clip directly as the final file.
                clip_path = self._make_simple_replay_clip(self.replay_last_capture_path, output_path=dst)
                if clip_path != dst:
                    shutil.copy2(self.replay_last_capture_path, dst)
                self.replay_active_file = filename
                self.replay_last_capture_path = dst
                return {"status": "ok", "filename": filename, "path": dst, "backend": "hackrf"}
            return self.ecrf.save_current_signal(name)

        elif cmd == "ecrf_load":
            filename = params.get("file", "")
            if self.replay_backend == "hackrf":
                if not filename:
                    return {"status": "error", "error": "No file provided"}
                if filename.endswith((".sub", ".rawdata")):
                    return {"status": "error", "error": "Replay backend is HackRF, load a .iq file"}
                path = filename if os.path.isabs(filename) else os.path.join(CAPTURES_DIR, filename)
                if not os.path.exists(path):
                    return {"status": "error", "error": "File not found"}
                self.replay_active_file = os.path.basename(path)
                self.replay_last_capture_path = path
                return {"status": "ok", "length": os.path.getsize(path), "backend": "hackrf"}
            return self.ecrf.load_signal(filename)

        elif cmd == "list_all_signals":
            return self.ecrf.list_all_signals()

        # ── Auto-Capture + Decode ─────────────────────────────
        elif cmd == "monitor_start":
            freq = params.get("freq", 315.07)
            duration = params.get("duration", 30)
            return self.processor.start_monitor(freq, duration)

        elif cmd == "monitor_stop":
            return self.processor.stop_monitor()

        elif cmd == "monitor_captures":
            return self.processor.get_captures()

        elif cmd == "decode_file":
            filename = params.get("file")
            freq = params.get("freq", 315.07)
            return self.processor.decode_file(filename, freq)

        # ── Unknown ───────────────────────────────────────────
        else:
            return {"status": "error", "error": f"Unknown command: {cmd}"}

    def cleanup(self):
        """Clean up on exit."""
        print("\n[STOP] Shutting down...")
        self.hackrf.stop_scan()
        self.processor.stop_monitor()
        self.ecrf.close_websocket()
        if self.ser and self.ser.is_open:
            self.ser.close()
        print("[OK] Cleanup complete.")


def main():
    ap = argparse.ArgumentParser(description="RF Mobile Controller — Backend Server")
    ap.add_argument("--port", default=ESP_SERIAL_PORT,
                    help=f"Serial port (default: {ESP_SERIAL_PORT})")
    ap.add_argument("--baud", type=int, default=ESP_BAUD_RATE,
                    help=f"Baud rate (default: {ESP_BAUD_RATE})")
    args = ap.parse_args()

    server = RFMobileServer(args.port, args.baud)

    def sig_handler(sig, frame):
        server.running = False

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    server.run()


if __name__ == "__main__":
    main()
