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
        cls.env = mock.patch.dict(os.environ, {"HOME": str(cls.home), "CREST_WORK": "", "CREST_WORK_ROOTS": ""})
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

    def test_index_carries_token(self):
        status, data = self.req("GET", "/index.html", token=False)
        self.assertEqual(status, 200)
        self.assertIn(f'<meta name="crest-token" content="{m.API_TOKEN}">'.encode(), data)

    def test_token_is_random_and_long(self):
        self.assertGreaterEqual(len(m.API_TOKEN), 32)

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
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "CREST_WORK": "", "CREST_WORK_ROOTS": ""})
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


if __name__ == "__main__":
    unittest.main()
