"""Fuse a coupled DEVSMap model into one minimal DEVSMap atomic.

    fuse(coupled_path, out_dir) -> 0 (accepted, files written) or 1

The coupled model's Cadmium trace is the reference. Gemini proposes a fused
atomic plus init state; each proposal is checked (devsmap_check), simulated
and compared with the reference, and rejections are fed back to the LLM.
Traces compare as (time, port, value) multisets. The LLM sees the first
SHOWN_EVENTS events; comparison runs HIDDEN_FACTOR times longer, so replaying
the shown events is not enough.

Environment (also read from .env): GEMINI_API_KEY (or GOOGLE_API_KEY /
LLM_API_KEY), GEMINI_MODELS (comma-separated fallbacks; GEMINI_MODEL for
one), GEMINI_RETRIES (per model, default 3), FUSE_MAX_ITERS (LLM rounds,
default 3).
"""

import json
import os
import re
import tempfile
import time
from typing import NamedTuple

import cadmium_api
import devsmap
import devsmap_check
import devsmap_flatten

REPO = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(REPO, "devsmap", "out")
ENV_FILE = os.path.join(REPO, ".env")
DEVSMAP_SPEC = os.path.join(REPO, "devsmap", "SKILL.md")
SHOWN_EVENTS = 10      # reference output events the LLM sees
HIDDEN_FACTOR = 3      # proposals are compared up to 3x the shown window
MAX_HORIZON = 1000.0   # the window ends by this time
SLACK = 1e-6           # runs end this far past the time they are cut at
REPLY_LIMIT = 1500     # chars of a json-less reply sent back

PROMPT = """\
You are given TWO compact views of the same DEVS model:

1. The coupled model itself, as DEVSMap JSON.
2. The flattened summary: its components, initial state, couplings, and
resultant semantics in a compact form.

Your task: combine these views into a MINIMAL fused atomic DEVS model with
the same output events as the original, expressed as DEVSMap documents.

The DEVSMap format you must follow is described below:
---
{devsmap_spec}
---

Rules:
- The fused model is TWO documents: a <name>_atomic.json and its
  <name>_init_state.json. Pick one model name and use it in both.
- Reproduce every output event of the original's atomic components, also
  the ones that only travel along internal couplings. Events are
  (time, port, value); which component sent an event is internal
  structure, not part of the output. This is the coupled model's trace
  in Cadmium until t = {window}:
{expected}
- The verifier keeps comparing after t = {window}, so reproduce the
  behavior, not just the listed events.
- Look for invariants in the state, such as correlated variables or values
  that are impossible or redundant, and represent them with fewer meaningful
  state variables when possible.
- First explain the invariant(s) you found in plain words, then give the
  atomic document in ONE ```json block and the init-state document in a
  second ```json block. No other code blocks.

Coupled model (DEVSMap):
```json
{coupled_json}
```

Flattened summary:
```text
{summary}
```
"""

FEEDBACK = """
---
Your previous proposal was REJECTED by the verifier.

Previous proposal:
{proposal}

Reason:
{reason}

Fix the problem and answer again in the same format: explain the
invariant(s), then the atomic document in one ```json block and the
init-state document in a second ```json block.
"""


class LLMError(Exception):
    """The LLM is not configured, or every request to it failed."""


class Reference(NamedTuple):
    """The original model's behavior that proposals must reproduce."""
    events: list     # canonical (time, port, value) events up to horizon
    window: float    # the LLM sees the events up to this time
    horizon: float   # proposals are compared up to this time


def canonical(trace):
    """Sorted (time, port, value) events; sender and order are not compared."""
    return sorted((e["time"], e["port"], str(e["value"])) for e in trace)


def shown(events, window):
    """The part of canonical `events` the LLM gets to see."""
    return [e for e in events if e[0] <= window]


def observe(path, horizon):
    """Canonical events of the model at `path` up to t = horizon."""
    # Run past the horizon and cut on rounded times, so float drift in the
    # end time cuts the reference and every proposal the same way.
    events = canonical(cadmium_api.simulate(path, horizon + SLACK))
    return [e for e in events if e[0] <= horizon]


def reference_run(path):
    """The original's Reference; ValueError if it gives nothing to fuse.

    The window ends at the SHOWN_EVENTS-th event; the horizon grows from
    HIDDEN_FACTOR times the window until some event lies past the window."""
    name = os.path.basename(path)
    horizon = 1.0
    events = observe(path, horizon)
    while len(events) < SHOWN_EVENTS and horizon < MAX_HORIZON:
        horizon = min(10 * horizon, MAX_HORIZON)
        events = observe(path, horizon)
    if not events:
        raise ValueError(f"{name}: no output events up to t = "
                         f"{MAX_HORIZON}; nothing to fuse")
    window = events[:SHOWN_EVENTS][-1][0]
    if window <= 0:
        raise ValueError(f"{name}: the first {SHOWN_EVENTS} output events "
                         f"are all at t = 0; nothing to fuse over time")

    horizon = round(HIDDEN_FACTOR * window, 10)
    while True:
        events = observe(path, horizon)
        if events[-1][0] > window:
            return Reference(events, window, horizon)
        if horizon >= HIDDEN_FACTOR * MAX_HORIZON:
            raise ValueError(
                f"{name}: no output events after t = {window} (up to "
                f"t = {horizon}), so a model replaying the shown events "
                f"could not be told apart; nothing to fuse")
        horizon = min(10 * horizon, HIDDEN_FACTOR * MAX_HORIZON)


