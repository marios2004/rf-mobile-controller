/*
 * RF Mobile Controller — ESP8266 Web Interface
 * 
 * Creates a WiFi AP that phones connect to.
 * Serves a mobile web UI for controlling HackRF and Evil Crow.
 * Acts as a serial bridge to the Python backend on the computer.
 * 
 * Hardware: ESP8266 (NodeMCU, Wemos D1 Mini, etc.)
 * 
 * Connections:
 *   - ESP8266 USB to computer (serial communication)
 *   - Phone connects to ESP8266 WiFi AP
 * 
 * Serial Protocol (JSON):
 *   Phone -> ESP -> Computer:  {"cmd": "scan", "params": {...}}
 *   Computer -> ESP -> Phone:  {"status": "ok", "data": {...}}
 *   Events (Server-Sent):      {"event": "scan_progress", "data": {...}}
 */

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ArduinoJson.h>

// ══════════════════════════════════════════════════════════════
//  Configuration
// ══════════════════════════════════════════════════════════════

const char* AP_SSID = "CHANGE_ME_RF_AP";
const char* AP_PASS = "CHANGE_ME_PASS";
const int AP_CHANNEL = 6;
const bool AP_HIDDEN = false;

IPAddress localIP(192, 168, 4, 1);
IPAddress gateway(192, 168, 4, 1);
IPAddress subnet(255, 255, 255, 0);

#define LED_PIN 2
#define SERIAL_BAUD 115200
#define MAX_RESPONSE_WAIT 30000
#define JSON_BUFFER_SIZE 4096

// ══════════════════════════════════════════════════════════════
//  Globals
// ══════════════════════════════════════════════════════════════

ESP8266WebServer server(80);

String pendingResponse = "";
bool waitingForResponse = false;
unsigned long responseTimeout = 0;
int currentRequestId = 0;

// Forward declarations
void broadcastSSE(const String& data);

// SSE clients for real-time events
#define MAX_SSE_CLIENTS 4
WiFiClient sseClients[MAX_SSE_CLIENTS];
bool sseActive[MAX_SSE_CLIENTS] = {false};

// ══════════════════════════════════════════════════════════════
//  HTML/CSS/JS (Embedded)
// ══════════════════════════════════════════════════════════════

