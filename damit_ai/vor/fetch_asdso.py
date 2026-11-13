#!/usr/bin/env python3
# fetch_asdso.py v1.4 — download public dam-safety reference documents listed by ASDSO
# (damsafety.org), plus a curated catalog of agency and state sources, into the vor library,
# with a citation for each file.
#
#   python3 fetch_asdso.py                      # the ASDSO resource-center pages (default set)
#   python3 fetch_asdso.py --dry-run            # list what would be downloaded
#   python3 fetch_asdso.py --search "dam breach" --search-pages 3
#                                               # also search ASDSO's Dam Safety Resource Database:
#                                               # records every hit's citation in asdso_references.csv
#                                               # and downloads the ones with a public PDF
#   python3 fetch_asdso.py --page https://damsafety.org/<page>   # any other ASDSO page with PDF links
#   python3 fetch_asdso.py --out DIR            # default ~/Vor/library/asdso
#   python3 fetch_asdso.py --catalog            # the curated catalog (reference_sources.json) instead:
#                                               # hazard-classification sources (federal + state rules,
#                                               # ND REG_05, TM-11, FEMA 333 ...) and dam-design references
#                                               # (USBR, USACE, NRCS, NOAA HMR, FERC ...)
#   python3 fetch_asdso.py --catalog hazard     # one group only, or a comma list (hazard | design |
#                                               # breach | software | consequence | data | hydrology | ponds)
#   python3 fetch_asdso.py --catalog --asdso    # catalog and the ASDSO pages in one run
#   python3 fetch_asdso.py --urls library-urls.txt
#                                               # write a plain-text list of every catalog source
#                                               # and its URL; download nothing (offline)
#
# Then index them:  vor ingest
#
# What it takes: only files ASDSO publishes openly (its own S3 file store, linked from its public
# pages). Members-only items (conference papers behind the login, the Journal of Dam Safety) are
# never downloaded: their citation and abstract go into asdso_references.csv so you know they exist.
# Links to other sites (fema.gov, archive.org, ...) are recorded but only downloaded with --external.
#
# Polite by default: one request at a time, a pause between requests (--delay), robots.txt honoured,
# files already downloaded are skipped (re-running only fetches what is new or failed).
# Each folder gets library.csv (title, organisation, document number, year) so vor cites
# "FEMA P-946 (2013)" instead of a file name, and manifest.json (source page, URL, size, SHA-256, date).
#
# The documents belong to their publishers (FEMA, USBR, ASDSO, ...). Use them under those terms.
import os, re, sys, csv, json, time, hashlib, argparse, html
import urllib.request, urllib.error, urllib.parse, urllib.robotparser
from datetime import datetime, timezone

VERSION = "1.4"
SITE = "https://damsafety.org"
UA = "damit-reference-fetch/%s (+research library; polite crawler)" % VERSION
DEFAULT_PAGES = [
    # FEMA / National Dam Safety Program guidelines, technical manuals, fact sheets
    ("ndsp-guidelines", SITE + "/resourcecenter/national-dam-safety-program-guidelines-flyers-and-other-tools"),
    # NDSP research workshop reports (breach, hydrology, spillways, seepage, ...)
    ("ndsp-research", SITE + "/national-dam-safety-program-research-needs"),
    # ASDSO's own guidance, awareness and state-program reports
    ("asdso-guidance", SITE + "/resourcecenter/asdso-resources"),
]
FILE_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip")
ASDSO_FILE_HOSTS = re.compile(r"^(damsafety(-prod|-live)?\.s3[.-][a-z0-9.-]*amazonaws\.com|damsafety\.org|damtoolbox\.org)$")
MAX_BYTES = 400 * 1024 * 1024


def say(msg):
    print(msg, flush=True)



def safe_url(url):
    """1.2: percent-encode spaces and other unsafe characters an index page links with
    (USBR best-practices chapters: 'A6-Subjective ProbabilityAndExpertElicitation.pdf');
    urllib refuses a URL with a raw space. Already-encoded %XX is kept."""
    p = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(p.path, safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(p.query, safe="=&%/:+,;@-._~")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, p.fragment))

