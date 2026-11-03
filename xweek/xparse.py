"""Parse the timeline JSON that X's web app loads (GraphQL UserTweets).

The schema shifts often, so this does not walk fixed paths. It finds tweet
objects anywhere in the payload (a dict with ``rest_id`` and ``legacy.full_text``)
and reads fields defensively. No Playwright import here, so it is unit-testable.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterator

X = "https://x.com"
TIMELINE_URL = re.compile(r"/i/api/graphql/[^/]+/(UserTweets|UserTweetsAndReplies)(\?|$)")


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
    return isinstance(d, dict) and "rest_id" in d and isinstance(d.get("legacy"), dict) and "full_text" in d["legacy"]


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
            if n.get("type") == "TimelinePinEntry":
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
    return long_text or tweet["legacy"].get("full_text", "")


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def to_record(tweet: dict) -> dict | None:
    tweet = _unwrap(tweet)
    if not _is_tweet(tweet):
        return None
    lg = tweet["legacy"]
    try:
        created = parse_created(lg["created_at"])
    except (KeyError, ValueError):
        return None
    handle = _screen_name(tweet) or ""
    quote = None
    q = _unwrap(_get(tweet, "quoted_status_result", "result") or _get(lg, "quoted_status_result", "result"))
    if _is_tweet(q):
        quote = {"handle": _screen_name(q) or "", "text": _text(q)[:600], "id": q["rest_id"]}
    links = [u.get("expanded_url") for u in _get(lg, "entities", "urls") or [] if u.get("expanded_url")]
    tid = tweet["rest_id"]
    return {
        "id": tid,
        "handle": handle,
        "url": f"{X}/{handle or 'i'}/status/{tid}",
        "created_at": created.isoformat(),
        "text": _text(tweet),
        "quote": quote,
        "links": links,
        "likes": _int(lg.get("favorite_count")),
        "reposts": _int(lg.get("retweet_count")),
        "replies": _int(lg.get("reply_count")),
        "quotes": _int(lg.get("quote_count")),
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


def keep_for_profile(rec: dict, handle: str, since: datetime) -> bool:
    """Own posts in the window: no reposts, no replies to others (self-threads kept)."""
    h = handle.lower()
    if (rec.get("handle") or "").lower() != h or rec.get("is_repost"):
        return False
    if rec.get("reply_to") and rec["reply_to"].lower() != h:
        return False
    return datetime.fromisoformat(rec["created_at"]) >= since


# DOM fallback, used only if no timeline JSON was captured (e.g. X renamed the
# GraphQL operation). Returns minimal records without metrics.
DOM_JS = r"""
() => [...document.querySelectorAll('article[data-testid="tweet"]')].map(a => {
  const t = a.querySelector('time'); const l = t && t.closest('a'); if (!l) return null;
  const u = l.href.split('?')[0]; const parts = u.split('/');
  const tx = [...a.querySelectorAll('[data-testid="tweetText"]')].map(e => e.innerText);
  const sc = (a.querySelector('[data-testid="socialContext"]') || {}).innerText || '';
  const like = a.querySelector('[data-testid="like"],[data-testid="unlike"]');
  return {id: parts[5], handle: parts[3], url: u, created_at: t.getAttribute('datetime'),
          text: tx[0] || '', quote: tx[1] ? {handle: '', text: tx[1]} : null, links: [],
          likes_label: like ? like.getAttribute('aria-label') : '', pinned: /pinned/i.test(sc),
          reply_to: null, is_repost: /reposted/i.test(sc)};
}).filter(Boolean)
"""


def normalize_dom_record(r: dict) -> dict:
    created = r.get("created_at") or ""
    if created.endswith("Z"):
        created = created[:-1] + "+00:00"
    m = re.match(r"([\d,.]+)\s*([KM]?)", r.get("likes_label") or "")
    likes = 0
    if m:
        likes = int(float(m.group(1).replace(",", "")) * {"K": 1e3, "M": 1e6, "": 1}[m.group(2)])
    out = {k: r.get(k) for k in ("id", "handle", "url", "text", "quote", "links", "reply_to", "is_repost")}
    out.update(created_at=created, likes=likes, reposts=0, replies=0, quotes=0, views=0, source="dom")
    return out
