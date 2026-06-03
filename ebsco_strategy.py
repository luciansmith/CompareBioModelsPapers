"""
EBSCO Research full-text strategy (UW Library).

Queries EBSCO by DOI and/or PMID, then fetches the PDF via the v2-pdf
linkprocessor. Requires SESSION_ID + SESSION_MAP + EBSCO_AFFILIATION cookies
in cookies.txt (refresh ~every 28 h via Cookie-Editor on research.ebsco.com).
"""

import re

from strategy_utils import (
    make_cookie_session, _follow_to_pdf, _prompt_cookies_refresh,
    is_pdf, _NO_SUBSCRIPTION,
)


def try_ebsco(pmid_numeric, doi, path, verbose=False):
    """
    Try EBSCO Research full-text search API (UW Library).
    Queries by DOI first, then PMID; fetches PDF via the v2-pdf linkprocessor.
    """
    if not pmid_numeric and not doi:
        return None

    def vlog(msg):
        if verbose:
            print(f"\n      [ebsco] {msg}")

    EBSCO_OPID = "2onyl7"

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
                        print(f"      [ebsco] EBSCO first item snippet: {__import__('json').dumps(items[0])[:600]}")
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

                # v2-pdf unavailable — try the details API
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
                    break
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
