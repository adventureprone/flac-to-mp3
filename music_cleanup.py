#!/usr/bin/env python3
"""Clean up music library filenames, fetch artwork, and fix track metadata.

Flags:

  --name_cleanup        Strip '(Explicit)' tags and leading artist-name prefixes
                        from song filenames and album directory names in both
                        the source (-s) and destination (-d) trees.

  --artwork_fetch_only  Fetch missing cover art (Folder.jpg / Folder.png) into
                        source album directories from Wikipedia.  No audio
                        conversion or MP3 embedding is performed.

  --title           Fetch the track listing for each album from Wikipedia
                        and compare against the title tag in each source FLAC.
                        For each album with differences, show what would change
                        and ask whether to apply the updates (y/n/q).
                        With -n (dry run) only the differences are shown.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path

# Suppress urllib3's LibreSSL warning on older macOS versions
warnings.filterwarnings("ignore", message=".*LibreSSL.*")

import requests

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
# Track-name helpers
# ---------------------------------------------------------------------------

_WIKI_API   = "https://en.wikipedia.org/w/api.php"
_WIKI_HDRS  = {"User-Agent": "flac-to-mp3-converter/1.0 (music library tool)"}


def _find_track_listing_blocks(content):
    """Return a list of raw inner strings from every {{Track listing …}} template
    in *content*.  Uses bracket-depth counting so nested {{ }} inside a title
    field don't confuse the parser.
    """
    blocks = []
    lower  = content.lower()
    pos    = 0
    while True:
        idx = lower.find("{{track listing", pos)
        if idx == -1:
            break
        depth = 0
        i     = idx
        while i < len(content) - 1:
            if content[i : i + 2] == "{{":
                depth += 1
                i += 2
            elif content[i : i + 2] == "}}":
                depth -= 1
                if depth == 0:
                    # inner content sits between the opening {{ and closing }}
                    blocks.append(content[idx + 2 : i])
                    pos = i + 2
                    break
                i += 2
            else:
                i += 1
        else:
            break   # ran off the end without closing — give up
    return blocks


def _clean_wikitext(text):
    """Strip common wikitext markup from a track title string."""
    # Remove <ref> footnotes
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<ref[^/]*/>",         "", text, flags=re.IGNORECASE)
    # Resolve [[Page|Display]] → Display  and  [[Page]] → Page
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", text)
    # Remove ''italic'' / '''bold''' markers
    text = re.sub(r"'{2,}", "", text)
    # Remove remaining {{template}} blocks (bonus-track notes, etc.)
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    # Decode common HTML entities
    for entity, char in (("&amp;", "&"), ("&quot;", '"'), ("&apos;", "'"),
                         ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    return text.strip()


def _title_similarity(a, b):
    """Jaccard similarity of word sets (0.0–1.0)."""
    a_words = set(re.sub(r'[^a-z0-9]', ' ', a.lower()).split())
    b_words = set(re.sub(r'[^a-z0-9]', ' ', b.lower()).split())
    if not a_words and not b_words:
        return 1.0
    if not a_words or not b_words:
        return 0.0
    return len(a_words & b_words) / len(a_words | b_words)


def _parse_numbered_list(text):
    """Parse plain ``#`` or ``*`` quoted-title list items into a ``{(1, n): title}`` dict."""
    tracks  = {}
    counter = 0
    for line in text.splitlines():
        m = (re.match(r'^[#*]\s*"([^"]+)"', line) or
             re.match(r"^[#*]\s*'([^']+)'", line))
        if m:
            title = _clean_wikitext(m.group(1)).strip()
            if title:
                counter += 1
                tracks[(1, counter)] = title
    return tracks


