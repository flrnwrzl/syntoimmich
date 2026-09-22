#!/usr/bin/env python3
"""
Synology Photos -> Immich Migration Tool  (v3 — multi-user, shared-album-aware)
================================================================================
Browser-based UI. Run this script; a browser window opens automatically.

Architecture (see chat for full rationale):
  - Synology Personal Space is only ever visible to the user who owns it —
    even DSM admins cannot read another user's Personal Space via the API.
    So this tool logs in ONCE PER USER with that user's own credentials.
  - Synology "Shared Albums" (an album owned by one user, shared with others)
    are distinct from Synology "Shared Space" (a separate team library).
    This tool focuses on Personal Space + Shared Albums, with optional
    Shared Space support.
  - Each item inside an album carries a `provider_user_id` field identifying
    who actually contributed it — used to attribute photos to the correct
    Immich account inside the recreated shared album.
  - Each Immich user uploads under their OWN api key, so Immich's native
    per-asset "owner" display inside a shared album shows who added what —
    no need for hacky tags or descriptions.

Usage:
    python synology_to_immich.py
    python synology_to_immich.py --port 8765
"""

import os, sys, subprocess, importlib.util

# Install missing deps BEFORE any other import that needs them
def ensure_deps():
    if getattr(sys, "frozen", False):
        return  # bundled exe already has everything
    missing = [p for p in ("requests",) if importlib.util.find_spec(p) is None]
    if missing:
        print(f"Installing: {', '.join(missing)} ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + missing)

ensure_deps()

# Now safe to import — works both as script and as PyInstaller exe
try:
    import requests
    import requests.adapters
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("ERROR: 'requests' konnte nicht geladen werden.")
    print("Bitte manuell installieren:  pip install requests")
    input("Enter druecken zum Beenden...")
    sys.exit(1)

import json, hashlib, mimetypes, time, threading, webbrowser, traceback, tempfile, shutil
from datetime import datetime, timezone
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ─────────────────────────────────────────────────────────────────────────────
# Config persistence
# ─────────────────────────────────────────────────────────────────────────────
def app_dir():
    """Directory to store config/reports in.
    Works both as a normal script and as a PyInstaller --onefile exe
    (where __file__ would otherwise point to a temporary extraction folder)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE  = os.path.join(app_dir(), "migration_config.json")
STATE_FILE   = os.path.join(app_dir(), "migration_state.json")   # persists completed albums

def load_migration_state():
    """Load persisted migration state dict for resume-after-crash."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
                # Old format was a set/list — discard
        except Exception:
            pass
    return {}

def save_migration_state(done_albums: set, resume_key: str = ""):
    """Persist migration state for crash recovery."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"done_albums": list(done_albums), "resume_key": resume_key}, f)

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "synology_url": "", "synology_admin_user": "", "synology_admin_pass": "", "synology_admin_otp": "",
        "immich_url": "",
        "users": [],   # [{synology_username, synology_password, immich_api_key, immich_user_id, immich_label}]
        "options": {"migrate_personal": True, "migrate_shared_albums": True,
                    "migrate_shared_space": False, "dry_run": False},
    }

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

# ─────────────────────────────────────────────────────────────────────────────
# SSL — bypass cert verification for self-signed NAS certificates
# ─────────────────────────────────────────────────────────────────────────────
class SSLAdapter(requests.adapters.HTTPAdapter):
    def _make_ctx(self):
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        return ctx
    def init_poolmanager(self, *a, **kw):
        kw["ssl_context"] = self._make_ctx()
        return super().init_poolmanager(*a, **kw)
    def proxy_manager_for(self, proxy, **kw):
        kw["ssl_context"] = self._make_ctx()
        return super().proxy_manager_for(proxy, **kw)

def make_session():
    s = requests.Session()
    s.verify = False
    a = SSLAdapter()
    s.mount("https://", a)
    s.mount("http://",  a)
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Synology Photos — hand-rolled client (verified endpoints, one instance = one
# independent session/user; Personal Space is only visible to its own owner)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Synology Photos client — verified against official API docs
# Key facts from docs:
#   SYNO.Foto.*     → Personal Space + Albums tab (virtual albums)
#   SYNO.FotoTeam.* → Shared Space (physical folder /volume1/photo)
#   Shared Albums via passphrase need: sharing_sid cookie + x-syno-sharing header + passphrase param
#   Download: SYNO.Foto.Download for personal items, SYNO.FotoTeam.Download for shared space items
#   provider_user_id in item.additional = who added this photo to the shared album
# ─────────────────────────────────────────────────────────────────────────────
class SynologyError(Exception):
    pass


class SynologyClient:
    ITEM_EXTRA = json.dumps(["thumbnail","resolution","orientation",
                             "video_convert","video_meta","provider_user_id","exif"])

    def __init__(self, base_url, username, password, otp=""):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.otp      = otp
        self.session  = make_session()
        self.sid      = None

    # ── Low-level POST call ──────────────────────────────────────────────────
    def _call(self, cgi, api, method, extra=None, version=1, timeout=30):
        url     = f"{self.base_url}/{cgi}"
        payload = {"api": api, "version": version, "method": method}
        if self.sid:
            payload["_sid"] = self.sid
        if extra:
            payload.update(extra)
        # Serialize list/dict values to JSON strings
        data = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                for k, v in payload.items()}
        # Try POST first (works when Synology security blocks GET), then GET fallback
        for use_post in (True, False):
            try:
                resp = (self.session.post(url, data=data, timeout=timeout)
                        if use_post else
                        self.session.get(url, params=data, timeout=timeout))
                if resp.status_code == 403:
                    continue
                resp.raise_for_status()
                body = resp.json()
                if body.get("success"):
                    return body.get("data", {})
                err  = body.get("error", {})
                code = err.get("code", "?")
                raise SynologyError(f"{api}.{method}: code={code} {err}")
            except SynologyError:
                raise
            except Exception as e:
                if not use_post:
                    raise SynologyError(f"{api}.{method}: {e}")
                continue
        raise SynologyError(f"{api}.{method}: both POST and GET returned 403")

    def _foto(self, api, method, extra=None, version=1, timeout=30):
        return self._call("webapi/entry.cgi", api, method, extra, version, timeout)

    # ── Authentication ───────────────────────────────────────────────────────
    def login(self):
        attempts = []
        for ver in (3, 6, 7):
            for ep in ("webapi/entry.cgi", "photo/webapi/auth.cgi", "webapi/auth.cgi"):
                payload = {"api": "SYNO.API.Auth", "version": ver, "method": "login",
                           "account": self.username, "passwd": self.password,
                           "session": "SynologyPhotos", "format": "sid"}
                if self.otp:
                    payload["otp_code"] = self.otp
                try:
                    resp = self.session.post(f"{self.base_url}/{ep}", data=payload, timeout=15)
                    if resp.status_code == 403:
                        attempts.append(f"v{ver} {ep}: HTTP 403")
                        continue
                    resp.raise_for_status()
                    body = resp.json()
                    if body.get("success"):
                        self.sid = body["data"]["sid"]
                        self.session.cookies.set("id", self.sid)
                        return self.sid
                    code = body.get("error", {}).get("code", "?")
                    hints = {400:"Falsches Passwort", 401:"Account deaktiviert",
                             402:"Zugriff verweigert — User braucht administrators-Gruppe für API-Zugriff",
                             403:"Account gesperrt", 404:"2FA erforderlich", 406:"2FA-Code falsch"}
                    hint = hints.get(code, f"Code {code}")
                    attempts.append(f"v{ver} {ep}: {hint} (raw={body})")
                    if code in (401, 403):
                        raise SynologyError(f"Login blockiert für '{self.username}': {hint}")
                except SynologyError:
                    raise
                except Exception as e:
                    attempts.append(f"v{ver} {ep}: {e}")
        raise SynologyError(
            f"Login fehlgeschlagen für '{self.username}'.\n" + "\n".join(attempts[:6]))

    def logout(self):
        try:
            self.session.post(f"{self.base_url}/webapi/auth.cgi",
                              data={"api":"SYNO.API.Auth","version":1,"method":"logout",
                                    "session":"SynologyPhotos"}, timeout=10)
        except Exception:
            pass

    # ── Personal Space items (SYNO.Foto.*) ──────────────────────────────────
    def list_personal_items_page(self, offset=0, limit=1000):
        return self._foto("SYNO.Foto.Browse.Item", "list",
                          {"offset": offset, "limit": limit,
                           "additional": self.ITEM_EXTRA}).get("list", [])

    def list_all_personal_items(self):
        """All items from Personal Space (only visible to the owning user)."""
        items, offset = [], 0
        while True:
            batch = self.list_personal_items_page(offset)
            items.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000
        return items

    # ── Shared Space items (SYNO.FotoTeam.*) ────────────────────────────────
    def list_all_team_items(self):
        """All items from Shared Space (/volume1/photo). Admin sees everything."""
        items, offset = [], 0
        while True:
            batch = self._foto("SYNO.FotoTeam.Browse.Item", "list",
                               {"offset": offset, "limit": 1000,
                                "additional": self.ITEM_EXTRA}).get("list", [])
            items.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000
        return items

    # ── Normal Albums (Albums tab in Synology Photos) ────────────────────────
    def list_albums(self):
        """Virtual albums visible to this user (own + shared with them)."""
        albums, seen = [], set()
        for api in ("SYNO.Foto.Browse.NormalAlbum", "SYNO.Foto.Browse.Album"):
            offset = 0
            while True:
                try:
                    batch = self._foto(api, "list",
                                       {"offset": offset, "limit": 200}).get("list", [])
                    for a in batch:
                        if a.get("id") not in seen:
                            seen.add(a.get("id"))
                            albums.append(a)
                    if len(batch) < 200:
                        break
                    offset += 200
                except SynologyError as e:
                    if any(c in str(e) for c in ("600","642","119")):
                        break
                    raise
        return albums

    def list_items_in_album(self, album_id):
        """Items in a normal album (by album_id)."""
        items, offset = [], 0
        while True:
            batch = self._foto("SYNO.Foto.Browse.Item", "list",
                               {"album_id": album_id, "offset": offset, "limit": 1000,
                                "additional": self.ITEM_EXTRA}).get("list", [])
            items.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000
        return items

    # ── Shared Albums via Passphrase (Freigabe tab) ──────────────────────────
    # Per official docs: need sharing_sid cookie + x-syno-sharing header + passphrase param
    # The sharing_sid is obtained by GET /photo/mo/sharing/<passphrase>

    def get_sharing_sid(self, passphrase):
        """GET the shared album page to receive the sharing_sid cookie."""
        try:
            url  = f"{self.base_url}/photo/mo/sharing/{passphrase}"
            resp = self.session.get(url, timeout=15, allow_redirects=True)
            # Cookie is set automatically in self.session by requests
            sid  = self.session.cookies.get("sharing_sid")
            return sid
        except Exception:
            return None

    def list_items_in_shared_album(self, passphrase):
        """Items in a shared album (Freigabe). Requires sharing_sid + x-syno-sharing.
        Per API docs: POST to /photo/mo/sharing/webapi/entry.cgi with all three:
          - Cookie sharing_sid (obtained by visiting the share URL)
          - Header x-syno-sharing: <passphrase>
          - POST body passphrase=<passphrase>
        """
        # Step 1: get sharing_sid cookie
        self.get_sharing_sid(passphrase)

        items, offset = [], 0
        url = f"{self.base_url}/photo/mo/sharing/webapi/entry.cgi"
        while True:
            payload = {
                "api": "SYNO.Foto.Browse.Item",
                "version": 1,
                "method": "list",
                "offset": offset,
                "limit": 1000,
                "passphrase": passphrase,
                "additional": self.ITEM_EXTRA,
            }
            if self.sid:
                payload["_sid"] = self.sid
            try:
                resp = self.session.post(
                    url, data=payload,
                    headers={"x-syno-sharing": passphrase},
                    timeout=30
                )
                resp.raise_for_status()
                body  = resp.json()
                if not body.get("success"):
                    break
                batch = body.get("data", {}).get("list", [])
                items.extend(batch)
                if len(batch) < 1000:
                    break
                offset += 1000
            except Exception as e:
                raise SynologyError(f"Passphrase-Album {passphrase}: {e}")
        return items

    def get_shared_passphrases(self):
        """Get passphrases for albums shared with this user.
        Uses SYNO.Foto.Sharing.Passphrase (in user's query.json as available API)."""
        try:
            data = self._foto("SYNO.Foto.Sharing.Passphrase", "list",
                              {"offset": 0, "limit": 500})
            return data.get("list", [])
        except SynologyError:
            return []

    # ── Download ─────────────────────────────────────────────────────────────
    def download_item(self, item, dest_path):
        """Stream download to disk.
        Per API docs:
          SYNO.Foto.Download     = Personal Space (needs session of file owner)
          SYNO.FotoTeam.Download = Team/Shared Space
        Code 117 = item not in this space — caller retries with file owner session.
        Always tries both APIs so caller doesn't need to know which space item lives in.
        """
        item_id   = item.get("id")
        thumb     = (item.get("additional") or {}).get("thumbnail") or {}
        cache_key = thumb.get("cache_key")
        payloads  = ([{"unit_id": f"[{item_id}]", "cache_key": cache_key}] if cache_key else []) +                     [{"unit_id": f"[{item_id}]"}]
        last_err  = None
        for api in ("SYNO.Foto.Download", "SYNO.FotoTeam.Download"):
            for payload in payloads:
                try:
                    data = {"api": api, "version": 1, "method": "download"}
                    if self.sid:
                        data["_sid"] = self.sid
                    data.update(payload)
                    resp = self.session.post(
                        f"{self.base_url}/webapi/entry.cgi",
                        data=data, stream=True, timeout=300)
                    if resp.status_code in (403, 404):
                        last_err = SynologyError(f"HTTP {resp.status_code}")
                        continue
                    resp.raise_for_status()
                    if "application/json" in resp.headers.get("Content-Type", ""):
                        body     = resp.json()
                        err_code = (body.get("error") or {}).get("code")
                        last_err = SynologyError(f"code={err_code}")
                        continue  # code 117 = wrong space, try next API
                    with open(dest_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=256 * 1024):
                            if chunk:
                                f.write(chunk)
                    if os.path.getsize(dest_path) == 0:
                        last_err = SynologyError("Empty file received")
                        os.remove(dest_path)
                        continue
                    return dest_path
                except SynologyError:
                    raise
                except Exception as e:
                    last_err = e
                    continue
        raise SynologyError(f"Download fehlgeschlagen item={item_id}: {last_err}")


    # ── Admin helpers ─────────────────────────────────────────────────────────
    def list_dsm_users(self):
        """Admin-only: list all DSM users to build uid→name map."""
        try:
            resp = self.session.post(
                f"{self.base_url}/webapi/entry.cgi",
                data={"api":"SYNO.Core.User","version":1,"method":"list",
                      "offset":0,"limit":-1,
                      "additional":json.dumps(["email","description"]),
                      "_sid": self.sid or ""},
                timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success"):
                return data["data"].get("users", [])
        except Exception:
            pass
        return []



# ─────────────────────────────────────────────────────────────────────────────
# Migration state — shared between migration thread and HTTP polling
# ─────────────────────────────────────────────────────────────────────────────
migration_state = {
    "status":         "idle",    # idle | running | paused | done | error
    "current_action": "",        # human-readable description of what's happening NOW
    "phase":          "",        # Phase 1: Persönliche Fotos | Phase 2: Alben | Phase 3: Team Space
    "log":            [],
    "progress":       0,
    "total":          0,
    "uploaded":       0,
    "duplicates":     0,
    "failed":         0,
    "albums_done":    0,
    "albums_total":   0,
    "report":         None,
}
_pause_event = threading.Event()
_pause_event.set()   # not paused initially

def pause_migration():
    _pause_event.clear()
    migration_state["status"] = "paused"
    log("⏸ Migration pausiert", "warn")

def resume_migration():
    migration_state["status"] = "running"
    log("▶ Migration fortgesetzt", "success")
    _pause_event.set()

def _check_pause():
    """Call this in inner loops — blocks while paused."""
    _pause_event.wait()

def log(msg, level="info"):
    migration_state["log"].append(
        {"ts": datetime.now().strftime("%H:%M:%S"), "msg": str(msg), "level": level})
    if len(migration_state["log"]) > 5000:
        migration_state["log"] = migration_state["log"][-5000:]

def set_action(msg):
    migration_state["current_action"] = msg


# ─────────────────────────────────────────────────────────────────────────────
# ImmichClient — correctly implemented per API docs
# ─────────────────────────────────────────────────────────────────────────────
class ImmichClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/") + "/api"
        self.api_key  = api_key
        self.session  = make_session()
        self.session.headers.update({"x-api-key": api_key, "Accept": "application/json"})

    def ping(self):
        r = self.session.get(f"{self.base_url}/server/ping", timeout=10)
        r.raise_for_status()

    def whoami(self):
        for path in ("/users/me", "/user/me"):
            try:
                r = self.session.get(f"{self.base_url}{path}", timeout=10)
                if r.ok:
                    return r.json()
            except Exception:
                continue
        raise Exception("Immich whoami fehlgeschlagen — API-Key ungültig?")

    def check_exists(self, filepath, filename):
        """Pre-check if asset already exists using SHA-1 checksum.
        Returns existing asset id if found, None otherwise.
        Per Immich docs: POST /api/assets/bulk-upload-check"""
        try:
            sha1 = hashlib.sha1()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha1.update(chunk)
            checksum = sha1.hexdigest()
            r = self.session.post(
                f"{self.base_url}/assets/bulk-upload-check",
                json={"assets": [{"id": filename, "checksum": checksum}]},
                timeout=30)
            if r.ok:
                results = r.json().get("results", [])
                if results and results[0].get("action") == "reject":
                    return results[0].get("assetId"), checksum
            return None, checksum
        except Exception:
            return None, None

    def upload_asset(self, filepath, filename, created_at, modified_at):
        """Upload a file with pre-check for duplicates.
        Returns {id, status} where status='duplicate' if already exists."""
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        # Use file content hash as deviceAssetId for reliable dedup across runs
        sha1 = hashlib.sha1()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha1.update(chunk)
        checksum = sha1.hexdigest()
        did = checksum  # stable ID based on content, not filename

        # Pre-check before uploading (saves bandwidth on re-runs)
        try:
            r = self.session.post(
                f"{self.base_url}/assets/bulk-upload-check",
                json={"assets": [{"id": did, "checksum": checksum}]},
                timeout=15)
            if r.ok:
                results = r.json().get("results", [])
                if results and results[0].get("action") == "reject":
                    return {"id": results[0].get("assetId"), "status": "duplicate"}
        except Exception:
            pass  # pre-check failed → proceed with upload anyway

        with open(filepath, "rb") as fh:
            resp = self.session.post(
                f"{self.base_url}/assets",
                files={"assetData": (filename, fh, mime)},
                data={"deviceAssetId": did, "deviceId": "synology-migration",
                      "fileCreatedAt": created_at, "fileModifiedAt": modified_at},
                timeout=600)
        resp.raise_for_status()
        result = resp.json()
        if result.get("status") == "duplicate":
            return result
        return result

    def get_albums(self):
        """List all albums visible to this API key.
        Per Immich docs: GET /api/albums → [{id, albumName, assetCount, ...}]
        Used to check which Synology albums have already been migrated."""
        r = self.session.get(f"{self.base_url}/albums", timeout=30)
        r.raise_for_status()
        return r.json()

    def create_album(self, name, description="", album_users=None):
        """Create album (empty). Share with other users immediately after.
        Per Immich docs: use PUT /api/albums/{id}/users for sharing."""
        r = self.session.post(
            f"{self.base_url}/albums",
            json={"albumName": name, "description": description},
            timeout=30)
        r.raise_for_status()
        album = r.json()
        if album_users and album.get("id"):
            try:
                sr = self.session.put(
                    f"{self.base_url}/albums/{album['id']}/users",
                    json={"albumUsers": album_users}, timeout=30)
                if not sr.ok:
                    log(f"  ⚠ Album-Freigabe '{name}': {sr.status_code} {sr.text[:80]}", "warn")
            except Exception as e:
                log(f"  ⚠ Album-Freigabe '{name}': {e}", "warn")
        return album

    def add_assets_to_album(self, album_id, asset_ids):
        """Add assets in batches of 100 (Immich hard limit per request).
        Deduplicates and logs any per-asset failures from response."""
        ids = list(dict.fromkeys(a for a in asset_ids if a))
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            try:
                r = self.session.put(
                    f"{self.base_url}/albums/{album_id}/assets",
                    json={"ids": batch}, timeout=60)
                r.raise_for_status()
                # Check per-asset result (Immich returns status per item)
                for res in r.json():
                    if not res.get("success") and res.get("error") != "duplicate":
                        log(f"  ⚠ Asset {res.get('id','?')} nicht zum Album hinzugefügt: {res.get('error')}", "warn")
            except Exception as e:
                log(f"  ⚠ add_assets Batch {i//100+1}: {e}", "warn")
                time.sleep(2)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def iso_ts(unix_ts):
    if not unix_ts:
        return datetime.now(timezone.utc).isoformat()
    try:
        return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()

def item_filename(item):
    return item.get("filename") or item.get("file_name") or f"item_{item.get('id')}"

def owner_uid_of(item):
    """Returns provider_user_id (who added to album) or owner_user_id (file owner)."""
    add  = item.get("additional") or {}
    puid = add.get("provider_user_id")
    if puid is None:
        puid = item.get("owner_user_id")
    return puid


# ─────────────────────────────────────────────────────────────────────────────
# Core migration
# ─────────────────────────────────────────────────────────────────────────────
def run_migration(cfg):
    global migration_state
    _pause_event.set()  # ensure not paused from previous run
    migration_state.update({
        "status": "running", "current_action": "Starte...", "phase": "",
        "log": [], "progress": 0, "total": 0,
        "uploaded": 0, "duplicates": 0, "failed": 0, "albums_done": 0, "albums_total": 0, "report": None,
    })

    opts        = cfg.get("options", {})
    dry_run     = bool(opts.get("dry_run"))
    do_personal = bool(opts.get("migrate_personal", True))
    do_albums   = bool(opts.get("migrate_shared_albums", True))
    do_shared   = bool(opts.get("migrate_shared_space", False))

    users_cfg = [u for u in cfg.get("users", [])
                 if u.get("synology_username") and u.get("synology_password") and u.get("immich_api_key")]
    if not users_cfg:
        migration_state["status"] = "error"
        log("Keine User konfiguriert. Bitte User mit Synology-Passwort und Immich-API-Key eintragen.", "error")
        return

    # selected_albums from UI:
    #   None or missing = no scan done, migrate ALL
    #   [] empty list = edge case, treat as ALL
    #   [...] with items = migrate only those specific albums
    sel_raw = cfg.get("selected_albums")
    if not sel_raw:  # None or empty list → migrate everything
        sel             = None
        sel_normal_ids  = None
        sel_pps         = None
    else:
        sel             = sel_raw
        sel_normal_ids  = {str(a["id"]) for a in sel if a.get("id")}
        sel_pps         = {a["passphrase"] for a in sel if a.get("passphrase")}

    tmp_dir = tempfile.mkdtemp(prefix="syn2immich_")
    report  = {"started_at": datetime.now().isoformat(), "dry_run": dry_run,
               "users": {}, "albums": [], "failed": []}

    try:
        # ── Connect sessions ─────────────────────────────────────────────────
        log("─── Verbinde User ───", "section")
        set_action("Verbinde Synology und Immich...")
        sessions   = {}   # uname → {syn, immich, immich_id, immich_label}
        uid_to_user = {}  # str(uid) → synology_username

        for u in users_cfg:
            uname = u["synology_username"]
            set_action(f"Login: {uname}...")
            try:
                syn = SynologyClient(cfg["synology_url"], uname,
                                     u["synology_password"], u.get("synology_otp",""))
                syn.login()
                imm   = ImmichClient(cfg["immich_url"], u["immich_api_key"])
                me    = imm.whoami()
                label = me.get("email") or me.get("name") or uname
                sessions[uname] = {"syn": syn, "immich": imm,
                                   "immich_id": me.get("id"), "immich_label": label}
                log(f"  ✓ {uname} → {label}", "success")
                # Try to get UID map — works if this user has admin rights
                if not uid_to_user:
                    for du in syn.list_dsm_users():
                        uid = str(du.get("uid",""))
                        if uid:
                            uid_to_user[uid] = du.get("name","")
                    if uid_to_user:
                        log(f"  {len(uid_to_user)} Synology UIDs aufgelöst", "info")
            except Exception as e:
                log(f"  ✗ {uname}: {e}", "error")
                report["failed"].append({"user": uname, "stage": "login", "error": str(e)})

        if not sessions:
            migration_state["status"] = "error"
            log("Kein User konnte verbunden werden. Abbruch.", "error")
            return

        # Build UID map from each user's own personal items (reliable, no admin needed).
        # DSM user list (list_dsm_users) often fails even for admin accounts via Photos API.
        # Personal items always carry the correct owner_user_id for that user's session.
        for uname, sess in sessions.items():
            if uname in [uid_to_user.get(k) for k in uid_to_user]:
                continue  # already have this user's UID
            try:
                items = sess["syn"].list_all_personal_items()
                for it in items[:10]:
                    uid = it.get("owner_user_id")
                    if uid and int(uid) != 0 and str(uid) not in uid_to_user:
                        uid_to_user[str(uid)] = uname
                        log(f"  UID {uid} → {uname}", "info")
                        break
            except Exception:
                pass

        if uid_to_user:
            log(f"  UID-Zuordnung: {uid_to_user}", "info")
        else:
            log("  ⚠ Keine UIDs aufgelöst — Foto-Zuordnung funktioniert ggf. nicht korrekt", "warn")

        fallback = list(sessions.keys())[0]
        _unknown_uids_logged = set()  # track which UIDs we've already warned about

        def immich_for(uname):
            return sessions.get(uname, sessions[fallback])["immich"]

        def resolve_owner(item):
            """Which Synology user should OWN this item in Immich?
            provider_user_id = who added photo to album → their Immich account.
            owner_user_id = file owner (fallback).
            """
            add  = item.get("additional") or {}
            puid = add.get("provider_user_id")
            if puid is None:
                puid = item.get("owner_user_id")
            if puid is not None:
                try:
                    puid_int = int(puid)
                except Exception:
                    puid_int = 0
                if puid_int != 0:
                    name = uid_to_user.get(str(puid_int))
                    if name and name in sessions:
                        return name
                    # UID exists but not in sessions — log once per unknown UID
                    if str(puid_int) not in _unknown_uids_logged:
                        _unknown_uids_logged.add(str(puid_int))
                        log(f"  ⚠ UID {puid_int} nicht in uid_to_user ({uid_to_user}) → fällt auf '{fallback}' zurück", "warn")
            return fallback

        # (uname, syn_item_id) → immich_asset_id
        item_map = {}
        # Always start fresh — Immich's built-in duplicate detection (by hash)
        # automatically skips already-uploaded files without re-uploading.
        # The state file is only used for explicit resume after a crash/pause.
        done_albums = set()
        # Check if this is an explicit resume (state file exists AND same albums selected)
        resume_key = json.dumps(sorted([str(a.get("id") or a.get("passphrase",""))
                                        for a in (sel or [])]))
        existing_state = load_migration_state()
        if existing_state and existing_state.get("resume_key") == resume_key and existing_state.get("done_albums"):
            done_albums = set(existing_state["done_albums"])
            log(f"  Fortsetze vorherigen Lauf: {len(done_albums)} Alben bereits erledigt", "info")
        else:
            # Fresh start — clear any old state
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)


        MAX_RETRIES = 3

        def download_with_retry(item, owner_uname, context=""):
            """Try to download using file-owner's session first, then all other sessions.
            Key insight from API docs:
              provider_user_id = who ADDED the photo to the album (not the file owner)
              owner_user_id    = who OWNS the file (determines which session can download)
            Code 117 = item not in this session's space.
            Retries on 502/503 with exponential backoff.
            """
            fname   = item_filename(item)
            dest    = os.path.join(tmp_dir, f"{owner_uname}_{item.get('id')}_{fname}")
            # Resolve actual file owner (owner_user_id, not provider_user_id)
            file_owner_uid = item.get("owner_user_id")
            file_owner_name = uid_to_user.get(str(file_owner_uid)) if file_owner_uid else None
            # Build session priority:
            # 1. File owner (owner_user_id) — most likely to succeed
            # 2. Album contributor (owner_uname) — who added to album
            # 3. All other sessions — fallback for code 117
            sessions_to_try = []
            for candidate in (file_owner_name, owner_uname):
                if candidate and candidate in sessions:
                    syn = sessions[candidate]["syn"]
                    if syn not in sessions_to_try:
                        sessions_to_try.append(syn)
            for uname, sess in sessions.items():
                if sess["syn"] not in sessions_to_try:
                    sessions_to_try.append(sess["syn"])

            last_err = None
            for attempt in range(MAX_RETRIES):
                for syn in sessions_to_try:
                    try:
                        set_action(f"↓ {fname}{' ('+context+')' if context else ''} "
                                   f"{'[retry '+str(attempt)+']' if attempt else ''}")
                        syn.download_item(item, dest)
                        return dest  # success
                    except SynologyError as e:
                        last_err = e
                        err_str = str(e)
                        # Code 117 = item not in this session's space → try next session
                        if "117" in err_str:
                            continue
                        # 502/503 = server overloaded → backoff and retry
                        if "502" in err_str or "503" in err_str or "Bad Gateway" in err_str:
                            wait = 2 ** attempt
                            log(f"  ⚠ Server überlastet ({err_str[:40]}), warte {wait}s...", "warn")
                            time.sleep(wait)
                            break  # retry outer loop
                        # Other errors → try next session
                        continue
                    except Exception as e:
                        last_err = e
                        err_str = str(e)
                        # 10054 = ConnectionResetError (Synology closed connection)
                        # 502/503 = server overloaded, timeout = network issue
                        is_transient = any(x in err_str for x in (
                            "502", "503", "10054", "ConnectionReset", "Connection aborted",
                            "timeout", "RemoteDisconnected", "ConnectionRefused"))
                        if is_transient:
                            wait = 2 ** attempt
                            log(f"  ⚠ Verbindung unterbrochen ({err_str[:50]}), warte {wait}s dann Reconnect...", "warn")
                            time.sleep(wait)
                            # Force reconnect by recreating session
                            try:
                                syn.session = make_session()
                                syn.login()
                            except Exception:
                                pass
                            break  # retry outer loop
                        continue
                else:
                    # All sessions tried this attempt without 502 — no point retrying
                    break

            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                return dest
            raise SynologyError(f"Download fehlgeschlagen item={item.get('id')}: {last_err}")

        def upload_item(item, owner_uname, context="", album_contributor=None):
            """Download via best available session, upload to correct Immich account."""
            iid   = item.get("id")
            fname = item_filename(item)
            t_c   = iso_ts(item.get("time") or item.get("create_time"))
            t_m   = iso_ts(item.get("indexed_time") or item.get("time"))
            key   = (owner_uname, iid)

            if key in item_map:
                migration_state["progress"] += 1
                return item_map[key]

            if dry_run:
                aid = f"dry-{owner_uname}-{iid}"
                item_map[key] = aid
                migration_state["uploaded"] += 1
                migration_state["progress"] += 1
                return aid

            dest = None
            try:
                dest = download_with_retry(item, owner_uname, context)
                set_action(f"↑ {fname} → {owner_uname}")
                result = immich_for(owner_uname).upload_asset(dest, fname, t_c, t_m)
                aid = result.get("id")
                item_map[key] = aid
                if result.get("status") == "duplicate":
                    migration_state["duplicates"] += 1
                else:
                    migration_state["uploaded"] += 1
                    time.sleep(0.05)  # light rate-limit to avoid overwhelming Synology
                migration_state["progress"] += 1
                return aid
            except Exception as e:
                migration_state["failed"] += 1
                migration_state["progress"] += 1
                report["failed"].append({"user": owner_uname, "item": fname,
                                         "context": context, "error": str(e)})
                log(f"  ✗ [{owner_uname}] {fname}: {e}", "error")
                return None
            finally:
                if dest and os.path.exists(dest):
                    os.remove(dest)

        def find_or_create_album(imm_client, primary_uname, name, description):
            """Reuse an existing Immich album with the same name under the primary
            user's account if one exists (e.g. from an earlier interrupted/partial
            run), instead of always creating a new one — otherwise a retry would
            split the same Synology album into two separate Immich albums.
            Returns (album_id, was_reused)."""
            if primary_uname not in imm_albums_cache:
                try:
                    imm_albums_cache[primary_uname] = imm_client.get_albums()
                except Exception:
                    imm_albums_cache[primary_uname] = []
            existing = next((a for a in imm_albums_cache[primary_uname]
                              if a.get("albumName") == name), None)
            if existing and existing.get("id"):
                return existing["id"], True
            created = imm_client.create_album(name, description=description)
            aid = created.get("id")
            # Remember it so a later album in this same run with an identical
            # name (edge case) also finds it instead of creating a duplicate.
            imm_albums_cache[primary_uname].append({"albumName": name, "id": aid})
            return aid, False

        # ── Phase 1: Personal photos ─────────────────────────────────────────
        # Skip personal space scan if user selected specific albums.
        # sel_normal_ids/sel_pps = None means "all" (no filter active).
        skip_personal = (sel_normal_ids is not None or sel_pps is not None)
        if do_personal and not skip_personal:
            migration_state["phase"] = "Phase 1: Persönliche Fotos"
            log("─── Phase 1: Persönliche Fotos ───", "section")
            for uname, sess in sessions.items():
                _check_pause()
                set_action(f"Scanne persönliche Fotos von '{uname}'...")
                log(f"Scanne '{uname}' Personal Space...")
                try:
                    items = sess["syn"].list_all_personal_items()
                except Exception as e:
                    log(f"  ✗ {uname}: {e}", "error")
                    continue

                log(f"  {len(items)} Fotos gefunden")
                migration_state["total"] += len(items)
                u_report = {"uploaded": 0, "duplicates": 0, "failed": 0}

                for item in items:
                    _check_pause()
                    aid = upload_item(item, uname, "Personal")
                    if aid:
                        if item_map.get((uname, item.get("id"))) == aid:
                            if dry_run or migration_state["duplicates"] > len(item_map) - migration_state["uploaded"]:
                                u_report["duplicates"] += 1
                            else:
                                u_report["uploaded"] += 1
                    else:
                        u_report["failed"] += 1

                report["users"][uname] = u_report
                log(f"  ✓ {uname}: {u_report['uploaded']} hoch, "
                    f"{u_report['duplicates']} Dupl., {u_report['failed']} Fehler", "success")

        # ── Phase 2: Albums (Normal + Shared via Passphrase) ─────────────────
        if do_albums:
            migration_state["phase"] = "Phase 2: Alben"
            log("─── Phase 2: Alben (Normal + Geteilte Freigabe-Alben) ───", "section")

            # Cache of each primary user's existing Immich albums (name → id), so a
            # retried/resumed run finds and completes the SAME album instead of
            # creating a duplicate one. Populated lazily, per primary user, below.
            imm_albums_cache = {}

            seen_album_ids, seen_pps = set(), set()
            all_album_jobs = []
            # (album_info_dict, fetch_uname, fetch_syn, is_passphrase)

            for uname, sess in sessions.items():
                _check_pause()
                set_action(f"Lade Albenliste von '{uname}'...")

                # Normal albums
                try:
                    for a in sess["syn"].list_albums():
                        aid = a.get("id")
                        if aid in seen_album_ids:
                            continue
                        if sel_normal_ids is not None and str(aid) not in sel_normal_ids:
                            continue
                        seen_album_ids.add(aid)
                        all_album_jobs.append((a, uname, sess["syn"], False))
                except Exception as e:
                    log(f"  ⚠ Normale Alben von '{uname}': {e}", "warn")

                # Shared/passphrase albums (Freigabe tab)
                try:
                    for p in sess["syn"].get_shared_passphrases():
                        pp = p.get("passphrase")
                        if not pp or pp in seen_pps:
                            continue
                        if sel_pps is not None and pp not in sel_pps:
                            continue
                        seen_pps.add(pp)
                        pseudo = {"id": None, "name": p.get("name") or f"Freigabe_{pp[:6]}",
                                  "passphrase": pp}
                        all_album_jobs.append((pseudo, uname, sess["syn"], True))
                except Exception as e:
                    log(f"  ⚠ Passphrase-Alben von '{uname}': {e}", "warn")

            log(f"  {len(all_album_jobs)} Alben zur Migration")
            migration_state["albums_total"] += len(all_album_jobs)

            for album, fetch_user, fetch_syn, is_pp in all_album_jobs:
                _check_pause()
                aname = album.get("name") or f"Album_{album.get('id')}"
                pp    = album.get("passphrase")
                album_key = f"pp_{pp}" if is_pp else f"normal_{album.get('id')}"
                if album_key in done_albums:
                    log(f"  ↷ '{aname}' — bereits migriert, übersprungen", "info")
                    migration_state["albums_done"] += 1
                    continue
                set_action(f"Album: '{aname}'")
                log(f"Album '{aname}' {'[Freigabe]' if is_pp else '[Normal]'}")

                try:
                    items = (fetch_syn.list_items_in_shared_album(pp)
                             if is_pp else
                             fetch_syn.list_items_in_album(album.get("id")))
                except Exception as e:
                    log(f"  ✗ Inhalte nicht ladbar: {e}", "error")
                    report["albums"].append({"name": aname, "error": str(e)})
                    continue

                if not items:
                    log(f"  ℹ Leer — übersprungen")
                    continue

                migration_state["total"] += len(items)

                # Group by contributor for Immich upload account:
                # provider_user_id = who added to album → determines WHOSE Immich account gets it
                # owner_user_id    = who owns the file → determines which session can DOWNLOAD it
                by_owner = {}
                for it in items:
                    ow = resolve_owner(it)  # uses provider_user_id preferentially
                    by_owner.setdefault(ow, []).append(it)

                # Album owner = user who owns/fetched the album on Synology
                primary     = fetch_user if fetch_user in sessions else list(by_owner.keys())[0]
                imm_primary = immich_for(primary)

                contributor_str = ", ".join(f"{u}: {len(v)} Fotos" for u, v in by_owner.items())
                log(f"  {len(items)} Fotos | Beitragende: {contributor_str} | Owner: {primary}")

                # Track failures across just this album's items, so we only mark
                # the album "done" below if every item actually made it through.
                failed_before = migration_state["failed"]

                # Upload each item to the correct owner's Immich account
                for ow, its in by_owner.items():
                    for it in its:
                        _check_pause()
                        upload_item(it, ow, f"Album:{aname}")
                desc          = (f"Migriert aus Synology Photos "
                                 f"({'Freigabe-Album' if is_pp else 'Album'}). "
                                 f"Beitragende: {contributor_str}")
                # Album sharing in Immich:
                # - primary owner already has owner-level access (creates the album)
                # - ALL other contributors + ALL users who have access to this album get "editor"
                # This matches Synology's shared album behaviour where all members can see photos
                shared_with = set(by_owner.keys()) | set(sessions.keys())  # contributors + all users
                album_users = []
                for ow in shared_with:
                    if ow == primary:
                        continue
                    uid = sessions.get(ow, {}).get("immich_id")
                    if uid:
                        album_users.append({"userId": uid, "role": "editor"})

                if dry_run:
                    report["albums"].append({
                        "name": aname, "type": "passphrase" if is_pp else "normal",
                        "total": len(items),
                        "contributors": {u: len(v) for u, v in by_owner.items()}})
                    log(f"  [DRY] '{aname}' — {len(items)} Fotos, {len(by_owner)} Beitragende")
                    album_step_ok = True
                else:
                    album_step_ok = False
                    try:
                        set_action(f"Suche/erstelle Album '{aname}' in Immich...")

                        # Step 1: Reuse the existing Immich album if this Synology album
                        # was partially migrated in an earlier run — otherwise create it.
                        album_id, reused = find_or_create_album(imm_primary, primary, aname, desc)
                        if reused:
                            log(f"  ↻ Album '{aname}' existiert bereits — ergänze fehlende Fotos", "info")

                        # Step 2: Share album with all other contributors IMMEDIATELY
                        # They need editor rights BEFORE we try to add their assets
                        if album_users and album_id:
                            try:
                                sr = imm_primary.session.put(
                                    f"{imm_primary.base_url}/albums/{album_id}/users",
                                    json={"albumUsers": album_users}, timeout=30)
                                if not sr.ok:
                                    log(f"  ⚠ Freigabe '{aname}': {sr.status_code} {sr.text[:60]}", "warn")
                            except Exception as e:
                                log(f"  ⚠ Freigabe '{aname}': {e}", "warn")

                        # Step 3: Each contributor adds THEIR OWN assets using THEIR OWN session
                        # This avoids the no_permission error (you can only add your own assets)
                        time.sleep(0.3)  # brief pause for Immich to process sharing
                        total_added = 0
                        add_to_album_failed = False
                        owner_asset_map = {}  # ow → [asset_ids]
                        for ow, its in by_owner.items():
                            owner_asset_map[ow] = []
                            for it in its:
                                iid = it.get("id")
                                aid = item_map.get((ow, iid))
                                if aid and not str(aid).startswith("dry-"):
                                    owner_asset_map[ow].append(aid)

                        for ow, aids in owner_asset_map.items():
                            if not aids or not album_id:
                                continue
                            imm_ow = immich_for(ow)
                            try:
                                set_action(f"Füge {len(aids)} Fotos von {ow} zu '{aname}' hinzu...")
                                imm_ow.add_assets_to_album(album_id, aids)
                                total_added += len(aids)
                                log(f"  + [{ow}] {len(aids)} Fotos hinzugefügt", "info")
                            except Exception as e:
                                add_to_album_failed = True
                                log(f"  ✗ [{ow}] Fotos zu Album: {e}", "error")

                        report["albums"].append({
                            "name": aname, "immich_id": album_id,
                            "type": "passphrase" if is_pp else "normal",
                            "total": len(items), "migrated": total_added,
                            "contributors": {u: len(v) for u, v in by_owner.items()}})
                        log(f"  ✓ '{aname}' — {total_added} Fotos, "
                            f"{len(album_users)} Freigaben eingerichtet", "success")
                        album_step_ok = not add_to_album_failed
                    except Exception as e:
                        log(f"  ✗ '{aname}': {e}", "error")
                        report["albums"].append({"name": aname, "error": str(e)})

                # Only persist this album as "done" (skip on next run) if every item
                # of it uploaded successfully AND the album/sharing step itself
                # succeeded. Otherwise it's deliberately left out of done_albums so
                # a re-run picks it up again and completes/repairs it — it will NOT
                # be recreated from scratch, find_or_create_album() above reuses it.
                had_item_failures = migration_state["failed"] > failed_before
                album_key = f"pp_{pp}" if is_pp else f"normal_{album.get('id')}"
                if album_step_ok and not had_item_failures:
                    done_albums.add(album_key)
                    save_migration_state(done_albums, resume_key)
                else:
                    log(f"  ⚠ '{aname}' — unvollständig, wird beim nächsten Lauf erneut geprüft", "warn")
                migration_state["albums_done"] += 1

        # ── Phase 3: Shared Space (optional, via admin) ───────────────────────
        # Only runs when explicitly enabled AND no specific album selection was made
        if do_shared and not skip_personal:
            migration_state["phase"] = "Phase 3: Team/Shared Space"
            log("─── Phase 3: Team Space ───", "section")
            # Use first user with admin rights (or just first session)
            admin_sess = sessions[fallback]
            try:
                set_action("Lade Team Space Fotos...")
                team_items = admin_sess["syn"].list_all_team_items()
                log(f"  {len(team_items)} Fotos im Team Space")
                migration_state["total"] += len(team_items)
                for it in team_items:
                    _check_pause()
                    ow  = resolve_owner(it)
                    key = (ow, it.get("id"))
                    if key not in item_map:  # skip re-download if already uploaded in phase 1/2
                        upload_item(it, ow, "TeamSpace")
                    else:
                        migration_state["progress"] += 1
            except Exception as e:
                log(f"  ✗ Team Space: {e}", "error")

        # ── Cleanup & report ──────────────────────────────────────────────────
        set_action("Aufräumen...")
        for sess in sessions.values():
            sess["syn"].logout()
        shutil.rmtree(tmp_dir, ignore_errors=True)

        # Delete resume state — run completed successfully, next run starts fresh
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

        report["finished_at"] = datetime.now().isoformat()
        report["summary"]     = {
            "uploaded":   migration_state["uploaded"],
            "duplicates": migration_state["duplicates"],
            "failed":     migration_state["failed"],
            "albums":     migration_state["albums_done"]}

        report_path = os.path.join(
            app_dir(), f"migration_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        migration_state["report"]         = report_path
        migration_state["status"]         = "done"
        migration_state["current_action"] = "Abgeschlossen"
        log(f"✓ Migration abgeschlossen! Report: {os.path.basename(report_path)}", "success")

    except Exception as e:
        migration_state["status"]         = "error"
        migration_state["current_action"] = f"Fehler: {e}"
        log(f"Schwerwiegender Fehler: {e}", "error")
        log(traceback.format_exc(), "error")
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# Debug / inspect  (uses user credentials, not admin)
# ─────────────────────────────────────────────────────────────────────────────
def inspect_user(cfg, username):
    users = {u["synology_username"]: u for u in cfg.get("users", [])}
    u = users.get(username)
    result = {}
    if u and u.get("synology_password"):
        login_user, login_pass, login_otp = username, u["synology_password"], u.get("synology_otp","")
        result["login_as"] = f"{username} (eigener Account)"
    else:
        login_user = cfg.get("synology_admin_user","")
        login_pass = cfg.get("synology_admin_pass","")
        login_otp  = cfg.get("synology_admin_otp","")
        result["login_as"] = f"{login_user} (Admin-Fallback)"
    try:
        syn = SynologyClient(cfg["synology_url"], login_user, login_pass, login_otp)
        syn.login()
        result["login"] = "ok"

        # Personal items
        try:
            personal = syn.list_all_personal_items()
            result["personal_items_total"] = len(personal)
            result["sample_personal_item"] = personal[0] if personal else None
        except Exception as e:
            result["personal_items_error"] = str(e)

        # Team items
        try:
            team = syn.list_all_team_items()
            result["team_items_total"] = len(team)
        except Exception as e:
            result["team_items_error"] = str(e)

        # Normal albums
        albums = []
        try:
            albums = syn.list_albums()
            result["albums_normal"] = [
                {"id": a.get("id"), "name": a.get("name") or a.get("title"),
                 "item_count": a.get("item_count")} for a in albums[:20]]
        except Exception as e:
            result["albums_normal_error"] = str(e)

        # Passphrase/shared albums
        try:
            pps = syn.get_shared_passphrases()
            result["passphrase_albums_count"] = len(pps)
            result["passphrase_albums"] = [
                {"passphrase": p.get("passphrase"), "name": p.get("name")} for p in pps[:10]]
            if pps:
                pp = pps[0].get("passphrase")
                try:
                    pp_items = syn.list_items_in_shared_album(pp)
                    result["sample_passphrase_album_count"] = len(pp_items)
                    result["sample_passphrase_item"]        = pp_items[0] if pp_items else None
                    if pp_items:
                        result["owner_fields"] = {
                            "owner_user_id":    pp_items[0].get("owner_user_id"),
                            "provider_user_id": (pp_items[0].get("additional") or {}).get("provider_user_id"),
                        }
                except Exception as e2:
                    result["sample_passphrase_error"] = str(e2)
        except Exception as e:
            result["passphrase_albums_error"] = str(e)

        # Sample album items
        if albums:
            try:
                items = syn.list_items_in_album(albums[0]["id"])
                result["sample_album_item_count"] = len(items)
                result["sample_album_item"]        = items[0] if items else None
            except Exception as e:
                result["sample_album_error"] = str(e)

        # UID map
        try:
            dsm_users = syn.list_dsm_users()
            result["uid_map"] = {str(u.get("uid")): u.get("name") for u in dsm_users if u.get("uid")}
        except Exception as e:
            result["uid_map_note"] = str(e)

        syn.logout()
    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Preview scan  (uses per-user sessions for correct album visibility)
# ─────────────────────────────────────────────────────────────────────────────
def preview_scan(cfg):
    out = {"users": {}, "normal_albums": 0, "passphrase_albums": 0,
           "unique_albums": 0, "team_items": 0, "errors": []}
    users_cfg = [u for u in cfg.get("users", [])
                 if u.get("synology_username") and u.get("synology_password")]
    seen_ids, seen_pps = set(), set()
    for u in users_cfg:
        uname = u["synology_username"]
        uinfo = {"normal_albums": 0, "passphrase_albums": 0, "personal_items": 0}
        try:
            syn = SynologyClient(cfg["synology_url"], uname,
                                 u["synology_password"], u.get("synology_otp",""))
            syn.login()
            try:
                items = syn.list_all_personal_items()
                uinfo["personal_items"] = len(items)
                out["team_items"] += len(items)
            except Exception as e:
                out["errors"].append({"user": uname, "stage": "personal", "error": str(e)})
            try:
                albums = syn.list_albums()
                new = [a for a in albums if a.get("id") not in seen_ids]
                for a in new: seen_ids.add(a.get("id"))
                uinfo["normal_albums"] = len(new)
                out["normal_albums"] += len(new)
            except Exception as e:
                out["errors"].append({"user": uname, "stage": "albums", "error": str(e)})
            try:
                pps = syn.get_shared_passphrases()
                new_pp = [p for p in pps if p.get("passphrase") not in seen_pps]
                for p in new_pp: seen_pps.add(p.get("passphrase"))
                uinfo["passphrase_albums"] = len(new_pp)
                out["passphrase_albums"] += len(new_pp)
            except Exception as e:
                out["errors"].append({"user": uname, "stage": "passphrases", "error": str(e)})
            syn.logout()
        except Exception as e:
            out["errors"].append({"user": uname, "stage": "login", "error": str(e)})
        out["users"][uname] = uinfo
    out["unique_albums"] = out["normal_albums"] + out["passphrase_albums"]
    return out


HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synology → Immich</title>
<style>
:root{
  --bg:#111318;--surface:#1a1d25;--card:#21252f;--border:#2a2f3d;
  --accent:#6366f1;--accent-dim:rgba(99,102,241,.12);
  --green:#22c55e;--red:#ef4444;--amber:#f59e0b;--blue:#3b82f6;--purple:#a855f7;
  --muted:#64748b;--sub:#b0bec5;--text:#e2e8f0;
  --mono:'JetBrains Mono','Fira Code',monospace;
  --sans:-apple-system,BlinkMacSystemFont,'Inter',system-ui,sans-serif;
  --radius:10px;--radius-sm:6px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{font-size:15px}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;-webkit-font-smoothing:antialiased}

/* ── Layout ── */
.app{display:grid;grid-template-columns:220px 1fr;min-height:100vh}
nav{background:var(--surface);border-right:1px solid var(--border);padding:20px 12px;display:flex;flex-direction:column;gap:2px;position:sticky;top:0;height:100vh;overflow-y:auto}
.nav-logo{display:flex;align-items:center;gap:8px;padding:8px 10px;margin-bottom:14px}
.nav-logo-icon{width:28px;height:28px;background:var(--accent);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px}
.nav-logo-text{font-size:.82rem;font-weight:600;color:var(--text)}
.nav-logo-sub{font-size:.68rem;color:var(--muted)}
.nav-item{display:flex;align-items:center;gap:9px;padding:9px 10px;border-radius:var(--radius-sm);cursor:pointer;font-size:.86rem;color:var(--sub);transition:all .15s;user-select:none}
.nav-item:hover{background:rgba(255,255,255,.04);color:var(--text)}
.nav-item.active{background:var(--accent-dim);color:var(--accent);font-weight:500}
.nav-item .ni{width:16px;text-align:center;flex-shrink:0}
.nav-badge{margin-left:auto;background:var(--red);color:#fff;font-size:.6rem;padding:1px 5px;border-radius:10px;display:none}
.nav-divider{height:1px;background:var(--border);margin:8px 4px}
.nav-hint{font-size:.68rem;color:var(--muted);padding:6px 10px;line-height:1.5}
.panel{padding:32px 40px;overflow-y:auto;max-height:100vh}
.content{max-width:760px}
.section{display:none}.section.visible{display:block;max-width:780px}

/* ── Typography ── */
h1{font-size:1.5rem;font-weight:600;letter-spacing:-.01em}
.page-sub{color:var(--sub);font-size:.88rem;margin-top:5px;margin-bottom:24px;line-height:1.6}
.section-label{font-size:.68rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px}
.dry-pill{display:inline-flex;align-items:center;gap:5px;background:rgba(245,158,11,.12);color:var(--amber);font-size:.68rem;font-weight:600;padding:2px 9px;border-radius:20px;margin-left:8px;vertical-align:middle}

/* ── Light mode ── */
body.light{
  --bg:#f8fafc;--surface:#ffffff;--card:#f1f5f9;--border:#e2e8f0;
  --muted:#94a3b8;--sub:#64748b;--text:#0f172a;
  --accent:#4f46e5;--accent-dim:rgba(79,70,229,.08);
}
body.light input,body.light select{background:#fff;color:#0f172a}
body.light .log-mini,body.light .action-banner{background:#f8fafc}
body.light pre.debug{background:#f1f5f9;color:#0f172a}

/* ── Migration controls ── */
.mig-controls{margin-top:18px;display:flex;flex-direction:column;gap:12px}
.mig-primary{display:flex;gap:10px}
.mig-secondary{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.btn-lg{padding:11px 24px;font-size:.95rem;font-weight:600}
.btn-success{background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.3);color:var(--green)}
.mig-hint{font-size:.75rem;color:var(--muted);padding:4px 0}
.mig-reset{display:flex;align-items:center;gap:10px;padding-top:4px;border-top:1px solid var(--border)}
.mig-reset-hint{font-size:.72rem;color:var(--muted)}

/* ── Selection summary box ── */
.sel-title{font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:4px}
.sel-main{font-size:.95rem;font-weight:500;color:var(--text)}
.sel-names{font-size:.78rem;color:var(--muted);margin-top:3px}

/* ── Migration page cards (groups status/progress and live-activity into one visual unit each) ── */
.mig-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:16px 18px;margin-bottom:16px}
.mig-card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}

/* ── Cards ── */
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;font-size:.92rem}
.card+.card{margin-top:12px}

/* ── Forms ── */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.fg{display:flex;flex-direction:column;gap:5px}
.fg.full{grid-column:1/-1}
label{font-size:.71rem;color:var(--muted);font-weight:500;text-transform:uppercase;letter-spacing:.05em}
input,select{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:var(--radius-sm);padding:9px 12px;font-size:.9rem;font-family:var(--sans);outline:none;transition:border-color .15s,box-shadow .15s;width:100%}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-dim)}
.field-hint{font-size:.69rem;color:var(--muted);margin-top:2px}

/* ── Status dots ── */
.status-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:16px}
.status-item{display:flex;align-items:center;gap:10px;padding:12px 14px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm)}
.sdot{width:8px;height:8px;border-radius:50%;background:var(--border);flex-shrink:0;transition:all .3s}
.sdot.ok{background:var(--green);box-shadow:0 0 6px var(--green)}
.sdot.err{background:var(--red);box-shadow:0 0 6px var(--red)}
.sdot.spin{animation:pulse .9s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.2}}
.sdot-label{font-size:.8rem;font-weight:500}
.sdot-sub{font-size:.69rem;color:var(--muted);margin-top:1px;line-height:1.4;word-break:break-all}

