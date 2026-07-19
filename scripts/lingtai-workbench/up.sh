#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_DIR="${ROOT_DIR}/scripts/lingtai-workbench"
STATE_DIR="${ROOT_DIR}/target/lingtai-workbench"
CANDIDATE_NOKV_BIN="${NOKV_BIN:-}"
NOKV_BIN=""
SERVER_BIND="${LINGTAI_WORKBENCH_SERVER_BIND:-127.0.0.1:7799}"
SERVER_BIND_IS_SET="${LINGTAI_WORKBENCH_SERVER_BIND+x}"
S3_ENDPOINT="${LINGTAI_WORKBENCH_S3_ENDPOINT:-http://127.0.0.1:9000}"
S3_ENDPOINT_IS_SET="${LINGTAI_WORKBENCH_S3_ENDPOINT+x}"
S3_BUCKET="${LINGTAI_WORKBENCH_S3_BUCKET:-nokv-lingtai-workbench}"
S3_BUCKET_IS_SET="${LINGTAI_WORKBENCH_S3_BUCKET+x}"
OBJECT_BACKEND="${LINGTAI_WORKBENCH_OBJECT_BACKEND:-rustfs}"
OBJECT_BACKEND_IS_SET="${LINGTAI_WORKBENCH_OBJECT_BACKEND+x}"
DEFAULT_WORKBENCH_ROOT='/agents/{agent_id}/wb'
WORKBENCH_ROOT="${LINGTAI_WORKBENCH_ROOT:-${DEFAULT_WORKBENCH_ROOT}}"
WORKBENCH_ROOT_IS_SET="${LINGTAI_WORKBENCH_ROOT+x}"
MCP_PROFILE="${LINGTAI_WORKBENCH_MCP_PROFILE-workbench}"
MCP_PROFILE_IS_SET="${LINGTAI_WORKBENCH_MCP_PROFILE+x}"
WORKSPACE_ID="${LINGTAI_WORKBENCH_WORKSPACE_ID-}"
WORKSPACE_ACTOR_ID="${LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID-}"
WORKSPACE_GRANT="${LINGTAI_WORKBENCH_WORKSPACE_GRANT-}"
WORKSPACE_ID_IS_SET="${LINGTAI_WORKBENCH_WORKSPACE_ID+x}"
WORKSPACE_ACTOR_ID_IS_SET="${LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID+x}"
WORKSPACE_GRANT_IS_SET="${LINGTAI_WORKBENCH_WORKSPACE_GRANT+x}"
WORKSPACE_DEV_MEMBERSHIP_IS_SET="${LINGTAI_WORKBENCH_WORKSPACE_DEV_MEMBERSHIP+x}"
META_DIR="${LINGTAI_WORKBENCH_META_DIR:-${STATE_DIR}/meta}"
SERVER_LOG="${LINGTAI_WORKBENCH_SERVER_LOG:-${STATE_DIR}/nokv-server.log}"
SERVER_PID="${LINGTAI_WORKBENCH_SERVER_PID:-${STATE_DIR}/nokv-server.pid}"
SERVER_STATE="${LINGTAI_WORKBENCH_SERVER_STATE:-${STATE_DIR}/nokv-server.json}"
TUI_PYTHON="${LINGTAI_TUI_PYTHON:-${HOME}/.lingtai-tui/runtime/venv/bin/python}"
RUNTIME_IDENTITY_ARGS=()
PROFILE_ARGS=()
SERVER_ARGV=()
PINNED_AGENT=""
PINNED_AGENT_IDENTITY=""
PINNED_AGENT_STATE_SHA256=""
WORKSPACE_ID_SHA256=""
WORKSPACE_ACTOR_ID_SHA256=""
WORKSPACE_GRANT_SHA256=""
UP_LOCK_DIR="${STATE_DIR}/up.lock"

log() {
  printf '[lingtai-workbench] %s\n' "$*"
}

die() {
  printf '[lingtai-workbench] error: %s\n' "$*" >&2
  exit 1
}

