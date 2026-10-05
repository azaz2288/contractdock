import argparse
import json
from pathlib import Path
import sys
from .core import ContractError, compare, load_fixture, parse_json, record, replay_server, write_fixture
from .scenario import load_scenario, scenario_server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record redacted JSON fixtures, replay locally, compare observed contracts")
    sub = parser.add_subparsers(dest="action", required=True)
    command = sub.add_parser("record")
    command.add_argument("url")
    command.add_argument("output", type=Path)
    command.add_argument("--allow-origin", action="append", required=True)
    command.add_argument("--method", choices=["GET", "POST"], default="GET")
    command.add_argument("--body-file", type=Path)
    command = sub.add_parser("compare")
    command.add_argument("before", type=Path)
    command.add_argument("after", type=Path)
    command = sub.add_parser("replay")
    command.add_argument("fixtures", type=Path, nargs="+")
    command.add_argument("--port", type=int, default=8099)
    command = sub.add_parser("scenario", help="Serve a globally ordered offline response sequence")
    command.add_argument("description", type=Path)
    command.add_argument("--port", type=int, default=8099)
    args = parser.parse_args(argv)
    try:
        if args.action == "record":
            body = None
            if args.body_file:
                with args.body_file.open("rb") as stream:
                    body = parse_json(stream.read(1024 * 1024 + 1))
            packet = record(args.url, allowed_origins=args.allow_origin, method=args.method, body=body)
            write_fixture(args.output, packet)
            print(json.dumps({"recorded": True, "key": packet["request"]["key"]}))
            return 0
        if args.action == "compare":
            report = compare(load_fixture(args.before), load_fixture(args.after))
            print(json.dumps(report))
            return 0 if report["passed"] else 1
        server = (scenario_server(load_scenario(args.description), port=args.port) if args.action == 'scenario'
                  else replay_server([load_fixture(path) for path in args.fixtures], port=args.port))
        with server:
            print(json.dumps({"replay_url": f"http://127.0.0.1:{server.server_port}", "offline": True,
                              "mode": args.action}), flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return 0
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({"complete": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
