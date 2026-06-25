#!/usr/bin/env python3
"""
Resolve PMID / PMCID / DOI for every entry in biomd_publication_info_with_controls.json
and write biomd_publication_info_resolved.json.

Run once before download_papers.py.  Re-run safely to pick up new entries or
fill in fields that were null on the previous run.

Usage:
  python resolve_ids.py                  # resolve all unresolved entries
  python resolve_ids.py --retry-missing  # also retry entries where pmcid or doi is still null
  python resolve_ids.py --force          # re-resolve everything from scratch
  python resolve_ids.py --count 50       # limit to 50 entries this run

Resolution strategy (for numeric PMID entries):
  A. PMC ID Converter  -- batch of 100, gives pmcid + doi for most
  B. esummary          -- fills in missing doi
  C. elink             -- fills in missing pmcid, one at a time

Progress is saved to disk after every API batch so a crash or Ctrl-C is
recoverable: just re-run and it picks up where it left off.

DOI-only entries (accession starts with '10.', i.e. the source BioModels
data only linked this paper by DOI, not PMID): doi=<accession>, and then
stage D (esearch by DOI) checks whether PubMed has indexed it anyway --
pmid is set to the real PMID if one is found, else stays None (most DOI-only
entries genuinely have no PMID; that's an expected negative, not an error).

URL entries (accession is a plain URL) have no PMID/PMCID/DOI in any database:
  pmid=None, pmcid=None, doi=None, _resolved=True.
"""

import json
import os
import re
import tempfile
import time
import argparse
from pathlib import Path

from strategy_utils import SCRIPT_DIR, EMAIL, NCBI_DELAY, ncbi_session

INPUT_FILE  = SCRIPT_DIR / "biomd_publication_info_with_controls.json"
OUTPUT_FILE = SCRIPT_DIR / "biomd_publication_info_resolved.json"


# -- I/O helpers --------------------------------------------------------------

