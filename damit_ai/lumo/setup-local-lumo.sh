#!/bin/zsh
# ============================================================================
# setup-local-lumo.sh v3.3 — local Ollama auditing for the dam suite (macOS)
#
#   NO sudo · NO Homebrew. Prefers your existing conda (the nd-dam-suite one)
#   for the Python side; falls back to downloading wheels only without conda.
#
# Fixes vs v3:
#   - DMG install: the mount point was searched with `grep -B30 ... ^/Volumes/`,
#     which never matches `hdiutil info` output, so the DMG route always died.
#   - PyMuPDF: the "abi3 = any Python" logic picked the newest wheel without
#     checking its minimum Python (PyMuPDF 2.x is cp311-abi3, needs 3.11+;
#     macOS /usr/bin/python3 is 3.9). The manual fallback then called `die`,
#     which aborted the whole install before the tools and aliases were written.
#     MAJOR_VER / MYPYVER / MY_MAJOR were never set or used consistently.
#   - pypdf fallback: f["filename"].ends(".whl") raised AttributeError inside
#     try/except, so pypdf was silently never installed.
#   - ~/.zshrc no longer exports PYTHONPATH globally (it leaked into every
#     Python, including QGIS started from a terminal).
#   - Model and num_ctx chosen from installed RAM; KV cache quantized so the
#     model stays 100% on the GPU.
#   - Persona says QGIS 3.34 / Python 3.12 (your conda env), not 3.28 / 3.9.
#   - serve.log kept; PDF support is optional in the final checks.
#   - Adds suite_check.py: runs the plugin tests, Breach Runner and crest
#     in your conda envs and has the model explain failures.
#   - Adds dam_chain.py: runs both plugin sets headless (self-tests, and
#     Screening -> Runner -> Hazard for a list of dams).
#
# Managed Mac with Cortex XDR: a self-downloading script and an unsigned
# binary run from your home folder are what EDR flags. Ask IT first; do not
# try to get around it.
#
# Usage:
#   zsh setup-local-lumo.sh                  # interactive
#   zsh setup-local-lumo.sh --small          # force qwen3:14b (qwen3:8b on an Intel Mac)
#   zsh setup-local-lumo.sh --model NAME     # any Ollama model tag
#   zsh setup-local-lumo.sh --skip-scripts   # don't rewrite the Python tools
# ============================================================================

set -u
LOCAL_AI="$HOME/local-ai"
OLLAMA_BIN_DIR="$LOCAL_AI/bin"
OLLAMA_EXE="$OLLAMA_BIN_DIR/Ollama.app/Contents/Resources/ollama"
MODELS_DIR="$LOCAL_AI/models"
PYLIBS_DIR="$LOCAL_AI/pylibs"
DOWNLOADS="$HOME/Downloads"
TMPDIR_L="$(mktemp -d)"
ZSHRC="$HOME/.zshrc"
MARKER="# ---- Local AI (no-sudo install) ----"
LUMO_ENV="ndlumo"

SMALL_MODEL=0; REWRITE_SCRIPTS=1; FORCE_MODEL=""
while (( $# )); do
  case "$1" in
    --small) SMALL_MODEL=1 ;;
    --skip-scripts) REWRITE_SCRIPTS=0 ;;
    --model) shift; FORCE_MODEL="${1:-}" ;;
  esac
  shift
done

say()  { printf '\n\033[1;35m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32mOK\033[0m  %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m   %s\n' "$*"; }
fail() { printf '  \033[1;31mX\033[0m   %s\n' "$*"; }
die()  { fail "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

wait_for_file() { # $1=path $2=label
  [[ -f "$1" ]] && return 0
  warn "Download '$2' in your browser and save as:"
  warn "    $1"
  while [[ ! -f "$1" ]]; do printf '\r  waiting...' ; sleep 3 ; done
  printf '\n' ; ok "$2 present"
}

is_zip()  { file -b "$1" | grep -qi "zip archive" ; }
is_html() { file -b "$1" | grep -qiE "html|ascii text" && head -c 200 "$1" | grep -qi "<" ; }

# ===========================================================================
say "Step 1/9 — Directory skeleton"
mkdir -p "$LOCAL_AI" "$OLLAMA_BIN_DIR" "$MODELS_DIR" "$PYLIBS_DIR" "$LOCAL_AI/reports"
ok "~/local-ai ready"

# ===========================================================================
say "Step 2/9 — Ollama (download + install, no sudo)"
if [[ -x "$OLLAMA_EXE" ]]; then
  ok "Ollama already installed"
else
  # Bundled zip first (damit_ai/offline/ollama, checked against its SHA256SUMS): no download.
  BUNDLED_OLLAMA="${0:A:h}/../offline/ollama/Ollama-darwin.zip"
  if [[ -f "$BUNDLED_OLLAMA" ]]; then
    want=$(awk '$2 ~ /Ollama-darwin.zip$/ {print $1}' "${BUNDLED_OLLAMA:h}/SHA256SUMS" 2>/dev/null)
    got=$(shasum -a 256 "$BUNDLED_OLLAMA" | awk '{print $1}')
    if [[ -n "$want" && "$want" == "$got" ]]; then
      cp "$BUNDLED_OLLAMA" "$TMPDIR_L/Ollama.zip"; ok "using the bundled Ollama zip (sha256 checked)"
    else
      warn "bundled Ollama zip does not match its SHA256SUMS; ignoring it"
    fi
  fi
  URL="https://ollama.com/download/Ollama-darwin.zip"
  if [[ ! -f "$TMPDIR_L/Ollama.zip" ]]; then
    say "Trying direct download: $URL"
    curl -fL --connect-timeout 20 -o "$TMPDIR_L/Ollama.zip" "$URL" 2>/dev/null
  fi
  if [[ -f "$TMPDIR_L/Ollama.zip" ]] && is_zip "$TMPDIR_L/Ollama.zip"; then
    ok "downloaded Ollama zip ($(du -h "$TMPDIR_L/Ollama.zip" | cut -f1))"
  else
    rm -f "$TMPDIR_L/Ollama.zip"
    warn "direct download failed or returned HTML — browser fallback"
    if [[ -f "$DOWNLOADS/Ollama-darwin.zip" ]]; then
      cp "$DOWNLOADS/Ollama-darwin.zip" "$TMPDIR_L/Ollama.zip"
    elif [[ -f "$DOWNLOADS/Ollama.dmg" || -f "$DOWNLOADS/ollama.dmg" ]]; then
      true  # handled by the DMG branch below
    else
      wait_for_file "$DOWNLOADS/Ollama-darwin.zip" "Ollama-darwin.zip (https://ollama.com/download → macOS)"
      cp "$DOWNLOADS/Ollama-darwin.zip" "$TMPDIR_L/Ollama.zip"
    fi
  fi

  if [[ -f "$TMPDIR_L/Ollama.zip" ]]; then
    ( cd "$TMPDIR_L" && unzip -q Ollama.zip ) || die "unzip failed"
    [[ -d "$TMPDIR_L/Ollama.app" ]] || die "zip had no Ollama.app — likely an HTML redirect; re-download in a browser"
    mv "$TMPDIR_L/Ollama.app" "$OLLAMA_BIN_DIR/"
  else
    # DMG route. `hdiutil attach` prints "<dev>\t<type>\t<mount point>"; take the mount point
    # from that output (v3 grepped `hdiutil info` for lines starting /Volumes/, which never exist).
    DMG=""
    for d in "$DOWNLOADS/Ollama.dmg" "$DOWNLOADS/ollama.dmg"; do [[ -f "$d" ]] && { DMG="$d"; break; }; done
    [[ -n "$DMG" ]] || { wait_for_file "$DOWNLOADS/Ollama.dmg" "Ollama.dmg (https://ollama.com/download → macOS)"; DMG="$DOWNLOADS/Ollama.dmg"; }
    VOL=$(hdiutil attach -nobrowse -noautoopen "$DMG" | grep -o '/Volumes/.*$' | tail -1)
    [[ -n "$VOL" && -d "$VOL/Ollama.app" ]] || { [[ -n "$VOL" ]] && hdiutil detach -quiet "$VOL"; die "no Ollama.app in mounted dmg ($VOL)"; }
    cp -R "$VOL/Ollama.app" "$OLLAMA_BIN_DIR/"
    hdiutil detach -quiet "$VOL" || warn "could not detach $VOL (eject it in Finder)"
  fi
  xattr -dr com.apple.quarantine "$OLLAMA_BIN_DIR/Ollama.app" 2>/dev/null
  [[ -x "$OLLAMA_EXE" ]] || die "Ollama binary missing after install"
  ARCHS=$(lipo -archs "$OLLAMA_EXE" 2>/dev/null || echo "?")
  ok "Ollama installed (architectures: $ARCHS)"
  MYARCH=$(uname -m)
  if [[ "$ARCHS" != *"$MYARCH"* ]]; then
    warn "binary lacks your architecture ($MYARCH: $ARCHS). It will run via Rosetta on M-series"
    warn "(slow, no Metal). Re-download from https://ollama.com/download."
  fi
fi
OLLAMA="$OLLAMA_EXE"
export OLLAMA_MODELS="$MODELS_DIR"
# Flash attention + 8-bit KV cache halve the context memory, so the model stays on the GPU.
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE=q8_0

# ===========================================================================
say "Step 3/9 — Models"
SERVE_LOG="$LOCAL_AI/serve-setup.log"
if pgrep -qf "ollama serve" 2>/dev/null; then
  warn "an ollama server is already running — models go wherever THAT server stores them"
  warn "(the Ollama menu-bar app uses ~/.ollama/models, not ~/local-ai/models). Quit it and"
  warn "re-run this script if you want everything under ~/local-ai."
else
  "$OLLAMA" serve >"$SERVE_LOG" 2>&1 &
  SERVE_PID=$!
fi
for i in {1..30}; do curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1 || die "server didn't start — see $SERVE_LOG"
ok "server up on 127.0.0.1:11434"

RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
# Processor. Ollama uses the GPU (Metal) only on Apple Silicon; on an Intel Mac it runs on the CPU
# whatever graphics card is fitted (an AMD Radeon is not used). A shell running under Rosetta on an
# M-series Mac reports x86_64, so sysctl.proc_translated=1 counts as Apple Silicon.
CPU_NAME=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo "?")
CPU_CORES=$(sysctl -n hw.physicalcpu 2>/dev/null || echo "?")
if [[ "$(uname -m)" == "arm64" || "$(sysctl -n sysctl.proc_translated 2>/dev/null)" == "1" ]]; then
  CPU_KIND=apple
  ok "Apple Silicon ($CPU_NAME, $CPU_CORES cores), ${RAM_GB} GB unified memory: the model runs on the GPU"
  [[ "$(uname -m)" != "arm64" ]] && warn "this Terminal runs under Rosetta; open a native Terminal for full speed"
else
  CPU_KIND=intel
  ok "Intel Mac ($CPU_NAME, $CPU_CORES cores), ${RAM_GB} GB RAM: Ollama runs on the CPU (the graphics card is not used)"
  warn "expect a few minutes per audit window; use lumo-audit --static-only for whole repos and the model"
  warn "pass on --changed main or single files. 'lumo-ps' shows 100% CPU here, which is expected."
fi
MACOS_MAJOR=$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)
[[ -n "$MACOS_MAJOR" ]] && (( MACOS_MAJOR < 14 )) && warn "macOS $(sw_vers -productVersion): current Ollama needs macOS 14 (Sonoma) or newer"
if [[ -n "$FORCE_MODEL" ]]; then
  CHAT_MODEL="$FORCE_MODEL"
elif [[ $SMALL_MODEL -eq 1 ]]; then
  CHAT_MODEL="qwen3:14b"; [[ "$CPU_KIND" == intel ]] && CHAT_MODEL="qwen3:8b"
else
  # On a CPU the 30b-a3b mixture of experts (about 3 B weights active per word) is faster than the dense
  # 14b, so an Intel Mac with the memory for it gets it; a small Intel Mac gets the 8b.
  SMALL="qwen3:14b"; SMALL_NOTE="16 GB Macs"
  [[ "$CPU_KIND" == intel ]] && { SMALL="qwen3:8b"; SMALL_NOTE="16 GB Intel Macs"; }
  DEF=1; (( RAM_GB < 32 )) && DEF=2
  printf '  Chat model — [1] qwen3:30b-a3b (needs 32 GB) / [2] %s (%s) [default %s]: ' "$SMALL" "$SMALL_NOTE" "$DEF"
  read -r choice; [[ -z "$choice" ]] && choice=$DEF
  CHAT_MODEL="$SMALL"; [[ "$choice" == "1" ]] && CHAT_MODEL="qwen3:30b-a3b"
  if [[ "$CHAT_MODEL" == "qwen3:30b-a3b" ]] && (( RAM_GB < 32 )); then
    warn "qwen3:30b-a3b is ~19 GB; on ${RAM_GB} GB it will not fit in memory. Using $SMALL."
    CHAT_MODEL="$SMALL"
  fi
fi
# Apple Silicon: context sized so weights + KV cache fit the GPU's share of memory (ollama ps → 100% GPU).
# Intel: memory is not the limit, reading the prompt on the CPU is, so a 16k window keeps each audit
# window to a few minutes (override: LUMO_NUM_CTX=24576 in ~/.zshrc).
if [[ "$CPU_KIND" == intel ]]; then NUM_CTX=$(( RAM_GB >= 32 ? 16384 : 12288 ))
elif (( RAM_GB >= 64 )); then NUM_CTX=32768
elif (( RAM_GB >= 32 )); then NUM_CTX=24576
else NUM_CTX=12288; fi
"$OLLAMA" list 2>/dev/null | grep -q "^$CHAT_MODEL" || "$OLLAMA" pull "$CHAT_MODEL" || die "pull failed: $CHAT_MODEL"
ok "chat model: $CHAT_MODEL (num_ctx $NUM_CTX)"
"$OLLAMA" list 2>/dev/null | grep -q "^nomic-embed-text" || "$OLLAMA" pull nomic-embed-text || die "pull failed: nomic-embed-text"
ok "nomic-embed-text present"

# ===========================================================================
say "Step 4/9 — 'lumo' persona"
cat > "$LOCAL_AI/Modelfile" << EOF
FROM $CHAT_MODEL
PARAMETER num_ctx $NUM_CTX
PARAMETER temperature 0.6
PARAMETER top_p 0.95
PARAMETER top_k 20
SYSTEM """You are Lumo-Lite, a fully local assistant for dam-safety engineering
work on this machine. Domain and working style:

DOMAIN:
- QGIS plugins of the ND dam suite (ND_Dam_Hazard_Helper, nd_breach_runner,
  nd_dam_screening, nd_data_updater) and the DamSafety fork; QGIS 3.34 with
  Python 3.12 from conda-forge on macOS (env ndqgis); CI also runs QGIS 4 / Qt6
- Crest: TypeScript desk-screening app (a screening tool, not a hazard classification)
- Dam hazard classification per USBR ACER TM 11 and ND state standards
- Depth/velocity/arrival-time rasters (NAD83 UTM, e.g. EPSG:26914), NSI / ND GIS Hub /
  FEMA / OSM data, LISFLOOD-FP and ANUGA outputs

CHARACTERISTICS:
- Precise about code, filenames, paths, units (ft vs m, cfs vs m3/s, NAVD88 vs local datum).
- Filename matching must never classify units on loose stems; junk filters target
  ._*, .DS_Store, __MACOSX specifically — not all dot-prefixed files.
- Show the complete corrected function, never fragments.
- Atomic-write discipline: write to .part, then rename.
- Never call sys.exit() or os._exit() in code that runs inside QGIS.
- Guard every operation that can receive None before string ops.
- Silent failures are defects: log and surface problems, never swallow them.
- Only report a defect you can point to by line number in code you were shown.
- When uncertain, say UNSURE plainly instead of guessing.
- Never recommend destructive operations without a backup (ZIP first) and a dry-run mode."""
EOF
"$OLLAMA" create lumo -f "$LOCAL_AI/Modelfile" >/dev/null || die "persona creation failed"
ok "'lumo' persona created (num_ctx $NUM_CTX)"

