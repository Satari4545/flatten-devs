# DEVSMap atomic models

How to write a DEVSMap atomic model (Govind, Cardenas & Wainer, "DEVSMAP:
On the Path of Standardized DEVS Model Representation") that the
DEVSMap_to_Cadmium generator translates correctly. Proposals are not
checked up front: breaking these rules fails in the generator, the C++
compiler or the simulation, and that tool's output comes back as the error.

## Files

An atomic model named `<name>` is two files:

- `<name>_atomic.json`: `{"<name>": { ...model body... }}`, the model
  name as the only top-level key.
- `<name>_init_state.json`: `{"init_states": {"<name>": {"<var>": "<value>"}}}`,
  setting every state variable and nothing else.

The atomic file never sets initial state (there is no `"initial"` key).

## Model body

The DEVS octuple, with exactly these keys:

`x`, `y`, `s`, `delta_int`, `delta_ext`, `delta_con`, `lambda`, `ta`

- `x`, `y`, `s` map input ports, output ports and state variables to
  their C++ type: `double`, `int`, `bool` or `std::string`, or a type
  the coupled model already uses (see "Other C++ types").
- `delta_int`, `delta_ext` and `lambda` are condition maps: keys are
  conditions (boolean expressions), values are assignment maps
  `{<target>: <expression>}`. Transitions assign state variables,
  `lambda` assigns output ports. A condition map has at least one
  branch; branches are tried top to bottom and the first match wins.
  An optional `"otherwise"` entry (at most one, last) is the `else`.
  An empty assignment map `{}`, or no matching branch, means "no change"
  (for `lambda`, "no output"). The assignments of a branch run top to
  bottom, so a later expression sees the values assigned before it
  (`{"a": "b", "b": "a"}` sets both to the old `b`).
- `ta` is exactly `{"otherwise": "<state variable>"}`: the time advance
  is one `double` state variable (e.g. `sigma`). A passive state sets
  that variable to `inf`.
- `delta_con` is `{"otherwise": {}}` for the default (the internal
  transition, then the external one), or else a condition map like
  `delta_ext`, used when an input arrives exactly when the model is
  imminent.

Output values assigned by `lambda` are plain values of the port's type
(e.g. `"ball"`). Never encode a sender or component id inside a value --
with one exception: a *fused* atomic (the output of closure under
coupling) MUST prefix each output value with the originating component
id (`"<id>:<value>"`, e.g. `"P01:ball"`), so component identity survives
in the observable trace and the verifier can check which component
emitted each output.

Initial values are constants: for `double` a real number written with a
decimal point (`"0.1"`, `"-2.0"`) or `"inf"`; for `std::string` a string
literal (`"\"Send\""`).

## Expressions

Expressions are JSON strings. A condition is boolean; an assignment's
expression has the type of its target. Tokens are separated by spaces
only (no tabs or newlines). An expression may use:

- the model's state variables;
- real numbers with a decimal point: `0.1`, `2.0` (never `2`: an integer
  literal does integer arithmetic in C++);
- string literals of letters, digits and `_ . : -` (no spaces), e.g.
  `"Send"`;
- for `double`: `+ - * /`, unary `-`, comparisons `== != < <= > >=`; two
  `-` are always separated by a space (`- -c`, `a - -b`: C++ reads
  `--` as its decrement operator);
- for `std::string`: `==` and `!=`, with a string variable on at least one
  side (two literals would compare pointers in C++);
- for conditions: `&& || !`; parentheses anywhere; at most one
  comparison per operand (`a < b < c` is not allowed);
- `e` (elapsed time since the last transition), only in `delta_ext` and
  `delta_con`;
- `inf` (infinity), only as a whole value: an initial value or the
  entire right-hand side of a `delta_int` assignment (`"sigma": "inf"`);
- input ports, only in `delta_ext` and `delta_con`, and only through the
  bag operators,
  written exactly `p.bagSize() != 0` or `p.bagSize() == 0` (in
  conditions) and `p.bag(-1)` (the most recent value) or `p.bag(0)` (the
  first value), in assignments. A branch reads either only when `p`
  surely holds a value: `p` is the model's only input port, or the
  branch's condition has `p.bagSize() != 0` as a top-level `&&` term
  (not negated, not in parentheses, no `||` outside parentheses), e.g.
  `"p.bagSize() != 0 && sigma > 0.0 "`.

A state variable is always followed by a space unless it is the last
token of an assignment (the generator finds state variables by text
replacement): write `"( sigma + 0.1 ) / 2.0"` and `"0.1 < sigma "`, not
`"(sigma+0.1)/2.0"` or `"0.1 < sigma"`.

## Other C++ types

Keep any other C++ type the coupled model uses (a struct like
`position_t`, `uint8_t`, `float`). Then:

- Write a struct member as `var .field`, with a space before the dot
  (the generator adds `state.` only to a state variable followed by a
  space or `;`).
- Expressions and initial values may use constructors, functions, macros
  and constants from the model's own C++ headers (e.g.
  `"position_t()"`). No state variable name may be the
  ending of these names or of a member name.
- An expression is a single expression: no statement blocks (no
  `[&] { ... }()`), no assignments to other variables, no `addMessage`.
  Computation that needs statements is a named function in the model's
  headers, called by name.
- A phase is a `std::string`, never an `enum class`: the generated
  `operator<<` prints every state variable.

## Names

Names (the model, ports, state variables) are identifiers: letters,
digits and `_`, not starting with a digit or `_`, without `__`, and not
ending in `State` (the generator names each atomic's state struct
`<model>State`). In addition:

- No C++ keyword, no well-known C/C++ macro name (`EOF`, `NULL`,
  `INFINITY`; component ids from the coupled model, like `P01`, are fine),
  and none of the reserved names
  - `e`, `inf`, `state`, `id`, `initial_state`, `std`, `cadmium`,
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
- A state variable name is not the ending of another state variable,
  of an output port, or of `if`, `else`, `inf` (so never `f`, `nf`,
  `se`, `lse`).
- An output port name does not start with `if` or `else`.
