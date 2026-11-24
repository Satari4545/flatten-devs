# DEVSMap atomic models

How to write a DEVSMap atomic model in the format of Govind, Cardenas &
Wainer, "DEVSMAP: On the Path of Standardized DEVS Model Representation".
Proposals are not checked up front: breaking these rules fails in the
generator, the C++ compiler or the simulation, and that tool's output comes
back as the error.

## Files

An atomic model named `<name>` is two files:

- `<name>_atomic.json`, with exactly three top-level keys:
  `{"<name>": {...}, "parameters": {...}, "include_sets": [...]}`.
  `parameters` maps each parameter to its domain (`{}` if none);
  `include_sets` lists the definitions files of the sets the domains use
  (`[]` if none). A fused model's `parameters` is the union of the input
  atomics' parameters, and its `include_sets` lists the input models'
  definitions files its domains need.
- `<name>_init_state.json`:
  `{"init_states": {"<name>": {"<state variable>": "<value>"}}}`, setting
  every state variable, and nothing else, to a constant.

## Model

The model object has all eight keys of the DEVS octuple:

`x`, `y`, `s`, `delta_int`, `delta_ext`, `delta_con`, `lambda`, `ta`

`x`, `y` and `s` map input ports, output ports and state variables to their
domains. Copy every domain exactly as the input models write it, set name or
type name alike, and never convert between the two; a new variable takes
the domain the input models use for such values. The paper's basic sets are
`integer`, `unsigned_integer`, `floating_point`, `boolean` and `char`;
other sets (e.g. `string` or a struct) come from the input models'
definitions files.

The other five keys are condition maps: each key is a condition, each value
what happens when it holds.

- Flat: no condition map inside another.
- For any state and input bags, exactly one key is true; key order means
  nothing. Write priority as explicit guards that repeat the negations of
  the higher-priority conditions: `"stop.bagSize() != 0"`, then
  `"stop.bagSize() == 0 && add.bagSize() != 0"`.
- Every map has exactly one `"otherwise"` (true when no other key is), last,
  even when its value is empty.

### Transitions

`delta_int`, `delta_ext` and `delta_con` map each condition to assignments
`{"<state variable>": "<expression>"}`. A state variable left out keeps its
value (`{}` changes nothing). An expression never reads a variable assigned
in the same branch: write it in terms of the old state and the input bags.

`delta_con` (input arriving exactly when the model is imminent) is always
written out in full, never as just `{"otherwise": {}}`. A model without a
confluent function of its own (an input model with no `delta_con`, or just
`{"otherwise": {}}`) uses the default: the internal transition, then the
external transition with `e = 0`. Compose the two for every phase that can
be confluent, in terms of the old state: where the external part reads a
variable the internal part assigns, use that assignment's expression. A
counter that ticks every 1.0 and adds what arrives on `add`:

    "delta_int": {"otherwise": {"count": "count + 1", "sigma": "1.0"}},
    "delta_ext": {
      "add.bagSize() != 0": {"count": "count + add.bag(-1)", "sigma": "sigma - e"},
      "otherwise": {"sigma": "sigma - e"}},
    "delta_con": {
      "add.bagSize() != 0": {"count": "count + 1 + add.bag(-1)", "sigma": "1.0"},
      "otherwise": {"count": "count + 1", "sigma": "1.0"}}

### Output and time advance

`lambda` keys are conditions on the state only; each value maps output
ports to one expression each (`{}` sends nothing). `lambda` never changes
the state: a value that is sent and also needed later is computed and
stored by a transition, and `lambda` only reads it.

`ta` is `{"otherwise": "sigma"}`: one floating-point state variable holds
the time advance and is `inf` while the model is passive.

## Expressions

Keys and values are JSON strings, each a single expression on one line: no
statement blocks, no assignments, no side effects. An expression may use:

- state variables, parameters, and `var.field` for a field of a struct;
- `e` (the time since the last transition), only in `delta_ext`;
- input ports, only in `delta_ext` and `delta_con`, and only through the
  bag operators: `p.bagSize()`, and `p.bag(i)` or `p.bag(i).field` with a
  Python-style index (`p.bag(0)` is the first message, `p.bag(-1)` the
  last). A branch reads `p.bag(i)` only when its condition makes sure the
  message exists (e.g. `p.bagSize() != 0` as an `&&` term);
- the operators `== != <= >= < > && || ( ) + - * / %`, at most one
  comparison per operand (no `a < b < c`) and two `-` always separated by a
  space (`a - -b`); `"text" + number` builds a string;
- values: numbers (a floating-point value has a decimal point, `2.0`; an
  integer has none, `2`), `true`, `false`, `inf`, `-nan`, string literals
  (`"Wait"`), arrays `[a, b, c]`, and struct values `set_name(v1, ..., vn)`
  giving every field in the order of the set's definitions file;
- functions the input models already call. Never invent a function.

There is no `!` (write the opposite comparison: `p.bagSize() == 0`,
`phase != "Wait"`), no `?:`, no cast, no method other than the bag
operators, and no C++.

A phase (an enumeration) is a string literal on a state variable of the
input models' string domain: the condition `phase == "Wait"`, the
assignment `"phase": "\"Send\""` (JSON escapes the quotes).

## Names

Names (the model, ports, state variables) are identifiers: letters, digits
and `_`, not starting with a digit or `_`, without `__`, and not ending in
`State` (the generator names each atomic's state struct `<model>State`).
In addition:

- No C++ keyword, no well-known C/C++ macro name (`EOF`, `NULL`,
  `INFINITY`; component ids from the coupled model, like `P01`, are fine),
  and none of the reserved names
  - `e`, `inf`, `nan`, `state`, `id`, `initial_state`, `std`, `cadmium`,
    `Port`, `Atomic`, `Coupled`, `Component`, `output`,
    `internalTransition`, `externalTransition`, `confluentTransition`,
    `timeAdvance`, `logState`, `addComponent`, `addCoupling`,
    `addInPort`, `addOutPort` (used by the generated C++ and Cadmium),
  - `main`, `model`, `rootCoordinator`, `wrapper`, `top` (used by the
    generated main.cpp and the Cadmium backend),
  - `errno`, `stdin`, `stdout`, `stderr` (C macros).
- The model name, input ports, output ports and state variables are all
  distinct (no port in both `x` and `y`, no state variable named like a
  port).
- A state variable name is not the ending of another name the model uses
  (a state variable, port, parameter, field or function), nor of `if`,
  `else`, `inf` (so never `f`, `nf`, `se`, `lse`).
- An output port name does not start with `if` or `else`.
