#!/usr/bin/env python3
"""Convert a YouTube video's songs (Songs section, chapters, description or comments) into a Spotify playlist."""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date
from difflib import SequenceMatcher

import requests
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
# "love" vs "loveme" scores 0.80, so stay above that; typos like "Spacial"/"Special" score 0.86
MIN_TITLE_SIM = 0.85


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


def find_key(obj, key):
    """Yield every value stored under `key` anywhere in nested dicts/lists."""
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for v in obj.values():
            yield from find_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_key(v, key)


def fetch_songs_section(video_id):
    """Songs YouTube itself identified in the video (the "Music" / "Songs" section under the
    description). They come with verified artist names, so they're the most reliable source."""
    req = urllib.request.Request(
        f"https://www.youtube.com/watch?v={video_id}&hl=en",
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": "SOCS=CAI",  # skip the EU cookie-consent page
        },
    )
    data, error = None, None
    for attempt in range(3):  # YouTube sometimes resets the connection or serves a page without data
        try:
            html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
            m = re.search(r"ytInitialData\s*=\s*(\{.*?\});\s*</script>", html, re.S)
            if m:
                data = json.loads(m.group(1))
                break
            error = "page had no song data"
        except Exception as e:
            error = e
        time.sleep(2 * (attempt + 1))
    if data is None:
        print(f"  (couldn't read the Songs section: {error})")
        return []

    songs = []
    for card_list in find_key(data, "horizontalCardListRenderer"):
        header = card_list.get("header", {}).get("richListHeaderRenderer", {}).get("title", {})
        text = header.get("simpleText") or "".join(r.get("text", "") for r in header.get("runs", []))
        if text.strip().lower() not in ("music", "songs"):
            continue
        for card in card_list.get("cards", []):
            v = card.get("videoAttributeViewModel") or {}
            title, artist = (v.get("title") or "").strip(), (v.get("subtitle") or "").strip()
            if title and (artist or None, title) not in songs:
                songs.append((artist or None, title))
    return songs


def merge_tracks(songs, tracklist):
    """Return [(artist, title, from_songs_section)].

    Songs-section entries win: a tracklist line naming the same song is replaced by the
    verified entry (keeping tracklist order), and section songs the tracklist never
    mentions are appended at the end.
    """
    out, used = [], set()
    for artist, title in tracklist:
        j = next(
            (i for i, (_, t) in enumerate(songs)
             if i not in used and max(title_sim(t, title), title_sim(t, artist)) >= MIN_TITLE_SIM),
            None,
        )
        if j is None:
            out.append((artist, title, False))
        else:
            used.add(j)
            out.append((*songs[j], True))
    out += [(*s, True) for i, s in enumerate(songs) if i not in used]
    return out


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


def bare_title(s):
    """Title reduced for comparison: drops "(feat. X)", "[2020 Demo]", " - 2011 Remaster", "feat. X"."""
    out = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", s)
    out = re.sub(r"\s+[-–—]\s+.*$", "", out)
    out = re.sub(r"\s+(feat|ft|featuring)\b.*$", "", out, flags=re.I)
    return norm(out) or norm(s)


def title_sim(a, b):
    a, b = bare_title(a or ""), bare_title(b or "")
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


def same_artist(item, artist):
    """True if any of the track's artists matches `artist` (fuzzy, or one name contains the other)."""
    want = norm(artist)
    for a in item["artists"]:
        n = norm(a["name"])
        if n and want and (n in want or want in n or SequenceMatcher(None, n, want).ratio() >= 0.75):
            return True
    return False


def describe(item):
    return f'{item["artists"][0]["name"]} - {item["name"]}'


_search_cache = {}


def search(sp, q, limit=10):
    """Spotify track search, cached, retrying when Spotify's API is slow or drops the connection."""
    if (q, limit) not in _search_cache:
        for attempt in range(4):
            try:
                _search_cache[q, limit] = sp.search(q=q, type="track", limit=limit)["tracks"]["items"]
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
    return _search_cache[q, limit]


def find_track(sp, artist, title, hints):
    """Return (item, confidence). confidence is "sure", "guess" (title matches but artist unknown) or None.

    A result is only accepted when its title matches the requested title, so a hinted
    artist's *other* songs can no longer slip in.
    """
    if artist:
        for q, want_title, want_artist in (
            (f"track:{title} artist:{artist}", title, artist),
            (f"{artist} {title}", title, artist),
            # some lists use "Title - Artist"
            (f"track:{artist} artist:{title}", artist, title),
        ):
            for item in search(sp, q, limit=5):
                if title_sim(item["name"], want_title) >= MIN_TITLE_SIM and same_artist(item, want_artist):
                    return item, "sure"

    # title only (or the given artist didn't pan out): need a hinted artist to be sure
    best, guess = (0, None), None
    for q in (f"track:{title}", title):
        for item in search(sp, q):
            score = title_sim(item["name"], title)
            if score < MIN_TITLE_SIM:
                continue
            if any(matches_hint(a["name"], hints) for a in item["artists"]) and score > best[0]:
                best = (score, item)
            guess = guess or item
    if best[1]:
        return best[1], "sure"
    return guess, ("guess" if guess else None)


