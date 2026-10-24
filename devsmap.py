"""Load DEVSMap models (atomic rules: devsmap/SKILL.md).

The loader does not validate; a file it cannot read raises one ValueError
naming the file. Stdlib only.

    load_model(path)         -> model dict (below)
    model_name(doc, where)   -> the one model key of a parsed document
    write_json(path, doc)    -> write a document (2-space indent, ASCII)

A model dict has: name, kind ("atomic"/"coupled"), path, body (as written);
coupled models add components [{"model", "id"}] and models {name: model};
load_model's result adds init {instance path: {state var: value}} from
<name>_init_state.json (paths are component ids like "sub.leaf"; a
standalone atomic is keyed by its name).
"""

import contextlib
import json
import os

KINDS = ("atomic", "coupled")


@contextlib.contextmanager
def _reading(filename):
    try:
        yield
    except OSError as err:
        raise ValueError(f"{filename}: cannot read: {err.strerror}") from None
    except json.JSONDecodeError as err:
        raise ValueError(f"{filename}: invalid JSON: {err}") from None
    except KeyError as err:
        raise ValueError(f"{filename}: missing key {err}") from None
    except (TypeError, AttributeError) as err:
        raise ValueError(f"{filename}: unexpected structure: {err}") from None


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, doc):
    """Write `doc` to `path` with 2-space indent, ASCII only."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")


def model_name(doc, where):
    """The one model key of a parsed DEVSMap document."""
    if not isinstance(doc, dict) or len(doc) != 1:
        raise ValueError(f"{where}: expected exactly one top-level key, "
                         f"the model name")
    return next(iter(doc))


def load_model(path):
    """Load a DEVSMap model, its component models and its initial states."""
    try:
        model = _load(path)
    except RecursionError:
        raise ValueError(f"{os.path.basename(path)}: models nested too "
                         f"deeply (does a coupled model contain "
                         f"itself?)") from None
    filename = f"{model['name']}_init_state.json"
    with _reading(filename):
        model["init"] = _read_init(model, filename)
    return model


def _load(path):
    filename = os.path.basename(path)
    with _reading(filename):
        data = _read_json(path)
        name = model_name(data, filename)
        kind = next((k for k in KINDS if filename == f"{name}_{k}.json"),
                    None)
        if kind is None:
            raise ValueError(f"{filename}: model '{name}' must be in "
                             f"{name}_atomic.json or {name}_coupled.json")
        model = {"name": name, "kind": kind, "path": os.path.abspath(path),
                 "body": data[name]}
        if kind == "atomic":
            return model
        # The paper's form maps a model to one id; a list allows several.
        model["components"] = [
            {"model": sub, "id": cid}
            for sub, ids in model["body"]["components"].items()
            for cid in ([ids] if isinstance(ids, str) else ids)]

    folder = os.path.dirname(model["path"])
    model["models"] = {}
    for sub in dict.fromkeys(c["model"] for c in model["components"]):
        found = [p for p in (os.path.join(folder, f"{sub}_{k}.json")
                             for k in KINDS) if os.path.exists(p)]
        if not found:
            raise ValueError(f"{filename}: component model '{sub}' needs "
                             f"{sub}_atomic.json or {sub}_coupled.json next "
                             f"to it")
        model["models"][sub] = _load(found[0])
    return model


def _read_init(model, filename):
    doc = _read_json(os.path.join(os.path.dirname(model["path"]), filename))
    init = {}

    def walk(m, node, path):
        if m["kind"] == "atomic":
            init[path] = node
            return
        for c in m["components"]:
            walk(m["models"][c["model"]], node[c["id"]],
                 f"{path}.{c['id']}" if path else c["id"])

    walk(model, doc["init_states"][model["name"]],
         model["name"] if model["kind"] == "atomic" else "")
    return init
