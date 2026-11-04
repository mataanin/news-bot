import unittest
from datetime import timedelta

from xweek.xparse import (is_timeline_url, keep_for_profile, normalize_dom_record, payloads_from_html, pinned_ids,
                          records_from_payload, snowflake_time, timeline_floor)
from tests.fixtures import NOW, new_payload, new_tweet, payload, sample_page, snowflake, tanstack_html, tweet


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.payload = sample_page()
        self.recs = {r["id"]: r for r in records_from_payload(self.payload)}

    def test_finds_top_level_posts_only(self):
        # 100 pin, 101-107 timeline; quoted 902 and reposted 900 are not posts of their own
        self.assertEqual(sorted(self.recs), ["100", "101", "102", "103", "104", "105", "106", "107"])

    def test_fields(self):
        r = self.recs["101"]
        self.assertEqual(r["handle"], "alice")
        self.assertEqual(r["url"], "https://x.com/alice/status/101")
        self.assertEqual(r["likes"], 1500)
        self.assertEqual(r["views"], 1234)
        self.assertEqual(r["quote"], {"handle": "carol", "text": "quoted text", "id": "902"})
        self.assertEqual(r["links"], ["https://example.com/post"])
        self.assertTrue(r["created_at"].startswith("2026-10-21T11:00"))

    def test_long_post_and_visibility_wrapper(self):
        self.assertEqual(self.recs["102"]["text"], "the full long post")

    def test_pinned(self):
        self.assertEqual(pinned_ids(self.payload), {"100"})

    def test_keep_rules(self):
        since = NOW - timedelta(days=7)
        kept = {i for i, r in self.recs.items() if keep_for_profile(r, "Alice", since)}
        # drops: 100 old pin, 103 repost, 104 reply to someone else, 107 too old; keeps self-thread 106
        self.assertEqual(kept, {"101", "102", "105", "106"})

    def test_timeline_url(self):
        self.assertTrue(is_timeline_url("https://x.com/i/api/graphql/AbC-1_2/UserTweets?variables=%7B%7D"))
        self.assertTrue(is_timeline_url("https://x.com/i/api/graphql/zz/UserTweetsAndReplies"))
        self.assertFalse(is_timeline_url("https://x.com/i/api/graphql/zz/UserByScreenName?x=1"))
        self.assertFalse(is_timeline_url("https://x.com/i/api/graphql/zz/UserTweetsFoo"))

    def test_dom_fallback_record(self):
        r = normalize_dom_record({"id": "9", "handle": "alice", "url": "https://x.com/alice/status/9",
                                  "created_at": "2026-10-20T10:00:00.000Z", "text": "hi", "quote": None,
                                  "links": [], "counts_label": "1.2K Likes. Like", "reply_to": None,
                                  "is_repost": False})
        self.assertEqual(r["likes"], 1200)
        self.assertTrue(keep_for_profile(r, "alice", NOW - timedelta(days=7)))


class TimelineUrlTest(unittest.TestCase):
    def test_timeline_calls_on_both_hosts(self):
        for u in ("https://x.com/i/api/graphql/abc-_1/UserOriginalsTimeline?variables=%7B%7D",
                  "https://api.x.com/graphql/abc/UserOriginalsTimeline?variables=1",
                  "https://x.com/i/api/graphql/abc/UserTweets?variables=1"):
            self.assertTrue(is_timeline_url(u), u)

    def test_other_calls_are_ignored(self):
        for u in ("https://x.com/i/api/graphql/abc/UserByScreenName?variables=1",
                  "https://x.com/i/api/graphql/abc/BroadcastQuery?variables=1",
                  "https://api.x.com/graphql/abc/UserSportsProfile",
                  "https://x.com/i/api/1.1/graphql/viewer_context.json"):
            self.assertFalse(is_timeline_url(u), u)


