#!/usr/bin/env python3
"""Unit tests for the CoomKit LLM provider layer. Run: python3 test_llm.py"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import llm

msgs = [
    {"role": "system", "content": "You are Dr. Rei. SYSTEMPROMPT: stay in character."},
    {"role": "user", "content": "Hi doc"},
    {"role": "assistant", "content": "Well hello there~"},
    {"role": "user", "content": "It hurts here..."},
]

# gemma4 canonical: system turn with think token; open thought channel prefill
p = llm.render_prompt("gemma4", msgs, prefill="Let me see",
                      thinking=True, thinking_prefill="The user says it hurts.")
assert p.startswith("<bos><|turn>system\n<|think|>\nYou are Dr. Rei."), p
assert "<|turn>user\nHi doc<turn|>" in p
assert "<|turn>model\nWell hello there~<turn|>" in p
# thought channel left OPEN with prefill as start of reasoning
assert p.endswith("<|turn>model\n<|channel>thought\nThe user says it hurts.Let me see"), p

# thinking off: no think token, system still present
p2 = llm.render_prompt("gemma4", msgs, thinking=False)
assert p2.startswith("<bos><|turn>system\nYou are Dr. Rei.")
assert "<|think|>" not in p2 and p2.endswith("<|turn>model\n")

# other templates
assert llm.render_prompt("chatml", msgs).endswith("<|im_start|>assistant\n")
assert "<|start_header_id|>assistant<|end_header_id|>" in llm.render_prompt("llama3", msgs)
assert llm.render_prompt("plain", msgs).endswith("Assistant: ")

# completion payload: stops merged, samplers passed through
pay = llm.build_completion_payload(
    msgs, "gemma-4", {"temperature": 0.8, "stop": ["XXX"]},
    template="gemma4", thinking=False)
assert "<|turn>model\n" in pay["prompt"] and pay["temperature"] == 0.8
assert set(pay["stop"]) == {"XXX", "<turn|>", "<|turn>"}

# chat payload with prefill
cp = llm.build_payload(msgs, "m", {"top_k": 20}, prefill="Oh?")
assert cp["messages"][-1] == {"role": "assistant", "content": "Oh?"}
assert cp["continue_final_message"] is True and cp["top_k"] == 20

# multimodal content flattens to text-only for completion mode
vm = llm.vision_message("look at this", [])
flat = llm.render_prompt("gemma4", [{"role": "user", "content": vm["content"]}])
assert "look at this" in flat

# ── the open thought channel, and who has to know about it ──────────────
# render_prompt leaves gemma4's thought channel OPEN when there is a reasoning
# prefill (the jailbreak vector). The model therefore resumes mid-thought and
# never re-emits the opening marker, so stream() has to be TOLD where it is —
# it used to assume it started outside, and classified the entire chain of
# thought, trailing <channel|> and all, as reply text.
assert llm.opens_thought("gemma4", True, "seed") is True
assert llm.opens_thought("gemma4", True, "") is False
assert llm.opens_thought("gemma4", False, "seed") is False
assert llm.opens_thought("chatml", True, "seed") is False

_OPEN, _CLOSE = "<|channel>thought\n", "<channel|>"
mid = "still reasoning" + _CLOSE + "*She smirks.*"
assert llm._split_stream(mid, "", True, _OPEN, _CLOSE)[0] == [
    ("think", "still reasoning"), ("text", "*She smirks.*")]
# and this is exactly what the bug looked like from the bubble
assert llm._split_stream(mid, "", False, _OPEN, _CLOSE)[0] == [("text", mid)]

# a prompt that did NOT open the channel still splits from the outside
closed = "hello " + _OPEN + "hmm" + _CLOSE + "there"
assert llm._split_stream(closed, "", False, _OPEN, _CLOSE)[0] == [
    ("text", "hello "), ("think", "hmm"), ("text", "there")]

print("ALL LLM LAYER TESTS PASS")
print("--- gemma4 canonical render ---")
print(p)