def model_size(model):
    """(state variables, delta_int + delta_ext + lambda branches), summed
    over every atomic instance in a load_model result."""
    if model["kind"] == "atomic":
        body = model["body"]
        return len(body["s"]), sum(
            len(body[f]) for f in ("delta_int", "delta_ext", "lambda"))
    sizes = [model_size(model["models"][c["model"]])
             for c in model["components"]]
    return sum(s for s, _ in sizes), sum(b for _, b in sizes)


def build_prompt(model, reference):
    with open(model["path"], encoding="utf-8") as f:
        coupled_json = f.read().strip()
    with open(DEVSMAP_SPEC, encoding="utf-8") as f:
        spec = f.read()
    expected = [{"time": t, "port": p, "value": v}
                for t, p, v in shown(reference.events, reference.window)]
    return PROMPT.format(
        devsmap_spec=spec,
        window=reference.window,
        expected=json.dumps(expected, indent=2),
        coupled_json=coupled_json,
        summary=devsmap_flatten.flattened_summary(model),
    )


def load_env_file(path=ENV_FILE):
    """Load KEY=VALUE lines from .env without adding a dependency."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def env_int(name, default):
    """A positive integer from the environment; ValueError otherwise."""
    text = os.environ.get(name, str(default))
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"{name} must be a positive integer, got {text!r}")
    return value


def llm_settings():
    """(api key, models, retries per model) from the environment and .env;
    an LLMError if they are unusable."""
    load_env_file()
    key = (os.environ.get("GEMINI_API_KEY")
           or os.environ.get("GOOGLE_API_KEY")
           or os.environ.get("LLM_API_KEY"))
    if not key:
        raise LLMError("set GEMINI_API_KEY first (e.g. in .env)")
    model_text = os.environ.get("GEMINI_MODELS") or os.environ.get(
        "GEMINI_MODEL", "gemini-3.8-flash")
    models = [m.strip() for m in model_text.split(",") if m.strip()]
    if not models:
        raise LLMError(f"no Gemini model in {model_text!r}")
    try:
        retries = env_int("GEMINI_RETRIES", 3)
    except ValueError as exc:
        raise LLMError(str(exc)) from None
    return key, models, retries


def _event_text(event):
    """Best-effort text extraction from a streaming Interactions event."""
    for attr in ("output_text", "text", "delta"):
        value = getattr(event, attr, None)
        if isinstance(value, str) and value:
            return value
    if isinstance(event, dict):
        for key in ("output_text", "text", "delta"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def call_llm(prompt):
    """Send `prompt` to Gemini and return the reply, trying every model up
    to its retries; LLMError if none answers."""
    key, models, retries = llm_settings()
    try:
        from google import genai
    except ImportError:
        raise LLMError("install the Gemini SDK first: "
                       "pip install -U google-genai") from None
    client = genai.Client(api_key=key)

    last_error = None
    for model in models:
        for attempt in range(1, retries + 1):
            print(f"fuse: sending streaming request to Gemini model {model} "
                  f"(attempt {attempt}/{retries})...")
            chunks = []
            try:
                stream = client.interactions.create(
                    model=model, input=prompt, stream=True)
                for event in stream:
                    text = _event_text(event)
                    if text:
                        print(".", end="", flush=True)  # progress only
                        chunks.append(text)
                reply = "".join(chunks)
                if not reply.strip():
                    raise RuntimeError("stream ended without text output")
            except Exception as exc:
                last_error = f"{model}: {exc}"
                print(f"fuse: Gemini request failed: {exc}")
                if attempt < retries:
                    wait = 3 * attempt
                    print(f"fuse: retrying in {wait}s...")
                    time.sleep(wait)
            else:
                # Outside the try: a console print error is not a failed call.
                print(f"\nfuse: Gemini request succeeded:\n{reply}")
                return reply
    raise LLMError(f"every Gemini request failed; last: {last_error}")


def parse_reply(reply):
    """(proposal text, [atomic doc, init doc] or None, why it is unusable)."""
    blocks = re.findall(r"```json[ \t]*\n(.*?)```", reply, re.S)
    proposal = "\n\n".join(blocks) if blocks else reply.strip()
    if len(proposal) > REPLY_LIMIT and not blocks:
        proposal = proposal[:REPLY_LIMIT] + "\n[... truncated]"
    if len(blocks) != 2:
        return proposal, None, (
            f"expected exactly two ```json blocks (atomic, then "
            f"init-state), found {len(blocks)}")
    try:
        docs = [json.loads(block) for block in blocks]
    except json.JSONDecodeError as exc:
        return proposal, None, f"invalid JSON in a block: {exc}"
    return proposal, docs, None


def write_proposal(folder, name, atomic, init):
    """Write a proposal checked as model `name` to <name>_atomic.json and
    <name>_init_state.json in folder; return the atomic's path."""
    devsmap.write_json(os.path.join(folder, f"{name}_init_state.json"), init)
    path = os.path.join(folder, f"{name}_atomic.json")
    devsmap.write_json(path, atomic)
    return path


