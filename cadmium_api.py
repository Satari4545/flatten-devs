"""Run DEVSMap models in Cadmium v2, via James's DEVSMap generator.

    simulate(path, end_time, build_dir) -> list of output events
    check(path)                         -> path of the generated model header

An event is {"time", "sender", "port", "value"} (sender is the component id).

The model is staged for James's generator (third_party/devsmap_generator),
whose generated main.cpp is compiled against Cadmium and run (`check` only
compiles it). Extra compiler flags come from $CXXFLAGS. Outputs are kept in
build/<model name>/ (overwritten per run): cpp/ holds the generated C++,
trace.csv the simulation log and stage/ the generator inputs. Models are not
validated here: when generating, compiling or simulating fails, CadmiumError
carries that step's output. An unreadable model file raises devsmap's
ValueError.
"""

import os
import shlex
import shutil
import subprocess
import sys

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
    for m in devsmap.models(model):
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


def _build_dir(model, build_dir=None):
    return os.path.abspath(
        build_dir or os.path.join(_REPO, "build", model["name"]))


def _generate(model, end_time, build_dir):
    """Stage the model and generate its C++, replacing build_dir's stage/
    and cpp/; return (stage, cpp)."""
    stage = os.path.join(build_dir, "stage")
    cpp = os.path.join(build_dir, "cpp")
    for d in (stage, cpp):
        shutil.rmtree(d, ignore_errors=True)
    _stage(model, end_time, stage)
    _run([sys.executable, _GENERATOR], "DEVSMap generator", timeout=120,
         cwd=stage)
    shutil.copytree(os.path.join(stage, "output", "main"), cpp)
    return stage, cpp


def _compile(stage, *args):
    """Compile in stage: built-in flags, then $CXXFLAGS, then args."""
    cxx = os.environ.get("CXX", "g++")
    # POSIX quoting: a backslash escapes, so Windows paths are written with /.
    extra = shlex.split(os.environ.get("CXXFLAGS", ""))
    _run([cxx, "-std=c++17", "-O1", "-I", _CADMIUM_INCLUDE, *extra, *args],
         "compile", timeout=300, cwd=stage)


def simulate(path, end_time, build_dir=None):
    """Generate, compile and run a DEVSMap model; return its output events.

    Outputs go to `build_dir` (default: build/<model name>/), replacing the
    previous run's: cpp/ (generated C++), trace.csv and stage/.
    """
    model = devsmap.load_model(path)
    build_dir = _build_dir(model, build_dir)
    trace = os.path.join(build_dir, "trace.csv")
    if os.path.exists(trace):
        os.remove(trace)
    stage, cpp = _generate(model, end_time, build_dir)

    exe = os.path.join(stage, "simulation" + (".exe" if os.name == "nt"
                                              else ""))
    # -static on Windows: never pick up a different libstdc++ DLL from PATH.
    static = ["-static"] if os.name == "nt" else []
    _compile(stage, *static, os.path.join(cpp, "main.cpp"), "-o", exe)

    _run([exe], "simulation", cwd=stage,
         timeout=int(os.environ.get("DEVS_SIM_TIMEOUT", "60")))
    # The generated main.cpp logs with Cadmium's CSVLogger.
    log = os.path.join(stage, "logfile.csv")
    if not os.path.exists(log):
        raise CadmiumError("simulation wrote no logfile.csv")
    shutil.copyfile(log, trace)
    return _parse_trace(trace)


def check(path):
    """Generate and compile (not link or run) a DEVSMap model in
    build/<model name>/; return the path of its generated <name>.hpp."""
    model = devsmap.load_model(path)
    stage, cpp = _generate(model, 0.0, _build_dir(model))
    # -c still instantiates the model's templates, so type errors surface.
    _compile(stage, "-c", os.path.join(cpp, "main.cpp"),
             "-o", os.path.join(stage, "main.o"))
    return os.path.join(cpp, "include", f"{model['name']}.hpp")


def _parse_trace(log_path):
    """Read a Cadmium CSVLogger log ("sep=;" line, then ";"-separated rows)
    into output events, skipping state rows.

    Values are verbatim; times are rounded to 10 decimals.
    """
    events = []
    try:
        with open(log_path, encoding="utf-8") as f:
            rows = [line.rstrip("\n") for line in f]
        if rows[:2] != ["sep=;", "time;model_id;model_name;port_name;data"]:
            raise ValueError("missing header")
        for row in rows[2:]:
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
