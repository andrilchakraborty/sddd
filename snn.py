import os
import json
import threading
import time
import httpx
from subprocess import run, CalledProcessError
from typing import List, Optional
import asyncio

from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

SERVICE_URL    = "https://sddd-2qot.onrender.com/"
BASE_MEDIA_DIR = "snap_media"
SUBS_FILE      = "subscriptions.json"
_lock          = threading.Lock()

# ─── Ensure subscription storage exists ───────────────────────────────────────
if not os.path.isfile(SUBS_FILE):
    with open(SUBS_FILE, "w") as f:
        json.dump([], f, indent=2)

# ─── FastAPI setup ────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],    # tighten in prod!
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount media directory
os.makedirs(BASE_MEDIA_DIR, exist_ok=True)
app.mount("/snap_media", StaticFiles(directory=BASE_MEDIA_DIR), name="snap_media")

templates = Jinja2Templates(directory="templates")

# ─── Subscription file ops ───────────────────────────────────────────────────
def load_subs():
    with open(SUBS_FILE, "r") as f:
        return json.load(f)

def save_subs(subs):
    with open(SUBS_FILE, "w") as f:
        json.dump(subs, f, indent=2)

# ─── Models ───────────────────────────────────────────────────────────────────
class SubscriptionAction(BaseModel):
    snap_users: List[str]

# ─── Download + Start helper ─────────────────────────────────────────────────
def download_then_start(user: str):
    """Runs `snapify -u user`, then `snapify start -u user`."""
    try:
        run(["snapify", "-u", user], check=True)
    except CalledProcessError as e:
        print(f"[one-time] download error for {user}: {e}")
    # now kick off the long-running monitor
    try:
        run(["snapify", "start", "-u", user], check=True)
    except CalledProcessError as e:
        print(f"[one-time] start error for {user}: {e}")

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "main.html",
        {"request": request, "api_url": SERVICE_URL}
    )

@app.post("/users/add")
def add_subscriptions(
    action: SubscriptionAction,
    background_tasks: BackgroundTasks
):
    added = []
    with _lock:
        subs = load_subs()
        for su in action.snap_users:
            if su not in subs:
                subs.append(su)
                added.append(su)
        save_subs(subs)

    # For each new user, fire off the download+start in background
    for u in added:
        background_tasks.add_task(download_then_start, u)

    return {"msg": f"Added {len(added)} user(s)", "added": added}

@app.post("/users/remove")
def remove_subscriptions(action: SubscriptionAction):
    removed = []
    with _lock:
        subs = load_subs()
        new_subs = []
        for s in subs:
            if s in action.snap_users:
                removed.append(s)
            else:
                new_subs.append(s)
        save_subs(new_subs)
    return {"msg": f"Removed {len(removed)} user(s)", "removed": removed}

@app.get("/subscriptions")
def list_subscriptions():
    return {"subscriptions": load_subs()}

@app.get("/gallery")
def get_gallery():
    out = []
    subs = load_subs()
    for u in subs:
        entry = {"snap_user": u, "stories": [], "highlights": [], "spotlights": []}
        # Stories
        sd = os.path.join(BASE_MEDIA_DIR, u, "stories")
        if os.path.isdir(sd):
            for fn in sorted(os.listdir(sd)):
                if fn.lower().endswith((".jpg", ".png", ".mp4")):
                    entry["stories"].append(f"/snap_media/{u}/stories/{fn}")
        # Highlights
        hr = os.path.join(BASE_MEDIA_DIR, u, "highlights")
        if os.path.isdir(hr):
            for album in sorted(os.listdir(hr)):
                ap = os.path.join(hr, album)
                if os.path.isdir(ap):
                    items = [
                        f"/snap_media/{u}/highlights/{album}/{fn}"
                        for fn in sorted(os.listdir(ap))
                        if fn.lower().endswith((".jpg", ".png", ".mp4"))
                    ]
                    if items:
                        entry["highlights"].append({"album": album, "items": items})
        # Spotlights
        sp = os.path.join(BASE_MEDIA_DIR, u, "spotlights")
        if os.path.isdir(sp):
            for fn in sorted(os.listdir(sp)):
                if fn.lower().endswith((".jpg", ".png", ".mp4")):
                    entry["spotlights"].append(f"/snap_media/{u}/spotlights/{fn}")
        out.append(entry)
    return {"gallery": out}

@app.post("/monitor/start")
def start_monitor(
    background_tasks: BackgroundTasks,
    interval: Optional[int] = 300,
    zip_it: Optional[bool] = False,
    highlights: Optional[bool] = False,
    spotlights: Optional[bool] = False,
):
    # Start a background monitor that loops `snapify start` forever
    def loop_start():
        while True:
            subs = load_subs()
            if subs:
                cmd = ["snapify", "start", "-u", ",".join(subs)]
                if zip_it:         cmd.append("-z")
                if highlights:     cmd.append("--highlights")
                if spotlights:     cmd.append("-s")
                try:
                    run(cmd, check=True)
                except CalledProcessError as e:
                    print(f"[monitor start] error: {e}")
            time.sleep(interval)

    background_tasks.add_task(loop_start)
    return {"msg": "Global monitoring started", "interval": interval}

