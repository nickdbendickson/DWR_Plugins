#!/bin/zsh
# first-run.zsh v1.0 - the first real run of dam-research on this Mac, in one command:
#   1. install dam-research if it is not installed yet (needs Ollama: Pierre's installer or the app)
#   2. download the reference library: the ASDSO / FEMA set and the hazard + design catalog
#   3. index it (and OCR scanned pages when OCR is installed)
#   4. score two models on the answer test (eval/questions.json) and pick the better one
#   5. pack the results into ~/Desktop/damresearch-first-run.zip to send back for review
#
#   cd damit_ai/research && zsh first-run.zsh
#   zsh first-run.zsh --models "qwen3.6:27b qwen3:30b-a3b"   # choose the models to compare
#   zsh first-run.zsh --skip-download                        # library already fetched
#   zsh first-run.zsh --skip-eval                            # no model test
#
# Safe to re-run: downloads, indexing and model pulls only fetch what is new. Takes 1-3 hours the
# first time (model downloads ~17-24 GB each, ~1 GB of PDFs, the test runs 16 questions per model).
# Nothing is uploaded anywhere; the zip at the end is for you to send.
set -u
HERE=${0:A:h}
MODELS=""; SKIP_DL=0; SKIP_EVAL=0
while (( $# )); do
  case "$1" in
    --models) shift; MODELS="${1:-}" ;;
    --skip-download) SKIP_DL=1 ;;
    --skip-eval) SKIP_EVAL=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) print -u2 "unknown option: $1"; exit 2 ;;
  esac
  shift
