"""Run DEVSMap models in Cadmium v2, via James's DEVSMap generator.

    simulate(path, end_time, build_dir) -> list of output events
    parse_trace(log_path)               -> list of output events

An event is {"time", "sender", "port", "value"} (sender is the component id).

The model is staged for James's generator (third_party/devsmap_generator),
whose generated main.cpp is compiled against Cadmium and run. Models are not
validated here: when generating, compiling or simulating fails, CadmiumError
carries that step's output. An unreadable model file raises devsmap's
ValueError.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

import devsmap

_REPO = os.path.dirname(os.path.abspath(__file__))
_GENERATOR = os.path.join(_REPO, "third_party", "devsmap_generator",
                          "DEVSMap_parser.py")
_CADMIUM_INCLUDE = os.environ.get(
    "CADMIUM_INCLUDE", os.path.join(_REPO, "cadmium_v2", "include"))

# The generator needs a coupled top model, so an atomic one runs as
# component "top" of coupled model "wrapper".
_WRAPPER, _WRAPPED_ID = "wrapper", "top"


class CadmiumError(Exception):
    """Generating, compiling or running a model in Cadmium failed."""


def _stage(model, end_time, stage):
    """Write the generator's input files and output dirs under stage."""
    in_dir = os.path.join(stage, "input")
    os.makedirs(in_dir)
    os.makedirs(os.path.join(stage, "output", "main", "include"))
    # The generator takes the first key as the model name and reads with the
    # platform encoding, so files are rewritten as ASCII {name: body}.
    for m in (model, *model.get("models", {}).values()):
        devsmap.write_json(os.path.join(in_dir, os.path.basename(m["path"])),
                           {m["name"]: m["body"]})

    if model["kind"] == "coupled":
        top, top_file = model["name"], os.path.basename(model["path"])
        init = model["init"]
    else:
        top, top_file = _WRAPPER, f"{_WRAPPER}_coupled.json"
        devsmap.write_json(os.path.join(in_dir, top_file), {_WRAPPER: {
            "x": {}, "y": {}, "components": {model["name"]: [_WRAPPED_ID]},
            "eic": [], "eoc": [], "ic": []}})
        init = {_WRAPPED_ID: model["init"][model["name"]]}

    init_file = f"{top}_init_state.json"
    devsmap.write_json(os.path.join(in_dir, init_file),
                       {"init_states": {top: init}})
    devsmap.write_json(os.path.join(in_dir, f"{top}_experiment.json"), {
        "model_under_test": {"model": top_file, "initial_state": init_file,
                             "parameters": ""},
        "experimental_frame": {}, "cpic": {}, "pocc": {},
        "time_span": repr(float(end_time))})


def _run(cmd, what, timeout, cwd):
    """Run cmd in cwd; a non-zero exit is a CadmiumError with its output."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CadmiumError(f"{what} failed: {exc}") from None
    if r.returncode != 0:
        lines = (r.stdout + r.stderr).strip().splitlines()
        # Compiler output can be long; start just before the first error.
        first = next((i for i, line in enumerate(lines)
                      if "error" in line.lower()), 0)
        output = "\n".join(lines[max(first - 3, 0):])[:1500]
        raise CadmiumError(f"{what} failed (exit {r.returncode}):\n"
                           f"{output or 'no output'}")
    return r


def simulate(path, end_time, build_dir=None):
    """Generate, compile and run a DEVSMap model; return its output events.

    `build_dir` is kept for inspection (default: a temporary directory).
    """
    model = devsmap.load_model(path)
    if build_dir is None:
        with tempfile.TemporaryDirectory(prefix="cadmium_",
                                         ignore_cleanup_errors=True) as tmp:
            return _build_and_run(model, end_time, tmp)
    return _build_and_run(model, end_time, build_dir)


def _build_and_run(model, end_time, build_dir):
    stage = os.path.join(os.path.abspath(build_dir), "stage")
    if os.path.exists(stage):
        shutil.rmtree(stage)
    _stage(model, end_time, stage)
    _run([sys.executable, _GENERATOR], "DEVSMap generator", timeout=120,
         cwd=stage)

    cxx = os.environ.get("CXX", "g++")
    exe = os.path.join(stage, "simulation" + (".exe" if os.name == "nt"
                                              else ""))
    # -static on Windows: never pick up a different libstdc++ DLL from PATH.
    static = ["-static"] if os.name == "nt" else []
    _run([cxx, "-std=c++17", "-O1", *static, "-I", _CADMIUM_INCLUDE,
          os.path.join("output", "main", "main.cpp"), "-o", exe],
         "compile", timeout=300, cwd=stage)

    r = _run([exe], "simulation", cwd=stage,
             timeout=int(os.environ.get("DEVS_SIM_TIMEOUT", "60")))
    # The generated main.cpp logs with Cadmium's STDOUTLogger.
    trace = os.path.join(stage, "trace.csv")
    with open(trace, "w", encoding="utf-8") as f:
        f.write(r.stdout)
    return parse_trace(trace)


_ANSI_COLOR = re.compile(r"\x1b\[[0-9;]*m")


def parse_trace(log_path):
    """Read a ";"-separated Cadmium log (STDOUTLogger or CSVLogger) into
    output events, skipping state rows.

    Values are verbatim; times are rounded to 10 decimals.
    """
    events = []
    try:
        with open(log_path, encoding="utf-8") as f:
            rows = [_ANSI_COLOR.sub("", line).rstrip("\n") for line in f
                    if not line.startswith("sep=")]
        if not rows or not rows[0].startswith("time;"):
            raise ValueError("missing header")
        for row in rows[1:]:
            # Values may contain ";"; only the first four separate.
            time, _, sender, port, value = row.split(";", 4)
            if port:  # state rows have no port
                events.append({"time": round(float(time), 10),
                               "sender": sender, "port": port,
                               "value": value})
    except (OSError, ValueError) as exc:
        raise CadmiumError(f"bad trace {os.path.basename(log_path)}: "
                           f"{exc}") from None
    return events
