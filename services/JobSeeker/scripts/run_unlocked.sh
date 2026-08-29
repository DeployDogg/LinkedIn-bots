#!/usr/bin/env bash
set -uo pipefail
SERVICE_NAME="${SERVICE_NAME:-JobSeeker}"
WORKDIR="/Users/deploydog-ai/LinkedIn/shared/legacy_state"
STATE_DIR="$WORKDIR/jobseeker"
LOG_DIR="/shared/logs/JobSeeker"
mkdir -p "$STATE_DIR" "$LOG_DIR"
cd /app/scripts || exit 1
source /app/scripts/logging.sh
linkedin_log_init

if [ "${SAFE_MODE:-0}" = "1" ]; then
  STAMP="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo "[$STAMP] JobSeeker SAFE_MODE: no LinkedIn actions" | tee -a "$LOG_DIR/run.log"
  python3 -m py_compile linkedin_central_browser.py linkedin_extractor.py linkedin_worker.py | tee -a "$LOG_DIR/run.log"
  exit ${PIPESTATUS[0]}
fi

DEFAULT_SEARCH_URL="https://www.linkedin.com/jobs/search-results/?keywords=Platform%20Engineer&f_AL=true&f_WT=2&f_TPR=r86400&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON"
SRE_URL="${LINKEDIN_JOBSEEKER_SRE_URL:-https://www.linkedin.com/jobs/search-results/?keywords=Site%20Reliability%20Engineer&f_AL=true&f_WT=2&f_TPR=r86400&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON}"
DEVOPS_URL="${LINKEDIN_JOBSEEKER_DEVOPS_URL:-https://www.linkedin.com/jobs/search-results/?keywords=DevOps%20Engineer&f_AL=true&f_WT=2&f_TPR=r86400&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON}"
SEARCH_URL="${LINKEDIN_SEARCH_URL:-$DEFAULT_SEARCH_URL}"
SOURCE_MD="${LINKEDIN_SOURCE_MD:-$STATE_DIR/checked_li_jobs_platform_full.md}"
SOURCE_JSON="${LINKEDIN_SOURCE_JSON:-$STATE_DIR/checked_li_jobs_platform_full.json}"
PROGRESS_JSON="${LINKEDIN_PROGRESS_JSON:-$STATE_DIR/li_apply_platform_full_progress.json}"
PROGRESS_MD="${LINKEDIN_PROGRESS_MD:-$STATE_DIR/li_apply_platform_full_progress.md}"
RUN_LOG="$LOG_DIR/last_run.log"
MIN_EXTRACTED_JOBS="${MIN_EXTRACTED_JOBS:-5}"

effective_total_limit() {
  if [ "${LINKEDIN_JOBSEEKER_DAILY_LIMIT:-0}" != "0" ]; then
    echo "${LINKEDIN_JOBSEEKER_DAILY_LIMIT}"
  else
    echo "${LINKEDIN_WORKER_MAX_JOBS:-0}"
  fi
}

combine_queue_sources() {
  local first_label="$1" first_json="$2" first_cap="$3" second_label="$4" second_json="$5" second_cap="$6" output_json="$7" output_md="$8"
  python3 - "$first_label" "$first_json" "$first_cap" "$second_label" "$second_json" "$second_cap" "$output_json" "$output_md" <<'PY'
import json, sys
first_label, first_json, first_cap, second_label, second_json, second_cap, output_json, output_md = sys.argv[1:]
seen=set(); combined=[]
for label, path, cap in [(first_label, first_json, int(first_cap)), (second_label, second_json, int(second_cap))]:
    try:
        jobs=json.load(open(path, encoding='utf-8'))
    except FileNotFoundError:
        jobs=[]
    added=0
    for job in jobs:
        url=job.get('url')
        if not url or url in seen:
            continue
        item=dict(job); item['queue_label']=label
        combined.append(item); seen.add(url); added += 1
        if added >= cap:
            break
with open(output_json, 'w', encoding='utf-8') as fh:
    json.dump(combined, fh, ensure_ascii=False, indent=2)
with open(output_md, 'w', encoding='utf-8') as fh:
    fh.write('# LinkedIn extracted jobs\n\n')
    for job in combined:
        title=job.get('title') or 'LinkedIn job'
        company=job.get('company') or ''
        location=job.get('location') or job.get('geo') or ''
        url=job.get('url') or ''
        label=job.get('queue_label') or 'job'
        fh.write(f'## Вакансия {title}\n')
        fh.write(f'- [{label}] {company} — {location} - {url}\n\n')
print(len(combined))
PY
}

run_worker_for_source() {
  local max_jobs="$1"
  WORKER_ARGS=()
  if [ "${LINKEDIN_STOP_ON_BLOCKER:-1}" = "1" ]; then WORKER_ARGS+=(--stop-on-blocker); fi
  if [ "$max_jobs" != "0" ]; then WORKER_ARGS+=(--max-jobs "$max_jobs"); fi
  if [ "${LINKEDIN_WORKER_RETRY_BLOCKED:-0}" = "1" ]; then WORKER_ARGS+=(--retry-blocked); fi
  LINKEDIN_JOBS_SOURCE="$SOURCE_MD" LINKEDIN_PROGRESS_JSON="$PROGRESS_JSON" LINKEDIN_PROGRESS_MD="$PROGRESS_MD" python3 linkedin_worker.py "${WORKER_ARGS[@]}"
}

{
  echo "--- JobSeeker run $(date "+%Y-%m-%dT%H:%M:%S%z") ---"
  if [ "${LINKEDIN_JOBSEEKER_QUEUE_MODE:-0}" = "1" ]; then
    echo "[1/1] quota runner: target is submitted Easy Apply applications, not processed jobs"
    python3 jobseeker_quota_runner.py
    code=$?
    echo "Quota runner exit code: $code"
    exit "$code"
  fi

  echo "[1/3] extract"
  python3 linkedin_extractor.py "$SEARCH_URL" "$SOURCE_MD" --json "$SOURCE_JSON" --max-start "${LINKEDIN_EXTRACTOR_MAX_START:-1000}" --delay "${LINKEDIN_EXTRACTOR_DELAY:-0.25}" || exit $?
  extracted_count=$(python3 - <<PY
import json
print(len(json.load(open('$SOURCE_JSON', encoding='utf-8'))))
PY
)
  echo "Extracted jobs: $extracted_count"
  if [ "$extracted_count" -lt "$MIN_EXTRACTED_JOBS" ]; then
    echo "Too few jobs; refusing worker."
    exit 70
  fi
  echo "[2/3] worker"
  run_worker_for_source "${LINKEDIN_WORKER_MAX_JOBS:-0}"
  code=$?
  echo "Worker exit code: $code"
  exit "$code"
} 2>&1 | tee -a "$RUN_LOG"
