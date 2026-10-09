"""Command-line entry point. The verifier itself is not implemented yet (see docs/ROADMAP.md M1)."""

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="otverify", description=__doc__)
    parser.add_argument("protocol", help="path to an Opentrons Python protocol")
    parser.parse_args(argv)
    print("otverify: not implemented yet", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
