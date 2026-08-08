# yt-roundtrip

Re-compress a folder of videos by round-tripping them through YouTube.

YouTube's encoder ladder is very good at shrinking large files. This script automates the
manual chore of *upload → wait → download it back*: it walks a folder, uploads every video
to **your own** channel as unlisted, waits until YouTube has finished transcoding a
rendition matching the source resolution, downloads that rendition, deletes the video from
YouTube, and writes the result into an output folder **with the original folder structure
and file names preserved**.

```
D:\videos\                         D:\videos_out\
├── 2024\trip\clip.mp4     ──>     ├── 2024\trip\clip.mp4
└── raw\a\b\big.mp4                └── raw\a\b\big.mp4
```

---

## Read this before you start

This is not a fast or unlimited tool, and the trade-offs are real:

| | |
|---|---|
| **~6 uploads per day** | The YouTube Data API gives you 10,000 quota units/day; each upload costs 1,600. The script detects quota exhaustion, stops, and resumes on the next run. A 100-file library takes about two and a half weeks. |
| **15-minute cap** | Uploads are limited to 15 minutes per video until your channel is phone-verified. |
| **Audio gets worse** | You get back ~128 kbps AAC/Opus no matter what went in. Lossless or high-bitrate audio does not survive. |
| **It's a lossy re-encode** | Fine for delivery copies. Don't do this to masters, and don't run it twice on the same file. |
| **Big files are slow** | 1440p/2160p transcodes on YouTube's side can take hours. |

**If you just want smaller files**, encoding locally gets you most of the way with none of the
above:

```bash
ffmpeg -i in.mp4 -c:v libsvtav1 -crf 35 -preset 6 -c:a libopus -b:a 128k out.mp4
```

Use this project when you specifically want *YouTube's* encoding ladder.

> Automated downloading from YouTube is against YouTube's Terms of Service. This tool is
> written for round-tripping videos you uploaded yourself, on your own channel, and deletes
> them afterwards. Use it accordingly.

---

## Requirements

- **Python 3.9+**
- **ffmpeg and ffprobe** on your `PATH`
- A Google account with a YouTube channel

### Install

```bash
git clone https://github.com/kpasupa/mp4-to-youtube-encoding.git
cd mp4-to-youtube-encoding
pip install -r requirements.txt
```

ffmpeg:

```powershell
winget install Gyan.FFmpeg          # Windows
```
```bash
brew install ffmpeg                 # macOS
sudo apt install ffmpeg             # Debian/Ubuntu
```

Confirm it worked — this must print a version, not "not recognized":

```bash
ffprobe -version
```

---

## Setting up YouTube API access

Every user needs their **own** credentials; there is nothing shared in this repo. Takes
about five minutes, and it's free.

Do all of this signed in with **the Google account that owns the channel you want to upload
to**.

### 1. Create a project

