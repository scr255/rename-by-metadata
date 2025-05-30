import concurrent.futures
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import exiftool
import pandas as pd
from tqdm import tqdm

logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


class RenamerExifTool:
    """
    Renames media files (photos/videos) based on metadata timestamps,
    preserves subfolder structure, and removes empty source directories.

    Args:
        dir_sc (str): Source directory for original media files.
    """

    VALID_VIDEO_EXTENSIONS = {".avi", ".mov", ".mp4"}
    VALID_PHOTO_EXTENSIONS = {".arw", ".dng", ".nef", ".x3f"}
    COLS = [
        "SourceFile",
        "File:FileName",
        "File:Directory",
        "File:FileSize",
        "File:FileTypeExtension",
        "QuickTime:CreateDate",
        "QuickTime:ImageWidth",
        "QuickTime:ImageHeight",
        "QuickTime:VideoFrameRate",
        "QuickTime:Model",
        "QuickTime:Sharpness",
        "QuickTime:ColorMode",
        "QuickTime:MaximumShutterAngle",
        "QuickTime:AutoISOMax",
        "QuickTime:AutoISOMin",
        "QuickTime:FieldOfView",
        "QuickTime:GoPro_EISE",
        "QuickTime:GoPro_LOGB",
        "QuickTime:GoPro_NR01",
        "QuickTime:GoPro_CPIN",
        "QuickTime:GoPro_EXPX",
        "QuickTime:GoPro_EXPN",
        "QuickTime:GoPro_EXPQ",
        "QuickTime:Encoder",
        "EXIF:Make",
        "EXIF:Model",
        "EXIF:ImageWidth",
        "EXIF:ImageHeight",
        "EXIF:CreateDate",
        "MakerNotes:ShutterCount",
        "Composite:SubSecCreateDate",
    ]

    def __init__(self, dir_sc: str) -> None:
        self.dir_sc = dir_sc
        self.df: Optional[pd.DataFrame] = None

    @staticmethod
    def _parse_datetime(dt_str: str) -> Optional[datetime]:
        """
        Attempt to parse a datetime string in various ExifTool-related formats.
        Returns a naive (UTC) datetime on success, or None on failure.
        """
        if not dt_str:
            return None
        try:
            # Try ExifTool style with optional fractions and time zone
            has_tz = bool(re.search(r"[+\-]\d{2}:\d{2}$", dt_str))
            parts = dt_str.split(" ")
            has_fraction = len(parts) > 1 and "." in parts[1]

            fmt = "%Y:%m:%d %H:%M:%S"
            if has_fraction:
                fmt += ".%f"
            if has_tz:
                fmt += "%z"

            capture_dt = datetime.strptime(dt_str, fmt)
            # Convert any tz-aware datetime to naive UTC
            if capture_dt.tzinfo is not None:
                capture_dt = capture_dt.astimezone(timezone.utc).replace(tzinfo=None)
            return capture_dt
        except Exception:
            return None

    @staticmethod
    def _parse_cpin(val: Any) -> Optional[int]:
        """
        Try converting GoPro CPIN to an integer. Return None if invalid.
        """
        if pd.isna(val) or str(val).strip() == "":
            return None
        try:
            return int(val)
        except Exception:
            return None

    @staticmethod
    def _is_video(file_ext: str) -> bool:
        """Return True if file_ext is recognized as a video."""
        return file_ext.lower() in RenamerExifTool.VALID_VIDEO_EXTENSIONS

    @staticmethod
    def _is_photo(file_ext: str) -> bool:
        """Return True if file_ext is recognized as a photo."""
        return file_ext.lower() in RenamerExifTool.VALID_PHOTO_EXTENSIONS

    @staticmethod
    def _get_preferred_datetime(meta: Dict[str, Any]) -> Tuple[str, str, str]:
        """
        Determine the preferred datetime tag and its value from metadata.

        Returns:
            Tuple (tag_date, chosen_datetime_str, warning_msg).
        """
        tag_date = ""
        chosen_datetime_str = ""
        warning_msg = ""

        # Get the file extension from metadata (lowercased for comparison)
        file_ext = meta.get("File:FileTypeExtension", "").lower()

        if RenamerExifTool._is_video(f".{file_ext}"):
            # For videos, prefer QuickTime:CreateDate if it's a non-empty string
            qt_val = meta.get("QuickTime:CreateDate")
            if isinstance(qt_val, str) and qt_val.strip() != "":
                tag_date = "QuickTime:CreateDate"
                chosen_datetime_str = qt_val
            else:
                warning_msg = "Warning: No valid QuickTime:CreateDate found."
        elif RenamerExifTool._is_photo(f".{file_ext}"):
            # For photos: prefer Composite:SubSecCreateDate, else EXIF:CreateDate
            sub_sec_val = meta.get("Composite:SubSecCreateDate")
            exif_val = meta.get("EXIF:CreateDate")

            if (
                isinstance(sub_sec_val, str)
                and sub_sec_val.strip() != ""
                and "." in sub_sec_val
            ):
                tag_date = "Composite:SubSecCreateDate"
                chosen_datetime_str = sub_sec_val
            elif isinstance(exif_val, str) and exif_val.strip() != "":
                tag_date = "EXIF:CreateDate"
                chosen_datetime_str = exif_val
            else:
                warning_msg = "Warning: No valid Composite:SubSecCreateDate or EXIF:CreateDate found."
        else:
            warning_msg = "Warning: Unrecognized file type or no date fields."

        return tag_date, chosen_datetime_str, warning_msg

    @staticmethod
    def _process_batch(batch: List[str]) -> List[dict]:
        """
        Helper method to process a batch of files using exiftool.
        Each process opens its own exiftool instance.
        """
        try:
            with exiftool.ExifTool() as et:
                return et.execute_json(
                    "-G1", "-u", "-api", "largefilesupport=1", *batch
                )
        except Exception as e:
            logger.warning(
                f"Error processing batch starting with {batch[0] if batch else 'N/A'}: {e}"
            )
            return []

    @staticmethod
    def _has_filesize_duplicates(group: pd.DataFrame) -> bool:
        """
        Return True if the group has repeated file sizes (indicating duplicates).
        """
        group = group.copy()
        group["filesize"] = pd.to_numeric(group["File:FileSize"], errors="coerce")
        return group["filesize"].duplicated().any()

    def collect_files(self) -> List[str]:
        """
        Walk through the source directory and return all non-hidden files (rel paths).
        """
        all_files = []
        for root, _, files in os.walk(self.dir_sc):
            for f in files:
                if not f.startswith("."):
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, self.dir_sc)
                    all_files.append(rel_path)
        all_files.sort()
        logger.info(f"Found {len(all_files)} files in {self.dir_sc}")
        return all_files

    def extract_metadata(self, collected_files: List[str]) -> pd.DataFrame:
        """
        Use exiftool to gather metadata for each file in batches using parallel processing.
        """
        abs_paths = [os.path.join(self.dir_sc, f) for f in collected_files]
        batch_size = 16
        batches = [
            abs_paths[i : i + batch_size] for i in range(0, len(abs_paths), batch_size)
        ]
        all_metadata = []

        with concurrent.futures.ProcessPoolExecutor() as executor:
            for batch_metadata in tqdm(
                executor.map(RenamerExifTool._process_batch, batches),
                total=len(batches),
                desc="Extracting metadata",
            ):
                all_metadata.extend(batch_metadata)

        df = pd.DataFrame(all_metadata)
        for col in RenamerExifTool.COLS:
            if col not in df.columns:
                df[col] = pd.NA
        l = []
        for col in df:
            if col in RenamerExifTool.COLS:
                l.append(col)
        print(l)
        df = df[RenamerExifTool.COLS]
        if "SourceFile" not in df.columns:
            raise ValueError("Missing 'SourceFile' in exiftool output.")
        df["SourceFile"] = df["SourceFile"].apply(
            lambda x: os.path.relpath(x, self.dir_sc)
        )
        return df

    def _determine_capture_datetime(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Pick the correct datetime for each file, parse it, and store in df.
        """
        tags, datetimes, warnings = [], [], []

        for _, row in tqdm(df.iterrows(), total=len(df), desc="Determining datetime"):
            meta = row.to_dict()
            tag_date, chosen_datetime_str, warning_msg = self._get_preferred_datetime(
                meta
            )
            capture_dt = None
            if chosen_datetime_str:
                capture_dt = self._parse_datetime(chosen_datetime_str)
                if capture_dt is None and not warning_msg:
                    warning_msg = "Warning: Could not parse date"

            tags.append(tag_date)
            datetimes.append(capture_dt)
            warnings.append(warning_msg)

        df["tag_date"] = tags
        df["datetime_capture"] = datetimes
        df["warning_msg"] = warnings
        df["dir"] = df["SourceFile"].apply(os.path.dirname)
        df["ext"] = df["File:FileName"].apply(lambda x: os.path.splitext(x)[1])
        return df

    def _filter_invalid_dates(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Filter out files that have no valid datetime_capture.
        """
        invalid = df[df["datetime_capture"].isna()]
        for _, row in invalid.iterrows():
            logger.warning(f"No valid date for {row['SourceFile']}")
        df = df[~df["datetime_capture"].isna()].copy()
        df["datetime_capture_sec"] = df["datetime_capture"].dt.floor("S")
        return df

    def _tie_break_by_size(
        self,
        group: pd.DataFrame,
        df: pd.DataFrame,
        dt_col: str,
        ext_col: str,
        skip_msg: str,
        label_msg: str,
        time_fmt: str = "%Y_%m_%d_%H_%M_%S",
        ascending: bool = False,
    ) -> None:
        """
        Tie-break items in 'group' by file size (descending by default).
        If repeated file sizes are found, skip them all (suspected duplicates).
        Otherwise label them in order.

        Args:
            group: Subgroup of rows sharing the same second or tie condition.
            df: The master DataFrame to update.
            dt_col: Name of the column holding the capture datetime.
            ext_col: Name of the column holding the file extension.
            skip_msg: Message to store in the 'warning_msg' column if skipped.
            label_msg: Message to store in the 'info_msg' column upon success.
            time_fmt: Strftime pattern for the renamed filename.
            ascending: Whether to sort file size ascending (default False).
        """
        if self._has_filesize_duplicates(group):
            for idx in group.index:
                df.loc[idx, "filename_new"] = None
                df.loc[idx, "warning_msg"] = skip_msg
            return

        group = group.copy()
        group["filesize"] = pd.to_numeric(group["File:FileSize"], errors="coerce")
        group = group.sort_values(by="filesize", ascending=ascending)

        i = 1
        for idx, row in group.iterrows():
            capture_dt = row[dt_col]
            ext_val = row[ext_col]
            if capture_dt:
                suffix = f"{i:03d}"
                df.loc[idx, "filename_new"] = (
                    f"{capture_dt.strftime(time_fmt)}_{suffix}{ext_val}"
                )
                df.loc[idx, "info_msg"] = label_msg
                i += 1

    def _build_new_filenames_videos(self, df: pd.DataFrame) -> None:
        """
        Build new filenames for videos.
        - GoPro videos (CPIN >= 1) follow CPIN logic.
        - Non-GoPro videos with multiple files in the same second:
          sort by file size (descending) for tie-breaking.
          If at least two share the same file size, skip renaming them all.
        """
        video_mask = df["ext"].apply(lambda x: x.lower() in self.VALID_VIDEO_EXTENSIONS)
        df_videos = df[video_mask].copy()

        for dir_name, group in df_videos.groupby("dir"):
            # QuickTime:Model check
            models = group["QuickTime:Model"].dropna().unique()
            if len(models) > 1:
                # Mismatch in QuickTime:Model => skip them all
                for idx in group.index:
                    df.loc[idx, "filename_new"] = None
                    df.loc[idx, "warning_msg"] = "Warning: Mismatch in QuickTime:Model"
                continue

            # Filter the videos for just this directory
            dir_mask = df_videos["dir"] == dir_name
            dir_videos = df_videos[dir_mask].copy()

            # Identify GoPro
            dir_videos["is_gopro"] = dir_videos.apply(
                lambda row: (self._parse_cpin(row.get("QuickTime:GoPro_CPIN", "")) or 0)
                >= 1,
                axis=1,
            )

            # ------------------
            # 1) GoPro videos
            # ------------------
            gopro_videos = dir_videos[dir_videos["is_gopro"]].copy()
            gopro_videos["cpin"] = gopro_videos["QuickTime:GoPro_CPIN"].apply(
                self._parse_cpin
            )
            ref_candidates = gopro_videos[gopro_videos["cpin"] == 1]

            for idx, row in gopro_videos.iterrows():
                capture_dt = row["datetime_capture"]
                cpin_val = row["cpin"]
                ext_val = row["ext"]

                if cpin_val is None or cpin_val < 1:
                    df.loc[idx, "warning_msg"] = "Warning: Invalid CPIN"
                    df.loc[idx, "filename_new"] = None
                    continue

                if cpin_val != 1:
                    if ref_candidates.empty:
                        df.loc[idx, "warning_msg"] = "Warning: No CPIN==1 reference"
                        df.loc[idx, "filename_new"] = None
                        continue
                    # find whichever CPIN==1 is closest in time
                    time_diffs = (ref_candidates["datetime_capture"] - capture_dt).abs()
                    capture_dt = ref_candidates.loc[
                        time_diffs.idxmin(), "datetime_capture"
                    ]

                if capture_dt:
                    time_str = capture_dt.strftime("%Y_%m_%d_%H_%M_%S")
                    df.loc[idx, "filename_new"] = f"{time_str}_{cpin_val:03d}{ext_val}"
                    df.loc[idx, "info_msg"] = "Video (GoPro)"

            # ------------------
            # 2) Non-GoPro
            # ------------------
            non_gopro = dir_videos[~dir_videos["is_gopro"]].copy()
            non_gopro["dt_sec"] = non_gopro["datetime_capture"].dt.floor("S")

            for _, sub_group in non_gopro.groupby("dt_sec"):
                if len(sub_group) == 1:
                    idx = sub_group.index[0]
                    capture_dt = sub_group.iloc[0]["datetime_capture"]
                    ext_val = sub_group.iloc[0]["ext"]
                    if capture_dt:
                        df.loc[idx, "filename_new"] = (
                            f"{capture_dt.strftime('%Y_%m_%d_%H_%M_%S')}_001{ext_val}"
                        )
                        df.loc[idx, "info_msg"] = "Video (non-GoPro, single)"
                else:
                    # Tie-break by file size (descending). Skip if duplicates
                    self._tie_break_by_size(
                        group=sub_group,
                        df=df,
                        dt_col="datetime_capture",
                        ext_col="ext",
                        skip_msg="Warning: Suspected duplicate (filesize repeated)",
                        label_msg="Video (non-GoPro, tie-break by size)",
                        ascending=False,
                    )

    def _build_new_filenames_photos(self, df: pd.DataFrame) -> None:
        """
        Build new filenames for photos.
        - If Composite:SubSecCreateDate is present, use sub-second ordering.
        - Otherwise, if multiple in the same second:
          check MakerNotes:ShutterCount; if missing, tie-break by file size.
          If duplicates by file size, skip them (suspected duplicates).
        """
        photo_mask = df["ext"].apply(lambda x: x.lower() in self.VALID_PHOTO_EXTENSIONS)
        df_photos = df[photo_mask].copy()

        for dir_name, group in df_photos.groupby("dir"):
            # Check EXIF:Make/Model mismatch
            filtered = (
                group[["EXIF:Make", "EXIF:Model"]].dropna(how="all").drop_duplicates()
            )
            if len(filtered) > 1:
                for idx in group.index:
                    df.loc[idx, "filename_new"] = None
                    df.loc[idx, "warning_msg"] = "Warning: Mismatch in EXIF:Make/Model"
                continue

            dir_photos = df_photos[df_photos["dir"] == dir_name].copy()

            # Check sub-second composite
            dir_photos["has_composite"] = dir_photos[
                "Composite:SubSecCreateDate"
            ].apply(lambda x: isinstance(x, str) and x.strip() != "" and "." in x)

            composite_photos = dir_photos[dir_photos["has_composite"]].copy()
            non_composite_photos = dir_photos[~dir_photos["has_composite"]].copy()

            # 1) Composite photos
            composite_photos["exif_dt"] = composite_photos["EXIF:CreateDate"].apply(
                self._parse_datetime
            )
            composite_photos["comp_dt"] = composite_photos[
                "Composite:SubSecCreateDate"
            ].apply(self._parse_datetime)
            composite_photos["dt_sec"] = composite_photos["exif_dt"].apply(
                lambda d: d.replace(microsecond=0) if d else None
            )

            for _, cgroup in composite_photos.groupby("dt_sec"):
                cgroup = cgroup.copy().sort_values(by="comp_dt")
                for i, (idx, row) in enumerate(cgroup.iterrows(), start=1):
                    capture_dt = row["exif_dt"]
                    ext_val = row["ext"]
                    if capture_dt:
                        suffix = f"{i:03d}"
                        df.loc[idx, "filename_new"] = (
                            f"{capture_dt.strftime('%Y_%m_%d_%H_%M_%S')}_{suffix}{ext_val}"
                        )
                        df.loc[idx, "info_msg"] = "Photo (composite)"

            # 2) Non-composite
            non_composite_photos["exif_dt"] = non_composite_photos[
                "EXIF:CreateDate"
            ].apply(self._parse_datetime)
            non_composite_photos["dt_sec"] = non_composite_photos["exif_dt"].apply(
                lambda d: d.replace(microsecond=0) if d else None
            )

            for _, ncgroup in non_composite_photos.groupby("dt_sec"):
                if len(ncgroup) == 1:
                    idx = ncgroup.index[0]
                    capture_dt = ncgroup.iloc[0]["exif_dt"]
                    ext_val = ncgroup.iloc[0]["ext"]
                    if capture_dt:
                        df.loc[idx, "filename_new"] = (
                            f"{capture_dt.strftime('%Y_%m_%d_%H_%M_%S')}_001{ext_val}"
                        )
                        df.loc[idx, "info_msg"] = "Photo (non-composite, single)"
                    continue

                # Multiple photos in the same second -> check shutter count first
                if "MakerNotes:ShutterCount" in ncgroup.columns:
                    shutter_vals = ncgroup["MakerNotes:ShutterCount"].fillna("")
                    # If all entries have a shutter count, sort by it
                    if shutter_vals.apply(lambda x: str(x).strip() != "").all():
                        ncgroup = ncgroup.copy()
                        ncgroup["shutter"] = ncgroup["MakerNotes:ShutterCount"].astype(
                            int
                        )
                        ncgroup = ncgroup.sort_values(by="shutter")
                        for i, (idx, row) in enumerate(ncgroup.iterrows(), start=1):
                            capture_dt = row["exif_dt"]
                            ext_val = row["ext"]
                            if capture_dt:
                                suffix = f"{i:03d}"
                                df.loc[idx, "filename_new"] = (
                                    f"{capture_dt.strftime('%Y_%m_%d_%H_%M_%S')}_{suffix}{ext_val}"
                                )
                                df.loc[idx, "info_msg"] = (
                                    "Photo (non-composite, shutter count)"
                                )
                        continue

                # Fallback: tie-break by file size
                self._tie_break_by_size(
                    group=ncgroup,
                    df=df,
                    dt_col="exif_dt",
                    ext_col="ext",
                    skip_msg="Warning: Suspected duplicate (filesize repeated)",
                    label_msg="Photo (non-composite, tie-break by size)",
                    ascending=False,
                )

    def _build_new_filenames(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Entrypoint for building new filenames for both videos and photos.
        """
        df["filename_new"] = None
        df["info_msg"] = ""
        self._build_new_filenames_videos(df)
        self._build_new_filenames_photos(df)
        return df

    def _rename_files(self, dir_ft: str, df: pd.DataFrame) -> None:
        """
        Move (rename) files from source to target, using the newly built filenames.
        Skips files that have no valid new filename or if the destination file exists.

        Args:
            dir_ft (str): Target (fact) directory where renamed files go.
            df: The master DataFrame to update.
        """
        os.makedirs(dir_ft, exist_ok=True)

        for _, row in tqdm(df.iterrows(), total=len(df), desc="Renaming files"):
            new_name = row["filename_new"]
            if not new_name:
                logger.warning(f"No new name for {row['SourceFile']}")
                continue

            old_path = os.path.join(self.dir_sc, row["SourceFile"])
            new_dir = os.path.join(dir_ft, row["dir"])
            new_path = os.path.join(new_dir, new_name)

            if not os.path.exists(old_path):
                logger.warning(f"Source missing {old_path}")
                continue
            if os.path.exists(new_path):
                logger.warning(f"Destination exists {new_path}")
                continue

            os.makedirs(new_dir, exist_ok=True)
            try:
                os.rename(old_path, new_path)
                logger.info(
                    f"Renamed {row['SourceFile']} -> {os.path.join(row['dir'], new_name)}"
                )
                if row["warning_msg"]:
                    logger.warning(row["warning_msg"])
            except Exception as e:
                logger.error(f"Error renaming {old_path} to {new_path} {e}")

    def _remove_empty_dirs(self, directory: str) -> None:
        logger.info("Starting removal of empty directories")
        # Build a list of all directories
        all_dirs = list(os.walk(directory, topdown=False))
        for root, dirs, files in tqdm(
            all_dirs, desc="Removing empty directories", unit="dir"
        ):
            # Identify non-hidden files/dirs
            visible_files = [f for f in files if not f.startswith(".")]
            visible_dirs = [d for d in dirs if not d.startswith(".")]

            # If directory has no visible content, then remove hidden items and try to remove dir
            if not visible_files and not visible_dirs:
                # Remove hidden files
                for hf in [f for f in files if f.startswith(".")]:
                    hf_path = os.path.join(root, hf)
                    try:
                        os.remove(hf_path)
                        logger.info(f"Removed hidden file {hf_path}")
                    except Exception as e:
                        logger.warning(f"Could not remove hidden file {hf_path} {e}")
                # Remove hidden directories
                for hd in [d for d in dirs if d.startswith(".")]:
                    hd_path = os.path.join(root, hd)
                    try:
                        shutil.rmtree(hd_path)
                        logger.info(f"Removed hidden directory {hd_path}")
                    except Exception as e:
                        logger.warning(
                            f"Could not remove hidden directory {hd_path} {e}"
                        )
                # Try to remove the empty directory
                try:
                    os.rmdir(root)
                    logger.info(f"Removed empty directory {root}")
                except OSError as e:
                    logger.warning(f"Cannot remove {root} {e}")
        logger.info("Completed removal of empty directories")

    def run(
        self, dir_st: str, dir_ft: str, path_csv: str, rename: bool = False
    ) -> pd.DataFrame:
        """
        Main entrypoint: gather metadata, plan new filenames, generate CSV,
        and optionally rename files.

        Args:
            dir_st (str): Staging directory for saving CSV reports.
            dir_ft (str): Target (fact) directory where renamed files go.
            path_csv (str): CSV filename (relative to dir_st) for reporting.
            rename (bool): If True, files are actually renamed; if False, only CSV is generated.

        Returns:
            pd.DataFrame: Final DataFrame with metadata and new filenames.
        """
        if self.df is not None:
            logger.info("Using cached metadata DataFrame.")
            df = self.df
        else:
            files = self.collect_files()
            df = self.extract_metadata(files)
            df = self._determine_capture_datetime(df)
            df = self._filter_invalid_dates(df)
            df = self._build_new_filenames(df)
            self.df = df

        os.makedirs(dir_st, exist_ok=True)
        out_path = os.path.join(dir_st, path_csv)
        try:
            self.df.to_csv(out_path, index=False)
            logger.info(f"CSV report saved to {out_path}")
        except Exception as e:
            logger.error(f"Error saving CSV report {e}")

        if rename:
            self._rename_files(self.df)
            self._remove_empty_dirs(self.dir_sc)
        else:
            logger.info("[INFO] No rename performed (rename=False)")

        return self.df


def check_column_consistency(df, columns_to_check, column_pairs=None):
    """
    Check consistency of specified columns within each directory and show anomalous files.
    Also check if specified column pairs match for all files within each directory.

    Args:
        df: DataFrame to analyze
        columns_to_check: List of column names to check for consistency
        column_pairs: List of tuples, where each tuple contains two column names that should match
    """
    # Check consistency for all specified columns
    all_groups = df.groupby("File:Directory").agg(
        {col: "nunique" for col in columns_to_check}
    )

    # Create boolean mask for inconsistencies (values > 1)
    inconsistencies_mask = all_groups > 1

    # Check column pair matching if specified
    pair_inconsistencies = {}
    if column_pairs:
        for col1, col2 in column_pairs:
            for directory in df["File:Directory"].unique():
                dir_files = df[df["File:Directory"] == directory]
                # Check if col1 and col2 match for all files in this directory
                mismatched_files = dir_files[dir_files[col1] != dir_files[col2]]
                if not mismatched_files.empty:
                    if f"{col1} vs {col2}" not in pair_inconsistencies:
                        pair_inconsistencies[f"{col1} vs {col2}"] = {}
                    pair_inconsistencies[f"{col1} vs {col2}"][
                        directory
                    ] = mismatched_files

    # Check if any inconsistencies exist
    has_individual_inconsistencies = inconsistencies_mask.any().any()
    has_pair_inconsistencies = len(pair_inconsistencies) > 0

    if not has_individual_inconsistencies and not has_pair_inconsistencies:
        print(
            "✔️✔️✔️ All directories have consistent values for specified columns and column pairs."
        )

    # Show individual column inconsistencies
    if has_individual_inconsistencies:
        print("❌❌❌ Inconsistent groups found:")
        for column in columns_to_check:
            inconsistent_dirs = all_groups[all_groups[column] > 1].index
            if len(inconsistent_dirs) > 0:
                print(f"🦑 {column}")
                for directory in inconsistent_dirs:
                    print(f"    {directory}")
                    dir_files = df[df["File:Directory"] == directory]
                    value_counts = dir_files[column].value_counts()
                    most_common_value = value_counts.index[0]
                    anomalous_files = dir_files[dir_files[column] != most_common_value]
                    print(f"        Most common value: {most_common_value}")
                    if not anomalous_files.empty:
                        print(f"        Anomalous files:")
                        for _, file_row in anomalous_files.iterrows():
                            print(
                                f"            {file_row['SourceFile']} (value: {file_row[column]})"
                            )
                    print()

    # Show column pair inconsistencies
    if has_pair_inconsistencies:
        for pair_name, directories in pair_inconsistencies.items():
            print(f"⚠️ {pair_name}")
            for directory, mismatched_files in directories.items():
                print(f"    {directory}")
                print(f"        Files with mismatched values:")
                for _, file_row in mismatched_files.iterrows():
                    col1, col2 = pair_name.split(" vs ")
                    print(
                        f"            {file_row['SourceFile']} ({col1}: {file_row[col1]}, {col2}: {file_row[col2]})"
                    )
                print()

    # Report consistent values for all columns
    print("📊📊📊 Consistent values report:")
    for column in columns_to_check:
        consistent_dirs = all_groups[all_groups[column] == 1].index
        if len(consistent_dirs) > 0:
            print(f"🐬 {column}:")
            for directory in consistent_dirs:
                dir_files = df[df["File:Directory"] == directory]
                consistent_value = dir_files[column].iloc[
                    0
                ]  # All values are the same, so take the first
                print(f"    {directory}: {consistent_value}")
            print()
        else:
            print(f"{column}: No directories with consistent values")
            print()