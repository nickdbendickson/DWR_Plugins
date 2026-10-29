"""Desktop smoke test: the window serves the bundled web desk, and that bundle runs the shared engine.

Headless and light: no pywebview or GUI needed. Needs Node (>= 18) only for the engine check, which imports the
engine module exactly as the local server hands it to the window.

    python3 crest-desktop/tests/test_smoke.py        # or: npm run test:desktop
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESKTOP = HERE.parent
REPO = DESKTOP.parent
sys.path.insert(0, str(DESKTOP))

import types  # noqa: E402
from unittest import mock  # noqa: E402

from crest.__main__ import Api, serve, web_dir, write_text  # noqa: E402

WEB = web_dir()
ASSET_RE = re.compile(r'(?:src|href)="\./(assets/[^"]+)"')
IMPORT_RE = re.compile(r'(?:from\s*|import\s*\(?\s*)"\./([A-Za-z0-9_.-]+\.js)"')
# Hosts a desk asset must never load from: the app has to work offline apart from basemap tiles.
CDN_RE = re.compile(r"unpkg\.com|cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net|fonts\.googleapis\.com|fonts\.gstatic\.com|esm\.sh|skypack")
TILE_HOSTS = ("basemaps.cartocdn.com", "tile.openstreetmap.org", "arcgisonline.com")


def sha256_lf(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")).hexdigest()


class BundleTests(unittest.TestCase):
    def test_bundle_present_and_complete(self):
        index = WEB / "index.html"
        self.assertTrue(index.is_file(), f"{index} missing: run `npm run build:desktop`")
        refs = ASSET_RE.findall(index.read_text(encoding="utf-8"))
        self.assertTrue(refs, "index.html references no assets")
        for ref in refs:
            self.assertTrue((WEB / ref).is_file(), ref)
        self.assertTrue((WEB / "assets" / "engine.js").is_file())
        # every relative import inside the JS resolves to a bundled file
        for js in (WEB / "assets").glob("*.js"):
            for dep in IMPORT_RE.findall(js.read_text(encoding="utf-8")):
                self.assertTrue((WEB / "assets" / dep).is_file(), f"{js.name} imports missing {dep}")

    def test_no_python_engine_left(self):
        for gone in ("engine.py", "export.py", "samples.py", "ui/index.html"):
            self.assertFalse((DESKTOP / "crest" / gone).exists(), f"crest/{gone} should be gone: one engine only")

    def test_bundle_is_fresh(self):
        info = json.loads((WEB / "build-info.json").read_text(encoding="utf-8"))
        sources = info["sources"]
        self.assertIn("src/lib/crest/engine.ts", sources)
        self.assertIn("src/components/crest/desk.tsx", sources)
        if not (REPO / "src" / "lib" / "crest" / "engine.ts").is_file():
            self.skipTest("repo sources not present (frozen copy)")
        stale = [rel for rel, digest in sources.items() if not (REPO / rel).is_file() or sha256_lf(REPO / rel) != digest]
        self.assertEqual(stale, [], "crest-desktop/crest/web is older than src/: run `npm run build:desktop` and commit it")

    def test_offline_assets(self):
        index = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertNotRegex(index, r'(?:src|href)="(?:https?:)?//', "index.html loads something remote")
        for f in (WEB / "assets").iterdir():
            if f.suffix not in (".js", ".css", ".html"):
                continue
            text = f.read_text(encoding="utf-8")
            self.assertIsNone(CDN_RE.search(text), f"{f.name} loads from a CDN")
            if f.suffix == ".css":
                self.assertNotRegex(text, r"url\(\s*['\"]?(?:https?:)?//", f"{f.name} has a remote url()")
        mapjs = "".join(p.read_text(encoding="utf-8") for p in (WEB / "assets").glob("*.js"))
        self.assertIn("leaflet", mapjs.lower(), "Leaflet is not bundled")
        self.assertIn("draw:created", mapjs, "leaflet-draw is not bundled")
        self.assertTrue(any(h in mapjs for h in TILE_HOSTS), "basemap tiles expected to be the only network use")

    def test_desk_imports_the_engine_module(self):
        engine = (WEB / "assets" / "engine.js").read_text(encoding="utf-8")
        engine_deps = set(IMPORT_RE.findall(engine)) or {"engine.js"}
        index_deps = set(IMPORT_RE.findall((WEB / "assets" / "index.js").read_text(encoding="utf-8")))
        self.assertTrue(engine_deps & index_deps, f"desk ({index_deps}) and engine.js ({engine_deps}) share no module")

    def test_write_text_keeps_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.bdy"
            write_text(p, "a\nb\r\nc")
            self.assertEqual(p.read_bytes(), b"a\nb\r\nc")


class SaveBridgeTests(unittest.TestCase):
    """Api.save_file with a stand-in pywebview: the Save dialog result decides where the export goes."""

    def run_save(self, picked, name="nd00199.bdy", text="a\r\nb"):
        calls = []

        class Win:
            def create_file_dialog(self, kind, **kw):
                calls.append((kind, kw))
                return picked

        fake = types.SimpleNamespace(FileDialog=types.SimpleNamespace(SAVE=30), windows=[])
        api = Api()
        api._window = Win()
        with mock.patch.dict(sys.modules, {"webview": fake}):
            return api.save_file(name, text), calls

    def test_save_writes_picked_path(self):
        with tempfile.TemporaryDirectory() as d:
            target = str(Path(d) / "out.bdy")
            res, calls = self.run_save((target,))
            self.assertEqual(res, {"saved": target})
            self.assertEqual(Path(target).read_bytes(), b"a\r\nb")
            self.assertEqual(calls[0][0], 30)
            self.assertEqual(calls[0][1]["save_filename"], "nd00199.bdy")

    def test_cancel_and_path_stripping(self):
        res, calls = self.run_save(None, name="../../etc/x.csv")
        self.assertEqual(res, {"cancelled": True})
        self.assertEqual(calls[0][1]["save_filename"], "x.csv")

    def test_window_not_exposed_to_js(self):
        self.assertFalse([k for k in vars(Api()) if not k.startswith("_")])


class ServedEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = serve(port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path: str) -> tuple[bytes, str]:
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.read(), r.headers.get("Content-Type", "")

    def test_serves_the_bundle(self):
        body, ctype = self.get("/index.html")
        self.assertEqual(body, (WEB / "index.html").read_bytes())
        self.assertIn("text/html", ctype)
        for ref in ASSET_RE.findall(body.decode("utf-8")):
            data, ctype = self.get("/" + ref)
            self.assertEqual(data, (WEB / ref).read_bytes(), ref)
            if ref.endswith(".js"):
                self.assertIn("javascript", ctype, f"{ref} served as {ctype}; module scripts need a JS type")

    def test_dwr2801_through_the_served_engine(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not found")
        with tempfile.TemporaryDirectory() as d:
            # Fetch engine.js and what it imports from the running server, then run it: the window's engine.
            todo, seen = ["engine.js"], set()
            while todo:
                name = todo.pop()
                if name in seen:
                    continue
                seen.add(name)
                data, _ = self.get(f"/assets/{name}")
                (Path(d) / name).write_bytes(data)
                todo.extend(IMPORT_RE.findall(data.decode("utf-8")))
            script = r"""
