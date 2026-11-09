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
   cp tools/dam_chain.py ~/local-ai/dam_chain.py   # the setup script embeds an older dam_chain (1.1)
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
   Then drop PDFs in `~/DamResearch/library` and run `damresearch ingest`. After that:
   `damresearch search | ask | research | chat ...`, also available as `damchain research ...`.
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

- `lumo/` is setup-local-lumo.sh v3.1 with rag_index 2.4, rag_query 2.2, audit_plugin 1.3,
  suite_check 1.0 and dam_chain 1.4. `suite_check.py` and `dam_chain.py` drive the ND and
  DamSafety plugin sets (QGIS profiles `nd` and `damsafety`). The DamIT plugins are not wired into
  them yet.
- `research/` is damresearch 1.0. The cloud workspace that built it could not download Ollama
  models, so it was tested end to end with a stand-in Ollama server and real PDFs. Answer quality
  with the real models has not been measured yet.
