"""JSON CLI; trusted launchers register objects, never import paths from input."""
import argparse
import json
from pathlib import Path
import sys
from .box import Box
from .contracts import Contribution, canonical, SailboatError
from .validation import catalog


def main(argv=None, *, domains=None, controllers=None):
    domains, controllers = domains or {}, controllers or {}
    parser = argparse.ArgumentParser(prog="sailboat-box", description="Blank core; adapters are registered by a trusted Python launcher.")
    parser.add_argument("--state", required=True, help="Explicit local SQLite state file")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("catalog"); p.add_argument("--domain", required=True)
    p = sub.add_parser("sort"); p.add_argument("--domain", required=True); p.add_argument("--input", required=True, help="UTF-8 contribution JSON file, or - for stdin")
    p = sub.add_parser("deliver"); p.add_argument("--result", required=True); p.add_argument("--controller", required=True)
    p = sub.add_parser("receipt"); p.add_argument("--delivery", required=True); p.add_argument("--recover", action="store_true"); p.add_argument("--controller")
    args = parser.parse_args(argv)
    try:
        domain = None
        if args.command in ("catalog", "sort"):
            if args.domain not in domains:
                raise SailboatError("domain is not explicitly registered by this launcher")
            domain = domains[args.domain]
        if args.command == "catalog":
            output = catalog(domain.catalog)
        else:
            controller = None
            if args.command == "deliver" or (args.command == "receipt" and args.recover):
                if args.controller not in controllers:
                    raise SailboatError("controller is not explicitly registered by this launcher")
                controller = controllers[args.controller]()
            with Box(args.state) as box:
                if args.command == "sort":
                    text = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8-sig")
                    raw = json.loads(text)
                    output = box.sort(Contribution(**raw), domain)
                elif args.command == "deliver":
                    output = box.deliver(args.result, controller)
                else:
                    output = box.receipt(args.delivery, controller=controller, recover=args.recover)
        _write(output)
        return 0
    except Exception as exc:
        _write({"error": type(exc).__name__, "message": str(exc)})
        return 2


def _write(value):
    text = canonical(value) + "\n"
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(text.encode("utf-8"))
    else:
        sys.stdout.write(text)
