#!/usr/bin/env python3
"""E2E tool-call flow vs mock ComfyUI + K3:
chat -> model emits ```tool block -> dialect rewrite -> pending -> approve
(edited prompt) -> comfy run -> asset saved."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import atexit
import base64
import json
import threading
import time
import urllib.request

import testkit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "http://127.0.0.1:3939"
CAPTURE = {}


class MockComfy(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        b = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/system_stats":
            self._json({"devices": [{"name": "mock gpu"}]})
        elif self.path.startswith("/history/"):
            pid = self.path.split("/")[-1]
            self._json({pid: {"outputs": {"9": {"images": [
                {"filename": "x.png", "subfolder": "", "type": "output"}]}}}})
        elif self.path.startswith("/view"):
            b = b"\x89PNG\r\n\x1a\nMOCKIMG"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/prompt":
            CAPTURE["prompt_wf"] = json.loads(raw.decode())["prompt"]
            self._json({"prompt_id": "p1"})
        else:
            self._json({})


mock = ThreadingHTTPServer(("127.0.0.1", 18188), MockComfy)
threading.Thread(target=mock.serve_forever, daemon=True).start()
time.sleep(0.3)


def call(path, body, method="POST"):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


# config: comfy -> mock
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

# store an anima-ish workflow with slots
WF = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}},
      "3": {"class_type": "KSampler", "inputs": {"seed": "{{seed}}"}}}
# Namespaced and cleaned up. A row called "anima t2i" here is not harmless
# test litter: `_tool_approve` picks a stored workflow by substring, then by
# "first of this kind", so a 2-node stub left behind by the suite silently
# becomes what every real ```tool``` call on this machine renders with — and
# it hides the fact that a fresh install has no rows at all.
_WF_ROW = call("/api/workflows", {"name": "zz-test-tool-wf", "kind": "image",
                                  "data": {"workflow": WF}})


@atexit.register
def _drop_test_workflow():
    try:
        req = urllib.request.Request(
            f"{BASE}/api/workflows/{_WF_ROW['id']}", method="DELETE")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        pass

# card + chat
cid = testkit.ensure_character()
chat_id = call("/api/chats/new", {"character_id": cid})["chat_id"]

# ask K3 to make an image — she should emit a tool block
body = {"chat_id": chat_id, "backend": "https://openrouter.ai/api/v1",
        "model": "moonshotai/kimi-k3",
        "text": "show me what you look like — generate an image of yourself, use a tool block",
        "samplers": {"max_tokens": 1500, "temperature": 0.7}}
req = urllib.request.Request(BASE + "/api/chats/send",
                             data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
pending = None
with urllib.request.urlopen(req, timeout=600) as resp:
    for line in resp:
        line = line.decode().strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if "error" in chunk:
            raise SystemExit("ERROR: " + chunk["error"])
        if chunk.get("tool_pending"):
            pending = ("tool", chunk["tool_pending"])
        # She may name a recipe instead of writing a prompt, which the studio
        # path handles. Both are real tool calls; the test accepts either.
        if chunk.get("studio_pending"):
            pending = ("studio", chunk["studio_pending"])

if not pending:
    # No fabricated fallback here. Pending calls live in the *server*
    # process, so registering one from the test process produced an id the
    # server had never heard of and the approve 404'd — a failure that said
    # nothing about the code under test.
    print("model did not emit a tool block this run (allowed to happen); "
          "nothing to approve, skipping the rest")
    mock.shutdown()
    raise SystemExit(0)

kind, pending = pending
if kind == "tool":
    print("TOOL CALL DETECTED:", pending["call"].get("action"))
    print("rewritten prompt:", pending["prompt"][:200])
    edited = pending["prompt"] + ", masterpiece quality"
    res = call("/api/tools/approve", {"id": pending["id"], "prompt": edited})
else:
    print("RECIPE CALL DETECTED:", pending["recipe"], "->", pending["label"])
    values = dict(pending["values"])
    if values.get("prompt"):
        edited = values["prompt"] + ", masterpiece quality"
        values["prompt"] = edited
    else:
        edited = next(iter(values.values()), "")
    res = call("/api/studio/approve", {"id": pending["id"], "values": values})
assert res["ok"] and res["assets"], res
# The legacy path drives the workflow this test registered, so the prompt
# lands on its node 6. The studio path picks a *bundled* workflow instead, so
# assert on the graph as a whole rather than on a node id that only exists in
# one of the two.
sent = CAPTURE["prompt_wf"]
landed = [v for n in sent.values() for v in (n.get("inputs") or {}).values()
          if isinstance(v, str) and edited and edited in v]
print("approved; comfy got prompt:", (landed[0] if landed else "??")[:120])
assert landed, f"the edited prompt never reached comfy: {list(sent)[:6]}"
data = urllib.request.urlopen(BASE + res["assets"][0]["url"]).read()
assert data == b"\x89PNG\r\n\x1a\nMOCKIMG"
print("asset served OK")
mock.shutdown()
print("TOOL CALL E2E PASS")