class NewAppTest(unittest.TestCase):
    def test_new_post_shape(self):
        when = NOW - timedelta(hours=3)
        r = records_from_payload(new_payload([new_tweet(555, "alice", when, "hello", likes=42)]))[0]
        self.assertEqual(r["url"], "https://x.com/alice/status/555")
        self.assertEqual(r["created_at"], when.isoformat())
        self.assertEqual((r["text"], r["likes"], r["reposts"], r["replies"], r["quotes"], r["views"]),
                         ("hello", 42, 2, 3, 4, 1234))
        self.assertEqual(r["links"], ["https://example.com/new"])

    def test_server_rendered_page(self):
        text = 'a "quoted" word, a <tag>, a newline\nand caf\u00e9'
        html = tanstack_html(new_payload([new_tweet(555, "alice", NOW, text), new_tweet(556, "alice", NOW, "two")]))
        recs = [r for p in payloads_from_html(html) for r in records_from_payload(p)]
        self.assertEqual({r["id"]: r["text"] for r in recs}, {"555": text, "556": "two"})

    def test_page_without_posts(self):
        self.assertEqual(payloads_from_html('<html><script>var a = "rest_id"; var b = "x";</script></html>'), [])

    def test_typename_pin(self):
        p = {"instructions": [{"__typename": "TimelinePinEntry", "entry": new_payload([new_tweet(7, "alice", NOW, "pin")])}]}
        self.assertEqual(pinned_ids(p), {"7"})

    def test_snowflake_date(self):
        self.assertEqual(snowflake_time(str(snowflake(NOW))), NOW)
        self.assertEqual(snowflake_time("2117680635561312732").year, 2026)

    def test_dom_record_from_new_markup(self):
        when = NOW - timedelta(hours=1)
        r = normalize_dom_record({
            "id": str(snowflake(when)), "handle": "alice", "text": "x",
            "created_at": when.isoformat().replace("+00:00", "Z"), "quote": None, "links": [], "is_repost": False,
            "counts_label": "208 replies, 398 reposts, 5659 likes, 2696 bookmarks, 621327 views"})
        self.assertEqual((r["replies"], r["reposts"], r["likes"], r["views"]), (208, 398, 5659, 621327))
        self.assertEqual(r["url"], f"https://x.com/alice/status/{snowflake(when)}")
        self.assertEqual(r["created_at"], when.isoformat())
        self.assertEqual(normalize_dom_record({"id": "1", "counts_label": "6042 Likes. Like", "created_at": "x"})["likes"], 6042)
        self.assertEqual(normalize_dom_record({"id": "1", "counts_label": "1.5K Likes. Like", "created_at": "x"})["likes"], 1500)


class TimelineFloorTest(unittest.TestCase):
    def module(self, *tweets):
        return {"entryId": "profile-conversation-1", "content": {"items": [
            {"item": {"itemContent": {"tweet_results": {"result": t}}}} for t in tweets]}}

    def test_old_thread_root_does_not_end_the_window(self):
        """This week's reply to a 3-month-old thread must not look like the timeline reached 3 months ago."""
        old = tweet(1, "alice", NOW - timedelta(days=90), "old root")
        reply = tweet(2, "alice", NOW - timedelta(days=1), "new reply", reply_to="alice")
        newest = tweet(3, "alice", NOW - timedelta(days=2), "post")
        self.assertEqual(timeline_floor(payload([self.module(old, reply)]), "alice", set()), NOW - timedelta(days=1))
        p = payload([self.module(old, reply), {"entryId": "t", "content": {"itemContent": {"tweet_results": {"result": newest}}}}])
        self.assertEqual(timeline_floor(p, "alice", set()), NOW - timedelta(days=2))

    def test_pins_are_not_the_floor(self):
        p = sample_page()  # its pin is 200 days old, and its oldest timeline post 9 days
        self.assertEqual(timeline_floor(p, "alice", {"100"}), NOW - timedelta(days=9))


if __name__ == "__main__":
    unittest.main()
