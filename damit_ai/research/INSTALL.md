# damresearch: local research assistant for dam-safety literature

A second local AI that sits next to `pierre`. Pierre triages code; `damresearch` reads your library
of dam-safety, dam-design and flood-hazard publications and answers with citations to the
document and page. It runs on your Mac through Ollama, so nothing leaves the machine.

> **It is a research assistant, not an engineer.** Every answer must be checked against the cited
> page of the source before it goes into any dam-safety decision, report or classification.
> The tool helps by checking every quote against the page automatically (see "How it keeps the
> model honest"), but it cannot judge whether a source applies to your dam.

## What gets installed

| Piece | Name | Kept apart from pierre by |
|---|---|---|
| Chat model | `dam-research` (Ollama persona, citation-first system prompt) | own name; pierre's `pierre` persona is untouched |
| Embedding model | `qwen3-embedding:0.6b` (falls back to `nomic-embed-text`) | own model; pierre keeps `nomic-embed-text` |
| Library database | `~/DamResearch/research.db` | pierre uses `~/local-ai/knowledge.db` |
| Drop folder | `~/DamResearch/library/` | |
| Reports | `~/DamResearch/reports/` | |
| Python | conda env `ndresearch` (Python 3.12, numpy, PyMuPDF) | never touches `ndqgis` or `ndpierre` |
| Commands | `damresearch ...`, `research-chat`, and `damchain research ...` | |

## Which model, and the memory trade-offs

The installer picks from the Mac's memory and processor; the first model that downloads wins.

| Mac | Model | Download | Context | Notes |
|---|---|---|---|---|
| Apple Silicon, 48 GB or more | `qwen3.6:35b-a3b` | ~24 GB | 32K | Best quality. Mixture-of-experts (3B active), so it is fast. Needs a recent Ollama; falls back to `qwen3:30b-a3b`. |
| Apple Silicon, 32 GB | `qwen3:30b-a3b` | ~19 GB (already there if pierre uses it) | 24K | Shares weights with pierre, so no extra download. `--big` tries qwen3.6 anyway, but ~24 GB on 32 GB means part of it runs on the CPU, which is slower. |
| Apple Silicon, 24 GB | `qwen3:14b` | ~9 GB | 12K | |
| Apple Silicon, 16 GB | `qwen3:8b` | ~5 GB | 12K | Fine for `search` and `ask`; deep research reports are thinner. |
| Intel iMac i9, 64 GB (CPU only) | `qwen3.6:35b-a3b` | ~24 GB | 24K | Ollama can't use the Radeon, so a mixture-of-experts model is the only fast option on the CPU. Thinking is off by default. |
| Intel, 32 GB | `qwen3:30b-a3b` | ~19 GB | 16K | |

Both models stay loaded together: the chat model plus about 1 GB for the embedding model. Close
QGIS runs before a long research session on a 32 GB Mac. Use `--model TAG` to try any other
Ollama model (for example a newer Qwen or `gpt-oss:20b`); the pipeline doesn't depend on the model.

The model tags and sizes come from public listings. They couldn't be pulled from the cloud
workspace, where Ollama's registry is blocked, so the installer falls back down the list if a
tag is missing.

## Install

You need Ollama, which `setup-local-pierre.sh` / `install_all.zsh` already installed in
`~/local-ai/bin`, or the Ollama app. Conda is optional; without it a venv is used.

```zsh
cd damit_ai/research          # or the unpacked dam-research zip folder
zsh setup-dam-research.zsh            # add --ocr to make scanned PDFs searchable
source ~/.zshrc                       # or open a new Terminal tab
```

Options: `--big`, `--small`, `--model TAG`, `--ocr`. Re-running is safe. Your `~/.zshrc` is
backed up first, and the installer only rewrites its own marked block.

`damchain research ...` works only if `~/local-ai/dam_chain.py` is a copy we shipped (1.3). The
installer upgrades that copy to 1.4 and keeps the old one as `dam_chain.py.bak-1.3`. If you have
edited the file, it is left alone; use `damresearch` directly.

## Use

```zsh
pierre-start                                     # Ollama server in its own tab (same as for pierre)

# 1. Build the library: drop PDFs (sub-folders fine) into ~/DamResearch/library, then
damresearch ingest                             # re-run any time; only new/changed files are read
damresearch ingest ~/Zotero/storage            # or index a folder where it already lives
damresearch list                               # what's in, pages, and pages with no text (scanned)

# 2. Fix citations (makes the references readable)
damresearch bib ~/Downloads/MyLibrary.bib      # Zotero: File > Export Library > BibTeX
damresearch meta fema_p946 --title "Federal Guidelines for Inundation Mapping..." --year 2013 --org FEMA --number "FEMA P-946"
#   or put a library.csv in the library folder (see library.csv.example) before ingest

# 3. Ask
damresearch search "Froehlich breach width side slope"      # passages + pages, no model, instant
damresearch ask "What flow depth do federal guidelines use for the inundation boundary?"
damresearch research "How should breach formation time be chosen for small homogeneous earthen dams, and how uncertain is it?"
damresearch chat                               # follow-ups; prefix a question with ! for deep research
damchain research ask "..."                    # same thing through damchain
damresearch doctor                             # checks Ollama, both models, the library
```

### Getting the ASDSO / FEMA reference set

`fetch_asdso.py` downloads the reference documents that ASDSO (damsafety.org) publishes openly. That
is about 49 PDFs and 0.5 GB: the FEMA / National Dam Safety Program guidelines and technical manuals
(P-93, P-94, P-946, P-1025, 333, 484, P-1015, the filter manual, ...), the NDSP research-workshop
reports (embankment dam failure analysis, predicting peak breach outflow, hydrology, spillways), and
ASDSO's own guides and state reports. Each folder gets a `library.csv`, so citations read
"FEMA P-946 (2013)", plus a `manifest.json` with URL, size and SHA-256 for every file.

```zsh
python3 fetch_asdso.py --dry-run          # see the list first
python3 fetch_asdso.py                    # -> ~/DamResearch/library/asdso/ (about 1 minute; re-run fetches only new files)
python3 fetch_asdso.py --search "dam breach" --search-pages 3
                                          # also search ASDSO's reference database: every hit's citation and
                                          # abstract go to asdso_references.csv; public PDFs are downloaded
damresearch ingest
```

Members-only items, such as conference papers behind the ASDSO login, are never downloaded; only
their citation is recorded. Files hosted on other sites are listed but fetched only with
`--external`. The script waits between requests and follows robots.txt. The documents belong to
their publishers. Many of these PDFs have pages with no text layer, either scans or figure pages.
`damresearch list` shows which, and `damresearch ocr` (installer `--ocr`) makes the scanned ones
searchable.

### First run and answer test

`zsh first-run.zsh` does the whole first run: install (if needed), both downloads, `ingest`, `ocr`,
then the answer test on two models chosen by memory (32 GB: `qwen3.6:27b` and `qwen3:30b-a3b`; 48 GB+:
`qwen3.6:35b-a3b` and `qwen3.6:27b`; `--models "A B"` to choose). It ends with
`~/Desktop/damresearch-first-run.zip`: the log, `sources.csv` for each catalog group, the library list
and the scores with every answer. It holds no PDFs and no database. Expect 1 to 3 hours the first time,
mostly model and PDF downloads.

The test alone:

```zsh
python3 eval_damresearch.py --list                 # which test questions have their source indexed
python3 eval_damresearch.py --model qwen3.6:27b    # score one model (default: the dam-research persona)
```

`eval/questions.json` has 16 questions whose answers were read off the PDF pages: FEMA 333 hazard
definitions, Froehlich 1995 and 2008 breach constants from HEC TD-39, EAP exercise frequencies (P-64),
the inundation-study downstream limit (P-946), filter criteria (FEMA filter manual) and the Pierce,
Thornton and Abt (2010) peak-outflow work. One question checks that the tool says "not covered"
instead of inventing numbers when ND REG_05.2026 is missing. A question passes only when every
expected fact is in the answer **and** a quote the code checked comes from the right document and
page (+/-1). Questions whose source is not indexed are skipped. Add your own questions in the same
format; use questions you know the answer to.

### Hazard-classification and dam-design sources (the catalog)

`reference_sources.json` lists 58 more sources, and `fetch_asdso.py --catalog` downloads them into
`~/DamResearch/library/references/<group>/`:

- **hazard** (39): the sources cited in the "US Dam Hazard Classification" papers: FEMA 333 and P-919,
  USBR ACER TM-11, DSO-99-06 and RCEM, NRCS TR-210-60, FERC Ch. 2, USFWS 361 FW 2, CRS R45981 and
  R46946, ND REG_05.2026 and N.D.A.C. 89-08, NY TOGS 3.1.5, the Nebraska Classification of Dams,
  Colorado 2 CCR 402-1 and its breach guidelines, and the CA, TX, AK, AZ, VA, RI and ID rules.
- **design** (19): USBR Design of Small Dams, Design Standards 13 and 14, the USBR/USACE Best Practices
  chapters, Wahl's breach-parameter review (DSO-98-004), USACE EM 1110-2-1420 / -2300 / -1902 / -1603 /
  -1913 and ER 1110-2-1156, HEC TD-39 (HEC-RAS dam break), NOAA HMR 51 / 52 / 55A and Atlas 14 Vol. 8,
  the FERC Engineering Guidelines, NY's dam design guidelines and TCEQ GI-364.

```zsh
python3 fetch_asdso.py --catalog --dry-run   # the list
python3 fetch_asdso.py --catalog             # both groups (or --catalog hazard / --catalog design)
damresearch ingest
```

PDFs are saved under the source's id (`usbr-acer-tm11.pdf`), and rules that exist only as web pages
(state codes, USFWS policy) are saved as Markdown text with the source URL and date at the top.
`kind: index` entries download every matching PDF on a page, such as all the Design Standards
chapters. Each group folder gets `library.csv` (citations), `manifest.json` and `sources.csv`, which
records what happened to every entry, including six sources with no stable public link to get by
hand (for example the 1985 ND Dam Design Handbook). At the end the script names any site it could
not reach.

Each entry has a `check` field. `listed` means the URL came from a search listing of that exact file,
`cited` means the papers named the host and file, and `pattern` means the URL follows the publisher's
usual layout but was not seen. Agency sites move files, so expect a few failures; fix the `url` in the
JSON (or add an `alt`) and re-run. Only new or failed sources are fetched again. Rules change: the
saved text of a state code is a snapshot, so check the official code before relying on it.

Scanned PDFs (old agency reports often are) have no text layer, and `list` flags them. Install
OCR with `zsh setup-dam-research.zsh --ocr`, then run `damresearch ocr`. That writes searchable
copies to `library/_ocr/` and indexes them.

## How deep research works

1. **Plan.** The model splits the question into up to 4 sub-questions plus exact search terms
   (equation authors, document numbers).
2. **Retrieve.** Each sub-question runs a hybrid search: meaning (embeddings) plus exact words
   (SQLite keyword index, which catches "Froehlich", "TR-60", "P-946"). The two are fused by rank,
   with at most 3 passages per document so one long manual can't crowd out the rest. Passages never
   span a page, so every citation points at one page. PyMuPDF reads the printed page label too,
   e.g. `p.12 (PDF p.30)`.
3. **Extract.** For each sub-question the model returns findings as JSON: source, an exact quote,
   a one-line claim and a kind (value, equation, method, requirement...).
4. **Check (code, not the model).** Each quote is looked up in its passage. A quote that isn't
   on the page, or that cites a passage the model wasn't shown, is rejected. A near match (PDF
   ligatures, line-break hyphens) is accepted only if every number in it matches the page, so
   "2 and 5 times" can't quietly become "3 and 7 times". Numbers in a claim that appear on
   neither the quote nor the page are flagged.
