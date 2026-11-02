"""Trusted local owner CLI. Register adapters in Python, never through input data."""
import argparse
import json
import sys
from .contracts import FeedbackPolicy


def main(argv=None, *, assemblies=None):
    factories = assemblies or {}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--assembly", required=True, help="Explicitly registered local adapter name")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect")
    for name in ("receipt", "feedback"):
        commands.add_parser(name).add_argument("--delivery", required=True)
    settings = commands.add_parser("set-feedback")
    settings.add_argument("--report-already-completed", choices=("on", "off"), required=True)
    settings.add_argument("--show-selected-pieces", choices=("on", "off"), required=True)
    finish = commands.add_parser("finalize")
    finish.add_argument("--revision", type=int, required=True)
    invalidate = commands.add_parser("invalidate")
    invalidate.add_argument("--candidate", required=True)
    invalidate.add_argument("--reason", required=True)
    invalidate.add_argument("--revision", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.assembly not in factories:
            raise ValueError("assembly adapter is not registered")
        with factories[args.assembly](args.state) as assembly:
            if args.command == "inspect":
                output = assembly.inspect()
            elif args.command == "receipt":
                output = assembly.receipt(args.delivery)
            elif args.command == "feedback":
                output = assembly.feedback(args.delivery)
            elif args.command == "set-feedback":
                assembly.set_feedback(FeedbackPolicy(args.report_already_completed == "on", args.show_selected_pieces == "on"))
                output = {"status": "UPDATED"}
            elif args.command == "finalize":
                output = assembly.finalize(expected_revision=args.revision)
            else:
                output = assembly.invalidate(args.candidate, reason=args.reason, expected_revision=args.revision)
        code = 0
    except Exception as exc:
        output, code = {"error": type(exc).__name__, "message": str(exc)}, 2
    encoded = json.dumps(output, ensure_ascii=False, allow_nan=False) + "\n"
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(encoded.encode("utf-8"))
    else:
        sys.stdout.write(encoded)
    return code
