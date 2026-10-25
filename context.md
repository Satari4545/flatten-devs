# devs-flatten: project context

## Goal

Turn a coupled DEVS model into ONE equivalent atomic model, automatically,
and check the equivalence by simulation. Input and output are DEVSMap, the
JSON representation from Govind, Cárdenas & Wainer, "DEVSMAP: On the Path
of Standardized DEVS Model Representation":

    coupled-model DEVSMap in  ->  DEVSMap atomic model out,
    accepted only if Cadmium runs both to the same output trace.

Target users are DEVS practitioners, so the DEVSMap we emit follows the
paper as closely as possible; anything we invent is a reason for an expert
to reject the tool.

## Architecture

| Part | Module | Job |
|---|---|---|
| Load, flatten | `devsmap.py`, `devsmap_flatten.py` | Loads a model, its component models and initial states, without validating them (a file it cannot read is one `ValueError` naming it). Dissolves nesting into leaf atomics and leaf-to-leaf couplings for the LLM summary. Exact, no AI. |
| Check | `devsmap_check.py` | Verifies an LLM proposal (atomic + init state) against `devsmap/SKILL.md`'s rules, the subset the generator translates faithfully; a failure is one `ValueError` written for the LLM. |
| Simulate | `cadmium_api.py`, `james_patches.py` | DEVSMap -> Cadmium v2 C++ via the lab's generator; compile, run, parse the trace. Failures are `CadmiumError`. |
| Fuse | `fuse.py` | Library: reference run, prompt, LLM call, check + verify loop. |
| CLI, checks | `main.py`, `verify.py` | `main.py` is the command line; `verify.py` holds the check suite and its fixtures. |

The LLM sees the coupled model, the flattened summary, `SKILL.md` and the
start of the reference trace, and proposes a fused atomic (it looks for
state invariants, e.g. exactly one player holds the ball). It is never
trusted with coupling semantics, and a flatten bug cannot fake a pass: the
reference trace comes from running the original model in Cadmium.
The LLM proposes, Cadmium disposes.

The user's coupled model is trusted: loaded, not validated. Malformed
input fails loudly in the loader, the generator or g++; a silent trap in
a user model (e.g. an unguarded `p.bag(-1)`) is the user's
responsibility. The LLM's output is not trusted: `devsmap_check` verifies
each proposal before anything is written or compiled, and a failure is
fed back as `invalid DEVSMap: ...`, like a trace mismatch or a g++ error.

## Verification: what it covers

`fuse.reference_run` simulates the original; `fuse.verify` simulates a
proposal that passed `devsmap_check` and compares.

- **Window:** the time of the 10th reference output event, or of the
  last one if fewer than 10 occur by t = 1000. The LLM sees the events
  up to the window and nothing after it.
- **Horizon:** 3x the window, grown x10 (up to t = 3000) until at least
  one reference event lies after the window, so replaying the shown
  events fails. A divergence after the window is reported to the LLM
  only as "diverges after that window", never with the hidden events.
- **Refused** (`ValueError`, before the LLM is asked): no output event by
  t = 1000, a window at t = 0, or no event after the window.
- **Same cut:** `fuse.observe` runs the reference and every proposal to
  horizon + 1e-6 and keeps the events with (rounded) t <= horizon, so
  float drift in event times cannot make a model fail against itself.
- **Compared:** the multiset of (time, port, value) over ALL atomic
  outputs, including those that only travel along internal couplings;
  the sender is ignored. Times are rounded to 10 decimals; values are
  compared exactly as logged (reals with 17 significant digits).
- **Size:** state variables and `delta_int`/`delta_ext`/`lambda` branches,
  proposal vs the sum over the components, are printed on accept, with a
  warning if not smaller. Size is not a rejection criterion.
- **Runaway cap:** a run that exceeds 100 000 trace rows (e.g. a zero or
  negative time advance) stops with a `CadmiumError`.

ACCEPTED means: the proposal reproduced the original's outputs in the runs
performed, up to the horizon. It does not cover:

- behavior after the horizon or from other initial states;
- open models (input ports): there is no stimulus yet, so the backend
  rejects them;
- bugs shared by both runs: reference and proposal go through the same
  generator, patches, driver and Cadmium.

## Cadmium backend

`cadmium_api.simulate(path, end_time, build_dir=None, max_events=100_000)`:

1. `devsmap.load_model(path)`, then refuses (`CadmiumError`) what the
   generator would drop: an open top model (`x`/`y`, hence EIC/EOC; the
   error says "closed") and nested coupled components (the error says
   "flat"); and the names the backend generates itself: any model named
   `top`, an atomic target named `wrapper`.