# ===========================================================================
say "Step 5/9 — Python for the tools (conda env '$LUMO_ENV' preferred)"
CONDA=""
for c in "${CONDA_EXE:-}" "$(command -v conda 2>/dev/null)" "$HOME/miniconda3/bin/conda" "$HOME/miniconda/bin/conda" "$HOME/miniforge3/bin/conda"; do
  [[ -n "$c" && -x "$c" ]] && { CONDA="$c"; break; }
done
LUMO_PY=""; USE_PYLIBS=0
if [[ -n "$CONDA" ]]; then
  ok "conda: $CONDA"
  ENVPATH=$("$CONDA" env list | awk -v n="$LUMO_ENV" '$1==n {print $NF}')
  if [[ -z "$ENVPATH" ]]; then
    # Separate small env: adding packages to ndqgis could make conda re-solve QGIS.
    "$CONDA" create -y -n "$LUMO_ENV" -c conda-forge --override-channels --strict-channel-priority \
        python=3.12 pymupdf pypdf numpy \
      || warn "conda create failed — falling back to wheels"
    ENVPATH=$("$CONDA" env list | awk -v n="$LUMO_ENV" '$1==n {print $NF}')
  fi
  [[ -n "$ENVPATH" && -x "$ENVPATH/bin/python" ]] && LUMO_PY="$ENVPATH/bin/python"
fi

if [[ -n "$LUMO_PY" ]]; then
  ok "tools will use $LUMO_PY"
else
  warn "no conda env — using python3 + wheels in ~/local-ai/pylibs"
  have python3 || die "python3 not found (xcode-select --install provides one)"
  LUMO_PY="$(command -v python3)"; USE_PYLIBS=1
  PYVER=$("$LUMO_PY" -c 'import sys;print("%d%d" % sys.version_info[:2])')
  MYARCH=$(uname -m)
  if PYTHONPATH="$PYLIBS_DIR" "$LUMO_PY" -c "import pymupdf" 2>/dev/null; then
    ok "PyMuPDF already importable"
  else
    # An abi3 wheel tagged cpXY works on Python X.Y AND NEWER only. Pick the newest
    # non-yanked release whose wheel's cpXY is <= this Python.
    WHEEL_URL=$("$LUMO_PY" - "$MYARCH" "$PYVER" << 'PYEOF'
import json, sys, re, urllib.request
arch, mine = sys.argv[1], int(sys.argv[2])
want_arch = "arm64" if arch == "arm64" else "x86_64"
try:
    data = json.load(urllib.request.urlopen("https://pypi.org/pypi/pymupdf/json", timeout=30))
except Exception:
    sys.exit(0)
def verkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])
releases = [v for v in data["releases"] if re.fullmatch(r"[\d.]+", v)]   # no rc/beta
for ver in sorted(releases, key=verkey, reverse=True):
    for f in data["releases"][ver]:
        n = f["filename"]
        if f.get("yanked") or not n.endswith(".whl") or "macosx" not in n or want_arch not in n:
            continue
        m = re.search(r"-cp(\d)(\d+)-(abi3|cp\d+)-", n)
        if not m:
            continue
        tag = int(m.group(1) + m.group(2))
        if (m.group(3) == "abi3" and tag <= mine) or (m.group(3) != "abi3" and tag == mine):
            print(f["url"]); sys.exit(0)
PYEOF
)
    if [[ -n "${WHEEL_URL:-}" ]]; then
      ok "found: $(basename "$WHEEL_URL")"
      if curl -fL --connect-timeout 20 -o "$TMPDIR_L/pymupdf.whl" "$WHEEL_URL" && ! is_html "$TMPDIR_L/pymupdf.whl"; then
        UNPACK="$TMPDIR_L/pymupdf-unpack"; rm -rf "$UNPACK"; mkdir -p "$UNPACK"
        if unzip -q "$TMPDIR_L/pymupdf.whl" -d "$UNPACK"; then
          xattr -dr com.apple.quarantine "$UNPACK" 2>/dev/null || true
          rm -rf "$PYLIBS_DIR/pymupdf" "$PYLIBS_DIR/fitz" "$PYLIBS_DIR"/pymupdf-*.dist-info
          cp -R "$UNPACK"/pymupdf "$PYLIBS_DIR/" 2>/dev/null
          [[ -d "$UNPACK/fitz" ]] && cp -R "$UNPACK/fitz" "$PYLIBS_DIR/"
          cp -R "$UNPACK"/pymupdf-*.dist-info "$PYLIBS_DIR/" 2>/dev/null
        fi
      fi
      if PYTHONPATH="$PYLIBS_DIR" "$LUMO_PY" -c "import pymupdf" 2>/dev/null; then
        ok "PyMuPDF installed into ~/local-ai/pylibs"
      else
        warn "PyMuPDF wheel did not import — PDFs will use pypdf (below)"
      fi
    else
      warn "no PyMuPDF wheel for Python ${PYVER:0:1}.${PYVER:1} on macOS ${MYARCH} — PDFs will use pypdf (below)"
    fi
  fi
  # --- pypdf fallback (pure Python) ---
  if ! PYTHONPATH="$PYLIBS_DIR" "$LUMO_PY" -c "import pypdf" 2>/dev/null; then
    PYPDF_URL=$("$LUMO_PY" - << 'PYEOF'
import json, urllib.request
try:
    data = json.load(urllib.request.urlopen("https://pypi.org/pypi/pypdf/json", timeout=30))
    for f in data["urls"]:
        if f["filename"].endswith(".whl") and "py3-none-any" in f["filename"]:
            print(f["url"]); break
except Exception as e:
    import sys; print("pypdf lookup failed: %s" % e, file=sys.stderr)
PYEOF
)
    if [[ -n "${PYPDF_URL:-}" ]] \
       && curl -fsL --connect-timeout 20 -o "$TMPDIR_L/pypdf.whl" "$PYPDF_URL" \
       && unzip -qo "$TMPDIR_L/pypdf.whl" -d "$PYLIBS_DIR" \
       && PYTHONPATH="$PYLIBS_DIR" "$LUMO_PY" -c "import pypdf" 2>/dev/null; then
      ok "pypdf fallback installed"
    else
      warn "pypdf not installed — code indexing works; PDFs will be skipped"
    fi
  fi
fi

# ===========================================================================
say "Step 6/9 — Python tools (written to ~/local-ai)"
if [[ $REWRITE_SCRIPTS -eq 1 ]]; then
  cat > "$LOCAL_AI/rag_index.py" << 'SCRIPT_EOF_RAG_INDEX'
#!/usr/bin/env python3
# rag_index.py v2.4 — multi-collection RAG indexer (stdlib only)
# Atomic rebuilds via a hidden per-collection stage; auto-migrates v1 DBs;
# PDF backends: pdftotext -> pymupdf -> pypdf -> skip.
#
# v2.4 fixes (vs v2.3):
#   - A missing folder, or a run that produced 0 chunks, no longer swaps an
#     empty result over the live collection (v2.3 wiped it while saying
#     "Live index untouched").
#   - Backend detection used one flag for "detected" and "pdftotext exists".
#   - Inserts run inside one transaction per batch (autocommit was one fsync
#     per chunk).
#   - Oversized / minified files are reported, not skipped silently.
#   - nomic-embed-text task prefixes (search_document: / search_query:).
#   - TypeScript/JS added so the crest repo indexes its source, not just docs.
#   - Stage name is per collection, so two indexers cannot clobber each other.
import os, sys, json, sqlite3, argparse, subprocess, urllib.request, urllib.error, time, re

DB = os.path.expanduser(os.environ.get("LUMO_DB", "~/local-ai/knowledge.db"))
EMBED_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/embed"
EMBED_MODEL = "nomic-embed-text"
DOC_PREFIX = "search_document: "
CHUNK_CHARS = 1500
OVERLAP = 200            # audit_plugin.py relies on this value when rebuilding files
MAX_TEXT_BYTES = 262144
MAX_PDF_BYTES = 104857600
MINIFIED_AVG_LINE = 300  # average chars per line above this = generated/minified, skip
MINIFIABLE_EXT = (".js", ".mjs", ".cjs", ".json")
BATCH = 16
STAGE_PREFIX = "__stage__"
SKIP_DIRS = {"__MACOSX", ".git", "node_modules", "__pycache__", ".venv",
             "testhazard", "_ocr_staging_tmp", "dist", "build", ".vite"}
SKIP_PREFIX = ("._",)
SKIP_EXACT = {".DS_Store"}
TEXT_EXT = (".py", ".md", ".txt", ".qml", ".rst", ".yml", ".yaml",
            ".toml", ".cfg", ".ini", ".json", ".log", ".sql", ".sh",
            ".ts", ".tsx", ".js", ".mjs", ".cjs")
PDF_EXT = (".pdf",)

_detected = [False]
_pdftotext = [False]
_pm = [None]
_pp = [None]


def _detect():
    if _detected[0]:
        return
    _detected[0] = True
    try:
        subprocess.run(["pdftotext", "-v"], capture_output=True)
        _pdftotext[0] = True
    except (FileNotFoundError, OSError):
        _pdftotext[0] = False
    try:
        import pymupdf
        _pm[0] = pymupdf
    except ImportError:
        try:
            import fitz
            _pm[0] = fitz
        except ImportError:
            pass
    try:
        import pypdf as m
        _pp[0] = m
    except ImportError:
        pass


def pdf_backend_names():
    _detect()
    n = []
    if _pdftotext[0]:
        n.append("pdftotext")
    if _pm[0] is not None:
        n.append("pymupdf")
    if _pp[0] is not None:
        n.append("pypdf")
    return n


def extract_pdf_text(path):
    _detect()
    if _pdftotext[0]:
        try:
            r = subprocess.run(["pdftotext", "-q", "-layout", path, "-"],
                               capture_output=True, timeout=600)
            if r.returncode == 0:
                t = r.stdout.decode("utf-8", errors="replace")
                if t.strip():
                    return t
        except Exception as e:
            print("    ! pdftotext failed on %s: %s" % (path, e))
    if _pm[0] is not None:
        try:
            doc = _pm[0].open(path)
            parts = [doc.load_page(i).get_text() for i in range(len(doc))]
            doc.close()
            t = "\n".join(parts)
            if t.strip():
                return t
        except Exception as e:
            print("    ! pymupdf failed on %s: %s" % (path, e))
    if _pp[0] is not None:
        try:
            rd = _pp[0].PdfReader(path)
            parts = []
            for pg in rd.pages:
                try:
                    parts.append(pg.extract_text() or "")
                except Exception:
                    continue
            t = "\n".join(parts)
            if t.strip():
                return t
        except Exception as e:
            print("    ! pypdf failed on %s: %s" % (path, e))
    return None


def connect():
    db = sqlite3.connect(DB)
    db.isolation_level = None
    return db


