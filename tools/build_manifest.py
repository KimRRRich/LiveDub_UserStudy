#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}


def infer_ids(path: Path, video_dir: Path) -> tuple[str, str, str]:
    relative = path.relative_to(video_dir)
    if len(relative.parts) >= 2:
        audio_id = relative.parts[0]
        method = path.stem
        return f"{audio_id}_{method}", audio_id, method

    stem = path.stem
    if "_" not in stem:
        return stem, stem, "unknown"
    audio_id, method = stem.rsplit("_", 1)
    return stem, audio_id, method


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build videos.json from videos/<audio_id>/<method>.mp4 or videos/<audio_id>_<method>.mp4"
    )
    parser.add_argument("--video-dir", default="videos")
    parser.add_argument("--output", default="videos.json")
    args = parser.parse_args()

    video_dir = Path(args.video_dir)
    items = []
    for path in sorted(video_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in VIDEO_EXTS:
            continue
        video_id, audio_id, method = infer_ids(path, video_dir)
        relative_url = path.relative_to(video_dir).as_posix()
        items.append(
            {
                "id": video_id,
                "audio_id": audio_id,
                "method": method,
                "url": f"/videos/{relative_url}",
            }
        )

    Path(args.output).write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(items)} videos to {args.output}")


if __name__ == "__main__":
    main()
