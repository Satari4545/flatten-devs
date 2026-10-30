# DEVSMap atomic models

How to write a DEVSMap atomic model (Govind, Cardenas & Wainer, "DEVSMAP:
On the Path of Standardized DEVS Model Representation") that this
project's checker (`devsmap_check.py`) accepts and the DEVSMap_to_Cadmium
generator translates correctly. Breaking these rules gets the proposal
rejected with an error saying where, or makes it fail to compile.

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
  their set, `string` or `R` (real numbers).
- `delta_int`, `delta_ext` and `lambda` are condition maps: keys are
  conditions (boolean expressions), values are assignment maps
  `{<target>: <expression>}`. Transitions assign state variables,
  `lambda` assigns output ports. Every condition map ends with the
  mandatory `"otherwise"` entry; branches are tried top to bottom and
  the first match wins. An empty assignment map `{}` means "no change"
  (for `lambda`, "no output"). The assignments of a branch run top to
  bottom, so a later expression sees the values assigned before it
  (`{"a": "b", "b": "a"}` sets both to the old `b`).
- `ta` is exactly `{"otherwise": "<state variable>"}`: the time advance
  is one state variable of set `R` (e.g. `sigma`). A passive state sets
  that variable to `inf`.
- `delta_con` is exactly `{"otherwise": {}}` and always means the
  internal transition, then the external one.

Output values assigned by `lambda` are plain values of the port's set
(e.g. `"ball"`). Never encode a sender or component id inside a value --
with one exception: a *fused* atomic (the output of closure under
coupling) MUST prefix each output value with the originating component
id (`"<id>:<value>"`, e.g. `"P01:ball"`), so component identity survives
in the observable trace and the verifier can check which component
emitted each output.

Initial values are constants: for `R` a real number written with a
decimal point (`"0.1"`, `"-2.0"`) or `"inf"`; for `string` a string
literal (`"\"Send\""`).

## Expressions

Expressions are JSON strings. A condition is boolean; an assignment's
expression has the set of its target. Tokens are separated by spaces
only (no tabs or newlines). An expression may use:

- the model's state variables;
- real numbers with a decimal point: `0.1`, `2.0` (never `2`: an integer
  literal does integer arithmetic in C++);
- string literals of letters, digits and `_ . : -` (no spaces), e.g.
  `"Send"`;
- for `R`: `+ - * /`, unary `-`, comparisons `== != < <= > >=`; two
  `-` are always separated by a space (`- -c`, `a - -b`: C++ reads
  `--` as its decrement operator);
- for `string`: `==` and `!=`, with a string variable on at least one
  side (two literals would compare pointers in C++);
- for conditions: `&& || !`; parentheses anywhere; at most one
  comparison per operand (`a < b < c` is not allowed);
- `e` (elapsed time since the last transition), only in `delta_ext`;
- `inf` (infinity), only as a whole value: an initial value or the
  entire right-hand side of a `delta_int` assignment (`"sigma": "inf"`);
- input ports, only in `delta_ext` and only through the bag operators,
  written exactly `p.bagSize() != 0` or `p.bagSize() == 0` (in
  conditions) and `p.bag(-1)` (the most recent value, in assignments).
  A branch reads `p.bag(-1)` only when `p` surely holds a value: `p` is
  the model's only input port, or the branch's condition has
  `p.bagSize() != 0` as a top-level `&&` term (not negated, not in
  parentheses, no `||` outside parentheses), e.g.
  `"p.bagSize() != 0 && sigma > 0.0 "`.

A state variable is always followed by a space unless it is the last
token of an assignment (the generator finds state variables by text
replacement): write `"( sigma + 0.1 ) / 2.0"` and `"0.1 < sigma "`, not
`"(sigma+0.1)/2.0"` or `"0.1 < sigma"`.

## Names

Names (the model, ports, state variables) are identifiers: letters,
digits and `_`, not starting with a digit or `_`, without `__`, and not
`e` or `inf`. In addition:

- Avoid C++ keywords and names the generated code uses (e.g. `Port`,
  `output`, `state`, `main`, names ending in `State`): they do not
  compile.
- The model name, input ports, output ports and state variables are all
  distinct (no port in both `x` and `y`, no state variable named like a
  port).
- A state variable name is not the ending of another state variable,
  of an output port, or of `if`, `else`, `inf` (so never `f`, `nf`,
  `se`, `lse`).
- An output port name does not start with `if` or `else`.