validate_profile_selection() {
  if [[ -n "${WORKSPACE_DEV_MEMBERSHIP_IS_SET}" ]]; then
    die "development workspace membership is unsupported; unset LINGTAI_WORKBENCH_WORKSPACE_DEV_MEMBERSHIP"
  fi

  case "${MCP_PROFILE}" in
    workbench)
      if [[ -n "${WORKSPACE_ID_IS_SET}" || -n "${WORKSPACE_ACTOR_ID_IS_SET}" || -n "${WORKSPACE_GRANT_IS_SET}" ]]; then
        die "the workbench MCP profile rejects LINGTAI_WORKBENCH_WORKSPACE_ID, LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID, and LINGTAI_WORKBENCH_WORKSPACE_GRANT; unset all three"
      fi
      PROFILE_ARGS=()
      if [[ -n "${MCP_PROFILE_IS_SET}" ]]; then
        PROFILE_ARGS=(--profile workbench)
      fi
      ;;
    lingtai)
      if [[ -z "${WORKSPACE_ID_IS_SET}" || -z "${WORKSPACE_ID}" || -z "${WORKSPACE_ACTOR_ID_IS_SET}" || -z "${WORKSPACE_ACTOR_ID}" || -z "${WORKSPACE_GRANT_IS_SET}" || -z "${WORKSPACE_GRANT}" ]]; then
        die "the lingtai MCP profile requires non-empty LINGTAI_WORKBENCH_WORKSPACE_ID, LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID, and LINGTAI_WORKBENCH_WORKSPACE_GRANT"
      fi
      PROFILE_ARGS=(
        --profile lingtai
        --workspace-id "${WORKSPACE_ID}"
        --workspace-actor-id "${WORKSPACE_ACTOR_ID}"
        --workspace-grant "${WORKSPACE_GRANT}"
      )
      ;;
    *)
      die "LINGTAI_WORKBENCH_MCP_PROFILE must be exactly workbench or lingtai"
      ;;
  esac
}

stable_sha256() {
  python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

canonical_workspace_grant_sha256() {
  LINGTAI_FINGERPRINT_WORKSPACE_ID="${WORKSPACE_ID}" \
    LINGTAI_FINGERPRINT_WORKSPACE_ACTOR_ID="${WORKSPACE_ACTOR_ID}" \
    LINGTAI_FINGERPRINT_WORKSPACE_GRANT="${WORKSPACE_GRANT}" \
    python3 -c 'import os, sys
sys.path.insert(0, sys.argv[1])
from workspace_grant import parse_workspace_grant, workspace_grant_sha256
grant = parse_workspace_grant(
    os.environ["LINGTAI_FINGERPRINT_WORKSPACE_GRANT"],
    workspace_id=os.environ["LINGTAI_FINGERPRINT_WORKSPACE_ID"],
    actor_id=os.environ["LINGTAI_FINGERPRINT_WORKSPACE_ACTOR_ID"],
)
print(workspace_grant_sha256(grant))' "${SCRIPT_DIR}"
}

cache_profile_fingerprints() {
  if [[ "${MCP_PROFILE}" != "lingtai" ]]; then
    return
  fi
  WORKSPACE_ID_SHA256="$(printf '%s' "${WORKSPACE_ID}" | stable_sha256)"
  WORKSPACE_ACTOR_ID_SHA256="$(printf '%s' "${WORKSPACE_ACTOR_ID}" | stable_sha256)"
  WORKSPACE_GRANT_SHA256="$(canonical_workspace_grant_sha256)"
}

print_locked_profile() {
  log "locked MCP profile: ${MCP_PROFILE}"
  if [[ "${MCP_PROFILE}" == "lingtai" ]]; then
    log "workspace id SHA-256: ${WORKSPACE_ID_SHA256}"
    log "workspace actor SHA-256: ${WORKSPACE_ACTOR_ID_SHA256}"
    log "workspace grant SHA-256: ${WORKSPACE_GRANT_SHA256}"
  fi
}

resolve_project() {
  if [[ -n "${LINGTAI_WORKBENCH_PROJECT:-}" ]]; then
    printf '%s\n' "${LINGTAI_WORKBENCH_PROJECT}"
    return
  fi
  if [[ -d ".lingtai" ]]; then
    pwd
    return
  fi
  if [[ -d "${HOME}/lingtai-demo/.lingtai" ]]; then
    printf '%s\n' "${HOME}/lingtai-demo"
    return
  fi
  die "cannot find a LingTai project; set LINGTAI_WORKBENCH_PROJECT=/path/to/project"
}

resolve_agent_once() {
  local project="$1"
  local selected="" pinned=""
  if ! pinned="$({
    LINGTAI_PIN_REQUESTED_AGENT="${LINGTAI_WORKBENCH_AGENT-}" \
      python3 -c 'import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from sync_workbench_mcp import capture_agent_identity
project = Path(sys.argv[2]).expanduser().resolve()
requested = os.environ.get("LINGTAI_PIN_REQUESTED_AGENT") or None
name, token = capture_agent_identity(project, requested)
if "\n" in name or "\r" in name:
    raise ValueError("LingTai agent directory name must not contain a newline")
print(name)
print(token, end="")' "${SCRIPT_DIR}" "${project}"
  })"; then
    die "cannot resolve one LingTai Agent before preflight"
  fi
  [[ "${pinned}" == *$'\n'* ]] || die "resolved LingTai Agent identity is malformed"
  selected="${pinned%%$'\n'*}"
  [[ -n "${selected}" ]] || die "resolved LingTai Agent name is empty"
  PINNED_AGENT_IDENTITY="${pinned#*$'\n'}"
  [[ -n "${PINNED_AGENT_IDENTITY}" ]] || die "resolved LingTai Agent identity is empty"
  PINNED_AGENT="${selected}"
  log "pinned Agent: ${PINNED_AGENT}"
}

