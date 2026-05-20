#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


ERROR_RE = re.compile(
    r"(调用失败|失败原因|报错|\berror\b|超时|\bUNAUTHORIZED\b|(?:http|status|状态码|响应码)[：:=\s_-]*(?:401|403|500)\b|缺少必填参数)",
    re.I,
)
CANDIDATE_POOL_RE = re.compile(r"(?:candidate_pool_id|候选池\s*ID|候选池ID)[：:=`*\s]*([0-9a-fA-F-]{32,36})")

DEFAULT_CASES = [
    {
        "case_id": "explicit_resolve_candidates_humidifier",
        "title": "显式 resolve_candidates humidifier",
        "prompt": "/tool 请调用 resolve_candidates，解析 humidifier 在 Amazon 美国站的候选池，marketplace=US，recall_mode=keyword，max_candidates=8，并说明 pool_quality 是否足以继续分析。",
        "required_terms": ["候选池", "pool_quality"],
    },
    {
        "case_id": "explicit_category_resolve_humidifiers",
        "title": "显式 category_resolve Humidifiers",
        "prompt": "/tool 请调用 category_resolve，把 Humidifiers 解析成 Amazon/Keepa 美国站稳定类目 ID，并返回本地覆盖度。",
        "required_terms": ["17685839011", "覆盖"],
    },
]


