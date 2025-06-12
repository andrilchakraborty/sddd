import os
import json
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
from passlib.hash import bcrypt

SERVICE_URL = "http://snapify-dkzn.onrender.com"
BASE_MEDIA_DIR = "snap_media"
USERS_FILE = "users.json"
SUBS_FILE = "subscriptions.json"
_lock = threading.Lock()

# Ensure storage files exist
for file, default in [(USERS_FILE, []), (SUBS_FILE, [])]:
    if not os.path.isfile(file):
        with open(file, 'w') as f:
            json.dump(default, f)

# FastAPI setup
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files for media
if not os.path.isdir(BASE_MEDIA_DIR):
    os.makedirs(BASE_MEDIA_DIR, exist_ok=True)
app.mount("/snap_media", StaticFiles(directory=BASE_MEDIA_DIR), name="snap_media")

templates = Jinja2Templates(directory="templates")

# In-memory set for monitors
_monitors_started = set()

# Utility file operations

def load_users():
    with open(USERS_FILE, 'r') as f:
        return json.load(f)

def save_users(data):
    with open(USERS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def load_subs():
    with open(SUBS_FILE, 'r') as f:
        return json.load(f)

def save_subs(data):
    with open(SUBS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# Models
class RegisterIn(BaseModel):
    username: str
    password: str

class LoginIn(BaseModel):
    username: str
    password: str

class SubscriptionAction(BaseModel):
    snap_users: List[str]

# Auth dependency

def get_current_user(request: Request):
    username = request.cookies.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not logged in")
    users = load_users()
    if username not in {u['username'] for u in users}:
        raise HTTPException(status_code=401, detail="Invalid user")
    return username

# Ping on startup
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
def register(data: RegisterIn):
    with _lock:
        users = load_users()
        if any(u['username'] == data.username for u in users):
            raise HTTPException(status_code=400, detail="Username already taken")
        users.append({
            'username': data.username,
            'password_hash': bcrypt.hash(data.password)
        })
        save_users(users)
    return {"msg": "Registered"}

@app.post("/login")
def login(data: LoginIn, response: Response):
    users = load_users()
    user = next((u for u in users if u['username'] == data.username), None)
    if not user or not bcrypt.verify(data.password, user['password_hash']):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    response.set_cookie(key="username", value=data.username, path="/")
    return {"msg": "Logged in"}

@app.post("/logout")
def logout(response: Response):
    response.delete_cookie(key="username", path="/")
    return {"msg": "Logged out"}

# Monitoring snap downloads

def monitor_snaps(owner: str, interval: int = 60, zip_it: bool = False,
                  only_highlights: bool = False, only_spotlights: bool = False):
    while True:
        subs = load_subs()
        users_list = [s['snap_user'] for s in subs if s['owner'] == owner]
        if users_list:
            cmd = ["snapify", "-u", ",".join(users_list)]
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

# Subscription routes
@app.post("/users/add")
def add_subscriptions(
    action: SubscriptionAction,
    background_tasks: BackgroundTasks,
    current_user: str = Depends(get_current_user)
):
    added = []
    with _lock:
        subs = load_subs()
        for su in action.snap_users:
            if not any(s['owner']==current_user and s['snap_user']==su for s in subs):
                subs.append({'owner': current_user, 'snap_user': su})
                added.append(su)
        save_subs(subs)
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
    current_user: str = Depends(get_current_user)
):
    removed = []
    with _lock:
        subs = load_subs()
        new_subs = []
        for s in subs:
            if s['owner']==current_user and s['snap_user'] in action.snap_users:
                removed.append(s['snap_user'])
            else:
                new_subs.append(s)
        save_subs(new_subs)
    return {"msg": f"Removed {len(removed)} user(s)", "removed": removed}

@app.get("/subscriptions")
def list_subscriptions(current_user: str = Depends(get_current_user)):
    subs = load_subs()
    user_subs = [s['snap_user'] for s in subs if s['owner'] == current_user]
    return {"subscriptions": user_subs}

@app.get("/gallery")
def get_gallery(current_user: str = Depends(get_current_user)):
    out = []
    base = BASE_MEDIA_DIR
    subs = [s['snap_user'] for s in load_subs() if s['owner']==current_user]
    for u in subs:
        entry = {"snap_user": u, "stories": [], "highlights": [], "spotlights": []}
        # Stories
        story_dir = os.path.join(base, u, "stories")
        if os.path.isdir(story_dir):
            for fn in sorted(os.listdir(story_dir)):
                if fn.lower().endswith((".jpg", ".png", ".mp4")):
                    entry["stories"].append(f"/snap_media/{u}/stories/{fn}")
        # Highlights
        high_root = os.path.join(base, u, "highlights")
        if os.path.isdir(high_root):
            for album in sorted(os.listdir(high_root)):
                path = os.path.join(high_root, album)
                if os.path.isdir(path):
                    items = [f"/snap_media/{u}/highlights/{album}/{fn}" for fn in sorted(os.listdir(path)) if fn.lower().endswith((".jpg",".png",".mp4"))]
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
