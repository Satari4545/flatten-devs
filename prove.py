"""Prove a fused atomic DEVSMap model equivalent to its coupled model.

    prove(coupled_path, fused_path, prune=False) -> Report
    assumptions(encoding)   what a proof made with that encoding assumes

Z3 checks that the map in <fused name>_mapping.json, with its invariant,
is a homomorphism from the coupled model's resultant (as Cadmium runs it)
to the fused atomic. context.md, "Proving a fusion", describes the mapping
file, the obligations and what a proof assumes.
"""

import os
import time
from typing import NamedTuple

import z3

import devsmap
import devsmap_flatten
from devsmap_z3 import (FALSE, STRING, Atomic, Encoding, all_of, any_of, at,
                        before, by, concat, duration, earliest, ite, show,
                        time_left, within)

QUERY_TIMEOUT = 600  # seconds per Z3 query
EVENTS = {"int": "internal", "ext": "external", "con": "confluent"}
WELL_FORMED = "0 <= e <= ta in every component"
ASSUMPTIONS = (
    "arithmetic is exact: real numbers, not IEEE doubles; signed integers "
    "without overflow (unsigned ones wrap around as in C++); this also "
    "decides which events happen at the same time",
    "functions the models call, and the text of numbers, are uninterpreted "
    "and pure: the same arguments give the same value at every call and in "
    "both models; nothing else is known about them",
    "what C++ leaves undefined (a bag read out of range, an integer "
    "division by zero, inf or NaN converted to an integer) gives some "
    "value, the same for the same operands",
    "parameters have the values in the coupled model's _param.json (any "
    "value where it gives none), the constants the C++ is compiled with",
    "the DEVSMap generator and Cadmium run the models as encoded: condition "
    "maps as if/else-if chains, simultaneous assignments, the default "
    "delta_con, Cadmium's bag order",
)


class Report(NamedTuple):
    proven: bool
    failed: list    # names of the obligations not proven
    vacuous: list   # names of those proven because no state reaches them
    dropped: list   # invariant conditions prune dropped
    seconds: float


class State(NamedTuple):
    """A state of R: {component: {variable: value}}, {component: e}."""
    vars: dict
    e: dict


class Event(NamedTuple):
    """An event of R from a symbolic state, and what it leads to."""
    kind: str
    holds: object   # when the event can happen
    after: State
    imminent: dict  # component -> Bool
    sent: dict      # component -> {out port: [(guard, message)]}
    inputs: dict    # coupled input port -> sequence


def _seq(slots, sort):
    """The sequence of the messages whose guards hold, in order."""
    empty = z3.Empty(z3.SeqSort(sort))
    return concat([ite(g, z3.Unit(m), empty) for g, m in slots], sort)


