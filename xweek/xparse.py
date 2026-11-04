"""Parse the timeline JSON that X's web apps load.

X serves two web apps (seen 2026-11). Logged-in visitors get the classic app: posts come from
x.com/i/api/graphql/<id>/UserOriginalsTimeline, in the ``legacy.*`` shape, and the DOM has data-testid.
Logged-out visitors get a newer TanStack app: the first page is server-rendered, as Relay JSON strings
inside seroval scripts, in the ``details.*``/``counts.*`` shape, and the DOM has no data-testid. Both are
handled so a rollout of the newer app to logged-in users doesn't break the crawl.

The schema shifts often, so this does not walk fixed paths. It finds tweet objects anywhere in the
payload (a dict with ``rest_id`` and ``full_text``) and reads fields defensively. No Playwright import
here, so it is unit-testable.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterator

X = "https://x.com"
# x.com/i/api/graphql/... or api.x.com/graphql/...; operations seen: UserOriginalsTimeline, formerly UserTweets.
TIMELINE_URL = re.compile(r"/graphql/[^/]+/(UserTweets|UserTweetsAndReplies|User\w*Timeline)(\?|$)")
SNOWFLAKE_EPOCH_MS = 1288834974657


def is_timeline_url(url: str) -> bool:
    return bool(TIMELINE_URL.search(url))


def parse_created(s: str) -> datetime:
    """X's legacy timestamp, e.g. 'Wed Oct 21 05:12:00 +0000 2026'."""
    return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)


def _get(d: Any, *path: str) -> Any:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _unwrap(t: Any) -> Any:
    """Visibility-limited posts arrive wrapped as TweetWithVisibilityResults."""
    if isinstance(t, dict) and t.get("__typename") == "TweetWithVisibilityResults":
        return t.get("tweet")
    return t


def _is_tweet(d: Any) -> bool:
    if not isinstance(d, dict) or "rest_id" not in d:
        return False
    return "full_text" in (_get(d, "legacy") or {}) or "full_text" in (_get(d, "details") or {})


def iter_tweet_objects(node: Any) -> Iterator[dict]:
    """Yield top-level tweet objects; does not descend into a tweet (so quoted
    and reposted originals are not yielded as posts of their own)."""
    node = _unwrap(node)
    if _is_tweet(node):
        yield node
        return
    if isinstance(node, dict):
        for v in node.values():
            yield from iter_tweet_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_tweet_objects(v)


def pinned_ids(payload: Any) -> set[str]:
    """Ids of pinned posts (TimelinePinEntry), so an old pin doesn't stop scrolling early."""
    out: set[str] = set()

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if "TimelinePinEntry" in (n.get("type"), n.get("__typename")):
                out.update(t["rest_id"] for t in iter_tweet_objects(n))
                return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(payload)
    return out


def _screen_name(tweet: dict) -> str | None:
    user = _get(tweet, "core", "user_results", "result")
    user = _unwrap(user)
    return _get(user, "core", "screen_name") or _get(user, "legacy", "screen_name")


def _text(tweet: dict) -> str:
    long_text = _get(tweet, "note_tweet", "note_tweet_results", "result", "text")
    return long_text or _get(tweet, "details", "full_text") or _get(tweet, "legacy", "full_text") or ""


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def to_record(tweet: dict) -> dict | None:
    tweet = _unwrap(tweet)
    if not _is_tweet(tweet):
        return None
    lg = tweet.get("legacy") or {}
    counts = {**lg, **(tweet.get("counts") or {})}
    try:
        ms = _get(tweet, "details", "created_at_ms")
        created = (datetime.fromtimestamp(ms / 1000, timezone.utc) if ms else parse_created(lg["created_at"]))
    except (KeyError, ValueError, TypeError):
        return None
    handle = _screen_name(tweet) or ""
    quote = None
    q = _unwrap(_get(tweet, "quoted_status_result", "result") or _get(lg, "quoted_status_result", "result"))
    if _is_tweet(q):
        quote = {"handle": _screen_name(q) or "", "text": _text(q)[:600], "id": q["rest_id"]}
    urls = (_get(lg, "entities", "urls") or []) + (tweet.get("url_entities") or [])
    links = [u.get("expanded_url") for u in urls if u.get("expanded_url")]
    tid = tweet["rest_id"]
    return {
        "id": tid,
        "handle": handle,
        "url": f"{X}/{handle or 'i'}/status/{tid}",
        "created_at": created.isoformat(),
        "text": _text(tweet),
        "quote": quote,
        "links": links,
        "likes": _int(counts.get("favorite_count")),
        "reposts": _int(counts.get("retweet_count")),
        "replies": _int(counts.get("reply_count")),
        "quotes": _int(counts.get("quote_count")),
        "views": _int(_get(tweet, "views", "count")),
        "reply_to": lg.get("in_reply_to_screen_name"),
        "is_repost": bool(lg.get("retweeted_status_result")),
    }


def records_from_payload(payload: Any) -> list[dict]:
    out = []
    for t in iter_tweet_objects(payload):
        r = to_record(t)
        if r:
            out.append(r)
    return out


