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
# Sentinel for "this 403 resolved to a domain in KNOWN_BLOCKED_PUBLISHER_DOMAINS
# (e.g. ScienceDirect) -- a confirmed 0%-success wall, not a subscription or
# cookie problem." Lets callers (e.g. libkey_strategy.py) tell this case apart
# from a real auth failure so they don't print a misleading "check your VPN /
# token" diagnostic when the actual blocker is the publisher's bot defense.
_KNOWN_BLOCKED = object()

# Set once we've shown the EZProxy-login-wall cookie refresh prompt, so a batch
# run doesn't print the same instructions once per paper.
_ezproxy_prompt_shown = False

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent
OUTPUT_DIR     = SCRIPT_DIR / "Biomodels papers"
COOKIES_FILE   = SCRIPT_DIR / "cookies.txt"
# Optional Semantic Scholar API key -- request one at
# https://www.semanticscholar.org/product/api#api-key-form (arrives by email).
# Drop the raw key (nothing else) into this file; it's read once at import
# time and, if present, attached as an x-api-key header on s2_session so S2
# rate-limits us against our own dedicated quota instead of the shared
# unauthenticated pool everyone else is also hammering. Not committed to git
# (same convention as cookies.txt).
S2_API_KEY_FILE = SCRIPT_DIR / "s2_api_key.txt"

UW_LIBKEY_ID  = "3478"
EMAIL         = "lpsmith@uw.edu"

NCBI_DELAY              = 0.4
GENERAL_DELAY           = 1.0
# Semantic Scholar's unauthenticated tier is shared across ALL unauthenticated
# callers worldwide and is much stricter than it looks -- hammering it with
# back-to-back DOI-lookup + title-search calls (one right after another, with
# no gap) reliably draws 429s within the first few papers of a batch. A
# bigger per-call delay here, plus the gentler retry policy below, is what
# actually avoids that instead of just retrying into more 429s.
SEMANTIC_SCHOLAR_DELAY  = 3.0

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
# Semantic Scholar specifically gets NO urllib3-level retries (total=0).
# Tuning backoff_factor up or down here was the wrong axis: any value still
# sleeps *silently inside* session.get(), invisible to vlog() -- a small
# factor just means it gives up on the rate limit sooner (more failures), a
# large factor means it waits longer with zero explanation in the log (the
# original "slept way longer than 3 seconds" complaint). Real fix: don't let
# urllib3 retry/sleep at all for S2; do it ourselves in s2_get() below, where
# every single wait gets a vlog() line explaining why and for how long.
_retry_s2 = Retry(total=0)

def _make_session(headers, retry=_retry):
    s = requests.Session()
    s.headers.update(headers)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s


def _load_s2_api_key():
    if not S2_API_KEY_FILE.exists():
        return None
    key = S2_API_KEY_FILE.read_text(encoding="utf-8", errors="replace").strip()
    return key or None


session      = _make_session(HEADERS)
ncbi_session = _make_session(NCBI_HEADERS)

_s2_api_key = _load_s2_api_key()
_s2_headers = dict(HEADERS)
if _s2_api_key:
    _s2_headers["x-api-key"] = _s2_api_key
HAS_S2_API_KEY = _s2_api_key is not None
s2_session = _make_session(_s2_headers, retry=_retry_s2)


# Commitment made in the Semantic Scholar API key application: "I will apply
# exponential backoff and similar strategies to help protect our systems from
# overloading." s2_get() below is that commitment in code -- do not change
# the backoff math to linear/constant/etc. without remembering that promise.
_S2_BACKOFF_BASE = 2.0  # seconds; wait doubles each retry: 2, 4, 8, 16, ...


