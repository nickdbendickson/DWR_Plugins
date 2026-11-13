# damit_ai: local AI (Ollama) for the dam suite

Two local assistants that run on your Mac through [Ollama](https://ollama.com). Nothing is sent
off the machine. They are kept apart: separate Ollama models, separate databases, separate commands.

> **Renamed 2026-11-13.** The code-triage AI was `pierre` (and `lumo` before that) and is now
> **Heim**; the literature AI was `damresearch` and is now **Vor**. Re-running either installer
> carries an existing install over: `~/DamResearch` is moved to `~/Vor`, `research.db` becomes
> `vor.db`, and Heim's RAG collection is renamed in place so nothing is re-embedded. `damchain
> research ...` still works as an alias for `damchain vor ...`. The conda environments are the one
> thing that is rebuilt rather than renamed (`ndpierre` -> `ndheim`, `ndresearch` -> `ndvor`); the
> packages are bundled, so nothing is downloaded, and the old environments can be deleted.

| Folder | Assistant | What it does | Ollama model | Data |
|---|---|---|---|---|
| [`heim/`](heim/) | **Heim**, code triage | Audits plugin code (static pass + model pass), runs the plugin test suites, drives the plugin chain headless (`damchain`), explains failed runs | `heim` | `~/local-ai/knowledge.db` |
| [`vor/`](vor/) | **vor**, literature | Answers questions from your library of dam-safety / dam-design / flood-hazard publications, citing document and page; deep-research briefs with every quote checked against its page | `vor` | `~/Vor/vor.db` |

> **Both are assistants, not engineers.** A local model misreads code and papers often enough that
> every Heim finding must be confirmed against the live code with a failing test, and every
> vor statement checked against the cited page of the source, before anything is
> changed or relied on in a dam-safety decision.

These are Mac tools (zsh, Ollama.app, conda). They are not QGIS plugins and are not part of any
plugin zip or CI job.

## Install

1. **Heim (installs Ollama too):**
   ```zsh
   cd damit_ai/heim
   zsh setup-local-heim.sh                     # --small for a 16 GB Mac, --model TAG for any model
   source ~/.zshrc
   ```
   Commands: `heim-start` (Ollama server, in its own tab), `heim-chat`, `heim-index`, `heim-query`,
   `heim-audit`, `damchain ...`.

   **Repo audit:** `damchain audit --repo ~/path/to/DWR_Plugins` writes one report to
   `~/local-ai/reports/audit_<repo>_<time>.md` (and `.json`). It checks:
   1. release and packaging: runs the repo's own `tools/ci_checks.py`, every plugin's metadata
      and its DamSafety Core dependency, release/install lists that leave a plugin out, and with
      `--zips FOLDER` the built zips (one top folder, versions match the checkout);
   2. security: built-in patterns (TLS checks off, curl into a shell, sudo, unpickling, eval,
      tar extract, plain FTP, hard-coded secrets) plus bandit and ruff when they are installed
      (never installed by the audit);
   3. network: every host the code names, written to `hosts_<repo>.txt` for IT; with
      `--allow-hosts FILE` any host not in the file is flagged;
   4. `--heim`: the local model reviews the diff against `origin/main`. Its findings are marked
      UNVERIFIED (triage only; confirm each with a failing test);
   5. golden runs: `damchain audit --golden-record --results ~/DamSafety/Dams --ids DWR-2801,DWR-4031`
      saves those dams' run_report.json numbers as a baseline, and after a plugin update rerun the
      dams and use `--golden` instead. Peak flow, flooded acres, depth, velocity, storage and arrival
      time must stay within tolerance, and LISFLOOD-FP volume error must stay at 1 % or under.
   Any HIGH finding gives exit code 1, so the audit can gate a merge. `--only release,security`
   runs a subset.

   Run the first command after the `source` line, not in the same paste: zsh reads a whole pasted
   block before running any of it, so a new command pasted along with `source ~/.zshrc` is
   `command not found` although the install is fine. A new Terminal tab does the same job.

2. **vor (needs Ollama from step 1, or the Ollama app):**
   ```zsh
   cd damit_ai/vor
   zsh setup-vor.zsh                  # --ocr for scanned PDFs, --big / --small / --model TAG
   source ~/.zshrc
   ```
   Then drop PDFs in `~/Vor/library`, or fetch the public ASDSO / FEMA reference set
   (about 49 PDFs) with `python3 fetch_asdso.py` and the 121 catalog sources (hazard, design, breach, hydrology and more)
   with `python3 fetch_asdso.py --catalog`, and run `vor ingest`. After that:
   `vor search | ask | research | chat ...`, also available as `damchain vor ...`.
   **One more document later:** `vor add <file>`, or drag the file into a running `vor chat`
   and press Return. For Heim: `heim-add <file>`.
   **Is it reading what you think it is:** `vor doctor` and `heim-doctor` (or `heim doctor`; `heim add`
   and `heim chat` are words too) name the states in which
   an AI runs without an error and answers from nothing (an empty index, a second library, a
   collection under an old name, an index older than its files). `heim-chat` looks every
   question up in the indexed documents first; `heim-chat --bare` is the model on its own.
   **First run, in one command:** `zsh first-run.zsh` installs vor if needed, downloads
   both reference sets, indexes them, scores two models on 16 questions with known answers
   (`eval_vor.py`), and packs the results into `~/Desktop/vor-first-run.zip`.
   Full guide, model and memory trade-offs: [`vor/INSTALL.md`](vor/INSTALL.md).
   Starter reading list: [`vor/READING-LIST.md`](vor/READING-LIST.md).

## Offline folder (nothing downloaded by the installers)

The `damit_ai-offline` download adds `damit_ai/offline/` next to `heim/` and `vor/`:

- `offline/ollama/Ollama-darwin.zip` + `SHA256SUMS`: Ollama for Mac, checked against Ollama's
  published hash. Both installers use it, after checking the hash, instead of downloading Ollama.
- `offline/conda/osx-arm64/` and `offline/conda/osx-64/`: every conda-forge package for the
  `ndvor` env (Python 3.12, numpy, PyMuPDF, pypdf, ocrmypdf, tesseract, ghostscript), with
  `ndvor.txt` and `SHA256SUMS`. `setup-vor.zsh` builds the env from these files with
  `conda create --offline`, OCR included. It needs conda (the dam-suites package ships Miniforge).

Models still come from Ollama's registry (`ollama pull`, 17 to 24 GB each). They are too big to bundle.

## Models by Mac

| Mac | Heim | vor |
|---|---|---|
| Apple Silicon 48 GB+ | `qwen3:30b-a3b` | `qwen3.6:35b-a3b` (falls back to `qwen3:30b-a3b`) |
| Apple Silicon 32 GB | `qwen3:30b-a3b` | `qwen3:30b-a3b`, sharing Heim's download. `--model qwen3.6:27b` trades speed for quality |
| Apple Silicon 16-24 GB | `qwen3:14b` | `qwen3:14b` / `qwen3:8b` |
| Intel Mac (CPU only) | `qwen3:30b-a3b` at 32 GB+, else `qwen3:8b` | `qwen3.6:35b-a3b` at 48 GB+, `qwen3:30b-a3b` at 32 GB |

Embeddings: Heim uses `nomic-embed-text`; vor uses `qwen3-embedding:0.6b`.

## Status

- `heim/` is setup-local-heim.sh v3.6 (bundled Ollama and packages, no web downloads) with rag_index 2.4, rag_query 2.2, audit_plugin 1.3,
  suite_check 1.0, dam_chain 1.5 and repo_audit 1.0 (all written by the installer itself; `heim-chat` shows the
  DaMiT banner). dam_chain 1.5 adds DamSafety Core to the DWR set and no longer reports the hidden
  self-test / run-chain tools as MISSING (`qgis_process list` leaves them out). `suite_check.py` and `dam_chain.py` drive the ND and DamSafety plugin sets (QGIS
  profiles `nd` and `damsafety`). The DamIT plugins are not wired into them yet.
- `vor/` is vor 1.3.0, setup-vor 1.2 (bundled Ollama and packages), fetch_asdso 1.4, eval_vor 1.2 and first-run.zsh 1.0.
  fetch_asdso was tested against damsafety.org on 2026-11-09 (48 of 49 files downloaded; the 49th
  is on damtoolbox.org, which the cloud workspace can't reach). The `--catalog` sources were only
  partly tested: the cloud workspace reaches 2 of their 31 sites, so most catalog URLs were checked
  against search listings, not downloaded. The cloud workspace could not download Ollama models, so
  everything was tested with a stand-in Ollama server and real PDFs. Answer quality with the real
  models has not been measured yet; `first-run.zsh` measures it.
