# devs-flatten: project context

## Goal

Turn a coupled DEVS model into one equivalent atomic model and check the
equivalence by simulation. Input and output are DEVSMap (Govind, Cárdenas
& Wainer, "DEVSMAP: On the Path of Standardized DEVS Model
Representation"):

    coupled-model DEVSMap in  ->  DEVSMap atomic model out,
    accepted only if Cadmium runs both to the same output trace
    (for open models, see "Open models").

Users are DEVS practitioners, so the emitted DEVSMap follows the paper as
closely as possible.

## How the parts fit

| Module | Job |
|---|---|
| `devsmap.py` | Loads a model, its component models and initial states (no validation; an unreadable file is a `ValueError` naming it); finds its definitions and parameter files. `write_json` is the one JSON writer. |
| `devsmap_flatten.py` | Dissolves nesting into leaf atomics and leaf-to-leaf couplings, with each input port's sources in Cadmium's bag order; writes the text summary the LLM gets. Exact, no AI. |
| `cadmium_api.py` | `simulate(path, end_time)`: stages the model for the DEVSMap generator, compiles the generated `main.cpp` against Cadmium, runs it and parses the CSVLogger trace (`logfile.csv`). `check(path)` only generates and compiles. Failures are `CadmiumError` with that step's output. |
| `fuse.py` | Reference run, prompt, LLM call, verify loop. |
| `devsmap_expr.py` | Parses one DEVSMap expression into a syntax tree. |
| `devsmap_z3.py` | Encodes DEVSMap domains, expressions and atomic models in Z3. |
| `prove.py` | Proves a fused atomic equivalent to its coupled model (see "Proving a fusion"). |
| `main.py` | Command line. |
| `verify.py` | Check suite; the LLM is replaced by canned replies. |

Nothing validates models. A bad user model or LLM proposal fails in the
generator, g++ or the simulation, and for a proposal that error goes back
to the LLM like a trace mismatch. The trace comparison (open models: the
compile) is the check.

The LLM gets the raw DEVSMap files, the flattened summary,
`devsmap/SKILL.md` (the atomic-model rules) and the start of the
reference trace, if any. The prompt asks for three phases: the closure
paper's construction, reduction by reachability, then a minimal atomic
with one state variable per component. A flatten bug cannot fake a pass:
the reference comes from running the original model in Cadmium.

## What an accepted fusion proves

`fuse.reference_run` simulates the original; `fuse.verify` simulates a
proposal and compares.

- **Window:** the time of the 10th reference output event (or the last
  one if fewer occur by t = 1000). The LLM sees only events up to it.
- **Horizon:** 3x the window and at least t = 20, doubled (up to t = 3000)
  until some event lies past the window, so replaying the shown events
  fails, and every emitting component has emitted twice (a full cycle). A
  divergence past the window is reported only as "diverges after that
  window", never with the hidden events.
- **Refused** (`ValueError`, before the LLM is asked): no output event
  by t = 1000, a window at t = 0, or nothing after the window.
- **Compared:** the sorted multiset of (time, port, value) over all atomic
  outputs, including those that only travel along internal couplings.
  For the coupled original the sender is folded into the value
  (`"A:ball"`), so a fused atomic must emit `"<component_id>:<value>"`.
  Times are rounded to 10 decimals; runs go 1e-6 past the horizon and are
  cut on the rounded times, so float drift cannot fail a model against
  itself. Values are compared as logged.
- **Size:** state variables and `delta_int`/`delta_ext`/`lambda`
  branches, proposal vs the components' sum, are printed on accept with a
  warning if not smaller. Size never rejects.

Accepted means the proposal reproduced the original's outputs up to the
horizon. It does not cover behavior after the horizon or from other
initial states, input ports (nothing drives them), or bugs shared by both
runs (same generator and Cadmium).

## Open models

A coupled model with ports (`x` or `y`) is open. The generator cannot
drive ports, so there is no reference run and outputs carry no `<id>:`
prefix. A proposal must keep the coupled model's `x` and `y` and compile
(`cadmium_api.check`); it is written with its generated `<name>.hpp`,
never simulated. Pass the model's own C++ headers with `--header` and
`-I` (see Usage); `CXXFLAGS` takes any other compiler flag. `--header`
can point at a small header of named functions that includes the model's
own headers (e.g. `devsmap/<model>_functions.hpp`).

## Proving a fusion

`python main.py <coupled>.json --prove <fused>_atomic.json` proves with Z3
that the fused atomic F behaves exactly like the coupled model's resultant
R (its closure under coupling, as Cadmium runs it), for every state,
input and parameter value, not only on a simulated trace. It needs a
system homomorphism (Zeigler, Theory of Modeling and Simulation) in
`<fused>_mapping.json` next to F, written by hand (or by the LLM):

    {"mapping": {"<fused>": {"<F state variable>": <expression, or
                             {"<condition>": <expression or map>, ...,
                              "otherwise": <expression or map>}>}},
     "invariant": ["<condition>", ...],
     "functions": {"<function>": "<result domain>", ...}}

Mapping expressions and invariant conditions are DEVSMap over R's state:
`<component id>.<variable>` (and `.field`; nested ids are paths,
`P.L.phase`), `<component id>.e` (time since its last transition),
parameters, and `ta` (R's time advance). A condition map of values may
nest; each level ends with `"otherwise"`. The invariant describes R's
reachable states; `0 <= e <= ta` in every component is added to it.
`functions` (optional) gives the result domains of the functions the
models call.

One Z3 query per obligation (unsat is proven; sat or unknown is not),
split per kind of event and per F branch:

- init: the invariant holds in R's initial state; h maps it to F's.
- the invariant is kept by internal, external and confluent events.
- `ta_F(h(s)) = ta_R(s)`; lambda_F(h(s)) is R's output.
- `delta_F(h(s), ...) = h(delta_R(s, ...))` for each kind of event.

R's events: internal at `t = ta_R`, external at `0 <= t < ta_R` with some
input, confluent at `t = ta_R` with some input. Imminent components (ta -
e = t) output; outputs travel through the couplings into input bags in
Cadmium's order (`flatten`'s `bags`: per level, ic sources in ic order,
then eic ones); imminent and receiving components take delta_con,
imminent ones delta_int, receiving ones delta_ext with their own e + t.
R's output is what its eoc sends, in eoc order; a model without ports
outputs every component's messages as `"<id>:<value>"`, compared as a
multiset, as fuse does.

A failed obligation prints a counterexample: each component's state, h
of it, the event (time, inputs, imminent components) and what differs.
A proven obligation whose premise the invariant rules out is marked
vacuous (e.g. an F branch no reachable state takes). `--prune` first
drops invariant conditions not proven initially or not kept relative to
the others (Houdini) and lists them. Exit 0 means PROVEN.

The encoding (`devsmap_z3`) follows the generator: if/else-if condition
maps (no true key: nothing happens), simultaneous assignments, the
default delta_con (delta_int, then delta_ext with e = 0), `ta` only as
`{"otherwise": "<state variable>"}`, C++ integer `/` and `%`, `"text" +
x` as the text of each operand. Domains:

- integer: unbounded integers. unsigned_integer: 32-bit words (`size_t`
  64) that wrap around as in C++ (`u - 1` at 0 is 4294967295, `u < 0`
  never holds; an int operand converts to unsigned).
- floating_point: the reals plus -0.0, inf, -inf and NaN. Zeros keep
  IEEE's signs (`1.0 / -0.0` is -inf, `-0.0 == 0.0`). NaN keeps its sign
  (`nan`, `-nan`); every comparison with it is false except `!=`,
  arithmetic passes a NaN operand on, and an invalid operation (inf - inf,
  0 * inf, 0 / 0) gives -nan, as on x86.
- string, boolean; a struct set of the definitions files is a datatype
  (`set(...)`, `.field`). A set of one field also converts to and from
  that field's value, as a C++ alias of the field's type would. Arrays
  and input bags are sequences (`bagSize()`, `bag(i)` with a
  Python-style index).
- A function the models call is uninterpreted; it returns its domain
  under `functions`, else a floating_point, and then a `/` or `%` that C++
  might do on integers through it is refused. The text of a number is
  uninterpreted too.

Refused outright: an integer literal with a leading zero (octal in C++),
and a state variable or port named `e`, `inf`, `nan`, `true` or `false`.
Parameter values come from `<coupled>_param.json` (one value per name),
else any value.

PROVEN assumes:

- exact arithmetic: real numbers, not IEEE doubles; signed integers
  without overflow. This also decides which events happen at the same
  time.
- functions and the text of numbers are pure: the same arguments give
  the same value at every call and in both models, and nothing else is
  known about them; an undeclared function returns a floating_point. A
  call without arguments is warned about.
- what C++ leaves undefined (a bag read out of range, an integer
  division by zero, inf or NaN converted to an integer) gives some value,
  the same for the same operands.
- the parameter values are the constants the C++ is compiled with.
- the generator and Cadmium run the models as encoded.

## Cadmium backend

- Generator: github.com/HazelGriffith/DEVSMap_to_Cadmium_Generator,
  submodule `third_party/devsmap_generator`, pinned to its unmerged
  `per-instance-initial-states` branch. It handles only internal
  couplings (no EIC/EOC); an atomic model runs as component `top` of a
  generated coupled model `wrapper`.
- Cadmium: submodule `cadmium_v2`, branch `dev-rt`.
- Compile: `g++ -std=c++17 -O1`, plus `-static` on Windows, then
  `CXXFLAGS`.
- Each run replaces `build/<model name>/`: `stage/` (generator inputs),
  `cpp/` (generated C++), `trace.csv` (simulation log).
- No event cap: a runaway model (e.g. zero time advance) stops at
  `DEVS_SIM_TIMEOUT`.

## Model format

`devsmap/SKILL.md` holds the atomic-model rules and is pasted into every
prompt. The rest:

- Files: `<name>_atomic.json`, `<name>_coupled.json`,
  `<name>_init_state.json`; a model file is `{"<name>": {...body...}}`.
  Component models are found by name next to the coupled file.
- Coupled body: `x`, `y`, `components`, `eic`, `eoc`, `ic`. One
  extension of the paper: `components` may map a model name to a list of
  ids (`{"player": ["A", "B"]}`) for several instances.
- Initial states live only in the top model's init file, in the paper's
  hierarchical form (`{"init_states": {"<top>": {"<id>": {"<var>":
  "<value>"}}}}`). Atomic files carry no `initial`, so a fused model is
  two files.
- The `_definitions` files a model's `include_sets` lists and the
  `<coupled>_param.json` parameter values go into the prompt and the
  prover. The paper's `_experiment` and `_metadata` files are not used;
  `cadmium_api` writes the experiment file per run.

## Setup

```
git submodule update --init      # not --recursive
pip install z3-solver            # the prover; --check needs it too
pip install -U google-genai      # or openai; only to call the LLM
cp .env.example .env             # then set the API key
python main.py --check           # needs g++, no API key
```

Cadmium's nested `json` submodule is not needed and fails on Windows
("Filename too long"), so don't recurse. Needs g++ with C++17 (Windows:
MinGW-w64 from MSYS2) and Python >= 3.10.

