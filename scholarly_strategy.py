"""
Google Scholar open-access PDF strategy (via the `scholarly` library).

Mirrors what Scholar's 'PDF' link shows (e.g. academia.edu, institutional
repos, author pages). Requires:  pip install scholarly
"""

import re
import threading
import time

from strategy_utils import (
    session, HAS_SCHOLARLY, is_pdf, find_pdf_urls_in_html, make_cookie_session,
    is_cloudflare_challenge,
)

# Shared with _track_captcha_encounters() below: set True whenever Google
# actually serves a CAPTCHA during the current try_scholarly() call, so the
# caller can tell a CAPTCHA-caused failure apart from any other kind (and
# --pause-on-captchas only pauses for an actual CAPTCHA).
_captcha_state = {"seen": False}


def _track_captcha_encounters():
    """
    Monkeypatch scholarly's ProxyGenerator._handle_captcha2() to record
    that a CAPTCHA was actually served, then delegate to the original.

    This script never opens a browser to solve a CAPTCHA automatically --
    see try_scholarly()'s docstring for why that doesn't reliably help.
    _disable_selenium_captcha_solving() (below) is always active, so the
    original _handle_captcha2() still fails fast via the disabled
    _get_webdriver(); this wrapper just observes that it was called at all
    before that happens.

    Idempotent (checks a flag on the class) -- cheap to call on every
    try_scholarly() invocation.
    """
    try:
        from scholarly._proxy_generator import ProxyGenerator
    except ImportError:
        return
    if getattr(ProxyGenerator, "_captcha_tracking_installed", False):
        return

    _orig_handle_captcha2 = ProxyGenerator._handle_captcha2

    def _tracked_handle_captcha2(self, url):
        _captcha_state["seen"] = True
        return _orig_handle_captcha2(self, url)

    ProxyGenerator._handle_captcha2 = _tracked_handle_captcha2
    ProxyGenerator._captcha_tracking_installed = True


def _disable_selenium_captcha_solving():
    """
    Monkeypatch scholarly's ProxyGenerator._get_webdriver() to fail fast
    instead of launching a real Selenium/Chrome browser.

    Root cause of the overnight crash: when Google Scholar serves a
    CAPTCHA, scholarly's _handle_captcha2() (_proxy_generator.py) calls
    self._get_webdriver(), which tries to launch a headless
    Firefox/geckodriver browser first and falls back to headless
    Chrome/chromedriver if that's not available, navigates to
    scholar.google.com, then separately waits up to *one week* for a human
    to solve the CAPTCHA in that window. The 15s thread-watchdog added
    earlier only stops *this script's* calling thread from blocking -- it
    does nothing to the browser process _get_webdriver() just launched,
    which is simply abandoned, still running, still waiting. Across an
    unattended overnight batch hitting many CAPTCHAs, that's a new
    orphaned browser process left behind every time, which can pile up
    until the machine runs out of memory -- consistent with what happened.

    Patching _get_webdriver() to raise immediately means _handle_captcha2()
    never gets as far as launching a browser at all. The retry loop in
    _navigator.py's _get_page() already has a generic `except Exception`
    around the captcha-handling call, so the raised error is treated like
    any other failed fetch attempt: it bumps the retry counter, tries the
    next proxy, and gives up with MaxTriesExceededException after a couple
    of quick retries (a few seconds, no browser ever opened) -- this is an
    existing, already-exercised code path, not a new one. The 15s
    thread-watchdog below is kept as a second line of defense, but with
    this patch in place it should rarely if ever actually need to trigger
    for the CAPTCHA case specifically.

    Idempotent (checks a flag on the class) -- cheap to call on every
    try_scholarly() invocation.
    """
    try:
        from scholarly._proxy_generator import ProxyGenerator
    except ImportError:
        return
    if getattr(ProxyGenerator, "_captcha_browser_disabled", False):
        return

    def _no_browser(self):
        raise RuntimeError(
            "Selenium/Chrome CAPTCHA-solving disabled (would otherwise "
            "launch and abandon a browser process waiting on a human to "
            "solve a CAPTCHA -- see _disable_selenium_captcha_solving() "
            "in scholarly_strategy.py)"
        )

    ProxyGenerator._get_webdriver = _no_browser
    ProxyGenerator._captcha_browser_disabled = True


