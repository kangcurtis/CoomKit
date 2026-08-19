#!/usr/bin/env python3
"""Live smoke test vs LM Studio: chat mode (think deltas) + completion mode."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json
import urllib.request

BASE = "http://127.0.0.1:3939"


def chat(body):
    req = urllib.request.Request(BASE + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    text, think = [], []
    with urllib.request.urlopen(req, timeout=300) as resp:
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
            if "think" in chunk:
                think.append(chunk["think"])
            else:
                text.append(chunk["text"])
    return "".join(text), "".join(think)


model = "google/gemma-4-e4b-it"  # adjust if id differs
models = json.loads(urllib.request.urlopen("http://127.0.0.1:1234/v1/models").read())
model = models["data"][0]["id"]
print("model:", model)

msgs = [{"role": "user", "content": "Say something bratty in one short sentence."}]

print("--- chat mode ---")
t, th = chat({"backend": "http://127.0.0.1:1234/v1", "model": model,
              "messages": msgs, "samplers": {"max_tokens": 2048, "temperature": 0.9}})
print("THINK:", th[:200])
print("TEXT:", t[:300])
assert t.strip(), "chat mode empty"

print("--- completion mode (gemma4, thinking off) ---")
t2, _ = chat({"backend": "http://127.0.0.1:1234/v1", "model": model,
              "messages": msgs, "mode": "completion", "template": "gemma4",
              "thinking": False,
              "samplers": {"max_tokens": 1024, "temperature": 0.9}})
print("TEXT:", t2[:300])
assert t2.strip(), "completion mode empty"
assert "<turn|>" not in t2 and "<|turn>" not in t2, "stop tokens leaked"

print("LIVE CHAT + COMPLETION TESTS PASS")
