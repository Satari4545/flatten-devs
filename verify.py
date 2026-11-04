"""Check suite for flattening, the Cadmium backend and the fuse loop.

    run_checks()  every check (python main.py --check)
    selftest()    the fuse verifier on the hand fusion (main.py --selftest)

A failed check raises CheckFailed. Needs g++; the LLM is stubbed.
"""

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
from unittest import mock

import cadmium_api
import devsmap
import devsmap_flatten
import fuse

REPO = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(REPO, "devsmap")
FIXTURES = os.path.join(REPO, "fixtures")
COUPLED = os.path.join(MODELS, "pingpong_coupled.json")
HAND_FUSED = os.path.join(FIXTURES, "fused_pingpong_atomic.json")
HAND_FUSED_INIT = os.path.join(FIXTURES, "fused_pingpong_init_state.json")


class CheckFailed(Exception):
    """A check of the suite failed."""


def check(ok, message):
    if not ok:
        raise CheckFailed(message)


def expect_error(error, text, what, run, *args):
    """Check that run(*args) raises `error` mentioning `text`."""
    try:
        run(*args)
    except error as exc:
        check(text in str(exc), f"{what}: expected {text!r} in: {exc}")
        return
    raise CheckFailed(f"{what}: no {error.__name__} raised")


def documents(name, body, init):
    """({name: body}, its init-state document)."""
    return {name: body}, {"init_states": {name: init}}


def write_model(folder, name, body, init, kind="atomic"):
    """Write <name>_<kind>.json and its init state; return the model path."""
    model_doc, init_doc = documents(name, body, init)
    path = os.path.join(folder, f"{name}_{kind}.json")
    devsmap.write_json(path, model_doc)
    devsmap.write_json(os.path.join(folder, f"{name}_init_state.json"),
                       init_doc)
    return path


def timer_body(**fields):
    """An atomic that outputs 1.0/2.0 once, after its initial sigma."""
    body = {
        "x": {}, "y": {"out": "double"}, "s": {"sigma": "double"},
        "delta_int": {"otherwise": {"sigma": "inf"}},
        "delta_ext": {"otherwise": {}},
        "delta_con": {"otherwise": {}},
        "lambda": {"otherwise": {"out": "1.0/2.0"}},
        "ta": {"otherwise": "sigma"},
    }
    body.update(fields)
    return body


def hand_fused():
    """(body, initial state) of the hand fusion, freshly loaded."""
    model = devsmap.load_model(HAND_FUSED)
    return model["body"], model["init"][model["name"]]


def slow_ball(folder):
    """The hand fusion with the wrong timer: wrong in the shown window."""
    body, init = hand_fused()
    for branch in body["delta_int"].values():
        branch["sigma"] = "0.2"
    return write_model(folder, "slow", body, init)


def count_to_ten(folder):
    """Replays the 10 shown hits, then stops: only the hidden horizon
    sees the difference."""
    body, init = hand_fused()
    body["s"]["n"] = "double"
    body["delta_int"] = {
        "n + 1.0 >= 10.0": {"n": "n + 1.0", "sigma": "inf"},
        **{c: {**v, "n": "n + 1.0"} for c, v in body["delta_int"].items()},
    }
    init["n"] = "0.0"
    return write_model(folder, "counter", body, init)


def reply(model_doc, init_doc):
    """An LLM reply carrying the two documents in the expected format."""
    return ("One player holds the ball at a time.\n"
            f"```json\n{json.dumps(model_doc)}\n```\n"
            f"```json\n{json.dumps(init_doc)}\n```\n")


def fusion_reply():
    """An LLM reply carrying the hand fusion."""
    return reply(devsmap.read_json(HAND_FUSED),
                 devsmap.read_json(HAND_FUSED_INIT))


def broken_reply():
    """The hand fusion, renamed "broken", outputting an undeclared name."""
    body, init = hand_fused()
    body["lambda"] = {"otherwise": {"send": "no_such_name"}}
    return reply(*documents("broken", body, init))


def check_flatten():
    flat = devsmap_flatten.flatten(devsmap.load_model(COUPLED))
    check(sorted(flat["components"]) == ["A", "B"],
          f"flatten: components {sorted(flat['components'])}")
    check(flat["couplings"] == [("A", "send", "B", "receive"),
                                ("B", "send", "A", "receive")],
          f"flatten: couplings {flat['couplings']}")
    phases = [flat["components"][c]["initial"]["phase"] for c in "AB"]
    check(phases == ["\"Send\"", "\"Wait\""],
          f"flatten: initial phases {phases}")
    print("flatten OK: A, B + 2 couplings, per-instance initial states")


def check_coupled_trace():
    events = cadmium_api.simulate(COUPLED, 1.05)
    got = [(e["time"], e["sender"], e["port"], e["value"]) for e in events]
    # Alternating senders show per-instance initial states work.
    want = [(round(0.1 * (i + 1), 10), "AB"[i % 2], "send", "ball")
            for i in range(10)]
    check(got == want, f"coupled trace: {got}")
    print("coupled trace OK: 10 alternating A/B hits, t = 0.1 .. 1.0")


