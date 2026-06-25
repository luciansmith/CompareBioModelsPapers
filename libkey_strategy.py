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
    make_cookie_session, _follow_to_pdf,
    _NO_SUBSCRIPTION, _KNOWN_BLOCKED,
)

try:
    import browser_cookie3
except ImportError:
    browser_cookie3 = None

_thirdiron_library_token_cache: dict = {}

# Shown at most once per process: the big "every library-scoped call
# returned 401" banner used to print in full for every single paper in a
# batch, since the diagnosis is identical every time (it's a property of
# this UW LibKey instance, not of any one paper). Print it once, then stay
# quiet about it for the rest of the run.
_token_diag_shown = False


def _get_thirdiron_library_token(vlog=None):
    """
    Mint a ThirdIron library Bearer token the same way the libkey.io SPA
    itself does on every route transition (Ember route beforeModel ->
    authenticateLibrary -> attemptDirectLibraryAuth -> storeTokenForLibrary).

    Confirmed via live DevTools capture (console.trace on the
    localStorage.setItem call, then inspecting the resulting Network
    request) that the token is NOT a static string anywhere in the JS
    bundles -- that's why scanning bundle text for a UUID/JWT-shaped string
    (the previous approach here) could never find anything, no matter which
    bundles got fetched. It's minted fresh by a plain CORS POST that sends
    no Authorization header of its own:

        POST https://api.thirdiron.com/v2/api-tokens
        Origin: https://libkey.io
        Referer: https://libkey.io/
        Content-Type: application/json; charset=UTF-8
        {"libraryId": "<id>", "returnPreproxy": true, "client": "bzweb",
         "failure": "/token-failure/<id>",
         "success": "/libraries/<id>/accept-token?intent=<base64 noop>"}

        -> 200 {"api-tokens": [{"id": "<uuid>", "expires_at": "...", ...}]}

    The response's access-control-allow-origin etc. headers only matter to
    an actual browser enforcing CORS; a plain HTTP client isn't subject to
    CORS at all, so this is callable directly with no JS execution needed.
    Origin/Referer are sent anyway in case the server checks them itself
    rather than relying solely on browser-side CORS enforcement.

    Returns (token_string, "Bearer") or (None, None) on failure. Cached for
    the lifetime of the process -- the minted token's observed expires_at is
    about three weeks out, comfortably longer than any single batch run, so
    one mint per run is enough.
    """
    if _thirdiron_library_token_cache.get("attempted"):
        return (_thirdiron_library_token_cache.get("token"),
                _thirdiron_library_token_cache.get("type"))
    _thirdiron_library_token_cache["attempted"] = True

    sess = make_cookie_session("libkey.io")
    try:
        r = sess.post(
            "https://api.thirdiron.com/v2/api-tokens",
            json={
                "libraryId": UW_LIBKEY_ID,
                "returnPreproxy": True,
                "client": "bzweb",
                "failure": f"/token-failure/{UW_LIBKEY_ID}",
                "success": (f"/libraries/{UW_LIBKEY_ID}/accept-token"
                            f"?intent=eyJ1cmwiOiIvbGlicmFyaWVzL3VuZGVmaW5lZC"
                            f"91bmRlZmluZWQifQ%3D%3D"),
            },
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "Accept": "*/*",
                "Origin": "https://libkey.io",
                "Referer": "https://libkey.io/",
            },
            timeout=20,
        )
        if vlog:
            vlog(f"POST /v2/api-tokens status={r.status_code}  "
                 f"ct={r.headers.get('content-type','')}")
        if r.status_code != 200:
            if vlog:
                vlog(f"api-tokens body: {r.text[:300]!r}")
            return None, None
        data = r.json()
        entries = data.get("api-tokens") or []
        if not entries or not entries[0].get("id"):
            if vlog:
                vlog(f"api-tokens response had no usable entry: {data!r}")
            return None, None
        token = entries[0]["id"]
        expires_at = entries[0].get("expires_at")
        if vlog:
            vlog(f"minted ThirdIron library token (expires_at={expires_at})")
        _thirdiron_library_token_cache["token"] = token
        _thirdiron_library_token_cache["type"] = "Bearer"
        _thirdiron_library_token_cache["expires_at"] = expires_at
        return token, "Bearer"
    except Exception as e:
        if vlog:
            vlog(f"_get_thirdiron_library_token (api-tokens POST) error: {e}")
    return None, None


