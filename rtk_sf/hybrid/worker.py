"""
worker.py — client for a local Ollama-compatible code model.

Deliberate constraints, each one a measured failure of the naive version:

* **stdlib only.** `requests` is not a declared dependency of rtk-sf
  (`pyproject.toml` lists watchdog and pyyaml), so importing it ships a module
  that raises ImportError for most installs. `urllib.request` is enough.

* **The model tag is kept whole.** Ollama names models `qwen2.5-coder:7b`.
  Deriving the name with `worker_type.split(":")[-1]` yields `"7b"` — a model
  that does not exist, so every generate call 404s. The tag is stored in its
  own field and never re-parsed out of a composite string.

* **`num_ctx` is set explicitly.** Measured on a local Ollama 0.21.2: a
  `qwen2.5-coder:7b` with a 32768-token trained window is *served* at
  `num_ctx=4096` by default (`/api/ps` reports `context_length=4096`). Prompt
  plus completion share that window, so a whole-class prompt is silently
  truncated and the completion is cut mid-body. Unset `num_ctx` is the single
  most dangerous default here, because the truncation is silent.

* **Fences are stripped regardless of instructions.** Measured: the model opens
  with ```` ``` ```` even when the system prompt says "no markdown blocks". A
  7B does not reliably follow a negative formatting constraint, so the parser
  handles it instead of trusting the prompt.

* **Timeouts are sized from throughput, not guessed.** Measured 13.3 tok/s for
  this model on this machine: a 1500-token body takes ~113 s, so the common
  `timeout=60` aborts every realistic task *after* paying the full compute.

* **Localhost by default, opt-in.** CONTRIBUTING.md requires rtk-sf to work
  offline with no LLM calls in core code. This module is never imported at
  startup; the orchestrator imports it lazily and only after the caller has
  explicitly enabled delegation.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE_URL = "http://localhost:11434"

# Preference order for an auto-detected worker. Coder-tuned models first: a
# general chat model will happily rewrite an Apex method into prose.
_MODEL_PREFERENCE = (
    "qwen2.5-coder",
    "qwen3-coder",
    "qwen2-coder",
    "deepseek-coder",
    "codellama",
    "codegemma",
    "starcoder",
    "qwen",
)

# Context sizing. Ollama allocates KV cache for the whole window, so asking for
# the full trained window on every call wastes memory; ask for what the task
# needs, rounded up to a power of two, capped so a laptop does not swap.
_CTX_FLOOR = 4096
_CTX_CAP = 16384
_CHARS_PER_TOKEN = 3.0  # conservative for source code

# Detection cache, keyed by (base_url, pinned model).
#
# The MCP server builds a fresh Orchestrator per tool call, so without this
# every call re-probes the daemon. A *refused* connection is instant, but an
# unreachable host — the normal case for a remote worker that is powered off —
# costs the full probe timeout. Measured: 3.01 s per call against an
# unroutable address, added to every hybrid_plan/delegate/review.
#
# Negative results expire sooner than positive ones so that starting the daemon
# is noticed quickly, while a long outage stays cheap.
_NEGATIVE_TTL = 30.0
_POSITIVE_TTL = 300.0
_detect_cache: dict[tuple[str, str], tuple[float, WorkerInfo]] = {}


def clear_detection_cache() -> None:
    """Forget cached probes. Mainly for tests and for an explicit retry."""
    _detect_cache.clear()


_FENCE_OPEN = re.compile(r"^\s*```[\w+-]*\s*\n", re.MULTILINE)
_FENCE_CLOSE = re.compile(r"\n\s*```\s*$")


@dataclass
class WorkerInfo:
    """A detected local worker. `available=False` means fall back to Claude."""

    available: bool
    model: str = ""
    base_url: str = DEFAULT_BASE_URL
    trained_ctx: int = 0
    detail: str = ""

    @property
    def label(self) -> str:
        return f"local:{self.model}" if self.available else "none"


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost only
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost only
        return json.loads(resp.read().decode("utf-8"))


def _rank(model_name: str) -> int:
    lowered = model_name.lower()
    for index, prefix in enumerate(_MODEL_PREFERENCE):
        if prefix in lowered:
            return index
    return len(_MODEL_PREFERENCE)


def detect_worker(
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 3.0,
    model: str | None = None,
) -> WorkerInfo:
    """Probe for a local Ollama daemon and pick a code model it serves.

    `timeout` is 3 s rather than the tempting 1 s: a daemon that is up but busy
    loading another model answers `/api/tags` late, and a 1 s probe reports "no
    worker" on a machine that has one — the worst outcome, because the fallback
    is silent.

    `model` pins an exact tag and skips preference ranking. This matters for a
    finetune: auto-detection ranks by name substring, so a custom model called
    e.g. `tokyoedu-apex:latest` matches no known coder prefix and would be
    rejected as "no code-tuned model" despite being the best worker available.
    A pinned tag is also the only way to choose between two viable models.
    Set it via the RTK_SF_WORKER_MODEL environment variable or the tool
    argument.
    """
    model = model or os.environ.get("RTK_SF_WORKER_MODEL") or None

    key = (base_url, model or "")
    cached = _detect_cache.get(key)
    if cached is not None:
        cached_at, info = cached
        ttl = _POSITIVE_TTL if info.available else _NEGATIVE_TTL
        if time.monotonic() - cached_at < ttl:
            return info

    def _remember(info: WorkerInfo) -> WorkerInfo:
        _detect_cache[key] = (time.monotonic(), info)
        return info

    try:
        tags = _get_json(f"{base_url.rstrip('/')}/api/tags", timeout)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        return _remember(WorkerInfo(available=False, base_url=base_url, detail=f"no daemon at {base_url}: {exc}"))

    models = tags.get("models")
    if not isinstance(models, list) or not models:
        return _remember(WorkerInfo(available=False, base_url=base_url, detail="daemon has no models pulled"))

    # Ollama returns both "name" and "model"; older builds only one of them.
    names = [
        str(entry.get("name") or entry.get("model") or "")
        for entry in models
        if isinstance(entry, dict)
    ]
    names = [n for n in names if n]
    if not names:
        return _remember(WorkerInfo(available=False, base_url=base_url, detail="model list had no usable names"))

    if model:
        # Accept an exact tag, or a bare name whose `:latest` form is served.
        match = next(
            (n for n in names if n == model or n.split(":")[0] == model.split(":")[0]), None
        )
        if match is None:
            return _remember(WorkerInfo(
                available=False,
                base_url=base_url,
                detail=f"pinned model '{model}' is not served; available: {', '.join(names[:6])}",
            ))
        best = match
    else:
        best = sorted(names, key=lambda n: (_rank(n), n))[0]
        if _rank(best) == len(_MODEL_PREFERENCE):
            return _remember(WorkerInfo(
                available=False,
                base_url=base_url,
                detail=(
                    f"no code-tuned model among {len(names)} available "
                    f"({', '.join(names[:4])}); pin one with RTK_SF_WORKER_MODEL "
                    "if it is a finetune with a custom name"
                ),
            ))

    trained = 0
    try:
        shown = _post_json(f"{base_url.rstrip('/')}/api/show", {"model": best}, timeout)
        info = shown.get("model_info")
        if isinstance(info, dict):
            for key, value in info.items():
                if key.endswith(".context_length") and isinstance(value, int):
                    trained = value
                    break
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        pass  # trained_ctx is an optimization; absence only means we cap lower

    return _remember(WorkerInfo(
        available=True,
        model=best,
        base_url=base_url,
        trained_ctx=trained,
        detail=(
            f"pinned {best}" if model else f"selected {best} from {len(names)} model(s)"
        ),
    ))


def strip_code_fences(text: str) -> str:
    """Remove markdown fences and any prose outside them.

    Measured behaviour: the model emits ```` ```apex ```` despite an explicit
    instruction not to. When a fenced block exists, its contents are the answer
    and anything outside it is commentary that must not reach a `.cls` file.
    """
    cleaned = text.strip()
    blocks = re.findall(r"```[\w+-]*\s*\n(.*?)(?:\n\s*```|$)", cleaned, re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip()
    cleaned = _FENCE_OPEN.sub("", cleaned)
    cleaned = _FENCE_CLOSE.sub("", cleaned)
    return cleaned.strip()


def _ctx_for(prompt_chars: int, num_predict: int, trained_ctx: int) -> int:
    """Pick a context window that fits prompt + completion with headroom."""
    needed = int(prompt_chars / _CHARS_PER_TOKEN) + num_predict + 512
    size = _CTX_FLOOR
    while size < needed and size < _CTX_CAP:
        size *= 2
    size = min(size, _CTX_CAP)
    if trained_ctx:
        size = min(size, trained_ctx)
    return size


@dataclass
class Generation:
    """Result of one local generation."""

    ok: bool
    text: str = ""
    error: str = ""
    truncated: bool = False
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    num_ctx: int = 0


def generate(
    worker: WorkerInfo,
    system_prompt: str,
    user_prompt: str,
    *,
    num_predict: int = 1536,
    timeout: float = 600.0,
    temperature: float = 0.0,
) -> Generation:
    """Run one deterministic completion against the local worker.

    Returns a `Generation` instead of raising, and never partially succeeds:
    `ok=False` means the caller must not write anything to disk. A completion
    that hit `num_predict` is reported as `truncated` — a truncated method body
    looks syntactically plausible and is exactly what must not be spliced in.
    """
    if not worker.available:
        return Generation(ok=False, error="no local worker available")

    num_ctx = _ctx_for(len(system_prompt) + len(user_prompt), num_predict, worker.trained_ctx)
    payload = {
        "model": worker.model,
        "system": system_prompt,
        "prompt": user_prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": 1.0,
            "seed": 0,  # reproducible output for a given prompt
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }

    try:
        data = _post_json(f"{worker.base_url.rstrip('/')}/api/generate", payload, timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except OSError:
            pass
        return Generation(ok=False, error=f"HTTP {exc.code} from worker: {detail or exc.reason}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return Generation(ok=False, error=f"worker unreachable or timed out after {timeout:.0f}s: {exc}")
    except json.JSONDecodeError as exc:
        return Generation(ok=False, error=f"worker returned non-JSON: {exc}")

    raw = str(data.get("response", ""))
    if not raw.strip():
        return Generation(ok=False, error="worker returned an empty completion")

    output_tokens = int(data.get("eval_count") or 0)
    return Generation(
        ok=True,
        text=strip_code_fences(raw),
        truncated=data.get("done_reason") == "length" or output_tokens >= num_predict,
        prompt_tokens=int(data.get("prompt_eval_count") or 0),
        output_tokens=output_tokens,
        seconds=float(data.get("total_duration") or 0) / 1e9,
        num_ctx=num_ctx,
    )
