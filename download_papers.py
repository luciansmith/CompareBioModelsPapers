#!/usr/bin/env python3
"""
Download PDFs for Biomodels papers listed in biomd_publication_info_with_controls.json.

Strategy (tried in order for each paper):
  1. PubMed Central (PMC) — free, works without VPN
  2. Europe PMC — fallback for PMC papers
  3. Unpaywall — finds open-access versions of paywalled papers
  4. Direct DOI — uses your UW VPN for subscribed journals, with
                   publisher-specific PDF URL patterns

Usage:
  python download_papers.py                          # first 10 new papers
  python download_papers.py --count 50              # first 50 new papers
  python download_papers.py --start 10 --count 10  # papers 11-20 (new only)
  python download_papers.py --all                   # all new papers
  python download_papers.py --all --skip tried      # skip all previously attempted
  python download_papers.py --all --skip auth       # retry everything except auth-blocked
  python download_papers.py --all --skip none       # reprocess everything

Requirements:
  pip install requests

Run with UW VPN active (BIG-IP Edge Client) for best journal access.
"""

import json
import re
import sys
import time
import argparse
from pathlib import Path

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("Missing dependency. Run:  pip install requests")
    sys.exit(1)

# Optional: use your browser's cookies for journal access
# Install with:  pip install browser-cookie3
try:
    import browser_cookie3
    HAS_BROWSER_COOKIES = True
except ImportError:
    HAS_BROWSER_COOKIES = False

# Optional: Google Scholar open-access PDF search
# Install with:  pip install scholarly
try:
    import scholarly as _scholarly_mod
    HAS_SCHOLARLY = True
except ImportError:
    HAS_SCHOLARLY = False

# Sentinel returned (instead of None) when a publisher explicitly denies access
# due to missing institutional subscription — signals "don't retry, log as
# no_subscription" rather than a transient failure.
_NO_SUBSCRIPTION = object()

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR    = Path(__file__).parent
JSON_FILE     = SCRIPT_DIR / "biomd_publication_info_with_controls.json"
OUTPUT_DIR    = SCRIPT_DIR / "Biomodels papers"
TRACKING_FILE = SCRIPT_DIR / "download_tracking.json"

# Optional: place a Netscape-format cookies.txt file here for journal auth.
# Export from Chrome with the "Get cookies.txt LOCALLY" extension (visit
# each publisher site first while logged in via UW, then export).
COOKIES_FILE  = SCRIPT_DIR / "cookies.txt"

# UW Library's LibKey link resolver — resolves PMIDs to full-text PDFs
# through UW's journal subscriptions.  Requires UW VPN (full-tunnel) active.
# Library ID 3478 = University of Washington.
UW_LIBKEY_ID  = "3478"

EMAIL = "lpsmith@uw.edu"   # used for NCBI + Unpaywall API contact info

# NCBI allows 3 req/s without an API key; stay comfortably under
NCBI_DELAY    = 0.4
GENERAL_DELAY = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ── HTTP sessions ─────────────────────────────────────────────────────────────
_retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
# EuropePMC returns 500 for "PDF not available" (not a transient error), so we
# must NOT retry on 500 there — it wastes ~15 s and hides the redirect chain.
_retry_no500 = Retry(total=2, backoff_factor=1.0, status_forcelist=[429, 502, 503, 504])

def _make_session(headers):
    s = requests.Session()
    s.headers.update(headers)
    s.mount("https://", HTTPAdapter(max_retries=_retry))
    s.mount("http://",  HTTPAdapter(max_retries=_retry))
    return s

# Browser-like session for journal sites
session = _make_session(HEADERS)

# Bot-like session for NCBI APIs (they don't care about UA, and static HTML
# is served more reliably to non-browser agents)
NCBI_HEADERS = {"User-Agent": f"BiomodelsDownloader/1.0 (mailto:{EMAIL})"}
ncbi_session = _make_session(NCBI_HEADERS)

def _load_cookies_txt(s):
    """
    Load a Netscape-format cookies.txt file into session s (if the file exists).
    Export from Chrome with the "Get cookies.txt LOCALLY" extension:
      1. Install: https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
      2. Visit each publisher site (PNAS, Springer, etc.) while logged in via UW
      3. Click the extension icon → Export  (saves cookies.txt for that domain)
      4. Combine all exported files into one cookies.txt in the script folder

    Note: Cookie-Editor marks HttpOnly cookies with a '#HttpOnly_' prefix on the
    domain field.  Python's MozillaCookieJar treats '#'-prefixed lines as comments
    and silently skips them.  We strip that prefix before loading.
    """
    if not COOKIES_FILE.exists():
        return 0
    try:
        import http.cookiejar
        import tempfile, os

        raw = COOKIES_FILE.read_text(encoding="utf-8", errors="replace")
        fixed_lines = []
        for line in raw.splitlines():
            # '#HttpOnly_domain ...' → 'domain ...'  (preserve the cookie data)
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            fixed_lines.append(line)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False, encoding="utf-8") as tf:
            tf.write("\n".join(fixed_lines))
            tmp_path = tf.name

        try:
            jar = http.cookiejar.MozillaCookieJar(tmp_path)
            jar.load(ignore_discard=True, ignore_expires=True)
            s.cookies.update(jar)
            return len(list(jar))
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        print(f"  ⚠  cookies.txt load error: {e}")
        return 0


def _prompt_cookies_refresh(site_name, visit_url, cookie_instructions):
    """
    Print instructions for refreshing session cookies for a site, then
    return False (no retry).

    To enable interactive retry prompting in the future, replace `return False`
    with the commented-out block below.
    """
    bar = "=" * 62
    print(f"\n  {bar}")
    print(f"  ⚠  {site_name}: session expired or bot-check triggered.")
    print(f"  To fix:")
    print(f"    1. Open Chrome and visit:")
    print(f"       {visit_url}")
    print(f"    2. {cookie_instructions}")
    print(f"    3. Re-run the script after updating cookies.txt.")
    print(f"  {bar}")
    return False
    # ── Enable this block to prompt interactively and retry in-process: ───────
    # print(f"    3. Press Enter here to retry (Ctrl+C to skip).")
    # print(f"  {bar}")
    # try:
    #     input("  > ")
    #     return True
    # except (EOFError, KeyboardInterrupt):
    #     print("  (skipping refresh)")
    #     return False


def make_cookie_session(domain):
    """
    Return a session pre-loaded with cookies for a domain.
    Always loads cookies.txt first (broad baseline), then layers on
    browser_cookie3 cookies for the specific domain — so domains not
    present in cookies.txt (e.g. thirdiron.com, libkey.io) still get
    their session cookies from the live browser profile.
    """
    s = _make_session(HEADERS)
    _load_cookies_txt(s)   # baseline: all cookies.txt entries
    if HAS_BROWSER_COOKIES and domain:
        for loader in (browser_cookie3.chrome, browser_cookie3.firefox):
            try:
                s.cookies.update(loader(domain_name=domain))
                break   # stop after first successful loader
            except Exception:
                pass
    return s

# ── Tracking JSON ─────────────────────────────────────────────────────────────
LOCK_FILE = SCRIPT_DIR / "download_tracking.lock"

def _acquire_lock():
    """Spin on exclusive file creation — atomic and cross-platform."""
    while True:
        try:
            open(LOCK_FILE, 'x').close()
            return
        except FileExistsError:
            time.sleep(0.05)

def _release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass

def load_tracking():
    return json.loads(TRACKING_FILE.read_text()) if TRACKING_FILE.exists() else {}

