#!/usr/bin/env python3
"""Evaluate full_videos with the standard SyncNet face-crop pipeline on CPU."""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


METHOD_PATTERNS = {
    "A2M": "{sid}_Mamba3DWithTeeth_selfref_A2M_cross_*.mp4",
    "ARTalk": "{sid}_Mamba3DWithTeeth_selfref_ARTalk_cross_*.mp4",
    "KeySync": "{sid}_KeySync_cross_*.mp4",
    "LatentSync": "{sid}_LatentSync_cross_*.mp4",
    "GTFLAME103": "{sid}_Mamba3DWithTeeth_selfref_GTFLAME103_cross_*.mp4",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full_videos", default="/home/kim/work/LiveDub_UserStudy/data/full_videos")
    parser.add_argument("--syncnet_dir", default="/mnt/nvme/kimi/work/avatar2026/flame2pixel/evaluate/syncnet_python")
    parser.add_argument("--out_dir", default="/home/kim/work/LiveDub_UserStudy/data/syncnet_full_videos_cpu_pipeline")
    parser.add_argument("--methods", nargs="+", default=list(METHOD_PATTERNS))
    parser.add_argument("--samples", nargs="*", default=None)
    parser.add_argument("--batch_size", type=int, default=20)
    parser.add_argument("--vshift", type=int, default=15)
    parser.add_argument("--facedet_scale", type=float, default=0.25)
    parser.add_argument("--min_track", type=int, default=40)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--keep_pipeline_work", action="store_true")
    return parser.parse_args()


def make_cpu_pipeline(syncnet_dir, out_dir):
    src = Path(syncnet_dir) / "run_pipeline.py"
    text = src.read_text()
    text = text.replace("DET = S3FD(device='cuda')", "DET = S3FD(device='cpu')")
    replacement = """class _SceneFrame:
  def __init__(self, frame_num):
    self.frame_num = frame_num
scene = [(_SceneFrame(0), _SceneFrame(len(faces)))]
print('scene_detect skipped: using full video as one scene')
"""
    text = text.replace("scene = scene_detect(opt)", replacement)
    dst = Path(out_dir) / "run_pipeline_syncnet_cpu_noscene.py"
    dst.write_text(text)
    return dst


def patch_torch_cpu():
    import torch

    torch.nn.Module.cuda = lambda self, *args, **kwargs: self
    torch.Tensor.cuda = lambda self, *args, **kwargs: self


def load_syncnet(syncnet_dir):
    os.chdir(syncnet_dir)
    sys.path.insert(0, syncnet_dir)
    patch_torch_cpu()
    from SyncNetInstance_calc_scores import SyncNetInstance

    model = SyncNetInstance()
    model.loadParameters("data/syncnet_v2.model")
    return model


def list_jobs(full_videos, methods, samples):
    root = Path(full_videos)
    sample_dirs = [p for p in root.iterdir() if p.is_dir()]
    if samples:
        wanted = set(samples)
        sample_dirs = [p for p in sample_dirs if p.name in wanted]
    jobs = []
    for sample_dir in sorted(sample_dirs, key=lambda p: int(p.name)):
        sid = sample_dir.name
        for method in methods:
            pattern = METHOD_PATTERNS[method].format(sid=sid)
            matches = sorted(sample_dir.glob(pattern))
            if len(matches) != 1:
                jobs.append({"sample_id": sid, "method": method, "error": f"video_count={len(matches)}", "video": ""})
                continue
            jobs.append({"sample_id": sid, "method": method, "video": str(matches[0])})
    return jobs


def read_existing(csv_path):
    if not Path(csv_path).exists():
        return {}
    rows = {}
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            rows[(row["sample_id"], row["method"])] = row
    return rows


def append_row(csv_path, row):
    fieldnames = [
        "sample_id",
        "method",
        "video",
        "reference",
        "crop_count",
        "selected_crop",
        "offset",
        "confidence",
        "min_distance",
        "error",
    ]
    exists = Path(csv_path).exists()
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fieldnames})


def clean_pipeline_reference(data_dir, reference):
    for sub in ["pyavi", "pyframes", "pywork"]:
        path = Path(data_dir) / sub / reference
        if path.exists():
            shutil.rmtree(path)


def run_pipeline(python_exe, pipeline_script, syncnet_dir, data_dir, video, reference, opt, log_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = syncnet_dir + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        python_exe,
        str(pipeline_script),
        "--videofile",
        video,
        "--reference",
        reference,
        "--data_dir",
        str(data_dir),
        "--facedet_scale",
        str(opt.facedet_scale),
        "--min_track",
        str(opt.min_track),
    ]
    with open(log_path, "w") as log:
        return subprocess.run(cmd, cwd=syncnet_dir, env=env, stdout=log, stderr=subprocess.STDOUT)


