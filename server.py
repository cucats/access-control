import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import aiosqlite
import uvicorn
from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

CERT_DIR = Path("certs")
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"
DB_PATH = Path("data/access.db")
PORT = 8443

ADMIN_TOKEN = uuid4().hex
COOKIE_NAME = "admin_session"

db: aiosqlite.Connection


def detect_local_ip() -> str:
    if ip := os.environ.get("HOST_IP"):
        return ip
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        pass
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        addr = info[4][0]
        if not addr.startswith("127."):
            return addr
    return "127.0.0.1"


def generate_self_signed_cert(ip: str) -> None:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    print(f"Generating self-signed certificate for {ip}...")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
            "-days", "30", "-nodes",
            "-subj", f"/CN={ip}",
            "-addext", f"subjectAltName=IP:{ip}",
        ],
        check=True,
        capture_output=True,
    )
    print("Certificate generated.")


async def init_db() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(DB_PATH))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS guests (
            uid           TEXT PRIMARY KEY,
            registered_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS meals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS collections (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_id      INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
            uid          TEXT NOT NULL REFERENCES guests(uid),
            collected_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(meal_id, uid)
        );
    """)
    await conn.commit()
    conn.row_factory = aiosqlite.Row
    return conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await init_db()
    yield
    await db.close()


app = FastAPI(lifespan=lifespan)


def require_admin(admin_session: str | None) -> None:
    if admin_session != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


async def get_active_meal() -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT id, name, start_time, end_time FROM meals "
        "WHERE start_time <= datetime('now','localtime') "
        "AND end_time > datetime('now','localtime') "
        "ORDER BY start_time LIMIT 1"
    )
    return await cursor.fetchone()


# --- Public ---

@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/stats")
async def stats():
    cursor = await db.execute("SELECT COUNT(*) FROM guests")
    (registered,) = await cursor.fetchone()
    meal = await get_active_meal()
    return {
        "registered": registered,
        "active_meal": {"id": meal["id"], "name": meal["name"]} if meal else None,
    }


# --- Reader ---

class UidRequest(BaseModel):
    uid: str


@app.post("/register")
async def register(req: UidRequest):
    uid = req.uid.strip().upper()
    if not uid:
        return JSONResponse({"error": "empty uid"}, status_code=400)

    try:
        await db.execute("INSERT INTO guests (uid) VALUES (?)", (uid,))
        await db.commit()
        cursor = await db.execute("SELECT COUNT(*) FROM guests")
        (total,) = await cursor.fetchone()
        return {"status": "registered", "total_guests": total}
    except aiosqlite.IntegrityError:
        cursor = await db.execute("SELECT COUNT(*) FROM guests")
        (total,) = await cursor.fetchone()
        return {"status": "already_registered", "total_guests": total}


@app.post("/collect")
async def collect(req: UidRequest):
    uid = req.uid.strip().upper()
    if not uid:
        return JSONResponse({"error": "empty uid"}, status_code=400)

    cursor = await db.execute("SELECT uid FROM guests WHERE uid = ?", (uid,))
    if not await cursor.fetchone():
        return {"status": "not_registered"}

    meal = await get_active_meal()
    if not meal:
        return {"status": "no_active_meal"}

    try:
        await db.execute(
            "INSERT INTO collections (meal_id, uid) VALUES (?, ?)",
            (meal["id"], uid),
        )
        await db.commit()
        return {"status": "ok", "meal_name": meal["name"]}
    except aiosqlite.IntegrityError:
        return {"status": "already_collected", "meal_name": meal["name"]}


# --- Admin Auth ---

@app.get("/admin")
async def admin_page(admin_session: str | None = Cookie(default=None)):
    require_admin(admin_session)
    return FileResponse("static/admin.html")


@app.get("/{token}")
async def admin_auth(token: str, response: Response):
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=404, detail="Not found")
    response = RedirectResponse(url="/admin", status_code=302)
    response.set_cookie(
        key=COOKIE_NAME,
        value=ADMIN_TOKEN,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return response


# --- Admin API ---

@app.get("/api/admin/stats")
async def admin_stats(admin_session: str | None = Cookie(default=None)):
    require_admin(admin_session)
    cursor = await db.execute("SELECT COUNT(*) FROM guests")
    (total_guests,) = await cursor.fetchone()

    cursor = await db.execute(
        "SELECT m.id, m.name, m.start_time, m.end_time, "
        "COUNT(c.id) AS served "
        "FROM meals m LEFT JOIN collections c ON c.meal_id = m.id "
        "GROUP BY m.id ORDER BY m.start_time"
    )
    meals = [
        {
            "id": row["id"],
            "name": row["name"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "served": row["served"],
        }
        for row in await cursor.fetchall()
    ]
    return {"total_guests": total_guests, "meals": meals}


class MealCreate(BaseModel):
    name: str
    start_time: str
    end_time: str


def normalize_datetime(s: str) -> str:
    return s.replace("T", " ")


@app.post("/api/admin/meals")
async def create_meal(
    meal: MealCreate,
    admin_session: str | None = Cookie(default=None),
):
    require_admin(admin_session)
    start = normalize_datetime(meal.start_time)
    end = normalize_datetime(meal.end_time)

    cursor = await db.execute(
        "SELECT id FROM meals WHERE start_time < ? AND end_time > ?",
        (end, start),
    )
    if await cursor.fetchone():
        raise HTTPException(status_code=409, detail="Overlapping meal exists")

    cursor = await db.execute(
        "INSERT INTO meals (name, start_time, end_time) VALUES (?, ?, ?)",
        (meal.name, start, end),
    )
    await db.commit()
    return {"id": cursor.lastrowid, "name": meal.name}


@app.delete("/api/admin/meals/{meal_id}")
async def delete_meal(
    meal_id: int,
    admin_session: str | None = Cookie(default=None),
):
    require_admin(admin_session)
    cursor = await db.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Meal not found")
    return {"status": "deleted"}


@app.post("/api/admin/meals/{meal_id}/seconds")
async def allow_seconds(
    meal_id: int,
    admin_session: str | None = Cookie(default=None),
):
    require_admin(admin_session)
    cursor = await db.execute("SELECT id FROM meals WHERE id = ?", (meal_id,))
    if not await cursor.fetchone():
        raise HTTPException(status_code=404, detail="Meal not found")
    await db.execute("DELETE FROM collections WHERE meal_id = ?", (meal_id,))
    await db.commit()
    return {"status": "cleared"}


if __name__ == "__main__":
    ip = detect_local_ip()
    generate_self_signed_cert(ip)
    print(f"\n  Reader:  https://{ip}:{PORT}")
    print(f"  Admin:   https://{ip}:{PORT}/{ADMIN_TOKEN}\n")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        ssl_keyfile=str(KEY_FILE),
        ssl_certfile=str(CERT_FILE),
        log_level="warning",
    )