def save_tracking(tracking, pmid):
    """Lock the tracking file, read it, update just this entry, write atomically, unlock."""
    import tempfile, os
    print("    [tracking] acquiring lock ...", end="", flush=True)
    _acquire_lock()
    try:
        print(" writing ...", end="", flush=True)
        on_disk = json.loads(TRACKING_FILE.read_text()) if TRACKING_FILE.exists() else {}
        on_disk[pmid] = tracking[pmid]
        # Write to a temp file in the same directory, then rename — atomic on POSIX,
        # best-effort on Windows (os.replace is atomic on Windows NTFS too).
        tmp_fd, tmp_path = tempfile.mkstemp(dir=SCRIPT_DIR, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(json.dumps(on_disk, indent=2))
            os.replace(tmp_path, TRACKING_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    finally:
        _release_lock()
        print(" done.")

# ── Utilities ─────────────────────────────────────────────────────────────────
def is_pdf(data, min_size=5_000):
    """Check that a byte string looks like a real PDF."""
    return len(data) >= min_size and data[:4] == b"%PDF"

def safe_filename(pmid, title):
    clean_id = re.sub(r"[^\w.-]", "_", str(pmid))   # sanitize slashes etc.
    clean = re.sub(r"[^\w\s-]", "_", title[:70]).strip()
    clean = re.sub(r"\s+", "_", clean)
    return f"PMID{clean_id}_{clean}.pdf"

# ── NCBI ID conversion ────────────────────────────────────────────────────────
def get_pmc_info(accessions, debug=False):
    """
    Convert accessions to PMCIDs + DOIs.
    Handles two types of accessions:
      - Numeric PMIDs  → look up via idconv + esummary
      - DOI accessions → the accession itself is the DOI; skip NCBI lookup
    Returns {accession: {pmcid, doi}, ...}
    """
    result = {}

    # Split: numeric PMIDs vs DOI-type accessions
    numeric = [a for a in accessions if re.match(r'^\d+$', a)]
    doi_accs = [a for a in accessions if not re.match(r'^\d+$', a)]

    # DOI-type entries already carry their own DOI
    for acc in doi_accs:
        result[acc] = {"pmcid": None, "doi": acc}

    if not numeric:
        return result

    # Strategy A: PMC ID Converter (only numeric PMIDs, batch of 100)
    idconv_url = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
    for i in range(0, len(numeric), 100):
        batch = numeric[i:i + 100]
        try:
            r = ncbi_session.get(
                idconv_url,
                params={"ids": ",".join(batch), "format": "json",
                        "tool": "BiomodelsDownloader", "email": EMAIL},
                timeout=30,
            )
            r.raise_for_status()
            if debug:
                print(f"  [debug] idconv status={r.status_code}, body={r.text[:300]}")
            for rec in r.json().get("records", []):
                pmid = rec.get("pmid")
                if pmid:
                    result[pmid] = {
                        "pmcid": rec.get("pmcid") or None,
                        "doi":   rec.get("doi")   or None,
                    }
        except Exception as e:
            print(f"  ⚠  idconv error (batch {i//100 + 1}): {e}")
        time.sleep(NCBI_DELAY)

    # Strategy B: esummary for numeric PMIDs still missing a DOI
    missing_doi = [p for p in numeric if not result.get(p, {}).get("doi")]
    if missing_doi:
        esum_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        for i in range(0, len(missing_doi), 20):
            batch = missing_doi[i:i + 20]
            try:
                r = ncbi_session.get(
                    esum_url,
                    params={"db": "pubmed", "id": ",".join(batch),
                            "retmode": "json", "tool": "BiomodelsDownloader",
                            "email": EMAIL},
                    timeout=30,
                )
                r.raise_for_status()
                if debug:
                    print(f"  [debug] esummary status={r.status_code}, body={r.text[:300]}")
                data = r.json().get("result", {})
                for pmid in batch:
                    rec = data.get(pmid, {})
                    doi = None
                    for aid in rec.get("articleids", []):
                        if aid.get("idtype") == "doi":
                            doi = aid.get("value")
                            break
                    if pmid not in result:
                        result[pmid] = {"pmcid": None, "doi": None}
                    if doi:
                        result[pmid]["doi"] = doi
            except Exception as e:
                print(f"  ⚠  esummary error (batch {i//20 + 1}): {e}")
            time.sleep(NCBI_DELAY)

    # Strategy C: elink — one PMID at a time so results map correctly
    # (idconv misses some old papers that ARE in PMC; batching elink
    #  conflates links from multiple input IDs)
    missing_pmc = [p for p in numeric if not result.get(p, {}).get("pmcid")]
    if missing_pmc:
        elink_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
        for pmid in missing_pmc:
            try:
                r = ncbi_session.get(
                    elink_url,
                    params={"dbfrom": "pubmed", "db": "pmc", "id": pmid,
                            "retmode": "json", "linkname": "pubmed_pmc",
                            "tool": "BiomodelsDownloader", "email": EMAIL},
                    timeout=30,
                )
                r.raise_for_status()
                if debug:
                    print(f"  [debug] elink({pmid}) status={r.status_code}, body={r.text[:300]}")
                data = r.json(strict=False)
                for linkset in data.get("linksets", []):
                    for lsdb in linkset.get("linksetdbs", []):
                        if (lsdb.get("dbto") == "pmc" and
                                lsdb.get("linkname") == "pubmed_pmc"):
                            pmc_ids = lsdb.get("links", [])
                            if pmc_ids:
                                pmcid = f"PMC{pmc_ids[0]}"
                                if pmid not in result:
                                    result[pmid] = {"pmcid": None, "doi": None}
                                result[pmid]["pmcid"] = pmcid
                                break
            except Exception as e:
                print(f"  ⚠  elink error ({pmid}): {e}")
            time.sleep(NCBI_DELAY)

    return result

# ── Download strategies ───────────────────────────────────────────────────────
def try_pubmed_page(pmid, path):
    """
    Scrape the PubMed page for a PMID and try any free-access PDF links it lists
    (PMC articles, author manuscripts, preprints, etc.).
    Returns (pdf_url, pmcid_found) or (None, None).
    """
    if not re.match(r'^\d+$', pmid):
        return None, None
    try:
        r = ncbi_session.get(f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                             timeout=30, allow_redirects=True)
        if r.status_code != 200:
            return None, None
        html = r.text

        # Look for a PMC ID mentioned on the page
        pmcid = None
        m = re.search(r'PMC(\d+)', html)
        if m:
            pmcid = f"PMC{m.group(1)}"

        # Collect free-text/full-text links PubMed lists
        free_urls = re.findall(
            r'href=["\']'
            r'(https?://(?:www\.ncbi\.nlm\.nih\.gov/pmc/articles/PMC\d+|'
            r'europepmc\.org/articles/PMC\d+|'
            r'www\.biorxiv\.org/content/[^\s"\']+|'
            r'arxiv\.org/[^\s"\']+)[^"\']*)["\']',
            html
        )
        for url in free_urls:
            try:
                r2 = session.get(url, timeout=60, allow_redirects=True)
                if r2.status_code == 200 and is_pdf(r2.content):
                    path.write_bytes(r2.content)
                    return url, pmcid
            except Exception:
                pass

        return None, pmcid   # may have found a pmcid even if no direct PDF link

    except Exception:
        return None, None


def _is_recaptcha_wall(response):
    """Return True if the response is a Google reCAPTCHA bot-check page."""
    if response.status_code != 200:
        return False
    ct = response.headers.get("content-type", "")
    if "text/html" not in ct:
        return False
    # Fast check: reCAPTCHA challenge pages always have this substring
    return b"recaptcha/challengepage" in response.content[:4096]


def _is_ezproxy_login_wall(response):
    """
    Return True if EZProxy redirected us to a login page rather than granting
    access.  This happens when VPN is off (IP not recognized) or when the
    EZProxy session has expired and needs a fresh Shibboleth login.
    Also handles the _LoginWallSentinel returned by _get_via_ezproxy.
    """
    url = getattr(response, "url", "") or ""
    # Sentinel from _get_via_ezproxy or URL-based detection
    if any(s in url for s in _EZPROXY_LOGIN_URLS):
        return True
    ct = ""
    try:
        ct = response.headers.get("content-type", "") if response.headers else ""
    except Exception:
        pass
    if "text/html" not in ct:
        return False
    # EZProxy itself says "login required" in the HTML body
    snippet = response.content[:4096].lower()
    return (b"login required" in snippet or b"ezproxy" in snippet) and b"offcampus" in snippet


def _poll_pmc_pdf(url, path, sess, vlog, max_retries=8, poll_delay=5):
    """
    Fetch a PMC PDF URL, retrying if it returns a 'preparing PDF' HTML page.
    Stops immediately if a reCAPTCHA wall is detected (no point retrying).
    Returns the URL on success, None on failure.
    """
    for attempt in range(max_retries):
        try:
            r = sess.get(url, timeout=60, allow_redirects=True)
            ct = r.headers.get("content-type", "")
            vlog(f"poll[{attempt+1}/{max_retries}] {url} "
                 f"status={r.status_code} ct={ct} "
                 f"size={len(r.content)} is_pdf={is_pdf(r.content)}")
            if r.status_code == 200 and is_pdf(r.content):
                path.write_bytes(r.content)
                return url
            if _is_recaptcha_wall(r):
                vlog("reCAPTCHA wall detected")
                return "captcha"
            if r.status_code not in (200, 202):
                break   # hard error, no point retrying
            # Still HTML (preparing PDF?) — wait and retry
            if attempt < max_retries - 1:
                time.sleep(poll_delay)
        except Exception as e:
            vlog(f"poll error: {e}")
            break
    return None


def try_pmc(pmcid, path, verbose=False):
    """
    Try to download from PubMed Central using several approaches:
    1. PMC OA API  → gives the actual PDF filename (most reliable for OA papers)
    2. Scrape the PMC article page for the PDF link
    3. Generic /pdf/ URL (follows redirects)
    4. Europe PMC direct render
    """
    pmc_full = pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
    pmc_num  = pmc_full[3:]  # numeric part, e.g. "52661"
    PMC_BASE = "https://pmc.ncbi.nlm.nih.gov"  # current PMC domain

    def vlog(msg):
        if verbose:
            print(f"\n      [pmc] {msg}")

    def _pmc_pdf_candidates_from_html(html, page_url):
        """
        Extract PMC PDF URLs from article page HTML.
        Tries general href patterns first, then searches specifically
        for /articles/PMCXXXX/pdf/*.pdf paths anywhere in the page
        (covers Next.js __NEXT_DATA__ JSON and SSR-rendered links).
        """
        candidates = find_pdf_urls_in_html(html, page_url)
        # Also scan broadly for any /articles/PMCxxx/pdf/…pdf path
        extra = re.findall(
            rf'(/articles/{re.escape(pmc_full)}/pdf/[^\s"\'<>\\]+\.pdf)',
            html, re.IGNORECASE,
        )
        for p in extra:
            full = PMC_BASE + p
            if full not in candidates:
                candidates.append(full)
        return candidates

    # 0. NCBI efetch (direct PDF via E-utilities) -----------------------------
    try:
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        r = ncbi_session.get(url,
                             params={"db": "pmc", "id": pmc_num, "rettype": "pdf",
                                     "tool": "BiomodelsDownloader", "email": EMAIL},
                             timeout=60)
        vlog(f"efetch status={r.status_code} ct={r.headers.get('content-type','')} size={len(r.content)} is_pdf={is_pdf(r.content)}")
        if r.status_code == 200 and is_pdf(r.content):
            path.write_bytes(r.content)
            return url
    except Exception as e:
        vlog(f"efetch error: {e}")
    time.sleep(NCBI_DELAY)

    # 1. PMC OA API -----------------------------------------------------------
    try:
        r = ncbi_session.get(
            "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi",
            params={"id": pmc_full}, timeout=30,
        )
        vlog(f"OA API status={r.status_code} body={r.text[:400]}")
        if r.status_code == 200:
            links = re.findall(r'href="([^"]+)"', r.text)
            pdf_links = [l for l in links if l.endswith(".pdf")]
            tgz_links = [l for l in links if l.endswith(".tgz") or l.endswith(".tar.gz")]
            vlog(f"OA API pdf links: {pdf_links}  tgz links: {tgz_links}")
            for raw in pdf_links:
                url = raw.replace("ftp://ftp.ncbi.nlm.nih.gov",
                                  "https://ftp.ncbi.nlm.nih.gov")
                try:
                    r2 = session.get(url, timeout=60, allow_redirects=True)
                    vlog(f"OA pdf fetch status={r2.status_code} size={len(r2.content)} is_pdf={is_pdf(r2.content)}")
                    if r2.status_code == 200 and is_pdf(r2.content):
                        path.write_bytes(r2.content)
                        return url
                except Exception as e:
                    vlog(f"OA pdf fetch error: {e}")
            # No direct PDF link — try extracting PDF from the OA tgz package.
            # Most OA full-text packages contain exactly one PDF.
            for raw in tgz_links:
                url = raw.replace("ftp://ftp.ncbi.nlm.nih.gov",
                                  "https://ftp.ncbi.nlm.nih.gov")
                try:
                    import io, tarfile
                    vlog(f"OA tgz fetch: {url}")
                    r2 = session.get(url, timeout=120, allow_redirects=True)
                    vlog(f"OA tgz status={r2.status_code} size={len(r2.content)}")
                    if r2.status_code != 200:
                        continue
                    with tarfile.open(fileobj=io.BytesIO(r2.content)) as tar:
                        pdf_members = [m for m in tar.getmembers()
                                       if m.name.lower().endswith(".pdf")]
                        vlog(f"OA tgz contains {len(pdf_members)} PDF(s): "
                             f"{[m.name for m in pdf_members]}")
                        for member in pdf_members:
                            f = tar.extractfile(member)
                            if f:
                                data = f.read()
                                if is_pdf(data):
                                    path.write_bytes(data)
                                    return url
                except Exception as e:
                    vlog(f"OA tgz error: {e}")
    except Exception as e:
        vlog(f"OA API error: {e}")
    time.sleep(NCBI_DELAY)

    # 2 + 3. Article page scrape and /pdf/ polling (with one CAPTCHA-refresh retry)
    # pmc.ncbi.nlm.nih.gov uses reCAPTCHA bot-detection.  We use a browser-UA
    # session + cookies.txt to pass it.  If we get a CAPTCHA wall we prompt the
    # user to refresh cookies and try once more.
    pmc_session = make_cookie_session("pmc.ncbi.nlm.nih.gov")
    _captcha = False

    # 2. Scrape article page ----------------------------------------------
    article_html = ""
    try:
        page_url = f"{PMC_BASE}/articles/{pmc_full}/"
        r = pmc_session.get(page_url, timeout=30, allow_redirects=True)
        vlog(f"Article page status={r.status_code} size={len(r.content)}")
        if _is_recaptcha_wall(r):
            vlog("reCAPTCHA wall on article page")
            _captcha = True
        elif r.status_code == 200:
            article_html = r.text
            candidates = _pmc_pdf_candidates_from_html(article_html, page_url)
            vlog(f"Article page PDF candidates: {candidates[:5]}")
            for url in candidates[:8]:
                result = _poll_pmc_pdf(url, path, pmc_session, vlog,
                                       max_retries=6, poll_delay=5)
                if result == "captcha":
                    _captcha = True
                    break
                if result:
                    return result
    except Exception as e:
        vlog(f"Article page error: {e}")
    time.sleep(NCBI_DELAY)

    # 3. Poll /pdf/ -------------------------------------------------------
    # Always try the bare /pdf/ URL even if the article page had a CAPTCHA wall —
    # PMC only gates the HTML article page, not the PDF binary itself.
    try:
        specific_urls = []
        if article_html:
            extra = re.findall(
                rf'(/articles/{re.escape(pmc_full)}/pdf/[^\s"\'<>\\]+\.pdf)',
                article_html, re.IGNORECASE,
            )
            specific_urls = [PMC_BASE + p for p in extra]

        bare_pdf_url = f"{PMC_BASE}/articles/{pmc_full}/pdf/"
        for pdf_url in (specific_urls or []) + [bare_pdf_url]:
            vlog(f"Polling PDF URL: {pdf_url}")
            result = _poll_pmc_pdf(pdf_url, path, pmc_session, vlog,
                                   max_retries=8, poll_delay=5)
            if result == "captcha":
                _captcha = True
                break
            if result:
                return result
    except Exception as e:
        vlog(f"/pdf/ poll error: {e}")
    time.sleep(NCBI_DELAY)

    # 4. Europe PMC -----------------------------------------------------------
    # Use a session that does NOT retry on 500 — EuropePMC returns 500 for
    # "PDF not available here", which is permanent, not transient.  Retrying
    # wastes ~15 s per URL and can swallow the redirect chain we need to log.
    eu_session = _make_session(HEADERS)
    eu_session.mount("https://", HTTPAdapter(max_retries=_retry_no500))
    eu_session.mount("http://",  HTTPAdapter(max_retries=_retry_no500))

    for eu_url in [
        f"https://europepmc.org/api/getPdf?pmcid={pmc_full}",
        f"https://europepmc.org/articles/{pmc_full}?pdf=render",
        # Legacy render endpoint — still serves some non-OA PMC articles
        f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmc_full}&blobtype=pdf",
    ]:
        try:
            r = eu_session.get(eu_url, timeout=60, allow_redirects=True,
                               headers={"Referer": "https://europepmc.org/",
                                        "Accept": "application/pdf,*/*"})
            vlog(f"EuropePMC {eu_url.split('?')[0].split('/')[-1]} "
                 f"status={r.status_code} final_url={r.url} "
                 f"ct={r.headers.get('content-type','')} "
                 f"size={len(r.content)} is_pdf={is_pdf(r.content)}")
            if r.status_code == 200 and is_pdf(r.content):
                path.write_bytes(r.content)
                return eu_url
        except Exception as e:
            vlog(f"EuropePMC error ({eu_url.split('/')[-1]}): {e}")

    if _captcha:
        _prompt_cookies_refresh(
            "PubMed Central (pmc.ncbi.nlm.nih.gov)",
            f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_full}/",
            'Use Cookie-Editor to export ONLY the cookies for this page:\n'
            '       1. Visit the URL above and wait for it to fully load.\n'
            '       2. Open Cookie-Editor, click Export → Export as Netscape.\n'
            '       3. APPEND (do not replace) those lines to cookies.txt.\n'
            '       Tip: exporting ALL cookies from the browser can interfere —\n'
            '       export only the cookies for pmc.ncbi.nlm.nih.gov.',
        )

    return None

