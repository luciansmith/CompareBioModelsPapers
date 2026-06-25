"""
EBSCO Research full-text strategy (UW Library).

Queries EBSCO by DOI and/or PMID, then fetches the PDF via the v2-pdf
linkprocessor. Requires SESSION_ID + SESSION_MAP + EBSCO_AFFILIATION cookies
in cookies.txt (refresh ~every 28 h via Cookie-Editor on research.ebsco.com).
"""

import re

from strategy_utils import (
    make_cookie_session, _prompt_cookies_refresh, is_pdf,
)


def try_ebsco(pmid_numeric, doi, path, verbose=False):
    """
    Try EBSCO Research full-text search API (UW Library).
    Queries by DOI first, then PMID; fetches PDF via the v2-pdf linkprocessor
    only. Doesn't chase the item's other embedded links (those can lead into
    LibKey/Primo resolution, which the dedicated LibKey step already covers
    cleanly) — if EBSCO itself doesn't have the PDF, this returns None and
    lets the caller move on to the next strategy.
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
                try:
                    jdata = rs.json()
                    items = jdata.get("search", {}).get("items", [])
                    vlog(f"EBSCO search items={len(items)}")
                    if verbose and items:
                        # shortDbName/longDbName tells us which EBSCO database
                        # matched -- "cmedm"/MEDLINE is the bibliographic citation
                        # index (same metadata PubMed has), NOT a full-text
                        # platform, so it'll always 404 on v2-pdf even though the
                        # search "hit". holdingsAvailable + links are EBSCO's own
                        # signal for whether any actual full-text holding exists
                        # for this citation (possibly via a different database
                        # or an OpenURL-style resolver link) -- worth seeing in
                        # full since that's the one place a real access path
                        # would show up if there is one.
                        it0 = items[0]
                        vlog(f"  item[0] db={it0.get('shortDbName')}/{it0.get('longDbName')} "
                             f"holdingsAvailable={it0.get('holdingsAvailable')}")
                        vlog(f"  item[0] links={it0.get('links')}")
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

                vlog(f"EBSCO search cids={cids}")
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
                        elif verbose:
                            # Show the actual error body -- v2-pdf returns JSON
                            # even on failure, and the reason (no full text vs.
                            # not entitled vs. something else) is in there.
                            snippet = r2.content[:300].decode("utf-8", errors="replace")
                            vlog(f"  v2-pdf body: {snippet}")

                # EBSCO's own v2-pdf delivery is the only thing this strategy
                # uses. If it doesn't have the PDF, stop here — don't chase
                # the item's other embedded links (details API / providerLinks
                # etc.) through _follow_to_pdf, since those can wander into
                # LibKey/Primo resolution and duplicate work the dedicated
                # LibKey step already does cleanly.
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
