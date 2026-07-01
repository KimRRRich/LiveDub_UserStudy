import csv
import hashlib
import hmac
import json
import os
import random
import secrets
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
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", ADMIN_TOKEN)

SCORE_FIELDS = ("visual_quality", "occlusion", "lip_sync", "teeth_quality")
PASSWORD_ITERATIONS = 120_000
ADMIN_SESSIONS: set[str] = set()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return salt.hex(), digest.hex()


def password_matches(password: str, salt_hex: str, password_hash: str) -> bool:
    _, candidate_hash = hash_password(password, salt_hex)
    return hmac.compare_digest(candidate_hash, password_hash)


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
                password_salt TEXT,
                password_hash TEXT,
                order_json TEXT NOT NULL,
                user_agent TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(participants)").fetchall()
        }
        if "password_salt" not in columns:
            conn.execute("ALTER TABLE participants ADD COLUMN password_salt TEXT")
        if "password_hash" not in columns:
            conn.execute("ALTER TABLE participants ADD COLUMN password_hash TEXT")
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
    password: str = Field(min_length=1, max_length=128)
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
    allow_partial: bool = False


class AdminLoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class AdminUserIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)


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


def fetch_current_participant(conn: sqlite3.Connection, participant_id: str | None, username: str) -> sqlite3.Row | None:
    if participant_id:
        row = conn.execute(
            "SELECT * FROM participants WHERE id = ? AND username = ?",
            (participant_id, username),
        ).fetchone()
        if row:
            return row
    return None


def fetch_participant_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM participants WHERE username = ?",
        (username,),
    ).fetchone()


