#!/usr/bin/env python3
"""Measure the incremental server-side input cost of Codex dynamic tools.

The experiment creates fresh ephemeral Codex threads with cumulative tool
subsets: no tools, the first tool, the first two tools, and so on.  It never
writes prompts or tool definitions to the result file; only hashes, names,
sizes, and usage counters are retained.

Dry run (the default):
    python3 tmp/codex_tool_token_experiment.py CAPTURE.json

Real run (consumes quota):
    python3 tmp/codex_tool_token_experiment.py CAPTURE.json --execute
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import queue
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = PROJECT_ROOT / ".claude" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

from codex_anthropic_bridge import (  # noqa: E402
    BRIDGE_INSTRUCTIONS,
    _normalized_usage,
    build_request,
    prepare_dynamic_tools,
    select_effort,
    select_model,
)
from codex_app_server import (  # noqa: E402
    CodexAppServerClient,
    CodexAppServerError,
)


PROBE_PROMPT = (
    "This is an input-token measurement probe. Do not call any tool. "
    "Reply with exactly: OK"
)
CAPTURED_TRAILER = (
    "\n\n<token_measurement_override>Do not perform the requested work and do "
    "not call any tool. Reply with exactly: OK</token_measurement_override>"
)
DEFAULT_TIMEOUT = 600.0
USAGE_GRACE_SECONDS = 3.0


class ExperimentError(RuntimeError):
    """The measurement cannot safely produce a valid result."""


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_capture(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"cannot read capture {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExperimentError("capture root must be a JSON object")
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ExperimentError("capture has no non-empty tools array")
    return payload


def load_token_counter(encoding_name: str) -> tuple[Callable[[str], int] | None, str]:
    try:
        import tiktoken  # type: ignore
    except ImportError:
        return None, "tiktoken is not installed; raw token columns will be null"
    try:
        encoding = tiktoken.get_encoding(encoding_name)
    except Exception as exc:
        raise ExperimentError(f"cannot load tiktoken encoding {encoding_name}: {exc}") from exc
    return lambda text: len(encoding.encode(text)), f"tiktoken:{encoding_name}"


def raw_subset_metrics(
    tools: list[dict[str, Any]], token_counter: Callable[[str], int] | None,
) -> list[dict[str, int | None]]:
    """Count the exact cumulative JSON representation sent in dynamicTools.

    Step zero represents an omitted dynamicTools field, not an empty array.
    """
    metrics: list[dict[str, int | None]] = [
        {"raw_json_chars": 0, "raw_json_bytes": 0, "raw_json_tokens": 0}
    ]
    for count in range(1, len(tools) + 1):
        serialized = compact_json(tools[:count])
        metrics.append({
            "raw_json_chars": len(serialized),
            "raw_json_bytes": len(serialized.encode("utf-8")),
            "raw_json_tokens": (
                token_counter(serialized) if token_counter is not None else None
            ),
        })
    return metrics


class EventCollector:
    """Collect app-server notifications and reject measurement-time tool calls."""

    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.Lock()
        self._tool_calls: dict[str, list[str]] = {}

    def on_notification(self, message: dict[str, Any]) -> None:
        self.events.put(message)

    def on_server_request(self, message: dict[str, Any]) -> dict[str, Any]:
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        thread_id = str(params.get("threadId") or "")
        tool = str(params.get("tool") or "<unknown>")
        with self._lock:
            self._tool_calls.setdefault(thread_id, []).append(tool)
        return {
            "contentItems": [{
                "type": "inputText",
                "text": "Tool calls are disabled during the token measurement.",
            }],
            "success": False,
        }

    def tool_calls(self, thread_id: str) -> list[str]:
        with self._lock:
            return list(self._tool_calls.get(thread_id, []))

    def wait_for_turn(
        self, thread_id: str, turn_id: str, timeout: float,
    ) -> dict[str, int]:
        deadline = time.monotonic() + timeout
        completed_at: float | None = None
        usage: dict[str, int] = {}

        while True:
            now = time.monotonic()
            if completed_at is not None and usage:
                return usage
            if completed_at is not None and now - completed_at >= USAGE_GRACE_SECONDS:
                raise ExperimentError(
                    f"turn {turn_id} completed without final token usage"
                )
            if now >= deadline:
                raise ExperimentError(f"timeout waiting for turn {turn_id}")

            wait_for = min(0.25, deadline - now)
            if completed_at is not None:
                wait_for = min(
                    wait_for,
                    max(0.001, USAGE_GRACE_SECONDS - (now - completed_at)),
                )
            try:
                message = self.events.get(timeout=max(0.001, wait_for))
            except queue.Empty:
                continue

            params = message.get("params")
            params = params if isinstance(params, dict) else {}
            if params.get("threadId") != thread_id:
                continue
            event_turn_id = params.get("turnId")
            if event_turn_id not in (None, turn_id):
                continue

            method = message.get("method")
            if method == "thread/tokenUsage/updated":
                token_usage = params.get("tokenUsage")
                if isinstance(token_usage, dict):
                    candidate = _normalized_usage(token_usage.get("last"))
                    if candidate:
                        usage = candidate
            elif method == "turn/completed":
                turn = params.get("turn")
                turn = turn if isinstance(turn, dict) else {}
                if turn.get("id") not in (None, turn_id):
                    continue
                status = turn.get("status")
                if status != "completed":
                    error = turn.get("error")
                    raise ExperimentError(
                        f"turn {turn_id} ended with {status}: {error}"
                    )
                completed_at = time.monotonic()
            elif method == "error":
                raise ExperimentError(f"app-server error for {turn_id}: {params}")


def atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "step", "added_tool", "tool_count", "input_tokens",
        "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
        "total_tokens", "raw_json_chars", "raw_json_bytes",
        "raw_json_tokens", "server_delta", "raw_delta", "internal_delta",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def enrich_rows(rows: list[dict[str, Any]]) -> None:
    for index, row in enumerate(rows):
        if index == 0:
            row["server_delta"] = None
            row["raw_delta"] = None
            row["internal_delta"] = None
            continue
        previous = rows[index - 1]
        row["server_delta"] = row["input_tokens"] - previous["input_tokens"]
        current_raw = row.get("raw_json_tokens")
        previous_raw = previous.get("raw_json_tokens")
        if isinstance(current_raw, int) and isinstance(previous_raw, int):
            row["raw_delta"] = current_raw - previous_raw
            row["internal_delta"] = row["server_delta"] - row["raw_delta"]
        else:
            row["raw_delta"] = None
            row["internal_delta"] = None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) < 2:
        return {"complete": False}
    baseline = rows[0]
    final = rows[-1]
    raw = final.get("raw_json_tokens")
    server_tools = final["input_tokens"] - baseline["input_tokens"]
    summary: dict[str, Any] = {
        "complete": True,
        "baseline_input_tokens": baseline["input_tokens"],
        "all_tools_input_tokens": final["input_tokens"],
        "server_tool_increment_tokens": server_tools,
        "raw_tool_json_tokens": raw,
        "internal_tool_increment_tokens": (
            server_tools - raw if isinstance(raw, int) else None
        ),
    }
    deltas = [
        row["server_delta"] for row in rows[1:]
        if isinstance(row.get("server_delta"), int)
    ]
    if deltas:
        summary["median_server_delta"] = statistics.median(deltas)
    return summary


def default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return PROJECT_ROOT / "tmp" / f"codex-tool-token-results-{stamp}.json"


def result_document(
    *, capture: Path, capture_hash: str, model: str, effort: str | None,
    prompt_mode: str, tokenizer: str, tools: list[dict[str, Any]],
    rows: list[dict[str, Any]], status: str,
) -> dict[str, Any]:
    enrich_rows(rows)
    return {
        "format_version": 1,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "capture_path": str(capture),
        "capture_sha256": capture_hash,
        "model": model,
        "effort": effort,
        "prompt_mode": prompt_mode,
        "tokenizer": tokenizer,
        "tool_order": [tool["name"] for tool in tools],
        "expected_steps": len(tools) + 1,
        "rows": rows,
        "summary": summarize(rows) if len(rows) == len(tools) + 1 else {
            "complete": False,
        },
    }


def load_resume_rows(
    path: Path, *, capture_hash: str, model: str, effort: str | None,
    prompt_mode: str, tokenizer: str, tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"cannot resume result {path}: {exc}") from exc
    expected = {
        "format_version": 1,
        "capture_sha256": capture_hash,
        "model": model,
        "effort": effort,
        "prompt_mode": prompt_mode,
        "tokenizer": tokenizer,
        "tool_order": [tool["name"] for tool in tools],
    }
    for key, value in expected.items():
        if document.get(key) != value:
            raise ExperimentError(
                f"cannot resume: {key} differs "
                f"({document.get(key)!r} != {value!r})"
            )
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise ExperimentError("cannot resume: rows is not an array")
    if len(rows) > len(tools) + 1:
        raise ExperimentError("cannot resume: result has too many rows")
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("step") != index:
            raise ExperimentError(
                f"cannot resume: row {index} is missing or has a wrong step"
            )
    return rows


def print_plan(
    *, capture: Path, output: Path, model: str, effort: str | None,
    prompt_mode: str, tools: list[dict[str, Any]], metrics: list[dict[str, Any]],
    tokenizer: str,
) -> None:
    final = metrics[-1]
    print(f"Capture: {capture}")
    print(f"Model / effort: {model} / {effort or '<default>'}")
    print(f"Prompt mode: {prompt_mode}")
    print(f"Tools: {len(tools)}; requests: {len(tools) + 1}")
    print(f"Tool order: {', '.join(tool['name'] for tool in tools)}")
    print(
        "Full dynamicTools JSON: "
        f"{final['raw_json_chars']} chars, {final['raw_json_bytes']} bytes, "
        f"{final['raw_json_tokens']} tokens ({tokenizer})"
    )
    print(f"Result JSON: {output}")
    print(f"Result CSV:  {output.with_suffix('.csv')}")
    print("Dry run only. Add --execute to consume quota and run the experiment.")


def run(args: argparse.Namespace) -> int:
    capture = args.capture.resolve()
    payload = load_capture(capture)
    tools, _names = prepare_dynamic_tools(payload)
    developer, captured_prompt, image_inputs = build_request(payload)
    model = args.model or select_model(payload)
    effort = args.effort or select_effort(payload)
    if not model:
        raise ExperimentError("capture does not select a Codex model; pass --model")

    token_counter, tokenizer = load_token_counter(args.encoding)
    metrics = raw_subset_metrics(tools, token_counter)
    output = (args.output or default_output_path()).resolve()
    capture_hash = sha256_file(capture)

    if args.prompt_mode == "probe":
        prompt = PROBE_PROMPT
        inputs = [{"type": "text", "text": prompt}]
    elif args.prompt_mode == "captured-short":
        prompt = captured_prompt + CAPTURED_TRAILER
        inputs = [{"type": "text", "text": prompt}] + image_inputs
    else:
        prompt = captured_prompt
        inputs = [{"type": "text", "text": prompt}] + image_inputs

    if not args.execute:
        print_plan(
            capture=capture, output=output, model=model, effort=effort,
            prompt_mode=args.prompt_mode, tools=tools, metrics=metrics,
            tokenizer=tokenizer,
        )
        return 0

    if token_counter is None:
        raise ExperimentError(
            "tiktoken is required for --execute so quota is not spent on an "
            "incomplete report; install it or expose it through PYTHONPATH"
        )

    if output.exists() and not args.resume:
        raise ExperimentError(
            f"refusing to overwrite existing result: {output}; "
            "choose --output or pass --resume"
        )

    collector = EventCollector()
    rows: list[dict[str, Any]] = (
        load_resume_rows(
            output, capture_hash=capture_hash, model=model, effort=effort,
            prompt_mode=args.prompt_mode, tokenizer=tokenizer, tools=tools,
        )
        if args.resume else []
    )
    if len(rows) == len(tools) + 1:
        print(f"Result is already complete: {output}")
        return 0
    document = result_document(
        capture=capture, capture_hash=capture_hash, model=model, effort=effort,
        prompt_mode=args.prompt_mode, tokenizer=tokenizer, tools=tools,
        rows=rows, status="running",
    )
    atomic_json_write(output, document)

    client = CodexAppServerClient(
        codex_bin=args.codex_bin,
        timeout=args.timeout,
        notification_handler=collector.on_notification,
        server_request_handler=collector.on_server_request,
    )
    try:
        client.start()
        for count in range(len(rows), len(tools) + 1):
            added_tool = tools[count - 1]["name"] if count else None
            params: dict[str, Any] = {
                "approvalPolicy": "never",
                "baseInstructions": BRIDGE_INSTRUCTIONS,
                "cwd": "/tmp",
                "developerInstructions": developer,
                "ephemeral": True,
                "sandbox": "read-only",
                "model": model,
            }
            if count:
                params["dynamicTools"] = tools[:count]

            print(
                f"[{count:02d}/{len(tools):02d}] "
                f"{added_tool or 'baseline'} ... ",
                end="",
                flush=True,
            )
            started = client.request("thread/start", params, timeout=args.timeout)
            thread = started.get("thread") if isinstance(started, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(thread_id, str):
                raise ExperimentError("thread/start returned no thread id")

            turn_params: dict[str, Any] = {"threadId": thread_id, "input": inputs}
            if model:
                turn_params["model"] = model
            if effort:
                turn_params["effort"] = effort
            turn_result = client.request(
                "turn/start", turn_params, timeout=args.timeout,
            )
            turn = turn_result.get("turn") if isinstance(turn_result, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str):
                raise ExperimentError("turn/start returned no turn id")

            usage = collector.wait_for_turn(thread_id, turn_id, args.timeout)
            calls = collector.tool_calls(thread_id)
            if calls:
                raise ExperimentError(
                    f"invalid measurement: turn called tools: {', '.join(calls)}"
                )
            row: dict[str, Any] = {
                "step": count,
                "added_tool": added_tool,
                "tool_count": count,
                **usage,
                **metrics[count],
            }
            rows.append(row)
            enrich_rows(rows)
            print(
                f"input={row.get('input_tokens')} "
                f"delta={row.get('server_delta')} cached={row.get('cached_input_tokens')}"
            )
            document = result_document(
                capture=capture, capture_hash=capture_hash, model=model,
                effort=effort, prompt_mode=args.prompt_mode,
                tokenizer=tokenizer, tools=tools, rows=rows, status="running",
            )
            atomic_json_write(output, document)
            write_csv(output.with_suffix(".csv"), rows)
    except BaseException as exc:
        document = result_document(
            capture=capture, capture_hash=capture_hash, model=model,
            effort=effort, prompt_mode=args.prompt_mode,
            tokenizer=tokenizer, tools=tools, rows=rows,
            status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
        )
        atomic_json_write(output, document)
        if rows:
            write_csv(output.with_suffix(".csv"), rows)
        raise
    finally:
        client.close()

    document = result_document(
        capture=capture, capture_hash=capture_hash, model=model, effort=effort,
        prompt_mode=args.prompt_mode, tokenizer=tokenizer, tools=tools,
        rows=rows, status="completed",
    )
    atomic_json_write(output, document)
    write_csv(output.with_suffix(".csv"), rows)
    summary = document["summary"]
    print("\nCompleted")
    print(f"Baseline input:       {summary['baseline_input_tokens']}")
    print(f"All-tools input:      {summary['all_tools_input_tokens']}")
    print(f"Server tool increase: {summary['server_tool_increment_tokens']}")
    print(f"Raw tool JSON:        {summary['raw_tool_json_tokens']}")
    print(f"Internal increase:    {summary['internal_tool_increment_tokens']}")
    print(f"JSON: {output}")
    print(f"CSV:  {output.with_suffix('.csv')}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure cumulative Codex dynamic-tool input-token increments. "
            "Without --execute, only validates and prints the 28-step plan."
        )
    )
    parser.add_argument("capture", type=Path, help="captured Claude payload JSON")
    parser.add_argument(
        "--execute", action="store_true",
        help="perform real model requests and consume quota",
    )
    parser.add_argument(
        "--prompt-mode", choices=("probe", "captured-short", "captured-exact"),
        default="probe",
        help=(
            "probe: minimal fixed prompt (default); captured-short: captured "
            "context plus an OK-only trailer; captured-exact: unmodified captured prompt"
        ),
    )
    parser.add_argument("--model", help="override model selected by the capture")
    parser.add_argument("--effort", help="override reasoning effort")
    parser.add_argument("--codex-bin", help="explicit Codex executable")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--encoding", default="o200k_base",
        help="tiktoken encoding for local compact-JSON counts",
    )
    parser.add_argument("--output", type=Path, help="result JSON path")
    parser.add_argument(
        "--resume", action="store_true",
        help="continue a matching partial --output file from its next step",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except (ExperimentError, CodexAppServerError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted; completed rows remain in the result file", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
