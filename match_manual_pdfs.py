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
     and copy it into "Biomodels papers/".
  4. Update download_tracking.json and biomd_publication_info_resolved.json
     the same way download_papers.py would (status=downloaded, source=manual).

Matching is deliberately conservative: if the best match isn't clearly
better than the runner-up, or two candidate titles are themselves so
similar that noisy extracted text can't reliably tell them apart (e.g. a
singular/plural title variant), the PDF is left unmatched rather than
risking a wrong assignment. See the "Euclid/purine" lesson documented in
semantic_scholar_strategy.py's _titles_match() for why this matters.

Usage:
  python match_manual_pdfs.py                  # scan manual_downloads/, dry run off
  python match_manual_pdfs.py --dry-run         # preview matches, write nothing
  python match_manual_pdfs.py --dir some/folder # scan a different folder
  python match_manual_pdfs.py --move            # delete the original after copying
  python match_manual_pdfs.py --threshold 0.85  # loosen/tighten match confidence

Requirements:
  pip install pypdf
"""

import argparse
import difflib
import re
import shutil
import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    print("Missing dependency. Run:  pip install pypdf")
    sys.exit(1)

from strategy_utils import SCRIPT_DIR, OUTPUT_DIR, safe_filename, is_pdf
from download_papers import (
    load_tracking, save_tracking, save_resolved, RESOLVED_FILE, TRACKING_FILE,
)

DEFAULT_INBOX = SCRIPT_DIR / "manual_downloads"

THRESHOLD = 0.90
AMBIGUITY_MARGIN = 0.03   # runner-up within this of the top score -> ambiguous
DUP_TITLE_SIM = 0.92      # two candidate TITLES this similar to each other ->
                          # don't trust noisy extracted text to tell them apart


# ── Matching logic ───────────────────────────────────────────────────────────
def _norm_title(t):
    return re.sub(r'[^a-z0-9]+', ' ', (t or '').lower()).strip()


def _score_candidate(norm_candidate_title, norm_meta_title, norm_page_text):
    if not norm_candidate_title:
        return 0.0
    score = 0.0
    if norm_meta_title:
        score = max(score, difflib.SequenceMatcher(
            None, norm_candidate_title, norm_meta_title).ratio())
    if norm_page_text and norm_candidate_title in norm_page_text:
        score = max(score, 0.97)
    return score


def find_best_match(meta_title, page_text, candidates, threshold=THRESHOLD,
                     dup_title_sim=DUP_TITLE_SIM, ambiguity_margin=AMBIGUITY_MARGIN):
    """
    candidates: list of (key, info, title)
    Returns (best_or_None, scored_list, ambiguous_with_or_None).
    scored_list is every candidate, sorted best-first, as (score, key, info, title).
    If ambiguous_with_or_None is set, best_or_None is None even if something
    scored above threshold -- caller should treat this PDF as needing a human
    to disambiguate rather than guessing.
    """
    norm_meta = _norm_title(meta_title) if meta_title else ""
    norm_text = _norm_title(page_text) if page_text else ""

    scored = []
    for key, info, title in candidates:
        nt = _norm_title(title)
        score = _score_candidate(nt, norm_meta, norm_text)
        scored.append((score, key, info, title, nt))

    scored.sort(key=lambda t: -t[0])
    public_scored = [(s, k, i, t) for (s, k, i, t, _nt) in scored]

    if not scored or scored[0][0] < threshold:
        return None, public_scored, None

    top = scored[0]
    for cand in scored[1:]:
        score_gap_ambiguous = (cand[0] >= threshold
                                and (top[0] - cand[0]) < ambiguity_margin)
        title_dup_ambiguous = (
            difflib.SequenceMatcher(None, top[4], cand[4]).ratio() >= dup_title_sim
        )
        if score_gap_ambiguous or title_dup_ambiguous:
            return None, public_scored, (cand[0], cand[1], cand[2], cand[3])

    return (top[0], top[1], top[2], top[3]), public_scored, None


# ── PDF reading ──────────────────────────────────────────────────────────────
def extract_pdf_title_and_text(path, max_pages=2):
    """Returns (metadata_title_or_None, page_text_or_empty_string)."""
    meta_title = None
    page_text = ""
    try:
        reader = PdfReader(str(path))
        if reader.metadata and reader.metadata.title:
            meta_title = str(reader.metadata.title).strip() or None
        for page in reader.pages[:max_pages]:
            page_text += (page.extract_text() or "") + "\n"
    except Exception as e:
        print(f"    [read error] {e}")
    return meta_title, page_text


# ── Candidate set ────────────────────────────────────────────────────────────
def find_candidates(data):
    """
    Every paper with a usable title, downloaded or not.

    Downloaded papers are deliberately kept in the pool: if a PDF's true
    best match is a paper that's already downloaded, we want to detect and
    report that specifically, not silently let it match some other,
    worse-scoring, not-yet-downloaded paper instead.
    """
    candidates = []
    for key, info in data.items():
        title = info.get("title", "")
        if not title:
            continue
        candidates.append((key, info, title))
    return candidates


def numeric_pmid(key, info):
    numeric_id = re.sub(r'^https?://identifiers\.org/pubmed/', '', key)
    numeric_id = re.sub(r'^https?://identifiers\.org/doi/', '', numeric_id)
    if not re.match(r'^\d+$', numeric_id):
        numeric_id = None
    return info.get("pmid") or numeric_id


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
    args = parser.parse_args()

    inbox = args.dir
    if not inbox.exists():
        inbox.mkdir(parents=True)
        print(f"Created empty inbox folder: {inbox}")
        print("Drop manually-downloaded PDFs there and re-run.")
        return

    pdfs = sorted(inbox.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {inbox}")
        return

    if not RESOLVED_FILE.exists():
        print(f"ERROR: {RESOLVED_FILE.name} not found.")
        sys.exit(1)

    import json
    data = json.loads(RESOLVED_FILE.read_text(encoding="utf-8"))
    tracking = load_tracking()
    candidates = find_candidates(data)
    print(f"Candidates (have a title): {len(candidates)}")
    print(f"PDFs to check in {inbox}: {len(pdfs)}\n")

    OUTPUT_DIR.mkdir(exist_ok=True)

    matched = skipped = ambiguous = already_downloaded = 0

    for pdf_path in pdfs:
        print(f"{pdf_path.name}")
        if not is_pdf(pdf_path.read_bytes()):
            print("    -> not a valid PDF, skipping")
            skipped += 1
            continue

        meta_title, page_text = extract_pdf_title_and_text(pdf_path)
        if meta_title:
            print(f"    metadata title: {meta_title[:90]!r}")

        best, scored, ambig = find_best_match(meta_title, page_text, candidates,
                                               threshold=args.threshold)

        if ambig is not None:
            score, key, info, title = scored[0] if scored else (0, None, None, None)
            print(f"    AMBIGUOUS -- top candidates are too close/too similar to "
                  f"trust automatic matching:")
            for s, k, i, t in scored[:3]:
                print(f"      {s:.3f}  {t[:90]!r}  ({k})")
            print("    Skipping. Rename/move this one manually if you know which it is.")
            ambiguous += 1
            continue

        if best is None:
            print("    no confident match")
            if scored:
                top = scored[0]
                print(f"      best guess was {top[0]:.3f}  {top[3][:90]!r}  ({top[1]})")
            skipped += 1
            continue

        score, key, info, title = best

        existing = tracking.get(key, {})
        if existing.get("status") == "downloaded":
            print(f"    ALREADY DOWNLOADED ({score:.3f}): {title[:90]!r}")
            print(f"    -> already have this one as "
                  f"{existing.get('filename', '(filename unknown)')} in "
                  f"{OUTPUT_DIR.name}/; not copying or touching tracking.")
            already_downloaded += 1
            continue

        pmid_num = numeric_pmid(key, info)
        new_filename = safe_filename(pmid_num, title)
        dest = OUTPUT_DIR / new_filename

        print(f"    MATCH ({score:.3f}): {title[:90]!r}")
        print(f"    -> {dest}")

        if args.dry_run:
            print("    [dry run] would copy + update tracking, no changes made")
            matched += 1
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

    print(f"\nDone. {matched} matched, {already_downloaded} already downloaded, "
          f"{ambiguous} ambiguous, {skipped} skipped/unmatched.")
    if args.dry_run:
        print("(dry run -- nothing was written)")


if __name__ == "__main__":
    main()
