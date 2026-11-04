"""Fuse a coupled DEVSMap model into one minimal DEVSMap atomic.

    fuse(coupled_path, out_dir) -> 0 (accepted, files written) or 1

The coupled model's Cadmium trace is the reference. An LLM proposes a fused
atomic plus init state; each proposal is simulated and compared with the
reference. Rejections (an unreadable reply, a failed generate, compile or
simulate, a trace mismatch) are fed back to the LLM.
Traces compare as (time, port, value) multisets. The LLM sees the first
SHOWN_EVENTS events; comparison runs HIDDEN_FACTOR times longer, so replaying
the shown events is not enough.

Environment (also read from .env): LLM_PROVIDER (gemini or openai), the
selected provider's API key, model(s) and retries, and FUSE_MAX_ITERS
(LLM rounds, default 3).
"""

import json
import os
import re
import tempfile
import time
from typing import NamedTuple

import cadmium_api
import devsmap
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
You are given a coupled DEVS model in two forms: its raw DEVSMap files
(the coupled model, every atomic model it uses, and its initial states)
and a flattened summary (hierarchy removed: leaf components, their
initial states, leaf-to-leaf couplings, the atomic models' behavior).

Your task: give the equivalent atomic model -- fuse the coupled model
into a MINIMAL atomic DEVS model, in three phases. Reason step by step
and write each phase before moving to the next.

Output budget: keep the reasoning tight. Each phase gets a short paragraph
or a few bullets -- the construction's key facts (Phase 1), the invariants
and what is discarded with why (Phase 2), the state mapping (Phase 3) --
then the JSON. The two JSON blocks are the deliverable; the prose is the
justification you will debug from if the verifier rejects the proposal.
Write plain text: no LaTeX or math markup; write formulas like
sigma_i - e_i.

Follow the papers exactly:
- Zeigler, Kewley, Wainer (2025), "Closure under coupling"
  (Computers 14(12), 514): a coupled model is defined over D, a set of
  NAMED components, and the closure resultant's state is indexed by those
  component names. Component identity is part of the construction, not an
  annotation.
- Govind, Cardenas, Wainer, "DEVSMap: On the Path of Standardized DEVS
  Model Representation": components are listed as model name -> instance
  ids; those ids are the D of the construction above.

## Phase 1 -- the paper's construction (exact, no reduction)

Build the closure paper's resultant conceptually, in six steps:

1. **State.** The product of the components' states, each paired with
   its elapsed time (time since that component's last transition).
2. **Time advance.** The minimum, over all components, of (time advance
   minus elapsed time). Components attaining the minimum are *imminent*.
3. **Output.** Every imminent component computes its output -- all of
   them; Parallel DEVS has no select function.
4. **Routing.** Outputs travel through the coupling table: internal
   couplings into recipients' input bags, external output couplings out
   of the model, external input couplings into components from outside.
5. **Transition.** Each component updates by case: imminent *and*
   receiving input -> its confluent transition; receiving but not
   imminent -> its external transition (with its own elapsed time);
   imminent but not receiving -> its internal transition; neither -> only
   its elapsed time grows.
6. **Repeat.** The clock advances to the next minimum; the cycle
   continues.

This product state is usually large; that is why Phase 2 exists. Do not
minimize yet, and you do not need to list every product variable.

## Phase 2 -- what is reachable (exact, no approximation)

From the given initial state, characterize the FULL reachable set:

- A state is reachable if ANY path from the initial state reaches it, at
  ANY time. A state unreachable for a while but reachable later MUST be
  kept; bounded-horizon reasoning is wrong.
- State the invariants: which combinations of component states and
  elapsed times can actually occur (e.g. "exactly one component is in
  Send; the others are in Wait").
- Per component, say whether its elapsed time can ever matter: does its
  delta_ext read `e`? Can it become imminent while passive? If neither,
  its individual clock can be dropped.
- Discard ONLY what you can prove unreachable by every path. Keeping an
  unreachable state is harmless; discarding a reachable one silently
  breaks the model. When in doubt, KEEP it.

## Phase 3 -- the minimal atomic (our extension: justify it)

Recode the reachable set as an atomic model that keeps component identity:

- **One state variable per component**, named by its id (e.g. `P01`,
  `P02`), recording that component's state directly. A component with
  several aspects gets one variable per aspect (`P01_phase`,
  `P01_count`, ...). Never fold several components into one variable (no
  holder, pointer, token, bare timer or counter): that erases which
  component is which and is not a quotient of the paper's resultant.
- Minimality happens *within* each component's variables: drop a
  variable only when it is provably redundant -- a function of variables
  you keep (e.g. sigma_Pi fixed by phase_Pi), or a clock no transition
  reads and that can never make its component imminent. Merge values of
  one component's variable only when they are behaviorally
  indistinguishable, and justify each merge.
- One shared `sigma` for the fused model's time advance is fine; it is
  the resultant's clock, not a component's state.
- `lambda` emits `"<component_id>:<value>"` (e.g. `"A:ball"`), never a
  bare value: the verifier tags each original output with its sender
  the same way.
- Give the explicit mapping: which reachable product states correspond
  to each fused state (the bisimulation relation).

Phase 1 is the paper (exact). Phase 2 is exact reduction by
reachability. Phase 3 is *our* extension -- minimality is the paper's
future work, not its result. The verifier only checks the trace, so your
Phase 2 invariants and Phase 3 mapping carry the justification.

## Worked example: 2-player ping-pong (study the pattern, don't copy it)

Coupled model: components A and B passing a ball in a ring
(A -> B -> A -> ...). Reference trace: (0.1, send, "A:ball"),
(0.2, send, "B:ball"), ...

Phase 1: product state (phase_A, sigma_A, e_A, phase_B, sigma_B, e_B);
resultant ta is the min over components; all imminents emit; outputs
route through the couplings; each component updates by case.

Phase 2: invariants -- exactly one component is in "Send"; Send <=>
sigma = 0.1, Wait <=> sigma = inf; neither delta_ext reads `e` and no
passive component can go imminent, so individual clocks are dropped.
Two reachable states.

Phase 3: `A` is `"Ball"` exactly when component A is Send (otherwise
`"NoBall"`), likewise `B`; one shared `sigma` drives ta. The fused
atomic (plain text here; your answer uses ```json blocks):

    {{"fused_pingpong": {{
      "x": {{}},
      "y": {{"send": "std::string"}},
      "s": {{"A": "std::string", "B": "std::string", "sigma": "double"}},
      "delta_int": {{
        "A == \"Ball\"": {{"A": "\"NoBall\"", "B": "\"Ball\"",
                           "sigma": "0.1"}},
        "otherwise": {{"A": "\"Ball\"", "B": "\"NoBall\"",
                       "sigma": "0.1"}}}},
      "delta_ext": {{"otherwise": {{}}}},
      "delta_con": {{"otherwise": {{}}}},
      "lambda": {{
        "A == \"Ball\"": {{"send": "\"A:ball\""}},
        "otherwise": {{"send": "\"B:ball\""}}}},
      "ta": {{"otherwise": "sigma"}}}}}}

Init: {{"init_states": {{"fused_pingpong": {{"A": "\"Ball\"",
"B": "\"NoBall\"", "sigma": "0.1"}}}}}}.

The DEVSMap format you must follow is described below:
---
{devsmap_spec}
---

Rules:
- The fused model is TWO documents: a <name>_atomic.json and its
  <name>_init_state.json. Pick one model name and use it in both.
- No JSON object may repeat a key (e.g. two "otherwise" branches).
- Reproduce every output event of the original's atomic components, also
  the ones that only travel along internal couplings, as
  (time, port, "<component_id>:<value>"). This is the coupled model's
  trace in Cadmium until t = {window}:
{expected}
- The verifier keeps comparing after t = {window}, so reproduce the
  behavior, not just the listed events.
- State-variable rules are in Phase 3; follow them.
- Answer in three parts: Phase 1 (the construction, in words), Phase 2
  (the reachability invariants), Phase 3 (the mapping, then the atomic
  document in ONE ```json block and the init-state document in a second
  ```json block). No other code blocks in your answer.
- End each phase with one line giving its state count, exact or
  estimated, counting discrete states (leave continuous clocks out),
  e.g. "Phase 1: 2^100 states", "Phase 2: 100 states",
  "Phase 3: 100 states".

Raw DEVSMap files:

{raw_files}

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
    events: list     # observe() events up to horizon
    window: float    # the LLM sees the events up to this time
    horizon: float   # proposals are compared up to this time


def shown(events, window):
    """The part of observed `events` the LLM gets to see."""
    return [e for e in events if e[0] <= window]


def observe(path, horizon):
    """Sorted (time, port, value) events of the model at `path` up to
    t = horizon; order is not compared.

    For a coupled model the sender is folded into the value ("P01:ball"),
    so which component emitted each output is compared. An atomic model
    is compared on its values as-is, so a fused atomic must emit
    "<id>:<value>" itself.
    """
    coupled = devsmap.load_model(path)["kind"] == "coupled"
    # Run past the horizon and cut on rounded times, so float drift in the
    # end time cuts the reference and every proposal the same way.
    trace = cadmium_api.simulate(path, horizon + SLACK)
    events = sorted((e["time"], e["port"],
                     f'{e["sender"]}:{e["value"]}' if coupled else e["value"])
                    for e in trace)
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


def model_files(model):
    """Paths of the model's file and every component model file below it."""
    paths = [model["path"]]
    for sub in model.get("models", {}).values():
        paths += model_files(sub)
    return list(dict.fromkeys(paths))


def raw_file(path):
    """A file's contents as a ```json block headed by its filename."""
    with open(path, encoding="utf-8") as f:
        return f"{os.path.basename(path)}:\n```json\n{f.read().strip()}\n```"


def build_prompt(model, reference):
    with open(DEVSMAP_SPEC, encoding="utf-8") as f:
        spec = f.read()
    paths = model_files(model) + [model["init_path"]]
    expected = [{"time": t, "port": p, "value": v}
                for t, p, v in shown(reference.events, reference.window)]
    return PROMPT.format(
        devsmap_spec=spec,
        window=reference.window,
        expected=json.dumps(expected, indent=2),
        raw_files="\n\n".join(raw_file(p) for p in paths),
        summary=devsmap_flatten.flattened_summary(model),
    )


def load_env_file():
    """Load KEY=VALUE lines from .env without adding a dependency."""
    if not os.path.exists(ENV_FILE):
        return
    with open(ENV_FILE, encoding="utf-8") as f:
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
    """(provider, api key, models, retries per model) from the environment."""
    provider = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
    if provider not in ("gemini", "openai"):
        raise LLMError("LLM_PROVIDER must be 'gemini' or 'openai'")
    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY")
        model_text = os.environ.get("OPENAI_MODELS") or os.environ.get(
            "OPENAI_MODEL", "gpt-6.1-sol")
        prefix = "OPENAI"
    else:
        key = (os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY")
               or os.environ.get("LLM_API_KEY"))
        model_text = os.environ.get("GEMINI_MODELS") or os.environ.get(
            "GEMINI_MODEL", "gemini-3.8-flash")
        prefix = "GEMINI"
    if not key:
        raise LLMError(f"set {prefix}_API_KEY first (e.g. in .env)")
    models = [m.strip() for m in model_text.split(",") if m.strip()]
    if not models:
        raise LLMError(f"set {prefix}_MODEL to an available model ID")
    try:
        retries = env_int(f"{prefix}_RETRIES", 3)
    except ValueError as exc:
        raise LLMError(str(exc)) from None
    return provider, key, models, retries


def _event_text(event):
    """Best-effort text extraction from a streaming Interactions event."""
    for attr in ("output_text", "text", "delta"):
        value = getattr(event, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _status(exc):
    """The HTTP status an SDK error carries, or None if it has none."""
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _with_retries(provider, models, retries, request):
    """request(model) -> reply text, trying each model with retries.

    A rejected key stops at once and a bad request or unknown model moves
    on to the next model; anything else (429, 5xx, timeouts) is retried."""
    last_error = None
    for model in models:
        for attempt in range(1, retries + 1):
            print(f"fuse: {provider} {model} (attempt {attempt}/{retries})...")
            try:
                reply = request(model)
                if not isinstance(reply, str) or not reply.strip():
                    raise RuntimeError("no text in the reply")
            except Exception as exc:
                last_error = f"{model}: {exc}"
                status = _status(exc)
                print(f"fuse: {provider} failed: {exc}")
                if status in (401, 403):
                    raise LLMError(f"{provider} rejected the API key: "
                                   f"{last_error}") from None
                if status in (400, 404):
                    break
                if attempt < retries:
                    time.sleep(3 * attempt)
            else:
                # Outside the try: a console print error is not a failed call.
                print(f"\nfuse: {provider} reply:\n{reply}")
                return reply
    raise LLMError(f"every {provider} request failed; last: {last_error}")


def call_llm(prompt, settings):
    """Ask the provider in settings (from llm_settings) for a reply."""
    provider, key, models, retries = settings
    if provider == "openai":
        try:
            from openai import OpenAI
        except ImportError:
            raise LLMError("install the OpenAI SDK first: "
                           "pip install -U openai") from None
        client = OpenAI(api_key=key, max_retries=0)
        return _with_retries("OpenAI", models, retries, lambda model:
                             client.responses.create(
                                 model=model, input=prompt).output_text)
    try:
        from google import genai
    except ImportError:
        raise LLMError("install the Gemini SDK first: "
                       "pip install -U google-genai") from None
    client = genai.Client(api_key=key)

    def stream(model):
        chunks = []
        for event in client.interactions.create(model=model, input=prompt,
                                                stream=True):
            text = _event_text(event)
            if text:
                print(".", end="", flush=True)  # progress only
                chunks.append(text)
        return "".join(chunks)

    return _with_retries("Gemini", models, retries, stream)


def _unique_keys(pairs):
    """object_pairs_hook for json.loads: a repeated key is an error."""
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key {key!r} in one JSON object; "
                             f"merge its entries into one")
        obj[key] = value
    return obj


def parse_reply(reply):
    """(proposal text, [atomic doc, init doc] or None, why it is unusable)."""
    blocks = re.findall(r"```json[ \t]*\r?\n(.*?)```", reply,
                        re.S | re.I)
    proposal = "\n\n".join(blocks) if blocks else reply.strip()
    if len(proposal) > REPLY_LIMIT and not blocks:
        proposal = proposal[:REPLY_LIMIT] + "\n[... truncated]"
    if len(blocks) != 2:
        return proposal, None, (
            f"expected exactly two ```json blocks (atomic, then "
            f"init-state), found {len(blocks)}")
    try:
        docs = [json.loads(block, object_pairs_hook=_unique_keys)
                for block in blocks]
    except ValueError as exc:
        return proposal, None, f"invalid JSON in a block: {exc}"
    return proposal, docs, None


def write_proposal(folder, atomic, init):
    """Write a proposal to <name>_atomic.json and <name>_init_state.json in
    folder; return the atomic's path. ValueError if it has no usable name."""
    name = devsmap.model_name(atomic, "the atomic document")
    if os.path.basename(name) != name:
        raise ValueError(f"model name {name!r} is not a plain name")
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
    """Mismatch report on observed traces: counts + first differences."""
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
    """(ok, reason): does the atomic at `path` match the Reference?"""
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
    write_proposal(folder, *docs)
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
    settings = llm_settings()
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
                reply = call_llm(prompt, settings)
            except LLMError as exc:
                if proposal is not None:
                    print(f"fuse: last proposal:\n{proposal}\n"
                          f"fuse: last reason: {reason}")
                print(f"fuse: LLM failed: {exc}")
                return 1
            proposal, docs, reason = parse_reply(reply)
            if docs is not None:
                try:
                    path = write_proposal(tempfile.mkdtemp(dir=tmp), *docs)
                except (ValueError, OSError) as exc:
                    reason = f"unusable proposal: {exc}"
                else:
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
