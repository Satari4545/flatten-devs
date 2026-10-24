"""Patch layer for James's DEVSMap_to_Cadmium_Generator.

    apply_patches(directory) -> patch the generator copy staged in directory

The vendored generator is never modified; cadmium_api copies it and these
anchor replacements are applied to the copy. An anchor found an unexpected
number of times raises PatchError, so upstream changes fail loudly.

  P1  Cadmium v2 include paths.
  P2  DEVSMap set names ("string", "R") mapped to C++ types.
  P3  Per-instance initial states (upstream gives every instance of a model
      the same state): state constructors, addComponent<M>(id, MState(...)).

Not patched (cadmium_api and devsmap/SKILL.md keep models out of them): no
EIC/EOC or coupled ports, no delta_con, ta only {"otherwise": var}.
"""

import os


class PatchError(Exception):
    """A patch anchor was not found in the staged generator copy."""


_TYPE_MAP_SRC = '''

TYPE_MAP = {"string": "std::string", "R": "double"}


def cpp_type(domain):
    """DEVSMap set name -> C++ type (unknown names pass through)."""
    return TYPE_MAP.get(domain, domain)
'''

_STATE_STRUCT_FN = '''

def generate_state_struct_per_instance(state_name, model):
    """State struct with a default and an all-variables constructor."""
    defaults = {"std::string": '\\"\\"', "double": "0"}
    variables = list(model["s"].items())
    out = "struct " + state_name + " {\\n"
    for var, domain in variables:
        out += "\\t" + cpp_type(domain) + " " + var + ";\\n"
    out += "\\n\\t" + state_name + "(): "
    out += ", ".join(
        var + "(" + defaults.get(cpp_type(domain),
                                 cpp_type(domain) + "()") + ")"
        for var, domain in variables)
    out += " {\\n\\t}\\n"
    params = ", ".join(cpp_type(domain) + " " + var
                       for var, domain in variables)
    inits = ", ".join(var + "(" + var + ")" for var, domain in variables)
    out += "\\t" + state_name + "(" + params + "): " + inits
    out += " {\\n\\t}\\n};\\n\\n"
    return out
'''

_COUPLED_HELPERS = '''

def _component_init_args(data, coupled_name, comp_id):
    """C++ constructor arguments from the component's init_state entry."""
    values = data["init_states"][coupled_name][comp_id].values()
    return ", ".join("std::numeric_limits<double>::infinity()"
                     if value == "inf" else value for value in values)
'''

_ADD_COMPONENT_OLD = """    # addComponent statements
    components = get_components(model)
    component_statements = ''
    for model_name, model_id in components.items():
        component_statements += '\\t\\tauto ' + model_id + ' = addComponent<' + model_name + '>("' + model_id + '");\\n'
    constructor += component_statements + '\\n'"""

_ADD_COMPONENT_NEW = """    # addComponent statements (P3: per-instance initial states)
    components = get_components(model)
    component_statements = ''
    for comp_model, comp_ids in components.items():
        if not isinstance(comp_ids, list):
            comp_ids = [comp_ids]
        for model_id in comp_ids:
            init_args = _component_init_args(data, model_name, model_id)
            component_statements += '\\t\\tauto ' + model_id + ' = addComponent<' + comp_model + '>("' + model_id + '", ' + comp_model + 'State(' + init_args + '));\\n'
    constructor += component_statements + '\\n'"""

_CTOR_OVERLOAD_OLD = """    port_initializations += '\\t}\\n\\n'
    return port_initializations"""

_CTOR_OVERLOAD_NEW = """    port_initializations += '\\t}\\n\\n'
    # P3: a second constructor taking the initial state
    _body = port_initializations.split('{\\n', 1)[1]
    port_initializations += ('\\t' + model_name + '(const std::string id, '
                             'const ' + state_name + '& initial_state) : '
                             'Atomic<' + state_name + '>(id, '
                             'initial_state) {\\n' + _body)
    return port_initializations"""

