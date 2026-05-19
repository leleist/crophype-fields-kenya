"""
Utility functions for the CropHype field QC toolkit.

Three function families:

1. Standardization
   - standardize_dataframe : rename + drop columns from a configurable mapping
     and drop list. Used to harmonize per-campaign source schemas to the
     published one.
   - assign_field_ids      : generate the 9-digit YYYYSNNNN field identifier.

2. Photo deduplication
   - resolve_photo_references : cleaning of the `photo` column:
     nulls references whose file is missing on disk, Checks filename and content
     duplicates (MD5). Detected duplicates are resolved utilizing minimum datetime difference.
     deletes redundant on-disk files.
   - hash_file_md5, _photo_datetime : low-level helpers.

3. QC result reporting
   - compute_qc_metrics    : turn a QC-reviewed GeoDataFrame into a dict of
     thematic accuracy statistics plus per-crop info.
   - print_qc_summary      : summarize thematic review results.
   - compute_shape_metrics : analogous dict for geometric validation,
     computed only from rows that carry the `qc_shape_*` columns.
   - print_shape_summary   : geometric val. summary.

The QC tool itself lives in `fieldqc.py`. It has a dependence to the utils herein.
"""

import hashlib
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd


# Standardization

def standardize_dataframe(df, column_mapping, drop_columns):
    """Apply a rename + drop pass to harmonize a raw source schema.

    Returns a copy of `df` with:

    * columns renamed according to `column_mapping`
    * columns in `drop_columns` removed

    `column_mapping` is intended to be a per-campaign dictionary mapping
    raw-source column names to the canonical published names
    (`field_id`, `crop_main`, `crop_sec`, `growth_main`, `growth_sec`,
    `photo`, `timestamp`, `area_acres`, etc.). `drop_columns` lists
    bookkeeping / shapefile-artefact columns that should not appear in the
    published file. Both are free configuration.
    """
    df_std = df.copy()
    df_std = df_std.rename(columns=column_mapping)
    df_std = df_std.drop(columns=[c for c in drop_columns if c in df_std.columns])
    return df_std


def assign_field_ids(df, year, season_code):
    """Insert a `field_id` column as the first column of a copy of `df`.

    The identifier is a 9-digit integer ``YYYYSNNNN`` where:

    * ``YYYY``  — four-digit year of the campaign.
    * ``S``     — single-digit season code (``0`` = long rains, ``1`` = short rains).
    * ``NNNN``  — 1-based sequence index within the campaign, zero-padded
      to four digits.

    Example: the 27th field of the 2024 long-rains campaign becomes
    ``202400027``. The function assigns IDs in the current row order of
    `df`.
    """
    ids = [int(f"{year}{season_code}{i + 1:04d}") for i in range(len(df))]
    df = df.copy()
    df.insert(0, "field_id", ids)
    return df


# Photo deduplication

def _photo_datetime(photo_val):
    """Extract the datetime embedded in a photo filename.

    Expected pattern: ``..._YYYYMMDD[HHMMSS]...`` (separator may be ``_`` or
    ``-``; the time portion is optional). Returns ``pd.NaT`` when nothing
    parseable is found.
    """
    stem = Path(str(photo_val)).stem
    m = re.search(r'[_-](\d{8})(\d{6})?', stem)
    if m is None:
        return pd.NaT
    time_str = m.group(2) or '000000'
    try:
        return pd.to_datetime(m.group(1) + time_str, format='%Y%m%d%H%M%S')
    except Exception:
        return pd.NaT