def _combine_track_blocks(blocks):
    """Merge a list of ``{{Track listing}}`` block strings with Side A/B offset logic.

    Returns a ``{(disc, track): title}`` dict.
    """
    tracks           = {}
    disc_track_count = {}

    for block in blocks:
        disc_m = re.search(r"\|\s*(?:disc|cd)\s*=\s*(\d+)", block, re.IGNORECASE)
        disc   = int(disc_m.group(1)) if disc_m else 1

        block_tracks = {}
        for m in re.finditer(
            r"\|\s*title(\d+)\s*=\s*((?:\[\[[^\]]*\]\]|[^\|\}\n])+)", block
        ):
            title = _clean_wikitext(m.group(2))
            if title:
                block_tracks[int(m.group(1))] = title

        if not block_tracks:
            continue

        # If this block restarts numbering at/below the current count for this disc
        # (e.g. Side B starting at title1 again), offset so tracks don't overwrite.
        current = disc_track_count.get(disc, 0)
        offset  = current if min(block_tracks) <= current else 0

        for n, t in block_tracks.items():
            tracks[(disc, n + offset)] = t

        disc_track_count[disc] = offset + max(block_tracks)

    return tracks


def _parse_all_versions(content):
    """Return all distinct track-listing versions found on a Wikipedia page.

    Splits the ``==Track listing==`` section at ``===subsection===`` headers so
    that each edition (original, reissue, deluxe, etc.) is parsed independently.
    Returns a list of ``{(disc, track): title}`` dicts — one per edition found.
    An empty list means no track listing could be extracted.
    """
    sec_m = re.search(r"==\s*Track\s*listing\s*==", content, re.IGNORECASE)
    if not sec_m:
        return []

    # Grab the Track listing section (stop at the next top-level == section)
    rest   = content[sec_m.end():]
    next_m = re.search(r"\n==(?!=)", rest)
    section = rest[: next_m.start()] if next_m else rest

    # Split at ===..=== sub-headers so each edition is isolated
    parts = re.split(r"===.*?===", section, flags=re.DOTALL)

    versions = []
    for part in parts:
        blocks = _find_track_listing_blocks(part)
        if blocks:
            tracks = _combine_track_blocks(blocks)
            if tracks:
                versions.append(tracks)
        else:
            tracks = _parse_numbered_list(part)
            if tracks:
                versions.append(tracks)

    return versions


def _best_matching_version(versions, local_tracks):
    """Return the version whose tracks best match the local FLAC track titles.

    Scoring: ``avg_similarity × coverage``, where *coverage* is the fraction of
    local tracks that appear (by disc/track number) in this version.
    If there is only one version, it is returned immediately without scoring.
    """
    if len(versions) == 1:
        return versions[0]

    best       = versions[0]
    best_score = -1.0

    for version in versions:
        matched = [(local_tracks[k], version[k]) for k in local_tracks if k in version]
        if not matched:
            continue
        avg_sim  = sum(_title_similarity(a, b) for a, b in matched) / len(matched)
        coverage = len(matched) / len(local_tracks)
        score    = avg_sim * coverage
        if score > best_score:
            best_score = score
            best       = version

    return best


def fetch_wikipedia_tracklist(artist, album, verbosity=0):
    """Search Wikipedia for *artist – album* and return all track-listing versions.

    Returns a list of ``{(disc, track): title}`` dicts — one per edition found on
    the Wikipedia page (original, reissue, deluxe, etc.).  Returns an empty list
    if the album page or any track listing could not be found.
    """
    try:
        # Build an ordered list of candidate page titles.
        # The disambiguated form "{album} ({artist} album)" is tried first
        # because a plain search often returns the song page instead of the
        # album page (e.g. "Let's Stay Together" song vs. album).
        candidates = [f"{album} ({artist} album)"]

        search_resp = requests.get(_WIKI_API, headers=_WIKI_HDRS, params={
            "action": "query", "list": "search",
            "srsearch": f"{artist} {album} album",
            "format": "json", "srlimit": 5,
        }, timeout=10)
        search_resp.raise_for_status()
        for r in search_resp.json().get("query", {}).get("search", []):
            if r["title"] not in candidates:
                candidates.append(r["title"])

        for page_title in candidates:
            log(f"[WIKI]    Trying page: '{page_title}'", verbosity)

            # Fetch wikitext (formatversion=2 gives pages as a list)
            rev = requests.get(_WIKI_API, headers=_WIKI_HDRS, params={
                "action": "query", "titles": page_title,
                "prop": "revisions", "rvprop": "content",
                "rvslots": "main", "format": "json", "formatversion": "2",
            }, timeout=15)
            rev.raise_for_status()
            pages = rev.json().get("query", {}).get("pages", [])
            if not pages:
                continue
            page = pages[0]
            # Skip if the page doesn't exist on Wikipedia
            if page.get("missing"):
                continue
            content = (page.get("revisions") or [{}])[0] \
                          .get("slots", {}).get("main", {}).get("content", "")

            versions = _parse_all_versions(content)
            if versions:
                log(f"[WIKI]    Found {len(versions)} version(s) on '{page_title}'",
                    verbosity)
                return versions

        return []

    except requests.RequestException as e:
        print(f"[ERROR]   Wikipedia request failed: {e}", file=sys.stderr)
        return []