PATCHES = [
    ("P1", "generate_atomic_model_hpp.py",
     "Cadmium v2 include path for atomics",
     [("cadmium/modeling/devs/atomic.hpp",
       "cadmium/core/modeling/atomic.hpp", 1)]),
    ("P1+P3", "generate_coupled_model_hpp.py",
     "Cadmium v2 include path for coupled models; <limits> for infinity "
     "in per-instance initial states",
     [('return \'#include "cadmium/modeling/devs/coupled.hpp"\\n\'',
       'return \'#include "cadmium/core/modeling/coupled.hpp"\\n'
       '#include <limits>\\n\'', 1)]),
    ("P2", "generate_atomic_model_hpp.py",
     "generated atomic headers include <string> and <limits>",
     [('return "#include <iostream>\\n"',
       'return "#include <iostream>\\n#include <string>\\n'
       '#include <limits>\\n"', 1)]),
    ("P2", "generate_atomic_model_hpp.py",
     "TYPE_MAP/cpp_type for DEVSMap set names",
     [("import re\n", "import re\n" + _TYPE_MAP_SRC, 1)]),
    ("P2", "generate_atomic_model_hpp.py",
     "state struct members use C++ types",
     [("'\\t' + variable_type + ' '", "'\\t' + cpp_type(variable_type) + ' '",
       1)]),
    ("P2", "generate_atomic_model_hpp.py",
     "addInPort uses C++ types (patched before the port declarations: "
     "the declaration anchor is a substring of this one)",
     [("addInPort<' + data_type + '>",
       "addInPort<' + cpp_type(data_type) + '>", 1)]),
    ("P2", "generate_atomic_model_hpp.py",
     "addOutPort uses C++ types (same ordering reason as addInPort)",
     [("addOutPort<' + data_type + '>",
       "addOutPort<' + cpp_type(data_type) + '>", 1)]),
    ("P2", "generate_atomic_model_hpp.py",
     "port declarations use C++ types",
     [("Port<' + data_type + '>", "Port<' + cpp_type(data_type) + '>", 2)]),
    ("P3", "generate_atomic_model_hpp.py",
     "state struct comes from the per-instance generator, not the "
     "first-match one",
     [("return generate_state_struct_no_parameters(init_states, "
       "state_name, model)",
       "return generate_state_struct_per_instance(state_name, model)", 1)]),
    ("P3", "generate_atomic_model_hpp.py",
     "atomic classes get a constructor taking an initial state",
     [(_CTOR_OVERLOAD_OLD, _CTOR_OVERLOAD_NEW, 1)]),
    ("P3", "generate_atomic_model_hpp.py",
     "the per-instance state struct generator itself (appended after "
     "the time-advance generator)",
     [("    return time_advance_function\n",
       "    return time_advance_function\n" + _STATE_STRUCT_FN, 1)]),
    ("P3", "generate_coupled_model_hpp.py",
     "thread the parsed data dict into coupled generation",
     [("generate_coupled_model(directory_cpp_code, coupled_model_name, "
       "coupled_model)",
       "generate_coupled_model(directory_cpp_code, coupled_model_name, "
       "coupled_model, data)", 1),
      ("def generate_coupled_model(directory, coupled_model_name, "
       "coupled_model):",
       "def generate_coupled_model(directory, coupled_model_name, "
       "coupled_model, data):", 1),
      ("generate_coupled_model_struct(coupled_model_name, coupled_model)",
       "generate_coupled_model_struct(coupled_model_name, coupled_model, "
       "data)", 1),
      ("def generate_coupled_model_struct(model_name, model):",
       "def generate_coupled_model_struct(model_name, model, data):", 1)]),
    ("P3", "generate_coupled_model_hpp.py",
     "addComponent passes each instance its own initial state",
     [(_ADD_COMPONENT_OLD, _ADD_COMPONENT_NEW, 1)]),
    ("P3", "generate_coupled_model_hpp.py",
     "init-state lookup helpers for the coupled generator (appended "
     "after the coupled struct generator)",
     [("    return constructor\n",
       "    return constructor\n" + _COUPLED_HELPERS, 1)]),
]


def apply_patches(directory):
    """Apply every patch to the generator copies staged in `directory`."""
    texts = {}
    for name, filename, why, replacements in PATCHES:
        path = os.path.join(directory, filename)
        if path not in texts:
            try:
                with open(path, encoding="utf-8") as f:
                    texts[path] = f.read()
            except OSError as exc:
                raise PatchError(f"patch {name} ({why}): cannot read "
                                 f"{filename}: {exc.strerror}") from None
        text = texts[path]
        for anchor, replacement, expected in replacements:
            found = text.count(anchor)
            if found != expected:
                raise PatchError(
                    f"patch {name} ({why}) failed on {filename}: anchor "
                    f"found {found} time(s), expected {expected}: "
                    f"{anchor[:80]!r}")
            text = text.replace(anchor, replacement)
        texts[path] = text
    for path, text in texts.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
