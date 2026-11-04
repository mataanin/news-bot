#!/usr/bin/env bash
# Write the weekly summary (data/digest-<since>.md) for the latest crawl, or for a posts file you name:
#   ./summarize.sh
#   ./summarize.sh data/posts-2026-11-02.jsonl
# Uses ANTHROPIC_API_KEY if set (or in .env), otherwise the logged-in Claude Code CLI.
# Safe to rerun: posts that are already tagged are skipped. A failure here never affects the crawl.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
POSTS="${1:-$("$PY" -c 'import json; print(json.load(open("data/latest.json"))["posts_file"])')}"
TAGS="${POSTS/posts-/tags-}"
DIGEST="${POSTS/posts-/digest-}"; DIGEST="${DIGEST%.jsonl}.md"
"$PY" -m xweek.tag "$POSTS"
"$PY" -m xweek.digest "$POSTS" "$TAGS"
"$PY" - "$POSTS" "$DIGEST" <<'PYEOF'
import json, sys
from pathlib import Path
posts, digest = sys.argv[1:]
latest = Path("data/latest.json")
if latest.exists():
    d = json.loads(latest.read_text())
    if d.get("posts_file") == posts:
        d["digest_file"] = digest
        latest.write_text(json.dumps(d, indent=1))
PYEOF
echo "Summary: $DIGEST"
