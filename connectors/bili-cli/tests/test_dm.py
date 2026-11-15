"""Restricted DM: guards, ledger, recipient resolution and the send body — no network."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bilicli import dm


def entry(mid, ts, text="hi", sha="x"):
    return {"ts": ts, "mid": str(mid), "sha256": sha, "chars": len(text)}


class Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"code": 0, "data": {"msg_key": 1}}
        self.posts = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data, "headers": headers})
        return Resp(self.payload)


class FakeAPI:
    def __init__(self, relation=(0, 0), users=None, me_mid=42, payload=None):
        self.authed = True
        self.csrf = "csrf-token"
        self.s = FakeSession(payload)
        self._relation = relation
        self._users = users or []
        self._me = me_mid

    def me(self):
        return {"mid": self._me, "isLogin": True}

    def _call(self, name, params=None):
        assert name == "relation"
        attr, be = self._relation
        return {"attribute": attr, "be_relation": {"attribute": be}}

    def search(self, query, kind="video", limit=10):
        return self._users[:limit]


class CheckTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_accepts_a_clean_send(self):
        info = dm.check([], 7, "你好", me_mid=42, now=self.NOW)
        self.assertEqual(info["chars"], 2)
        self.assertEqual(info["remaining_24h"], dm.DM_MAX_PER_DAY - 1)

    def test_rejects_empty_long_and_self(self):
        for args, needle in [(([], 7, "   ", 42), "不能为空"),
                             (([], 7, "x" * 451, 42), "太长"),
                             (([], 42, "hi", 42), "自己")]:
            ledger, mid, text, me = args
            with self.assertRaises(dm.DMPolicyError) as ctx:
                dm.check(ledger, mid, text, me_mid=me, now=self.NOW)
            self.assertIn(needle, str(ctx.exception))

    def test_daily_cap(self):
        ledger = [entry(i, self.NOW - 60 * i) for i in range(dm.DM_MAX_PER_DAY)]
        with self.assertRaises(dm.DMPolicyError) as ctx:
            dm.check(ledger, 99, "hi", me_mid=42, now=self.NOW)
        self.assertIn("上限", str(ctx.exception))

    def test_per_recipient_cooldown(self):
        ledger = [entry(7, self.NOW - 3600)]
        with self.assertRaises(dm.DMPolicyError) as ctx:
            dm.check(ledger, 7, "hi", me_mid=42, now=self.NOW)
        self.assertIn("同一收件人", str(ctx.exception))

    def test_global_min_gap(self):
        ledger = [entry(8, self.NOW - 10)]
        with self.assertRaises(dm.DMPolicyError) as ctx:
            dm.check(ledger, 9, "hi", me_mid=42, now=self.NOW)
        self.assertIn("间隔", str(ctx.exception))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "dm-ledger.json"
        self.patch = mock.patch.object(dm, "LEDGER_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_roundtrip_mode_and_trim(self):
        for i in range(dm.LEDGER_MAX + 5):
            dm.record({"ts": i, "mid": "1"})
        rows = dm.load_ledger()
        self.assertEqual(len(rows), dm.LEDGER_MAX)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_status_counts_only_last_24h(self):
        now = time.time()
        dm.record({"ts": now - 3600, "mid": "1", "ok": True})
        dm.record({"ts": now - 3 * 86400, "mid": "2", "ok": True})
        st = dm.status(now=now)
        self.assertEqual(st["sent_24h"], 1)
        self.assertEqual(st["remaining_24h"], dm.DM_MAX_PER_DAY - 1)
        self.assertEqual(st["recipients_24h"], ["1"])

    def test_entry_never_stores_the_full_text(self):
        e = dm.entry(7, "x" * 900, ok=True, mutual=True, forced=False)
        self.assertEqual(e["chars"], 900)
        self.assertEqual(len(e["preview"]), 80)
        self.assertEqual(len(e["sha256"]), 64)


class ReviewRegressionTests(unittest.TestCase):
    """Regression tests for the four PR-#8 review findings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(dm, "LEDGER_FILE", Path(self.tmp.name) / "dm-ledger.json")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    # F1 (P1): fail closed on unreadable/corrupt ledger
    def test_corrupt_ledger_refuses_send_and_keeps_evidence(self):
        dm.LEDGER_FILE.write_text("{broken", encoding="utf-8")
        with self.assertRaises(dm.DMLedgerError):
            dm.load_ledger()
        with self.assertRaises(dm.DMPolicyError):
            dm.check(dm.load_ledger(), 7, "hi", me_mid=42)
        with self.assertRaises(dm.DMLedgerError):
            dm.reserve(7, "hi", mutual=True, forced=False, me_mid=42)
        self.assertEqual(dm.LEDGER_FILE.read_text(), "{broken")   # not overwritten

    def test_wrong_shape_ledger_refuses_send(self):
        dm.LEDGER_FILE.write_text('{"rows": []}', encoding="utf-8")
        with self.assertRaises(dm.DMLedgerError):
            dm.load_ledger()

    def test_permission_denied_ledger_refuses_send(self):
        dm.LEDGER_FILE.write_text("[]", encoding="utf-8")
        dm.LEDGER_FILE.chmod(0o000)
        self.addCleanup(lambda: dm.LEDGER_FILE.chmod(0o600))
        try:
            dm.LEDGER_FILE.read_text(encoding="utf-8")   # root ignores file permissions
            self.skipTest("root ignores file permissions")
        except PermissionError:
            pass
        with self.assertRaises(dm.DMLedgerError):
            dm.load_ledger()

    def test_missing_ledger_is_first_use(self):
        self.assertEqual(dm.load_ledger(), [])

    def test_status_degrades_loudly(self):
        dm.LEDGER_FILE.write_text("{broken", encoding="utf-8")
        st = dm.status()
        self.assertIn("ledger_error", st)
        self.assertIsNone(st["sent_24h"])

    # F2 (P1): durable reservation under interprocess lock
    def test_reserve_persists_pending_before_send_and_finalizes(self):
        info = dm.reserve(7, "hi", mutual=True, forced=False, me_mid=42, name="a")
        self.assertEqual(info["chars"], 2)
        rows = dm.load_ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "pending")
        self.assertIsNone(rows[0]["ok"])
        self.assertTrue(dm.finalize_sent(7))
        rows = dm.load_ledger()
        self.assertEqual(rows[0]["kind"], "sent")
        self.assertTrue(rows[0]["ok"])

    def test_pending_and_failed_rows_count_toward_quota(self):
        now = time.time()
        dm.LEDGER_FILE.write_text(json.dumps([
            {"ts": now - 10, "mid": "7", "kind": "pending"},
            {"ts": now - 10, "mid": "8", "kind": "failed"},
        ]), encoding="utf-8")
        with self.assertRaises(dm.DMPolicyError) as ctx:
            dm.check(dm.load_ledger(), 9, "hi", me_mid=42, now=now)
        self.assertIn("间隔", str(ctx.exception))   # pending/failed trip the min-gap gate

    def test_reserve_enforces_guards_cross_process_rereads_disk(self):
        # ledger written by "another process" after this one loaded it: reserve re-reads under lock
        dm.LEDGER_FILE.write_text(json.dumps(
            [{"ts": time.time(), "mid": "7", "kind": "sent"}]), encoding="utf-8")
        with self.assertRaises(dm.DMPolicyError) as ctx:
            dm.reserve(7, "hi", mutual=True, forced=False, me_mid=42)
        self.assertIn("同一收件人", str(ctx.exception))
        self.assertEqual(len(dm.load_ledger()), 1)   # nothing appended

    def test_finalize_failed_marks_quota_bearing_row(self):
        dm.reserve(7, "hi", mutual=True, forced=False, me_mid=42)
        self.assertTrue(dm.finalize_failed(7, reason="code=-400"))
        row = dm.load_ledger()[0]
        self.assertEqual(row["kind"], "failed")
        self.assertFalse(row["ok"])
        self.assertEqual(row["reason"], "code=-400")

    # F3 (P2): normalize once — whitespace-only bypass of the cap
    def test_check_counts_stripped_text(self):
        padded = " " * 500 + "x"
        info = dm.check([], 7, padded, me_mid=42)
        self.assertEqual(info["chars"], 1)   # check evaluates the text that would be sent
        with self.assertRaises(dm.DMPolicyError):
            dm.check([], 7, "x" * 451, me_mid=42)

    # F4 (P2): refusals are recorded, audit-only
    def test_record_refused_writes_audit_only_row(self):
        dm.record_refused(7, "hi", reason="未互关", mutual=False, me_mid=42)
        rows = dm.load_ledger()
        self.assertEqual(rows[0]["kind"], "refused")
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(rows[0]["reason"], "未互关")
        # audit-only: does not consume quota or trip the min-gap gate
        info = dm.check(dm.load_ledger(), 8, "hi", me_mid=42,
                        now=rows[0]["ts"] + 1)
        self.assertEqual(info["sent_24h"], 0)