def hash_file_md5(path):
    """Return the MD5 hex digest of a file.
    computational load is considerable on larger archives. We only apply it to pre-filtered
    candidates.

    """
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def resolve_photo_references(df, image_dir, name,
                              photo_col='photo',
                              timestamp_col='timestamp',
                              id_col='field_id'):
    """Reconcile the `photo` column of a campaign GeoDataFrame with the files
    on disk: clear stale references, resolve duplicates, and ditch redundant
    image files.

    Three issues are handled in one pass:

    * **missing files** — `photo` references whose file is not on disk are
      set to ``NaN``.
    * **filename duplicates** — multiple rows whose `photo` column points to
      the same filename.
    * **content duplicates** — different filenames on disk whose bytes hash
      identically (MD5). File size is used as a fast pre-filter so only
      same-size files are hashed.

    For each duplicate group, the winning row keeps its photo reference and
    the others have theirs cleared. The winner is the row whose `timestamp`
    is closest to the datetime embedded in the filename. If timestamps tie or
    cannot be parsed, the row with the lowest `field_id` wins. For
    content-duplicate groups, the redundant files are deleted from disk.

    Note: mutates input inplace

    Parameters
    ----------
    df : GeoDataFrame
        Mutated in place: `photo` is set to NaN for non-winners.
    image_dir : str or Path
        Directory containing the photos referenced in `photo_col`.
    name : str
        Label used in the printed report.
    photo_col, timestamp_col, id_col : str
        Column names; defaults match the published schema.

    Returns
    -------
    list of dict
        One entry per file deleted from disk, with keys
        ``campaign``, ``deleted_file``, ``kept_file``, ``winner_field_id``.
    """
    image_dir = Path(image_dir)
    if not image_dir.exists():
        print(f"{name}: image directory not found, skipping.")
        return []

    def bare(val):
        return Path(str(val)).name if pd.notna(val) else None

    # group files on disk by size, then MD5-hash same-size files
    size_to_paths = defaultdict(list)
    for p in image_dir.iterdir():
        if p.is_file():
            size_to_paths[p.stat().st_size].append(p)

    files_on_disk = {p.name for paths in size_to_paths.values() for p in paths}

    hash_to_names = defaultdict(list)
    for paths in size_to_paths.values():
        if len(paths) > 1:
            for p in paths:
                hash_to_names[hash_file_md5(p)].append(p.name)
    content_dup_groups = [names for names in hash_to_names.values() if len(names) > 1]
    n_redundant = sum(len(g) - 1 for g in content_dup_groups)

    # step 1: clear photo refs for files that are not on disk
    before = int(df[photo_col].isna().sum())
    df[photo_col] = df[photo_col].where(
        df[photo_col].apply(lambda v: pd.isna(v) or bare(v) in files_on_disk),
        other=pd.NA,
    )
    n_cleared_missing = int(df[photo_col].isna().sum()) - before

    # steps 2 + 3: assign a content-group id to every on-disk file
    fname_to_gid = {}
    for gid, group in enumerate(content_dup_groups):
        for fname in group:
            fname_to_gid[fname] = gid
    next_gid = len(content_dup_groups)
    for fname in files_on_disk:
        if fname not in fname_to_gid:
            fname_to_gid[fname] = next_gid
            next_gid += 1

    df['_gid'] = df[photo_col].apply(
        lambda v: fname_to_gid.get(bare(v)) if pd.notna(v) else None
    )

    # step 4: pick a winner per duplicate group
    dup_gids = df['_gid'].value_counts()
    dup_gids = dup_gids[dup_gids > 1].index.tolist()

    resolved_ts  = 0
    resolved_fid = 0
    deleted = []

    for gid in dup_gids:
        gid_int = int(gid)
        rows = df[df['_gid'] == gid]

        ts        = pd.to_datetime(rows[timestamp_col], errors='coerce')
        photo_dts = rows[photo_col].apply(
            lambda v: _photo_datetime(v) if pd.notna(v) else pd.NaT
        )
        diffs = (ts - photo_dts).abs()
        valid = diffs.dropna()

        if len(valid) > 0 and (valid == valid.min()).sum() == 1:
            best_idx = diffs.idxmin()
            resolved_ts += 1
        else:
            best_idx = rows[id_col].idxmin()
            resolved_fid += 1

        df.loc[rows.index[rows.index != best_idx], photo_col] = pd.NA

        # step 5: drop redundant on-disk files for content-duplicate groups
        if gid_int < len(content_dup_groups):
            winner_fname = bare(df.at[best_idx, photo_col])
            winner_fid   = df.at[best_idx, id_col]
            for fname in content_dup_groups[gid_int]:
                if fname != winner_fname:
                    fpath = image_dir / fname
                    if fpath.exists():
                        fpath.unlink()
                        deleted.append({
                            'campaign':        name,
                            'deleted_file':    fname,
                            'kept_file':       winner_fname,
                            'winner_field_id': winner_fid,
                        })

    df.drop(columns=['_gid'], inplace=True)

    print(f"{name}:")
    print(f"  files on disk:                          {len(files_on_disk)}")
    print(f"  content-duplicate groups found:         {len(content_dup_groups)}  ({n_redundant} redundant files)")
    print(f"  photo refs cleared - file not on disk:  {n_cleared_missing}")
    print(f"  dup groups resolved by timestamp:       {resolved_ts}")
    print(f"  dup groups resolved by field_id:        {resolved_fid}")
    print(f"  files deleted from disk:                {len(deleted)}")
    return deleted


