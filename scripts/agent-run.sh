#!/usr/bin/env sh
# Run the configured external agent once with agents/scheduled-run.md (Linux/macOS).
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
PY=.venv/bin/python
CMD=$($PY -c "from infohub import config; c=config.load(); print(c['agent']['command'])")
[ -z "$CMD" ] && { echo "agent.command is empty in config/hub.local.yaml"; exit 1; }
# shellcheck disable=SC2086
$CMD "$(cat agents/scheduled-run.md)" 2>&1 | tee "logs/agent-$(date +%Y%m%d-%H%M).log"
# cron example (every 2 hours):  0 */2 * * *  /path/to/info-hub/scripts/agent-run.sh