class Resultant:
    """The coupled model's resultant R on symbolic states, as Cadmium runs
    it (devsmap_flatten gives its leaves and couplings)."""

    def __init__(self, enc, model):
        flat = devsmap_flatten.flatten(model)
        bodies = devsmap_flatten.atomic_models(model)
        self.enc = enc
        self.parts = {cid: Atomic(enc, c["model"], bodies[c["model"]])
                      for cid, c in flat["components"].items()}
        self.ids = {cid: ids for cid, (ids, _)
                    in devsmap.leaves(model).items()}
        self.initials = {cid: c["initial"]
                         for cid, c in flat["components"].items()}
        self.bags, self.eoc = flat["bags"], flat["outputs"]
        self.x, self.y = ({p: enc.sort(d)
                           for p, d in model["body"].get(key, {}).items()}
                          for key in ("x", "y"))

    def symbols(self):
        return State({cid: {v: z3.Const(f"{cid}.{v}", sort)
                            for v, sort in a.s.items()}
                      for cid, a in self.parts.items()},
                     {cid: z3.Real(f"{cid}.e") for cid in self.parts})

    def initial(self):
        return State({cid: a.initial(self.initials[cid])
                      for cid, a in self.parts.items()},
                     {cid: z3.RealVal(0) for cid in self.parts})

    def remaining(self, s):
        """{component: ta - e}, the time until its next internal event."""
        return {cid: time_left(a.ta(s.vars[cid]), s.e[cid])
                for cid, a in self.parts.items()}

    def ta(self, s):
        return earliest(self.remaining(s).values())

    def well_formed(self, s):
        """0 <= e <= ta in every component (ta not NaN)."""
        return all_of(*[all_of(s.e[cid] >= 0,
                               by(s.e[cid], a.ta(s.vars[cid])))
                        for cid, a in self.parts.items()])

    def event(self, s, kind, t, inputs):
        """The event of `kind` after elapsed time t, with these input bags.

        internal: t = ta_R < inf, no input; external: 0 <= t < ta_R, some
        input; confluent: t = ta_R < inf, some input. Each component with
        ta - e = t is imminent: it outputs, the outputs travel through the
        couplings, and it takes delta_int, or delta_con if it also receives;
        a receiving component that is not imminent takes delta_ext with its
        own e + t; the others' e grows by t.
        """
        remaining = self.remaining(s)
        imminent = {cid: FALSE if kind == "ext" else at(t, r)
                    for cid, r in remaining.items()}
        timing = ([before(t, r) for r in remaining.values()] if kind == "ext"
                  else [by(t, r) for r in remaining.values()]
                  + [any_of(*imminent.values())])
        arriving = [any_of(*[z3.Length(x) > 0 for x in inputs.values()])
                    ] if kind != "int" else []
        sent = {cid: {p: [(all_of(imminent[cid], g), m) for g, m in slots]
                      for p, slots in a.output(s.vars[cid]).items()}
                for cid, a in self.parts.items()
                if not z3.is_false(imminent[cid])}
        after = State({}, {})
        for cid, a in self.parts.items():
            bags, arrived = {}, []
            for p, sort in a.x.items():
                parts = []
                for src, port in self.bags[(cid, p)]:
                    if src is None:
                        parts.append(inputs[port])
                        if kind != "int":
                            arrived.append(z3.Length(inputs[port]) > 0)
                    else:
                        slots = [(g, self.enc.convert(m, sort))
                                 for g, m in sent.get(src, {}).get(port, [])]
                        parts.append(_seq(slots, sort))
                        arrived += [g for g, _ in slots]
                bags[p] = concat(parts, sort)
            after.vars[cid], after.e[cid] = self._transition(
                a, s.vars[cid], s.e[cid] + t, imminent[cid], any_of(*arrived),
                bags)
        holds = all_of(t >= 0, *timing, *arriving)
        return Event(kind, holds, after, imminent, sent, inputs)

    @staticmethod
    def _transition(a, old, e, imminent, receiving, bags):
        new = dict(old)
        if not z3.is_false(receiving):
            ext = a.delta("ext", old, duration(e), bags)
            new = {v: ite(receiving, ext[v], new[v]) for v in old}
        if not z3.is_false(imminent):
            internal = a.delta("int", old)
            new = {v: ite(imminent, internal[v], new[v]) for v in old}
            if not z3.is_false(receiving):
                con = a.delta("con", old, duration(e), bags)
                both = all_of(imminent, receiving)
                new = {v: ite(both, con[v], new[v]) for v in old}
        return new, ite(any_of(imminent, receiving), z3.RealVal(0), e)

    def output(self, sent):
        """{port: [(guard, message)]}: what R outputs, in Cadmium's order.
        Without ports, every component's messages, as "<id>:<value>"."""
        if self.x or self.y:
            out = {p: [] for p in self.y}
            for src, port, p in self.eoc:
                out[p] += [(g, self.enc.convert(m, self.y[p]))
                           for g, m in sent.get(src, {}).get(port, [])]
            return out
        out = {}
        for cid, ports in sent.items():
            tag = z3.StringVal(self.ids[cid][-1] + ":")
            for p, slots in ports.items():
                out.setdefault(p, []).extend(
                    (g, z3.Concat(tag, self._logged(m))) for g, m in slots)
        return out

    def _logged(self, m):
        if m.sort() == STRING:
            return m
        return self.enc.function("logged_text", [m.sort()], STRING)(m)


class Mapping:
    """h and the invariant of a <fused name>_mapping.json."""

    def __init__(self, enc, resultant, fused, path):
        self.enc, self.resultant, self.fused = enc, resultant, fused
        self.where = os.path.basename(path)
        with devsmap.reading(self.where):
            doc = devsmap.read_json(path)
            self.exprs = doc["mapping"][fused.name]
            self.invariant = doc.get("invariant", [])
            functions = doc.get("functions", {})
        if set(self.exprs) != set(fused.s):
            raise ValueError(f"{self.where}: maps {sorted(self.exprs)}, "
                             f"{fused.name}'s state variables are "
                             f"{sorted(fused.s)}")
        within(self.where, enc.declare_functions, functions)

    def scope(self, s):
        names = dict(self.enc.params)
        for cid, values in s.vars.items():
            *outer, last = self.resultant.ids[cid]
            node = names
            for part in outer:
                node = node.setdefault(part, {})
            node[last] = {**values, "e": duration(s.e[cid])}
        names["ta"] = self.resultant.ta(s)
        return names

    def __call__(self, s):
        """h(s): F's state."""
        scope = self.scope(s)
        return {v: within(f"{self.where}: {v}", self.enc.choose, expr,
                          self.fused.s[v], scope)
                for v, expr in self.exprs.items()}

    def holds(self, condition, s):
        return within(f"{self.where}: {condition}", self.enc.condition,
                      condition, self.scope(s))