/* ── Toggles ── */
.toggle-list{display:flex;flex-direction:column;gap:8px}
.toggle-row{display:flex;align-items:center;justify-content:space-between;padding:13px 16px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm)}
.toggle-info .tl{font-size:.84rem;font-weight:500}
.toggle-info .td{font-size:.72rem;color:var(--muted);margin-top:2px}
.tog{position:relative;width:38px;height:21px;background:var(--border);border-radius:11px;cursor:pointer;transition:background .2s;flex-shrink:0}
.tog.on{background:var(--accent)}
.tog::after{content:'';position:absolute;top:3px;left:3px;width:15px;height:15px;border-radius:50%;background:#fff;transition:transform .2s;box-shadow:0 1px 3px rgba(0,0,0,.3)}
.tog.on::after{transform:translateX(17px)}

/* ── Buttons ── */
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border-radius:var(--radius-sm);border:none;cursor:pointer;font-size:.82rem;font-weight:500;font-family:var(--sans);transition:all .15s}
.btn:hover{filter:brightness(1.1)}.btn:active{transform:scale(.98)}.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-primary{background:var(--accent);color:#fff}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--sub)}
.btn-ghost:hover{border-color:var(--text);color:var(--text)}
.btn-green{background:var(--green);color:#000}
.btn-amber{background:rgba(245,158,11,.15);border:1px solid rgba(245,158,11,.3);color:var(--amber)}
.btn-sm{padding:5px 10px;font-size:.74rem}
.btn-row{display:flex;gap:8px;margin-top:20px;flex-wrap:wrap;align-items:center}

/* ── User table ── */
.user-table{width:100%;border-collapse:collapse;margin-top:10px;font-size:.8rem}
.user-table th{text-align:left;font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;padding:4px 8px;border-bottom:1px solid var(--border)}
.user-table td{padding:5px 8px;border-bottom:1px solid var(--border);vertical-align:middle}
.user-table input{padding:6px 9px;font-size:.78rem}
.rs{width:12px;height:12px;border-radius:50%;background:var(--border);display:inline-block}
.rs.ok{background:var(--green)}.rs.err{background:var(--red)}.rs.spin{animation:pulse .9s infinite;background:var(--amber)}

/* ── Collapsible debug ── */
.collap-trigger{display:flex;align-items:center;gap:8px;cursor:pointer;padding:10px 0;color:var(--muted);font-size:.78rem;border-top:1px solid var(--border);margin-top:20px;user-select:none}
.collap-trigger:hover{color:var(--sub)}
.collap-body{display:none;padding-top:12px}
.collap-body.open{display:block}
pre.debug{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px;font-family:var(--mono);font-size:.71rem;max-height:340px;overflow:auto;white-space:pre-wrap;word-break:break-all}

/* ── Album list ── */
.album-toolbar{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.chip{padding:4px 10px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--muted);font-size:.72rem;cursor:pointer;font-family:var(--sans);transition:all .15s}
.chip:hover{border-color:var(--sub);color:var(--text)}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.chip.on-green{background:var(--green);border-color:var(--green);color:#000}
.chip.on-amber{background:var(--amber);border-color:var(--amber);color:#000}
.chip.on-purple{background:var(--purple);border-color:var(--purple);color:#fff}
.chip-sep{width:1px;height:18px;background:var(--border)}
.album-search{flex:1;min-width:140px;max-width:200px;padding:5px 10px;font-size:.76rem;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:20px;outline:none}
.album-search:focus{border-color:var(--accent)}
.sort-sel{padding:4px 8px;font-size:.73rem;background:var(--bg);border:1px solid var(--border);color:var(--sub);border-radius:20px;outline:none;cursor:pointer}
.album-sel-count{font-size:.72rem;color:var(--muted);margin-left:auto}
.album-list{border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;max-height:380px;overflow-y:auto}
.album-item{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .12s}
.album-item:last-child{border-bottom:none}
.album-item:hover{background:rgba(255,255,255,.02)}
.album-item input[type=checkbox]{width:15px;height:15px;accent-color:var(--accent);flex-shrink:0;cursor:pointer}
.album-meta{flex:1;min-width:0}
.album-name{font-size:.9rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.album-info{font-size:.7rem;color:var(--muted);margin-top:2px}
.badge{display:inline-flex;align-items:center;gap:4px;font-size:.66rem;font-weight:600;padding:2px 7px;border-radius:20px;flex-shrink:0}
.badge-normal{background:rgba(59,130,246,.12);color:var(--blue)}
.badge-shared{background:rgba(168,85,247,.12);color:var(--purple)}
.badge-link{background:rgba(245,158,11,.12);color:var(--amber)}
.album-count{font-size:.72rem;color:var(--muted);font-family:var(--mono);flex-shrink:0}
.album-count.partial{color:var(--amber)}
.album-count.done{color:var(--green)}
.album-item.hidden-row{display:none}
.album-item.album-done{opacity:.5}
.album-item.album-done .album-name{text-decoration:line-through}
.album-done-check{color:var(--green);margin-right:2px}
.album-item.album-excluded{opacity:.4}
.album-item.album-excluded .album-name{text-decoration:line-through}
.album-exclude-btn{flex-shrink:0;width:22px;height:22px;border-radius:50%;border:1px solid var(--border);background:transparent;color:var(--muted);font-size:.75rem;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .12s}
.album-exclude-btn:hover{border-color:var(--red);color:var(--red);background:rgba(239,68,68,.08)}
.album-item.album-excluded .album-exclude-btn{color:var(--accent)}
.album-item.album-excluded .album-exclude-btn:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-dim)}
#toggle-excluded.on{background:var(--accent);border-color:var(--accent);color:#fff}

/* ── Stat cards ── */
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:14px}
.stat-val{font-family:var(--mono);font-size:1.6rem;font-weight:700}
.stat-label{font-size:.69rem;color:var(--muted);margin-top:3px}
.sv-green{color:var(--green)}.sv-amber{color:var(--amber)}.sv-red{color:var(--red)}.sv-blue{color:var(--blue)}

/* ── Progress ── */
.prog-wrap{background:var(--border);border-radius:3px;height:4px;overflow:hidden;margin:12px 0}
.prog-bar{height:100%;background:var(--accent);transition:width .4s;border-radius:3px}
.prog-label{font-size:.72rem;color:var(--muted);margin-bottom:12px}

/* ── Action banner ── */
.action-banner{background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.2);border-radius:var(--radius-sm);padding:10px 14px;font-size:.78rem;font-family:var(--mono);color:var(--accent);min-height:38px;word-break:break-all;margin-bottom:12px}

/* ── Status pill ── */
.status-pill{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;font-size:.74rem;font-weight:600;margin-bottom:12px}
.sp-run{background:rgba(99,102,241,.12);color:var(--accent)}
.sp-pause{background:rgba(59,130,246,.12);color:var(--blue)}
.sp-done{background:rgba(34,197,94,.12);color:var(--green)}
.sp-err{background:rgba(239,68,68,.12);color:var(--red)}
.sp-idle{background:var(--border);color:var(--muted)}
.dot-blink{width:6px;height:6px;border-radius:50%;background:currentColor;animation:pulse .9s infinite}

/* ── Mini log (live-activity box on the Migration page) ── */
.log-mini{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);height:180px;overflow-y:auto;padding:8px 12px;font-family:var(--mono);font-size:.72rem}
.ll{display:flex;gap:9px;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.02);line-height:1.7}
.ll:last-child{border-bottom:none}
.ll-ts{color:var(--muted);flex-shrink:0;font-size:.74rem;padding-top:2px;width:52px}
.ll-lv{flex-shrink:0;width:54px;font-size:.72rem;font-weight:600;text-transform:uppercase;padding-top:2px}
.ll-msg{flex:1;word-break:break-word;white-space:pre-wrap}
.ll.info .ll-lv{color:var(--muted)}.ll.info .ll-msg{color:var(--text)}
.ll.success .ll-lv,.ll.success .ll-msg{color:var(--green)}
.ll.error .ll-lv,.ll.error .ll-msg{color:var(--red)}
.ll.warn .ll-lv,.ll.warn .ll-msg{color:var(--amber)}
.ll.section .ll-lv,.ll.section .ll-msg{color:var(--blue);font-weight:600}
.ll.error{background:rgba(239,68,68,.04);border-radius:3px}

