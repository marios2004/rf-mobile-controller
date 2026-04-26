"""
RF Mobile Controller — Configuration
"""

import os

# Serial port for ESP8266 communication
ESP_SERIAL_PORT = "/dev/ttyUSB2"  # Adjust based on your setup (ESP)
ESP_BAUD_RATE = 115200

# Evil Crow RF2 Settings
ECRF_HOST = "192.168.4.1"
ECRF_WIFI_SSID = "CHANGE_ME_ECRF_SSID"
ECRF_WIFI_PASS = "CHANGE_ME_ECRF_PASS"

# h-RAT modulation codes
MOD_CODES = {
    "OOK": "2",
    "ASK": "2",
    "FSK": "0",
    "2FSK": "0",
}

# h-RAT preset codes
PRESET_CODES = {
    "AM270": "1",
    "AM650": "2",
    "FM238": "3",
    "FM476": "4",
}

# HackRF Settings
HACKRF_LNA_GAIN = 32
HACKRF_VGA_GAIN = 40
HACKRF_SAMPLE_RATE = 2_000_000

# Frequency bands
SCAN_BANDS = {
    "315": (312, 318),
    "433": (430, 436),
    "868": (865, 870),
    "915": (902, 928),
    "wide": (300, 928),
}

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAPTURES_DIR = os.path.join(BASE_DIR, "captures")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