5. **Write.** The model writes the brief from the checked findings only, citing `[F3]`. The code
   turns those into `[2, p.14]` with a numbered reference list, marks citations to findings that
   don't exist, and counts long sentences that carry no citation.

`ask` is the same pipeline without step 1. The answer prints in Terminal. The report, saved as
Markdown in `~/DamResearch/reports/`, also holds the full evidence table (every quote and page),
the rejected items, what the library did not cover, and the search log. The reference links open
the PDF at the page in a browser or Acrobat. Preview ignores the page number.

## Speed (estimates, not measured)

These figures are estimates; nothing here ran on a real Mac. Indexing with the 0.6B embedding
model should take roughly 0.1 to 0.5 s per page on Apple Silicon and a few times longer on the
Intel iMac. A deep research run makes about 6 model calls: expect minutes on an M2 Pro, and
10 to 20 minutes on the Intel iMac. `ask` is about a third of that, and `search` takes about a
second.

## Limits to know

- Tested in the cloud with a stand-in Ollama server and real PDFs. Ingest, page labels, hybrid
  search, quote checking, rejected quotes, reports, chat follow-ups, the installer and
  `damchain research` all ran. Answer quality with the real models has NOT been tested; Ollama's
  registry is blocked from the cloud. Try a few questions whose answers you know first.
