#!/usr/bin/env python3
"""
Local reverse proxy for EvilCrowRF h-RAT web panel.

Serves static assets (HTML/CSS/JS) from local SD card copy for instant loading,
and proxies API + WebSocket requests to the ESP32 at 192.168.4.1.

Usage:
  1. Connect your laptop to the ECRF WiFi
  2. Run: python3 ecrf_proxy.py
  3. Open http://localhost:8080 in your browser
"""

import http.server
import urllib.request
import urllib.error
import os
import sys
import hashlib
import struct
import base64
import socket
import threading
import select
import signal

ECRF_IP = "192.168.4.1"
ECRF_PORT = 80
LOCAL_PORT = 8080

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_DIR = os.path.join(SCRIPT_DIR, "h-rat-firmware", "sd-files-repo", "SD", "HTML")

PAGE_ROUTES = {
    "/": "index.html",
    "/record": "record.html",
    "/transmit": "transmit.html",
    "/saved": "saved.html",
    "/jammer": "jammer.html",
    "/scanner": "scanner.html",
    "/bruteforcer": "bruteforcer.html",
    "/settings": "settings.html",
    "/analyzer": "analyzer.html",
    "/rolljam": "rolljam.html",
    "/rollback": "rollback.html",
    "/logs": "logs.html",
    "/ecrf": "ecrf.html",
    "/result": "result.html",
    "/404": "404.html",
}

MIME_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
}


def websocket_proxy(client_sock, path):
    """Proxy a WebSocket connection between browser and ESP32."""
    try:
        esp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        esp_sock.settimeout(10)
        esp_sock.connect((ECRF_IP, ECRF_PORT))

        ws_key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {ECRF_IP}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        esp_sock.sendall(handshake.encode())

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = esp_sock.recv(4096)
            if not chunk:
                break
            response += chunk

        header_end = response.find(b"\r\n\r\n") + 4
        remaining = response[header_end:]

        if remaining:
            try:
                client_sock.sendall(remaining)
            except Exception:
                pass

        esp_sock.settimeout(None)
        esp_sock.setblocking(False)
        client_sock.setblocking(False)

        while True:
            readable, _, exceptional = select.select(
                [client_sock, esp_sock], [], [client_sock, esp_sock], 30
            )
            if exceptional:
                break
            if not readable:
                continue
            for sock in readable:
                try:
                    data = sock.recv(4096)
                    if not data:
                        return
                    target = esp_sock if sock is client_sock else client_sock
                    target.sendall(data)
                except Exception:
                    return
    except Exception as e:
        print(f"  WebSocket proxy error: {e}")
    finally:
        try:
            esp_sock.close()
        except Exception:
            pass


class ECRFProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        msg = format % args
        if "/ws" not in self.path:
            print(f"  {msg}")

    def do_GET(self):
        path_no_query = self.path.split("?")[0]

        if path_no_query == "/ws":
            self._handle_websocket()
            return

        local_file = self._resolve_local_file(path_no_query)
        if local_file:
            self._serve_local_file(local_file, path_no_query)
        else:
            self._proxy_to_esp32()

    def do_POST(self):
        self._proxy_to_esp32()

    def _resolve_local_file(self, path):
        if path in PAGE_ROUTES:
            candidate = os.path.join(HTML_DIR, PAGE_ROUTES[path])
            if os.path.isfile(candidate):
                return candidate

        stripped = path.lstrip("/")
        if stripped:
            candidate = os.path.join(HTML_DIR, stripped)
            if os.path.isfile(candidate):
                return candidate

        return None

    def _serve_local_file(self, filepath, url_path):
        ext = os.path.splitext(filepath)[1].lower()
        content_type = MIME_TYPES.get(ext, "application/octet-stream")

        try:
            with open(filepath, "rb") as f:
                content = f.read()

            if os.path.basename(filepath) == "main.js":
                content = self._patch_main_js(content)

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(content))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, str(e))

    def _patch_main_js(self, content):
        """Rewrite WebSocket URL to point directly to ESP32."""
        original = b'var webSocketUrl = "ws:\\/\\/" + window.location.hostname + "/ws";'
        patched = f'var webSocketUrl = "ws://{ECRF_IP}/ws";'.encode()
        if original in content:
            content = content.replace(original, patched)
        else:
            original_alt = b'var webSocketUrl = "ws://" + window.location.hostname + "/ws";'
            if original_alt in content:
                content = content.replace(original_alt, patched)
        return content

    def _proxy_to_esp32(self):
        url = f"http://{ECRF_IP}:{ECRF_PORT}{self.path}"
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            req = urllib.request.Request(url, data=body, method=self.command)
            for key in self.headers:
                if key.lower() not in ("host", "connection"):
                    req.add_header(key, self.headers[key])
            req.add_header("Host", ECRF_IP)

            with urllib.request.urlopen(req, timeout=15) as response:
                resp_body = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(key, value)
                self.send_header("Content-Length", len(resp_body))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            self.send_error(e.code, str(e.reason))
        except Exception as e:
            self.send_error(502, f"ESP32 unreachable: {e}")

    def _handle_websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.send_error(400, "Not a WebSocket request")
            return

        accept_val = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-5AB9B54DA35E").encode()).digest()
        ).decode()

        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_val}\r\n"
            "\r\n"
        )
        self.wfile.write(response.encode())
        self.wfile.flush()

        websocket_proxy(self.request, "/ws")

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            pass


class ThreadedHTTPServer(http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request, client_address):
        t = threading.Thread(target=self.process_request_thread, args=(request, client_address))
        t.daemon = True
        t.start()

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def main():
    if not os.path.isdir(HTML_DIR):
        print(f"ERROR: HTML directory not found at {HTML_DIR}")
        print("Make sure the SD card files are in h-rat-firmware/sd-files-repo/SD/HTML/")
        sys.exit(1)

    print(f"EvilCrowRF Local Proxy")
    print(f"  Static files: {HTML_DIR}")
    print(f"  ESP32 target: http://{ECRF_IP}:{ECRF_PORT}")
    print(f"  Local server: http://localhost:{LOCAL_PORT}")
    print()
    print("Connect to ECRF WiFi, then open http://localhost:8080 in your browser.")
    print("Press Ctrl+C to stop.\n")

    server = ThreadedHTTPServer(("", LOCAL_PORT), ECRFProxyHandler)
    signal.signal(signal.SIGINT, lambda s, f: (server.shutdown(), sys.exit(0)))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
