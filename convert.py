#!/usr/bin/env python3
"""Convert FLAC files to MP3, mirroring the source directory structure."""

import argparse
import os
import re
import shutil
import sys
import warnings
from pathlib import Path

# Suppress urllib3's LibreSSL warning on older macOS versions
warnings.filterwarnings("ignore", message=".*LibreSSL.*")

import requests
from pydub import AudioSegment
from pydub.exceptions import CouldntDecodeError


def log(message, verbose, force=False):
    if verbose or force:
        print(message)


def strip_explicit(name):
    """Remove '(Explicit)' or any partial tag starting with '(E' (case-insensitive)
    and tidy up leftover whitespace."""
    # Apply regex directly to the full name — avoids stem/suffix splitting bugs
    # with directory names that contain dots (e.g. 'Mr. Morale & The Big Steppers (Explicit)').
    # [^).] stops matching at a dot or closing paren so file extensions are preserved
    # even when the tag is truncated (e.g. 'Song (Expli.flac' -> 'Song.flac').
    return re.sub(r'\s*\(E[^).]*\)?\s*', '', name, flags=re.IGNORECASE).strip()


def rename_explicit(path, verbose=False, dry_run=False):
    """Rename a file or directory by stripping '(Explicit)' from its name.

    Returns the (possibly renamed) Path.
    """
    cleaned_name = strip_explicit(path.name)
    if cleaned_name == path.name:
        return path  # nothing to do

    new_path = path.parent / cleaned_name
    if dry_run:
        print(f"[RENAME]  {path.name!r} -> {cleaned_name!r}")
        return new_path  # return what it *would* be renamed to
    log(f"[RENAME]  {path.name!r} -> {cleaned_name!r}", verbose)
    try:
        os.rename(path, new_path)
    except OSError as e:
        print(f"[ERROR]   Could not rename {path.name!r}: {e}", file=sys.stderr)
    return new_path


def find_flac_files(source_dir):
    """Yield (artist, album, flac_path) tuples for all .flac files."""
    source = Path(source_dir)
    for artist_dir in sorted(source.iterdir()):
        if not artist_dir.is_dir():
            continue
        for album_dir in sorted(artist_dir.iterdir()):
            if not album_dir.is_dir():
                continue
            for flac_file in sorted(album_dir.glob("*.flac")):
                yield artist_dir.name, album_dir.name, flac_file


def build_dest_path(dest_dir, artist, album, flac_path):
    return Path(dest_dir) / artist / album / (flac_path.stem + ".mp3")


def clean_explicit_names(source_dir, dest_dir, verbose=False, dry_run=False):
    """Rename any album directories or song files containing '(Explicit)'
    in both the source and destination directory trees."""
    if dry_run:
        print("[DRY RUN] No files will be renamed.")

    for root_dir in [Path(source_dir), Path(dest_dir)]:
        if not root_dir.exists():
            continue
        for artist_dir in sorted(root_dir.iterdir()):
            if not artist_dir.is_dir():
                continue
            for album_dir in sorted(artist_dir.iterdir()):
                if not album_dir.is_dir():
                    continue
                # Rename songs first (before potentially renaming their parent album dir)
                for song in sorted(album_dir.iterdir()):
                    if song.suffix.lower() in ('.flac', '.mp3'):
                        rename_explicit(song, verbose=verbose, dry_run=dry_run)
                # Rename album directory
                rename_explicit(album_dir, verbose=verbose, dry_run=dry_run)
            # Rename artist directory last (after all children are handled)
            rename_explicit(artist_dir, verbose=verbose, dry_run=dry_run)


