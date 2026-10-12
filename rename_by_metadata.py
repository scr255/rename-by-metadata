import logging
from datetime import datetime

import numpy as np
import pandas as pd

from src.functions import RenamerExifTool, check_column_consistency

# Directories
DIR_SC = "sc"  # Source
DIR_ST = "st"  # Staging
DIR_FT = "ft"  # Fact


def confirm(prompt):
    """Ask a yes/no question on the terminal."""
    while True:
        raw = input(f"{prompt} [y/N]: ").strip().lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("", "n", "no"):
            return False
        print("Please answer 'y' or 'n'.")


def prompt_value(prompt, default):
    """Ask for a value on the terminal, keeping ``default`` on empty input."""
    raw = input(f"{prompt} [{default}]: ").strip()
    return raw or default


def main():
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    pd.set_option("display.max_colwidth", None)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.max_rows", None)

    # Mapping of filenames before to after to allow for reversibility
    current_time = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    path_csv = f"mapping_{current_time}.csv"

    # Directories (press Enter to keep the default)
    dir_sc = prompt_value("Source directory", DIR_SC)
    dir_st = prompt_value("Staging directory", DIR_ST)
    dir_ft = prompt_value("Fact directory", DIR_FT)

    # Init
    renamer = RenamerExifTool(dir_sc=dir_sc)

    # Create df without actual renaming
    df = renamer.run(dir_st=dir_st, dir_ft=dir_ft, path_csv=path_csv, rename=False)
    print(df.shape)

    # Count warnings
    df["warning_msg"] = df["warning_msg"].fillna("")
    if df[df["warning_msg"] != ""].shape[0] == 0:
        print("No warnings")
    if df[df["warning_msg"] != ""].shape[0] > 0:
        print(df[df["warning_msg"] != ""].head())

    # Unique info
    quicktime = df.get("QuickTime:Model", pd.Series(np.nan, index=df.index))
    exif = df.get("EXIF:Model", pd.Series(np.nan, index=df.index))

    df["manufacturer"] = np.where(
        quicktime.notnull() & (quicktime != ""),
        quicktime,
        np.where(exif.notnull() & (exif != ""), exif, "Unknown"),
    )

    print(df.groupby("manufacturer")["info_msg"].value_counts(dropna=False))

    # Check consistency of files in directories
    # Photo
    df_photo = df[
        df["File:FileTypeExtension"].str.lower().isin(["arw", "dng", "nef", "x3f"])
    ]
    print(df_photo.shape)

    columns_to_check = [
        "File:FileTypeExtension",
        "EXIF:Make",
        "EXIF:Model",
        "EXIF:ImageWidth",
        "EXIF:ImageHeight",
    ]
    check_column_consistency(
        df=df_photo, columns_to_check=columns_to_check, column_pairs=None
    )

    # Video
    df_video = df[df["File:FileTypeExtension"].str.lower().isin(["avi", "mov", "mp4"])]
    print(df_video.shape)

    columns_to_check = [
        "File:FileTypeExtension",
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
        "QuickTime:GoPro_EXPX",
        "QuickTime:GoPro_EXPN",
        "QuickTime:GoPro_EXPQ",
        "QuickTime:Encoder",
    ]
    check_column_consistency(df_video, columns_to_check)

    # Number of files to rename
    n_rename = df[df["File:FileName"] != df["filename_new"]].shape[0]
    if n_rename == 0:
        print("No files to rename")
    elif n_rename > 0:
        print(f"{n_rename} files to rename")

    # Rename
    assert df[df["warning_msg"] != ""].shape[0] == 0
    if not confirm(f"Rename {n_rename} file(s)?"):
        print("Aborted; no files renamed.")
        return
    df_result = renamer.run(
        dir_st=dir_st, dir_ft=dir_ft, path_csv=path_csv, rename=True
    )
    print(df_result.shape)
    print(df_result[["File:FileName", "filename_new"]].head())
    print(df_result[["File:FileName", "filename_new"]].tail())


if __name__ == "__main__":
    main()
