"""
Google Scholar open-access PDF strategy (via the `scholarly` library).

Mirrors what Scholar's 'PDF' link shows (e.g. academia.edu, institutional
repos, author pages). Requires:  pip install scholarly
"""

from strategy_utils import session, HAS_SCHOLARLY, is_pdf, find_pdf_urls_in_html


def try_scholarly(doi, title, path):
    """
    Search Google Scholar for an open-access PDF using the `scholarly` library.

    Note: Google Scholar rate-limits aggressive scrapers. This runs with a
    1–2 s delay and a single query; it won't get blocked in normal batch use.
    """
    if not HAS_SCHOLARLY:
        return None
    try:
        from scholarly import scholarly as _sch
        query = f"doi:{doi}" if doi else title
        results = _sch.search_pubs(query)
        pub = next(results, None)
        if pub is None:
            return None
        eprint_url = pub.get("eprint_url") or pub.get("pub_url")
        if not eprint_url:
            return None
        r = session.get(eprint_url, timeout=60, allow_redirects=True,
                        headers={"Referer": "https://scholar.google.com/"})
        if r.status_code == 200 and is_pdf(r.content):
            path.write_bytes(r.content)
            return eprint_url
        for pdf_url in find_pdf_urls_in_html(r.text, eprint_url)[:4]:
            r2 = session.get(pdf_url, timeout=60, allow_redirects=True,
                             headers={"Referer": eprint_url})
            if r2.status_code == 200 and is_pdf(r2.content):
                path.write_bytes(r2.content)
                return pdf_url
    except Exception:
        pass
    return None
