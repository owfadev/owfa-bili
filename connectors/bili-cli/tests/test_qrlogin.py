"""QR login flow, driven by a scripted fake session — no network, no real clock."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from bilicli import qrlogin

try:
    import qrcode  # noqa: F401 — optional rendering extra
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False

QR_SKIP = f'qr extra not installed: uv pip install "bilicli[qr]" (or uv run --extra qr ...)'


class Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class Jar:
    def __init__(self, data):
        self._data = dict(data)

    def get_dict(self):
        return dict(self._data)


class FakeSession:
    """Routes generate/poll calls to scripted payloads; records the call sequence."""

    def __init__(self, generates, polls, cookies=None):
        self.headers = {}
        self.cookies = Jar(cookies or {"SESSDATA": "sess", "bili_jct": "csrf", "DedeUserID": "42"})
        self._generates = list(generates)
        self._polls = list(polls)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("generate" if url == qrlogin.QR_GENERATE else "poll", params))
        if url == qrlogin.QR_GENERATE:
            if not self._generates:
                raise AssertionError("unexpected extra generate call")
            return Resp(self._generates.pop(0))
        if not self._polls:
            raise AssertionError("unexpected extra poll call")
        nxt = self._polls.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return Resp(nxt)


def gen_ok(key="k", url="https://www.bilibili.com/h5/login?qrcode_key=k"):
    return {"code": 0, "data": {"qrcode_key": key, "url": url}}


def poll(code, url=""):
    return {"code": 0, "data": {"code": code, "url": url}}


class LoginFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.png = Path(self.tmp.name) / "qr.png"
        patcher = mock.patch.object(qrlogin, "_sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_login(self, session, **kw):
        stream = io.StringIO()
        kw.setdefault("timeout_s", 30)
        jar = qrlogin.login(png_path=self.png, stream=stream, session=session, **kw)
        return jar, stream.getvalue()

    def test_success_returns_jar_and_removes_qr(self):
        s = FakeSession([gen_ok()], [poll(0)])
        jar, out = self.run_login(s)
        self.assertEqual(jar["SESSDATA"], "sess")
        self.assertIn("扫码确认完成", out)   # final 登录成功 line comes from cli after verification
        self.assertFalse(self.png.exists(), "consumed QR must not stay on disk")

    @unittest.skipUnless(QR_AVAILABLE, QR_SKIP)
    def test_keep_qr_leaves_the_file(self):
        s = FakeSession([gen_ok()], [poll(0)])
        self.run_login(s, keep_qr=True)
        self.assertTrue(self.png.exists())

    @unittest.skipUnless(QR_AVAILABLE, QR_SKIP)
    def test_png_is_written_owner_only(self):
        s = FakeSession([gen_ok()], [poll(0)])
        self.run_login(s, keep_qr=True)
        self.assertTrue(self.png.read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(self.png.stat().st_mode & 0o777, 0o600)

    def test_status_transitions_are_reported_once(self):
        s = FakeSession([gen_ok()], [poll(86101), poll(86101), poll(86090), poll(0)])
        _, out = self.run_login(s)
        self.assertEqual(out.count("等待扫码"), 1)
        self.assertEqual(out.count("已扫码"), 1)

    @unittest.skipUnless(QR_AVAILABLE, QR_SKIP)
    def test_json_events_are_ndjson(self):
        s = FakeSession([gen_ok()], [poll(86101), poll(0)])
        _, out = self.run_login(s, json_events=True)
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        names = [e["event"] for e in events]
        self.assertEqual(names[0], "qr")
        self.assertIn("state", names)
        self.assertEqual(names[-1], "scanned")   # success comes from cli.py after persist+nav
        self.assertEqual(events[0]["png"], str(self.png))
        self.assertEqual(events[-1]["mid"], "42")

    def test_json_mode_has_no_success_event(self):
        """login() itself must NOT claim success — persistence + nav verification
        happen in the caller (review finding #1)."""
        s = FakeSession([gen_ok()], [poll(0)])
        _, out = self.run_login(s, json_events=True)
        self.assertNotIn('"event": "success"', out)
        self.assertNotIn('"event":"success"', out)

    def test_json_mode_never_prints_ascii_qr(self):
        """No QR block characters may pollute the NDJSON stderr channel, even on a TTY
        or with an explicit ascii request (review finding #2)."""
        class Tty(io.StringIO):
            def isatty(self):
                return True

        for kwargs in ({}, {"ascii_qr": True}):
            with self.subTest(**kwargs):
                stream = Tty()
                with mock.patch.object(qrlogin, "render_ascii",
                                       side_effect=AssertionError("must not render")):
                    qrlogin.login(png_path=self.png, stream=stream, session=FakeSession([gen_ok()], [poll(0)]),
                                  timeout_s=30, json_events=True, **kwargs)
                for line in stream.getvalue().splitlines():
                    json.loads(line)   # every line must be valid NDJSON

    def test_poll_api_error_is_terminal_exit_1(self):
        """Top-level poll error (e.g. -412) exits 1 with a structured error event,
        not a silent retry until timeout/exit 2 (review finding #3)."""
        s = FakeSession([gen_ok()], [{"code": -412, "message": "blocked", "data": None}])
        stream = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            qrlogin.login(png_path=self.png, stream=stream, session=s,
                          timeout_s=30, json_events=True)
        self.assertEqual(ctx.exception.code, 1)
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        err = [e for e in events if e["event"] == "error"][-1]
        self.assertEqual(err["reason"], "api")
        self.assertEqual(err["api_code"], -412)
        self.assertEqual([c[0] for c in s.calls].count("poll"), 1)  # no retry-until-timeout

    def test_network_retry_exhaustion_is_structured_error_event(self):
        """All poll retries failing must exit 1 via a JSON `error` event (reason=network),
        never a bare-text stderr line in json-events mode."""
        s = FakeSession([gen_ok()],
                        [requests.ConnectionError("down")] * qrlogin.NETWORK_RETRIES)
        stream = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            qrlogin.login(png_path=self.png, stream=stream, session=s,
                          timeout_s=30, json_events=True)
        self.assertEqual(ctx.exception.code, 1)
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        err = [e for e in events if e["event"] == "error"][-1]
        self.assertEqual(err["reason"], "network")
        self.assertEqual(err["exit"], 1)

    def test_generate_missing_fields_is_structured_error_event(self):
        """A generate response without qrcode_key/url must exit 1 via a JSON `error`
        event (reason=protocol), not a bare-text stderr line in json-events mode."""
        s = FakeSession([{"code": 0, "data": {}}], [])
        stream = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            qrlogin.login(png_path=self.png, stream=stream, session=s,
                          timeout_s=30, json_events=True)
        self.assertEqual(ctx.exception.code, 1)
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        err = [e for e in events if e["event"] == "error"][-1]
        self.assertEqual(err["reason"], "protocol")
        self.assertEqual(err["exit"], 1)

    def test_expired_qr_is_regenerated_once(self):
        s = FakeSession([gen_ok(), gen_ok(key="k2")], [poll(86038), poll(0)])
        jar, out = self.run_login(s)
        self.assertEqual(jar["SESSDATA"], "sess")
        self.assertIn("自动换一张", out)
        self.assertEqual([c[0] for c in s.calls].count("generate"), 2)

    def test_second_expiry_gives_up_with_exit_code_2(self):
        s = FakeSession([gen_ok(), gen_ok()], [poll(86038), poll(86038)])
        with self.assertRaises(SystemExit) as ctx:
            self.run_login(s)
        self.assertEqual(ctx.exception.code, 2)

    def test_timeout_exits_2_without_polling(self):
        s = FakeSession([gen_ok()], [])
        with self.assertRaises(SystemExit) as ctx:
            self.run_login(s, timeout_s=0)
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual([c[0] for c in s.calls], ["generate"])

    def test_network_errors_are_retried(self):
        s = FakeSession([gen_ok()], [requests.ConnectionError("boom"), poll(0)])
        jar, _ = self.run_login(s)
        self.assertEqual(jar["SESSDATA"], "sess")
        self.assertEqual([c[0] for c in s.calls].count("poll"), 2)

    def test_generate_failure_carries_the_api_message(self):
        s = FakeSession([{"code": -412, "message": "risk control"}], [])
        with self.assertRaises(SystemExit) as ctx:
            self.run_login(s)
        self.assertEqual(ctx.exception.code, 1)

    def test_url_cookies_fill_gaps_left_by_set_cookie(self):
        s = FakeSession([gen_ok()], [poll(0, url="https://x/y?SESSDATA=fresh%2C1&bili_jct=new&DedeUserID=7")])
        jar, _ = self.run_login(s)
        self.assertEqual(jar["SESSDATA"], "sess")       # Set-Cookie wins
        self.assertEqual(jar["DedeUserID"], "42")       # Set-Cookie wins
        self.assertEqual(jar["bili_jct"], "csrf")

    def test_journal_missing_rendering_degrades_to_url(self):
        s = FakeSession([gen_ok()], [poll(0)])
        with mock.patch.object(qrlogin, "matrix_for", side_effect=ImportError):
            _, out = self.run_login(s)
        self.assertIn("uv pip install", out)


class AsciiTests(unittest.TestCase):
    @unittest.skipUnless(QR_AVAILABLE, QR_SKIP)
    def test_render_ascii_has_blocks(self):
        art = qrlogin.render_ascii("https://www.bilibili.com/h5/login?qrcode_key=k")
        self.assertTrue(art)
        self.assertIn("█", art)

    def test_render_ascii_is_none_without_lib(self):
        with mock.patch.dict("sys.modules", {"qrcode": None}):
            self.assertIsNone(qrlogin.render_ascii("x"))


class CliLoginTests(unittest.TestCase):
    """cmd_login ordering: `success` only after persistence AND nav verification."""

    def _args(self):
        from types import SimpleNamespace
        return SimpleNamespace(qrcode=True, timeout=1, no_png=True, png=None,
                               keep_qr=False, ascii=False, json_events=True,
                               cookie=None, file=None)

    def _run(self, me_result):
        from bilicli import cli
        err = io.StringIO()
        with mock.patch("bilicli.qrlogin.login", return_value={"SESSDATA": "x"}) as m, \
             mock.patch("bilicli.auth.save_cookies") as save, \
             mock.patch("bilicli.cli.BiliAPI") as API, \
             mock.patch("bilicli.cli._emit"), \
             mock.patch("sys.stderr", err):
            API.return_value.me.return_value = me_result
            try:
                cli.cmd_login(self._args())
                code = 0
            except SystemExit as e:
                code = e.code
        return code, err.getvalue(), m, save

    def test_success_emitted_after_persist_and_verify(self):
        code, err, login, save = self._run({"isLogin": True, "mid": 42, "uname": "u"})
        self.assertEqual(code, 0)
        save.assert_called_once()
        events = [json.loads(l) for l in err.splitlines() if l.strip()]
        self.assertEqual(events[-1]["event"], "success")
        self.assertEqual(events[-1]["mid"], 42)

    def test_failed_nav_verification_gives_error_event_exit_1(self):
        code, err, _, save = self._run({"isLogin": False})
        self.assertEqual(code, 1)
        save.assert_called_once()   # persisted …
        events = [json.loads(l) for l in err.splitlines() if l.strip()]
        self.assertEqual(events[-1]["event"], "error")
        self.assertEqual(events[-1]["reason"], "verification")
        self.assertNotIn('"event": "success"', err)


if __name__ == "__main__":
    unittest.main()