# QC result reporting

def compute_qc_metrics(gdf, name="",
                       flag_col="qc_flag",
                       qc_crop_main_col="qc_crop_main",
                       qc_crop_sec_col="qc_crop_sec",
                       reviewed_col="qc_reviewed",
                       crop_col="crop_main",
                       sec_crop_col="crop_sec",
                       photo_col="photo"):
    """Compute QC accuracy metrics from a reviewed GeoDataFrame.

    Returns a dict containing all basic analytic numbers.

    Accuracy definitions
    --------------------
    field_accuracy : n_correct / n_assessable
        Fraction of assessable fields where ALL labels were confirmed correct.
    main_accuracy  : (n_assessable - n_main_changed) / n_assessable
        Fraction of assessable fields where the main crop was correct.
    sec_accuracy   : (n_assessed_with_sec - n_sec_changed) / n_assessed_with_sec
        Fraction of assessable intercrop fields where the secondary crop was
        correct. Denominator = assessable fields that ORIGINALLY had a
        secondary crop. Fields where the secondary crop was added during QC
        (original was None) are logged separately as `n_sec_added` and are
        NOT included in the denominator.

    --> The accuracy is computed with respect to the original labels.
    """
    n_total       = len(gdf)
    n_intercrop   = int(gdf[sec_crop_col].notna().sum())
    n_monocrop    = n_total - n_intercrop
    pct_intercrop = 100 * n_intercrop / n_total if n_total > 0 else float("nan")
    n_with_photos = int(gdf[photo_col].notna().sum()) if photo_col in gdf.columns else None

    sample_mask  = gdf[reviewed_col].isin([True, 1, "True"])
    n_reviewed   = int(sample_mask.sum())
    n_skipped    = int((gdf.loc[sample_mask, flag_col] == "skip").sum())
    n_correct    = int((gdf.loc[sample_mask, flag_col] == "correct").sum())
    n_mismatch   = int((gdf.loc[sample_mask, flag_col] == "mismatch").sum())
    n_assessable = n_correct + n_mismatch
    skip_rate    = n_skipped / n_reviewed if n_reviewed > 0 else float("nan")

    field_accuracy = n_correct / n_assessable if n_assessable > 0 else float("nan")

    sample_assessed = sample_mask & gdf[flag_col].isin(["correct", "mismatch"])
    n_main_changed  = int(gdf.loc[sample_assessed, qc_crop_main_col].notna().sum())
    main_accuracy   = (n_assessable - n_main_changed) / n_assessable if n_assessable > 0 else float("nan")

    assessed_with_sec_mask = sample_assessed & gdf[sec_crop_col].notna()
    n_assessed_with_sec    = int(assessed_with_sec_mask.sum())
    n_sec_changed          = int((assessed_with_sec_mask & gdf[qc_crop_sec_col].notna()).sum())
    n_sec_removed          = int((assessed_with_sec_mask & (gdf[qc_crop_sec_col] == "__removed__")).sum())
    n_sec_added = int(
        (sample_assessed & gdf[sec_crop_col].isna() & gdf[qc_crop_sec_col].notna()
         & (gdf[qc_crop_sec_col] != "__removed__")).sum()
    )
    sec_accuracy = (
        (n_assessed_with_sec - n_sec_changed) / n_assessed_with_sec
        if n_assessed_with_sec > 0 else float("nan")
    )

    per_crop_rows = [] # logic with respect to original labels, not QC-updated ones
    for crop, grp in gdf.loc[sample_mask].groupby(crop_col, sort=False):
        n_c      = len(grp)
        n_skip_c = int((grp[flag_col] == "skip").sum())
        n_ok     = int((grp[flag_col] == "correct").sum())
        n_ass    = n_c - n_skip_c
        per_crop_rows.append({
            "crop":         str(crop),
            "n_sampled":    n_c,
            "n_assessable": n_ass,
            "n_correct":    n_ok,
            "n_skipped":    n_skip_c,
            "accuracy":     n_ok / n_ass if n_ass > 0 else float("nan"),
        })
    per_crop_df = (pd.DataFrame(per_crop_rows)
                   .sort_values("n_sampled", ascending=False)
                   .reset_index(drop=True))

    changed_mask = sample_assessed & gdf[qc_crop_main_col].notna()
    if changed_mask.sum() > 0:
        ch = gdf.loc[changed_mask, [crop_col, qc_crop_main_col]].copy()
        ch.columns = ["from", "to"]
        main_changes_df = (ch.groupby(["from", "to"]).size()
                           .reset_index(name="count")
                           .sort_values("count", ascending=False)
                           .reset_index(drop=True))
    else:
        main_changes_df = pd.DataFrame(columns=["from", "to", "count"])

    return {
        "name":                name,
        "n_total":             n_total,
        "n_monocrop":          n_monocrop,
        "n_intercrop":         n_intercrop,
        "pct_intercrop":       pct_intercrop,
        "n_with_photos":       n_with_photos,
        "n_reviewed":          n_reviewed,
        "n_correct":           n_correct,
        "n_skipped":           n_skipped,
        "n_assessable":        n_assessable,
        "skip_rate":           skip_rate,
        "field_accuracy":      field_accuracy,
        "n_main_changed":      n_main_changed,
        "main_accuracy":       main_accuracy,
        "n_assessed_with_sec": n_assessed_with_sec,
        "n_sec_changed":       n_sec_changed,
        "n_sec_removed":       n_sec_removed,
        "n_sec_added":         n_sec_added,
        "sec_accuracy":        sec_accuracy,
        "per_crop_df":         per_crop_df,
        "main_changes_df":     main_changes_df,
    }