def find_with_artists(sp, title, names):
    """Retry a missed/unsure title paired with each artist the video is known to feature.
    Catches typos and common titles ("Spacial", "Love") whose right version isn't in the top hits."""
    best = (0, None)
    for name in names:
        for q in (f"{title} {name}", f"track:{title} artist:{name}"):
            for item in search(sp, q, limit=5):
                score = title_sim(item["name"], title)
                if score >= MIN_TITLE_SIM and same_artist(item, name) and score > best[0]:
                    best = (score, item)
                    if score == 1.0:
                        return item
    return best[1]


def ordered_unique(names):
    out, keys = [], set()
    for n in names:
        if n and norm(n) not in keys:
            keys.add(norm(n))
            out.append(n)
    return out


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
    p.add_argument("--songs-only", action="store_true", help="Use only the video's Songs section, ignore tracklists")
    p.add_argument("--include-guesses", action="store_true",
                   help="Also add unsure matches (right title, artist unconfirmed)")
    args = p.parse_args()

    load_dotenv()
    for var in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"):
        if not os.environ.get(var):
            sys.exit(f"Missing env var {var}. See README.")

    print("Fetching video info...")
    info = fetch_video(args.url)
    songs = fetch_songs_section(info["id"])
    print(f"  Songs section: {len(songs)} songs")

    tracklist = []
    if not args.songs_only:
        tracklist = [] if args.comments else extract_tracks(info)
        if len(tracklist) < 3:
            print("No tracklist in description/chapters, checking comments...")
            comment_tracks = parse_lines(best_comment_lines(fetch_video(args.url, comments=True).get("comments")))
            if len(comment_tracks) > len(tracklist):
                tracklist = comment_tracks
        print(f"  Tracklist: {len(tracklist)} lines")

    tracks = merge_tracks(songs, tracklist)
    if not tracks:
        sys.exit("No songs found in the Songs section, chapters, description, or top comments.")
    print(f"{len(tracks)} tracks to match\n")

    sp = spotipy.Spotify(
        auth_manager=SpotifyOAuth(scope="playlist-modify-private playlist-modify-public"),
        requests_timeout=20,  # default 5s times out on slow connections
        retries=3,
    )

    hints = artist_hints(info) | {norm(a) for a, _ in songs if a}
    results = [find_track(sp, artist, title, hints) for artist, title, _ in tracks]

    # second pass: retry missed/unsure titles with each artist the video features
    # (Songs section, artists confirmed above, then hashtags like #kendricklamar)
    names = ordered_unique(
        [a for a, _ in songs]
        + [a["name"] for item, conf in results if conf == "sure" for a in item["artists"]]
        + re.findall(r"#(\w+)", info.get("description") or "")
    )
    for i, (item, conf) in enumerate(results):
        if conf != "sure":
            retry = find_with_artists(sp, tracks[i][1], names)
            if retry:
                results[i] = (retry, "sure")

    uris, seen, missed, unsure, dupes = [], set(), [], [], []
    for (artist, title, from_songs), (item, confidence) in zip(tracks, results):
        label = (f"{artist} - {title}" if artist else title) + ("  [Songs]" if from_songs else "")
        if not item:
            missed.append(label)
            print(f"  ✗ {label}")
            continue
        found = describe(item)
        key = (norm(item["artists"][0]["name"]), bare_title(item["name"]))
        if item["uri"] in uris or key in seen:
            dupes.append(label)
            print(f"  = {label}  ->  {found}  (duplicate, skipped)")
            continue
        if confidence == "guess" and not args.include_guesses:
            unsure.append(f"{label}  ->  {found}?")
            print(f"  ? {label}  ->  {found}  (unsure, not added)")
            continue
        seen.add(key)
        uris.append(item["uri"])
        print(f"  {'?' if confidence == 'guess' else '✓'} {label}  ->  {found}")

    print(f"\nAdding {len(uris)}/{len(tracks)} tracks")
    if unsure:
        print(f"{len(unsure)} unsure match(es) left out (artist unconfirmed); add them with --include-guesses:")
        print("  " + "\n  ".join(unsure))
    if dupes:
        print(f"{len(dupes)} duplicate(s) skipped.")
    if missed:
        print("Not found on Spotify:\n  " + "\n  ".join(missed))
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


if __name__ == "__main__":
    main()
