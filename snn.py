import os
import threading
import time
import httpx
from subprocess import run, CalledProcessError
from typing import List, Optional
import asyncio
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Request, Response
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, Table, MetaData
from sqlalchemy.orm import sessionmaker
from passlib.hash import bcrypt

SERVICE_URL = "http://snapify-dkzn.onrender.com"
# --- Database setup ---
# Read DATABASE_URL from env; fallback to local SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./snaps.db")
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL)
metadata = MetaData()

users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String, unique=True, index=True),
    Column("password_hash", String),
)

subscriptions = Table(
    "subscriptions", metadata,
    Column("id", Integer, primary_key=True),
    Column("owner", String, index=True),
    Column("snap_user", String),
)

# Create tables if they don't exist
metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

# --- FastAPI setup ---
app = FastAPI()

# CORS for preflight (dev). Restrict allow_origins in prod.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Base media dir
BASE_MEDIA_DIR = "snap_media"
if not os.path.isdir(BASE_MEDIA_DIR):
    os.makedirs(BASE_MEDIA_DIR, exist_ok=True)

# Mount static files at /snap_media
app.mount("/snap_media", StaticFiles(directory=BASE_MEDIA_DIR), name="snap_media")

templates = Jinja2Templates(directory="templates")

# Track which users have a monitor loop started
_monitors_started = set()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(request: Request, db=Depends(get_db)):
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not logged in")
    row = db.execute(users.select().where(users.c.username == username)).first()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid user")
    return username

# --- Schemas ---
class RegisterIn(BaseModel):
    username: str
    password: str

class LoginIn(BaseModel):
    username: str
    password: str

class SubscriptionAction(BaseModel):
    snap_users: List[str]
# --- Routes ---

@app.on_event("startup")
async def schedule_ping_task():
    async def ping_loop():
        async with httpx.AsyncClient(timeout=5) as client:
            while True:
                try:
                    resp = await client.get(f"{SERVICE_URL}/ping")
                    if resp.status_code != 200:
                        print(f"Health ping returned {resp.status_code}")
                except Exception as e:
                    print(f"External ping failed: {e!r}")
                await asyncio.sleep(120)
    asyncio.create_task(ping_loop())

@app.get("/ping")
async def ping():
    return {"status": "alive"}

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("main.html", {"request": request, "api_url": ""})

@app.post("/register", status_code=201)
def register(data: RegisterIn, db=Depends(get_db)):
    if db.execute(users.select().where(users.c.username == data.username)).first():
        raise HTTPException(status_code=400, detail="Username already taken")
    db.execute(
        users.insert().values(
            username=data.username,
            password_hash=bcrypt.hash(data.password)
        )
    )
    db.commit()
    return {"msg": "Registered"}

@app.post("/login")
def login(data: LoginIn, response: Response, db=Depends(get_db)):
    row = db.execute(users.select().where(users.c.username == data.username)).first()
    if not row or not bcrypt.verify(data.password, row.password_hash):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    response.set_cookie(key="username", value=data.username, path="/")
    return {"msg": "Logged in"}

@app.post("/logout")
def logout(response: Response):
    response.delete_cookie(key="username", path="/")
    return {"msg": "Logged out"}

def monitor_snaps(owner: str, interval: int = 60, zip_it: bool = False,
                  only_highlights: bool = False, only_spotlights: bool = False):
    while True:
        db = SessionLocal()
        rows = db.execute(subscriptions.select().where(subscriptions.c.owner == owner)).fetchall()
        db.close()
        users_list = ",".join(r.snap_user for r in rows)
        if users_list:
            cmd = ["snapify", "-u", users_list]
            if zip_it:
                cmd.append("-z")
            if only_highlights:
                cmd.append("--highlights")
            if only_spotlights:
                cmd.append("-s")
            try:
                run(cmd, check=True)
            except CalledProcessError as e:
                print(f"[monitor] error for {owner}: {e}")
        time.sleep(interval)

