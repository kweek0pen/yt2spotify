#!/usr/bin/env python3
"""Convert a YouTube video tracklist (chapters / description timestamps) into a Spotify playlist."""
import argparse
import os
import re
import sys
from datetime import date
from difflib import SequenceMatcher

import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth
from yt_dlp import YoutubeDL

TIMESTAMP = re.compile(r"^\W*(?:\d{1,2}:)?\d{1,2}:\d{2}\W*")
TRAILING_TS = re.compile(r"\W*(?:\d{1,2}:)?\d{1,2}:\d{2}\W*$")
NUMBERING = re.compile(r"^\s*\d{1,3}\s*[.)\]]\s+")
NOISE = re.compile(
    r"\s*[\(\[](official|lyrics?|audio|video|music video|hd|hq|4k|visualizer|live)[^\)\]]*[\)\]]",
    re.I,
)
SEPARATORS = (" - ", " – ", " — ", " | ", " // ")


def fetch_video(url, comments=False):
    opts = {"quiet": True, "skip_download": True, "noplaylist": True}
    if comments:
        opts["getcomments"] = True
        # top-sorted, 200 top-level comments, no replies
        opts["extractor_args"] = {
            "youtube": {"comment_sort": ["top"], "max_comments": ["200,200,0,0"]}
        }
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def timestamped_lines(text):
    return [l for l in (text or "").splitlines() if TIMESTAMP.match(l) or TRAILING_TS.search(l)]


def best_comment_lines(comments):
    """Pick the comment that looks most like a tracklist."""
    best, best_score = [], 0
    for c in comments or []:
        if c.get("parent", "root") != "root":
            continue
        lines = timestamped_lines(c.get("text"))
        if len(lines) < 3:
            continue
        score = len(lines) + (5 if c.get("is_pinned") else 0) + (3 if c.get("author_is_uploader") else 0)
        if score > best_score:
            best, best_score = lines, score
    return best


def clean(line):
    line = TIMESTAMP.sub("", line)
    line = TRAILING_TS.sub("", line)
    line = NUMBERING.sub("", line)
    line = NOISE.sub("", line)
    return line.strip(" \t-–—|•*·")


def split_track(text):
    """Return (artist, title). Artist may be None if no separator found."""
    for sep in SEPARATORS:
        if sep in text:
            artist, title = text.split(sep, 1)
            return artist.strip(), title.strip()
    return None, text.strip()


def extract_tracks(info):
    chapters = info.get("chapters") or []
    if chapters:
        return parse_lines([c.get("title", "") for c in chapters])
    return parse_lines(timestamped_lines(info.get("description")))


def parse_lines(lines):
    tracks, seen = [], set()
    for raw in lines:
        text = clean(raw)
        if len(text) < 2:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        tracks.append(split_track(text))
    # if most lines are "Artist - Title", drop separator-less ones like "Intro"
    with_artist = [t for t in tracks if t[0]]
    if len(with_artist) * 2 >= len(tracks):
        tracks = with_artist
    return tracks


def norm(s):
    return re.sub(r"[\W_]", "", s.casefold())


def artist_hints(info):
    """Hashtags and tags often name the artists, e.g. '#sza #frankocean'."""
    words = re.findall(r"#(\w+)", info.get("description") or "") + (info.get("tags") or [])
    return {norm(w) for w in words if len(norm(w)) > 1}


def matches_hint(name, hints):
    # fuzzy: hashtags are often misspelled ("#danelceaser") or drop symbols ("A$AP" -> "asap")
    n = norm(name)
    return any(n == h or SequenceMatcher(None, n, h).ratio() >= 0.75 for h in hints)


def describe(item):
    return f'{item["artists"][0]["name"]} - {item["name"]}'


def find_uri(sp, artist, title, hints=()):
    """Return (uri, label, guessed). guessed=True when the match is just Spotify's top hit for a bare title."""
    if artist:
        queries = [
            f"track:{title} artist:{artist}",
            f"{artist} {title}",
            # some lists use "Title - Artist"
            f"track:{artist} artist:{title}",
        ]
        for q in queries:
            items = sp.search(q=q, type="track", limit=1)["tracks"]["items"]
            if items:
                return items[0]["uri"], describe(items[0]), False
        return None, None, False

    # title only: prefer a result whose artist matches a hint from the video
    first = None
    for q in (f"track:{title}", title):
        items = sp.search(q=q, type="track", limit=10)["tracks"]["items"]
        first = first or (items[0] if items else None)
        for item in items:
            if any(matches_hint(a["name"], hints) for a in item["artists"]):
                return item["uri"], describe(item), False
    if first:
        return first["uri"], describe(first), True
    return None, None, False


# Hangul fillers render blank but count as letters; zero-width chars are invisible too
INVISIBLE = re.compile("[ᅟᅠㅤﾠ​-‏⁠﻿]")


def playlist_title(title):
    """Video title with invisible padding removed, or a dated fallback if nothing visible remains."""
    name = " ".join(INVISIBLE.sub(" ", title or "").split())
    if not any(ch.isalnum() for ch in name):
        name = f"YouTube mix {date.today().isoformat()}"
    return name


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("url", help="YouTube video URL")
    p.add_argument("-n", "--name", help="Playlist name (default: video title)")
    p.add_argument("--public", action="store_true", help="Make playlist public (default private)")
    p.add_argument("--dry-run", action="store_true", help="Parse and match only; do not create playlist")
    p.add_argument("--comments", action="store_true", help="Read tracklist from comments even if description has one")
    args = p.parse_args()

    load_dotenv()
    for var in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"):
        if not os.environ.get(var):
            sys.exit(f"Missing env var {var}. See README.")

    print("Fetching video info...")
    info = fetch_video(args.url)
    tracks = [] if args.comments else extract_tracks(info)
    if len(tracks) < 3:
        print("No tracklist in description/chapters, checking comments...")
        comment_tracks = parse_lines(best_comment_lines(fetch_video(args.url, comments=True).get("comments")))
        if len(comment_tracks) > len(tracks):
            tracks = comment_tracks
    if not tracks:
        sys.exit("No tracklist found in chapters, description, or top comments.")
    print(f'"{info["title"]}": {len(tracks)} tracks parsed')

    sp = spotipy.Spotify(
        auth_manager=SpotifyOAuth(scope="playlist-modify-private playlist-modify-public")
    )

    hints = artist_hints(info)
    uris, missed, guessed = [], [], []
    for artist, title in tracks:
        uri, found, guess = find_uri(sp, artist, title, hints)
        label = f"{artist} - {title}" if artist else title
        if uri:
            if uri not in uris:
                uris.append(uri)
            if guess:
                guessed.append(f"{label}  ->  {found}")
            print(f"  {'?' if guess else '✓'} {label}  ->  {found}")
        else:
            missed.append(label)
            print(f"  ✗ {label}")

    print(f"\nMatched {len(uris)}/{len(tracks)}")
    if guessed:
        print(f"{len(guessed)} marked '?' are best guesses (title only, artist unknown) — check them.")
    if args.dry_run or not uris:
        return

    name = args.name or playlist_title(info.get("title"))
    playlist = sp.current_user_playlist_create(
        name,
        public=args.public,
        description=f'Imported from {info.get("webpage_url", args.url)}',
    )
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(playlist["id"], uris[i : i + 100])
    print(f'Created playlist "{name}": {playlist["external_urls"]["spotify"]}')
    if missed:
        print("Not found:\n  " + "\n  ".join(missed))


if __name__ == "__main__":
    main()
