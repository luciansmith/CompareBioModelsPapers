#!/usr/bin/env python3
"""
Download PDFs for Biomodels papers listed in biomd_publication_info_with_controls.json.

Strategy (tried in order for each paper):
  1. PubMed Central (PMC) — free, works without VPN
  2. Europe PMC — fallback for PMC papers
  3. Unpaywall — finds open-access versions of paywalled papers
  4. Direct DOI — uses your UW VPN for subscribed journals, with
                   publisher-specific PDF URL patterns

Usage:
  python download_papers.py                          # first 10 new papers
  python download_papers.py --count 50              # first 50 new papers
  python download_papers.py --start 10 --count 10  # papers 11-20 (new only)
  python download_papers.py --all                   # all new papers
  python download_papers.py --all --skip tried      # skip all previously attempted
  python download_papers.py --all --skip auth       # retry everything except auth-blocked
  python download_papers.py --all --skip none       # reprocess everything

Requirements:
  pip install requests

Run with UW VPN active (BIG-IP Edge Client) for best journal access.
"""

import json
import re
import sys
import time
import argparse
from pathlib import Path

# ── Shared infrastructure ─────────────────────────────────────────────────────
from strategy_utils import (
    SCRIPT_DIR, OUTPUT_DIR,
    NCBI_DELAY, GENERAL_DELAY,
    safe_filename,
    HAS_SCHOLARLY, _NO_SUBSCRIPTION,
)

# ── Strategies ────────────────────────────────────────────────────────────────
from pmc_strategy            import try_pmc
from pubmed_page_strategy    import try_pubmed_page
from ebsco_strategy          import try_ebsco
from libkey_strategy         import try_libkey
from unpaywall_strategy      import try_unpaywall
from semantic_scholar_strategy import try_semantic_scholar
from scholarly_strategy      import try_scholarly
from direct_doi_strategy     import try_direct_doi

# ── File paths ────────────────────────────────────────────────────────────────
RESOLVED_FILE = SCRIPT_DIR / "biomd_publication_info_resolved.json"
TRACKING_FILE = SCRIPT_DIR / "download_tracking.json"
LOCK_FILE     = SCRIPT_DIR / "download_tracking.lock"

# ── Tracking JSON ─────────────────────────────────────────────────────────────
def _acquire_lock():
    """Spin on exclusive file creation — atomic and cross-platform."""
    while True:
        try:
            open(LOCK_FILE, 'x').close()
            return
        except FileExistsError:
            time.sleep(0.05)

def _release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass

def load_tracking():
    return json.loads(TRACKING_FILE.read_text()) if TRACKING_FILE.exists() else {}