@app.post("/users/add")
def add_subscriptions(
    action: SubscriptionAction,
    background_tasks: BackgroundTasks,
    current_user: str = Depends(get_current_user),
    db=Depends(get_db)
):
    added = []
    for su in action.snap_users:
        exists = db.execute(
            subscriptions.select()
            .where(subscriptions.c.owner == current_user)
            .where(subscriptions.c.snap_user == su)
        ).first()
        if not exists:
            db.execute(subscriptions.insert().values(owner=current_user, snap_user=su))
            added.append(su)
    db.commit()

    if added:
        def one_time_download(users_list):
            cmd = ["snapify", "-u", ",".join(users_list)]
            try:
                run(cmd, check=False)
            except CalledProcessError as e:
                print(f"[one-time] error for {current_user}: {e}")
        threading.Thread(target=one_time_download, args=(added,), daemon=True).start()

    if current_user not in _monitors_started:
        _monitors_started.add(current_user)
        background_tasks.add_task(monitor_snaps, current_user)

    return {"msg": f"Added {len(added)} user(s)", "added": added}

@app.post("/users/remove")
def remove_subscriptions(
    action: SubscriptionAction,
    current_user: str = Depends(get_current_user),
    db=Depends(get_db)
):
    removed = []
    for su in action.snap_users:
        res = db.execute(
            subscriptions.delete()
            .where(subscriptions.c.owner == current_user)
            .where(subscriptions.c.snap_user == su)
        )
        if res.rowcount:
            removed.append(su)
    db.commit()
    return {"msg": f"Removed {len(removed)} user(s)", "removed": removed}

@app.get("/subscriptions")
def list_subscriptions(
    current_user: str = Depends(get_current_user),
    db=Depends(get_db)
):
    rows = db.execute(subscriptions.select().where(subscriptions.c.owner == current_user)).fetchall()
    subs = [r.snap_user for r in rows]
    return {"subscriptions": subs}

@app.get("/gallery")
def get_gallery(
    current_user: str = Depends(get_current_user),
    db=Depends(get_db)
):
    """
    Return for each subscribed user:
      - stories: list of URLs
      - highlights: list of { album: name, items: [URLs] }
      - spotlights: list of URLs
    Directory structure expected:
      snap_media/<user>/stories/<files>
      snap_media/<user>/highlights/<album_name>/<files>
      snap_media/<user>/spotlights/<files>
    """
    out = []
    base = BASE_MEDIA_DIR
    for row in db.execute(subscriptions.select().where(subscriptions.c.owner == current_user)):
        u = row.snap_user
        entry = {"snap_user": u, "stories": [], "highlights": [], "spotlights": []}
        # Stories
        story_dir = os.path.join(base, u, "stories")
        if os.path.isdir(story_dir):
            for fn in sorted(os.listdir(story_dir)):
                if fn.lower().endswith((".jpg", ".png", ".mp4")):
                    entry["stories"].append(f"/snap_media/{u}/stories/{fn}")
        # Highlights: subfolders
        high_root = os.path.join(base, u, "highlights")
        if os.path.isdir(high_root):
            for album in sorted(os.listdir(high_root)):
                album_path = os.path.join(high_root, album)
                if os.path.isdir(album_path):
                    items = []
                    for fn in sorted(os.listdir(album_path)):
                        if fn.lower().endswith((".jpg", ".png", ".mp4")):
                            items.append(f"/snap_media/{u}/highlights/{album}/{fn}")
                    if items:
                        entry["highlights"].append({"album": album, "items": items})
        # Spotlights
        spot_dir = os.path.join(base, u, "spotlights")
        if os.path.isdir(spot_dir):
            for fn in sorted(os.listdir(spot_dir)):
                if fn.lower().endswith((".jpg", ".png", ".mp4")):
                    entry["spotlights"].append(f"/snap_media/{u}/spotlights/{fn}")
        out.append(entry)
    return {"gallery": out}

@app.post("/monitor/start")
def start_monitor(
    background_tasks: BackgroundTasks,
    current_user: str = Depends(get_current_user),
    interval: Optional[int] = 300,
    zip_it: Optional[bool] = False,
    highlights: Optional[bool] = False,
    spotlights: Optional[bool] = False,
):
    if current_user not in _monitors_started:
        _monitors_started.add(current_user)
        background_tasks.add_task(monitor_snaps, current_user, interval, zip_it, highlights, spotlights)
    return {"msg": "Monitoring started", "interval": interval}
