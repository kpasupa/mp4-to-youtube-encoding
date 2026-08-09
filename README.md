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

## Which approach should you use?

Measured head-to-head against YouTube's own encoder, on 60-second slices scored with
VMAF (Netflix's perceptual quality metric, 0-100, where a gap under ~1 point is invisible):

**1080p source, 6.7 Mbps:**

| Encoder | 60s size | VMAF |
|---|---:|---:|
| YouTube | 19.9 MB | 92.35 |
| local x264 crf26 | 13.5 MB | 92.74 |
| local av1 crf38 | 11.4 MB | 93.55 |
| **local av1 crf42** | **8.8 MB** | 92.70 |

**720p source, 2.7 Mbps:**

| Encoder | 60s size | VMAF |
|---|---:|---:|
| YouTube | 2.2 MB | 97.05 |
| **local x264 crf23** | **1.5 MB** | 96.82 |

Local encoding wins on both, and by a wide margin at 1080p — **56% smaller at slightly
better measured quality**. It also avoids the daily upload cap, the bot-check that blocks
downloads after sustained use, copyright rejections on legitimate content, and the fact
that low-bitrate files come back from YouTube *larger* than they went in.

**Use `local_encode.py` unless you specifically want YouTube's encoder.** The YouTube
scripts remain here and work, but the round trip costs days of uploading to produce a
bigger file.

---

## Usage - local encoding (`local_encode.py`)

```bash
python local_encode.py "E:\source folder" "E:\output folder"
```

Walks the tree, encodes each video, and writes it to the mirrored path. Defaults to AV1
at CRF 42. Progress lives in `OUTPUT/.local_encode_state.json`, so it is resumable -
rerun the same command and finished files are skipped.

| Flag | Default | What it does |
|---|---|---|
| `--codec` | `av1` | `av1` (smallest), `h264` (plays anywhere), `h265` (middle) |
| `--crf` | per codec | Lower is better quality. av1 42, h264 26, h265 28 |
| `--min-bitrate` | `1.5` | Mbps below which a file is **copied verbatim** rather than encoded |
| `--force-mp4` | off | Encode low-bitrate files too, so every output is `.mp4` |
| `--ext` | `.mp4,.wmv,.mov,.avi,.mkv` | Which files to pick up |
| `--dry-run` | off | List what would happen and exit |

**Why `--min-bitrate` matters.** Below roughly 1.5 Mbps there is nothing left to remove,
and re-encoding makes the file *bigger*. Those files are copied through untouched so your
output tree stays a complete mirror. This was measured, not guessed - a 0.2 Mbps file put
through YouTube came back at 188% of its original size.

**Getting a uniformly `.mp4` output.** A copied file keeps its own container, so a
low-bitrate `.wmv` stays `.wmv`. Pass `--force-mp4` to encode those as well. They are
encoded at `crf + 4`, because a normal crf inflates an already-heavily-compressed source -
measured on a 0.2 Mbps wmv, crf26 produced 110% of the original while crf30 produced 96%.
Expect a small quality drop on these files: they have little margin left, and any
re-encode costs something. Re-running with `--force-mp4` re-processes anything a previous
run copied, and deletes the file it supersedes.

Encoding runs at roughly 2x realtime for 1080p AV1 on a typical desktop, so an hour of
footage takes about half an hour. Every encode is verified against the source duration
before being marked done, so a truncated or failed encode is reported rather than silently
accepted.

Choose `--codec h264` if the output is going to other people - AV1 needs a recent player,
while H.264 plays on anything, and even at CRF 26 it still beats YouTube by ~32%.

---

## Two ways to run it through YouTube

**`yt_pullback.py` — drag-and-drop (recommended for large libraries).** You upload
through the YouTube Studio web UI yourself, then this script pulls everything back down
and rebuilds the folder tree. Browser uploads are not subject to the API upload quota, so
the 6-per-day ceiling disappears entirely.

**`yt_roundtrip.py` — fully automated.** Uploads via the API too, so nothing is manual —
but you are capped at roughly 6 videos per day.

Both need the same OAuth setup, and both preserve your folder structure and file names.

---

## Usage — drag-and-drop (`yt_pullback.py`)

**1. Upload.** In [YouTube Studio](https://studio.youtube.com/) → **Create → Upload
videos**, drag your folders in. YouTube flattens them — that's fine, the script puts the
structure back. Set visibility to **Unlisted** during upload if you can; it saves 50 quota
units per video versus letting the script flip them later.

**2. Pull them back.**

```bash
python yt_pullback.py "E:\source folder" "E:\output folder" --since 2026-07-21 --dry-run
```

`--since` is the safety catch: videos published before that date are ignored completely, so
the script can never touch pre-existing content on your channel. Set it to the day you
started uploading.

`--dry-run` prints the full match table and exits without changing anything. Check it, then
drop the flag to run for real.

### How matching works

YouTube uses the filename as the title but rewrites punctuation —
`1.การอ่าน Block diagram.mp4` becomes the title `1 การอ่าน Block diagram`. The script folds
both sides to a comparable key (punctuation → spaces, case-flattened, Unicode NFC, and
combining marks such as Thai tone marks preserved) and matches on that.

Anything it cannot resolve is reported, never guessed:

- **name collisions** — two source files whose names normalise identically are skipped,
  since there is no safe way to know which folder a video belongs in
- **unmatched videos** — on YouTube but with no local counterpart; left completely alone
- **missing files** — local files with no video yet, i.e. still uploading

### What it does per video

Flips Private → Unlisted if needed → waits until YouTube serves a rendition at the source
resolution → downloads to `OUTPUT/<original relative path>` → **verifies the file exists and
is non-empty** → only then deletes it from YouTube.

### Quota

Listing is ~1 unit per 50 videos, but `videos.update` and `videos.delete` cost **50 units
each**. With a 10,000/day budget that's about 100 videos per run if the script has to both
unlist and delete, or ~200 if you set visibility during upload. It tracks its own spend and
stops cleanly at `--quota-budget`; rerun the next day to continue.

### Options

| Flag | Default | What it does |
|---|---|---|
| `--since` | `2026-07-21` | Ignore anything published before this date |
| `--keep-remote` | off | Don't delete from YouTube after downloading |
| `--keep-private` | off | Don't flip Private → Unlisted (then you need `--cookies*`) |
| `--quota-budget` | `10000` | Stop before exceeding this many API units |
| `--codec` | `any` | `av1`, `vp9`, `h264` |
| `--dry-run` | off | Print the match table and exit |

Plus `--ext`, `--client-secrets`, `--token`, `--cookies`, `--cookies-from-browser`,
`--wait-timeout`, `--poll-interval`, same as below.

---

## Usage — fully automated (`yt_roundtrip.py`)

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