def _query(premises, goal):
    """("proven", None), ("failed", model) or ("unknown", reason)."""
    solver = z3.Solver()
    solver.set(timeout=QUERY_TIMEOUT * 1000)
    solver.add(*premises)
    solver.add(z3.Not(goal))
    result = solver.check()
    if result == z3.unsat:
        return "proven", None
    if result == z3.sat:
        return "failed", solver.model()
    return "unknown", solver.reason_unknown()


def _equal_multisets(a, b):
    """Whether two guarded message lists hold the same messages, counted."""
    def count(slots, v):
        return z3.Sum([z3.If(all_of(g, m == v), 1, 0) for g, m in slots]
                      or [z3.IntVal(0)])
    return all_of(*[count(a, v) == count(b, v) for _, v in a + b])


class _Proof:
    """The obligations for a resultant R, a fused model F and a Mapping h.
    Each check prints its result; failed collects the unproven ones."""

    def __init__(self, resultant, fused, h):
        self.r, self.f, self.h = resultant, fused, h
        self.s = resultant.symbols()
        self.hs = h(self.s)
        self.t = z3.Real("t")
        self.failed, self.dropped = [], []
        self.checked, self.vacuous = 0, []
        inputs = {p: z3.Const(p, z3.SeqSort(sort))
                  for p, sort in resultant.x.items()}
        none = {p: z3.Empty(z3.SeqSort(sort))
                for p, sort in resultant.x.items()}
        self.events = [resultant.event(self.s, kind, self.t,
                                       none if kind == "int" else inputs)
                       for kind in (EVENTS if inputs else ["int"])]

    def parts(self, conditions, s):
        """{name: Bool}: the invariant's conditions in state s."""
        return {WELL_FORMED: self.r.well_formed(s),
                **{c: self.h.holds(c, s) for c in conditions}}

    def invariant(self, conditions, s):
        return all_of(*self.parts(conditions, s).values())

    def not_initially(self, conditions):
        """[(condition, why)]: the invariant's conditions not proven in R's
        initial state."""
        found = []
        for c, g in self.parts(conditions, self.r.initial()).items():
            status, why = _query([], g)
            if status != "proven":
                found.append((c, "false" if status == "failed"
                              else f"unknown to Z3 ({why})"))
        return found

    def broken(self, conditions, event):
        """(status, model, the conditions the event breaks in it)."""
        after = self.parts(conditions, event.after)
        status, m = _query([self.invariant(conditions, self.s), event.holds],
                           all_of(*after.values()))
        if status != "failed":
            return status, m, []
        return status, m, [c for c, g in after.items()
                           if z3.is_false(m.eval(g, True))]

    def report(self, name, status, detail, premise):
        """Print an obligation's result; detail is a counterexample's lines
        if it failed, why Z3 gave up if unknown. A proven obligation whose
        premise no state satisfies is marked vacuous."""
        self.checked += 1
        if status == "proven":
            vacuous = _query(premise, FALSE)[0] == "proven"
            if vacuous:
                self.vacuous.append(name)
            print(f"prove: {name}: proven"
                  + (" (vacuously: the invariant rules it out)"
                     if vacuous else ""))
            return
        self.failed.append(name)
        if status == "unknown":
            print(f"prove: {name}: NOT PROVEN (Z3: {detail})")
        else:
            print("\n".join([f"prove: {name}: NOT PROVEN", *detail]))

    def describe(self, m, event=None):
        """A counterexample: R's state, h of it, and the event."""
        def value(x):
            return show(m.eval(x, model_completion=True))

        lines = ["  state:", *self.state_lines(m, self.s),
                 "  fused state h(s): " + ", ".join(
                     f"{v} = {value(x)}" for v, x in self.hs.items())]
        if event is not None:
            given = [f"{p} = {value(x)}" for p, x in event.inputs.items()
                     if z3.is_true(m.eval(z3.Length(x) > 0, True))]
            imminent = [cid for cid, x in event.imminent.items()
                        if z3.is_true(m.eval(x, True))]
            lines.append(f"  event: {EVENTS[event.kind]} at t = "
                         f"{value(duration(self.t))}"
                         + (f", inputs {', '.join(given)}" if given else "")
                         + f"; imminent: {', '.join(imminent) or 'none'}")
        return lines

    @staticmethod
    def state_lines(m, s):
        def value(x):
            return show(m.eval(x, True))

        return [f"    {cid}: "
                + ", ".join(f"{v} = {value(x)}" for v, x in values.items())
                + f", e = {value(duration(s.e[cid]))}"
                for cid, values in s.vars.items()]

    @staticmethod
    def differences(m, want, got, what):
        return [f"  {what} {k}: expected {show(m.eval(want[k], True))}, "
                f"got {show(m.eval(got[k], True))}"
                for k in want
                if not z3.is_true(m.eval(want[k] == got[k], True))]

    def prune(self, conditions):
        """The conditions left after dropping, until none changes, those
        false initially or not inductive relative to the rest (Houdini)."""
        alive = list(conditions)

        def drop(found, why):
            nonlocal alive
            found = [c for c in found if c != WELL_FORMED]
            alive = [c for c in alive if c not in found]
            self.dropped += [(c, why) for c in found]
            for c in found:
                print(f"prove: prune: dropped {c} ({why})")
            return bool(found)

        drop([c for c, _ in self.not_initially(alive)],
             "not proven initially")
        changed = True
        while changed:
            changed = False
            for event in self.events:
                while drop(self.broken(alive, event)[2],
                           f"not kept by {EVENTS[event.kind]} events"):
                    changed = True
        print(f"prove: prune: kept {len(alive)} of {len(conditions)} "
              f"invariant conditions")
        return alive

    def check_init(self, init, conditions):
        detail = [f"  invariant {why} initially: {c}"
                  for c, why in self.not_initially(conditions)]
        mapped = self.h(self.r.initial())
        status, m = _query([], all_of(*[mapped[v] == init[v] for v in init]))
        if status == "failed":
            detail += self.differences(m, init, mapped, "fused initial")
        elif status == "unknown":
            detail.append(f"  h(R's initial state) = F's initial state: "
                          f"unknown to Z3 ({m})")
        self.report("init", "failed" if detail else "proven", detail, [])

    def check_inductive(self, conditions):
        for event in self.events:
            status, m, broken = self.broken(conditions, event)
            detail = m
            if status == "failed":
                detail = [*self.describe(m, event), "  after it:",
                          *self.state_lines(m, event.after),
                          *[f"  breaks: {c}" for c in broken]]
            self.report(f"invariant kept by {EVENTS[event.kind]} events",
                        status, detail,
                        [self.invariant(conditions, self.s), event.holds])

    def check_ta(self, conditions):
        want, got = self.r.ta(self.s), self.f.ta(self.hs)
        self.check(
            "ta", [self.invariant(conditions, self.s)], want == got,
            lambda m: self.describe(m) + [
                f"  ta: R {show(m.eval(want, True))}, F "
                f"{show(m.eval(got, True))}"])

    def check_output(self, conditions):
        event = self.events[0]  # internal: R's imminent components output
        want, got = self.r.output(event.sent), self.f.output(self.hs)
        ports = list(dict.fromkeys([*want, *got]))
        has_ports = bool(self.r.x or self.r.y)
        seqs = [{p: _seq(side.get(p, []),
                         self.r.y[p] if has_ports else STRING) for p in ports}
                for side in (want, got)]
        if has_ports:
            same = all_of(*[seqs[0][p] == seqs[1][p] for p in ports])
        else:
            same = all_of(*[_equal_multisets(want.get(p, []), got.get(p, []))
                            for p in ports])
        premise = [self.invariant(conditions, self.s), event.holds]
        for label, guard, _ in self.f.enc.branches(self.f.body["lambda"],
                                                   self.f.scope(self.hs)):
            self.check(f"lambda, F branch {label!r}", premise + [guard], same,
                       lambda m: self.describe(m, event) + self.differences(
                           m, seqs[0], seqs[1], "output on"))

    def check_transitions(self, conditions):
        for event in self.events:
            want = self.h(event.after)
            premise = [self.invariant(conditions, self.s), event.holds]
            for label, guard, got in self.f.transitions(
                    event.kind, self.hs, duration(self.t), event.inputs):
                self.check(
                    f"delta_{event.kind} on {EVENTS[event.kind]} events, F "
                    f"branch {label!r}", premise + [guard],
                    all_of(*[want[v] == got[v] for v in want]),
                    lambda m, got=got: self.describe(m, event)
                    + self.differences(m, want, got, "fused"))

    def check(self, name, premise, goal, explain):
        status, m = _query(premise, goal)
        self.report(name, status, explain(m) if status == "failed" else m,
                    premise)