2. Stages in a temp dir only what the generator reads: the model and its
   component models, re-written from the loaded bodies with
   `devsmap.write_json` as `{name: body}` (the generator takes the first
   key as the model name and reads with the platform encoding; the
   rewrite is ASCII with the model key alone), a generated
   `<top>_init_state.json` (from `model["init"]`, values in `s` order; a
   missing state variable is a `CadmiumError`) and
   `<top>_experiment.json`. Stray sibling files are never read. An atomic
   target runs as component `top` of a generated coupled model `wrapper`.
3. Copies the generator, applies `james_patches.py`, runs it.
4. Compiles `cadmium/driver/main.cpp` against the generated headers
   (`gen/top.hpp` aliases the top model): `g++ -std=c++17`, `-static` on
   Windows only.
5. Runs the driver with `--max-events` (exit code 3 when exceeded) and
   parses its CSV trace into `{"time", "sender", "port", "value"}`
   events.

A failing step is a `CadmiumError` with up to 1500 characters of the
tool's output, from 3 lines before its first "error" line, local
directories stripped.

Generator: github.com/HazelGriffith/DEVSMap_to_Cadmium_Generator @
`d2b6549`, pinned submodule at `third_party/devsmap_generator`, never
edited in place. Upstream has no licence file (open question for the
owner). The patches are textual, applied to the staged copy, and every
anchor must match its expected count or the run fails (`PatchError`):

| Patch | Fixes |
|---|---|
| P1 | Old Cadmium include layout -> Cadmium v2 paths |
| P2 | DEVSMap set names (`string`, `R`) -> C++ types |
| P3 | Per-instance initial states: state/atomic constructors and `addComponent(id, State(...))` from the staged init file |

