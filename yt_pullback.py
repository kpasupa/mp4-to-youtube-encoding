#!/usr/bin/env python3
"""
yt_pullback.py - pull drag-and-dropped uploads back down, rebuilding the tree.

Companion to yt_roundtrip.py for when you upload through the YouTube Studio web
UI instead of the API (drag-and-drop has no 6/day upload quota).

You drag the folder into YouTube Studio; this script then:

  1. lists your channel's uploads, keeping only videos published on/after --since
  2. matches each YouTube title back to a source file by normalised name
     ("1.การอ่าน Block diagram.mp4" -> title "1 การอ่าน Block diagram")
  3. flips anything still Private to Unlisted so it can be downloaded
  4. waits until YouTube serves a rendition matching the source resolution
  5. downloads it into OUTPUT at the original relative path and file name
  6. deletes the video from YouTube once the file is safely on disk

    python yt_pullback.py SOURCE OUTPUT --since 2026-07-21

Always start with --dry-run: it prints the full match table and touches nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import unicodedata
from pathlib import Path

import yt_dlp
from googleapiclient.errors import HttpError

from yt_roundtrip import (
    State,
    available_heights,
    base_opts,
    download,
    get_service,
    probe_height,
    target_rung,
    wait_for_rung,
)

__version__ = "1.1.0"

STATE_NAME = ".yt_pullback_state.json"
# YouTube Data API v3 costs.
COST_LIST = 1
COST_UPDATE = 50
COST_DELETE = 50


class Quota:
    """Stop cleanly at the daily ceiling instead of erroring out mid-run."""

    def __init__(self, budget: int):
        self.budget, self.spent = budget, 0

    def can(self, cost: int) -> bool:
        return self.spent + cost <= self.budget

    def charge(self, cost: int) -> None:
        self.spent += cost


def _is_word_char(char: str) -> bool:
    # Thai tone marks and vowel signs are combining marks (Unicode category M*),
    # which are not alnum - keep them or 'การอ่าน' degrades to 'การอ าน'.
    return char.isalnum() or unicodedata.category(char).startswith("M")


def normalise(text: str) -> str:
    """Fold a filename or YouTube title into a comparable key.

    YouTube rewrites punctuation in titles - '1.การอ่าน Block diagram' comes
    back as '1 การอ่าน Block diagram' - so collapse every non-word run to a
    single space. NFC first, so the same Thai text composed two different ways
    still compares equal.
    """
    text = unicodedata.normalize("NFC", text)
    return " ".join("".join(c if _is_word_char(c) else " " for c in text).split()).lower()


def iso_to_date(value: str) -> dt.date:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def fetch_uploads(service, since: dt.date, quota: Quota) -> list[dict]:
    """Every video on the authenticated channel published on/after `since`."""
    quota.charge(COST_LIST)
    channels = service.channels().list(part="contentDetails", mine=True).execute()
    if not channels.get("items"):
        sys.exit("no channel found for this account")
    uploads = channels["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    video_ids, page = [], None
    while True:
        quota.charge(COST_LIST)
        resp = service.playlistItems().list(
            part="contentDetails", playlistId=uploads,
            maxResults=50, pageToken=page,
        ).execute()
        video_ids += [i["contentDetails"]["videoId"] for i in resp["items"]]
        page = resp.get("nextPageToken")
        if not page:
            break

    videos = []
    for i in range(0, len(video_ids), 50):
        quota.charge(COST_LIST)
        resp = service.videos().list(
            part="snippet,status", id=",".join(video_ids[i:i + 50])
        ).execute()
        for item in resp["items"]:
            published = item["snippet"].get("publishedAt")
            if published and iso_to_date(published) < since:
                continue
            videos.append({
                "id": item["id"],
                "title": item["snippet"]["title"],
                "published": published,
                "status": item["status"],
            })
    return videos


def build_index(files: list[Path], source: Path) -> tuple[dict, dict]:
    """normalised stem -> relative path, plus whatever collided."""
    index, clashes = {}, {}
    for f in files:
        key = normalise(f.stem)
        if key in index:
            clashes.setdefault(key, [index[key]]).append(f.relative_to(source))
        else:
            index[key] = f.relative_to(source)
    for key in clashes:
        index.pop(key, None)
    return index, clashes


def set_unlisted(service, video: dict, quota: Quota) -> bool:
    """Private videos can't be read by yt-dlp without cookies; flip to unlisted."""
    status = dict(video["status"])
    if status.get("privacyStatus") == "unlisted":
        return True
    if not quota.can(COST_UPDATE):
        return False
    status["privacyStatus"] = "unlisted"
    # videos.update replaces the whole part, so send back what was already there.
    body = {"id": video["id"], "status": {
        k: v for k, v in status.items()
        if k in ("privacyStatus", "selfDeclaredMadeForKids", "embeddable",
                 "license", "publicStatsViewable")
    }}
    service.videos().update(part="status", body=body).execute()
    quota.charge(COST_UPDATE)
    video["status"] = status
    return True


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Download drag-and-dropped YouTube uploads back into a mirrored tree.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("source", type=Path, help="the folder you dragged into YouTube")
    p.add_argument("output", type=Path, help="where to rebuild the tree")
    p.add_argument("--since", default="2026-07-21",
                   help="ignore videos published before this date (YYYY-MM-DD)")
    p.add_argument("--ext", default=".mp4", help="comma-separated source extensions")
    p.add_argument("--codec", choices=["any", "av1", "vp9", "h264"], default="any")
    p.add_argument("--keep-remote", action="store_true",
                   help="don't delete videos from YouTube after downloading")
    p.add_argument("--keep-private", action="store_true",
                   help="don't flip Private videos to Unlisted (needs --cookies then)")
    p.add_argument("--client-secrets", type=Path, default=Path("client_secrets.json"))
    p.add_argument("--token", type=Path, default=Path("token.json"))
    p.add_argument("--cookies")
    p.add_argument("--cookies-from-browser")
    p.add_argument("--wait-timeout", type=int, default=7200)
    p.add_argument("--poll-interval", type=int, default=60)
    p.add_argument("--quota-budget", type=int, default=10000,
                   help="stop before exceeding this many API units")
    p.add_argument("--dry-run", action="store_true",
                   help="print the match table and exit")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args(argv)
    if not args.source.is_dir():
        sys.exit(f"source is not a folder: {args.source}")
    try:
        since = dt.date.fromisoformat(args.since)
    except ValueError:
        sys.exit(f"--since must be YYYY-MM-DD, got {args.since!r}")

    exts = {e if e.startswith(".") else "." + e
            for e in (x.strip().lower() for x in args.ext.split(",")) if e}
    files = sorted(p for p in args.source.rglob("*")
                   if p.is_file() and p.suffix.lower() in exts)
    if not files:
        sys.exit(f"no {'/'.join(sorted(exts))} files under {args.source}")

    index, clashes = build_index(files, args.source)
    quota = Quota(args.quota_budget)
    state = State(args.output / STATE_NAME)
    opts = base_opts(args)

    service = get_service(args.client_secrets, args.token)
    print(f"scanning channel for uploads on/after {since} ...")
    videos = fetch_uploads(service, since, quota)
    print(f"{len(files)} local file(s), {len(videos)} video(s) since {since}\n")

    matched, unmatched = [], []
    for v in videos:
        rel = index.get(normalise(v["title"]))
        (matched if rel else unmatched).append((v, rel))
    matched = [(v, rel) for v, rel in matched if rel]

    seen = {rel for _, rel in matched}
    missing = [f.relative_to(args.source) for f in files
               if f.relative_to(args.source) not in seen]

    print(f"matched: {len(matched)}   "
          f"unmatched videos: {len(unmatched)}   "
          f"local files with no video: {len(missing)}")

    if clashes:
        print(f"\n!! {len(clashes)} name collision(s) - skipped, rename to fix:")
        for key, paths in clashes.items():
            for p in paths:
                print(f"     {p}")
    if unmatched:
        print("\nvideos on YouTube with no matching local file (left alone):")
        for v, _ in unmatched:
            print(f"     {v['title'][:70]}")
    if missing:
        print("\nlocal files not found on YouTube (not uploaded yet?):")
        for m in missing[:20]:
            print(f"     {m}")
        if len(missing) > 20:
            print(f"     ... and {len(missing) - 20} more")

    if args.dry_run:
        print("\n--- match table ---")
        for v, rel in matched:
            e = state.entry(rel.as_posix())
            print(f"  [{e.get('status', 'pending'):>8}] {v['id']}  "
                  f"{v['status']['privacyStatus']:<8} -> {rel}")
        print(f"\nquota used: {quota.spent}/{args.quota_budget} (listing only)")
        return 0

    done = failed = skipped = 0
    for n, (v, rel) in enumerate(matched, 1):
        key = rel.as_posix()
        e = state.entry(key)
        dest = args.output / rel
        src = args.source / rel

        if e.get("status") == "done" and dest.exists():
            done += 1
            continue

        print(f"\n[{n}/{len(matched)}] {key}")
        e["video_id"] = v["id"]

        if not args.keep_private and v["status"]["privacyStatus"] == "private":
            if not set_unlisted(service, v, quota):
                print("    quota exhausted - rerun tomorrow")
                break
            print("    set to unlisted")

        try:
            rung = target_rung(probe_height(src))
        except Exception as exc:
            print(f"    ffprobe failed, skipping: {exc}")
            e.update(status="failed", error=str(exc))
            failed += 1
            state.save()
            continue
        e["target_rung"] = rung

        url = f"https://www.youtube.com/watch?v={v['id']}"
        best = wait_for_rung(url, rung, opts, args.wait_timeout, args.poll_interval)
        if best is None:
            print(f"    still transcoding, no {rung}p yet - rerun later")
            skipped += 1
            continue

        try:
            download(url, rung, dest, args.codec, opts)
        except yt_dlp.utils.DownloadError as exc:
            print(f"    download failed: {exc}")
            e.update(status="failed", error=str(exc))
            failed += 1
            state.save()
            continue

        if not dest.exists() or dest.stat().st_size == 0:
            print("    download produced no file - leaving the video up")
            e.update(status="failed", error="empty output")
            failed += 1
            state.save()
            continue

        got = min(best, rung)
        e.update(status="done", downloaded_height=got, size=dest.stat().st_size)
        print(f"    saved {dest.stat().st_size / 1e6:.1f} MB at {got}p")
        done += 1

        # Only ever delete once the file is verifiably on disk.
        if not args.keep_remote:
            if not quota.can(COST_DELETE):
                print("    quota exhausted - video left on YouTube, rerun to clean up")
                e["pending_delete"] = True
                state.save()
                break
            try:
                service.videos().delete(id=v["id"]).execute()
                quota.charge(COST_DELETE)
                e["deleted"] = True
                print("    deleted from YouTube")
            except HttpError as exc:
                print(f"    could not delete: {exc}")
        state.save()

    state.save()
    print(f"\ndone: {done}   failed: {failed}   still processing: {skipped}")
    print(f"quota used: {quota.spent}/{args.quota_budget}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