def ensure_schema(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(chunks)").fetchall()]
    if not cols:
        db.execute("""CREATE TABLE chunks (id INTEGER PRIMARY KEY,
            collection TEXT NOT NULL, path TEXT NOT NULL, idx INTEGER NOT NULL,
            txt TEXT NOT NULL, vec TEXT NOT NULL)""")
    elif "collection" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN collection TEXT NOT NULL DEFAULT 'legacy'")
        print("  migrated v1 index: existing rows tagged collection='legacy'")
    db.execute("CREATE INDEX IF NOT EXISTS idx_coll ON chunks(collection)")
    db.execute("""CREATE TABLE IF NOT EXISTS roots (collection TEXT PRIMARY KEY,
        roots TEXT NOT NULL, indexed_at TEXT NOT NULL)""")


def embed(texts):
    body = {"model": EMBED_MODEL, "input": [DOC_PREFIX + t for t in texts]}
    req = urllib.request.Request(EMBED_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        vecs = json.loads(r.read())["embeddings"]
    if len(vecs) != len(texts):
        raise RuntimeError("embed returned %d vectors for %d texts" % (len(vecs), len(texts)))
    return vecs


def file_filter(name):
    if name in SKIP_EXACT or name.startswith(SKIP_PREFIX):
        return None
    lo = name.lower()
    if lo.endswith(PDF_EXT):
        return "pdf"
    if lo.endswith(TEXT_EXT):
        return "text"
    return None


def read_text(full, rel):
    """Return the file text, or None with a printed reason. Never silent."""
    try:
        size = os.path.getsize(full)
        if size > MAX_TEXT_BYTES:
            print("    [too big, skipped: %d KB > %d KB] %s" % (size // 1024, MAX_TEXT_BYTES // 1024, rel))
            return None
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print("    [unreadable, skipped: %s] %s" % (e, rel))
        return None
    if not text.strip():
        return None
    lines = text.count("\n") + 1
    if rel.lower().endswith(MINIFIABLE_EXT) and len(text) / lines > MINIFIED_AVG_LINE:
        print("    [minified/generated, skipped] %s" % rel)
        return None
    return text


def iter_chunks(kind, full, rel):
    if kind == "pdf":
        text = extract_pdf_text(full)
    else:
        text = read_text(full, rel)
    if not text:
        return
    step = CHUNK_CHARS - OVERLAP
    # Every slice is yielded (even whitespace-only) so chunk idx stays contiguous
    # and audit_plugin.py can rebuild the file exactly.
    for i in range(0, len(text), step):
        yield text[i:i + CHUNK_CHARS]
        if i + CHUNK_CHARS >= len(text):
            break


def index_collection(collection, roots, allow_empty=False):
    t0 = time.time()
    nf = nc = nnp = 0
    pending = []
    stage = STAGE_PREFIX + collection
    abs_roots = [os.path.abspath(os.path.expanduser(r)) for r in roots]
    missing = [r for r in abs_roots if not os.path.isdir(r)]
    if missing:
        sys.exit("ERROR: not a directory: %s\nLive collection '%s' untouched."
                 % (", ".join(missing), collection))
    db = connect()
    ensure_schema(db)
    db.execute("DELETE FROM chunks WHERE collection = ?", (stage,))

    def flush():
        nonlocal nc
        if not pending:
            return
        vecs = embed([t for _, _, t in pending])
        db.execute("BEGIN")
        db.executemany("INSERT INTO chunks (collection,path,idx,txt,vec) VALUES (?,?,?,?,?)",
                       [(stage, p, i, t, json.dumps(v)) for (p, i, t), v in zip(pending, vecs)])
        db.execute("COMMIT")
        nc += len(pending)
        pending.clear()

    multi = len(abs_roots) > 1
    try:
        for root in abs_roots:
            print("  walking %s ..." % root)
            for dp, dns, fns in os.walk(root):
                dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
                for fn in sorted(fns):
                    kind = file_filter(fn)
                    if kind is None:
                        continue
                    full = os.path.join(dp, fn)
                    rel = os.path.relpath(full, root)
                    if multi:  # keep paths unique and traceable to their root
                        rel = os.path.join(os.path.basename(root), rel)
                    if kind == "pdf":
                        try:
                            if os.path.getsize(full) > MAX_PDF_BYTES:
                                print("    [PDF too big, skipped] %s" % rel)
                                continue
                        except OSError as e:
                            print("    [unreadable, skipped: %s] %s" % (e, rel))
                            continue
                    got = False
                    for i, c in enumerate(iter_chunks(kind, full, rel)):
                        pending.append((rel, i, c))
                        got = True
                        if len(pending) >= BATCH:
                            flush()
                            print("    ...%d chunks staged" % nc, flush=True)
                    if got:
                        nf += 1
                    elif kind == "pdf":
                        nnp += 1
                        print("    [no text layer] %s — needs OCR" % rel)
        flush()
    except KeyboardInterrupt:
        try:
            db.execute("DELETE FROM chunks WHERE collection = ?", (stage,))
        except Exception:
            pass
        db.close()
        sys.exit("\nInterrupted. Live collection '%s' untouched." % collection)
    except Exception as e:
        try:
            if db.in_transaction:
                db.execute("ROLLBACK")
            db.execute("DELETE FROM chunks WHERE collection = ?", (stage,))
        except Exception:
            pass
        db.close()
        sys.exit("ERROR: indexing '%s' failed (%s). Live collection untouched." % (collection, e))
    if nc == 0 and not allow_empty:
        db.execute("DELETE FROM chunks WHERE collection = ?", (stage,))
        db.close()
        sys.exit("ERROR: no indexable files under %s. Live collection '%s' untouched "
                 "(pass --allow-empty to clear it on purpose)." % (", ".join(abs_roots), collection))
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM chunks WHERE collection = ?", (collection,))
        db.execute("UPDATE chunks SET collection = ? WHERE collection = ?", (collection, stage))
        db.execute("INSERT OR REPLACE INTO roots (collection, roots, indexed_at) VALUES (?,?,?)",
                   (collection, json.dumps(abs_roots), time.strftime("%Y-%m-%d %H:%M:%S")))
        db.execute("COMMIT")
    except Exception as e:
        try:
            db.execute("ROLLBACK")
            db.execute("DELETE FROM chunks WHERE collection = ?", (stage,))
        except Exception:
            pass
        db.close()
        sys.exit("ERROR: swap failed (%s). Live collection untouched." % e)
    db.close()
    print("Indexed collection '%s': %d files, %d chunks in %.0fs" % (collection, nf, nc, time.time() - t0))
    if nnp:
        print("NOTE: %d PDFs had no extractable text (scanned?). OCR then re-run." % nnp)


def list_collections():
    if not os.path.exists(DB):
        print("No index yet at %s" % DB)
        return
    db = connect()
    try:
        rows = db.execute("SELECT collection,COUNT(DISTINCT path),COUNT(*) FROM chunks "
                          "WHERE collection NOT LIKE '\\_\\_stage\\_\\_%' ESCAPE '\\' "
                          "GROUP BY collection ORDER BY collection").fetchall()
    except sqlite3.OperationalError:
        print("Database exists but has no usable collections yet.")
        return
    if not rows:
        print("Index exists but is empty.")
        return
    print("Collections in %s:" % DB)
    print("  %-20s %8s %8s" % ("collection", "files", "chunks"))
    for c, a, b in rows:
        print("  %-20s %8d %8d" % (c, a, b))


def main():
    ap = argparse.ArgumentParser(description="Index folders (text+PDFs) into a named collection.")
    ap.add_argument("-c", "--collection", default=None)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--list-collections", action="store_true")
    ap.add_argument("--allow-empty", action="store_true",
                    help="allow a run with 0 chunks to replace (clear) the collection")
    args = ap.parse_args()
    if args.list_collections:
        list_collections()
        return
    if not args.paths:
        sys.exit("usage: rag_index.py [-c NAME] <folder> ...\n  rag_index.py --list-collections")
    coll = args.collection or os.path.basename(os.path.normpath(os.path.expanduser(args.paths[0])))
    if not re.match(r"^[a-zA-Z0-9_\-]{1,32}$", coll) or coll.startswith("__"):
        sys.exit("ERROR: bad collection name '%s' (letters, digits, _ and -, max 32, no leading __)" % coll)
    b = pdf_backend_names()
    print("PDF extraction backends: %s" % (", ".join(b) if b else "NONE — PDFs skipped"))
    index_collection(coll, args.paths, args.allow_empty)
    list_collections()


if __name__ == "__main__":
    main()
SCRIPT_EOF_RAG_INDEX

  cat > "$LOCAL_AI/rag_query.py" << 'SCRIPT_EOF_RAG_QUERY'
#!/usr/bin/env python3
# rag_query.py v2.2 — query the RAG store, streaming output (stdlib only; numpy if present)
#
# v2.2 fixes (vs v2.1):
#   - nomic-embed-text query prefix (search_query:) to match the v2.4 index.
#   - Hidden per-collection stages excluded from "all".
#   - num_ctx sent per request, sized to the prompt, so Ollama never trims the
#     front of the prompt (the instructions) without saying so.
#   - qwen3 "thinking" is streamed separately and kept out of the answer.
import os, sys, json, sqlite3, urllib.request, urllib.error, math, argparse

DB = os.path.expanduser(os.environ.get("LUMO_DB", "~/local-ai/knowledge.db"))
BASE = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = "nomic-embed-text"
QUERY_PREFIX = "search_query: "
CHAT_MODEL = os.environ.get("LUMO_MODEL", "lumo")
NUM_CTX = int(os.environ.get("LUMO_NUM_CTX", "16384"))
TOP_K = 10
try:
    import numpy as np
except ImportError:
    np = None


def post(url, payload, timeout=900):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def retrieve(q, coll, top_k):
    if not os.path.exists(DB):
        sys.exit("No index at %s — run rag_index.py first." % DB)
    db = sqlite3.connect(DB)
    if coll == "all":
        rows = db.execute("SELECT collection,path,idx,txt,vec FROM chunks "
                          "WHERE collection NOT LIKE '\\_\\_%' ESCAPE '\\'").fetchall()
    else:
        rows = db.execute("SELECT collection,path,idx,txt,vec FROM chunks WHERE collection=?",
                          (coll,)).fetchall()
    db.close()
    if not rows:
        h = (" Collection '%s' empty — index it: rag_index.py -c %s <folder>" % (coll, coll)) if coll != "all" else ""
        sys.exit("Index is empty.%s" % h)
    qv = post(BASE + "/api/embed", {"model": EMBED_MODEL, "input": [QUERY_PREFIX + q]})["embeddings"][0]
    if np is not None:
        M = np.array([json.loads(r[4]) for r in rows], dtype=np.float32)
        v = np.array(qv, dtype=np.float32)
        sims = (M @ v) / (np.linalg.norm(M, axis=1) * np.linalg.norm(v) + 1e-9)
        return [(float(sims[i]), rows[i][0], rows[i][1], rows[i][2], rows[i][3])
                for i in np.argsort(-sims)[:top_k]]

    def cos(a, b):
        d = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return d / (na * nb) if na and nb else 0.0
    sc = sorted(((cos(qv, json.loads(r[4])), r) for r in rows), key=lambda x: -x[0])[:top_k]
    return [(s, r[0], r[1], r[2], r[3]) for s, r in sc]


def chat_payload(prompt, stream, think=True):
    est_tokens = len(prompt) // 3 + 4096          # prompt + room for thinking and answer
    p = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": prompt}],
         "stream": stream, "options": {"num_ctx": max(NUM_CTX, min(est_tokens, 40960))}}
    if think:
        p["think"] = True
    return p


def stream_answer(prompt, think=True):
    req = urllib.request.Request(BASE + "/api/chat", data=json.dumps(chat_payload(prompt, True, think)).encode(),
                                 headers={"Content-Type": "application/json"})
    buf = []
    thinking_shown = False
    try:
        r = urllib.request.urlopen(req, timeout=1800)
    except urllib.error.HTTPError as e:
        if think and e.code == 400:      # older Ollama without the "think" field
            return stream_answer(prompt, think=False)
        raise
    with r:
        while True:
            line = r.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                ch = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ch.get("error"):
                raise RuntimeError(ch["error"])
            msg = ch.get("message") or {}
            if msg.get("thinking") and not thinking_shown:
                sys.stdout.write("(thinking…)\n")
                sys.stdout.flush()
                thinking_shown = True
            piece = msg.get("content") or ""
            if piece:
                buf.append(piece)
                sys.stdout.write(piece)
                sys.stdout.flush()
            if ch.get("done"):
                break
    sys.stdout.write("\n")
    return "".join(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--collection", default="all")
    ap.add_argument("-k", "--top-k", type=int, default=TOP_K)
    ap.add_argument("question", nargs="+")
    args = ap.parse_args()
    q = " ".join(args.question).strip()
    if not q:
        sys.exit("no question given")
    hits = retrieve(q, args.collection, args.top_k)
    print("Top %d context hits:" % len(hits))
    for s, c, p, i, _ in hits:
        print("  %.3f  [%s] %s (chunk %d)" % (s, c, p, i))
    print("-" * 70)
    ctx = "\n\n".join("--- [%s] %s (chunk %d) ---\n%s" % (c, p, i, t) for _, c, p, i, t in hits)
    prompt = ("Answer using ONLY the context below. Cite each fact with the collection "
              "and file path shown in its bracket header. If insufficient, say so plainly.\n\n"
              "=== CONTEXT ===\n" + ctx + "\n=== QUESTION ===\n" + q)
    print("Answer (%s, streaming — Ctrl+C aborts):" % CHAT_MODEL)
    print("-" * 70)
    ans = ""
    try:
        ans = stream_answer(prompt)
    except KeyboardInterrupt:
        print("\n[interrupted]")
        sys.exit(0)
    except Exception as e:
        print("[stream failed (%s) — non-streaming fallback]" % e)
        out = post(BASE + "/api/chat", chat_payload(prompt, False, think=False))
        ans = (out.get("message") or {}).get("content") or ""
        print(ans)
    if not ans.strip():
        print("[empty response — check `ollama ps` and the server log]")


if __name__ == "__main__":
    main()
SCRIPT_EOF_RAG_QUERY

  cat > "$LOCAL_AI/audit_plugin.py" << 'SCRIPT_EOF_AUDIT'
#!/usr/bin/env python3
# audit_plugin.py v1.3 — whole-folder Python error auditor (stdlib only)
#
# Two passes per file:
#   1. STATIC (exact, no model): syntax errors, sys.exit/os._exit/exit/quit,
#      bare or swallowing except blocks, gdal.DontUseExceptions(), `is` against
#      a literal. Line numbers are real. Run alone with --static-only.
#   2. MODEL (advisory): the local model reads the file with line numbers, in
#      windows split at top-level def/class boundaries so nothing is truncated.
#
# v1.3 fixes (vs v1.2):
#   - v1.2 rebuilt files by concatenating overlapping chunks, so the model saw
#     200 duplicated characters at every 1,300-character boundary and reported
#     phantom syntax errors. Files are now read from disk (the indexed folder or
#     --path); the DB fallback strips the overlap.
#   - Files over 60,000 chars were cut off; they are now audited in windows.
#   - num_ctx is sent per request and the window is sized to fit, so Ollama
#     never silently trims the front of the prompt (the instructions).
#   - The verdict is parsed from the "FILE VERDICT:" line after any <think>
#     block, not by looking for "CLEAN" anywhere in the first 200 chars (the
#     echoed format line "CLEAN | ISSUES FOUND" counted as clean).
#   - Only .py files are audited (v1.2 sent README.md and JSON to a Python audit).
#   - Sampling follows Qwen3's thinking-mode advice (temperature 0.1 makes
#     thinking models loop until the 30-minute timeout).
#   - Ctrl+C writes the partial report instead of losing it.
#   - --changed REF audits only files changed since a git ref.
import os, sys, re, ast, json, sqlite3, argparse, subprocess, urllib.request, urllib.error
from datetime import datetime

DB = os.path.expanduser(os.environ.get("LUMO_DB", "~/local-ai/knowledge.db"))
CHAT_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/chat"
MODEL = os.environ.get("LUMO_MODEL", "lumo")
NUM_CTX = int(os.environ.get("LUMO_NUM_CTX", "16384"))
OUT_RESERVE_TOKENS = 6144       # thinking + answer
PROMPT_TOKENS = 900             # the audit instructions themselves
CHARS_PER_TOKEN = 3.0           # conservative for Python source
MAX_WINDOW_CHARS = 60000
INDEX_OVERLAP = 200             # must equal OVERLAP in rag_index.py
REPORT_DIR = os.path.expanduser("~/local-ai/reports")
SKIP_DIRS = {"__MACOSX", ".git", "node_modules", "__pycache__", ".venv", "releases"}
SCRIPT_DIRS = {"tests", "test", "scripts", "tools"}

AUDIT_PROMPT = """You are performing a focused Python error audit on part of one file from the
{collection} codebase (QGIS plugin, dam-hazard domain; runs in QGIS 3.34 / Python 3.12
from conda-forge on macOS, and CI also runs it on QGIS 4 / Qt6).

This is lines {first}-{last} of {total}. Other parts of the file are audited separately,
so a name defined outside these lines is NOT an error. Each line starts with its real
line number and "|"; cite those numbers.

Audit for:
1. NoneType operands where strings or numbers are expected (TypeError-prone lines)
2. Filename-matching logic that is too broad (e.g. single-letter stem matches
   misclassifying units) or mishandles ._*, .DS_Store, __MACOSX
3. Silent failures: swallowed exceptions, or paths that write empty output without logging
4. sys.exit() / os._exit() reachable from code running inside QGIS
5. GDAL exception-handling misuse
6. Logic bugs: wrong comparison operators, off-by-one, unreachable code,
   wrong units (ft vs m, cfs vs m3/s), wrong variable used
Syntax errors are checked separately by a compiler; do not report them.

Format your answer EXACTLY as:
FILE VERDICT: <CLEAN or ISSUES FOUND>
ISSUES:
- [SEVERITY: low/med/high] LINE <n>: description
(if CLEAN, write "ISSUES: none")

Report real defects only, no style. If unsure, prefix the item with UNSURE.

=== FILE: {path} (lines {first}-{last} of {total}) ===
{code}
"""

VERDICT_RE = re.compile(r"FILE VERDICT:\s*\**\s*(CLEAN|ISSUES FOUND)", re.I)
THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


# ----------------------------------------------------------------- static pass
def _main_guard_ranges(tree):
    out = []
    for node in tree.body:
        if isinstance(node, ast.If):
            t = node.test
            if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                    and t.left.id == "__name__"):
                out.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
    return out


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def static_findings(src, path):
    """Exact checks. Returns (findings, tree or None)."""
    found = []
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        found.append(("high", e.lineno or 0, "SyntaxError: %s" % e.msg))
        return found, None
    in_script = bool(SCRIPT_DIRS & set(path.replace("\\", "/").split("/")[:-1]))
    guards = _main_guard_ranges(tree)

    def guarded(line):
        return any(a <= line <= b for a, b in guards)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in ("sys.exit", "os._exit", "exit", "quit"):
                if in_script or guarded(node.lineno):
                    sev, note = "low", " (script/test or __main__ block; fine unless imported by the plugin)"
                else:
                    sev, note = "high", " — terminates QGIS if reached from plugin code"
                found.append((sev, node.lineno, "%s() call%s" % (name, note)))
            elif name and name.endswith("DontUseExceptions"):
                found.append(("med", node.lineno, "%s() — GDAL errors become silent None returns" % name))
        elif isinstance(node, ast.ExceptHandler):
            body = node.body
            swallow = all(isinstance(b, (ast.Pass, ast.Continue)) or
                          (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))
                          for b in body)
            if node.type is None:
                found.append(("med", node.lineno, "bare except: (also catches KeyboardInterrupt/SystemExit)"
                              + ("; body swallows it silently" if swallow else "")))
            elif swallow:
                found.append(("med", node.lineno, "except %s: swallowed silently (no log)"
                              % (ast.unparse(node.type) if hasattr(ast, "unparse") else "...")))
        elif isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(right, ast.Constant) \
                        and right.value is not None and not isinstance(right.value, bool) \
                        and right.value is not Ellipsis:
                    found.append(("med", node.lineno, "`is` compared with literal %r (identity, not equality)"
                                  % (right.value,)))
    # Swallowed excepts are often deliberate (best-effort cleanup), so they are
    # collapsed into one low line per file instead of drowning the real findings.
    sw = [f for f in found if f[2].startswith("except ") and "swallowed silently" in f[2]]
    found = [f for f in found if f not in sw]
    if sw:
        found.append(("low", sw[0][1], "%d except blocks swallow errors without logging, lines %s "
                      "(check the ones whose failure would leave a wrong or empty result)"
                      % (len(sw), ", ".join(str(f[1]) for f in sorted(sw, key=lambda f: f[1])))))
    found.sort(key=lambda f: f[1])
    return found, tree


# ------------------------------------------------------------- model windows
def window_chars():
    budget = (NUM_CTX - OUT_RESERVE_TOKENS - PROMPT_TOKENS) * CHARS_PER_TOKEN
    return int(max(8000, min(MAX_WINDOW_CHARS, budget)))


def split_windows(src, tree, limit):
    """Yield (first_line, last_line, numbered_text). Splits at top-level statements."""
    lines = src.splitlines()
    total = len(lines)
    if total == 0:
        return
    numbered = ["%5d| %s" % (i + 1, l) for i, l in enumerate(lines)]
    starts = [1]
    if tree is not None:
        for node in tree.body:
            ln = node.lineno
            decos = getattr(node, "decorator_list", None) or []
            if decos:
                ln = min([ln] + [d.lineno for d in decos])
            if ln > 1:
                starts.append(ln)
    starts = sorted(set(starts))
    blocks = []                                  # (first, last) 1-based inclusive
    for i, s in enumerate(starts):
        e = (starts[i + 1] - 1) if i + 1 < len(starts) else total
        blocks.append((s, e))
    win_first, win_len = None, 0
    cur_last = 0
    for s, e in blocks:
        size = sum(len(numbered[k]) + 1 for k in range(s - 1, e))
        if size > limit:                         # one huge block: flush, then hard-split it
            if win_first is not None:
                yield win_first, cur_last, "\n".join(numbered[win_first - 1:cur_last])
                win_first, win_len = None, 0
            a, acc = s, 0
            for k in range(s - 1, e):
                acc += len(numbered[k]) + 1
                if acc > limit and k >= a:
                    yield a, k, "\n".join(numbered[a - 1:k])
                    a, acc = k + 1, len(numbered[k]) + 1
            yield a, e, "\n".join(numbered[a - 1:e])
            continue
        if win_first is not None and win_len + size > limit:
            yield win_first, cur_last, "\n".join(numbered[win_first - 1:cur_last])
            win_first, win_len = None, 0
        if win_first is None:
            win_first = s
        win_len += size
        cur_last = e
    if win_first is not None:
        yield win_first, cur_last, "\n".join(numbered[win_first - 1:cur_last])


def chat(prompt, think=True, timeout=1800):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        # Qwen3 thinking-mode sampling; greedy/low temperature makes it repeat itself.
        "options": {"temperature": 0.6, "top_p": 0.95, "top_k": 20,
                    "num_ctx": NUM_CTX, "num_predict": OUT_RESERVE_TOKENS},
    }
    if think:
        payload["think"] = True
    req = urllib.request.Request(CHAT_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if think and e.code == 400:              # Ollama older than 0.9: no "think" field
            return chat(prompt, think=False, timeout=timeout)
        raise
    content = (out.get("message") or {}).get("content") or ""
    content = THINK_RE.sub("", content).strip()
    if out.get("done_reason") == "length":
        content += "\n[model hit num_predict before finishing — verdict may be incomplete]"
    pe = out.get("prompt_eval_count")
    if isinstance(pe, int) and pe >= NUM_CTX - 64:
        content += "\n[WARNING: prompt filled the whole context; Ollama may have trimmed it]"
    return content


def parse_verdict(text):
    m = VERDICT_RE.search(text)
    if not m:
        return "UNPARSED"
    return "CLEAN" if m.group(1).upper() == "CLEAN" else "ISSUES"


# ---------------------------------------------------------------- file source
def files_from_disk(root, changed_ref=None):
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        sys.exit("Not a directory: %s" % root)
    if changed_ref:
        try:
            top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            names = subprocess.run(["git", "-C", root, "diff", "--name-only", "--diff-filter=AMR",
                                    changed_ref, "--", "."], capture_output=True, text=True,
                                   check=True).stdout.split()
            names += subprocess.run(["git", "-C", root, "ls-files", "--others", "--exclude-standard", "--", "."],
                                    capture_output=True, text=True, check=True).stdout.split()
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            sys.exit("--changed needs a git checkout and a valid ref (%s)" % e)
        out = {}
        for n in sorted(set(names)):
            full = os.path.join(top, n)
            if n.endswith(".py") and os.path.isfile(full) and full.startswith(root + os.sep):
                out[os.path.relpath(full, root)] = full
        return out
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
        for fn in sorted(fns):
            if fn.endswith(".py") and not fn.startswith("._"):
                full = os.path.join(dp, fn)
                out[os.path.relpath(full, root)] = full
    return out


def collection_root(db, collection):
    try:
        row = db.execute("SELECT roots FROM roots WHERE collection=?", (collection,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    roots = json.loads(row[0])
    if len(roots) == 1 and os.path.isdir(roots[0]):
        return roots[0]
    return None


def files_from_db(db, collection):
    """Fallback when the indexed folder is gone: rebuild each file, removing the chunk overlap."""
    rows = db.execute("SELECT path, idx, txt FROM chunks WHERE collection = ? ORDER BY path, idx",
                      (collection,)).fetchall()
    files = {}
    for path, idx, txt in rows:
        if path.endswith(".py"):
            files.setdefault(path, []).append((idx, txt))
    out = {}
    for path, chunks in files.items():
        chunks.sort()
        idxs = [i for i, _ in chunks]
        if idxs != list(range(len(idxs))):
            print("  ! %s: chunk gap in index (indexed by rag_index < 2.4?) — reindex for an exact copy" % path)
        parts = [chunks[0][1]] + [t[INDEX_OVERLAP:] for _, t in chunks[1:]]
        out[path] = "".join(parts)
    return out


# ---------------------------------------------------------------------- main
def write_report(report_path, lines):
    tmp = report_path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, report_path)                 # atomic — no half-written reports


def main():
    global MODEL, NUM_CTX
    ap = argparse.ArgumentParser(description="Audit Python files: exact static checks + local-model review.")
    ap.add_argument("-c", "--collection", default=None,
                    help="indexed collection (reads its folder from disk when it still exists)")
    ap.add_argument("-p", "--path", default=None, help="audit this folder directly (no index needed)")
    ap.add_argument("-f", "--file", default=None, help="only files whose path contains this text")
    ap.add_argument("--changed", metavar="REF", default=None,
                    help="only .py files changed since this git ref (e.g. origin/main), plus untracked")
    ap.add_argument("--static-only", action="store_true", help="skip the model; exact checks only")
    ap.add_argument("--model", default=None)
    ap.add_argument("--num-ctx", type=int, default=None)
    args = ap.parse_args()
    if args.model:
        MODEL = args.model
    if args.num_ctx:
        NUM_CTX = args.num_ctx
    if not args.collection and not args.path:
        sys.exit("give -p <folder> or -c <collection>")

    label = args.collection or os.path.basename(os.path.normpath(os.path.expanduser(args.path)))
    sources = {}                                 # rel path -> (full path or None, text or None)
    if args.path:
        for rel, full in files_from_disk(args.path, args.changed).items():
            sources[rel] = (full, None)
    else:
        if not os.path.exists(DB):
            sys.exit("No index at %s. Run rag_index.py first, or use -p <folder>." % DB)
        db = sqlite3.connect(DB)
        root = collection_root(db, args.collection)
        if root:
            print("Reading files from disk: %s" % root)
            for rel, full in files_from_disk(root, args.changed).items():
                sources[rel] = (full, None)
        else:
            if args.changed:
                sys.exit("--changed needs the folder on disk; use -p <folder>.")
            print("Indexed folder not found; rebuilding files from the index.")
            for rel, txt in files_from_db(db, args.collection).items():
                sources[rel] = (None, txt)
        db.close()
    if args.file:
        sources = {p: v for p, v in sources.items() if args.file in p}
    if not sources:
        sys.exit("No .py files to audit for '%s'." % label)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, "audit_%s_%s.txt" % (label, stamp))
    limit = window_chars()
    lines = ["PYTHON ERROR AUDIT — %s" % label,
             "Model: %s (local Ollama), num_ctx %d, window %d chars   Run: %s"
             % ("none (--static-only)" if args.static_only else MODEL, NUM_CTX, limit, stamp),
             "Files: %d%s" % (len(sources), ("   changed since %s" % args.changed) if args.changed else ""),
             "=" * 70]
    n = {"issues": 0, "clean": 0, "failed": 0, "unparsed": 0, "static": 0}
    interrupted = False
    try:
        for i, rel in enumerate(sorted(sources), 1):
            full, text = sources[rel]
            if text is None:
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError as e:
                    lines += ["", "-" * 70, "FILE: %s" % rel, "UNREADABLE: %s" % e]
                    n["failed"] += 1
                    continue
            print("[%d/%d] %s ... " % (i, len(sources), rel), end="", flush=True)
            sf, tree = static_findings(text, rel)
            block = ["", "-" * 70, "FILE: %s" % rel]
            if sf:
                n["static"] += 1
                block.append("STATIC (exact):")
                block += ["- [SEVERITY: %s] LINE %d: %s" % (s, ln, msg) for s, ln, msg in sf]
            verdicts = []
            if not args.static_only:
                wins = list(split_windows(text, tree, limit))
                for first, last, code in wins:
                    prompt = AUDIT_PROMPT.format(collection=label, path=rel, first=first, last=last,
                                                 total=len(text.splitlines()), code=code)
                    try:
                        ans = chat(prompt)
                    except Exception as e:
                        verdicts.append("FAILED")
                        block.append("MODEL lines %d-%d: AUDIT CALL FAILED: %s" % (first, last, e))
                        continue
                    if not ans.strip():
                        verdicts.append("FAILED")
                        block.append("MODEL lines %d-%d: EMPTY MODEL RESPONSE" % (first, last))
                        continue
                    v = parse_verdict(ans)
                    verdicts.append(v)
                    hdr = "MODEL (advisory)" + (" lines %d-%d" % (first, last) if len(wins) > 1 else "") + ":"
                    block += [hdr, ans]
            if "FAILED" in verdicts:
                n["failed"] += 1
                status = "CALL FAILED"
            elif "ISSUES" in verdicts:
                n["issues"] += 1
                status = "ISSUES FOUND"
            elif "UNPARSED" in verdicts:
                n["unparsed"] += 1
                status = "UNPARSED (read it)"
            else:
                n["clean"] += 1
                status = "clean" if not args.static_only else "done"
            if sf:
                status += " + %d static" % len(sf)
            print(status)
            lines += block
            write_report(report_path + ".inprogress", lines)   # survives a crash
    except KeyboardInterrupt:
        interrupted = True
        print("\n[interrupted — writing partial report]")

    if args.static_only:
        n["clean"] = 0
    lines += ["", "=" * 70,
              "SUMMARY: %d files — %d model ISSUES, %d clean, %d unparsed, %d call failures; "
              "%d files with static findings%s"
              % (len(sources), n["issues"], n["clean"], n["unparsed"], n["failed"], n["static"],
                 "   (INTERRUPTED — partial)" if interrupted else ""),
              "STATIC findings are exact. MODEL findings are advisory: confirm each one",
              "against the code (ideally with a failing test) before changing anything."]
    write_report(report_path, lines)
    try:
        os.remove(report_path + ".inprogress")
    except OSError:
        pass
    print("\nReport: %s" % report_path)
    print("Summary: %d model issues, %d clean, %d unparsed, %d failures, %d with static findings"
          % (n["issues"], n["clean"], n["unparsed"], n["failed"], n["static"]))


if __name__ == "__main__":
    main()
SCRIPT_EOF_AUDIT

  cat > "$LOCAL_AI/suite_check.py" << 'SCRIPT_EOF_SUITE'
#!/usr/bin/env python3
# suite_check.py v1.0 — run the dam-suite tests and runs locally in your conda envs,
# then (optionally) have the local model explain each failure. Stdlib only.
#
#   suite_check.py nd     --repo ~/Documents/DSHazard/nd_hazard          CI fast-checks + plugin suites
#   suite_check.py nd     --repo ... --models                            + LISFLOOD-FP/ANUGA model tests
#   suite_check.py dwr    --repo ~/Documents/DWR_Plugins                  same discovery on the DamSafety repo
#   suite_check.py runner --repo ... selftest|check|run CONFIG.json        Breach Runner without QGIS (ndbreach_cli)
#   suite_check.py crest  --repo ~/Documents/DSHazard/crest              npm ci, typecheck, lint, test
#   add --triage to send each failing step's log (and the code it points at) to the local model.
#
# The tests are the source of truth. The model only explains a failure you already have.
import os, sys, re, json, shutil, argparse, subprocess, time, urllib.request, urllib.error
from datetime import datetime

CHAT_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/chat"
MODEL = os.environ.get("LUMO_MODEL", "lumo")
NUM_CTX = int(os.environ.get("LUMO_NUM_CTX", "16384"))
REPORT_DIR = os.path.expanduser("~/local-ai/reports")
DEFAULT_LISFLOOD = os.path.expanduser("~/nd-dam-suite/bin/lisflood")   # built by the Mac installer

ENV_HINTS = {
    "ndqgis": "conda create -n ndqgis -c conda-forge --override-channels --strict-channel-priority "
              "python=3.12 qgis=3.34 draco=1.5.7 'numpy<2' scipy",
    "ndanuga": "conda create -n ndanuga -c conda-forge --override-channels --strict-channel-priority "
               "python=3.12 anuga=4.0.1 gmsh python-gmsh scipy",
    "ndnode": "conda create -n ndnode -c conda-forge --override-channels nodejs=22 python=3.12",
}


# ---------------------------------------------------------------- conda glue
def conda_exe():
    for c in (os.environ.get("CONDA_EXE"), shutil.which("conda"),
              os.path.expanduser("~/miniconda3/bin/conda"), os.path.expanduser("~/miniconda/bin/conda"),
              os.path.expanduser("~/miniforge3/bin/conda")):
        if c and os.path.exists(c):
            return c
    sys.exit("conda not found. Open a terminal where `conda` works, or set CONDA_EXE.")


def env_prefix(conda, name):
    out = subprocess.run([conda, "env", "list", "--json"], capture_output=True, text=True)
    try:
        envs = json.loads(out.stdout)["envs"]
    except (ValueError, KeyError):
        envs = []
    for p in envs:
        if os.path.basename(p) == name:
            return p
    hint = ENV_HINTS.get(name, "conda create -n %s ..." % name)
    sys.exit("conda env '%s' not found. Create it once:\n  %s" % (name, hint))


def in_env(conda, env, argv):
    """argv run inside a conda env with its activation scripts (GDAL_DATA, PROJ, Qt plugins)."""
    return [conda, "run", "--no-capture-output", "-n", env] + argv


# ------------------------------------------------------------------ stepping
class Steps:
    def __init__(self, label):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = os.path.join(REPORT_DIR, "suite_%s_%s" % (label, stamp))
        os.makedirs(self.dir, exist_ok=True)
        self.results = []          # (name, rc, seconds, log)

    def run(self, name, argv, cwd, env=None, timeout=3600):
        log = os.path.join(self.dir, re.sub(r"[^A-Za-z0-9_.-]+", "_", name) + ".log")
        print("== %s" % name, flush=True)
        t0 = time.time()
        e = dict(os.environ)
        e.update(env or {})
        e.pop("PYTHONPATH", None)      # a shell PYTHONPATH has broken the ND tools before
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("$ (cd %s && %s)\n\n" % (cwd, " ".join(argv)))
            fh.flush()
            try:
                p = subprocess.Popen(argv, cwd=cwd, env=e, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, errors="replace")
                for line in p.stdout:
                    fh.write(line)
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                rc = 124
                fh.write("\n[TIMEOUT after %ds]\n" % timeout)
            except FileNotFoundError as ex:
                rc = 127
                fh.write("\n[not found: %s]\n" % ex)
        dt = time.time() - t0
        print("   %s  (%.0fs)  %s" % ("PASS" if rc == 0 else "FAIL rc=%d" % rc, dt, log), flush=True)
        self.results.append((name, rc, dt, log))
        return rc

    def summary(self):
        bad = [r for r in self.results if r[1] != 0]
        print("\n" + "=" * 70)
        for name, rc, dt, _ in self.results:
            print("  %-4s %-48s %6.0fs" % ("ok" if rc == 0 else "FAIL", name[:48], dt))
        print("%d steps, %d failed. Logs: %s" % (len(self.results), len(bad), self.dir))
        return bad


# ------------------------------------------------------------------- targets
def discover_python_suites(repo):
    """Mirror the nd_hazard CI layout: <pkg>/tests/run_tests.py run as modules from the repo root,
    and packages with only tests/test_*.py run file by file."""
    mods, files = [], []
    for d in sorted(os.listdir(repo)):
        tdir = os.path.join(repo, d, "tests")
        if d.startswith(".") or d in ("releases", "docs", "tools", "shared") or not os.path.isdir(tdir):
            continue
        if os.path.isfile(os.path.join(tdir, "run_tests.py")):
            mods.append(d)
        else:
            files += [os.path.join(d, "tests", f) for f in sorted(os.listdir(tdir))
                      if f.startswith("test_") and f.endswith(".py")]
    return mods, files


def target_plugins(args, conda, steps):
    repo = os.path.abspath(os.path.expanduser(args.repo))
    env_prefix(conda, args.qgis_env)
    py = in_env(conda, args.qgis_env, ["python"])
    qt = {"QT_QPA_PLATFORM": "offscreen"}
    for f in ("tools/ci_checks.py", "tools/test_ci_checks.py"):
        if os.path.isfile(os.path.join(repo, f)):
            steps.run("fast: " + f, py + [f], repo, qt, timeout=900)
    mods, files = discover_python_suites(repo)
    if args.only:
        mods = [m for m in mods if args.only in m]
        files = [f for f in files if args.only in f]
    for m in mods:
        steps.run("suite: %s" % m, py + ["-m", m + ".tests.run_tests"], repo, qt)
    for f in files:
        steps.run("test: %s" % f, py + [f], repo, qt, timeout=1800)
    if args.gui and os.path.isfile(os.path.join(repo, "tools/gui_lint.py")):
        steps.run("gui_lint", py + ["-X", "faulthandler", "tools/gui_lint.py"], repo, qt)
    if args.models:
        runner = next((m for m in ("nd_breach_runner",) if os.path.isdir(os.path.join(repo, m))), None)
        if not runner:
            print("   (no nd_breach_runner package here; --models skipped)")
            return
        menv = dict(qt)
        lis = args.lisflood or DEFAULT_LISFLOOD
        if os.path.isfile(lis):
            menv["NDBREACH_LISFLOOD"] = lis
        else:
            print("   ! no LISFLOOD-FP at %s — model tests that need it will skip or fail" % lis)
        anuga = os.path.join(env_prefix(conda, args.anuga_env), "bin", "python")
        menv["NDBREACH_ANUGA_PYTHON"] = anuga
        menv["OMP_NUM_THREADS"] = str(args.threads)
        steps.run("models: %s --models" % runner, py + ["-m", runner + ".tests.run_tests", "--models"],
                  repo, menv, timeout=7200)


def target_runner(args, conda, steps):
    repo = os.path.abspath(os.path.expanduser(args.repo))
    cli = None
    for cand in ("nd_breach_runner/scripts/ndbreach_cli.py", "DamSafety_Runner/scripts/ndbreach_cli.py"):
        if os.path.isfile(os.path.join(repo, cand)):
            cli = cand
            break
    if not cli:
        sys.exit("ndbreach_cli.py not found under %s" % repo)
    env_prefix(conda, args.qgis_env)
    py = in_env(conda, args.qgis_env, ["python"])
    extra = []
    lis = args.lisflood or DEFAULT_LISFLOOD
    if args.action in ("check", "run", "estimate") and os.path.isfile(lis):
        extra += ["--lisflood", lis]
    if args.action in ("check", "run", "estimate"):
        extra += ["--anuga-python", os.path.join(env_prefix(conda, args.anuga_env), "bin", "python"),
                  "--threads", str(args.threads)]
    argv = py + [cli, args.action] + ([args.config] if args.config else []) + extra
    steps.run("runner: %s" % args.action, argv, repo, {"OMP_NUM_THREADS": str(args.threads)}, timeout=4 * 3600)


def target_crest(args, conda, steps):
    repo = os.path.abspath(os.path.expanduser(args.repo))
    env_prefix(conda, args.node_env)
    run = lambda *a: in_env(conda, args.node_env, list(a))
    nm, lock = os.path.join(repo, "node_modules"), os.path.join(repo, "package-lock.json")
    if not os.path.isdir(nm) or os.path.getmtime(lock) > os.path.getmtime(nm):
        steps.run("npm ci", run("npm", "ci"), repo, timeout=1800)
    for s in ("typecheck", "lint", "test"):
        steps.run("npm run " + s, run("npm", "run", s), repo, timeout=1800)
    if args.desktop:
        steps.run("npm run test:desktop", run("npm", "run", "test:desktop"), repo)
    if args.validate:
        dam, flood = args.validate
        steps.run("validate:crest", run("npm", "run", "validate:crest", "--", "--runner", dam, "--crest", flood),
                  repo)


# -------------------------------------------------------------------- triage
TRACE_RE = re.compile(r'File "([^"]+)", line (\d+)')
TS_RE = re.compile(r"((?:src|scripts|server)/[\w./-]+\.(?:ts|tsx|mjs|js))[(:](\d+)")


def code_context(repo, log_text, radius=30):
    """The deepest traceback frame inside the repo, with line numbers."""
    hits = [(f, int(n)) for f, n in TRACE_RE.findall(log_text)] + \
           [(f, int(n)) for f, n in TS_RE.findall(log_text)]
    for f, n in reversed(hits):
        full = f if os.path.isabs(f) else os.path.join(repo, f)
        full = os.path.realpath(full)
        if full.startswith(os.path.realpath(repo) + os.sep) and os.path.isfile(full):
            with open(full, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            a, b = max(1, n - radius), min(len(lines), n + radius)
            body = "\n".join("%5d| %s" % (i, lines[i - 1]) for i in range(a, b + 1))
            return "%s (line %d)\n%s" % (os.path.relpath(full, repo), n, body)
    return "(no frame inside the repository found in the log)"


def chat(prompt):
    payload = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False,
               "think": True, "options": {"temperature": 0.6, "top_p": 0.95, "top_k": 20,
                                          "num_ctx": NUM_CTX, "num_predict": 4096}}
    req = urllib.request.Request(CHAT_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 400:
            payload.pop("think")
            req = urllib.request.Request(CHAT_URL, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1800) as r:
                out = json.loads(r.read())
        else:
            raise
    txt = (out.get("message") or {}).get("content") or ""
    return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()


def triage(repo, failures, out_dir):
    path = os.path.join(out_dir, "TRIAGE.txt")
    lines = ["FAILURE TRIAGE — model %s (advisory; the logs are the facts)" % MODEL, "=" * 70]
    for name, rc, _, log in failures:
        with open(log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        tail = "\n".join(text.splitlines()[-150:])
        prompt = ("A test step failed. Say in a few lines (1) whether this is an ENVIRONMENT problem "
                  "(missing module, wrong numpy/GDAL/Qt version, missing LISFLOOD-FP/ANUGA, network) "
                  "or a CODE bug, (2) the most likely cause, citing log lines and code line numbers, "
                  "(3) the smallest next check to confirm it. Do not invent code you cannot see; say "
                  "UNSURE when unsure.\n\nSTEP: %s (exit %d)\n\n=== LOG TAIL ===\n%s\n\n=== CODE ===\n%s"
                  % (name, rc, tail, code_context(repo, text)))
        print("triage: %s ..." % name, flush=True)
        try:
            ans = chat(prompt)
        except Exception as e:
            ans = "TRIAGE CALL FAILED: %s" % e
        lines += ["", "-" * 70, "STEP: %s (exit %d)  log: %s" % (name, rc, log), ans]
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    print("Triage: %s" % path)


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Run the dam-suite tests/runs in conda envs.")
    ap.add_argument("target", choices=["nd", "dwr", "runner", "crest"])
    ap.add_argument("action", nargs="?", default=None, help="runner: selftest | check | run | estimate | hydrograph")
    ap.add_argument("config", nargs="?", default=None, help="runner: config JSON for run/estimate/hydrograph")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--qgis-env", default="ndqgis")
    ap.add_argument("--anuga-env", default="ndanuga")
    ap.add_argument("--node-env", default="ndnode")
    ap.add_argument("--lisflood", default="")
    ap.add_argument("--threads", type=int, default=1, help="OpenMP threads (1 is the efficient choice)")
    ap.add_argument("--only", default="", help="nd/dwr: only suites/tests whose name contains this")
    ap.add_argument("--models", action="store_true", help="nd/dwr: also run the model tests")
    ap.add_argument("--gui", action="store_true", help="nd/dwr: also run tools/gui_lint.py")
    ap.add_argument("--desktop", action="store_true", help="crest: also the desktop smoke test")
    ap.add_argument("--validate", nargs=2, metavar=("DAM_FOLDER", "FLOOD_GEOJSON"),
                    help="crest: score a Crest layer against a Runner run")
    ap.add_argument("--triage", action="store_true", help="ask the local model to explain failures")
    args = ap.parse_args()

    conda = conda_exe()
    steps = Steps(args.target)
    if args.target in ("nd", "dwr"):
        target_plugins(args, conda, steps)
    elif args.target == "runner":
        if args.action not in ("selftest", "check", "run", "estimate", "hydrograph"):
            sys.exit("runner needs an action: selftest | check | run CONFIG | estimate CONFIG | hydrograph CONFIG")
        if args.action in ("run", "estimate", "hydrograph") and not args.config:
            sys.exit("runner %s needs a config JSON (make one: runner template)" % args.action)
        if args.config:
            args.config = os.path.abspath(os.path.expanduser(args.config))
        target_runner(args, conda, steps)
    else:
        target_crest(args, conda, steps)
    bad = steps.summary()
    if bad and args.triage:
        triage(os.path.abspath(os.path.expanduser(args.repo)), bad, steps.dir)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
SCRIPT_EOF_SUITE

  cat > "$LOCAL_AI/dam_chain.py" << 'SCRIPT_EOF_CHAIN'
#!/usr/bin/env python3
# dam_chain.py v1.4 — run both dam-plugin sets headless from Terminal (macOS, conda), stdlib only.
#
#   dam_chain.py setup                       one-time: batch profiles that point at your QGIS
#                                            profiles "nd" and "damsafety", plugins enabled
#   dam_chain.py plugins --set nd|dwr        load every plugin of the set, check the chain's
#                                            algorithms are there, run the self-tests
#   dam_chain.py open --set nd|dwr           open QGIS (GUI) with that set's profile
#   dam_chain.py seed --dams-folder D --csv dams.csv
#                                            make <D>/<dam_id>/dam.json for each row (never overwrites)
#   dam_chain.py run --set nd  --dams-folder D --ids DWR-2801,DWR-4031
#                                            per dam: Get data -> Screening -> Runner -> Hazard
#                                            (ndhazard:getdamdata, then ndhazard:suitechain)
#                                            --hi-res downloads a 1 m DEM (DEM_CELL_M=1).
#                                            --triage asks the local Ollama model to explain a
#                                            failed step (writes TRIAGE.txt next to the run log).
#                                            Defaults can live in the environment (a --flag still wins):
#                                              DAMCHAIN_GETDATA_PARAM, DAMCHAIN_BREACH_PARAM,
#                                              DAMCHAIN_SCREEN_PARAM, DAMCHAIN_CLASSIFY_PARAM
#                                            each a space-separated list of KEY=VALUE, e.g.
#                                              export DAMCHAIN_GETDATA_PARAM="DEM_CELL_M=1 DEM_MI=15"
#   dam_chain.py run --set dwr --dams LAYER --ids 2801,4031 --out D --dem DEM --screen-work W
#                                            the DamSafety Runner's own list chain
#                                            (ndbreach:chainbatch: Screening -> Runner -> Hazard)
#   dam_chain.py research ask|research|search|ingest|chat|list ...
#                                            the literature research assistant (damresearch, a
#                                            separate model and library from lumo); same as typing
#                                            damresearch ...  (v1.4)
#   dam_chain.py crest --set nd|dwr          open the Crest desk wired to that set: its Report-step
#                                            buttons (dam folder, screening, flood model) run that
#                                            set's plugins, in ~/CrestDams/<set>
#
# Every run goes through qgis_process in the conda env (ndqgis), with the set's own QGIS
# profile, so ND and DamSafety never load together (they share Processing ids).
# --dry-run prints the commands without running them. Logs: <dams folder>/_chain_logs/.
import os, sys, re, csv, json, time, shutil, argparse, subprocess, threading, urllib.request, urllib.error
from datetime import datetime

QGIS_PROFILES = os.path.expanduser(os.environ.get("DAMCHAIN_QGIS_PROFILES",
                                   "~/Library/Application Support/QGIS/QGIS3/profiles"))
BATCH_ROOT = os.path.expanduser(os.environ.get("DAMCHAIN_BATCH_ROOT", "~/local-ai/qgis-batch"))
CREST_DIR = os.path.expanduser(os.environ.get("DAMCHAIN_CREST_DIR", "~/local-ai/crest-desktop"))

# Local Ollama triage (shared convention with suite_check.py / audit_plugin.py): optional, advisory.
LUMO_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/chat"
LUMO_MODEL = os.environ.get("LUMO_MODEL", "lumo")
LUMO_NUM_CTX = int(os.environ.get("LUMO_NUM_CTX", "16384"))
SETS = {
    "nd": {"profile": "nd", "runner": "nd_breach_runner",
           "plugins": ["ND_Dam_Hazard_Helper", "nd_breach_runner", "nd_dam_screening",
                       "nd_data_updater"],
           "needs": ["ndhazard:getdamdata", "ndhazard:suitechain", "nddam:runchain",
                     "ndbreach:rundam", "ndhazard:classify", "ndbreach:selftest", "nddam:selftest"]},
    "dwr": {"profile": "damsafety", "runner": "DamSafety_Runner",
            "plugins": ["DamSafety_Setup", "DamSafety_Data", "DamSafety_Screening",
                        "DamSafety_Runner", "DamSafety_Hazard", "DamSafety_HazardLite",
                        "DamSafety_PMP"],
            "needs": ["ndbreach:chainbatch", "nddam:runchain", "ndbreach:batch",
                      "ndhazard:classify", "ndbreach:selftest", "nddam:selftest"]},
}
DWR_HEC = {"run": 0, "wait": 1, "skip": 2}
DWR_STOP = {"end": 0, "screening": 1, "runner": 2}
DEM_UNITS = {"detect": 0, "feet": 1, "us-feet": 2, "meters": 3}


# ------------------------------------------------------------------ plumbing
def conda_exe():
    for c in (os.environ.get("CONDA_EXE"), shutil.which("conda"),
              os.path.expanduser("~/miniconda3/bin/conda"), os.path.expanduser("~/miniconda/bin/conda"),
              os.path.expanduser("~/miniforge3/bin/conda")):
        if c and os.path.exists(c):
            return c
    sys.exit("conda not found. Open a Terminal where `conda` works, or set CONDA_EXE.")


def env_prefix(conda, name):
    out = subprocess.run([conda, "env", "list", "--json"], capture_output=True, text=True)
    try:
        for p in json.loads(out.stdout)["envs"]:
            if os.path.basename(p) == name:
                return p
    except (ValueError, KeyError):
        pass
    sys.exit("conda env '%s' not found — run the suite installer first." % name)


def find_qgis_process(prefix, override=""):
    if override:
        return override
    for c in (os.path.join(prefix, "bin", "qgis_process"),
              os.path.join(prefix, "QGIS.app", "Contents", "MacOS", "bin", "qgis_process"),
              os.path.join(prefix, "QGIS.app", "Contents", "MacOS", "qgis_process")):
        if os.path.isfile(c):
            return c
    return "qgis_process"          # on the env's PATH after activation


class QP:
    """qgis_process inside the conda env, with one set's batch profile."""

    def __init__(self, args, set_name):
        self.dry = args.dry_run
        self.quiet = getattr(args, "quiet", False)
        if self.dry:
            self.conda, self.prefix = shutil.which("conda") or "conda", "<%s>" % args.env
        else:
            self.conda = conda_exe()
            self.prefix = env_prefix(self.conda, args.env)
        self.qp = find_qgis_process(self.prefix, args.qgis_process)
        self.base = os.path.join(BATCH_ROOT, set_name)
        self.env = dict(os.environ)
        self.env.pop("PYTHONPATH", None)
        self.env.update({"QGIS_CUSTOM_CONFIG_PATH": self.base, "QT_QPA_PLATFORM": "offscreen",
                         "OMP_NUM_THREADS": str(args.threads), "NDBREACH_ANUGA_MESHER": "gmsh"})
        self.envname = args.env
        lis = os.path.expanduser(args.lisflood or "")
        if lis and os.path.isfile(lis):
            # Also read by tools that get no EXE parameter (DamSafety Screening's self-test before 0.18.5).
            self.env["NDBREACH_LISFLOOD"] = lis

    def argv(self, *qp_args):
        return [self.conda, "run", "--no-capture-output", "-n", self.envname, self.qp] + list(qp_args)

    @staticmethod
    def _is_noise(t):
        return (not t) or ("XDG_RUNTIME_DIR" in t) or t.startswith("Warning 1:") \
            or t.startswith("QStandardPaths") or t.startswith("Warning:")

    def run(self, alg, params, log_path, timeout=None, label=""):
        """Returns (ok, results dict, last error lines). Streams the plugin's own progress lines
        to the terminal (unless --quiet) with a heartbeat, so a long step never looks frozen."""
        cmd = self.argv("--json", "run", alg, "--") + ["%s=%s" % (k, v) for k, v in params.items()]
        shown = " ".join(_q(c) for c in cmd)
        if self.dry:
            print("  DRY  QGIS_CUSTOM_CONFIG_PATH=%s %s" % (_q(self.base), shown))
            return True, {}, ""
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        tag = label or alg
        out_chunks, start, last, in_json = [], time.time(), [time.time()], [False]
        with open(log_path, "w", encoding="utf-8") as log:
            log.write("$ %s\n\n" % shown)
            log.flush()
            try:
                p = subprocess.Popen(cmd, env=self.env, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
            except OSError as e:
                log.write("\n[LAUNCH FAILED] %s\n" % e)
                return False, {}, str(e)

            def pump_out():
                for line in p.stdout:
                    out_chunks.append(line)
                    last[0] = time.time()
                    t = line.strip()
                    if t.startswith("{"):
                        in_json[0] = True           # the --json result block begins; stop echoing
                    if not self.quiet and not in_json[0] and not self._is_noise(t):
                        sys.stdout.write("      %s\n" % t)
                        sys.stdout.flush()

            def pump_err():
                for line in p.stderr:
                    log.write(line)
                    log.flush()

            stop = threading.Event()

            def beat():
                while not stop.wait(20):
                    if not self.quiet and time.time() - last[0] >= 18:
                        sys.stdout.write("      ... %s running (%ds elapsed)\n"
                                         % (tag, int(time.time() - start)))
                        sys.stdout.flush()

            threads = [threading.Thread(target=f, daemon=True) for f in (pump_out, pump_err, beat)]
            for th in threads:
                th.start()
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                stop.set()
                threads[0].join(2); threads[1].join(2)
                log.write("\n[TIMEOUT]\n")
                return False, {}, "timeout"
            stop.set()
            threads[0].join(2); threads[1].join(2)
            out = "".join(out_chunks)
            log.write("\n--- stdout ---\n" + (out or ""))
        res = _json_results(out)
        if p.returncode == 0 and res.get("OK") is not False:
            return True, res, ""
        if p.returncode == 0:      # the algorithm finished but reports a failure (self-tests)
            return False, res, "failed step %s, see %s" % (res.get("FAILED_STEP", "?"),
                                                           res.get("REPORT") or log_path)
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            tail = []
            for l in fh.read().split("\n--- stdout ---")[0].splitlines():
                l = l.strip()
                if l and not self._is_noise(l) and (not tail or tail[-1] != l):
                    tail.append(l)
        return False, res, " | ".join(tail[-2:])

    def plain(self, *qp_args):
        cmd = self.argv(*qp_args)
        if self.dry:
            print("  DRY  %s" % " ".join(_q(c) for c in cmd))
            return 0, ""
        p = subprocess.run(cmd, env=self.env, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr


def _json_results(out):
    """qgis_process --json results. Plugins print log lines to stdout before the JSON, so parse
    from the first line that is just "{"."""
    out = out or ""
    for start in [0] + [m.start() + 1 for m in re.finditer(r"\n\{", out)]:
        try:
            return json.loads(out[start:]).get("results") or {}
        except (ValueError, AttributeError):
            continue
    return {}


def _q(s):
    s = str(s)
    return s if re.fullmatch(r"[\w@%+=:,./-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def _lumo_chat(prompt, timeout=1800):
    """Ask the local Ollama model once; return its text (thinking stripped). Raises on transport error."""
    payload = {"model": LUMO_MODEL, "messages": [{"role": "user", "content": prompt}],
               "stream": False, "think": True,
               "options": {"temperature": 0.6, "top_p": 0.95, "top_k": 20,
                           "num_ctx": LUMO_NUM_CTX, "num_predict": 4096}}
    def _post(pl):
        req = urllib.request.Request(LUMO_URL, data=json.dumps(pl).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    try:
        out = _post(payload)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        payload.pop("think")                       # some models reject the think flag
        out = _post(payload)
    txt = (out.get("message") or {}).get("content") or ""
    return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()


def triage_failure(step_name, err, log_path, out_dir):
    """Send a failed step's log tail to the local model and append an advisory note to TRIAGE.txt.
    Advisory only: the log is the fact. Never raises (a triage problem must not fail the run)."""
    tail = ""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            tail = "\n".join(fh.read().splitlines()[-150:])
    except Exception:                                                        # noqa
        tail = "(log not readable: %s)" % log_path
    prompt = ("A dam-suite step failed while running headless in QGIS (qgis_process). In a few lines, "
              "say (1) whether this is an ENVIRONMENT/DATA problem (no offline pack or chosen layers, "
              "missing DEM, DEM no-data at the flood edge, LISFLOOD-FP/ANUGA missing, wrong DEM units, "
              "network/proxy) or a CODE bug, (2) the most likely cause citing log lines, (3) the "
              "smallest next check or fix. Do not invent anything you cannot see in the log; say UNSURE "
              "when unsure.\n\nSTEP: %s\nERROR: %s\n\n=== LOG TAIL ===\n%s"
              % (step_name, err or "(none)", tail))
    print("   triage (%s): asking %s ..." % (step_name, LUMO_MODEL), flush=True)
    try:
        ans = _lumo_chat(prompt)
    except Exception as e:                                                   # noqa
        ans = ("TRIAGE UNAVAILABLE: could not reach Ollama at %s (%s). Start Ollama and pull the "
               "model, or set OLLAMA_URL / LUMO_MODEL." % (LUMO_URL, e))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "TRIAGE.txt")
    block = ["", "=" * 72, "%s  STEP: %s" % (_stamp(), step_name),
             "error: %s" % (err or "(none)"), "log:   %s" % log_path,
             "model: %s (advisory; the log is the fact)" % LUMO_MODEL, "-" * 72, ans]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(block) + "\n")
    print("   triage -> %s" % path, flush=True)
    for ln in ans.splitlines()[:6]:
        print("      | " + ln, flush=True)
    return path


def parse_kv(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            sys.exit("--param needs KEY=VALUE, got: %s" % it)
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def _env_kv(env_name):
    """KEY=VALUE defaults from an environment variable, split on whitespace.
    e.g.  export DAMCHAIN_BREACH_PARAM="CELL=10 MANNING=0.06"
    """
    raw = (os.environ.get(env_name) or "").strip()
    return [tok for tok in re.split(r"\s+", raw) if tok] if raw else []


def kv(items, env_name=None):
    """parse_kv with environment defaults applied first; a --flag value overrides the env."""
    out = {}
    if env_name:
        out.update(parse_kv(_env_kv(env_name)))
    out.update(parse_kv(items))
    return out


def read_ids(args):
    ids = [i.strip() for i in (args.ids or "").split(",") if i.strip()]
    if args.ids_csv:
        with open(os.path.expanduser(args.ids_csv), newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                v = (row.get("dam_id") or "").strip()
                if v and v not in ids:
                    ids.append(v)
    return ids


def write_atomic(path, text):
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- commands
def cmd_setup(args):
    """~/local-ai/qgis-batch/<set>/profiles/default -> your real QGIS profile, plugins enabled."""
    for set_name, spec in SETS.items():
        prof = getattr(args, "%s_profile" % set_name) or spec["profile"]
        real = os.path.join(QGIS_PROFILES, prof)
        link_dir = os.path.join(BATCH_ROOT, set_name, "profiles")
        link = os.path.join(link_dir, "default")
        if not os.path.isdir(real):
            print("! %s: no QGIS profile '%s' at %s — install that set first (skipped)" % (set_name, prof, real))
            continue
        os.makedirs(link_dir, exist_ok=True)
        if os.path.islink(link):
            os.remove(link)
        elif os.path.exists(link):
            sys.exit("%s exists and is not a link; move it away first" % link)
        os.symlink(real, link)
        print("%s: batch profile -> %s" % (set_name, real))
        qp = QP(args, set_name)
        for p in spec["plugins"]:
            if os.path.isdir(os.path.join(real, "python", "plugins", p)):
                qp.plain("plugins", "enable", p)
        print("   plugins enabled for qgis_process")


def cmd_plugins(args):
    spec = SETS[args.set]
    qp = QP(args, args.set)
    rc, out = qp.plain("list")
    if not args.dry_run:
        have = set(re.findall(r"\b(\w+:\w+)\b", out))
        for alg in spec["needs"]:
            print("  %-4s %s" % ("ok" if alg in have else "MISSING", alg))
        providers = sorted({a.split(":")[0] for a in have if a.split(":")[0].startswith("nd")})
        print("  providers loaded: %s" % ", ".join(providers))
        missing = [a for a in spec["needs"] if a not in have]
        if missing:
            print("  -> run `dam_chain.py setup`, and check the plugins are installed in profile '%s'"
                  % spec["profile"])
    if args.selftest:
        logs = os.path.join(os.path.expanduser(args.log_dir), "selftest_%s_%s" % (args.set, _stamp()))
        lis = os.path.expanduser(args.lisflood)
        # ND Screening takes the step-7 cell in feet (CELL_FT), DamSafety Screening in metres (CELL).
        cell = {"CELL_FT": 100} if args.set == "nd" else {"CELL": 30}
        scr = dict(cell, ENGINE=2, SIMH=2, REPORT=os.path.join(logs, "nddam_selftest.txt"))
        run = {"RUN_MODEL": "true", "THREADS": args.threads}
        if os.path.isfile(lis):                  # test the real engine too, not only the built-in one
            scr.update(ENGINE=0, EXE=lis)        # both sets take EXE (DamSafety Screening from 0.18.5)
            run["LISFLOOD_EXE"] = lis
        for alg, params in (("ndbreach:selftest", run), ("nddam:selftest", scr)):
            ok, res, err = qp.run(alg, params, os.path.join(logs, alg.replace(":", "_") + ".log"),
                                  timeout=1800)
            print("  %-4s %s  %s" % ("PASS" if ok else "FAIL", alg,
                                     res.get("RESULT") or res.get("SUMMARY") or err))
        if not args.dry_run:
            print("  logs: %s" % logs)


def cmd_open(args):
    spec = SETS[args.set]
    conda = conda_exe()
    prefix = env_prefix(conda, args.env)
    app = os.path.join(prefix, "QGIS.app", "Contents", "MacOS", "QGIS")
    exe = app if os.path.isfile(app) else "qgis"
    cmd = [conda, "run", "-n", args.env, "env", "NDBREACH_ANUGA_MESHER=gmsh", exe,
           "--profile", getattr(args, "profile", None) or spec["profile"]]
    print(" ".join(_q(c) for c in cmd))
    if not args.dry_run:
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        subprocess.Popen(cmd, env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_pack(args):
    """Build / refresh the offline data pack with the ND Data Updater plugin, into the data folder.
    The pack build (structures, roads, rail, 3DHP, ABI barriers, NLCD, counties ...) is a statewide, multi-
    gigabyte download; it has no headless processing algorithm, so this opens QGIS to the Updater,
    which creates the pack in the folder you point it at and tells the dam plugins to read it there."""
    folder = os.path.expanduser(args.data_folder) if args.data_folder else "~/ND_Suite_Data"
    bar = "=" * 72
    print(bar)
    print("Build the offline pack into:  %s" % folder)
    print(bar)
    print("QGIS is opening with the '%s' profile. In QGIS:" % (getattr(args, "profile", None) or SETS[args.set]["profile"]))
    print("  1. Plugins menu > ND Data Updater > 'Settings and status...'.")
    print("  2. Local data > 'Choose...' > %s  (the three dam plugins follow it)." % folder)
    print("  3. Keep 'Offline pack for the ND dam plugins (all layers)' and")
    print("     'Make the ND dam plugins read this pack' ticked; 3DHP on 'auto'.")
    print("  4. Click 'Save and Check All Now'. Statewide, several GB, an hour or more the")
    print("     first time; 'Rebuild Only Missing Layers' resumes an interrupted build.")
    print("  The pack lands at %s/ND_Dam_Suite_Pack/; 'Offline pack and data folder...'" % folder)
    print("  in the same menu shows it layer by layer. Every later run reads it offline.")
    print(bar)
    if getattr(args, "print_only", False):
        return
    cmd_open(args)


def cmd_seed(args):
    """dam.json per CSV row. Columns: dam_id (required), name, lat, lon, height_ft, storage_acft,
    dam_length_ft, dwr_structure_id, nid_id, existing_hazard_class. Existing dam.json is kept."""
    dams = os.path.expanduser(args.dams_folder)
    num = {"lat", "lon", "height_ft", "storage_acft", "dam_length_ft"}
    made = kept = 0
    if not os.path.isfile(os.path.expanduser(args.csv)):
        sys.exit("No CSV at %s. Make one with a header line, e.g.\n"
                 "  dam_id,name,lat,lon,height_ft,storage_acft,dwr_structure_id,nid_id\n"
                 "  DWR-2801,Swan Buffalo 12,46.984382,-97.390918,27.5,1615.344,2801,ND00199"
                 % os.path.expanduser(args.csv))
    with open(os.path.expanduser(args.csv), newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            did = (row.get("dam_id") or "").strip()
            if not did:
                continue
            folder = os.path.join(dams, did)
            path = os.path.join(folder, "dam.json")
            if os.path.exists(path):
                kept += 1
                continue
            fields = {"dam_id": did}
            for k, v in row.items():
                if k == "dam_id" or v is None or not str(v).strip():
                    continue
                k = k.strip()
                try:
                    fields[k] = float(v) if k in num else str(v).strip()
                except ValueError:
                    sys.exit("%s: %s is not a number: %r" % (did, k, v))
            if "lat" not in fields or "lon" not in fields:
                print("! %s: no lat/lon — skipped" % did)
                continue
            rec = {"format": "nd_dam_suite/dam_record", "format_version": 1, "fields": fields,
                   "sources": {"_seed": {"tool": "dam_chain.py seed", "when": _stamp()[:8],
                                         "note": "from %s" % os.path.basename(args.csv)}},
                   "status": []}
            if args.dry_run:
                print("  DRY  %s" % path)
                continue
            os.makedirs(folder, exist_ok=True)
            write_atomic(path, json.dumps(rec, indent=1) + "\n")
            made += 1
    print("dam.json written: %d, already there (kept): %d" % (made, kept))


def run_nd(args):
    dams = os.path.abspath(os.path.expanduser(args.dams_folder))
    ids = read_ids(args) or sorted(d for d in os.listdir(dams)
                                   if os.path.isfile(os.path.join(dams, d, "dam.json")))
    if not ids:
        sys.exit("No dams: give --ids / --ids-csv, or seed dam folders first.")
    qp = QP(args, "nd")
    logs = os.path.join(dams, "_chain_logs", _stamp())
    summary = os.path.join(dams, "Chain_Summary_ND.csv")
    rows = []
    flags = {"SCREEN": str(not args.no_screen).lower(), "BREACH": str(not args.no_breach).lower(),
             "CLASSIFY": str(not args.no_classify).lower()}
    _env = {"SCREEN_PARAMETERS": "DAMCHAIN_SCREEN_PARAM",
            "BREACH_PARAMETERS": "DAMCHAIN_BREACH_PARAM",
            "CLASSIFY_PARAMETERS": "DAMCHAIN_CLASSIFY_PARAM"}
    for k, items in (("SCREEN_PARAMETERS", args.screen_param), ("BREACH_PARAMETERS", args.breach_param),
                     ("CLASSIFY_PARAMETERS", args.classify_param)):
        kvv = kv(items, _env[k])
        if kvv:
            flags[k] = json.dumps(kvv)
    for n, did in enumerate(ids, 1):
        folder = os.path.join(dams, did)
        state_path = os.path.join(folder, "_chain_nd", "state.json")
        t0 = time.time()
        print("[%d/%d] %s" % (n, len(ids), did), flush=True)
        if not os.path.isfile(os.path.join(folder, "dam.json")):
            rows.append([did, "no dam.json", "", "0", "", "seed it first (dam_chain.py seed)"])
            print("   no dam.json — skipped")
            continue
        if not args.redo and os.path.isfile(state_path):
            with open(state_path, encoding="utf-8") as fh:
                st = json.load(fh)
            if st.get("status") == "done":
                rows.append([did, "done (earlier run)", st.get("nd_class", ""), "0", st.get("log", ""), ""])
                print("   done earlier (class %s) — skipped; --redo to run again" % st.get("nd_class", "?"))
                continue
        status, nd_class, err, log = "done", "", "", ""
        if not args.no_getdata:
            gp = {"DAM_FOLDER": folder}
            if args.data_folder:
                gp["DATA_FOLDER"] = os.path.expanduser(args.data_folder)
            gp.update(kv(args.getdata_param, "DAMCHAIN_GETDATA_PARAM"))
            if getattr(args, "hi_res", False) and "DEM_CELL_M" not in gp:
                gp["DEM_CELL_M"] = "1"
            log = os.path.join(logs, "%s_1_getdamdata.log" % did)
            ok, _r, err = qp.run("ndhazard:getdamdata", gp, log, timeout=args.timeout_min * 60,
                                  label="get data for %s" % did)
            print("   get data: %s" % ("ok" if ok else "FAILED " + err))
            if not ok:
                status = "failed: get data"
                if getattr(args, "triage", False):
                    triage_failure("get data (%s)" % did, err, log, logs)
        if status == "done":
            cp = dict(flags, DAM_FOLDER=folder)
            cp.update(parse_kv(args.param))
            log = os.path.join(logs, "%s_2_suitechain.log" % did)
            ok, res, err = qp.run("ndhazard:suitechain", cp, log, timeout=args.timeout_min * 60,
                                   label="screening/runner/hazard for %s" % did)
            nd_class = str(res.get("ND_CLASS") or "")
            print("   chain: %s" % (("ok, ND class %s" % (nd_class or "?")) if ok else "FAILED " + err))
            if not ok:
                status = "failed: chain"
                if getattr(args, "triage", False):
                    triage_failure("chain (%s)" % did, err, log, logs)
        mins = "%.1f" % ((time.time() - t0) / 60)
        rows.append([did, status, nd_class, mins, log, err])
        if not args.dry_run:
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            write_atomic(state_path, json.dumps({"status": "done" if status == "done" else "failed",
                                                 "nd_class": nd_class, "log": log, "error": err,
                                                 "when": _stamp()}, indent=1))
            _write_summary(summary, rows)
        if status != "done" and args.stop_on_error:
            break
    if not args.dry_run:
        print("\nSummary: %s" % summary)


def _write_summary(path, rows):
    """One row per dam: this run's rows replace earlier rows of the same dam, others are kept."""
    import io
    head = ["dam_id", "status", "nd_class", "minutes", "log", "error"]
    merged = {}
    if os.path.isfile(path):
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.reader(fh):
                if r and r[0] != "dam_id":
                    merged[r[0]] = r
    for r in rows:
        merged[r[0]] = r
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(merged[k] for k in sorted(merged))
    write_atomic(path, buf.getvalue())


def run_dwr(args):
    if not args.dams:
        sys.exit("--set dwr needs --dams (a points layer: .gpkg, .shp, .geojson, or a 'path|layername=x')")
    p = {"DAMS": os.path.expanduser(args.dams), "OUT_FOLDER": os.path.expanduser(args.out or ""),
         "HEC_MODE": DWR_HEC[args.hec], "STOP_AFTER": DWR_STOP[args.stop_after],
         "RUNNER_MODELS": 1 if args.anuga else 0, "SCREENING": str(not args.no_screen).lower(),
         "KEEP_GOING": str(not args.stop_on_error).lower(), "SKIP_DONE": str(not args.redo).lower(),
         "CHECK_ONLY": str(args.check_only).lower()}
    if not p["OUT_FOLDER"]:
        sys.exit("--set dwr needs --out (the dams folder the chain writes into)")
    for key, val in (("DAM_ID_FIELD", args.id_field), ("DAM_IDS", args.ids),
                     ("IDS_CSV", args.ids_csv), ("DEM", args.dem), ("SCREEN_WORK", args.screen_work),
                     ("SETTINGS_FILE", args.settings_file), ("OVERRIDES_CSV", args.overrides_csv)):
        if val:
            p[key] = os.path.expanduser(val) if key not in ("DAM_IDS", "DAM_ID_FIELD") else val
    p.update(parse_kv(args.param))
    # Per-tool settings (e.g. the Runner's DEM units) reach chainbatch only through the
    # settings file or an overrides CSV; write the overrides CSV for the listed dams.
    tool_kv = {"ndbreach.batch.%s" % k: v for k, v in parse_kv(args.runner_param).items()}
    tool_kv.update({"ndhazard.classify.%s" % k: v for k, v in parse_kv(args.classify_param).items()})
    if args.dem_units:
        tool_kv["ndbreach.batch.DEM_Z"] = DEM_UNITS[args.dem_units]
    logs = os.path.join(p["OUT_FOLDER"], "_chain_logs")
    if tool_kv:
        ids = read_ids(args)
        if not ids:
            sys.exit("--dem-units / --runner-param / --classify-param need the dams named (--ids or --ids-csv)")
        if args.overrides_csv:
            sys.exit("give either --overrides-csv or --dem-units/--runner-param/--classify-param, not both")
        ov = os.path.join(logs, "overrides_%s.csv" % _stamp())
        cols = sorted(tool_kv)
        lines = [",".join(["dam_id"] + cols)] + [",".join([i] + [str(tool_kv[c]) for c in cols]) for i in ids]
        if not args.dry_run:
            os.makedirs(logs, exist_ok=True)
            write_atomic(ov, "\n".join(lines) + "\n")
        p["OVERRIDES_CSV"] = ov
    qp = QP(args, "dwr")
    log = os.path.join(logs, "chainbatch_%s.log" % _stamp())
    ok, res, err = qp.run("ndbreach:chainbatch", p, log, label="DamSafety chain")
    if args.dry_run:
        return
    counts = str(res.get("COUNTS") or "")
    m = re.search(r"(\d+)\s+failed", counts)
    if ok and (m and int(m.group(1)) > 0 or "error" in counts):
        ok = False                       # the chain finished, but some dams (or the preflight) failed
    print("%s  %s" % ("OK" if ok else "FAILED", counts or err))
    print("log: %s" % log)
    if res.get("SUMMARY"):
        print("summary: %s" % res["SUMMARY"])
    if not ok and getattr(args, "triage", False):
        triage_failure("DamSafety chain", err or counts, log, os.path.dirname(log))
    sys.exit(0 if ok else 1)


def _stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _wrapper(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n# written by dam_chain.py crest; safe to delete (it is rewritten)\n")
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)


def crest_env(args, set_name):
    """Wrapper scripts + environment that point the Crest desk at one plugin set.
    Crest calls `qgis_process` (QGIS_PROCESS) and the Runner's ndbreach_cli.py (CREST_NDBREACH_CLI,
    run with CREST_PYTHON); both wrappers run inside the conda env with that set's batch profile."""
    spec = SETS[set_name]
    qp = QP(args, set_name)
    base = qp.base
    real = os.path.realpath(os.path.join(base, "profiles", "default"))
    if not os.path.isdir(real):
        sys.exit("No batch profile for '%s' at %s: run `dam_chain.py setup` first." % (set_name, base))
    cli = os.path.join(real, "python", "plugins", spec["runner"], "scripts", "ndbreach_cli.py")
    if not os.path.isfile(cli):
        sys.exit("%s not found: is the %s set installed in profile '%s'?" % (cli, set_name, spec["profile"]))
    exports = ["export QGIS_CUSTOM_CONFIG_PATH=%s" % _q(base), "export QT_QPA_PLATFORM=offscreen",
               "export NDBREACH_ANUGA_MESHER=gmsh", "export OMP_NUM_THREADS=%d" % args.threads,
               "unset PYTHONPATH"]
    if qp.env.get("NDBREACH_LISFLOOD"):
        exports.append("export NDBREACH_LISFLOOD=%s" % _q(qp.env["NDBREACH_LISFLOOD"]))
    bindir = os.path.join(base, "bin")
    run = "exec %s run --no-capture-output -n %s" % (_q(qp.conda), _q(qp.envname))
    _wrapper(os.path.join(bindir, "qgis_process"), exports + ['%s %s "$@"' % (run, _q(qp.qp))])
    _wrapper(os.path.join(bindir, "python3"), exports + ['%s python "$@"' % run])
    work = os.path.expanduser(args.work or os.path.join("~/CrestDams", set_name))
    os.makedirs(work, exist_ok=True)
    env = {"QGIS_PROCESS": os.path.join(bindir, "qgis_process"),
           "CREST_PYTHON": os.path.join(bindir, "python3"),
           "CREST_NDBREACH_CLI": cli,
           "CREST_WORK": work,
           "CREST_WORK_ROOTS": os.pathsep.join([os.path.expanduser("~/CrestDams"), work])}
    if args.data_folder:
        env["NDSUITE_DATA"] = os.path.expanduser(args.data_folder)
    return env


def cmd_crest(args):
    env = crest_env(args, args.set)
    for k in sorted(env):
        print("export %s=%s" % (k, _q(env[k])))
    if args.print_env:
        return
    crest = os.path.expanduser(args.crest_dir)
    run_py = os.path.join(crest, "run.py")
    if not os.path.isfile(run_py):
        sys.exit("Crest desk not found at %s (run.py). Install it with install_all.zsh or pass --crest-dir." % crest)
    py = args.crest_python
    if not py:
        try:
            py = os.path.join(env_prefix(conda_exe(), "ndcrest"), "bin", "python")
        except SystemExit:
            py = sys.executable              # no ndcrest env: the desk opens in the browser instead
    full = dict(os.environ)
    full.pop("PYTHONPATH", None)
    full.update(env)
    print("Crest desk -> %s plugins (profile '%s'), work folder %s. Leave this Terminal open."
          % ("ND" if args.set == "nd" else "DamSafety", SETS[args.set]["profile"], env["CREST_WORK"]))
    if args.dry_run:
        print("  DRY  %s %s" % (_q(py), _q(run_py)))
        return
    sys.exit(subprocess.call([py, run_py], cwd=crest, env=full))


def research_passthrough(argv):
    """`damchain research ...` -> the damresearch tool, with its own Python (ndresearch env)."""
    tool = os.path.expanduser(os.environ.get("DAMRESEARCH_TOOL", "~/local-ai/research/damresearch.py"))
    py = os.path.expanduser(os.environ.get("DAMRESEARCH_PY", "") or "~/local-ai/research/python")
    if not os.path.isfile(tool):
        sys.exit("damresearch is not installed. Run: zsh setup-dam-research.zsh (from the dam-research folder)")
    if not os.path.exists(py):
        py = sys.executable
    os.execv(py, [py, tool] + argv)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "research":
        research_passthrough(sys.argv[2:])
    ap = argparse.ArgumentParser(description="Run the ND and DamSafety dam plugins headless. "
                                             "`research ...` runs the literature assistant (damresearch).")
    ap.add_argument("--env", default="ndqgis", help="conda env with QGIS (default ndqgis)")
    ap.add_argument("--qgis-process", default="", help="path to qgis_process (default: found in the env)")
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--lisflood", default="~/nd-dam-suite/bin/lisflood",
                    help="LISFLOOD-FP binary (missing = the plugins' own choice / built-in engine)")
    ap.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    ap.add_argument("--quiet", action="store_true", help="don't stream each step's progress lines to the terminal (the full log is still written)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup")
    s.add_argument("--nd-profile", default="")
    s.add_argument("--dwr-profile", default="")

    s = sub.add_parser("plugins")
    s.add_argument("--set", choices=SETS, required=True)
    s.add_argument("--selftest", action="store_true")
    s.add_argument("--log-dir", default="~/local-ai/reports")

    s = sub.add_parser("open")
    s.add_argument("--set", choices=SETS, required=True)
    s.add_argument("--profile", default="")

    s = sub.add_parser("pack", help="build/refresh the offline data pack into the data folder (opens the Updater)")
    s.add_argument("--set", choices=SETS, default="nd")
    s.add_argument("--data-folder", default="", help="where the pack is built (default ~/ND_Suite_Data)")
    s.add_argument("--profile", default="")
    s.add_argument("--print-only", action="store_true", help="print the steps, do not open QGIS")

    s = sub.add_parser("crest", help="open the Crest desk wired to one plugin set")
    s.add_argument("--set", choices=SETS, required=True)
    s.add_argument("--work", default="", help="Crest work folder (default ~/CrestDams/<set>)")
    s.add_argument("--data-folder", default="", help="ND offline pack / suite data folder (NDSUITE_DATA)")
    s.add_argument("--crest-dir", default=CREST_DIR, help="the crest-desktop folder (default ~/local-ai/crest-desktop)")
    s.add_argument("--crest-python", default="", help="Python for the desk (default: the ndcrest env)")
    s.add_argument("--print-env", action="store_true", help="write the wrappers, print the exports, do not open")

    s = sub.add_parser("seed")
    s.add_argument("--dams-folder", required=True)
    s.add_argument("--csv", required=True)

    s = sub.add_parser("run")
    s.add_argument("--set", choices=SETS, required=True)
    s.add_argument("--ids", default="", help="comma separated dam ids")
    s.add_argument("--ids-csv", default="", help="CSV with a dam_id column")
    s.add_argument("--no-screen", action="store_true")
    s.add_argument("--redo", action="store_true", help="run dams again that finished before")
    s.add_argument("--stop-on-error", action="store_true")
    s.add_argument("--param", action="append", help="extra KEY=VALUE for the chain algorithm")
    s.add_argument("--triage", action="store_true",
                   help="on a failed step, send the log tail to the local Ollama model and write "
                        "an advisory TRIAGE.txt next to the run log")
    s.add_argument("--lumo-model", default="", help="Ollama model for --triage (default: $LUMO_MODEL or 'lumo')")
    g = s.add_argument_group("nd set (ndhazard:suitechain per dam folder)")
    g.add_argument("--dams-folder", default="", help="folder of <dam_id>/dam.json folders")
    g.add_argument("--data-folder", default="", help="suite data folder / offline pack for Get data")
    g.add_argument("--no-getdata", action="store_true")
    g.add_argument("--hi-res", action="store_true",
                   help="download a 1 m 3DEP DEM (DEM_CELL_M=1) unless --getdata-param sets it; "
                        "pair with DEM_MI to bound the download")
    g.add_argument("--no-breach", action="store_true")
    g.add_argument("--no-classify", action="store_true")
    g.add_argument("--getdata-param", action="append",
                   help="KEY=VALUE for ndhazard:getdamdata, e.g. DEM_CELL_M=5, DEM_MI=10, OVERWRITE=true")
    g.add_argument("--screen-param", action="append", help="KEY=VALUE for nddam:runchain")
    g.add_argument("--breach-param", action="append", help="KEY=VALUE for ndbreach:rundam")
    g.add_argument("--classify-param", action="append", help="KEY=VALUE for ndhazard:classify")
    g.add_argument("--timeout-min", type=int, default=480)
    g = s.add_argument_group("dwr set (ndbreach:chainbatch)")
    g.add_argument("--dams", default="", help="dams points layer")
    g.add_argument("--id-field", default="", help="dam id field of the layer")
    g.add_argument("--out", default="", help="output folder, one folder per dam")
    g.add_argument("--dem", default="", help="DEM or VRT covering every dam")
    g.add_argument("--screen-work", default="", help="Screening work folder (statewide run + data package)")
    g.add_argument("--hec", choices=DWR_HEC, default="skip",
                   help="HEC-RAS 2D stage (default skip: it cannot run on a Mac)")
    g.add_argument("--anuga", action="store_true", help="LISFLOOD-FP then ANUGA")
    g.add_argument("--stop-after", choices=DWR_STOP, default="end")
    g.add_argument("--settings-file", default="")
    g.add_argument("--overrides-csv", default="")
    g.add_argument("--check-only", action="store_true", help="preflight only")
    g.add_argument("--dem-units", choices=DEM_UNITS, default="",
                   help="the Runner's DEM elevation units (needed when the DEM records none, e.g. a VRT)")
    g.add_argument("--runner-param", action="append", help="KEY=VALUE for the Runner (ndbreach:batch)")

    args = ap.parse_args()
    if getattr(args, "lumo_model", ""):
        globals()["LUMO_MODEL"] = args.lumo_model
    if args.cmd == "setup":
        cmd_setup(args)
    elif args.cmd == "plugins":
        cmd_plugins(args)
    elif args.cmd == "open":
        cmd_open(args)
    elif args.cmd == "pack":
        cmd_pack(args)
    elif args.cmd == "seed":
        cmd_seed(args)
    elif args.cmd == "crest":
        cmd_crest(args)
    elif args.set == "nd":
        if not args.dams_folder:
            sys.exit("--set nd needs --dams-folder")
        run_nd(args)
    else:
        run_dwr(args)


if __name__ == "__main__":
    main()
SCRIPT_EOF_CHAIN

  chmod +x "$LOCAL_AI"/rag_index.py "$LOCAL_AI"/rag_query.py "$LOCAL_AI"/audit_plugin.py "$LOCAL_AI"/suite_check.py "$LOCAL_AI"/dam_chain.py
  for f in rag_index rag_query audit_plugin suite_check dam_chain; do
    "$LUMO_PY" -m py_compile "$LOCAL_AI/$f.py" || die "$f.py did not compile"
  done
  ok "rag_index 2.4, rag_query 2.2, audit_plugin 1.3, suite_check 1.0, dam_chain 1.4 written and compiled"
else
  ok "script rewrite skipped (--skip-scripts)"
fi

# ===========================================================================
say "Step 7/9 — Shell config (~/.zshrc)"
if grep -qF "$MARKER" "$ZSHRC" 2>/dev/null; then
  cp "$ZSHRC" "$ZSHRC.bak-lumo"
  /usr/bin/sed -i '' "/^$MARKER\$/,/^# ---- end local AI ----\$/d" "$ZSHRC"
fi
PYRUN="$LUMO_PY"
(( USE_PYLIBS )) && PYRUN="env PYTHONPATH=\$HOME/local-ai/pylibs $LUMO_PY"
cat >> "$ZSHRC" << EOF
$MARKER
# Edit these four paths to where your clones and PDFs really are.
export ND_REPO="\$HOME/Documents/DSHazard/nd_hazard"
export DWR_REPO="\$HOME/Documents/DWR_Plugins"
export CREST_REPO="\$HOME/Documents/DSHazard/crest"
export STD_PDFS="\$HOME/Documents/Standards_PDFs"
export OLLAMA_MODELS="\$HOME/local-ai/models"
export LUMO_NUM_CTX=$NUM_CTX
alias lumo-start="OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 \$HOME/local-ai/bin/Ollama.app/Contents/Resources/ollama serve"
unalias lumo-chat 2>/dev/null
function lumo-chat {
  [[ -t 1 && -z "\$NO_COLOR" ]] && printf '\033[1;38;2;48;196;200mD\033[1;38;2;250;250;240ma\033[1;38;2;48;196;200mM\033[1;38;2;244;158;24miT\033[0m  \033[2mLumo - local triage\033[0m\n \033[38;2;31;188;188m~\033[0m\n'
  \$HOME/local-ai/bin/Ollama.app/Contents/Resources/ollama run lumo "\$@"
}
alias lumo-ps="\$HOME/local-ai/bin/Ollama.app/Contents/Resources/ollama ps"
alias lumo-index="$PYRUN \$HOME/local-ai/rag_index.py"
alias lumo-query="$PYRUN \$HOME/local-ai/rag_query.py"
alias lumo-audit="$PYRUN \$HOME/local-ai/audit_plugin.py"
alias lumo-test="$PYRUN \$HOME/local-ai/suite_check.py"
alias damchain="$PYRUN \$HOME/local-ai/dam_chain.py"
alias lumo-std="$PYRUN \$HOME/local-ai/rag_query.py -c standards"
alias lumo-hazard="$PYRUN \$HOME/local-ai/rag_query.py -c nd_hazard"
alias lumo-reindex-all='lumo-index -c nd_hazard "\$ND_REPO" && lumo-index -c dwr_plugins "\$DWR_REPO" && lumo-index -c crest "\$CREST_REPO" && lumo-index -c standards "\$STD_PDFS"'
# ---- end local AI ----
EOF
ok "aliases written (run: source ~/.zshrc, then edit ND_REPO/DWR_REPO/CREST_REPO/STD_PDFS)"

# ===========================================================================
say "Step 8/9 — Housekeeping"
rm -rf "$TMPDIR_L"
if [[ -n "${SERVE_PID:-}" ]]; then
  warn "the setup-time server is still running (PID $SERVE_PID, log $SERVE_LOG)."
  warn "'kill $SERVE_PID' when done, then use 'lumo-start' in its own terminal tab."
fi

# ===========================================================================
say "Step 9/9 — Verification"
PASS=0; TOTAL=0
check(){ TOTAL=$((TOTAL+1)); if eval "$2" >/dev/null 2>&1; then PASS=$((PASS+1)); ok "$1"; else fail "$1"; fi }
check "Ollama binary"              "[[ -x '$OLLAMA_EXE' ]]"
check "Server responding"          "curl -sf http://127.0.0.1:11434/api/version"
check "Chat model pulled"          "'$OLLAMA' list | grep -q '^$CHAT_MODEL'"
check "nomic-embed-text pulled"    "'$OLLAMA' list | grep -q '^nomic-embed-text'"
check "'lumo' persona"             "'$OLLAMA' list | grep -q '^lumo'"
check "rag_index.py on disk"       "[[ -f '$LOCAL_AI/rag_index.py' ]]"
check "rag_query.py on disk"       "[[ -f '$LOCAL_AI/rag_query.py' ]]"
check "audit_plugin.py on disk"    "[[ -f '$LOCAL_AI/audit_plugin.py' ]]"
check "suite_check.py on disk"     "[[ -f '$LOCAL_AI/suite_check.py' ]]"
check "dam_chain.py on disk"       "[[ -f '$LOCAL_AI/dam_chain.py' ]]"
check "zshrc block"                "grep -qF '$MARKER' '$ZSHRC'"
# PDF support is optional: report it, don't fail on it.
if PYTHONPATH="$PYLIBS_DIR" "$LUMO_PY" -c 'import pymupdf' 2>/dev/null; then ok "PDF text: PyMuPDF"
elif PYTHONPATH="$PYLIBS_DIR" "$LUMO_PY" -c 'import pypdf' 2>/dev/null; then ok "PDF text: pypdf (slower, fine)"
else warn "PDF text: none — PDFs will be skipped when indexing"; fi
echo ""
if [[ $PASS -eq $TOTAL ]]; then
  printf '\033[1;32mALL %s CHECKS PASSED — fully operational.\033[0m\n' "$TOTAL"
  cat << 'NEXT'

NEXT:
  source ~/.zshrc                       # then fix ND_REPO etc. in ~/.zshrc if needed
  lumo-start                            # in its own terminal tab
  lumo-audit -p "$ND_REPO" --static-only                 # exact checks, seconds
  lumo-audit -p "$ND_REPO" --changed origin/main         # model review of what you changed
  lumo-test nd --repo "$ND_REPO" --triage                # plugin tests in ndqgis + failure notes
NEXT
else
  printf '\033[1;33m%s/%s passed — fix failures, re-run (completed steps auto-skip).\033[0m\n' "$PASS" "$TOTAL"
fi
