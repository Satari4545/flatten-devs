"""Encode DEVSMap atomic models in Z3 (pip install z3-solver).

    Encoding(definition_paths)     Z3 sorts, values and expressions
    Atomic(encoding, name, body)   an atomic model's functions on symbolic
                                   states
    show(value)                    a concrete value (from a model) as text
    FLOAT, fin, f_arith, f_eq, f_lt, f_le, f_min
                                   floating_point values and operations
    all_of, any_of, not_, ite      Boolean and if-then-else builders
    within(where, run, *args)      run, naming `where` in its ValueError

Domains: integer -> Int, unsigned_integer -> 32-bit vector, boolean -> Bool,
floating_point -> FLOAT (the reals plus inf, -inf and NaN), string ->
String, a struct set of the definitions files -> a datatype (constructor
`set(...)`, accessor `.field`), an array field -> a sequence. An input bag
is a sequence: bagSize() is its length, bag(i) a Python-style index.

Meaning follows the generator and Cadmium: a condition map is an if/else-if
chain in key order (no true key: nothing happens), a branch's assignments
all read the state before the transition, a default delta_con is delta_int
then delta_ext with e = 0, integer / and % truncate toward zero, and
"text" + x joins each operand's text. Unlike C++, arithmetic is exact (real
and signed integer: no rounding or overflow). NaN keeps its sign (nan,
-nan): every comparison with it is false except !=, arithmetic passes a NaN
operand on, and an invalid operation (inf - inf, 0 * inf, 0 / 0) gives
-nan, as on x86. A function the models call is uninterpreted and returns a
floating_point; so is the text of a number.
"""

import fractions
import json
import operator

import z3

import devsmap
from devsmap_expr import parse

_float = z3.Datatype("floating_point")
_float.declare("fin", ("val", z3.RealSort()))  # fin(0) is +0.0
_float.declare("nzero")                         # -0.0
_float.declare("pinf")
_float.declare("ninf")
_float.declare("nan", ("negative", z3.BoolSort()))
FLOAT = _float.create()
INF, NINF, NZERO = FLOAT.pinf, FLOAT.ninf, FLOAT.nzero
INVALID = FLOAT.nan(z3.BoolVal(True))  # x86 gives -nan for inf - inf
_KINDS = {kind: FLOAT.constructor(i)
          for i, kind in enumerate(("fin", "nzero", "pinf", "ninf", "nan"))}

INT, BOOL, STRING = z3.IntSort(), z3.BoolSort(), z3.StringSort()
TRUE, FALSE = z3.BoolVal(True), z3.BoolVal(False)
_ZERO = z3.RealVal(0)
_BASIC = {
    "integer": INT, "int": INT, "long": INT, "char": INT,
    "unsigned_integer": z3.BitVecSort(32), "unsigned int": z3.BitVecSort(32),
    "unsigned": z3.BitVecSort(32), "size_t": z3.BitVecSort(64),
    "floating_point": FLOAT, "double": FLOAT, "float": FLOAT,
    "boolean": BOOL, "bool": BOOL,
    "string": STRING, "std::string": STRING,
}
_CONSTANTS = {"true": TRUE, "false": FALSE, "inf": INF,
              "nan": FLOAT.nan(FALSE)}


def fin(x):
    """A finite floating_point holding the Real (or number) x; 0 is +0.0."""
    return FLOAT.fin(x)


# Boolean and if-then-else builders that fold literal true and false, so
# operations on literal values stay small.
def all_of(*cs):
    cs = [c for c in cs if not z3.is_true(c)]
    if any(z3.is_false(c) for c in cs):
        return FALSE
    return z3.And(cs) if len(cs) > 1 else cs[0] if cs else TRUE


def any_of(*cs):
    cs = [c for c in cs if not z3.is_false(c)]
    if any(z3.is_true(c) for c in cs):
        return TRUE
    return z3.Or(cs) if len(cs) > 1 else cs[0] if cs else FALSE


