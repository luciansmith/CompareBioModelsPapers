"""
validate_biomd_pubs.py
 
Validates biomd_publication_info.json against PubMed and CrossRef.
 
Outputs:
  biomd_validation_report.txt        — human-readable report for BioModels team
  biomd_publication_info_corrected.json — corrected copy of the source JSON
 
Usage:
  python validate_biomd_pubs.py [path/to/biomd_publication_info.json]
 
Defaults to searching for biomd_publication_info.json in the same directory
as the script, then the current working directory.
"""
 
import json
import os
import re
import sys
import time
import copy
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
 
from Bio import Entrez
 
# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
 
Entrez.email = "lpsmith@uw.edu"
CROSSREF_UA = "BioModels-validator/1.0 (mailto:lpsmith@uw.edu)"
 
# Throttle constants (seconds between requests)
PUBMED_SLEEP = 0.34   # ~3 req/sec
CROSSREF_SLEEP = 0.2

# ---------------------------------------------------------------------------
# Hardcoded preprint → published mappings
# CrossRef does not reliably return 'is-preprint-of' for these two entries,
# so the relationship is recorded here explicitly.
# Preprint metadata is kept in the corrected JSON (the BioModel was built from
# the preprint); this table is used only for the report's preprint section.
# ---------------------------------------------------------------------------
KNOWN_PREPRINTS: dict[str, dict] = {
    "10.1101/498741": {
        "published_doi":     "10.1016/j.jtbi.2020.110185",
        "published_journal": "Journal of Theoretical Biology",
        "published_year":    "2020",
    },
    "10.1101/565531": {
        "published_doi":     "10.1016/j.jtbi.2019.109999",
        "published_journal": "Journal of Theoretical Biology",
        "published_year":    "2019",
    },
}

# ---------------------------------------------------------------------------
# URL parsing helpers
# ---------------------------------------------------------------------------
 
def extract_pmid_from_key(key: str) -> str | None:
    """
    Parse a PMID from keys like:
      https://identifiers.org/pubmed/12345678
      http://identifiers.org/pubmed/12345678
      https://www.ncbi.nlm.nih.gov/pubmed/12345678
    Returns the PMID string or None.
    """
    m = re.search(r'pubmed[:/](\d+)', key, re.IGNORECASE)
    if m:
        return m.group(1)
    return None
 
 
