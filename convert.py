#!/usr/bin/env python3
"""Convert FLAC files to MP3, mirroring the source directory structure."""

import argparse
import os
import re
import sys
import warnings
from pathlib import Path

# Suppress urllib3's LibreSSL warning on older macOS versions
warnings.filterwarnings("ignore", message=".*LibreSSL.*")

import subprocess

import requests
from PIL import Image


def log(message, verbosity, level=1):
    """Print message if verbosity >= level.

    level=1  shown with -v  (verbose — progress messages, no skips)
    level=2  shown with -vv (very verbose — everything including skips)
    """
    if verbosity >= level:
        print(message)


def detect_source_level(source_dir):
    """Detect whether source_dir is a top-level, artist-level, or album-level directory.

    Returns one of:
      'album'  — the directory directly contains .flac files
      'artist' — subdirectories contain .flac files
      'top'    — sub-subdirectories contain .flac files (default)
    """
    source = Path(source_dir)
    if any(source.glob("*.flac")):
        return "album"
    for subdir in source.iterdir():
        if subdir.is_dir() and any(subdir.glob("*.flac")):
            return "artist"
    return "top"


def find_albums(source_dir):
    """Yield (artist, album, album_dir) tuples, adapting to the detected source level.

    album level  → one tuple; artist = parent dir name, album = dir name
    artist level → one tuple per album subdir
    top level    → one tuple per artist/album pair (original behaviour)
    """
    source = Path(source_dir)
    level = detect_source_level(source_dir)

    if level == "album":
        yield source.parent.name, source.name, source
    elif level == "artist":
        for album_dir in sorted(source.iterdir()):
            if album_dir.is_dir():
                yield source.name, album_dir.name, album_dir
    else:  # top
        for artist_dir in sorted(source.iterdir()):
            if not artist_dir.is_dir():
                continue
            for album_dir in sorted(artist_dir.iterdir()):
                if album_dir.is_dir():
                    yield artist_dir.name, album_dir.name, album_dir


def find_flac_files(source_dir):
    """Yield (artist, album, flac_path) tuples for all .flac files."""
    for artist, album, album_dir in find_albums(source_dir):
        for flac_file in sorted(album_dir.glob("*.flac")):
            yield artist, album, flac_file


def build_dest_path(dest_dir, artist, album, flac_path):
    return Path(dest_dir) / artist / album / (flac_path.stem + ".mp3")


