"""Flatten a loaded DEVSMap model and summarize it for the LLM.

    flatten(model)           -> leaf atomics + leaf-to-leaf couplings
    flattened_summary(model) -> compact text for the LLM

`model` is a devsmap.load_model result. Stdlib only.
"""

import json


def flatten(model):
    """Dissolve nesting into leaf atomics and leaf-to-leaf couplings.

    Returns {"components": {path_id: {"model", "x", "y", "initial"}},
    "couplings": [(src, out_port, dst, in_port)],
    "inputs": [(root_in_port, dst, in_port)],
    "outputs": [(src, out_port, root_out_port)],
    "bags": {(dst, in_port): [(src, out_port) or (None, root_in_port)]}}.
    Duplicate edges are kept: Cadmium delivers once per hierarchical path.
    Outputs are in the order Cadmium collects a root output port's bag
    (eoc order, recursively), not sorted. `bags` lists the sources of each
    leaf input port in the order Cadmium appends their messages to its bag:
    at each level, the ic sources in ic order, then the eic ones in eic
    order.
    """
    components = {}
    feeds = {}  # (component path, in port) -> [(source path, port)]
    root = model["name"] if model["kind"] == "atomic" else ""

    # Returns output port -> [(leaf_id, leaf_port)], in bag order.
    def walk(m, path):
        body = m["body"]
        if m["kind"] == "atomic":
            components[path] = {"model": m["name"], "x": body.get("x", {}),
                                "y": body.get("y", {}),
                                "initial": model["init"][path]}
            return {p: [(path, p)] for p in body.get("y", {})}

        def child(cid):
            return f"{path}.{cid}" if path else cid

        outs = {c["id"]: walk(m["models"][c["model"]], child(c["id"]))
                for c in m["components"]}
        # Cadmium propagates ics while collecting outputs, eics afterwards.
        for c in body.get("ic", []):
            feeds.setdefault((child(c["component_to"]), c["port_to"]), []) \
                .extend(outs[c["component_from"]][c["port_from"]])
        for c in body.get("eic", []):
            feeds.setdefault((child(c["component_to"]), c["port_to"]), []) \
                .append((path, c["port_from"]))
        ys = {p: [] for p in body.get("y", {})}
        for c in body.get("eoc", []):
            ys[c["port_to"]] += outs[c["component_from"]][c["port_from"]]
        return ys

    def sources(dst, port):
        found = []
        for src, p in feeds.get((dst, port), []):
            if src in components:
                found.append((src, p))
            elif src == root:
                found.append((None, p))
            else:
                found += sources(src, p)
        return found

    outs = walk(model, root)
    bags = {(leaf, p): sources(leaf, p)
            for leaf, c in components.items() for p in c["x"]}
    edges = [(s, sp, d, dp) for (d, dp), srcs in bags.items()
             for s, sp in srcs]
    return {
        "components": components,
        "couplings": sorted(e for e in edges if e[0] is not None),
        "inputs": sorted((sp, d, dp) for s, sp, d, dp in edges if s is None),
        "outputs": [(*s, p) for p, ss in outs.items() for s in ss],
        "bags": bags,
    }


# Key order is kept: condition maps are ordered, "otherwise" last.
def _compact(obj):
    return json.dumps(obj, separators=(",", ":"))


def _atomic_models(model, out=None):
    out = {} if out is None else out
    if model["kind"] == "atomic":
        out[model["name"]] = model["body"]
    for sub in model.get("models", {}).values():
        _atomic_models(sub, out)
    return out


def flattened_summary(model):
    """Compact, deterministic text summary of the flattened model."""
    flat = flatten(model)
    lines = [f"Flattened DEVS summary of '{model['name']}'", "", "Components:"]
    for cid, c in sorted(flat["components"].items()):
        lines.append(f"- {cid}: model={c['model']}, "
                     f"in={sorted(c['x'])}, out={sorted(c['y'])}, "
                     f"initial={_compact(dict(sorted(c['initial'].items())))}")

    lines += ["", "Couplings:"]
    lines += [f"- {s}.{sp} -> {d}.{dp}" for s, sp, d, dp in flat["couplings"]]
    if not flat["couplings"]:
        lines.append("- (none)")

    lines += ["", "External ports:",
              f"- x (inputs):  {_compact(model['body'].get('x', {}))}",
              f"- y (outputs): {_compact(model['body'].get('y', {}))}"]
    lines += [f"- x.{p} -> {d}.{dp}" for p, d, dp in flat["inputs"]]
    lines += [f"- {s}.{sp} -> y.{p}" for s, sp, p in flat["outputs"]]

    lines += ["", "Atomic models (DEVSMap):"]
    for name, body in sorted(_atomic_models(model).items()):
        lines.append(f"- {name}: {_compact(body)}")

    lines += [
        "",
        "Resultant semantics:",
        "- time_advance = min over children of their remaining ta",
        "- imminent children (remaining ta equals that minimum) emit "
        "their outputs",
        "- outputs are routed through the couplings into input bags",
        "- imminent children take delta_int",
        "- children with input bags take delta_ext, where e is the time "
        "since their last transition (non-imminent children keep "
        "accumulating e)",
        "- if both happen together, use delta_con",
    ]
    return "\n".join(lines)