class Fetcher:
    def __init__(self, delay, retries=3):
        self.delay = delay
        self.retries = retries
        self.last = 0.0
        self.robots = {}

    def allowed(self, url):
        p = urllib.parse.urlsplit(url)
        root = "%s://%s" % (p.scheme, p.netloc)
        if root not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                req = urllib.request.Request(root + "/robots.txt", headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    rp.parse(r.read().decode("utf-8", "replace").splitlines())
            except Exception:
                rp = None                       # no robots.txt (e.g. S3): allowed
            self.robots[root] = rp
        rp = self.robots[root]
        return True if rp is None else rp.can_fetch(UA, url)

    def _wait(self):
        dt = time.time() - self.last
        if dt < self.delay:
            time.sleep(self.delay - dt)
        self.last = time.time()

    def open(self, url):
        url = safe_url(url)
        last = None
        for attempt in range(self.retries):
            self._wait()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                return urllib.request.urlopen(req, timeout=120)
            except urllib.error.HTTPError as e:
                if e.code in (403, 404, 410):
                    raise
                last = e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
            time.sleep(2 ** attempt * 2)
        raise last

    def text(self, url):
        with self.open(url) as r:
            return r.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- page parsing

def main_content(page_html):
    """The page body between the main-content anchor and the sponsor strip / footer."""
    i = page_html.find('id="main-content"')
    body = page_html[i:] if i >= 0 else page_html
    for stop in ("sustaining members", "<footer"):
        j = body.lower().find(stop)
        if j > 0:
            body = body[:j]
    return body


def clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def page_links(page_html, base):
    out = []
    for href, inner in re.findall(r'<a\s[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', main_content(page_html), re.S):
        href = html.unescape(href).strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")) or "/cdn-cgi/" in href:
            continue
        out.append((urllib.parse.urljoin(base, href), clean_text(inner)))
    return out


def is_file_url(url):
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(FILE_EXT) or "/s3fs-public/" in path


def is_asdso_host(url):
    return bool(ASDSO_FILE_HOSTS.match(urllib.parse.urlsplit(url).hostname or ""))


def file_links(page_html, base):
    """{url: best title}: one entry per file (pages often link a file twice, once with no text)."""
    found = {}
    for url, title in page_links(page_html, base):
        if not is_file_url(url):
            continue
        if title and GENERIC_TITLE.match(title):
            title = ""
        if title and (not found.get(url) or len(title) > len(found[url])):
            found[url] = title
        else:
            found.setdefault(url, title)
    return found


# --------------------------------------------------------------------------- citations

NUMBER_RE = re.compile(r"\b((?:FEMA|USBR|USACE|NRCS|FERC|EM|ER|TR|DSO)[ -]?(?:P[ -]?)?\d{2,4}[A-Za-z]?(?:-\d+)?)\b", re.I)


GENERIC_TITLE = re.compile(r"^(view( or download)?( as)?( a)? ?pdf|download( pdf| here)?|pdf|learn more|click here|here|view|link|"
                           r"https?://\S+)$", re.I)


def guess_citation(title, url, default_org="ASDSO"):
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(url).path))
    if not title or GENERIC_TITLE.match(title.strip()):
        title = re.sub(r"[_]+", " ", os.path.splitext(name)[0]).strip()
    t = title
    number = ""
    m = re.search(r"\(([^()]*\d[^()]*)\)\s*$", t)          # "... (FEMA P-946)"
    if m and NUMBER_RE.search(m.group(1)):
        number = NUMBER_RE.search(m.group(1)).group(1)
    elif NUMBER_RE.search(t):
        number = NUMBER_RE.search(t).group(1)
    elif NUMBER_RE.search(name):
        number = NUMBER_RE.search(name).group(1)
    number = re.sub(r"^fema", "FEMA", re.sub(r"\s+", " ", number), flags=re.I)
    if number and not re.match(r"FEMA[ -]", number) and number.upper().startswith("FEMA"):
        number = "FEMA " + number[4:]
    org = default_org
    if number.upper().startswith("FEMA") or re.search(r"\bFEMA\b|National Dam Safety Program|NDSP", t + " " + name, re.I):
        org = "FEMA"
    years = re.findall(r"(?<!\d)(19[5-9]\d|20[0-3]\d)(?!\d)", name + " " + t)
    year = years[-1] if years else ""
    return {"title": t, "org": org, "number": number, "year": year}


