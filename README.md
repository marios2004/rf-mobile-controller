# RF Mobile Controller

Mobile-controlled RF pentesting toolkit. Control HackRF One and Evil Crow RF2 from your phone via an ESP8266-hosted web interface.

## Final Report Guide (10-15 pages)

Use this structure directly for the final project report.

### 1) Introduction to Topic (3-4 paragraphs)
- Introduce RF key fobs and sub-GHz access systems (cars, gates, garages).
- Explain why replay and rollback testing matters in real-world security.
- Motivate the project: practical, low-cost hardware security assessment.
- Define the scope: authorized pentesting and education only.

### 2) Project Objectives (1 paragraph)
- Build a mobile-controlled RF pentesting toolkit that can:
  - scan active RF frequencies,
  - capture/replay signals,
  - test rollback-style workflows,
  - demonstrate end-to-end hardware/software integration.

### 3) Project Review / State of the Art (1-2 pages)
- Brief review of SDR-based pentesting tools and methods.
- Compare common replay approaches:
  - raw IQ replay (HackRF),
  - timing replay (`.rawdata` / `.sub`, Evil Crow/Flipper style).
- Discuss fixed-code vs rolling-code systems and known limitations.
- Include legal/ethical boundaries and responsible testing practice.

### 4) Methodology (0.5-1 page)
- System design method:
  - ESP8266 web UI,
  - Python serial backend,
  - HackRF scanning/capture/replay,
  - Evil Crow HTTP/WebSocket control.
- Test method:
  - identify target frequency,
  - capture during button press,
  - replay and observe behavior,
  - iterate gains/timing/format.

### 5) Project Description + Evaluation (3-5 pages)
- Describe architecture and each component role.
- Explain each tab/workflow and command path.
- Show experiments performed, logs/screenshots, and outcomes.
- Evaluate reliability:
  - successful cases,
  - failed cases and root causes,
  - fixes applied (capture timing, backend routing, replay clipping).
- Provide your own critique on design trade-offs and performance.

### 6) Conclusions + Future Work (0.5 page)
- Summarize what was achieved and what was not fully solved.
- Propose practical next steps:
  - automatic capture quality scoring,
  - backend switch between HackRF/Evil Crow modes,
  - improved replay burst extraction,
  - stronger UI feedback and test automation.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                        LAPTOP                             │
│  ┌──────────────────────────────────────────────────────┐│
│  │              Python Backend (server.py)              ││
│  │  • Receives commands from ESP via USB serial         ││
│  │  • Controls HackRF (scan, capture, replay)           ││
│  │  • Controls Evil Crow via WiFi (HTTP + WebSocket)    ││
│  │  • Auto-capture + OOK/FSK demodulation               ││
│  └──────────────────────────────────────────────────────┘│
│         │ USB              │ USB              WiFi       │
│         ▼                  ▼                  ▼          │
│   ┌──────────┐      ┌─────────────┐    ┌─────────────┐  │
│   │ HackRF   │      │ ESP8266     │    │ Evil Crow   │  │
│   │ One      │      │ (AP + Web)  │    │ RF2         │  │
│   └──────────┘      └─────────────┘    └─────────────┘  │
└──────────────────────────────────────────────────────────┘
                             │ WiFi AP ("RF-Pentest")
                             ▼
                      ┌──────────────┐
                      │   Phone      │
                      │  (browser)   │
                      └──────────────┘
```

## Features

### 1. Frequency Scanner (HackRF)
- Scan 315/433/868/915 MHz bands
- Real-time peak detection with SNR
- Identify active signals

### 2. Simple Replay (Evil Crow)
- Configure frequency and modulation
- Record incoming signals via WebSocket
- One-tap replay

### 3. Rollback Attack (HackRF)
- Passive capture of N consecutive signals
- Session management with metadata
- Sequential replay for rolling code bypass

### 4. Auto-Capture + Decode
- Monitor frequency for signal bursts
- Auto-detect OOK vs 2-FSK modulation
- Demodulate and extract pulse timings
- Save as `.rawdata` (h-RAT) and `.sub` (Flipper) files

### 5. Evil Crow Direct Control
- Set frequency, modulation, preset
- Start/stop recording
- Quick link to h-RAT web UI

## Hardware Requirements

- **ESP8266** (NodeMCU, Wemos D1 Mini, etc.)
- **HackRF One** (SDR for scanning and rollback)
- **Evil Crow RF2** (h-RAT firmware, sub-GHz replay)
- **Computer** (runs Python backend, connects all devices)
- **Phone** (any device with a web browser)

## Installation

### 1. Flash ESP8266 Firmware

Open `firmware/esp8266_controller/esp8266_controller.ino` in Arduino IDE:

1. Install ESP8266 board support (Preferences → Boards Manager URL: `http://arduino.esp8266.com/stable/package_esp8266com_index.json`)
2. Install libraries: `ArduinoJson`
3. Select board: NodeMCU 1.0 (or your ESP8266 variant)
4. Flash the firmware

