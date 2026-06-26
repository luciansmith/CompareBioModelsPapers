#!/usr/bin/env python3
"""
Ingest manually-downloaded PDFs into the Biomodels papers pipeline.

For papers that strategies in download_papers.py can't reach automatically
(e.g. publishers behind a Cloudflare bot challenge), you can download the
PDF yourself in a browser, drop it in a folder, and run this script to:

  1. Read the title out of each PDF (metadata first, then page-1 text).
  2. Match it against every paper in biomd_publication_info_resolved.json,
     downloaded or not -- if the best match turns out to be a paper that's
     already downloaded, that's reported as its own case rather than being
     silently matched against some other, worse-scoring candidate instead.
  3. Rename it to the project's standard PMID<id>_<title>.pdf convention
     (or DOI<id>_<title>.pdf for papers with no real PubMed ID) and copy
     it into "Biomodels papers/".
  4. Update download_tracking.json and biomd_publication_info_resolved.json
     the same way download_papers.py would (status=downloaded, source=manual).

Matching is deliberately conservative: if the best match isn't clearly
better than the runner-up, or two candidate titles are themselves so
similar that noisy extracted text can't reliably tell them apart (e.g. a
singular/plural title variant), the PDF is left unmatched rather than
risking a wrong assignment. See the "Euclid/purine" lesson documented in
semantic_scholar_strategy.py's _titles_match() for why this matters.

For PDFs where automatic matching just isn't reliable -- scanned/image-only
pages with no extractable text, mangled metadata, or anything else that
makes the title/synopsis scoring untrustworthy -- use --claim to tell the
script directly which paper a specific file is. A claim skips the
matching/scoring/ambiguity logic entirely for that file: it's trusted at
face value (modulo the same "already downloaded" safety check every other
match goes through). When any --claim is given, the run is restricted to
just the claimed file(s) -- other PDFs sitting in the folder are left
alone, not auto-matched.

A claim's ID can be a bare PMID, a bare DOI, a full identifiers.org key,
or (if none of those match) a paper TITLE -- in which case it's resolved
by fuzzy-matching against every paper's title, accepted only on an exact
match or a clear, unambiguous best match (see resolve_title_claim() in
match_library.py).

Usage:
  python match_manual_pdfs.py                  # scan manual_downloads/, dry run off
  python match_manual_pdfs.py --dry-run         # preview matches, write nothing
  python match_manual_pdfs.py --dir some/folder # scan a different folder
  python match_manual_pdfs.py --move            # delete the original after copying
  python match_manual_pdfs.py --threshold 0.85  # loosen/tighten match confidence
  python match_manual_pdfs.py --claim scan.pdf=12345678
  python match_manual_pdfs.py --claim scan.pdf=10.1016/j.cub.2022.04.016
  python match_manual_pdfs.py --claim scan.pdf="A Scientific Paper Title"
  python match_manual_pdfs.py --claim manual_downloads/scan.pdf=12345678
                                                 # force scan.pdf to be this
                                                 # paper, bypassing matching
                                                 # (repeatable for multiple
                                                 # files; only the claimed
                                                 # files are processed --
                                                 # a leading path before the
                                                 # filename, e.g. from shell
                                                 # tab-completion, is fine,
                                                 # only the basename is used)

Requirements:
  pip install pypdf
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

from strategy_utils import SCRIPT_DIR, OUTPUT_DIR, safe_filename, is_pdf
from download_papers import (
    load_tracking, save_tracking, save_resolved, RESOLVED_FILE, TRACKING_FILE,
)
from match_library import (
    THRESHOLD, AMBIGUITY_MARGIN,
    extract_pdf_title_and_text, find_best_match, find_candidates,
    load_synopsis_map, numeric_pmid, resolve_key, resolve_title_claim,
)

DEFAULT_INBOX = SCRIPT_DIR / "manual_downloads"


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Match manually-downloaded PDFs to missing Biomodels papers, "
                     "rename/copy them, and update the tracking JSON files."
    )
    parser.add_argument("--dir", type=Path, default=DEFAULT_INBOX,
                        help=f"Folder to scan for PDFs (default: {DEFAULT_INBOX.name}/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen; don't copy files or write JSON.")
    parser.add_argument("--move", action="store_true",
                        help="Delete the original file after a successful copy "
                             "(default: leave it in place).")
    parser.add_argument("--threshold", type=float, default=THRESHOLD,
                        help=f"Minimum match confidence, 0-1 (default {THRESHOLD}).")
    parser.add_argument("--claim", action="append", default=[], metavar="FILE=ID",
                        help="Force-match FILE (by name, inside --dir -- a "
                             "leading path, e.g. manual_downloads/scan.pdf, "
                             "is accepted and ignored, so tab-completion "
                             "works) to the paper identified by ID, skipping "
                             "automatic title/synopsis matching for that "
                             "file. ID can be a bare PMID, a bare DOI, a "
                             "full identifiers.org key, or a paper title "
                             "(matched exactly or, failing that, by a clear "
                             "best fuzzy match). Repeatable. When any "
                             "--claim is given, only the claimed file(s) "
                             "are processed.")
    args = parser.parse_args()

    claims = {}
    for spec in args.claim:
        if "=" not in spec:
            print(f"ERROR: --claim {spec!r} isn't in FILE=ID form.")
            sys.exit(1)
        fname, ident = spec.split("=", 1)
        # Accept a leading path (e.g. tab-completed "manual_downloads/scan.pdf")
        # and match on the basename only -- claims are always looked up
        # against pdf_path.name, never a full path.
        claims[Path(fname.strip()).name] = ident.strip()

    inbox = args.dir
    if not inbox.exists():
        inbox.mkdir(parents=True)
        print(f"Created empty inbox folder: {inbox}")
        print("Drop manually-downloaded PDFs there and re-run.")
        return

    all_pdfs = sorted(inbox.glob("*.pdf"))
    if claims:
        # --claim given: process only the claimed files, full stop. Don't
        # run automatic matching over the rest of the folder just because
        # it happens to be there.
        pdfs = [p for p in all_pdfs if p.name in claims]
        print(f"--claim given: processing only {len(pdfs)} claimed file(s), "
              f"ignoring {len(all_pdfs) - len(pdfs)} other PDF(s) in {inbox}.\n")
    else:
        pdfs = all_pdfs

    if not pdfs:
        if claims:
            print(f"None of the --claim filenames were found in {inbox}.")
        else:
            print(f"No PDFs found in {inbox}")
        return

    if not RESOLVED_FILE.exists():
        print(f"ERROR: {RESOLVED_FILE.name} not found.")
        sys.exit(1)

    import json
    data = json.loads(RESOLVED_FILE.read_text(encoding="utf-8"))
    tracking = load_tracking()

    if claims:
        # Claimed files skip matching entirely -- no need to build the
        # candidate/synopsis pool.
        synopsis_map, candidates = {}, []
    else:
        synopsis_map = load_synopsis_map()
        candidates = find_candidates(data, synopsis_map)
        print(f"Candidates (have a title): {len(candidates)}")
        print(f"Candidates with a synopsis: "
              f"{sum(1 for c in candidates if c[3])}")

    print(f"PDFs to check in {inbox}: {len(pdfs)}\n")

    OUTPUT_DIR.mkdir(exist_ok=True)

    matched = skipped = ambiguous = already_downloaded = claimed = 0

    for pdf_path in pdfs:
        print(f"{pdf_path.name}")
        if not is_pdf(pdf_path.read_bytes()):
            print("    -> not a valid PDF, skipping")
            skipped += 1
            continue

        meta_title, page_text = extract_pdf_title_and_text(pdf_path)
        if meta_title:
            print(f"    metadata title: {meta_title[:90]!r}")

        claim_id = claims.pop(pdf_path.name, None)
        is_claim = claim_id is not None

        if is_claim:
            # --claim: trust the human's identification outright and skip
            # the title/synopsis matching and ambiguity-detection entirely
            # -- for PDFs (scans, mangled metadata, etc.) where that logic
            # just can't be trusted to get it right. Try claim_id as an ID
            # first (PMID/DOI/identifiers.org key); if that doesn't match
            # anything, fall back to treating it as a title.
            key = resolve_key(data, claim_id)
            title_candidates = None
            if key is None:
                key, title_candidates = resolve_title_claim(data, claim_id)
            if key is None:
                print(f"    CLAIM ERROR: {claim_id!r} doesn't match any "
                      f"paper in {RESOLVED_FILE.name} (tried as an ID and "
                      f"as a title) -- skipping this file.")
                if title_candidates:
                    print(f"    Closest title(s) found:")
                    for ratio, k, t in title_candidates[:3]:
                        print(f"      {ratio:.3f}  {t[:90]!r}  ({k})")
                skipped += 1
                continue
            info = data[key]
            title = info.get("title") or "(no title on file)"
            match_label = f"CLAIMED as {claim_id!r}"
        else:
            best, scored, ambig = find_best_match(meta_title, page_text, candidates,
                                                   threshold=args.threshold)

            if ambig is not None:
                score, key, info, title = scored[0] if scored else (0, None, None, None)
                print(f"    AMBIGUOUS -- top candidates are too close/too similar to "
                      f"trust automatic matching:")
                for s, k, i, t in scored[:3]:
                    print(f"      {s:.3f}  {t[:90]!r}  ({k})")
                print(f"    Skipping. Rename/move this one manually if you know "
                      f"which it is, or re-run with "
                      f"--claim {pdf_path.name}=<pmid-or-doi> if you're sure.")
                ambiguous += 1
                continue

            if best is None:
                print("    no confident match")
                if scored:
                    top = scored[0]
                    print(f"      best guess was {top[0]:.3f}  {top[3][:90]!r}  ({top[1]})")
                print(f"    If you know which paper this is, re-run with "
                      f"--claim {pdf_path.name}=<pmid-or-doi>.")
                skipped += 1
                continue

            score, key, info, title = best
            match_label = f"MATCH ({score:.3f})"

        existing = tracking.get(key, {})
        if existing.get("status") == "downloaded":
            print(f"    ALREADY DOWNLOADED ({match_label}): {title[:90]!r}")
            print(f"    -> already have this one as "
                  f"{existing.get('filename', '(filename unknown)')} in "
                  f"{OUTPUT_DIR.name}/; not copying or touching tracking.")
            already_downloaded += 1
            continue

        # Prefer a real PMID for the filename; fall back to the DOI under a
        # "DOI" prefix when this paper has no real PubMed ID, instead of
        # mislabeling a DOI-only paper "PMID<doi>" (or, if even the DOI is
        # missing, falling back to the raw tracking key).
        pmid_num = numeric_pmid(key, info)
        doi = info.get("doi")
        if pmid_num:
            new_filename = safe_filename(pmid_num, title, id_label="PMID")
        elif doi:
            new_filename = safe_filename(doi, title, id_label="DOI")
        else:
            new_filename = safe_filename(key, title, id_label="PMID")
        dest = OUTPUT_DIR / new_filename

        print(f"    {match_label}: {title[:90]!r}  ({key})")
        print(f"    -> {dest}")

        if args.dry_run:
            print("    [dry run] would copy + update tracking, no changes made")
            matched += 1
            if is_claim:
                claimed += 1
            continue

        shutil.copy2(pdf_path, dest)
        if args.move:
            pdf_path.unlink()

        tracking[key] = {
            "status": "downloaded",
            "filename": new_filename,
            "source": "manual",
            "doi": info.get("doi"),
            "pmcid": info.get("pmcid"),
        }
        save_tracking(tracking, key)
        save_resolved(data, key, tracking[key])
        matched += 1
        if is_claim:
            claimed += 1

    if claims:
        print(f"\nWARNING: {len(claims)} --claim entry(ies) didn't match any "
              f"PDF actually found in {inbox}:")
        for fname, ident in claims.items():
            print(f"    {fname} = {ident}")

    print(f"\nDone. {matched} matched ({claimed} by --claim), "
          f"{already_downloaded} already downloaded, "
          f"{ambiguous} ambiguous, {skipped} skipped/unmatched.")
    if args.dry_run:
        print("(dry run -- nothing was written)")


if __name__ == "__main__":
    main()
