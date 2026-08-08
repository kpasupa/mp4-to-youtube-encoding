#!/usr/bin/env python3
"""
yt_roundtrip.py - re-compress videos by round-tripping them through YouTube.

Uploads every video under SOURCE to your own YouTube channel, waits until
YouTube has finished transcoding a rendition matching the source resolution,
then downloads that rendition back into OUTPUT, preserving folder structure
and file names.

    python yt_roundtrip.py SOURCE OUTPUT

Requires ffmpeg/ffprobe on PATH and a Google OAuth client_secrets.json.
Progress is stored in OUTPUT/.yt_roundtrip_state.json so the run is resumable
(important: the YouTube API only allows ~6 uploads per day on default quota).
"""
from __future__ import annotations

import argparse
import json
import random
import string
import subprocess
import sys
import time
from pathlib import Path

import yt_dlp
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

__version__ = "1.0.2"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
# Resolution rungs YouTube actually encodes to.
RUNGS = [144, 240, 360, 480, 720, 1080, 1440, 2160, 4320]
STATE_NAME = ".yt_roundtrip_state.json"
RETRIABLE_STATUS = {500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def probe_height(path: Path) -> int:
    """Display height of the first video stream, accounting for rotation."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:stream_side_data=rotation",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    stream = json.loads(out)["streams"][0]
    w, h = int(stream["width"]), int(stream["height"])
    for sd in stream.get("side_data_list", []):
        if abs(int(sd.get("rotation", 0))) % 180 == 90:
            w, h = h, w
    return h


def target_rung(height: int) -> int:
    """Highest standard rung YouTube will produce for this source."""
    return max([r for r in RUNGS if r <= height] or [RUNGS[0]])


def short_id(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def is_quota_error(exc: HttpError) -> bool:
    text = str(getattr(exc, "content", b"")) + str(exc)
    return exc.resp.status == 403 and (
        "quotaExceeded" in text or "uploadLimitExceeded" in text
    )


class State:
    """One JSON blob keyed by source path relative to SOURCE."""

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def entry(self, key: str) -> dict:
        return self.data.setdefault(key, {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))


# --------------------------------------------------------------------------- #
# youtube api
# --------------------------------------------------------------------------- #
def resolve_secrets(explicit: Path) -> Path:
    """Use the given path, else any client_secret*.json next to this script."""
    if explicit.exists():
        return explicit
    for folder in (Path(__file__).parent, Path.cwd()):
        found = sorted(folder.glob("client_secret*.json"))
        if found:
            return found[0]
    return explicit


def get_service(secrets: Path, token: Path):
    secrets = resolve_secrets(secrets)
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not secrets.exists():
                sys.exit(
                    "no OAuth client config found - download the Desktop app JSON "
                    "from Google Cloud Console into this folder, or pass "
                    "--client-secrets PATH"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
            creds = flow.run_local_server(port=0)
        token.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(service, path: Path, title: str, description: str, privacy: str) -> str:
    body = {
        "snippet": {"title": title[:100], "description": description[:5000]},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response, errors = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"\r    uploading {status.progress() * 100:5.1f}%", end="", flush=True)
        except HttpError as e:
            if e.resp.status in RETRIABLE_STATUS and errors < 5:
                errors += 1
                time.sleep(2 ** errors)
                continue
            raise
    print("\r    uploading 100.0%")
    return response["id"]


def delete_video(service, video_id: str) -> None:
    service.videos().delete(id=video_id).execute()


# --------------------------------------------------------------------------- #
# yt-dlp
# --------------------------------------------------------------------------- #
def base_opts(args) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    if args.cookies:
        opts["cookiefile"] = args.cookies
    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    return opts


def available_heights(url: str, opts: dict) -> list[int]:
    """Video-stream heights YouTube currently serves. [] if not ready yet."""
    try:
        with yt_dlp.YoutubeDL({**opts, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError:
        return []
    return sorted(
        {
            f["height"]
            for f in info.get("formats", [])
            if f.get("vcodec") not in (None, "none") and f.get("height")
        }
    )


def wait_for_rung(url: str, rung: int, opts: dict, timeout: int, interval: int) -> int | None:
    """Poll until a rendition >= rung exists. Returns the best height found."""
    deadline = time.time() + timeout
    seen = 0
    while time.time() < deadline:
        heights = available_heights(url, opts)
        if heights:
            best = max(heights)
            if best != seen:
                print(f"    transcoded so far: {best}p")
                seen = best
            if best >= rung:
                return best
        remaining = int(deadline - time.time())
        print(f"\r    waiting for {rung}p ... {remaining}s left", end="", flush=True)
        time.sleep(interval)
    print()
    return None


def download(url: str, rung: int, dest: Path, codec: str, opts: dict) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                pct = d["downloaded_bytes"] / total * 100
                print(f"\r    downloading {pct:5.1f}%", end="", flush=True)

    sort = {"av1": ["vcodec:av01"], "vp9": ["vcodec:vp9"], "h264": ["vcodec:avc1"]}
    ydl_opts = {
        **opts,
        # Prefer m4a audio so the result stays a broadly-playable mp4.
        "format": (
            f"bv*[height<={rung}]+ba[ext=m4a]/"
            f"bv*[height<={rung}]+ba/"
            f"b[height<={rung}]"
        ),
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "outtmpl": str(dest.parent / dest.stem) + ".%(ext)s",
        "progress_hooks": [hook],
        "overwrites": True,
    }
    if codec in sort:
        ydl_opts["format_sort"] = sort[codec]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    print()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Re-compress videos by round-tripping them through YouTube.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("source", type=Path, help="folder to scan (searched recursively)")
    p.add_argument("output", type=Path, help="folder to write results into")
    p.add_argument("--ext", default=".mp4",
                   help="comma-separated extensions to process")
    p.add_argument("--privacy", choices=["unlisted", "private", "public"],
                   default="unlisted",
                   help="'private' needs --cookies* to download back")
    p.add_argument("--codec", choices=["any", "av1", "vp9", "h264"], default="any",
                   help="preferred video codec of the downloaded copy")
    p.add_argument("--client-secrets", type=Path, default=Path("client_secrets.json"),
                   help="falls back to any client_secret*.json beside the script")
    p.add_argument("--token", type=Path, default=Path("token.json"))
    p.add_argument("--cookies", help="cookies.txt file, for private videos")
    p.add_argument("--cookies-from-browser", help="e.g. chrome, firefox, edge")
    p.add_argument("--wait-timeout", type=int, default=7200,
                   help="max seconds to wait for YouTube to finish one video")
    p.add_argument("--poll-interval", type=int, default=60)
    p.add_argument("--keep-remote", action="store_true",
                   help="keep the video on YouTube (default: delete it after a "
                        "successful download)")
    p.add_argument("--dry-run", action="store_true", help="list work and exit")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.source.is_dir():
        sys.exit(f"source is not a folder: {args.source}")

    exts = {e if e.startswith(".") else "." + e
            for e in (x.strip().lower() for x in args.ext.split(",")) if e}
    files = sorted(p for p in args.source.rglob("*")
                   if p.is_file() and p.suffix.lower() in exts)
    if not files:
        print(f"no {'/'.join(sorted(exts))} files under {args.source}")
        return 0

    state = State(args.output / STATE_NAME)
    opts = base_opts(args)

    print(f"{len(files)} file(s) under {args.source}\n")
    if args.dry_run:
        for f in files:
            key = f.relative_to(args.source).as_posix()
            st = state.entry(key).get("status", "pending")
            print(f"  [{st:>10}] {key}")
        return 0

    service = get_service(args.client_secrets, args.token)

    # ---- phase 1: upload everything (YouTube transcodes in parallel) ------- #
    print("=== phase 1/2: uploading ===")
    quota_hit = False
    for i, path in enumerate(files, 1):
        key = path.relative_to(args.source).as_posix()
        e = state.entry(key)
        dest = args.output / path.relative_to(args.source)

        if e.get("status") == "done" and dest.exists():
            print(f"[{i}/{len(files)}] {key}\n    already done, skipping")
            continue
        if e.get("video_id"):
            print(f"[{i}/{len(files)}] {key}\n    already uploaded: {e['video_id']}")
            continue

        print(f"[{i}/{len(files)}] {key}")
        try:
            height = probe_height(path)
        except (subprocess.CalledProcessError, KeyError, IndexError) as exc:
            print(f"    ffprobe failed, skipping: {exc}")
            e.update(status="failed", error=f"ffprobe: {exc}")
            state.save()
            continue

        e["source_height"] = height
        e["target_rung"] = target_rung(height)
        print(f"    source {height}p -> waiting for {e['target_rung']}p")

        try:
            e["video_id"] = upload(
                service, path,
                title=f"{path.stem} [{short_id()}]",
                description=f"yt_roundtrip {__version__}\n{key}",
                privacy=args.privacy,
            )
            e["status"] = "uploaded"
            print(f"    video id {e['video_id']}")
        except HttpError as exc:
            if is_quota_error(exc):
                print("    quota exhausted - stopping uploads for today")
                quota_hit = True
                state.save()
                break
            print(f"    upload failed: {exc}")
            e.update(status="failed", error=str(exc))
        state.save()

    # ---- phase 2: wait for the matching rung, then pull it back ----------- #
    print("\n=== phase 2/2: waiting + downloading ===")
    done = failed = waiting = 0
    for i, path in enumerate(files, 1):
        key = path.relative_to(args.source).as_posix()
        e = state.entry(key)
        dest = args.output / path.relative_to(args.source)

        if e.get("status") == "done" and dest.exists():
            done += 1
            continue
        if not e.get("video_id"):
            continue

        url = f"https://www.youtube.com/watch?v={e['video_id']}"
        rung = e["target_rung"]
        print(f"[{i}/{len(files)}] {key}")

        best = wait_for_rung(url, rung, opts, args.wait_timeout, args.poll_interval)
        if best is None:
            print(f"    timed out before {rung}p appeared - rerun to resume")
            waiting += 1
            continue

        try:
            download(url, rung, dest, args.codec, opts)
        except yt_dlp.utils.DownloadError as exc:
            print(f"    download failed: {exc}")
            e.update(status="failed", error=str(exc))
            failed += 1
            state.save()
            continue

        if not dest.exists():
            produced = list(dest.parent.glob(dest.stem + ".*"))
            print(f"    expected {dest.name}, got {[p.name for p in produced]}")
            e.update(status="failed", error="unexpected output extension")
            failed += 1
            state.save()
            continue

        e.update(status="done", downloaded_height=best,
                 size=dest.stat().st_size)
        print(f"    saved {dest}  ({dest.stat().st_size / 1e6:.1f} MB, {best}p)")
        done += 1

        if not args.keep_remote:
            try:
                delete_video(service, e["video_id"])
                e["deleted"] = True
                print("    deleted from YouTube")
            except HttpError as exc:
                print(f"    could not delete: {exc}")
        state.save()

    state.save()
    print(f"\ndone: {done}   failed: {failed}   still processing: {waiting}")
    if quota_hit:
        print("Daily upload quota was hit - rerun tomorrow to continue.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
