import csv
import json
import os
import random
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "study.db"))
MANIFEST_PATH = Path(os.getenv("VIDEO_MANIFEST", BASE_DIR / "videos.json"))
VIDEO_DIR = Path(os.getenv("VIDEO_DIR", BASE_DIR / "videos"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

SCORE_FIELDS = ("visual_quality", "occlusion", "lip_sync", "teeth_quality")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_manifest() -> list[dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return []
    with MANIFEST_PATH.open("r", encoding="utf-8") as f:
        videos = json.load(f)
    seen: set[str] = set()
    for item in videos:
        for key in ("id", "audio_id", "method", "url"):
            if key not in item:
                raise RuntimeError(f"videos.json item is missing {key}: {item}")
        if item["id"] in seen:
            raise RuntimeError(f"Duplicate video id in videos.json: {item['id']}")
        seen.add(item["id"])
    return videos


def public_video(item: dict[str, Any]) -> dict[str, str]:
    return {
        "id": item["id"],
        "audio_id": item["audio_id"],
        "url": item["url"],
    }


def group_manifest(manifest: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in manifest:
        groups.setdefault(item["audio_id"], []).append(item)
    for videos in groups.values():
        videos.sort(key=lambda item: item["method"])
    return dict(sorted(groups.items()))


def create_order(manifest: list[dict[str, Any]]) -> dict[str, Any]:
    rng = random.SystemRandom()
    grouped = group_manifest(manifest)
    sample_ids = list(grouped)
    rng.shuffle(sample_ids)
    video_order: dict[str, list[str]] = {}
    for sample_id, videos in grouped.items():
        ids = [item["id"] for item in videos]
        rng.shuffle(ids)
        video_order[sample_id] = ids
    return {"samples": sample_ids, "videos": video_order}


def normalize_order(raw_order: Any, manifest: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = group_manifest(manifest)
    video_to_audio = {item["id"]: item["audio_id"] for item in manifest}
    all_video_ids = {item["id"] for item in manifest}
    all_sample_ids = set(grouped)

    if isinstance(raw_order, dict):
        samples = [sample for sample in raw_order.get("samples", []) if sample in all_sample_ids]
        methods = raw_order.get("videos", {})
        video_order = {
            sample: [video_id for video_id in methods.get(sample, []) if video_id in all_video_ids]
            for sample in samples
        }
    elif isinstance(raw_order, list):
        samples = []
        video_order = {}
        for item in raw_order:
            sample = item if item in all_sample_ids else video_to_audio.get(item)
            if sample and sample not in samples:
                samples.append(sample)
        for sample in samples:
            video_order[sample] = [item["id"] for item in grouped[sample]]
    else:
        samples = []
        video_order = {}

    for sample in all_sample_ids:
        if sample not in samples:
            samples.append(sample)
        known = set(video_order.get(sample, []))
        missing = [item["id"] for item in grouped[sample] if item["id"] not in known]
        video_order[sample] = video_order.get(sample, []) + missing

    return {"samples": samples, "videos": video_order}


def public_groups(order: dict[str, Any], manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manifest_by_id = {item["id"]: item for item in manifest}
    groups = []
    for sample_id in order["samples"]:
        videos = []
        for index, video_id in enumerate(order["videos"].get(sample_id, []), start=1):
            item = manifest_by_id.get(video_id)
            if item:
                public = public_video(item)
                public["label"] = f"视频 {index}"
                videos.append(public)
        if videos:
            groups.append({"audio_id": sample_id, "videos": videos})
    return groups


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS participants (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                order_json TEXT NOT NULL,
                user_agent TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ratings (
                participant_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                audio_id TEXT NOT NULL,
                method TEXT NOT NULL,
                visual_quality INTEGER NOT NULL CHECK (visual_quality BETWEEN 1 AND 5),
                occlusion INTEGER NOT NULL CHECK (occlusion BETWEEN 1 AND 5),
                lip_sync INTEGER NOT NULL CHECK (lip_sync BETWEEN 1 AND 5),
                teeth_quality INTEGER NOT NULL CHECK (teeth_quality BETWEEN 1 AND 5),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (participant_id, video_id),
                FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE
            )
            """
        )


class ParticipantIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    participant_id: str | None = None


class RatingIn(BaseModel):
    participant_id: str
    video_id: str
    visual_quality: int = Field(ge=1, le=5)
    occlusion: int = Field(ge=1, le=5)
    lip_sync: int = Field(ge=1, le=5)
    teeth_quality: int = Field(ge=1, le=5)


class CompleteIn(BaseModel):
    participant_id: str


app = FastAPI(title="User Study Rating Server")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
if VIDEO_DIR.exists():
    app.mount("/videos", StaticFiles(directory=VIDEO_DIR), name="videos")


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "videos": len(load_manifest())}


@app.get("/api/videos")
def videos() -> dict[str, Any]:
    manifest = load_manifest()
    grouped = group_manifest(manifest)
    groups = [
        {
            "audio_id": audio_id,
            "videos": [
                {**public_video(video), "label": f"视频 {index}"}
                for index, video in enumerate(videos, start=1)
            ],
        }
        for audio_id, videos in grouped.items()
    ]
    return {"groups": groups, "count": len(manifest), "sample_count": len(groups)}


def fetch_existing_participant(conn: sqlite3.Connection, participant_id: str | None, username: str) -> sqlite3.Row | None:
    if participant_id:
        row = conn.execute(
            "SELECT * FROM participants WHERE id = ? AND username = ?",
            (participant_id, username),
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT * FROM participants WHERE username = ?",
        (username,),
    ).fetchone()


def participant_payload(conn: sqlite3.Connection, participant: sqlite3.Row) -> dict[str, Any]:
    manifest = load_manifest()
    order = normalize_order(json.loads(participant["order_json"]), manifest)
    ratings = conn.execute(
        """
        SELECT video_id, visual_quality, occlusion, lip_sync, teeth_quality, updated_at
        FROM ratings
        WHERE participant_id = ?
        """,
        (participant["id"],),
    ).fetchall()
    return {
        "participant": {
            "id": participant["id"],
            "username": participant["username"],
            "completed_at": participant["completed_at"],
        },
        "order": order,
        "groups": public_groups(order, manifest),
        "ratings": {
            row["video_id"]: {field: row[field] for field in SCORE_FIELDS}
            for row in ratings
        },
    }


@app.post("/api/participants")
def upsert_participant(data: ParticipantIn, request: Request) -> dict[str, Any]:
    username = " ".join(data.username.strip().split())
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    manifest = load_manifest()
    if not manifest:
        raise HTTPException(status_code=400, detail="videos.json has no videos")

    with connect() as conn:
        existing = fetch_existing_participant(conn, data.participant_id, username)
        if existing:
            conn.execute(
                "UPDATE participants SET updated_at = ? WHERE id = ?",
                (utc_now(), existing["id"]),
            )
            return participant_payload(conn, existing)

        order = create_order(manifest)
        participant_id = str(uuid.uuid4())
        now = utc_now()
        try:
            conn.execute(
                """
                INSERT INTO participants
                    (id, username, order_json, user_agent, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    participant_id,
                    username,
                    json.dumps(order),
                    request.headers.get("user-agent", ""),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM participants WHERE username = ?",
                (username,),
            ).fetchone()
            if row:
                return participant_payload(conn, row)
            raise

        participant = conn.execute(
            "SELECT * FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()
        return participant_payload(conn, participant)


@app.post("/api/ratings")
def save_rating(data: RatingIn) -> dict[str, Any]:
    manifest_by_id = {item["id"]: item for item in load_manifest()}
    video = manifest_by_id.get(data.video_id)
    if not video:
        raise HTTPException(status_code=404, detail="unknown video_id")

    now = utc_now()
    with connect() as conn:
        participant = conn.execute(
            "SELECT id FROM participants WHERE id = ?",
            (data.participant_id,),
        ).fetchone()
        if not participant:
            raise HTTPException(status_code=404, detail="unknown participant_id")

        existing = conn.execute(
            "SELECT created_at FROM ratings WHERE participant_id = ? AND video_id = ?",
            (data.participant_id, data.video_id),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        conn.execute(
            """
            INSERT INTO ratings (
                participant_id, video_id, audio_id, method,
                visual_quality, occlusion, lip_sync, teeth_quality,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(participant_id, video_id) DO UPDATE SET
                visual_quality = excluded.visual_quality,
                occlusion = excluded.occlusion,
                lip_sync = excluded.lip_sync,
                teeth_quality = excluded.teeth_quality,
                updated_at = excluded.updated_at
            """,
            (
                data.participant_id,
                data.video_id,
                video["audio_id"],
                video["method"],
                data.visual_quality,
                data.occlusion,
                data.lip_sync,
                data.teeth_quality,
                created_at,
                now,
            ),
        )
        conn.execute(
            "UPDATE participants SET updated_at = ? WHERE id = ?",
            (now, data.participant_id),
        )
    return {"ok": True, "updated_at": now}


@app.post("/api/complete")
def complete(data: CompleteIn) -> dict[str, Any]:
    total = len(load_manifest())
    with connect() as conn:
        rated = conn.execute(
            "SELECT COUNT(*) AS count FROM ratings WHERE participant_id = ?",
            (data.participant_id,),
        ).fetchone()["count"]
        if rated < total:
            raise HTTPException(status_code=400, detail=f"{rated}/{total} videos rated")
        now = utc_now()
        conn.execute(
            "UPDATE participants SET completed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, data.participant_id),
        )
    return {"ok": True, "completed_at": now}


def check_admin_token(token_query: str | None, token_header: str | None) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="ADMIN_TOKEN is not configured")
    if token_query != ADMIN_TOKEN and token_header != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")


@app.get("/api/export.csv")
def export_csv(
    token: str | None = Query(default=None),
    x_admin_token: str | None = Header(default=None),
) -> StreamingResponse:
    check_admin_token(token, x_admin_token)

    def rows():
        header = [
            "participant_id",
            "username",
            "completed_at",
            "video_id",
            "audio_id",
            "method",
            "visual_quality",
            "occlusion",
            "lip_sync",
            "teeth_quality",
            "rating_created_at",
            "rating_updated_at",
        ]
        buffer: list[str] = []

        class ListWriter:
            def write(self, value: str) -> None:
                buffer.append(value)

        writer = csv.writer(ListWriter())
        writer.writerow(header)
        yield "".join(buffer)
        buffer.clear()

        with connect() as conn:
            for row in conn.execute(
                """
                SELECT
                    p.id AS participant_id,
                    p.username,
                    p.completed_at,
                    r.video_id,
                    r.audio_id,
                    r.method,
                    r.visual_quality,
                    r.occlusion,
                    r.lip_sync,
                    r.teeth_quality,
                    r.created_at AS rating_created_at,
                    r.updated_at AS rating_updated_at
                FROM ratings r
                JOIN participants p ON p.id = r.participant_id
                ORDER BY p.created_at, r.audio_id, r.method
                """
            ):
                writer.writerow([row[key] for key in header])
                yield "".join(buffer)
                buffer.clear()

    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="userstudy_ratings.csv"'},
    )
