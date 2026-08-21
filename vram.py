#!/usr/bin/env python3
"""CoomKit VRAM orchestrator — one GPU, two hungry tenants.

A 12B chat model with a long context and a video model do not fit in 32 GB at
the same time. Every local-first setup hits this the moment the character tries
to send a picture: the generation OOMs, or ComfyUI silently swaps to CPU and a
four-second clip takes nine minutes.

So CoomKit brokers the card. Before a heavy ComfyUI job it asks the LLM server
to let go; after the job it hands the card back. Both halves are optional and
both are reversible — with `policy: "off"` nothing here ever runs, which is the
right setting for anyone with two GPUs or 80 GB of one.

Drivers:
  lmstudio   `lms` CLI (load/unload) + REST for state. Ships with LM Studio.
  llamacpp   llama-server in ROUTER mode: POST /models/unload + /models/load.
  koboldcpp  admin-mode reload_config with the two special targets.
  command    user-supplied shell commands, for TabbyAPI/vLLM/whatever.
  none       never unload the LLM (default).

ComfyUI is asked to release with POST /free {unload_models, free_memory} —
a core endpoint, no custom nodes needed.

Nothing in here raises on failure. A GPU that refuses to be tidied is not a
reason to refuse to generate; the caller gets a report of what happened and the
job runs anyway.
"""
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

GB = 1024 ** 3

DEFAULTS = {
    "policy": "off",          # off | auto | always
    "driver": "none",         # none | lmstudio | koboldcpp | command
    "headroom_gb": 2.0,       # slack on top of a job's declared need
    "restore": True,          # reload the LLM when the job is done
    "free_comfy_after": True,  # ask ComfyUI to drop its models afterwards
    "lms_bin": "lms",
    "kcpp_url": "http://127.0.0.1:5001",   # driver=koboldcpp
    "kcpp_key": "",           # driver=koboldcpp; --adminpassword, if set
    "lcpp_url": "http://127.0.0.1:8080",   # driver=llamacpp (router mode)
    "lcpp_key": "",           # driver=llamacpp; --api-key, if set
    "unload_cmd": "",         # driver=command
    "load_cmd": "",           # driver=command; {model} {context} substituted
    "load_timeout_s": 300,
}


def settings(cfg: dict) -> dict:
    """Merge the user's `vram` config block over the defaults."""
    out = dict(DEFAULTS)
    out.update(cfg.get("vram") or {})
    return out


# --------------------------------------------------------------------------
# ComfyUI side
# --------------------------------------------------------------------------

def comfy_stats(url: str, timeout: int = 8) -> dict:
    """Return {vram_total_gb, vram_free_gb, torch_free_gb, name} or {}."""
    base = (url or "").strip().rstrip("/")
    if not base:
        return {}
    if "://" not in base:
        base = "http://" + base
    try:
        with urllib.request.urlopen(base + "/system_stats", timeout=timeout) as r:
            stats = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return {}
    devices = stats.get("devices") or []
    if not devices:
        return {}
    d = devices[0]
    return {
        "name": d.get("name", "?"),
        "vram_total_gb": round(d.get("vram_total", 0) / GB, 2),
        "vram_free_gb": round(d.get("vram_free", 0) / GB, 2),
        "torch_free_gb": round(d.get("torch_vram_free", 0) / GB, 2),
    }