def _save(resolved):
    """Atomically write the resolved dict to OUTPUT_FILE."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=SCRIPT_DIR, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(resolved, indent=2, ensure_ascii=False))
        os.replace(tmp_path, OUTPUT_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# -- NCBI resolution stages ---------------------------------------------------

def _stage_a_idconv(pmids, resolved, key_by_pmid):
    """
    Stage A: PMC ID Converter -- batch of 100.
    Fills in pmcid + doi for most PMID entries.
    Saves after every batch.
    """
    idconv_url = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
    total_batches = (len(pmids) + 99) // 100
    for i in range(0, len(pmids), 100):
        batch = pmids[i:i + 100]
        batch_num = i // 100 + 1
        print(f"  [A] idconv batch {batch_num}/{total_batches} ({len(batch)} PMIDs) ...",
              end="", flush=True)
        try:
            r = ncbi_session.get(
                idconv_url,
                params={"ids": ",".join(batch), "format": "json",
                        "tool": "BiomodelsResolver", "email": EMAIL},
                timeout=30,
            )
            r.raise_for_status()
            for rec in r.json().get("records", []):
                pmid = rec.get("pmid")
                if pmid and pmid in key_by_pmid:
                    key = key_by_pmid[pmid]
                    resolved[key]["pmcid"] = rec.get("pmcid") or None
                    resolved[key]["doi"]   = rec.get("doi")   or None
            print(" OK")
        except Exception as e:
            print(f" ERROR: {e}")
        time.sleep(NCBI_DELAY)
        _save(resolved)


def _stage_b_esummary(pmids, resolved, key_by_pmid):
    """
    Stage B: esummary -- fills in doi for PMIDs that idconv missed.
    Saves after every batch of 20.
    """
    missing = [p for p in pmids
               if not resolved[key_by_pmid[p]].get("doi")
               and not resolved[key_by_pmid[p]].get("doi_checked")]
    if not missing:
        print("  [B] esummary: nothing to do (all have doi)")
        return
    print(f"  [B] esummary: {len(missing)} PMIDs missing doi ...")
    esum_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
    for i in range(0, len(missing), 20):
        batch = missing[i:i + 20]
        try:
            r = ncbi_session.get(
                esum_url,
                params={"db": "pubmed", "id": ",".join(batch),
                        "retmode": "json", "tool": "BiomodelsResolver",
                        "email": EMAIL},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json().get("result", {})
            for pmid in batch:
                for aid in data.get(pmid, {}).get("articleids", []):
                    if aid.get("idtype") == "doi":
                        resolved[key_by_pmid[pmid]]["doi"] = aid.get("value")
                        break
                # Mark checked whether or not a DOI was found
                resolved[key_by_pmid[pmid]]["doi_checked"] = True
        except Exception as e:
            print(f"    WARNING  esummary error (batch {i // 20 + 1}): {e}")
        time.sleep(NCBI_DELAY)
        _save(resolved)


def _stage_d_doi_to_pmid(doi_keys, resolved):
    """
    Stage D: PubMed esearch by DOI -- looks for a real PMID for DOI-only
    entries (accession is a bare DOI, e.g. from a doi/-style identifiers.org
    link in the source BioModels data).

    Most of these will find nothing -- a lot of older/non-biomedical-journal
    DOIs were simply never indexed in PubMed at all, which is a genuine
    negative, not an error. When a PMID IS found, it's saved so
    download_papers.py can use the real PMID instead of falling back to the
    bare DOI string (which is not a valid pmid_numeric value).

    One request at a time (esearch has no reliable multi-DOI batch mode),
    checked-state cached so re-runs don't repeat lookups.
    Returns the list of (key, pmid) pairs found, for follow-up pmcid lookup.
    """
    missing = [k for k in doi_keys
               if not resolved[k].get("pmid") and not resolved[k].get("pmid_checked")]
    if not missing:
        print("  [D] doi->pmid: nothing to do (all checked)")
        return []
    print(f"  [D] doi->pmid: checking {len(missing)} DOI-only entries against PubMed ...")
    esearch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    found = []
    for idx, key in enumerate(missing, 1):
        doi = resolved[key]["doi"]
        try:
            r = ncbi_session.get(
                esearch_url,
                params={"db": "pubmed", "term": f"{doi}[doi]",
                        "retmode": "json", "tool": "BiomodelsResolver",
                        "email": EMAIL},
                timeout=30,
            )
            r.raise_for_status()
            idlist = r.json().get("esearchresult", {}).get("idlist", [])
            if idlist:
                resolved[key]["pmid"] = idlist[0]
                found.append((key, idlist[0]))
                print(f"    {doi} -> PMID {idlist[0]}")
            resolved[key]["pmid_checked"] = True
        except Exception as e:
            print(f"    WARNING  esearch error ({doi}): {e}")
        time.sleep(NCBI_DELAY)
        _save(resolved)
        if idx % 20 == 0:
            print(f"    ... {idx}/{len(missing)}")
    print(f"  [D] found {len(found)} real PMID(s) for {len(missing)} DOI-only entries checked")
    return found


def _stage_c_elink(pmids, resolved, key_by_pmid):
    """
    Stage C: elink -- fills in pmcid for PMIDs that idconv missed, one at a time.
    Retries on transient errors (premature response, 500s) with backoff.
    Saves after every call.
    """
    missing = [p for p in pmids
               if not resolved[key_by_pmid[p]].get("pmcid")
               and not resolved[key_by_pmid[p]].get("pmcid_checked")]
    if not missing:
        print("  [C] elink: nothing to do (all have pmcid)")
        return
    print(f"  [C] elink: {len(missing)} PMIDs missing pmcid ...")
    elink_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
    BACKOFFS   = [5, 15, 45]   # seconds to wait before each retry
    for idx, pmid in enumerate(missing, 1):
        for attempt, backoff in enumerate([0] + BACKOFFS):
            if backoff:
                print(f"    retrying {pmid} in {backoff}s ...", end="", flush=True)
                time.sleep(backoff)
            try:
                r = ncbi_session.get(
                    elink_url,
                    params={"dbfrom": "pubmed", "db": "pmc", "id": pmid,
                            "retmode": "json", "linkname": "pubmed_pmc",
                            "tool": "BiomodelsResolver", "email": EMAIL},
                    timeout=60,
                )
                r.raise_for_status()
                data = r.json(strict=False)
                for linkset in data.get("linksets", []):
                    for lsdb in linkset.get("linksetdbs", []):
                        if (lsdb.get("dbto") == "pmc" and
                                lsdb.get("linkname") == "pubmed_pmc"):
                            pmc_ids = lsdb.get("links", [])
                            if pmc_ids:
                                resolved[key_by_pmid[pmid]]["pmcid"] = f"PMC{pmc_ids[0]}"
                            break
                resolved[key_by_pmid[pmid]]["pmcid_checked"] = True
                break  # success -- exit retry loop
            except Exception as e:
                if attempt < len(BACKOFFS):
                    print(f"    WARNING  elink transient error ({pmid}): {e}")
                else:
                    print(f"    WARNING  elink gave up ({pmid}) after {len(BACKOFFS)+1} attempts: {e}")
        time.sleep(NCBI_DELAY)
        _save(resolved)
        if idx % 20 == 0:
            print(f"    ... {idx}/{len(missing)}")


# -- Main ---------------------------------------------------------------------

def _print_summary(resolved):
    n_pmcid = sum(1 for v in resolved.values() if v.get("pmcid"))
    n_doi   = sum(1 for v in resolved.values() if v.get("doi"))
    n_res   = sum(1 for v in resolved.values() if v.get("_resolved"))
    n_unres = len(resolved) - n_res
    print(f"\n{OUTPUT_FILE.name}: {len(resolved)} entries  |  "
          f"Resolved: {n_res}  |  Has PMCID: {n_pmcid}  |  Has DOI: {n_doi}"
          + (f"  |  Still unresolved: {n_unres}" if n_unres else ""))


def main():
    parser = argparse.ArgumentParser(
        description="Resolve PMID/PMCID/DOI for all entries and write "
                    "biomd_publication_info_resolved.json."
    )
    parser.add_argument("--retry-missing", action="store_true",
                        help="Re-query entries that are resolved but still have "
                             "null pmcid or null doi (re-runs stages B + C only).")
    parser.add_argument("--force", action="store_true",
                        help="Re-resolve all entries from scratch.")
    parser.add_argument("--count", type=int, default=0,
                        help="Resolve at most N entries this run (0 = all).")
    args = parser.parse_args()

    data = json.loads(INPUT_FILE.read_text(encoding="utf-8"))

    # Load existing resolved data for incremental updates
    if OUTPUT_FILE.exists() and not args.force:
        resolved = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    else:
        resolved = {}

    # Ensure every source key is present
    for key in data:
        if key not in resolved:
            resolved[key] = dict(data[key])

    # Determine which entries need work
    def needs_resolve(key):
        entry = resolved[key]
        if args.force:
            return True
        if not entry.get("_resolved"):
            return True
        if args.retry_missing:
            acc = entry.get("accession", "")
            if re.match(r"^\d+$", acc):   # PMID entry
                missing_pmcid = not entry.get("pmcid") and not entry.get("pmcid_checked")
                missing_doi   = not entry.get("doi") and not entry.get("doi_checked")
                return missing_pmcid or missing_doi
            if re.match(r"^10\.", acc):   # DOI-only entry
                missing_pmid = not entry.get("pmid") and not entry.get("pmid_checked")
                return missing_pmid
        return False

    to_resolve = [k for k in data if needs_resolve(k)]
    if args.count:
        to_resolve = to_resolve[:args.count]

    print(f"Entries to resolve: {len(to_resolve)} / {len(data)}")
    if not to_resolve:
        print("Nothing to do.")
        _print_summary(resolved)
        return

    # Classify entries by accession type
    numeric_keys = [k for k in to_resolve
                    if re.match(r"^\d+$", resolved[k].get("accession", ""))]
    doi_keys     = [k for k in to_resolve
                    if re.match(r"^10\.", resolved[k].get("accession", ""))]
    url_keys     = [k for k in to_resolve
                    if k not in set(numeric_keys) | set(doi_keys)]

    # URL entries: no PMID, PMCID, or DOI in any database
    if url_keys:
        for key in url_keys:
            acc = resolved[key]["accession"]
            resolved[key].update({"pmid": None, "pmcid": None, "doi": None, "_resolved": True})
            print(f"  URL entry (no PMID/DOI): {acc}")
        _save(resolved)

    # DOI-only entries: accession is the DOI itself (no NCBI lookup needed for
    # that), but we still check PubMed in case a real PMID exists for it.
    if doi_keys:
        for key in doi_keys:
            acc = resolved[key]["accession"]
            resolved[key].update({"pmcid": None, "doi": acc, "doi_checked": True})
            resolved[key].setdefault("pmid", None)
        print(f"  Marked {len(doi_keys)} DOI-only entries (doi known; checking PubMed for a PMID)")
        _save(resolved)

        found = _stage_d_doi_to_pmid(doi_keys, resolved)
        if found:
            new_pmids      = [pmid for _, pmid in found]
            key_by_new_pmid = {pmid: key for key, pmid in found}
            orig_doi        = {key: resolved[key]["doi"] for key, _ in found}
            print(f"  [D] enriching {len(found)} newly-found PMID(s) via idconv/elink ...")
            _stage_a_idconv(new_pmids, resolved, key_by_new_pmid)
            # idconv overwrites doi with whatever it returns (or None if it
            # doesn't recognize the pmid yet) -- the accession's doi is already
            # authoritative, so keep it no matter what idconv says.
            for key, _ in found:
                resolved[key]["doi"] = orig_doi[key]
            _stage_c_elink(new_pmids, resolved, key_by_new_pmid)

        for key in doi_keys:
            resolved[key]["_resolved"] = True
        _save(resolved)

    # PMID entries: three-stage NCBI resolution, saving after every batch
    if numeric_keys:
        for key in numeric_keys:
            acc = resolved[key]["accession"]
            resolved[key]["pmid"] = acc
            if args.force:
                resolved[key]["pmcid"] = None
                resolved[key]["doi"]   = None

        pmids       = [resolved[k]["accession"] for k in numeric_keys]
        key_by_pmid = {resolved[k]["accession"]: k for k in numeric_keys}

        print(f"Resolving {len(pmids)} PMID entries via NCBI ...")

        if args.retry_missing and not args.force:
            print("  [A] idconv: skipped (--retry-missing only re-runs B + C)")
        else:
            _stage_a_idconv(pmids, resolved, key_by_pmid)

        _stage_b_esummary(pmids, resolved, key_by_pmid)
        _stage_c_elink(pmids, resolved, key_by_pmid)

        for key in numeric_keys:
            resolved[key]["_resolved"] = True
        _save(resolved)

        for key in numeric_keys:
            e = resolved[key]
            status = f"pmcid={str(e.get('pmcid') or '-'):14s}  doi={e.get('doi') or '-'}"
            print(f"  {e['pmid']}: {status}")

    _print_summary(resolved)


if __name__ == "__main__":
    main()