/* ── Preview grid ── */
.prev-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:16px}
.prev-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px}
.prev-num{font-family:var(--mono);font-size:1.3rem;font-weight:700;color:var(--accent)}
.prev-label{font-size:.7rem;color:var(--muted);margin-top:3px}
</style>
</head>
<body>
<div class="app">

<!-- ── Sidebar nav ── -->
<nav>
  <div class="nav-logo">
    <div class="nav-logo-icon">→</div>
    <div><div class="nav-logo-text">Syn → Immich</div><div class="nav-logo-sub">Migration</div></div>
  </div>
  <div class="nav-item active" onclick="goStep(0)"><span class="ni">⚙</span> Setup</div>
  <div class="nav-item" onclick="goStep(1)"><span class="ni">≡</span> Optionen</div>
  <div class="nav-item" onclick="goStep(2)"><span class="ni">◫</span> Album-Auswahl</div>
  <div class="nav-item" onclick="goStep(3)">
    <span class="ni">▶</span> Migration
    <span class="nav-badge" id="log-err-badge">0</span>
  </div>
  <div class="nav-divider"></div>
  <div style="padding:6px 10px">
    <button class="btn btn-ghost btn-sm" onclick="toggleTheme()" id="theme-btn" style="width:100%;justify-content:center">
      🌙 Dark Mode
    </button>
  </div>
  <div class="nav-divider"></div>
  <div class="nav-hint">Zugangsdaten werden lokal in <code style="font-size:.65rem">migration_config.json</code> gespeichert.</div>