def comfy_free(url: str, unload_models: bool = True, free_memory: bool = True,
               timeout: int = 20) -> bool:
    """POST /free — ask ComfyUI to drop cached models and empty its cache.

    The flags are consumed by the queue worker, so the memory does not come
    back on the same tick. Callers that need the space immediately should
    settle for a moment afterwards; `wait_for_free` does that properly.
    """
    base = (url or "").strip().rstrip("/")
    if not base:
        return False
    if "://" not in base:
        base = "http://" + base
    payload = json.dumps({"unload_models": unload_models,
                          "free_memory": free_memory}).encode()
    req = urllib.request.Request(base + "/free", data=payload,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def wait_for_free(url: str, want_gb: float, timeout_s: float = 25.0,
                  poll: float = 0.75, patience: int = 3) -> float:
    """Poll /system_stats until `want_gb` is free. Returns the free GB seen.

    Releasing is asynchronous on both sides — ComfyUI defers to its queue
    worker, and a CUDA context does not shrink the instant a process exits.
    Polling is the only honest way to know.

    It also stops as soon as the number *stops climbing*, which matters more
    than the timeout: a job asking for more than the card can ever give would
    otherwise sit out the full timeout before every single generation. Three
    flat polls means everyone who is going to let go already has.
    """
    deadline = time.time() + timeout_s
    best = 0.0
    flat = 0
    while True:
        free = comfy_stats(url).get("vram_free_gb", 0.0)
        if free > best + 0.05:
            flat = 0
        else:
            flat += 1
        best = max(best, free)
        if free >= want_gb or flat >= patience or time.time() >= deadline:
            return best
        time.sleep(poll)


# --------------------------------------------------------------------------
# LM Studio driver
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# KoboldCpp driver
#
# KoboldCpp will free its model and keep serving, but ONLY in admin mode:
# `--admin --admindir <dir>` (optionally `--adminpassword`). Without it the
# endpoints below answer, they just refuse to do anything, so the driver has to
# check `admin` in /api/extra/version rather than trust a 200.
#
# The two special targets are the whole reason this is worth having:
#   {"filename": "unload_model"}  frees the model, server stays up
#   {"filename": "initial_model"} reloads whatever it was LAUNCHED with
# so the reload is faithful by construction — context, quant, layer split and
# all — without CoomKit having to capture any of it. That is what the generic
# `command` driver cannot do.
#
# Reloading is not synchronous. A supervisor process terminates the inner
# process and starts a new one, so the port DROPS and comes back; every poll
# here has to treat a connection error as "still working", not as failure.
# --------------------------------------------------------------------------

def _kcpp_norm(s: str) -> str:
    """Alphanumerics only, lowercased — for comparing a config filename to a
    model name across their different punctuation habits."""
    return "".join(c for c in (s or "").lower() if c.isalnum())


def kcpp_base(url: str) -> str:
    """Normalise a KoboldCpp URL to its root.

    The admin API lives at the root, but the URL a user has to hand is the
    OpenAI-compatible one they pasted into the backend box, which ends in
    /v1. Strip it rather than make them keep two copies of the same address.
    """
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if "://" not in base:
        base = "http://" + base
    for tail in ("/api/v1", "/v1"):     # longest first, or /api/v1 leaves /api
        if base.endswith(tail):
            base = base[: -len(tail)]
            break
    return base.rstrip("/")


def _kcpp_call(base: str, path: str, key: str = "", payload=None,
               timeout: int = 10):
    if not base:
        return None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001  — a restart in progress lands here too
        return None


def kcpp_caps(base: str, key: str = "", timeout: int = 6) -> dict:
    """/api/extra/version. `admin` is 0 (off), 1 (on), 2 (on, password)."""
    return _kcpp_call(base, "/api/extra/version", key, timeout=timeout) or {}


def kcpp_model(base: str, key: str = "", timeout: int = 6) -> str:
    r = _kcpp_call(base, "/api/v1/model", key, timeout=timeout) or {}
    name = r.get("result") or ""
    # what it says when the password did not match — never a real model
    return "" if name == "koboldcpp/protected-model" else name


def kcpp_options(base: str, key: str = "", timeout: int = 6) -> list:
    r = _kcpp_call(base, "/api/admin/list_options", key, timeout=timeout)
    return r if isinstance(r, list) else []


def kcpp_reload(base: str, target: str, key: str = "",
                timeout: int = 20) -> bool:
    r = _kcpp_call(base, "/api/admin/reload_config", key,
                   payload={"filename": target}, timeout=timeout)
    return bool(r and r.get("success"))


def kcpp_wait(base: str, want_llm: bool, key: str = "",
              timeout_s: float = 300.0, poll: float = 1.5) -> bool:
    """Poll until the model is (un)loaded. The port drops mid-restart."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        caps = kcpp_caps(base, key, timeout=4)
        if caps and bool(caps.get("llm")) == want_llm:
            return True
        time.sleep(poll)
    return False


# --------------------------------------------------------------------------
# llama.cpp llama-server driver
#
# llama-server grew real model management: launched with NO -m it runs as a
# ROUTER that spawns one instance per model, and `POST /models/load` /
# `POST /models/unload` start and stop them over HTTP. The instance's args
# (context, offload, mmproj) live server-side — in the router's --models-dir
# scan or --models-preset INI — so a reload is faithful by construction, the
# same trust the KoboldCpp driver puts in `initial_model`. Nothing has to be
# captured, which is what the generic `command` driver cannot offer.
#
# Launched the classic way (`-m model.gguf`) those routes DO NOT EXIST — the
# only parking is the automatic `--sleep-idle-seconds` timer. The driver
# detects that shape and says so instead of pretending: GET /models answers in
# both modes, but only router entries carry a `status` object.
#
# Three protocol facts the code leans on, all verified against a live build
# (2026-08-21):
#   · a failed instance load reports {"value": "unloaded", "failed": true,
#     "exit_code": N} — the wait must bail on `failed`, not sit out the
#     timeout (measured: an OOM'd 12B died in 1.4s; the naive poll waited
#     120s for a "loaded" that could never come);
#   · GET /models is exempt from the idle timer, so polling it neither wakes
#     a sleeping model nor postpones anyone's sleep;
#   · a `sleeping` model has already left the card (the instance destroys its
#     model and KV on sleep) and reloads ITSELF on the next request — so it
#     is skipped, never unloaded, and never recorded as a debt.
# --------------------------------------------------------------------------

def lcpp_base(url: str) -> str:
    """Normalise a llama-server URL to its root.

    Same job as kcpp_base: the URL the user has to hand is the OpenAI one
    ending in /v1, the management API lives at the root.
    """
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if "://" not in base:
        base = "http://" + base
    if base.endswith("/v1"):
        base = base[:-3]
    return base.rstrip("/")


def _lcpp_call(base: str, path: str, key: str = "", payload=None,
               timeout: int = 10):
    """GET (payload None) or POST JSON. Returns parsed body, or None if the
    server did not answer / did not send JSON."""
    if not base:
        return None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def lcpp_models(base: str, key: str = "", timeout: int = 6):
    """GET /models. Returns the entry list, or None when nothing answered.
    The None/[] distinction is load-bearing: a dead server must read as
    "left the LLM alone", not as "no model was loaded"."""
    r = _lcpp_call(base, "/models", key, timeout=timeout)
    if not isinstance(r, dict) or not isinstance(r.get("data"), list):
        return None
    return r["data"]


def lcpp_is_router(models: list) -> bool:
    """Router entries carry a status object; single-model /models is the
    plain OpenAI list. Decided over the raw entries, same discipline as the
    lorebook keyless check."""
    return any(isinstance(m, dict) and isinstance(m.get("status"), dict)
               and "value" in m["status"] for m in (models or []))


def _lcpp_status(m: dict) -> str:
    st = m.get("status")
    return (st.get("value") or "") if isinstance(st, dict) else ""


def lcpp_loaded(models: list) -> list[str]:
    """Ids currently occupying (or about to occupy) the card."""
    return [m.get("id", "") for m in (models or [])
            if _lcpp_status(m) in ("loaded", "loading") and m.get("id")]


def lcpp_wait(base: str, model: str, want_loaded: bool, key: str = "",
              timeout_s: float = 300.0, poll: float = 1.0) -> tuple[bool, str]:
    """Poll GET /models until `model` settles. Bails early when the instance
    DIED rather than waiting for a state it can never reach."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        models = lcpp_models(base, key, timeout=4)
        if models is not None:
            entry = next((m for m in models if m.get("id") == model), None)
            if entry is None:
                return False, f"llama-server no longer lists {model}"
            st = entry.get("status") or {}
            value = st.get("value", "")
            if want_loaded and st.get("failed"):
                code = st.get("exit_code")
                return False, (f"{model} failed to load"
                               + (f" (exit code {code})" if code is not None
                                  else "")
                               + " — probably not enough free VRAM")
            if want_loaded and value == "loaded":
                return True, "loaded"
            if not want_loaded and value in ("unloaded", "sleeping"):
                return True, "unloaded"
        time.sleep(poll)
    return False, f"timed out waiting for {model} to "\
                  f"{'load' if want_loaded else 'unload'}"


def lcpp_load(base: str, model: str, key: str = "", timeout: int = 20) -> bool:
    r = _lcpp_call(base, "/models/load", key, payload={"model": model},
                   timeout=timeout)
    return bool(r and r.get("success"))


def lcpp_unload(base: str, model: str, key: str = "",
                timeout: int = 20) -> bool:
    r = _lcpp_call(base, "/models/unload", key, payload={"model": model},
                   timeout=timeout)
    return bool(r and r.get("success"))


_LCPP_SINGLE = ("llama-server is running single-model (-m); it can only park "
                "in router mode — start it with no -m and point --models-dir "
                "at your GGUF folder (each model in its own subfolder, or "
                "loose .gguf files), or give it --sleep-idle-seconds N and it "
                "parks itself when idle")


def _run(cmd: list, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def lms_available(bin_path: str = "lms") -> bool:
    return bool(shutil.which(bin_path))


def lms_ps(bin_path: str = "lms") -> list[dict]:
    """Currently-loaded models, with the context length they were loaded at.

    That context length is the whole reason we shell out instead of reading
    /api/v0/models: reloading a model at its default context instead of the
    20k the user chose silently truncates every later chat.
    """
    code, out = _run([bin_path, "ps", "--json"], timeout=30)
    if code != 0:
        return []
    try:
        data = json.loads(out.strip() or "[]")
    except json.JSONDecodeError:
        # `lms` occasionally prefixes a banner line; take the JSON tail.
        start = out.find("[")
        if start < 0:
            return []
        try:
            data = json.loads(out[start:])
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []


def lms_unload_all(bin_path: str = "lms") -> tuple[bool, str]:
    code, out = _run([bin_path, "unload", "--all"], timeout=90)
    return code == 0, out.strip()


def lms_load(model_key: str, context_length: int = 0, gpu: str = "",
             ttl_s: int = 0, bin_path: str = "lms", identifier: str = "",
             parallel: int = 0, timeout: int = 300) -> tuple[bool, str]:
    """Load a model back.

    `gpu` defaults to EMPTY, not "max". Passing --gpu overwrites the offload
    ratio LM Studio remembered for that model — so parking a model the user
    had deliberately set to a partial offload used to hand it back fully
    offloaded, which is a settings change they never asked for. Omitting the
    flag lets LM Studio pick, which is its documented default.
    """
    cmd = [bin_path, "load", model_key, "-y"]
    if context_length:
        cmd += ["-c", str(int(context_length))]
    if gpu:
        cmd += ["--gpu", gpu]
    if ttl_s:
        cmd += ["--ttl", str(int(ttl_s))]
    if parallel:
        cmd += ["--parallel", str(int(parallel))]
    if identifier and identifier != model_key:
        cmd += ["--identifier", identifier]
    code, out = _run(cmd, timeout=timeout)
    return code == 0, out.strip()


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

_lock = threading.RLock()
_parked: list[dict] = []   # models CoomKit unloaded and owes the user back

# The debt outlives the process. If CoomKit is restarted (or crashes) between
# unloading someone's chat model and putting it back, an in-memory list means
# nobody ever remembers to reload it — and the symptom is LM Studio quietly
# JIT-loading at its *default* context instead of the 20k they chose, which
# truncates every later chat without saying anything.
_PARKED_FILE = Path(__file__).resolve().parent / "data" / "vram-parked.json"


def _save_parked() -> None:
    try:
        _PARKED_FILE.parent.mkdir(exist_ok=True)
        _PARKED_FILE.write_text(json.dumps(_parked, indent=1))
    except OSError:
        pass


def _load_parked() -> None:
    if _parked or not _PARKED_FILE.exists():
        return
    try:
        data = json.loads(_PARKED_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(data, list):
        _parked.extend(d for d in data if isinstance(d, dict))


def parked() -> list[dict]:
    with _lock:
        _load_parked()
        return list(_parked)


def _unload_llm(st: dict) -> tuple[list[dict], str]:
    """Unload the local LLM. Returns (what_was_unloaded, note)."""
    driver = st.get("driver", "none")
    if driver == "lmstudio":
        binp = st.get("lms_bin", "lms")
        if not lms_available(binp):
            return [], f"{binp} not on PATH — left the LLM alone"
        loaded = lms_ps(binp)
        if not loaded:
            return [], "no LM Studio model was loaded"
        ok, out = lms_unload_all(binp)
        if not ok:
            return [], f"lms unload failed: {out[:200]}"
        # `lms unload --all` one line up evicts embedding models too, so
        # capturing only type=='llm' means they never come back.
        keep = []
        for m in loaded:
            ttl_ms = m.get("ttlMs")
            keep.append({
                "driver": "lmstudio",
                "kind": m.get("type", "llm"),
                # MEASURED, and the opposite of what this used to do: `lms ps`
                # reports selectedVariant as "publisher/name@quant", and
                # `lms load` REJECTS that string outright — "No model found
                # that matches model key". Only the bare modelKey loads. The
                # old code captured the variant, so every single reload failed
                # with the model left unloaded and the debt stuck in
                # data/vram-parked.json. Verified against LM Studio's own
                # `lms load --estimate-only`: the two @-forms are refused, the
                # bare key is accepted.
                "model": (m.get("modelKey") or m.get("identifier", "")),
                # kept only to notice if a multi-variant model comes back as a
                # different quant — `lms load -y` takes "the first matching
                # model", which is a real hazard we can report but not prevent.
                "variant": m.get("selectedVariant", ""),
                "identifier": m.get("identifier", ""),
                "context": int(m.get("contextLength") or 0),
                # lms ps reports MILLISECONDS; `lms load --ttl` takes SECONDS.
                # Named ttl_s so the next reader cannot get it backwards.
                "ttl_s": int(ttl_ms / 1000) if ttl_ms else 0,
                "parallel": int(m.get("parallel") or 0),
            })
        names = ", ".join(k["model"] for k in keep) or "nothing"
        return keep, f"unloaded {names}"
    if driver == "llamacpp":
        base = lcpp_base(st.get("lcpp_url", ""))
        key = st.get("lcpp_key", "")
        if not base:
            return [], "no llama-server address set"
        models = lcpp_models(base, key)
        if models is None:
            return [], f"nothing answering at {base} — left the LLM alone"
        if not lcpp_is_router(models):
            return [], _LCPP_SINGLE
        loaded = lcpp_loaded(models)
        if not loaded:
            return [], "no llama-server model was loaded"
        parked_now, failed = [], []
        for mid in loaded:
            if not lcpp_unload(base, mid, key):
                failed.append(mid)
                continue
            ok, why = lcpp_wait(base, mid, False, key, timeout_s=60)
            if ok:
                parked_now.append({"driver": "llamacpp", "model": mid,
                                   "url": base})
            else:
                failed.append(f"{mid} ({why})")
        note = "unloaded " + (", ".join(p["model"] for p in parked_now)
                              or "nothing")
        if failed:
            note += " — could not unload " + ", ".join(failed)
        return parked_now, note
    if driver == "koboldcpp":
        base = kcpp_base(st.get("kcpp_url", ""))
        if not base:
            return [], "no KoboldCpp address set"
        caps = kcpp_caps(base, st.get("kcpp_key", ""))
        if not caps:
            return [], f"nothing answering at {base} — left the LLM alone"
        if not caps.get("admin"):
            return [], ("KoboldCpp is not in admin mode — restart it with "
                        "--admin --admindir <folder> and it can park its model")
        if caps.get("admin") == 2 and not st.get("kcpp_key"):
            return [], "KoboldCpp wants its --adminpassword, and none is set"
        if not caps.get("llm"):
            return [], "no KoboldCpp model was loaded"
        model = kcpp_model(base, st.get("kcpp_key", ""))
        if not kcpp_reload(base, "unload_model", st.get("kcpp_key", "")):
            return [], "KoboldCpp refused the unload"
        # It frees the model and keeps serving, so this settles fast — but the
        # supervisor may still bounce the port, hence the tolerant wait.
        if not kcpp_wait(base, False, st.get("kcpp_key", ""), timeout_s=120):
            return [], "KoboldCpp did not report the model gone"
        return [{"driver": "koboldcpp", "model": model, "url": base}], \
               f"unloaded {model or 'the KoboldCpp model'}"
    if driver == "command":
        cmd = st.get("unload_cmd", "")
        if not cmd:
            return [], "driver=command but no unload_cmd set"
        code, out = _run(["sh", "-c", cmd], timeout=120)
        if code != 0:
            return [], f"unload_cmd failed: {out[:200]}"
        return [{"driver": "command"}], "ran unload_cmd"
    return [], "no LLM driver configured"


def _lms_key(entry: dict) -> str:
    """The string `lms load` will actually take.

    Entries parked by an older build stored the "name@quant" variant, which
    does not load. Strip the suffix so a debt written before the fix still
    comes back instead of failing forever.
    """
    return (entry.get("model") or entry.get("identifier") or "").split("@", 1)[0]


def _reload_llm(entry: dict, st: dict) -> tuple[bool, str]:
    if entry.get("driver") == "lmstudio":
        ok, out = lms_load(_lms_key(entry),
                           context_length=entry.get("context") or 0,
                           gpu=st.get("gpu_offload", ""),
                           ttl_s=entry.get("ttl_s") or 0,
                           identifier=entry.get("identifier", ""),
                           parallel=entry.get("parallel") or 0,
                           bin_path=st.get("lms_bin", "lms"),
                           timeout=int(st.get("load_timeout_s", 300)))
        if ok and entry.get("variant"):
            back = next((m.get("selectedVariant") for m in lms_ps(st.get("lms_bin", "lms"))
                         if m.get("modelKey") == _lms_key(entry)), "")
            if back and back != entry["variant"]:
                return True, (f"reloaded {_lms_key(entry)}, but LM Studio picked "
                              f"{back} where you had {entry['variant']} — that "
                              "model has several variants and -y takes the first")
        bits = [f"reloaded {_lms_key(entry)}"]
        if entry.get("context"):
            bits.append(f"at {entry['context']} ctx")
        if entry.get("parallel"):
            bits.append(f"parallel {entry['parallel']}")
        if entry.get("ttl_s"):
            bits.append(f"ttl {entry['ttl_s']}s")
        return ok, (" ".join(bits) if ok else f"reload failed: {out[:200]}")
    if entry.get("driver") == "llamacpp":
        base = entry.get("url") or lcpp_base(st.get("lcpp_url", ""))
        key = st.get("lcpp_key", "")
        want = entry.get("model") or ""
        if not want:
            return False, "parked entry names no model"
        if not lcpp_load(base, want, key):
            return False, "llama-server refused the load"
        ok, why = lcpp_wait(base, want, True, key,
                            timeout_s=float(st.get("load_timeout_s", 300)))
        # The instance args live in the router's own preset, so a successful
        # load IS the faithful restore — context, offload, mmproj and all.
        return ok, (f"reloaded {want}" if ok else why)
    if entry.get("driver") == "koboldcpp":
        base = entry.get("url") or kcpp_base(st.get("kcpp_url", ""))
        key = st.get("kcpp_key", "")
        want = entry.get("model") or ""
        # `initial_model` restores the launch config exactly, which is the
        # faithful answer in the ordinary case. It is the WRONG answer if the
        # user had already swapped configs through admin, so prefer a config
        # whose name matches what was actually parked when one exists.
        target = "initial_model"
        # Match the CONFIG NAME into the MODEL NAME, not the other way round:
        # a .kcpps is named after the model it loads, so "cydonia-24b.kcpps"
        # sits inside "koboldcpp/Cydonia-24B-v4.1-Q5_K_M". Compare on
        # alphanumerics only — stripping the extension with rsplit(".") ate
        # the version dot in "v4.1" and matched nothing.
        want_norm = _kcpp_norm(want.rsplit("/", 1)[-1])
        if want_norm:
            for opt in kcpp_options(base, key):
                if opt in ("initial_model", "unload_model"):
                    continue
                stem = _kcpp_norm(opt.rsplit(".", 1)[0])
                if len(stem) >= 4 and stem in want_norm:
                    target = opt
                    break
        if not kcpp_reload(base, target, key):
            return False, "KoboldCpp refused the reload"
        ok = kcpp_wait(base, True, key,
                       timeout_s=float(st.get("load_timeout_s", 300)))
        if not ok:
            return False, "KoboldCpp did not come back with a model"
        back = kcpp_model(base, key)
        if want and back and want != back:
            # Say so rather than quietly hand back a different model.
            return True, (f"reloaded, but KoboldCpp came back as {back}, "
                          f"not {want} — check --admindir")
        return True, f"reloaded {back or want or 'the KoboldCpp model'}"
    if entry.get("driver") == "command":
        cmd = st.get("load_cmd", "")
        if not cmd:
            return False, "no load_cmd set"
        code, out = _run(["sh", "-c", cmd],
                         timeout=int(st.get("load_timeout_s", 300)))
        return code == 0, ("ran load_cmd" if code == 0
                           else f"load_cmd failed: {out[:200]}")
    return False, "unknown driver"


def is_lmstudio(backend: str) -> bool:
    """Does this backend URL look like the LM Studio we can drive?

    Deliberately loose: people run it on a LAN box or a non-default port. The
    driver setting is what actually authorises us to touch anything; this only
    stops us trying to `lms unload` because a request to OpenRouter failed.
    """
    u = (backend or "").lower()
    return ":1234" in u or "lmstudio" in u or "lm-studio" in u


def ensure_model(cfg: dict, backend: str, model: str, context_tokens: int = 0,
                 note=None) -> tuple[bool, str]:
    """Make `model` the loaded one, evicting whatever is in the way.

    This is the LLM-to-LLM half of brokering, and it was missing. `make_room`
    parks the chat model so a ComfyUI job can have the card and gives it back
    afterwards. Nothing covered the commoner case: you swap models in LM Studio,
    ask CoomKit for one that is not resident, and the load fails outright
    because a 24 GB model already owns the GPU. The error LM Studio returns is
    `Failed to load model "X"`, which reads like the model is broken.

    NOT a park: you asked for a different model, so the old one is evicted and
    does NOT come back. Nothing is written to the parked debt.

    Loads at `context_tokens` on purpose. LM Studio JIT-loads at its DEFAULT
    context, which silently truncates every later chat — the same trap the
    parked-list restore exists to avoid.

    Returns (did_something, note).
    """
    st = settings(cfg)
    say = note or (lambda _m: None)
    if st["policy"] == "off":
        return False, ("GPU policy is off, so I left your loaded models alone")
    if st.get("driver") == "llamacpp":
        # The gate is identity, not vibes: the request's backend must BE the
        # router this driver is configured to touch. An OpenRouter 404 or a
        # typo'd LAN box must never unload anything here.
        base = lcpp_base(st.get("lcpp_url", ""))
        if not base or lcpp_base(backend) != base:
            return False, "no driver that can swap models on this backend"
        key = st.get("lcpp_key", "")
        want = (model or "").strip()
        if not want:
            return False, "no model named"
        models = lcpp_models(base, key)
        if models is None:
            return False, f"nothing answering at {base}"
        if not lcpp_is_router(models):
            return False, _LCPP_SINGLE
        if not any(m.get("id") == want for m in models):
            return False, f"llama-server does not list {want}"
        loaded = lcpp_loaded(models)
        if want in loaded:
            return False, "already loaded"
        others = ", ".join(m for m in loaded if m != want) or "nothing"
        say(f"{want} is not loaded and {others} is holding the card — swapping")
        for mid in loaded:
            if lcpp_unload(base, mid, key):
                lcpp_wait(base, mid, False, key, timeout_s=60)
        if not lcpp_load(base, want, key):
            return False, f"unloaded {others} but llama-server refused {want}"
        ok, why = lcpp_wait(base, want, True, key,
                            timeout_s=float(st.get("load_timeout_s", 300)))
        if not ok:
            return False, f"unloaded {others} but {why}"
        return True, f"swapped {others} out for {want}"
    if st.get("driver") != "lmstudio" or not is_lmstudio(backend):
        return False, "no driver that can swap models on this backend"
    binp = st.get("lms_bin", "lms")
    if not lms_available(binp):
        return False, f"{binp} is not on PATH"

    want = (model or "").split("@", 1)[0]
    if not want:
        return False, "no model named"
    loaded = lms_ps(binp)
    for m in loaded:
        if (m.get("modelKey") or "").split("@", 1)[0] == want:
            return False, "already loaded"

    others = ", ".join((m.get("modelKey") or "?") for m in loaded) or "nothing"
    say(f"{want} is not loaded and {others} is holding the card — swapping")
    if loaded:
        ok, out = lms_unload_all(binp)
        if not ok:
            return False, f"could not unload {others}: {out[:120]}"
    ok, out = lms_load(want, context_length=int(context_tokens or 0),
                       bin_path=binp, timeout=int(st.get("load_timeout_s", 300)))
    if not ok:
        return False, f"unloaded {others} but {want} would not load: {out[:160]}"
    ctx = f" at {context_tokens} ctx" if context_tokens else ""
    return True, f"swapped {others} out for {want}{ctx}"


def make_room(cfg: dict, comfy_url: str, need_gb: float,
              note=None) -> dict:
    """Free up `need_gb` of VRAM before a ComfyUI job.

    Returns a report: {acted, freed_gb, before_gb, after_gb, steps[]}. The
    caller passes the same report to `give_back()` afterwards.

    `note` is an optional callable for progress lines — the chat UI shows them
    live, because a 40-second pause with no explanation reads as a hang.
    """
    st = settings(cfg)
    say = note or (lambda _m: None)
    report = {"acted": False, "steps": [], "policy": st["policy"],
              "need_gb": need_gb, "restore": []}

    if st["policy"] == "off":
        return report

    before = comfy_stats(comfy_url)
    report["before_gb"] = before.get("vram_free_gb")
    report["total_gb"] = before.get("vram_total_gb")
    want = float(need_gb) + float(st.get("headroom_gb", 2.0))

    enough = (report["before_gb"] is not None
              and report["before_gb"] >= want)
    if enough and st["policy"] != "always":
        report["steps"].append(
            f"{report['before_gb']} GB free, job wants {want:.1f} GB — nobody"
            " has to move")
        return report

    with _lock:
        unloaded, msg = _unload_llm(st)
        report["steps"].append(msg)
        if unloaded:
            report["acted"] = True
            report["restore"] = unloaded
            # Read the file first: a process that restarted while holding a
            # debt has an empty _parked, and extend+save would write the new
            # entry over the record of the old one.
            _load_parked()
            # Dedupe on (driver, model). A reload that fails leaves the debt
            # standing, and without this the next park appends a SECOND entry
            # for the same model — a real dev box here had two, at different
            # context lengths, so `restore_all` would have loaded it twice and
            # the older, wrong context could win.
            seen = {(e.get("driver"), e.get("model")) for e in unloaded}
            _parked[:] = [e for e in _parked
                          if (e.get("driver"), e.get("model")) not in seen]
            _parked.extend(unloaded)
            _save_parked()
            say(f"{msg} to make room on the GPU")

        # ComfyUI may itself be sitting on a model from a previous job of a
        # different kind (an image model when the next job is video).
        if comfy_free(comfy_url):
            report["steps"].append("asked ComfyUI to drop cached models")
            report["acted"] = True

    after = wait_for_free(comfy_url, want, timeout_s=25.0)
    report["after_gb"] = after
    if report["before_gb"] is not None:
        report["freed_gb"] = round(after - report["before_gb"], 2)
    if after < want:
        report["steps"].append(
            f"still only {after:.1f} GB free of the {want:.1f} GB wanted —"
            " running anyway, ComfyUI will offload if it must")
    return report


def give_back(cfg: dict, comfy_url: str, report: dict, note=None) -> dict:
    """Undo `make_room`: drop ComfyUI's models, reload whatever we unloaded."""
    st = settings(cfg)
    say = note or (lambda _m: None)
    out = {"steps": []}
    if not report or not report.get("acted"):
        return out

    with _lock:
        if st.get("free_comfy_after", True):
            if comfy_free(comfy_url):
                out["steps"].append("ComfyUI released its models")

        if st.get("restore", True):
            entries = report.get("restore") or []
            if entries:
                # make_room waits for the card; this side never did, and got
                # away with it only because --gpu max forced a full offload
                # regardless. Now that the offload ratio is LM Studio's
                # choice again, reloading into a still-occupied card would
                # make it pick a CPU spill — which presents as "CoomKit made
                # my model slow", a worse bug than the one being fixed.
                need = sum((e.get("context") or 0) for e in entries) and 1.0
                wait_for_free(comfy_url, need or 1.0, timeout_s=20.0)
            for entry in entries:
                say("loading your chat model back onto the GPU…")
                ok, msg = _reload_llm(entry, st)
                out["steps"].append(msg)
                if ok:
                    _drop_parked(entry)
    out["free_gb"] = comfy_stats(comfy_url).get("vram_free_gb")
    return out


def _drop_parked(entry: dict) -> None:
    for i, p in enumerate(_parked):
        if p.get("model") == entry.get("model") and \
                p.get("driver") == entry.get("driver"):
            _parked.pop(i)
            _save_parked()
            return


def restore_all(cfg: dict) -> dict:
    """Reload anything CoomKit unloaded and never put back.

    Belt and braces for the case where a job crashed between the two halves —
    without it the user's next message hits a backend with no model loaded and
    the error looks like CoomKit broke their LM Studio.
    """
    st = settings(cfg)
    done = []
    with _lock:
        _load_parked()
        for entry in list(_parked):
            ok, msg = _reload_llm(entry, st)
            done.append(msg)
            if ok:
                _drop_parked(entry)
    return {"steps": done, "parked": parked()}


def status(cfg: dict, comfy_url: str) -> dict:
    """Everything the UI needs to draw the VRAM widget."""
    st = settings(cfg)
    info = {"policy": st["policy"], "driver": st["driver"],
            "gpu": comfy_stats(comfy_url), "parked": parked()}
    if st["driver"] == "none":
        # Nothing configured yet: probe both so the UI can pick the one that
        # is actually running instead of assuming LM Studio, which is what it
        # used to do the moment anyone switched the policy on.
        info["lms_available"] = lms_available(st.get("lms_bin", "lms"))
        info["kcpp_up"] = bool(kcpp_caps(kcpp_base(st.get("kcpp_url", "")),
                                         st.get("kcpp_key", "")))
        info["lcpp_up"] = lcpp_models(lcpp_base(st.get("lcpp_url", "")),
                                      st.get("lcpp_key", "")) is not None
    if st["driver"] == "koboldcpp":
        base = kcpp_base(st.get("kcpp_url", ""))
        caps = kcpp_caps(base, st.get("kcpp_key", ""))
        info["kcpp_url"] = base
        info["kcpp_up"] = bool(caps)
        info["kcpp_admin"] = int(caps.get("admin") or 0)
        info["loaded"] = ([{"model": kcpp_model(base, st.get("kcpp_key", "")),
                            "context": None, "size_gb": None,
                            "status": "loaded"}]
                          if caps.get("llm") else [])
        if caps and not caps.get("admin"):
            info["problem"] = ("KoboldCpp is running but not in admin mode — "
                               "restart it with --admin --admindir <folder>")
    if st["driver"] == "llamacpp":
        base = lcpp_base(st.get("lcpp_url", ""))
        key = st.get("lcpp_key", "")
        models = lcpp_models(base, key)
        info["lcpp_url"] = base
        info["lcpp_up"] = models is not None
        router = lcpp_is_router(models or [])
        info["lcpp_router"] = router
        info["loaded"] = [
            {"model": m.get("id"), "context": None, "size_gb": None,
             "status": _lcpp_status(m)}
            for m in (models or [])
            if _lcpp_status(m) in ("loaded", "loading", "sleeping")]
        if models is not None and not router:
            info["problem"] = _LCPP_SINGLE
    if st["driver"] == "lmstudio":
        binp = st.get("lms_bin", "lms")
        info["lms_available"] = lms_available(binp)
        info["loaded"] = [
            {"model": m.get("modelKey"), "context": m.get("contextLength"),
             "size_gb": round((m.get("sizeBytes") or 0) / GB, 2),
             "status": m.get("status")}
            for m in (lms_ps(binp) if info["lms_available"] else [])]
    return info
