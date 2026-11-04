"""Synthetic payloads shaped like X's timeline GraphQL responses and pages (no real data)."""
import json
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


def snowflake(when):
    """A post id that encodes this time, as X's do."""
    return (int(when.timestamp() * 1000) - 1288834974657) << 22


def new_tweet(tid, handle, when, text, likes=10):
    """The newer app's post shape: details/counts instead of legacy.full_text and legacy counts."""
    return {
        "__typename": "Tweet", "id": "VHdlZXQ6MQ==", "rest_id": str(tid),
        "core": {"user_results": {"id": "x", "result": {"__typename": "User", "core": {"screen_name": handle}}}},
        "counts": {"bookmark_count": 5, "favorite_count": likes, "quote_count": 4, "reply_count": 3, "retweet_count": 2},
        "details": {"created_at_ms": int(when.timestamp() * 1000), "full_text": text},
        "legacy": {"entities": {}, "lang": "en"},
        "url_entities": [{"expanded_url": "https://example.com/new"}],
        "views": {"count": "1234"},
    }


def new_payload(tweets):
    entries = [{"entry_id": f"tweet-{t['rest_id']}", "content": {
        "__typename": "TimelineTimelineItem", "content": {"__typename": "TimelineTweet", "tweet_results": {
            "id": "x", "rest_id": t["rest_id"], "result": t}}}} for t in tweets]
    return {"data": {"user_result_by_screen_name": {"result": {"profile_user_originals_timeline": {"timeline": {
        "instructions": [{"__typename": "TimelineClearCache"},
                         {"__typename": "TimelineAddEntries", "entries": entries}]}}}}}}


def seroval(text):
    """A JS string literal, the way seroval writes one: quotes escaped and < as \\x3C."""
    return json.dumps(text, ensure_ascii=False).replace("<", "\\x3C")


def tanstack_html(payload, body=""):
    """A server-rendered document from the newer app, with the Relay response inside a seroval script."""
    key = seroval('hash{"count":20,"cursor":null,"screenName":"alice"}')
    script = (f'($R=>$R[21].next($R[23]={{kind:1,requestId:2,key:{key},'
              f'result:$R[24]={{kind:"Result.Ok",value:{seroval(json.dumps(payload))}}}}}))($R["tsr"]);')
    return f'<!doctype html><html><body>{body}<script nonce="n">{script}</script></body></html>'


def new_article(tid, handle, text, replies=1, reposts=2, likes=5659, views=621327):
    """The newer app's markup for a post (no data-testid, no <time>, rounded visible counts)."""
    return f"""<li><div role="link" data-href="/{handle}/status/{tid}" data-navigation-stop=""><article>
      <a href="/{handle}">{handle}</a><a href="/{handle}/status/{tid}">18h</a>
      <div dir="auto"><span>{text}</span></div>
      <div role="group" aria-label="{replies} replies, {reposts} reposts, {likes} likes, 7 bookmarks, {views} views">
        <div data-engagement-action="like"><button aria-label="{likes} Likes. Like">5.6K</button></div></div>
    </article></div></li>"""