</nav>

<!-- ── Content ── -->
<div class="panel" id="mainPanel">

<!-- Step 0: Setup -->
<div class="section visible" id="step-0">
  <h1>Setup</h1>
  <p class="page-sub">Server-URLs und Zugangsdaten. Jeder User braucht sein Synology-Passwort und einen Immich-API-Key.</p>

  <div class="card">
    <div class="section-label">Server</div>
    <div class="form-grid">
      <div class="fg"><label>Synology URL</label>
        <input id="synology_url" placeholder="https://192.168.1.10:5001">
        <span class="field-hint">Mit https:// und Port</span></div>
      <div class="fg"><label>Immich URL</label>
        <input id="immich_url" placeholder="http://192.168.1.20:2283"></div>
      <div class="fg"><label>Admin-Username</label>
        <input id="synology_admin_user" placeholder="admin">
        <span class="field-hint">Für UID-Auflösung (Beitragenden-Zuordnung)</span></div>
      <div class="fg"><label>Admin-Passwort</label>
        <input id="synology_admin_pass" type="password" placeholder="••••••••"></div>
      <div class="fg"><label>2FA-Code (optional)</label>
        <input id="synology_admin_otp" placeholder="123456" maxlength="6"></div>
    </div>
    <div class="status-row">
      <div class="status-item"><div class="sdot" id="dot-syn"></div>
        <div><div class="sdot-label">Synology</div><div class="sdot-sub" id="sub-syn">Nicht getestet</div></div></div>
      <div class="status-item"><div class="sdot" id="dot-imm"></div>
        <div><div class="sdot-label">Immich</div><div class="sdot-sub" id="sub-imm">Nicht getestet</div></div></div>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="section-label">User & Zugänge</div>
    <p style="font-size:.75rem;color:var(--muted);margin-bottom:10px">User müssen in der <code style="font-size:.7rem">administrators</code>-Gruppe sein. Das Synology-Passwort wird für den Zugriff auf geteilte Alben benötigt.</p>
    <table class="user-table" id="userTable">
      <thead><tr><th></th><th>Synology-User</th><th>Passwort</th><th>Immich API-Key</th><th>Status</th><th></th></tr></thead>
      <tbody id="userTableBody"></tbody>
    </table>
    <div class="btn-row" style="margin-top:10px">
      <button class="btn btn-ghost btn-sm" onclick="addUserRow()">+ User</button>
      <button class="btn btn-ghost btn-sm" onclick="discoverUsers()">↓ User von NAS laden</button>
      <button class="btn btn-ghost btn-sm" onclick="testAllUsers()">Alle testen</button>
    </div>
  </div>

  <!-- Debug (collapsible) -->
  <div class="collap-trigger" onclick="toggleDebug()">
    <span id="collap-arrow">▶</span>
    <span style="font-weight:500">Debug / Rohdaten</span>
    <span style="font-size:.7rem;color:var(--muted)">— Synology API-Felder prüfen</span>
  </div>
  <div class="collap-body" id="debug-body">
    <div class="form-grid" style="max-width:300px;margin-bottom:10px">
      <div class="fg full"><label>User</label><select id="inspectUser"></select></div>
    </div>
    <button class="btn btn-ghost btn-sm" onclick="runInspect()" style="margin-bottom:12px">Rohdaten abrufen</button>
    <div id="inspectResult"></div>
  </div>

  <div class="btn-row">
    <button class="btn btn-ghost" onclick="testConnections()">Verbindung testen</button>
    <button class="btn btn-primary" onclick="saveAndNext(0)">Weiter →</button>
  </div>