def save_tracking(tracking, pmid):
    """Lock the tracking file, read it, update just this entry, write atomically, unlock."""
    import tempfile, os
    print("    [tracking] acquiring lock ...", end="", flush=True)
    _acquire_lock()
    try:
        print(" writing ...", end="", flush=True)
        on_disk = json.loads(TRACKING_FILE.read_text()) if TRACKING_FILE.exists() else {}
        on_disk[pmid] = tracking[pmid]
        tmp_fd, tmp_path = tempfile.mkstemp(dir=SCRIPT_DIR, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(json.dumps(on_disk, indent=2))
            os.replace(tmp_path, TRACKING_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    finally:
        _release_lock()
        print(" done.")


def _should_update_resolved(old_status, new_status):
    """
    Return True only when something genuinely new happened:
      - Paper never attempted before (old_status is None)
      - Previously failed in some way, now succeeded
    Never update if the paper was already downloaded (don't downgrade),
    or if it was failing before and is still failing.
    """
    if old_status is None:
        return True                            # first attempt
    if old_status == "downloaded":
        return False                           # never overwrite a success
    return new_status == "downloaded"          # upgrade: failure → success


def save_resolved(data, pmid, tracking_entry):
    """
    Write the download result back into the resolved JSON file.
    Only the 'download' sub-key is touched; all other fields are preserved.
    Uses an atomic write so a crash mid-write doesn't corrupt the file.
    """
    import tempfile, os
    download_info = {k: v for k, v in tracking_entry.items()
                     if k in ("status", "filename", "source", "manual_url")}
    try:
        on_disk = json.loads(RESOLVED_FILE.read_text(encoding="utf-8"))
        if pmid in on_disk:
            on_disk[pmid]["download"] = download_info
        tmp_fd, tmp_path = tempfile.mkstemp(dir=SCRIPT_DIR, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(on_disk, indent=2, ensure_ascii=False))
            os.replace(tmp_path, RESOLVED_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"    [resolved] write error: {e}")

# ── Download orchestration ────────────────────────────────────────────────────
def download_paper(pmid, title, pmc_info, tracking, tracking_key,
                   verbose=False, pmc_only=False, ebsco_only=False):
    pmcid        = pmc_info.get("pmcid")
    doi          = pmc_info.get("doi")
    pmid_numeric = pmc_info.get("pmid_numeric") or pmid

    if pmc_only and not pmcid:
        return None

    filename     = safe_filename(pmid_numeric, title)
    path         = OUTPUT_DIR / filename
    print(f"\n  PMID {pmid}  PMC {pmcid or '---'}  DOI {doi or '---'}")
    print(f"  {title[:80]}")

    if ebsco_only:
        print("    [3] EBSCO ...        ", end="", flush=True)
        time.sleep(GENERAL_DELAY)
        src = try_ebsco(pmid_numeric, doi, path, verbose=verbose)
        if src:
            print("OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "ebsco", "doi": doi, "pmcid": pmcid}
            return True
        print("X")
        tracking[tracking_key] = {"status": "failed", "doi": doi, "pmcid": pmcid}
        return False

    # 1. PubMed Central (known PMCID) -----------------------------------------
    if pmcid:
        print("    [1] PMC ...          ", end="", flush=True)
        src = try_pmc(pmcid, path, verbose=verbose)
        if src:
            print("OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "pmc", "pmcid": pmcid, "doi": doi}
            return True
        print("X")

    if pmc_only:
        tracking[tracking_key] = {"status": "failed", "doi": doi, "pmcid": pmcid}
        return False

    # 2. PubMed page (may reveal PMC ID or free links) ------------------------
    print("    [2] PubMed page ...  ", end="", flush=True)
    time.sleep(NCBI_DELAY)
    src, found_pmcid = try_pubmed_page(pmid_numeric, path)
    if src:
        print("OK")
        tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                           "source": "pubmed_page", "pmcid": found_pmcid, "doi": doi}
        return True
    if found_pmcid and found_pmcid != pmcid:
        print(f"X (found {found_pmcid})")
        print("    [2b] PMC (new) ...   ", end="", flush=True)
        src = try_pmc(found_pmcid, path, verbose=verbose)
        if src:
            print("OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "pmc", "pmcid": found_pmcid, "doi": doi}
            return True
        print("X")
    else:
        print("X")

    # 3. EBSCO Research (UW Library full-text database) -----------------------
    print("    [3] EBSCO ...        ", end="", flush=True)
    time.sleep(GENERAL_DELAY)
    src = try_ebsco(pmid_numeric, doi, path, verbose=verbose)
    if src:
        print("OK")
        tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                           "source": "ebsco", "doi": doi, "pmcid": pmcid}
        return True
    else:
        print("X")

    # 4. LibKey / ThirdIron (UW library link resolver -- needs UW VPN) --------
    print("    [4] LibKey ...       ", end="", flush=True)
    time.sleep(GENERAL_DELAY)
    src = try_libkey(pmid_numeric, doi, path, verbose=verbose)
    if src is _NO_SUBSCRIPTION:
        print("no UW subscription")
        tracking[tracking_key] = {"status": "no_subscription", "doi": doi, "pmcid": pmcid}
        # Fall through to open-access steps
    elif src:
        print("OK")
        tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                           "source": "libkey", "doi": doi, "pmcid": pmcid}
        return True
    else:
        print("X")

    # 5. Unpaywall ------------------------------------------------------------
    if doi:
        print("    [5] Unpaywall ...    ", end="", flush=True)
        time.sleep(GENERAL_DELAY)
        src = try_unpaywall(doi, path)
        if src:
            print("OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "unpaywall", "doi": doi}
            return True
        print("X")

    # 5b. Semantic Scholar open-access PDF ------------------------------------
    if doi or title:
        print("    [5b] Semantic Scholar", end="", flush=True)
        time.sleep(GENERAL_DELAY)
        src = try_semantic_scholar(doi, title, path, verbose=verbose)
        if src:
            print(" OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "semantic_scholar", "doi": doi}
            return True
        print(" X")

    # 5c. Google Scholar (scholarly) ------------------------------------------
    if HAS_SCHOLARLY:
        print("    [5c] Google Scholar  ", end="", flush=True)
        time.sleep(2.0)
        src = try_scholarly(doi, title, path)
        if src:
            print(" OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "google_scholar", "doi": doi}
            return True
        print(" X")

    # 6. Direct DOI (UW VPN + EZProxy) ----------------------------------------
    if doi:
        print("    [6] Direct DOI ...   ", end="", flush=True)
        time.sleep(GENERAL_DELAY)
        src, manual_url = try_direct_doi(doi, path, verbose=verbose)
        if src:
            print("OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "direct_doi", "doi": doi}
            return True
        print("X")
        if manual_url:
            print(f"    -> needs manual: {manual_url}")
            tracking[tracking_key] = {"status": "needs_manual", "filename": filename,
                               "manual_url": manual_url, "doi": doi, "pmcid": pmcid}
            return False

    print("    All strategies failed.")
    tracking[tracking_key] = {"status": "failed", "doi": doi, "pmcid": pmcid}
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Download PDFs for Biomodels papers."
    )
    parser.add_argument("--count", type=int, default=10,
                        help="How many new papers to attempt (default 10).")
    parser.add_argument("--start", type=int, default=0,
                        help="Skip the first N new papers (0-indexed).")
    parser.add_argument("--all", action="store_true",
                        help="Process all remaining papers (ignores --count).")
    parser.add_argument("--pmc-only", action="store_true",
                        help="Only try PMC (skip LibKey, Unpaywall, direct DOI).")
    parser.add_argument("--ebsco-only", action="store_true",
                        help="Only try EBSCO (skip PMC, PubMed page, LibKey, etc.).")
    parser.add_argument("--pmid", metavar="PMID",
                        help="Process only this PMID (overrides --count/--all/--skip).")
    parser.add_argument("--doi", metavar="DOI",
                        help="Process only the paper with this DOI (overrides --count/--all/--skip).")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose debug output for each strategy.")
    parser.add_argument(
        "--skip",
        choices=["tried", "auth", "downloaded", "none"],
        default="tried",
        help=(
            "tried:       skip all previously attempted PMIDs (default).\n"
            "downloaded:  skip only successfully downloaded PMIDs.\n"
            "auth:        skip only auth-blocked PMIDs; retry other failures.\n"
            "none:        reprocess everything."
        ),
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    tracking = load_tracking()

    if not RESOLVED_FILE.exists():
        print(f"ERROR: {RESOLVED_FILE.name} not found.")
        print("Run  python resolve_ids.py  first to resolve PMIDs/PMCIDs/DOIs.")
        sys.exit(1)
    data = json.loads(RESOLVED_FILE.read_text(encoding="utf-8"))
    print(f"Papers in resolved JSON : {len(data)}")
    print(f"Entries in tracking     : {len(tracking)}")

    skip_statuses = set()
    if args.skip == "tried":
        skip_statuses = {"downloaded", "failed", "needs_manual",
                         "no_subscription", "no_pmcid"}
    elif args.skip == "downloaded":
        skip_statuses = {"downloaded"}
    elif args.skip == "auth":
        skip_statuses = {"downloaded", "no_subscription"}

    # --pmid / --doi: target a single paper regardless of skip/count settings
    if args.pmid or args.doi:
        needle_pmid = (args.pmid or "").strip()
        needle_doi  = (args.doi  or "").strip().lower()
        batch = []
        for pmid, info in data.items():
            numeric_id = re.sub(r'^https?://identifiers\.org/pubmed/', '', pmid)
            numeric_id = re.sub(r'^https?://identifiers\.org/doi/', '', numeric_id)
            if needle_pmid and needle_pmid == numeric_id:
                batch = [(pmid, info)]; break
            if needle_doi:
                info_doi = (info.get("doi") or "").lower()
                if needle_doi == info_doi or needle_doi == numeric_id.lower():
                    batch = [(pmid, info)]; break
        if not batch:
            print(f"No paper found matching "
                  f"{'PMID ' + args.pmid if args.pmid else 'DOI ' + args.doi}")
            sys.exit(1)
    else:
        papers = [
            (pmid, info) for pmid, info in data.items()
            if tracking.get(pmid, {}).get("status") not in skip_statuses
        ]
        print(f"Papers after skip filter: {len(papers)}")
        start = args.start
        count = len(papers) if args.all else args.count
        if args.pmc_only:
            batch = papers[start:]
        else:
            batch = papers[start: start + count]

    print(f"Output directory  : {OUTPUT_DIR}")
    print(f"Tracking file     : {TRACKING_FILE}")

    ok = 0
    failed = 0
    attempted = 0
    for pmid, info in batch:
        title      = info.get("title", "")
        numeric_id = re.sub(r'^https?://identifiers\.org/pubmed/', '', pmid)
        numeric_id = re.sub(r'^https?://identifiers\.org/doi/', '', numeric_id)

        pmc_info = {
            "pmid_numeric": info.get("pmid") or numeric_id,
            "pmcid":        info.get("pmcid"),
            "doi":          info.get("doi"),
        }

        tracking_key = pmid
        old_status = tracking.get(tracking_key, {}).get("status")

        result = download_paper(
            pmid, title, pmc_info, tracking,
            tracking_key=tracking_key,
            verbose=args.debug,
            pmc_only=args.pmc_only,
            ebsco_only=args.ebsco_only,
        )
        if result is None:
            pass  # no PMCID — derivable from resolved JSON, not worth persisting
        elif result:
            attempted += 1
            ok += 1
            if _should_update_resolved(old_status, "downloaded"):
                save_tracking(tracking, tracking_key)
                save_resolved(data, pmid, tracking[tracking_key])
        else:
            attempted += 1
            failed += 1
            new_status = tracking.get(tracking_key, {}).get("status")
            if _should_update_resolved(old_status, new_status):
                save_tracking(tracking, tracking_key)
                save_resolved(data, pmid, tracking[tracking_key])

        if not args.all and attempted >= count:
            break

    print(f"\nDone. {ok}/{attempted} downloaded.")
    tracked_downloaded = sum(1 for v in tracking.values() if v.get("status") == "downloaded")
    on_disk = sum(
        1 for v in tracking.values()
        if v.get("status") == "downloaded" and
        v.get("filename") and (OUTPUT_DIR / v["filename"]).exists()
    )
    print(f"Tracked as downloaded: {tracked_downloaded}  |  PDF on disk: {on_disk}")


if __name__ == "__main__":
    main()

