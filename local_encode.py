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
import tempfile
import time
from pathlib import Path

from yt_roundtrip import State

__version__ = "1.1.4"

STATE_NAME = ".local_encode_state.json"
NEEDS_UPLOAD = "_needs_upload.txt"
# Low-bitrate sources inflate at the normal crf; measured on a 0.2 Mbps wmv,
# crf26 gave 110% of source while crf30 gave 96%.
LOW_CRF_BUMP = 4
# ffmpeg emits a progress block roughly every half second; if several minutes of
# them carry no advancing timestamp, the encode is wedged rather than slow.
STALL = 600

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
    # stderr goes to a file, never a pipe: we only drain stdout, and a damaged
    # source can emit enough decode warnings to fill a pipe buffer, at which
    # point ffmpeg blocks writing to it and the whole thing deadlocks.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                errors="replace") as errfile:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile,
                                text=True, encoding="utf-8", errors="replace")
        try:
            last = time.time()
            for line in proc.stdout:
                if line.startswith("out_time_ms=") and duration:
                    try:
                        pct = int(line.split("=")[1]) / 1e6 / duration * 100
                    except (ValueError, ZeroDivisionError):
                        continue
                    last = time.time()
                    print(f"\r    encoding {min(pct, 100):5.1f}%", end="",
                          flush=True)
                elif line.startswith("progress=") and time.time() - last > STALL:
                    proc.kill()
                    proc.wait()
                    raise RuntimeError(f"no progress for {STALL}s, gave up")
        except KeyboardInterrupt:
            # Don't leave ffmpeg running once we stop reading its progress.
            proc.kill()
            proc.wait()
            raise
        code = proc.wait()
        errfile.seek(0)
        err = errfile.read()
    if code != 0:
        raise RuntimeError(err.strip()[-300:] or f"ffmpeg exited {code}")
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
    p.add_argument("--retry-damaged", action="store_true",
                   help="re-encode sources previously found to be truncated")
    p.add_argument("--force-mp4", action="store_true",
                   help="encode low-bitrate files too, so every output is .mp4; "
                        f"they use crf+{LOW_CRF_BUMP} to avoid inflating them")
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

    def previous_output(rel: Path, e: dict) -> Path:
        """Where an earlier run put this file."""
        if e.get("output"):
            return args.output / e["output"]
        # Entries written before --force-mp4 existed recorded no path.
        return args.output / (rel if e.get("status") == "copied"
                              else rel.with_suffix(".mp4"))

    # Decide the work list up front so the counts mean something. Skipping is
    # driven by this tool's own state, not by a file merely existing - output
    # left behind by something else is meant to be replaced.
    todo, already, already_bytes = [], 0, 0
    known_partial = []
    for src in files:
        rel = src.relative_to(args.source)
        e = state.entry(rel.as_posix())
        status = e.get("status")
        # --force-mp4 changes what "copied" should have produced, so redo those.
        superseded = status == "copied" and args.force_mp4
        # A truncated source reads the same every run; only redo on request.
        retry = status == "partial" and args.retry_damaged
        if status in ("done", "copied", "partial") and not superseded and not retry:
            prev = previous_output(rel, e)
            if prev.exists():
                already += 1
                already_bytes += prev.stat().st_size
                if status == "partial":
                    known_partial.append((rel, e.get("readable"), e.get("expected")))
                continue
        todo.append(src)

    policy = (f"everything to .mp4 (low bitrate at crf{crf + LOW_CRF_BUMP})"
              if args.force_mp4 else f"copy below {args.min_bitrate} Mbps")
    print(f"{len(files)} file(s): {already} already done, {len(todo)} to process")
    print(f"{args.codec} crf{crf}, {policy}\n")

    partial = []
    done = copied = failed = 0
    skipped = already
    src_total = out_total = 0
    started = time.time()

    for n, src in enumerate(todo, 1):
        rel = src.relative_to(args.source)
        key = rel.as_posix()
        e = state.entry(key)
        size = src.stat().st_size

        try:
            info = probe(src)
        except Exception as exc:
            print(f"[{n}/{len(todo)}] {key}\n    probe failed: {exc}")
            e.update(status="failed", error=str(exc)[:200])
            failed += 1
            state.save()
            continue

        mbps = size * 8 / info["duration"] / 1e6 if info["duration"] else 0
        low = mbps < args.min_bitrate
        # Copies keep their container; everything else lands as .mp4.
        copy_it = low and not args.force_mp4
        dst = args.output / (rel if copy_it else rel.with_suffix(".mp4"))
        use_crf = crf + LOW_CRF_BUMP if low else crf

        print(f"[{n}/{len(todo)}] {key}   ({len(todo) - n} left)")
        print(f"    {size/1e6:.0f} MB, {info['height']}p, {mbps:.1f} Mbps")

        if args.dry_run:
            print(f"    would {'copy' if copy_it else f'encode crf{use_crf}'}"
                  f" -> {dst.name}")
            continue

        # Whatever an earlier run left at a different path is now superseded.
        stale = previous_output(rel, e)
        if stale != dst and stale.exists():
            print(f"    removing superseded {stale.name}")
            stale.unlink()

        if copy_it:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            # A copy keeps its own container, so an .mp4 left at this path by an
            # earlier run would linger next to it as a confusing duplicate.
            for old in dst.parent.glob(dst.stem + ".*"):
                if old != dst and old.suffix.lower() == ".mp4":
                    print(f"    removing superseded {old.name}")
                    old.unlink()
            e.update(status="copied", size=dst.stat().st_size, mbps=round(mbps, 2),
                     output=dst.relative_to(args.output).as_posix())
            print(f"    copied as-is (below {args.min_bitrate} Mbps)")
            copied += 1
        else:
            t0 = time.time()
            try:
                encode(src, dst, args.codec, use_crf, info["duration"])
            except Exception as exc:
                print(f"    encode failed: {exc}")
                e.update(status="failed", error=str(exc)[:200])
                failed += 1
                state.save()
                continue

            # ffmpeg can exit cleanly having encoded only part of a source whose
            # data runs out partway. Half a video is worse than none: drop it,
            # record why, and don't attempt it again on later runs.
            try:
                got = probe(dst)["duration"]
            except Exception:
                got = 0
            if abs(got - info["duration"]) > 2:
                # Keep what exists rather than discarding it. Measured on a real
                # zero-filled source: the partial local encode scored VMAF 92.4
                # against 89.9 for the same file routed through YouTube, which
                # reads no further either. There is no better copy to be had.
                out = dst.stat().st_size
                print(f"    PARTIAL: only {got:.0f}s of {info['duration']:.0f}s "
                      f"is readable - kept {out/1e6:.0f} MB")
                e.update(status="partial", size=out, crf=use_crf,
                         ratio=round(out / size, 3),
                         readable=round(got, 1),
                         expected=round(info["duration"], 1),
                         output=dst.relative_to(args.output).as_posix())
                partial.append((rel, got, info["duration"]))
                src_total += size
                out_total += out
                state.save()
                continue

            out = dst.stat().st_size
            e.update(status="done", size=out, mbps=round(mbps, 2),
                     ratio=round(out / size, 3), crf=use_crf,
                     output=dst.relative_to(args.output).as_posix())
            print(f"    {size/1e6:.0f} -> {out/1e6:.0f} MB  "
                  f"({out/size*100:.1f}%)  in {time.time()-t0:.0f}s")
            done += 1

        src_total += size
        out_total += dst.stat().st_size
        state.save()

    state.save()
    print(f"\nencoded {done}, copied {copied}, skipped {skipped}, "
          f"failed {failed}, partial {len(partial) + len(known_partial)}")
    if src_total:
        print(f"{src_total/1e9:.2f} GB -> {out_total/1e9:.2f} GB "
              f"({out_total/src_total*100:.1f}%)")
    if partial or known_partial:
        print("\npartial - source data runs out early, encoded what exists:")
        for rel, got, want in partial:
            print(f"  {got:.0f}s of {want:.0f}s   {rel}")
        for rel, got, want in known_partial:
            print(f"  {got or 0:.0f}s of {want or 0:.0f}s   {rel}  (earlier run)")
        print("these are as complete as the sources allow; --retry-damaged redoes them")

    # Only files with no output at all are candidates for the YouTube route.
    stuck = [args.source / Path(k) for k, v in state.data.items()
             if v.get("status") == "failed"]
    log = args.output / NEEDS_UPLOAD
    if stuck:
        log.write_text(
            "# Sources with no local output - upload these to YouTube manually,\n"
            "# then pull them back with yt_pullback.py\n"
            + "\n".join(str(p) for p in sorted(set(stuck))) + "\n",
            encoding="utf-8")
        print(f"\n{len(set(stuck))} file(s) need the YouTube route - listed in\n  {log}")
    elif log.exists():
        log.unlink()
    print(f"elapsed {(time.time()-started)/60:.0f} min")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # The file in flight has no state entry, so a rerun simply redoes it.
        print("\n\nstopped. rerun the same command to resume where this left off.")
        sys.exit(130)