done

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32mOK\033[0m  %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m   %s\n' "$*"; }
die()  { printf '  \033[1;31mX\033[0m   %s\n' "$*"; exit 1; }

[[ -f "$HERE/damresearch.py" && -f "$HERE/fetch_asdso.py" ]] || die "run this from damit_ai/research"
LOCAL_AI="$HOME/local-ai"
DR_HOME="${DAMRESEARCH_HOME:-$HOME/DamResearch}"
STAMP=$(date +%Y%m%d-%H%M)
OUT="$DR_HOME/first-run-$STAMP"
mkdir -p "$OUT"
exec > >(tee -a "$OUT/first-run.log") 2>&1
printf '\033[1;38;2;48;196;200mD\033[1;38;2;250;250;240ma\033[1;38;2;48;196;200mM\033[1;38;2;244;158;24miT\033[0m  \033[2mdam-research first run\033[0m\n'
echo "log: $OUT/first-run.log"

# ---------------------------------------------------------------------------
say "1/5  Install"
PY="$LOCAL_AI/research/python"
if [[ -x "$PY" && -f "$LOCAL_AI/research/damresearch.py" ]]; then
  ok "dam-research already installed (re-run setup-dam-research.zsh yourself to change model)"
  if ! cmp -s "$HERE/damresearch.py" "$LOCAL_AI/research/damresearch.py"; then
    cp "$LOCAL_AI/research/damresearch.py" "$LOCAL_AI/research/damresearch.py.bak-$STAMP"
    cp "$HERE/damresearch.py" "$LOCAL_AI/research/damresearch.py"
    ok "updated ~/local-ai/research/damresearch.py to this folder's version (old copy kept as .bak-$STAMP)"
  fi
else
  zsh "$HERE/setup-dam-research.zsh" --ocr || die "the installer stopped; see the messages above"
fi
[[ -x "$PY" ]] || die "no Python launcher at $PY after the install"

OLLAMA=""
for c in "$LOCAL_AI/bin/Ollama.app/Contents/Resources/ollama" "$(command -v ollama 2>/dev/null)" \
         "/Applications/Ollama.app/Contents/Resources/ollama"; do
  [[ -n "$c" && -x "$c" ]] && { OLLAMA="$c"; break; }
done
[[ -n "$OLLAMA" ]] || die "Ollama not found"
[[ "$OLLAMA" == "$LOCAL_AI/bin/"* ]] && export OLLAMA_MODELS="${OLLAMA_MODELS:-$LOCAL_AI/models}"
export OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0
SERVE_PID=""
if curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  ok "Ollama server already running"
else
  "$OLLAMA" serve >"$OUT/ollama-serve.log" 2>&1 &
  SERVE_PID=$!
  for i in {1..30}; do curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
  curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1 || die "Ollama server did not start; see $OUT/ollama-serve.log"
  ok "started an Ollama server for this run (stopped at the end)"
fi
trap '[[ -n "$SERVE_PID" ]] && kill $SERVE_PID 2>/dev/null' EXIT

# ---------------------------------------------------------------------------
say "2/5  Download the reference library"
if (( SKIP_DL )); then
  ok "skipped (--skip-download)"
else
  "$PY" "$HERE/fetch_asdso.py" || warn "ASDSO set: some files failed (listed above)"
  "$PY" "$HERE/fetch_asdso.py" --catalog || warn "catalog: some sources failed (see sources.csv)"
fi

# ---------------------------------------------------------------------------
say "3/5  Index"
"$PY" "$HERE/damresearch.py" ingest || die "ingest failed"
"$PY" "$HERE/damresearch.py" ocr || warn "OCR did not run (not installed: zsh setup-dam-research.zsh --ocr); scanned pages stay unsearchable, the rest is indexed"
"$PY" "$HERE/damresearch.py" list > "$OUT/library-list.txt" 2>&1
"$PY" "$HERE/damresearch.py" doctor > "$OUT/doctor.txt" 2>&1
tail -3 "$OUT/library-list.txt"

# ---------------------------------------------------------------------------
say "4/5  Answer test"
if (( SKIP_EVAL )); then
  ok "skipped (--skip-eval)"
else
  if [[ -z "$MODELS" ]]; then
    RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
    if (( RAM_GB >= 48 )); then MODELS="qwen3.6:35b-a3b qwen3.6:27b"
    elif (( RAM_GB >= 32 )); then MODELS="qwen3.6:27b qwen3:30b-a3b"
    elif (( RAM_GB >= 24 )); then MODELS="qwen3:14b"
    else MODELS="qwen3:8b"; fi
    ok "${RAM_GB} GB: comparing $MODELS"
  fi
  "$PY" "$HERE/eval_damresearch.py" --list | tee "$OUT/eval-coverage.txt"
  for m in ${=MODELS}; do
    if ! "$OLLAMA" list 2>/dev/null | awk '{print $1}' | grep -qx "$m"; then
      printf '  pulling %s ...\n' "$m"
      "$OLLAMA" pull "$m" || { warn "could not pull $m, skipped"; continue; }
    fi
    "$PY" "$HERE/eval_damresearch.py" --model "$m" || warn "test run for $m stopped early"
  done
  cp "$DR_HOME"/reports/eval_*.md "$DR_HOME"/reports/eval_*.json "$OUT"/ 2>/dev/null
  echo
  for j in "$OUT"/eval_*.json(N); do
    "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print("  %-22s %2d / %2d passed  %5.1f min" % (d["model"], d["passed"], d["scored"], d["minutes"]))' "$j"
  done
  echo "  To use the better model:  zsh setup-dam-research.zsh --model TAG   (rebuilds the dam-research persona)"
fi

# ---------------------------------------------------------------------------
say "5/5  Pack the results"
for f in "$DR_HOME"/library/references/*/sources.csv(N); do
  cp "$f" "$OUT/sources-$(basename "$(dirname "$f")").csv"
done
[[ -f "$DR_HOME/library/asdso/manifest.json" ]] && cp "$DR_HOME/library/asdso/manifest.json" "$OUT/asdso-manifest.json"
[[ -f "$DR_HOME/library/references/manifest.json" ]] && cp "$DR_HOME/library/references/manifest.json" "$OUT/catalog-manifest.json"
ZIP="$HOME/Desktop/damresearch-first-run.zip"
rm -f "$ZIP"
(cd "$(dirname "$OUT")" && /usr/bin/zip -qr "$ZIP" "$(basename "$OUT")") && ok "results: $ZIP" || warn "could not zip; the results are in $OUT"
echo "  Holds the log, which sources downloaded or failed, the library list and the test scores with every answer."
echo "  No PDFs and no database are in it."