require_pinned_agent() {
  [[ -n "${PINNED_AGENT}" ]] || die "LingTai Agent selection was not pinned"
  [[ -n "${PINNED_AGENT_IDENTITY}" ]] || die "LingTai Agent identity was not pinned"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

release_up_lock() {
  if [[ -f "${UP_LOCK_DIR}/pid" ]] && [[ "$(cat "${UP_LOCK_DIR}/pid")" == "$$" ]]; then
    rm -f "${UP_LOCK_DIR}/pid"
    rmdir "${UP_LOCK_DIR}" 2>/dev/null || true
  fi
}

abort_up() {
  local status="$1"
  trap - INT TERM
  exit "${status}"
}

acquire_up_lock() {
  mkdir -p "${STATE_DIR}"
  if ! mkdir "${UP_LOCK_DIR}" 2>/dev/null; then
    local owner=""
    if [[ -f "${UP_LOCK_DIR}/pid" ]]; then
      owner="$(cat "${UP_LOCK_DIR}/pid")"
    fi
    if [[ "${owner}" =~ ^[0-9]+$ ]] && kill -0 "${owner}" >/dev/null 2>&1; then
      die "another lingtai-workbench up.sh is active (pid ${owner})"
    fi
    rm -f "${UP_LOCK_DIR}/pid"
    rmdir "${UP_LOCK_DIR}" 2>/dev/null ||
      die "stale update lock requires manual inspection: ${UP_LOCK_DIR}"
    mkdir "${UP_LOCK_DIR}"
  fi
  printf '%s\n' "$$" >"${UP_LOCK_DIR}/pid"
  trap release_up_lock EXIT
  trap 'abort_up 130' INT
  trap 'abort_up 143' TERM
}

prepare_runtime() {
  local project="$1"
  local candidate distribution
  local stage_args=(--stage-only --project "${project}")

  if [[ -z "${CANDIDATE_NOKV_BIN}" ]]; then
    require_cmd cargo
    require_cmd git
    distribution="source"
    stage_args+=(--build-source "${ROOT_DIR}" --distribution source)
    if [[ "${LINGTAI_WORKBENCH_ALLOW_DIRTY:-0}" == "1" ]]; then
      stage_args+=(--allow-dirty)
    fi
  else
    candidate="${CANDIDATE_NOKV_BIN}"
    [[ -n "${NOKV_BUILD_INFO:-}" ]] ||
      die "external NOKV_BIN requires its artifact-bound NOKV_BUILD_INFO"
    stage_args+=(--nokv-bin "${candidate}" --build-info "${NOKV_BUILD_INFO}")
    if [[ -n "${NOKV_REVISION:-}" ]]; then
      stage_args+=(--revision "${NOKV_REVISION}")
    fi
    if [[ -n "${NOKV_DISTRIBUTION:-}" ]]; then
      distribution="${NOKV_DISTRIBUTION}"
    elif [[ "${candidate}" == *"/Cellar/"* || "${candidate}" == *"/Homebrew/"* ]]; then
      distribution="brew"
    else
      distribution="release"
    fi
    stage_args+=(--distribution "${distribution}")
    if [[ "${LINGTAI_WORKBENCH_ALLOW_DIRTY:-0}" == "1" ]]; then
      stage_args+=(--allow-dirty)
    fi
  fi

  if [[ -n "${NOKV_EXPECTED_SHA256:-}" ]]; then
    stage_args+=(--expected-sha256 "${NOKV_EXPECTED_SHA256}")
  fi
  NOKV_BIN="$(python3 "${SCRIPT_DIR}/sync_workbench_mcp.py" "${stage_args[@]}")"
  [[ -x "${NOKV_BIN}" ]] || die "staged NoKV runtime is not executable: ${NOKV_BIN}"
  RUNTIME_IDENTITY_ARGS=(
    --build-info "$(dirname "${NOKV_BIN}")/build-info.json"
    --distribution "${distribution}"
  )
  if [[ "${LINGTAI_WORKBENCH_ALLOW_DIRTY:-0}" == "1" ]]; then
    RUNTIME_IDENTITY_ARGS+=(--allow-dirty)
  fi
  SERVER_ARGV=(
    "${NOKV_BIN}"
    --server-bind "${SERVER_BIND}"
    --object-backend "${OBJECT_BACKEND}"
    --s3-endpoint "${S3_ENDPOINT}"
    --s3-bucket "${S3_BUCKET}"
    --meta "${META_DIR}"
    serve
  )
  log "immutable NoKV runtime: ${NOKV_BIN}"
}

probe_candidate_contract() {
  local project="$1"
  require_pinned_agent
  local args=(
    --probe-only
    --project "${project}"
    --nokv-bin "${NOKV_BIN}"
    "${RUNTIME_IDENTITY_ARGS[@]}"
    --server-bind "${SERVER_BIND}"
    --object-backend "${OBJECT_BACKEND}"
    --s3-endpoint "${S3_ENDPOINT}"
    --s3-bucket "${S3_BUCKET}"
    --workbench-root "${WORKBENCH_ROOT}"
    "${PROFILE_ARGS[@]}"
    --agent "${PINNED_AGENT}"
    --orchestration-agent-identity "${PINNED_AGENT_IDENTITY}"
  )
  if [[ -n "${LINGTAI_WORKBENCH_ACCEPT_CONTRACT_SHA256:-}" ]]; then
    args+=(
      --accept-contract-sha256
      "${LINGTAI_WORKBENCH_ACCEPT_CONTRACT_SHA256}"
    )
  fi
  if [[ -n "${NOKV_EXPECTED_SHA256:-}" ]]; then
    args+=(--expected-sha256 "${NOKV_EXPECTED_SHA256}")
  fi
  python3 "${SCRIPT_DIR}/sync_workbench_mcp.py" "${args[@]}"
}

preflight_agent() {
  local project="$1"
  local output=""
  local line=""
  local digest_count=0
  require_pinned_agent
  local args=(
    --preflight-only
    --project "${project}"
    "${PROFILE_ARGS[@]}"
    --agent "${PINNED_AGENT}"
    --orchestration-agent-identity "${PINNED_AGENT_IDENTITY}"
  )
  if [[ -n "${SERVER_BIND_IS_SET}" && -n "${LINGTAI_WORKBENCH_SERVER_BIND-}" ]]; then
    args+=(--server-bind "${SERVER_BIND}")
  fi
  if [[ -n "${OBJECT_BACKEND_IS_SET}" && -n "${LINGTAI_WORKBENCH_OBJECT_BACKEND-}" ]]; then
    args+=(--object-backend "${OBJECT_BACKEND}")
  fi
  if [[ -n "${S3_ENDPOINT_IS_SET}" && -n "${LINGTAI_WORKBENCH_S3_ENDPOINT-}" ]]; then
    args+=(--s3-endpoint "${S3_ENDPOINT}")
  fi
  if [[ -n "${S3_BUCKET_IS_SET}" && -n "${LINGTAI_WORKBENCH_S3_BUCKET-}" ]]; then
    args+=(--s3-bucket "${S3_BUCKET}")
  fi
  if [[ -n "${WORKBENCH_ROOT_IS_SET}" && -n "${LINGTAI_WORKBENCH_ROOT-}" ]]; then
    args+=(--workbench-root "${WORKBENCH_ROOT}")
  fi
  if [[ -n "${LINGTAI_WORKBENCH_ACCEPT_CONTRACT_SHA256:-}" ]]; then
    args+=(
      --accept-contract-sha256
      "${LINGTAI_WORKBENCH_ACCEPT_CONTRACT_SHA256}"
    )
  fi
  if ! output="$(python3 "${SCRIPT_DIR}/sync_workbench_mcp.py" "${args[@]}")"; then
    die "Agent preflight failed"
  fi
  printf '%s\n' "${output}"
  PINNED_AGENT_STATE_SHA256=""
  while IFS= read -r line; do
    if [[ "${line}" == agent_state_sha256:\ * ]]; then
      PINNED_AGENT_STATE_SHA256="${line#agent_state_sha256: }"
      digest_count=$((digest_count + 1))
    fi
  done <<<"${output}"
  if [[ "${digest_count}" -ne 1 || ! "${PINNED_AGENT_STATE_SHA256}" =~ ^[0-9a-f]{64}$ ]]; then
    die "Agent preflight did not return one valid state precondition"
  fi
}

sync_agent() {
  local project="$1"
  require_pinned_agent
  [[ "${PINNED_AGENT_STATE_SHA256}" =~ ^[0-9a-f]{64}$ ]] ||
    die "LingTai Agent state was not pinned by preflight"
  local args=(
    --project "${project}"
    --nokv-bin "${NOKV_BIN}"
    "${RUNTIME_IDENTITY_ARGS[@]}"
    --server-bind "${SERVER_BIND}"
    --object-backend "${OBJECT_BACKEND}"
    --s3-endpoint "${S3_ENDPOINT}"
    --s3-bucket "${S3_BUCKET}"
    --workbench-root "${WORKBENCH_ROOT}"
    "${PROFILE_ARGS[@]}"
    --agent "${PINNED_AGENT}"
    --orchestration-agent-identity "${PINNED_AGENT_IDENTITY}"
    --orchestration-agent-state-sha256 "${PINNED_AGENT_STATE_SHA256}"
  )
  if [[ -n "${LINGTAI_WORKBENCH_ACCEPT_CONTRACT_SHA256:-}" ]]; then
    args+=(
      --accept-contract-sha256
      "${LINGTAI_WORKBENCH_ACCEPT_CONTRACT_SHA256}"
    )
  fi
  if [[ -n "${NOKV_EXPECTED_SHA256:-}" ]]; then
    args+=(--expected-sha256 "${NOKV_EXPECTED_SHA256}")
  fi
  python3 "${SCRIPT_DIR}/sync_workbench_mcp.py" "${args[@]}"
}

check_agent() {
  local project="$1"
  require_pinned_agent
  local args=(
    --project "${project}"
    --check
    --agent "${PINNED_AGENT}"
    --orchestration-agent-identity "${PINNED_AGENT_IDENTITY}"
  )
  python3 "${SCRIPT_DIR}/sync_workbench_mcp.py" "${args[@]}"
}

check_runtime_skill() {
  [[ -x "${TUI_PYTHON}" ]] || die "LingTai TUI runtime python not found: ${TUI_PYTHON}"
  "${TUI_PYTHON}" - <<'PY' || die "LingTai TUI runtime does not expose intrinsic skill nokv-workbench; install a workbench-enabled LingTai runtime first"
from pathlib import Path
import lingtai.intrinsic_skills as skills

root = Path(skills.__file__).parent
if not (root / "nokv-workbench" / "SKILL.md").exists():
    raise SystemExit(1)
PY
  log "LingTai runtime skill ready"
}

validate_guarded_credentials() {
  local access_key="${LINGTAI_WORKBENCH_S3_ACCESS_KEY_ID:-rustfsadmin}"
  local secret_key="${LINGTAI_WORKBENCH_S3_SECRET_ACCESS_KEY:-rustfsadmin}"
  if [[ "${access_key}" != "rustfsadmin" || "${secret_key}" != "rustfsadmin" ]]; then
    die "up.sh supports the dedicated local RustFS credentials only; custom credential deployment is outside this guarded helper"
  fi
}

ensure_rustfs() {
  "${SCRIPT_DIR}/start_rustfs.sh"
}

nokv_ls() {
  "${NOKV_BIN}" \
    --server-bind "${SERVER_BIND}" \
    --object-backend "${OBJECT_BACKEND}" \
    --s3-endpoint "${S3_ENDPOINT}" \
    --s3-bucket "${S3_BUCKET}" \
    ls / >/dev/null
}

port_in_use() {
  local host="${SERVER_BIND%:*}"
  local port="${SERVER_BIND##*:}"
  lsof -nP -iTCP@"${host}:${port}" -sTCP:LISTEN >/dev/null 2>&1
}

verify_managed_server() {
  python3 "${SCRIPT_DIR}/managed_nokv_server.py" verify --state "${SERVER_STATE}"
}

verify_reusable_server() {
  python3 "${SCRIPT_DIR}/managed_nokv_server.py" verify \
    --state "${SERVER_STATE}" \
    --expect-binary "${NOKV_BIN}" \
    --expect-server-bind "${SERVER_BIND}" \
    --expect-meta "${META_DIR}" \
    --expect-object-backend "${OBJECT_BACKEND}" \
    --expect-s3-endpoint "${S3_ENDPOINT}" \
    --expect-s3-bucket "${S3_BUCKET}" \
    -- "${SERVER_ARGV[@]}"
}

managed_server_pid() {
  python3 "${SCRIPT_DIR}/managed_nokv_server.py" pid --state "${SERVER_STATE}"
}

stop_managed_server() {
  local pid=""
  if ! pid="$(managed_server_pid)"; then
    die "managed server state is invalid and cannot be terminated: ${SERVER_STATE}"
  fi
  log "stopping managed NoKV server pid=${pid}"
  python3 "${SCRIPT_DIR}/managed_nokv_server.py" terminate \
    --state "${SERVER_STATE}" \
    --timeout-seconds 5 >/dev/null ||
    die "cannot safely terminate the managed NoKV server recorded in ${SERVER_STATE}"
  rm -f "${SERVER_PID}" "${SERVER_STATE}"
}

cleanup_started_server() {
  local pid="$1"
  local recorded_pid=""
  if kill -0 "${pid}" >/dev/null 2>&1; then
    kill "${pid}" >/dev/null 2>&1 || true
    wait "${pid}" 2>/dev/null || true
  fi
  if [[ -f "${SERVER_PID}" ]] && [[ "$(cat "${SERVER_PID}")" == "${pid}" ]]; then
    rm -f "${SERVER_PID}"
  fi
  recorded_pid="$(managed_server_pid 2>/dev/null || true)"
  if [[ "${recorded_pid}" == "${pid}" ]]; then
    rm -f "${SERVER_STATE}"
  fi
}

record_started_server() {
  local pid="$1"
  python3 "${SCRIPT_DIR}/managed_nokv_server.py" write \
    --state "${SERVER_STATE}" \
    --pid "${pid}" \
    --binary "${NOKV_BIN}" \
    --server-bind "${SERVER_BIND}" \
    --meta "${META_DIR}" \
    --object-backend "${OBJECT_BACKEND}" \
    --s3-endpoint "${S3_ENDPOINT}" \
    --s3-bucket "${S3_BUCKET}" \
    -- "${SERVER_ARGV[@]}" >/dev/null
}

start_nokv_server() {
  mkdir -p "${STATE_DIR}" "${META_DIR}"
  log "starting NoKV server at ${SERVER_BIND}"
  python3 "${SCRIPT_DIR}/managed_nokv_server.py" launch \
    --state "${SERVER_STATE}" \
    --binary "${NOKV_BIN}" \
    --server-bind "${SERVER_BIND}" \
    --meta "${META_DIR}" \
    --object-backend "${OBJECT_BACKEND}" \
    --s3-endpoint "${S3_ENDPOINT}" \
    --s3-bucket "${S3_BUCKET}" \
    -- "${SERVER_ARGV[@]}" >"${SERVER_LOG}" 2>&1 &
  local pid="$!"
  local pid_tmp="${SERVER_PID}.tmp.$$"
  printf '%s\n' "${pid}" >"${pid_tmp}"
  mv "${pid_tmp}" "${SERVER_PID}"

  for _ in $(seq 1 60); do
    if nokv_ls; then
      if ! record_started_server "${pid}"; then
        cleanup_started_server "${pid}"
        die "NoKV server became ready but its launch identity could not be recorded"
      fi
      log "NoKV server ready pid=${pid}"
      return
    fi
    if ! kill -0 "${pid}" >/dev/null 2>&1; then
      tail -80 "${SERVER_LOG}" >&2 || true
      cleanup_started_server "${pid}"
      die "NoKV server exited before becoming ready"
    fi
    sleep 1
  done

  tail -80 "${SERVER_LOG}" >&2 || true
  cleanup_started_server "${pid}"
  die "NoKV server did not become ready at ${SERVER_BIND}"
}

ensure_nokv_server() {
  if [[ -e "${SERVER_STATE}" || -L "${SERVER_STATE}" ]]; then
    if verify_reusable_server >/dev/null 2>&1; then
      if nokv_ls; then
        log "NoKV server already ready at ${SERVER_BIND} with the locked runtime"
        return
      fi
      log "managed NoKV server is not healthy; restarting it"
      stop_managed_server
    elif verify_managed_server >/dev/null 2>&1; then
      stop_managed_server
    else
      local recorded_pid=""
      recorded_pid="$(managed_server_pid 2>/dev/null || true)"
      if [[ ! "${recorded_pid}" =~ ^[0-9]+$ ]]; then
        managed_server_pid >&2 || true
        die "managed server state is invalid and requires manual inspection: ${SERVER_STATE}"
      fi
      if kill -0 "${recorded_pid}" >/dev/null 2>&1; then
        verify_managed_server >&2 || true
        die "managed server state is unsafe to reuse or stop: ${SERVER_STATE}"
      fi
      log "removing stale managed server state"
      rm -f "${SERVER_PID}" "${SERVER_STATE}"
    fi
  fi
  if port_in_use; then
    lsof -nP -iTCP@"${SERVER_BIND}" -sTCP:LISTEN >&2 || true
    die "${SERVER_BIND} is occupied, but NoKV client preflight failed; stop the conflicting process or change LINGTAI_WORKBENCH_SERVER_BIND"
  fi
  start_nokv_server
}

main() {
  [[ "$#" -eq 0 ]] || die "up.sh accepts no arguments; configure it with LINGTAI_WORKBENCH_* environment variables"
  require_cmd python3
  require_cmd lsof
  acquire_up_lock
  validate_profile_selection

  local project
  project="$(resolve_project)"
  [[ -d "${project}/.lingtai" ]] || die "not a LingTai project: ${project}"
  resolve_agent_once "${project}"

  log "project: ${project}"
  check_runtime_skill
  preflight_agent "${project}"
  cache_profile_fingerprints
  validate_guarded_credentials
  prepare_runtime "${project}"

  export AWS_ACCESS_KEY_ID="rustfsadmin"
  export AWS_SECRET_ACCESS_KEY="rustfsadmin"
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
  export AWS_EC2_METADATA_DISABLED=true
  ensure_rustfs
  if port_in_use; then
    log "validating candidate MCP contract before server handoff"
    probe_candidate_contract "${project}"
  fi
  ensure_nokv_server

  sync_agent "${project}"
  log "configuration committed; running post-commit verification"
  if ! check_agent "${project}"; then
    die "configuration committed but post-commit verification failed; not rolled back"
  fi
  print_locked_profile

  cat <<EOF

LingTai workbench is ready.
Run /refresh in the target LingTai agent.

Defaults used:
  project:        ${project}
  agent:          ${PINNED_AGENT}
  server_bind:    ${SERVER_BIND}
  s3_endpoint:    ${S3_ENDPOINT}
  s3_bucket:      ${S3_BUCKET}
  workbench_root: ${WORKBENCH_ROOT}
  mcp_profile:    ${MCP_PROFILE}
EOF
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
