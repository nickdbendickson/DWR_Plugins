"""Local API hardening: per-launch token, Host/Origin/Content-Type checks, work folder confined to allowed roots.

    python3 crest-desktop/tests/test_local_api.py
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DESKTOP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DESKTOP))

import crest.__main__ as m  # noqa: E402
from crest.qgis_run import WorkFolderError, resolve_work  # noqa: E402


class LocalApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls.tmp.name) / "home"
        cls.home.mkdir()
        cls.env = mock.patch.dict(os.environ, {"HOME": str(cls.home), "USERPROFILE": str(cls.home), "CREST_WORK": "", "CREST_WORK_ROOTS": ""})
        cls.env.start()
        web = Path(cls.tmp.name) / "web"
        web.mkdir()
        (web / "index.html").write_text("<!doctype html><html><head><title>t</title></head><body></body></html>")
        cls.server = m.serve(web, port=0)
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.env.stop()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, token=True, host=None, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h[m.TOKEN_HEADER] = m.API_TOKEN
        if body is not None and ctype:
            h["Content-Type"] = ctype
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def test_index_has_no_token(self):
        # The token must never be written into the served HTML; a saved copy of the page could otherwise carry it.
        status, data = self.req("GET", "/index.html", token=False)
        self.assertEqual(status, 200)
        self.assertNotIn(b"crest-token", data)
        self.assertNotIn(m.API_TOKEN.encode(), data)

    def test_no_endpoint_hands_out_the_token(self):
        # Security audit 2026-11-18: GET /api/token gave the token to any loopback peer that sent no Origin header
        # (another process or OS user). No path answers with the token now; the desk gets it from the URL fragment.
        for path in ("/api/token", "/", "/index.html", "/api/job?id=x", "/api/nope"):
            for headers in ({}, {"Origin": f"http://127.0.0.1:{self.port}"}):
                status, data = self.req("GET", path, token=False, headers=headers)
                self.assertNotIn(m.API_TOKEN.encode(), data, path)
                if path.startswith("/api/"):
                    self.assertEqual(status, 403, path)

    def test_desk_url_carries_token_in_fragment_only(self):
        url = m.desk_url(self.port)
        base, _, frag = url.partition("#")
        self.assertEqual(base, f"http://127.0.0.1:{self.port}/index.html")
        self.assertEqual(frag, f"crest-token={m.API_TOKEN}")

    def test_token_is_random_and_long(self):
        self.assertGreaterEqual(len(m.API_TOKEN), 32)

    def test_security_headers_on_every_response(self):
        # A bundle without security-headers.json (this test's) gets the built-in policy; pages, files, API answers
        # and refusals all carry it.
        for path in ("/index.html", "/api/job?id=x", "/nope.js"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", m.TOKEN_HEADER: m.API_TOKEN})
            r = conn.getresponse()
            r.read()
            got = {k.lower(): v for (k, v) in r.getheaders()}
            conn.close()
            self.assertIn("script-src 'self'", got.get("content-security-policy", ""), path)
            script = [d for d in got["content-security-policy"].split(";") if d.strip().startswith("script-src")][0]
            self.assertNotIn("unsafe", script, path)
            self.assertEqual(got.get("x-content-type-options"), "nosniff", path)

    def test_security_headers_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(m.load_security_headers(root), m._FALLBACK_SECURITY_HEADERS)
            (root / "security-headers.json").write_text(
                json.dumps({"Content-Security-Policy": "default-src 'self'", "X-Test": "1", "X-Bad": "a\r\nSet-Cookie: x=1"})
            )
            got = m.load_security_headers(root)
            self.assertEqual(got, {"Content-Security-Policy": "default-src 'self'", "X-Test": "1"})
            with mock.patch.dict(os.environ, {"CREST_NO_CSP": "1"}):
                self.assertEqual(m.load_security_headers(root), {"X-Test": "1"})
            (root / "security-headers.json").write_text("not json")
            self.assertEqual(m.load_security_headers(root), m._FALLBACK_SECURITY_HEADERS)

    def test_single_cache_control_header(self):
        # Cache-Control: no-store is added by the end_headers() override; _send must not also emit it,
        # or every API/static response carries the header twice.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/index.html", headers={"Host": f"127.0.0.1:{self.port}"})
        r = conn.getresponse()
        r.read()
        cache = [v for (k, v) in r.getheaders() if k.lower() == "cache-control"]
        conn.close()
        self.assertEqual(cache, ["no-store"], f"exactly one Cache-Control header, got {cache}")

    def test_post_without_token_rejected(self):
        status, _ = self.req("POST", "/api/folder", {"dam": {"id": "x"}}, token=False)
        self.assertEqual(status, 403)

    def test_post_wrong_token_rejected(self):
        status, _ = self.req("POST", "/api/folder", {"dam": {"id": "x"}}, token=False, headers={m.TOKEN_HEADER: "nope"})
        self.assertEqual(status, 403)

    def test_text_plain_rejected(self):
        status, _ = self.req("POST", "/api/folder", {"dam": {"id": "x"}}, ctype="text/plain")
        self.assertEqual(status, 415)

    def test_foreign_origin_rejected(self):
        status, _ = self.req("POST", "/api/folder", {"dam": {"id": "x"}}, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_rebinding_host_rejected(self):
        for path in ("/api/job?id=x", "/index.html"):
            status, _ = self.req("GET", path, host=f"evil.example:{self.port}")
            self.assertEqual(status, 403, path)

    def _raw_post(self, extra_headers, body: bytes | None):
        """POST /api/folder with headers set by hand (to send a chunked or malformed framing the req() helper
        can't). Returns the status code."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/folder", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader(m.TOKEN_HEADER, m.API_TOKEN)
        conn.putheader("Content-Type", "application/json")
        for key, value in extra_headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        if body is not None:
            conn.send(body)
        r = conn.getresponse()
        status = r.status
        r.read()
        conn.close()
        return status

    def test_chunked_body_rejected(self):
        # A chunked body would read as empty and start a job with defaults; it must be a 400 instead.
        self.assertEqual(self._raw_post({"Transfer-Encoding": "chunked"}, b"0\r\n\r\n"), 400)

    def test_bad_content_length_rejected(self):
        # A non-numeric Content-Length must be a 400, not an unhandled exception in the handler thread.
        self.assertEqual(self._raw_post({"Content-Length": "not-a-number"}, None), 400)

    def test_job_get_needs_token(self):
        status, _ = self.req("GET", "/api/job?id=x", token=False)
        self.assertEqual(status, 403)
        status, _ = self.req("GET", "/api/job?id=x")
        self.assertEqual(status, 404)

    def test_cancel_needs_token(self):
        status, _ = self.req("POST", "/api/cancel?id=x", token=False)
        self.assertEqual(status, 403)

    def test_localhost_host_and_own_origin_allowed(self):
        status, _ = self.req("GET", "/api/job?id=x", host=f"localhost:{self.port}",
                             headers={"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 404)

    def test_work_outside_root_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        status, data = self.req("POST", "/api/folder", {"dam": {"id": "x"}, "work": str(outside)})
        self.assertEqual(status, 400, data)
        self.assertFalse(outside.exists())

    def test_work_inside_root_accepted(self):
        with mock.patch.object(m, "start_job", return_value={"id": "j"}) as sj:
            status, _ = self.req("POST", "/api/folder", {"dam": {"id": "x"}, "work": str(self.home / "CrestDams" / "a")})
        self.assertEqual(status, 200)
        self.assertEqual(sj.call_args.args[3], str((self.home / "CrestDams" / "a").resolve()))


class ResolveWorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home), "CREST_WORK": "", "CREST_WORK_ROOTS": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_default(self):
        self.assertIsNone(resolve_work(None))
        self.assertIsNone(resolve_work("  "))

    def test_dotdot_escape(self):
        with self.assertRaises(WorkFolderError):
            resolve_work(str(self.home / "CrestDams" / ".." / "elsewhere"))

    def test_relative_goes_under_default_root(self):
        self.assertEqual(resolve_work("proj"), str((self.home / "CrestDams" / "proj").resolve()))
        with self.assertRaises(WorkFolderError):
            resolve_work("../x")

    def test_symlink_escape(self):
        (self.home / "CrestDams").mkdir()
        target = self.home / "secret"
        target.mkdir()
        (self.home / "CrestDams" / "link").symlink_to(target)
        with self.assertRaises(WorkFolderError):
            resolve_work(str(self.home / "CrestDams" / "link"))

    def test_extra_roots(self):
        extra = self.home / "data"
        with mock.patch.dict(os.environ, {"CREST_WORK_ROOTS": str(extra)}):
            self.assertEqual(resolve_work(str(extra / "a")), str((extra / "a").resolve()))
        with mock.patch.dict(os.environ, {"CREST_WORK": str(extra)}):
            self.assertEqual(resolve_work(str(extra)), str(extra.resolve()))

    def test_non_string(self):
        with self.assertRaises(WorkFolderError):
            resolve_work(["/etc"])


class OpenExternalTests(unittest.TestCase):
    """Api.open_external hands only http/https links to the OS browser; the nav guard routes every off-origin
    link through it, so the desk window never navigates to an outside site."""

    @mock.patch.object(m.webbrowser, "open")
    def test_http_and_https_open_in_browser(self, wb):
        api = m.Api()
        self.assertEqual(api.open_external("https://usgs.gov/x"), {"opened": "https://usgs.gov/x"})
        self.assertEqual(api.open_external("http://example.com/"), {"opened": "http://example.com/"})
        self.assertEqual([c.args[0] for c in wb.call_args_list], ["https://usgs.gov/x", "http://example.com/"])

    @mock.patch.object(m.webbrowser, "open")
    def test_non_web_schemes_refused(self, wb):
        api = m.Api()
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "mailto:a@b.c", "", "nope"):
            self.assertIn("error", api.open_external(bad), bad)
        wb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