def s2_get(url, params=None, timeout=20, vlog=None, max_retries=4):
    """
    GET against Semantic Scholar with a fully-visible exponential-backoff
    retry loop.

    S2's unauthenticated tier is shared globally and rate-limits hard;
    hitting a 429 here is routine, not exceptional. Every wait this function
    does -- whether from a 429/5xx response or a transient connection error
    -- gets a vlog() line first, so "why did this take so long" always has
    an answer in the debug output instead of being a silent urllib3 sleep.
    If S2 sends a Retry-After header, that takes precedence over our own
    backoff (the server is telling us exactly how long it wants us to wait);
    otherwise we double the wait each retry starting from
    _S2_BACKOFF_BASE (2s, 4s, 8s, 16s, ...).
    """
    def _vlog(msg):
        if vlog:
            vlog(msg)

    def _backoff(attempt):
        return _S2_BACKOFF_BASE * (2 ** attempt)

    for attempt in range(max_retries + 1):
        try:
            r = s2_session.get(url, params=params, timeout=timeout)
        except Exception as e:
            if attempt == max_retries:
                raise
            wait = _backoff(attempt)
            _vlog(f"request error ({e}); waiting {wait:.0f}s before retry "
                  f"{attempt + 1}/{max_retries} (exponential backoff) ...")
            time.sleep(wait)
            continue

        if r.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            retry_after = r.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else _backoff(attempt)
            except ValueError:
                wait = _backoff(attempt)
            reason = "Retry-After header" if retry_after else "exponential backoff"
            _vlog(f"status={r.status_code}; waiting {wait:.0f}s before retry "
                  f"{attempt + 1}/{max_retries} ({reason}) ...")
            time.sleep(wait)
            continue

        return r
    return r

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


def is_cloudflare_challenge(r):
    """
    Detect Cloudflare's managed JS bot-challenge ("Just a moment...") response.

    This is a real browser-JS challenge, not a User-Agent-sniffing block --
    confirmed by testing identical requests with different UAs and getting the
    same 403 either way. Not something a plain HTTP client can solve, and not
    something this script should try to solve (same policy as the JSTOR wall).
    Same detection signature already used in _try_ingenta_ahah_pdf(); factored
    out here so other strategies (e.g. semantic_scholar_strategy.py) can reuse
    it instead of getting an opaque, unexplained 403.
    """
    return (r.headers.get("cf-mitigated", "").lower() == "challenge"
            or (r.status_code == 403
                and b"challenges.cloudflare.com" in r.content[:8192]))


# Publishers whose bot defense has never once been beaten by any strategy in
# this project's history. download_tracking.json shows 0 of 157 attempted
# ScienceDirect/Elsevier (10.1016-DOI) papers have ever reached
# "downloaded", across every strategy tried (PMC, EBSCO, LibKey, Unpaywall,
# Semantic Scholar, Google Scholar, Direct DOI -- including the EZProxy
# fallback and the documented "/pdfft?isDTMRedir=true&download=true" force-
# download trick). The 403 survives even a cookie-loaded session with a
# real browser User-Agent, consistent with a behavioral/TLS-fingerprint bot
# defense (Akamai/PerimeterX-style) rather than a cookie or UA problem this
# script can fix. Treated the same way as a Cloudflare managed challenge:
# recognize it and flag for manual download immediately, don't keep
# spending requests/retries on it.
KNOWN_BLOCKED_PUBLISHER_DOMAINS = (
    "sciencedirect.com",
    "linkinghub.elsevier.com",
)


def is_known_blocked_publisher(url_or_domain):
    """True if url_or_domain falls under a publisher with a 0-success track
    record in this project (see KNOWN_BLOCKED_PUBLISHER_DOMAINS above)."""
    return any(d in url_or_domain.lower() for d in KNOWN_BLOCKED_PUBLISHER_DOMAINS)


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

# ── Primo/Alma OpenURL fallback (no JS execution) ────────────────────────────
#
# Primo VE's /discovery/openurl page is an Angular SPA — confirmed by testing
# directly: it returns "JavaScript must be enabled" regardless of query
# params, and an /openurl/<institution>/<vid> "legacy" path just redirects
# back into the same SPA on this Primo instance. So neither can be coaxed
# into a plain response.
#
# But Primo VE is just a front end for Alma, and Alma's own OpenURL resolver
# ("uresolver") lives on a separate, undocumented host — e.g.
# na01.alma.exlibrisgroup.com — and answers the *same* OpenURL query params
# with plain XML via svc_dat=CTO. Confirmed working, unauthenticated, against
# the live UW/Orbis Cascade Alliance system:
#
#   https://na01.alma.exlibrisgroup.com/view/uresolver/01ALLIANCE_UW/openurl
#       ?ID=pmid:9664759&genre=article&sid=EBSCO:MEDLINE&svc_dat=CTO
#
# The XML lists every context_service Alma considered. Each one carries a
# full_text_indicator and, for getFullTxt services, a current_access flag and
# (when filtered out) a Filter reason — e.g. "Date Filter" when the item's
# year falls in a gap in the subscription's date coverage. That's a precise,
# programmatic version of "I looked at the page and there's no PDF link":
# if every getFullTxt service is filtered out (or there are none at all),
# UW genuinely has no accessible full text for this item, full stop — no
# amount of retrying or JS-rendering would find one.
_ALMA_DOMAIN_HINTS = {
    "01ALLIANCE_UW": "na01.alma.exlibrisgroup.com",
}


