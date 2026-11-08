#!/usr/bin/env bash
# Phase timer. Usage: tools/timelog.sh start "<phase>" | tools/timelog.sh end "<phase>" "<notes>"
# Appends wall-clock timestamps to TIMELOG.md; durations are computed from the recorded start.
set -euo pipefail
cd "$(dirname "$0")/.."
action="$1"; phase="$2"; notes="${3:-}"
state=".timelog_state"
now_iso=$(date '+%Y-%m-%d %H:%M:%S %z'); now_s=$(date +%s)
if [[ ! -f TIMELOG.md ]]; then
  printf '# Build time log\n\nWall-clock times recorded by `tools/timelog.sh` at the start and end of each phase. Each phase also ends in its own git commit, so `git log --format="%%ad %%s" --date=iso` independently confirms the timeline.\n\n| Phase | Start | End | Duration | Notes |\n|---|---|---|---|---|\n' > TIMELOG.md
fi
case "$action" in
  start) echo "$phase|$now_iso|$now_s" > "$state"; echo "started: $phase at $now_iso" ;;
  end)
    IFS='|' read -r p s_iso s_s < "$state"
    [[ "$p" == "$phase" ]] || { echo "phase mismatch: started '$p', ending '$phase'"; exit 1; }
    d=$((now_s - s_s)); dur=$(printf '%dm %02ds' $((d/60)) $((d%60)))
    echo "| $phase | $s_iso | $now_iso | $dur | $notes |" >> TIMELOG.md
    rm -f "$state"; echo "ended: $phase at $now_iso ($dur)" ;;
  *) echo "usage: start|end <phase> [notes]"; exit 2 ;;
esac