Generator limits that are not patched, and who keeps models out of them
(a user model is not checked: keeping it inside `devsmap_check`'s rules
is the user's responsibility):

| Limit | Handled by |
|---|---|
| Only IC couplings, no coupled-model ports, init looked up under the top model only | `cadmium_api`: closed, flat models only |
| No `delta_con` generation (Cadmium's default: internal, then external) | `devsmap_check` (LLM output): `delta_con` must be `{"otherwise": {}}` |
| `ta` only as one state variable | `devsmap_check` (LLM output): `ta` must be `{"otherwise": "<R state variable>"}` |
| State variables prefixed by text replacement of `"<var> "` / `"<var>;"` | `devsmap_check` (LLM output): a space after every state variable, no state variable is the ending of another name |
| `p.bag(-1)` reads the bag unchecked (an empty bag is UB) | `devsmap_check` (LLM output): only for the sole input port or under a top-level `p.bagSize() != 0 &&` guard |
| First key of a model file is the model name; files read in the platform encoding | `cadmium_api`: stages `{name: body}` in ASCII |

## Format decisions

`devsmap/SKILL.md` holds the rules for atomic models (body, expressions,
names): it is pasted into every LLM prompt, so it covers only what the
LLM writes, and `devsmap_check` enforces it on proposals. The rest of the
format is here; nothing checks it in a user model.

- Files follow the paper's naming: `<name>_atomic.json`,
  `<name>_coupled.json`, `<name>_init_state.json`. A model file is
  `{"<name>": {...body...}}`, the model name its only top-level key.
- **Dropped for now: the paper's companion files.** The paper's full
  file set also has `_experiment.json` (model under test, init and param
  files, experimental frame, `time_span`), `_param.json` (parameter
  values), `_metadata.json` (author, description, keywords) and
  `<set>_definitions.json` (what `R`, `string` mean), plus the
  `include_sets` and `"parameters"` keys inside model files. Nothing here
  reads them: the generator ignores `_param`, `_metadata` and
  `_definitions`, and the experiment file it does need is written by
  `cadmium_api` per run (step 2 above). So `devsmap/` ships only the model
  and init-state files, and `devsmap_check` refuses the extra keys in a
  proposal. We may need to bring them back: for a model with parameters,
  an open model driven by an experimental frame, or exchanging models
  with other DEVSMap tools or a model repository. Restoring them means
  adding the files, allowing the keys in `devsmap_check.ATOMIC_KEYS`, and
  documenting them in `SKILL.md` if the LLM must write them.
- Coupled model body: exactly `x`, `y`, `components`, `eic`, `eoc`,
  `ic`. `ic` entries are `component_from`, `port_from`, `component_to`,
  `port_to`; `eic` entries omit `component_from`, `eoc` entries omit
  `component_to`. A coupling joins two ports of the same set. Component
  ids are plain (no dots). Component models are found by name next to
  the coupled file.
- ONE extension of the paper: `components` maps a model name to a LIST of
  ids (`{"player": ["A", "B"]}`); the paper's single-id form cannot express
  two instances of one model.
- Initial states live only in the top model's `<name>_init_state.json`, in
  the paper's hierarchical form (`{"init_states": {"<top>": {"<id>":
  {"<var>": "<value>"}}}}`, one level deeper per nested coupled
  component); component models' init files are not read. `load_model`
  resolves them into
  `model["init"]` (`{instance path: {var: value}}`). Atomic files never
  carry `initial`, so a fused model is two files.
- Output values are plain domain values (`"ball"`), no sender prefix: who
  sent an event is internal structure, and removing it is the point of
  fusion.
- The fused atomic carries the coupled model's interface. For a closed
  model like ping-pong it surfaces the components' output port (`send`),
  so the behavior is observable at all.
- Names are identifiers starting with a letter, no `__`, not `e` or
  `inf` (the expression language uses them). C++ keywords and names the
  generated code uses are left to g++, whose errors are fed back too.
- `devsmap.write_json` is the one JSON writer (2-space indent, ASCII).

## Setup

```
git clone <repo-url> && cd <repo>
git submodule update --init      # cadmium_v2 + the generator, NOT --recursive
pip install -U google-genai      # only needed to call the LLM
cp .env.example .env             # then set GEMINI_API_KEY in .env
python main.py --check           # needs g++, no API key
```

- Two submodules. Cadmium's own nested `json` submodule is not needed and
  fails on Windows with "Filename too long", so don't clone with
  `--recurse-submodules` or update with `--recursive`.
- g++ with C++17 (Windows: MinGW-w64 from MSYS2; if `sim_driver` crashes,
  put `C:\msys64\ucrt64\bin` first on PATH). Python >= 3.10.

## Usage

    python main.py <name>_coupled.json   # fuse, e.g. devsmap/pingpong_coupled.json
    python main.py --check               # full check suite
    python main.py --selftest            # only the fuse verifier check

| Exit | When |
|---|---|
| 0 | `--help`; checks passed; fuse accepted a proposal |
| 1 | fuse accepted no proposal in `FUSE_MAX_ITERS` rounds, or the LLM call failed (no SDK, every request failed; the log says why, no `error:` line) |
| 1 | any other error: one `error: ...` line on stderr (bad input, backend, settings, failed check) |
| 2 | bad usage |

stdout replaces characters the console cannot encode (an LLM reply on a
cp1252 console).

| Variable | Default | Used by |
|---|---|---|
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`, `LLM_API_KEY`) | none | fuse |
| `GEMINI_MODELS` (comma-separated) or `GEMINI_MODEL` | `gemini-3.8-flash` | fuse |
| `GEMINI_RETRIES` | 3 per model | fuse |
| `FUSE_MAX_ITERS` | 3 LLM rounds | fuse |
| `CXX` | `g++` | backend |
| `CADMIUM_INCLUDE` | `cadmium_v2/include` | backend |
| `CADMIUM_DRIVER` | `cadmium/driver/main.cpp` | backend |
| `DEVS_GENERATOR_DIR` | `third_party/devsmap_generator` | backend |
| `DEVS_SIM_TIMEOUT` | 60 s per simulation | backend |

fuse also reads its variables from `.env` (the environment wins).

Outputs: an accepted fusion goes to a new `devsmap/out/<name>-<random>/`
(gitignored) as `<name>_atomic.json` + `<name>_init_state.json`; earlier
results are never overwritten. Proposals and builds live in temp dirs and
are removed afterwards.

## Repo map

- `devsmap.py`: loader, `write_json`; `devsmap_flatten.py`: flatten, LLM
  summary; `devsmap_check.py`: the LLM-proposal check
- `devsmap/`: the ping-pong model (`player_atomic`, `pingpong_coupled`,
  `pingpong_init_state`) and `SKILL.md`, the atomic-model rules the LLM
  gets
- `fixtures/`: the hand fusion (`fused_pingpong_*`), a fused ping-pong
  written by hand that the checks expect to pass
- `cadmium_api.py`: `simulate()`; `james_patches.py`: P1-P3
- `cadmium/driver/main.cpp`: sim driver + CSV trace logger
- `fuse.py`: fusion library; `main.py`: CLI; `verify.py`: check suite
- `cadmium_v2/`, `third_party/devsmap_generator/`: pinned submodules
- `.env.example`: template for `.env` (gitignored, like `devsmap/out/`)

## Conventions

- Python stdlib only, except `google-genai` (imported only when the LLM
  is called).
- Models are data. LLM-written expressions reach the generated C++ only
  after `devsmap_check`'s whitelist parser accepts them.
- Error messages name files, never local absolute paths.