</div>

<!-- Step 1: Optionen -->
<div class="section" id="step-1">
  <h1>Optionen <span id="dryBadge"></span></h1>
  <p class="page-sub">Was soll migriert werden?</p>
  <div class="card">
    <div class="toggle-list">
      <div class="toggle-row"><div class="toggle-info"><div class="tl">Persönliche Fotos</div>
        <div class="td">Jeder User → eigener Immich-Account</div></div>
        <div class="tog on" id="tog-personal" onclick="toggleOpt(this,'migrate_personal')"></div></div>
      <div class="toggle-row"><div class="toggle-info"><div class="tl">Alben & geteilte Freigabe-Alben</div>
        <div class="td">Normale Alben + Passphrase-Alben mit korrekter User-Zuordnung</div></div>
        <div class="tog on" id="tog-albums" onclick="toggleOpt(this,'migrate_shared_albums')"></div></div>
      <div class="toggle-row"><div class="toggle-info"><div class="tl">Shared Space (Team-Bibliothek)</div>
        <div class="td">Physischer Ordner /volume1/photo — optional</div></div>
        <div class="tog" id="tog-shared" onclick="toggleOpt(this,'migrate_shared_space')"></div></div>
      <div class="toggle-row"><div class="toggle-info"><div class="tl">Dry Run</div>
        <div class="td">Nur simulieren, nichts hochladen</div></div>
        <div class="tog" id="tog-dry" onclick="toggleOpt(this,'dry_run')"></div></div>
    </div>
  </div>
  <div class="btn-row">
    <button class="btn btn-ghost" onclick="goStep(0)">← Zurück</button>
    <button class="btn btn-primary" onclick="saveAndNext(1)">Weiter →</button>
  </div>
</div>

<!-- Step 2: Album-Auswahl -->
<div class="section" id="step-2">
  <h1>Album-Auswahl <span id="dryBadge2"></span></h1>
  <p class="page-sub">Wähle welche Alben migriert werden. Beitragende (📸) werden dem richtigen Immich-Account zugeordnet.</p>

  <div id="scan-empty" style="color:var(--muted);font-size:.82rem;margin-bottom:12px">Klicke „Scannen" um Alben zu laden...</div>
  <div id="scan-ui" style="display:none">
    <div class="prev-grid" id="prev-grid"></div>
    <div class="album-toolbar">
      <button class="chip on" onclick="selectAllAlbums(true)">✓ Alle</button>
      <button class="chip" onclick="selectAllAlbums(false)">✗ Keine</button>
      <div class="chip-sep"></div>
      <div id="user-chips" style="display:flex;gap:5px;flex-wrap:wrap"></div>
      <div class="chip-sep"></div>
      <button class="chip type-chip on" data-type="all"        onclick="setTypeFilter('all')">Alle</button>
      <button class="chip type-chip"    data-type="normal"     onclick="setTypeFilter('normal')">📁 Eigene</button>
      <button class="chip type-chip"    data-type="shared"     onclick="setTypeFilter('shared')">👥 Geteilt</button>
      <button class="chip type-chip"    data-type="passphrase" onclick="setTypeFilter('passphrase')">🔗 Freigabe</button>
      <div class="chip-sep"></div>
      <button class="chip" id="toggle-excluded" onclick="toggleShowExcluded()">🚫 <span id="excluded-count">0</span> ausgeschlossen</button>
      <div class="chip-sep"></div>
      <input class="album-search" id="album-search" placeholder="🔍 Suchen..." oninput="applyAlbumFilters()">
      <select class="sort-sel" id="album-sort" onchange="applyAlbumFilters()">
        <option value="type">Typ</option>
        <option value="name">Name</option>
        <option value="count-desc">Fotos ↓</option>
        <option value="count-asc">Fotos ↑</option>
      </select>
      <span class="album-sel-count" id="album-sel-count"></span>
    </div>
    <div class="album-list" id="album-list"></div>
  </div>

  <div class="btn-row">
    <button class="btn btn-ghost" onclick="goStep(1)">← Zurück</button>
    <button class="btn btn-ghost" onclick="runPreview()">↻ Scannen</button>
    <button class="btn btn-primary" onclick="confirmAndNext()">Weiter →</button>
  </div>
