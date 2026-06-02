# -*- coding: utf-8 -*-
"""
Collect journal/issue and MeSH control articles for BioModels publications.

For each BioModels article, two independent control pools are built:

  Journal controls ("Similar PMIDs")
  -----------------------------------
  Level 1: same journal issue (volume or month + year)
  Level 2: same journal + same year
  Level 3: same journal ± 1 year
  Level 4: same journal ± 2 years

  MeSH controls ("MeSH PMIDs")
  ------------------------------
  A search_terms list is built from major MeSH headings, padded with
  minor headings until at least 5 terms are present (or all terms are
  exhausted).  Strategies then operate on search_terms:

  Level 1: AND top-3 search_terms + ± YEAR_OFFSET years
  Level 2: AND top-2 search_terms + ± YEAR_OFFSET years
  Level 3: OR  all  search_terms  + ± YEAR_OFFSET years
  Level 4: OR  all  search_terms    (no year constraint)

Each pool has its own excluded-PMID set (excluded_pmids_journal /
excluded_pmids_mesh) so the two strategies draw from independent
reservoirs. Both sets are seeded with all BioModels source PMIDs.

At most MAX_CONTROLS articles are stored per source article per pool.
When a PubMed query returns more usable results than needed, a random
sample is taken.  If a query returns exactly RETMAX results and fewer
than needed are usable (because most were already excluded), the query
is automatically retried with a larger retmax to find more candidates.

"Journal Level Reached" and "MeSH Level Reached" record the last
expansion level actually executed (1-4), i.e. how hard the script
had to work. 0 means no search ran (missing journal info / PMID /
MeSH terms).

Progress is saved after every article so the script can be safely
interrupted and restarted; already-processed articles are skipped.

Reads:  biomd_publication_info_corrected.json
Writes: biomd_publication_info_with_controls.json
"""

import json
import os
import random
import time
import xml.etree.ElementTree as ET
import Bio.Entrez

Bio.Entrez.email = 'lpsmith@uw.edu'

MIN_CONTROL  = 10    # flag articles whose control group falls below this
MAX_CONTROLS = 50    # maximum controls stored per source article per pool
RETMAX       = 100   # starting max results per PubMed query (auto-expanded if needed)
MAX_RETMAX   = 9999  # hard ceiling on retmax (PubMed esearch limit without history)
YEAR_OFFSET  = 2     # MeSH search: base_year ± this many years

RANDOM_SEED  = 42   # set to None to disable seeding

# ---------------------------------------------------------------------------
# Hardcoded preprint overrides
#
# These entries are stored as bioRxiv preprint DOIs.  bioRxiv articles have
# no PubMed PMID and appear in no journal issue, so neither the journal nor
# the MeSH control search would find anything useful without intervention.
# For each preprint we record the published article's metadata so the script
# can search the right journal/issue and fetch proper MeSH terms.
#
# The stored JSON is NOT modified; these values are used only during the
# control-collection queries.
# ---------------------------------------------------------------------------
PREPRINT_OVERRIDES: dict[str, dict] = {
    "10.1101/498741": {          # BIOMD0000000742
        "journal": "Journal of Theoretical Biology",
        "year":    "2020",
        "volume":  "492",
        "pmid":    "32035826",
    },
    "10.1101/565531": {          # BIOMD0000000926
        "journal": "Journal of Theoretical Biology",
        "year":    "2019",
        "volume":  "482",
        "pmid":    "31493486",
    },
}


def extract_doi_from_key(key: str):
    """Return the DOI portion of a doi.org or identifiers.org/doi/ key, or None."""
    import re
    m = re.search(r'(?:identifiers\.org/doi/|doi\.org/)(.+)', key, re.IGNORECASE)
    return m.group(1).strip().rstrip('/') if m else None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def getIdListFromXML(record):
    ret = []
    root = ET.fromstring(record)
    idlist = root.find("IdList")
    for id1 in idlist:
        ret.append(id1.text)
    return ret


def searchPubMed(query, retmax=RETMAX):
    """Run an esearch and return a list of PMIDs. Throttles to ~3 req/sec."""
    handle = Bio.Entrez.esearch(db="pubmed", term=query, retmax=retmax)
    record = handle.read()
    time.sleep(0.34)
    return getIdListFromXML(record)


