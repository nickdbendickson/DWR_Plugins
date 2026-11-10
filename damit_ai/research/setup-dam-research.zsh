#!/bin/zsh
# setup-dam-research.zsh v1.1 — the dam-literature research assistant, next to (not inside) lumo.
#
#   * Ollama model "dam-research": a reasoning model chosen for this Mac's memory, with a
#     citation-first system prompt. lumo is not touched.
#   * Embedding model qwen3-embedding:0.6b (falls back to nomic-embed-text).
#   * conda env "ndresearch" (Python 3.12, numpy, PyMuPDF; --ocr adds ocrmypdf + tesseract).
#   * ~/local-ai/research/damresearch.py, library folders under ~/DamResearch.
#   * `damresearch` command (and `damchain research ...` when dam_chain.py is the shipped copy).
#
# Needs Ollama already installed (setup-local-lumo.sh / install_all.zsh put it in ~/local-ai/bin,
# or the Ollama app in /Applications). Run from INSIDE the unpacked folder:
#   zsh setup-dam-research.zsh               # pick the model from this Mac's memory
#   zsh setup-dam-research.zsh --big         # try qwen3.6:35b-a3b even on a 32 GB Mac
#   zsh setup-dam-research.zsh --small       # a small model (16 GB Macs, or for speed)
#   zsh setup-dam-research.zsh --model TAG   # any Ollama model tag
#   zsh setup-dam-research.zsh --ocr         # also install OCR for scanned PDFs
# Re-running is safe: it only redoes what changed.
set -u
HERE="${0:A:h}"
LOCAL_AI="$HOME/local-ai"
TOOL_DIR="$LOCAL_AI/research"
RHOME="${DAMRESEARCH_HOME:-$HOME/DamResearch}"
ZSHRC="$HOME/.zshrc"
ENV_NAME="ndresearch"
PERSONA="dam-research"
BEGIN_MARK="# >>> dam-research (literature assistant) >>>"
END_MARK="# <<< dam-research <<<"

FORCE_MODEL=""; BIG=0; SMALL=0; OCR=0
while (( $# )); do
  case "$1" in
    --model) shift; FORCE_MODEL="${1:-}" ;;
    --big) BIG=1 ;;
    --small) SMALL=1 ;;
    --ocr) OCR=1 ;;
    -h|--help) sed -n 2,19p "$0"; exit 0 ;;
    *) print -u2 "unknown option: $1"; exit 2 ;;
  esac
  shift
