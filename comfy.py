#!/usr/bin/env python3
"""CoomKit ComfyUI bridge — bring-your-own-workflow.

The user exports an API-format workflow JSON from their ComfyUI (Save (API
format)) and pastes/uploads it. We scan it for {{slot}} placeholders in any
string/int/float value and substitute at submission time.

Well-known slots (all optional — only ones present get filled):
  {{prompt}}           positive prompt text
  {{negative}}         negative prompt text
  {{seed}}             randomised per run unless pinned
  {{width}} {{height}} resolution
  {{image}}            input image filename (uploaded via /upload/image first)
  {{audio_text}}       text for TTS workflows
  {{music_prompt}}     music generation prompt
Any other {{whatever}} slot is exposed in the UI for the user to fill.

Flow: upload image (optional) -> substitute -> POST /prompt -> poll
/history/{prompt_id} until outputs appear -> download each output file via
/view -> save to assets.
"""
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SLOT_RE = re.compile(r"\{\{(\w+)\}\}")


def find_slots(workflow: dict) -> dict:
    """Scan an API-format workflow for {{slot}} placeholders.

    Returns {slot_name: [(node_id, field), ...]}.
    """
    slots: dict[str, list] = {}
    for node_id, node in workflow.items():
        inputs = (node or {}).get("inputs", {})
        for field, value in inputs.items():
            if isinstance(value, str):
                for m in SLOT_RE.finditer(value):
                    slots.setdefault(m.group(1), []).append((node_id, field))
    return slots


def substitute(workflow: dict, values: dict) -> dict:
    """Deep-copy the workflow with all {{slot}} placeholders replaced.

    Numeric slots (seed/width/height) replace the whole value when the field
    is exactly "{{slot}}" — otherwise string interpolation is used.
    """
    def fix(value):
        if isinstance(value, str):
            slots = SLOT_RE.findall(value)
            if not slots:
                return value
            if len(slots) == 1 and value == "{{%s}}" % slots[0]:
                replacement = values.get(slots[0], value)
                return replacement
            out = value
            for s in slots:
                out = out.replace("{{%s}}" % s, str(values.get(s, "")))
            return out
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        return value

    return {node_id: {**node, "inputs": fix(node.get("inputs", {}))}
            for node_id, node in workflow.items()}


class ComfyError(Exception):
    pass


VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".gif"}
AUDIO_EXT = {".flac", ".mp3", ".wav", ".ogg", ".opus", ".m4a"}


def kind_of(filename: str, bucket: str = "") -> str:
    """Classify an output by its extension, falling back to ComfyUI's bucket.

    `SaveVideo` files arrive under the history's `images` key — trusting the
    key alone files an mp4 as an image, and the chat then tries to render a
    video in an <img> tag.
    """
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext:
        return "image"
    return {"gifs": "video", "videos": "video", "audio": "audio"}.get(
        bucket, "image")


def _explain_rejection(exc) -> str:
    """Turn ComfyUI's 400 body into one line a person can act on."""
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return f"ComfyUI rejected the workflow (HTTP {exc.code})"
    bits = []
    for node_id, info in (body.get("node_errors") or {}).items():
        cls = info.get("class_type", f"node {node_id}")
        for err in info.get("errors") or []:
            detail = err.get("details") or err.get("message") or "invalid"
            bits.append(f"{cls}: {detail}")
    if bits:
        return "ComfyUI rejected the workflow — " + "; ".join(bits[:4])
    msg = (body.get("error") or {}).get("message") or ""
    return f"ComfyUI rejected the workflow{': ' + msg if msg else ''}"


def _failure(entry: dict) -> str:
    """Turn a /history entry into a readable error, or '' if it's still fine.

    ComfyUI puts the useful part — which node, which value it refused — in the
    execution_error message, not in status_str. Quoting it back is the
    difference between "generation failed" and "OmniVoice rejected 'close mic'
    as an instruct value".
    """
    status = entry.get("status") or {}
    if status.get("status_str") != "error":
        return ""
    for tag, payload in reversed(status.get("messages") or []):
        if tag != "execution_error" or not isinstance(payload, dict):
            continue
        node = payload.get("node_type") or payload.get("node_id") or "a node"
        msg = (payload.get("exception_message") or "").strip()
        detail = f"{node}: {msg}" if msg else f"{node} failed"
        typ = payload.get("exception_type") or ""
        if "OutOfMemory" in typ or "out of memory" in msg.lower():
            # Naming the two knobs that actually move, in the order that
            # costs least. The old advice was "turn on VRAM management",
            # which is no help at all to the many people who already have —
            # H3 at 1.0 MP peaks at 98.5% of a 32 GB card with the chat model
            # already parked, so on this workflow the length and the
            # megapixels are the only things left to give.
            detail += ("  — the GPU ran out of room. If this was a video: "
                       "shorten it, or drop megapixels (0.7 MP keeps the "
                       "framing at about half the pixels). Otherwise turn on "
                       "VRAM management in settings so the chat model steps "
                       "off the card, or pick a lighter workflow.")
        return detail
    if status.get("status_str") == "error":
        return "ComfyUI reported the job failed but said nothing about why"
    return ""


