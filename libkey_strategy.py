"""
LibKey / ThirdIron (UW Library) download strategy.

Queries the ThirdIron public and authenticated v2 APIs by PMID and DOI,
then follows the returned fullTextFile / contentLocation URL through auth
redirects, handling the EBSCO viewer if needed.
Requires UW VPN in full-tunnel mode.
"""

import re

from strategy_utils import (
    UW_LIBKEY_ID, HAS_BROWSER_COOKIES,
    make_cookie_session, _follow_to_pdf, _prompt_cookies_refresh,
    _NO_SUBSCRIPTION,
)

try:
    import browser_cookie3
except ImportError:
    browser_cookie3 = None

_thirdiron_library_token_cache: dict = {}


def _get_thirdiron_library_token(vlog=None):
    """
    Extract the ThirdIron library Bearer token from the LibKey Ember.js JS bundle.
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
        bundle_urls = re.findall(
            r'(?:src|href)=["\']([^"\']+\.js(?:\?[^"\']*)?)["\']', r.text)
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
                jwt_re = r'[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'
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


def try_libkey(pmid_numeric, doi, path, verbose=False):
    """
    Try UW Library's LibKey / ThirdIron link resolver.
    Requires UW VPN in full-tunnel mode.
    """
    if not pmid_numeric and not doi:
        return None

    def vlog(msg):
        if verbose:
            print(f"\n      [libkey] {msg}")

    TI_BASE = "https://api.thirdiron.com/v2"
    THIRDIRON_PUBLIC = f"https://api.thirdiron.com/public/v1/libraries/{UW_LIBKEY_ID}"

    ti_targets = []
    if pmid_numeric:
        ti_targets += [
            (f"{THIRDIRON_PUBLIC}/articles/pmid:{pmid_numeric}",
             "thirdiron-pub-pmid"),
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

    _lib_ti_labels = {lbl for _, lbl in ti_targets
                      if f"/libraries/{UW_LIBKEY_ID}/" in _ or "pub" in lbl}

    ti_sess = make_cookie_session("thirdiron.com")
    if HAS_BROWSER_COOKIES and browser_cookie3:
        for loader in (browser_cookie3.chrome, browser_cookie3.firefox):
            try:
                ti_sess.cookies.update(loader(domain_name="libkey.io"))
                break
            except Exception:
                pass

    _ti_lib_token, _ti_lib_token_type = _get_thirdiron_library_token(vlog=vlog)
    if _ti_lib_token:
        vlog(f"Using extracted ThirdIron library token (length={len(_ti_lib_token)})")

    _lib_auth_failures = 0

    for ti_url, ti_label in ti_targets:
        try:
            ti_headers = {"Referer": f"https://libkey.io/libraries/{UW_LIBKEY_ID}/",
                          "Origin": "https://libkey.io"}
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

            if ti_article_id:
                ftf = (f"https://libkey.io/libraries/{UW_LIBKEY_ID}"
                       f"/articles/{ti_article_id}/full-text-file"
                       f"?utm_source=api_{UW_LIBKEY_ID}&allow_speedbump=true")
                if ftf not in [u for u, _ in candidates]:
                    candidates.append((ftf, f"{ti_label}-libkey-ftf"))

            for url, label in candidates:
                result = _follow_to_pdf(url, label, path, verbose, sess=ti_sess)
                if result is _NO_SUBSCRIPTION:
                    return _NO_SUBSCRIPTION
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