done

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32mOK\033[0m  %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m   %s\n' "$*"; }
die()  { printf '  \033[1;31mX\033[0m   %s\n' "$*"; exit 1; }

[[ -f "$HERE/damresearch.py" ]] || die "run this from the unpacked dam-research folder (damresearch.py not next to it)"

# ---------------------------------------------------------------------------
say "1/6  Ollama"
OLLAMA=""
for c in "$LOCAL_AI/bin/Ollama.app/Contents/Resources/ollama" "$(command -v ollama 2>/dev/null)" \
         "/Applications/Ollama.app/Contents/Resources/ollama"; do
  [[ -n "$c" && -x "$c" ]] && { OLLAMA="$c"; break; }
done
# Bundled Ollama (damit_ai/offline/ollama/Ollama-darwin.zip, checked against its SHA256SUMS): no download.
OLLAMA_ZIP="$HERE/../offline/ollama/Ollama-darwin.zip"
if [[ -z "$OLLAMA" && -f "$OLLAMA_ZIP" ]]; then
  want=$(awk '$2 ~ /Ollama-darwin.zip$/ {print $1}' "$HERE/../offline/ollama/SHA256SUMS" 2>/dev/null)
  got=$(shasum -a 256 "$OLLAMA_ZIP" | awk '{print $1}')
  [[ -n "$want" && "$want" == "$got" ]] || die "bundled Ollama-darwin.zip does not match its SHA256SUMS; not installing it"
  mkdir -p "$LOCAL_AI/bin"
  T_O="$(mktemp -d)"; ( cd "$T_O" && /usr/bin/unzip -q "$OLLAMA_ZIP" ) || die "could not unzip the bundled Ollama"
  [[ -d "$T_O/Ollama.app" ]] || die "bundled zip has no Ollama.app"
  rm -rf "$LOCAL_AI/bin/Ollama.app"; mv "$T_O/Ollama.app" "$LOCAL_AI/bin/"; rm -rf "$T_O"
  xattr -dr com.apple.quarantine "$LOCAL_AI/bin/Ollama.app" 2>/dev/null
  OLLAMA="$LOCAL_AI/bin/Ollama.app/Contents/Resources/ollama"
  [[ -x "$OLLAMA" ]] && ok "Ollama installed from the bundled, sha256-checked zip -> ~/local-ai/bin" || die "Ollama binary missing after unzip"
fi
[[ -n "$OLLAMA" ]] || die "Ollama not found. Install it first: zsh setup-local-lumo.sh (in the dam-suites package), or the app from https://ollama.com/download"
ok "ollama: $OLLAMA"
# Same model store as lumo, so a base model both use is downloaded once.
[[ "$OLLAMA" == "$LOCAL_AI/bin/"* ]] && export OLLAMA_MODELS="${OLLAMA_MODELS:-$LOCAL_AI/models}"
export OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0
SERVE_PID=""
if curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  ok "server already running"
else
  "$OLLAMA" serve >"$LOCAL_AI/serve-research-setup.log" 2>&1 &
  SERVE_PID=$!
  for i in {1..30}; do curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
  curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1 || die "server did not start; see $LOCAL_AI/serve-research-setup.log"
  ok "started a server for the install (stopped at the end)"
fi
trap '[[ -n "$SERVE_PID" ]] && kill $SERVE_PID 2>/dev/null' EXIT
OLLAMA_VER=$(curl -sf http://127.0.0.1:11434/api/version | sed -E 's/.*"version": *"([^"]+)".*/\1/')
ok "Ollama $OLLAMA_VER"

# ---------------------------------------------------------------------------
say "2/6  Models for this Mac"
RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
if [[ "$(uname -m)" == "arm64" || "$(sysctl -n sysctl.proc_translated 2>/dev/null)" == "1" ]]; then
  CPU=apple; ok "Apple Silicon, ${RAM_GB} GB unified memory (model runs on the GPU)"
else
  CPU=intel; ok "Intel Mac, ${RAM_GB} GB RAM (model runs on the CPU; a mixture-of-experts model is the fast choice)"
fi
# Candidates, best first; the first one that downloads wins.
#   qwen3.6:35b-a3b  ~24 GB, 3B active (fast), 256K context; needs a recent Ollama
#   qwen3:30b-a3b    ~19 GB, 3B active; the model lumo already uses on 32 GB Macs (no extra download)
#   qwen3:14b        ~9 GB dense;  qwen3:8b ~5 GB dense
if [[ -n "$FORCE_MODEL" ]]; then
  CANDIDATES=("$FORCE_MODEL")
elif (( SMALL )); then
  CANDIDATES=(qwen3:14b qwen3:8b); [[ $CPU == intel || $RAM_GB -lt 24 ]] && CANDIDATES=(qwen3:8b)
elif (( RAM_GB >= 48 )) || (( BIG && RAM_GB >= 32 )); then
  CANDIDATES=(qwen3.6:35b-a3b qwen3:30b-a3b)
elif (( RAM_GB >= 32 )); then
  CANDIDATES=(qwen3:30b-a3b qwen3:14b)
elif (( RAM_GB >= 24 )); then
  CANDIDATES=(qwen3:14b qwen3:8b)
else
  CANDIDATES=(qwen3:8b)
fi
(( BIG && RAM_GB < 48 )) && warn "--big on ${RAM_GB} GB: qwen3.6:35b-a3b (~24 GB) leaves little room; part of it may run on the CPU (slower)"
BASE=""
for m in "${CANDIDATES[@]}"; do
  if "$OLLAMA" list 2>/dev/null | awk '{print $1}' | grep -qx "$m"; then BASE="$m"; ok "$m already downloaded"; break; fi
  printf '  pulling %s ...\n' "$m"
  if "$OLLAMA" pull "$m"; then BASE="$m"; ok "$m downloaded"; break; fi
  warn "could not pull $m (older Ollama, or no network) — trying the next one"
done
[[ -n "$BASE" ]] || die "no chat model could be downloaded (tried: ${CANDIDATES[*]})"

# Context window: room for ~10 passages per step and a long report, without slowing the Mac down.
if [[ $CPU == intel ]]; then NUM_CTX=$(( RAM_GB >= 48 ? 24576 : 16384 ))
elif (( RAM_GB >= 48 )); then NUM_CTX=32768
elif (( RAM_GB >= 32 )); then NUM_CTX=24576
else NUM_CTX=12288; fi
# Thinking before the report helps synthesis but costs minutes on a CPU.
THINK=1; [[ $CPU == intel ]] && THINK=0
EMBED=""
for m in qwen3-embedding:0.6b nomic-embed-text; do
  if "$OLLAMA" list 2>/dev/null | awk '{print $1}' | grep -qE "^${m}(:latest)?$"; then EMBED="$m"; break; fi
  "$OLLAMA" pull "$m" && { EMBED="$m"; break; }
  warn "could not pull $m"
done
[[ -n "$EMBED" ]] || die "no embedding model could be downloaded"
ok "chat base $BASE (num_ctx $NUM_CTX, think $THINK), embeddings $EMBED"
if [[ -f "$RHOME/research.db" ]]; then
  OLD_EMBED=$(python3 -c "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);r=c.execute(\"select v from meta where k='embed_model'\").fetchone();print(r[0] if r else '')" "$RHOME/research.db" 2>/dev/null)
  if [[ -n "$OLD_EMBED" && "$OLD_EMBED" != "$EMBED" ]]; then
    warn "your library was indexed with $OLD_EMBED; keeping that so searches still work"
    EMBED="$OLD_EMBED"
  fi
fi

# ---------------------------------------------------------------------------
say "3/6  The '$PERSONA' model (citation-first research persona)"
mkdir -p "$TOOL_DIR"
cat > "$TOOL_DIR/Modelfile" << EOF
FROM $BASE
PARAMETER num_ctx $NUM_CTX
PARAMETER temperature 0.2
PARAMETER top_p 0.9
PARAMETER repeat_penalty 1.05
SYSTEM """You are a research assistant for dam-safety engineers in North Dakota: dam safety, dam and
spillway design, breach parameters, inundation mapping, hydrology and flood hazards.
When source passages are supplied, use only them: every fact gets a citation to its source and page,
quotes are copied exactly, numbers, units, equations and their applicability limits are kept exactly
as written, and disagreements between sources are reported, not resolved silently.
When no passages are supplied (someone chatting with you directly), say at the start that the answer
is from general knowledge, is UNVERIFIED, and must be checked against the primary publication; give
the publication a reader should check (agency, document number, year) and never invent a citation,
page number, equation coefficient or value. If you are not sure, say so plainly.
You are not a licensed engineer and your output is not engineering judgement."""
EOF
"$OLLAMA" create "$PERSONA" -f "$TOOL_DIR/Modelfile" >/dev/null || die "could not create the $PERSONA model"
ok "'$PERSONA' created from $BASE (lumo untouched)"

# ---------------------------------------------------------------------------
say "4/6  Python (conda env '$ENV_NAME')"
CONDA=""
for c in "${CONDA_EXE:-}" "$(command -v conda 2>/dev/null)" "$HOME/miniconda3/bin/conda" "$HOME/miniconda/bin/conda" "$HOME/miniforge3/bin/conda"; do
  [[ -n "$c" && -x "$c" ]] && { CONDA="$c"; break; }
done
PY=""
if [[ -n "$CONDA" ]]; then
  ENVPATH=$("$CONDA" env list | awk -v n="$ENV_NAME" '$1==n {print $NF}')
  # Bundled packages (damit_ai/offline/conda/<subdir>/ndresearch.txt + SHA256SUMS): the env, OCR
  # included, is built from local files and nothing is downloaded.
  SUBDIR=$("$CONDA" info --json 2>/dev/null | awk -F'"' '/"platform":/ {print $4; exit}')
  PKG_DIR="$HERE/../offline/conda/${SUBDIR:-none}"; PKG_DIR="${PKG_DIR:A}"
  if [[ -z "$ENVPATH" && -f "$PKG_DIR/ndresearch.txt" ]]; then
    TMPX="$(mktemp "${TMPDIR:-/tmp}/ndresearch.XXXXXX")"; print "@EXPLICIT" > "$TMPX"; BAD=0
    while read -r fn md5; do
      [[ -z "$fn" || "$fn" == \#* ]] && continue
      want="$(awk -v n="$fn" '$2==n{print $1}' "$PKG_DIR/SHA256SUMS")"
      got="$(shasum -a 256 "$PKG_DIR/$fn" 2>/dev/null | awk '{print $1}')"
      [[ -n "$want" && "$want" == "$got" ]] || { warn "bundled package missing or bad sha256: $fn"; BAD=1; break; }
      print "file://${PKG_DIR// /%20}/$fn#$md5" >> "$TMPX"
    done < "$PKG_DIR/ndresearch.txt"
    if (( ! BAD )) && "$CONDA" create -y -n "$ENV_NAME" --offline --file "$TMPX"; then
      ok "env '$ENV_NAME' built from the bundled packages ($SUBDIR, OCR included; nothing downloaded)"; OCR=0
    else
      warn "bundled packages did not install; trying conda-forge"
    fi
    rm -f "$TMPX"
    ENVPATH=$("$CONDA" env list | awk -v n="$ENV_NAME" '$1==n {print $NF}')
  fi
  if [[ -z "$ENVPATH" ]]; then
    "$CONDA" create -y -n "$ENV_NAME" -c conda-forge --override-channels python=3.12 numpy pymupdf pypdf \
      || warn "conda create failed — falling back to a venv"
    ENVPATH=$("$CONDA" env list | awk -v n="$ENV_NAME" '$1==n {print $NF}')
  fi
  if (( OCR )) && [[ -n "$ENVPATH" ]]; then
    "$CONDA" install -y -n "$ENV_NAME" -c conda-forge --override-channels ocrmypdf tesseract \
      && ok "OCR installed (ocrmypdf + tesseract)" || warn "OCR install failed; scanned PDFs stay unsearchable"
  fi
  [[ -n "$ENVPATH" && -x "$ENVPATH/bin/python" ]] && PY="$ENVPATH/bin/python"
fi
if [[ -z "$PY" ]]; then
  command -v python3 >/dev/null || die "no conda and no python3 (xcode-select --install provides python3)"
  python3 -m venv "$TOOL_DIR/venv" || die "venv failed"
  "$TOOL_DIR/venv/bin/pip" -q install numpy pypdf || warn "numpy/pypdf install failed (search still works, slower)"
  "$TOOL_DIR/venv/bin/pip" -q install pymupdf 2>/dev/null || warn "PyMuPDF wheel not available for this Python; using pypdf (no printed page labels)"
  PY="$TOOL_DIR/venv/bin/python"
fi
# A tiny launcher, not a symlink: a symlinked venv python would lose its packages.
rm -f "$TOOL_DIR/python"
printf '#!/bin/sh\nexec "%s" "$@"\n' "$PY" > "$TOOL_DIR/python" && chmod +x "$TOOL_DIR/python"
"$PY" -c "import numpy, sqlite3; c=sqlite3.connect(':memory:'); c.execute('create virtual table t using fts5(x)')" 2>/dev/null \
  && ok "Python ready: $PY (numpy + FTS5 keyword search)" \
  || warn "Python ready: $PY, but numpy or SQLite FTS5 is missing (search works, less well)"

# ---------------------------------------------------------------------------
say "5/6  Tool, folders, commands"
cp "$HERE/damresearch.py" "$TOOL_DIR/damresearch.py"
mkdir -p "$RHOME/library" "$RHOME/reports"
[[ -f "$RHOME/library/library.csv" ]] || cp "$HERE/library.csv.example" "$RHOME/library/library.csv.example" 2>/dev/null
ok "tool -> $TOOL_DIR/damresearch.py; drop PDFs in $RHOME/library"

TMPRC="$(mktemp)"
if [[ -f "$ZSHRC" ]]; then
  awk -v b="$BEGIN_MARK" -v e="$END_MARK" '$0==b{skip=1;next} $0==e{skip=0;next} !skip' "$ZSHRC" > "$TMPRC"
else
  : > "$TMPRC"
fi
cat >> "$TMPRC" << EOF
$BEGIN_MARK
export DAMRESEARCH_HOME="$RHOME"
export DAMRESEARCH_MODEL="$PERSONA"
export DAMRESEARCH_EMBED="$EMBED"
export DAMRESEARCH_NUM_CTX=$NUM_CTX
export DAMRESEARCH_THINK=$THINK
alias damresearch="\$HOME/local-ai/research/python \$HOME/local-ai/research/damresearch.py"
alias research-chat="damresearch chat"
$END_MARK
EOF
cp "$ZSHRC" "$ZSHRC.bak-damresearch" 2>/dev/null
mv "$TMPRC" "$ZSHRC"
ok "damresearch command added to ~/.zshrc (backup: ~/.zshrc.bak-damresearch)"

# damchain research ...: only replace dam_chain.py when it is a copy we shipped (never your edits).
SHIPPED=(b99ae0264e4aec9dfa5a22f3eb973a1f2e2a1345 8aa559dccee85442b0b64957b6609c35aa0f0a3d)
DC="$LOCAL_AI/dam_chain.py"
if [[ -f "$DC" ]]; then
  if grep -q "research_passthrough" "$DC"; then
    ok "damchain research already available"
  elif (( ${SHIPPED[(Ie)$(shasum "$DC" | awk '{print $1}')]} )); then
    NEW_DC="$HERE/damchain-1.4/dam_chain.py"
    [[ -f "$NEW_DC" ]] || NEW_DC="$HERE/../lumo/tools/dam_chain.py"   # damit_ai repo layout
    cp "$DC" "$DC.bak-1.3" && cp "$NEW_DC" "$DC" \
      && ok "dam_chain.py 1.3 -> 1.4: 'damchain research ...' works (old copy: dam_chain.py.bak-1.3)"
  else
    warn "your ~/local-ai/dam_chain.py has local changes; left alone. Use 'damresearch ...' directly."
  fi
fi

# ---------------------------------------------------------------------------
say "6/6  Self-check"
DAMRESEARCH_HOME="$RHOME" DAMRESEARCH_MODEL="$PERSONA" DAMRESEARCH_EMBED="$EMBED" DAMRESEARCH_NUM_CTX=$NUM_CTX \
  "$PY" "$TOOL_DIR/damresearch.py" doctor || warn "doctor reported a problem (above)"

cat << EOF

Done. Open a NEW Terminal tab (or: source ~/.zshrc), then:

  lumo-start                                  # Ollama server, in its own tab (if not already running)
  # copy or move your publications into  $RHOME/library   (sub-folders are fine)
  damresearch ingest                          # index them (first run: ~1-3 s per page)
  damresearch search "Froehlich breach width" # passages only, no model, instant
  damresearch ask "What depth defines the inundation boundary in FEMA P-946?"
  damresearch research "How should breach formation time be chosen for small earthen dams?"
  damresearch chat                            # follow-ups; start a question with ! for deep research

Reports land in $RHOME/reports.
Research assistant only: check every cited page in the source before relying on it.
EOF