def _follow_to_pdf(url, label, path, verbose=False, sess=None):
    """
    Follow a URL (with redirects) and try to save a PDF.
    If it lands on the EBSCO viewer, use the EBSCO download API.
    Uses a single session so cookies set during the redirect chain persist
    for the subsequent EBSCO API call.
    Returns the source URL string on success, None on failure.
    """
    if sess is None:
        sess = make_cookie_session("")   # load cookies.txt; accumulates redirect cookies

    def vlog(msg):
        if verbose:
            print(f"\n      [libkey] {msg}")

    try:
        # For libkey.io full-text-file URLs, first peek at the initial redirect
        # without following it — the server may redirect directly to the PDF host,
        # bypassing the Ember.js SPA entirely.
        _libkey_ftf = "libkey.io" in url and "full-text-file" in url
        if _libkey_ftf:
            r0 = sess.get(url, timeout=30, allow_redirects=False,
                          headers={"Referer": "https://libkey.io/",
                                   "Accept": "text/html,application/xhtml+xml,*/*"})
            vlog(f"{label} (no-redir) → {r0.url}  status={r0.status_code}  "
                 f"Location={r0.headers.get('Location','')}")
            loc = r0.headers.get("Location", "")
            if loc and r0.status_code in (301, 302, 303, 307, 308):
                # Follow the redirect chain manually
                from urllib.parse import urljoin
                loc = urljoin(url, loc)
                vlog(f"{label} → redirect to {loc}")
                r = sess.get(loc, timeout=60, allow_redirects=True,
                             headers={"Referer": url})
                vlog(f"{label} → {r.url}  status={r.status_code}  "
                     f"ct={r.headers.get('content-type','')}  "
                     f"size={len(r.content)}  is_pdf={is_pdf(r.content)}")
                if r.status_code == 200 and is_pdf(r.content):
                    path.write_bytes(r.content)
                    return url
            else:
                # No server-side redirect; fall through with the SPA HTML response
                r = r0
        else:
            r = sess.get(url, timeout=60, allow_redirects=True,
                         headers={"Referer": "https://libkey.io/"})
        vlog(f"{label} → {r.url}  status={r.status_code}  "
             f"ct={r.headers.get('content-type','')}  "
             f"size={len(r.content)}  is_pdf={is_pdf(r.content)}")

        if r.status_code == 200 and is_pdf(r.content):
            path.write_bytes(r.content)
            return url

        # Landed on a PMC article page (SPA shell) — try EuropePMC render
        if r.status_code == 200 and re.search(r'pmc.*?/articles/PMC\d+', r.url, re.I):
            pm = re.search(r'PMC(\d+)', r.url)
            if pm:
                pmc_id = f"PMC{pm.group(1)}"
                eu_url = f"https://europepmc.org/api/getPdf?pmcid={pmc_id}"
                vlog(f"PMC redirect → {pmc_id}, trying EuropePMC /api/getPdf")
                try:
                    r_eu = sess.get(eu_url, timeout=60, allow_redirects=True,
                                    headers={"Referer": "https://europepmc.org/",
                                             "Accept": "application/pdf,*/*"})
                    vlog(f"EuropePMC status={r_eu.status_code} ct={r_eu.headers.get('content-type','')} is_pdf={is_pdf(r_eu.content)}")
                    if r_eu.status_code == 200 and is_pdf(r_eu.content):
                        path.write_bytes(r_eu.content)
                        return eu_url
                except Exception as e_eu:
                    vlog(f"EuropePMC error: {e_eu}")

        # Landed on EBSCO — extract opid + content_id and try v2-pdf
        if r.status_code == 200 and "research.ebsco.com" in r.url:
            # Case 1: already on viewer URL → extract directly
            cid, opid = None, None
            m = re.search(r"research\.ebsco\.com/c/([^/]+)/viewer/pdf/([^?/]+)", r.url)
            if m:
                opid, cid = m.group(1), m.group(2)

            # Case 2: search/openurl result page — content_id may be buried in HTML
            # or require a JSON API call (React SPA, so initial HTML may lack it).
            if not cid and "text/html" in r.headers.get("content-type", ""):
                html = r.text
                om = re.search(r'research\.ebsco\.com/c/([^/?]+)', r.url)
                if om:
                    opid = om.group(1)

                # 2a. viewer/pdf link anywhere in the HTML
                vm = re.search(r'/viewer/pdf/([a-z0-9]{8,20})', html)
                if vm:
                    cid = vm.group(1)
                    vlog(f"found cid in viewer/pdf link: {cid!r}")

                # 2b. sourceRecordId in a linkprocessor URL embedded in the page
                if not cid:
                    sm = re.search(r'sourceRecordId=([a-z0-9]{8,20})', html)
                    if sm:
                        cid = sm.group(1)
                        vlog(f"found cid in sourceRecordId param: {cid!r}")

                # 2c. Re-fetch the same URL as a JSON API request — EBSCO may
                #     return structured data when XMLHttpRequest headers are sent.
                if not cid:
                    try:
                        rj = sess.get(
                            r.url, timeout=30, allow_redirects=True,
                            headers={
                                "Accept": "application/json, text/plain, */*",
                                "X-Requested-With": "XMLHttpRequest",
                                "Referer": r.url,
                            },
                        )
                        ct_j = rj.headers.get("content-type", "")
                        vlog(f"EBSCO JSON fetch: status={rj.status_code} ct={ct_j} size={len(rj.content)}")
                        if "json" in ct_j:
                            vm2 = re.search(r'/viewer/pdf/([a-z0-9]{8,20})', rj.text)
                            if vm2:
                                cid = vm2.group(1)
                            if not cid:
                                sm2 = re.search(
                                    r'"(?:sourceRecordId|contentId)"\s*:\s*"([a-z0-9]{8,20})"',
                                    rj.text,
                                )
                                if sm2:
                                    cid = sm2.group(1)
                            if cid:
                                vlog(f"found cid in JSON API response: {cid!r}")
                            else:
                                vlog(f"JSON API snippet: {rj.text[:400]}")
                    except Exception as ej:
                        vlog(f"EBSCO JSON fetch error: {ej}")

                vlog(f"EBSCO search page: extracted cid={cid!r} opid={opid!r}")

            if cid and opid:
                for intent in ("view", "download"):
                    ebsco_url = (
                        f"https://research.ebsco.com/linkprocessor/v2-pdf"
                        f"?sourceRecordId={cid}&recordId={cid}"
                        f"&profileIdentifier={opid}&intent={intent}"
                        f"&type=pdfLink&lang=en-US"
                    )
                    r2 = sess.get(ebsco_url, timeout=60, allow_redirects=True,
                                  headers={"Referer": r.url})
                    vlog(f"EBSCO linkprocessor ({intent}) → status={r2.status_code}  "
                         f"ct={r2.headers.get('content-type','')}  "
                         f"size={len(r2.content)}  is_pdf={is_pdf(r2.content)}")
                    if r2.status_code == 200 and is_pdf(r2.content):
                        path.write_bytes(r2.content)
                        return ebsco_url

        # Landed on a LibKey HTML page — the SPA shell needs JS to resolve the
        # final URL.  First try fetching the same URL with Accept: application/json
        # (LibKey may return the resolved URL as JSON for API consumers).
        if (r.status_code == 200
                and "libkey.io" in r.url
                and "text/html" in r.headers.get("content-type", "")):
            try:
                rj = sess.get(r.url, timeout=30, allow_redirects=True,
                              headers={"Accept": "application/json"})
                ct_j = rj.headers.get("content-type", "")
                vlog(f"LibKey JSON probe → {rj.url}  status={rj.status_code}  ct={ct_j}  size={len(rj.content)}")
                if rj.status_code == 200 and is_pdf(rj.content):
                    path.write_bytes(rj.content)
                    return r.url
                if "json" in ct_j:
                    vlog(f"LibKey JSON body: {rj.text[:800]}")
                    # Look for a URL in the JSON that points to the PDF
                    pdf_url = None
                    for key in ("fullTextFile", "pdfUrl", "url", "contentUrl",
                                "downloadUrl", "pdf", "link"):
                        m_j = re.search(
                            rf'"{key}"\s*:\s*"([^"]+)"', rj.text
                        )
                        if m_j:
                            pdf_url = m_j.group(1).replace("\\u002F", "/").replace("\\/", "/")
                            vlog(f"LibKey JSON field {key!r} → {pdf_url}")
                            break
                    if not pdf_url:
                        # Grab any https URL from the JSON
                        m_j = re.search(r'"(https://[^"]+\.pdf[^"]*)"', rj.text)
                        if m_j:
                            pdf_url = m_j.group(1).replace("\\/", "/")
                    if pdf_url:
                        result = _follow_to_pdf(pdf_url, f"{label}→libkey-json",
                                                path, verbose, sess)
                        if result:
                            return result
                else:
                    # LibKey SPA: JS-only app, can't extract PDF URL without a browser.
                    # The browser would fetch a session token at runtime and call the
                    # ThirdIron library API — no static token available to replicate this.
                    vlog(f"LibKey returned Ember.js SPA — requires browser execution to resolve")
            except Exception as e_j:
                vlog(f"LibKey JSON probe error: {e_j}")

        # Landed on a Primo/ExLibris OpenURL resolver page.
        # Primo is an Angular SPA — the shell HTML has no publisher links.
        # We can't resolve it without a browser, so bail early.
        if (r.status_code == 200
                and "primo.exlibrisgroup.com" in r.url
                and "text/html" in r.headers.get("content-type", "")):
            vlog(f"Primo/ExLibris SPA ({len(r.content)} bytes) — requires JS, skipping")

        if (r.status_code == 200
                and "libkey.io" in r.url
                and "text/html" in r.headers.get("content-type", "")):
            html = r.text
            # Try meta-refresh, JS location redirect, and any href/src links
            redirect_url = None
            m = re.search(
                r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+'
                r'content=["\'][^"\']*url=([^"\'>\s]+)',
                html, re.I,
            )
            if m:
                redirect_url = m.group(1)
            if not redirect_url:
                m = re.search(
                    r'(?:location\.(?:href|replace)\s*[=(]|window\.location\s*=)\s*'
                    r'["\']([^"\']+)["\']',
                    html,
                )
                if m:
                    redirect_url = m.group(1)
            if not redirect_url:
                # Look for a prominent href that points to actual content
                _skip_domains = (
                    "libkey.io", "thirdiron.com", "support.", "help.",
                    "google.com", "twitter.com", "facebook.com",
                )
                for m in re.finditer(r'href=["\']([^"\']+)["\']', html):
                    href = m.group(1)
                    if (href.startswith("http")
                            and not any(d in href for d in _skip_domains)):
                        redirect_url = href
                        break
            if redirect_url:
                vlog(f"LibKey redirect → {redirect_url}")
                result = _follow_to_pdf(redirect_url, f"{label}→libkey-redir",
                                        path, verbose, sess)
                if result:
                    return result

        # Publisher returned 403 — first check whether the 403 body itself
        # says UW doesn't subscribe (saves an EZProxy round-trip, and works
        # even when the EZProxy cookie is expired).
        _deny_phrases = [
            "does not subscribe to this content",
            "your institution does not have access",
            "institution does not have access",
            "not available through your institution",
            "institutional access is required",
            "purchase this article",
        ]
        if r.status_code == 403 and r.content:
            _body_lower = r.content[:32768].decode("utf-8", errors="replace").lower()
            if any(p in _body_lower for p in _deny_phrases):
                vlog(f"403 body indicates no institutional subscription: {r.url[:80]}")
                return _NO_SUBSCRIPTION

        # Publisher returned 403 — try routing through UW EZProxy.
        # Use _get_via_ezproxy (not allow_redirects=True) so that publisher
        # redirect responses (e.g. OUP article-lookup → article page) stay
        # within the EZProxy domain instead of escaping to the canonical URL.
        if r.status_code == 403 and r.url.startswith("http"):
            ez = ezproxy_url(r.url)
            if ez != r.url:
                vlog(f"403 from publisher, trying EZProxy: {ez}")
                try:
                    # Use a session with EZProxy cookies — the caller's session may
                    # only have ThirdIron/libkey cookies, not the offcampus session.
                    ez_sess = make_cookie_session("offcampus.lib.washington.edu")
                    r_ez = _get_via_ezproxy(r.url, ez_sess, timeout=60, vlog=vlog)
                    if r_ez is None:
                        pass
                    else:
                        ez_final = getattr(r_ez, "url", "")
                        vlog(f"EZProxy → status={r_ez.status_code}  "
                             f"ct={r_ez.headers.get('content-type','') if r_ez.headers else ''}  "
                             f"final_url={ez_final}  "
                             f"is_pdf={is_pdf(r_ez.content)}")
                        if _is_ezproxy_login_wall(r_ez):
                            vlog("EZProxy login wall — visit https://offcampus.lib.washington.edu to authenticate")
                        elif r_ez.status_code == 200 and is_pdf(r_ez.content):
                            path.write_bytes(r_ez.content)
                            return ez_final
                        elif r_ez.status_code == 200:
                            # Apply publisher PDF pattern to the *resolved* URL
                            # (ez_final is the actual article page, e.g. /article/55/2/383/...
                            #  rather than the original /article-lookup/doi/... URL)
                            for domain, pdf_fn in PUBLISHER_PDF_PATTERNS.items():
                                if domain in r.url or domain in ez_final:
                                    try:
                                        base = ez_final if domain in ez_final else r.url
                                        ez_pdf_url = ezproxy_url(pdf_fn(base, ""))
                                        r_pdf = ez_sess.get(
                                            ez_pdf_url, timeout=60, allow_redirects=True,
                                            headers={"Accept": "application/pdf,*/*",
                                                     "Referer": ez_final,
                                                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"})
                                        ct_pdf = r_pdf.headers.get("content-type", "")
                                        vlog(f"EZProxy PDF fetch → status={r_pdf.status_code}  ct={ct_pdf}  url={r_pdf.url[:80]}")
                                        if not is_pdf(r_pdf.content):
                                            snippet = r_pdf.content[:200].decode("utf-8", errors="replace").replace("\n", " ")
                                            vlog(f"  (not PDF) body[:200]: {snippet}")
                                        if r_pdf.status_code == 200 and is_pdf(r_pdf.content):
                                            path.write_bytes(r_pdf.content)
                                            return r_pdf.url
                                    except Exception:
                                        pass
                                    break
                            for pdf_url in find_pdf_urls_in_html(r_ez.text, ez_final)[:6]:
                                # Wrap any publisher-domain URLs back through EZProxy
                                ez_pdf_url2 = ezproxy_url(pdf_url)
                                vlog(f"HTML-scan PDF candidate: {ez_pdf_url2[:80]}")
                                r2 = ez_sess.get(ez_pdf_url2, timeout=60, allow_redirects=True,
                                                 headers={"Accept": "application/pdf,*/*",
                                                          "Referer": ez_final})
                                if r2.status_code == 200 and is_pdf(r2.content):
                                    path.write_bytes(r2.content)
                                    return r2.url
                except Exception as e_ez:
                    vlog(f"EZProxy error: {e_ez}")

    except Exception as e:
        vlog(f"{label} error: {e}")
    return None


def try_ebsco(pmid_numeric, doi, path, verbose=False):
    """
    Try EBSCO Research full-text search API (UW Library).
    Requires SESSION_ID + SESSION_MAP + EBSCO_AFFILIATION cookies in cookies.txt.
    Refresh cookies.txt when SESSION_ID expires (~28 h) via Cookie-Editor on
    research.ebsco.com.

    Queries by DOI first, then PMID; fetches PDF via the v2-pdf linkprocessor.
    """
    if not pmid_numeric and not doi:
        return None

    def vlog(msg):
        if verbose:
            print(f"\n      [ebsco] {msg}")

    EBSCO_OPID = "2onyl7"

    # Real API URLs captured from Chrome DevTools while searching EBSCO Research:
    #   POST /api/search/v1/search?applyAllLimiters=true&...  → returns results
    #         with recordId (= content_id) for each hit
    #   GET  /api/search/v2/details?recordId={cid}&profileIdentifier=2onyl7&...
    # We POST a DOI/PMID query, extract the first recordId, then call v2-pdf.
    EBSCO_SEARCH_URL = (
        "https://research.ebsco.com/api/search/v1/search"
        "?applyAllLimiters=true&includeSavedItems=false"
        "&excludeLinkValidation=true&includeHbrRestrictedLinks=true"
    )
    EBSCO_SEARCH_HEADERS = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://research.ebsco.com",
        "Referer": f"https://research.ebsco.com/c/{EBSCO_OPID}/",
    }

    search_bodies = []
    if doi:
        search_bodies += [
            {"query": f"doi:{doi}",    "profileIdentifier": EBSCO_OPID},
            {"query": f"DX {doi}",     "profileIdentifier": EBSCO_OPID},
        ]
    if pmid_numeric:
        search_bodies.append(
            {"query": f"pmid:{pmid_numeric}", "profileIdentifier": EBSCO_OPID}
        )

    # The key cookies (SESSION_ID, SESSION_MAP, EBSCO_AFFILIATION) last ~28 h;
    # refresh them via Cookie-Editor on research.ebsco.com when they expire.
    ebsco_sess = make_cookie_session("research.ebsco.com")
    _ebsco_auth_failures = 0

    for body in search_bodies:
        vlog(f"EBSCO search query={body['query']!r}")
        try:
            rs = ebsco_sess.post(
                EBSCO_SEARCH_URL, json=body, timeout=20,
                headers=EBSCO_SEARCH_HEADERS,
            )
            ct_s = rs.headers.get("content-type", "")
            vlog(f"EBSCO search status={rs.status_code} ct={ct_s} size={len(rs.content)}")
            if rs.status_code == 200 and "json" in ct_s:
                cid = None
                try:
                    jdata = rs.json()
                    items = jdata.get("search", {}).get("items", [])
                    vlog(f"EBSCO search items={len(items)}")
                    if verbose and items:
                        print(f"      [ebsco] EBSCO first item keys: {list(items[0].keys())}")
                        print(f"      [ebsco] EBSCO first item snippet: {json.dumps(items[0])[:600]}")
                    cids = []
                    for item in items[:5]:
                        for fld in ("id", "recordId", "sourceRecordId",
                                    "resultId", "itemId"):
                            val = item.get(fld, "")
                            if val and re.match(r'^[a-z0-9]{8,20}$', str(val)):
                                cids.append(str(val))
                                break
                except Exception as je:
                    vlog(f"EBSCO search JSON parse error: {je}")
                    cids = []
                    for pat in (r'"id"\s*:\s*"([a-z0-9]{8,20})"',
                                r'"recordId"\s*:\s*"([a-z0-9]{8,20})"'):
                        for m in re.finditer(pat, rs.text):
                            cids.append(m.group(1))

                # Collect full-text links from the result for fallback.
                # EBSCO's links dict has many buckets; only fullText/customLink
                # entries are useful — skip export/drive/email/bib/print links.
                _SKIP_LINK_TYPES = {
                    "oneDriveUpload", "driveUpload", "driveUploadStatus",
                    "csv", "email", "easybib", "refworks", "endnote",
                    "noodletools", "ris", "cover", "thumb",
                }
                _FULLTEXT_BUCKETS = {
                    "fullTextLinks", "v2-fullTextAndCustomLinks",
                    "fullTextAndCustomLinks", "cardCallToActionLinks",
                    "plinks", "providerLinks",
                }
                item_links = []
                for item in items[:5]:
                    raw_links = item.get("links") or {}
                    if isinstance(raw_links, dict):
                        # Prefer fulltext-specific buckets; fall back to all buckets
                        buckets = [(k, v) for k, v in raw_links.items()
                                   if k in _FULLTEXT_BUCKETS and isinstance(v, list)]
                        if not buckets:
                            buckets = [(k, v) for k, v in raw_links.items()
                                       if isinstance(v, list)]
                        for _bk, entries in buckets:
                            for lnk in entries:
                                if isinstance(lnk, str):
                                    href = lnk
                                    ltype = ""
                                elif isinstance(lnk, dict):
                                    if lnk.get("type") in _SKIP_LINK_TYPES:
                                        continue
                                    href = lnk.get("url") or lnk.get("href") or lnk.get("link") or ""
                                    ltype = lnk.get("type", "")
                                else:
                                    continue
                                if href and not href.startswith("http"):
                                    href = "https://research.ebsco.com" + href
                                if href and href.startswith("http") and href not in item_links:
                                    item_links.append(href)
                if verbose:
                    print(f"      [ebsco] item links extracted: {item_links[:8]}")

                vlog(f"EBSCO search cids={cids}")
                v2pdf_404 = False
                for cid in cids:
                    for intent in ("view", "download"):
                        eu = (
                            f"https://research.ebsco.com/linkprocessor/v2-pdf"
                            f"?sourceRecordId={cid}&recordId={cid}"
                            f"&profileIdentifier={EBSCO_OPID}&intent={intent}"
                            f"&type=pdfLink&lang=en-US"
                        )
                        r2 = ebsco_sess.get(
                            eu, timeout=60, allow_redirects=True,
                            headers={"Referer": f"https://research.ebsco.com/c/{EBSCO_OPID}/viewer/pdf/{cid}"},
                        )
                        vlog(f"v2-pdf cid={cid} ({intent}) status={r2.status_code} ct={r2.headers.get('content-type','')} is_pdf={is_pdf(r2.content)}")
                        if r2.status_code == 200 and is_pdf(r2.content):
                            try:
                                path.write_bytes(r2.content)
                                return eu
                            except Exception as e_w:
                                vlog(f"write error: {e_w}")
                                return None
                        if r2.status_code == 404:
                            v2pdf_404 = True

                # v2-pdf unavailable — try the details API for a link-resolver URL
                if v2pdf_404 and cids:
                    for cid in cids:
                        try:
                            first_item = next((i for i in items if i.get("id") == cid), items[0] if items else None)
                            db = (first_item or {}).get("shortDbName") or (first_item or {}).get("shortDBName") or ""
                            det_url = (
                                f"https://research.ebsco.com/api/search/v2/details"
                                f"?recordId={cid}&profileIdentifier={EBSCO_OPID}"
                                + (f"&db={db}" if db else "")
                            )
                            rd = ebsco_sess.get(det_url, timeout=20,
                                                headers={"Accept": "application/json",
                                                         "Referer": f"https://research.ebsco.com/c/{EBSCO_OPID}/"})
                            vlog(f"details API status={rd.status_code} ct={rd.headers.get('content-type','')} size={len(rd.content)}")
                            if rd.status_code == 200 and "json" in rd.headers.get("content-type", ""):
                                if verbose:
                                    print(f"      [ebsco] details snippet: {rd.text[:800]}")
                                # Hunt for any fulltext/link-resolver URL in the details JSON
                                for pat in (r'"(?:fullText|fullTextUrl|linkResolverUrl|pdfUrl|bestFullTextUrl|url)"\s*:\s*"([^"]+)"',
                                            r'https?://[^\s"\'<>]+\.pdf[^\s"\'<>]*'):
                                    for m in re.finditer(pat, rd.text):
                                        candidate = m.group(1) if m.lastindex else m.group(0)
                                        candidate = candidate.replace("\\u002F", "/").replace("\\/", "/")
                                        if candidate.startswith("http"):
                                            vlog(f"details link candidate: {candidate}")
                                            result = _follow_to_pdf(candidate, "ebsco-details-link", path, verbose, sess=ebsco_sess)
                                            if result and result is not _NO_SUBSCRIPTION:
                                                return result
                        except Exception as e_det:
                            vlog(f"details API error: {e_det}")

                # v2-pdf unavailable — try publisher links embedded in the result
                if v2pdf_404 and item_links:
                    vlog(f"v2-pdf returned 404; trying {len(item_links)} item link(s)")
                    for href in item_links[:8]:
                        vlog(f"trying item link: {href}")
                        result = _follow_to_pdf(href, "ebsco-item-link", path, verbose, sess=ebsco_sess)
                        if result and result is not _NO_SUBSCRIPTION:
                            return result

                if cids:
                    break  # tried all cids from this query; move to next body
            else:
                vlog(f"EBSCO search non-JSON (status={rs.status_code}): {rs.text[:200]}")
                _ebsco_auth_failures += 1
        except Exception as e:
            vlog(f"EBSCO search error: {e}")
            _ebsco_auth_failures += 1

    if search_bodies and _ebsco_auth_failures == len(search_bodies):
        _prompt_cookies_refresh(
            "EBSCO Research (UW Library)",
            f"https://research.ebsco.com/c/{EBSCO_OPID}/",
            "Use Cookie-Editor to export these cookies and append them\n"
            "       to cookies.txt (overwrite any existing lines for the same names):\n"
            "         SESSION_ID, SESSION_MAP, SESSION_EXPIRATION, EBSCO_AFFILIATION\n"
            "       (In Cookie-Editor: open the extension on that page, find each\n"
            "       cookie, click Export, choose Netscape format, append to cookies.txt.)",
        )

    return None


def try_libkey(pmid_numeric, doi, path, verbose=False):
    """
    Try UW Library's LibKey / ThirdIron link resolver.
    Requires UW VPN in full-tunnel mode.

    Queries the ThirdIron public and authenticated v2 APIs by PMID and DOI,
    then follows the returned fullTextFile / contentLocation URL through auth
    redirects, handling the EBSCO viewer if needed.
    """
    if not pmid_numeric and not doi:
        return None

    def vlog(msg):
        if verbose:
            print(f"\n      [libkey] {msg}")

    # The actual API LibKey uses in the browser is /v2/ (not /public/v1/).
    # Auth is via session cookies from libkey.io / thirdiron.com — no API key
    # in the URL.  Re-export cookies.txt from Chrome after visiting
    # https://libkey.io/libraries/3478/<any_pmid> to pick up those cookies.
    #
    # PMID form:  /v2/articles/pmid%3A{pmid}?include=issue%2Cjournal
    # DOI  form:  /v2/articles/doi%3A{doi}?include=issue%2Cjournal
    TI_BASE = "https://api.thirdiron.com/v2"

    THIRDIRON_PUBLIC = f"https://api.thirdiron.com/public/v1/libraries/{UW_LIBKEY_ID}"

    ti_targets = []
    if pmid_numeric:
        ti_targets += [
            # Public v1 library endpoint — no auth required, often returns fullTextFile
            (f"{THIRDIRON_PUBLIC}/articles/pmid:{pmid_numeric}",
             "thirdiron-pub-pmid"),
            # Authenticated v2 library endpoint
            (f"{TI_BASE}/libraries/{UW_LIBKEY_ID}/articles/pmid%3A{pmid_numeric}?include=issue%2Cjournal&reload=true",
             "thirdiron-lib-pmid"),
            (f"{TI_BASE}/articles/pmid%3A{pmid_numeric}?include=issue%2Cjournal&reload=true",
             "thirdiron-pmid"),
        ]
    if doi:
        ti_targets += [
            (f"{THIRDIRON_PUBLIC}/articles/doi:{doi}",
             "thirdiron-pub-doi"),
            (f"{TI_BASE}/libraries/{UW_LIBKEY_ID}/articles/doi%3A{doi}?include=issue%2Cjournal",
             "thirdiron-lib-doi"),
            (f"{TI_BASE}/articles/doi%3A{doi}?include=issue%2Cjournal",
             "thirdiron-doi"),
        ]

    # Library-specific ThirdIron endpoints require libkey.io session cookies.
    # If all of them return 401, prompt the user to export those cookies once.
    _lib_ti_labels = {lbl for _, lbl in ti_targets
                      if f"/libraries/{UW_LIBKEY_ID}/" in _ or "pub" in lbl}

    ti_sess = make_cookie_session("thirdiron.com")
    if HAS_BROWSER_COOKIES:
        for loader in (browser_cookie3.chrome, browser_cookie3.firefox):
            try:
                ti_sess.cookies.update(loader(domain_name="libkey.io"))
                break
            except Exception:
                pass

    # Try to extract the library auth token from the LibKey JS bundle.
    # The SPA embeds a library-specific Bearer token for ThirdIron API calls.
    _ti_lib_token, _ti_lib_token_type = _get_thirdiron_library_token(vlog=vlog)
    if _ti_lib_token:
        vlog(f"Using extracted ThirdIron library token (length={len(_ti_lib_token)})")

    _lib_auth_failures = 0

    for ti_url, ti_label in ti_targets:
        try:
            ti_headers = {"Referer": f"https://libkey.io/libraries/{UW_LIBKEY_ID}/",
                          "Origin": "https://libkey.io"}
            # Add the library auth token for library-specific endpoints
            if _ti_lib_token and ti_label in _lib_ti_labels:
                ti_headers["Authorization"] = f"{_ti_lib_token_type} {_ti_lib_token}"
            r = ti_sess.get(ti_url, timeout=20, headers=ti_headers)
            vlog(f"ThirdIron v2 ({ti_label}) status={r.status_code}  "
                 f"ct={r.headers.get('content-type','')}")
            if r.status_code == 401 and ti_label in _lib_ti_labels:
                _lib_auth_failures += 1
                continue
            if r.status_code != 200:
                continue
            body = r.json()
            rec = body.get("data") or {}
            attrs = rec.get("attributes", rec)
            article_id = rec.get("id") or attrs.get("id")
            if verbose:
                print(f"      [libkey] ThirdIron v2 attrs: {list(attrs.keys())}")
                for fld in ("fullTextFile", "libkeyFullTextFile",
                            "contentLocation", "libkeyContentLocation",
                            "linkResolverOpenurlLink", "nomadFallbackURL",
                            "vpnRequired"):
                    print(f"      [libkey]   {fld} = {attrs.get(fld)!r}")

            ti_article_id = article_id
            if not ti_article_id:
                lkcl = attrs.get("libkeyContentLocation", "")
                m_id = re.search(r'/articles/(\d+)/', lkcl)
                if m_id:
                    ti_article_id = m_id.group(1)

            candidates = []
            for fld in ("libkeyFullTextFile", "fullTextFile", "browzineWebLink",
                        "contentLocation", "libkeyContentLocation",
                        "linkResolverOpenurlLink", "nomadFallbackURL"):
                val = attrs.get(fld)
                if val and isinstance(val, str) and val not in [u for u, _ in candidates]:
                    candidates.append((val, f"{ti_label}-{fld}"))

            # If we got a libkeyContentLocation with an article ID, also
            # construct the library full-text-file URL directly — this is
            # what the browser SPA resolves to (Silverchair signed token URL).
            if ti_article_id:
                ftf = (f"https://libkey.io/libraries/{UW_LIBKEY_ID}"
                       f"/articles/{ti_article_id}/full-text-file"
                       f"?utm_source=api_{UW_LIBKEY_ID}&allow_speedbump=true")
                if ftf not in [u for u, _ in candidates]:
                    candidates.append((ftf, f"{ti_label}-libkey-ftf"))

            for url, label in candidates:
                result = _follow_to_pdf(url, label, path, verbose, sess=ti_sess)
                if result is _NO_SUBSCRIPTION:
                    return _NO_SUBSCRIPTION   # propagate upward
                if result:
                    return result
        except Exception as e:
            vlog(f"ThirdIron v2 error ({ti_label}): {e}")

    if _lib_auth_failures == len(_lib_ti_labels) and _lib_ti_labels:
        _prompt_cookies_refresh(
            "LibKey / ThirdIron (UW Library)",
            f"https://libkey.io/libraries/{UW_LIBKEY_ID}/",
            'Export ALL cookies to cookies.txt using the\n'
            '       "Get cookies.txt LOCALLY" Chrome extension\n'
            '       (click the extension while on that page, then Export).',
        )

    return None


def try_unpaywall(doi, path):
    """Look up an open-access PDF via the Unpaywall API."""
    if not doi:
        return None
    try:
        r = session.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": EMAIL}, timeout=30,
        )
        if r.status_code != 200:
            return None
        best    = r.json().get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf")
        if not pdf_url:
            return None
        r2 = session.get(pdf_url, timeout=60, allow_redirects=True)
        if r2.status_code == 200 and is_pdf(r2.content):
            path.write_bytes(r2.content)
            return pdf_url
    except Exception:
        pass
    return None