def not_(c):
    return FALSE if z3.is_true(c) else TRUE if z3.is_false(c) else z3.Not(c)


def _xor(p, q):
    if z3.is_true(p) or z3.is_false(p):
        return not_(q) if z3.is_true(p) else q
    if z3.is_true(q) or z3.is_false(q):
        return not_(p) if z3.is_true(q) else p
    return z3.Xor(p, q)


def ite(c, a, b):
    """If(c, a, b), folded when c is a literal or a and b are the same."""
    if z3.is_true(c) or a.eq(b):
        return a
    return b if z3.is_false(c) else z3.If(c, a, b)


def _kind(a):
    """Which FLOAT constructor builds `a`, if `a` is a constructor term."""
    if z3.is_app(a):
        return next((k for k, decl in _KINDS.items() if a.decl().eq(decl)),
                    None)
    return None


def _is(kind, a):
    known = _kind(a)
    if known is not None:
        return z3.BoolVal(known == kind)
    return getattr(FLOAT, f"is_{kind}")(a)


def _finite(a):
    return any_of(_is("fin", a), _is("nzero", a))


def _real(a):
    """The Real value of a finite a."""
    kind = _kind(a)
    if kind == "fin":
        return a.arg(0)
    return _ZERO if kind == "nzero" else ite(_is("nzero", a), _ZERO,
                                             FLOAT.val(a))


def _rational(x):
    return x.as_fraction() if z3.is_rational_value(x) else None


_REAL_OPS = {"+": operator.add, "-": operator.sub, "*": operator.mul,
             "/": operator.truediv, "<": operator.lt, "==": operator.eq,
             ">": operator.gt}


def _real_op(op, x, y):
    """x op y on Reals, folded when both are numerals."""
    px, py = _rational(x), _rational(y)
    if px is None or py is None or (op == "/" and py == 0):
        return _REAL_OPS[op](x, y)
    value = _REAL_OPS[op](px, py)
    return z3.BoolVal(value) if isinstance(value, bool) else \
        z3.RealVal(str(value))


def _signbit(a):
    """For a non-NaN a: whether it is negative (-0.0 and -inf too)."""
    return any_of(_is("nzero", a), _is("ninf", a),
                  all_of(_is("fin", a), _real_op("<", _real(a), _ZERO)))


def _zero(a):
    return any_of(_is("nzero", a),
                  all_of(_is("fin", a), _real_op("==", _real(a), _ZERO)))


def _make(x, negative):
    """The finite floating_point x; when x is 0, -0.0 if `negative`."""
    return ite(all_of(_real_op("==", x, _ZERO), negative), NZERO, fin(x))


def f_neg(a):
    return ite(_finite(a), _make(_real_op("-", _ZERO, _real(a)),
                                 not_(_signbit(a))),
               ite(_is("pinf", a), NINF,
                   ite(_is("ninf", a), INF,
                       FLOAT.nan(z3.Not(FLOAT.negative(a))))))


def _exact(op, a, b):
    """a op b for finite a and b, with IEEE's signed zeros."""
    x, y = _real(a), _real(b)
    sign = _xor(_signbit(a), _signbit(b))
    if op == "/":
        return ite(_real_op("==", y, _ZERO),
                   ite(_real_op("==", x, _ZERO), INVALID,
                       ite(sign, NINF, INF)),
                   _make(_real_op("/", x, y), sign))
    negative = {"*": sign, "+": all_of(_signbit(a), _signbit(b)),
                "-": all_of(_signbit(a), not_(_signbit(b)))}[op]
    return _make(_real_op(op, x, y), negative)


def _infinite(op, a, b):
    """a op b when neither is NaN and one is infinite."""
    if op == "+":
        return ite(_finite(a), b,
                   ite(_finite(b), a, ite(a == b, a, INVALID)))
    if op == "-":
        return ite(_finite(a), f_neg(b),
                   ite(_finite(b), a, ite(a == b, INVALID, a)))
    sign = _xor(_signbit(a), _signbit(b))
    if op == "*":
        return ite(any_of(_zero(a), _zero(b)), INVALID,
                   ite(sign, NINF, INF))
    return ite(_finite(a), ite(sign, NZERO, fin(0)),
               ite(_finite(b), ite(sign, NINF, INF), INVALID))