def check_arithmetic(folder):
    """A real literal survives the generator as a double: 1.0/2.0 is 0.5."""
    half = write_model(folder, "half", timer_body(), {"sigma": "1.0"})
    values = [e["value"] for e in cadmium_api.simulate(half, 2.0)]
    check(values == ["0.5"], f"arithmetic: 1.0/2.0 gave {values}")
    print("generator arithmetic OK: 1.0/2.0 is 0.5")


def check_stray_files(folder):
    """Unrelated model files next to the simulated one are ignored."""
    for name in os.listdir(MODELS):
        if name.endswith(".json"):
            shutil.copy(os.path.join(MODELS, name), folder)
    with open(os.path.join(folder, "broken_coupled.json"), "w",
              encoding="utf-8") as f:
        f.write("{not json")
    events = cadmium_api.simulate(
        os.path.join(folder, os.path.basename(COUPLED)), 1.05)
    check(len(events) == 10,
          f"stray sibling files: {len(events)} events, expected 10")
    print("stray files OK: an unreadable sibling model file is ignored")


def check_fuse_verifier():
    reference = fuse.reference_run(COUPLED)
    check((reference.window, reference.horizon) == (1.0, fuse.MIN_HORIZON),
          f"fuse reference: window {reference.window}, "
          f"horizon {reference.horizon}")
    ok, info = fuse.verify(HAND_FUSED, reference)
    check(ok, f"fuse verifier rejected the hand fusion: {info}")
    print(f"fuse verifier OK: accepts the hand fusion ({info})")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ok, info = fuse.verify(slow_ball(tmp), reference)
        check(not ok and "trace mismatch" in info,
              f"fuse verifier on a wrong timer: {ok}, {info}")
        ok, info = fuse.verify(count_to_ten(tmp), reference)
        check(not ok and "after that window" in info,
              f"fuse verifier on a horizon cheat: {ok}, {info}")
    print("fuse verifier OK: rejects a wrong timer and a horizon cheat")


def check_reference_window(folder):
    """A model with float-drifting times (0.3 * 10 != 3.0) matches itself;
    one with nothing after the window is refused."""
    timer = write_model(folder, "timer", timer_body(
        delta_int={"otherwise": {"sigma": "0.3"}}), {"sigma": "0.3"})
    ok, info = fuse.verify(timer, fuse.reference_run(timer))
    check(ok, f"a period-0.3 timer failed against itself: {info}")
    expect_error(ValueError, "no output events after", "fusing a model "
                 "that stops after the window", fuse.reference_run,
                 count_to_ten(folder))
    print("fuse reference OK: a period-0.3 timer matches itself; a model "
          "with nothing after the window is refused")


def check_fuse_loop(folder):
    """fuse() with a stubbed LLM: bad replies are fed back, the hand fusion
    is accepted and written."""
    replies = iter(["```json\n{\"oops\": \n```\n```json\n{}\n```",
                    broken_reply(), fusion_reply()])
    prompts = []

    def llm(prompt, settings):
        prompts.append(prompt)
        return next(replies)

    out_dir = os.path.join(folder, "out")
    log = io.StringIO()
    with mock.patch.dict(os.environ, {"FUSE_MAX_ITERS": "3"}), \
            mock.patch.object(fuse, "llm_settings"), \
            mock.patch.object(fuse, "call_llm", llm), \
            contextlib.redirect_stdout(log):
        code = fuse.fuse(COUPLED, out_dir)
        quiet = write_model(folder, "quiet", timer_body(
            **{"lambda": {"otherwise": {}}}), {"sigma": "1.0"})
        expect_error(ValueError, "no output events", "fusing a silent model",
                     fuse.fuse, quiet, out_dir)
    text = log.getvalue()
    check(code == 0 and "invalid JSON in a block" in text,
          f"fuse loop: exit {code}, output:\n{text}")
    shown_times = [float(t) for t in re.findall(r'"time": ([0-9.]+)',
                                                 prompts[0])]
    check(shown_times and max(shown_times) <= 1.0,
          f"fuse loop: first prompt shows event times {shown_times}, "
          f"expected none after the window t = 1.0")
    check(len(prompts) == 3 and "REJECTED" in prompts[1]
          and "invalid JSON in a block" in prompts[1],
          "fuse loop: the second prompt does not carry the feedback on "
          "the malformed reply")
    check("failed (exit" in prompts[2],
          "fuse loop: the third prompt does not carry the Cadmium error on "
          "the broken proposal")
    written = [sorted(os.listdir(os.path.join(out_dir, d)))
               for d in os.listdir(out_dir)]
    check(written == [["fused_pingpong_atomic.json",
                       "fused_pingpong_init_state.json"]],
          f"fuse loop wrote {written}")
    print("fuse loop OK: window-only first prompt, malformed reply and "
          "a failing proposal fed back, hand fusion accepted and written; "
          "a model without outputs refused")


def selftest():
    check_fuse_verifier()


def run_checks():
    check_flatten()
    check_coupled_trace()
    for run in (check_arithmetic, check_stray_files):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            run(tmp)
    check_fuse_verifier()
    for run in (check_reference_window, check_fuse_loop):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            run(tmp)