</div>

<!-- Step 3: Migration -->
<div class="section" id="step-3">
  <h1>Migration <span id="dryBadge3"></span></h1>

  <!-- Selected albums summary -->
  <div class="mig-card" id="selection-box">
    <div class="sel-title">Ausgewählte Alben</div>
    <div id="selected-summary" class="sel-main">—</div>
    <div id="selected-names" class="sel-names"></div>
  </div>

  <!-- Status + progress — grouped into one card so it reads as a single unit -->
  <div class="mig-card">
    <div id="status-area"><div class="status-pill sp-idle">Bereit zum Start</div></div>
    <div class="action-banner" id="action-banner">Bereit zum Start...</div>
    <div class="prog-wrap"><div class="prog-bar" id="prog-bar" style="width:0%"></div></div>
    <div class="prog-label" id="prog-label">—</div>
  </div>

  <!-- Counters -->
  <div class="stat-grid">
    <div class="stat-card"><div class="stat-val sv-green" id="s-up">0</div><div class="stat-label">Hochgeladen</div></div>
    <div class="stat-card"><div class="stat-val sv-amber" id="s-dup">0</div><div class="stat-label">Duplikate</div></div>
    <div class="stat-card"><div class="stat-val sv-red"   id="s-fail">0</div><div class="stat-label">Fehler</div></div>
    <div class="stat-card"><div class="stat-val sv-blue"  id="s-alb">0</div><div class="stat-label">Alben</div></div>
  </div>

  <!-- Live activity -->
  <div class="mig-card">
    <div class="mig-card-head">
      <span class="section-label" style="margin:0">Live-Aktivität</span>
      <div style="display:flex;gap:6px">
        <button class="btn btn-ghost btn-sm" onclick="copyAllLogs()">Kopieren</button>
        <button class="btn btn-ghost btn-sm" onclick="exportLogs()">Export</button>
      </div>
    </div>
    <div class="log-mini" id="log-mini"></div>
  </div>

  <!-- Migration controls -->
  <div class="mig-controls">
    <!-- Primary actions -->
    <div class="mig-primary">
      <button class="btn btn-primary btn-lg" onclick="startMigration()" id="btn-start">
        ▶ Migration starten
      </button>
      <button class="btn btn-amber btn-lg" onclick="togglePause()" id="btn-pause" style="display:none">
        ⏸ Pausieren
      </button>
    </div>
    <!-- Secondary actions -->
    <div class="mig-secondary">
      <button class="btn btn-ghost" onclick="goStep(2)" id="btn-back">← Album-Auswahl</button>
      <button class="btn btn-success" onclick="goToNewRun()" id="btn-new-run" style="display:none">
        ↻ Neues Album migrieren
      </button>
      <button class="btn btn-green" onclick="downloadReport()" id="btn-report" style="display:none">
        ↓ Report herunterladen
      </button>
    </div>
    <!-- Hint during run -->
    <div id="btn-stop-hint" class="mig-hint" style="display:none">
      Zum Abbrechen: Fenster schließen und neu starten
    </div>
    <!-- Reset (always visible but subtle) -->
    <div class="mig-reset">
      <button class="btn btn-ghost btn-sm" onclick="resetState()">↺ Fortschritt zurücksetzen</button>
      <span class="mig-reset-hint">Setzt gespeicherte Album-Fortschritte zurück (Fotos werden als Duplikate erkannt)</span>
    </div>
  </div>
</div>

</div><!-- /panel -->
</div><!-- /app -->

<script>
'use strict';
// ── State ──────────────────────────────────────────────────────────────────
let cfg = {users:[], options:{}};
let opts = {migrate_personal:true, migrate_shared_albums:true, migrate_shared_space:false, dry_run:false};
let scannedAlbums=[], selectedIds=new Set();
let activeUserFilter='all', activeTypeFilter='all';
// Manually-excluded albums (for decluttering large migrations). Persisted in
// this browser via localStorage — independent of migration_config.json, so it
// survives config resets and works purely as a display/selection preference.
let excludedIds=new Set();
try{ excludedIds=new Set(JSON.parse(localStorage.getItem('synmig_excluded_albums')||'[]')); }catch(e){}
let showExcluded=false;
let allLogs=[], lastLogLen=0;
let polling=null, isPaused=false;

// ── Config ─────────────────────────────────────────────────────────────────
async function loadConfig(){
  try{
    const r=await fetch('/api/config'); cfg=await r.json();
    ['synology_url','synology_admin_user','synology_admin_pass','synology_admin_otp','immich_url']
      .forEach(k=>{if(cfg[k]) document.getElementById(k).value=cfg[k];});
    opts=Object.assign(opts,cfg.options||{});
    syncToggles(); renderUserTable(cfg.users||[]);
  }catch(e){}
}
function syncToggles(){
  setTog('tog-personal',opts.migrate_personal);
  setTog('tog-albums',opts.migrate_shared_albums);
  setTog('tog-shared',opts.migrate_shared_space);
  setTog('tog-dry',opts.dry_run);
  const b=opts.dry_run?'<span class="dry-pill">DRY RUN</span>':'';
  ['dryBadge','dryBadge2','dryBadge3'].forEach(id=>document.getElementById(id).innerHTML=b);
}
function setTog(id,v){document.getElementById(id).classList.toggle('on',!!v);}
function toggleOpt(el,k){opts[k]=!opts[k];el.classList.toggle('on',opts[k]);syncToggles();}
function readForm(){
  return{
    synology_url:document.getElementById('synology_url').value.trim(),
    synology_admin_user:document.getElementById('synology_admin_user').value.trim(),
    synology_admin_pass:document.getElementById('synology_admin_pass').value,
    synology_admin_otp:document.getElementById('synology_admin_otp').value.trim(),
    immich_url:document.getElementById('immich_url').value.trim(),
    users:readUserTable(), options:opts,
  };
}
async function saveAndNext(step){
  cfg=readForm();
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  goStep(step+1);
}

// ── Navigation ─────────────────────────────────────────────────────────────
function goStep(n){
  document.querySelectorAll('.section').forEach((s,i)=>s.classList.toggle('visible',i===n));
  document.querySelectorAll('.nav-item').forEach((s,i)=>{
    // nav items: 0=Setup,1=Optionen,2=Album,3=Migration
    if(i<4) s.classList.toggle('active',i===n);
  });
  if(n===0) populateInspectDropdown();
}