def try_semantic_scholar(doi, title, path, verbose=False):
    """
    Query the Semantic Scholar API for an open-access PDF.
    Free, no key required for moderate rates. Complements Unpaywall —
    Semantic Scholar indexes preprints, repositories, and some hosted PDFs
    that Unpaywall misses.

    Tries DOI lookup first; falls back to title search if the DOI isn't
    indexed (Semantic Scholar's DOI coverage is incomplete for older papers).
    """
    def vlog(msg):
        if verbose:
            print(f"\n      [s2] {msg}")

    pdf_url = None

    # ── Pass 1: DOI lookup ────────────────────────────────────────────────────
    if doi:
        try:
            r = session.get(
                f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                params={"fields": "openAccessPdf,externalIds,title"},
                timeout=20,
            )
            vlog(f"DOI lookup status={r.status_code} ct={r.headers.get('content-type','')}")
            if r.status_code == 200:
                data = r.json()
                vlog(f"title={data.get('title','')!r}")
                oa = data.get("openAccessPdf") or {}
                pdf_url = oa.get("url")
                vlog(f"openAccessPdf={oa}")
            else:
                vlog(f"DOI lookup body: {r.text[:200]}")
        except Exception as e:
            vlog(f"DOI lookup error: {e}")

    # ── Pass 2: title search fallback ─────────────────────────────────────────
    if not pdf_url and title:
        try:
            r = session.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": title, "fields": "openAccessPdf,externalIds,title",
                        "limit": 5},
                timeout=20,
            )
            vlog(f"title search status={r.status_code}")
            if r.status_code == 200:
                items = r.json().get("data", [])
                vlog(f"title search hits={len(items)}")
                for item in items:
                    vlog(f"  candidate title={item.get('title','')!r}  "
                         f"doi={item.get('externalIds',{}).get('DOI','')!r}  "
                         f"oa={item.get('openAccessPdf')}")
                # Prefer the hit whose DOI matches; otherwise take first with OA PDF
                doi_norm = (doi or "").lower()
                for item in items:
                    item_doi = (item.get("externalIds") or {}).get("DOI", "").lower()
                    oa = item.get("openAccessPdf") or {}
                    if oa.get("url") and (not doi_norm or item_doi == doi_norm):
                        pdf_url = oa["url"]
                        vlog(f"selected: {item.get('title','')!r} → {pdf_url}")
                        break
                if not pdf_url:
                    # No DOI match — take first with an OA PDF
                    for item in items:
                        oa = item.get("openAccessPdf") or {}
                        if oa.get("url"):
                            pdf_url = oa["url"]
                            vlog(f"no DOI match; using first OA: {item.get('title','')!r} → {pdf_url}")
                            break
        except Exception as e:
            vlog(f"title search error: {e}")

    if not pdf_url:
        vlog("no OA PDF found")
        return None

    try:
        vlog(f"fetching {pdf_url}")
        r2 = session.get(pdf_url, timeout=60, allow_redirects=True)
        vlog(f"fetch status={r2.status_code} is_pdf={is_pdf(r2.content)}")
        if r2.status_code == 200 and is_pdf(r2.content):
            path.write_bytes(r2.content)
            return pdf_url
    except Exception as e:
        vlog(f"PDF fetch error: {e}")
    return None