def print_qc_summary(metrics):
    """Print a summary from a metrics dict."""
    m = metrics
    assessable     = m["n_assessable"]
    correct        = m["n_correct"]
    mismatches     = assessable - correct
    n_main_changed = m["n_main_changed"]
    n_aws          = m["n_assessed_with_sec"]
    n_sec_changed  = m["n_sec_changed"]
    n_sec_removed  = m["n_sec_removed"]
    n_sec_added    = m["n_sec_added"]
    field_acc      = m["field_accuracy"]
    main_acc       = m["main_accuracy"]
    sec_acc        = m["sec_accuracy"]

    title = "QC Summary"
    if m.get("name"):
        title = f"{title} — {m['name']}"

    sep = "=" * 50
    print()
    print(sep)
    print(title)
    print(sep)
    print(f"  Reviewed:        {m['n_reviewed']}")
    print(f"  Skipped:         {m['n_skipped']}  (unusable or ambiguous, not assessed)")
    print(f"  Assessable:      {assessable}  (correct + mismatch)")
    print()
    print("  Label Accuracy")
    print("  " + "-" * 46)
    print(f"  Field-level (any label wrong):")
    print(f"    Correct:            {correct} / {assessable}  ({100*field_acc:.1f}%)")
    print(f"    Mismatch:           {mismatches} / {assessable}  ({100*(1-field_acc):.1f}%)")
    print(f"  Main crop  (n={assessable}):")
    print(f"    Correct:            {assessable - n_main_changed} / {assessable}  ({100*main_acc:.1f}%)")
    print(f"    Changed:            {n_main_changed} / {assessable}  ({100*(1-main_acc):.1f}%)")
    print(f"  Secondary crop  (n={n_aws} with original sec crop):")
    print(f"    Correct:            {n_aws - n_sec_changed} / {n_aws}  ({100*sec_acc:.1f}%)")
    print(f"    Changed:            {n_sec_changed} / {n_aws}  ({100*(1-sec_acc):.1f}%)")
    print(f"      of which removed: {n_sec_removed}")
    if n_sec_added:
        print(f"  Sec crop added (was None):  {n_sec_added}  (not in accuracy denominator)")

    per_crop = m["per_crop_df"]
    if len(per_crop) > 0:
        print()
        print("  Per-crop Accuracy")
        print("  " + "-" * 46)
        print(f"  {'Crop':<24}  {'N':>5}  {'Correct':>7}  {'Accuracy':>8}")
        for _, row in per_crop.iterrows():
            crop = str(row["crop"])
            n    = int(row["n_sampled"])
            ok   = int(row["n_correct"])
            acc  = row["accuracy"]
            pct_str = f"{100*acc:>7.1f}%" if pd.notna(acc) else "    N/A"
            print(f"  {crop:<24}  {n:>5}  {ok:>7}  {pct_str}")

    print(sep)
    print()


