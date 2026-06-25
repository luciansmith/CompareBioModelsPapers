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
  python download_papers.py --count 3:5             # papers 3, 4, 5 (1-indexed)
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
    NCBI_DELAY, GENERAL_DELAY, SEMANTIC_SCHOLAR_DELAY,
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


# Manual-download fallback links found via Semantic Scholar, Google Scholar,
# or the direct-DOI resolution vary in how useful they turn out to be by hand
# (some land on a real article page, some just show a "UW doesn't subscribe
# to this" notice) -- but which is which isn't something this script can
# judge reliably, and an earlier attempt to guess via a fixed domain
# preference (academia.edu / sciencedirect.com) turned out to be wrong in
# practice. So: no preference, no picking a "best" one -- just dedupe and
# keep every candidate that turned up, in the order found, and let the human
# looking at needs_manual decide which link to try first.
def _dedupe_manual_urls(candidates):
    """Dedupe a list of candidate manual-download URLs, preserving order."""
    seen = []
    for u in candidates:
        if u and u not in seen:
            seen.append(u)
    return seen


def _should_update_resolved(old_status, new_status):
    """
    Return True unless this would downgrade an already-confirmed download.

    Originally this only persisted a first attempt or an upgrade to
    "downloaded", on the theory that "still failing" attempts had nothing
    new to record. That's wrong: a "still failing" result can carry
    genuinely new detail -- most importantly manual_urls (e.g. failed ->
    needs_manual, or needs_manual with a different/expanded set of
    candidate URLs found this run) -- and that was getting silently
    discarded: the in-memory tracking dict got updated, but save_tracking()
    was never called, so it never reached disk and was gone the moment the
    process exited. The only thing actually worth protecting is not
    clobbering a confirmed download with a worse status.
    """
    if old_status == "downloaded":
        return False                           # never overwrite a success
    return True                                # persist everything else


