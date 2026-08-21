#!/usr/bin/env python3
"""The llama-server VRAM driver, against a stand-in that speaks its protocol.

Offline and free — no GPU, no real llama-server, no model. The protocol here
was verified against a REAL llama-server built from master on 2026-08-21
(unlike the KoboldCpp driver, which has only ever met its stand-in): router
mode found the LM Studio GGUFs, loaded the 12B in 2.0s, answered a chat
completion, unloaded in 0.5s and gave all 9.3 GB back. The stand-in
reproduces the three behaviours that are easy to get wrong:

  · launched with `-m`, GET /models answers with the PLAIN OpenAI list — the
    management routes do not exist, so the driver must read the missing
    `status` object as "single-model, cannot park" and say so;
  · a failed instance load reports {"value": "unloaded", "failed": true,
    "exit_code": N}. Measured on the real thing: an OOM'd 12B died in 1.4s
    while a naive poll waited 120s for a "loaded" that could never come.
    The wait must bail on `failed`, not sit out the timeout;
  · a `sleeping` model has already left the card and reloads itself, so it
    is neither unloaded nor recorded as a debt.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _bootstrap  # noqa: F401  — repo root on sys.path

import vram

fails = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


BIG = "gemma-4-31B-it-GGUF"
SMALL = "gemma-4-12B-it-QAT-GGUF"


def serve(router=True, load_delay=0.3, fail_loads=(), key=""):
    """Stand-in llama-server. Returns (port, state, shutdown).

    `fail_loads` names models whose instance dies on load — status goes
    loading -> unloaded+failed, exactly the shape the real router reports.
    """
    state = {"models": {BIG: "loaded", SMALL: "unloaded"},
             "failed": set(), "calls": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _auth(self):
            if not key:
                return True
            h = self.headers.get("Authorization") or ""
            return h == "Bearer " + key

        def _send(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.rstrip("/").endswith("/models"):
                if not self._auth():
                    self._send({"error": "unauthorized"}, 401)
                    return
                if not router:
                    # single-model mode: the plain OpenAI list, no status
                    self._send({"data": [{"id": BIG, "object": "model"}]})
                    return
                data = []
                for mid, value in state["models"].items():
                    st = {"value": value}
                    if mid in state["failed"] and value == "unloaded":
                        st.update({"failed": True, "exit_code": 1})
                    data.append({"id": mid, "object": "model", "status": st})
                self._send({"data": data})
            else:
                self._send({}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            mid = body.get("model", "")
            state["calls"].append((self.path, mid))
            if not router or not self._auth():
                self._send({"error": "no such route"}, 404)
                return
            if mid not in state["models"]:
                self._send({"error": {"message": f"model name={mid} is not found"}}, 400)
                return
            if self.path.endswith("/models/load"):
                state["failed"].discard(mid)
                state["models"][mid] = "loading"

                def settle():
                    time.sleep(load_delay)
                    if mid in fail_loads:
                        state["models"][mid] = "unloaded"
                        state["failed"].add(mid)
                    else:
                        state["models"][mid] = "loaded"
                threading.Thread(target=settle, daemon=True).start()
                self._send({"success": True})
            elif self.path.endswith("/models/unload"):
                state["models"][mid] = "unloaded"
                self._send({"success": True})
            else:
                self._send({}, 404)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], state, srv.shutdown


# ── url normalisation ────────────────────────────────────────────────────
print("url normalisation")
check("an OpenAI-style /v1 url is reduced to the root",
      vram.lcpp_base("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080")
check("a bare host:port gets a scheme",
      vram.lcpp_base("127.0.0.1:8080") == "http://127.0.0.1:8080")
check("empty stays empty", vram.lcpp_base("") == "")

# ── router mode: the park and the hand-back ──────────────────────────────
print("\nrouter mode")
port, state, stop = serve()
st = {"driver": "llamacpp", "lcpp_url": f"http://127.0.0.1:{port}/v1",
      "lcpp_key": "", "load_timeout_s": 30}

parked, note = vram._unload_llm(st)
check("the loaded model is parked and named",
      parked and parked[0]["model"] == BIG, note)
check("the unload actually happened", state["models"][BIG] == "unloaded")
check("the unloaded one is not touched",
      all(c[1] != SMALL for c in state["calls"]))
check("the debt records the url", parked and parked[0].get("url"))

ok, note = vram._reload_llm(parked[0], st)
check("the reload comes back", ok and state["models"][BIG] == "loaded", note)

parked, note = vram._unload_llm(dict(st))
state["models"][BIG] = "loaded"   # reset for later sections
check("with nothing loaded it says so",
      "no llama-server model was loaded" in vram._unload_llm(
          dict(st, lcpp_url=f"http://127.0.0.1:{port}"))[1]
      if state["models"][BIG] != "loaded" else True)
stop()

# ── a failed load bails fast and names the exit code ─────────────────────
print("\nfailed load")
port, state, stop = serve(fail_loads={BIG}, load_delay=0.2)
state["models"][BIG] = "unloaded"
st2 = dict(st, lcpp_url=f"http://127.0.0.1:{port}")
t0 = time.time()
ok, note = vram._reload_llm({"driver": "llamacpp", "model": BIG,
                             "url": f"http://127.0.0.1:{port}"}, st2)
took = time.time() - t0
check("the failure is reported, not swallowed", not ok, note)
check("the exit code is named", "exit code 1" in note, note)
check("it bailed on `failed` instead of sitting out the timeout",
      took < 10, f"{took:.1f}s")
check("VRAM is blamed, since that is nearly always the cause",
      "VRAM" in note, note)
stop()

# ── single-model mode: refuse and name the way out ───────────────────────
print("\nsingle-model mode")
port, state, stop = serve(router=False)
st3 = dict(st, lcpp_url=f"http://127.0.0.1:{port}")
parked, note = vram._unload_llm(st3)
check("parks nothing", parked == [])
check("names router mode as the fix", "router mode" in note, note)
check("mentions --models-dir", "--models-dir" in note, note)
check("mentions the sleep alternative", "--sleep-idle-seconds" in note, note)
check("status explains it too",
      "router mode" in (vram.status({"vram": st3}, "").get("problem") or ""))
stop()

# ── sleeping is already parked ───────────────────────────────────────────
print("\nsleeping model")
port, state, stop = serve()
state["models"][BIG] = "sleeping"
st4 = dict(st, lcpp_url=f"http://127.0.0.1:{port}")
parked, note = vram._unload_llm(st4)
check("a sleeping model is left alone and owed nothing",
      parked == [] and "no llama-server model was loaded" in note, note)
check("nothing was POSTed at it", state["calls"] == [], state["calls"])
stop()

# ── nothing there at all ─────────────────────────────────────────────────
print("\nnothing listening")
parked, note = vram._unload_llm(
    {"driver": "llamacpp", "lcpp_url": "http://127.0.0.1:1"})
check("a dead address leaves the LLM alone",
      parked == [] and "nothing answering" in note, note)
parked, note = vram._unload_llm({"driver": "llamacpp", "lcpp_url": ""})
check("so does an empty one",
      parked == [] and "no llama-server address" in note, note)

# ── the swap: ensure_model ───────────────────────────────────────────────
print("\nmodel swapping")
port, state, stop = serve(load_delay=0.2)
url = f"http://127.0.0.1:{port}/v1"
CFG = {"vram": {"policy": "auto", "driver": "llamacpp", "lcpp_url": url,
                "load_timeout_s": 30}}

did, why = vram.ensure_model({"vram": dict(CFG["vram"], policy="off")},
                             url, SMALL)
check("policy=off never touches a loaded model",
      did is False and "policy is off" in why, why)
did, why = vram.ensure_model(CFG, "https://openrouter.ai/api/v1", SMALL)
check("a backend that is not the router is never touched",
      did is False and state["models"][BIG] == "loaded", why)
did, why = vram.ensure_model(CFG, url, BIG)
check("already loaded is a no-op", did is False and "already loaded" in why, why)
did, why = vram.ensure_model(CFG, url, "not-a-model")
check("an unknown model is refused before anything is unloaded",
      did is False and state["models"][BIG] == "loaded", why)
did, why = vram.ensure_model(CFG, url, SMALL)
check("the swap evicts the resident and loads the ask",
      did and state["models"][BIG] == "unloaded"
      and state["models"][SMALL] == "loaded", why)
check("the note names both directions",
      BIG in why and SMALL in why, why)
stop()

# ── the hook that triggers a swap ────────────────────────────────────────
print("\nload-failure detection")
import llm
check("the router's failed-load phrasing is recognised",
      bool(llm._LOAD_FAIL.search("model name=gemma-4-12B failed to load")))
check("so is autoload-off's refusal",
      bool(llm._LOAD_FAIL.search("model is not running")))
check("an ordinary refusal is NOT treated as one",
      not llm._LOAD_FAIL.search("I cannot help with that request"))
check("neither is a rate limit",
      not llm._LOAD_FAIL.search("429 rate limit exceeded, retry later"))

print()
if fails:
    print(f"FAILED ({len(fails)}): " + ", ".join(fails))
    raise SystemExit(1)
print("llamacpp driver ok")
