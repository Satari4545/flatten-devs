"""Check an LLM-proposed DEVSMap atomic model before anything is built.

    check_proposal(atomic_doc, init_doc) -> the model name

Accepts the subset of DEVSMap that the vendored generator translates
faithfully (devsmap/SKILL.md). Anything else is one ValueError written
for the LLM: what is wrong, where, and how to fix it. Names that only
break compilation (C++ keywords, generated identifiers) are left to g++.

The generator finds state variables by text replacement of "<var> " and
"<var>;" and copies the rest verbatim into C++, so some checks are
textual on purpose.
"""

import re

import devsmap

SETS = ("string", "R")
ATOMIC_KEYS = ("x", "y", "s", "delta_int", "delta_ext", "delta_con",
               "lambda", "ta")
RESERVED = ("e", "inf")
IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
STRING_LITERAL = re.compile(r'"[A-Za-z0-9_.:-]*"')
REAL_CONSTANT = re.compile(r"-?[0-9]+\.[0-9]+|inf")
# Bag operators, exactly as the generator's rewrite patterns spell them.
BAG_FORMS = "'p.bagSize() != 0', 'p.bagSize() == 0', 'p.bag(-1)'"


def check_proposal(atomic_doc, init_doc):
    """Check a proposed atomic document and its init-state document (both
    parsed JSON). Returns the model name; raises ValueError on anything
    outside devsmap/SKILL.md's rules."""
    name = devsmap.model_name(atomic_doc, "the atomic document")
    _check_name("the atomic document", "model name", name)
    body = atomic_doc[name]
    if not isinstance(body, dict):
        raise ValueError(f"atomic '{name}': the model body must be an "
                         f"object")
    _check_atomic(name, body)
    _check_init(name, body["s"], init_doc)
    return name


def _check_name(where, kind, name):
    if not (isinstance(name, str) and IDENT.fullmatch(name)
            and "__" not in name):
        raise ValueError(f"{where}: {kind} {name!r} is not an identifier "
                         f"(letters, digits, _, starting with a letter, "
                         f"no __)")
    if name in RESERVED:
        raise ValueError(f"{where}: {kind} '{name}' is reserved (e is the "
                         f"elapsed time, inf infinity); pick another name")


def _check_sets(where, names, kind):
    """x, y or s: {name: set}."""
    if not isinstance(names, dict):
        raise ValueError(f"{where}: expected {{{kind}: set}}, got {names!r}")
    for name, typ in names.items():
        _check_name(where, kind, name)
        if typ not in SETS:
            raise ValueError(f"{where}: {kind} '{name}' has set {typ!r} "
                             f"(only {list(SETS)})")


# --- expressions ------------------------------------------------------------

_TOKEN = re.compile(r"""
    (?P<space>\ +)
  | (?P<bag_size>[A-Za-z_][A-Za-z0-9_]*\.bagSize\(\)\ [!=]=\ 0)
  | (?P<bag>[A-Za-z_][A-Za-z0-9_]*\.bag\(-1\))
  | (?P<number>[0-9]+(?:\.[0-9]+)?)
  | (?P<string>"[^"]*")
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>==|!=|<=|>=|&&|\|\||[-+*/()<>!])
""", re.X)


def _tokenize(text):
    """[(kind, text, end offset)], spaces dropped."""
    tokens, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            hint = f" (bag operators: {BAG_FORMS})" if text[pos] == "." else ""
            raise ValueError(f"unsupported text {text[pos:]!r}{hint}")
        pos = m.end()
        if m.group() == "-" and text.startswith("-", pos):
            raise ValueError("'--' is C++'s decrement operator; write '- -'")
        if m.lastgroup != "space":
            tokens.append((m.lastgroup, m.group(), pos))
    return tokens


def _guarded_ports(cond):
    """Ports p whose `p.bagSize() != 0` is a top-level && term of `cond`:
    a branch with that condition only runs when p holds a value."""
    tokens, depth, ports = _tokenize(cond), 0, set()
    for i, (kind, text, _) in enumerate(tokens):
        depth += (text == "(") - (text == ")")
        if depth == 0 and text == "||":
            return set()
        if depth == 0 and kind == "bag_size" and text.endswith("!= 0") \
                and (i == 0 or tokens[i - 1][1] != "!"):
            ports.add(text.split(".")[0])
    return ports


def _set_of(typ):
    return "string" if typ == "literal" else typ


