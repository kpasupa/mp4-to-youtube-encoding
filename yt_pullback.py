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
import time
import unicodedata
from pathlib import Path

import yt_dlp
from googleapiclient.errors import HttpError

from yt_roundtrip import (
    State,
    base_opts,
    download,
    get_service,
    probe_height,
    target_rung,
)

__version__ = "1.1.2"

STATE_NAME = ".yt_pullback_state.json"
# YouTube Data API v3 costs.
COST_LIST = 1
COST_UPDATE = 50
COST_DELETE = 50


# yt-dlp reports codecs as e.g. 'vp9', 'av01.0.08M', 'avc1.640028'.
CODEC_PREFIX = {"av1": "av01", "vp9": "vp9", "h264": "avc1"}


def probe_renditions(url: str, opts: dict) -> list[tuple[int, str]]:
    """(height, codec) for every video rendition YouTube currently serves."""
    try:
        with yt_dlp.YoutubeDL({**opts, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError:
        return []
    return [(f["height"], (f.get("vcodec") or "").split(".")[0])
            for f in info.get("formats", [])
            if f.get("vcodec") not in (None, "none") and f.get("height")]


def readiness(url: str, rung: int, codec: str, opts: dict) -> tuple[int, bool]:
    """Best height available, and whether the wanted codec exists at `rung`.

    YouTube publishes H.264 within minutes but VP9/AV1 only later, so treating
    'the resolution exists' as ready hands you the largest rendition on offer.
    """
    rends = probe_renditions(url, opts)
    best = max([h for h, _ in rends] or [0])
    if codec == "any":
        return best, best >= rung
    prefix = CODEC_PREFIX[codec]
    return best, any(h >= rung and vc.startswith(prefix) for h, vc in rends)


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
    p.add_argument("--codec", choices=["any", "av1", "vp9", "h264"], default="vp9",
                   help="wait for this codec at source resolution before "
                        "downloading; 'any' takes the first rendition, which is "
                        "H.264 and roughly twice the size")
    p.add_argument("--codec-wait", type=int, default=21600,
                   help="seconds to wait for --codec before settling for H.264")
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

    # ---- prepare the work list: unlist everything, probe every source ----- #
    done = failed = 0
    pending = []
    for v, rel in matched:
        key = rel.as_posix()
        e = state.entry(key)
        dest = args.output / rel

        if e.get("status") == "done" and dest.exists():
            done += 1
            continue
        e["video_id"] = v["id"]

        if not args.keep_private and v["status"]["privacyStatus"] == "private":
            if not set_unlisted(service, v, quota):
                print(f"quota exhausted while unlisting - {key} left for next run")
                continue
            print(f"unlisted  {key}")

        try:
            rung = target_rung(probe_height(args.source / rel))
        except Exception as exc:
            print(f"ffprobe failed, skipping {key}: {exc}")
            e.update(status="failed", error=str(exc))
            failed += 1
            continue
        e["target_rung"] = rung
        pending.append({"v": v, "rel": rel, "dest": dest, "rung": rung,
                        "url": f"https://www.youtube.com/watch?v={v['id']}"})
    state.save()

    # ---- take whatever is ready, cycle, don't let one slow transcode block --- #
    print(f"\n{len(pending)} to fetch; waiting for a {args.codec} rendition "
          f"at source resolution\n")
    start = time.time()
    settled_warned = False
    # Never give up before the codec deadline, or the two flags fight.
    deadline = start + max(args.wait_timeout, args.codec_wait)
    while pending:
        # Past the codec deadline, take H.264 rather than lose the upload.
        settle = time.time() > start + args.codec_wait
        want = "any" if settle else args.codec
        if settle and not settled_warned:
            print(f"  waited {args.codec_wait}s for {args.codec}; "
                  f"accepting whatever is available now")
            settled_warned = True

        ready = []
        for job in list(pending):
            best, is_ready = readiness(job["url"], job["rung"], want, opts)
            job["best"] = best
            job["codec"] = want
            if is_ready:
                ready.append(job)

        for job in ready:
            pending.remove(job)
            rel, dest, rung = job["rel"], job["dest"], job["rung"]
            key = rel.as_posix()
            e = state.entry(key)
            print(f"[{done + failed + 1}/{len(matched)}] {key}")

            try:
                download(job["url"], rung, dest, job["codec"], opts)
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

            got = min(job["best"], rung)
            e.update(status="done", downloaded_height=got, size=dest.stat().st_size)
            print(f"    saved {dest.stat().st_size / 1e6:.1f} MB at {got}p")
            done += 1

            # Only ever delete once the file is verifiably on disk.
            if not args.keep_remote:
                if not quota.can(COST_DELETE):
                    print("    quota exhausted - left on YouTube, rerun to clean up")
                    e["pending_delete"] = True
                elif not args.dry_run:
                    try:
                        service.videos().delete(id=job["v"]["id"]).execute()
                        quota.charge(COST_DELETE)
                        e["deleted"] = True
                        print("    deleted from YouTube")
                    except HttpError as exc:
                        print(f"    could not delete: {exc}")
            state.save()

        if not pending:
            break
        if time.time() > deadline:
            print(f"\nstill transcoding after {args.wait_timeout}s, left for a rerun:")
            for job in pending:
                print(f"     {job['best']}p / need {job['rung']}p  {job['rel']}")
            break
        if not ready:
            waiting = ", ".join(f"{j['best']}p/{j['rung']}p" for j in pending[:6])
            print(f"  {len(pending)} awaiting {want} ({waiting}) - "
                  f"checking again in {args.poll_interval}s")
            time.sleep(args.poll_interval)

    state.save()
    print(f"\ndone: {done}   failed: {failed}   still processing: {len(pending)}")
    print(f"quota used: {quota.spent}/{args.quota_budget}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
