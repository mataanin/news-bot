#!/usr/bin/env python3
"""Write the weekly summary: topics combined across people, with links to posts.

Stats (who posted most, most-liked posts, coverage) are computed in Python.
One Claude call writes the narrative. It cites posts as [@handle](#<post id>),
and the script swaps each id for the real URL, so links cannot be invented.

  python -m xweek.digest data/posts-2026-10-14.jsonl data/tags-2026-10-14.jsonl
  python -m xweek.digest ... --dry-run    # print the prompt, no API call
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

MODEL = os.environ.get("XWEEK_DIGEST_MODEL", "claude-sonnet-5-5")

INSTRUCTIONS = """Below are coding-related posts from the past week by people who shape AI coding, grouped by \
topic and sorted by likes. Each line is: [post id] @handle date likes: summary.

Write a weekly summary in Markdown for a senior engineer who builds AI agents:
1. "## TL;DR": 6-8 bullets, the biggest conversations of the week, each with its key number or claim.
2. Then one "##" section per theme. Combine topics discussed by several people into one theme, and \
say who said what. Lead each section with its point in one sentence. Prefer specifics (numbers, \
names, dates) to adjectives. Disagreements are worth showing side by side.
3. Cite every claim with a link written exactly as [@handle](#POSTID), using the post ids given. \
Never invent ids or URLs. Leave out themes with only trivial posts.
Keep sentences under 25 words. No preamble, no closing summary."""

CITE = re.compile(r"\[([^\]]+)\]\(#(\d{1,25})\)")


def load_jsonl(p: str) -> list[dict]:
    return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]


def fmt_likes(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e3 else str(n)


def build_prompt(posts: dict, tags: list[dict]) -> str:
    groups = collections.defaultdict(list)
    for t in tags:
        if t.get("coding") and t["id"] in posts:
            groups[(t.get("topics") or ["Other"])[0]].append(t)
    parts = []
    for topic, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        items.sort(key=lambda t: -posts[t["id"]].get("likes", 0))
        lines = [f'[{t["id"]}] @{posts[t["id"]]["handle"]} {posts[t["id"]]["created_at"][:10]} '
                 f'{fmt_likes(posts[t["id"]].get("likes", 0))}: {t.get("summary", "")}' for t in items]
        parts.append(f"### {topic} ({len(items)})\n" + "\n".join(lines))
    return INSTRUCTIONS + "\n\n" + "\n\n".join(parts)


def link_citations(body: str, posts: dict) -> str:
    """[@h](#ID) -> [@h](real post URL); an unknown id keeps its text and loses the link."""
    def link(m: re.Match) -> str:
        p = posts.get(m.group(2))
        return f"[{m.group(1)}]({p['url']})" if p else m.group(1)
    return CITE.sub(link, body)


def stats_sections(posts: dict, tags: list[dict], crawl_stats: dict | None) -> str:
    coding = [posts[t["id"]] for t in tags if t.get("coding") and t["id"] in posts]
    by_handle = collections.Counter(p["handle"] for p in coding)
    truncated = {h for h, s in (crawl_stats or {}).items() if s.get("truncated")}
    out = ["## Who drove the conversation", "", "| Account | Coding posts |", "| --- | --- |"]
    for h, n in by_handle.most_common(15):
        out.append(f"| [@{h}](https://x.com/{h}) | {n}{'+' if h in truncated else ''} |")
    out += ["", "**Most-liked coding posts**", "", "| Likes | Account | Post |", "| --- | --- | --- |"]
    summ = {t["id"]: t.get("summary", "") for t in tags}
    for p in sorted(coding, key=lambda p: -p.get("likes", 0))[:10]:
        s = summ.get(p["id"], "")
        out.append(f"| {fmt_likes(p.get('likes', 0))} | @{p['handle']} | [{s[:90]}{'…' if len(s) > 90 else ''}]({p['url']}) |")
    if crawl_stats:
        bad = {h: s["status"] for h, s in crawl_stats.items() if s.get("status") != "ok"}
        silent = sorted(h for h, s in crawl_stats.items() if s.get("status") == "ok" and s.get("kept", 0) == 0)
        out += ["", "**Coverage**", "",
                f"- {len(crawl_stats)} accounts crawled; {len(posts)} posts in the window, {len(coding)} coding-related "
                f"from {len(by_handle)} accounts."]
        if truncated:
            out.append(f"- Hit the scroll limit (earliest posts may be missing, marked +): {', '.join('@' + h for h in sorted(truncated))}.")
        if silent:
            out.append(f"- No posts in the window: {', '.join('@' + h for h in silent)}.")
        if bad:
            out.append(f"- Not crawled: {', '.join(f'@{h} ({s})' for h, s in sorted(bad.items()))}.")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("posts")
    ap.add_argument("tags")
    ap.add_argument("--crawl-stats", help="default: crawl-stats-*.json next to the posts file")
    ap.add_argument("--out", help="default: digest-YYYY-MM-DD.md next to the posts file")
    ap.add_argument("--title", default="AI coding on X")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    posts = {p["id"]: p for p in load_jsonl(a.posts)}
    tags = load_jsonl(a.tags)
    pp = Path(a.posts)
    cs_path = Path(a.crawl_stats) if a.crawl_stats else pp.with_name(pp.name.replace("posts-", "crawl-stats-").replace(".jsonl", ".json"))
    crawl_stats = json.loads(cs_path.read_text()) if cs_path.exists() else None
    prompt = build_prompt(posts, tags)
    if a.dry_run:
        print(prompt)
        return

    import anthropic
    client = anthropic.Anthropic(max_retries=6)
    msg = client.messages.create(model=MODEL, max_tokens=12000,
                                 messages=[{"role": "user", "content": prompt}])
    body = "".join(b.text for b in msg.content if b.type == "text")

    body = link_citations(body, posts)
    window = pp.stem.replace("posts-", "")
    doc = f"# {a.title}: week from {window}\n\n{body.strip()}\n\n{stats_sections(posts, tags, crawl_stats)}\n"
    out = Path(a.out) if a.out else pp.with_name(f"digest-{window}.md")
    out.write_text(doc)
    print(f"digest -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
