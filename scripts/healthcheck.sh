#!/usr/bin/env bash
#
# healthcheck.sh — probe the dashboard /health endpoint and alert on failure.
#
# Designed for cron: it is quiet on success (no output, exit 0) and, on failure,
# emits one alert via a webhook and/or email, then exits non-zero.  A failure is
# any of: the endpoint is unreachable, returns a non-200 status, or the JSON body
# does not report "status":"ok".
#
# Usage:
#   scripts/healthcheck.sh
#   HEALTH_URL=http://127.0.0.1:8501/health scripts/healthcheck.sh
#   ALERT_WEBHOOK=https://hooks.slack.com/services/xxx scripts/healthcheck.sh
#   ALERT_EMAIL=ops@example.com scripts/healthcheck.sh
#
# Env:
#   HEALTH_URL     endpoint to probe        (default: http://127.0.0.1:8501/health)
#   TIMEOUT        curl timeout seconds     (default: 10)
#   RETRIES        attempts before failing  (default: 3, 5s apart — rides out a restart)
#   ALERT_WEBHOOK  POST {"text": "..."} here on failure (Slack/Discord/generic)
#   ALERT_EMAIL    mail this address on failure (needs `mail`/`mailx` installed)
#   HOSTLABEL      label used in the alert  (default: `hostname`)
#
# Exit codes: 0 healthy, 1 unhealthy (alert sent), 2 misconfig (curl missing).

set -uo pipefail

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8501/health}"
TIMEOUT="${TIMEOUT:-10}"
RETRIES="${RETRIES:-3}"
HOSTLABEL="${HOSTLABEL:-$(hostname 2>/dev/null || echo unknown-host)}"

command -v curl >/dev/null 2>&1 || { echo "healthcheck: curl not found" >&2; exit 2; }

attempt=0
ok=0
detail=""
while [ "$attempt" -lt "$RETRIES" ]; do
  attempt=$((attempt + 1))
  resp="$(curl -sS --max-time "$TIMEOUT" -w $'\n%{http_code}' "$HEALTH_URL" 2>/dev/null)" || resp=""
  status="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  if [ "$status" = "200" ] && printf '%s' "$body" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    ok=1
    break
  fi
  detail="HTTP ${status:-none}; body: ${body:-<empty>}"
  # Wait between attempts so a brief restart window doesn't page anyone.
  [ "$attempt" -lt "$RETRIES" ] && sleep 5
done

if [ "$ok" = "1" ]; then
  exit 0
fi

MESSAGE="🚨 USTradingBot health check FAILED on ${HOSTLABEL}: ${HEALTH_URL} — ${detail} (after ${RETRIES} attempts)"
echo "$MESSAGE" >&2

# --- Webhook alert (Slack/Discord/generic JSON {"text": ...}) ---
if [ -n "${ALERT_WEBHOOK:-}" ]; then
  # jq-free JSON escaping of the message.
  esc="$(printf '%s' "$MESSAGE" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  curl -fsS --max-time "$TIMEOUT" -X POST \
    -H 'Content-Type: application/json' \
    -d "{\"text\": \"${esc}\"}" \
    "$ALERT_WEBHOOK" >/dev/null 2>&1 \
    && echo "healthcheck: webhook alert sent" >&2 \
    || echo "healthcheck: webhook alert FAILED to send" >&2
fi

# --- Email alert (best-effort; needs a configured MTA + mail/mailx) ---
if [ -n "${ALERT_EMAIL:-}" ]; then
  if command -v mail >/dev/null 2>&1; then
    printf '%s\n' "$MESSAGE" | mail -s "USTradingBot health check FAILED (${HOSTLABEL})" "$ALERT_EMAIL" \
      && echo "healthcheck: email alert sent" >&2 \
      || echo "healthcheck: email alert FAILED to send" >&2
  else
    echo "healthcheck: ALERT_EMAIL set but 'mail' not installed" >&2
  fi
fi

exit 1

# ---------------------------------------------------------------------------
# Cron setup (probe every 5 minutes, alert to Slack):
#
#   crontab -e
#   */5 * * * * ALERT_WEBHOOK=https://hooks.slack.com/services/xxx \
#               /opt/USTradingBot/scripts/healthcheck.sh >> /var/log/ustb-health.log 2>&1
#
# Or a systemd timer (create ustb-health.service + ustb-health.timer). The script
# is quiet on success, so cron only mails you (via MAILTO) if it also writes to
# stderr — which it only does on failure.
# ---------------------------------------------------------------------------
