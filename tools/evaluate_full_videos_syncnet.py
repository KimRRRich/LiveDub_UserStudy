#!/usr/bin/env python3
"""Evaluate full_videos with SyncNet.

Default methods:
  A2M, ARTalk, KeySync, LatentSync, GTFLAME103

`Original` is intentionally excluded by default because the copied original
crop videos do not carry the target speech audio in this dataset.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import python_speech_features
from scipy import signal
from scipy.io import wavfile
import torch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FULL_VIDEOS = PROJECT_ROOT / "data" / "full_videos"
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "syncnet_full_videos"
SYNCNET_DIR = Path("/mnt/nvme/kimi/work/avatar2026/flame2pixel/evaluate/syncnet_python")
SYNCNET_MODEL = SYNCNET_DIR / "data" / "syncnet_v2.model"

METHOD_PATTERNS = {
    "A2M": "*_Mamba3DWithTeeth_selfref_A2M_cross_*.mp4",
    "ARTalk": "*_Mamba3DWithTeeth_selfref_ARTalk_cross_*.mp4",
    "KeySync": "*_KeySync_cross_*.mp4",
    "LatentSync": "*_LatentSync_cross_*.mp4",
    "GTFLAME103": "*_Mamba3DWithTeeth_selfref_GTFLAME103_cross_*.mp4",
}


def sample_sort_key(path: Path) -> tuple[int, int | str]:
    return (0, int(path.name)) if path.name.isdigit() else (1, path.name)


def build_items(full_videos: Path, methods: list[str]) -> list[dict]:
    items = []
    for sample_dir in sorted([p for p in full_videos.iterdir() if p.is_dir()], key=sample_sort_key):
        for method in methods:
            pattern = METHOD_PATTERNS[method]
            matches = sorted(sample_dir.glob(pattern))
            if not matches:
                continue
            if len(matches) > 1:
                raise RuntimeError(f"Multiple {method} videos for {sample_dir.name}: {matches}")
            items.append({
                "sample_id": sample_dir.name,
                "method": method,
                "video_path": str(matches[0]),
            })
    return items


def to_scalar(value) -> float:
    arr = np.asarray(value)
    return float(arr.reshape(-1)[0])


def calc_pdist(feat1: torch.Tensor, feat2: torch.Tensor, vshift: int = 10) -> list[torch.Tensor]:
    win_size = vshift * 2 + 1
    feat2p = torch.nn.functional.pad(feat2, (0, 0, vshift, vshift))
    dists = []
    for i in range(len(feat1)):
        dists.append(torch.nn.functional.pairwise_distance(feat1[[i], :].repeat(win_size, 1), feat2p[i:i + win_size, :]))
    return dists


class SyncNetEvaluator:
    def __init__(self, model_path: Path, device: torch.device):
        from SyncNetModel import S

        self.device = device
        self.model = S(num_layers_in_fc_layers=1024).to(device)
        loaded_state = torch.load(str(model_path), map_location=device)
        state = self.model.state_dict()
        for name, param in loaded_state.items():
            state[name].copy_(param)
        self.model.eval()

    @torch.no_grad()
    def evaluate(self, opt: SimpleNamespace, videofile: str) -> tuple[float, float, float]:
        tmp_ref_dir = Path(opt.tmp_dir) / opt.reference
        if tmp_ref_dir.exists():
            shutil.rmtree(tmp_ref_dir)
        tmp_ref_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", videofile, "-threads", "1", "-f", "image2", str(tmp_ref_dir / "%06d.jpg")],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-i",
                videofile,
                "-async",
                "1",
                "-ac",
                "1",
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                str(tmp_ref_dir / "audio.wav"),
            ],
            check=True,
        )

        images = []
        for frame_path in sorted(tmp_ref_dir.glob("*.jpg")):
            img = cv2.imread(str(frame_path))
            if img is None:
                continue
            images.append(cv2.resize(img, (224, 224)))
        if len(images) < 6:
            raise RuntimeError(f"not enough frames: {len(images)}")

        im = np.stack(images, axis=3)
        im = np.expand_dims(im, axis=0)
        im = np.transpose(im, (0, 3, 4, 1, 2))
        imtv = torch.from_numpy(im.astype(float)).float()

        sample_rate, audio = wavfile.read(str(tmp_ref_dir / "audio.wav"))
        mfcc = zip(*python_speech_features.mfcc(audio, sample_rate))
        mfcc = np.stack([np.array(i) for i in mfcc])
        cc = np.expand_dims(np.expand_dims(mfcc, axis=0), axis=0)
        cct = torch.from_numpy(cc.astype(float)).float()

        min_length = min(len(images), math.floor(len(audio) / 640))
        lastframe = min_length - 5
        if lastframe <= 0:
            raise RuntimeError(f"not enough synchronized frames/audio: min_length={min_length}")

        im_feat = []
        cc_feat = []
        for i in range(0, lastframe, opt.batch_size):
            end = min(lastframe, i + opt.batch_size)
            im_batch = [imtv[:, :, vframe:vframe + 5, :, :] for vframe in range(i, end)]
            im_in = torch.cat(im_batch, 0).to(self.device)
            im_out = self.model.forward_lip(im_in)
            im_feat.append(im_out.cpu())

            cc_batch = [cct[:, :, :, vframe * 4:vframe * 4 + 20] for vframe in range(i, end)]
            cc_in = torch.cat(cc_batch, 0).to(self.device)
            cc_out = self.model.forward_aud(cc_in)
            cc_feat.append(cc_out.cpu())

        im_feat = torch.cat(im_feat, 0)
        cc_feat = torch.cat(cc_feat, 0)

        dists = calc_pdist(im_feat, cc_feat, vshift=opt.vshift)
        mdist = torch.mean(torch.stack(dists, 1), 1)
        minval, minidx = torch.min(mdist, 0)
        offset = opt.vshift - minidx
        conf = torch.median(mdist) - minval

        # Keep parity with the original implementation, including the same min-distance definition.
        _fdist = np.stack([dist[minidx].numpy() for dist in dists])
        _fconf = torch.median(mdist).numpy() - _fdist
        _ = signal.medfilt(_fconf, kernel_size=9)

        return float(offset), float(conf), float(minval)


def load_existing(csv_path: Path) -> dict[tuple[str, str], dict]:
    if not csv_path.exists():
        return {}
    rows = {}
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("status") == "ok":
                rows[(row["sample_id"], row["method"])] = row
    return rows


def write_outputs(rows: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "scores.csv"
    json_path = out_dir / "scores.json"
    summary_path = out_dir / "summary.json"

    fieldnames = [
        "sample_id",
        "method",
        "video_path",
        "offset",
        "confidence",
        "min_distance",
        "status",
        "error",
        "elapsed_sec",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    by_method = defaultdict(list)
    for row in rows:
        if row["status"] == "ok":
            by_method[row["method"]].append(row)

    summary = {}
    for method, method_rows in sorted(by_method.items()):
        conf = np.array([float(row["confidence"]) for row in method_rows], dtype=np.float64)
        dist = np.array([float(row["min_distance"]) for row in method_rows], dtype=np.float64)
        off = np.array([float(row["offset"]) for row in method_rows], dtype=np.float64)
        summary[method] = {
            "count": len(method_rows),
            "mean_confidence": float(conf.mean()),
            "median_confidence": float(np.median(conf)),
            "mean_min_distance": float(dist.mean()),
            "median_min_distance": float(np.median(dist)),
            "mean_abs_offset": float(np.abs(off).mean()),
            "median_abs_offset": float(np.median(np.abs(off))),
        }

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full_videos", type=Path, default=DEFAULT_FULL_VIDEOS)
    parser.add_argument("--out_dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--tmp_dir", type=Path, default=Path("/tmp/livedub_syncnet_eval"))
    parser.add_argument("--methods", nargs="+", default=list(METHOD_PATTERNS))
    parser.add_argument("--batch_size", type=int, default=20)
    parser.add_argument("--vshift", type=int, default=15)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    bad_methods = [method for method in args.methods if method not in METHOD_PATTERNS]
    if bad_methods:
        raise ValueError(f"Unknown methods: {bad_methods}. Valid: {sorted(METHOD_PATTERNS)}")
    if not SYNCNET_MODEL.exists():
        raise FileNotFoundError(SYNCNET_MODEL)
    if args.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but no CUDA device is visible.")

    sys.path.insert(0, str(SYNCNET_DIR))

    items = build_items(args.full_videos, args.methods)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    existing = load_existing(args.out_dir / "scores.csv") if args.resume else {}

    opt = SimpleNamespace(
        initial_model=str(SYNCNET_MODEL),
        batch_size=args.batch_size,
        vshift=args.vshift,
        tmp_dir=str(args.tmp_dir),
        reference="livedub_syncnet",
    )

    device = torch.device(args.device)
    print(f"Loading SyncNet: {SYNCNET_MODEL} on {device}")
    model = SyncNetEvaluator(SYNCNET_MODEL, device=device)
    print(f"Evaluating {len(items)} videos -> {args.out_dir}")

    rows = []
    for index, item in enumerate(items, start=1):
        key = (item["sample_id"], item["method"])
        if key in existing:
            row = dict(existing[key])
            rows.append(row)
            print(f"[{index:3d}/{len(items)}] SKIP {item['sample_id']} {item['method']}")
            continue

        t0 = time.time()
        row = {
            **item,
            "offset": "",
            "confidence": "",
            "min_distance": "",
            "status": "ok",
            "error": "",
            "elapsed_sec": "",
        }
        try:
            offset, conf, dist = model.evaluate(opt, videofile=item["video_path"])
            row["offset"] = f"{to_scalar(offset):.6f}"
            row["confidence"] = f"{to_scalar(conf):.6f}"
            row["min_distance"] = f"{to_scalar(dist):.6f}"
            row["elapsed_sec"] = f"{time.time() - t0:.3f}"
            print(
                f"[{index:3d}/{len(items)}] {item['sample_id']} {item['method']}: "
                f"offset={row['offset']} conf={row['confidence']} dist={row['min_distance']}"
            )
        except Exception as exc:
            row["status"] = "error"
            row["error"] = repr(exc)
            row["elapsed_sec"] = f"{time.time() - t0:.3f}"
            print(f"[{index:3d}/{len(items)}] ERROR {item['sample_id']} {item['method']}: {exc}")
        rows.append(row)
        write_outputs(rows, args.out_dir)

    write_outputs(rows, args.out_dir)
    print(f"Wrote {args.out_dir / 'scores.csv'}")
    print(f"Wrote {args.out_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