def f_arith(op, a, b):
    """a op b for op in + - * / on FLOAT values."""
    both = all_of(_finite(a), _finite(b))
    if z3.is_true(both):
        return _exact(op, a, b)
    special = ite(_is("nan", a), a,
                  ite(_is("nan", b), b, _infinite(op, a, b)))
    return ite(both, _exact(op, a, b), special)


def f_lt(a, b):
    both = all_of(_finite(a), _finite(b))
    if z3.is_true(both):
        return _real_op("<", _real(a), _real(b))
    return all_of(not_(_is("nan", a)), not_(_is("nan", b)),
                  ite(both, _real_op("<", _real(a), _real(b)),
                      any_of(all_of(_is("ninf", a), not_(_is("ninf", b))),
                             all_of(not_(_is("pinf", a)), _is("pinf", b)))))


def f_eq(a, b):
    """IEEE a == b: false if either is NaN, -0.0 == 0.0."""
    both = all_of(_finite(a), _finite(b))
    if z3.is_true(both):
        return _real_op("==", _real(a), _real(b))
    return all_of(not_(_is("nan", a)), not_(_is("nan", b)),
                  ite(both, _real_op("==", _real(a), _real(b)), a == b))


def f_le(a, b):
    return any_of(f_lt(a, b), f_eq(a, b))


def f_min(a, b):
    return ite(f_le(a, b), a, b)


def _float_op(op, a, b):
    if op in ("<", ">"):
        return f_lt(a, b) if op == "<" else f_lt(b, a)
    if op in ("<=", ">="):
        return f_le(a, b) if op == "<=" else f_le(b, a)
    if op in ("==", "!="):
        return f_eq(a, b) if op == "==" else not_(f_eq(a, b))
    if op == "%":
        raise ValueError("% of floating_point values")
    return f_arith(op, a, b)


def _int_div(a, b):
    """C++ a / b on integers: truncates toward zero."""
    q = z3.If(a >= 0, a, -a) / z3.If(b >= 0, b, -b)
    return z3.If((a >= 0) == (b >= 0), q, -q)


_INT_OPS = {"+": operator.add, "-": operator.sub, "*": operator.mul,
            "/": _int_div, "%": lambda a, b: a - b * _int_div(a, b),
            "<": operator.lt, ">": operator.gt, "<=": operator.le,
            ">=": operator.ge, "==": operator.eq, "!=": operator.ne}
# Unsigned integers wrap around, as in C++.
_UNSIGNED_OPS = {**_INT_OPS, "/": z3.UDiv, "%": z3.URem, "<": z3.ULT,
                 ">": z3.UGT, "<=": z3.ULE, ">=": z3.UGE}


def _unsigned(sort):
    return isinstance(sort, z3.BitVecSortRef)


def _numeric(sort):
    return sort in (INT, FLOAT) or _unsigned(sort)


def _number(text):
    if any(c in text for c in ".eE"):
        return fin(z3.RealVal(str(fractions.Fraction(text))))
    return z3.IntVal(int(text))


class Bag:
    """An input port's bag: a Z3 sequence of its messages, in order."""

    def __init__(self, seq):
        self.seq = seq


def _plain(v):
    """v, if it is a value (not a Bag or a dict of names)."""
    if isinstance(v, Bag):
        raise ValueError("an input port is read only through bagSize() and "
                         "bag(i)")
    if isinstance(v, dict):
        raise ValueError(f"{', '.join(v)}: not a value")
    return v


def sort_name(sort):
    if sort == FLOAT:
        return "floating_point"
    if _unsigned(sort):
        return f"{sort.size()}-bit unsigned integer"
    if isinstance(sort, z3.SeqSortRef) and not sort == STRING:
        return f"array of {sort_name(sort.basis())}"
    return {INT: "integer", BOOL: "boolean", STRING: "string"}.get(
        sort, sort.name())


