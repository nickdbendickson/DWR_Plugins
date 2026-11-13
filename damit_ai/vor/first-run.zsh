#!/bin/zsh
# first-run.zsh v1.0 - the first real run of vor on this Mac, in one command:
#   1. install vor if it is not installed yet (needs Ollama: Heim's installer or the app)
#   2. download the reference library: the ASDSO / FEMA set and the whole catalog
#   3. index it (and OCR scanned pages when OCR is installed)
#   4. score two models on the answer test (eval/questions.json) and pick the better one
#   5. pack the results into ~/Desktop/vor-first-run.zip to send back for review
#
#   cd damit_ai/vor && zsh first-run.zsh
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

[[ -f "$HERE/vor.py" && -f "$HERE/fetch_asdso.py" ]] || die "run this from damit_ai/vor"
LOCAL_AI="$HOME/local-ai"
DR_HOME="${VOR_HOME:-$HOME/Vor}"
# This run's log folder is made inside $DR_HOME two lines down, BEFORE the installer runs. On a
# machine that still has the pre-rename ~/DamResearch that would create ~/Vor first, and
# setup-vor.zsh only moves the old library across when ~/Vor does not exist. So do that one
# move here, ahead of the mkdir. Same condition and same move as setup-vor.zsh.
if [[ -z "${VOR_HOME:-}" && ! -d "$HOME/Vor" && -d "$HOME/DamResearch" ]]; then
  if mv "$HOME/DamResearch" "$HOME/Vor" 2>/dev/null; then
    ok "moved your library from ~/DamResearch to ~/Vor (renamed 2026-11-13; nothing was copied)"
  else
    DR_HOME="$HOME/DamResearch"
    warn "could not move ~/DamResearch to ~/Vor; using the library where it is"
  fi
fi
STAMP=$(date +%Y%m%d-%H%M)
OUT="$DR_HOME/first-run-$STAMP"
mkdir -p "$OUT"
exec > >(tee -a "$OUT/first-run.log") 2>&1
printf '\033[1;38;2;48;196;200mD\033[1;38;2;250;250;240ma\033[1;38;2;48;196;200mM\033[1;38;2;244;158;24miT\033[0m  \033[2mvor first run\033[0m\n'
echo "log: $OUT/first-run.log"

# ---------------------------------------------------------------------------
say "1/5  Install"
PY="$LOCAL_AI/vor/python"
if [[ -x "$PY" && -f "$LOCAL_AI/vor/vor.py" ]]; then
  ok "vor already installed (re-run setup-vor.zsh yourself to change model)"
  if ! cmp -s "$HERE/vor.py" "$LOCAL_AI/vor/vor.py"; then
    cp "$LOCAL_AI/vor/vor.py" "$LOCAL_AI/vor/vor.py.bak-$STAMP"
    cp "$HERE/vor.py" "$LOCAL_AI/vor/vor.py"
    ok "updated ~/local-ai/vor/vor.py to this folder's version (old copy kept as .bak-$STAMP)"
  fi