def extract_doi_from_key(key: str) -> str | None:
    """
    Parse a DOI from keys like:
      http://identifiers.org/doi/10.1101/498741
      https://doi.org/10.1101/498741
      https://identifiers.org/doi/10.1016/j.jtbi.2007.01.019
    Returns the DOI string or None.
    """
    # identifiers.org/doi/ style
    m = re.search(r'identifiers\.org/doi/(.+)', key, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip('/')
    # doi.org/ style
    m = re.search(r'doi\.org/(.+)', key, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip('/')
    return None
 
 
# ---------------------------------------------------------------------------
# PubMed helpers
# ---------------------------------------------------------------------------
 
def pubmed_fetch_full(pmid: str) -> dict | None:
    """
    Fetch full PubMed XML for a PMID.
    Returns dict with keys: title, abstract, journal, journal_abbr, year, volume
    or None on failure.
    """
    try:
        time.sleep(PUBMED_SLEEP)
        handle = Entrez.efetch(db="pubmed", id=pmid, rettype="xml", retmode="xml")
        raw = handle.read()
        handle.close()
    except Exception as e:
        print(f"  [WARN] PubMed efetch failed for PMID {pmid}: {e}", file=sys.stderr)
        return None
 
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  [WARN] XML parse error for PMID {pmid}: {e}", file=sys.stderr)
        return None
 
    article = root.find(".//PubmedArticle")
    if article is None:
        print(f"  [WARN] No PubmedArticle element for PMID {pmid}", file=sys.stderr)
        return None
 
    def find_text(path, default=""):
        el = article.find(path)
        return el.text.strip() if el is not None and el.text else default
 
    # Title (may contain nested tags like <i>; gather all text)
    title_el = article.find(".//ArticleTitle")
    title = "".join(title_el.itertext()).strip() if title_el is not None else ""
 
    # Abstract (may have multiple AbstractText sections)
    abstract_parts = []
    for ab in article.findall(".//AbstractText"):
        label = ab.get("Label")
        text = "".join(ab.itertext()).strip()
        if label:
            abstract_parts.append(f"{label}: {text}")
        elif text:
            abstract_parts.append(text)
    abstract = " ".join(abstract_parts)
 
    # Journal
    journal = find_text(".//Journal/Title")
    journal_abbr = find_text(".//Journal/ISOAbbreviation")
 
    # Year — prefer PubDate/Year; fall back to MedlineDate parsing
    year = find_text(".//Journal/JournalIssue/PubDate/Year")
    if not year:
        medline_date = find_text(".//Journal/JournalIssue/PubDate/MedlineDate")
        m = re.match(r'(\d{4})', medline_date)
        year = m.group(1) if m else ""
 
    volume = find_text(".//Journal/JournalIssue/Volume")
 
    # Collect DOI from ArticleIdList
    article_doi = None
    for aid in article.findall(".//ArticleId"):
        if aid.get("IdType") == "doi":
            article_doi = aid.text.strip() if aid.text else None
            break
 
    return {
        "title": title,
        "abstract": abstract,
        "journal": journal,
        "journal_abbr": journal_abbr,
        "year": year,
        "volume": volume,
        "doi": article_doi,
    }
 
 
def pubmed_find_pmid_by_doi(doi: str) -> str | None:
    """
    Search PubMed for a DOI using the [aid] qualifier.
    Returns a confirmed PMID or None.
 
    PubMed's [aid] field has false positives, so we validate by fetching the
    record and checking that its own DOI field contains the searched DOI.
    """
    try:
        time.sleep(PUBMED_SLEEP)
        handle = Entrez.esearch(db="pubmed", term=f'"{doi}"[aid]', retmax=2)
        record = Entrez.read(handle)
        handle.close()
    except Exception as e:
        print(f"  [WARN] PubMed esearch failed for DOI {doi}: {e}", file=sys.stderr)
        return None
 
    ids = record.get("IdList", [])
    if len(ids) != 1:
        # 0 = not found; >1 = ambiguous
        return None
 
    pmid = ids[0]
    # Validate by fetching and checking the DOI field
    fetched = pubmed_fetch_full(pmid)
    if fetched is None:
        return None
 
    fetched_doi = fetched.get("doi") or ""
    # Case-insensitive substring match to handle minor formatting differences
    if doi.lower() in fetched_doi.lower() or fetched_doi.lower() in doi.lower():
        return pmid
 
    print(
        f"  [INFO] PubMed [aid] returned PMID {pmid} for DOI {doi}, "
        f"but its DOI field is '{fetched_doi}' — rejecting.",
        file=sys.stderr,
    )
    return None
 
 
# ---------------------------------------------------------------------------
# CrossRef helper
# ---------------------------------------------------------------------------
 
def crossref_fetch(doi: str) -> dict | None:
    """
    Fetch metadata from CrossRef for a DOI.
    Returns dict with: title, journal, year, volume, type, published_doi,
                       is_preprint_of (str|None)
    or None on failure.
    """
    encoded = urllib.parse.quote(doi, safe="")
    url = f"https://api.crossref.org/works/{encoded}"
    req = urllib.request.Request(url, headers={"User-Agent": CROSSREF_UA})
    try:
        time.sleep(CROSSREF_SLEEP)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [WARN] CrossRef fetch failed for DOI {doi}: {e}", file=sys.stderr)
        return None
 
    msg = data.get("message", {})
 
    def first_str(lst):
        if isinstance(lst, list) and lst:
            return str(lst[0])
        return ""
 
    title = first_str(msg.get("title", []))
 
    # Journal / container title
    journal = first_str(msg.get("container-title", []))
 
    # Year from published-print or published-online or created
    year = ""
    for date_field in ("published-print", "published-online", "created"):
        date_parts = msg.get(date_field, {}).get("date-parts", [[]])
        if date_parts and date_parts[0]:
            year = str(date_parts[0][0])
            break
 
    volume = msg.get("volume", "")
    cr_type = msg.get("type", "")
 
    # Published DOI (i.e. published version of a preprint)
    published_doi = None
    relation = msg.get("relation", {})
    is_preprint_of_list = relation.get("is-preprint-of", [])
    if is_preprint_of_list:
        # Take the first DOI-type relation
        for rel in is_preprint_of_list:
            if rel.get("id-type") == "doi":
                published_doi = rel.get("id")
                break
 
    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": str(volume),
        "type": cr_type,
        "published_doi": published_doi,
        "is_preprint_of": published_doi,
    }
 
 
# ---------------------------------------------------------------------------
# Field comparison helper
# ---------------------------------------------------------------------------
 
def normalise(val) -> str:
    """Normalise a field value to a comparable string."""
    if val is None:
        return ""
    s = str(val).strip()
    # Collapse internal whitespace
    s = re.sub(r'\s+', ' ', s)
    return s
 
 
def fields_match(stored, pubmed) -> bool:
    """Loose comparison: case-insensitive, whitespace-collapsed."""
    return normalise(stored).lower() == normalise(pubmed).lower()
 
 
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
 
def main():
    # ------------------------------------------------------------------
    # Locate input file
    # ------------------------------------------------------------------
    if len(sys.argv) > 1:
        input_path = Path(sys.argv[1])
    else:
        # Try directory of this script first, then cwd
        candidates = [
            Path(__file__).parent / "biomd_publication_info.json",
            Path.cwd() / "biomd_publication_info.json",
        ]
        input_path = next((p for p in candidates if p.exists()), candidates[0])
 
    if not input_path.exists():
        sys.exit(f"ERROR: Cannot find {input_path}")
 
    output_dir = input_path.parent
    report_path = output_dir / "biomd_validation_report.txt"
    corrected_path = output_dir / "biomd_publication_info_corrected.json"
 
    print(f"Reading {input_path} …")
    with open(input_path, encoding="utf-8") as f:
        data: dict = json.load(f)
 
    corrected = copy.deepcopy(data)
 
    # ------------------------------------------------------------------
    # 1. Deduplicate: same PMID under multiple keys
    # ------------------------------------------------------------------
    print("Checking for duplicate PMIDs …")
    pmid_to_keys: dict[str, list[str]] = defaultdict(list)
    for key in data:
        pmid = extract_pmid_from_key(key)
        if pmid:
            pmid_to_keys[pmid].append(key)
 
    duplicates: dict[str, list[str]] = {
        pmid: keys for pmid, keys in pmid_to_keys.items() if len(keys) > 1
    }
 
    # Map duplicate PMIDs → BioModel IDs
    dup_biomodels: dict[str, list[str]] = {}
    for pmid, keys in duplicates.items():
        bm_ids = []
        for k in keys:
            bm_ids.extend(data[k].get("BioModel(s)", []))
        dup_biomodels[pmid] = sorted(set(bm_ids))
 
    # ------------------------------------------------------------------
    # 2 & 3. Per-entry PubMed/CrossRef validation
    # ------------------------------------------------------------------
 
    # issues[biomodel_id] = list of issue strings
    issues: dict[str, list[str]] = defaultdict(list)
    # preprints[biomodel_id] = {preprint_doi, published_doi, pub_journal, pub_year}
    preprints: dict[str, dict] = {}
 
    total = len(data)
    for idx, (key, entry) in enumerate(data.items(), 1):
        bm_ids: list[str] = entry.get("BioModel(s)", ["UNKNOWN"])
        bm_label = ", ".join(bm_ids)
        print(f"  [{idx}/{total}] {key}  ({bm_label})")
 
        pmid = extract_pmid_from_key(key)
        doi = extract_doi_from_key(key)
 
        pubmed_data: dict | None = None
        crossref_data: dict | None = None
        confirmed_pmid: str | None = pmid  # None for DOI-only entries until confirmed
 
        # ---- PMID-keyed entry ----
        if pmid:
            pubmed_data = pubmed_fetch_full(pmid)
 
        # ---- DOI-keyed entry ----
        elif doi:
            crossref_data = crossref_fetch(doi)
 
            # Try to find/confirm a PMID via PubMed
            found_pmid = pubmed_find_pmid_by_doi(doi)
            if found_pmid:
                confirmed_pmid = found_pmid
                pubmed_data = pubmed_fetch_full(found_pmid)
                # Record confirmed PMID in corrected JSON
                for bm_id in bm_ids:
                    corrected[key]["confirmed_pmid"] = found_pmid
 
            # Preprint detection — hardcoded table takes priority over CrossRef
            if doi in KNOWN_PREPRINTS:
                kp = KNOWN_PREPRINTS[doi]
                for bm_id in bm_ids:
                    preprints[bm_id] = {
                        "preprint_doi":      doi,
                        "published_doi":     kp["published_doi"],
                        "published_journal": kp["published_journal"],
                        "published_year":    kp["published_year"],
                    }
            elif crossref_data and crossref_data.get("is_preprint_of"):
                pub_doi = crossref_data["is_preprint_of"]
                pub_journal = ""
                pub_year = ""
                # Try to get published version metadata from CrossRef
                pub_cr = crossref_fetch(pub_doi)
                if pub_cr:
                    pub_journal = pub_cr.get("journal", "")
                    pub_year = pub_cr.get("year", "")
                for bm_id in bm_ids:
                    preprints[bm_id] = {
                        "preprint_doi":      doi,
                        "published_doi":     pub_doi,
                        "published_journal": pub_journal,
                        "published_year":    pub_year,
                    }
            elif crossref_data and crossref_data.get("type") in (
                "posted-content", "preprint"
            ):
                # CrossRef type indicates preprint but no published version found
                for bm_id in bm_ids:
                    preprints[bm_id] = {
                        "preprint_doi":      doi,
                        "published_doi":     None,
                        "published_journal": "",
                        "published_year":    "",
                    }
 
        # ---- Field comparison (uses PubMed as ground truth) ----
        if pubmed_data:
            stored_journal = normalise(entry.get("journal", ""))
            stored_year = normalise(entry.get("year", entry.get("month", "")))
            stored_volume = normalise(entry.get("volume", ""))
            stored_title = normalise(entry.get("title", ""))
            stored_abstract = normalise(entry.get("abstract", ""))
 
            pm_journal = normalise(pubmed_data["journal"])
            pm_abbr = normalise(pubmed_data["journal_abbr"])
            pm_year = normalise(pubmed_data["year"])
            pm_volume = normalise(pubmed_data["volume"])
            pm_title = normalise(pubmed_data["title"])
            pm_abstract = normalise(pubmed_data["abstract"])
 
            def flag(field, stored_val, pm_val, pm_alt=None):
                """Flag a mismatch; apply correction to corrected dict."""
                if stored_val.lower() == pm_val.lower():
                    return
                if pm_alt and stored_val.lower() == pm_alt.lower():
                    return  # matches abbreviation or alt form
                msg = (
                    f"  Field '{field}': stored={stored_val!r}  "
                    f"PubMed={pm_val!r}"
                )
                if pm_alt:
                    msg += f"  (abbr={pm_alt!r})"
                for bm_id in bm_ids:
                    issues[bm_id].append(f"[{key}]\n{msg}")
 
            # Journal: accept either full name or ISO abbreviation
            flag("journal", stored_journal, pm_journal, pm_abbr)
            flag("year", stored_year, pm_year)
            if stored_volume or pm_volume:
                flag("volume", stored_volume, pm_volume)
            if stored_title:
                flag("title", stored_title, pm_title)
            if stored_abstract:
                flag("abstract", stored_abstract, pm_abstract)
 
            # Apply corrections to corrected JSON (only for confirmed PubMed data)
            def apply_correction(field, pm_val, stored_val):
                if pm_val and not fields_match(stored_val, pm_val):
                    corrected[key][field] = pm_val
 
            apply_correction("journal", pm_journal, stored_journal)
            apply_correction("year", pm_year, stored_year)
            if stored_volume or pm_volume:
                apply_correction("volume", pm_volume, stored_volume)
            if "title" in entry:
                apply_correction("title", pm_title, stored_title)
            if "abstract" in entry:
                apply_correction("abstract", pm_abstract, stored_abstract)
 
        elif pubmed_data is None and pmid:
            for bm_id in bm_ids:
                issues[bm_id].append(
                    f"[{key}]\n  PubMed fetch failed for PMID {pmid}"
                )
        elif pubmed_data is None and doi and crossref_data is None:
            for bm_id in bm_ids:
                issues[bm_id].append(
                    f"[{key}]\n  CrossRef and PubMed lookup both failed for DOI {doi}"
                )
 
    # ------------------------------------------------------------------
    # Write corrected JSON
    # ------------------------------------------------------------------
    print(f"\nWriting {corrected_path} …")
    with open(corrected_path, "w", encoding="utf-8") as f:
        json.dump(corrected, f, indent=2, ensure_ascii=False)
 
    # ------------------------------------------------------------------
    # Write report
    # ------------------------------------------------------------------
    print(f"Writing {report_path} …")
    lines = []
 
    def section(title: str):
        lines.append("")
        lines.append("=" * 72)
        lines.append(title)
        lines.append("=" * 72)
 
    lines.append("BioModels Publication Validation Report")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    lines.append(f"Source: {input_path}")
    lines.append(f"Entries checked: {total}")
 
    # --- Duplicates ---
    section("1. DUPLICATE PMIDs (same PMID under multiple keys)")
    if duplicates:
        for pmid, keys in sorted(duplicates.items()):
            bm_ids = dup_biomodels.get(pmid, [])
            lines.append(f"\nPMID {pmid}  →  BioModel(s): {', '.join(bm_ids)}")
            for k in keys:
                lines.append(f"    {k}")
    else:
        lines.append("  No duplicates found.")
 
    # --- Field mismatches, grouped by BioModel ID ---
    section("2. FIELD MISMATCHES (grouped by BioModel ID)")
    all_bm_ids_with_issues = sorted(issues.keys())
    if all_bm_ids_with_issues:
        for bm_id in all_bm_ids_with_issues:
            lines.append(f"\n--- {bm_id} ---")
            for issue in issues[bm_id]:
                lines.append(issue)
    else:
        lines.append("  No field mismatches found.")
 
    # --- Preprints ---
    section("3. PREPRINTS WITH KNOWN PUBLISHED VERSIONS")
    preprints_with_pub = {
        bm: info for bm, info in preprints.items() if info["published_doi"]
    }
    preprints_no_pub = {
        bm: info for bm, info in preprints.items() if not info["published_doi"]
    }
 
    if preprints_with_pub:
        lines.append("\nBioModels where the stored entry is a preprint that has since been published:")
        lines.append(
            "NOTE: Corrected JSON retains preprint metadata. Review manually.\n"
        )
        for bm_id, info in sorted(preprints_with_pub.items()):
            lines.append(f"  {bm_id}")
            lines.append(f"    Preprint DOI  : {info['preprint_doi']}")
            lines.append(f"    Published DOI : {info['published_doi']}")
            if info["published_journal"]:
                lines.append(f"    Published in  : {info['published_journal']}")
            if info["published_year"]:
                lines.append(f"    Published year: {info['published_year']}")
            lines.append("")
    else:
        lines.append("  No preprints with a confirmed published version found.")
 
    if preprints_no_pub:
        lines.append("\nBioModels with preprint entries (no published version found yet):")
        for bm_id, info in sorted(preprints_no_pub.items()):
            lines.append(f"  {bm_id}  —  preprint DOI: {info['preprint_doi']}")
 
    # --- Summary ---
    section("SUMMARY")
    lines.append(f"  Entries checked          : {total}")
    lines.append(f"  Duplicate PMIDs          : {len(duplicates)}")
    lines.append(f"  BioModels with issues    : {len(issues)}")
    lines.append(f"  Preprints (pub. known)   : {len(preprints_with_pub)}")
    lines.append(f"  Preprints (no pub. ver.) : {len(preprints_no_pub)}")
    lines.append("")
 
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
 
    print("Done.")
    print(f"  Report   : {report_path}")
    print(f"  Corrected: {corrected_path}")
 
 
if __name__ == "__main__":
    main()