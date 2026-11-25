"""Load DEVSMap model trees and provide JSON helpers.

`load_model` includes referenced component models and initial states;
`models` lists the models of a loaded tree, each file once, and `leaves`
its atomic instances by leaf path; `model_files`, `definition_files` and
`param_path` locate the files a model uses and `read_params` reads its
parameter values; `model_name` extracts the model key; `read_json` and
`write_json` read and write a document.
"""

import contextlib
import json
import os

_KINDS = ("atomic", "coupled")
_IGNORED_KEYS = ("include_sets", "parameters")  # the paper's, not models


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


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, doc):
    """Write `doc` to `path` with 2-space indent, ASCII only."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")


def model_name(doc, where):
    """The one model key of a parsed DEVSMap document."""
    names = ([k for k in doc if k not in _IGNORED_KEYS]
             if isinstance(doc, dict) else [])
    if len(names) != 1:
        raise ValueError(f"{where}: expected exactly one top-level key, "
                         f"the model name")
    return names[0]


def load_model(path):
    """Load a DEVSMap model, its component models and its initial states."""
    try:
        model = _load(path)
    except RecursionError:
        raise ValueError(f"{os.path.basename(path)}: models nested too "
                         f"deeply (does a coupled model contain "
                         f"itself?)") from None
    model["init_path"] = os.path.join(os.path.dirname(model["path"]),
                                      f"{model['name']}_init_state.json")
    with _reading(os.path.basename(model["init_path"])):
        model["init"] = _read_init(model)
    return model


def _load(path):
    filename = os.path.basename(path)
    with _reading(filename):
        data = read_json(path)
        name = model_name(data, filename)
        kind = next((k for k in _KINDS if filename == f"{name}_{k}.json"),
                    None)
        if kind is None:
            raise ValueError(f"{filename}: model '{name}' must be in "
                             f"{name}_atomic.json or {name}_coupled.json")
        model = {"name": name, "kind": kind, "path": os.path.abspath(path),
                 "body": data[name], "parameters": data.get("parameters", {})}
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
                             for k in _KINDS) if os.path.exists(p)]
        if not found:
            raise ValueError(f"{filename}: component model '{sub}' needs "
                             f"{sub}_atomic.json or {sub}_coupled.json next "
                             f"to it")
        model["models"][sub] = _load(found[0])
    return model


def models(model):
    """The model and every model below it, each file once."""
    found = {model["path"]: model}
    for sub in model.get("models", {}).values():
        for m in models(sub):
            found.setdefault(m["path"], m)
    return list(found.values())


def leaves(model):
    """{leaf path: (component ids, atomic model)} of every atomic instance
    in the tree, in component order. The path is the ids from the top
    model down, joined by "."; an atomic model is its own leaf, with no
    ids and its name as its path."""
    found = {}

    def walk(m, ids):
        if m["kind"] == "atomic":
            found[".".join(ids) or model["name"]] = (ids, m)
            return
        for c in m["components"]:
            walk(m["models"][c["model"]], (*ids, c["id"]))

    walk(model, ())
    return found


def model_files(model):
    """Paths of the model's file and every component model file below it."""
    return [m["path"] for m in models(model)]


def definition_files(model):
    """Paths of the files in the include_sets of the model and every
    component model, each relative to the model file listing it."""
    paths = []
    for model_path in model_files(model):
        folder = os.path.dirname(model_path)
        for name in read_json(model_path).get("include_sets", []):
            path = os.path.abspath(os.path.join(folder, name))
            if not os.path.exists(path):
                raise ValueError(f"{os.path.basename(model_path)}: "
                                 f"include_sets file {name} does not exist")
            paths.append(path)
    return list(dict.fromkeys(paths))


def param_path(model):
    """Where the model's parameter values are, if it has any."""
    return os.path.join(os.path.dirname(model["path"]),
                        f"{model['name']}_param.json")


def read_params(model):
    """{leaf path: {parameter: value}} from the model's parameter file ({}
    without one); a component the file leaves out gets {}."""
    path = param_path(model)
    if not os.path.exists(path):
        return {}
    with _reading(os.path.basename(path)):
        return _per_leaf(model, read_json(path)["parameters"], default={})


def _read_init(model):
    return _per_leaf(model, read_json(model["init_path"])["init_states"])


def _per_leaf(model, doc, default=None):
    """{leaf path: node} of a document in the paper's hierarchical form
    ({"<top>": {"<id>": {"<sub id>": ...}}}); a component missing from it is
    an error, or gets `default` if one is given."""
    top, found = doc[model["name"]], {}
    for path, (ids, _) in leaves(model).items():
        node = top
        for cid in ids:
            node = node[cid] if default is None else node.get(cid, default)
        found[path] = node
    return found