def fetch_wikipedia_cover(artist, album, album_dir, verbosity=0):
    """Search Wikipedia for the album page and download its cover art into album_dir.

    Saves as Folder.jpg or Folder.png depending on the image format found.
    Returns the saved Path on success, or None on failure.
    """
    api_url = "https://en.wikipedia.org/w/api.php"
    # Wikipedia requires a descriptive User-Agent or it returns 403
    headers = {"User-Agent": "flac-to-mp3-converter/1.0 (music library tool)"}

    # Supported raster formats for embedding in MP3
    SUPPORTED_EXTS = (".jpg", ".jpeg", ".png")
    # Filenames containing these strings are never album art
    SKIP_KEYWORDS  = ("logo", "icon", "flag", "map", "commons", "edit-clear")

    def find_image_on_page(page_title):
        """Try to find a usable cover image URL for a given Wikipedia page title.
        Returns an image URL string or None.

        Strategy:
          1. Parse the page wikitext to extract the cover filename directly from
             the {{Infobox album}} template — the most reliable source.
          2. Fall back to the images list sorted by filename similarity to the
             album title (substring match beats word overlap beats character count).
          3. Last resort: pageimages (free images only, no SVGs).
        """
        def imageinfo_url(file_title, render_width=None):
            """Return the direct URL for a File: title, or None.

            Pass render_width to request a server-side render (useful for SVGs —
            Wikimedia will return a PNG thumbnail URL instead of the raw SVG).
            """
            if not file_title.lower().startswith("file:"):
                file_title = f"File:{file_title}"
            params = {
                "action": "query", "titles": file_title,
                "prop": "imageinfo", "iiprop": "url", "format": "json",
            }
            if render_width:
                params["iiurlwidth"] = render_width
            r = requests.get(api_url, headers=headers, params=params, timeout=10)
            r.raise_for_status()
            info = next(iter(r.json()["query"]["pages"].values())).get("imageinfo", [])
            if not info:
                return None
            # iiurlwidth populates thumburl; fall back to url for normal images
            return info[0].get("thumburl") or info[0].get("url")

        img_url = None

        # --- Step 1: extract cover from infobox wikitext ---
        rev_resp = requests.get(api_url, headers=headers, params={
            "action": "query", "titles": page_title,
            "prop": "revisions", "rvprop": "content",
            "rvslots": "main", "format": "json", "formatversion": "2",
        }, timeout=15)
        rev_resp.raise_for_status()
        rev_pages = rev_resp.json().get("query", {}).get("pages", [])
        if rev_pages:
            content = (rev_pages[0].get("revisions") or [{}])[0] \
                          .get("slots", {}).get("main", {}).get("content", "")
            m = re.search(r'\|\s*(?:cover|image)\s*=\s*([^\|\}\n\[\]]+)', content, re.IGNORECASE)
            if m:
                cover_file = m.group(1).strip()
                if cover_file:
                    log(f"[WIKI]    Infobox cover: '{cover_file}'", verbosity)
                    if cover_file.lower().endswith(".svg"):
                        # Request a server-side PNG render of the SVG at 600px wide
                        log(f"[WIKI]    SVG cover — requesting PNG render", verbosity)
                        img_url = imageinfo_url(cover_file, render_width=600)
                    else:
                        img_url = imageinfo_url(cover_file)

        # --- Step 2: images list sorted by filename similarity ---
        if not img_url:
            img_resp = requests.get(api_url, headers=headers, params={
                "action": "query", "titles": page_title,
                "prop": "images", "imlimit": 20, "format": "json",
            }, timeout=10)
            img_resp.raise_for_status()
            page_data = next(iter(img_resp.json()["query"]["pages"].values()))

            def cover_score(file_title):
                stem = re.sub(r'[^a-z0-9]', '', Path(file_title).stem.lower())
                norm_album  = re.sub(r'[^a-z0-9]', '', album.lower())
                norm_artist = re.sub(r'[^a-z0-9]', '', artist.lower())
                if norm_album and norm_album in stem:
                    return 100
                if norm_artist and norm_artist in stem:
                    return 50
                kw = set(re.sub(r'[^a-z0-9]', ' ', f"{album} {artist}").lower().split())
                return len(set(re.sub(r'[^a-z0-9]', ' ', stem).split()) & kw)

            candidates = sorted([
                img["title"] for img in page_data.get("images", [])
                if any(img["title"].lower().endswith(ext) for ext in SUPPORTED_EXTS)
                and not any(kw in img["title"].lower() for kw in SKIP_KEYWORDS)
            ], key=cover_score, reverse=True)

            for title in candidates:
                img_url = imageinfo_url(title)
                if img_url:
                    break

        # --- Step 3: pageimages fallback (free images, no SVGs) ---
        if not img_url:
            pi_resp = requests.get(api_url, headers=headers, params={
                "action": "query", "titles": page_title,
                "prop": "pageimages", "piprop": "original", "format": "json",
            }, timeout=10)
            pi_resp.raise_for_status()
            pi_page = next(iter(pi_resp.json()["query"]["pages"].values()))
            pi_url  = pi_page.get("original", {}).get("source")
            if pi_url and Path(pi_url.split("?")[0]).suffix.lower() != ".svg":
                img_url = pi_url

        return img_url

    try:
        # Step 1: search for the album article — fetch top 3 candidates
        search_resp = requests.get(
            api_url,
            headers=headers,
            params={
                "action": "query",
                "list": "search",
                "srsearch": f"{artist} {album} album",
                "format": "json",
                "srlimit": 3,
            },
            timeout=10,
        )
        search_resp.raise_for_status()
        results = search_resp.json().get("query", {}).get("search", [])
        if not results:
            print(f"[WARN]    No Wikipedia page found for '{artist} — {album}'", file=sys.stderr)
            return None

        # Step 2: try each candidate until one yields a usable image
        img_url = None
        for result in results:
            page_title = result["title"]
            log(f"[WIKI]    Trying page: '{page_title}'", verbosity)
            img_url = find_image_on_page(page_title)
            if img_url:
                log(f"[WIKI]    Found image on page: '{page_title}'", verbosity)
                break

        if not img_url:
            print(f"[WARN]    No image found on Wikipedia for '{artist} — {album}'", file=sys.stderr)
            return None

        # Derive Folder filename from the URL's extension
        url_ext = Path(img_url.split("?")[0]).suffix.lower()
        folder_name = "Folder.png" if url_ext == ".png" else "Folder.jpg"
        dest_path = album_dir / folder_name

        log(f"[WIKI]    Downloading image: {img_url}", verbosity)

        # Step 3: download with retries on 429 (rate limit)
        import time
        dl_resp = None
        for attempt in range(4):
            dl_resp = requests.get(img_url, headers=headers, timeout=30)
            if dl_resp.status_code != 429:
                break
            wait = int(dl_resp.headers.get("Retry-After", 5)) * (attempt + 1)
            log(f"[WIKI]    Rate limited — waiting {wait}s before retry {attempt + 1}/3", verbosity)
            time.sleep(wait)
        dl_resp.raise_for_status()
        album_dir.mkdir(parents=True, exist_ok=True)

        # Scale down to 600x600 max if needed, preserving aspect ratio
        MAX_SIZE = 600
        import io
        img = Image.open(io.BytesIO(dl_resp.content))
        if img.width > MAX_SIZE or img.height > MAX_SIZE:
            original_size = f"{img.width}x{img.height}"
            img.thumbnail((MAX_SIZE, MAX_SIZE), Image.LANCZOS)
            log(f"[WIKI]    Scaled cover art from {original_size} to {img.width}x{img.height}", verbosity)
            img.save(dest_path)
        else:
            dest_path.write_bytes(dl_resp.content)

        log(f"[DONE]    Cover art saved: {dest_path}", verbosity)
        return dest_path

    except requests.RequestException as e:
        print(f"[ERROR]   Could not fetch cover art for '{artist} — {album}': {e}", file=sys.stderr)
        return None