def try_scholarly(doi, title, path, verbose=False, pause_on_captchas=False):
    """
    Search Google Scholar for an open-access PDF using the `scholarly` library.

    Note: Google Scholar rate-limits aggressive scrapers. This runs with a
    1–2 s delay and a single query; it won't get blocked in normal batch use.

    Returns (source_url_or_None, manual_candidates) where manual_candidates
    is a list of 0-2 URLs (eprint_url first, then pub_url -- Scholar's "all
    versions"/landing-page link) worth offering as a manual-download
    fallback, returned even when the automated fetch fails. eprint_url is
    listed first because it's Scholar's own pick for the best open-access
    copy (e.g. an academia.edu or repository mirror); pub_url is usually
    just the publisher's paywalled landing page.

    This never opens a browser to solve a CAPTCHA automatically. That was
    tried (--solve-captchas) and confirmed -- by reading scholarly's actual
    source -- not to reliably help: solving the CAPTCHA only satisfies the
    visible browser, while the actual scraping happens over a separate
    plain-HTTP session that Google can (and often does, especially from a
    VPN/datacenter IP with no proxy configured) keep blocking
    independently. That follow-up block makes scholarly silently close the
    browser and wipe its cookies, so the human ends up re-solving the same
    CAPTCHA over and over for no benefit.

    pause_on_captchas: if False (default, safe for unattended batch runs),
    a CAPTCHA hit fails fast (no pause, no browser). If True, on a CAPTCHA
    this prints a message and blocks on input() waiting for Enter, then
    retries the whole search exactly once from scratch; if that retry also
    hits a CAPTCHA, it gives up rather than pausing again. Pressing Enter
    doesn't solve anything by itself -- it's just a deliberate pause (e.g.
    to switch networks/VPN exit node, or simply wait out a rate limit)
    before the one retry.
    """
    def vlog(msg):
        if verbose:
            print(f"\n      [scholarly] {msg}")

    if not HAS_SCHOLARLY:
        vlog("HAS_SCHOLARLY is False -- the `scholarly` package isn't "
             "importable in this environment (pip install scholarly "
             "--break-system-packages)")
        return None, []
    try:
        from scholarly import scholarly as _sch
        _disable_selenium_captcha_solving()
        _track_captcha_encounters()
        query = f"doi:{doi}" if doi else title
        vlog(f"query={query!r}")

        # search_pubs() scrapes Google's public search-results HTML rather
        # than calling a stable API -- an empty result set sometimes comes
        # back with no error at all (no captcha, status 200) simply because
        # that particular page load didn't render the expected result-row
        # markup. Observed directly: the identical query returned nothing on
        # one attempt and a real result on an immediate retry. One retry
        # after a short pause cheaply absorbs that flakiness instead of
        # reporting a false "not found".
        #
        # search_pubs()/next() can also block longer than expected with no
        # console output at all if something deep inside scholarly/httpx
        # gets stuck. Run the query in a daemon thread with a 15s watchdog:
        # if it hasn't returned by then, abandon this attempt and let the
        # rest of the batch keep going instead of hanging. daemon=True only
        # means the thread won't block the main script from exiting later
        # -- it doesn't kill the thread, which is simply abandoned.
        def _run_query(q, out):
            try:
                results = _sch.search_pubs(q)
                out["pub"] = next(results, None)
            except Exception as e:
                out["error"] = e

        def _attempt_search():
            """
            One full attempt: up to 2 tries (absorbing the empty-result
            flakiness described above), each with a 15s watchdog. Returns
            the found pub, or None if nothing was found. Raises whatever
            search_pubs()/next() raised (e.g. MaxTriesExceededException
            from a CAPTCHA wall) rather than swallowing it.
            """
            found = None
            for attempt in range(2):
                out = {}
                t = threading.Thread(target=_run_query, args=(query, out), daemon=True)
                t.start()
                t.join(timeout=15)
                if t.is_alive():
                    vlog(f"attempt {attempt + 1}/2: search_pubs() didn't return "
                         f"within 15s -- giving up on this attempt")
                    continue
                if "error" in out:
                    raise out["error"]
                found = out.get("pub")
                if found is not None:
                    break
                vlog(f"attempt {attempt + 1}/2: no results returned by search_pubs()")
                if attempt == 0:
                    time.sleep(2.0)
            return found

        pub = None
        _captcha_state["seen"] = False
        try:
            pub = _attempt_search()
        except Exception as e:
            if pause_on_captchas and _captcha_state["seen"]:
                vlog(f"exception: {type(e).__name__}: {e}")
                print("\n      Google Scholar served a CAPTCHA. This "
                      "script doesn't solve it automatically (doesn't "
                      "reliably help -- see try_scholarly()'s docstring). "
                      "Press Enter to retry once from scratch: ",
                      end="", flush=True)
                try:
                    input()
                except EOFError:
                    pass
                vlog("retrying once from scratch after --pause-on-captchas keystroke")
                _captcha_state["seen"] = False
                try:
                    pub = _attempt_search()
                except Exception as e2:
                    vlog(f"retry exception: {type(e2).__name__}: {e2}")
                    pub = None
            else:
                raise
        if pub is None:
            return None, []
        if verbose:
            print(f"      [scholarly] result keys: {list(pub.keys())}")
            print(f"      [scholarly]   title         = {pub.get('bib', {}).get('title')!r}")
            print(f"      [scholarly]   eprint_url    = {pub.get('eprint_url')!r}")
            print(f"      [scholarly]   pub_url       = {pub.get('pub_url')!r}")
        eprint_url = pub.get("eprint_url") or pub.get("pub_url")
        # Both URLs are worth keeping as manual-download fallbacks even if the
        # automated fetch below fails -- eprint_url first (Scholar's own pick
        # for the best open-access copy), then pub_url if it's a different URL.
        manual_candidates = []
        for u in (pub.get("eprint_url"), pub.get("pub_url")):
            if u and u not in manual_candidates:
                manual_candidates.append(u)
        if not eprint_url:
            vlog("result has neither eprint_url nor pub_url -- nothing to fetch")
            return None, []

        # Use a cookie-loaded session (cookies.txt + any matching browser
        # cookies for the eprint host) rather than the bare unauthenticated
        # `session` -- sites like academia.edu gate the actual PDF download
        # behind a logged-in session (an unauthenticated request gets a 403
        # or an upsell/interstitial page instead of the file), and the user
        # may have manually exported cookies for exactly this kind of host
        # into cookies.txt.
        from urllib.parse import urlparse as _urlparse
        eprint_domain = _urlparse(eprint_url).netloc
        fetch_sess = make_cookie_session(eprint_domain) if eprint_domain else session
        if verbose:
            n_domain_cookies = sum(
                1 for c in fetch_sess.cookies
                if eprint_domain and eprint_domain.split(":")[0].lstrip("www.") in (c.domain or "")
            )
            vlog(f"using cookie session for {eprint_domain!r} "
                 f"({n_domain_cookies} cookie(s) matching that domain, "
                 f"{len(fetch_sess.cookies)} total loaded)")

        r = fetch_sess.get(eprint_url, timeout=60, allow_redirects=True,
                        headers={"Referer": "https://scholar.google.com/"})
        vlog(f"GET {eprint_url} -> status={r.status_code}  "
             f"ct={r.headers.get('content-type','')}  size={len(r.content)}  "
             f"is_pdf={is_pdf(r.content)}")
        if verbose and r.url != eprint_url:
            vlog(f"redirected to: {r.url}")
        if r.status_code == 200 and is_pdf(r.content):
            path.write_bytes(r.content)
            return eprint_url, []
        if is_cloudflare_challenge(r):
            # Confirmed via a live response body: academia.edu's /download/
            # links sit behind Cloudflare's managed JS challenge ("Just a
            # moment..."). This is a network-edge bot check, not a cookie or
            # login problem -- cookies.txt being fully loaded doesn't matter
            # because Cloudflare blocks the plain HTTP request before
            # academia.edu's own auth logic ever runs. A real browser can
            # pass it (confirmed: the user got through manually), but a
            # script can't solve a JS challenge -- same "needs manual
            # download" treatment as the JSTOR/Ingenta walls elsewhere in
            # this project. No point scraping this page for candidate PDF
            # links; a challenge page never contains any.
            vlog(f"Cloudflare managed challenge at {eprint_url} -- not a "
                 f"cookie/login issue, can't be solved by a script. Needs "
                 f"manual download (open this URL in a real browser): "
                 f"{eprint_url}")
            return None, manual_candidates
        candidates = find_pdf_urls_in_html(r.text, eprint_url)
        vlog(f"HTML scrape of {eprint_url} found {len(candidates)} candidate PDF URL(s)")
        if verbose and not candidates and r.status_code != 200:
            # No candidates and a non-200 -- print a snippet of the body so
            # we can see what the site is actually saying (login wall, rate
            # limit, expired/signed-link error, etc.) instead of guessing.
            snippet = re.sub(r'\s+', ' ', r.text[:4000])
            vlog(f"response body snippet (first 1500 of {len(r.text)} chars "
                 f"after whitespace-collapse): {snippet[:1500]!r}")
        for pdf_url in candidates[:4]:
            r2 = fetch_sess.get(pdf_url, timeout=60, allow_redirects=True,
                             headers={"Referer": eprint_url})
            vlog(f"  -> {pdf_url}  status={r2.status_code}  is_pdf={is_pdf(r2.content)}")
            if r2.status_code == 200 and is_pdf(r2.content):
                path.write_bytes(r2.content)
                return pdf_url, []
        return None, manual_candidates
    except Exception as e:
        vlog(f"exception: {type(e).__name__}: {e}")
        if verbose and type(e).__name__ in (
            "MaxTriesExceededException", "_navigator.MaxTriesExceededException"
        ):
            vlog("this exception name usually means Google Scholar served a "
                 "CAPTCHA / blocked the request -- scholarly has no logged-in "
                 "session and no proxy configured, so datacenter/cloud IPs "
                 "(like this sandbox's) get walled quickly; a home/VPN IP may "
                 "fare better, or scholarly.use_proxy(...) would be needed")
    return None, []