class Encoding:
    """Z3 sorts for DEVSMap domains, and the values of expressions.

    `params` maps parameter names to their values; a scope (a dict) maps
    the other names an expression may use to a Z3 value, a Bag, or a dict
    of further names (`a.b` looks b up in a).
    """

    def __init__(self, definition_paths):
        self.params = {}
        self._sets = {}
        for path in definition_paths:
            for name, fields in devsmap.read_json(path).items():
                if name in _BASIC:
                    continue
                if self._sets.get(name, fields) != fields:
                    raise ValueError(f"set {name} has different definitions "
                                     f"in the definitions files")
                self._sets[name] = fields
        self._structs = {}
        self._functions = {}

    def sort(self, domain):
        """The Z3 sort of a domain (a set name)."""
        if domain in _BASIC:
            return _BASIC[domain]
        if domain not in self._structs:
            if domain not in self._sets:
                raise ValueError(f"unknown domain {domain!r}: not a basic "
                                 f"set and in no definitions file")
            self._structs[domain] = None  # being made: catches recursion
            fields = [(f"{domain}.{field}", self._field_sort(spec))
                      for field, spec in self._sets[domain].items()]
            datatype = z3.Datatype(domain)
            datatype.declare(domain, *fields)
            self._structs[domain] = datatype.create()
        elif self._structs[domain] is None:
            raise ValueError(f"set {domain!r} contains itself")
        return self._structs[domain]

    def _field_sort(self, spec):
        base = self.sort(spec["domain"])
        return base if spec.get("dimension", ["1"]) == ["1"] else \
            z3.SeqSort(base)

    def fields(self, sort):
        """The field names of a struct sort, else None."""
        name = sort.name()
        return list(self._sets[name]) if name in self._structs else None

    def value(self, expr, sort, scope):
        """The value of an expression (text or syntax tree) as `sort`."""
        node = parse(expr) if isinstance(expr, str) else expr
        if node[0] == "array" and isinstance(sort, z3.SeqSortRef):
            return self._array(node[1], sort.basis(), scope)
        return self.convert(self.eval(node, scope), sort)

    def condition(self, expr, scope):
        return self.value(expr, BOOL, scope)

    def convert(self, v, sort):
        """C++'s implicit conversion of a value to `sort`."""
        have = _plain(v).sort()
        if have == sort:
            return v
        if have == BOOL and _numeric(sort):
            return self.convert(z3.If(v, z3.IntVal(1), z3.IntVal(0)), sort)
        if sort == BOOL and _numeric(have):
            return not_(f_eq(v, fin(0))) if have == FLOAT else v != 0
        if sort == FLOAT and have != FLOAT and _numeric(have):
            return fin(z3.ToReal(v if have == INT else z3.BV2Int(v)))
        if sort == INT and have == FLOAT:
            x = _real(v)
            truncated = z3.If(x >= 0, z3.ToInt(x), -z3.ToInt(-x))
            undefined = self.function("float_to_int", [FLOAT], INT)(v)
            return ite(_finite(v), truncated, undefined)
        if sort == INT and _unsigned(have):
            low = v if have.size() <= 32 else z3.Extract(31, 0, v)
            return z3.BV2Int(low, is_signed=True)
        if _unsigned(sort) and _unsigned(have):
            n, m = sort.size(), have.size()
            return z3.ZeroExt(n - m, v) if n > m else z3.Extract(n - 1, 0, v)
        if _unsigned(sort) and _numeric(have):
            x = self.convert(v, INT)
            if z3.is_int_value(x):
                return z3.BitVecVal(x.as_long() % 2 ** sort.size(),
                                    sort.size())
            return z3.Int2BV(x, sort.size())
        raise ValueError(f"a {sort_name(have)} where a {sort_name(sort)} is "
                         f"expected")

    def function(self, name, sorts, result):
        """The uninterpreted function `name` from `sorts` to `result`."""
        key = (name, *sorts, result)
        if key not in self._functions:
            self._functions[key] = z3.Function(name, *sorts, result)
        return self._functions[key]

    def eval(self, node, scope):
        """The Z3 value of a syntax tree (devsmap_expr) in a scope."""
        kind = node[0]
        if kind == "num":
            return _number(node[1])
        if kind == "str":
            return z3.StringVal(node[1])
        if kind == "name":
            return self._name(node[1], scope)
        if kind == "member":
            return self._member(self.eval(node[1], scope), node[2])
        if kind == "method":
            return self._bag_op(self.eval(node[1], scope), node[2], node[3],
                                scope)
        if kind == "call":
            return self._call(node[1], node[2], scope)
        if kind == "array":
            return self._array(node[1], None, scope)
        if kind == "neg":
            return self._negate(self.eval(node[1], scope))
        if kind == "concat":
            return z3.Concat([self._text(part, scope) for part in node[1]])
        return self._binary(node[1], self.eval(node[2], scope),
                            self.eval(node[3], scope))

    def _name(self, name, scope):
        if name in scope:
            return scope[name]
        if name in _CONSTANTS:
            return _CONSTANTS[name]
        raise ValueError(f"unknown name {name!r}")

    def _member(self, v, field):
        if isinstance(v, dict):
            if field not in v:
                raise ValueError(f"unknown name {field!r} after '.'")
            return v[field]
        fields = None if isinstance(v, Bag) else self.fields(v.sort())
        if fields is None or field not in fields:
            raise ValueError(f".{field} of a value without that field")
        return self._structs[v.sort().name()].accessor(
            0, fields.index(field))(v)

    def _bag_op(self, bag, method, args, scope):
        if not isinstance(bag, Bag):
            raise ValueError(f".{method}() of something not an input port")
        size = z3.Length(bag.seq)
        if method == "bagSize" and not args:
            return size
        if method == "bag" and len(args) == 1:
            i = self.convert(self.eval(args[0], scope), INT)
            if z3.is_int_value(i):
                return bag.seq[i if i.as_long() >= 0 else size + i]
            return bag.seq[z3.If(i >= 0, i, size + i)]
        raise ValueError(f"unknown bag operation .{method}() with "
                         f"{len(args)} arguments")

    def _call(self, name, args, scope):
        if name in self._sets:
            make = self.sort(name).constructor(0)
            if len(args) != make.arity():
                raise ValueError(f"{name}(...) takes {make.arity()} values, "
                                 f"got {len(args)}")
            return make(*[self.value(a, make.domain(i), scope)
                          for i, a in enumerate(args)])
        values = [_plain(self.eval(a, scope)) for a in args]
        return self.function(name, [v.sort() for v in values],
                             FLOAT)(*values)

    def _array(self, items, element, scope):
        if element is None:
            if not items:
                raise ValueError("an empty array literal where its type "
                                 "is unknown")
            sorts = [self.eval(item, scope).sort() for item in items]
            element = FLOAT if FLOAT in sorts else sorts[0]
        units = [z3.Unit(self.value(item, element, scope)) for item in items]
        if not units:
            return z3.Empty(z3.SeqSort(element))
        return z3.Concat(units) if len(units) > 1 else units[0]

    def _negate(self, v):
        sort = _plain(v).sort()
        if sort == FLOAT:
            return f_neg(v)
        return -v if _unsigned(sort) else -self.convert(v, INT)

    def _text(self, part, scope):
        """std::to_string of one operand of a "text" + x chain."""
        v = _plain(self.eval(part, scope))
        sort = v.sort()
        if sort == STRING:
            return v
        if sort == BOOL:
            return z3.If(v, z3.StringVal("1"), z3.StringVal("0"))
        if _numeric(sort):
            return self.function("to_string", [sort], STRING)(v)
        raise ValueError(f"the text of a {sort_name(sort)}")

    def _binary(self, op, a, b):
        a, b = _plain(a), _plain(b)
        if op in ("&&", "||"):
            a, b = self.convert(a, BOOL), self.convert(b, BOOL)
            return all_of(a, b) if op == "&&" else any_of(a, b)
        sorts = (a.sort(), b.sort())
        if not all(_numeric(s) or s == BOOL for s in sorts) \
                or sorts == (BOOL, BOOL):
            if sorts == (STRING, STRING) and op == "+":
                return z3.Concat(a, b)
            if sorts[0] == sorts[1] and op in ("==", "!="):
                return a == b if op == "==" else a != b
            raise ValueError(f"{sort_name(a.sort())} {op} "
                             f"{sort_name(b.sort())}")
        # C++'s usual arithmetic conversions: double, else unsigned, else int
        words = [s for s in sorts if _unsigned(s)]
        sort = FLOAT if FLOAT in sorts else max(
            words, key=lambda s: s.size()) if words else INT
        a, b = self.convert(a, sort), self.convert(b, sort)
        if sort == FLOAT:
            return _float_op(op, a, b)
        return (_UNSIGNED_OPS if words else _INT_OPS)[op](a, b)

    def branches(self, cmap, scope):
        """[(label, guard, body)]: each leaf of a (possibly nested)
        condition map, its guard true exactly when the generator's
        if/else-if chain takes it."""
        found = []

        def walk(m, guard, labels):
            rest = guard
            for key, body in m.items():
                cond = TRUE if key == "otherwise" else \
                    self.condition(key, scope)
                taken, rest = all_of(rest, cond), all_of(rest, not_(cond))
                if isinstance(body, dict) and any(isinstance(v, dict)
                                                  for v in body.values()):
                    walk(body, taken, labels + [key])
                else:
                    found.append((" / ".join(labels + [key]), taken, body))
            if not z3.is_false(rest):
                found.append((" / ".join(labels + ["(no key holds)"]), rest,
                              {}))

        walk(cmap, TRUE, [])
        return found

    def choose(self, cmap, sort, scope):
        """The value of an expression, or of a condition map whose branches
        are expressions (it must end with "otherwise")."""
        if isinstance(cmap, str):
            return self.value(cmap, sort, scope)
        if list(cmap)[-1:] != ["otherwise"]:
            raise ValueError("a condition map of values needs \"otherwise\" "
                             "last")
        result = None
        for _, guard, body in reversed(self.branches(cmap, scope)):
            v = self.value(body, sort, scope)
            result = v if result is None else ite(guard, v, result)
        return result


