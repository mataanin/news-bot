#!/usr/bin/env bash
# Crawl the last 7 days of posts from handles.txt (started by launchd when trigger/run-request changes).
# If ANTHROPIC_API_KEY is set (or in .env), also tag the posts and write the digest locally;
# otherwise the Claude scheduled task reads the crawl output and writes the report.
# Extra arguments go to the crawler. Writes data/latest.json for the reporter.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
mkdir -p data
# One crawl at a time; tell the reporter that a run has started.
find data -maxdepth 1 -name .lock -mmin +120 -exec rmdir {} \; 2>/dev/null || true   # clear a stale lock
if ! mkdir data/.lock 2>/dev/null; then echo "another run is in progress"; exit 0; fi
on_exit() { rc=$?; rmdir data/.lock 2>/dev/null || true
  if [ $rc -ne 0 ]; then printf '{"state": "failed", "exit_code": %s, "finished_at": "%s"}' "$rc" "$(date -u +%FT%TZ)" > data/status.json; fi; }
trap on_exit EXIT
"$PY" -c 'import json,sys; from datetime import datetime, timezone; json.dump({"state": "running", "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, open("data/status.json", "w"))'
SINCE=$("$PY" -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"))')
echo "== $(date -u +%FT%TZ) xweek run, window since $SINCE"
CH=""; [ -d "/Applications/Google Chrome.app" ] && CH="--channel chrome"
"$PY" -m xweek.crawl run --handles handles.txt --since "$SINCE" $CH "$@"
DIGEST=""
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  "$PY" -m xweek.tag "data/posts-$SINCE.jsonl"
  "$PY" -m xweek.digest "data/posts-$SINCE.jsonl" "data/tags-$SINCE.jsonl"
  DIGEST="data/digest-$SINCE.md"
fi
"$PY" - "$SINCE" "$DIGEST" <<'PYEOF'
import json, sys
from datetime import datetime, timezone
since, digest = sys.argv[1], sys.argv[2] or None
posts = f"data/posts-{since}.jsonl"
stats = json.load(open(f"data/crawl-stats-{since}.json"))
n = sum(1 for _ in open(posts))
json.dump({"since": since, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "posts_file": posts, "stats_file": f"data/crawl-stats-{since}.json", "digest_file": digest,
           "posts": n, "accounts": len(stats),
           "not_ok": {h: s["status"] for h, s in stats.items() if s["status"] != "ok"},
           "truncated": sorted(h for h, s in stats.items() if s.get("truncated"))},
          open("data/latest.json", "w"), indent=1)
json.dump({"state": "done", "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, open("data/status.json", "w"))
print(f"latest.json: {n} posts from {len(stats)} accounts")
PYEOF
