"""Synthetic payloads shaped like X's UserTweets GraphQL response (no real data)."""
from datetime import datetime, timedelta, timezone

FMT = "%a %b %d %H:%M:%S +0000 %Y"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def tweet(tid, handle, when, text, likes=10, quote=None, repost_of=None, reply_to=None, long_text=None,
          wrap=False):
    t = {
        "__typename": "Tweet",
        "rest_id": str(tid),
        "core": {"user_results": {"result": {"__typename": "User", "core": {"screen_name": handle}}}},
        "views": {"count": "1234"},
        "legacy": {
            "full_text": text,
            "created_at": when.strftime(FMT),
            "favorite_count": likes, "retweet_count": 2, "reply_count": 3, "quote_count": 1,
            "in_reply_to_screen_name": reply_to,
            "entities": {"urls": [{"expanded_url": "https://example.com/post"}]},
        },
    }
    if quote:
        t["quoted_status_result"] = {"result": quote}
    if repost_of:
        t["legacy"]["retweeted_status_result"] = {"result": repost_of}
    if long_text:
        t["note_tweet"] = {"note_tweet_results": {"result": {"text": long_text}}}
    if wrap:
        return {"__typename": "TweetWithVisibilityResults", "tweet": t}
    return t


def entry(eid, result):
    return {"entryId": eid, "content": {"itemContent": {"tweet_results": {"result": result}}}}


def payload(entries, pin=None, more=True, dom=None):
    instructions = []
    if pin is not None:
        instructions.append({"type": "TimelinePinEntry", "entry": entry("tweet-pin", pin)})
    instructions.append({"type": "TimelineAddEntries", "entries": entries})
    return {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": instructions}}}}},
            "more": more, "dom": dom or []}


def sample_page():
    """One page with every case the parser must handle."""
    h = "alice"
    other = tweet(900, "bob", NOW - timedelta(hours=5), "bob's original")
    entries = [
        entry("tweet-1", tweet(101, h, NOW - timedelta(hours=1), "short", likes=1500,
                               quote=tweet(902, "carol", NOW - timedelta(days=1), "quoted text"))),
        entry("tweet-2", tweet(102, h, NOW - timedelta(hours=2), "trunc", long_text="the full long post", wrap=True)),
        entry("tweet-3", tweet(103, h, NOW - timedelta(hours=3), "RT @bob", repost_of=other)),
        entry("tweet-4", tweet(104, h, NOW - timedelta(hours=4), "@bob reply", reply_to="bob")),
        {"entryId": "profile-conversation-1", "content": {"items": [
            {"item": {"itemContent": {"tweet_results": {"result": tweet(105, h, NOW - timedelta(hours=6), "thread 1/2")}}}},
            {"item": {"itemContent": {"tweet_results": {"result": tweet(106, h, NOW - timedelta(hours=6), "thread 2/2", reply_to=h)}}}},
        ]}},
        entry("tweet-7", tweet(107, h, NOW - timedelta(days=9), "too old")),
    ]
    pin = tweet(100, h, NOW - timedelta(days=200), "pinned and old")
    return payload(entries, pin=pin)


def paged_timeline(handle="alice", pages=5, per_page=6, hours_apart=10):
    """Pages going back in time; each post 10 hours older than the last. Page 0 has an old pin."""
    out, n = [], 0
    base = 5000 + 1000 * (sum(map(ord, handle)) % 97)  # ids are unique per account, like real X ids
    for k in range(pages):
        entries, dom = [], []
        for _ in range(per_page):
            when = NOW - timedelta(hours=hours_apart * n)
            tid = base - n
            entries.append(entry(f"tweet-{tid}", tweet(tid, handle, when, f"post {n}")))
            dom.append({"h": handle, "id": str(tid), "d": when.isoformat().replace("+00:00", "Z"), "t": f"post {n}"})
            n += 1
        pin = tweet(1, handle, NOW - timedelta(days=300), "old pin") if k == 0 else None
        out.append(payload(entries, pin=pin, more=k < pages - 1, dom=dom))
    return out