// ── Connection test ─────────────────────────────────────────────────────────
async function testConnections(){
  const body=readForm();
  ['syn','imm'].forEach(k=>setDot(k,'spin','Teste...'));
  try{
    const r=await fetch('/api/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    setDot('syn',d.synology.ok?'ok':'err',d.synology.msg);
    setDot('imm',d.immich.ok?'ok':'err',d.immich.msg);
    pushLocalLog('Synology: '+d.synology.msg, d.synology.ok?'success':'error');
    pushLocalLog('Immich: '+d.immich.msg, d.immich.ok?'success':'error');
  }catch(e){
    ['syn','imm'].forEach(k=>setDot(k,'err','Verbindung fehlgeschlagen'));
    pushLocalLog('Verbindungstest: '+e.message,'error');
  }
}
function setDot(id,state,msg){
  const el=document.getElementById('dot-'+id), sub=document.getElementById('sub-'+id);
  if(el) el.className='sdot '+(state==='spin'?'spin':state==='ok'?'ok':'err');
  if(sub) sub.textContent=msg||'';
}

// ── User table ─────────────────────────────────────────────────────────────
function renderUserTable(users){
  document.getElementById('userTableBody').innerHTML='';
  (users||[]).forEach(u=>addUserRow(u));
}
function addUserRow(u){
  u=u||{};
  const tr=document.createElement('tr');
  tr.innerHTML=`<td><span class="rs" data-rs></span></td>
    <td><input data-un value="${ea(u.synology_username||'')}" placeholder="florian"></td>
    <td><input data-pw type="password" value="${ea(u.synology_password||'')}" placeholder="Passwort"></td>
    <td><input data-ak type="password" value="${ea(u.immich_api_key||'')}" placeholder="API-Key"></td>
    <td><span data-st style="font-size:.7rem;color:var(--muted)">—</span></td>
    <td><button class="btn btn-ghost btn-sm" onclick="this.closest('tr').remove()">✕</button></td>`;
  document.getElementById('userTableBody').appendChild(tr);
}
function readUserTable(){
  return [...document.querySelectorAll('#userTableBody tr')].map(tr=>({
    synology_username:tr.querySelector('[data-un]').value.trim(),
    synology_password:tr.querySelector('[data-pw]').value,
    immich_api_key:tr.querySelector('[data-ak]').value.trim(),
  })).filter(u=>u.synology_username);
}
function ea(s){return (s||'').replace(/"/g,'&quot;');}

async function discoverUsers(){
  const body=readForm();
  try{
    const r=await fetch('/api/discover-users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.error){pushLocalLog('User laden: '+d.error,'error');return;}
    const ex=readUserTable(), names=new Set(ex.map(u=>u.synology_username));
    d.users.forEach(n=>{if(!names.has(n))ex.push({synology_username:n,synology_password:'',immich_api_key:''});});
    renderUserTable(ex);
    pushLocalLog(d.users.length+' User geladen','success');
  }catch(e){pushLocalLog('User laden: '+e.message,'error');}
}

async function testAllUsers(){
  const rows=[...document.querySelectorAll('#userTableBody tr')], body=readForm();
  pushLocalLog('── User-Test ──','section');
  for(const tr of rows){
    const uname=tr.querySelector('[data-un]').value.trim(); if(!uname) continue;
    const rs=tr.querySelector('[data-rs]'), st=tr.querySelector('[data-st]');
    rs.className='rs spin'; st.textContent='...';
    try{
      const r=await fetch('/api/test-user',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...body,target_user:{synology_username:uname,
          synology_password:tr.querySelector('[data-pw]').value,
          immich_api_key:tr.querySelector('[data-ak]').value.trim()}})});
      const d=await r.json();
      rs.className='rs '+(d.ok?'ok':'err'); st.textContent=d.msg;
      pushLocalLog('  '+uname+': '+d.msg, d.ok?'success':'error');
    }catch(e){rs.className='rs err';st.textContent='Fehler';pushLocalLog('  '+uname+': '+e.message,'error');}
  }
}

// ── Debug ──────────────────────────────────────────────────────────────────
let debugOpen=false;
function toggleDebug(){
  debugOpen=!debugOpen;
  document.getElementById('debug-body').classList.toggle('open',debugOpen);
  document.getElementById('collap-arrow').textContent=debugOpen?'▼':'▶';
  if(debugOpen) populateInspectDropdown();
}
function populateInspectDropdown(){
  const sel=document.getElementById('inspectUser'), users=readUserTable();
  sel.innerHTML=users.map(u=>`<option value="${ea(u.synology_username)}">${ea(u.synology_username)}</option>`).join('');
}
async function runInspect(){
  const body=readForm(), uname=document.getElementById('inspectUser').value, el=document.getElementById('inspectResult');
  el.innerHTML='<div style="color:var(--muted);font-size:.78rem">Lade...</div>';
  try{
    const r=await fetch('/api/inspect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,target_username:uname})});
    el.innerHTML=`<pre class="debug">${escH(JSON.stringify(await r.json(),null,2))}</pre>`;
  }catch(e){el.innerHTML=`<div style="color:var(--red);font-size:.78rem">Fehler: ${e.message}</div>`;}
}

// ── Album scan ──────────────────────────────────────────────────────────────

async function runPreview(){
  document.getElementById('scan-empty').textContent='Lade Alben...';
  document.getElementById('scan-empty').style.display='block';
  document.getElementById('scan-ui').style.display='none';
  const body=readForm();
  try{
    const [r1,r2]=await Promise.all([
      fetch('/api/preview',    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
      fetch('/api/list-albums',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
    ]);
    const [d,al]=await Promise.all([r1.json(),r2.json()]);
    scannedAlbums=al.albums||[];
    document.getElementById('scan-empty').style.display='none';
    document.getElementById('scan-ui').style.display='block';

    // Summary cards
    const summaryCards=[
      {n:d.unique_albums||0,     l:'Alben gesamt'},
      {n:d.normal_albums||0,    l:'Normale Alben'},
      {n:d.passphrase_albums||0,l:'Freigabe-Alben'},
      {n:d.team_items||0,       l:'Team Space Fotos'},
    ];
    // "Bereits in Immich" card — only shown once we could actually check
    // (needs immich_url + at least one user's api key already saved).
    let totalPhotos=0, alreadyPhotos=0, anyChecked=false;
    scannedAlbums.forEach(a=>{
      if(a.item_count!=null){
        totalPhotos+=a.item_count;
        if(a.immich_count!=null){anyChecked=true; alreadyPhotos+=Math.min(a.immich_count,a.item_count);}
      }
    });
    if(anyChecked){
      summaryCards.push({n:`${alreadyPhotos}/${totalPhotos}`, l:'Bereits in Immich'});
    }
    document.getElementById('prev-grid').innerHTML=summaryCards
      .map(c=>`<div class="prev-card"><div class="prev-num">${c.n}</div><div class="prev-label">${c.l}</div></div>`).join('');

    // Build user-filter chips from album data
    const users=[...new Set(scannedAlbums.flatMap(a=>a.users||[]))].sort();
    const uc=document.getElementById('user-chips');
    uc.innerHTML='<button class="chip on" id="uf-all" onclick="setUserFilter(\'all\')">Alle User</button>';
    users.forEach(u=>{
      const sid='uf-'+u.replace(/[^a-z0-9]/gi,'_');
      uc.innerHTML+=`<button class="chip" id="${sid}" onclick="setUserFilter('${escH(u)}')">${escH(u)}</button>`;
    });

    // Default selection: everything EXCEPT albums already fully present in Immich
    // (checked live via /api/list-albums → immich_count, not from any local state file —
    // so this still works correctly even if migration_config.json was deleted) AND
    // except manually-excluded albums (this browser's localStorage).
    selectedIds=new Set(
      scannedAlbums
        .filter(a=>!isAlbumFullyMigrated(a) && !excludedIds.has(String(a.id||a.passphrase)))
        .map(a=>String(a.id||a.passphrase))
    );
    activeUserFilter='all'; activeTypeFilter='all';
    applyAlbumFilters();
    updateSelCount();
    if(d.errors&&d.errors.length) d.errors.forEach(e=>pushLocalLog(e.user+': '+e.error,'warn'));
  }catch(e){
    document.getElementById('scan-empty').textContent='Fehler: '+e.message;
    pushLocalLog('Scan: '+e.message,'error');
  }
}

function albumType(a){return a.type||'normal';}

// True when an album's Immich asset count (checked live against the Immich server
// in /api/list-albums, matched by album name) already covers all its Synology items.
// Returns false when we couldn't check (immich_count is null, e.g. no api key saved).
function isAlbumFullyMigrated(a){
  if(a.item_count==null || a.immich_count==null) return false;
  return a.item_count>0 && Math.min(a.immich_count,a.item_count)>=a.item_count;
}

function applyAlbumFilters(){
  const albumSearchEl=document.getElementById('album-search');
  const q=((albumSearchEl&&albumSearchEl.value)||'').toLowerCase();
  const albumSortEl=document.getElementById('album-sort');
  const sort=(albumSortEl&&albumSortEl.value)||'type';
  let list=[...scannedAlbums];
  list.sort((a,b)=>{
    if(sort==='type'){
      const order={passphrase:0,shared:1,normal:2};
      const td=(order[albumType(a)]||0)-(order[albumType(b)]||0);
      return td||((a.name||'').localeCompare(b.name||''));
    }
    if(sort==='name')      return (a.name||'').localeCompare(b.name||'');
    if(sort==='count-desc')return (b.item_count||0)-(a.item_count||0);
    if(sort==='count-asc') return (a.item_count||0)-(b.item_count||0);
    return 0;
  });
  renderAlbumList(list,q);
}

function renderAlbumList(list,q=''){
  const wrap=document.getElementById('album-list'); wrap.innerHTML='';
  let shown=0, excludedCount=0;
  list.forEach(a=>{
    const id=String(a.id||a.passphrase), type=albumType(a);
    const users=a.users||[], contributors=a.contributors||[];
    const excluded=excludedIds.has(id);
    if(excluded) excludedCount++;
    // User filter: show if any of the album's users/contributors match
    if(activeUserFilter!=='all'){
      const relevant=[...users,...contributors];
      if(!relevant.includes(activeUserFilter)) return;
    }
    if(activeTypeFilter!=='all'&&type!==activeTypeFilter) return;
    if(q&&!(a.name||'').toLowerCase().includes(q)) return;
    if(excluded&&!showExcluded) return; // hidden by default to declutter large migrations
    shown++;
    const checked=selectedIds.has(id);
    const badgeClass={passphrase:'badge-link',shared:'badge-shared',normal:'badge-normal'}[type]||'badge-normal';
    const badgeLabel={passphrase:'🔗 Freigabe',shared:'👥 Geteilt',normal:'📁 Eigenes'}[type]||'📁 Eigenes';
    const info=[];
    if(contributors.length) info.push('📸 '+contributors.join(', '));
    else if(users.length>1) info.push('👤 '+users.join(', '));

    // Already-in-Immich indicator (best-effort, matched by album name — see /api/list-albums).
    // a.immich_count is null when it couldn't be checked (e.g. no Immich API key saved yet).
    const total=a.item_count, immichCount=a.immich_count;
    const doneFull=isAlbumFullyMigrated(a);
    let countCls='', countText = total!=null ? total+' Fotos' : '—';
    if(total!=null && immichCount!=null){
      const capped=Math.min(immichCount,total);
      if(doneFull){
        countCls='done';
        countText=`✓ ${total} Fotos`;
      }else if(capped>0){
        countCls='partial';
        countText=`${capped}/${total} Fotos`;
      }
    }
    const excludeBtn=excluded
      ?`<button type="button" class="album-exclude-btn" onclick="toggleExcludeAlbum('${id}',event)" title="Wieder einschließen">↺</button>`
      :`<button type="button" class="album-exclude-btn" onclick="toggleExcludeAlbum('${id}',event)" title="Ausschließen (ausblenden)">✕</button>`;

    const div=document.createElement('div');
    div.className='album-item'+(doneFull?' album-done':'')+(excluded?' album-excluded':''); div.dataset.id=id;
    div.innerHTML=`<input type="checkbox" ${checked?'checked':''} onchange="toggleAlbum('${id}',this.checked)">
      <div class="album-meta">
        <div class="album-name">${escH(a.name||'Unbekannt')}</div>
        ${info.length?`<div class="album-info">${escH(info.join('  ·  '))}</div>`:''}
      </div>
      <span class="badge ${badgeClass}">${badgeLabel}</span>
      <span class="album-count ${countCls}">${countText}</span>
      ${excludeBtn}`;
    div.onclick=e=>{if(e.target.tagName!=='INPUT'&&!e.target.closest('.album-exclude-btn')){const cb=div.querySelector('input');cb.checked=!cb.checked;toggleAlbum(id,cb.checked);}};
    wrap.appendChild(div);
  });
  if(shown===0&&scannedAlbums.length>0){
    wrap.innerHTML=excludedCount>0&&!showExcluded
      ?'<div style="padding:20px;text-align:center;color:var(--muted);font-size:.85rem">Keine Alben entsprechen dem Filter (ggf. sind alle passenden ausgeschlossen).</div>'
      :'<div style="padding:20px;text-align:center;color:var(--muted);font-size:.85rem">Keine Alben entsprechen dem Filter.</div>';
  }
  const ec=document.getElementById('excluded-count'); if(ec) ec.textContent=excludedCount;
  updateSelCount();
}

function setUserFilter(u){
  activeUserFilter=u;
  // Update chip states
  document.querySelectorAll('#user-chips .chip').forEach(b=>{
    const isAll=b.id==='uf-all';
    b.classList.toggle('on',(u==='all'&&isAll)||(b.textContent===u));
  });
  applyAlbumFilters();
}

function setTypeFilter(t){
  activeTypeFilter=t;
  document.querySelectorAll('.type-chip').forEach(b=>{
    b.classList.toggle('on',b.dataset.type===t);
  });
  applyAlbumFilters();
}

function toggleAlbum(id,v){
  if(v) selectedIds.add(id); else selectedIds.delete(id);
  updateSelCount();
}

function toggleExcludeAlbum(id,ev){
  if(ev) ev.stopPropagation();
  if(excludedIds.has(id)){
    excludedIds.delete(id);
  }else{
    excludedIds.add(id);
    selectedIds.delete(id);
  }
  try{ localStorage.setItem('synmig_excluded_albums', JSON.stringify([...excludedIds])); }catch(e){}
  applyAlbumFilters();
}

function toggleShowExcluded(){
  showExcluded=!showExcluded;
  const btn=document.getElementById('toggle-excluded');
  if(btn) btn.classList.toggle('on',showExcluded);
  applyAlbumFilters();
}

function selectAllAlbums(v){
  // Only affect currently VISIBLE items (respects active filters); never
  // re-select an excluded album via "Alle".
  document.querySelectorAll('#album-list .album-item').forEach(row=>{
    const cb=row.querySelector('input'); if(!cb)return;
    if(v && row.classList.contains('album-excluded')) return;
    cb.checked=v;
    if(v) selectedIds.add(row.dataset.id); else selectedIds.delete(row.dataset.id);
  });
  updateSelCount();
}

function updateSelCount(){
  const total=scannedAlbums.length, sel=selectedIds.size;
  let photos=0, already=0, anyChecked=false;
  getSelectedAlbums().forEach(a=>{
    if(a.item_count!=null){
      photos+=a.item_count;
      if(a.immich_count!=null){anyChecked=true; already+=Math.min(a.immich_count,a.item_count);}
    }
  });
  const extra = photos>0 ? (anyChecked?` · ${already}/${photos} Fotos bereits in Immich`:` · ${photos} Fotos`) : '';
  document.getElementById('album-sel-count').textContent=`${sel} / ${total} ausgewählt${extra}`;
}

function getSelectedAlbums(){
  return scannedAlbums.filter(a=>selectedIds.has(String(a.id||a.passphrase)));
}

function confirmAndNext(){
  // Always show Start button when entering migration page
  document.getElementById('btn-start').style.display='inline-flex';
  document.getElementById('btn-start').disabled=false;
  document.getElementById('btn-start').textContent='▶ Migration starten';
  document.getElementById('btn-pause').style.display='none';
  document.getElementById('btn-new-run').style.display='none';
  document.getElementById('btn-report').style.display='none';
  document.getElementById('btn-back').style.display='none';
  document.getElementById('btn-stop-hint').style.display='none';
  const sel=getSelectedAlbums();
  // Build readable summary
  const types={passphrase:0,shared:0,normal:0};
  sel.forEach(a=>{ types[albumType(a)]=(types[albumType(a)]||0)+1; });
  const parts=[];
  if(types.passphrase) parts.push(`${types.passphrase} Freigabe`);
  if(types.shared)     parts.push(`${types.shared} Geteilt`);
  if(types.normal)     parts.push(`${types.normal} Eigene`);
  const summary= sel.length===0 ? 'Keine Alben ausgewählt — persönliche Fotos werden migriert' :
    sel.length===scannedAlbums.length ? `Alle ${sel.length} Alben` :
    `${sel.length} Alben (${parts.join(', ')})`;
  document.getElementById('selected-summary').textContent=summary;
  // Show album names list
  const names=sel.slice(0,5).map(a=>a.name).join(', ')+(sel.length>5?` und ${sel.length-5} weitere`:'');
  document.getElementById('selected-names').textContent=sel.length>0?names:'';
  goStep(3);
}

// ── Migration ─────────────────────────────────────────────────────────────────
let migrationRunning=false;

function resetMigrationUI(){
  // Reset stats
  ['s-up','s-dup','s-fail','s-alb'].forEach(id=>document.getElementById(id).textContent='0');
  document.getElementById('prog-bar').style.width='0%';
  document.getElementById('prog-label').textContent='—';
  document.getElementById('action-banner').textContent='Bereit...';
  document.getElementById('status-area').innerHTML='<div class="status-pill sp-run"><span class="dot-blink"></span>Startet...</div>';
  document.getElementById('log-mini').innerHTML='';
  allLogs=[]; lastLogLen=0;
}

async function startMigration(){
  cfg=readForm();
  cfg.selected_albums=getSelectedAlbums()
    .map(a=>({id:a.id,passphrase:a.passphrase,type:a.type,name:a.name}));

  resetMigrationUI();
  migrationRunning=true; isPaused=false;

  // Button states: running
  document.getElementById('btn-start').style.display='none';
  document.getElementById('btn-pause').style.display='inline-flex';
  document.getElementById('btn-pause').textContent='⏸ Pausieren';
  document.getElementById('btn-stop-hint').style.display='block';
  document.getElementById('btn-report').style.display='none';
  document.getElementById('btn-back').style.display='none';
  document.getElementById('btn-new-run').style.display='none';

  await fetch('/api/migrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  polling=setInterval(pollStatus,700);
}

// Called once on page load. The server keeps migration_state (incl. the full log)
// alive in memory even after the browser tab is closed/reloaded — this re-hydrates
// the UI from it, so the Logs page and progress aren't just empty after a refresh.
async function resumeIfRunning(){
  try{
    const r=await fetch('/api/status');
    const d=await r.json();
    if(d.status==='running'||d.status==='paused'){
      migrationRunning=true; isPaused=(d.status==='paused');
      document.getElementById('btn-start').style.display='none';
      document.getElementById('btn-pause').style.display='inline-flex';
      document.getElementById('btn-pause').textContent=isPaused?'▶ Fortsetzen':'⏸ Pausieren';
      document.getElementById('btn-pause').className=isPaused?'btn btn-primary btn-lg':'btn btn-amber btn-lg';
      document.getElementById('btn-stop-hint').style.display='block';
      document.getElementById('btn-report').style.display='none';
      document.getElementById('btn-back').style.display='none';
      document.getElementById('btn-new-run').style.display='none';
      polling=setInterval(pollStatus,700);
    }
    // Pulls migration_state["log"] into allLogs (via ingestLogs) and refreshes all
    // stats/progress/status displays immediately — covers 'done'/'error' too.
    if(d.status!=='idle') await pollStatus();
  }catch(e){}
}

async function togglePause(){
  if(isPaused){
    await fetch('/api/resume',{method:'POST'});
    isPaused=false;
    document.getElementById('btn-pause').textContent='⏸ Pausieren';
    document.getElementById('btn-pause').className='btn btn-amber';
  }else{
    await fetch('/api/pause',{method:'POST'});
    isPaused=true;
    document.getElementById('btn-pause').textContent='▶ Fortsetzen';
    document.getElementById('btn-pause').className='btn btn-primary';
  }
}

async function pollStatus(){
  try{
    const r=await fetch('/api/status'), d=await r.json();
    document.getElementById('s-up').textContent=d.uploaded||0;
    document.getElementById('s-dup').textContent=d.duplicates||0;
    document.getElementById('s-fail').textContent=d.failed||0;
    document.getElementById('s-alb').textContent=d.albums_total>0?`${d.albums_done||0}/${d.albums_total}`:(d.albums_done||0);
    const pct=d.total>0?Math.round(d.progress/d.total*100):0;
    document.getElementById('prog-bar').style.width=pct+'%';
    document.getElementById('prog-label').textContent=
      d.total>0?`${d.progress} / ${d.total} (${pct}%)  ${d.phase||''}`:d.phase||'Verbinde...';
    document.getElementById('action-banner').textContent=d.current_action||'—';
    if(d.log) ingestLogs(d.log);
    const sa=document.getElementById('status-area');
    if(d.status==='running'){
      sa.innerHTML='<div class="status-pill sp-run"><span class="dot-blink"></span>Läuft...</div>';
    }else if(d.status==='paused'){
      sa.innerHTML='<div class="status-pill sp-pause">⏸ Pausiert — klicke Fortsetzen</div>';
    }else if(d.status==='done'){
      sa.innerHTML='<div class="status-pill sp-done">✓ Abgeschlossen</div>';
      document.getElementById('action-banner').textContent='Migration abgeschlossen.';
      clearInterval(polling); migrationRunning=false;
      // Done state: show "Neues Album migrieren" and Report
      document.getElementById('btn-pause').style.display='none';
      document.getElementById('btn-stop-hint').style.display='none';
      document.getElementById('btn-back').style.display='inline-flex';
      document.getElementById('btn-new-run').style.display='inline-flex';
      if(d.report) document.getElementById('btn-report').style.display='inline-flex';
    }else if(d.status==='error'){
      sa.innerHTML='<div class="status-pill sp-err">✗ Fehler — siehe Live-Aktivität</div>';
      clearInterval(polling); migrationRunning=false;
      document.getElementById('btn-pause').style.display='none';
      document.getElementById('btn-stop-hint').style.display='none';
      document.getElementById('btn-back').style.display='inline-flex';
      document.getElementById('btn-new-run').style.display='inline-flex';
    }
  }catch(e){}
}

function goToNewRun(){
  // Go back to album selection for a new run
  goStep(2);
  // btn-start will be shown again when user hits "Weiter" from album selection
  document.getElementById('btn-start').style.display='inline-flex';
  document.getElementById('btn-new-run').style.display='none';
  document.getElementById('btn-report').style.display='none';
}

// ── Log system (mini live-activity box on the Migration page) ──────────────
function ingestLogs(lines){
  if(!lines||lines.length===lastLogLen) return;
  const newE=lines.slice(lastLogLen); lastLogLen=lines.length;
  allLogs.push(...newE);
  // Update error badge on the Migration nav item
  const errs=allLogs.filter(l=>l.level==='error').length;
  const badge=document.getElementById('log-err-badge');
  badge.style.display=errs>0?'inline':'none'; badge.textContent=errs;
  // Append to the live-activity box, capped to the last 80 entries
  const mini=document.getElementById('log-mini');
  newE.forEach(l=>{
    mini.appendChild(buildLL(l));
    while(mini.children.length>80) mini.removeChild(mini.firstChild);
  });
  mini.scrollTop=mini.scrollHeight;
}

function buildLL(l){
  const el=document.createElement('div'); el.className='ll '+l.level;
  el.innerHTML=`<span class="ll-ts">${l.ts}</span><span class="ll-lv">${l.level}</span><span class="ll-msg">${escH(l.msg)}</span>`;
  return el;
}

function pushLocalLog(msg,level='info'){
  const ts=new Date().toTimeString().slice(0,8);
  const entry={ts,msg,level}; allLogs.push(entry);
  const errs=allLogs.filter(l=>l.level==='error').length;
  const badge=document.getElementById('log-err-badge');
  badge.style.display=errs>0?'inline':'none'; badge.textContent=errs;
  const mini=document.getElementById('log-mini');
  mini.appendChild(buildLL(entry));
  while(mini.children.length>80) mini.removeChild(mini.firstChild);
  mini.scrollTop=mini.scrollHeight;
}

function copyAllLogs(){
  navigator.clipboard.writeText(allLogs.map(l=>`[${l.ts}] [${l.level.toUpperCase()}] ${l.msg}`).join('\n'))
    .then(()=>alert('Logs kopiert.')).catch(()=>alert('Kopieren fehlgeschlagen.'));
}
function exportLogs(){
  const t=allLogs.map(l=>`[${l.ts}] [${l.level.toUpperCase()}] ${l.msg}`).join('\n');
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([t],{type:'text/plain'}));
  a.download=`log_${new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')}.txt`; a.click();
}
// ── Utils ──────────────────────────────────────────────────────────────────
function escH(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// ── Theme ──────────────────────────────────────────────────────────────────
function toggleTheme(){
  const isLight=document.body.classList.toggle('light');
  document.getElementById('theme-btn').textContent=isLight?'🌙 Dark':'☀ Light';
  try{localStorage.setItem('theme',isLight?'light':'dark');}catch(e){}
}
function applyStoredTheme(){
  try{
    if(localStorage.getItem('theme')==='light'){
      document.body.classList.add('light');
      const b=document.getElementById('theme-btn');
      if(b) b.textContent='🌙 Dark';
    }
  }catch(e){}
}

// ── Downloads ──────────────────────────────────────────────────────────────
async function downloadReport(){window.open('/api/report','_blank');}
async function resetState(){
  if(!confirm('Fortschritt zurücksetzen?\n\nAlben werden neu migriert. Bereits hochgeladene Fotos werden als Duplikate erkannt.')) return;
  await fetch('/api/reset-state',{method:'POST'});
  pushLocalLog('Fortschritt zurückgesetzt','warn');
}

applyStoredTheme();
loadConfig();
resumeIfRunning();
</script>
</body>
</html>"""




class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ct, data):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        try:
            self._do_GET()
        except Exception as e:
            try:
                self._json({"error": f"Server-Fehler: {e}"}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:
            try:
                self._json({"error": f"Server-Fehler: {e}"}, 500)
            except Exception:
                pass

    def _do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        elif p == "/api/config":
            self._json(load_config())
        elif p == "/api/status":
            self._json(migration_state)
        elif p == "/api/report":
            rp = migration_state.get("report")
            if rp and os.path.exists(rp):
                with open(rp, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", f"attachment; filename={os.path.basename(rp)}")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json({"error": "Kein Report verfügbar"}, 404)
        else:
            self._json({"error": "Not found"}, 404)

    def _do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json({"error": "Ungültiges JSON im Request"}, 400)
            return
        p = urlparse(self.path).path

        # Defensive defaults so a partially-filled form never causes a raw
        # KeyError / dropped connection — missing fields just fail their
        # specific check below with a clean message instead.
        body.setdefault("synology_url", "")
        body.setdefault("immich_url", "")
        body.setdefault("synology_admin_user", "")
        body.setdefault("synology_admin_pass", "")
        body.setdefault("synology_admin_otp", "")
        body.setdefault("users", [])
        body.setdefault("options", {})

        if p == "/api/config":
            save_config(body)
            self._json({"ok": True})

        elif p == "/api/test":
            result = {"synology": {"ok": False, "msg": ""}, "immich": {"ok": False, "msg": ""}}
            try:
                syn = SynologyClient(body["synology_url"], body["synology_admin_user"],
                                     body["synology_admin_pass"], body.get("synology_admin_otp", ""))
                syn.login()
                syn.logout()
                result["synology"] = {"ok": True, "msg": f"Verbunden als {body['synology_admin_user']}"}
            except Exception as e:
                result["synology"] = {"ok": False, "msg": str(e)[:100]}
            try:
                r = requests.get(f"{body['immich_url'].rstrip('/')}/api/server/ping", timeout=10)
                r.raise_for_status()
                result["immich"] = {"ok": True, "msg": "Server erreichbar"}
            except Exception as e:
                result["immich"] = {"ok": False, "msg": str(e)[:100]}
            self._json(result)

        elif p == "/api/discover-users":
            try:
                syn = SynologyClient(body["synology_url"], body["synology_admin_user"],
                                     body["synology_admin_pass"], body.get("synology_admin_otp", ""))
                syn.login()
                users = syn.list_dsm_users()
                syn.logout()
                names = [u.get("name") for u in users if u.get("name") not in ("guest",)]
                self._json({"users": names})
            except Exception as e:
                self._json({"error": str(e)})

        elif p == "/api/test-user":
            tu    = body.get("target_user") or {}
            uname = tu.get("synology_username", "")
            upass = tu.get("synology_password", "")
            akey  = tu.get("immich_api_key", "")
            if not uname:
                self._json({"ok": False, "msg": "Synology-Username fehlt"}); return
            if not upass:
                self._json({"ok": False, "msg": "Synology-Passwort fehlt"}); return
            if not akey:
                self._json({"ok": False, "msg": "Immich-API-Key fehlt"}); return
            msgs = []
            syn_ok = imm_ok = False
            try:
                syn = SynologyClient(body["synology_url"], uname, upass, "")
                syn.login(); syn.logout(); syn_ok = True
                msgs.append("Synology ✓")
            except Exception as e:
                msgs.append(f"Synology ✗: {str(e)[:60]}")
            try:
                imm   = ImmichClient(body["immich_url"], akey)
                me    = imm.whoami()
                label = me.get("email") or me.get("name") or "?"
                imm_ok = True
                msgs.append(f"Immich ✓ → {label}")
            except Exception as e:
                msgs.append(f"Immich ✗: {str(e)[:60]}")
            self._json({"ok": syn_ok and imm_ok, "msg": " | ".join(msgs)})

        elif p == "/api/pause":
            pause_migration()
            self._json({"ok": True})

        elif p == "/api/resume":
            resume_migration()
            self._json({"ok": True})

        elif p == "/api/reset-state":
            try:
                if os.path.exists(STATE_FILE):
                    os.remove(STATE_FILE)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        elif p == "/api/inspect":
            uname = body.get("target_username")
            self._json(inspect_user(body, uname))

        elif p == "/api/preview":
            self._json(preview_scan(body))

        elif p == "/api/list-albums":
            users_cfg = [u for u in body.get("users", [])
                         if u.get("synology_username") and u.get("synology_password")]
            albums_map = {}
            # uid → username map (built from all sessions)
            uid_map = {}
            for u in users_cfg:
                uname = u["synology_username"]
                try:
                    syn = SynologyClient(body["synology_url"], uname,
                                         u["synology_password"], u.get("synology_otp",""))
                    syn.login()

                    # Build UID map from admin-capable session
                    if not uid_map:
                        for du in syn.list_dsm_users():
                            uid = str(du.get("uid",""))
                            if uid: uid_map[uid] = du.get("name","")

                    # Normal albums
                    for a in syn.list_albums():
                        key = f"normal_{a.get('id')}"
                        # Determine if album is shared:
                        # Synology sets shared=True, or has passphrase, or member_count>1
                        is_shared_album = bool(
                            a.get("shared") or
                            a.get("passphrase") or
                            (a.get("member_count") or 0) > 1 or
                            (a.get("shared_with") and len(a.get("shared_with", [])) > 0)
                        )
                        if key not in albums_map:
                            albums_map[key] = {
                                "id": a.get("id"),
                                "name": a.get("name") or a.get("title"),
                                "type": "shared" if is_shared_album else "normal",
                                "item_count": a.get("item_count"),
                                "users": set(),
                                "contributors": set(),
                                "_raw_shared": is_shared_album,
                                "_primary_user": uname,
                            }
                        else:
                            # If any user sees it as shared, mark it shared
                            if is_shared_album:
                                albums_map[key]["type"] = "shared"
                        albums_map[key]["users"].add(uname)
                        # Peek at items to find contributors via provider_user_id
                        try:
                            items = syn.list_items_in_album(a.get("id"))
                            for it in items[:100]:
                                add  = it.get("additional") or {}
                                puid = add.get("provider_user_id")
                                if puid is None: puid = it.get("owner_user_id")
                                if puid is not None and int(puid) != 0:
                                    cname = uid_map.get(str(puid))
                                    if cname: albums_map[key]["contributors"].add(cname)
                        except Exception:
                            pass

                    # Passphrase / shared albums
                    for p in syn.get_shared_passphrases():
                        pp = p.get("passphrase")
                        if not pp: continue
                        key = f"pp_{pp}"
                        if key not in albums_map:
                            albums_map[key] = {
                                "id": None, "passphrase": pp,
                                "name": p.get("name") or f"Freigabe_{pp[:6]}",
                                "type": "passphrase",
                                "item_count": p.get("item_count"),
                                "users": set(),
                                "contributors": set(),
                                "_primary_user": uname,
                            }
                        albums_map[key]["users"].add(uname)
                        # Peek at items to find contributors
                        try:
                            items = syn.list_items_in_shared_album(pp)
                            for it in items[:50]:
                                puid = (it.get("additional") or {}).get("provider_user_id")
                                if puid is None: puid = it.get("owner_user_id")
                                if puid and int(puid) != 0:
                                    cname = uid_map.get(str(puid))
                                    if cname: albums_map[key]["contributors"].add(cname)
                        except Exception:
                            pass

                    syn.logout()
                except Exception:
                    pass

            # Cross-check against Immich: for each album, look up whether an album
            # with the same name already exists under its primary user's account
            # (same account that /api/migrate would create it under) and how many
            # assets it already has. Best-effort only — used for the "already
            # migrated" indicator in the album list, not for the actual migration.
            immich_url = body.get("immich_url", "")
            immich_albums_by_user = {}  # uname → {albumName: assetCount}
            if immich_url:
                for u in users_cfg:
                    uname = u["synology_username"]
                    akey  = u.get("immich_api_key")
                    if not akey or uname in immich_albums_by_user:
                        continue
                    try:
                        imm = ImmichClient(immich_url, akey)
                        immich_albums_by_user[uname] = {
                            ia.get("albumName"): ia.get("assetCount", 0)
                            for ia in imm.get_albums()
                        }
                    except Exception:
                        pass  # no indicator for this user's albums — not fatal

            albums_out = []
            for a in albums_map.values():
                a["users"]        = sorted(a["users"])
                a["contributors"] = sorted(a["contributors"])
                primary_user      = a.pop("_primary_user", None)
                by_name = immich_albums_by_user.get(primary_user)
                a["immich_count"] = by_name.get(a["name"]) if by_name is not None else None
                albums_out.append(a)
            albums_out.sort(key=lambda a: (0 if a["type"]=="passphrase" else 1,
                                           (a["name"] or "").lower()))
            self._json({"albums": albums_out})

        elif p == "/api/migrate":
            if migration_state["status"] == "running":
                self._json({"ok": False, "msg": "Läuft bereits"})
                return
            save_config(body)
            threading.Thread(target=run_migration, args=(body,), daemon=True).start()
            self._json({"ok": True})

        else:
            self._json({"error": "Not found"}, 404)


if __name__ == "__main__":
    port = 8765
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"\n  Synology → Immich Migration Tool")
    print(f"  ─────────────────────────────────")
    print(f"  Öffne im Browser: {url}")
    print(f"  Zum Beenden: Ctrl+C\n")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Gestoppt.")