## Usage

    python main.py <name>_coupled.json   # fuse, e.g. devsmap/pingpong_coupled.json
    python main.py <name>_coupled.json --header C:/path/to/repo/include/model.hpp -I C:/path/to/repo/include
                                         # fuse against a repo's C++ headers
    python main.py <name>_coupled.json --prove <fused>_atomic.json [--prune]
                                         # prove a fusion equivalent, with
                                         # <fused>_mapping.json next to it
    python main.py --check               # full check suite
    python main.py --selftest            # only the fuse verifier check

`--header FILE` is force-included (g++ `-include`) and `-I DIR`
(repeatable) adds a header search directory; both are appended to
`CXXFLAGS` and need a model path.

| Exit | When |
|---|---|
| 0 | checks passed, fuse accepted a proposal, or --prove proved it |
| 1 | no proposal accepted in `FUSE_MAX_ITERS` rounds, or the LLM call failed (the log says why) |
| 1 | --prove: some obligation not proven (the log has its counterexample) |
| 1 | any other error: one `error: ...` line on stderr |
| 2 | bad usage |

| Variable | Default | Used by |
|---|---|---|
| `LLM_PROVIDER` | `gemini` (or `openai`) | fuse |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`, `LLM_API_KEY`) / `OPENAI_API_KEY` | none | fuse |
| `GEMINI_MODELS` or `GEMINI_MODEL` / `OPENAI_MODELS` or `OPENAI_MODEL` | `gemini-3.8-flash` / `gpt-6.1-sol` | fuse |
| `GEMINI_RETRIES` / `OPENAI_RETRIES` | 3 per model | fuse |
| `FUSE_MAX_ITERS` | 3 | fuse |
| `CXX` | `g++` | backend |
| `CADMIUM_INCLUDE` | `cadmium_v2/include` | backend |
| `CXXFLAGS` | none | backend; POSIX quoting, so paths use `/` |
| `DEVS_SIM_TIMEOUT` | 60 s per simulation | backend |

`fuse` loads `.env` into the environment (set variables win);
`--check` does not. `*_MODELS` is a comma-separated list tried in order. A
rejected key stops at once, a bad request or unknown model moves to the
next model, anything else is retried. A reply with a repeated JSON key is
rejected.

Outputs: an accepted fusion goes to a new `devsmap/out/<name>-<random>/`
as `<name>_atomic.json` + `<name>_init_state.json`. Simulation outputs go
to `build/`. Both are gitignored.

## Repo map

- `devsmap/`: ping-pong (`player_atomic`, `pingpong_coupled`,
  `pingpong_init_state`) and `SKILL.md`
- `fixtures/`: `fused_pingpong_*`, a hand fusion the checks expect to pass,
  with the mapping that proves it
- `cadmium_v2/`, `third_party/devsmap_generator/`: submodules
- `.env.example`: template for `.env`

## Scaling plan

In dependency order.

### 1 Upstream the generator fixes

Get the saved fix (per-instance initial states for multi-instance
components) merged into James's generator and repin
`third_party/devsmap_generator` to his main. The same PR switches his
`main.cpp` to `CSVLogger` (`logfile.csv`) and adds `bag(i)` for i >= 0
and `delta_con`.

### 2 Close the remaining generator gaps

Also upstream, each needed by a later item:

- **EIC/EOC and coupled-model ports**, for open models (3) and nested
  models (4).
- **Init states per nesting level**: check that a nested component gets
  its state from the hierarchical init file, and fix it if not.
- **Multi-branch `ta`**: SKILL.md forbids it until the generator
  translates it.

### 3 Open models: input scripts

Needs 2. Today nothing drives input ports, so open models are only
compiled. Generate input scripts and feed the same script to the original
and the proposal: cases from the model's ports and value sets, seeded
random scripts (the seed is logged), inputs exactly on internal event
times (exercises `delta_con`), bursts, and the empty script. Each input
port gets a Cadmium `IEStream` atomic, wired in by a test coupled model.
The verdict reads "matches under scripts S up to horizon H"; fuse refuses
an open model it has no scripts for.

### 4 Bigger models: fuse bottom-up

Needs 2 and 3. One LLM call over a whole large model hits state
explosion, and every check re-simulates all of it. Instead fuse the
innermost coupled model first, check it alone (its scripts can include the
inputs it received in a run of the full original), replace it by its
fused atomic (same ports) and move up a level. Finish with one end-to-end
run of the root's fused atomic against the original full model: passing
each level on finite scripts does not prove the composition.

Once a sub-model is fused its internal events are gone, so runs are
compared on EOC outputs (or a named set of ports) instead of every atomic
output.

### 5 Stronger checks

- **Float tolerance**: real-valued outputs need a per-instant comparison
  with a tolerance instead of exact values.
- **State mapping**: done symbolically by `--prove` (see "Proving a
  fusion"), given a hand-written mapping and invariant. Next: have the
  LLM state them with its fusion and prove each proposal.
- **Second simulator** (e.g. adevs): the only check against bugs shared by
  both runs. Needs a second DEVSMap translator; not planned until one
  exists.

### 6 Cost and reproducibility

- Prompt size grows with the model and nothing caps it.
- Save prompt, model name and reply per run; today the prompt is not kept.
- Every simulation regenerates and recompiles, and the reference run
  (several simulations) is redone on every fuse. Cache by model hash.

### 7 Size as a score

Size stays a report, never a reason to reject. A score over accepted
fusions (e.g. flagging one that just concatenates the components'
states) belongs in reporting.