def searchPubMedFiltered(query, excluded, max_keep):
    """Fetch PMIDs for *query*, remove *excluded*, return up to *max_keep*.

    If the fetch is truncated (returned exactly retmax results) and fewer
    than max_keep usable results were found, retries with a larger retmax:
        n_already_excluded_from_raw + max_keep
    This repeats until we have enough usable results, the query is genuinely
    exhausted (PubMed returned fewer results than requested), or MAX_RETMAX
    is reached.  Results are randomly sampled when more candidates exist
    than max_keep.
    """
    if max_keep <= 0:
        return set()

    current_retmax = RETMAX
    raw    = searchPubMed(query, retmax=current_retmax)
    usable = set(raw) - excluded

    while len(raw) == current_retmax and len(usable) < max_keep:
        n_overlap      = len(raw) - len(usable)
        current_retmax = min(n_overlap + max_keep, MAX_RETMAX)
        print(f"    [retry retmax={current_retmax}: {n_overlap} excluded + {max_keep} needed]")
        raw    = searchPubMed(query, retmax=current_retmax)
        usable = set(raw) - excluded
        if current_retmax == MAX_RETMAX:
            break  # don't attempt to exceed the PubMed cap

    if len(usable) > max_keep:
        usable = set(random.sample(sorted(usable), max_keep))

    return usable


def getPMID(issue_key, publication):
    """Extract a bare PMID string from the publication key or dict."""
    for field in ("pmid", "PMID", "pubmedId", "confirmed_pmid"):
        if field in publication:
            return str(publication[field])
    for sep in ('/', ':', '_'):
        parts = issue_key.split(sep)
        for part in parts:
            if part.isdigit() and len(part) >= 4:
                return part
    return None


# ---------------------------------------------------------------------------
# Journal helpers
# ---------------------------------------------------------------------------

def normalise_journal_title(title: str) -> str:
    """Normalise a journal title for use in PubMed [ta] queries.

    PubMed stores title abbreviations with 'and' rather than '&', so queries
    containing '&' return zero results even when the journal is in PubMed.
    """
    return title.replace('&', 'and')


def getJournalIssue(publication):
    if 'journal' in publication and 'year' in publication and 'volume' in publication:
        return {'journal_title': normalise_journal_title(publication['journal']),
                'year':          str(publication['year']),
                'volume':        str(publication['volume'])}
    elif 'journal' in publication and 'year' in publication and 'month' in publication:
        return {'journal_title': normalise_journal_title(publication['journal']),
                'year':          str(publication['year']),
                'month':         str(publication['month'])}
    return None


def searchByYearOffsetFiltered(journal_title, base_year, offset, excluded, max_keep):
    """Search a journal for base_year ± offset, filtering and capping results.

    offset=0 searches only base_year; offset>0 searches base_year-offset
    and base_year+offset.  The max_keep budget is shared across sub-queries:
    each sub-query is given the remaining budget so we never over-collect.
    """
    base_year = int(base_year)
    ids       = set()
    years     = [base_year] if offset == 0 else [base_year - offset, base_year + offset]
    for year in years:
        remaining = max_keep - len(ids)
        if remaining <= 0:
            break
        query = f'{journal_title}[ta] AND {year}[dp]'
        print(f"    Year search ({year}): {query}")
        new = searchPubMedFiltered(query, excluded | ids, remaining)
        ids |= new
        print(f"      -> +{len(new)} (total this level: {len(ids)})")
    return ids


# ---------------------------------------------------------------------------
# MeSH helpers
# ---------------------------------------------------------------------------

def getMeSHTerms(pmid):
    """Return (major_terms, minor_terms) for a PMID."""
    handle = Bio.Entrez.efetch(db="pubmed", id=pmid, rettype="xml", retmode="xml")
    record = handle.read()
    time.sleep(0.34)
    root = ET.fromstring(record)
    major_terms = []
    minor_terms = []
    for descriptor in root.findall('.//MeshHeading/DescriptorName'):
        name = descriptor.text
        if descriptor.attrib.get('MajorTopicYN') == 'Y':
            major_terms.append(name)
        else:
            minor_terms.append(name)
    return major_terms, minor_terms


