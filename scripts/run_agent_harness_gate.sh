#!/usr/bin/env zsh
set -euo pipefail

repo_dir="${0:A:h:h}"
out_dir="${XM_OUT_DIR:-/tmp/xm_agent_harness_gate_$(date +%Y%m%d_%H%M%S)}"
case_file="${XM_CASE_FILE:-$repo_dir/scripts/agent_harness_p4_cases.json}"

if [[ -z "${XM_EMAIL:-}" || -z "${XM_PASSWORD:-}" ]]; then
  print -u2 "XM_EMAIL and XM_PASSWORD are required for authenticated 3002 eval."
  exit 2
fi

cd "$repo_dir"
cmd=(python3 scripts/run_agent_harness_eval.py \
  --base-url "${XM_BASE_URL:-http://127.0.0.1:3002}" \
  --model "${XM_MODEL:-xiamimate.agent}" \
  --email "$XM_EMAIL" \
  --password "$XM_PASSWORD" \
  --out-dir "$out_dir" \
  --curl-max-time "${XM_CURL_MAX_TIME:-900}" \
  --min-passed "${XM_MIN_PASSED:-2}")
if [[ -n "$case_file" && -f "$case_file" ]]; then
  cmd+=(--case-file "$case_file")
fi
if [[ -n "${XM_FAIL_FAST:-}" ]]; then
  cmd+=(--fail-fast)
fi
"${cmd[@]}"

print "Agent harness gate summary: $out_dir/summary.md"