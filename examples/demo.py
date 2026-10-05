"""Real local request, upstream shutdown, offline replay; no external network."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.request import urlopen
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contractdock.core import record, replay_server

class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        payload = json.dumps({"players": [{"name": "mage", "level": 7}], "token": "synthetic-private-value"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
thread = threading.Thread(target=upstream.serve_forever, daemon=True)
thread.start()
origin = f"http://127.0.0.1:{upstream.server_port}"
try:
    packet = record(origin + "/players", allowed_origins=[origin])
finally:
    upstream.shutdown()
    upstream.server_close()
    thread.join()
with replay_server([packet]) as replay:
    worker = threading.Thread(target=replay.serve_forever, daemon=True)
    worker.start()
    try:
        with urlopen(f"http://127.0.0.1:{replay.server_port}/players", timeout=2) as response:
            value = json.load(response)
        assert value["players"][0]["level"] == 7 and value["token"] == "[REDACTED]"
        print(json.dumps({"upstream_stopped": True, "offline_replay_level": 7, "token_redacted": True}))
    finally:
        replay.shutdown()
        worker.join()
