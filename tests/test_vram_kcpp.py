#!/usr/bin/env python3
"""The KoboldCpp VRAM driver, against a stand-in that speaks its protocol.

Offline and free — no GPU, no real KoboldCpp, no model. The mock reproduces
koboldcpp.py's actual behaviour on the four endpoints the driver touches, and
the two bits of it that are easy to get wrong:

  · the admin gate ANSWERS with 200 and simply refuses to act when the server
    was started without --admin, so a driver that trusts a 200 will report
    success and park nothing;
  · reload_config replies BEFORE the swap, then a supervisor process bounces
    the inner process, so the port disappears for a while. Every poll has to
    read a connection error as "still working", not as failure.

What this does NOT prove: that a real KoboldCpp behaves as documented. The
protocol here was read out of koboldcpp.py at concedo; nobody has run this
against the real thing. See CLAUDE.md.
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


MODEL = "koboldcpp/Cydonia-24B-v4.1-Q5_K_M"
OPTIONS = ["cydonia-24b.kcpps", "mistral-small.kcpps", "initial_model",
           "unload_model"]


def serve(admin, password="", gap=0.6):
    """Start a stand-in KoboldCpp. Returns (port, state, shutdown)."""
    state = {"llm": True, "model": MODEL, "down_until": 0.0, "targets": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _auth(self):
            if admin != 2 or not password:
                return True
            h = (self.headers.get("Authorization")
                 or self.headers.get("authorization") or "")
            return h.startswith("Bearer ") and h[7:].strip() == password

        def _send(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if time.time() < state["down_until"]:
                raise ConnectionError("restarting")
            if self.path.endswith("/api/extra/version"):
                self._send({"result": "KoboldCpp", "version": "1.99",
                            "llm": state["llm"], "admin": admin})
            elif self.path.endswith("/api/v1/model"):
                self._send({"result": state["model"] if self._auth()
                            else "koboldcpp/protected-model"})
            elif self.path.endswith("/api/admin/list_options"):
                self._send(OPTIONS if (admin and self._auth()) else [])
            else:
                self._send({})

        def do_POST(self):
            if time.time() < state["down_until"]:
                raise ConnectionError("restarting")
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not self.path.endswith("/api/admin/reload_config"):
                self._send({})
                return
            if not (admin and self._auth()):
                self._send({"success": False})      # answers, refuses to act
                return
            target = body.get("filename", "")
            if target not in OPTIONS:
                self._send({"success": False})
                return
            state["targets"].append(target)
            self._send({"success": True})           # replies, THEN restarts

            def swap():
                state["down_until"] = time.time() + gap
                time.sleep(gap)
                state["llm"] = target != "unload_model"
            threading.Thread(target=swap, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], state, srv.shutdown


# ── url normalisation ────────────────────────────────────────────────────
print("url normalisation")
check("an OpenAI-style /v1 url is reduced to the root",
      vram.kcpp_base("http://127.0.0.1:5001/v1") == "http://127.0.0.1:5001")
check("/api/v1 is stripped whole, not down to /api",
      vram.kcpp_base("http://box.lan:5001/api/v1") == "http://box.lan:5001",
      vram.kcpp_base("http://box.lan:5001/api/v1"))
check("a bare host:port gets a scheme",
      vram.kcpp_base("127.0.0.1:5001") == "http://127.0.0.1:5001")
check("empty stays empty", vram.kcpp_base("") == "")

# ── admin ON, no password ────────────────────────────────────────────────
print("\nadmin mode, no password")
port, state, stop = serve(admin=1)
st = {"driver": "koboldcpp", "kcpp_url": f"http://127.0.0.1:{port}/v1",
      "kcpp_key": "", "load_timeout_s": 30}

parked, note = vram._unload_llm(st)
check("the model is parked and named", parked and parked[0]["model"] == MODEL, note)
check("the unload survives the port dropping", state["llm"] is False)
check("it asked for unload_model", state["targets"] == ["unload_model"])

ok, note = vram._reload_llm(parked[0], st)
check("the reload comes back", ok and state["llm"] is True, note)
check("it matched the parked model to a config rather than guessing",
      state["targets"][-1] == "cydonia-24b.kcpps", state["targets"])
stop()

# ── a model with no matching config falls back to initial_model ──────────
print("\nno config matches the parked model")
port, state, stop = serve(admin=1)
st2 = dict(st, kcpp_url=f"http://127.0.0.1:{port}")
ok, note = vram._reload_llm(
    {"driver": "koboldcpp", "model": "someone/Unrelated-70B",
     "url": f"http://127.0.0.1:{port}"}, st2)
check("falls back to initial_model", ok and state["targets"] == ["initial_model"],
      state["targets"])
stop()

# ── admin OFF: must refuse, and say which knob is wrong ──────────────────
print("\nadmin mode off")
port, state, stop = serve(admin=0)
st3 = dict(st, kcpp_url=f"http://127.0.0.1:{port}")
parked, note = vram._unload_llm(st3)
check("parks nothing", parked == [])
check("names --admin as the fix", "--admin" in note, note)
check("and does NOT unload anything", state["llm"] is True)
check("status explains it too",
      "--admin" in (vram.status({"vram": st3}, "").get("problem") or ""))
stop()

# ── admin + password ─────────────────────────────────────────────────────
print("\nadmin mode with a password")
port, state, stop = serve(admin=2, password="hunter2")
missing = dict(st, kcpp_url=f"http://127.0.0.1:{port}", kcpp_key="")
parked, note = vram._unload_llm(missing)
check("a missing password is caught before anything is touched",
      parked == [] and "adminpassword" in note, note)
check("nothing was unloaded", state["llm"] is True)

wrong = dict(missing, kcpp_key="nope")
parked, note = vram._unload_llm(wrong)
check("a wrong password refuses rather than claiming success",
      parked == [] and state["llm"] is True, note)

right = dict(missing, kcpp_key="hunter2")
parked, note = vram._unload_llm(right)
check("the right password parks it", parked and state["llm"] is False, note)
ok, note = vram._reload_llm(parked[0], right)
check("and hands it back", ok and state["llm"] is True, note)
stop()

# ── nothing there at all ─────────────────────────────────────────────────
print("\nnothing listening")
parked, note = vram._unload_llm(
    {"driver": "koboldcpp", "kcpp_url": "http://127.0.0.1:1"})
check("a dead address leaves the LLM alone",
      parked == [] and "nothing answering" in note, note)
parked, note = vram._unload_llm({"driver": "koboldcpp", "kcpp_url": ""})
check("so does an empty one", parked == [] and "no KoboldCpp address" in note, note)

# ── the LM Studio key bug this round fixed ───────────────────────────────
# `lms ps` reports selectedVariant as "publisher/name@quant" and `lms load`
# rejects that string, so a debt parked by the old build must still be
# loadable — the suffix is stripped rather than passed through.
print("\nLM Studio model keys")
check("a variant suffix is stripped so an old parked debt still loads",
      vram._lms_key({"model": "google/gemma-4-12b-qat@q4_0"})
      == "google/gemma-4-12b-qat")
check("a bare key is left alone",
      vram._lms_key({"model": "google/gemma-4-12b-qat"})
      == "google/gemma-4-12b-qat")
check("it falls back to the identifier",
      vram._lms_key({"identifier": "some/model"}) == "some/model")

# ── model swapping: the LLM-to-LLM half of brokering ─────────────────────
# Only the refusal paths are exercised here. The swap itself unloads and loads
# real models through the lms CLI, which is neither offline nor free.
print("\nmodel swapping")
ON = {"vram": {"policy": "auto", "driver": "lmstudio", "lms_bin": "lms"}}
OFF = {"vram": {"policy": "off", "driver": "lmstudio", "lms_bin": "lms"}}
NODRIVER = {"vram": {"policy": "auto", "driver": "none"}}

did, why = vram.ensure_model(OFF, "http://127.0.0.1:1234/v1", "some/model")
check("policy=off never touches a loaded model",
      did is False and "policy is off" in why, why)
did, why = vram.ensure_model(NODRIVER, "http://127.0.0.1:1234/v1", "some/model")
check("driver=none does nothing", did is False, why)
did, why = vram.ensure_model(ON, "https://openrouter.ai/api/v1", "moonshot/kimi-k3")
check("a remote backend is never unloaded", did is False, why)
check("is_lmstudio spots a LAN install",
      vram.is_lmstudio("http://192.168.1.9:1234/v1"))
check("is_lmstudio does not claim OpenRouter",
      not vram.is_lmstudio("https://openrouter.ai/api/v1"))
check("is_lmstudio does not claim llama-server",
      not vram.is_lmstudio("http://127.0.0.1:8080/v1"))

# the hook that triggers a swap: a full card reads as "failed to load"
import llm
check("a load failure is recognised",
      bool(llm._LOAD_FAIL.search('Failed to load model "google/gemma-4-12b-qat"')))
check("so is an out-of-memory refusal",
      bool(llm._LOAD_FAIL.search("Insufficient GPU memory to load model")))
check("an ordinary refusal is NOT treated as one",
      not llm._LOAD_FAIL.search("I cannot help with that request"))
check("neither is a rate limit",
      not llm._LOAD_FAIL.search("429 rate limit exceeded, retry later"))

print()
if fails:
    print(f"FAILED ({len(fails)}): " + ", ".join(fails))
    raise SystemExit(1)
print("koboldcpp driver ok")