def within(where, run, *args):
    """run(*args), naming `where` in a ValueError."""
    try:
        return run(*args)
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from None


class Atomic:
    """An atomic DEVSMap model's functions on symbolic states.

    A state is a dict {state variable: Z3 value}; bags are a dict {input
    port: Z3 sequence} (a missing port is empty); e is a FLOAT.
    """

    def __init__(self, encoding, name, body):
        self.enc, self.name, self.body = encoding, name, body
        self.s, self.x, self.y = (
            {k: within(f"{name}.{key}.{k}", encoding.sort, d)
             for k, d in body.get(key, {}).items()} for key in ("s", "x", "y"))
        con = body.get("delta_con")
        self.own_confluent = bool(con) and con != {"otherwise": {}}

    def scope(self, state, e=None, bags=None):
        names = {**self.enc.params, **state}
        if bags is not None:
            names.update({p: Bag(bags.get(p, z3.Empty(z3.SeqSort(sort))))
                          for p, sort in self.x.items()})
        if e is not None:
            names["e"] = e
        return names

    def initial(self, values):
        """The state set by an init-state entry {variable: expression}."""
        if set(values) != set(self.s):
            raise ValueError(f"{self.name}: the initial state sets "
                             f"{sorted(values)}, the state variables are "
                             f"{sorted(self.s)}")
        return {v: within(f"{self.name} initial {v}", self.enc.value,
                           values[v], sort, self.enc.params)
                for v, sort in self.s.items()}

    def ta(self, state):
        return within(f"{self.name}.ta", self.enc.choose, self.body["ta"],
                       FLOAT, self.scope(state))

    def output(self, state):
        """{output port: [(guard, message)]}: the messages lambda sends, in
        order, each when its guard holds."""
        return within(f"{self.name}.lambda", self._output, state)

    def _output(self, state):
        scope, sent = self.scope(state), {p: [] for p in self.y}
        for label, guard, body in self.enc.branches(self.body["lambda"],
                                                    scope):
            for port, messages in body.items():
                if port not in self.y:
                    raise ValueError(f"{label!r}: {port} is not an output "
                                     f"port")
                for m in messages if isinstance(messages, list) else [messages]:
                    sent[port].append((guard, within(
                        f"{label!r} {port}", self.enc.value, m, self.y[port],
                        scope)))
        return sent

    def transitions(self, kind, state, e=None, bags=None):
        """[(label, guard, next state)] for each branch of delta_<kind>
        (kind: int, ext or con); a default delta_con is delta_int then
        delta_ext with e = 0."""
        if kind == "con" and not self.own_confluent:
            return [(f"{label} (delta_int, then delta_ext)", guard,
                     self.delta("ext", after, fin(0), bags))
                    for label, guard, after in self.transitions("int", state)]
        where = f"{self.name}.delta_{kind}"
        scope = self.scope(state, *((e, bags) if kind != "int" else ()))
        found = []
        for label, guard, body in within(where, self.enc.branches,
                                          self.body[f"delta_{kind}"], scope):
            unknown = set(body) - set(self.s)
            if unknown:
                raise ValueError(f"{where} {label!r}: assigns "
                                 f"{', '.join(sorted(unknown))}, not state "
                                 f"variables")
            found.append((label, guard, {**state, **{
                v: within(f"{where} {label!r} {v}", self.enc.value, x,
                           self.s[v], scope) for v, x in body.items()}}))
        return found

    def delta(self, kind, state, e=None, bags=None):
        """The next state after delta_<kind>."""
        branches = self.transitions(kind, state, e, bags)
        result = {}
        for v, old in state.items():
            for _, guard, after in reversed(branches):
                old = ite(guard, after[v], old)
            result[v] = old
        return result


