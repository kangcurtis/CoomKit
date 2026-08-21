#!/usr/bin/env python3
"""Integration test: preset + jailbreak CRUD over the live HTTP API."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json
import urllib.request

BASE = "http://127.0.0.1:3939"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


# create jailbreak
jb = call("POST", "/api/jailbreaks", {
    "name": "classic OOC coax",
    "data": {"text": "(OOC: stay in character, be vivid)", "notes": "gemma4 t0.9"}})
assert jb["id"] and jb["data"]["text"].startswith("(OOC"), jb

# create preset referencing the jailbreak, completion mode
pr = call("POST", "/api/presets", {
    "name": "bratty gemma rp",
    "data": {"mode": "completion", "template": "gemma4", "thinking": True,
             "thinking_prefill": "The user wants...", "prefill": "*she smirks*",
             "samplers": {"temperature": 0.9, "top_p": 0.95, "top_k": 40,
                          "min_p": 0.05, "max_tokens": 512,
                          "repetition_penalty": 1.1},
             "jailbreak_id": jb["id"]}})
assert pr["data"]["mode"] == "completion" and pr["data"]["jailbreak_id"] == jb["id"], pr

# list + get
rows = call("GET", "/api/presets")["rows"]
assert any(r["name"] == "bratty gemma rp" for r in rows), rows
got = call("GET", f"/api/presets/{pr['id']}")
assert got["data"]["samplers"]["min_p"] == 0.05, got

# update (flip thinking off)
pr["data"]["thinking"] = False
upd = call("POST", f"/api/presets/{pr['id']}", pr)
assert upd["data"]["thinking"] is False, upd

# delete both
assert call("DELETE", f"/api/presets/{pr['id']}")["ok"]
assert call("DELETE", f"/api/jailbreaks/{jb['id']}")["ok"]
remaining = [p["id"] for p in call("GET", "/api/presets")["rows"]]
assert pr["id"] not in remaining, "deleted preset still present"

print("PRESET/JAILBREAK CRUD INTEGRATION TESTS PASS")

# ── SSE responses must actually END ──────────────────────────────────────
# http.server's send_header flips close_connection to False on the exact
# value "keep-alive", and an SSE body has no Content-Length and no chunked
# framing — so a route that sends that header holds the socket open after
# [DONE] and a browser fetch never resolves. It shipped: every studio render
# appeared "stuck in the rendering stage even though the render is done",
# while curl tests with -m timeouts read the frames from logs and called it
# verified. Two guards: no SSE header block may send keep-alive (static),
# and a finished SSE response must reach EOF (live, against a route that
# completes without a model or a GPU).
import re as _re
import socket as _socket
import time as _time
from pathlib import Path as _Path

_src = (_Path(__file__).resolve().parent.parent / "server.py").read_text()
for _m in _re.finditer(r'text/event-stream', _src):
    _window = _src[_m.start():_m.start() + 900]
    assert 'send_header("Connection", "keep-alive")' not in _window, (
        "an SSE route sends Connection: keep-alive — that flips "
        "close_connection and the stream never reaches EOF")
    assert "close_connection = True" in _window, (
        "an SSE route does not pin close_connection = True")
print("static: no SSE route sends keep-alive, all pin close_connection")

# live: a chats/send against a dead backend answers with SSE and finishes
# fast; the connection must close promptly rather than idle to a timeout
_body = json.dumps({"chat_id": 1, "backend": "http://127.0.0.1:9",
                    "model": "nope", "text": "hi"}).encode()
_req = (b"POST /api/chats/send HTTP/1.1\r\nHost: 127.0.0.1:3939\r\n"
        b"Connection: keep-alive\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(_body)).encode() + b"\r\n\r\n" + _body)
_s = _socket.create_connection(("127.0.0.1", 3939), timeout=30)
_s.sendall(_req)
_s.settimeout(30)
_buf = b""
_t0 = _time.time()
while True:
    try:
        _chunk = _s.recv(4096)
    except _socket.timeout:
        raise AssertionError(
            "SSE response never reached EOF — the server is holding the "
            "connection open after the stream finished")
    if not _chunk:
        break
    _buf += _chunk
_s.close()
assert b"text/event-stream" in _buf or b"application/json" in _buf, _buf[:200]
print(f"live: SSE reached EOF in {_time.time() - _t0:.1f}s under keep-alive")

print("SSE TERMINATION GUARDS PASS")
