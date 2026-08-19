#!/usr/bin/env python3
"""Integration: /api/comfy/* against a mock ComfyUI HTTP server."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import atexit
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- tiny mock ComfyUI -----------------------------------------------------
MOCK_WF_CAPTURE = {}


class MockComfy(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/system_stats":
            self._json({"devices": [{"name": "NVIDIA RTX 5090 (mock)"}]})
        elif self.path.startswith("/history/"):
            pid = self.path.split("/")[-1]
            self._json({pid: {"outputs": {
                "9": {"images": [{"filename": "out.png", "subfolder": "",
                                  "type": "output"}]}}}})
        elif self.path.startswith("/view"):
            body = b"\x89PNG\r\n\x1a\nFAKEPNGDATA"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"error": "nope"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.path == "/prompt":
            payload = json.loads(raw.decode())
            MOCK_WF_CAPTURE["prompt"] = payload["prompt"]
            self._json({"prompt_id": "abc123"})
        elif self.path == "/upload/image":
            MOCK_WF_CAPTURE["upload_len"] = len(raw)
            self._json({"name": "coomkit_upload.png"})
        else:
            self._json({"error": "nope"}, 404)


mock = ThreadingHTTPServer(("127.0.0.1", 18188), MockComfy)
threading.Thread(target=mock.serve_forever, daemon=True).start()
time.sleep(0.3)

# --- exercise CoomKit's endpoints ------------------------------------------
BASE = "http://127.0.0.1:3939"


def call(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


# point CoomKit at the mock
# Remember what was configured so the suite can hand it back — via atexit, so
# a failing assertion still restores it. Without that, one crashed run leaves
# the config pointing at a dead mock port, and the *next* run then faithfully
# "restores" the mock, permanently breaking a working install.
try:
    with urllib.request.urlopen(BASE + "/api/config", timeout=15) as _r:
        PREV_COMFY_URL = json.loads(_r.read().decode()).get("comfyui_url", "")
except Exception:  # noqa: BLE001
    PREV_COMFY_URL = ""
if "18188" in PREV_COMFY_URL:
    print("WARNING: config still points at the mock from an earlier crashed "
          "run — set your real ComfyUI address again in settings")
    PREV_COMFY_URL = ""


@atexit.register
def _restore_comfy_url():
    try:
        call("/api/config", {"comfyui_url": PREV_COMFY_URL})
        print("restored comfyui_url:", PREV_COMFY_URL or "(none)")
    except Exception as exc:  # noqa: BLE001
        print("WARNING: could not restore comfyui_url:", exc)

call("/api/config", {"comfyui_url": "http://127.0.0.1:18188"})

ping = call("/api/comfy/ping", {})
assert ping["ok"] and "5090" in ping["devices"][0], ping
print("ping OK:", ping["devices"])

WF = {"6": {"class_type": "CLIPTextEncode",
            "inputs": {"text": "{{prompt}}"}},
      "3": {"class_type": "KSampler", "inputs": {"seed": "{{seed}}"}},
      "10": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}}}

slots = call("/api/comfy/slots", {"workflow": WF})
assert slots["slots"] == {"prompt": 1, "seed": 1, "image": 1}, slots
print("slots OK:", slots["slots"])

# save workflow then run it by id with an image upload
row = call("/api/workflows", {"name": "zz-test-comfy-wf", "kind": "image",
                              "data": {"workflow": WF}})


@atexit.register
def _drop_test_workflow():
    """Leaving this row behind makes it the workflow real tool calls use."""
    try:
        req = urllib.request.Request(
            f"{BASE}/api/workflows/{row['id']}", method="DELETE")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        pass
run = call("/api/comfy/run", {"workflow_id": row["id"],
                              "values": {"prompt": "1girl, smile"},
                              "image_b64": "aGVsbG8=",  # 'hello'
                              "image_name": "in.png"})
assert run["ok"] and run["assets"], run
asset = run["assets"][0]
print("run OK, asset:", asset)

# seed was randomised, prompt + image substituted
assert MOCK_WF_CAPTURE["prompt"]["6"]["inputs"]["text"] == "1girl, smile"
assert isinstance(MOCK_WF_CAPTURE["prompt"]["3"]["inputs"]["seed"], int)
assert MOCK_WF_CAPTURE["prompt"]["10"]["inputs"]["image"] == "coomkit_upload.png"
assert MOCK_WF_CAPTURE["upload_len"] > 0

# asset file served
data = urllib.request.urlopen(BASE + asset["url"]).read()
assert data == b"\x89PNG\r\n\x1a\nFAKEPNGDATA"
print("asset fetch OK")

mock.shutdown()
print("ALL COMFY ENDPOINT TESTS PASS")