def _parameters(enc, coupled, fused):
    """Define the parameters of every model: one value per name, from the
    coupled model's parameter file, else any value."""
    domains = {}
    for model in [*devsmap.models(coupled), fused]:
        for name, domain in model["parameters"].items():
            if enc.sort(domains.setdefault(name, domain)) != enc.sort(domain):
                raise ValueError(f"parameter {name} has the domains "
                                 f"{domains[name]} and {domain}")
    values = {}
    for leaf in devsmap.read_params(coupled).values():
        for name, text in leaf.items():
            if values.setdefault(name, text) != text:
                raise ValueError(f"parameter {name} has the values "
                                 f"{values[name]} and {text}; one value per "
                                 f"name is supported")
    enc.define_parameters(domains, values)


def assumptions(enc):
    """What a proof made with the Encoding enc assumes."""
    found = list(ASSUMPTIONS)
    if enc.ranges:
        found.append("values of a set with a range stay within it: "
                     + ", ".join(f"{name} {text}" for name, text
                                 in sorted(enc.ranges.items()))
                     + "; the encoding uses the field's full domain")
    return found + [f"{name} returns a floating_point (its domain is not "
                    f"under \"functions\" in the mapping file)"
                    for name in sorted(enc.assumed)]


def prove(coupled_path, fused_path, prune=False):
    """Prove the fused atomic at fused_path equivalent to the coupled model
    at coupled_path; print each obligation's result and return a Report.
    A bad model or mapping file is a ValueError."""
    started = time.time()
    coupled = devsmap.load_model(coupled_path)
    fused_model = devsmap.load_model(fused_path)
    if coupled["kind"] != "coupled" or fused_model["kind"] != "atomic":
        raise ValueError("prove needs a coupled model and a fused atomic "
                         "model")
    enc = Encoding(devsmap.definition_files(coupled)
                   + devsmap.definition_files(fused_model))
    _parameters(enc, coupled, fused_model)
    resultant = Resultant(enc, coupled)
    fused = Atomic(enc, fused_model["name"], fused_model["body"])
    if resultant.x or resultant.y:
        if (fused.x, fused.y) != (resultant.x, resultant.y):
            raise ValueError(f"{fused.name} must have the coupled model's "
                             f"input and output ports")
    elif fused.x:
        raise ValueError(f"{fused.name} has input ports; the coupled model "
                         f"has none")
    path = os.path.join(os.path.dirname(fused_model["path"]),
                        f"{fused.name}_mapping.json")
    h = Mapping(enc, resultant, fused, path)
    print(f"prove: {coupled['name']} (components "
          f"{', '.join(resultant.parts)}) -> {fused.name}, mapping "
          f"{os.path.basename(path)}")

    proof = _Proof(resultant, fused, h)
    conditions = list(h.invariant)
    if prune:
        conditions = proof.prune(conditions)
    proof.check_init(fused.initial(fused_model["init"][fused.name]),
                     conditions)
    proof.check_inductive(conditions)
    proof.check_ta(conditions)
    proof.check_output(conditions)
    proof.check_transitions(conditions)

    seconds = time.time() - started
    counts = (f"{proof.checked} obligations, {len(proof.vacuous)} of them "
              f"vacuous; {seconds:.1f} s")
    for name in sorted(enc.constant_calls):
        print(f"prove: warning: {name}() takes no arguments, yet is "
              f"assumed to return the same value at every call")
    if proof.failed:
        print(f"prove: NOT PROVEN: {len(proof.failed)} failed ({counts})")
    else:
        print(f"prove: PROVEN: {fused.name} is equivalent to "
              f"{coupled['name']} ({counts}), assuming:")
        print("\n".join(f"- {a}" for a in assumptions(enc)))
    return Report(not proof.failed, proof.failed, proof.vacuous,
                  [c for c, _ in proof.dropped], seconds)