def _discover_alma_domain(primo_domain, sess, verbose=False):
    """
    Fallback for institutions not in _ALMA_DOMAIN_HINTS (or if Ex Libris ever
    migrates UW to a different data center): Primo's own REST API 404s on a
    bare /primaws/rest/pub/openurl probe, but the error response's
    Report-To / Content-Security-Policy headers name the backing Alma host.
    """
    def vlog(msg):
        if verbose:
            print(f"\n      [primo] {msg}")
    try:
        r = sess.get(f"https://{primo_domain}/primaws/rest/pub/openurl", timeout=15)
        for hdr in ("report-to", "content-security-policy"):
            m = re.search(r'([a-z0-9.-]*\.alma\.exlibrisgroup\.com)',
                          r.headers.get(hdr, ""))
            if m:
                vlog(f"discovered Alma domain {m.group(1)} via {hdr} header")
                return m.group(1)
    except Exception as e:
        vlog(f"_discover_alma_domain error: {e}")
    return None


def _try_primo_alma_xml(discovery_url, sess, path, verbose=False):
    """
    Resolve a Primo /discovery/openurl SPA URL via Alma's uresolver XML API
    instead of executing JS. Returns a PDF path on success, _NO_SUBSCRIPTION
    if Alma confirms no accessible full text exists, or None if the lookup
    itself didn't work out (caller should fall back to other strategies).
    """
    from urllib.parse import urlsplit, parse_qsl, urlencode

    def vlog(msg):
        if verbose:
            print(f"\n      [primo] {msg}")

    parts = urlsplit(discovery_url)
    qs = dict(parse_qsl(parts.query, keep_blank_values=True))
    institution = qs.get("institution")
    if not institution:
        vlog("no institution= param in discovery URL — can't resolve via Alma")
        return None

    alma_domain = _ALMA_DOMAIN_HINTS.get(institution) or \
        _discover_alma_domain(parts.netloc, sess, verbose)
    if not alma_domain:
        vlog("couldn't determine the Alma delivery-network domain — skipping")
        return None

    alma_qs = dict(qs)
    alma_qs["svc_dat"] = "CTO"
    uresolver_url = f"https://{alma_domain}/view/uresolver/{institution}/openurl?{urlencode(alma_qs)}"

    try:
        rr = sess.get(uresolver_url, timeout=30, headers={"Referer": discovery_url})
        ct = rr.headers.get("content-type", "")
        vlog(f"uresolver CTO → status={rr.status_code}  ct={ct}  size={len(rr.content)}")
        if rr.status_code != 200 or "xml" not in ct:
            vlog(f"unexpected uresolver response; snippet: {rr.text[:300]}")
            return None

        xml = rr.text
        m = re.search(r'<key id="full_text_indicator">(true|false)</key>', xml)
        full_text = m.group(1) if m else None
        vlog(f"full_text_indicator={full_text}")

        svc_blocks = re.findall(
            r'<context_service\b[^>]*service_type="getFullTxt"[^>]*>(.*?)</context_service>',
            xml, re.DOTALL)
        vlog(f"getFullTxt context_service block(s): {len(svc_blocks)}")

        # current_access reflects Alma's own date-matching against each
        # package's coverage, which is occasionally wrong for backfill/rolling-
        # window packages (e.g. JSTOR) — so it's used only to *order* attempts,
        # not to exclude them. Filtered=true is the one unambiguous "Alma is
        # explicitly telling us this service doesn't apply" signal.
        usable_targets = []      # (is_current_access, resolution_url)
        any_unfiltered = False
        for blk in svc_blocks:
            cur = re.search(r'<key id="current_access">(true|false)</key>', blk)
            filt = re.search(r'<key id="Filtered">(true|false)</key>', blk)
            reason = re.search(r'<key id="Filter reason">([^<]*)</key>', blk)
            res = re.search(r'<resolution_url>([^<]+)</resolution_url>', blk)
            vlog(f"  service: current_access={cur.group(1) if cur else '?'}  "
                 f"filtered={filt.group(1) if filt else '?'}  "
                 f"reason={reason.group(1) if reason else ''}  "
                 f"resolution_url={res.group(1) if res else ''}")
            if filt and filt.group(1) == "true":
                continue  # explicitly excluded (e.g. a date-coverage gap)
            any_unfiltered = True
            if res:
                is_current = not cur or cur.group(1) == "true"
                usable_targets.append((is_current, res.group(1).replace("&amp;", "&")))

        if full_text == "false" or not any_unfiltered:
            vlog("Alma confirms no currently-accessible full text for this item "
                 "(every getFullTxt service is explicitly filtered out — e.g. a "
                 "subscription date-coverage gap) — treating as no_subscription")
            return _NO_SUBSCRIPTION

        # Try current_access=true services first, then the rest as fallback.
        usable_targets.sort(key=lambda t: not t[0])
        for _, target in usable_targets:
            result = _follow_to_pdf(target, "primo-alma-xml", path, verbose, sess=sess)
            if result and result is not _NO_SUBSCRIPTION:
                return result
    except Exception as e:
        vlog(f"uresolver error: {e}")

    return None


