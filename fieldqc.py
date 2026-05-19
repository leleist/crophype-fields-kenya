"""
Interactive QC tool for reviewing adn relabeling crop-type labels against field images.

Keybindings (main):
    A  - Accept / correct  (both labels confirmed correct)
    D  - Mismatch  ->  sub-menu:
        W  - Relabel main crop
        E  - Relabel secondary crop
        F  - Relabel both (two dialogs in sequence)
        Q  - Cancel
    E  - Skip  (image unusable OR too ambiguous to assess)
    Q  - Go back one step
    R  - End session (quit)

Flag semantics
--------------
    correct  : reviewer confirmed both labels are correct.
    mismatch : reviewer identified at least one wrong label and corrected it.
    skip     : image cannot be assessed - unusable (dark, not a field, corrupted)
               or too ambiguous (crop not yet emerged, partial view).
               NOT a label error.

Only "correct" and "mismatch" rows enter accuracy calculations.
"skip" is excluded from the accuracy denominator and reported separately.

Resume support: a tracking column records which rows have been visited.
On relaunch you are prompted to continue or restart.

QC columns written to gdf (4 total):
    qc_reviewed  : True | None
    qc_flag      : "correct" | "mismatch" | "skip" | None (unreviewed)
    qc_crop_main : corrected main crop label | None (unchanged)
    qc_crop_sec  : corrected sec crop label | "__removed__" (sec crop removed) | None (unchanged)
"""

import random as _random
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

from PIL import Image, ImageTk, ImageDraw
import geopandas as gpd
import pandas as pd

# The literal "__removed__" is written into qc_crop_sec when the reviewer
# explicitly removes a secondary crop. It is distinct from None (= "no
# change") so post-QC queries can find these rows