### 2. Install Python Dependencies

```bash
cd backend
pip3 install -r requirements.txt
```

### 3. Connect Hardware

1. **ESP8266** → Computer USB (note the port, e.g., `/dev/ttyUSB1`)
2. **HackRF One** → Computer USB
3. **Evil Crow RF2** → Computer USB (for power; WiFi for control)
4. **Computer WiFi** → Connect to your Evil Crow AP (use your own credentials)

### 4. Configure Backend

Edit `backend/config.py`:

```python
ESP_SERIAL_PORT = "/dev/ttyUSB1"  # Your ESP8266 port
ECRF_HOST = "192.168.4.1"         # Evil Crow IP (default)
```

## Usage

### Start the Backend

```bash
cd backend
python3 server.py
```

Expected output:
```
============================================================
  RF MOBILE CONTROLLER — Backend Server
============================================================
  Serial Port : /dev/ttyUSB1
  Baud Rate   : 115200
  Captures    : /path/to/captures
============================================================
  Waiting for commands from ESP8266...
============================================================
[OK] HackRF One connected
[OK] Evil Crow RF2 connected (192.168.4.1)
```

### Connect Your Phone

1. Open WiFi settings on your phone
2. Connect to your ESP8266 AP (SSID/password configured in firmware)
3. Open browser and navigate to **http://192.168.4.1**

### Mobile UI Tabs

| Tab | Function |
|-----|----------|
| **Scan** | Frequency sweep with HackRF |
| **Replay** | Simple capture/replay with Evil Crow |
| **Rollback** | Multi-signal passive rollback attack |
| **Auto** | Auto-capture + demodulation |
| **ECRF** | Direct Evil Crow control |

## File Structure

```
rf-mobile-controller/
├── README.md
├── firmware/
│   └── esp8266_controller/
│       └── esp8266_controller.ino   # ESP8266 AP + Web Server
├── backend/
│   ├── requirements.txt
│   ├── config.py                    # Configuration
│   ├── server.py                    # Main serial listener
│   ├── hackrf_controller.py         # HackRF operations
│   ├── evil_crow_controller.py      # Evil Crow HTTP API
│   └── signal_processor.py          # Auto-capture + decode
└── captures/                        # Saved signals (created at runtime)
```

## Serial Protocol

The ESP8266 communicates with the Python backend via JSON over serial:

**Request (Phone → ESP → Computer):**
```json
{"cmd": "scan", "params": {"band": "315", "duration": 10}, "id": 1}
```

**Response (Computer → ESP → Phone):**
```json
{"status": "ok", "peaks": [...], "id": 1}
```

**Event (Computer → ESP → Phone via SSE):**
```json
{"event": "scan_progress", "data": {"progress": 50, "peaks": [...]}}
```

## WiFi Network Topology

```
┌─────────────────────────────────────────────┐
│  ESP8266 Access Point: (custom SSID)        │
│  IP: 192.168.4.1                            │
│                                             │
│  Connected:                                 │
│    • Phone (192.168.4.x) — Web UI client    │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│  Evil Crow AP: (custom SSID)                │
│  IP: 192.168.4.1                            │
│                                             │
│  Connected:                                 │
│    • Laptop WiFi — HTTP/WS control          │
└─────────────────────────────────────────────┘
```

The laptop bridges both networks:
- **USB** to ESP8266 for serial communication
- **WiFi** to Evil Crow for h-RAT API access

## Captured File Formats

### `.rawdata` (h-RAT compatible)
```
# Auto-Capture #1
# Freq: 315.07 MHz
# Modulation: OOK
350,680,340,700,...
```

### `.sub` (Flipper Zero compatible)
```
Filetype: Flipper SubGhz RAW File
Version: 1
Frequency: 315070000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: RAW
RAW_Data: 350 -680 340 -700 ...
```

## Troubleshooting

### ESP8266 Not Responding
- Check serial port in `config.py`
- Verify baud rate is 115200
- Reflash ESP8266 firmware

### HackRF Not Detected
- Run `hackrf_info` to verify connection
- Check USB cable (must be data-capable)

### Evil Crow Not Reachable
- Connect laptop WiFi to your Evil Crow AP
- Verify Evil Crow is powered and running h-RAT
- Test with `curl http://192.168.4.1/`

### Phone Can't Connect to ESP8266
- Verify ESP8266 LED is blinking (AP active)
- Check the ESP8266 AP password configured in firmware
- Try forgetting and reconnecting to network

## Legal Notice

This tool is intended for authorized security testing and educational purposes only. Unauthorized interception or transmission of RF signals may violate local laws. Always obtain proper authorization before testing any RF systems.
