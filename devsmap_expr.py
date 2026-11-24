"""Parse DEVSMap expressions (devsmap/SKILL.md) into syntax trees.

    parse(text) -> node

A node is a tuple whose first item names its kind:

    ("num", text)                a number literal, as written
    ("str", text)                a string literal, unescaped
    ("name", name)               a variable, parameter, port or constant
    ("member", node, field)      node.field
    ("method", node, name, [node])   node.name(args): bagSize(), bag(i)
    ("call", name, [node])       a function or struct constructor
    ("array", [node])            [a, b, c]
    ("neg", node)                -node
    ("bin", op, node, node)      a binary operator
    ("concat", [node])           "text" + x + ...: the operands of a + chain
                                 holding a string literal, joined as text

Precedence is C++'s. A + chain holding a string literal is joined as text,
operand by operand, as the generator does. Stdlib only.
"""

import functools
import re

_TOKEN = re.compile(r'\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?)|("(?:[^"\\]|\\.)*")'
                    r'|([A-Za-z_]\w*)|(&&|\|\||[=!<>]=|[-+*/%<>()\[\],.]))')
_ESCAPES = {"n": "\n", "t": "\t"}
_LEVELS = [("||",), ("&&",), ("==", "!="), ("<", "<=", ">", ">="),
           ("+", "-"), ("*", "/", "%")]


@functools.lru_cache(maxsize=None)
def parse(text):
    """The syntax tree of one expression; ValueError if it is not one."""
    parser = _Parser(text)
    node = parser.binary(0)
    if parser.pos < len(parser.tokens):
        parser.fail(f"unexpected {parser.tokens[parser.pos][1]!r}")
    return node


def _unescape(literal):
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m[1], m[1]), literal[1:-1])


class _Parser:
    def __init__(self, text):
        self.text, self.tokens, pos = text, [], 0
        while text[pos:].strip():
            match = _TOKEN.match(text, pos)
            if not match:
                self.fail(f"unexpected character {text[pos:].strip()[0]!r}")
            num, string, name, op = match.groups()
            self.tokens.append(("num", num) if num else
                               ("str", _unescape(string)) if string else
                               ("name", name) if name else ("op", op))
            pos = match.end()
        self.pos = 0

    def fail(self, why):
        raise ValueError(f"cannot parse {self.text!r}: {why}")

    def at(self, *ops):
        """Whether the next token is one of the operators `ops`."""
        return (self.pos < len(self.tokens)
                and self.tokens[self.pos] in [("op", op) for op in ops])

    def take(self, *ops):
        """The next token; it must be one of the operators `ops`, if given."""
        if self.pos == len(self.tokens) or (ops and not self.at(*ops)):
            found = (repr(self.tokens[self.pos][1])
                     if self.pos < len(self.tokens) else "the end")
            wanted = " or ".join(repr(op) for op in ops) or "more"
            self.fail(f"expected {wanted}, got {found}")
        self.pos += 1
        return self.tokens[self.pos - 1]

    def binary(self, level):
        if level == len(_LEVELS):
            return self.unary()
        operands, ops = [self.binary(level + 1)], []
        while self.at(*_LEVELS[level]):
            ops.append(self.take()[1])
            operands.append(self.binary(level + 1))
        if ops and ops[0] in ("+", "-") and self._has_text(operands, ops):
            return self._concat(operands, ops)
        node = operands[0]
        for op, operand in zip(ops, operands[1:]):
            node = ("bin", op, node, operand)
        return node

    @staticmethod
    def _has_text(operands, ops):
        return any(node[0] == "str"
                   and "+" in (ops[i - 1] if i else None,
                               ops[i] if i < len(ops) else None)
                   for i, node in enumerate(operands))

    @staticmethod
    def _concat(operands, ops):
        parts = [operands[0]]
        for op, operand in zip(ops, operands[1:]):
            if op == "+":
                parts.append(operand)
            else:
                parts[-1] = ("bin", op, parts[-1], operand)
        return ("concat", parts)

    def unary(self):
        if self.at("-"):
            self.take()
            return ("neg", self.unary())
        return self.postfix(self.primary())

    def primary(self):
        kind, value = self.take()
        if kind in ("num", "str"):
            return (kind, value)
        if kind == "name":
            if self.at("("):
                return ("call", value, self.arguments(")"))
            return ("name", value)
        if value == "(":
            node = self.binary(0)
            self.take(")")
            return node
        if value == "[":
            return ("array", self.arguments("]", opened=True))
        self.fail(f"unexpected {value!r}")

    def arguments(self, close, opened=False):
        if not opened:
            self.take("(")
        args = []
        while not self.at(close):
            if args:
                self.take(",", close)
            args.append(self.binary(0))
        self.take(close)
        return args

    def postfix(self, node):
        while self.at("."):
            self.take()
            kind, name = self.take()
            if kind != "name":
                self.fail(f"expected a name after '.', got {name!r}")
            node = (("method", node, name, self.arguments(")"))
                    if self.at("(") else ("member", node, name))
        return node