def compute_shape_metrics(gdf, name="",
                          shape_skip_col="qc_shape_skip",
                          shape_mismatch_col="qc_shape_mismatch",
                          shape_offset_col="qc_shape_offset_m",
                          shape_sample_col="qc_shape_sample",
                          reviewed_col="qc_reviewed"):
    """Compute geometric QC metrics from a reviewed GeoDataFrame.

    Shape review is an offline QGIS step performed after the
    thematic `run_qc` session: a reviewer inspects the digitised polygon
    against the underlying imagery, marks `qc_shape_skip=True` if it cannot
    be assessed, `qc_shape_mismatch=True` if the polygon shape does nota ligh or is a subset of
    the field/parcel visible in the image,
    and records `qc_shape_offset_m` as the metric magnitude of the
    digitising offset (NaN = no offset measured, 0 = exact, > 0 in metres).

    The shape sample auto-aligns to the thematic sample (`qc_reviewed=True`).
    If `qc_shape_sample` is present (a separate, often larger, shape-only
    sample — e.g. SRS23 where the thematic sample is small) it is unioned
    with the thematic sample.

    All percentages share a common denominator `n_shape_reviewed` so the
    three quantities (skip, mismatch, offset) are directly comparable. The
    offset (`qc_shape_offset_m`) is also summarised as mean / std over rows
    with a positive measured offset.
    """
    required = [shape_skip_col, shape_mismatch_col, shape_offset_col]
    missing  = [c for c in required if c not in gdf.columns]
    if missing:
        raise KeyError(f"missing shape columns: {missing}")

    if reviewed_col in gdf.columns:
        sample_mask = gdf[reviewed_col].isin([True, 1, "True"])
    else:
        sample_mask = pd.Series(False, index=gdf.index)
    if shape_sample_col in gdf.columns:
        sample_mask = sample_mask | gdf[shape_sample_col].fillna(False).astype(bool)

    n_reviewed = int(sample_mask.sum())
    n_skipped  = int(gdf.loc[sample_mask, shape_skip_col].fillna(False).astype(bool).sum())
    n_mismatch = int(gdf.loc[sample_mask, shape_mismatch_col].fillna(False).astype(bool).sum())

    offsets = pd.to_numeric(gdf.loc[sample_mask, shape_offset_col], errors="coerce")
    pos = offsets[offsets > 0]
    n_with_offset = int(len(pos))

    denom = n_reviewed if n_reviewed > 0 else float("nan")
    pct_shape_skipped  = n_skipped      / denom
    pct_shape_mismatch = n_mismatch     / denom
    pct_with_offset    = n_with_offset  / denom

    mean_offset_m = float(pos.mean()) if n_with_offset > 0 else float("nan")
    std_offset_m  = float(pos.std())  if n_with_offset > 1 else float("nan")

    return {
        "name":               name,
        "n_shape_reviewed":   n_reviewed,
        "n_shape_skipped":    n_skipped,
        "pct_shape_skipped":  pct_shape_skipped,
        "n_shape_mismatch":   n_mismatch,
        "pct_shape_mismatch": pct_shape_mismatch,
        "n_with_offset":      n_with_offset,
        "pct_with_offset":    pct_with_offset,
        "mean_offset_m":      mean_offset_m,
        "std_offset_m":       std_offset_m,
    }


def print_shape_summary(metrics):
    """Print a summary from a shape-metrics dict (see `compute_shape_metrics`)."""
    m = metrics
    n_r = m["n_shape_reviewed"]

    title = "Shape QC Summary"
    if m.get("name"):
        title = f"{title} — {m['name']}"

    sep = "=" * 50
    print()
    print(sep)
    print(title)
    print(sep)
    print(f"  Reviewed: {n_r}  (denominator for all percentages below)")
    print()
    print(f"  {'Metric':<22} {'N':>6} {'%':>8}")
    print("  " + "-" * 38)
    print(f"  {'Skipped':<22} {m['n_shape_skipped']:>6} {100*m['pct_shape_skipped']:>7.1f}%")
    print(f"  {'Shape mismatch':<22} {m['n_shape_mismatch']:>6} {100*m['pct_shape_mismatch']:>7.1f}%")
    print(f"  {'Offset > 0':<22} {m['n_with_offset']:>6} {100*m['pct_with_offset']:>7.1f}%")
    print()
    print("  Offset (m), rows with offset > 0")
    print("  " + "-" * 38)
    if m["n_with_offset"] > 0:
        print(f"    Mean:  {m['mean_offset_m']:.2f}")
        if pd.notna(m["std_offset_m"]):
            print(f"    Std:   {m['std_offset_m']:.2f}")
    else:
        print("    (no positive offsets recorded)")

    print(sep)
    print()
