#!/usr/bin/env python3
"""
local_encode.py - re-compress a video tree locally, mirroring folder structure.

Same interface as yt_pullback.py, but the encoding happens on this machine
instead of going through YouTube. Measured against YouTube's own encoder on
1080p source material, AV1 at CRF 42 produced 56% smaller files at slightly
higher VMAF, so this is both smaller and faster end to end.

    python local_encode.py SOURCE OUTPUT

Files whose source bitrate is below --min-bitrate are copied verbatim rather
than encoded: below roughly 1.5 Mbps there is nothing left to remove, and
re-encoding makes them larger.

Progress is stored in OUTPUT/.local_encode_state.json so the run is resumable.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from yt_roundtrip import State

__version__ = "1.0.0"

STATE_NAME = ".local_encode_state.json"

# crf defaults chosen from the VMAF head-to-head against YouTube's renditions.
CODECS = {
    "av1": {
        "crf": 42,
        "args": ["-c:v", "libsvtav1", "-preset", "6"],
        "audio": ["-c:a", "libopus", "-b:a", "128k"],
    },
    "h264": {
        "crf": 26,
        "args": ["-c:v", "libx264", "-preset", "slow"],
        "audio": ["-c:a", "aac", "-b:a", "128k"],
    },
    "h265": {
        "crf": 28,
        "args": ["-c:v", "libx265", "-preset", "medium", "-tag:v", "hvc1"],
        "audio": ["-c:a", "aac", "-b:a", "128k"],
    },
}


def probe(path: Path) -> dict:
    """Duration and stream info for one file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-show_entries", "stream=codec_type,height", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    return {
        "duration": float(data["format"]["duration"]),
        "height": int(video[0]["height"]) if video else 0,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def encode(src: Path, dst: Path, codec: str, crf: int, duration: float) -> None:
    """Run ffmpeg, printing a live percentage from its progress stream."""
    spec = CODECS[codec]
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?",
        *spec["args"], "-crf", str(crf), "-pix_fmt", "yuv420p",
        *spec["audio"], "-movflags", "+faststart", str(dst),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        if line.startswith("out_time_ms=") and duration:
            try:
                pct = int(line.split("=")[1]) / 1e6 / duration * 100
            except (ValueError, ZeroDivisionError):
                continue
            print(f"\r    encoding {min(pct, 100):5.1f}%", end="", flush=True)
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise RuntimeError(err.strip()[:300] or "ffmpeg failed")
    print("\r    encoding 100.0%")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Re-compress a video tree locally, preserving structure.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("source", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--codec", choices=list(CODECS), default="av1",
                   help="av1 is smallest; h264 plays on anything")
    p.add_argument("--crf", type=int,
                   help="quality, lower is better (default depends on --codec)")
    p.add_argument("--min-bitrate", type=float, default=1.5,
                   help="Mbps below which a file is copied instead of encoded")
    p.add_argument("--ext", default=".mp4,.wmv,.mov,.avi,.mkv")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args(argv)
    if not args.source.is_dir():
        sys.exit(f"source is not a folder: {args.source}")
    crf = args.crf if args.crf is not None else CODECS[args.codec]["crf"]

    exts = {e if e.startswith(".") else "." + e
            for e in (x.strip().lower() for x in args.ext.split(",")) if e}
    files = sorted(p for p in args.source.rglob("*")
                   if p.is_file() and p.suffix.lower() in exts)
    if not files:
        sys.exit(f"no video files under {args.source}")

    state = State(args.output / STATE_NAME)
    print(f"{len(files)} file(s), {args.codec} crf{crf}, "
          f"copy below {args.min_bitrate} Mbps\n")

    done = copied = failed = skipped = 0
    src_total = out_total = 0
    started = time.time()

    for n, src in enumerate(files, 1):
        rel = src.relative_to(args.source)
        key = rel.as_posix()
        e = state.entry(key)
        size = src.stat().st_size

        # Copies keep their container; encodes always land as .mp4.
        try:
            info = probe(src)
        except Exception as exc:
            print(f"[{n}/{len(files)}] {key}\n    probe failed: {exc}")
            e.update(status="failed", error=str(exc)[:200])
            failed += 1
            state.save()
            continue

        mbps = size * 8 / info["duration"] / 1e6 if info["duration"] else 0
        low = mbps < args.min_bitrate
        dst = args.output / (rel if low else rel.with_suffix(".mp4"))

        if e.get("status") in ("done", "copied") and dst.exists():
            src_total += size
            out_total += dst.stat().st_size
            skipped += 1
            continue

        print(f"[{n}/{len(files)}] {key}")
        print(f"    {size/1e6:.0f} MB, {info['height']}p, {mbps:.1f} Mbps")

        if args.dry_run:
            print(f"    would {'copy' if low else 'encode'} -> {dst.name}")
            continue

        if low:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            e.update(status="copied", size=dst.stat().st_size, mbps=round(mbps, 2))
            print(f"    copied as-is (below {args.min_bitrate} Mbps)")
            copied += 1
        else:
            t0 = time.time()
            try:
                encode(src, dst, args.codec, crf, info["duration"])
            except Exception as exc:
                print(f"    encode failed: {exc}")
                e.update(status="failed", error=str(exc)[:200])
                failed += 1
                state.save()
                continue

            # A truncated encode is the failure mode that matters here.
            try:
                got = probe(dst)["duration"]
            except Exception:
                got = 0
            if abs(got - info["duration"]) > 2:
                print(f"    TRUNCATED: {got:.0f}s vs source {info['duration']:.0f}s")
                e.update(status="failed", error="duration mismatch")
                failed += 1
                state.save()
                continue

            out = dst.stat().st_size
            e.update(status="done", size=out, mbps=round(mbps, 2),
                     ratio=round(out / size, 3))
            print(f"    {size/1e6:.0f} -> {out/1e6:.0f} MB  "
                  f"({out/size*100:.1f}%)  in {time.time()-t0:.0f}s")
            done += 1

        src_total += size
        out_total += dst.stat().st_size
        state.save()

    state.save()
    print(f"\nencoded {done}, copied {copied}, skipped {skipped}, failed {failed}")
    if src_total:
        print(f"{src_total/1e9:.2f} GB -> {out_total/1e9:.2f} GB "
              f"({out_total/src_total*100:.1f}%)")
    print(f"elapsed {(time.time()-started)/60:.0f} min")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
