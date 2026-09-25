from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from contextlib import AsyncExitStack, suppress
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import EvidenceGateway, GatewayUnavailable, connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case

STAGE_DIR = ".day09-run"
MAX_CASE_ATTEMPTS = 4


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.discover_tools():
            print(
                json.dumps(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    },
                    ensure_ascii=False,
                )
            )


async def _close(stack: AsyncExitStack | None) -> None:
    if stack is None:
        return
    # A broken session may fail again while closing; the run reconnects anyway.
    with suppress(Exception, BaseExceptionGroup):
        await stack.aclose()


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, GatewayUnavailable) or (
        isinstance(exc, RuntimeError) and "MCP failed" in str(exc)
    )


async def _run(root: Path, resume: bool = False) -> None:
    """Solve every case into a staging directory, then publish outputs and trace together.

    Each finished case is staged (trace first, output last), so an interrupted run can
    continue with ``--resume``. Existing artifacts are only replaced when all cases pass.
    """
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    stage = root / STAGE_DIR
    if stage.exists() and not resume:
        shutil.rmtree(stage)
    staged_outputs = stage / "outputs"
    staged_outputs.mkdir(parents=True, exist_ok=True)
    pending = [
        case_id
        for case_id in case_set.case_ids
        if not (staged_outputs / f"{case_id}.json").exists()
    ]
    if resume:
        print(f"Resuming: {len(case_set.case_ids) - len(pending)} cases already staged")

    stack: AsyncExitStack | None = None
    gateway: EvidenceGateway | None = None
    try:
        for index, case_id in enumerate(pending, 1):
            case = case_set.cases[case_id]
            case_trace_path = stage / f"{case_id}.trace.jsonl"
            for attempt in range(1, MAX_CASE_ATTEMPTS + 1):
                case_trace_path.unlink(missing_ok=True)
                try:
                    if gateway is None:
                        stack = AsyncExitStack()
                        gateway = await stack.enter_async_context(
                            connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts)
                        )
                        if not await gateway.discover_tools():
                            raise RuntimeError("MCP Gateway returned no tools")
                        gateway.record_dir = stage / "evidence"
                    (stage / "evidence" / f"{case_id}.jsonl").unlink(missing_ok=True)
                    case_trace = TraceWriter(case_trace_path, contracts)
                    case_trace.emit(
                        case_id=case_id, event_type="case_received", actor="coordinator"
                    )
                    output = await solve_case(case, gateway, case_trace)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    case_trace.emit(
                        case_id=case_id, event_type="case_finalized", actor="coordinator"
                    )
                    break
                except Exception as exc:
                    connection_failed = gateway is None
                    if not (_is_transient(exc) or connection_failed):
                        raise
                    await _close(stack)
                    stack, gateway = None, None
                    if attempt == MAX_CASE_ATTEMPTS:
                        raise RuntimeError(
                            f"{case_id}: MCP unavailable after {attempt} attempts; "
                            "rerun with `day09 run --resume`"
                        ) from exc
                    print(
                        f"{case_id}: MCP unavailable ({exc}); reconnecting "
                        f"({attempt}/{MAX_CASE_ATTEMPTS - 1})",
                        file=sys.stderr,
                        flush=True,
                    )
                    await asyncio.sleep(10 * attempt)
            staged_output = staged_outputs / f"{case_id}.json"
            staged_output.with_suffix(".tmp").write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            staged_output.with_suffix(".tmp").replace(staged_output)
            if index % 10 == 0 or index == len(pending):
                print(f"Completed {index}/{len(pending)} pending cases", flush=True)
    finally:
        await _close(stack)

    staged_trace = stage / "trace.jsonl"
    with staged_trace.open("w", encoding="utf-8") as target:
        for case_id in case_set.case_ids:
            with (stage / f"{case_id}.trace.jsonl").open("r", encoding="utf-8") as source:
                shutil.copyfileobj(source, target)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    for staged in staged_outputs.glob("*.json"):
        staged.replace(output_root / staged.name)
    staged_trace.replace(trace_path)
    evidence_root = root / ".local" / "evidence"
    if (stage / "evidence").exists():
        shutil.rmtree(evidence_root, ignore_errors=True)
        evidence_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(stage / "evidence", evidence_root)
    shutil.rmtree(stage)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume",
        action="store_true",
        help="continue an interrupted run, keeping cases already staged by that run",
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
