"""
Signal Processor — Auto-capture and decode (OOK/FSK)

Adapted from rf-sync-sentinel/signal_capturer.py for standalone use.
"""

import os
import subprocess
import threading
import time

import numpy as np

from config import HACKRF_SAMPLE_RATE, HACKRF_LNA_GAIN, HACKRF_VGA_GAIN, CAPTURES_DIR


DECIMATION = 10


class SignalProcessor:
    """Auto-capture and demodulate RF signals."""

    def __init__(self, on_event=None):
        self.on_event = on_event
        self.monitoring = False
        self._stop_event = threading.Event()
        self._capture_count = 0
        self._recent_captures = []

    def _emit(self, event_type, data):
        if self.on_event:
            self.on_event(event_type, data)

    def start_monitor(self, freq_mhz, duration=30):
        """Start monitoring for signals at specified frequency."""
        if self.monitoring:
            return {"error": "Already monitoring"}

        self.monitoring = True
        self._stop_event.clear()

        thread = threading.Thread(
            target=self._monitor_loop,
            args=(freq_mhz, duration),
            daemon=True,
        )
        thread.start()

        return {"status": "ok", "freq_mhz": freq_mhz, "duration": duration}

    def stop_monitor(self):
        self._stop_event.set()
        self.monitoring = False
        return {"status": "ok"}

    def get_captures(self):
        """Get recent captures and monitoring status."""
        return {
            "status": "ok",
            "monitoring": self.monitoring,
            "captures": self._recent_captures[-10:]
        }

    def clear_captures(self):
        """Clear recent captures list."""
        self._recent_captures = []

    def _monitor_loop(self, freq_mhz, duration):
        """Continuously capture and analyze for signal bursts."""
        freq_hz = int(freq_mhz * 1e6)
        segment_duration = 3
        n_samples = HACKRF_SAMPLE_RATE * segment_duration

        print(f"[MONITOR] Started at {freq_mhz} MHz for {duration}s")
        self._recent_captures = []  # Clear previous captures

        start_time = time.time()

        while not self._stop_event.is_set() and (time.time() - start_time) < duration:
            tmp_file = f"/tmp/rf_monitor_{int(time.time())}.iq"

            cmd = [
                "hackrf_transfer",
                "-r", tmp_file,
                "-f", str(freq_hz),
                "-s", str(HACKRF_SAMPLE_RATE),
                "-l", str(HACKRF_LNA_GAIN),
                "-g", str(HACKRF_VGA_GAIN),
                "-n", str(n_samples),
            ]

            try:
                subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=segment_duration + 5, check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

            if os.path.exists(tmp_file):
                result = self._analyze_and_decode(tmp_file, freq_mhz)
                os.remove(tmp_file)

                if result and result.get("has_signal"):
                    print(f"[MONITOR] Signal detected: {result['modulation']} @ {freq_mhz} MHz, {result['n_pulses']} pulses")
                    # Add to recent captures for frontend
                    self._recent_captures.append({
                        "time": time.strftime("%H:%M:%S"),
                        "freq_mhz": freq_mhz,
                        "modulation": result.get("modulation", "?"),
                        "n_pulses": result.get("n_pulses", 0),
                        "duration_ms": result.get("duration_ms", 0),
                    })

            elapsed = time.time() - start_time
            print(f"[MONITOR] Progress: {int(elapsed)}/{duration}s")

        self.monitoring = False
        print("[MONITOR] Stopped")

    def _analyze_and_decode(self, filename, freq_mhz):
        """Analyze IQ file, detect modulation, demodulate."""
        try:
            raw = np.fromfile(filename, dtype=np.int8)
            if len(raw) < 1000:
                return None

            i_data = raw[0::2].astype(np.float32)
            q_data = raw[1::2].astype(np.float32)
            iq = i_data + 1j * q_data
            mag = np.abs(iq)

            noise = float(np.percentile(mag, 20))
            peak = float(np.percentile(mag, 99))

            if peak / (noise + 1e-10) < 3.0:
                return None

            modulation, deviation_hz = self._detect_modulation(iq)

            if modulation == "OOK":
                timings = self._demod_ook(iq)
            else:
                timings, deviation_hz = self._demod_fsk(iq, deviation_hz)

            if not timings or len(timings) < 8:
                return None

            bursts = self._split_bursts(timings)
            if not bursts:
                return None

            burst = max(bursts, key=len)

            for i, (is_high, _) in enumerate(burst):
                if is_high:
                    burst = burst[i:]
                    break

            rawdata = ",".join(str(dur) for _, dur in burst)
            n_pulses = sum(1 for h, _ in burst if h)
            total_ms = sum(d for _, d in burst) / 1000

            self._capture_count += 1

            result = {
                "has_signal": True,
                "capture_id": self._capture_count,
                "freq_mhz": freq_mhz,
                "modulation": modulation,
                "deviation_hz": round(deviation_hz, 1) if modulation == "FSK" else 0,
                "n_pulses": n_pulses,
                "duration_ms": round(total_ms, 1),
                "rawdata": rawdata,
                "rawdata_length": len(rawdata),
                "n_bursts": len(bursts),
            }

            self._save_capture(result)

            return result

        except Exception as exc:
            return None

    def _detect_modulation(self, iq):
        """Classify signal as OOK/ASK or 2-FSK."""
        mag = np.abs(iq)

        dec = max(1, len(mag) // 50000)
        if dec > 1:
            trim = len(mag) // dec * dec
            mag_d = mag[:trim].reshape(-1, dec).mean(axis=1)
            iq_d = iq[:trim:dec]
        else:
            mag_d = mag
            iq_d = iq

        p10 = float(np.percentile(mag_d, 10))
        p90 = float(np.percentile(mag_d, 90))
        amp_ratio = p90 / (p10 + 1e-10)

        phase = np.unwrap(np.angle(iq_d))
        inst_freq = np.diff(phase)

        median_amp = float(np.median(mag_d))
        active_mask = mag_d[1:] > median_amp
        deviation_hz = 0.0
        freq_is_bimodal = False

        if np.sum(active_mask) > 50:
            active_freq = inst_freq[active_mask]
            freq_median = float(np.median(active_freq))

            above = active_freq[active_freq > freq_median]
            below = active_freq[active_freq <= freq_median]

            if len(above) > 10 and len(below) > 10:
                mean_hi = float(np.mean(above))
                mean_lo = float(np.mean(below))
                eff_rate = HACKRF_SAMPLE_RATE / dec
                separation_hz = abs(mean_hi - mean_lo) * eff_rate / (2 * np.pi)
                deviation_hz = separation_hz / 2

                var_hi = float(np.var(above))
                var_lo = float(np.var(below))
                cluster_spread = (var_hi + var_lo) / 2
                gap = (mean_hi - mean_lo) ** 2
                freq_is_bimodal = gap > cluster_spread * 4 if cluster_spread > 0 else False

        if amp_ratio > 3.0 and not freq_is_bimodal:
            return "OOK", 0.0
        elif amp_ratio < 2.5 and freq_is_bimodal:
            return "FSK", deviation_hz
        elif freq_is_bimodal and deviation_hz > 500:
            return "FSK", deviation_hz
        else:
            return "OOK", 0.0

    def _demod_ook(self, iq):
        mag = np.abs(iq)
        trim = len(mag) // DECIMATION * DECIMATION
        mag = mag[:trim].reshape(-1, DECIMATION).mean(axis=1)
        eff_rate = HACKRF_SAMPLE_RATE / DECIMATION

        noise = float(np.percentile(mag, 30))
        peak = float(np.percentile(mag, 99))
        if peak - noise < noise * 0.3:
            return []

        thresh = noise + (peak - noise) * 0.3
        binary = (mag > thresh).astype(np.int8)

        return self._rle_timings(binary, eff_rate)

    def _demod_fsk(self, iq, estimated_dev):
        trim = len(iq) // DECIMATION * DECIMATION
        iq_d = iq[:trim].reshape(-1, DECIMATION).mean(axis=1)
        eff_rate = HACKRF_SAMPLE_RATE / DECIMATION

        phase = np.unwrap(np.angle(iq_d))
        inst_freq = np.diff(phase) * eff_rate / (2 * np.pi)

        kernel_size = max(1, int(eff_rate * 0.0002))
        if kernel_size > 1:
            kernel = np.ones(kernel_size) / kernel_size
            inst_freq = np.convolve(inst_freq, kernel, mode="same")

        mag_d = np.abs(iq_d)
        active = mag_d[1:] > np.percentile(mag_d, 40)

        if np.sum(active) < 50:
            return [], estimated_dev

        active_freq = inst_freq[active]
        thresh = float(np.median(active_freq))
        binary = (inst_freq > thresh).astype(np.int8)

        above = active_freq[active_freq > thresh]
        below = active_freq[active_freq <= thresh]
        if len(above) > 5 and len(below) > 5:
            estimated_dev = abs(float(np.mean(above)) - float(np.mean(below))) / 2

        timings = self._rle_timings(binary, eff_rate)
        return timings, estimated_dev

    @staticmethod
    def _rle_timings(binary, eff_rate):
        diff = np.diff(binary)
        transitions = np.where(diff != 0)[0]
        if len(transitions) < 8:
            return []

        timings = []
        for i in range(len(transitions) - 1):
            dur_samples = transitions[i + 1] - transitions[i]
            dur_us = int(round(dur_samples * 1e6 / eff_rate))
            is_high = bool(binary[transitions[i] + 1])
            timings.append((is_high, dur_us))
        return timings

    @staticmethod
    def _split_bursts(timings, gap_us=20_000):
        bursts = []
        current = []
        for is_high, dur in timings:
            if not is_high and dur > gap_us:
                if len(current) >= 8:
                    bursts.append(current)
                current = []
            else:
                current.append((is_high, dur))
        if len(current) >= 8:
            bursts.append(current)
        return bursts

    def _save_capture(self, result):
        ts = time.strftime("%Y%m%d_%H%M%S")
        cap_id = result["capture_id"]
        mod = result["modulation"]

        rawdata_path = os.path.join(
            CAPTURES_DIR, f"auto_{ts}_{cap_id}_{result['freq_mhz']}MHz.rawdata"
        )
        with open(rawdata_path, "w") as f:
            f.write(f"# Auto-Capture #{cap_id}\n")
            f.write(f"# Freq: {result['freq_mhz']} MHz\n")
            f.write(f"# Modulation: {mod}\n")
            if mod == "FSK":
                f.write(f"# Deviation: {result['deviation_hz']:.0f} Hz\n")
            f.write(f"# Pulses: {result['n_pulses']}\n")
            f.write(f"# Duration: {result['duration_ms']:.1f} ms\n")
            f.write(result["rawdata"] + "\n")

        sub_path = os.path.join(
            CAPTURES_DIR, f"auto_{ts}_{cap_id}_{result['freq_mhz']}MHz.sub"
        )
        freq_hz = int(result["freq_mhz"] * 1e6)
        preset = "FuriHalSubGhzPresetOok650Async" if mod == "OOK" else "FuriHalSubGhzPreset2FSKDev476Async"

        with open(sub_path, "w") as f:
            f.write("Filetype: Flipper SubGhz RAW File\n")
            f.write("Version: 1\n")
            f.write(f"Frequency: {freq_hz}\n")
            f.write(f"Preset: {preset}\n")
            f.write("Protocol: RAW\n")

            vals = result["rawdata"].split(",")
            signed_vals = []
            for i, v in enumerate(vals):
                try:
                    num = int(v.strip())
                    signed_vals.append(str(num) if i % 2 == 0 else str(-num))
                except ValueError:
                    continue

            for i in range(0, len(signed_vals), 512):
                chunk = signed_vals[i:i + 512]
                f.write(f"RAW_Data: {' '.join(chunk)}\n")

        result["rawdata_file"] = rawdata_path
        result["sub_file"] = sub_path

    def decode_file(self, filename, freq_mhz):
        """Decode an existing IQ file."""
        if not os.path.exists(filename):
            return {"error": "File not found"}

        result = self._analyze_and_decode(filename, freq_mhz)
        if result:
            return result
        return {"error": "No signal found in file"}
