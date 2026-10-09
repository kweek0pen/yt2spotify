# yt2spotify

Turn a YouTube mix video into a Spotify playlist on your account, using the video's Songs section and/or its tracklist (chapters, description timestamps, or a comment).

## Setup

1. Create an app at https://developer.spotify.com/dashboard. Add redirect URI `http://127.0.0.1:8888/callback`.
2. Copy `.env.example` to `.env` and fill in your app's client ID and secret (`.env` is gitignored):

```bash
cp .env.example .env
```

3. Install:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Use

```bash
.venv/bin/python yt2spotify.py "https://www.youtube.com/watch?v=VIDEO_ID" --dry-run   # preview matches
.venv/bin/python yt2spotify.py "https://www.youtube.com/watch?v=VIDEO_ID" -n "My Mix"  # create playlist
```

(Or run `source .venv/bin/activate` once per terminal, then plain `python` works.)

First run opens a browser for Spotify login; token is cached in `.cache`.

## Where songs come from

1. **Songs section** (the "Music" list YouTube shows under the description). Verified artist + title, so it always wins.
2. **Tracklist**: chapters, then timestamped description lines, then the top 200 comments (pinned/uploader comments preferred). `--comments` forces comment mode.

Tracklist lines naming a song from the Songs section are replaced by the verified entry. Songs-section songs missing from the tracklist are added at the end. `--songs-only` ignores tracklists entirely.

## Matching

- A Spotify result is only accepted if its **title matches** the requested one (ignoring things like `(feat. X)`, `(2020 Demo)`, `- Remastered`).
- Title-only lines (no artist) also need the artist to match a hint: hashtags/tags on the video (e.g. `#sza #frankocean`) or artists from the Songs section.
- Missed or unsure titles get a second search paired with each artist the video features (Songs section, artists already matched, hashtags), e.g. `Love` + `Kendrick Lamar` finds `LOVE.`. This fixes small typos and common titles, but not typos Spotify can't resolve.
- If the title matches but the artist still can't be confirmed, the track is **left out** and listed as unsure. `--include-guesses` adds those too.
- Duplicates (same song, even a different Spotify version) are skipped.
- Unmatched tracks are listed at the end.

## License

MIT. See [LICENSE](LICENSE).