const char INDEX_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>RF Pentest Controller</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a1a;
            color: #e0e0e0;
            min-height: 100vh;
            padding-bottom: 70px;
        }
        header {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            padding: 15px;
            text-align: center;
            border-bottom: 2px solid #00ff88;
            position: sticky;
            top: 0;
            z-index: 100;
        }
        header h1 {
            font-size: 20px;
            color: #00ff88;
            margin-bottom: 5px;
        }
        .status-bar {
            display: flex;
            justify-content: center;
            gap: 15px;
            font-size: 11px;
        }
        .status-item {
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #ff4444;
        }
        .status-dot.online { background: #00ff88; }
        
        .tabs {
            display: flex;
            background: #1a1a2e;
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            z-index: 100;
            border-top: 1px solid #333;
        }
        .tab {
            flex: 1;
            padding: 12px 5px;
            text-align: center;
            font-size: 11px;
            color: #888;
            border: none;
            background: transparent;
            cursor: pointer;
        }
        .tab.active {
            color: #00ff88;
            background: rgba(0, 255, 136, 0.1);
        }
        .tab-icon { font-size: 18px; display: block; margin-bottom: 2px; }
        
        .page {
            display: none;
            padding: 15px;
        }
        .page.active { display: block; }
        
        .card {
            background: #1a1a2e;
            border-radius: 12px;
            padding: 15px;
            margin-bottom: 15px;
            border: 1px solid #333;
        }
        .card-title {
            font-size: 14px;
            color: #00ccff;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 12px 20px;
            border: none;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            width: 100%;
            margin-bottom: 10px;
        }
        .btn-primary {
            background: linear-gradient(135deg, #00ff88 0%, #00ccff 100%);
            color: #0a0a1a;
        }
        .btn-danger {
            background: linear-gradient(135deg, #ff4444 0%, #ff6b6b 100%);
            color: white;
        }
        .btn-secondary {
            background: #333;
            color: #e0e0e0;
        }
        .btn:active { transform: scale(0.98); }
        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        .input-group {
            margin-bottom: 12px;
        }
        .input-group label {
            display: block;
            font-size: 12px;
            color: #888;
            margin-bottom: 5px;
        }
        input, select {
            width: 100%;
            padding: 12px;
            border: 1px solid #333;
            border-radius: 8px;
            background: #0a0a1a;
            color: #e0e0e0;
            font-size: 16px;
        }
        input:focus, select:focus {
            outline: none;
            border-color: #00ff88;
        }
        
        .btn-row {
            display: flex;
            gap: 10px;
        }
        .btn-row .btn {
            flex: 1;
            margin-bottom: 0;
        }
        
        .peaks-list {
            max-height: 250px;
            overflow-y: auto;
        }
        .peak-item {
            display: flex;
            justify-content: space-between;
            padding: 8px;
            background: #0a0a1a;
            border-radius: 6px;
            margin-bottom: 6px;
            font-size: 12px;
        }
        .peak-freq { color: #00ff88; font-weight: bold; }
        .peak-pwr { color: #00ccff; }
        .peak-snr { color: #888; }
        
        .signal-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px;
            background: #0a0a1a;
            border-radius: 8px;
            margin-bottom: 8px;
        }
        .signal-name { font-size: 12px; color: #00ff88; }
        .signal-meta { font-size: 10px; color: #888; }
        .signal-actions { display: flex; gap: 8px; }
        .signal-btn {
            padding: 6px 12px;
            font-size: 11px;
            border-radius: 6px;
        }
        
        .progress-bar {
            height: 6px;
            background: #333;
            border-radius: 3px;
            overflow: hidden;
            margin-top: 10px;
        }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #00ff88, #00ccff);
            transition: width 0.3s;
        }
        
        .rollback-step {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px;
            background: #0a0a1a;
            border-radius: 8px;
            margin-bottom: 8px;
        }
        .step-num {
            width: 28px;
            height: 28px;
            border-radius: 50%;
            background: #333;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 12px;
            font-weight: bold;
        }
        .step-num.done { background: #00ff88; color: #0a0a1a; }
        .step-num.active { background: #00ccff; color: #0a0a1a; }
        .step-info { flex: 1; }
        .step-title { font-size: 13px; }
        .step-status { font-size: 11px; color: #888; }

        .hidden { display: none !important; }
        
        .record-indicator {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            padding: 15px;
            background: #1a0a0a;
            border: 1px solid #ff4444;
            border-radius: 8px;
            margin-bottom: 10px;
        }
        .record-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #ff4444;
            animation: blink 1s infinite;
        }
        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
    </style>
</head>
<body>
    <header>
        <h1>RF Pentest Controller</h1>
        <div class="status-bar">
            <div class="status-item">
                <div class="status-dot" id="hackrf-status"></div>
                <span>HackRF</span>
            </div>
            <div class="status-item">
                <div class="status-dot" id="ecrf-status"></div>
                <span>Evil Crow</span>
            </div>
            <div class="status-item">
                <div class="status-dot online" id="esp-status"></div>
                <span>ESP</span>
            </div>
        </div>
    </header>

    <!-- SCAN PAGE -->
    <div id="page-scan" class="page active">
        <div class="card">
            <div class="card-title">Frequency Scanner</div>
            <div class="input-group">
                <label>Frequency Band</label>
                <select id="scan-band">
                    <option value="315">315 MHz (US key fobs)</option>
                    <option value="433" selected>433 MHz (EU/Asia)</option>
                    <option value="868">868 MHz (EU)</option>
                    <option value="915">915 MHz (US)</option>
                </select>
            </div>
            <div class="input-group">
                <label>Duration (seconds)</label>
                <input type="number" id="scan-duration" value="10" min="5" max="60">
            </div>
            <button class="btn btn-primary" id="btn-scan" onclick="startScan()">Start Scan</button>
            <button class="btn btn-danger hidden" id="btn-scan-stop" onclick="stopScan()">Stop</button>
            <div class="progress-bar hidden" id="scan-progress">
                <div class="progress-fill" id="scan-progress-fill"></div>
            </div>
        </div>
        <div class="card">
            <div class="card-title">Detected Peaks</div>
            <div class="peaks-list" id="peaks-list">
                <p style="color: #888; font-size: 12px;">No peaks detected yet.</p>
            </div>
        </div>
    </div>

    <!-- REPLAY PAGE -->
    <div id="page-replay" class="page">
        <div class="card">
            <div class="card-title">Record & Replay</div>
            <div class="input-group">
                <label>Frequency (MHz)</label>
                <input type="number" id="replay-freq" value="433.92" step="0.01">
            </div>
            <div class="input-group">
                <label>Modulation</label>
                <select id="replay-mod">
                    <option value="OOK">OOK / ASK</option>
                    <option value="FSK">2-FSK</option>
                </select>
            </div>
            <div class="input-group">
                <label>Record Duration</label>
                <select id="replay-duration">
                    <option value="5">5 seconds</option>
                    <option value="10" selected>10 seconds</option>
                    <option value="20">20 seconds</option>
                    <option value="0">Manual stop</option>
                </select>
            </div>
            <div id="record-indicator" class="record-indicator hidden">
                <div class="record-dot"></div>
                <span style="color: #ff6b6b;">Recording... <span id="record-time">0</span>s - PRESS KEY FOB NOW!</span>
            </div>
            <p id="replay-status" style="color: #888; font-size: 12px; margin-bottom: 10px;"></p>
            <button class="btn btn-primary" id="btn-record" onclick="startRecord()">Start Recording</button>
            <button class="btn btn-danger hidden" id="btn-stop" onclick="stopRecord()">Stop Recording</button>
            <div class="btn-row">
                <button class="btn btn-secondary" onclick="replaySignal()">Replay</button>
                <button class="btn btn-secondary" onclick="showSaveDialog()">Save</button>
            </div>
        </div>
        <div class="card hidden" id="save-dialog">
            <div class="card-title">Save Signal</div>
            <div class="input-group">
                <label>Signal Name</label>
                <input type="text" id="save-name" placeholder="e.g. garage_door">
            </div>
            <div class="btn-row">
                <button class="btn btn-primary" onclick="saveSignal()">Save</button>
                <button class="btn btn-secondary" onclick="hideSaveDialog()">Cancel</button>
            </div>
        </div>
        <div class="card">
            <div class="card-title">Saved Signals</div>
            <div id="signals-list" style="max-height: 200px; overflow-y: auto;">
                <p style="color: #888; font-size: 12px;">No signals yet.</p>
            </div>
            <button class="btn btn-secondary" onclick="loadSignals()" style="margin-top: 10px;">Refresh</button>
        </div>
    </div>

    <!-- ROLLBACK PAGE -->
    <div id="page-rollback" class="page">
        <div class="card">
            <div class="card-title">Rollback Attack</div>
            <div class="input-group">
                <label>Target Frequency (MHz)</label>
                <input type="number" id="rollback-freq" value="315.07" step="0.01">
            </div>
            <div class="input-group">
                <label>Signals to Capture</label>
                <select id="rollback-count">
                    <option value="2">2 signals</option>
                    <option value="3" selected>3 signals</option>
                    <option value="4">4 signals</option>
                </select>
            </div>
            <button class="btn btn-primary" id="btn-rollback-start" onclick="startRollback()">Start Session</button>
        </div>
        <div class="card hidden" id="rollback-progress">
            <div class="card-title">Capture Progress</div>
            <div id="rollback-steps"></div>
            <button class="btn btn-primary" id="btn-capture-next" onclick="captureNext()">Capture Signal 1</button>
            <button class="btn btn-danger hidden" id="btn-rollback-replay" onclick="rollbackReplay()">Execute Replay</button>
        </div>
        <div class="card">
            <div class="card-title">Saved Sessions</div>
            <div id="sessions-list" style="max-height: 200px; overflow-y: auto;">
                <p style="color: #888; font-size: 12px;">No sessions yet.</p>
            </div>
            <button class="btn btn-secondary" onclick="loadSessions()" style="margin-top: 10px;">Load Sessions</button>
        </div>
    </div>

    <!-- JAM PAGE -->
    <div id="page-jam" class="page">
        <div class="card">
            <div class="card-title">Jammer Control</div>
            <div class="input-group">
                <label>Frequency (MHz)</label>
                <input type="number" id="jam-freq" value="315.07" step="0.01">
            </div>
            <button class="btn btn-danger" id="btn-jam" onclick="toggleJam()">Start Jammer</button>
            <p id="jam-status" style="color: #888; font-size: 12px; margin-top: 10px;"></p>
        </div>
    </div>

    <!-- TAB BAR -->
    <div class="tabs">
        <button class="tab active" onclick="showPage('scan')">
            <span class="tab-icon">📡</span>Scan
        </button>
        <button class="tab" onclick="showPage('replay')">
            <span class="tab-icon">📻</span>Replay
        </button>
        <button class="tab" onclick="showPage('rollback')">
            <span class="tab-icon">🔓</span>Rollback
        </button>
        <button class="tab" onclick="showPage('jam')">
            <span class="tab-icon">🔴</span>Jam
        </button>
    </div>

    <script>
        var eventSource = null;
        var rollbackSession = null;
        var rollbackIndex = 0;
        var rollbackCount = 3;
        var recordTimer = null;
        var recordSeconds = 0;
        var isJamming = false;

        function showPage(name) {
            var pages = document.querySelectorAll('.page');
            var tabs = document.querySelectorAll('.tab');
            for (var i = 0; i < pages.length; i++) pages[i].classList.remove('active');
            for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove('active');
            document.getElementById('page-' + name).classList.add('active');
            event.target.closest('.tab').classList.add('active');
        }

        function api(cmd, params) {
            params = params || {};
            return fetch('/api', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({cmd: cmd, params: params})
            }).then(function(r) { return r.json(); })
            .catch(function(e) { return {status: 'error', error: e.message}; });
        }

        function updatePeaks(peaks) {
            var list = document.getElementById('peaks-list');
            if (!peaks || !peaks.length) {
                list.innerHTML = '<p style="color: #888; font-size: 12px;">No peaks detected.</p>';
                return;
            }
            var html = '';
            for (var i = 0; i < peaks.length; i++) {
                var p = peaks[i];
                html += '<div class="peak-item"><span class="peak-freq">' + p.freq_mhz + ' MHz</span><span class="peak-pwr">' + p.power_db + ' dB</span><span class="peak-snr">SNR ' + p.snr_db + '</span></div>';
            }
            list.innerHTML = html;
        }

        function startScan() {
            var band = document.getElementById('scan-band').value;
            var duration = parseInt(document.getElementById('scan-duration').value);
            document.getElementById('btn-scan').classList.add('hidden');
            document.getElementById('btn-scan-stop').classList.remove('hidden');
            document.getElementById('scan-progress').classList.remove('hidden');
            document.getElementById('scan-progress-fill').style.width = '0%';
            
            var progress = 0;
            var interval = setInterval(function() {
                progress += 100 / duration;
                if (progress > 95) progress = 95;
                document.getElementById('scan-progress-fill').style.width = progress + '%';
            }, 1000);
            
            api('scan_sync', {band: band, duration: duration}).then(function(resp) {
                clearInterval(interval);
                document.getElementById('scan-progress-fill').style.width = '100%';
                if (resp.peaks) updatePeaks(resp.peaks);
                document.getElementById('btn-scan').classList.remove('hidden');
                document.getElementById('btn-scan-stop').classList.add('hidden');
                document.getElementById('scan-progress').classList.add('hidden');
            });
        }

        function stopScan() {
            api('scan_stop');
            document.getElementById('btn-scan').classList.remove('hidden');
            document.getElementById('btn-scan-stop').classList.add('hidden');
            document.getElementById('scan-progress').classList.add('hidden');
        }

        function startRecord() {
            var freq = parseFloat(document.getElementById('replay-freq').value);
            var mod = document.getElementById('replay-mod').value;
            var duration = parseInt(document.getElementById('replay-duration').value);
            var status = document.getElementById('replay-status');
            
            status.textContent = 'Connecting...';
            status.style.color = '#00ccff';
            
            api('ecrf_configure', {freq: freq, mod: mod, preset: 'AM650'}).then(function(resp) {
                if (resp.status !== 'ok') {
                    status.textContent = 'Error: Connect laptop WiFi to Evil Crow RF v2';
                    status.style.color = '#ff4444';
                    return;
                }
                
                api('ecrf_record').then(function(resp2) {
                    if (resp2.status === 'ok') {
                        document.getElementById('btn-record').classList.add('hidden');
                        document.getElementById('btn-stop').classList.remove('hidden');
                        document.getElementById('record-indicator').classList.remove('hidden');
                        status.textContent = 'Recording at ' + freq + ' MHz - Press key fob now!';
                        status.style.color = '#ff6b6b';
                        
                        recordSeconds = 0;
                        document.getElementById('record-time').textContent = '0';
                        recordTimer = setInterval(function() {
                            recordSeconds++;
                            document.getElementById('record-time').textContent = recordSeconds;
                            if (duration > 0 && recordSeconds >= duration) {
                                stopRecord();
                            }
                        }, 1000);
                    }
                });
            });
        }

        function stopRecord() {
            if (recordTimer) {
                clearInterval(recordTimer);
                recordTimer = null;
            }
            api('ecrf_stop_record').then(function() {
                document.getElementById('btn-record').classList.remove('hidden');
                document.getElementById('btn-stop').classList.add('hidden');
                document.getElementById('record-indicator').classList.add('hidden');
                document.getElementById('replay-status').textContent = 'Recording stopped. Use Replay to transmit.';
                document.getElementById('replay-status').style.color = '#00ff88';
            });
        }

        function replaySignal() {
            var status = document.getElementById('replay-status');
            status.textContent = 'Transmitting...';
            status.style.color = '#00ccff';
            api('ecrf_replay').then(function(resp) {
                if (resp.status === 'ok') {
                    status.textContent = 'Signal transmitted!';
                    status.style.color = '#00ff88';
                } else {
                    status.textContent = resp.error || 'No signal to replay';
                    status.style.color = '#ff4444';
                }
            });
        }

        function showSaveDialog() {
            document.getElementById('save-dialog').classList.remove('hidden');
            document.getElementById('save-name').value = '';
            document.getElementById('save-name').focus();
        }

        function hideSaveDialog() {
            document.getElementById('save-dialog').classList.add('hidden');
        }

        function saveSignal() {
            var name = document.getElementById('save-name').value.trim();
            var status = document.getElementById('replay-status');
            
            if (!name) {
                status.textContent = 'Please enter a name';
                status.style.color = '#ff4444';
                return;
            }
            
            status.textContent = 'Saving...';
            status.style.color = '#00ccff';
            
            api('ecrf_save', {name: name}).then(function(resp) {
                if (resp.status === 'ok') {
                    status.textContent = 'Signal saved as: ' + resp.filename;
                    status.style.color = '#00ff88';
                    hideSaveDialog();
                    loadSignals();
                } else {
                    status.textContent = resp.error || 'No signal to save';
                    status.style.color = '#ff4444';
                }
            });
        }

        function loadSignals() {
            var list = document.getElementById('signals-list');
            list.innerHTML = '<p style="color: #888;">Loading...</p>';
    api('ecrf_signals', {include_sub: true}).then(function(resp) {
                if (!resp.signals || !resp.signals.length) {
                    list.innerHTML = '<p style="color: #888; font-size: 12px;">No signals saved yet.</p>';
                    return;
                }
                var html = '';
                for (var i = 0; i < resp.signals.length; i++) {
                    var s = resp.signals[i];
                    html += '<div class="signal-item"><div><div class="signal-name">' + s.name + '</div><div class="signal-meta">' + s.created + '</div></div><button class="btn btn-secondary signal-btn" onclick="playSignal(\'' + s.name + '\')">Play</button></div>';
                }
                list.innerHTML = html;
            });
        }

        function playSignal(name) {
            api('ecrf_load', {file: name}).then(function() {
                api('ecrf_replay', {file: name});
            });
        }

        function startRollback() {
            var freq = parseFloat(document.getElementById('rollback-freq').value);
            rollbackCount = parseInt(document.getElementById('rollback-count').value);
            rollbackIndex = 0;
            
            api('rollback_start', {freq: freq, count: rollbackCount}).then(function(resp) {
                if (resp.status === 'ok') {
                    rollbackSession = resp.session;
                    document.getElementById('btn-rollback-start').classList.add('hidden');
                    document.getElementById('rollback-progress').classList.remove('hidden');
                    
                    var html = '';
                    for (var i = 1; i <= rollbackCount; i++) {
                        html += '<div class="rollback-step"><div class="step-num" id="step-' + i + '">' + i + '</div><div class="step-info"><div class="step-title">Signal ' + i + '</div><div class="step-status" id="step-status-' + i + '">Waiting</div></div></div>';
                    }
                    document.getElementById('rollback-steps').innerHTML = html;
                    document.getElementById('btn-capture-next').textContent = 'Capture Signal 1';
                }
            });
        }

        function captureNext() {
            var idx = rollbackIndex + 1;
            document.getElementById('step-' + idx).classList.add('active');
            document.getElementById('step-status-' + idx).textContent = 'Recording...';
            document.getElementById('btn-capture-next').disabled = true;
            
            api('rollback_capture', {index: idx, duration: 4}).then(function() {
                setTimeout(function() {
                    document.getElementById('step-' + idx).classList.remove('active');
                    document.getElementById('step-' + idx).classList.add('done');
                    document.getElementById('step-status-' + idx).textContent = 'Captured';
                    document.getElementById('btn-capture-next').disabled = false;
                    
                    rollbackIndex++;
                    if (rollbackIndex < rollbackCount) {
                        document.getElementById('btn-capture-next').textContent = 'Capture Signal ' + (rollbackIndex + 1);
                    } else {
                        document.getElementById('btn-capture-next').classList.add('hidden');
                        document.getElementById('btn-rollback-replay').classList.remove('hidden');
                    }
                }, 5000);
            });
        }

        function rollbackReplay() {
            document.getElementById('btn-rollback-replay').disabled = true;
            document.getElementById('btn-rollback-replay').textContent = 'Transmitting...';
            api('rollback_replay', {session: rollbackSession, delay: 0.3}).then(function() {
                document.getElementById('btn-rollback-replay').textContent = 'Done!';
                setTimeout(function() {
                    document.getElementById('rollback-progress').classList.add('hidden');
                    document.getElementById('btn-rollback-start').classList.remove('hidden');
                    document.getElementById('btn-rollback-replay').classList.add('hidden');
                    document.getElementById('btn-rollback-replay').disabled = false;
                    document.getElementById('btn-rollback-replay').textContent = 'Execute Replay';
                    document.getElementById('btn-capture-next').classList.remove('hidden');
                }, 2000);
            });
        }

        function loadSessions() {
            var list = document.getElementById('sessions-list');
            list.innerHTML = '<p style="color: #888;">Loading...</p>';
            api('rollback_sessions').then(function(resp) {
                if (!resp.sessions || !resp.sessions.length) {
                    list.innerHTML = '<p style="color: #888; font-size: 12px;">No sessions saved.</p>';
                    return;
                }
                var html = '';
                for (var i = 0; i < resp.sessions.length; i++) {
                    var s = resp.sessions[i];
                    html += '<div class="signal-item"><div><div class="signal-name">' + s.name + '</div><div class="signal-meta">' + s.freq_mhz + ' MHz - ' + s.signal_count + ' signals</div></div><button class="btn btn-primary signal-btn" onclick="replaySession(\'' + s.path + '\')">Replay</button></div>';
                }
                list.innerHTML = html;
            });
        }

        function replaySession(path) {
            api('rollback_replay', {session: path, delay: 0.3});
        }

        function toggleJam() {
            var freq = parseFloat(document.getElementById('jam-freq').value);
            var btn = document.getElementById('btn-jam');
            var status = document.getElementById('jam-status');
            
            if (isJamming) {
                api('ecrf_jam_stop').then(function() {
                    isJamming = false;
                    btn.textContent = 'Start Jammer';
                    btn.classList.remove('btn-secondary');
                    btn.classList.add('btn-danger');
                    status.textContent = 'Jammer stopped';
                });
            } else {
                api('ecrf_jam', {freq: freq, power: '12'}).then(function(resp) {
                    if (resp.status === 'ok') {
                        isJamming = true;
                        btn.textContent = 'Stop Jammer';
                        btn.classList.remove('btn-danger');
                        btn.classList.add('btn-secondary');
                        status.textContent = 'Jamming ' + freq + ' MHz';
                        status.style.color = '#ff6b6b';
                    } else {
                        status.textContent = 'Failed: Connect to Evil Crow WiFi';
                        status.style.color = '#ff4444';
                    }
                });
            }
        }

        function checkStatus() {
            api('status').then(function(resp) {
                document.getElementById('hackrf-status').className = 'status-dot' + (resp.hackrf_connected ? ' online' : '');
                document.getElementById('ecrf-status').className = 'status-dot' + (resp.ecrf_connected ? ' online' : '');
            });
        }

        checkStatus();
        setInterval(checkStatus, 10000);
    </script>
</body>
</html>
)rawliteral";

// ══════════════════════════════════════════════════════════════
//  Serial Communication
// ══════════════════════════════════════════════════════════════

void sendToBackend(const String& cmd, const String& params) {
    currentRequestId++;
    String msg = "{\"cmd\":\"" + cmd + "\",\"params\":" + params + ",\"id\":" + String(currentRequestId) + "}";
    Serial.println(msg);
}

String waitForResponse(unsigned long timeout) {
    unsigned long start = millis();
    String response = "";
    
    while (millis() - start < timeout) {
        if (Serial.available()) {
            char c = Serial.read();
            if (c == '\n') {
                // Skip event messages, keep waiting for actual response
                if (response.indexOf("\"event\"") >= 0) {
                    // This is an event, not a response - broadcast via SSE and continue waiting
                    broadcastSSE(response);
                    response = "";
                    continue;
                }
                return response;
            }
            response += c;
        }
        yield();
    }
    return "";
}

void processSerialInput() {
    static String serialBuffer = "";
    
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n') {
            if (serialBuffer.length() > 0) {
                broadcastSSE(serialBuffer);
                serialBuffer = "";
            }
        } else {
            serialBuffer += c;
            if (serialBuffer.length() > 2048) {
                serialBuffer = "";
            }
        }
    }
}

// ══════════════════════════════════════════════════════════════
//  SSE (Server-Sent Events)
// ══════════════════════════════════════════════════════════════

void handleSSE() {
    WiFiClient client = server.client();
    
    int slot = -1;
    for (int i = 0; i < MAX_SSE_CLIENTS; i++) {
        if (!sseActive[i]) {
            slot = i;
            break;
        }
    }
    
    if (slot == -1) {
        server.send(503, "text/plain", "Too many clients");
        return;
    }
    
    sseClients[slot] = client;
    sseActive[slot] = true;
    
    client.println("HTTP/1.1 200 OK");
    client.println("Content-Type: text/event-stream");
    client.println("Cache-Control: no-cache");
    client.println("Connection: keep-alive");
    client.println();
    client.flush();
}

void broadcastSSE(const String& data) {
    for (int i = 0; i < MAX_SSE_CLIENTS; i++) {
        if (sseActive[i]) {
            if (sseClients[i].connected()) {
                sseClients[i].print("data: ");
                sseClients[i].println(data);
                sseClients[i].println();
                sseClients[i].flush();
            } else {
                sseActive[i] = false;
            }
        }
    }
}

// ══════════════════════════════════════════════════════════════
//  HTTP Handlers
// ══════════════════════════════════════════════════════════════

void handleRoot() {
    server.send(200, "text/html", FPSTR(INDEX_HTML));
}

void handleAPI() {
    if (server.method() != HTTP_POST) {
        server.send(405, "application/json", "{\"error\":\"Method not allowed\"}");
        return;
    }
    
    // Clear any stale data in serial buffer
    while (Serial.available()) {
        Serial.read();
    }
    
    String body = server.arg("plain");
    Serial.println(body);
    Serial.flush();
    
    String response = waitForResponse(30000);
    
    if (response.length() > 0) {
        server.send(200, "application/json", response);
    } else {
        server.send(504, "application/json", "{\"error\":\"Backend timeout\"}");
    }
}

void handleNotFound() {
    server.send(404, "text/plain", "Not Found");
}

// ══════════════════════════════════════════════════════════════
//  Setup & Loop
// ══════════════════════════════════════════════════════════════

void setup() {
    Serial.begin(SERIAL_BAUD);
    delay(500);
    
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH);
    
    WiFi.mode(WIFI_AP);
    WiFi.softAPConfig(localIP, gateway, subnet);
    WiFi.softAP(AP_SSID, AP_PASS, AP_CHANNEL, AP_HIDDEN);
    
    Serial.println();
    Serial.println("=== RF Mobile Controller ===");
    Serial.print("SSID: ");
    Serial.println(AP_SSID);
    Serial.print("IP: ");
    Serial.println(WiFi.softAPIP());
    
    server.on("/", HTTP_GET, handleRoot);
    server.on("/api", HTTP_POST, handleAPI);
    server.on("/events", HTTP_GET, handleSSE);
    server.onNotFound(handleNotFound);
    
    server.begin();
    Serial.println("Ready");
}

void loop() {
    server.handleClient();
    processSerialInput();
    yield();
}
