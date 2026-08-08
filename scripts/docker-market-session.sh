#!/bin/zsh
set -u

SCRIPT_DIR="${0:A:h}"
TARGETS_FILE="$SCRIPT_DIR/docker-session-targets.conf"
LOG_FILE="/Users/jasonjiang/Developer/a-stock-discipline-bot/data/session-scheduler.log"
PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

mkdir -p "${LOG_FILE:h}"
exec >>"$LOG_FILE" 2>&1

log() {
  echo "[$(/bin/date '+%Y-%m-%d %H:%M:%S %z')] $*"
}

is_market_day() {
  local config_file="$1"
  /usr/bin/python3 - "$config_file" <<'PY'
import re
import sys
from datetime import date
from pathlib import Path

today = date.today()
if today.weekday() >= 5:
    raise SystemExit(1)

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r"(?ms)^holidays:\s*(.*?)(?=^[A-Za-z_][A-Za-z0-9_]*:\s*|\Z)", text)
holidays = set(re.findall(r"\d{4}-\d{2}-\d{2}", match.group(1) if match else ""))
raise SystemExit(1 if today.isoformat() in holidays else 0)
PY
}

is_scheduled_day() {
  local calendar="$1"
  local holiday_config="$2"
  case "$calendar" in
    daily) return 0 ;;
    market) is_market_day "$holiday_config" ;;
    *) log "未知日历类型：$calendar"; return 1 ;;
  esac
}

wait_for_docker() {
  if docker info >/dev/null 2>&1; then
    return 0
  fi
  log "Docker Desktop 未运行，正在启动"
  /usr/bin/open -gja "Docker" || return 1
  for _ in {1..36}; do
    /bin/sleep 5
    if docker info >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

run_compose() {
  local project_dir="$1"
  local compose_file="$2"
  local command="$3"
  local services="$4"
  cd "$project_dir" || return 1
  if [[ -n "$services" ]]; then
    docker compose -f "$compose_file" "$command" ${=services}
  else
    docker compose -f "$compose_file" "$command"
  fi
}

start_target() {
  local name="$1"
  local project_dir="$2"
  local compose_file="$3"
  local services="$4"
  if ! wait_for_docker; then
    log "$name：Docker Desktop 启动超时"
    return 1
  fi
  if run_compose "$project_dir" "$compose_file" start "$services"; then
    log "$name：Docker 服务已启动"
  else
    log "$name：Docker 服务启动失败"
    return 1
  fi
}

stop_target() {
  local name="$1"
  local project_dir="$2"
  local compose_file="$3"
  local services="$4"
  if ! docker info >/dev/null 2>&1; then
    log "$name：Docker Desktop 未运行，无需停止"
    return 0
  fi
  if run_compose "$project_dir" "$compose_file" stop "$services"; then
    log "$name：Docker 服务已停止"
  else
    log "$name：Docker 服务停止失败"
    return 1
  fi
}

show_status() {
  while IFS='|' read -r name project_dir compose_file services _; do
    [[ -z "$name" || "$name" == \#* ]] && continue
    log "$name："
    run_compose "$project_dir" "$compose_file" ps "$services" || true
  done < "$TARGETS_FILE"
}

action="${1:-run}"
if [[ "$action" == "status" ]]; then
  show_status
  exit 0
fi

if [[ "$action" != "run" ]]; then
  log "未知动作：$action"
  exit 2
fi

if [[ ! -f "$TARGETS_FILE" ]]; then
  log "未找到目标配置：$TARGETS_FILE"
  exit 1
fi

now_time=$(/bin/date '+%H:%M')
failures=0
while IFS='|' read -r name project_dir compose_file services start_time stop_time calendar holiday_config; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  if [[ "$now_time" != "$start_time" && "$now_time" != "$stop_time" ]]; then
    continue
  fi
  if ! is_scheduled_day "$calendar" "$holiday_config"; then
    log "$name：非运行日，跳过 $now_time 调度"
    continue
  fi
  if [[ "$now_time" == "$start_time" ]]; then
    start_target "$name" "$project_dir" "$compose_file" "$services" || failures=1
  else
    stop_target "$name" "$project_dir" "$compose_file" "$services" || failures=1
  fi
done < "$TARGETS_FILE"

exit "$failures"