class _Parser:
    """Type of one expression: "R", "bool", "string" or "literal", with C++
    precedence. `inputs` is the model's x in delta_ext, else None."""

    def __init__(self, text, states, inputs, assignment):
        self.text, self.states, self.inputs = text, states, inputs
        self.assignment = assignment
        self.tokens, self.pos = _tokenize(text), 0
        self.state_uses = []  # (name, end offset)

    def parse(self):
        typ = self.disjunction()
        if self.pos < len(self.tokens):
            raise ValueError(f"unexpected {self.tokens[self.pos][1]!r}")
        # The generator appends ";" after an assignment's expression.
        for name, end in self.state_uses:
            if not (self.text[end:end + 1] == " "
                    or end == len(self.text) and self.assignment):
                raise ValueError(f"state variable '{name}' must be followed "
                                 f"by a space")
        return typ

    def peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos][1]
        return None

    def next(self):
        if self.pos == len(self.tokens):
            raise ValueError("unexpected end of expression")
        self.pos += 1
        return self.tokens[self.pos - 1]

    def chain(self, ops, operand, typ):
        """operand (op operand)*, every operand of type `typ`."""
        left = operand()
        while self.peek() in ops:
            op = self.next()[1]
            right = operand()
            if left != typ or right != typ:
                raise ValueError(f"'{op}' needs {typ} operands")
            left = typ
        return left

    def disjunction(self):
        return self.chain(("||",), self.conjunction, "bool")

    def conjunction(self):
        return self.chain(("&&",), self.comparison, "bool")

    def comparison(self):
        left = self.sum()
        if self.peek() not in ("==", "!=", "<", "<=", ">", ">="):
            return left
        op = self.next()[1]
        right = self.sum()
        sets = {_set_of(left), _set_of(right)}
        if sets == {"string"} and op in ("==", "!=") \
                and (left, right) != ("literal", "literal"):
            return "bool"
        if sets != {"R"}:
            raise ValueError(
                f"'{op}' cannot compare {left} with {right} (R with R; "
                f"strings only with == or !=, and not two literals)")
        return "bool"

    def sum(self):
        return self.chain(("+", "-"), self.product, "R")

    def product(self):
        return self.chain(("*", "/"), self.unary, "R")

    def unary(self):
        for op, typ in (("-", "R"), ("!", "bool")):
            if self.peek() == op:
                self.next()
                if self.unary() != typ:
                    raise ValueError(f"'{op}' needs a {typ} operand")
                return typ
        return self.primary()

    def primary(self):
        kind, text, end = self.next()
        if kind == "number":
            if "." not in text:
                raise ValueError(f"write {text} as {text}.0 (an integer "
                                 f"literal does integer arithmetic in C++)")
            return "R"
        if kind == "string":
            if not STRING_LITERAL.fullmatch(text):
                raise ValueError(f"string literal {text} may only contain "
                                 f"letters, digits and _ . : -")
            return "literal"
        if text == "(":
            typ = self.disjunction()
            if self.next()[1] != ")":
                raise ValueError("expected ')'")
            return typ
        if kind == "name":
            return self.name(text, end)
        if kind in ("bag_size", "bag"):
            return self.bag(kind, text)
        raise ValueError(f"unexpected {text!r}")

    def name(self, text, end):
        if text in self.states:
            self.state_uses.append((text, end))
            return self.states[text]
        if text == "e" and self.inputs is not None:
            return "R"
        if text == "e":
            raise ValueError("e (elapsed time) may only be used in delta_ext")
        if text == "inf":
            raise ValueError("inf may only be a whole value: an initial "
                             "value or a delta_int assignment's entire "
                             "right-hand side")
        if self.inputs and text in self.inputs:
            raise ValueError(f"input port '{text}' may only be used through "
                             f"a bag operator: {BAG_FORMS}")
        raise ValueError(f"unknown name '{text}'")

    def bag(self, kind, text):
        port = text.split(".")[0]
        if self.inputs is None:
            raise ValueError("bag operators may only be used in delta_ext")
        if port not in self.inputs:
            raise ValueError(f"'{port}' is not an input port")
        if kind == "bag_size":
            if self.assignment:
                raise ValueError(f"{text} may only be used in a condition")
            return "bool"
        if not self.assignment:
            raise ValueError(f"{text} may only be used in an assignment")
        return self.inputs[port]


def _check_expr(where, expr, want, states, inputs=None, assignment=False):
    """`expr` must be an expression SKILL.md allows, of set `want` ("bool"
    for a condition)."""
    if not isinstance(expr, str):
        raise ValueError(f"{where}: expression must be a string, "
                         f"got {expr!r}")
    try:
        typ = _Parser(expr, states, inputs, assignment).parse()
    except ValueError as err:
        raise ValueError(f"{where}: {expr!r}: {err}") from None
    except RecursionError:
        raise ValueError(f"{where}: expression nested too deeply") from None
    if _set_of(typ) != want:
        raise ValueError(f"{where}: {expr!r} is {_set_of(typ)}, "
                         f"expected {want}")


def _check_value(where, value, typ):
    """An initial value: a constant of the state variable's set."""
    if typ == "R":
        ok, expected = REAL_CONSTANT, 'a real number like "0.1", or "inf"'
    else:
        ok, expected = STRING_LITERAL, ('a string literal like "\\"Send\\"" '
                                        '(letters, digits and _ . : -)')
    if not (isinstance(value, str) and ok.fullmatch(value)):
        raise ValueError(f"{where}: initial value {value!r} must be "
                         f"{expected}")


# --- the model --------------------------------------------------------------

