#!/usr/bin/env bash
#
# backup.sh — snapshot USTradingBot's runtime state into a timestamped tarball.
#
# Backs up the things git does NOT track and that a deploy preserves rather than
# ships: the trade ledger, the whole data_store/ (open positions, watchlists,
# users, api keys, learnings, universe.db, heartbeat, …), and the .env config.
# The keys/ directory is included too (opt out with INCLUDE_KEYS=0) since it
# holds loose credentials the app reads.
#
# Usage:
#   scripts/backup.sh                      # back up ./  -> ./backups
#   APP_DIR=/opt/USTradingBot scripts/backup.sh
#   BACKUP_DIR=/var/backups/ustb KEEP=14 scripts/backup.sh
#
# Env:
#   APP_DIR       app root to back up            (default: repo root of this script)
#   BACKUP_DIR    where tarballs are written     (default: $APP_DIR/backups)
#   KEEP          how many newest tarballs to keep (default: 7; 0 = keep all)
#   INCLUDE_KEYS  include the keys/ dir (1/0)     (default: 1)
#
# Exit codes: 0 ok, non-zero on failure (safe to wire into cron + alerting).

set -euo pipefail

# Resolve the app dir: explicit APP_DIR wins, else the repo root above scripts/.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP="${KEEP:-7}"
INCLUDE_KEYS="${INCLUDE_KEYS:-1}"

# Portable timestamp (no Date.now dependency — plain date is fine in a shell).
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$BACKUP_DIR/ustb-backup-$STAMP.tar.gz"

log() { printf '[backup] %s\n' "$*" >&2; }

mkdir -p "$BACKUP_DIR"

# Assemble the include list from what actually exists (skip missing paths so a
# fresh install with no trades.csv yet still backs up cleanly).
declare -a INCLUDES=()
for path in "trades.csv" "data_store" ".env"; do
  if [ -e "$APP_DIR/$path" ]; then
    INCLUDES+=("$path")
  else
    log "skip (not found): $path"
  fi
done
if [ "$INCLUDE_KEYS" = "1" ] && [ -d "$APP_DIR/keys" ]; then
  INCLUDES+=("keys")
fi

if [ "${#INCLUDES[@]}" -eq 0 ]; then
  log "nothing to back up under $APP_DIR — aborting"
  exit 1
fi

log "app_dir=$APP_DIR"
log "archiving: ${INCLUDES[*]}"

# -C so paths inside the tarball are relative to the app dir (portable restore).
tar -czf "$ARCHIVE" -C "$APP_DIR" "${INCLUDES[@]}"

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
log "wrote $ARCHIVE ($SIZE)"

# Prune old backups, keeping the KEEP newest (0 disables pruning).  Uses a
# portable while-read loop (no `mapfile`, so it also runs on bash 3.2 / macOS).
if [ "$KEEP" -gt 0 ]; then
  ls -1t "$BACKUP_DIR"/ustb-backup-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | \
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      log "pruning old backup: $f"
      rm -f "$f"
    done
fi

log "done"

# ---------------------------------------------------------------------------
# Cron setup (daily at 02:30, keep 14 days, log to a file):
#
#   crontab -e
#   30 2 * * * APP_DIR=/opt/USTradingBot BACKUP_DIR=/var/backups/ustb KEEP=14 \
#              /opt/USTradingBot/scripts/backup.sh >> /var/log/ustb-backup.log 2>&1
#
# Restore a tarball:
#   tar -xzf /var/backups/ustb/ustb-backup-YYYYMMDD-HHMMSS.tar.gz -C /opt/USTradingBot
#   systemctl restart ustradingbot ustradingbot-engine
# ---------------------------------------------------------------------------