class ComfyClient:
    def __init__(self, base_url: str, timeout: int = 30):
        base = (base_url or "").strip().rstrip("/")
        if base and "://" not in base:
            base = "http://" + base
        self.base = base
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _get(self, path: str):
        with urllib.request.urlopen(self._url(path), timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post_json(self, path: str, payload: dict):
        req = urllib.request.Request(
            self._url(path), data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # ComfyUI rejects a bad graph with a 400 whose *body* names the
            # node, the field and the value it refused. Letting urllib's
            # "HTTP Error 400: Bad Request" escape instead throws that away
            # and leaves the user with nothing to act on.
            raise ComfyError(_explain_rejection(exc)) from None

    def ping(self) -> dict:
        """Return system stats; raises ComfyError if unreachable."""
        try:
            return self._get("/system_stats")
        except Exception as exc:  # noqa: BLE001
            raise ComfyError(f"ComfyUI unreachable at {self.base}: {exc}")

    def upload_image(self, data: bytes, filename: str) -> str:
        """POST /upload/image (multipart). Returns the stored filename."""
        boundary = uuid.uuid4().hex
        parts = []
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="image"; '
                     f'filename="{filename}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n"
                     .encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            self._url("/upload/image"), data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode())
        return resp.get("name", filename)

    def queue_prompt(self, workflow: dict, client_id: str = "coomkit") -> str:
        resp = self._post_json("/prompt",
                               {"prompt": workflow, "client_id": client_id})
        prompt_id = resp.get("prompt_id")
        if not prompt_id:
            raise ComfyError(f"queue rejected: {resp}")
        return prompt_id

    def wait_outputs(self, prompt_id: str, timeout_s: int = 600,
                     poll: float = 1.5) -> list[dict]:
        """Poll /history until done; return list of output file descriptors
        [{filename, subfolder, type, node_id, kind}].

        A failed job is reported the moment ComfyUI records it. Waiting only
        for outputs to appear meant every rejected prompt — a bad model name,
        an unsupported value, an OOM — sat out the full timeout and then
        surfaced as "timed out", which is the least useful description of a
        node that said exactly what was wrong ten seconds in.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            hist = self._get(f"/history/{prompt_id}")
            entry = hist.get(prompt_id)
            if entry:
                if entry.get("outputs"):
                    files = []
                    for node_id, out in entry["outputs"].items():
                        for key in ("images", "gifs", "videos", "audio"):
                            for f in out.get(key, []) or []:
                                files.append({**f, "node_id": node_id,
                                              "kind": kind_of(f["filename"],
                                                              key)})
                    if files:
                        return files
                failure = _failure(entry)
                if failure:
                    raise ComfyError(failure)
            time.sleep(poll)
        raise ComfyError(f"timed out waiting for {prompt_id}")

    def fetch_file(self, f: dict) -> bytes:
        qs = urllib.parse.urlencode({
            "filename": f["filename"],
            "subfolder": f.get("subfolder", ""),
            "type": f.get("type", "output"),
        })
        with urllib.request.urlopen(self._url(f"/view?{qs}"), timeout=300) as r:
            return r.read()


def run_workflow(base_url: str, workflow: dict, values: dict,
                 timeout_s: int = 600) -> list[dict]:
    """High-level: substitute values, queue, wait, fetch bytes.

    values may include _image_bytes + _image_name for {{image}} slots.
    Returns [{kind, filename, data(bytes), node_id}].
    """
    client = ComfyClient(base_url)
    values = dict(values)
    if "seed" in find_slots(workflow) and "seed" not in values:
        values["seed"] = random.randint(0, 2**63 - 1)
    if values.get("_image_bytes") and "image" in find_slots(workflow):
        values["image"] = client.upload_image(
            values["_image_bytes"], values.get("_image_name", "coomkit.png"))
    filled = substitute(workflow, values)
    prompt_id = client.queue_prompt(filled)
    files = client.wait_outputs(prompt_id, timeout_s=timeout_s)
    return [{**f, "data": client.fetch_file(f)} for f in files]