def find_token(obj: Any) -> str:
    if isinstance(obj, dict):
        for key in ("token", "access_token", "jwt", "id_token"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            token = find_token(value)
            if token:
                return token
    if isinstance(obj, list):
        for value in obj:
            token = find_token(value)
            if token:
                return token
    return ""


def extract_sse(raw: bytes) -> tuple[str, str, str, bool]:
    text_parts: list[str] = []
    response_id = ""
    finish_reason = ""
    has_done = False
    for line in raw.splitlines():
        if line.strip() == b"data: [DONE]":
            has_done = True
        if not line.startswith(b"data: "):
            continue
        data = line[6:].strip()
        if not data or data == b"[DONE]":
            continue
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            continue
        response_id = response_id or str(payload.get("id") or "")
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = str(choice.get("finish_reason") or "")
            for obj in (choice.get("delta"), choice.get("message")):
                if not isinstance(obj, dict):
                    continue
                for key in ("reasoning_content", "content"):
                    value = obj.get(key)
                    if isinstance(value, str):
                        text_parts.append(value)
    return "".join(text_parts), response_id, finish_reason, has_done


def extract_chat_completion(raw: bytes) -> tuple[str, str, str, bool]:
    text = raw.decode("utf-8", errors="ignore")
    try:
        payload = json.loads(text or "{}")
    except Exception:
        return text, "", "", bool(text.strip())
    response_id = str(payload.get("id") or "") if isinstance(payload, dict) else ""
    finish_reason = ""
    text_parts: list[str] = []
    if isinstance(payload, dict):
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = str(choice.get("finish_reason") or "")
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            for key in ("reasoning_content", "content"):
                value = message.get(key)
                if isinstance(value, str):
                    text_parts.append(value)
    return "".join(text_parts) or text, response_id, finish_reason, True


def notable_snippets(text: str) -> list[str]:
    marks = ("/agent 进度", "100%", "调用", "候选池", "category", "coverage", "覆盖", "结论", "建议", "失败")
    snippets: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and any(mark in stripped for mark in marks):
            snippets.append(stripped[:160])
        if len(snippets) >= 10:
            break
    return snippets


def quality(case: dict, text: str, http_status: int | None, curl_returncode: int, error_text: str, completed: bool) -> tuple[bool, str]:
    if http_status != 200 or curl_returncode != 0:
        return False, "未通过：HTTP 或 curl 异常。"
    if not completed:
        return False, "未通过：响应未完整结束。"
    if ERROR_RE.search(text or "") or ERROR_RE.search(error_text or ""):
        return False, "未通过：正文或 stderr 包含错误词。"
    min_chars = int(case.get("min_chars") or 120)
    if len(text or "") < min_chars:
        return False, "未通过：回复过短，可能没有充分执行。"
    missing = [term for term in case.get("required_terms") or [] if str(term).lower() not in text.lower()]
    if missing:
        return False, "未通过：缺少关键结果 " + ", ".join(missing)
    any_terms = [str(term) for term in case.get("required_any_terms") or [] if str(term).strip()]
    if any_terms and not any(term.lower() in text.lower() for term in any_terms):
        return False, "未通过：未命中任一关键结果 " + ", ".join(any_terms)
    forbidden = [term for term in case.get("forbidden_terms") or [] if str(term).lower() in text.lower()]
    if forbidden:
        return False, "未通过：命中禁用词 " + ", ".join(str(term) for term in forbidden)
    max_term_counts = case.get("max_term_counts") if isinstance(case.get("max_term_counts"), dict) else {}
    for term, max_count in max_term_counts.items():
        term_text = str(term or "")
        if not term_text:
            continue
        observed_count = text.count(term_text)
        if observed_count > int(max_count):
            return False, "未通过：%s 出现 %d 次，超过上限 %s" % (term_text, observed_count, max_count)
    entry_label = "流式" if case.get("stream", True) else "非流式"
    return True, "通过：真实 3002 %s入口返回明确业务结果。" % entry_label


def load_cases(args: argparse.Namespace) -> list[dict]:
    if not args.case_file:
        return DEFAULT_CASES
    payload = json.loads(Path(args.case_file).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("case file must contain a JSON list")
    cases: list[dict] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or item.get("content") or "").strip()
        if not prompt:
            continue
        title = item.get("title") or "case_%02d" % index
        if isinstance(title, list):
            title = " / ".join(str(part) for part in title)
        cases.append(
            {
                "case_id": str(item.get("case_id") or re.sub(r"[^A-Za-z0-9]+", "_", str(title))[:48] or "case_%02d" % index),
                "title": str(title),
                "prompt": prompt,
                "stream": bool(item.get("stream", True)),
                "min_chars": int(item.get("min_chars") or 120),
                "required_terms": item.get("required_terms") or [],
                "required_any_terms": item.get("required_any_terms") or [],
                "forbidden_terms": item.get("forbidden_terms") or [],
                "max_term_counts": item.get("max_term_counts") or {},
            }
        )
    return cases


def login(base_url: str, email: str, password: str) -> str:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(
        base_url + "/api/v1/auths/signin",
        method="POST",
        data=json.dumps({"email": email, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with opener.open(request, timeout=45) as response:
        return find_token(json.loads(response.read().decode("utf-8", errors="ignore") or "{}"))


def run_case(base_url: str, model: str, token: str, case: dict, out_dir: Path, curl_max_time: str) -> dict:
    case_id = str(case["case_id"])
    stream = bool(case.get("stream", True))
    raw_path = out_dir / (case_id + ".sse")
    text_path = out_dir / (case_id + ".md")
    summary_path = out_dir / (case_id + ".summary.json")
    payload_path = out_dir / (case_id + ".payload.json")
    payload_path.write_text(
        json.dumps({"model": model, "stream": stream, "messages": [{"role": "user", "content": case["prompt"]}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    started = time.time()
    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "-N",
            "--max-time",
            curl_max_time,
            "--connect-timeout",
            "20",
            "-w",
            "\n__HTTP_CODE__:%{http_code}\n",
            "-H",
            "Content-Type: application/json",
            "-H",
            "Accept: " + ("text/event-stream" if stream else "application/json"),
            "-H",
            "Authorization: Bearer " + token,
            "--data-binary",
            "@" + str(payload_path),
            base_url + "/api/chat/completions",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    payload_path.unlink(missing_ok=True)
    raw = proc.stdout
    http_status = None
    match = re.search(rb"__HTTP_CODE__:(\d+)", raw)
    if match:
        http_status = int(match.group(1))
        raw = re.sub(rb"\n__HTTP_CODE__:\d+\n?$", b"", raw)
    raw_path.write_bytes(raw)
    if stream:
        text, response_id, finish_reason, completed = extract_sse(raw)
    else:
        text, response_id, finish_reason, completed = extract_chat_completion(raw)
    text_path.write_text(text, encoding="utf-8")
    curl_stderr = proc.stderr.decode("utf-8", errors="ignore")[:800]
    passed, quality_note = quality(case, text, http_status, proc.returncode, curl_stderr, completed)
    summary = {
        "case_id": case_id,
        "title": case["title"],
        "prompt": case["prompt"],
        "stream": stream,
        "http_status": http_status,
        "curl_returncode": proc.returncode,
        "curl_stderr": curl_stderr,
        "response_id": response_id,
        "finish_reason": finish_reason,
        "has_done": completed if stream else False,
        "completed": completed,
        "elapsed_seconds": round(time.time() - started, 1),
        "content_chars": len(text),
        "has_error_word": bool(ERROR_RE.search(text or "") or ERROR_RE.search(curl_stderr or "")),
        "candidate_pool_ids": sorted(set(CANDIDATE_POOL_RE.findall(text))),
        "notable_snippets": notable_snippets(text),
        "quality_note": quality_note,
        "passed": passed,
        "raw_path": str(raw_path),
        "text_path": str(text_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def write_markdown_summary(path: Path, run_summary: dict) -> None:
    lines = [
        "# XiaMimate Agent Harness Eval",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        "| passed | %s |" % run_summary.get("passed"),
        "| passed_count | %s |" % run_summary.get("passed_count"),
        "| failed_count | %s |" % run_summary.get("failed_count"),
        "| elapsed_seconds | %s |" % run_summary.get("elapsed_seconds"),
        "| base_url | %s |" % run_summary.get("base_url"),
        "| model | %s |" % run_summary.get("model"),
        "",
        "## Cases",
        "",
        "| case_id | stream | passed | http | done | seconds | note |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in run_summary.get("cases") or []:
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s |"
            % (
                str(item.get("case_id") or "").replace("|", "\\|"),
                "1" if item.get("stream") else "0",
                "1" if item.get("passed") else "0",
                item.get("http_status"),
                "1" if item.get("completed") else "0",
                item.get("elapsed_seconds"),
                str(item.get("quality_note") or "").replace("|", "\\|"),
            )
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run XiaMimate agent harness smoke eval through the real OpenWebUI chat API.")
    parser.add_argument("--base-url", default=os.environ.get("XM_BASE_URL", "http://127.0.0.1:3002"))
    parser.add_argument("--model", default=os.environ.get("XM_MODEL", "xiamimate.agent"))
    parser.add_argument("--email", default=os.environ.get("XM_EMAIL", ""))
    parser.add_argument("--password", default=os.environ.get("XM_PASSWORD", ""))
    parser.add_argument("--out-dir", default=os.environ.get("XM_OUT_DIR", "/tmp/xm_agent_harness_eval"))
    parser.add_argument("--curl-max-time", default=os.environ.get("XM_CURL_MAX_TIME", "900"))
    parser.add_argument("--case-file", default=os.environ.get("XM_CASE_FILE", ""))
    parser.add_argument("--fail-fast", action="store_true", default=os.environ.get("XM_FAIL_FAST", "").lower() in {"1", "true", "yes"})
    parser.add_argument("--min-passed", type=int, default=int(os.environ.get("XM_MIN_PASSED", "0") or "0"))
    parser.add_argument("--summary-md", default=os.environ.get("XM_SUMMARY_MD", ""))
    args = parser.parse_args()

    if not args.email or not args.password:
        print("XM_EMAIL and XM_PASSWORD are required, or pass --email/--password.", file=sys.stderr)
        return 2

    base_url = str(args.base_url).rstrip("/")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = load_cases(args)
    run_started = time.time()
    token = login(base_url, args.email, args.password)
    if not token:
        print("Login succeeded but no token was found.", file=sys.stderr)
        return 1

    summaries = []
    for case in cases:
        print(json.dumps({"case_id": case["case_id"], "title": case["title"], "status": "start"}, ensure_ascii=False), flush=True)
        summary = run_case(base_url, args.model, token, case, out_dir, args.curl_max_time)
        summaries.append(summary)
        print(
            json.dumps(
                {
                    "case_id": summary["case_id"],
                    "passed": summary["passed"],
                    "http_status": summary["http_status"],
                    "has_done": summary["has_done"],
                    "elapsed_seconds": summary["elapsed_seconds"],
                    "quality_note": summary["quality_note"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if args.fail_fast and not summary["passed"]:
            break

    index_path = out_dir / "summary.json"
    passed_count = sum(1 for item in summaries if item["passed"])
    failed_count = len(summaries) - passed_count
    gate_passed = all(item["passed"] for item in summaries) and (not args.min_passed or passed_count >= args.min_passed)
    run_summary = {
        "passed": gate_passed,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "case_count": len(summaries),
        "planned_case_count": len(cases),
        "elapsed_seconds": round(time.time() - run_started, 1),
        "base_url": base_url,
        "model": args.model,
        "cases": summaries,
    }
    index_path.write_text(json.dumps(run_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown_summary(Path(args.summary_md) if args.summary_md else out_dir / "summary.md", run_summary)
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