class _CropPickerDialog(tk.Toplevel):
    """Modal dialog with a searchable combobox for picking a crop type.
    W/S keys navigate up/down through options."""

    def __init__(self, parent, title: str, current_crop: str, crop_choices: list[str]):
        super().__init__(parent)
        self.result: str | None = None
        self._choices = crop_choices
        self._idx = -1
        self.title(title)
        self.configure(bg="#1e1e1e")
        self.transient(parent)
        self.grab_set()

        tk.Label(
            self, text=f"Current: {current_crop}",
            font=("Helvetica", 12), fg="#aaaaaa", bg="#1e1e1e",
        ).pack(padx=20, pady=(15, 5))

        tk.Label(
            self, text="[W/S] Navigate   [Enter] OK   [Esc] Cancel",
            font=("Courier", 10), fg="#777777", bg="#1e1e1e",
        ).pack(padx=20, pady=(0, 5))

        self.combo = ttk.Combobox(
            self, values=crop_choices, font=("Helvetica", 13), width=30,
        )
        self.combo.pack(padx=20, pady=5)
        self.combo.focus_set()

        btn_frame = tk.Frame(self, bg="#1e1e1e")
        btn_frame.pack(pady=(5, 15))
        tk.Button(btn_frame, text="OK", width=10, command=self._ok).pack(
            side=tk.LEFT, padx=5,
        )
        tk.Button(btn_frame, text="Cancel", width=10, command=self._cancel).pack(
            side=tk.LEFT, padx=5,
        )

        self.combo.bind("<Return>", lambda _: self._ok())
        self.combo.bind("w", self._nav_up)
        self.combo.bind("W", self._nav_up)
        self.combo.bind("s", self._nav_down)
        self.combo.bind("S", self._nav_down)
        self.bind("<Escape>", lambda _: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.update_idletasks()
        px = parent.winfo_rootx() + parent.winfo_width() // 2 - self.winfo_width() // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2 - self.winfo_height() // 2
        self.geometry(f"+{px}+{py}")

        self.wait_window()

    def _nav_up(self, _event=None):
        if not self._choices:
            return "break"
        self._idx = max(0, self._idx - 1)
        self.combo.set(self._choices[self._idx])
        return "break"

    def _nav_down(self, _event=None):
        if not self._choices:
            return "break"
        self._idx = min(len(self._choices) - 1, self._idx + 1)
        self.combo.set(self._choices[self._idx])
        return "break"

    def _ok(self):
        val = self.combo.get().strip()
        if val:
            self.result = val
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


def _build_sample(
    reviewable: gpd.GeoDataFrame,
    n_samples: int,
    random_seed,
    stratify_col: str | None,
) -> list:
    """Return a list of row indices for stratified random sampling.

    Divides n_samples roughly equally across strata. If a stratum has fewer
    rows than its quota, the shortfall is filled from the pooled leftovers of
    other strata. The final list is shuffled so strata are interleaved.
    """
    rng = _random.Random(random_seed)

    if stratify_col and stratify_col in reviewable.columns:
        strata_groups = {}
        for stratum, group in reviewable.groupby(stratify_col, sort=False):
            indices = group.index.tolist()
            rng.shuffle(indices)
            strata_groups[stratum] = indices

        n_strata = len(strata_groups)
        per_stratum = max(1, n_samples // n_strata)

        selected = []
        leftover = []
        for indices in strata_groups.values():
            take = min(per_stratum, len(indices))
            selected.extend(indices[:take])
            leftover.extend(indices[take:])

        still_needed = n_samples - len(selected)
        if still_needed > 0 and leftover:
            rng.shuffle(leftover)
            selected.extend(leftover[:still_needed])

        rng.shuffle(selected)
        return selected[:n_samples]

    indices = reviewable.index.tolist()
    return rng.sample(indices, min(n_samples, len(indices)))


def run_qc(
    gdf: gpd.GeoDataFrame,
    image_col: str,
    crop_col: str,
    sec_crop_col: str,
    image_dir: str | Path,
    id_col: str | None = "field_id",
    flag_col: str = "qc_flag",
    qc_crop_main_col: str = "qc_crop_main",
    qc_crop_sec_col: str = "qc_crop_sec",
    reviewed_col: str = "qc_reviewed",
    max_image_size: tuple[int, int] = (800, 600),
    checkpoint_path: str | Path | None = None,
    n_samples: int | None = None,
    random_seed: int | None = None,
    stratify_col: str | None = None,
) -> None:
    """Launch an interactive tkinter window for visual QC of crop-type labels.

    Mutates `gdf` in place: writes verdicts and optional relabelings into the
    four QC columns. On return, `gdf` is guaranteed to contain exactly the
    columns it had on entry plus the four QC columns; any columns that may
    have been added internally (e.g. via checkpoint loading) are removed.

    Parameters
    ----------
    gdf : GeoDataFrame
        Must contain `image_col`, `crop_col`, and `sec_crop_col`.
    image_col : str
        Column holding image filenames (joined with `image_dir`).
    crop_col, sec_crop_col : str
        Columns holding the original main and secondary crop labels.
    image_dir : str or Path
        Directory where the images live.
    id_col : str or None
        Column with a unique field ID. Defaults to "field_id".
        Falls back to row index if the column is absent.
    flag_col : str
        QC verdict: "correct" | "mismatch" | "skip" | None (unreviewed).
    qc_crop_main_col : str
        Corrected main crop label. None if not changed.
    qc_crop_sec_col : str
        Corrected secondary crop label. ``"__removed__"`` when the reviewer
        explicitly removes a secondary crop; None when unchanged.
    reviewed_col : str
        Tracking column: True for every row visited (used for resume).
    max_image_size : (int, int)
        Maximum display size (width, height). Images are scaled to fit.
    checkpoint_path : str or Path or None
        If set, saves `gdf` to this `.gpkg` path after every review action.
    n_samples : int or None
        If set, review only this many randomly selected rows (with photos).
        When None (default) all rows with photos are reviewed in ID order.
    random_seed : int or None
        Seed for the random sampler - makes the sample reproducible, given a stable input order.
        Only used when `n_samples` is set.
    stratify_col : str or None
        Column to stratify the sample by (e.g. "crop_main"). When set,
        `n_samples` is divided roughly equally across unique values of this
        column. Strata that run out of photos donate their shortfall.
    """
    image_dir = Path(image_dir)
    input_cols = list(gdf.columns)
    qc_cols = [reviewed_col, flag_col, qc_crop_main_col, qc_crop_sec_col]

    # auto-load checkpoint if present. Restores QC progress without needing
    # to change the input gdf. Merges by field_id.
    if checkpoint_path is not None and Path(checkpoint_path).exists():
        ckpt = gpd.read_file(checkpoint_path)
        ckpt_cols = [c for c in qc_cols if c in ckpt.columns]
        if ckpt_cols:
            if "field_id" not in gdf.columns or "field_id" not in ckpt.columns:
                raise ValueError(
                    "Checkpoint merge requires a `field_id` column in both the input "
                    "GeoDataFrame and the checkpoint file."
                )
            ckpt_indexed = ckpt.set_index("field_id")[ckpt_cols]
            for col in ckpt_cols:
                gdf[col] = gdf["field_id"].map(ckpt_indexed[col])
            n_restored = int(ckpt[reviewed_col].notna().sum()) if reviewed_col in ckpt.columns else 0
            print(f"Checkpoint loaded: {n_restored} rows restored from {checkpoint_path}")

    all_crops = set(gdf[crop_col].dropna().unique()) | set(gdf[sec_crop_col].dropna().unique())
    crop_choices = sorted(all_crops, key=str.lower)

    for col in [flag_col, qc_crop_main_col, qc_crop_sec_col, reviewed_col]:
        if col not in gdf.columns:
            gdf[col] = None

    # reviewable rows: photo filename present, file exists on disk, deduplicated by filename
    reviewable_mask = gdf[image_col].notna()
    reviewable = gdf.loc[reviewable_mask]
    exists_mask = reviewable[image_col].apply(lambda f: (image_dir / str(f)).exists())
    reviewable = reviewable.loc[exists_mask]
    reviewable = reviewable.drop_duplicates(subset=[image_col], keep="first")
    n_with_photos = len(reviewable)

    if n_samples is None:
        if id_col and id_col in gdf.columns:
            reviewable = reviewable.sort_values(id_col)
        review_indices = reviewable.index.tolist()
    else:
        review_indices = _build_sample(reviewable, n_samples, random_seed, stratify_col)

    total = len(review_indices)

    if total == 0:
        print("No reviewable rows (all image filenames are NaN).")
        return

    resume_pos = total
    for i, idx in enumerate(review_indices):
        if pd.isna(gdf.at[idx, reviewed_col]):
            resume_pos = i
            break

    # Resume prompt - uses messagebox on a temporary root so there is no
    # Toplevel-on-withdrawn-root issue on Windows.
    if 0 < resume_pos < total:
        _tmp = tk.Tk()
        _tmp.withdraw()
        _tmp.attributes("-topmost", True)
        answer = messagebox.askyesnocancel(
            "Resume QC",
            f"Prior progress found: {resume_pos} / {total} reviewed.\n\n"
            f"Yes  → Continue from row {resume_pos + 1}\n"
            f"No   → Restart from row 1\n"
            f"Cancel → Abort",
            parent=_tmp,
        )
        _tmp.destroy()
        if answer is None:
            print("QC session aborted.")
            return
        start = resume_pos if answer else 0
    else:
        start = resume_pos

    root = tk.Tk()
    root.title("Crop-Type QC")
    root.configure(bg="#1e1e1e")
    root.lift()
    root.focus_force()

    state = {"pos": start, "mode": "main", "photo_ref": None}

    top_frame = tk.Frame(root, bg="#1e1e1e")
    top_frame.pack(fill=tk.X, padx=10, pady=(10, 0))

    progress_var = tk.StringVar()
    tk.Label(
        top_frame, textvariable=progress_var,
        font=("Helvetica", 14), fg="#aaaaaa", bg="#1e1e1e",
    ).pack(side=tk.LEFT)

    status_var = tk.StringVar()
    status_lbl = tk.Label(
        top_frame, textvariable=status_var,
        font=("Helvetica", 14, "bold"), fg="#66bb6a", bg="#1e1e1e",
    )
    status_lbl.pack(side=tk.RIGHT)

    crop_var = tk.StringVar()
    tk.Label(
        root, textvariable=crop_var,
        font=("Helvetica", 20, "bold"), fg="#ffffff", bg="#1e1e1e",
    ).pack(pady=(5, 0))

    sec_crop_var = tk.StringVar()
    tk.Label(
        root, textvariable=sec_crop_var,
        font=("Helvetica", 16), fg="#bbbbbb", bg="#1e1e1e",
    ).pack(pady=(0, 0))

    id_var = tk.StringVar()
    tk.Label(
        root, textvariable=id_var,
        font=("Helvetica", 12), fg="#888888", bg="#1e1e1e",
    ).pack()

    img_lbl = tk.Label(root, bg="#1e1e1e")
    img_lbl.pack(padx=10, pady=10, expand=True, fill=tk.BOTH)

    HELP_MAIN     = "[A] Accept   [D] Mismatch   [E] Skip   [Q] Back   [R] End"
    HELP_MISMATCH = "[W] Main   [E] Secondary   [F] Both   [Q] Cancel"

    help_var = tk.StringVar(value=HELP_MAIN)
    tk.Label(
        root, textvariable=help_var,
        font=("Courier", 12), fg="#777777", bg="#1e1e1e",
    ).pack(fill=tk.X, padx=10, pady=(0, 10))

    def _load_photo(filepath: Path) -> ImageTk.PhotoImage:
        try:
            img = Image.open(filepath)
        except Exception as exc:
            img = Image.new("RGB", (400, 300), color=(40, 40, 40))
            draw = ImageDraw.Draw(img)
            draw.text((20, 140), f"Cannot load:\n{exc}", fill=(255, 80, 80))
        w, h = img.size
        max_w, max_h = max_image_size
        scale = min(max_w / w, max_h / h, 1.0)
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(img)

    def _add_to_choices(crop: str):
        if crop not in crop_choices:
            crop_choices.append(crop)
            crop_choices.sort(key=str.lower)

    _NONE_SENTINEL = "-- None --"

    def _pick_crop(title: str, current: str, allow_none: bool = False) -> str | None:
        choices = [_NONE_SENTINEL] + crop_choices if allow_none else crop_choices
        dlg = _CropPickerDialog(root, title, current, choices)
        return dlg.result

    def _checkpoint(idx):
        gdf.at[idx, reviewed_col] = True
        if checkpoint_path is not None:
            gdf.to_file(checkpoint_path, driver="GPKG")

    def _show():
        pos = state["pos"]
        if pos < 0:
            state["pos"] = pos = 0
        if pos >= total:
            progress_var.set(f"Done - {total}/{total} reviewed")
            crop_var.set("All rows reviewed!")
            sec_crop_var.set("")
            id_var.set("Press [R] to end, [Q] to go back")
            img_lbl.config(image="")
            state["photo_ref"] = None
            return

        idx = review_indices[pos]
        row = gdf.loc[idx]

        progress_var.set(f"Field {pos + 1} / {total}")

        flag = gdf.at[idx, flag_col]
        flag_colours = {"correct": "#66bb6a", "mismatch": "#ef5350", "skip": "#aaaaaa"}
        if pd.notna(flag):
            status_lbl.config(fg=flag_colours.get(flag, "#aaaaaa"))
            status_var.set(f"[{flag}]")
        else:
            status_var.set("")

        main_display = row[crop_col]
        sec_display  = row[sec_crop_col] if pd.notna(row[sec_crop_col]) else "—"
        if pd.notna(flag) and flag == "mismatch":
            new_main = gdf.at[idx, qc_crop_main_col]
            new_sec  = gdf.at[idx, qc_crop_sec_col]
            if pd.notna(new_main):
                main_display = f"{new_main}  (was: {row[crop_col]})"
            if pd.notna(new_sec):
                if new_sec == "__removed__":
                    sec_display = f"None  (was: {row[sec_crop_col]})"
                else:
                    sec_display = f"{new_sec}  (was: {row[sec_crop_col]})"

        crop_var.set(f"Main: {main_display}")
        sec_crop_var.set(f"Secondary: {sec_display}")

        if id_col and id_col in gdf.columns:
            display_id = row[id_col]
        else:
            display_id = idx
        id_var.set(f"ID: {display_id}")

        photo = _load_photo(image_dir / str(row[image_col]))
        img_lbl.config(image=photo)
        state["photo_ref"] = photo

        state["mode"] = "main"
        help_var.set(HELP_MAIN)

    def _advance(delta: int = 1):
        state["pos"] += delta
        _show()

    def _on_key(event):
        key = event.keysym.lower()
        pos = state["pos"]

        if key == "r" and state["mode"] == "main":
            _quit()
            return

        if pos >= total:
            if key == "q":
                _advance(-1)
            return

        idx = review_indices[pos]

        if state["mode"] == "mismatch":
            if key == "w":
                new = _pick_crop("Relabel Main Crop", str(gdf.at[idx, crop_col]))
                if new:
                    gdf.at[idx, flag_col]         = "mismatch"
                    gdf.at[idx, qc_crop_main_col] = new
                    _add_to_choices(new)
                    _checkpoint(idx)
                    _advance()

            elif key == "e":
                new = _pick_crop("Relabel Secondary Crop", str(gdf.at[idx, sec_crop_col]), allow_none=True)
                if new:
                    gdf.at[idx, flag_col]        = "mismatch"
                    gdf.at[idx, qc_crop_sec_col] = "__removed__" if new == _NONE_SENTINEL else new
                    if new != _NONE_SENTINEL:
                        _add_to_choices(new)
                    _checkpoint(idx)
                    _advance()

            elif key == "f":
                new_main = _pick_crop("Relabel Main Crop", str(gdf.at[idx, crop_col]))
                if new_main is None:
                    return
                new_sec = _pick_crop("Relabel Secondary Crop", str(gdf.at[idx, sec_crop_col]), allow_none=True)
                if new_sec is None:
                    return
                gdf.at[idx, flag_col]         = "mismatch"
                gdf.at[idx, qc_crop_main_col] = new_main
                gdf.at[idx, qc_crop_sec_col]  = "__removed__" if new_sec == _NONE_SENTINEL else new_sec
                _add_to_choices(new_main)
                if new_sec != _NONE_SENTINEL:
                    _add_to_choices(new_sec)
                _checkpoint(idx)
                _advance()

            elif key == "q":
                state["mode"] = "main"
                help_var.set(HELP_MAIN)
            return

        if key == "a":
            gdf.at[idx, flag_col]         = "correct"
            gdf.at[idx, qc_crop_main_col] = None
            gdf.at[idx, qc_crop_sec_col]  = None
            _checkpoint(idx)
            _advance()

        elif key == "d":
            state["mode"] = "mismatch"
            help_var.set(HELP_MISMATCH)

        elif key == "e":
            gdf.at[idx, flag_col] = "skip"
            _checkpoint(idx)
            _advance()

        elif key == "q":
            if pos > 0:
                _advance(-1)

    def _quit():
        # Schedule destroy AFTER the current event handler returns - calling
        # destroy() directly inside a handler leaves Tcl in a broken state on
        # Windows and prevents the window reappearing on re-run. Was big trouble.
        root.after(0, root.destroy)

    root.protocol("WM_DELETE_WINDOW", _quit)
    root.bind("<Key>", _on_key)
    _show()
    root.mainloop()

    try:
        root.destroy()
    except Exception:
        pass

    # enforce output contract: input cols + the four QC cols, nothing else
    allowed = set(input_cols) | {reviewed_col, flag_col, qc_crop_main_col, qc_crop_sec_col}
    extra = [c for c in gdf.columns if c not in allowed]
    if extra:
        gdf.drop(columns=extra, inplace=True)

    # session summary: delegated to utils
    from utils import compute_qc_metrics, print_qc_summary

    metrics = compute_qc_metrics(
        gdf,
        flag_col=flag_col,
        qc_crop_main_col=qc_crop_main_col,
        qc_crop_sec_col=qc_crop_sec_col,
        reviewed_col=reviewed_col,
        crop_col=crop_col,
        sec_crop_col=sec_crop_col,
        photo_col=image_col,
    )
    n_reviewed = metrics["n_reviewed"]
    print()
    print(f"Sample: {total} fields (of {n_with_photos} with photos on disk)")
    print(f"Progress: {n_reviewed} / {total}" + (f"  ({100*n_reviewed/total:.1f}%)" if total > 0 else ""))
    print_qc_summary(metrics)