def timeline_floor(payload: Any, handle: str, skip: set[str]) -> datetime | None:
    """How far back this response reaches: the oldest timeline entry, dated by the newest own post in it.

    A conversation module can hold an old thread root next to this week's reply, so dating an entry by
    its oldest post could end the scroll early. Pins are excluded, and so are their instructions, which
    use ``entry`` rather than ``entries``.
    """
    h = handle.lower()
    floor: datetime | None = None

    def walk(n: Any) -> None:
        nonlocal floor
        if isinstance(n, dict):
            for k, v in n.items():
                if k != "entries" or not isinstance(v, list):
                    walk(v)
                    continue
                for e in v:
                    ds = [datetime.fromisoformat(r["created_at"]) for r in records_from_payload(e)
                          if r["handle"].lower() == h and not r["is_repost"] and r["id"] not in skip]
                    if ds and (floor is None or max(ds) < floor):
                        floor = max(ds)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(payload)
    return floor


def _js_string(literal: str) -> str | None:
    """Decode the body of a double-quoted JS string. seroval also writes \\xHH, which JSON doesn't allow."""
    body = re.sub(r"\\(x[0-9a-fA-F]{2}|'|.)",
                  lambda m: "\\u00" + m[1][1:] if m[1][0] == "x" and len(m[1]) == 3 else "'" if m[1] == "'" else m[0],
                  literal, flags=re.S)
    try:
        return json.loads(f'"{body}"', strict=False)
    except ValueError:
        return None


def payloads_from_html(html: str) -> list[Any]:
    """Relay responses server-rendered into the document (the TanStack app, as seroval-escaped strings)."""
    out = []
    for lit in re.findall(r'"((?:[^"\\]|\\.)*)"', html, re.S):
        if "rest_id" not in lit:
            continue
        text = _js_string(lit)
        try:
            out.append(json.loads(text))
        except (TypeError, ValueError):
            continue
    return out


def keep_for_profile(rec: dict, handle: str, since: datetime) -> bool:
    """Own posts in the window: no reposts, no replies to others (self-threads kept)."""
    h = handle.lower()
    if (rec.get("handle") or "").lower() != h or rec.get("is_repost"):
        return False
    if rec.get("reply_to") and rec["reply_to"].lower() != h:
        return False
    return datetime.fromisoformat(rec["created_at"]) >= since


# DOM fallback, used only if no timeline JSON was captured (e.g. X renamed the GraphQL operation). Reads
# the classic markup (data-testid) and the newer one (div[data-href] around a plain article, no <time>,
# so the date comes from the post id). The "208 replies, 398 reposts, 5659 likes, 621327 views" label
# gives exact counts, where the visible numbers are rounded.
DOM_JS = r"""
() => {
  const label = a => (a.querySelector('[role="group"][aria-label]') || a.querySelector(
    '[data-testid="like"],[data-testid="unlike"],[data-engagement-action="like"] [aria-label]') || {getAttribute: () => ''})
    .getAttribute('aria-label') || '';
  const classic = [...document.querySelectorAll('article[data-testid="tweet"]')].map(a => {
    const t = a.querySelector('time'); const l = t && t.closest('a'); if (!l) return null;
    const parts = l.href.split('?')[0].split('/');
    const tx = [...a.querySelectorAll('[data-testid="tweetText"]')].map(e => e.innerText);
    const sc = (a.querySelector('[data-testid="socialContext"]') || {}).innerText || '';
    return {id: parts[5], handle: parts[3], created_at: t.getAttribute('datetime'), text: tx[0] || '',
            quote: tx[1] ? {handle: '', text: tx[1]} : null, links: [], counts_label: label(a),
            pinned: /pinned/i.test(sc), reply_to: null, is_repost: /reposted/i.test(sc)};
  });
  const newer = [...document.querySelectorAll('[data-href*="/status/"]')].map(w => {
    const a = w.querySelector('article');
    const m = (w.getAttribute('data-href') || '').match(/^\/([^/]+)\/status\/(\d+)/);
    if (!a || !m) return null;
    const tx = [...a.querySelectorAll('div[dir="auto"]')].map(e => e.innerText);
    const ms = Number((BigInt(m[2]) >> 22n) + 1288834974657n);
    const first = a.innerText.split('\n')[0];
    return {id: m[2], handle: m[1], created_at: new Date(ms).toISOString(), text: tx[0] || '',
            quote: tx[1] ? {handle: '', text: tx[1]} : null, links: [], counts_label: label(a),
            pinned: /^pinned/i.test(first), reply_to: null, is_repost: /\breposted$/i.test(first)};
  });
  return [...classic, ...newer].filter(Boolean);
}
"""


def snowflake_time(tid: str) -> datetime:
    """When a post was created, from its id."""
    return datetime.fromtimestamp(((int(tid) >> 22) + SNOWFLAKE_EPOCH_MS) / 1000, timezone.utc)


def _count(n: str, unit: str) -> int:
    return int(float(n.replace(",", "")) * {"K": 1e3, "M": 1e6, "": 1}[unit.upper()])


def normalize_dom_record(r: dict) -> dict:
    created = r.get("created_at") or snowflake_time(r["id"]).isoformat()
    if created.endswith("Z"):
        created = datetime.fromisoformat(created[:-1] + "+00:00").isoformat()
    found = {k.lower(): _count(n, u) for n, u, k in re.findall(
        r"(\d[\d,.]*)\s*([KMkm]?)\s+(repl|repost|like|quote|view)", r.get("counts_label") or "", re.I)}
    out = {k: r.get(k) for k in ("id", "handle", "text", "quote", "links", "reply_to", "is_repost")}
    out.update(url=f"{X}/{r.get('handle') or 'i'}/status/{r['id']}", created_at=created,
               likes=found.get("like", 0), reposts=found.get("repost", 0), replies=found.get("repl", 0),
               quotes=found.get("quote", 0), views=found.get("view", 0), source="dom")
    return out
