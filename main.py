"""devs-flatten command line.

    python main.py <name>_coupled.json   fuse a coupled DEVSMap model
    python main.py --check               run the check suite (verify.py)
    python main.py --selftest            only the fuse verifier check
"""

import argparse
import sys

import cadmium_api
import fuse
import verify


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Fuse a coupled DEVSMap model into one atomic model, "
                    "verified in Cadmium.")
    parser.add_argument("model", nargs="?",
                        help="the <name>_coupled.json to fuse")
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
    try:
        if args.check:
            verify.run_checks()
        elif args.selftest:
            verify.selftest()
        else:
            return fuse.fuse(args.model)
    except (ValueError, cadmium_api.CadmiumError, fuse.LLMError,
            verify.CheckFailed) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