def safe_name(url):
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(url).path)) or "file"
    name = re.sub(r"[^\w.\- ()&,']+", "_", name).strip(" .") or "file"
    return name[:150]


# --------------------------------------------------------------------------- download

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def download(fetcher, url, dest):
    """Returns (status, bytes). Writes to dest.part then renames, so a cut-off run never leaves a
    half file under the real name. Refuses HTML (a login page instead of the document)."""
    part = dest + ".part"
    with fetcher.open(url) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        size = int(r.headers.get("Content-Length") or 0)
        if size > MAX_BYTES:
            return "too large (%d MB)" % (size >> 20), 0
        first = r.read(4096)
        if b"<html" in first[:1024].lower() or "text/html" in ctype:
            return "members-only or not a file (got a web page)", 0
        n = len(first)
        with open(part, "wb") as f:
            f.write(first)
            for b in iter(lambda: r.read(1 << 20), b""):
                n += len(b)
                if n > MAX_BYTES:
                    f.close()
                    os.remove(part)
                    return "too large", 0
                f.write(b)
    if dest.lower().endswith(".pdf") and not first.startswith(b"%PDF"):
        os.remove(part)
        return "not a PDF", 0
    os.replace(part, dest)
    return "ok", n


def load_manifest(out):
    p = os.path.join(out, "manifest.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"tool": "fetch_asdso.py", "files": {}}


def save_manifest(out, man):
    man["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    p = os.path.join(out, "manifest.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)


def write_library_csv(folder, rows):
    """library.csv for vor: file,title,authors,year,org,number. Keeps rows you edited."""
    p = os.path.join(folder, "library.csv")
    have = {}
    if os.path.isfile(p):
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("file"):
                    have[r["file"]] = r
    for r in rows:
        have.setdefault(r["file"], r)            # never overwrite a row already there
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["file", "title", "authors", "year", "org", "number"])
        w.writeheader()
        for k in sorted(have):
            w.writerow({c: have[k].get(c, "") for c in w.fieldnames})