def try_scholarly(doi, title, path):
    """
    Search Google Scholar for an open-access PDF using the `scholarly` library.
    Mirrors what Scholar's 'PDF' link shows (e.g. academia.edu, institutional
    repos, author pages).  Requires:  pip install scholarly

    Note: Google Scholar rate-limits aggressive scrapers.  This runs with a
    1–2 s delay and a single query; it won't get blocked in normal batch use.
    """
    if not HAS_SCHOLARLY:
        return None
    try:
        from scholarly import scholarly as _sch
        query = f"doi:{doi}" if doi else title
        results = _sch.search_pubs(query)
        pub = next(results, None)
        if pub is None:
            return None
        # Scholar shows a direct-to-PDF 'eprint' link for hosted PDFs
        eprint_url = pub.get("eprint_url") or pub.get("pub_url")
        if not eprint_url:
            return None
        r = session.get(eprint_url, timeout=60, allow_redirects=True,
                        headers={"Referer": "https://scholar.google.com/"})
        if r.status_code == 200 and is_pdf(r.content):
            path.write_bytes(r.content)
            return eprint_url
        # Some eprint links are landing pages — look for a PDF link inside
        for pdf_url in find_pdf_urls_in_html(r.text, eprint_url)[:4]:
            r2 = session.get(pdf_url, timeout=60, allow_redirects=True,
                             headers={"Referer": eprint_url})
            if r2.status_code == 200 and is_pdf(r2.content):
                path.write_bytes(r2.content)
                return pdf_url
    except Exception:
        pass
    return None