def _check_atomic(name, body):
    where = f"atomic '{name}'"
    if "initial" in body:
        raise ValueError(f"{where}: \"initial\" is not allowed in an "
                         f"atomic model; put initial states in the "
                         f"{name}_init_state.json document")
    missing = [key for key in ATOMIC_KEYS if key not in body]
    extra = sorted(set(body) - set(ATOMIC_KEYS))
    if missing or extra:
        raise ValueError(f"{where}: an atomic body has exactly the keys "
                         f"{list(ATOMIC_KEYS)}; missing {missing}, "
                         f"unknown {extra}")
    _check_names(where, name, body)
    states = body["s"]
    _check_function(where, body, "delta_int", states, "state variable")
    _check_function(where, body, "delta_ext", states, "state variable",
                    inputs=body["x"])
    _check_function(where, body, "lambda", body["y"], "output port")
    if body["delta_con"] != {"otherwise": {}}:
        raise ValueError(f"{where}: delta_con must be "
                         f"{{\"otherwise\": {{}}}} (Cadmium's default "
                         f"confluent transition)")
    ta = body["ta"]
    if not (isinstance(ta, dict) and list(ta) == ["otherwise"]
            and isinstance(ta["otherwise"], str)
            and states.get(ta["otherwise"]) == "R"):
        raise ValueError(f"{where}: ta must be {{\"otherwise\": "
                         f"\"<state variable of set R>\"}}, got {ta!r}")


def _check_names(where, name, body):
    """Ports and state variables: identifiers, distinct, and safe from the
    generator's text replacement."""
    seen = {name: "the model name"}
    for key, kind in (("x", "input port"), ("y", "output port"),
                      ("s", "state variable")):
        _check_sets(where, body[key], kind)
        for n in body[key]:
            if n in seen:
                raise ValueError(f"{where}: {kind} '{n}' is also "
                                 f"{seen[n]}; names must be distinct")
            seen[n] = ("an " if kind[0] in "aeiou" else "a ") + kind
    # "<var> " would also match the end of these names ("inf" as " inf;").
    for var in body["s"]:
        for other in (*body["s"], *body["y"], "if", "else", "inf"):
            if other != var and other.endswith(var):
                raise ValueError(f"{where}: state variable '{var}' is the "
                                 f"end of '{other}'; the generator would "
                                 f"rewrite '{other}'; rename '{var}'")
    # The generator takes lambda lines starting with if/else for if/else.
    for port in body["y"]:
        if port.startswith(("if", "else")):
            raise ValueError(f"{where}: output port '{port}' must not "
                             f"start with 'if' or 'else'")


def _check_function(where, body, key, targets, kind, inputs=None):
    """A condition map ending in "otherwise" whose branches assign
    `targets`; `inputs` (delta_ext only) allows e and bag operators."""
    cmap = body[key]
    if not isinstance(cmap, dict) or list(cmap)[-1:] != ["otherwise"]:
        raise ValueError(f"{where} {key}: expected a condition map "
                         f"ending in \"otherwise\"")
    states = body["s"]
    for cond, branch in cmap.items():
        w = f"{where} {key} [{cond}]"
        if cond != "otherwise":
            _check_expr(w, cond, "bool", states, inputs)
        if not isinstance(branch, dict):
            raise ValueError(f"{w}: expected {{{kind}: expression}}, "
                             f"got {branch!r}")
        # p.bag(-1) of an empty bag is undefined behaviour in C++.
        filled = (set(inputs) if inputs and len(inputs) == 1
                  else _guarded_ports(cond))
        for target, expr in branch.items():
            if target not in targets:
                raise ValueError(f"{w}: unknown {kind} {target!r}")
            if (key == "delta_int" and expr == "inf"
                    and targets[target] == "R"):
                continue
            _check_expr(f"{w} '{target}'", expr, targets[target],
                        states, inputs, assignment=True)
            for token, text, _ in _tokenize(expr):
                port = text.split(".")[0]
                if token == "bag" and port not in filled:
                    raise ValueError(
                        f"{w} '{target}': {text} may be empty here; read "
                        f"it only in a branch whose condition has "
                        f"'{port}.bagSize() != 0' as a top-level && term "
                        f"(or when '{port}' is the only input port)")


def _check_init(name, states, doc):
    """The init-state document sets every state variable of `states` to a
    constant of its set, and nothing else."""
    where = f"{name}_init_state.json"
    if not (isinstance(doc, dict) and list(doc) == ["init_states"]
            and isinstance(doc["init_states"], dict)
            and list(doc["init_states"]) == [name]
            and isinstance(doc["init_states"][name], dict)):
        raise ValueError(f"{where}: expected {{\"init_states\": "
                         f"{{\"{name}\": {{<state variable>: <value>}}}}}}, "
                         f"keyed by the atomic's model name")
    init = doc["init_states"][name]
    missing = [var for var in states if var not in init]
    unknown = sorted(set(init) - set(states))
    if missing or unknown:
        raise ValueError(f"{where}: set exactly the state variables "
                         f"{list(states)}; missing {missing}, unknown "
                         f"{unknown}")
    for var, value in init.items():
        _check_value(f"{where} '{var}'", value, states[var])