const m = await import(process.argv[1]);
const s = m.SAMPLE_DAMS.find((x) => x.nid === "ND00199");
// DWR-2801 as the Breach Runner ran it (src/lib/crest/fixtures.ts) with H 27.50 / V 1615.5 / SA 154.5.
const dam = { ...s, id: "dwr-2801", heightFt: 27.5, storageAcFt: 1615.5, surfaceAreaAc: 154.5, lengthFt: 1700,
  crestFt: 1072.24, streambedFt: 1044.74, spillwayFt: NaN, normalPoolFt: NaN, statedDatum: "NAVD88", demCrestFt: 1071.15,
  failureMode: "piping", method: "froehlich", engine: "anuga", reachKm: undefined, classOfRecord: undefined, sampleNote: undefined };
const a = m.analyze(dam);
const mlm = m.analyze({ ...dam, storageAcFt: 1612.59, method: "macdonald" });
const sample = m.analyze(s);
console.log(JSON.stringify({ peak: a.breach.peakCfs, mlm: mlm.breach.peakCfs, sampleCrest: sample.datum.crestNavdFt,
  sampleHeight: s.heightFt, sampleLabel: sample.screen.label }));
"""
            out = subprocess.run(
                [node, "--input-type=module", "-e", script, (Path(d) / "engine.js").as_uri()],
                capture_output=True, text=True, timeout=60, check=True,
            ).stdout
        r = json.loads(out.strip().splitlines()[-1])
        print(f"\n  DWR-2801 via the served bundle: peak {r['peak']:,.0f} cfs, MacDonald {r['mlm']:,.0f} cfs, "
              f"sample crest {r['sampleCrest']} ft NAVD88 / H {r['sampleHeight']}, screen {r['sampleLabel']!r}")
        self.assertEqual(round(r["peak"]), 28671)  # old Python engine: 20,559
        self.assertLess(abs(r["mlm"] - 41584.7), 1)  # old Python engine: 129,168
        self.assertAlmostEqual(r["sampleCrest"], 1072.24, places=2)  # old sample: 912
        self.assertEqual(r["sampleHeight"], 27.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