def ensure_cover(album_dir, artist, album, verbosity=0, dry_run=False):
    """Return the path to cover art (Folder.jpg or Folder.png) in album_dir.

    Fetches from Wikipedia if neither file exists.
    In dry-run mode no fetch is performed — returns None if the file is absent.
    """
    for name in ("Folder.jpg", "folder.jpg", "Folder.png", "folder.png"):
        cover = album_dir / name
        if cover.exists():
            return cover
    if dry_run:
        print(f"[MISSING] No cover art for '{artist} — {album}' (would fetch from Wikipedia)")
        return None
    log(f"[FETCH]   No cover art for '{artist} — {album}', fetching from Wikipedia...", verbosity)
    return fetch_wikipedia_cover(artist, album, album_dir, verbosity=verbosity)


def artwork_update_only(source_dir, dest_dir, verbosity=0, dry_run=False):
    """Re-embed cover art into existing destination MP3s without re-encoding audio.

    Only processes MP3s that already exist in the destination — no conversion runs.
    Uses ffmpeg's -codec:a copy so only the ID3 picture frame is rewritten.
    """
    if dry_run:
        print("[DRY RUN] No files will be modified.")

    for artist, album, album_dir in find_albums(source_dir):
        # Only use artwork that already exists — do not fetch from Wikipedia
        cover = next(
            (album_dir / name for name in ("Folder.jpg", "folder.jpg", "Folder.png", "folder.png")
             if (album_dir / name).exists()),
            None,
        )
        if not cover:
            log(f"[SKIP]    No cover art found for '{artist} — {album}'", verbosity, level=2)
            continue

        dest_album = Path(dest_dir) / artist / album
        if not dest_album.exists():
            continue

        for mp3 in sorted(dest_album.glob("*.mp3")):
            if dry_run:
                print(f"[UPDATE]  Cover art: {mp3.name}")
                continue
            log(f"[UPDATE]  Cover art: {mp3.name}", verbosity)
            tmp = mp3.with_suffix(".tmp.mp3")
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-i", str(mp3),
                        "-i", str(cover),
                        "-map", "0:a",               # audio from existing MP3
                        "-map", "1:v",               # image from Folder.jpg/png
                        "-codec:a", "copy",          # copy audio — no re-encode
                        "-map_metadata", "0",        # preserve existing tags
                        "-metadata:s:v", "title=Album cover",
                        "-metadata:s:v", "comment=Cover (front)",
                        "-id3v2_version", "3",
                        "-y", str(tmp),
                    ],
                    check=True,
                    capture_output=True,
                )
                tmp.replace(mp3)
            except subprocess.CalledProcessError as e:
                print(f"[ERROR]   Failed to update cover art for {mp3.name}:\n{e.stderr.decode()}", file=sys.stderr)
                if tmp.exists():
                    tmp.unlink()