# ── Ingenta Edify "AHAH" two-hop PDF resolution ──────────────────────────────
#
# Some journal platforms (e.g. microbiologyresearch.org, "Ingenta Edify")
# don't expose a real PDF link in their static HTML — the article page's PDF
# button is a bare href="#" wired up to a Drupal AJAX behavior called "AHAH".
# Confirmed mechanics:
#   1. The page embeds a working locator URL for the *HTML* full-text
#      equivalent, e.g. /deliver/fulltext/micro/148/4/1481003a.html
#      ?itemId=...&mimeType=html&fmt=ahah — swap html→pdf in both the file
#      extension and mimeType= to get the PDF-variant locator.
#   2. Fetching that locator returns HTTP 200 whose body is a plain-text
#      docserver URL (not a redirect — this is the literal AHAH response
#      format a real browser's JS parses and navigates to).
#   3. Fetching that docserver URL is the actual PDF download.
#
# Confirmed (by live testing) that a clean 404 on step 3 is NOT always
# Cloudflare — even with a legitimate-looking, non-"guest" subscriber token,
# the docserver fetch can still 404. Most likely explanation: the real PDF
# button only works after the article page's own JS has run and set some
# client-side session state that never appears in a Set-Cookie header, so a
# plain HTTP fetch (no JS execution) can't reproduce it. This helper still
# does the legitimate two-hop lookup and real download, and distinguishes an
# explicit Cloudflare challenge (won't try to solve it) from this more
# ambiguous failure mode.
def _try_ingenta_ahah_pdf(html, base_url, sess, path, vlog):
    """
    Detect and resolve an Ingenta Edify "deliver/fulltext ... fmt=ahah" AJAX
    PDF locator embedded in an article page. Returns the docserver URL on
    success, None if no such pattern was found or the download didn't pan
    out (caller should fall back to other strategies either way).
    """
    from urllib.parse import urljoin

    try:
        m = re.search(
            r'["\']([^"\']*/deliver/fulltext/[^"\']*fmt=ahah[^"\']*)["\']',
            html, re.IGNORECASE,
        )
        if not m:
            return None
        locator = m.group(1).replace("&amp;", "&")
        pdf_locator = re.sub(r'\.html(?=\?)', '.pdf', locator, flags=re.IGNORECASE)
        pdf_locator = re.sub(r'mimeType=html', 'mimeType=pdf', pdf_locator, flags=re.IGNORECASE)
        pdf_locator = urljoin(base_url, pdf_locator)

        r1 = sess.get(pdf_locator, timeout=30, allow_redirects=True,
                      headers={"Referer": base_url,
                               "X-Requested-With": "XMLHttpRequest",
                               "Accept": "text/html, */*; q=0.01"})
        url_m = re.search(r'https?://\S+', r1.text.strip())
        if r1.status_code != 200 or not url_m:
            vlog(f"Ingenta AHAH: locator fetch failed (status={r1.status_code})")
            return None
        docserver_url = url_m.group(0).rstrip('"\'')

        r2 = sess.get(docserver_url, timeout=60, allow_redirects=True,
                      headers={"Referer": base_url,
                               "Accept": "application/pdf,*/*"})
        if r2.status_code == 200 and is_pdf(r2.content):
            path.write_bytes(r2.content)
            return docserver_url

        if (r2.headers.get("cf-mitigated", "").lower() == "challenge"
                or (r2.status_code == 403
                    and b"challenges.cloudflare.com" in r2.content[:8192])):
            vlog("Ingenta AHAH: docserver fetch blocked by a Cloudflare bot "
                 "challenge — same kind of wall as JSTOR, won't try to solve it.")
        else:
            vlog(f"Ingenta AHAH: docserver fetch failed (status={r2.status_code}) "
                 f"— likely needs page JS this script doesn't execute.")
        return None
    except Exception as e:
        vlog(f"Ingenta AHAH resolution error: {e}")
        return None


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
            vlog(f"Primo/ExLibris SPA ({len(r.content)} bytes) — resolving via Alma uresolver XML instead")
            result = _try_primo_alma_xml(r.url, sess, path, verbose)
            if result is _NO_SUBSCRIPTION:
                return _NO_SUBSCRIPTION
            if result:
                return result
            vlog("Alma uresolver lookup didn't resolve anything — giving up on this link")

        # Landed on JSTOR — confirmed (by direct testing) that JSTOR puts its
        # search/article pages behind bot-detection that 403s even a plain
        # browser-User-Agent request with no cookies at all. That's not a JS
        # rendering problem we can route around server-side; it's a deliberate
        # anti-automation check, and this script won't try to defeat it. Flag
        # it clearly and fall through so other strategies (or a manual JSTOR
        # search) get a chance instead.
        if "jstor.org" in r.url:
            vlog(f"Landed on JSTOR ({r.url[:100]}) — JSTOR blocks automated "
                 f"requests with bot-detection (status={r.status_code}); this "
                 f"needs a manual lookup on jstor.org, skipping.")

        # Ingenta Edify platforms (e.g. microbiologyresearch.org) hide the
        # real PDF link behind a Drupal AJAX "AHAH" locator rather than a
        # static href — try that before giving up on a direct 200 HTML page.
        if (r.status_code == 200
                and "text/html" in r.headers.get("content-type", "")
                and not is_pdf(r.content)):
            result = _try_ingenta_ahah_pdf(r.text, r.url, sess, path, vlog)
            if result:
                return result

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

        # 403 (or a 200 that's really just a no-access page): check body /
        # URL for a "no subscription" signal.
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

        # Silverchair-platform journals (Oxford UP, Royal Society Publishing,
        # many society journals) silently redirect a fulltext URL to an
        # "article-abstract" page when the visiting session has no
        # entitlement to the full text -- status 200, no 403, no obvious
        # error, just a different page with "?redirectedFrom=fulltext" still
        # in the query string. Confirmed directly: DOI 10.1098/rspb.1995.0153
        # via EZProxy landed on exactly this URL shape. Treat it as a
        # conclusive no-subscription signal instead of silently falling
        # through every remaining candidate (libkeyContentLocation /
        # full-text-file are Ember SPA shells for this same article and can
        # never succeed either).
        if (r.status_code == 200 and "article-abstract" in r.url
                and "redirectedFrom=fulltext" in r.url):
            vlog(f"landed on an article-abstract page redirected away from "
                 f"fulltext -- no institutional access to this content: {r.url[:100]}")
            return _NO_SUBSCRIPTION

        if (r.status_code == 200 and r.content
                and "text/html" in r.headers.get("content-type", "")
                and not is_pdf(r.content)):
            _body_lower = r.content[:32768].decode("utf-8", errors="replace").lower()
            if any(p in _body_lower for p in _deny_phrases):
                vlog(f"200 body indicates no institutional subscription: {r.url[:80]}")
                return _NO_SUBSCRIPTION

        # 403: try EZProxy -- unless this is a publisher with a confirmed 0%
        # success rate (ScienceDirect/Elsevier etc.), in which case EZProxy
        # against this exact URL has already been proven pointless and isn't
        # worth another request. This only stops *this* candidate URL --
        # other candidates in the caller's loop (e.g. libkeyContentLocation,
        # full-text-file) still get tried normally.
        if r.status_code == 403 and r.url.startswith("http") and is_known_blocked_publisher(r.url):
            from urllib.parse import urlparse as _urlparse
            vlog(f"known-blocked publisher ({_urlparse(r.url).netloc}) -- won't "
                 f"try EZProxy against this exact URL (0% historical success); "
                 f"this paper will need manual browser-based access: {r.url}")
            return _KNOWN_BLOCKED

        # 403 + Cloudflare managed challenge ("Just a moment...", cf-mitigated:
        # challenge): confirmed via a live test against Wiley (PMID 15300679)
        # that this fires even with UW full-tunnel VPN connected and even
        # though a real browser on the same VPN/IP sails straight through with
        # no NetID/Duo login at all -- the access itself isn't the problem,
        # solving Cloudflare's JS challenge is, and a plain HTTP client just
        # can't do that (no JS engine). Trying EZProxy next would either hit
        # the exact same Cloudflare challenge again (EZProxy forwards to this
        # same publisher) or, worse, surface a confusing "EZProxy session
        # expired, log in with Duo" prompt that sends the user chasing the
        # wrong fix -- the real fix is just opening the link in an actual
        # browser, which is exactly what already worked. Treat it the same as
        # a known-blocked publisher: stop on this URL, let other candidates
        # (libkeyContentLocation, full-text-file) still get a try.
        if r.status_code == 403 and is_cloudflare_challenge(r):
            vlog(f"Cloudflare managed JS challenge on {r.url[:100]} -- not "
                 f"solvable by a plain HTTP client (direct or via EZProxy); "
                 f"needs manual browser-based access instead")
            return _KNOWN_BLOCKED
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
                            vlog(f"EZProxy login wall — visit {ez} to authenticate")
                            global _ezproxy_prompt_shown
                            if not _ezproxy_prompt_shown:
                                _ezproxy_prompt_shown = True
                                _prompt_cookies_refresh(
                                    "UW EZProxy (off-campus library access)",
                                    ez,
                                    "That URL is the *proxied* article link, not the bare\n"
                                    "       offcampus.lib.washington.edu front door — visiting the front\n"
                                    "       door with no target resource just bounces you to lib.uw.edu\n"
                                    "       and won't create a real EZProxy session. Log in with your UW\n"
                                    "       NetID (Duo) on the link above, then once you land on the actual\n"
                                    "       article page, use Cookie-Editor to export cookies for whichever\n"
                                    "       *.offcampus.lib.washington.edu subdomain is in the address bar\n"
                                    "       at that point (it'll be a host-rewritten name like\n"
                                    "       some-publisher-com.offcampus.lib.washington.edu — NOT\n"
                                    "       lib.uw.edu, which is just the public marketing site and won't\n"
                                    "       carry the authenticated session). Append those to cookies.txt.",
                                )
                        elif r_ez.status_code == 200 and is_pdf(r_ez.content):
                            path.write_bytes(r_ez.content)
                            return ez_final
                        elif r_ez.status_code == 200:
                            if ("article-abstract" in ez_final
                                    and "redirectedFrom=fulltext" in ez_final):
                                vlog(f"EZProxy landed on an article-abstract page "
                                     f"redirected away from fulltext -- no "
                                     f"institutional access to this content: "
                                     f"{ez_final[:100]}")
                                return _NO_SUBSCRIPTION
                            if (r_ez.content
                                    and "text/html" in r_ez.headers.get("content-type", "")):
                                _ez_body_lower = r_ez.content[:32768].decode(
                                    "utf-8", errors="replace").lower()
                                if any(p in _ez_body_lower for p in _deny_phrases):
                                    vlog(f"EZProxy 200 body indicates no "
                                         f"institutional subscription: {ez_final[:80]}")
                                    return _NO_SUBSCRIPTION
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

                            result = _try_ingenta_ahah_pdf(r_ez.text, ez_final, ez_sess, path, vlog)
                            if result:
                                return result

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