def _read_flac_tags(flac_path):
    """Return a dict of lowercase tag names → values for a FLAC file via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(flac_path)],
            capture_output=True, text=True, check=True,
        )
        raw = json.loads(result.stdout).get("format", {}).get("tags", {})
        return {k.lower(): v for k, v in raw.items()}
    except Exception:
        return {}


def _parse_int(s):
    """Parse the leading integer from strings like '1', '01', '1/12'. Returns None on failure."""
    m = re.match(r"(\d+)", str(s).strip()) if s else None
    return int(m.group(1)) if m else None


def _update_flac_title(flac_path, new_title, verbosity=0):
    """Rewrite the title tag in a FLAC file without re-encoding the audio."""
    tmp = flac_path.with_suffix(".tmp.flac")
    try:
        subprocess.run(
            ["ffmpeg",
             "-i", str(flac_path),
             "-map_metadata", "0",            # preserve all existing tags
             "-metadata", f"title={new_title}",  # override title only
             "-codec:a", "copy",              # no re-encode
             "-y", str(tmp)],
            check=True, capture_output=True,
        )
        tmp.replace(flac_path)
        log(f"[DONE]    {flac_path.name}  →  {new_title!r}", verbosity)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR]   Failed to update {flac_path.name}:\n{e.stderr.decode()}",
              file=sys.stderr)
        if tmp.exists():
            tmp.unlink()


def title_list(source_dir):
    """Print the album and track titles read from source FLAC metadata.

    No Wikipedia lookups are performed — this is a plain read of existing tags.
    """
    for artist, album, album_dir in find_albums(source_dir):
        flac_files = sorted(album_dir.glob("*.flac"))
        if not flac_files:
            continue

        print(f"\n{artist} — {album}")
        for flac_path in flac_files:
            tags  = _read_flac_tags(flac_path)
            title = tags.get("title", "(no title)")
            prefix_m = re.match(r"(\d+\s*[-–])", flac_path.stem)
            prefix   = (prefix_m.group(1) + " ") if prefix_m else (flac_path.stem + " ")
            print(f"  {prefix} {title!r}")


def title(source_dir, verbosity=0, dry_run=False):
    """Fetch Wikipedia track names for each album and offer to update FLAC title tags.

    Wikipedia pages often contain multiple editions (original release, reissue,
    deluxe, etc.).  This function picks the edition whose track names best match
    the local FLAC tags, then shows only the differences.  Suggestions whose
    Wikipedia title looks nothing like the current tag (Jaccard similarity < 0.3)
    are silently dropped to avoid noise when the wrong edition is partially matched.

    For each album with differences, the changes are displayed and the user is asked
    whether to apply them (y), skip the album (n), or quit entirely (q).
    With dry_run=True the prompt is skipped — differences are shown but nothing is written.
    """
    for artist, album, album_dir in find_albums(source_dir):
        flac_files = sorted(album_dir.glob("*.flac"))
        if not flac_files:
            continue

        log(f"[CHECK]   {artist} — {album}", verbosity)

        versions = fetch_wikipedia_tracklist(artist, album, verbosity=verbosity)
        if not versions:
            print(f"[SKIP]    No Wikipedia track listing for '{artist} — {album}'")
            continue

        # Build local track dict: {(disc, track): (flac_path, current_title)}
        local_tracks = {}
        for flac_path in flac_files:
            tags  = _read_flac_tags(flac_path)
            disc  = _parse_int(tags.get("discnumber")) or 1
            track = _parse_int(tags.get("tracknumber"))
            # Fall back to leading digits in the filename if the tag is absent
            if track is None:
                track = _parse_int(flac_path.stem)
            if track is None:
                continue
            local_tracks[(disc, track)] = (flac_path, tags.get("title", ""))

        if not local_tracks:
            continue

        # Pick the Wikipedia edition whose track names best match the local titles
        local_titles = {k: v[1] for k, v in local_tracks.items()}
        wiki = _best_matching_version(versions, local_titles)

        # Build list of differences; drop suggestions that look completely wrong
        differences = []
        for (disc, track), (flac_path, current_title) in sorted(local_tracks.items()):
            wiki_title = wiki.get((disc, track))
            if wiki_title is None:
                continue
            if current_title == wiki_title:
                continue
            if _title_similarity(current_title, wiki_title) < 0.3:
                log(f"[SKIP]    Low similarity for track {track}: "
                    f"{current_title!r} vs {wiki_title!r}", verbosity)
                continue
            differences.append((flac_path, current_title, wiki_title))

        if not differences:
            log(f"[OK]      All track names match for '{artist} — {album}'", verbosity)
            continue

        # Display the differences for this album
        print(f"\n{artist} — {album}")
        for flac_path, current, suggested in differences:
            # Extract leading track-number prefix, e.g. "01 -" from "01 - Call Me"
            prefix_m = re.match(r"(\d+\s*[-–])", flac_path.stem)
            prefix   = (prefix_m.group(1) + " ") if prefix_m else (flac_path.stem + " ")
            # "To:" is 3 chars; pad so the quoted value aligns with the line above
            to_pad   = " " * (len(prefix) - 2)
            print(f"{prefix} {current!r}")
            print(f"To:{to_pad}{suggested!r}")

        if dry_run:
            continue

        # Ask the user what to do
        while True:
            try:
                answer = input("\nUpdate track names for this album? [y/n/q]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(0)

            if answer == "q":
                sys.exit(0)
            elif answer == "y":
                for flac_path, _, new_title in differences:
                    _update_flac_title(flac_path, new_title, verbosity=verbosity)
                break
            elif answer == "n":
                break
            # Unrecognised input — re-prompt


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
    parser.add_argument("--title_list", action="store_true",
                        help="List album and track titles from source FLAC metadata — no Wikipedia lookups")
    parser.add_argument("--title", action="store_true",
                        help="Fetch track listings from Wikipedia and compare against "
                             "source FLAC title tags; prompt to apply differences per album")

    args = parser.parse_args()

    if not os.path.isdir(args.s):
        parser.error(f"Source directory does not exist: {args.s}")

    if not args.name_cleanup and not args.artwork_fetch_only and not args.title and not args.title_list:
        parser.error("Specify at least one of --name_cleanup, --artwork_fetch_only, --title, or --title_list")

    if args.name_cleanup and not args.d:
        parser.error("--name_cleanup requires a destination directory (-d)")

    if args.name_cleanup:
        clean_explicit_names(args.s, args.d, verbosity=args.verbosity, dry_run=args.dry_run)

    if args.artwork_fetch_only:
        artwork_fetch_only(args.s, verbosity=args.verbosity, dry_run=args.dry_run)

    if args.title_list:
        title_list(args.s)

    if args.title:
        title(args.s, verbosity=args.verbosity, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