Go to [console.cloud.google.com](https://console.cloud.google.com/) → project dropdown in
the top bar → **New Project** → name it anything → **Create**. Make sure it's selected in
the dropdown afterwards.

### 2. Enable the API

Search bar → **YouTube Data API v3** → open it → **Enable**.

### 3. Configure the consent screen

Left nav → **APIs & Services → OAuth consent screen** (newer consoles call this **Google
Auth Platform**).

- **Audience / User type:** External → Create
- **App name**, **user support email**, **developer contact email** — your own details
- **Scopes:** skip, the script requests them itself
- **Test users:** click **Add users** and add your own Google address

> ⚠️ If you skip the test-user step, every login fails with `access_denied`.

### 4. Create the OAuth client

Left nav → **Credentials** → **Create Credentials** → **OAuth client ID**.

- **Application type: Desktop app** — must be this one. Not "Web application"; the script
  uses a local-loopback flow.
- **Create** → **Download JSON**

Drop that file into the project folder. **You don't need to rename it** — the script picks
up any `client_secret*.json` sitting next to it. (`--client-secrets PATH` overrides, and is
what you want if you keep several.)

`.gitignore` already excludes `client_secret*.json` and `token.json`, so your credentials
stay out of git.

### 5. First run

```bash
python yt_roundtrip.py "D:\videos" "D:\videos_out" --dry-run
```

A browser opens. You'll see **"Google hasn't verified this app"** — expected for a personal
app. Click **Advanced → Go to <app name> (unsafe)**, choose your account, then **Allow** on
both permission checkboxes.

A `token.json` is cached beside the script; you won't be prompted again.

### 6. Publish the app (recommended)

While the consent screen sits in *Testing*, Google expires your refresh token after **7
days** and you'll start getting `invalid_grant`. Since a real run spans weeks, go back to
the consent screen and hit **Publish app** / set it to *In production*.

This does **not** require Google verification while you're the only user — you'll just keep
seeing the "unverified" warning at login. Alternatively, delete `token.json` and
re-authenticate whenever it expires.

---

## Usage

```bash
python yt_roundtrip.py SOURCE OUTPUT [options]
```

```bash
# see what would happen, no uploads
python yt_roundtrip.py ~/videos ~/videos_out --dry-run

# normal run
python yt_roundtrip.py ~/videos ~/videos_out

# smallest possible files, keep the uploads on YouTube
python yt_roundtrip.py ~/videos ~/videos_out --codec av1 --keep-remote
```

### Options

| Flag | Default | What it does |
|---|---|---|
| `--ext` | `.mp4` | Comma-separated extensions to pick up, e.g. `.mp4,.mov,.mkv` |
| `--privacy` | `unlisted` | `unlisted` / `private` / `public`. See the note below. |
| `--codec` | `any` | `av1`, `vp9`, `h264` — preferred codec of the copy you get back |
| `--keep-remote` | off | Don't delete the video from YouTube after downloading it |
| `--client-secrets` | auto | Path to the OAuth JSON |
| `--token` | `token.json` | Where the cached login is stored |
| `--cookies` | — | `cookies.txt` file, needed for `--privacy private` |
| `--cookies-from-browser` | — | `chrome`, `firefox`, `edge`, … same purpose |
| `--wait-timeout` | `7200` | Max seconds to wait for one video to finish transcoding |
| `--poll-interval` | `60` | Seconds between checks |
| `--dry-run` | off | List the queue and its state, then exit |

**Why unlisted and not private?** Private videos can't be read back without an
authenticated session, so `--privacy private` also requires `--cookies-from-browser chrome`
(or `--cookies`). Unlisted videos aren't searchable or listed on your channel, are only
reachable via the exact URL, and get deleted as soon as the download succeeds — so unlisted
is the default.

---

## How it works

**Phase 1 — upload everything.** Uploading the whole queue first means YouTube transcodes
your videos in parallel while you wait, instead of one at a time.

**Phase 2 — wait, then download.** For each video, the script polls until a rendition at
least as tall as the target appears, downloads it, remuxes to `.mp4`, saves it to the
mirrored path, and deletes the upload.

Source resolution is read with `ffprobe` (rotation-aware) and mapped to the nearest
standard YouTube rung at or below it — 144/240/360/480/720/1080/1440/2160/4320. A 1080p
source therefore waits for the 1080p rendition; a 1000×562 source targets 480p.

### Resuming

Progress is written to `OUTPUT/.yt_roundtrip_state.json`, keyed by path relative to
`SOURCE`. Rerun the same command any time:

- finished files are skipped
- already-uploaded files jump straight to the download phase
- files not yet uploaded are attempted until the daily quota runs out

This is the intended workflow, not an error path — you *will* hit the quota, and you're
meant to just run it again the next day.

---

## Troubleshooting

**`access_denied` at login** — your Google address isn't in the consent screen's **Test
users** list.

**`invalid_grant` after about a week** — Testing-mode token expiry. Publish the app (step
6) or delete `token.json` and log in again.

**`quotaExceeded`** — expected after ~6 uploads. Rerun tomorrow; nothing is lost.

**`uploadLimitExceeded`** — YouTube's own per-channel daily upload cap, separate from API
quota. Same fix: wait.

**`ffprobe failed, skipping`** — ffmpeg isn't installed or isn't on `PATH`, or the file is
corrupt.

**Timed out before Npp appeared** — YouTube is still transcoding. Rerun and it picks up
where it left off, or raise `--wait-timeout`.

**Uploads rejected over 15 minutes** — verify your channel's phone number at
[youtube.com/verify](https://www.youtube.com/verify).
