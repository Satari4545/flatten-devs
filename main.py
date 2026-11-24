"""devs-flatten command line.

    python main.py <name>_coupled.json   fuse a coupled DEVSMap model
    python main.py <name>_coupled.json --header FILE -I DIR
                                         ... against a repo's C++ headers
    python main.py <name>_coupled.json --prove <fused>_atomic.json [--prune]
                                         prove a fusion equivalent (prove.py)
    python main.py --check               run the check suite (verify.py)
    python main.py --selftest            only the fuse verifier check
"""

import argparse
import os
import shlex
import sys

import cadmium_api
import fuse
import prove
import verify


def compiler_flags(header, include_dirs):
    """g++ flags for --header and -I, quoted for $CXXFLAGS (POSIX split)."""
    flags = [f for d in include_dirs for f in ("-I", os.path.abspath(d))]
    if header:
        flags += ["-include", os.path.abspath(header)]
    return shlex.join(flags)


def add_compiler_flags(flags):
    """Append flags to $CXXFLAGS, after any value it has or gets from .env."""
    fuse.load_env_file()
    old = os.environ.get("CXXFLAGS", "")
    os.environ["CXXFLAGS"] = f"{old} {flags}".strip()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Fuse a coupled DEVSMap model into one atomic model, "
                    "verified in Cadmium.")
    parser.add_argument("model", nargs="?",
                        help="the <name>_coupled.json to fuse")
    parser.add_argument("--header", metavar="FILE",
                        help="a C++ header the model needs (g++ -include)")
    parser.add_argument("-I", "--include-dir", dest="include_dirs",
                        metavar="DIR", action="append", default=[],
                        help="a header search directory (repeatable)")
    parser.add_argument("--prove", metavar="FUSED",
                        help="instead of fusing, prove the fused atomic model "
                             "FUSED equivalent to the coupled model, by the "
                             "<name>_mapping.json next to FUSED")
    parser.add_argument("--prune", action="store_true",
                        help="with --prove: first drop the invariant "
                             "conditions that are not inductive")
    parser.add_argument("--check", action="store_true",
                        help="run the check suite (needs g++, no API key)")
    parser.add_argument("--selftest", action="store_true",
                        help="run only the fuse verifier check")
    args = parser.parse_args(argv)
    # A cp1252 console shows "?" for characters it cannot encode.
    sys.stdout.reconfigure(errors="replace")
    if [bool(args.model), args.check, args.selftest].count(True) != 1:
        parser.error("give exactly one of: a model path, --check, "
                     "--selftest")
    flags = compiler_flags(args.header, args.include_dirs)
    if flags and not args.model:
        parser.error("--header and -I need a model path")
    if args.prove and (flags or not args.model):
        parser.error("--prove needs a model path, and no --header or -I")
    if args.prune and not args.prove:
        parser.error("--prune needs --prove")
    try:
        if args.check:
            verify.run_checks()
        elif args.selftest:
            verify.selftest()
        elif args.prove:
            return 0 if prove.prove(args.model, args.prove,
                                    args.prune).proven else 1
        else:
            if flags:
                add_compiler_flags(flags)
            return fuse.fuse(args.model)
    except (ValueError, cadmium_api.CadmiumError, fuse.LLMError,
            verify.CheckFailed) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
