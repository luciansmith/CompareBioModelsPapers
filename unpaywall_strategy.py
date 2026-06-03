"""
Unpaywall open-access PDF strategy.

Queries the Unpaywall API for the best open-access PDF location for a DOI.
"""

from strategy_utils import session, EMAIL, is_pdf


def try_unpaywall(doi, path):
    """Look up an open-access PDF via the Unpaywall API."""
    if not doi:
        return None
    try:
        r = session.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": EMAIL}, timeout=30,
        )
        if r.status_code != 200:
            return None
        best    = r.json().get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf")
        if not pdf_url:
            return None
        r2 = session.get(pdf_url, timeout=60, allow_redirects=True)
        if r2.status_code == 200 and is_pdf(r2.content):
            path.write_bytes(r2.content)
            return pdf_url
    except Exception:
        pass
    return None