def try_libkey(pmid_numeric, doi, path, verbose=False):
    """
    Try UW Library's LibKey / ThirdIron link resolver.
    Requires UW VPN in full-tunnel mode.

    Returns (result, manual_candidates):
      - result is a downloaded-PDF source URL on success, _NO_SUBSCRIPTION if
        UW's library confirms no access, or None if nothing downloaded.
      - manual_candidates is a list of the ThirdIron/LibKey URLs that
        resolved to a real article but hit a confirmed dead-end an HTTP
        client can't get past (known-blocked publisher or a Cloudflare bot
        challenge -- see _KNOWN_BLOCKED in strategy_utils.py). These are the
        *entry* links (contentLocation / fullTextFile / full-text-file),
        i.e. exactly what a human would click -- confirmed by direct testing
        that following one of these by hand in a real browser, through its
        redirect chain, lands on the actual publisher PDF (a real browser
        passes the Cloudflare challenge / institutional auth that this
        script's plain HTTP client can't). Empty list if no candidate hit
        that case.
    """
    if not pmid_numeric and not doi:
        return None, []

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
    else:
        vlog("no ThirdIron library token extracted -- the "
             "/libraries/{id}/-scoped calls below will go out with no "
             "Authorization header at all, which alone is enough to "
             "produce a 401 regardless of cookies or VPN")

    _lib_auth_failures = 0
    _hit_known_blocked = False  # True once any candidate resolved to a known-
                                 # blocked publisher (ScienceDirect etc.) -- the
                                 # real reason this paper needs manual download
                                 # in that case, independent of the 401s below.
    _manual_candidates = []  # the original candidate entry URLs (libkeyContent-
                              # Location / fullTextFile / full-text-file) for
                              # every candidate that hit _KNOWN_BLOCKED -- these
                              # are exactly what a human would click, and
                              # confirmed by direct testing to get past the same
                              # Cloudflare challenge / institutional auth a
                              # plain HTTP client can't.

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
                vlog(f"401 body: {r.text[:300]!r}")
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
                    return _NO_SUBSCRIPTION, []
                if result is _KNOWN_BLOCKED:
                    # This candidate resolved to a confirmed dead-end
                    # publisher (e.g. ScienceDirect) or a Cloudflare bot
                    # challenge -- not a PDF, but also not evidence of an
                    # auth problem. Keep trying the remaining candidates
                    # (libkeyContentLocation, the full-text-file SPA link,
                    # etc.) in case one of them routes somewhere else, but
                    # remember this happened so the end-of-function
                    # diagnostic doesn't blame the 401s for what's actually
                    # this paper's own publisher wall, and keep the entry
                    # URL itself as a manual-download candidate -- a human
                    # following this same link in a real browser gets past
                    # the challenge/auth that blocked this script.
                    _hit_known_blocked = True
                    if url not in _manual_candidates:
                        _manual_candidates.append(url)
                    continue
                if result:
                    return result, []
        except Exception as e:
            vlog(f"ThirdIron v2 error ({ti_label}): {e}")

    if _hit_known_blocked:
        # The real reason this paper didn't download is a confirmed dead-end
        # publisher (ScienceDirect/Elsevier etc, 0% historical success here),
        # found via the *unauthenticated* public endpoint -- that succeeded
        # fine and returned real article data, so the 401s on the
        # library-scoped calls below are a separate, unrelated problem and
        # not why this particular paper failed. Printing the VPN/token
        # diagnostic here would send the user chasing the wrong cause.
        if verbose:
            print(f"\n      [libkey] this paper resolved to a known-blocked "
                  f"publisher -- needs manual download regardless of the "
                  f"library-token issue (noted separately above if it "
                  f"occurred)")
    elif _lib_auth_failures == len(_lib_ti_labels) and _lib_ti_labels:
        # As of the api-tokens POST fix, _get_thirdiron_library_token() mints
        # a real token directly from https://api.thirdiron.com/v2/api-tokens
        # (confirmed via live DevTools capture -- see that function's
        # docstring), so this branch should now be rare: it only fires if
        # that POST itself failed (network error, non-200, or a future
        # change on ThirdIron's end -- e.g. they start checking Origin
        # server-side and reject non-browser callers). NOT cookies.txt
        # (libkey.io sets no session cookie) and NOT VPN (already confirmed
        # full-tunnel + UW IP didn't matter for this path either) -- the
        # mint call has nothing to do with either. Print this once per run,
        # not once per paper, since the cause is the same every time it
        # happens.
        global _token_diag_shown
        if not _token_diag_shown:
            _token_diag_shown = True
            bar = "=" * 62
            print(f"\n  {bar}")
            print(f"  ⚠  LibKey / ThirdIron (UW Library): every library-scoped "
                  f"call is returning 401 this run.")
            print(f"  Not cookies.txt or VPN -- the library token is minted "
                  f"directly via a POST to api.thirdiron.com/v2/api-tokens, "
                  f"unrelated to either. This means that mint call itself "
                  f"failed (see the \"POST /v2/api-tokens status=...\" line "
                  f"above, if printed, for the actual status/error). This is "
                  f"a property of this run, not of any one paper -- it will "
                  f"recur for every paper this run; only printing once.")
            print(f"  Unauthenticated lookups (bare /articles/pmid:.. and "
                  f"/articles/doi:.. endpoints, no /libraries/{{id}}/ in the "
                  f"path) still work and are tried regardless.")
            print(f"  {bar}")

    return None, _manual_candidates