def show(v):
    """A concrete Z3 value (from a model) as DEVSMap-like text."""
    sort = v.sort()
    if sort == FLOAT:
        kind = _kind(v)
        if kind == "fin":
            return _show_real(v.arg(0))
        if kind == "nan":
            return "-nan" if z3.is_true(v.arg(0)) else "nan"
        return {"nzero": "-0.0", "pinf": "inf", "ninf": "-inf"}.get(
            kind, str(v))
    if sort == STRING and z3.is_string_value(v):
        return json.dumps(v.as_string())
    if z3.is_int_value(v) or z3.is_true(v) or z3.is_false(v):
        return str(v).lower()
    if z3.is_bv_value(v):
        return str(v.as_long())
    if isinstance(sort, z3.SeqSortRef):
        return f"[{', '.join(show(x) for x in _elements(v))}]"
    if isinstance(sort, z3.DatatypeSortRef) and z3.is_app(v):
        return f"{v.decl().name()}({', '.join(show(a) for a in v.children())})"
    return str(v)


def _elements(seq):
    if z3.is_app(seq) and seq.decl().name() == "seq.unit":
        return [seq.arg(0)]
    if z3.is_app(seq) and seq.decl().name() == "seq.++":
        return [x for part in seq.children() for x in _elements(part)]
    return []


def _show_real(x):
    if not z3.is_rational_value(x):
        return x.as_decimal(10) if z3.is_algebraic_value(x) else str(x)
    q = x.as_fraction()
    text = f"{float(q):.15g}"
    if fractions.Fraction(text) == q:
        return text if any(c in text for c in ".en") else text + ".0"
    return f"{q.numerator}/{q.denominator}"
