# GNU Radio Companion Flowgraphs

This folder contains GRC flowgraphs for HackRF signal analysis and manipulation.

## Prerequisites

```bash
# Install GNU Radio with HackRF support
sudo apt install gnuradio gr-osmosdr
```

## Flowgraphs

### 1. `hackrf_spectrum.grc` - Spectrum Analyzer
Basic spectrum analyzer with waterfall and time-domain display.

**Features:**
- Real-time spectrum display
- Waterfall plot
- Signal amplitude over time
- Adjustable frequency (300-928 MHz)
- Adjustable LNA/VGA gains

**Usage:**
```bash
grcc hackrf_spectrum.grc
python3 hackrf_spectrum.py
```

---

### 2. `hackrf_ook_demod.grc` - OOK Demodulator
Demodulates On-Off Keying signals (common for car key fobs).

**Features:**
- Raw magnitude display
- Low-pass filtered signal
- Threshold-based digital output
- Visual pulse/gap timing analysis

**Usage:**
```bash
grcc hackrf_ook_demod.grc
python3 hackrf_ook_demod.py
```

---

### 3. `hackrf_iq_recorder.grc` - IQ Recorder
Records raw IQ samples to file for later analysis or replay.

**Features:**
- Real-time spectrum monitoring while recording
- Output to 8-bit interleaved IQ file
- Compatible with `hackrf_transfer` format

**Usage:**
```bash
grcc hackrf_iq_recorder.grc
python3 hackrf_iq_recorder.py -o /path/to/capture.iq
```

---

### 4. `hackrf_iq_player.grc` - IQ File Transmitter
Transmits previously captured IQ files through HackRF.

**Features:**
- Load any IQ file captured by hackrf_transfer or the recorder
- Adjustable TX frequency and gain
- Optional repeat mode
- Visual TX monitoring

**Usage:**
```bash
grcc hackrf_iq_player.grc
python3 hackrf_iq_player.py -f /path/to/signal.iq
```

**Warning:** Only transmit signals you are authorized to transmit!

---

### 5. `hackrf_signal_detector.grc` - Signal Detector with ZMQ
Advanced signal detector that publishes detected peaks via ZMQ for integration with Python backend.

**Features:**
- Spectrum + waterfall display
- FFT-based peak detection
- Adjustable SNR threshold
- ZMQ PUB socket on `tcp://127.0.0.1:5555`
- JSON output format

**Usage:**
```bash
grcc hackrf_signal_detector.grc
python3 hackrf_signal_detector.py
```

**ZMQ Output Format:**
```json
{
  "freq_hz": 315070000.0,
  "freq_mhz": 315.07,
  "power_db": -25.3,
  "noise_db": -65.1,
  "snr_db": 39.8,
  "timestamp": 1713746400.123
}
```

**Python ZMQ Subscriber Example:**
```python
import zmq
import json

ctx = zmq.Context()
sub = ctx.socket(zmq.SUB)
sub.connect("tcp://127.0.0.1:5555")
sub.setsockopt_string(zmq.SUBSCRIBE, "")

while True:
    msg = sub.recv_string()
    peak = json.loads(msg)
    print(f"Signal at {peak['freq_mhz']} MHz, SNR: {peak['snr_db']} dB")
```

---

## Compiling All Flowgraphs

```bash
cd /path/to/rf-mobile-controller/grc
for f in *.grc; do
    echo "Compiling $f..."
    grcc "$f"
done
```

## Common Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| center_freq | 315.07 MHz | Target frequency |
| samp_rate | 2 MHz | Sample rate |
| lna_gain | 32 | RF front-end gain (0-40, step 8) |
| vga_gain | 40 | Baseband gain (0-62, step 2) |
| fft_size | 1024 | FFT bins for spectrum |

## Integration with Backend

The `hackrf_signal_detector.grc` flowgraph can be used alongside the Python backend:

1. Start the signal detector: `python3 hackrf_signal_detector.py`
2. It publishes peaks on ZMQ port 5555
3. The backend can subscribe to receive real-time peak notifications

This is useful for auto-capture workflows where you want visual monitoring while the backend handles signal processing.
