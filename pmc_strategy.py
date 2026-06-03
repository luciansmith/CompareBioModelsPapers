"""
PubMed Central (PMC) download strategy.

Tries in order:
  0. NCBI efetch (direct PDF via E-utilities)
  1. PMC OA API → direct PDF or tgz package
  2. Scrape article page for PDF link
  3. Poll /pdf/ URL
  4. Europe PMC render endpoints
"""

import re
import time

from strategy_utils import (
    NCBI_DELAY, EMAIL, HEADERS,
    _retry_no500,
    ncbi_session, session,
    make_cookie_session,
    is_pdf, find_pdf_urls_in_html,
    _prompt_cookies_refresh,
)

try:
    from requests.adapters import HTTPAdapter
except ImportError:
    pass


def _is_recaptcha_wall(response):
    """Return True if the response is a Google reCAPTCHA bot-check page."""
    if response.status_code != 200:
        return False
    ct = response.headers.get("content-type", "")
    if "text/html" not in ct:
        return False
    return b"recaptcha/challengepage" in response.content[:4096]


def _poll_pmc_pdf(url, path, sess, vlog, max_retries=8, poll_delay=5):
    """
    Fetch a PMC PDF URL, retrying if it returns a 'preparing PDF' HTML page.
    Returns the URL on success, None on failure, or "captcha" if blocked.
    """
    _last_size = -1
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
                break
            if "text/html" in r.headers.get("content-type", "") and len(r.content) < 10_000:
                snippet = r.content[:4096].lower()
                _wall_phrases = [b"log in", b"sign in", b"subscription",
                                 b"access denied", b"not available",
                                 b"requires a subscription", b"purchase"]
                if any(p in snippet for p in _wall_phrases):
                    vlog(f"access wall detected ({len(r.content)} bytes), stopping poll")
                    break
                if attempt > 0 and len(r.content) == _last_size:
                    vlog(f"static HTML response ({len(r.content)} bytes) repeated, stopping poll")
                    break
            _last_size = len(r.content)
            if attempt < max_retries - 1:
                time.sleep(poll_delay)
        except Exception as e:
            vlog(f"poll error: {e}")
            break
    return None


def try_pmc(pmcid, path, verbose=False):
    """
    Try to download from PubMed Central using several approaches:
    0. NCBI efetch (direct PDF via E-utilities)
    1. PMC OA API  → gives the actual PDF filename (most reliable for OA papers)
    2. Scrape the PMC article page for the PDF link
    3. Generic /pdf/ URL (follows redirects)
    4. Europe PMC direct render
    """
    pmc_full = pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
    pmc_num  = pmc_full[3:]
    PMC_BASE = "https://pmc.ncbi.nlm.nih.gov"

    def vlog(msg):
        if verbose:
            print(f"\n      [pmc] {msg}")

    def _pmc_pdf_candidates_from_html(html, page_url):
        candidates = find_pdf_urls_in_html(html, page_url)
        extra = re.findall(
            rf'(/articles/{re.escape(pmc_full)}/pdf/[^\s"\'<>\\]+\.pdf)',
            html, re.IGNORECASE,
        )
        for p in extra:
            full = PMC_BASE + p
            if full not in candidates:
                candidates.append(full)
        return candidates

    # 0. NCBI efetch -----------------------------------------------------------
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

    # 1. PMC OA API ------------------------------------------------------------
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

    # 2 + 3. Article page scrape and /pdf/ polling
    pmc_session = make_cookie_session("pmc.ncbi.nlm.nih.gov")
    _captcha = False

    # 2. Scrape article page --------------------------------------------------
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

    # 3. Poll /pdf/ -----------------------------------------------------------
    if _captcha:
        vlog("skipping /pdf/ poll — already captcha-blocked")
    else:
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
    from requests.adapters import HTTPAdapter
    from strategy_utils import _make_session
    eu_session = _make_session(HEADERS)
    eu_session.mount("https://", HTTPAdapter(max_retries=_retry_no500))
    eu_session.mount("http://",  HTTPAdapter(max_retries=_retry_no500))

    for eu_url in [
        f"https://europepmc.org/api/getPdf?pmcid={pmc_full}",
        f"https://europepmc.org/articles/{pmc_full}?pdf=render",
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
