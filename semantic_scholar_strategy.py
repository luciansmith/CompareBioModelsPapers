"""
Semantic Scholar open-access PDF strategy.

Queries the Semantic Scholar API (no key required for moderate rates).
Complements Unpaywall — indexes preprints, repositories, and some hosted
PDFs that Unpaywall misses.
"""

import difflib
import re
import time
from urllib.parse import urlparse

from strategy_utils import (
    s2_session as session, s2_get, is_pdf, is_cloudflare_challenge,
    find_pdf_urls_in_html, PUBLISHER_PDF_PATTERNS,
    make_cookie_session, HAS_BROWSER_COOKIES,
    SEMANTIC_SCHOLAR_DELAY, HAS_S2_API_KEY,
    is_known_blocked_publisher,
)


def _norm_title(t):
    return re.sub(r'[^a-z0-9]+', ' ', (t or '').lower()).strip()


def _titles_match(a, b, threshold=0.88):
    """
    Fuzzy title match used to gate which title-search result we trust.
    S2's /paper/search is a relevance search, not an exact-title lookup --
    it can (and does) return a completely unrelated paper ranked above or
    alongside the real one (e.g. searching "Mathematical models of purine
    metabolism in man." returned a Euclid space-telescope paper as result #2
    because it merely scored as topically "relevant" text-wise). Blindly
    taking "the first result that happens to have an OA PDF" -- regardless
    of whether its title has anything to do with the query -- silently
    downloads the wrong paper. Requiring a high title-similarity ratio
    keeps the minor-punctuation/whitespace tolerance we want without
    accepting an unrelated match.
    """
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def try_semantic_scholar(doi, title, path, verbose=False, pmid=None):
    """
    Query the Semantic Scholar API for an open-access PDF.

    Tries DOI lookup first, then PMID lookup -- both exact, conclusive
    identifier lookups via S2's /paper/{id} endpoint (PMID: is a supported
    prefix per S2's own OpenAPI spec, same as DOI: and ARXIV:). Falls back
    to fuzzy title search only if neither identifier resolved the paper,
    since title search is a relevance search that has previously matched
    the wrong paper entirely.

    Returns (source_url_or_None, manual_fallback_url). The manual fallback
    is S2's "openAccessPdf" landing-page URL whenever one was found but
    couldn't be turned into an actual downloaded PDF (Cloudflare wall, known-
    blocked publisher, or just not a direct PDF link) -- still a useful link
    for a human to open in a real browser, same idea as direct_doi_strategy's
    manual_url.
    """
    def vlog(msg):
        if verbose:
            print(f"\n      [s2] {msg}")

    vlog(f"using {'authenticated' if HAS_S2_API_KEY else 'unauthenticated'} S2 API access")

    pdf_url = None
    resolved = False      # True once S2 has conclusively identified this exact
                           # paper -- via DOI or PMID lookup, status 200 --
                           # regardless of whether it has an OA PDF. Title search
                           # has nothing more to learn at that point, and it's
                           # the exact mechanism that previously matched a wrong
                           # paper (the Euclid/purine mixup).
    request_made = False  # whether we've already made an S2 API call this run,
                           # so we know to space the next one out -- back-to-back
                           # calls with no gap is exactly what draws 429s from
                           # Semantic Scholar's shared unauthenticated rate limit.

    def _identifier_lookup(prefix, value, label):
        nonlocal pdf_url, resolved, request_made
        if request_made:
            vlog(f"sleeping {SEMANTIC_SCHOLAR_DELAY}s before {label} lookup ...")
            time.sleep(SEMANTIC_SCHOLAR_DELAY)
        try:
            r = s2_get(
                f"https://api.semanticscholar.org/graph/v1/paper/{prefix}:{value}",
                params={"fields": "openAccessPdf,externalIds,title"},
                timeout=20,
                vlog=vlog,
            )
            request_made = True
            vlog(f"{label} lookup status={r.status_code} ct={r.headers.get('content-type','')}")
            if r.status_code == 200:
                resolved = True
                data = r.json()
                vlog(f"title={data.get('title','')!r}")
                oa = data.get("openAccessPdf") or {}
                pdf_url = oa.get("url")
                vlog(f"openAccessPdf={oa}")
            else:
                vlog(f"{label} lookup body: {r.text[:200]}")
        except Exception as e:
            request_made = True
            vlog(f"{label} lookup error: {e}")

    # ── Pass 1: DOI lookup ────────────────────────────────────────────────────
    if doi:
        _identifier_lookup("DOI", doi, "DOI")
    else:
        vlog("no DOI available -- skipping DOI lookup")

    # ── Pass 1b: PMID lookup ──────────────────────────────────────────────────
    # Just as conclusive as DOI lookup (S2 supports PMID: as a paper-id prefix),
    # and many PMC/PubMed records that lack a DOI still have a PMID -- e.g.
    # PMID 2732237 (DOI ---). Worth trying before the riskier, rate-limit-
    # hungrier title search.
    if not resolved and pmid:
        _identifier_lookup("PMID", pmid, "PMID")
    elif not resolved:
        vlog("no PMID available either -- falling back to title search")

    # ── Pass 2: title search fallback ─────────────────────────────────────────
    # Skip entirely if DOI or PMID lookup already gave a conclusive answer
    # (found the paper, whether or not it had an OA PDF) -- nothing left to
    # learn, and title search only adds 429 risk plus a chance of matching the
    # wrong paper for no benefit.
    if not pdf_url and title and not resolved:
        if request_made:
            vlog(f"sleeping {SEMANTIC_SCHOLAR_DELAY}s before title search ...")
            time.sleep(SEMANTIC_SCHOLAR_DELAY)
        try:
            r = s2_get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": title, "fields": "openAccessPdf,externalIds,title",
                        "limit": 5},
                timeout=20,
                vlog=vlog,
            )
            vlog(f"title search status={r.status_code}")
            if r.status_code == 200:
                items = r.json().get("data", [])
                vlog(f"title search hits={len(items)}")
                for item in items:
                    vlog(f"  candidate title={item.get('title','')!r}  "
                         f"doi={item.get('externalIds',{}).get('DOI','')!r}  "
                         f"oa={item.get('openAccessPdf')}")
                doi_norm = (doi or "").lower()
                for item in items:
                    item_doi = (item.get("externalIds") or {}).get("DOI", "").lower()
                    oa = item.get("openAccessPdf") or {}
                    if not oa.get("url"):
                        continue
                    if doi_norm and item_doi == doi_norm:
                        pdf_url = oa["url"]
                        vlog(f"selected (DOI match): {item.get('title','')!r} → {pdf_url}")
                        break
                    if _titles_match(title, item.get("title", "")):
                        pdf_url = oa["url"]
                        vlog(f"selected (title match): {item.get('title','')!r} → {pdf_url}")
                        break
                if not pdf_url:
                    # Deliberately NOT falling back to "first result with any OA
                    # PDF" -- that's what previously grabbed an unrelated paper
                    # (e.g. a Euclid space-telescope paper for a purine-metabolism
                    # query) just because it happened to have an open PDF while
                    # the real match was closed-access. No DOI match and no
                    # title-similar match means we don't have a confident result.
                    vlog("no DOI or title match among results with an OA PDF -- not guessing")
        except Exception as e:
            vlog(f"title search error: {e}")

    if not pdf_url:
        if resolved:
            vlog("DOI/PMID lookup resolved this paper but it has no OA PDF -- "
                 "skipping title search (nothing more to learn, no point risking a wrong match)")
        vlog("no OA PDF found")
        return None, None

    try:
        vlog(f"fetching {pdf_url}")
        r2 = session.get(pdf_url, timeout=60, allow_redirects=True)
        vlog(f"fetch status={r2.status_code} is_pdf={is_pdf(r2.content)}")
        if r2.status_code == 200 and is_pdf(r2.content):
            path.write_bytes(r2.content)
            return pdf_url, None
        if is_cloudflare_challenge(r2):
            # "bronze" OA links from S2 are often just the publisher's DOI/
            # landing-page URL rather than a direct PDF. When that publisher
            # sits behind Cloudflare's managed JS challenge (e.g. the
            # Microbiology Society's microbiologyresearch.org), a plain HTTP
            # client can't get past it -- same wall as JSTOR/Ingenta AHAH.
            # Report it clearly instead of leaving an unexplained 403.
            vlog("blocked by a Cloudflare bot challenge -- won't try to solve it; "
                 "this paper will need manual browser-based access")
            return None, pdf_url
        if is_known_blocked_publisher(r2.url):
            # ScienceDirect/Elsevier etc. -- 0 successes out of 157 attempted
            # papers in this project's history, via every strategy this
            # pipeline has, EZProxy included. Not worth spending a cookie-
            # session fetch, a publisher-pattern guess, and an HTML scrape on
            # a wall with a perfect 0% track record. Recognize it and bail
            # immediately, same philosophy as the Cloudflare-challenge case.
            vlog(f"known-blocked publisher ({urlparse(r2.url).netloc}) -- "
                 f"won't try to bypass it; this paper will need manual "
                 f"browser-based access: {r2.url}")
            return None, r2.url
        if r2.status_code == 200:
            # Not a Cloudflare wall or a known-blocked publisher either --
            # S2's "openAccessPdf" url is often just the publisher's article
            # landing page (e.g. a bare doi.org redirect), not a direct PDF
            # link. Try the known publisher PDF-URL pattern first (the same
            # one direct_doi_strategy uses), then fall back to scraping the
            # page for an embedded link in case it's some other, simpler
            # template.
            #
            # The plain s2_session has browser-like headers but no real
            # cookies, and some publisher download endpoints 403 cookie-less
            # requests as bot traffic even with a convincing User-Agent. Use
            # the same cookie-loaded session direct_doi_strategy relies on --
            # this is also what lets a UW-subscribed/EZProxy session through.
            # See direct_doi_strategy.py's matching comment: make_cookie_session()
            # always loads cookies.txt on its own; gating the call itself on
            # HAS_BROWSER_COOKIES meant cookies.txt was silently never used
            # whenever the optional browser_cookie3 package wasn't installed.
            cookie_domain = urlparse(r2.url).netloc
            cookie_sess = make_cookie_session(cookie_domain)

            tried_urls = set()

            def _try(cand_url, how):
                if not cand_url or cand_url in tried_urls:
                    return None
                tried_urls.add(cand_url)
                try:
                    r3 = cookie_sess.get(cand_url, timeout=60, allow_redirects=True,
                                      headers={"Accept": "application/pdf,*/*",
                                               "Referer": r2.url})
                    vlog(f"not a PDF -- {how} -> {cand_url}  "
                         f"status={r3.status_code}  is_pdf={is_pdf(r3.content)}")
                    if r3.status_code == 200 and is_pdf(r3.content):
                        path.write_bytes(r3.content)
                        return cand_url
                except Exception as e:
                    vlog(f"  -> {cand_url}  error: {e}")
                return None

            for domain, pdf_fn in PUBLISHER_PDF_PATTERNS.items():
                if domain in r2.url:
                    try:
                        pattern_url = pdf_fn(r2.url, doi)
                    except Exception:
                        pattern_url = None
                    hit = _try(pattern_url, "publisher pattern")
                    if hit:
                        return hit, None
                    break

            candidates = find_pdf_urls_in_html(r2.text, r2.url)
            vlog(f"landing page scrape found {len(candidates)} candidate PDF link(s)")
            for cand_url in candidates[:5]:
                hit = _try(cand_url, "scraped link")
                if hit:
                    return hit, None
    except Exception as e:
        vlog(f"PDF fetch error: {e}")
    return None, pdf_url