else
  zsh "$HERE/setup-vor.zsh" --ocr || die "the installer stopped; see the messages above"
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
"$PY" "$HERE/vor.py" ingest || die "ingest failed"
"$PY" "$HERE/vor.py" ocr || warn "OCR did not run (not installed: zsh setup-vor.zsh --ocr); scanned pages stay unsearchable, the rest is indexed"
"$PY" "$HERE/vor.py" list > "$OUT/library-list.txt" 2>&1
"$PY" "$HERE/vor.py" doctor > "$OUT/doctor.txt" 2>&1
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
  "$PY" "$HERE/eval_vor.py" --list | tee "$OUT/eval-coverage.txt"
  MODEL_LIST=( ${=MODELS} )
  # mtime marker so the copy below can tell this run's reports from older ones
  EVAL_MARK="$OUT/.eval-start"
  : > "$EVAL_MARK"
  for m in $MODEL_LIST; do
    if ! "$OLLAMA" list 2>/dev/null | awk '{print $1}' | grep -qx "$m"; then
      # Say what is about to happen. `ollama pull` draws its progress with carriage
      # returns, which the tee into first-run.log swallows, so a 17-24 GB download can
      # look exactly like a hung script for twenty minutes.
      printf '  pulling %s (this model is not installed yet: expect 17-24 GB).\n' "$m"
      printf '  The progress bar may not redraw here. To watch it: du -sh ~/.ollama/models\n'
      "$OLLAMA" pull "$m" || { warn "could not pull $m, skipped"; continue; }
    fi
    # PYTHONUNBUFFERED: line 42 sends this whole script's stdout through `tee`, so the
    # eval's stdout is a PIPE and Python block-buffers it. The eval prints a banner plus
    # one "[n/16] <id> PASS 42 s" line per question -- about 1.5 KB, which never fills a
    # 4 KB buffer -- so on a long model run the screen shows NOTHING for the best part of
    # an hour and then the whole block at once. (`--list` above escapes this only because
    # that process exits straight away, which flushes it.) Verified both ways.
    PYTHONUNBUFFERED=1 "$PY" "$HERE/eval_vor.py" --model "$m" || warn "test run for $m stopped early"
    # Unload before the next model. Every multi-model tier above pairs two models that
    # do not fit in RAM together (32 GB: qwen3.6:27b ~17 GB + qwen3:30b-a3b ~18 GB), and
    # eval_vor.py sets no keep_alive, so Ollama holds the one just scored for its
    # default 5 minutes while the next one loads. The machine then swaps and the second
    # half of the comparison is timed under memory pressure, which makes the two reports
    # not comparable. `stop` is advisory: never let it fail the run.
    if (( ${#MODEL_LIST} > 1 )); then
      "$OLLAMA" stop "$m" >/dev/null 2>&1 || true
    fi
  done
  # Copy only the reports THIS run produced. This used to be a bare
  # `cp "$DR_HOME"/reports/eval_*` which swept up every report ever written, so the
  # zip and the summary below mixed old runs in with the new ones. On 2026-11-12 that
  # put a stale 11-10 report showing "qwen3.6:27b 0 / 16 passed" (all 16 ERROR, from an
  # older Ollama that answered /api/chat with "structured output is unavailable") right
  # next to the same model's real 9 / 16 from that day -- actively misleading for the
  # one decision this step exists to inform.
  for f in "$DR_HOME"/reports/eval_*.md(N) "$DR_HOME"/reports/eval_*.json(N); do
    if [[ "$f" -nt "$EVAL_MARK" ]]; then
      cp "$f" "$OUT/"
    fi
  done
  rm -f "$EVAL_MARK"
  echo
  if [[ -z "$OUT"/eval_*.json(N) ]]; then
    warn "no report from this run: every model errored or was skipped (see above)"
  fi
  for j in "$OUT"/eval_*.json(N); do
    "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print("  %-22s %2d / %2d passed  %5.1f min" % (d["model"], d["passed"], d["scored"], d["minutes"]))' "$j"
  done
  # Name the winning tag instead of printing a literal "TAG". On 2026-11-13 Nick read that
  # line as the command and ran `--model TAG`, which Ollama dutifully tried to pull
  # ("pull model manifest: file does not exist"). Best = most passed, ties broken by the
  # shorter run.
  BEST=$("$PY" - "$OUT"/eval_*.json(N) <<'PY'
import json, sys
best = None
for p in sys.argv[1:]:
    try:
        d = json.load(open(p))
    except Exception:
        continue
    if not d.get("scored"):
        continue
    k = (d["passed"], -d["minutes"])
    if best is None or k > best[0]:
        best = (k, d["model"])
print(best[1] if best else "")
PY
)
  if [[ -n "$BEST" ]]; then
    echo "  To use the better model:  zsh setup-vor.zsh --model $BEST   (rebuilds the vor persona)"
  else
    echo "  To use a model:  zsh setup-vor.zsh --model <a tag from the table above>   (rebuilds the vor persona)"
  fi
fi

# ---------------------------------------------------------------------------
say "5/5  Pack the results"
for f in "$DR_HOME"/library/references/*/sources.csv(N); do
  cp "$f" "$OUT/sources-$(basename "$(dirname "$f")").csv"
done
[[ -f "$DR_HOME/library/asdso/manifest.json" ]] && cp "$DR_HOME/library/asdso/manifest.json" "$OUT/asdso-manifest.json"
[[ -f "$DR_HOME/library/references/manifest.json" ]] && cp "$DR_HOME/library/references/manifest.json" "$OUT/catalog-manifest.json"
ZIP="$HOME/Desktop/vor-first-run.zip"
rm -f "$ZIP"
(cd "$(dirname "$OUT")" && /usr/bin/zip -qr "$ZIP" "$(basename "$OUT")") && ok "results: $ZIP" || warn "could not zip; the results are in $OUT"
echo "  Holds the log, which sources downloaded or failed, the library list and the test scores with every answer."
echo "  No PDFs and no database are in it."
