"""Aggregate AgentTrace JSONL records into a weekly grader-quality summary.

Reads one or more JSONL files produced by AgentTraceJsonlSink (see
xiamimate_agent_harness.AgentTraceJsonlSink) and prints pass/partial/fail
ratios grouped by scene and mode. Records without grader_result are counted
under `no_grader` so non-graded traffic is still visible.

Usage:
    python scripts/agent_trace_grader_summary.py [trace.jsonl ...] \\
        [--since-days 7] [--scene SCENE] [--json] [--top-failures N]

If no path is given, the script falls back to $AGENT_TRACE_SINK_PATH.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable


GRADER_STATUSES = ("pass", "partial", "fail", "error", "no_grader")


def _iter_records(paths: Iterable[Path]) -> Iterable[dict]:
    for path in paths:
        if not path.is_file():
            print(f"warning: {path} not found, skipping", file=sys.stderr)
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line_no, raw in enumerate(handle, 1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    yield json.loads(stripped)
                except json.JSONDecodeError as exc:
                    print(f"warning: {path}:{line_no} bad json ({exc}), skipping", file=sys.stderr)


def _record_status(record: dict) -> str:
    grader = record.get("grader_result") or {}
    status = str(grader.get("status") or "").strip().lower()
    if status in {"pass", "partial", "fail", "error"}:
        return status
    if status == "skipped":
        return "no_grader"
    return "no_grader"


def _group_key(record: dict) -> tuple[str, str, str]:
    scene = str(record.get("scene") or "unknown").strip() or "unknown"
    mode = str(record.get("mode") or "unknown").strip() or "unknown"
    model = str(record.get("model") or "").strip() or "-"
    return scene, mode, model


def _aggregate(records: Iterable[dict]) -> dict[str, Any]:
    by_group: dict[tuple[str, str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    failure_terms: collections.Counter = collections.Counter()
    earliest = None
    latest = None
    for record in records:
        key = _group_key(record)
        status = _record_status(record)
        by_group[key]["total"] += 1
        by_group[key][status] += 1
        grader = record.get("grader_result") or {}
        for failure in grader.get("failures") or []:
            failure_terms[str(failure)] += 1
        ts = record.get("finished_at_ms") or record.get("started_at_ms")
        if isinstance(ts, (int, float)):
            earliest = ts if earliest is None else min(earliest, ts)
            latest = ts if latest is None else max(latest, ts)
    return {"by_group": by_group, "failure_terms": failure_terms, "earliest_ms": earliest, "latest_ms": latest}


def _ratio(numerator: int, total: int) -> str:
    if total <= 0:
        return "-"
    return f"{numerator / total * 100:.1f}%"


def _print_text(summary: dict[str, Any], top_failures: int) -> None:
    by_group = summary["by_group"]
    if not by_group:
        print("no trace records in the selected window.")
        return
    header = ("scene", "mode", "model", "total", "pass", "partial", "fail", "error", "no_grader", "pass_ratio")
    rows: list[tuple[str, ...]] = []
    grand = collections.Counter()
    for (scene, mode, model), counter in sorted(by_group.items()):
        total = counter["total"]
        grand.update(counter)
        rows.append(
            (
                scene,
                mode,
                model,
                str(total),
                str(counter["pass"]),
                str(counter["partial"]),
                str(counter["fail"]),
                str(counter["error"]),
                str(counter["no_grader"]),
                _ratio(counter["pass"], total),
            )
        )
    rows.append(
        (
            "TOTAL",
            "-",
            "-",
            str(grand["total"]),
            str(grand["pass"]),
            str(grand["partial"]),
            str(grand["fail"]),
            str(grand["error"]),
            str(grand["no_grader"]),
            _ratio(grand["pass"], grand["total"]),
        )
    )
    widths = [max(len(header[i]), max(len(row[i]) for row in rows)) for i in range(len(header))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    earliest = summary["earliest_ms"]
    latest = summary["latest_ms"]
    if earliest and latest:
        window = f"{_fmt_ts(earliest)} -> {_fmt_ts(latest)}"
    else:
        window = "n/a"
    print(f"agent trace grader summary (window: {window})")
    print(fmt.format(*header))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in rows:
        print(fmt.format(*row))

    failures = summary["failure_terms"].most_common(max(0, int(top_failures)))
    if failures:
        print()
        print("top grader failure tags:")
        max_name = max(len(name) for name, _ in failures)
        for name, count in failures:
            print(f"  {name.ljust(max_name)}  {count}")


def _fmt_ts(ms: int | float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) // 1000))


def _print_json(summary: dict[str, Any]) -> None:
    output = {
        "window": {
            "earliest_ms": summary["earliest_ms"],
            "latest_ms": summary["latest_ms"],
        },
        "groups": [
            {
                "scene": scene,
                "mode": mode,
                "model": model,
                **{status: counter.get(status, 0) for status in ("total", *GRADER_STATUSES)},
                "pass_ratio": (counter["pass"] / counter["total"]) if counter["total"] else None,
            }
            for (scene, mode, model), counter in sorted(summary["by_group"].items())
        ],
        "top_failures": summary["failure_terms"].most_common(20),
    }
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _filter_since(records: Iterable[dict], since_days: float | None, scene_filter: str | None) -> Iterable[dict]:
    cutoff_ms: float | None
    cutoff_ms = (time.time() - since_days * 86400) * 1000 if since_days and since_days > 0 else None
    for record in records:
        if cutoff_ms is not None:
            ts = record.get("finished_at_ms") or record.get("started_at_ms") or 0
            if not isinstance(ts, (int, float)) or ts < cutoff_ms:
                continue
        if scene_filter and str(record.get("scene") or "") != scene_filter:
            continue
        yield record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="trace JSONL files; defaults to $AGENT_TRACE_SINK_PATH")
    parser.add_argument("--since-days", type=float, default=7.0, help="only include records finished within this many days (default 7; use 0 for all)")
    parser.add_argument("--scene", default=None, help="only include records matching this scene")
    parser.add_argument("--top-failures", type=int, default=10, help="print this many top grader failure tags (default 10)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text table")
    args = parser.parse_args(argv)

    raw_paths = args.paths or [p for p in [os.getenv("AGENT_TRACE_SINK_PATH")] if p]
    if not raw_paths:
        print("error: provide trace path(s) or set AGENT_TRACE_SINK_PATH", file=sys.stderr)
        return 2
    paths = [Path(p).expanduser() for p in raw_paths]

    records = _filter_since(_iter_records(paths), args.since_days, args.scene)
    summary = _aggregate(records)

    if args.json:
        _print_json(summary)
    else:
        _print_text(summary, args.top_failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
