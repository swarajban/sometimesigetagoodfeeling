#!/usr/bin/env python3
"""Fetch the freely licensed sample media used for the end-to-end run.

Downloads the originals listed in scripts/sample_media.json, verifies their SHA-1,
and lays them out as a DJI-style footage folder (VP9 stream-copied into MP4, audio
dropped, creation_time set, one clip split in two to mimic a split recording).
Nothing is written inside the repository unless you point --dest there.

    uv run python scripts/fetch_sample.py --dest ~/.capy/work/sample
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

MANIFEST = Path(__file__).with_name("sample_media.json")
USER_AGENT = "goodfeeling-sample-fetch/0.1 (https://github.com/swarajban/sometimesigetagoodfeeling)"


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, expected_sha1: str | None) -> None:
    if dest.exists() and (expected_sha1 is None or sha1(dest) == expected_sha1):
        print(f"ok      {dest.name}")
        return
    print(f"fetch   {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    if expected_sha1 and sha1(tmp) != expected_sha1:
        tmp.unlink()
        sys.exit(f"checksum mismatch for {url}")
    tmp.rename(dest)


def remux(src: Path, outs: list[Path], creation: datetime, split_at: float | None) -> None:
    if all(o.exists() for o in outs):
        print(f"ok      {', '.join(o.name for o in outs)}")
        return
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v:0", "-c", "copy", "-an"]
    if split_at is None:
        cmd = base + ["-movflags", "+faststart", "-metadata", f"creation_time={creation.isoformat()}Z", str(outs[0])]
        subprocess.run(cmd, check=True)
        return
    pattern = outs[0].parent / "_part%d.mp4"
    cmd = base + [
        "-f",
        "segment",
        "-segment_times",
        str(split_at),
        "-reset_timestamps",
        "1",
        "-segment_format_options",
        "movflags=+faststart",
        str(pattern),
    ]
    subprocess.run(cmd, check=True)
    offset = 0.0
    for i, out in enumerate(outs):
        part = outs[0].parent / f"_part{i}.mp4"
        stamp = (creation + timedelta(seconds=offset)).isoformat() + "Z"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(part),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-metadata",
                f"creation_time={stamp}",
                str(out),
            ],
            check=True,
        )
        dur = float(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(part)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        offset += dur
        part.unlink()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, default=Path.home() / ".capy" / "work" / "sample")
    args = ap.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    dest = args.dest.expanduser()
    raw, footage, music = dest / "raw", dest / "footage", dest / "music"
    for d in (raw, footage, music):
        d.mkdir(parents=True, exist_ok=True)

    credits = ["# Sample media credits", "", "Footage (stream-copied into MP4, audio removed, renamed DJI-style):", ""]
    for item in manifest["footage"]:
        src = raw / f"{item['id']}.webm"
        download(item["url"], src, item["sha1"])
        creation = datetime.fromisoformat(item["creation_time"].removesuffix("Z"))
        remux(src, [footage / n for n in item["dji_name"]], creation, item.get("split_at"))
        credits.append(
            f'- {", ".join(item["dji_name"])}: "{item["title"]}" by {item["author"]}, '
            f"{item['license']} ({item['license_url']}), {item['page']}"
        )
    credits += ["", "Music:", ""]
    for item in manifest["music"]:
        download(item["url"], music / item["file"], item.get("sha1"))
        credits.append(
            f'- {item["file"]}: "{item["title"]}" by {item["author"]}, {item["license"]} '
            f"({item['license_url']}), {item['page']}"
        )
    (dest / "CREDITS.md").write_text("\n".join(credits) + "\n")
    print(f"\nfootage: {footage}\nmusic:   {music}\ncredits: {dest / 'CREDITS.md'}")


if __name__ == "__main__":
    main()
