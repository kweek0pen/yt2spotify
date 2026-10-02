# yt2spotify

Turn a YouTube video with a tracklist (chapters or timestamps in the description) into a Spotify playlist on your account.

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

## Notes

- Uses YouTube chapters first, then timestamped description lines. If neither gives 3+ tracks, scans the top 200 comments and picks the one with the most timestamped lines (pinned/uploader comments preferred). `--comments` forces comment mode.
- Expects `Artist - Title` per line. Title-only lines are matched using hashtags/tags on the video as artist hints (e.g. `#sza #frankocean`); if no hint matches, Spotify's top hit is used and marked `?` in the output.
- Unmatched tracks are listed at the end.

## License

MIT. See [LICENSE](LICENSE).