- Tables and equations often come out of PDFs garbled. Check equations on the page itself, every time.
- Retrieval finds what is in your library and nothing else. "Not covered" usually means a missing
  publication, not that the topic has no answer.
- A local model can still misread a correct quote, for example by applying a coefficient outside
  its stated range. The quote check proves only that the words are on the page.
- Changing the embedding model means re-indexing (`rm ~/DamResearch/research.db && damresearch ingest`).
  The tool refuses to mix the two.

## Remove

Delete the block between `# >>> dam-research` and `# <<< dam-research` in `~/.zshrc`, then
`ollama rm dam-research qwen3-embedding:0.6b`, `conda env remove -n ndresearch`, and
`~/local-ai/research`. Your PDFs in `~/DamResearch/library` are yours. The tool never modifies them.

## Files

- `setup-dam-research.zsh`: installer
- `damresearch.py`: the tool (stdlib + optional numpy / PyMuPDF)
- `../pierre/tools/dam_chain.py` (in the zip: `damchain-1.4/dam_chain.py`): dam_chain 1.3 plus the `research` pass-through, nothing else changed
- `fetch_asdso.py`: downloads the public ASDSO / FEMA reference set, and with `--catalog` the sources in `reference_sources.json`
- `reference_sources.json`: catalog of hazard-classification and dam-design sources (edit to add more)
- `first-run.zsh`: install, download, index, test and pack the results, in one command
- `eval_damresearch.py` + `eval/questions.json`: the answer test
- `READING-LIST.md`: a starter library of public dam-safety publications
- `library.csv.example`: citation sidecar format