def set_participant_password(conn: sqlite3.Connection, participant_id: str, password: str) -> None:
    salt, password_hash = hash_password(password)
    conn.execute(
        """
        UPDATE participants
        SET password_salt = ?, password_hash = ?, updated_at = ?
        WHERE id = ?
        """,
        (salt, password_hash, utc_now(), participant_id),
    )


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
    if not data.password.strip():
        raise HTTPException(status_code=400, detail="password is required")
    if username == ADMIN_USERNAME:
        raise HTTPException(status_code=401, detail="该用户名已被注册或者密码错误")

    manifest = load_manifest()
    if not manifest:
        raise HTTPException(status_code=400, detail="videos.json has no videos")

    with connect() as conn:
        current = fetch_current_participant(conn, data.participant_id, username)
        existing = current or fetch_participant_by_username(conn, username)
        if existing:
            if existing["password_salt"] and existing["password_hash"]:
                if not password_matches(data.password, existing["password_salt"], existing["password_hash"]):
                    raise HTTPException(status_code=401, detail="该用户名已被注册或者密码错误")
            else:
                set_participant_password(conn, existing["id"], data.password)
                existing = fetch_participant_by_username(conn, username)
            conn.execute(
                "UPDATE participants SET updated_at = ? WHERE id = ?",
                (utc_now(), existing["id"]),
            )
            return participant_payload(conn, existing)

        order = create_order(manifest)
        participant_id = str(uuid.uuid4())
        password_salt, password_hash = hash_password(data.password)
        now = utc_now()
        try:
            conn.execute(
                """
                INSERT INTO participants
                    (
                        id, username, password_salt, password_hash,
                        order_json, user_agent, created_at, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    participant_id,
                    username,
                    password_salt,
                    password_hash,
                    json.dumps(order),
                    request.headers.get("user-agent", ""),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail="该用户名已被注册或者密码错误",
            )

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
    manifest = load_manifest()
    total = len(manifest)
    grouped = group_manifest(manifest)
    videos_per_sample = {
        sample_id: len(videos)
        for sample_id, videos in grouped.items()
    }
    with connect() as conn:
        rated = conn.execute(
            "SELECT COUNT(*) AS count FROM ratings WHERE participant_id = ?",
            (data.participant_id,),
        ).fetchone()["count"]
        if data.allow_partial:
            if rated == 0:
                raise HTTPException(status_code=400, detail="no samples completed")
            partial = [
                row["audio_id"]
                for row in conn.execute(
                    """
                    SELECT audio_id, COUNT(*) AS count
                    FROM ratings
                    WHERE participant_id = ?
                    GROUP BY audio_id
                    """,
                    (data.participant_id,),
                ).fetchall()
                if row["count"] != videos_per_sample.get(row["audio_id"], 0)
            ]
            if partial:
                raise HTTPException(status_code=400, detail=f"{len(partial)} samples incomplete")
        elif rated < total:
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


def check_admin_session(x_admin_session: str | None) -> None:
    if not x_admin_session or x_admin_session not in ADMIN_SESSIONS:
        raise HTTPException(status_code=401, detail="invalid admin session")


@app.post("/api/admin/login")
def admin_login(data: AdminLoginIn) -> dict[str, Any]:
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="admin password is not configured")
    username = " ".join(data.username.strip().split())
    if username != ADMIN_USERNAME or not hmac.compare_digest(data.password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="该用户名已被注册或者密码错误")
    session = secrets.token_urlsafe(32)
    ADMIN_SESSIONS.add(session)
    return {"ok": True, "session": session, "username": ADMIN_USERNAME}


@app.get("/api/admin/stats")
def admin_stats(x_admin_session: str | None = Header(default=None)) -> dict[str, Any]:
    check_admin_session(x_admin_session)
    manifest = load_manifest()
    grouped = group_manifest(manifest)
    total_videos = len(manifest)
    with connect() as conn:
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS participants,
                SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completed
            FROM participants
            """
        ).fetchone()
        rating_total = conn.execute("SELECT COUNT(*) AS count FROM ratings").fetchone()["count"]
        participant_rows = conn.execute(
            """
            SELECT
                p.username,
                p.created_at,
                p.updated_at,
                p.completed_at,
                COUNT(r.video_id) AS rated_count
            FROM participants p
            LEFT JOIN ratings r ON r.participant_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """
        ).fetchall()
        method_rows = conn.execute(
            """
            SELECT
                method,
                COUNT(*) AS rating_count,
                AVG(visual_quality) AS visual_quality,
                AVG(occlusion) AS occlusion,
                AVG(lip_sync) AS lip_sync,
                AVG(teeth_quality) AS teeth_quality
            FROM ratings
            GROUP BY method
            ORDER BY method
            """
        ).fetchall()
        video_rows = conn.execute(
            """
            SELECT
                audio_id,
                method,
                video_id,
                COUNT(*) AS rating_count,
                AVG(visual_quality) AS visual_quality,
                AVG(occlusion) AS occlusion,
                AVG(lip_sync) AS lip_sync,
                AVG(teeth_quality) AS teeth_quality
            FROM ratings
            GROUP BY video_id
            ORDER BY audio_id, method
            """
        ).fetchall()

    participants = totals["participants"] or 0
    completed = totals["completed"] or 0
    expected_ratings = participants * total_videos
    return {
        "summary": {
            "participants": participants,
            "completed": completed,
            "in_progress": participants - completed,
            "samples": len(grouped),
            "videos": total_videos,
            "ratings": rating_total,
            "expected_ratings": expected_ratings,
            "rating_progress": round((rating_total / expected_ratings) * 100, 1) if expected_ratings else 0,
        },
        "participants": [
            {
                "username": row["username"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "completed_at": row["completed_at"],
                "rated_count": row["rated_count"],
                "progress": round((row["rated_count"] / total_videos) * 100, 1) if total_videos else 0,
            }
            for row in participant_rows
        ],
        "method_stats": [
            {
                "method": row["method"],
                "rating_count": row["rating_count"],
                **{field: round(row[field], 3) if row[field] is not None else None for field in SCORE_FIELDS},
            }
            for row in method_rows
        ],
        "video_stats": [
            {
                "audio_id": row["audio_id"],
                "method": row["method"],
                "video_id": row["video_id"],
                "rating_count": row["rating_count"],
                **{field: round(row[field], 3) if row[field] is not None else None for field in SCORE_FIELDS},
            }
            for row in video_rows
        ],
    }


@app.get("/api/admin/export.csv")
def admin_export_csv(x_admin_session: str | None = Header(default=None)) -> StreamingResponse:
    check_admin_session(x_admin_session)
    return ratings_csv_response()


@app.post("/api/admin/users/delete")
def admin_delete_user(data: AdminUserIn, x_admin_session: str | None = Header(default=None)) -> dict[str, Any]:
    check_admin_session(x_admin_session)
    username = " ".join(data.username.strip().split())
    with connect() as conn:
        participant = fetch_participant_by_username(conn, username)
        if not participant:
            raise HTTPException(status_code=404, detail="用户不存在")
        ratings_deleted = conn.execute(
            "DELETE FROM ratings WHERE participant_id = ?",
            (participant["id"],),
        ).rowcount
        conn.execute(
            "DELETE FROM participants WHERE id = ?",
            (participant["id"],),
        )
    return {"ok": True, "username": username, "ratings_deleted": ratings_deleted}


@app.post("/api/admin/users/clear-ratings")
def admin_clear_user_ratings(data: AdminUserIn, x_admin_session: str | None = Header(default=None)) -> dict[str, Any]:
    check_admin_session(x_admin_session)
    username = " ".join(data.username.strip().split())
    now = utc_now()
    with connect() as conn:
        participant = fetch_participant_by_username(conn, username)
        if not participant:
            raise HTTPException(status_code=404, detail="用户不存在")
        ratings_deleted = conn.execute(
            "DELETE FROM ratings WHERE participant_id = ?",
            (participant["id"],),
        ).rowcount
        conn.execute(
            "UPDATE participants SET completed_at = NULL, updated_at = ? WHERE id = ?",
            (now, participant["id"]),
        )
    return {"ok": True, "username": username, "ratings_deleted": ratings_deleted}


@app.post("/api/admin/users/clear-all")
def admin_clear_all_users(x_admin_session: str | None = Header(default=None)) -> dict[str, Any]:
    check_admin_session(x_admin_session)
    with connect() as conn:
        ratings_deleted = conn.execute("DELETE FROM ratings").rowcount
        users_deleted = conn.execute("DELETE FROM participants").rowcount
    return {"ok": True, "users_deleted": users_deleted, "ratings_deleted": ratings_deleted}


@app.get("/api/export.csv")
def export_csv(
    token: str | None = Query(default=None),
    x_admin_token: str | None = Header(default=None),
) -> StreamingResponse:
    check_admin_token(token, x_admin_token)
    return ratings_csv_response()


def ratings_csv_response() -> StreamingResponse:
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
