# -*- coding: utf-8 -*-
"""
check_journal_self_hit.py

For every BioModels article that has a PMID and enough journal metadata to
build a Level-1 issue query, searches PubMed and checks whether the article's
own PMID appears in the results.

If the total hit count for the issue exceeds RETMAX we can't verify the full
result set, so those entries are reported separately as SKIPPED.

Reads:  biomd_publication_info_corrected.json   (falls back to
        biomd_publication_info.json if the corrected file is absent)
Output: printed to stdout
"""

import json
import os
import sys
import time
import xml.etree.ElementTree as ET

import Bio.Entrez

Bio.Entrez.email = 'lpsmith@uw.edu'
RETMAX = 100

# ---------------------------------------------------------------------------
# Locate input file
# ---------------------------------------------------------------------------
for candidate in ('biomd_publication_info_corrected.json',
                  'biomd_publication_info.json'):
    if os.path.exists(candidate):
        INPUT_FILE = candidate
        break
else:
    sys.exit('ERROR: no input JSON found in the current directory')

with open(INPUT_FILE, encoding='utf-8') as f:
    all_publications = json.load(f)

print(f"Loaded {len(all_publications)} entries from {INPUT_FILE}\n")


# ---------------------------------------------------------------------------
# Helpers  (mirror sort_biomd_pubs_combined.py)
# ---------------------------------------------------------------------------

def normalise_journal(title: str) -> str:
    """PubMed stores 'and' not '&' in title abbreviations."""
    return title.replace('&', 'and')


def get_pmid(key: str, pub: dict):
    """Extract PMID from known fields or from the key URL."""
    for field in ('pmid', 'PMID', 'pubmedId', 'confirmed_pmid'):
        if field in pub:
            return str(pub[field])
    for sep in ('/', ':', '_'):
        for part in key.split(sep):
            if part.isdigit() and len(part) >= 4:
                return part
    return None


def build_issue_query(pub: dict):
    """Build the Level-1 journal/issue PubMed query, or None if not possible."""
    journal = pub.get('journal', '').strip()
    year    = pub.get('year')
    volume  = pub.get('volume')
    month   = pub.get('month')

    if not journal or not year:
        return None

    journal = normalise_journal(journal)

    if volume:
        return f'{journal}[ta] AND {volume}[vi] AND {year}[dp]'
    elif month:
        return f'{journal}[ta] AND {year}/{month}[dp]'
    return None


def pubmed_search(query: str, retmax: int = RETMAX):
    """Run esearch; return (total_count, [pmid, ...]).

    Returns (-1, []) if PubMed returns an error or unparseable response,
    so the caller can treat it as a skip rather than a crash.
    """
    try:
        handle = Bio.Entrez.esearch(db='pubmed', term=query, retmax=retmax)
        raw    = handle.read()
        time.sleep(0.34)
        root   = ET.fromstring(raw)

        count_el = root.find('Count')
        if count_el is None:
            error_el = root.find('ERROR')
            msg = error_el.text if error_el is not None else raw[:200]
            print(f"    [WARN] PubMed returned no Count element. Error: {msg}",
                  file=sys.stderr)
            time.sleep(2)   # back off before next request after an error
            return -1, []

        count  = int(count_el.text)
        idlist = root.find('IdList')
        ids    = [el.text for el in idlist] if idlist is not None else []
        return count, ids

    except Exception as exc:
        print(f"    [WARN] PubMed search failed: {exc}", file=sys.stderr)
        time.sleep(2)
        return -1, []


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
ok       = []   # (key, pmid)
missing  = []   # (key, pmid, query, count)  — not found, issue small enough
skipped  = []   # (key, pmid, query, count)  — issue > RETMAX, can't verify
errored  = []   # (key, pmid, query)         — PubMed returned an error
no_query = []   # (key, pmid)               — missing journal/volume/month

for key, pub in all_publications.items():
    pmid = get_pmid(key, pub)
    if not pmid:
        continue   # DOI-only entry without a confirmed PMID

    query = build_issue_query(pub)
    if not query:
        no_query.append((key, pmid))
        print(f"  [NO QUERY] {pmid}  {key}")
        continue

    count, ids = pubmed_search(query)

    if count == -1:
        errored.append((key, pmid, query))
        print(f"  [ERROR]        PMID {pmid}  query: {query}")
    elif count > RETMAX:
        skipped.append((key, pmid, query, count))
        print(f"  [SKIP >RETMAX] PMID {pmid}  count={count}")
    elif pmid not in ids:
        missing.append((key, pmid, query, count))
        print(f"  [MISSING]      PMID {pmid}  count={count}  query: {query}")
    else:
        ok.append((key, pmid))
        print(f"  [OK]           PMID {pmid}  count={count}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
sep = '=' * 68
print(f"\n{sep}")
print(f"SUMMARY")
print(f"{sep}")
print(f"  Checked (PMID found in results) : {len(ok)}")
print(f"  MISSING (not in results)        : {len(missing)}")
print(f"  Skipped (issue > RETMAX={RETMAX}) : {len(skipped)}")
print(f"  PubMed errors                   : {len(errored)}")
print(f"  No query possible               : {len(no_query)}")
print()

if missing:
    print(f"{sep}")
    print(f"MISSING — article not found in its own journal issue search")
    print(f"{sep}")
    for key, pmid, query, count in missing:
        print(f"\n  PMID  : {pmid}")
        print(f"  Key   : {key}")
        print(f"  Query : {query}")
        print(f"  Hits  : {count}")

if skipped:
    print(f"\n{sep}")
    print(f"SKIPPED — issue exceeds RETMAX={RETMAX}, cannot verify")
    print(f"{sep}")
    for key, pmid, query, count in skipped:
        print(f"\n  PMID  : {pmid}")
        print(f"  Key   : {key}")
        print(f"  Query : {query}")
        print(f"  Hits  : {count}")

if errored:
    print(f"\n{sep}")
    print(f"ERRORS — PubMed returned no parseable response")
    print(f"{sep}")
    for key, pmid, query in errored:
        print(f"\n  PMID  : {pmid}")
        print(f"  Key   : {key}")
        print(f"  Query : {query}")

if no_query:
    print(f"\n{sep}")
    print(f"NO QUERY — missing journal/volume/month, cannot build issue query")
    print(f"{sep}")
    for key, pmid in no_query:
        print(f"  PMID {pmid}  {key}")