def fenced(text):
    """A Markdown code block that backticks inside `text` cannot close."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def trace_diff(got, expected, limit=3):
    """Mismatch report on canonical traces: counts + first differences."""
    lines = [f"trace mismatch: got {len(got)} events, "
             f"expected {len(expected)}"]
    for i in range(max(len(got), len(expected))):
        g = got[i] if i < len(got) else None
        e = expected[i] if i < len(expected) else None
        if g != e:
            lines.append(f"- event {i}: expected {e}, got {g}")
            if len(lines) > limit:
                break
    return "\n".join(lines)


def verify(path, reference):
    """(ok, reason): does the checked atomic at `path` match the Reference?"""
    try:
        got = observe(path, reference.horizon)
    except (ValueError, cadmium_api.CadmiumError) as exc:
        return False, str(exc)
    if got == reference.events:
        return True, (f"trace matches ({len(got)} events up to "
                      f"t = {reference.horizon})")
    window = reference.window
    if shown(got, window) != shown(reference.events, window):
        return False, trace_diff(shown(got, window),
                                 shown(reference.events, window))
    # Don't leak the hidden part: the LLM only learns that it diverges.
    return False, (f"trace matches up to t = {window}, but diverges from "
                   f"the original after that window; model the behavior, "
                   f"not the listed events")


def accept(path, docs, original, reason, out_dir):
    """Report an accepted proposal and write it to a new dir in out_dir."""
    fused = devsmap.load_model(path)
    states, branches = model_size(fused)
    orig_states, orig_branches = model_size(original)
    print(f"fuse: ACCEPTED - {reason}; {states} state variables, "
          f"{branches} branches (components: {orig_states}, "
          f"{orig_branches})")
    if states >= orig_states and branches >= orig_branches:
        print("fuse: warning: the fused model is not smaller than its "
              "components")
    os.makedirs(out_dir, exist_ok=True)
    folder = tempfile.mkdtemp(prefix=f"{fused['name']}-", dir=out_dir)
    write_proposal(folder, fused["name"], *docs)
    print(f"fuse: wrote {fused['name']}_atomic.json + "
          f"{fused['name']}_init_state.json to {folder}")


def fuse(coupled_path, out_dir=OUT_DIR):
    """Fuse the model at coupled_path; 0 if a proposal is accepted, else 1.

    Bad input or settings raise ValueError, CadmiumError or LLMError before
    the LLM is asked anything.
    """
    model = devsmap.load_model(coupled_path)
    load_env_file()
    max_iters = env_int("FUSE_MAX_ITERS", 3)
    llm_settings()
    print(f"fuse: running {model['name']} in Cadmium for the reference "
          f"trace...")
    reference = reference_run(coupled_path)
    print(f"fuse: {len(shown(reference.events, reference.window))} "
          f"reference events up to t = {reference.window} shown to the LLM; "
          f"proposals are compared up to t = {reference.horizon}")
    base = build_prompt(model, reference)
    prompt, proposal, reason = base, None, None

    with tempfile.TemporaryDirectory(prefix="fuse_",
                                     ignore_cleanup_errors=True) as tmp:
        for attempt in range(1, max_iters + 1):
            print(f"fuse: attempt {attempt}/{max_iters}: asking the LLM...")
            try:
                reply = call_llm(prompt)
            except LLMError as exc:
                if proposal is not None:
                    print(f"fuse: last proposal:\n{proposal}\n"
                          f"fuse: last reason: {reason}")
                print(f"fuse: LLM failed: {exc}")
                return 1
            proposal, docs, reason = parse_reply(reply)
            if docs is not None:
                try:
                    name = devsmap_check.check_proposal(*docs)
                except ValueError as exc:
                    reason = f"invalid DEVSMap: {exc}"
                else:
                    path = write_proposal(tempfile.mkdtemp(dir=tmp), name,
                                          *docs)
                    ok, reason = verify(path, reference)
                    if ok:
                        accept(path, docs, model, reason, out_dir)
                        return 0
            print(f"fuse: REJECTED - {reason}")
            prompt = base + FEEDBACK.format(proposal=fenced(proposal.strip()),
                                            reason=reason)

    print(f"fuse: no accepted model after {max_iters} attempts; "
          f"last reason: {reason}")
    return 1