# Publisher-specific rules for guessing the direct PDF URL from the abstract URL.
PUBLISHER_PDF_PATTERNS = {
    "link.springer.com":          lambda u, d: f"https://link.springer.com/content/pdf/{d}.pdf",
    "springer.com":               lambda u, d: f"https://link.springer.com/content/pdf/{d}.pdf",
    "onlinelibrary.wiley.com":    lambda u, d: re.sub(r"/doi/(abs/|full/)?", "/doi/pdfdirect/", u),
    "www.pnas.org":               lambda u, d: f"https://www.pnas.org/doi/pdf/{d}?download=true",
    "pnas.org":                   lambda u, d: f"https://www.pnas.org/doi/pdf/{d}?download=true",
    "www.nature.com":             lambda u, d: f"https://www.nature.com/articles/{d.split('/',1)[-1]}.pdf",
    "nature.com":                 lambda u, d: f"https://www.nature.com/articles/{d.split('/',1)[-1]}.pdf",
    "www.science.org":            lambda u, d: re.sub(r"/doi/", "/doi/pdf/", u),
    "science.org":                lambda u, d: re.sub(r"/doi/", "/doi/pdf/", u),
    "www.cell.com":               lambda u, d: re.sub(r"/article/", "/article/pdfExtended/", u),
    "cell.com":                   lambda u, d: re.sub(r"/article/", "/article/pdfExtended/", u),
    "www.sciencedirect.com":      lambda u, d: u + "/pdfft?isDTMRedir=true&download=true",
    "sciencedirect.com":          lambda u, d: u + "/pdfft?isDTMRedir=true&download=true",
    "linkinghub.elsevier.com":    lambda u, d: (
        "https://www.sciencedirect.com/science/article/pii/" + u.split("/pii/")[-1]
        + "/pdfft?isDTMRedir=true&download=true"
        if "/pii/" in u else
        f"https://www.sciencedirect.com/science/article/doi/{d}/pdfft?isDTMRedir=true&download=true"
    ),
    "academic.oup.com":           lambda u, d: re.sub(r"/article/", "/article-pdf/", u) + "/pdf",
    "journals.plos.org":          lambda u, d: re.sub(r"article\?id=", "article/file?id=", u) + "&type=printable",
    "biorxiv.org":                lambda u, d: u.rstrip("/") + ".full.pdf",
    "www.biorxiv.org":            lambda u, d: u.rstrip("/") + ".full.pdf",
    "rupress.org":                lambda u, d: re.sub(r"/content/", "/content/pdf/", u),
    "journals.physiology.org":    lambda u, d: re.sub(r"/doi/", "/doi/pdf/", u),
    "physiology.org":             lambda u, d: re.sub(r"/doi/", "/doi/pdf/", u),
    "febs.onlinelibrary.wiley.com": lambda u, d: re.sub(r"/doi/(abs/|full/)?", "/doi/pdfdirect/", u),
    "biochemj.org":               lambda u, d: u + ".full-text.pdf",
    "portlandpress.com":          lambda u, d: re.sub(r"/article/", "/article/pdf/", u) + "/pdf",
    "www.jbc.org":                lambda u, d: re.sub(r"/doi/", "/doi/pdf/", u),
    "jbc.org":                    lambda u, d: re.sub(r"/doi/", "/doi/pdf/", u),
    "www.embopress.org":          lambda u, d: re.sub(r"/doi/", "/doi/pdf/", u),
    "embopress.org":              lambda u, d: re.sub(r"/doi/", "/doi/pdf/", u),
    "www.plosone.org":            lambda u, d: re.sub(r"article\?id=", "article/file?id=", u) + "&type=printable",
    "genome.cshlp.org":           lambda u, d: u + ".full.pdf",
    "genesdev.cshlp.org":         lambda u, d: u + ".full.pdf",
    "rnajournal.cshlp.org":       lambda u, d: u + ".full.pdf",
    "www.g3journal.org":          lambda u, d: re.sub(r"/doi/", "/doi/pdf/", u),
    "genetics.org":               lambda u, d: u + ".full.pdf",
    "www.genetics.org":           lambda u, d: u + ".full.pdf",
}

def find_pdf_urls_in_html(html, base_url):
    """Extract candidate PDF URLs from an HTML page using regex."""
    from urllib.parse import urljoin, urlparse
    raw_patterns = [
        # Normal HTML href attrs (no trailing }) — these were broken before
        r'href=["\']([^"\']*\.pdf(?:\?[^"\']*)?)["\'"]',
        r'href=["\']([^"\']*/pdf/[^"\']+)["\']',
        r'href=["\']([^"\']*/doi/pdf[^"\']+)["\']',
        r'href=["\']([^"\']*/doi/pdfdirect[^"\']+)["\']',
        r'href=["\']([^"\']*pdfft[^"\']+)["\']',
        r'href=["\']([^"\']*article-pdf[^"\']+)["\']',
        # JSON-embedded (Next.js __NEXT_DATA__ etc.) — keep the } variant too
        r'href=["\']([^"\']*\.pdf(?:\?[^"\']*)?)["\']}',
        r'href=["\']([^"\']*/pdf/[^"\']+)["\']}',
        r'"(?:pdf|pdfUrl|downloadPdfUrl|pdf_url)"\s*:\s*"([^"]+)"',
        r'data-pdf[^>]*href=["\']([^"\']+)["\']',
    ]
    seen, results = set(), []
    for pat in raw_patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            raw = m.group(1).replace("\\u002F", "/").replace("\\/", "/")
            if raw.startswith('#'):
                continue
            url = urljoin(base_url, raw)
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https'):
                continue
            if '#' in url:
                path_part = url.split('#', 1)[0].lower()
                if not any(kw in path_part for kw in
                           ('.pdf', '/pdf', 'pdfft', 'pdfdirect', 'article-pdf', 'epdf')):
                    continue
            if url not in seen:
                seen.add(url)
                results.append(url)
    _supp_re = re.compile(
        r'/esm/|moesm|_esm\.|supplementar|suppl[_.]|[/_]si\d+[._]|mediaobjects',
        re.IGNORECASE,
    )
    main = [u for u in results if not _supp_re.search(u)]
    supp = [u for u in results if     _supp_re.search(u)]
    return main + supp

def ezproxy_url(publisher_url):
    """
    Convert a publisher URL to its UW EZProxy equivalent.
    UW EZProxy domain: offcampus.lib.washington.edu
    Format: https://{domain-with-dots-as-hyphens}.offcampus.lib.washington.edu/path

    With UW VPN in full-tunnel mode, EZProxy authenticates by IP --
    no Shibboleth login needed. This gives access to all UW-subscribed
    journals regardless of whether the publisher supports IP auth directly.

    If you get "Login required" from EZProxy while on VPN, log in once at
    https://offcampus.lib.washington.edu and export those cookies to cookies.txt.
    """
    from urllib.parse import urlparse
    parsed = urlparse(publisher_url)
    if not parsed.netloc:
        return publisher_url
    proxy_host = parsed.netloc.replace(".", "-") + ".offcampus.lib.washington.edu"
    return publisher_url.replace(
        parsed.scheme + "://" + parsed.netloc,
        parsed.scheme + "://" + proxy_host,
    )


_EZPROXY_LOGIN_URLS = (
    "offcampus.lib.washington.edu/login",
    "login.offcampus.lib.washington.edu",
    "www.lib.washington.edu/connect",
    "idp.u.washington.edu",
    "weblogin.washington.edu",
)

def _get_via_ezproxy(url, sess, timeout=60, max_redirects=12, vlog=None):
    """
    Fetch a URL through EZProxy, keeping every redirect within the EZProxy domain.

    The problem with plain allow_redirects=True: publisher redirect responses use
    canonical domain names (e.g. 'Location: https://academic.oup.com/jxb/article/...')
    so requests follows them OUTSIDE the proxy and loses institutional IP access.

    This manually follows each hop and rewrites Location headers that escape the
    EZProxy domain back through EZProxy before following them.

    Returns the final requests.Response, or None on error / redirect loop.
    If EZProxy redirects to a login page, returns that response so the caller
    can detect it via _is_ezproxy_login_wall().
    """
    from urllib.parse import urlparse, urljoin
    current_url = ezproxy_url(url)
    visited = set()
    for _ in range(max_redirects):
        if current_url in visited:
            break
        # If we're about to fetch a login page, return a sentinel response.
        # We can't log in programmatically; surface this to the caller.
        if any(s in current_url for s in _EZPROXY_LOGIN_URLS):
            if vlog:
                vlog(f"EZProxy login wall at hop {_+1}: {current_url}")
            # Return a fake response-like object the caller can detect
            class _LoginWallSentinel:
                status_code = 302
                url = current_url
                content = b""
                headers = {}
                text = ""
                def __init__(self, u): self.url = u
            return _LoginWallSentinel(current_url)
        visited.add(current_url)
        try:
            r = sess.get(current_url, timeout=timeout, allow_redirects=False,
                         headers={"Referer": url,
                                  "Accept": "text/html,application/xhtml+xml,*/*;q=0.9"})
        except Exception as e:
            if vlog:
                vlog(f"_get_via_ezproxy error at {current_url}: {e}")
            return None
        if vlog:
            vlog(f"EZProxy hop {_+1}: {current_url} → {r.status_code}")
        if r.status_code not in (301, 302, 303, 307, 308):
            return r
        location = r.headers.get("Location", "")
        if not location:
            return r
        # Make location absolute
        location = urljoin(current_url, location)
        # If the redirect leaves EZProxy domain, re-proxy it
        # (but don't re-proxy auth/login domains — let them surface as login walls)
        if ("offcampus.lib.washington.edu" not in location
                and not any(s in location for s in _EZPROXY_LOGIN_URLS)):
            location = ezproxy_url(location)
        current_url = location
    return None


