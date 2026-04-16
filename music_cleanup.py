#!/usr/bin/env python3
"""Clean up music library filenames and fetch missing album artwork.

Two independent features, each behind its own flag:

  --name_cleanup        Strip '(Explicit)' tags and leading artist-name prefixes
                        from song filenames and album directory names in both
                        the source (-s) and destination (-d) trees.

  --artwork_fetch_only  Fetch missing cover art (Folder.jpg / Folder.png) into
                        source album directories from Wikipedia.  No audio
                        conversion or MP3 embedding is performed.
"""

import argparse
import os
import re
import sys
import warnings
from pathlib import Path

# Suppress urllib3's LibreSSL warning on older macOS versions
warnings.filterwarnings("ignore", message=".*LibreSSL.*")

# Shared utilities live in convert.py (also used during conversion)
from convert import log, detect_source_level, find_albums, ensure_cover


# ---------------------------------------------------------------------------
# Name-cleanup helpers
# ---------------------------------------------------------------------------

def strip_explicit(name):
    """Remove '(Explicit)' or any partial tag starting with '(E' (case-insensitive)
    and tidy up leftover whitespace."""
    return re.sub(r'\s*\(E[^).]*\)?\s*', '', name, flags=re.IGNORECASE).strip()


def rename_explicit(path, verbosity=0, dry_run=False):
    """Rename a file or directory by stripping '(Explicit)' from its name.

    Returns the (possibly renamed) Path.
    """
    cleaned_name = strip_explicit(path.name)
    if cleaned_name == path.name:
        return path

    new_path = path.parent / cleaned_name
    if dry_run:
        print(f"[RENAME]  {path.name!r} -> {cleaned_name!r}")
        return new_path
    log(f"[RENAME]  {path.name!r} -> {cleaned_name!r}", verbosity)
    try:
        os.rename(path, new_path)
    except OSError as e:
        print(f"[ERROR]   Could not rename {path.name!r}: {e}", file=sys.stderr)
    return new_path


def strip_artist_prefix(filename, artist):
    """Remove a leading artist name and separator from a song filename.

    Matches patterns like:
      'Artist Name - 01 Song Title.flac'
      'Artist Name – 01 Song Title.mp3'
    The match is case-insensitive and allows optional whitespace around the separator.
    """
    pattern = rf'^{re.escape(artist)}\s*[-–]\s*'
    return re.sub(pattern, '', filename, count=1, flags=re.IGNORECASE)


def rename_artist_prefix(path, artist, verbosity=0, dry_run=False):
    """Rename a song file by stripping a leading artist name prefix if present."""
    cleaned_name = strip_artist_prefix(path.name, artist)
    if cleaned_name == path.name:
        return path

    new_path = path.parent / cleaned_name
    if dry_run:
        print(f"[RENAME]  {path.name!r} -> {cleaned_name!r}")
        return new_path
    log(f"[RENAME]  {path.name!r} -> {cleaned_name!r}", verbosity)
    try:
        os.rename(path, new_path)
    except OSError as e:
        print(f"[ERROR]   Could not rename {path.name!r}: {e}", file=sys.stderr)
    return new_path


def _clean_songs_in_dir(album_dir, artist, verbosity, dry_run):
    """Rename song files inside album_dir: strip artist prefix then (Explicit) tag."""
    for song in sorted(album_dir.iterdir()):
        if song.suffix.lower() in ('.flac', '.mp3'):
            song = rename_artist_prefix(song, artist, verbosity=verbosity, dry_run=dry_run)
            rename_explicit(song, verbosity=verbosity, dry_run=dry_run)


