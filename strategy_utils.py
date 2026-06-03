"""
Shared infrastructure for download strategies:
  - Configuration constants
  - HTTP sessions
  - Cookie helpers
  - Utility functions (is_pdf, find_pdf_urls_in_html, safe_filename)
  - EZProxy helpers
  - Publisher PDF patterns
  - _follow_to_pdf (shared by ebsco and libkey strategies)
"""

import re
import sys
import time
from pathlib import Path

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("Missing dependency. Run:  pip install requests")
    sys.exit(1)

try:
    import browser_cookie3
    HAS_BROWSER_COOKIES = True
except ImportError:
    HAS_BROWSER_COOKIES = False

try:
    import scholarly as _scholarly_mod
    HAS_SCHOLARLY = True
except ImportError:
    HAS_SCHOLARLY = False

# Sentinel returned (instead of None) when a publisher explicitly denies access
# due to missing institutional subscription.
_NO_SUBSCRIPTION = object()

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
OUTPUT_DIR   = SCRIPT_DIR / "Biomodels papers"
COOKIES_FILE = SCRIPT_DIR / "cookies.txt"

UW_LIBKEY_ID  = "3478"
EMAIL         = "lpsmith@uw.edu"

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

NCBI_HEADERS = {"User-Agent": f"BiomodelsDownloader/1.0 (mailto:{EMAIL})"}

# ── HTTP sessions ─────────────────────────────────────────────────────────────
_retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
_retry_no500 = Retry(total=2, backoff_factor=1.0, status_forcelist=[429, 502, 503, 504])

def _make_session(headers):
    s = requests.Session()
    s.headers.update(headers)
    s.mount("https://", HTTPAdapter(max_retries=_retry))
    s.mount("http://",  HTTPAdapter(max_retries=_retry))
    return s

session      = _make_session(HEADERS)
ncbi_session = _make_session(NCBI_HEADERS)

# ── Cookie helpers ────────────────────────────────────────────────────────────
def _load_cookies_txt(s):
    """Load a Netscape-format cookies.txt file into session s (if it exists)."""
    if not COOKIES_FILE.exists():
        return 0
    try:
        import http.cookiejar
        import tempfile, os

        raw = COOKIES_FILE.read_text(encoding="utf-8", errors="replace")
        fixed_lines = []
        for line in raw.splitlines():
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
    """Print instructions for refreshing session cookies for a site."""
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


def make_cookie_session(domain):
    """Return a session pre-loaded with cookies.txt + browser cookies for domain."""
    s = _make_session(HEADERS)
    _load_cookies_txt(s)
    if HAS_BROWSER_COOKIES and domain:
        for loader in (browser_cookie3.chrome, browser_cookie3.firefox):
            try:
                s.cookies.update(loader(domain_name=domain))
                break
            except Exception:
                pass
    return s

# ── Utilities ─────────────────────────────────────────────────────────────────
def is_pdf(data, min_size=5_000):
    """Check that a byte string looks like a real PDF."""
    return len(data) >= min_size and data[:4] == b"%PDF"


def safe_filename(pmid, title):
    clean_id = re.sub(r"[^\w.-]", "_", str(pmid))
    clean = re.sub(r"[^\w\s-]", "_", title[:70]).strip()
    clean = re.sub(r"\s+", "_", clean)
    return f"PMID{clean_id}_{clean}.pdf"


