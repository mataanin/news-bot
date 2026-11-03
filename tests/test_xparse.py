import unittest
from datetime import timedelta

from xweek.xparse import (is_timeline_url, keep_for_profile, normalize_dom_record, pinned_ids,
                          records_from_payload)
from tests.fixtures import NOW, sample_page


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
                                  "links": [], "likes_label": "1.2K Likes. Like", "reply_to": None,
                                  "is_repost": False})
        self.assertEqual(r["likes"], 1200)
        self.assertTrue(keep_for_profile(r, "alice", NOW - timedelta(days=7)))


if __name__ == "__main__":
    unittest.main()