def clean_explicit_names(source_dir, dest_dir, verbosity=0, dry_run=False):
    """Rename album directories and song files containing '(Explicit)', scoped to
    the detected source level.  The destination is always treated as top-level but
    only the artist/album subtrees that correspond to the source scope are touched.
    Also strips leading artist-name prefixes from song filenames."""
    if dry_run:
        print("[DRY RUN] No files will be renamed.")

    source = Path(source_dir)
    level  = detect_source_level(source_dir)
    albums = list(find_albums(source_dir))

    # --- Clean source ---
    for artist, album, album_dir in albums:
        _clean_songs_in_dir(album_dir, artist, verbosity, dry_run)
        rename_explicit(album_dir, verbosity=verbosity, dry_run=dry_run)

    # Rename artist dirs in source (only when visible at this source level)
    if level == "top":
        for artist_dir in sorted(source.iterdir()):
            if artist_dir.is_dir():
                rename_explicit(artist_dir, verbosity=verbosity, dry_run=dry_run)
    elif level == "artist":
        rename_explicit(source, verbosity=verbosity, dry_run=dry_run)
    # album level: artist dir is source's parent — leave it untouched

    # --- Clean dest (always top-level; scoped to affected artists/albums) ---
    dest = Path(dest_dir)
    if not dest.exists():
        return

    artists_affected = {artist for artist, _, _ in albums}

    for artist_dir in sorted(dest.iterdir()):
        if not artist_dir.is_dir():
            continue
        if level != "top" and artist_dir.name not in artists_affected:
            continue
        artist = artist_dir.name
        for album_dir in sorted(artist_dir.iterdir()):
            if not album_dir.is_dir():
                continue
            if level == "album" and album_dir.name not in {alb for _, alb, _ in albums}:
                continue
            _clean_songs_in_dir(album_dir, artist, verbosity, dry_run)
            rename_explicit(album_dir, verbosity=verbosity, dry_run=dry_run)
        rename_explicit(artist_dir, verbosity=verbosity, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Artwork fetch
# ---------------------------------------------------------------------------

def artwork_fetch_only(source_dir, verbosity=0, dry_run=False):
    """Fetch missing cover art into source album directories only.

    Does not convert any audio or embed images in MP3 files.
    """
    if dry_run:
        print("[DRY RUN] No cover art will be fetched.")

    for artist, album, album_dir in find_albums(source_dir):
        ensure_cover(album_dir, artist, album, verbosity=verbosity, dry_run=dry_run)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="music_cleanup",
        description="Clean up music library filenames and fetch missing album artwork.",
    )
    parser.add_argument("-s", required=True, metavar="SOURCE",
                        help="Source directory — auto-detected as top-level, artist-level, or album-level")
    parser.add_argument("-d", metavar="DEST",
                        help="Destination directory (required for --name_cleanup)")
    parser.add_argument("-v", dest="verbosity", action="count", default=0,
                        help="Verbose output (-v: progress messages; -vv: also show skipped files)")
    parser.add_argument("--verbose", dest="verbosity", action="store_const", const=1,
                        help="Verbose output — progress messages, no skip messages")
    parser.add_argument("--very_verbose", dest="verbosity", action="store_const", const=2,
                        help="Very verbose — all messages including skipped files")
    parser.add_argument("-n", action="store_true", dest="dry_run",
                        help="Dry run — show what would be done without taking action")
    parser.add_argument("--name_cleanup", action="store_true",
                        help="Remove '(Explicit)' tags and artist-name prefixes from filenames "
                             "and directory names in both source and destination")
    parser.add_argument("--artwork_fetch_only", action="store_true",
                        help="Fetch missing cover art into source directories only — "
                             "no conversion or MP3 embedding")

    args = parser.parse_args()

    if not os.path.isdir(args.s):
        parser.error(f"Source directory does not exist: {args.s}")

    if not args.name_cleanup and not args.artwork_fetch_only:
        parser.error("Specify at least one of --name_cleanup or --artwork_fetch_only")

    if args.name_cleanup and not args.d:
        parser.error("--name_cleanup requires a destination directory (-d)")

    if args.name_cleanup:
        clean_explicit_names(args.s, args.d, verbosity=args.verbosity, dry_run=args.dry_run)

    if args.artwork_fetch_only:
        artwork_fetch_only(args.s, verbosity=args.verbosity, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