def fetch_set(fetcher, label, items, out, man, args):
    """items: {url: title}. Downloads into out/label/."""
    folder = os.path.join(out, label)
    os.makedirs(folder, exist_ok=True)
    rows, counts = [], {"new": 0, "have": 0, "skipped": 0, "failed": 0}
    for n, (url, title) in enumerate(sorted(items.items(), key=lambda kv: kv[1] or kv[0]), 1):
        cite = guess_citation(title, url, "FEMA" if label.startswith("ndsp") else "ASDSO")
        name = safe_name(url)
        dest = os.path.join(folder, name)
        tag = "[%s %d/%d]" % (label, n, len(items))
        if not is_asdso_host(url) and not args.external:
            say("%s skip (other site; --external to fetch): %s" % (tag, url))
            counts["skipped"] += 1
            man["files"].setdefault(url, {"status": "external, not fetched", "title": cite["title"], "page": label})
            continue
        if os.path.isfile(dest) and man["files"].get(url, {}).get("status") == "ok":
            counts["have"] += 1
            rows.append(dict(cite, file=name, authors=""))
            continue
        if args.dry_run:
            say("%s would fetch: %s  <- %s" % (tag, cite["title"][:90], url))
            continue
        if not fetcher.allowed(url):
            say("%s robots.txt disallows: %s" % (tag, url))
            counts["skipped"] += 1
            continue
        try:
            status, size = download(fetcher, url, dest)
        except Exception as e:
            status, size = "failed: %s" % e, 0
        entry = {"status": status, "title": cite["title"], "page": label, "file": os.path.join(label, name),
                 "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if status == "ok":
            entry.update(bytes=size, sha256=sha256_of(dest), number=cite["number"], year=cite["year"], org=cite["org"])
            rows.append(dict(cite, file=name, authors=""))
            counts["new"] += 1
            say("%s ok  %5.1f MB  %s" % (tag, size / 1e6, cite["title"][:90]))
        else:
            counts["failed" if status.startswith("failed") else "skipped"] += 1
            say("%s %s: %s" % (tag, status, url))
        man["files"][url] = entry
        save_manifest(out, man)
    if rows and not args.dry_run:
        write_library_csv(folder, rows)
    return counts


# --------------------------------------------------------------------------- reference database

def search_references(fetcher, query, pages, out, args):
    """ASDSO Dam Safety Resource Database: every hit's citation to asdso_references.csv; hits with a
    public file become download items."""
    items, recs = {}, []
    for pg in range(pages):
        url = SITE + "/resource/results?" + urllib.parse.urlencode({"fulltext": query, "page": pg})
        say("search page %d: %s" % (pg + 1, url))
        try:
            page = fetcher.text(url)
        except Exception as e:
            say("  search failed: %s" % e)
            break
        hits = [(u, t) for u, t in page_links(page, url)
                if re.match(re.escape(SITE) + r"/(content|reference)/", u)]
        if not hits:
            break
        for u, t in dict.fromkeys(hits):
            try:
                d = fetcher.text(u)
            except Exception as e:
                say("  %s: %s" % (t[:70], e))
                continue
            rec = parse_reference(d, u)
            members_only = "Please log in to view Members Only content" in d
            files = file_links(d, u)
            rec["members_only"] = "yes" if members_only else ""
            rec["files"] = " ".join(files)
            recs.append(rec)
            for fu, ft in files.items():
                items[fu] = rec.get("title") or ft
            say("  %s%s%s" % (rec.get("title", t)[:80], "  [members only]" if members_only else "",
                              "  [%d file(s)]" % len(files) if files else ""))
    if recs:
        p = os.path.join(out, "asdso_references.csv")
        cols = ["title", "authors", "year", "type", "organization", "event", "abstract", "members_only", "files", "url"]
        old = {}
        if os.path.isfile(p):
            with open(p, newline="", encoding="utf-8-sig") as f:
                old = {r["url"]: r for r in csv.DictReader(f)}
        for r in recs:
            old[r["url"]] = r
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in old.values():
                w.writerow({c: r.get(c, "") for c in cols})
        say("%d reference records -> %s" % (len(recs), p))
    return items


FIELD_LABELS = {"Reference Title": "title", "Author/Presenter": "authors", "Year": "year",
                "Resource Type": "type", "Organization/Agency": "organization", "Event Name": "event",
                "Abstract/Additional Information": "abstract"}


def parse_reference(page_html, url):
    t = clean_text(re.sub(r"<script.*?</script>|<style.*?</style>", "", main_content(page_html), flags=re.S))
    labels = sorted(FIELD_LABELS, key=lambda k: t.find(k) if k in t else 10 ** 9)
    rec = {"url": url}
    stops = ["Reference Title", "Author/Presenter", "Organization/Agency", "Publisher Name", "Year", "Date",
             "Event Name", "Event Location", "ASDSO Session Title", "Abstract/Additional Information",
             "Resource Type", "Please log in", "Topic", "Keywords", "Page Numbers", "Volume", "Issue",
             "Document Number", "URL(s)", "ISBN", "Journal", "Publisher", "SPECIAL THANKS"]
    for lab in labels:
        i = t.find(lab)
        if i < 0:
            continue
        rest = t[i + len(lab):]
        end = min([rest.find(" " + s + " ") for s in stops if rest.find(" " + s + " ") >= 0] or [len(rest)])
        rec[FIELD_LABELS[lab]] = rest[:end].strip()[:2000]
    return rec


# --------------------------------------------------------------------------- main

# --------------------------------------------------------------------------- curated catalog

CATALOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference_sources.json")


def html_to_markdown(page_html, entry, url):
    """A law / policy web page as plain text with a citation header, for vor (.md)."""
    body = page_html
    for tag in ("script", "style", "noscript", "nav", "header", "footer", "form", "svg"):
        body = re.sub(r"<%s\b.*?</%s>" % (tag, tag), " ", body, flags=re.S | re.I)
    m = re.search(r"<main\b.*?</main>", body, re.S | re.I) or re.search(r'id="(?:main-content|content|main)".*', body, re.S | re.I)
    if m:
        body = m.group(0)
    body = re.sub(r"<h([1-4])[^>]*>(.*?)</h\1>", lambda h: "\n\n%s %s\n\n" % ("#" * min(3, int(h.group(1)) + 1),
                  clean_text(h.group(2))), body, flags=re.S | re.I)
    body = re.sub(r"<li[^>]*>", "\n- ", body, flags=re.I)
    body = re.sub(r"</?(p|div|br|tr|table|section|article|blockquote|dd|dt)\b[^>]*>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html.unescape(body).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in body.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    head = ["# %s" % entry["title"], "",
            "Source: %s" % url,
            "Publisher: %s" % entry.get("org", ""),
            "Retrieved: %s (web page saved as text by fetch_asdso.py; check the live page before citing)"
            % datetime.now(timezone.utc).strftime("%Y-%m-%d"), ""]
    if entry.get("number"):
        head.insert(4, "Citation: %s" % entry["number"])
    return "\n".join(head) + "\n" + text + "\n"


def load_catalog(path):
    with open(path, encoding="utf-8") as f:
        cat = json.load(f)
    return cat.get("groups", {})


def write_url_list(path, groups, pages):
    """1.3: a plain-text index of every library source and its web address — no download.

    Every curated-catalog entry (its title, number, and URL, plus any alternate URLs it
    tries and any 'get by hand' note), followed by the ASDSO resource-center index pages
    (these are crawled for their PDF links at download time, so the page URL is listed, not
    each file). Written offline from reference_sources.json; nothing is fetched."""
    lines = ["# Vor library — source files and web addresses",
             "# Written by fetch_asdso.py --urls (v%s) on %s. No files were downloaded." % (
                 VERSION, datetime.now(timezone.utc).strftime("%Y-%m-%d")),
             "# kind: pdf = direct download | html = web page saved as text | index = all matching",
             "#       PDF links on the page | manual = no stable public link, get by hand.",
             ""]
    n = 0
    for g, entries in groups.items():
        lines.append("== catalog group: %s (%d sources) ==" % (g, len(entries)))
        for e in entries:
            if e.get("skip"):
                continue
            n += 1
            num = (" [%s]" % e["number"]) if e.get("number") else ""
            yr = (" (%s)" % e["year"]) if e.get("year") else ""
            lines.append("%s%s%s — %s  {%s}" % (e.get("title", e["id"]), num, yr,
                                                e.get("org", ""), e.get("kind", "pdf")))
            lines.append("    %s" % (e.get("url") or "(no public URL — see note)"))
            for alt in e.get("alt") or []:
                lines.append("    alt: %s" % alt)
            if e.get("match"):
                lines.append("    download PDF links matching: %s" % e["match"])
            if e.get("note"):
                lines.append("    note: %s" % e["note"])
            lines.append("")
    lines.append("== ASDSO resource-center pages (crawled for PDF links at download time) ==")
    for label, url in pages:
        n += 1
        lines.append("%s" % label)
        lines.append("    %s" % url)
        lines.append("")
    lines.append("# %d sources listed." % n)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return n


def _cite(entry, file_name, title=None):
    return {"file": file_name, "title": title or entry["title"], "authors": entry.get("authors", ""),
            "year": entry.get("year", ""), "org": entry.get("org", ""), "number": entry.get("number", "")}


def fetch_catalog(fetcher, groups, wanted, out, args):
    """Downloads the catalog groups into out/<group>/. Returns totals and the hosts that failed."""
    total = {"new": 0, "have": 0, "skipped": 0, "failed": 0, "manual": 0}
    bad_hosts = {}
    man = load_manifest(out)
    man["tool"] = "fetch_asdso.py --catalog"
    for g in wanted:
        entries = [e for e in groups.get(g, []) if not e.get("skip")]
        folder = os.path.join(out, g)
        os.makedirs(folder, exist_ok=True)
        say("\n== catalog: %s (%d sources)  -> %s" % (g, len(entries), folder))
        rows, report = [], []
        for n, e in enumerate(entries, 1):
            tag = "[%s %d/%d]" % (g, n, len(entries))
            kind = e.get("kind", "pdf")
            urls = [e["url"]] + list(e.get("alt", []))
            status, got = "", ""
            if kind == "manual":
                say("%s get by hand: %s  (%s)" % (tag, e["title"][:80], e.get("note", e["url"])))
                total["manual"] += 1
                report.append(dict(e, status="get by hand", file=""))
                continue
            if args.dry_run:
                say("%s would fetch (%s): %s  <- %s" % (tag, kind, e["title"][:80], e["url"]))
                continue
            if kind == "index":
                c = fetch_catalog_index(fetcher, e, folder, man, args, tag, bad_hosts)
                for k in ("new", "have", "failed", "skipped"):
                    total[k] += c[k]
                rows.extend(c["rows"])
                report.append(dict(e, status="index: %d new, %d already here, %d failed" % (c["new"], c["have"], c["failed"]),
                                   file=e["id"] + "/"))
                continue
            name = e["id"] + (".md" if kind == "html" else ".pdf")
            dest = os.path.join(folder, name)
            if os.path.isfile(dest) and man["files"].get(e["id"], {}).get("status") == "ok":
                total["have"] += 1
                rows.append(_cite(e, name))
                report.append(dict(e, status="already here", file=name))
                continue
            for u in urls:
                host = urllib.parse.urlsplit(u).hostname or u
                if host in bad_hosts:
                    status = "skipped: %s could not be reached earlier in this run" % host
                    continue
                if not fetcher.allowed(u):
                    status = "robots.txt disallows"
                    continue
                try:
                    if kind == "html":
                        page = fetcher.text(u)
                        md = html_to_markdown(page, e, u)
                        if len(md) < 600:
                            status = "page had almost no text"
                            continue
                        with open(dest, "w", encoding="utf-8") as f:
                            f.write(md)
                        status, size = "ok", len(md.encode("utf-8"))
                    else:
                        status, size = download(fetcher, u, dest)
                except Exception as ex:
                    status, size = "failed: %s" % ex, 0
                    if not isinstance(ex, urllib.error.HTTPError):      # site unreachable, not a missing file
                        bad_hosts.setdefault(host, str(ex)[:80])
                if status == "ok":
                    got = u
                    break
            entry = {"status": status, "title": e["title"], "page": "catalog:" + g, "url": got or e["url"],
                     "file": os.path.join(g, name), "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            if status == "ok":
                entry.update(bytes=size, sha256=sha256_of(dest), number=e.get("number", ""), year=e.get("year", ""), org=e.get("org", ""))
                rows.append(_cite(e, name))
                total["new"] += 1
                say("%s ok  %5.1f MB  %s" % (tag, size / 1e6, e["title"][:80]))
            else:
                total["failed"] += 1
                say("%s %s: %s" % (tag, status, e["url"]))
            man["files"][e["id"]] = entry
            save_manifest(out, man)
            report.append(dict(e, status=status, file=name if status == "ok" else ""))
        if not args.dry_run:
            if rows:
                write_library_csv(folder, rows)
            write_sources_csv(folder, report)
    if not args.dry_run:
        save_manifest(out, man)
    return total, bad_hosts


def fetch_catalog_index(fetcher, e, folder, man, args, tag, bad_hosts):
    """kind=index: every file link on the page whose URL matches e['match'] goes to folder/<id>/."""
    c = {"new": 0, "have": 0, "failed": 0, "skipped": 0, "rows": []}
    sub = os.path.join(folder, e["id"])
    host = urllib.parse.urlsplit(e["url"]).hostname or e["url"]
    if host in bad_hosts:
        say("%s skipped: %s could not be reached earlier in this run" % (tag, host))
        c["failed"] += 1
        return c
    try:
        if not fetcher.allowed(e["url"]):
            say("%s robots.txt disallows: %s" % (tag, e["url"]))
            c["skipped"] += 1
            return c
        links = file_links(fetcher.text(e["url"]), e["url"])
    except Exception as ex:
        if not isinstance(ex, urllib.error.HTTPError):
            bad_hosts.setdefault(host, str(ex)[:80])
        say("%s could not read the index page: %s" % (tag, ex))
        c["failed"] += 1
        return c
    pat = re.compile(e.get("match") or ".")
    links = {u: t for u, t in links.items() if pat.search(u) or pat.search(t or "")}
    say("%s index %s: %d matching file(s)" % (tag, e["id"], len(links)))
    os.makedirs(sub, exist_ok=True)
    for u, t in sorted(links.items()):
        name = safe_name(u)
        dest = os.path.join(sub, name)
        title = t if t and not GENERIC_TITLE.match(t) else guess_citation("", u, e.get("org", ""))["title"]
        title = "%s: %s" % (e["title"], title) if title.lower() not in e["title"].lower() else title
        if os.path.isfile(dest) and man["files"].get(u, {}).get("status") == "ok":
            c["have"] += 1
            c["rows"].append(_cite(e, os.path.join(e["id"], name), title))
            continue
        try:
            status, size = download(fetcher, u, dest)
        except Exception as ex:
            status, size = "failed: %s" % ex, 0
        entry = {"status": status, "title": title, "page": "catalog:" + e["id"], "file": os.path.relpath(dest, os.path.dirname(folder)),
                 "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if status == "ok":
            entry.update(bytes=size, sha256=sha256_of(dest), org=e.get("org", ""))
            c["rows"].append(_cite(e, os.path.join(e["id"], name), title))
            c["new"] += 1
            say("%s   ok  %5.1f MB  %s" % (tag, size / 1e6, title[:80]))
        else:
            c["failed"] += 1
            say("%s   %s: %s" % (tag, status, u))
        man["files"][u] = entry
    return c


def write_sources_csv(folder, report):
    """sources.csv: every catalog entry with what happened, so gaps are visible (and the 'get by hand' list)."""
    p = os.path.join(folder, "sources.csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "status", "file", "title", "org", "number", "year", "url", "check", "note"])
        w.writeheader()
        for r in report:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})


def vor_home():
    """1.4: the folder vor.py reads, by vor.py's own rule (its _home): VOR_HOME, else whichever
    of ~/Vor and the pre-rename ~/DamResearch HOLDS THE INDEX, else ~/Vor.

    Before 1.4 this script always wrote to ~/Vor. Run before setup-vor.zsh had moved an
    existing ~/DamResearch across, that created ~/Vor, which made vor.py and the setup script
    both treat the rename as done: the downloads landed in a second library and the indexed
    one was left behind, with no message from anything."""
    if os.environ.get("VOR_HOME"):
        return os.path.expanduser(os.environ["VOR_HOME"])
    new = os.path.expanduser("~/Vor")
    old = os.path.expanduser("~/DamResearch")
    # DAMRESEARCH_HOME: the old installers exported it, so a library kept somewhere else is
    # still named by it in a shell whose rc file has not been rewritten yet.
    legacy = os.path.expanduser(os.environ.get("DAMRESEARCH_HOME") or old)
    for d in (new, legacy, old):
        if any(os.path.isfile(os.path.join(d, f)) for f in ("vor.db", "research.db")):
            return d
    if not os.path.isdir(new) and os.path.isdir(old):
        return old
    return new


def main():
    if sys.argv[1:] == ["--print-home"]:        # for the shell scripts: one rule, not four copies
        print(vor_home())
        return
    ap = argparse.ArgumentParser(description="Download public ASDSO-listed dam-safety references into the vor library.")
    ap.add_argument("--out", default=os.path.join(vor_home(), "library", "asdso"))
    ap.add_argument("--page", action="append", default=[], help="another ASDSO page to collect file links from")
    ap.add_argument("--no-default-pages", action="store_true", help="skip the built-in resource-center pages")
    ap.add_argument("--search", help="also search ASDSO's Dam Safety Resource Database for these words")
    ap.add_argument("--search-pages", type=int, default=2, help="result pages to read (about 25 hits each)")
    ap.add_argument("--external", action="store_true", help="also download files hosted on other sites (fema.gov, ...)")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests (default 1.5)")
    ap.add_argument("--dry-run", action="store_true", help="list, download nothing")
    ap.add_argument("--catalog", nargs="?", const="all", metavar="GROUPS",
                    help="download the curated catalog (reference_sources.json) instead of the ASDSO pages: "
                         "all (default), or a comma list of groups, e.g. hazard or breach,ponds")
    ap.add_argument("--catalog-file", default=CATALOG_FILE, help="catalog JSON (default: next to this script)")
    ap.add_argument("--catalog-out", default=os.path.join(vor_home(),
                                                           "library", "references"),
                    help="folder for catalog downloads (default ~/Vor/library/references)")
    ap.add_argument("--asdso", action="store_true", help="with --catalog: also run the ASDSO pages")
    ap.add_argument("--urls", nargs="?", const="library-urls.txt", metavar="FILE",
                    help="write a plain-text list of every catalog source and its URL (default "
                         "library-urls.txt), download nothing, and exit")
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args()

    if args.urls is not None:
        groups = load_catalog(args.catalog_file)
        pages = [] if args.no_default_pages else list(DEFAULT_PAGES)
        dest = os.path.abspath(os.path.expanduser(args.urls))
        n = write_url_list(dest, groups, pages)
        say("Wrote %d sources and their URLs to %s" % (n, dest))
        return

    fetcher = Fetcher(args.delay)
    if args.catalog:
        groups = load_catalog(args.catalog_file)
        wanted = list(groups) if args.catalog == "all" else [g.strip() for g in args.catalog.split(",") if g.strip()]
        unknown = [g for g in wanted if g not in groups]
        if unknown:
            sys.exit("unknown catalog group(s): %s (have: %s)" % (", ".join(unknown), ", ".join(groups)))
        cout = os.path.abspath(os.path.expanduser(args.catalog_out))
        os.makedirs(cout, exist_ok=True)
        t, bad = fetch_catalog(fetcher, groups, wanted, cout, args)
        say("\nCatalog: %d new, %d already here, %d failed, %d to get by hand (see sources.csv).  Folder: %s"
            % (t["new"], t["have"], t["failed"], t["manual"], cout))
        if bad:
            say("Sites that could not be reached (network block, site down or moved):")
            for h, why in sorted(bad.items()):
                say("  %-40s %s" % (h, why))
        if not args.asdso:
            args.no_default_pages = True
        if not args.asdso and not args.page and not args.search:
            if t["new"] and not args.dry_run:
                say("Index them:  vor ingest")
            return

    out = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out, exist_ok=True)
    man = load_manifest(out)
    pages = [] if args.no_default_pages else list(DEFAULT_PAGES)
    for u in args.page:
        slug = re.sub(r"[^a-z0-9]+", "-", urllib.parse.urlsplit(u).path.lower()).strip("-")[:50] or "page"
        pages.append((slug, u))
    total = {"new": 0, "have": 0, "skipped": 0, "failed": 0}
    for label, url in pages:
        say("\n== %s  %s" % (label, url))
        if not fetcher.allowed(url):
            say("robots.txt disallows this page; skipped")
            continue
        try:
            items = file_links(fetcher.text(url), url)
        except Exception as e:
            say("could not read the page: %s" % e)
            continue
        say("%d file link(s)" % len(items))
        for k, v in fetch_set(fetcher, label, items, out, man, args).items():
            total[k] += v
    if args.search:
        say("\n== reference database: %r" % args.search)
        items = search_references(fetcher, args.search, args.search_pages, out, args)
        if items:
            slug = "search-" + re.sub(r"[^a-z0-9]+", "-", args.search.lower()).strip("-")[:40]
            for k, v in fetch_set(fetcher, slug, items, out, man, args).items():
                total[k] += v
    if not args.dry_run:
        save_manifest(out, man)
    say("\nDone: %d new, %d already here, %d skipped, %d failed.  Folder: %s"
        % (total["new"], total["have"], total["skipped"], total["failed"], out))
    if total["new"] and not args.dry_run:
        say("Index them:  vor ingest")


if __name__ == "__main__":
    main()
