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
    "outputs": [(src, out_port, root_out_port)]}.
    Duplicate edges are kept: Cadmium delivers once per hierarchical path.
    """
    components, couplings = {}, []

    # Returns (ins, outs): boundary port -> [(leaf_id, leaf_port)].
    def walk(m, path):
        body = m["body"]
        x, y = body.get("x", {}), body.get("y", {})
        if m["kind"] == "atomic":
            components[path] = {"model": m["name"], "x": x, "y": y,
                                "initial": model["init"][path]}
            return {p: [(path, p)] for p in x}, {p: [(path, p)] for p in y}

        child = {}
        for comp in m["components"]:
            cid = comp["id"]
            child[cid] = walk(m["models"][comp["model"]],
                              f"{path}.{cid}" if path else cid)

        for c in body.get("ic", []):
            for src in child[c["component_from"]][1][c["port_from"]]:
                for dst in child[c["component_to"]][0][c["port_to"]]:
                    couplings.append((*src, *dst))

        ins = {p: [] for p in x}
        for c in body.get("eic", []):
            ins[c["port_from"]] += child[c["component_to"]][0][c["port_to"]]
        outs = {p: [] for p in y}
        for c in body.get("eoc", []):
            outs[c["port_to"]] += child[c["component_from"]][1][c["port_from"]]
        return ins, outs

    ins, outs = walk(model, model["name"] if model["kind"] == "atomic" else "")
    return {
        "components": components,
        "couplings": sorted(couplings),
        "inputs": sorted((p, *d) for p, ds in ins.items() for d in ds),
        "outputs": sorted((*s, p) for p, ss in outs.items() for s in ss),
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
