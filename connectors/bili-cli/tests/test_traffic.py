import json
import tempfile
import time
import unittest
from unittest import mock

from bilicli import traffic


class FakeCookies(dict):
    pass


class FakeResponse:
    def __init__(self, body=None, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.cookies = FakeCookies()

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.responses.pop(0)


VIEW_URL = "https://api.bilibili.com/x/web-interface/view?bvid=BV1xx"
OK_VIEW = {"code": 0, "data": {"aid": 1, "title": "t"}}


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        traffic._set_state_dir(self._dir.name)
        self._gap = traffic.MIN_GAP_S
        traffic.MIN_GAP_S = 0.0  # tests must not sleep

    def tearDown(self):
        traffic.MIN_GAP_S = self._gap
        self._dir.cleanup()

    def test_cache_hit_skips_second_network_call(self):
        s = FakeSession([FakeResponse(OK_VIEW)])
        first = traffic.guarded_get(s, VIEW_URL)
        second = traffic.guarded_get(s, VIEW_URL)
        self.assertEqual(len(s.calls), 1)
        self.assertEqual(first["data"]["aid"], 1)
        self.assertTrue(second.get("_cached"))



    def test_cache_key_strips_signature_params(self):
        s = FakeSession([FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, VIEW_URL)
        signed = VIEW_URL + "&wts=1700000000&w_rid=abc123"
        out = traffic.guarded_get(s, signed)
        self.assertEqual(len(s.calls), 1)
        self.assertTrue(out.get("_cached"))

    def test_distinct_params_kwarg_get_distinct_cache_entries(self):
        """Regression (issue #9): query params travel via the requests params= kwarg,
        never inside the URL — different bvids must not share one cache entry."""
        base = "https://api.bilibili.com/x/web-interface/wbi/view"
        body_a = {"code": 0, "data": {"aid": 1, "bvid": "BV1xx", "title": "video A"}}
        body_b = {"code": 0, "data": {"aid": 2, "bvid": "BV1yy", "title": "video B"}}
        s = FakeSession([FakeResponse(body_a), FakeResponse(body_b)])
        out_a = traffic.guarded_get(s, base, params={"bvid": "BV1xx"})
        out_b = traffic.guarded_get(s, base, params={"bvid": "BV1yy"})
        # each call hit the network (no cross-bvid cache hit)
        self.assertEqual(len(s.calls), 2)
        self.assertEqual(out_a["data"]["aid"], 1)
        self.assertEqual(out_b["data"]["aid"], 2)
        self.assertFalse(out_a.get("_cached"))
        self.assertFalse(out_b.get("_cached"))
        # and now both are cached independently: repeat calls stay per-bvid correct
        again_a = traffic.guarded_get(s, base, params={"bvid": "BV1xx"})
        again_b = traffic.guarded_get(s, base, params={"bvid": "BV1yy"})
        self.assertEqual(len(s.calls), 2)
        self.assertEqual(again_a["data"]["aid"], 1)
        self.assertEqual(again_b["data"]["aid"], 2)
        self.assertTrue(again_a.get("_cached"))
        self.assertTrue(again_b.get("_cached"))

    def test_signed_params_kwarg_collides_with_unsigned(self):
        """wts/w_rid inside the params kwarg are stripped: same call, one entry."""
        base = "https://api.bilibili.com/x/web-interface/wbi/view"
        s = FakeSession([FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, base, params={"bvid": "BV1xx"})
        signed = traffic.guarded_get(
            s, base, params={"bvid": "BV1xx", "wts": 1700000000, "w_rid": "abc123"})
        self.assertEqual(len(s.calls), 1)
        self.assertTrue(signed.get("_cached"))

    def test_url_query_and_params_kwarg_merge_in_key(self):
        """A param present in both URL query and kwarg is one key; different values differ."""
        base = "https://api.bilibili.com/x/web-interface/view?bvid=BV1xx"
        s = FakeSession([FakeResponse(OK_VIEW), FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, base, params={"bvid": "BV1xx"})
        out = traffic.guarded_get(s, base, params={"bvid": "BV1xx", "other": "1"})
        self.assertEqual(len(s.calls), 2)   # different merged query -> different entry
        self.assertFalse(out.get("_cached"))

    def test_cache_keys_include_sorted_query(self):
        class S:
            cookies = FakeCookies()
        k = traffic._cache_key(
            S(), "https://api.bilibili.com/x/web-interface/wbi/view",
            params={"bvid": "BV1yy", "aid": "5"})
        self.assertIn("aid=5&bvid=BV1yy", k)
        self.assertNotIn("wts", k)

    def test_api_level_two_bvids_no_cross_cache(self):
        """Regression (issue #9), api layer: consecutive view calls for two bvids
        return each video's own data (mock session, no network)."""
        from bilicli.api import BiliAPI

        api = BiliAPI.__new__(BiliAPI)          # skip __init__ (no creds/network)
        api._wbi_key = "k" * 32                 # pre-seed: no nav fetch
        api.csrf = ""
        api.authed = False

        vid_a = {"code": 0, "data": {"aid": 1, "bvid": "BV1xx", "cid": 111, "title": "A"}}
        vid_b = {"code": 0, "data": {"aid": 2, "bvid": "BV1yy", "cid": 222, "title": "B"}}
        s = FakeSession([FakeResponse(vid_a), FakeResponse(vid_b)])
        api.s = s
        with mock.patch("bilicli.api.BiliAPI._ensure_buvid"):
            out_a = api.video("BV1xx")
            out_b = api.video("BV1yy")
        self.assertEqual(out_a["aid"], 1)
        self.assertEqual(out_b["aid"], 2)
        # the two requests went out with their own bvid params
        self.assertEqual([c[1].get("params", {}).get("bvid") for c in s.calls],
                         ["BV1xx", "BV1yy"])
        # and a third call for the first bvid is a cache HIT with the first video's data
        out_again = api.video("BV1xx")
        self.assertEqual(len(s.calls), 2)
        self.assertEqual(out_again["aid"], 1)

    def test_ttl_expiry_refetches(self):
        s = FakeSession([FakeResponse(OK_VIEW), FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, VIEW_URL)
        with mock.patch("bilicli.traffic.time.time", return_value=time.time() + 25 * 3600):
            out = traffic.guarded_get(s, VIEW_URL)
        self.assertEqual(len(s.calls), 2)
        self.assertNotIn("_stale", out)

    def test_http_412_arms_breaker_then_blocks(self):
        s = FakeSession([FakeResponse(status=412)])
        with self.assertRaises(traffic.GuardBlocked):
            traffic.guarded_get(s, VIEW_URL)
        state = traffic.api_state()
        self.assertTrue(state["banned"])
        self.assertEqual(state["ban_minutes_left"], 5)
        with self.assertRaises(traffic.GuardBlocked) as cm:  # no cache to carry it
            traffic.guarded_get(s, VIEW_URL)
        self.assertIn("pause active", str(cm.exception))

    def test_stale_metadata_serves_during_ban(self):
        s = FakeSession([FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, VIEW_URL)   # fresh cache entry (one network call)
        # age it past TTL but inside the 7-day stale window, then arm the breaker
        store = json.loads(traffic.CACHE_FILE.read_text())
        store[next(iter(store))]["at"] = time.time() - 25 * 3600
        traffic._write_json(traffic.CACHE_FILE, store)
        traffic._note_ban("HTTP 412", "/x/web-interface/view")
        out = traffic.guarded_get(s, VIEW_URL)   # no response queued: must NOT hit the network
        self.assertEqual(len(s.calls), 1)
        self.assertTrue(out.get("_stale"))

    def test_interface_ban_code_arms_breaker(self):
        s = FakeSession([FakeResponse({"code": -412, "message": "请求被拦截"})])
        with self.assertRaises(traffic.GuardBlocked):
            traffic.guarded_get(s, VIEW_URL)
        self.assertTrue(traffic.api_state()["banned"])

    def test_streak_escalation_5_10_20_30(self):
        mins = [traffic._note_ban("HTTP 412", "/p") for _ in range(5)]
        self.assertEqual(mins, [5, 10, 20, 30, 30])

    def test_success_clears_streak_on_same_endpoint(self):
        traffic._note_ban("HTTP 412", "/x/web-interface/view")
        traffic._write_json(traffic.BAN_FILE, {**traffic.ban_state(), "until": 0})
        s = FakeSession([FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, VIEW_URL)
        self.assertEqual(traffic.ban_state().get("streak"), 0)

    def test_non_bilibili_hosts_bypass_guard(self):
        s = FakeSession([FakeResponse({"code": 0})])
        out = traffic.guarded_get(s, "https://example.com/api", params=None)
        self.assertEqual(len(s.calls), 1)
        self.assertFalse(out.get("_cached"))

    def test_lru_trim_keeps_400(self):
        store = {f"k{i}": {"at": i, "data": OK_VIEW} for i in range(450)}
        traffic._write_json(traffic.CACHE_FILE, store)
        s = FakeSession([FakeResponse(OK_VIEW)])
        traffic.guarded_get(s, VIEW_URL + "&x=1")
        saved = json.loads(traffic.CACHE_FILE.read_text())
        self.assertLessEqual(len(saved), 400)
        self.assertNotIn("k0", saved)
        self.assertIn("k449", saved)


if __name__ == "__main__":
    unittest.main()
