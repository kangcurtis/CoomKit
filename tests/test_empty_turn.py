#!/usr/bin/env python3
"""A turn that produced nothing is explained, and never stored.

Offline and free: the backend is a stand-in OpenAI-compatible server on
127.0.0.1, so no real model is called and no tokens are spent.

The behaviour being pinned was measured on moonshotai/kimi-k3 through
OpenRouter, which is the most-used cloud model for this. Driven with no
jailbreak it declines by returning an EMPTY completion — zero tokens, no
error, no reasoning to blame it on. Three things went wrong with that:

  1. The blank was stored, so the log grew an empty bubble that reads as
     CoomKit being broken rather than as the model declining.
  2. On a RE-ROLL the blank became a swipe, replacing a take that was fine.
  3. Nothing said a word. The budget-exhaustion retry could not help — it is
     gated on there being reasoning to give room to — so the user got
     silence and no way to tell what happened.

It also pins the other half: a turn that produced only REASONING is still
stored, because the thought is worth keeping and the retry path owns that
case.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _bootstrap  # noqa: F401  — repo root on sys.path

import engine
import server
import testkit
from testkit import call

FAILED = []


def check(label, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"  [{extra}]" if extra and not ok else ""))
    if not ok:
        FAILED.append(label)


def stub_backend(mode):
    """An OpenAI-compatible server that streams `mode` and nothing else.

    mode "empty"    -> a well-formed stream carrying no content at all
    mode "thinking" -> reasoning only, still no visible content
    """
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def frame(delta):
                payload = {"choices": [{"delta": delta, "index": 0}]}
                self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")

            if mode == "thinking":
                frame({"reasoning_content": "I am deliberating at length."})
            # …and in both modes, never any `content`.
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/v1", srv.shutdown


def send(chat_id, backend, **extra):
    """Drive /api/chats/send and collect the frames we care about."""
    import urllib.request
    body = {"chat_id": chat_id, "backend": backend, "model": "stub",
            "mode": "chat", "text": "say something", **extra}
    req = urllib.request.Request(server_url + "/api/chats/send",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    notices, done = [], None
    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if p == "[DONE]":
                break
            d = json.loads(p)
            if d.get("notice"):
                notices.append(d["notice"])
            if d.get("done"):
                done = d
    return notices, done


server_url = "http://127.0.0.1:3939"
char_id = testkit.ensure_character()


def messages_of(chat_id):
    return call("GET", f"/api/chats/{chat_id}")["messages"]


# ── 1. nothing at all: explained, not stored ─────────────────────────────
print("a turn that produced nothing")
backend, stop = stub_backend("empty")
try:
    chat = call("POST", "/api/chats/new",
                {"character_id": char_id, "mode": "rp"})["chat_id"]
    before = len(messages_of(chat))
    notices, done = send(chat, backend)
    after = messages_of(chat)

    check("the user is told what happened",
          any("nothing at all" in n for n in notices),
          f"notices={notices}")
    check("the empty reply is NOT stored",
          len(after) == before + 1,          # the user's own message only
          f"{before} -> {len(after)}")
    check("...and what IS stored is the user's own message",
          after[-1]["role"] == "user")
    check("the done frame carries no message id",
          done and done.get("message_id") is None, done)
    check("the scene is intact — the user can just send again",
          after[-1]["content"] == "say something")

    # ── 2. a re-roll must not replace a good take with a blank ───────────
    print("\na re-roll that produced nothing")
    with server.get_db() as conn:
        engine.add_message(conn, chat, "assistant", "A perfectly good take.")
    good = messages_of(chat)[-1]
    notices, done = send(chat, backend, regenerate=True)
    now = messages_of(chat)[-1]
    check("the original take survives",
          now["content"] == "A perfectly good take.", now.get("content"))
    # `swipes` on the detail route is a COUNT, not a list.
    check("no blank swipe was added",
          (now.get("swipes") or 0) == (good.get("swipes") or 0),
          f"{good.get('swipes')} -> {now.get('swipes')}")
    check("and the re-roll explains itself too",
          any("nothing at all" in n for n in notices), f"notices={notices}")
finally:
    stop()

# ── 3. reasoning-only is still stored: it is not nothing ─────────────────
print("\na turn that produced only reasoning")
backend, stop = stub_backend("thinking")
try:
    chat = call("POST", "/api/chats/new",
                {"character_id": char_id, "mode": "rp"})["chat_id"]
    before = len(messages_of(chat))
    notices, done = send(chat, backend)
    after = messages_of(chat)
    check("the reply IS stored, because the thought is worth keeping",
          len(after) == before + 2, f"{before} -> {len(after)}")
    # `think` is a top-level field on the detail route's message, not under
    # `data` — it is scrubbed on the way out, because it is drawn into the
    # image export.
    check("the thought came with it",
          "deliberating" in (after[-1].get("think") or ""),
          repr(after[-1].get("think")))
    check("the quiet-refusal notice does NOT fire here",
          not any("nothing at all" in n for n in notices), f"notices={notices}")
finally:
    stop()

print()
if FAILED:
    raise SystemExit(f"FAILED ({len(FAILED)}): " + "; ".join(FAILED))
print("test_empty_turn: all sections passed")
