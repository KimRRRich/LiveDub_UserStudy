#!/usr/bin/env python3
"""Batch Mamba3DWithTeeth inference from precomputed [T,103] motion tensors.

The output layout follows the existing user-study videos:
  videos/{sample_id}/{sample_id}_Mamba3DWithTeeth_selfref_A2M_cross_{source_id}.mp4

This script does not load or run ARTalk.
"""

from __future__ import annotations

import argparse
import json
import os
import os.path as osp
import re
import time
import types
from pathlib import Path

import cv2
import torch
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data" / "celebv_crossaudio"
PRED_DIR = DATA_ROOT / "tmp_pred_parms"
VIDEOS_ROOT = PROJECT_ROOT / "videos"
REF_SCRIPT = Path("/mnt/nvme/kimi/work/avatar2026/flame2pixel/batch_infer_Mamba3DWithTeeth_crossaudio_clean20.py")
METHOD_NAME = "Mamba3DWithTeeth_selfref_A2M"


def load_reference_module():
    module = types.ModuleType("mamba3d_crossaudio_ref")
    module.__file__ = str(REF_SCRIPT)
    source = REF_SCRIPT.read_text(encoding="utf-8")
    filtered_lines = []
    for line in source.splitlines():
        if line.startswith("from core.trainer.inferencer import "):
            continue
        if line.startswith("from core.libs.utils import "):
            continue
        filtered_lines.append(line)
    code = compile("\n".join(filtered_lines), str(REF_SCRIPT), "exec")
    exec(code, module.__dict__)
    return module


def sample_sort_key(path: Path) -> tuple[int, int | str]:
    name = path.name
    return (0, int(name)) if name.isdigit() else (1, name)


