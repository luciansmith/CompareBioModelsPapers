"""
Semantic Scholar open-access PDF strategy.

Queries the Semantic Scholar API (no key required for moderate rates).
Complements Unpaywall — indexes preprints, repositories, and some hosted
PDFs that Unpaywall misses.
"""

from strategy_utils import session, is_pdf


def try_semantic_scholar(doi, title, path, verbose=False):
    """
    Query the Semantic Scholar API for an open-access PDF.

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
                doi_norm = (doi or "").lower()
                for item in items:
                    item_doi = (item.get("externalIds") or {}).get("DOI", "").lower()
                    oa = item.get("openAccessPdf") or {}
                    if oa.get("url") and (not doi_norm or item_doi == doi_norm):
                        pdf_url = oa["url"]
                        vlog(f"selected: {item.get('title','')!r} → {pdf_url}")
                        break
                if not pdf_url:
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
