import unittest

from xweek.digest import build_prompt, link_citations, stats_sections

POSTS = {
    "111": {"id": "111", "handle": "alice", "url": "https://x.com/alice/status/111", "created_at": "2026-10-20T10:00:00+00:00", "likes": 2500},
    "222": {"id": "222", "handle": "bob", "url": "https://x.com/bob/status/222", "created_at": "2026-10-19T10:00:00+00:00", "likes": 40},
    "333": {"id": "333", "handle": "bob", "url": "https://x.com/bob/status/333", "created_at": "2026-10-18T10:00:00+00:00", "likes": 7},
}
TAGS = [
    {"id": "111", "coding": True, "topics": ["Claude Code"], "summary": "Ships desktop multi-session"},
    {"id": "222", "coding": True, "topics": ["Claude Code", "Codex"], "summary": "Prefers Codex for reviews"},
    {"id": "333", "coding": False},
]


class DigestTest(unittest.TestCase):
    def test_links_are_resolved_from_ids_only(self):
        out = link_citations("A ([@alice](#111)), B ([@bob](#999999)).", POSTS)
        self.assertEqual(out, "A ([@alice](https://x.com/alice/status/111)), B (@bob).")

    def test_prompt_groups_coding_posts_by_topic(self):
        p = build_prompt(POSTS, TAGS)
        self.assertIn("### Claude Code (2)", p)
        self.assertIn("[111] @alice 2026-10-20 2.5K: Ships desktop multi-session", p)
        self.assertNotIn("[333]", p)

    def test_stats(self):
        s = stats_sections(POSTS, TAGS, {"alice": {"status": "ok", "kept": 1, "truncated": True},
                                         "bob": {"status": "ok", "kept": 2}, "zed": {"status": "not_found"}})
        self.assertIn("| [@alice](https://x.com/alice) | 1+ |", s)
        self.assertIn("Not crawled: @zed (not_found)", s)


if __name__ == "__main__":
    unittest.main()
