# damit_ai: local AI (Ollama) for the dam suite

Two local assistants that run on your Mac through [Ollama](https://ollama.com). Nothing is sent
off the machine. They are kept apart: separate Ollama models, separate databases, separate commands.

| Folder | Assistant | What it does | Ollama model | Data |
|---|---|---|---|---|
| [`lumo/`](lumo/) | **Lumo**, code triage | Audits plugin code (static pass + model pass), runs the plugin test suites, drives the plugin chain headless (`damchain`), explains failed runs | `lumo` | `~/local-ai/knowledge.db` |
| [`research/`](research/) | **dam-research**, literature | Answers questions from your library of dam-safety / dam-design / flood-hazard publications, citing document and page; deep-research briefs with every quote checked against its page | `dam-research` | `~/DamResearch/research.db` |

> **Both are assistants, not engineers.** A local model misreads code and papers often enough that
> every Lumo finding must be confirmed against the live code with a failing test, and every
> dam-research statement checked against the cited page of the source, before anything is
> changed or relied on in a dam-safety decision.

These are Mac tools (zsh, Ollama.app, conda). They are not QGIS plugins and are not part of any
plugin zip or CI job.

## Install

1. **Lumo (installs Ollama too):**
   ```zsh
   cd damit_ai/lumo
   zsh setup-local-lumo.sh                     # --small for a 16 GB Mac, --model TAG for any model
   source ~/.zshrc
   ```
   Commands: `lumo-start` (Ollama server, in its own tab), `lumo-chat`, `lumo-index`, `lumo-query`,
   `lumo-audit`, `damchain ...`.

2. **dam-research (needs Ollama from step 1, or the Ollama app):**
   ```zsh
   cd damit_ai/research
   zsh setup-dam-research.zsh                  # --ocr for scanned PDFs, --big / --small / --model TAG
   source ~/.zshrc
   ```
   Then drop PDFs in `~/DamResearch/library`, or fetch the public ASDSO / FEMA reference set
   (about 49 PDFs) with `python3 fetch_asdso.py` and 58 hazard-classification and dam-design sources
   with `python3 fetch_asdso.py --catalog`, and run `damresearch ingest`. After that:
   `damresearch search | ask | research | chat ...`, also available as `damchain research ...`.
   **First run, in one command:** `zsh first-run.zsh` installs dam-research if needed, downloads
   both reference sets, indexes them, scores two models on 16 questions with known answers
   (`eval_damresearch.py`), and packs the results into `~/Desktop/damresearch-first-run.zip`.
   Full guide, model and memory trade-offs: [`research/INSTALL.md`](research/INSTALL.md).
   Starter reading list: [`research/READING-LIST.md`](research/READING-LIST.md).

## Models by Mac

| Mac | Lumo | dam-research |
|---|---|---|
| Apple Silicon 48 GB+ | `qwen3:30b-a3b` | `qwen3.6:35b-a3b` (falls back to `qwen3:30b-a3b`) |
| Apple Silicon 32 GB | `qwen3:30b-a3b` | `qwen3:30b-a3b`, sharing Lumo's download. `--model qwen3.6:27b` trades speed for quality |
| Apple Silicon 16-24 GB | `qwen3:14b` | `qwen3:14b` / `qwen3:8b` |
| Intel Mac (CPU only) | `qwen3:30b-a3b` at 32 GB+, else `qwen3:8b` | `qwen3.6:35b-a3b` at 48 GB+, `qwen3:30b-a3b` at 32 GB |

Embeddings: Lumo uses `nomic-embed-text`; dam-research uses `qwen3-embedding:0.6b`.

## Status

- `lumo/` is setup-local-lumo.sh v3.2 with rag_index 2.4, rag_query 2.2, audit_plugin 1.3,
  suite_check 1.0 and dam_chain 1.4 (now written by the installer itself; `lumo-chat` shows the
  DaMiT banner). `suite_check.py` and `dam_chain.py` drive the ND and DamSafety plugin sets (QGIS
  profiles `nd` and `damsafety`). The DamIT plugins are not wired into them yet.
- `research/` is damresearch 1.0.2, fetch_asdso 1.1, eval_damresearch 1.0 and first-run.zsh 1.0.
  fetch_asdso was tested against damsafety.org on 2026-11-09 (48 of 49 files downloaded; the 49th
  is on damtoolbox.org, which the cloud workspace can't reach). The `--catalog` sources were only
  partly tested: the cloud workspace reaches 2 of their 31 sites, so most catalog URLs were checked
  against search listings, not downloaded. The cloud workspace could not download Ollama models, so
  everything was tested with a stand-in Ollama server and real PDFs. Answer quality with the real
  models has not been measured yet; `first-run.zsh` measures it.
