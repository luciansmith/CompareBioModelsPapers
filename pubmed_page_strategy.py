"""
PubMed page scraping strategy.

Scrapes the PubMed page for a PMID and tries any free-access PDF links
it lists (PMC articles, author manuscripts, preprints, etc.).
"""

import re

from strategy_utils import ncbi_session, session, is_pdf, find_pdf_urls_in_html


def try_pubmed_page(pmid, path):
    """
    Scrape the PubMed page for a PMID and try any free-access PDF links it lists.
    Returns (pdf_url, pmcid_found) or (None, None).
    """
    if not re.match(r'^\d+$', pmid):
        return None, None
    try:
        r = ncbi_session.get(f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                             timeout=30, allow_redirects=True)
        if r.status_code != 200:
            return None, None
        html = r.text

        pmcid = None
        m = re.search(r'PMC(\d+)', html)
        if m:
            pmcid = f"PMC{m.group(1)}"

        free_urls = re.findall(
            r'href=["\']'
            r'(https?://(?:www\.ncbi\.nlm\.nih\.gov/pmc/articles/PMC\d+|'
            r'europepmc\.org/articles/PMC\d+|'
            r'www\.biorxiv\.org/content/[^\s"\']+|'
            r'arxiv\.org/[^\s"\']+)[^"\']*)["\']',
            html
        )
        for url in free_urls:
            try:
                r2 = session.get(url, timeout=60, allow_redirects=True)
                if r2.status_code == 200 and is_pdf(r2.content):
                    path.write_bytes(r2.content)
                    return url, pmcid
            except Exception:
                pass

        return None, pmcid

    except Exception:
        return None, None