def find_pdf_urls_in_html(html, base_url):
    """Extract candidate PDF URLs from an HTML page using regex."""
    from urllib.parse import urljoin, urlparse
    raw_patterns = [
        r'href=["\']([^"\']*\.pdf(?:\?[^"\']*)?)["\'"]',
        r'href=["\']([^"\']*/pdf/[^"\']+)["\']',
        r'href=["\']([^"\']*/doi/pdf[^"\']+)["\']',
        r'href=["\']([^"\']*/doi/pdfdirect[^"\']+)["\']',
        r'href=["\']([^"\']*pdfft[^"\']+)["\']',
        r'href=["\']([^"\']*article-pdf[^"\']+)["\']',
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

# ── EZProxy helpers ───────────────────────────────────────────────────────────
def ezproxy_url(publisher_url):
    """Convert a publisher URL to its UW EZProxy equivalent."""
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


def _is_ezproxy_login_wall(response):
    """Return True if EZProxy redirected us to a login page."""
    url = getattr(response, "url", "") or ""
    if any(s in url for s in _EZPROXY_LOGIN_URLS):
        return True
    ct = ""
    try:
        ct = response.headers.get("content-type", "") if response.headers else ""
    except Exception:
        pass
    if "text/html" not in ct:
        return False
    snippet = response.content[:4096].lower()
    return (b"login required" in snippet or b"ezproxy" in snippet) and b"offcampus" in snippet


def _get_via_ezproxy(url, sess, timeout=60, max_redirects=12, vlog=None):
    """
    Fetch a URL through EZProxy, keeping every redirect within the EZProxy domain.
    Returns the final requests.Response, or None on error / redirect loop.
    """
    from urllib.parse import urljoin
    current_url = ezproxy_url(url)
    visited = set()
    for _ in range(max_redirects):
        if current_url in visited:
            break
        if any(s in current_url for s in _EZPROXY_LOGIN_URLS):
            if vlog:
                vlog(f"EZProxy login wall at hop {_+1}: {current_url}")
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
        location = urljoin(current_url, location)
        if ("offcampus.lib.washington.edu" not in location
                and not any(s in location for s in _EZPROXY_LOGIN_URLS)):
            location = ezproxy_url(location)
        current_url = location
    return None

# ── Publisher PDF patterns ────────────────────────────────────────────────────
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


def pdf_url_from_doi(doi):
    """Build publisher-specific PDF URLs directly from a DOI prefix."""
    candidates = []
    d = doi.lower()

    def _add(direct_url):
        candidates.append(direct_url)
        ez = ezproxy_url(direct_url)
        if ez != direct_url:
            candidates.append(ez)

    if d.startswith("10.1073/"):
        _add(f"https://www.pnas.org/doi/pdf/{doi}?download=true")
    if d.startswith("10.1038/"):
        _add("https://www.nature.com/articles/" + doi.split("/", 1)[-1] + ".pdf")
    if d.startswith("10.1126/"):
        _add(f"https://www.science.org/doi/pdf/{doi}")
    if d.startswith("10.1016/"):
        pii = doi.split("/", 1)[-1].upper().replace("-", "").replace("(", "").replace(")", "").replace(".", "")
        _add(f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?isDTMRedir=true&download=true")
    if d.startswith("10.1007/"):
        _add(f"https://link.springer.com/content/pdf/{doi}.pdf")
    return candidates

# ── _follow_to_pdf (shared by ebsco and libkey strategies) ───────────────────
def _follow_to_pdf(url, label, path, verbose=False, sess=None):
    """
    Follow a URL (with redirects) and try to save a PDF.
    If it lands on the EBSCO viewer, use the EBSCO download API.
    Returns the source URL string on success, None on failure.
    """
    if sess is None:
        sess = make_cookie_session("")

    def vlog(msg):
        if verbose:
            print(f"\n      [libkey] {msg}")

    try:
        _libkey_ftf = "libkey.io" in url and "full-text-file" in url
        if _libkey_ftf:
            r0 = sess.get(url, timeout=30, allow_redirects=False,
                          headers={"Referer": "https://libkey.io/",
                                   "Accept": "text/html,application/xhtml+xml,*/*"})
            vlog(f"{label} (no-redir) → {r0.url}  status={r0.status_code}  "
                 f"Location={r0.headers.get('Location','')}")
            loc = r0.headers.get("Location", "")
            if loc and r0.status_code in (301, 302, 303, 307, 308):
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

        # Landed on a PMC article page — try EuropePMC render
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

        # Landed on EBSCO
        if r.status_code == 200 and "research.ebsco.com" in r.url:
            cid, opid = None, None
            m = re.search(r"research\.ebsco\.com/c/([^/]+)/viewer/pdf/([^?/]+)", r.url)
            if m:
                opid, cid = m.group(1), m.group(2)

            if not cid and "text/html" in r.headers.get("content-type", ""):
                html = r.text
                om = re.search(r'research\.ebsco\.com/c/([^/?]+)', r.url)
                if om:
                    opid = om.group(1)

                vm = re.search(r'/viewer/pdf/([a-z0-9]{8,20})', html)
                if vm:
                    cid = vm.group(1)
                    vlog(f"found cid in viewer/pdf link: {cid!r}")

                if not cid:
                    sm = re.search(r'sourceRecordId=([a-z0-9]{8,20})', html)
                    if sm:
                        cid = sm.group(1)
                        vlog(f"found cid in sourceRecordId param: {cid!r}")

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

        # Landed on LibKey HTML page — probe for JSON
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
                    pdf_url = None
                    for key in ("fullTextFile", "pdfUrl", "url", "contentUrl",
                                "downloadUrl", "pdf", "link"):
                        m_j = re.search(rf'"{key}"\s*:\s*"([^"]+)"', rj.text)
                        if m_j:
                            pdf_url = m_j.group(1).replace("\\u002F", "/").replace("\\/", "/")
                            vlog(f"LibKey JSON field {key!r} → {pdf_url}")
                            break
                    if not pdf_url:
                        m_j = re.search(r'"(https://[^"]+\.pdf[^"]*)"', rj.text)
                        if m_j:
                            pdf_url = m_j.group(1).replace("\\/", "/")
                    if pdf_url:
                        result = _follow_to_pdf(pdf_url, f"{label}→libkey-json",
                                                path, verbose, sess)
                        if result:
                            return result
                else:
                    vlog(f"LibKey returned Ember.js SPA — requires browser execution to resolve")
            except Exception as e_j:
                vlog(f"LibKey JSON probe error: {e_j}")

        # Landed on Primo/ExLibris
        if (r.status_code == 200
                and "primo.exlibrisgroup.com" in r.url
                and "text/html" in r.headers.get("content-type", "")):
            vlog(f"Primo/ExLibris SPA ({len(r.content)} bytes) — requires JS, skipping")

        if (r.status_code == 200
                and "libkey.io" in r.url
                and "text/html" in r.headers.get("content-type", "")):
            html = r.text
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

        # 403: check body for "no subscription" message
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

        # 403: try EZProxy
        if r.status_code == 403 and r.url.startswith("http"):
            ez = ezproxy_url(r.url)
            if ez != r.url:
                vlog(f"403 from publisher, trying EZProxy: {ez}")
                try:
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