def save_resolved(data, pmid, tracking_entry):
    """
    Write the download result back into the resolved JSON file.
    Only the 'download' sub-key is touched; all other fields are preserved.
    Uses an atomic write so a crash mid-write doesn't corrupt the file.
    """
    import tempfile, os
    download_info = {k: v for k, v in tracking_entry.items()
                     if k in ("status", "filename", "source", "manual_urls")}
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
                   verbose=False, pmc_only=False, ebsco_only=False,
                   semantic_scholar_only=False, libkey_only=False,
                   scholarly_only=False, pause_on_captchas=False,
                   position=None, total=None):
    pmcid        = pmc_info.get("pmcid")
    doi          = pmc_info.get("doi")
    pmid_numeric = pmc_info.get("pmid_numeric") or pmid

    if pmc_only and not pmcid:
        return None

    filename     = safe_filename(pmid_numeric, title)
    path         = OUTPUT_DIR / filename
    # Manual-download candidates a previous run already recorded for this
    # paper, if any -- merged below with whatever this run finds, so a rerun
    # never silently drops links an earlier run discovered (see
    # _dedupe_manual_urls and _should_update_resolved's docstring). Older
    # tracking entries (confirmed via download_tracking.json: 41 entries,
    # zero using the plural key) used a singular "manual_url" string field
    # before this was changed to a "manual_urls" list -- fold that legacy
    # key in too, or every one of those existing entries would look like it
    # had no prior candidates at all.
    _old_entry = tracking.get(tracking_key, {})
    old_manual_urls = list(_old_entry.get("manual_urls") or [])
    if _old_entry.get("manual_url"):
        old_manual_urls.append(_old_entry["manual_url"])
    # position/total: this paper's 1-indexed position in the post-skip-filter
    # paper list, e.g. "[151/849]" -- if a run gets interrupted, this number
    # is exactly what --count n:total (or --start) needs to resume from.
    pos_str = f"[{position}/{total}]  " if position is not None else ""
    print(f"\n  {pos_str}PMID {pmid}  PMC {pmcid or '---'}  DOI {doi or '---'}")
    print(f"  {title[:80]}")

    if ebsco_only:
        print("    [3] EBSCO ...        ", end="", flush=True)
        time.sleep(GENERAL_DELAY)
        src = try_ebsco(pmid_numeric, doi, path, verbose=verbose)
        if src is _NO_SUBSCRIPTION:
            print("no UW subscription")
            tracking[tracking_key] = {"status": "no_subscription", "doi": doi, "pmcid": pmcid}
            return False
        if src:
            print("OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "ebsco", "doi": doi, "pmcid": pmcid}
            return True
        print("X")
        tracking[tracking_key] = {"status": "failed", "doi": doi, "pmcid": pmcid}
        return False

    if libkey_only:
        print("    [4] LibKey ...       ", end="", flush=True)
        time.sleep(GENERAL_DELAY)
        src, libkey_manual = try_libkey(pmid_numeric, doi, path, verbose=verbose)
        if src is _NO_SUBSCRIPTION:
            print("no UW subscription")
            tracking[tracking_key] = {"status": "no_subscription", "doi": doi, "pmcid": pmcid}
            return False
        if src:
            print("OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "libkey", "doi": doi, "pmcid": pmcid}
            return True
        print("X")
        all_candidates = _dedupe_manual_urls(old_manual_urls + (libkey_manual or []))
        if all_candidates:
            for u in all_candidates:
                print(f"    -> manual candidate: {u}")
            tracking[tracking_key] = {"status": "needs_manual", "filename": filename,
                     "manual_urls": all_candidates, "doi": doi, "pmcid": pmcid}
            return False
        tracking[tracking_key] = {"status": "failed", "doi": doi, "pmcid": pmcid}
        return False

    if semantic_scholar_only:
        if not (doi or title):
            return None
        print("    [5b] Semantic Scholar", end="", flush=True)
        time.sleep(SEMANTIC_SCHOLAR_DELAY)
        src, manual_url = try_semantic_scholar(doi, title, path, verbose=verbose, pmid=pmid_numeric)
        if src:
            print(" OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "semantic_scholar", "doi": doi}
            return True
        print(" X")
        all_candidates = _dedupe_manual_urls(old_manual_urls + ([manual_url] if manual_url else []))
        if all_candidates:
            for u in all_candidates:
                print(f"    -> manual candidate: {u}")
            tracking[tracking_key] = {"status": "needs_manual", "filename": filename,
                               "manual_urls": all_candidates, "doi": doi, "pmcid": pmcid}
            return False
        tracking[tracking_key] = {"status": "failed", "doi": doi, "pmcid": pmcid}
        return False

    if scholarly_only:
        if not (doi or title):
            return None
        print("    [5c] Google Scholar  ", end="", flush=True)
        time.sleep(2.0)
        src, manual_candidates = try_scholarly(doi, title, path, verbose=verbose,
                                                pause_on_captchas=pause_on_captchas)
        if src:
            print(" OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "google_scholar", "doi": doi}
            return True
        print(" X")
        all_candidates = _dedupe_manual_urls(old_manual_urls + manual_candidates)
        if all_candidates:
            for u in all_candidates:
                print(f"    -> manual candidate: {u}")
            tracking[tracking_key] = {"status": "needs_manual", "filename": filename,
                     "manual_urls": all_candidates, "doi": doi, "pmcid": pmcid}
            return False
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
    _ebsco_no_subscription = src is _NO_SUBSCRIPTION
    if _ebsco_no_subscription:
        print("no UW subscription")
        tracking[tracking_key] = {"status": "no_subscription", "doi": doi, "pmcid": pmcid}
        # Fall through to LibKey / open-access steps — EBSCO confirmed there's
        # no accessible full text via this route, but others may still work.
    elif src:
        print("OK")
        tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                           "source": "ebsco", "doi": doi, "pmcid": pmcid}
        return True
    else:
        print("X")

    # Manual-download fallback links surfaced by 4/5b/5c/6 even when each
    # strategy's automated fetch fails -- collected (all of them, deduped,
    # no preference -- see _dedupe_manual_urls above) so the final
    # "needs_manual" entry gives a human every link that turned up, not just
    # one guessed-at "best" one.
    manual_candidates = []

    # 4. LibKey / ThirdIron (UW library link resolver -- needs UW VPN) --------
    print("    [4] LibKey ...       ", end="", flush=True)
    time.sleep(GENERAL_DELAY)
    src, libkey_manual = try_libkey(pmid_numeric, doi, path, verbose=verbose)
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
    manual_candidates.extend(libkey_manual or [])

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
        time.sleep(SEMANTIC_SCHOLAR_DELAY)
        src, s2_manual = try_semantic_scholar(doi, title, path, verbose=verbose, pmid=pmid_numeric)
        if src:
            print(" OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "semantic_scholar", "doi": doi}
            return True
        print(" X")
        if s2_manual:
            manual_candidates.append(s2_manual)

    # 5c. Google Scholar (scholarly) ------------------------------------------
    if HAS_SCHOLARLY:
        print("    [5c] Google Scholar  ", end="", flush=True)
        time.sleep(2.0)
        src, scholarly_manual = try_scholarly(doi, title, path, verbose=verbose,
                                               pause_on_captchas=pause_on_captchas)
        if src:
            print(" OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "google_scholar", "doi": doi}
            return True
        print(" X")
        manual_candidates.extend(scholarly_manual or [])

    # 6. Direct DOI (UW VPN + EZProxy) ----------------------------------------
    if doi:
        print("    [6] Direct DOI ...   ", end="", flush=True)
        time.sleep(GENERAL_DELAY)
        src, doi_manual = try_direct_doi(doi, path, verbose=verbose)
        if src:
            print("OK")
            tracking[tracking_key] = {"status": "downloaded", "filename": filename,
                               "source": "direct_doi", "doi": doi}
            return True
        print("X")
        if doi_manual:
            manual_candidates.append(doi_manual)

    all_candidates = _dedupe_manual_urls(old_manual_urls + manual_candidates)
    if all_candidates:
        for u in all_candidates:
            print(f"    -> needs manual: {u}")
        tracking[tracking_key] = {"status": "needs_manual", "filename": filename,
                 "manual_urls": all_candidates, "doi": doi, "pmcid": pmcid}
        return False

    print("    All strategies failed.")
    tracking[tracking_key] = {"status": "failed", "doi": doi, "pmcid": pmcid}
    return False


def _parse_count(count_str):
    """
    Parse the --count value.

    Plain integer ("10"):  attempt that many papers, starting at --start.
    Range ("n:m", 1-indexed, inclusive): skip the first n-1 papers (on top
        of --start), then attempt the nth through mth paper. "3:5" attempts
        papers 3, 4, and 5; "3:3" attempts paper 3 only.

    Returns (extra_start, count) where extra_start should be added to
    --start before slicing the paper list.
    """
    count_str = count_str.strip()
    if ":" in count_str:
        n_str, m_str = count_str.split(":", 1)
        try:
            n, m = int(n_str), int(m_str)
        except ValueError:
            raise ValueError(f"invalid --count range {count_str!r}; expected n:m with integers")
        if n < 1 or m < n:
            raise ValueError(f"invalid --count range {count_str!r}; require 1 <= n <= m")
        return n - 1, m - n + 1
    try:
        return 0, int(count_str)
    except ValueError:
        raise ValueError(f"invalid --count value {count_str!r}; expected an integer or n:m range")


def main():
    parser = argparse.ArgumentParser(
        description="Download PDFs for Biomodels papers."
    )
    parser.add_argument("--count", type=str, default="10",
                        help="How many new papers to attempt (default 10). "
                             "Also accepts a 1-indexed range 'n:m' to attempt "
                             "the nth through mth paper, e.g. --count 3:5.")
    parser.add_argument("--start", type=int, default=0,
                        help="Skip the first N new papers (0-indexed).")
    parser.add_argument("--all", action="store_true",
                        help="Process all remaining papers (ignores --count).")
    parser.add_argument("--pmc-only", action="store_true",
                        help="Only try PMC (skip LibKey, Unpaywall, direct DOI).")
    parser.add_argument("--ebsco-only", action="store_true",
                        help="Only try EBSCO (skip PMC, PubMed page, LibKey, etc.).")
    parser.add_argument("--semantic-scholar-only", action="store_true",
                        help="Only try Semantic Scholar (skip PMC, EBSCO, LibKey, etc.).")
    parser.add_argument("--libkey-only", action="store_true",
                        help="Only try LibKey/ThirdIron (skip PMC, EBSCO, Unpaywall, etc.).")
    parser.add_argument("--scholarly-only", action="store_true",
                        help="Only try Google Scholar via `scholarly` (skip PMC, EBSCO, LibKey, etc.).")
    parser.add_argument("--pmid", metavar="PMID",
                        help="Process only this PMID (overrides --count/--all/--skip).")
    parser.add_argument("--doi", metavar="DOI",
                        help="Process only the paper with this DOI (overrides --count/--all/--skip).")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose debug output for each strategy.")
    parser.add_argument("--pause-on-captchas", action="store_true",
                        help="When Google Scholar serves a CAPTCHA, pause "
                             "and wait for you to press Enter instead of "
                             "failing fast -- no browser is opened by this "
                             "script either way (solving a CAPTCHA in an "
                             "automated browser doesn't help: the actual "
                             "scraping happens over a separate plain-HTTP "
                             "session that Google can still block "
                             "independently). Press Enter once you're "
                             "ready and it retries the search exactly "
                             "once from scratch; if that retry also hits "
                             "a CAPTCHA, it gives up rather than pausing "
                             "again. Only use this for an attended run --"
                             "for an unattended/overnight batch, leave "
                             "this off (the default): a CAPTCHA then "
                             "fails fast with no pause.")
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

    if args.scholarly_only and not HAS_SCHOLARLY:
        print("ERROR: --scholarly-only requires the `scholarly` package, which "
              "isn't installed in this environment.")
        print("Install it with:  pip install scholarly --break-system-packages")
        sys.exit(1)

    try:
        extra_start, count_n = _parse_count(args.count)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

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
        count = len(batch)  # single-paper batch -- the loop-end count check still applies
        start = None         # no position numbering for a direct --pmid/--doi target
        total_papers = None
    else:
        papers = [
            (pmid, info) for pmid, info in data.items()
            if tracking.get(pmid, {}).get("status") not in skip_statuses
        ]
        print(f"Papers after skip filter: {len(papers)}")
        total_papers = len(papers)
        start = args.start + extra_start
        count = len(papers) if args.all else count_n
        if args.pmc_only:
            batch = papers[start:]
        else:
            batch = papers[start: start + count]

    print(f"Output directory  : {OUTPUT_DIR}")
    print(f"Tracking file     : {TRACKING_FILE}")

    ok = 0
    failed = 0
    attempted = 0
    for i, (pmid, info) in enumerate(batch):
        title      = info.get("title", "")
        numeric_id = re.sub(r'^https?://identifiers\.org/pubmed/', '', pmid)
        numeric_id = re.sub(r'^https?://identifiers\.org/doi/', '', numeric_id)
        if not re.match(r'^\d+$', numeric_id):
            # This key was doi/-style (or something else), not pubmed/-style --
            # numeric_id is the bare DOI text, not a PMID. Don't let it leak
            # into pmid_numeric (that's what produced bogus "pmid:10.1016/..."
            # queries). info.get("pmid") below is the real fallback: it's set
            # by resolve_ids.py's DOI->PMID lookup if a real PMID exists for
            # this DOI, else stays None.
            numeric_id = None

        pmc_info = {
            "pmid_numeric": info.get("pmid") or numeric_id,
            "pmcid":        info.get("pmcid"),
            "doi":          info.get("doi"),
        }

        tracking_key = pmid
        old_status = tracking.get(tracking_key, {}).get("status")

        position = (start + i + 1) if start is not None else None
        result = download_paper(
            pmid, title, pmc_info, tracking,
            tracking_key=tracking_key,
            verbose=args.debug,
            pmc_only=args.pmc_only,
            ebsco_only=args.ebsco_only,
            semantic_scholar_only=args.semantic_scholar_only,
            libkey_only=args.libkey_only,
            scholarly_only=args.scholarly_only,
            pause_on_captchas=args.pause_on_captchas,
            position=position,
            total=total_papers,
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