def metadata_update_only(source_dir, dest_dir, verbosity=0, dry_run=False):
    """Update tags and cover art in existing destination MP3s from source FLACs.

    Audio is not re-encoded.  Only destination files that already exist are
    processed — there is no conversion of new files.  The source directory is
    never modified.
    """
    if dry_run:
        print("[DRY RUN] No files will be modified.")

    for artist, album, flac_path in find_flac_files(source_dir):
        dest_path = build_dest_path(dest_dir, artist, album, flac_path)

        if not dest_path.exists():
            log(f"[SKIP]    {flac_path.name} — no destination file", verbosity, level=2)
            continue

        # Use existing cover art from the source album dir — do not fetch
        cover = next(
            (flac_path.parent / name
             for name in ("Folder.jpg", "folder.jpg", "Folder.png", "folder.png")
             if (flac_path.parent / name).exists()),
            None,
        )

        if dry_run:
            suffix = " (with cover art)" if cover else ""
            print(f"[UPDATE]  Metadata: {dest_path.name}{suffix}")
            continue

        log(f"[UPDATE]  Metadata: {dest_path.name}", verbosity)

        tmp = dest_path.with_suffix(".tmp.mp3")
        try:
            cmd = [
                "ffmpeg",
                "-i", str(dest_path),    # input 0 — existing MP3 (audio)
                "-i", str(flac_path),    # input 1 — source FLAC (metadata)
            ]
            if cover:
                cmd += ["-i", str(cover)]  # input 2 — cover image

            cmd += [
                "-map", "0:a",             # audio stream from existing MP3
                "-map_metadata", "1",      # all tags from source FLAC
            ]
            if cover:
                cmd += [
                    "-map", "2:v",
                    "-metadata:s:v", "title=Album cover",
                    "-metadata:s:v", "comment=Cover (front)",
                ]
            cmd += [
                "-codec:a", "copy",        # copy audio — no re-encode
                "-id3v2_version", "3",
                "-y", str(tmp),
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            tmp.replace(dest_path)
            log(f"[DONE]    {dest_path.name}{' (with cover art)' if cover else ''}", verbosity)
        except subprocess.CalledProcessError as e:
            print(f"[ERROR]   Failed to update metadata for {dest_path.name}:\n{e.stderr.decode()}", file=sys.stderr)
            if tmp.exists():
                tmp.unlink()


def convert(source_dir, dest_dir, verbosity=0, dry_run=False):
    flac_files = list(find_flac_files(source_dir))

    if not flac_files:
        print("No .flac files found in source directory.")
        return

    for artist, album, flac_path in flac_files:
        dest_path = build_dest_path(dest_dir, artist, album, flac_path)

        if dest_path.exists():
            log(f"[SKIP]    {flac_path.name} — destination already exists", verbosity, level=2)
            continue

        # Only use artwork already present in the source — do not write to source
        cover = next(
            (flac_path.parent / name
             for name in ("Folder.jpg", "folder.jpg", "Folder.png", "folder.png")
             if (flac_path.parent / name).exists()),
            None,
        )

        if dry_run:
            print(f"[CONVERT] {flac_path} -> {dest_path}")
            continue

        log(f"[CONVERT] {flac_path.name} -> {dest_path}", verbosity)

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        has_cover = cover is not None and cover.exists()

        try:
            cmd = ["ffmpeg", "-i", str(flac_path)]
            if has_cover:
                cmd += ["-i", str(cover)]        # second input: cover art
            cmd += ["-map_metadata", "0"]        # copy all metadata from FLAC
            cmd += ["-map", "0:a"]               # map audio stream
            if has_cover:
                cmd += [
                    "-map", "1:v",               # map cover image stream
                    "-metadata:s:v", "title=Album cover",
                    "-metadata:s:v", "comment=Cover (front)",
                ]
            cmd += [
                "-id3v2_version", "3",           # ID3v2.3 for widest compatibility
                "-q:a", "2",                     # VBR quality ~190 kbps
                "-y",                            # overwrite without prompting
                str(dest_path),
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            log(f"[DONE]    {dest_path.name}{' (with cover art)' if has_cover else ''}", verbosity)
        except subprocess.CalledProcessError as e:
            print(f"[ERROR]   ffmpeg failed for {flac_path.name}:\n{e.stderr.decode()}", file=sys.stderr)
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
    parser.add_argument("-s", required=True, metavar="SOURCE", help="Source directory — auto-detected as top-level (artist/album/song), artist-level (album/song), or album-level (song) structure")
    parser.add_argument("-d", required=True, metavar="DEST", help="Destination directory")
    parser.add_argument("-v", dest="verbosity", action="count", default=0,
                        help="Verbose output (-v: progress messages; -vv: also show skipped files)")
    parser.add_argument("--verbose", dest="verbosity", action="store_const", const=1,
                        help="Verbose output — progress messages, no skip messages")
    parser.add_argument("--very_verbose", dest="verbosity", action="store_const", const=2,
                        help="Very verbose — all messages including skipped files")
    parser.add_argument("-n", action="store_true", dest="dry_run", help="Dry run — show what would be done without taking action")
    parser.add_argument("-m", "--metadata_update_only", action="store_true", help="Update tags and cover art in existing destination MP3s from source FLACs — no conversion or re-encoding")
    parser.add_argument("--artwork_update_only", action="store_true", help="Re-embed cover art into existing destination MP3s without re-encoding audio — no conversion runs")

    args = parser.parse_args()

    if not os.path.isdir(args.s):
        parser.error(f"Source directory does not exist: {args.s}")

    if args.metadata_update_only:
        metadata_update_only(args.s, args.d, verbosity=args.verbosity, dry_run=args.dry_run)
        return

    if args.artwork_update_only:
        artwork_update_only(args.s, args.d, verbosity=args.verbosity, dry_run=args.dry_run)
        return

    if args.dry_run:
        print("[DRY RUN] No files will be converted.")

    convert(args.s, args.d, verbosity=args.verbosity, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
