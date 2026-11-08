#!/usr/bin/env bash
# APP Shield verify: every check CI would run. Exits non-zero on the first failure.
# Usage: ./verify.sh            (uses .venv if present, else python3; override with PYTHON=...)
set -euo pipefail
cd "$(dirname "$0")"

if [[ -n "${PYTHON:-}" ]]; then PY="$PYTHON"
elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python
elif [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe
else PY=python3; fi

step() { printf '\n== %s\n' "$1"; }

step "1/6 Python version (3.11+ required)"
"$PY" -c 'import sys; v = sys.version_info; print(sys.version.split()[0]); sys.exit(0 if v >= (3, 11) else 1)'

step "2/6 Dependencies importable"
"$PY" -c 'import fastapi, pydantic, uvicorn, httpx, pytest; print("fastapi", fastapi.__version__, "| pydantic", pydantic.VERSION)'

step "3/6 Byte-compile application code"
"$PY" -m compileall -q app && echo "ok"

step "4/6 Shipped rule config and mule list stub are valid"
"$PY" - <<'PY'
from app.config import ConfigStore
from app.services.mule_service import MuleService
loaded = ConfigStore("config/rules.json").get()
status = MuleService("sample-data/mule_accounts.json").status(enabled=True)
assert status == "up", f"mule list stub unreadable: {status}"
print(f"ruleset {loaded.rules.ruleset_version} | config sha256 {loaded.config_hash[:12]} | mule list {status}")
PY

step "5/6 Repository hygiene (no databases, PDFs or obvious secrets tracked)"
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  bad=$(git ls-files | grep -Ei '\.(db|sqlite|pdf|pem|key)$|(^|/)\.env$' || true)
  [[ -z "$bad" ]] || { echo "tracked files that must not be committed:"; echo "$bad"; exit 1; }
  secrets=$(git grep -nIE 'BEGIN (RSA |EC )?PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[0-9A-Za-z]{36}' -- . ':!verify.sh' || true)
  [[ -z "$secrets" ]] || { echo "possible secrets:"; echo "$secrets"; exit 1; }
  echo "ok"
else
  echo "skipped (not a git checkout)"
fi

step "6/6 Tests: unit, integration, acceptance and labelled scenario suite"
"$PY" -m pytest -p no:cacheprovider

if [[ -f reports/scenario-report.md ]]; then
  step "Scenario report (reports/scenario-report.md)"
  head -n 3 reports/scenario-report.md | tail -n 1
fi

printf '\nVERIFY PASSED\n'