def buildMeSHQuery(terms, operator='AND', year=None, year_offset=YEAR_OFFSET):
    """Build a PubMed query from a list of MeSH descriptor names.

    When operator is 'OR' and a year range is requested, the OR group is
    wrapped in parentheses before appending the AND date filter.  Without
    this, PubMed's precedence rule (AND binds tighter than OR) would apply
    the date filter only to the last term, leaving the rest unfiltered.
    """
    mesh_parts = [f'"{term}"[MeSH Terms]' for term in terms]
    joined = f' {operator} '.join(mesh_parts)
    if year is not None:
        start = int(year) - year_offset
        end   = int(year) + year_offset
        if operator == 'OR' and len(terms) > 1:
            query = f'({joined}) AND {start}:{end}[dp]'
        else:
            query = f'{joined} AND {start}:{end}[dp]'
    else:
        query = joined
    return query


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if RANDOM_SEED is not None:
    random.seed(RANDOM_SEED)

all_publications = {}
with open('biomd_publication_info_corrected.json', 'r', encoding='utf-8') as f:
    all_publications = json.load(f)

# Load prior progress if it exists, so we can resume interrupted runs.
OUT_FILE = 'biomd_publication_info_with_controls.json'
if os.path.exists(OUT_FILE):
    with open(OUT_FILE, 'r', encoding='utf-8') as f:
        prior = json.load(f)
    for key, val in prior.items():
        if key in all_publications:
            all_publications[key].update(val)
    print(f"Loaded prior progress from {OUT_FILE}")

# Both excluded sets start with all BioModels source PMIDs.
excluded_pmids_journal = set()
excluded_pmids_mesh    = set()
for key, pub in all_publications.items():
    candidate = getPMID(key, pub)
    if candidate:
        excluded_pmids_journal.add(candidate)
        excluded_pmids_mesh.add(candidate)

# Seed from ALL previously collected controls, including those belonging to
# articles that will be re-processed in this run.  This is intentional:
# it prevents a re-processed article from "stealing" controls that already
# belong to a later article, which would create duplicates.  The trade-off
# is that early articles' re-runs are slightly more restricted than a fresh
# run (later articles' existing controls are pre-excluded), but correctness
# requires it.  Each article's own existing controls are preloaded into
# `ids` below so they still count toward that article's total.
for pub in all_publications.values():
    excluded_pmids_journal.update(pub.get("Similar PMIDs", []))
    excluded_pmids_mesh.update(pub.get("MeSH PMIDs", []))

print(f"Journal excluded set: {len(excluded_pmids_journal)} PMIDs")
print(f"MeSH    excluded set: {len(excluded_pmids_mesh)} PMIDs\n")


