"""
Direct DOI / EZProxy download strategy.

Strategy:
  A. Direct PDF URL from DOI prefix; each has an EZProxy twin
  B. Follow DOI redirect, then try publisher pattern on the final URL
  C. Scrape the article page HTML for PDF links
  D. EZProxy fallback: proxy the resolved article URL through UW EZProxy
"""

from strategy_utils import (
    HAS_BROWSER_COOKIES, COOKIES_FILE, GENERAL_DELAY,
    session, make_cookie_session,
    ezproxy_url, PUBLISHER_PDF_PATTERNS, pdf_url_from_doi,
    find_pdf_urls_in_html, _get_via_ezproxy, _is_ezproxy_login_wall,
    is_pdf,
)


def try_direct_doi(doi, path, verbose=False):
    """
    Try to get a PDF via the journal site (works best with UW VPN active).

    Returns (source_url_or_None, manual_fallback_url).
    """
    if not doi:
        return None, None

    pdf_headers = {"Accept": "application/pdf,*/*"}
    _DOI_PREFIX_DOMAIN = {
        "10.1073":  ".pnas.org",
        "10.1038":  ".nature.com",
        "10.1126":  ".science.org",
        "10.1016":  ".sciencedirect.com",
        "10.1007":  ".springer.com",
        "10.1002":  ".wiley.com",
        "10.1046":  ".wiley.com",
        "10.1093":  ".academic.oup.com",
        "10.1371":  ".plos.org",
        "10.1083":  ".rupress.org",
        "10.1152":  ".physiology.org",
        "10.1042":  ".portlandpress.com",
        "10.1074":  ".jbc.org",
        "10.15252": ".embopress.org",
        "10.1101":  ".biorxiv.org",
        "10.1006":  ".sciencedirect.com",
        "10.1098":  ".royalsocietypublishing.org",
        "10.1099":  ".microbiologyresearch.org",
        "10.1113":  ".onlinelibrary.wiley.com",
        "10.1523":  ".jneurosci.org",
        "10.1085":  ".rupress.org",
        "10.1111":  ".onlinelibrary.wiley.com",
        "10.1096":  ".fasebj.org",
        "10.1128":  ".asm.org",
        "10.1104":  ".plantphysiol.org",
        "10.1105":  ".plantcell.org",
    }
    doi_prefix = doi.split("/")[0] if "/" in doi else ""
    cookie_domain = _DOI_PREFIX_DOMAIN.get(doi_prefix, doi_prefix)
    cookie_session = make_cookie_session(cookie_domain) if HAS_BROWSER_COOKIES else session
    if verbose:
        n_total = len(cookie_session.cookies)
        has_file = COOKIES_FILE.exists()
        sent = [c.name for c in cookie_session.cookies
                if cookie_domain.lstrip(".") in (c.domain or "")]
        print(f"\n      [verbose] cookie_domain={cookie_domain!r}  "
              f"total_cookies={n_total}  domain_cookies={len(sent)} ({sent})  "
              f"cookies_txt={has_file}  browser_cookie3={HAS_BROWSER_COOKIES}")

    def try_url(pdf_url, referer=None):
        h = dict(pdf_headers)
        if referer:
            h["Referer"] = referer
        try:
            r2 = cookie_session.get(pdf_url, timeout=60, allow_redirects=True, headers=h)
            if verbose:
                ct2 = r2.headers.get("content-type", "")
                print(f"      [verbose]   -> {pdf_url}  status={r2.status_code}  ct={ct2}  size={len(r2.content)}  is_pdf={is_pdf(r2.content)}")
            if r2.status_code == 200 and is_pdf(r2.content):
                path.write_bytes(r2.content)
                return True
        except Exception as e:
            if verbose:
                print(f"      [verbose]   -> {pdf_url}  error: {e}")
        return False

    # --- Strategy A: direct PDF URL from DOI prefix (+ EZProxy twin) ---------
    direct_candidates = pdf_url_from_doi(doi)
    if direct_candidates and verbose:
        print(f"\n      [verbose] trying {len(direct_candidates)} direct PDF URL(s) from DOI prefix")
    doi_referer = f"https://doi.org/{doi}"
    for pdf_url in direct_candidates:
        if try_url(pdf_url, referer=doi_referer):
            return pdf_url, None

    # --- Follow DOI redirect --------------------------------------------------
    doi_url = f"https://doi.org/{doi}"
    final_url = doi_url
    html = ""
    try:
        r = cookie_session.get(doi_url, timeout=30, allow_redirects=True)
        if verbose:
            ct = r.headers.get("content-type", "")
            print(f"\n      [verbose] DOI resolved to {r.url}  status={r.status_code}  content-type={ct}  size={len(r.content)}")
        final_url = r.url
        if r.status_code == 200 and is_pdf(r.content):
            path.write_bytes(r.content)
            return final_url, None
        if r.status_code == 200:
            html = r.text
    except Exception as e:
        if verbose:
            print(f"      [verbose] DOI redirect error: {e}")

    # --- Strategy B: publisher pattern on resolved URL -----------------------
    if verbose:
        print(f"      [verbose] trying publisher patterns on {final_url}")
    for domain, pdf_fn in PUBLISHER_PDF_PATTERNS.items():
        if domain in final_url:
            try:
                pdf_url = pdf_fn(final_url, doi)
                if try_url(pdf_url, referer=final_url):
                    return pdf_url, None
                ez = ezproxy_url(pdf_url)
                if ez != pdf_url and try_url(ez, referer=final_url):
                    return ez, None
            except Exception as e:
                if verbose:
                    print(f"      [verbose] pattern error: {e}")
            break

    # --- Strategy C: scrape article page HTML --------------------------------
    if html:
        candidates = find_pdf_urls_in_html(html, final_url)
        if verbose:
            print(f"      [verbose] HTML scrape found {len(candidates)} candidate PDF URLs:")
            for u in candidates[:5]:
                print(f"        {u}")
        for pdf_url in candidates[:8]:
            if try_url(pdf_url, referer=final_url):
                return pdf_url, None

    # --- Strategy D: EZProxy on the resolved article page itself -------------
    if final_url and final_url != f"https://doi.org/{doi}":
        ez_article_start = ezproxy_url(final_url)
        if ez_article_start != final_url:
            _vlog = (lambda msg: print(f"      [verbose] {msg}")) if verbose else (lambda msg: None)
            _vlog(f"Strategy D: EZProxy (redirect-following) for {final_url}")
            _ez_login_wall = False
            ez_sess = make_cookie_session("offcampus.lib.washington.edu")
            r_ez = _get_via_ezproxy(final_url, ez_sess, timeout=60, vlog=_vlog)
            if r_ez is None:
                return None, final_url
            _vlog(f"  EZProxy final: {r_ez.url}  status={r_ez.status_code}  "
                  f"ct={r_ez.headers.get('content-type','')}  "
                  f"size={len(r_ez.content)}  is_pdf={is_pdf(r_ez.content)}")
            if _is_ezproxy_login_wall(r_ez):
                _ez_login_wall = True
                _vlog(f"  EZProxy login wall: {r_ez.url}")
            elif r_ez.status_code == 200 and is_pdf(r_ez.content):
                path.write_bytes(r_ez.content)
                return r_ez.url, None
            elif r_ez.status_code == 200:
                for domain, pdf_fn in PUBLISHER_PDF_PATTERNS.items():
                    if domain in r_ez.url or domain in final_url:
                        try:
                            ez_pdf = ezproxy_url(pdf_fn(r_ez.url, doi))
                            r_p = ez_sess.get(ez_pdf, timeout=60, allow_redirects=True,
                                              headers={"Accept": "application/pdf,*/*",
                                                       "Referer": r_ez.url})
                            _vlog(f"  EZProxy PDF pattern -> {ez_pdf}  status={r_p.status_code}  is_pdf={is_pdf(r_p.content)}")
                            if r_p.status_code == 200 and is_pdf(r_p.content):
                                path.write_bytes(r_p.content)
                                return r_p.url, None
                        except Exception:
                            pass
                        break
                for pdf_url in find_pdf_urls_in_html(r_ez.text, r_ez.url)[:6]:
                    ez_pdf2 = ezproxy_url(pdf_url)
                    r_p2 = ez_sess.get(ez_pdf2, timeout=60, allow_redirects=True,
                                       headers={"Accept": "application/pdf,*/*",
                                                "Referer": r_ez.url})
                    if r_p2.status_code == 200 and is_pdf(r_p2.content):
                        path.write_bytes(r_p2.content)
                        return r_p2.url, None
            if _ez_login_wall:
                return None, final_url

    return None, None