_thirdiron_library_token_cache: dict = {}

def _get_thirdiron_library_token(vlog=None):
    """
    Extract the ThirdIron library Bearer token from the LibKey Ember.js JS bundle.

    LibKey embeds a library-specific API key in its client-side JS.  Fetching
    it programmatically simulates exactly what the browser SPA does, so we can
    call the authenticated ThirdIron library endpoints directly.

    Returns (token_string, "Bearer") or (None, None) on failure.
    Cached after the first successful fetch for the lifetime of the process.
    """
    if "token" in _thirdiron_library_token_cache:
        return _thirdiron_library_token_cache["token"], _thirdiron_library_token_cache.get("type")

    sess = make_cookie_session("libkey.io")
    try:
        r = sess.get(f"https://libkey.io/libraries/{UW_LIBKEY_ID}/",
                     timeout=30, allow_redirects=True)
        if r.status_code != 200:
            if vlog:
                vlog(f"LibKey SPA page status={r.status_code}")
            return None, None
        # Find all JS bundle URLs from the HTML
        bundle_urls = re.findall(
            r'(?:src|href)=["\']([^"\']+\.js(?:\?[^"\']*)?)["\']', r.text)
        # Also look for hashed bundle names (common in Ember/webpack)
        bundle_urls += re.findall(
            r'"(/assets/[^"]+(?:chunk|app|vendor|libkey)[^"]*\.js)"', r.text)
        base = "https://libkey.io"
        seen = set()
        for burl in bundle_urls:
            if not burl.startswith("http"):
                burl = base + burl if burl.startswith("/") else base + "/" + burl
            if burl in seen:
                continue
            seen.add(burl)
            try:
                rb = sess.get(burl, timeout=60)
                if rb.status_code != 200 or len(rb.content) < 5000:
                    continue
                text = rb.text
                # JWT pattern: three base64url-encoded parts separated by dots
                jwt_re = r'[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'
                # Look for JWT near auth-related keywords
                for m in re.finditer(
                    r'(?:bearer|Bearer|authorization|Authorization|'
                    r'apiKey|api_key|accessToken|access_token)'
                    r'["\s:,{(]*(' + jwt_re + r')',
                    text
                ):
                    candidate = m.group(1)
                    if len(candidate) > 50:
                        if vlog:
                            vlog(f"Extracted ThirdIron token from {burl} "
                                 f"(length={len(candidate)})")
                        _thirdiron_library_token_cache["token"] = candidate
                        _thirdiron_library_token_cache["type"] = "Bearer"
                        return candidate, "Bearer"
            except Exception as e:
                if vlog:
                    vlog(f"Bundle fetch error {burl}: {e}")
    except Exception as e:
        if vlog:
            vlog(f"_get_thirdiron_library_token error: {e}")
    return None, None