for issue, publication in all_publications.items():
    print(f"\n=== {issue} ===")
    changed = False

    # If this entry is a known preprint, substitute published-article metadata
    # for the purposes of journal and MeSH lookups only.
    doi = extract_doi_from_key(issue)
    override = PREPRINT_OVERRIDES.get(doi, {}) if doi else {}
    if override:
        effective_pub = dict(publication)
        effective_pub.update(override)   # journal, year, volume, pmid
        print(f"  [Preprint override] Using published metadata: "
              f"{override['journal']} {override['year']} "
              f"vol.{override['volume']} PMID {override['pmid']}")
    else:
        effective_pub = publication

    # ------------------------------------------------------------------
    # 1. Journal/issue controls
    # ------------------------------------------------------------------
    if len(publication.get("Similar PMIDs", [])) < MAX_CONTROLS:
        changed = True
        lookup = getJournalIssue(effective_pub)

        if lookup is None:
            print("  [Journal] No journal/year info — skipping")
            publication["Similar PMIDs"]         = []
            publication["Journal Level Reached"] = 0
        else:
            # Preload any controls already collected so we only top up.
            ids      = set(publication.get("Similar PMIDs", []))
            level    = publication.get("Journal Level Reached", 0)
            prev_len = len(ids)
            if ids:
                print(f"  [Journal] Resuming with {len(ids)} existing controls")

            # Level 1: specific issue (volume or month)
            if "volume" in lookup:
                q = (f'{lookup["journal_title"]}[ta] AND '
                     f'{lookup["volume"]}[vi] AND {lookup["year"]}[dp]')
            elif "month" in lookup:
                q = (f'{lookup["journal_title"]}[ta] AND '
                     f'{lookup["year"]}/{lookup["month"]}[dp]')
            else:
                q = None

            if q and len(ids) < MAX_CONTROLS:
                need  = MAX_CONTROLS - len(ids)
                print(f"  [Journal] L1 issue: {q}")
                ids  |= searchPubMedFiltered(q, excluded_pmids_journal | ids, need)
                level = max(level, 1)
                print(f"    -> {len(ids)} results")
            publication["Journal level 1: same issue"] = len(ids) - prev_len
            prev_len = len(ids)

            # Level 2: same journal + same year
            if len(ids) < MAX_CONTROLS:
                need  = MAX_CONTROLS - len(ids)
                new   = searchByYearOffsetFiltered(
                            lookup["journal_title"], lookup["year"],
                            offset=0, excluded=excluded_pmids_journal | ids,
                            max_keep=need)
                ids  |= new
                level = max(level, 2)
                print(f"  [Journal] L2 same year -> {len(ids)} total")
            publication["Journal level 2: same year"] = len(ids) - prev_len
            prev_len = len(ids)

            # Level 3: ± 1 year
            if len(ids) < MAX_CONTROLS:
                need  = MAX_CONTROLS - len(ids)
                new   = searchByYearOffsetFiltered(
                            lookup["journal_title"], lookup["year"],
                            offset=1, excluded=excluded_pmids_journal | ids,
                            max_keep=need)
                ids  |= new
                level = max(level, 3)
                print(f"  [Journal] L3 ±1 yr -> {len(ids)} total")
            publication["Journal level 3: +/- one year"] = len(ids) - prev_len
            prev_len = len(ids)

            # Level 4: ± 2 years
            if len(ids) < MAX_CONTROLS:
                need  = MAX_CONTROLS - len(ids)
                new   = searchByYearOffsetFiltered(
                            lookup["journal_title"], lookup["year"],
                            offset=2, excluded=excluded_pmids_journal | ids,
                            max_keep=need)
                ids  |= new
                level = max(level, 4)
                print(f"  [Journal] L4 ±2 yr -> {len(ids)} total")
            publication["Journal level 4: +/- two years"] = len(ids) - prev_len

            publication["Similar PMIDs"]         = list(ids)
            publication["Journal Level Reached"] = level
            excluded_pmids_journal              |= ids

            if len(ids) < MIN_CONTROL:
                publication["Thin Control"] = True
                print(f"  WARNING: only {len(ids)} journal controls for {issue}")
            elif "Thin Control" in publication:
                del publication["Thin Control"]

    # ------------------------------------------------------------------
    # 2. MeSH controls
    # ------------------------------------------------------------------
    if len(publication.get("MeSH PMIDs", [])) < MAX_CONTROLS:
        changed = True
        pmid = getPMID(issue, effective_pub)

        if not pmid:
            print("  [MeSH] Could not extract PMID — skipping")
            publication["MeSH PMIDs"]        = []
            publication["MeSH Level Reached"] = 0
        else:
            year        = publication.get('year')
            major_terms = []
            minor_terms = []
            mesh_error  = False

            try:
                major_terms, minor_terms = getMeSHTerms(pmid)
            except Exception as exc:
                print(f"  [MeSH] Error fetching terms: {exc}")
                mesh_error = True

            if mesh_error:
                publication["MeSH PMIDs"]        = []
                publication["MeSH Level Reached"] = 0
            elif not major_terms and not minor_terms:
                print("  [MeSH] No MeSH terms — article may not be indexed yet")
                publication["MeSH PMIDs"]        = []
                publication["MeSH Level Reached"] = 0
                publication["Thin MeSH Control"] = True
            else:
                print(f"  [MeSH] Major ({len(major_terms)}): {major_terms}")
                if minor_terms:
                    suffix = '...' if len(minor_terms) > 5 else ''
                    print(f"  [MeSH] Minor ({len(minor_terms)}): "
                          f"{minor_terms[:5]}{suffix}")

                # Pad major terms with minor terms until we have at least 5.
                MIN_SEARCH_TERMS = 5
                n_minor_needed = max(0, MIN_SEARCH_TERMS - len(major_terms))
                search_terms = major_terms + minor_terms[:n_minor_needed]
                if n_minor_needed:
                    print(f"  [MeSH] search_terms (padded to {len(search_terms)}): "
                          f"{search_terms}")

                # Preload any controls already collected so we only top up.
                ids      = set(publication.get("MeSH PMIDs", []))
                level    = publication.get("MeSH Level Reached", 0)
                prev_len = len(ids)
                if ids:
                    print(f"  [MeSH] Resuming with {len(ids)} existing controls")

                # Strategy 1: AND top-3 search_terms + year window
                if len(search_terms) >= 3 and len(ids) < MAX_CONTROLS:
                    need  = MAX_CONTROLS - len(ids)
                    q     = buildMeSHQuery(search_terms[:3], operator='AND', year=year)
                    print(f"  [MeSH] S1 AND top-3 ±{YEAR_OFFSET}yr: {q}")
                    ids  |= searchPubMedFiltered(q, excluded_pmids_mesh | ids, need)
                    level = max(level, 1)
                    print(f"    -> {len(ids)} results")
                publication["MeSH level 1"] = len(ids) - prev_len
                prev_len = len(ids)

                # Strategy 2: AND top-2 search_terms + year window
                if len(ids) < MAX_CONTROLS and len(search_terms) >= 2:
                    need  = MAX_CONTROLS - len(ids)
                    q     = buildMeSHQuery(search_terms[:2], operator='AND', year=year)
                    print(f"  [MeSH] S2 AND top-2 ±{YEAR_OFFSET}yr: {q}")
                    ids  |= searchPubMedFiltered(
                                q, excluded_pmids_mesh | ids, need)
                    level = max(level, 2)
                    print(f"    -> {len(ids)} results")
                publication["MeSH level 2"] = len(ids) - prev_len
                prev_len = len(ids)

                # Strategy 3: OR all search_terms + year window
                if len(ids) < MAX_CONTROLS and search_terms:
                    need  = MAX_CONTROLS - len(ids)
                    q     = buildMeSHQuery(search_terms, operator='OR', year=year)
                    print(f"  [MeSH] S3 OR all ±{YEAR_OFFSET}yr: {q}")
                    ids  |= searchPubMedFiltered(
                                q, excluded_pmids_mesh | ids, need)
                    level = max(level, 3)
                    print(f"    -> {len(ids)} results")
                publication["MeSH level 3"] = len(ids) - prev_len
                prev_len = len(ids)

                # Strategy 4: OR all search_terms, no year constraint
                if len(ids) < MAX_CONTROLS and search_terms:
                    need  = MAX_CONTROLS - len(ids)
                    q     = buildMeSHQuery(search_terms, operator='OR', year=None)
                    print(f"  [MeSH] S4 OR all no year: {q}")
                    ids  |= searchPubMedFiltered(
                                q, excluded_pmids_mesh | ids, need)
                    level = max(level, 4)
                    print(f"    -> {len(ids)} results")
                publication["MeSH level 4"] = len(ids) - prev_len

                publication["MeSH PMIDs"]        = list(ids)
                publication["MeSH Level Reached"] = level
                excluded_pmids_mesh             |= ids

                if len(ids) < MIN_CONTROL:
                    publication["Thin MeSH Control"] = True
                    print(f"  WARNING: only {len(ids)} MeSH controls for {issue}")
                elif "Thin MeSH Control" in publication:
                    del publication["Thin MeSH Control"]

    # Save after each article to guard against interruptions.
    if changed:
        with open(OUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_publications, f, ensure_ascii=False, indent=4)


# Final save.
with open(OUT_FILE, 'w', encoding='utf-8') as f:
    json.dump(all_publications, f, ensure_ascii=False, indent=4)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
thin_j  = [k for k, v in all_publications.items() if v.get("Thin Control")]
thin_m  = [k for k, v in all_publications.items() if v.get("Thin MeSH Control")]
no_mesh = [k for k, v in all_publications.items()
           if "MeSH PMIDs" in v and len(v["MeSH PMIDs"]) == 0]

print(f"\n--- Summary ---")
print(f"Total articles              : {len(all_publications)}")
print(f"Thin journal control (<{MIN_CONTROL}): {len(thin_j)}")
print(f"Thin MeSH control    (<{MIN_CONTROL}): {len(thin_m)}")
print(f"No MeSH terms / skipped     : {len(no_mesh)}")

if thin_j:
    print("\nThin journal controls:")
    for t in thin_j:
        print(f"  {t}: {len(all_publications[t].get('Similar PMIDs', []))} controls")
if thin_m:
    print("\nThin MeSH controls:")
    for t in thin_m:
        print(f"  {t}: {len(all_publications[t].get('MeSH PMIDs', []))} controls")