def evaluate_crops(model, syncnet_dir, out_dir, reference, crop_files, opt):
    results = []
    score_tmp = Path(out_dir) / "score_tmp"
    for crop_file in crop_files:
        score_ref = f"{reference}_{Path(crop_file).stem}"
        score_opt = SimpleNamespace(
            batch_size=opt.batch_size,
            vshift=opt.vshift,
            tmp_dir=str(score_tmp),
            reference=score_ref,
        )
        os.chdir(syncnet_dir)
        offset, confidence, min_distance = model.evaluate(score_opt, videofile=str(crop_file))
        results.append(
            {
                "crop": str(crop_file),
                "offset": float(offset),
                "confidence": float(confidence),
                "min_distance": float(min_distance),
            }
        )
    if score_tmp.exists():
        shutil.rmtree(score_tmp)
    return results


def write_json_outputs(out_dir, rows, track_rows):
    valid = [r for r in rows if not r.get("error")]
    summary = {}
    for method in sorted({r["method"] for r in rows}):
        mrows = [r for r in valid if r["method"] == method]
        if not mrows:
            summary[method] = {"n": 0}
            continue
        conf = [float(r["confidence"]) for r in mrows]
        dist = [float(r["min_distance"]) for r in mrows]
        offset = [abs(float(r["offset"])) for r in mrows]
        summary[method] = {
            "n": len(mrows),
            "mean_confidence": sum(conf) / len(conf),
            "mean_min_distance": sum(dist) / len(dist),
            "mean_abs_offset": sum(offset) / len(offset),
        }
    out = Path(out_dir)
    (out / "scores.json").write_text(json.dumps(rows, indent=2) + "\n")
    (out / "track_scores.json").write_text(json.dumps(track_rows, indent=2) + "\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    opt = parse_args()
    out_dir = Path(opt.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pipeline_data = out_dir / "pipeline_work"
    logs_dir = out_dir / "logs"
    pipeline_data.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    for method in opt.methods:
        if method not in METHOD_PATTERNS:
            raise ValueError(f"unknown method: {method}")

    csv_path = out_dir / "scores.csv"
    if opt.overwrite and csv_path.exists():
        csv_path.unlink()
    existing = read_existing(csv_path)
    jobs = list_jobs(opt.full_videos, opt.methods, opt.samples)
    pipeline_script = make_cpu_pipeline(opt.syncnet_dir, out_dir)
    model = load_syncnet(opt.syncnet_dir)

    track_rows = []
    for idx, job in enumerate(jobs, 1):
        key = (job["sample_id"], job["method"])
        if key in existing and not opt.overwrite:
            print(f"[{idx}/{len(jobs)}] skip existing {key[0]} {key[1]}", flush=True)
            continue

        reference = f"{job['sample_id']}_{job['method']}"
        if job.get("error"):
            row = {**job, "reference": reference}
            append_row(csv_path, row)
            print(f"[{idx}/{len(jobs)}] missing {key[0]} {key[1]} {job['error']}", flush=True)
            continue

        print(f"[{idx}/{len(jobs)}] crop+score {key[0]} {key[1]}", flush=True)
        log_path = logs_dir / f"{reference}.log"
        proc = run_pipeline(
            sys.executable,
            pipeline_script,
            opt.syncnet_dir,
            pipeline_data,
            job["video"],
            reference,
            opt,
            log_path,
        )
        crop_dir = pipeline_data / "pycrop" / reference
        crop_files = sorted(crop_dir.glob("*.avi"))
        if proc.returncode != 0 or not crop_files:
            row = {
                **job,
                "reference": reference,
                "crop_count": len(crop_files),
                "error": f"pipeline_failed_rc={proc.returncode}; log={log_path}",
            }
            append_row(csv_path, row)
            continue

        try:
            crop_scores = evaluate_crops(model, opt.syncnet_dir, out_dir, reference, crop_files, opt)
            best = max(crop_scores, key=lambda r: r["confidence"])
            for score in crop_scores:
                track_rows.append({**job, "reference": reference, **score})
            row = {
                **job,
                "reference": reference,
                "crop_count": len(crop_scores),
                "selected_crop": best["crop"],
                "offset": best["offset"],
                "confidence": best["confidence"],
                "min_distance": best["min_distance"],
                "error": "",
            }
            append_row(csv_path, row)
        except Exception as exc:
            row = {
                **job,
                "reference": reference,
                "crop_count": len(crop_files),
                "error": f"score_failed={type(exc).__name__}: {exc}",
            }
            append_row(csv_path, row)
        finally:
            if not opt.keep_pipeline_work:
                clean_pipeline_reference(pipeline_data, reference)

    rows = list(read_existing(csv_path).values())
    summary = write_json_outputs(out_dir, rows, track_rows)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