def fetch_wikipedia_cover(artist, album, dest_path, verbose=False):
    """Search Wikipedia for the album page and download its main image as Folder.jpg."""
    api_url = "https://en.wikipedia.org/w/api.php"
    # Wikipedia requires a descriptive User-Agent or it returns 403
    headers = {"User-Agent": "flac-to-mp3-converter/1.0 (music library tool)"}

    # Filenames containing these strings are never album art
    SKIP_KEYWORDS = ("logo", "icon", "flag", "map", "commons", "edit-clear")

    try:
        # Step 1: search for the album article
        search_resp = requests.get(
            api_url,
            headers=headers,
            params={
                "action": "query",
                "list": "search",
                "srsearch": f"{artist} {album} album",
                "format": "json",
                "srlimit": 1,
            },
            timeout=10,
        )
        search_resp.raise_for_status()
        results = search_resp.json().get("query", {}).get("search", [])
        if not results:
            print(f"[WARN]    No Wikipedia page found for '{artist} — {album}'", file=sys.stderr)
            return
        page_title = results[0]["title"]
        log(f"[WIKI]    Found page: '{page_title}'", verbose)

        # Step 2a: try pageimages (fast path — works for freely-licensed images)
        img_resp = requests.get(
            api_url,
            headers=headers,
            params={
                "action": "query",
                "titles": page_title,
                "prop": "pageimages|images",
                "piprop": "original",
                "imlimit": 10,
                "format": "json",
            },
            timeout=10,
        )
        img_resp.raise_for_status()
        pages = img_resp.json().get("query", {}).get("pages", {})
        page = next(iter(pages.values()))
        img_url = page.get("original", {}).get("source")

        # Step 2b: fall back to images list + imageinfo for non-free (copyrighted) covers
        if not img_url:
            images = page.get("images", [])
            for img in images:
                title = img["title"]
                lower = title.lower()
                # Only consider jpg/jpeg/png; skip known non-art files
                if not any(lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png")):
                    continue
                if any(kw in lower for kw in SKIP_KEYWORDS):
                    continue
                # Get the direct URL via imageinfo
                info_resp = requests.get(
                    api_url,
                    headers=headers,
                    params={
                        "action": "query",
                        "titles": title,
                        "prop": "imageinfo",
                        "iiprop": "url",
                        "format": "json",
                    },
                    timeout=10,
                )
                info_resp.raise_for_status()
                info_pages = info_resp.json().get("query", {}).get("pages", {})
                info_page = next(iter(info_pages.values()))
                imageinfo = info_page.get("imageinfo", [])
                if imageinfo:
                    img_url = imageinfo[0].get("url")
                    if img_url:
                        break

        if not img_url:
            print(f"[WARN]    No image found on Wikipedia for '{artist} — {album}'", file=sys.stderr)
            return
        log(f"[WIKI]    Downloading image: {img_url}", verbose)

        # Step 3: download and save
        dl_resp = requests.get(img_url, headers=headers, timeout=30)
        dl_resp.raise_for_status()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(dl_resp.content)
        log(f"[DONE]    Cover art saved: {dest_path}", verbose)

    except requests.RequestException as e:
        print(f"[ERROR]   Could not fetch cover art for '{artist} — {album}': {e}", file=sys.stderr)


def handle_cover_art(source_dir, dest_dir, verbose=False, dry_run=False):
    """Ensure each album in the destination has a Folder.jpg.
    Copies from source if present, otherwise fetches from Wikipedia."""
    source = Path(source_dir)
    dest = Path(dest_dir)

    for artist_dir in sorted(source.iterdir()):
        if not artist_dir.is_dir():
            continue
        for album_dir in sorted(artist_dir.iterdir()):
            if not album_dir.is_dir():
                continue

            artist = artist_dir.name
            album = album_dir.name
            src_cover = album_dir / "Folder.jpg"
            dest_cover = dest / artist / album / "Folder.jpg"

            if dest_cover.exists():
                log(f"[SKIP]    Cover art already exists: {artist} — {album}", verbose)
                continue

            if src_cover.exists():
                if dry_run:
                    print(f"[COPY]    Cover art: {src_cover} -> {dest_cover}")
                else:
                    log(f"[COPY]    Cover art: {artist} — {album}", verbose)
                    dest_cover.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_cover, dest_cover)
            else:
                if dry_run:
                    print(f"[MISSING] Cover art not found for '{artist} — {album}' (would fetch from Wikipedia into both source and destination)")
                else:
                    log(f"[FETCH]   Cover art missing for '{artist} — {album}', fetching from Wikipedia...", verbose)
                    fetch_wikipedia_cover(artist, album, dest_cover, verbose=verbose)
                    # Also copy into the source (flac) directory if the download succeeded
                    if dest_cover.exists():
                        shutil.copy2(dest_cover, src_cover)


def convert(source_dir, dest_dir, verbose=False, dry_run=False):
    flac_files = list(find_flac_files(source_dir))

    if not flac_files:
        print("No .flac files found in source directory.")
        return

    for artist, album, flac_path in flac_files:
        dest_path = build_dest_path(dest_dir, artist, album, flac_path)

        if dest_path.exists():
            log(f"[SKIP]    {flac_path.name} — destination already exists", verbose)
            continue

        if dry_run:
            print(f"[CONVERT] {flac_path} -> {dest_path}")
            continue

        log(f"[CONVERT] {flac_path.name} -> {dest_path}", verbose)

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            audio = AudioSegment.from_file(flac_path, format="flac")
            audio.export(dest_path, format="mp3")
            log(f"[DONE]    {dest_path.name}", verbose)
        except CouldntDecodeError as e:
            print(f"[ERROR]   Could not decode {flac_path}: {e}", file=sys.stderr)
        except FileNotFoundError:
            print(
                "[ERROR]   ffmpeg not found. Install it to use this tool:\n"
                "          macOS:  https://ffmpeg.org/download.html\n"
                "          or via a package manager: brew install ffmpeg",
                file=sys.stderr,
            )
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="convert",
        description="Convert FLAC files to MP3, preserving artist/album directory structure.",
    )
    parser.add_argument("-s", required=True, metavar="SOURCE", help="Source directory (artist/album/song structure)")
    parser.add_argument("-d", required=True, metavar="DEST", help="Destination directory")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output — explain each step")
    parser.add_argument("-n", action="store_true", dest="dry_run", help="Dry run — show what would be done without taking action")
    parser.add_argument("--name_cleanup", action="store_true", help="Remove '(Explicit)' from album directory and song file names in both source and destination")
    parser.add_argument("--cover_art", action="store_true", help="Ensure each album has a Folder.jpg — copies from source if present, otherwise fetches from Wikipedia")

    args = parser.parse_args()

    if not os.path.isdir(args.s):
        parser.error(f"Source directory does not exist: {args.s}")

    if args.dry_run:
        print("[DRY RUN] No files will be converted.")

    convert(args.s, args.d, verbose=args.verbose, dry_run=args.dry_run)
    if args.name_cleanup:
        clean_explicit_names(args.s, args.d, verbose=args.verbose, dry_run=args.dry_run)
    if args.cover_art:
        handle_cover_art(args.s, args.d, verbose=args.verbose, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
