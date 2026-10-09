"""
Continuous 24/7 Gold Signals Service for Render / Railway.
- Runs an automated background loop that checks gold candles immediately
  (~20 seconds after every 15-minute candle closes at :00, :15, :30, :45).
- Runs a lightweight HTTP health-check server listening on $PORT (required by Render & Railway).
- Exposes:
    GET /         -> Service status & last check details
    GET /check    -> Manually trigger an immediate signal check
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import cloud_check

PORT = int(os.environ.get("PORT", 8080))
OFFSET_SECONDS = int(os.environ.get("CANDLE_OFFSET_SEC", 20))  # seconds after candle close to allow data finalization

last_run_info = {
    "status": "initialized",
    "last_check_time": None,
    "last_error": None,
}


def execute_signal_check():
    """Run cloud_check.main() and record execution status."""
    try:
        now_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        print(f"[{now_str}] Starting 15m signal check...")
        cloud_check.main()
        last_run_info["status"] = "ok"
        last_run_info["last_check_time"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        last_run_info["last_error"] = None
        print(f"[{last_run_info['last_check_time']}] Signal check completed successfully.")
    except Exception as e:
        last_run_info["status"] = "error"
        last_run_info["last_error"] = str(e)
        last_run_info["last_check_time"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        print(f"[{last_run_info['last_check_time']}] Error checking signals: {e}", file=sys.stderr)


def seconds_until_next_candle(offset_sec=20):
    """
    Calculate seconds until the next 15-minute boundary + offset_sec.
    Boundaries: :00, :15, :30, :45.
    """
    now = time.time()
    interval = 15 * 60  # 900 seconds
    next_boundary = ((now // interval) + 1) * interval + offset_sec
    wait_time = next_boundary - now
    if wait_time < 5:
        next_boundary += interval
        wait_time = next_boundary - now
    return wait_time


def worker_loop():
    """Background worker that sleeps until each 15m candle close and checks for signals."""
    print("Worker loop started. Performing initial startup check...")
    execute_signal_check()

    while True:
        wait_seconds = seconds_until_next_candle(OFFSET_SECONDS)
        target_time = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() + wait_seconds))
        print(f"Next candle check scheduled for {target_time} (in {int(wait_seconds)} seconds)...")
        time.sleep(wait_seconds)
        execute_signal_check()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/check":
            execute_signal_check()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"message": "Manual check executed", "info": last_run_info}).encode("utf-8"))
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = {
                "service": "gold-signals-bot",
                "uptime": "active",
                "last_run": last_run_info,
            }
            self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, format, *args):
        # Silence standard HTTP ping access logs to keep stdout clean
        return


def main():
    # Start background signal checking thread
    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()

    # Start HTTP server on PORT
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"HTTP health server listening on port {PORT}...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
