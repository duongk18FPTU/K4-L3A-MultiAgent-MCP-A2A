from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .resume import completed_cases
from .runner import solve_with_reconnect
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path, *, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    completed = completed_cases(root, case_set, contracts) if resume else set()
    if not resume:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    else:
        print(
            f"RESUME: {len(completed)} verified outputs preserved; "
            f"{len(case_set.case_ids) - len(completed)} cases remaining",
            flush=True,
        )
    trace = TraceWriter(trace_path, contracts)

    for case_id in case_set.case_ids:
        if case_id in completed:
            continue
        case = case_set.cases[case_id]
        output = await solve_with_reconnect(case, settings, contracts, trace)
        target = output_root / f"{case_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(target)
        print(f"OK: {case_id}", flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume",
        action="store_true",
        help="preserve verified outputs and continue the same competition run",
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, resume=args.resume))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