def pdf_url_from_doi(doi):
    """
    Build publisher-specific PDF URLs directly from a DOI prefix.
    Returns a list of candidate URLs to try (direct first, EZProxy second).
    """
    candidates = []
    d = doi.lower()

    def _add(direct_url):
        candidates.append(direct_url)
        ez = ezproxy_url(direct_url)
        if ez != direct_url:
            candidates.append(ez)

    if d.startswith("10.1073/"):        # PNAS
        _add(f"https://www.pnas.org/doi/pdf/{doi}?download=true")
    if d.startswith("10.1038/"):        # Nature family
        _add("https://www.nature.com/articles/" + doi.split("/", 1)[-1] + ".pdf")
    if d.startswith("10.1126/"):        # Science
        _add(f"https://www.science.org/doi/pdf/{doi}")
    if d.startswith("10.1016/"):        # Elsevier ScienceDirect
        pii = doi.split("/", 1)[-1].upper().replace("-", "").replace("(", "").replace(")", "").replace(".", "")
        _add(f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?isDTMRedir=true&download=true")
    if d.startswith("10.1007/"):        # Springer
        _add(f"https://link.springer.com/content/pdf/{doi}.pdf")
    # Wiley/Oxford: need resolved URL, handled in Strategy B
    return candidates


def try_direct_doi(doi, path, verbose=False):
    """
    Try to get a PDF via the journal site (works best with UW VPN active).
    Strategy:
      A. Direct PDF URL from DOI prefix; each has an EZProxy twin
      B. Follow DOI redirect, then try publisher pattern on the final URL
         (also tries EZProxy version if direct fails)
      C. Scrape the article page HTML for PDF links
      D. EZProxy fallback: proxy the resolved article URL through UW EZProxy

    Returns (source_url_or_None, manual_fallback_url).
    """
    if not doi:
        return None, None

    pdf_headers = {"Accept": "application/pdf,*/*"}
    _DOI_PREFIX_DOMAIN = {
        "10.1073":  ".pnas.org",
        "10.1038":  ".nature.com",
        "10.1126":  ".science.org",
        "10.1016":  ".sciencedirect.com",
        "10.1007":  ".springer.com",
        "10.1002":  ".wiley.com",
        "10.1046":  ".wiley.com",
        "10.1093":  ".academic.oup.com",
        "10.1371":  ".plos.org",
        "10.1083":  ".rupress.org",
        "10.1152":  ".physiology.org",
        "10.1042":  ".portlandpress.com",
        "10.1074":  ".jbc.org",
        "10.15252": ".embopress.org",
        "10.1101":  ".biorxiv.org",
        "10.1006":  ".sciencedirect.com",
        "10.1098":  ".royalsocietypublishing.org",
        "10.1099":  ".microbiologyresearch.org",
        "10.1113":  ".onlinelibrary.wiley.com",
        "10.1523":  ".jneurosci.org",
        "10.1085":  ".rupress.org",
        "10.1111":  ".onlinelibrary.wiley.com",
        "10.1096":  ".fasebj.org",
        "10.1128":  ".asm.org",
        "10.1104":  ".plantphysiol.org",
        "10.1105":  ".plantcell.org",
    }
    doi_prefix = doi.split("/")[0] if "/" in doi else ""
    cookie_domain = _DOI_PREFIX_DOMAIN.get(doi_prefix, doi_prefix)
    cookie_session = make_cookie_session(cookie_domain) if HAS_BROWSER_COOKIES else session
    if verbose:
        n_total = len(cookie_session.cookies)
        has_file = COOKIES_FILE.exists()
        sent = [c.name for c in cookie_session.cookies
                if cookie_domain.lstrip(".") in (c.domain or "")]
        print(f"\n      [verbose] cookie_domain={cookie_domain!r}  "
              f"total_cookies={n_total}  domain_cookies={len(sent)} ({sent})  "
              f"cookies_txt={has_file}  browser_cookie3={HAS_BROWSER_COOKIES}")

    def try_url(pdf_url, referer=None):
        h = dict(pdf_headers)
        if referer:
            h["Referer"] = referer
        try:
            r2 = cookie_session.get(pdf_url, timeout=60, allow_redirects=True, headers=h)
            if verbose:
                ct2 = r2.headers.get("content-type", "")
                print(f"      [verbose]   -> {pdf_url}  status={r2.status_code}  ct={ct2}  size={len(r2.content)}  is_pdf={is_pdf(r2.content)}")
            if r2.status_code == 200 and is_pdf(r2.content):
                path.write_bytes(r2.content)
                return True
        except Exception as e:
            if verbose:
                print(f"      [verbose]   -> {pdf_url}  error: {e}")
        return False

    # --- Strategy A: direct PDF URL from DOI prefix (+ EZProxy twin) --------
    direct_candidates = pdf_url_from_doi(doi)
    if direct_candidates and verbose:
        print(f"\n      [verbose] trying {len(direct_candidates)} direct PDF URL(s) from DOI prefix")
    doi_referer = f"https://doi.org/{doi}"
    for pdf_url in direct_candidates:
        if try_url(pdf_url, referer=doi_referer):
            return pdf_url, None

    # --- Follow DOI redirect ------------------------------------------------
    doi_url = f"https://doi.org/{doi}"
    final_url = doi_url
    html = ""
    try:
        r = cookie_session.get(doi_url, timeout=30, allow_redirects=True)
        if verbose:
            ct = r.headers.get("content-type", "")
            print(f"\n      [verbose] DOI resolved to {r.url}  status={r.status_code}  content-type={ct}  size={len(r.content)}")
        final_url = r.url
        if r.status_code == 200 and is_pdf(r.content):
            path.write_bytes(r.content)
            return final_url, None
        if r.status_code == 200:
            html = r.text
    except Exception as e:
        if verbose:
            print(f"      [verbose] DOI redirect error: {e}")

    # --- Strategy B: publisher pattern on resolved URL ----------------------
    if verbose:
        print(f"      [verbose] trying publisher patterns on {final_url}")
    for domain, pdf_fn in PUBLISHER_PDF_PATTERNS.items():
        if domain in final_url:
            try:
                pdf_url = pdf_fn(final_url, doi)
                if try_url(pdf_url, referer=final_url):
                    return pdf_url, None
                ez = ezproxy_url(pdf_url)
                if ez != pdf_url and try_url(ez, referer=final_url):
                    return ez, None
            except Exception as e:
                if verbose:
                    print(f"      [verbose] pattern error: {e}")
            break

    # --- Strategy C: scrape article page HTML -------------------------------
    if html:
        candidates = find_pdf_urls_in_html(html, final_url)
        if verbose:
            print(f"      [verbose] HTML scrape found {len(candidates)} candidate PDF URLs:")
            for u in candidates[:5]:
                print(f"        {u}")
        for pdf_url in candidates[:8]:
            if try_url(pdf_url, referer=final_url):
                return pdf_url, None

    # --- Strategy D: EZProxy on the resolved article page itself ------------
    # Route through UW EZProxy, keeping every redirect within the proxy domain.
    # OUP and other publishers redirect from article-lookup to the canonical
    # article URL, which takes requests outside EZProxy if we use allow_redirects.
    # _get_via_ezproxy() rewrites each Location header back through EZProxy.
    if final_url and final_url != f"https://doi.org/{doi}":
        ez_article_start = ezproxy_url(final_url)
        if ez_article_start != final_url:
            _vlog = (lambda msg: print(f"      [verbose] {msg}")) if verbose else (lambda msg: None)
            _vlog(f"Strategy D: EZProxy (redirect-following) for {final_url}")
            _ez_login_wall = False
            ez_sess = make_cookie_session("offcampus.lib.washington.edu")
            r_ez = _get_via_ezproxy(final_url, ez_sess, timeout=60,
                                    vlog=_vlog)
            if r_ez is None:
                return None, final_url
            _vlog(f"  EZProxy final: {r_ez.url}  status={r_ez.status_code}  "
                  f"ct={r_ez.headers.get('content-type','')}  "
                  f"size={len(r_ez.content)}  is_pdf={is_pdf(r_ez.content)}")
            if _is_ezproxy_login_wall(r_ez):
                _ez_login_wall = True
                _vlog(f"  EZProxy login wall: {r_ez.url}")
            elif r_ez.status_code == 200 and is_pdf(r_ez.content):
                path.write_bytes(r_ez.content)
                return r_ez.url, None
            elif r_ez.status_code == 200:
                # Try publisher PDF patterns on the EZProxy-resolved page
                for domain, pdf_fn in PUBLISHER_PDF_PATTERNS.items():
                    if domain in r_ez.url or domain in final_url:
                        try:
                            ez_pdf = ezproxy_url(pdf_fn(r_ez.url, doi))
                            r_p = ez_sess.get(ez_pdf, timeout=60, allow_redirects=True,
                                              headers={"Accept": "application/pdf,*/*",
                                                       "Referer": r_ez.url})
                            _vlog(f"  EZProxy PDF pattern -> {ez_pdf}  status={r_p.status_code}  is_pdf={is_pdf(r_p.content)}")
                            if r_p.status_code == 200 and is_pdf(r_p.content):
                                path.write_bytes(r_p.content)
                                return r_p.url, None
                        except Exception:
                            pass
                        break
                for pdf_url in find_pdf_urls_in_html(r_ez.text, r_ez.url)[:6]:
                    ez_pdf2 = ezproxy_url(pdf_url)
                    r_p2 = ez_sess.get(ez_pdf2, timeout=60, allow_redirects=True,
                                       headers={"Accept": "application/pdf,*/*",
                                                "Referer": r_ez.url})
                    if r_p2.status_code == 200 and is_pdf(r_p2.content):
                        path.write_bytes(r_p2.content)
                        return r_p2.url, None
            if _ez_login_wall:
                return None, final_url   # caller: needs manual EZProxy auth

    return None, None


def download_paper(pmid, title, pmc_info, tracking, verbose=False, pmc_only=False, ebsco_only=False):
    pmcid        = pmc_info.get("pmcid")
    doi          = pmc_info.get("doi")
    pmid_numeric = pmc_info.get("pmid_numeric") or pmid

    # Skip entirely (no output, no tracking) if pmc_only and no PMCID
    if pmc_only and not pmcid:
        return False

    filename     = safe_filename(pmid_numeric, title)
    path         = OUTPUT_DIR / filename
    print(f"\n  PMID {pmid}  PMC {pmcid or '---'}  DOI {doi or '---'}")
    print(f"  {title[:80]}")

    if ebsco_only:
        # Jump straight to EBSCO; skip everything else (including PMC)
        print("    [3] EBSCO ...        ", end="", flush=True)
        time.sleep(GENERAL_DELAY)
        src = try_ebsco(pmid_numeric, doi, path, verbose=verbose)
        if src:
            print("OK")
            tracking[pmid] = {"status": "downloaded", "filename": filename,
                               "source": "ebsco", "doi": doi, "pmcid": pmcid}
            return True
        print("X")
        tracking[pmid] = {"status": "failed", "doi": doi, "pmcid": pmcid}
        return False

    # 1. PubMed Central (known PMCID) -----------------------------------------
    if pmcid:
        print("    [1] PMC ...          ", end="", flush=True)
        src = try_pmc(pmcid, path, verbose=verbose)
        if src:
            print("OK")
            tracking[pmid] = {"status": "downloaded", "filename": filename,
                               "source": "pmc", "pmcid": pmcid, "doi": doi}
            return True
        print("X")

    if pmc_only:
        tracking[pmid] = {"status": "failed", "doi": doi, "pmcid": pmcid}
        return False

    # 2. PubMed page (may reveal PMC ID or free links) ------------------------
    print("    [2] PubMed page ...  ", end="", flush=True)
    time.sleep(NCBI_DELAY)
    src, found_pmcid = try_pubmed_page(pmid_numeric, path)
    if src:
        print("OK")
        tracking[pmid] = {"status": "downloaded", "filename": filename,
                           "source": "pubmed_page", "pmcid": found_pmcid, "doi": doi}
        return True
    if found_pmcid and found_pmcid != pmcid:
        print(f"X (found {found_pmcid})")
        print("    [2b] PMC (new) ...   ", end="", flush=True)
        src = try_pmc(found_pmcid, path, verbose=verbose)
        if src:
            print("OK")
            tracking[pmid] = {"status": "downloaded", "filename": filename,
                               "source": "pmc", "pmcid": found_pmcid, "doi": doi}
            return True
        print("X")
    else:
        print("X")

    # 3. EBSCO Research (UW Library full-text database) -----------------------
    print("    [3] EBSCO ...        ", end="", flush=True)
    time.sleep(GENERAL_DELAY)
    src = try_ebsco(pmid_numeric, doi, path, verbose=verbose)
    if src:
        print("OK")
        tracking[pmid] = {"status": "downloaded", "filename": filename,
                           "source": "ebsco", "doi": doi, "pmcid": pmcid}
        return True
    else:
        print("X")

    # 4. LibKey / ThirdIron (UW library link resolver -- needs UW VPN) --------
    print("    [4] LibKey ...       ", end="", flush=True)
    time.sleep(GENERAL_DELAY)
    src = try_libkey(pmid_numeric, doi, path, verbose=verbose)
    if src is _NO_SUBSCRIPTION:
        print("no UW subscription")
        tracking[pmid] = {"status": "no_subscription", "doi": doi, "pmcid": pmcid}
        # Fall through to open-access steps
    elif src:
        print("OK")
        tracking[pmid] = {"status": "downloaded", "filename": filename,
                           "source": "libkey", "doi": doi, "pmcid": pmcid}
        return True
    else:
        print("X")

    # 5. Unpaywall ------------------------------------------------------------
    if doi:
        print("    [5] Unpaywall ...    ", end="", flush=True)
        time.sleep(GENERAL_DELAY)
        src = try_unpaywall(doi, path)
        if src:
            print("OK")
            tracking[pmid] = {"status": "downloaded", "filename": filename,
                               "source": "unpaywall", "doi": doi}
            return True
        print("X")

    # 5b. Semantic Scholar open-access PDF ------------------------------------
    if doi or title:
        print("    [5b] Semantic Scholar", end="", flush=True)
        time.sleep(GENERAL_DELAY)
        src = try_semantic_scholar(doi, title, path, verbose=verbose)
        if src:
            print(" OK")
            tracking[pmid] = {"status": "downloaded", "filename": filename,
                               "source": "semantic_scholar", "doi": doi}
            return True
        print(" X")

    # 5c. Google Scholar (scholarly) ------------------------------------------
    if HAS_SCHOLARLY:
        print("    [5c] Google Scholar  ", end="", flush=True)
        time.sleep(2.0)
        src = try_scholarly(doi, title, path)
        if src:
            print(" OK")
            tracking[pmid] = {"status": "downloaded", "filename": filename,
                               "source": "google_scholar", "doi": doi}
            return True
        print(" X")

    # 6. Direct DOI (UW VPN + EZProxy) ----------------------------------------
    if doi:
        print("    [6] Direct DOI ...   ", end="", flush=True)
        time.sleep(GENERAL_DELAY)
        src, manual_url = try_direct_doi(doi, path, verbose=verbose)
        if src:
            print("OK")
            tracking[pmid] = {"status": "downloaded", "filename": filename,
                               "source": "direct_doi", "doi": doi}
            return True
        print("X")
        if manual_url:
            print(f"    -> needs manual: {manual_url}")
            tracking[pmid] = {"status": "needs_manual", "filename": filename,
                               "manual_url": manual_url, "doi": doi, "pmcid": pmcid}
            return False

    print("    All strategies failed.")
    tracking[pmid] = {"status": "failed", "doi": doi, "pmcid": pmcid}
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Download PDFs for Biomodels papers."
    )
    parser.add_argument("--count", type=int, default=10,
                        help="How many new papers to attempt (default 10).")
    parser.add_argument("--start", type=int, default=0,
                        help="Skip the first N new papers (0-indexed).")
    parser.add_argument("--all", action="store_true",
                        help="Process all remaining papers (ignores --count).")
    parser.add_argument("--pmc-only", action="store_true",
                        help="Only try PMC (skip LibKey, Unpaywall, direct DOI).")
    parser.add_argument("--ebsco-only", action="store_true",
                        help="Only try EBSCO (skip PMC, PubMed page, LibKey, etc.).")
    parser.add_argument("--pmid", metavar="PMID",
                        help="Process only this PMID (overrides --count/--all/--skip).")
    parser.add_argument("--doi", metavar="DOI",
                        help="Process only the paper with this DOI (overrides --count/--all/--skip).")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose debug output for each strategy.")
    parser.add_argument(
        "--skip",
        choices=["tried", "auth", "downloaded", "none"],
        default="tried",
        help=(
            "tried:       skip all previously attempted PMIDs (default).\n"
            "downloaded:  skip only successfully downloaded PMIDs.\n"
            "auth:        skip only auth-blocked PMIDs; retry other failures.\n"
            "none:        reprocess everything."
        ),
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    tracking = load_tracking()

    data = json.loads(JSON_FILE.read_text(encoding="utf-8"))

    skip_statuses = set()
    if args.skip == "tried":
        skip_statuses = {"downloaded", "failed", "needs_manual",
                         "no_subscription", "no_pmcid"}
    elif args.skip == "downloaded":
        skip_statuses = {"downloaded"}
    elif args.skip == "auth":
        skip_statuses = {"downloaded", "no_subscription"}

    # --pmid / --doi: target a single paper regardless of skip/count settings
    if args.pmid or args.doi:
        needle_pmid = (args.pmid or "").strip()
        needle_doi  = (args.doi  or "").strip().lower()
        batch = []
        for pmid, info in data.items():
            numeric_id = re.sub(r'^https?://identifiers\.org/pubmed/', '', pmid)
            numeric_id = re.sub(r'^https?://identifiers\.org/doi/', '', numeric_id)
            if needle_pmid and needle_pmid == numeric_id:
                batch = [(pmid, info)]; break
            if needle_doi:
                info_doi = (info.get("doi") or "").lower()
                if needle_doi == info_doi or needle_doi == numeric_id.lower():
                    batch = [(pmid, info)]; break
        if not batch:
            print(f"No paper found matching "
                  f"{'PMID ' + args.pmid if args.pmid else 'DOI ' + args.doi}")
            sys.exit(1)
    else:
        papers = [
            (pmid, info) for pmid, info in data.items()
            if tracking.get(pmid, {}).get("status") not in skip_statuses
        ]
        start = args.start
        count = len(papers) if args.all else args.count
        # When pmc_only, don't pre-slice — the loop counts only attempted papers
        if args.pmc_only:
            batch = papers[start:]
        else:
            batch = papers[start: start + count]

    print(f"Output directory  : {OUTPUT_DIR}")
    print(f"Tracking file     : {TRACKING_FILE}")

    ok = 0
    failed = 0
    attempted = 0
    for pmid, info in batch:
        title     = info.get("title", "")
        accession = info.get("accession") or ""
        # Strip URI prefix if the key is a full identifiers.org URI
        numeric_id = re.sub(r'^https?://identifiers\.org/pubmed/', '', pmid)
        numeric_id = re.sub(r'^https?://identifiers\.org/doi/', '', numeric_id)

        # Look up PMC ID and DOI via NCBI (idconv → esummary → elink)
        pmc_map = get_pmc_info([accession or numeric_id], debug=args.debug)
        pmc_info = pmc_map.get(accession or numeric_id) or {}
        # Ensure pmid_numeric is set so try_libkey / try_pubmed_page work
        if "pmid_numeric" not in pmc_info:
            pmc_info["pmid_numeric"] = numeric_id

        tracking_before = {pmid: tracking[pmid]} if pmid in tracking else {}
        success  = download_paper(
            pmid, title, pmc_info, tracking,
            verbose=args.debug,
            pmc_only=args.pmc_only,
            ebsco_only=args.ebsco_only,
        )
        if success:
            attempted += 1
            ok += 1
            save_tracking(tracking, pmid)
        elif pmid in tracking and tracking[pmid] != tracking_before.get(pmid):
            attempted += 1
            failed += 1
            save_tracking(tracking, pmid)
        # else: silently skipped (no PMCID with --pmc-only); don't count

        if not args.all and attempted >= count:
            break

    print(f"\nDone. {ok}/{attempted} downloaded.")
    already = sum(1 for v in tracking.values() if v.get("status") == "downloaded")
    print(f"Total downloaded so far: {already}")


if __name__ == "__main__":
    main()
