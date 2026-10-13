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
OFFSET_SECONDS = int(os.environ.get("CANDLE_OFFSET_SEC", 15))  # seconds after candle close to start first check

last_run_info = {
    "status": "initialized",
    "last_check_time": None,
    "last_check_epoch": 0,
    "last_candle": None,
    "last_error": None,
}


def execute_signal_check():
    """Run cloud_check.main(force=True) and record execution status."""
    try:
        now_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        print(f"[{now_str}] Starting 15m signal check...")
        res = cloud_check.main(force=True)
        last_run_info["status"] = "ok"
        last_run_info["last_check_time"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        last_run_info["last_check_epoch"] = int(time.time())
        if isinstance(res, dict) and "last_candle" in res:
            last_run_info["last_candle"] = res["last_candle"]
        last_run_info["last_error"] = None
        print(f"[{last_run_info['last_check_time']}] Signal check completed successfully.")
        return res
    except Exception as e:
        last_run_info["status"] = "error"
        last_run_info["last_error"] = str(e)
        last_run_info["last_check_time"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        last_run_info["last_check_epoch"] = int(time.time())
        print(f"[{last_run_info['last_check_time']}] Error checking signals: {e}", file=sys.stderr)
        return {"status": "error", "error": str(e)}


def check_candle_with_retry(max_retries=5, retry_delay=15):
    """
    Check candles immediately upon close. If Yahoo Finance has not finalized the
    latest closed candle yet, retry every 15s (up to ~75s) until it appears.
    This guarantees alerts are sent within 30-75s of candle closing.
    """
    now = time.time()
    interval = 15 * 60  # 900 seconds
    expected_candle_open = int((now // interval) * interval - interval)

    for attempt in range(1, max_retries + 1):
        res = execute_signal_check()
        last_ts = res.get("last_candle_ts") if isinstance(res, dict) else None
        if last_ts is not None and last_ts >= expected_candle_open:
            print(f"Candle data confirmed up-to-date on attempt {attempt}.")
            return res
        if attempt < max_retries:
            print(f"[Attempt {attempt}/{max_retries}] Latest candle not finalized on Yahoo yet. Retrying in {retry_delay}s...")
            time.sleep(retry_delay)
    return None


def seconds_until_next_candle(offset_sec=15):
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
    """Background worker that checks immediately when each 15m candle closes with rapid retry."""
    print("Worker loop started. Performing initial startup check...")
    execute_signal_check()

    while True:
        wait_seconds = seconds_until_next_candle(OFFSET_SECONDS)
        target_time = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() + wait_seconds))
        print(f"Next candle check scheduled for {target_time} (in {int(wait_seconds)} seconds)...")
        time.sleep(wait_seconds)
        check_candle_with_retry()


class HealthHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code, data):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self):
        if self.path == "/check":
            execute_signal_check()
            self._send_json(200, {"message": "Manual check executed", "info": last_run_info})
        elif self.path == "/test-signal":
            try:
                res = cloud_check.send_last_signal()
                self._send_json(200, {"message": "Last signal sent to Telegram", "signal": res})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        elif self.path == "/test":
            try:
                cloud_check.send("🔔 Gold Signals Bot: Test alert from Railway is working successfully!")
                self._send_json(200, {"message": "Test ping sent to Telegram successfully"})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        elif self.path == "/debug" or self.path == "/debug-telegram":
            try:
                debug_info = cloud_check.debug_telegram()
                self._send_json(200, debug_info)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        elif self.path == "/state":
            try:
                st = cloud_check.load_state()
                self._send_json(200, st)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        else:
            response = {
                "service": "gold-signals-bot",
                "uptime": "active",
                "endpoints": {
                    "/": "Service status & last check details",
                    "/check": "Run manual check right now",
                    "/test-signal": "Send the most recent trading signal to Telegram (replay/test)",
                    "/test": "Send a simple test ping message to Telegram",
                    "/debug": "Debug Telegram bot token, chat ID, and see recent incoming chats",
                    "/state": "View current bot state (last_signal_ts, heartbeat)",
                },
                "last_run": last_run_info,
            }
            self._send_json(200, response)

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