def load_cross_source_map(data_root: Path, videos_root: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for name in ("infer_audio_manifest.json", "forward_completion_manifest.json"):
        path = data_root / name
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for item in json.load(f):
                sample_id = str(item["sample_id"])
                source_id = item.get("infer_audio_source_id")
                if source_id is not None:
                    mapping[sample_id] = str(source_id)

    pattern = re.compile(r"^(\d+)_Mamba3DWithTeeth_selfref_ARTalk_cross_(\d+)\.mp4$")
    for path in videos_root.glob("*/*Mamba3DWithTeeth_selfref_ARTalk_cross_*.mp4"):
        match = pattern.match(path.name)
        if match:
            mapping.setdefault(match.group(1), match.group(2))
    return mapping


def list_samples(data_root: Path, pred_dir: Path, selected: list[str] | None) -> list[str]:
    if selected:
        return selected
    samples = []
    for path in sorted(data_root.iterdir(), key=sample_sort_key):
        if not path.is_dir() or path.name == pred_dir.name:
            continue
        pred_path = pred_dir / f"{path.name}_pred_a2m_parms.pt"
        if pred_path.exists():
            samples.append(path.name)
    return samples


def make_pred103_tp(ref, tp_orig: dict, pred103: torch.Tensor) -> tuple[dict, int]:
    if pred103.ndim != 2 or pred103.shape[1] != 103:
        raise ValueError(f"Expected [T,103] pred tensor, got {tuple(pred103.shape)}")

    n_total = min(
        int(pred103.shape[0]),
        int(tp_orig["exp_code"].shape[0]),
        int(tp_orig["flame_pose_params"].shape[0]),
    )
    tp = ref.clone_tp(tp_orig)
    exp_code = tp["exp_code"].clone()
    jaw_poses = tp["flame_pose_params"].clone()
    pred103 = pred103[:n_total].to(exp_code.dtype)

    exp_code[:n_total, :100] = pred103[:, :100]
    jaw_poses[:n_total, :3] = pred103[:, 100:103].to(jaw_poses.dtype)
    tp["exp_code"] = exp_code
    tp["flame_pose_params"] = jaw_poses
    return tp, n_total


def read_video_frames(video_path: Path, n_total: int) -> list:
    cap = cv2.VideoCapture(str(video_path))
    frames = []
    for _ in range(n_total):
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    cap.release()
    return frames


def video_frame_count(video_path: Path) -> int:
    cap = cv2.VideoCapture(str(video_path))
    count = int(round(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
    cap.release()
    return count


def audio_frame_count(audio_path: Path, fps: int) -> int:
    import torchaudio

    info = torchaudio.info(str(audio_path))
    return int(info.num_frames * fps / info.sample_rate)


def write_video_atomic(ref, frames_bgr, audio_path: Path, out_path: Path, fps: int) -> None:
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp.mp4")
    if tmp_path.exists():
        tmp_path.unlink()
    ref.write_video_ffmpeg(frames_bgr, str(audio_path), str(tmp_path), fps=fps)
    os.replace(tmp_path, out_path)


def maybe_write_reference_image(sample_dir: Path, out_dir: Path) -> None:
    out_path = out_dir / "reference.jpg"
    if out_path.exists():
        return
    frame_index = 0
    ref_json = sample_dir / "reference_frames.json"
    if ref_json.exists():
        try:
            with ref_json.open("r", encoding="utf-8") as f:
                data = json.load(f)
            first_value = next(iter(data.values()))
            if isinstance(first_value, list) and first_value:
                frame_index = int(first_value[0])
            elif isinstance(first_value, int):
                frame_index = int(first_value)
        except Exception:
            frame_index = 0

    video_path = sample_dir / "crop_512" / "512.mp4"
    cap = cv2.VideoCapture(str(video_path))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_index))
    ok, frame = cap.read()
    cap.release()
    if ok:
        x1, y1, x2, y2 = (64, 96, 448, 480)
        cv2.imwrite(str(out_path), frame[y1:y2, x1:x2])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch_size", type=int, default=4)
    parser.add_argument("--samples", nargs="*", help="Optional sample ids to process")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--output_root", default=str(VIDEOS_ROOT))
    parser.add_argument("--recompute", action="store_true", help="Recompute latent caches")
    parser.add_argument("--max_frames", type=int, default=0, help="Debug cap; 0 means no extra cap")
    args = parser.parse_args()

    ref = load_reference_module()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    with open(ref.CONFIG_PATH, "r", encoding="utf-8") as f:
        conf = yaml.safe_load(f)
    cfg = {**conf["base"], **conf["train"]}
    cfg["public_model_base_dir"] = ref.PUBLIC_MODEL_DIR
    cfg["backbone"]["avatar_model_path"] = ref.PUBLIC_MODEL_DIR
    cfg["backbone"]["assets_path"] = osp.join(ref.PUBLIC_MODEL_DIR, "assets")
    cfg["backbone"]["vae_ft_mse_path"] = osp.join(ref.CHECKPOINTS_ROOT, "sd-vae-ft-mse")
    win_size = cfg["dataset"]["win_size"]

    print("Building Mamba3DWithTeeth...")
    flame_model, vae, wav2lip, refine, renderer, bufs, mask_expand = ref.build_models(cfg, win_size, device)

    output_root = Path(args.output_root)
    mapping = load_cross_source_map(DATA_ROOT, VIDEOS_ROOT)
    samples = list_samples(DATA_ROOT, PRED_DIR, args.samples)
    print(f"Found {len(samples)} samples. Results -> {output_root}")

    total_frames = 0
    total_time = 0.0
    for index, sample_id in enumerate(samples, start=1):
        sample_dir = DATA_ROOT / sample_id
        pred_path = PRED_DIR / f"{sample_id}_pred_a2m_parms.pt"
        video_path = sample_dir / "crop_512" / "512.mp4"
        audio_path = sample_dir / "infer_audio.wav"
        source_id = mapping.get(sample_id, "unknown")
        out_dir = output_root / sample_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{sample_id}_{METHOD_NAME}_cross_{source_id}.mp4"

        print(f"[{index:2d}/{len(samples)}] DST={sample_id} SRC={source_id}")
        if not pred_path.exists():
            print(f"  [SKIP] missing pred tensor: {pred_path}")
            continue
        if not video_path.exists():
            print(f"  [SKIP] missing dst video: {video_path}")
            continue
        if not audio_path.exists():
            print(f"  [SKIP] missing infer audio: {audio_path}")
            continue
        if out_path.exists() and not args.overwrite:
            ref.add_audio_to_video(str(out_path), str(audio_path))
            if ref.video_ready_with_audio(str(out_path)):
                print("  [SKIP] output exists with audio")
                maybe_write_reference_image(sample_dir, out_dir)
                continue

        t0 = time.time()
        tp_orig = torch.load(sample_dir / "tracking" / "track_params.pt", weights_only=False, map_location="cpu")
        pred103 = torch.load(pred_path, weights_only=False, map_location="cpu")
        tp_pred, n_pred = make_pred103_tp(ref, tp_orig, pred103)

        n_video = video_frame_count(video_path)
        n_audio = audio_frame_count(audio_path, ref.FPS)
        n_total = min(n_pred, n_video, n_audio)
        if args.max_frames > 0:
            n_total = min(n_total, args.max_frames)
        if n_total <= 0:
            print(f"  [SKIP] empty duration (pred={n_pred}, video={n_video}, audio={n_audio})")
            continue
        print(f"  T={n_total} (pred={n_pred}, video={n_video}, audio={n_audio})")

        frames_np = read_video_frames(video_path, n_total)
        if len(frames_np) != n_total:
            n_total = len(frames_np)
            frames_np = frames_np[:n_total]
            print(f"  [WARN] video decode stopped early; T={n_total}")
        if n_total <= 0:
            continue

        print("  [1/3] Precomputing GT latents...")
        masked_latents, ref_latents = ref.precompute_gt_latents(
            str(sample_dir),
            n_total,
            frames_np,
            tp_orig,
            flame_model,
            renderer,
            bufs,
            mask_expand,
            vae,
            device,
            args.recompute,
        )

        print("  [2/3] Mamba3DWithTeeth inference (pred103 coeffs)...")
        frames_bgr = ref.run_variant(
            tp_pred,
            masked_latents,
            ref_latents,
            frames_np,
            n_total,
            flame_model,
            vae,
            wav2lip,
            refine,
            renderer,
            bufs,
            mask_expand,
            win_size,
            device,
            args.batch_size,
        )

        print("  [3/3] Writing video...")
        write_video_atomic(ref, frames_bgr, audio_path, out_path, ref.FPS)
        ref.add_audio_to_video(str(out_path), str(audio_path))
        maybe_write_reference_image(sample_dir, out_dir)

        elapsed = time.time() - t0
        total_frames += n_total
        total_time += elapsed
        print(f"  [OK] {out_path.name} ({elapsed:.1f}s, {n_total / elapsed:.1f} fps)\n")

    print("=" * 60)
    print(f"Done. Processed {len(samples)} samples, {total_frames} frames")
    if total_time > 0:
        print(f"Overall avg fps: {total_frames / total_time:.1f}")
    print(f"Results: {output_root}")
    print("=" * 60)


if __name__ == "__main__":
    main()