class ResolveTests(unittest.TestCase):
    def test_digits_pass_through(self):
        self.assertEqual(dm.resolve(FakeAPI(), "503383791")["mid"], 503383791)

    def test_exact_name(self):
        api = FakeAPI(users=[{"mid": 7, "uname": "阿张"}, {"mid": 8, "uname": "阿张三"}])
        self.assertEqual(dm.resolve(api, "@阿张"), {"mid": 7, "name": "阿张"})

    def test_ambiguous_and_missing_names(self):
        api = FakeAPI(users=[{"mid": 7, "uname": "同名"}, {"mid": 8, "uname": "同名"}])
        with self.assertRaises(dm.DMPolicyError) as ctx:
            dm.resolve(api, "同名")
        self.assertIn("同名用户", str(ctx.exception))
        with self.assertRaises(dm.DMPolicyError):
            dm.resolve(FakeAPI(users=[]), "不存在")


class RelationTests(unittest.TestCase):
    def test_mutual_requires_both_directions(self):
        self.assertTrue(dm.relation(FakeAPI(relation=(6, 6)), 7)["mutual"])
        self.assertFalse(dm.relation(FakeAPI(relation=(6, 0)), 7)["mutual"])
        self.assertFalse(dm.relation(FakeAPI(relation=(0, 6)), 7)["mutual"])


class SendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(dm, "DEVICE_FILE", Path(self.tmp.name) / "dev")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(dm.traffic, "throttle", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(dm.traffic, "classify", lambda *a, **k: None)
        q.start()
        self.addCleanup(q.stop)

    def test_body_shape(self):
        api = FakeAPI()
        dm.send(api, 777, "晚上好")
        post = api.s.posts[0]
        body = post["data"]
        self.assertIn("/x/im/dm/sendMsg", post["url"])
        self.assertEqual(body["msg[sender_uid]"], "42")
        self.assertEqual(body["msg[receiver_id]"], "777")
        self.assertEqual(json.loads(body["msg[content]"])["content"], "晚上好")
        self.assertEqual(body["csrf"], "csrf-token")
        self.assertTrue(body["msg[dev_id]"].startswith("WEB-"))
        self.assertEqual(post["headers"]["Referer"], "https://message.bilibili.com/")

    def test_device_id_is_stable(self):
        api = FakeAPI()
        dm.send(api, 7, "a")
        dm.send(api, 8, "b")
        ids = {p["data"]["msg[dev_id]"] for p in api.s.posts}
        self.assertEqual(len(ids), 1)

    def test_api_error_becomes_policy_error(self):
        api = FakeAPI(payload={"code": -400, "message": "bad request"})
        with self.assertRaises(dm.DMPolicyError) as ctx:
            dm.send(api, 7, "a")
        self.assertIn("-400", str(ctx.exception))


class CmdDmTests(unittest.TestCase):
    """cmd_dm ordering: reserve -> send -> finalize; refusals recorded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(dm, "LEDGER_FILE", Path(self.tmp.name) / "dm-ledger.json")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def _args(self, **kw):
        from types import SimpleNamespace
        base = dict(to="7", msg="hi", dry=False, status=False,
                    allow_non_mutual=False, yes=True)
        base.update(kw)
        return SimpleNamespace(**base)

    def _run(self, args):
        from bilicli import cli
        order = []
        with mock.patch("bilicli.cli.BiliAPI") as API, \
             mock.patch("bilicli.cli._emit"), \
             mock.patch.object(dm, "resolve", return_value={"mid": 7, "name": "a"}) as m_res, \
             mock.patch.object(dm, "relation", return_value={"attribute": 6, "be_attribute": 6, "mutual": True}), \
             mock.patch.object(dm, "reserve", side_effect=lambda *a, **k: order.append("reserve") or dm.check([], 7, "hi", me_mid=42)), \
             mock.patch.object(dm, "send", side_effect=lambda *a, **k: order.append("send")) as m_send, \
             mock.patch.object(dm, "finalize_sent", side_effect=lambda *a, **k: order.append("finalize_sent")), \
             mock.patch.object(dm, "finalize_failed", side_effect=lambda *a, **k: order.append("finalize_failed")), \
             mock.patch.object(dm, "record_refused", side_effect=lambda *a, **k: order.append("refused")):
            API.return_value.me.return_value = {"mid": 42, "isLogin": True}
            try:
                cli.cmd_dm(args)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, order, m_send

    def test_send_path_order_reserve_send_finalize(self):
        code, order, _ = self._run(self._args())
        self.assertEqual(code, 0)
        self.assertEqual(order, ["reserve", "send", "finalize_sent"])

    def test_failed_send_marks_failed_not_success(self):
        from bilicli import cli
        order = []
        with mock.patch("bilicli.cli.BiliAPI"), mock.patch("bilicli.cli._emit"), \
             mock.patch.object(dm, "resolve", return_value={"mid": 7, "name": "a"}), \
             mock.patch.object(dm, "relation", return_value={"attribute": 6, "be_attribute": 6, "mutual": True}), \
             mock.patch.object(dm, "reserve", return_value=dm.check([], 7, "hi", me_mid=42)), \
             mock.patch.object(dm, "send", side_effect=dm.DMPolicyError("code=-400")), \
             mock.patch.object(dm, "finalize_failed", side_effect=lambda *a, **k: order.append("finalize_failed")), \
             mock.patch.object(dm, "finalize_sent", side_effect=lambda *a, **k: order.append("finalize_sent")), \
             mock.patch.object(dm, "record_refused", side_effect=lambda *a, **k: order.append("refused")):
            with self.assertRaises(SystemExit) as ctx:
                cli.cmd_dm(self._args())
            self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(order, ["finalize_failed"])   # and NO refusal row (pending already ledgered)

    def test_non_mutual_refusal_is_recorded(self):
        from bilicli import cli
        order = []
        with mock.patch("bilicli.cli.BiliAPI"), mock.patch("bilicli.cli._emit"), \
             mock.patch.object(dm, "resolve", return_value={"mid": 7, "name": "a"}), \
             mock.patch.object(dm, "relation", return_value={"attribute": 0, "be_attribute": 6, "mutual": False}), \
             mock.patch.object(dm, "reserve", side_effect=lambda *a, **k: order.append("reserve")), \
             mock.patch.object(dm, "send", side_effect=lambda *a, **k: order.append("send")), \
             mock.patch.object(dm, "finalize_sent", side_effect=lambda *a, **k: order.append("finalize_sent")), \
             mock.patch.object(dm, "record_refused", side_effect=lambda *a, **k: order.append("refused")):
            with self.assertRaises(SystemExit) as ctx:
                cli.cmd_dm(self._args())
            self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(order, ["refused"])   # no reserve/send on refusal

    def test_msg_is_normalized_once_before_everything(self):
        from bilicli import cli
        seen = {}
        with mock.patch("bilicli.cli.BiliAPI"), mock.patch("bilicli.cli._emit"), \
             mock.patch.object(dm, "resolve", return_value={"mid": 7, "name": "a"}), \
             mock.patch.object(dm, "relation", return_value={"attribute": 6, "be_attribute": 6, "mutual": True}), \
             mock.patch.object(dm, "check", side_effect=lambda led, mid, text, **k: (seen.setdefault("check", text), {})[1]), \
             mock.patch.object(dm, "reserve", side_effect=lambda mid, text, **k: (seen.setdefault("reserve", text), {})[1]), \
             mock.patch.object(dm, "send", side_effect=lambda api, mid, text: seen.setdefault("send", text)):
            cli.cmd_dm(self._args(msg="  你好  "))
        self.assertEqual(seen, {"reserve": "你好", "send": "你好"})


if __name__ == "__main__":
    unittest.main()
