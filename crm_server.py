#!/usr/bin/env python3
"""CRM Fansly FYP — dashboard + telechargement + parametrage de publication."""
from __future__ import annotations

import json
import random
import re
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

import requests

import scheduler as sched_mod
from fansly_api import FanslyClient, FanslyError

BASE = Path(__file__).resolve().parent
DATA_FILE = BASE / "videos_crm.json"
HTML_FILE = BASE / "crm.html"
SCHED_CONFIG = BASE / "scheduler_config.json"
SESSION_CONFIG = BASE / "config.json"

app = Flask(__name__)

_cache: dict = {"videos": [], "by_id": {}}
_run_state: dict = {"running": False, "log": [], "done": 0, "total": 0, "error": None, "stop_requested": False}
_analyze_state: dict = {"running": False, "log": [], "error": None, "report": None}
_session_cache: dict = {"username": None, "checked_at": 0.0}
SESSION_CACHE_TTL = 300.0


def load_data() -> None:
    if not _cache["videos"] and DATA_FILE.exists():
        _cache["videos"] = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        _cache["by_id"] = {v["mediaOfferId"]: v for v in _cache["videos"]}


def load_sched_config() -> dict:
    if SCHED_CONFIG.exists():
        return json.loads(SCHED_CONFIG.read_text(encoding="utf-8"))
    return {}


def load_session_config() -> dict:
    if SESSION_CONFIG.exists():
        return json.loads(SESSION_CONFIG.read_text(encoding="utf-8"))
    return {}


def parse_session_paste(raw: str) -> dict:
    """Extrait authorization/cookie/client_id/session_id d'un texte colle :
    soit un 'Copy as cURL' depuis les devtools du navigateur, soit des
    en-tetes brutes (une paire nom: valeur par ligne)."""
    raw = raw or ""

    def grab(name: str) -> str:
        m = re.search(rf"""-H\s+(['"]){re.escape(name)}\s*:\s*(.*?)\1""", raw, re.IGNORECASE)
        if m:
            return m.group(2).strip()
        m = re.search(rf"^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", raw, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).strip()
        return ""

    cookie = grab("cookie")
    if not cookie:
        m = re.search(r"""-b\s+(['"])(.*?)\1""", raw)
        if m:
            cookie = m.group(2).strip()

    return {
        "authorization": grab("authorization"),
        "cookie": cookie,
        "client_id": grab("fansly-client-id"),
        "session_id": grab("fansly-session-id"),
    }


def _check_session(cfg: dict, force: bool = False) -> str | None:
    """Username si la session est valide (teste via /account/me), None sinon.
    Mise en cache (TTL) pour ne pas re-verifier a chaque chargement de page."""
    now = time.time()
    if not force and _session_cache["username"] and now - _session_cache["checked_at"] < SESSION_CACHE_TTL:
        return _session_cache["username"]
    try:
        me = FanslyClient(cfg).get_me()
    except FanslyError:
        return None
    _session_cache["username"] = me.get("username")
    _session_cache["checked_at"] = now
    return _session_cache["username"]


def resolve_reels_dir(cfg: dict) -> Path:
    p = Path(cfg.get("reels_dir", "../reels"))
    if not p.is_absolute():
        p = BASE / p
    return p.resolve()


def app_tz(tzname: str | None = None):
    """Fuseau de l'app : force Europe/Paris (ou celui donne), jamais le fuseau systeme.
    Le Mac est configure en heure francaise aujourd'hui, mais on ne veut pas en dependre :
    ca casserait silencieusement les heures affichees/programmees si ca change."""
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    try:
        return ZoneInfo(tzname or "Europe/Paris")
    except Exception:  # noqa: BLE001
        return ZoneInfo("Europe/Paris")


def start_date_of(cfg: dict) -> datetime:
    tz = app_tz(cfg.get("timezone"))
    s = cfg.get("start_date", datetime.now().strftime("%Y-%m-%d"))
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=tz)


def _categories_cfg(cfg: dict) -> dict:
    return cfg.get("categories", {})


def compute_best_hours(n: int = 5, tz=None) -> tuple[list[dict], list[float]]:
    """Analyse videos_crm.json -> les n meilleures heures de posting (vitesse d'engagement)."""
    if tz is None:
        tz = app_tz()
    load_data()
    now = datetime.now(tz).timestamp()
    hourly: dict[int, list[float]] = defaultdict(list)
    for v in _cache["videos"]:
        ca = v.get("createdAt")
        if not ca:
            continue
        age_h = (now - ca) / 3600.0
        if age_h < 6 or age_h > 14 * 24:
            continue
        likes = v.get("mediaLikeCount") or 0
        velocity = likes / max(age_h, 1.0)
        hour = datetime.fromtimestamp(ca, tz).hour
        hourly[hour].append(velocity)

    mean = {h: sum(vals) / len(vals) for h, vals in hourly.items() if vals}
    if not mean:
        return [], [0.0] * 24

    smooth: list[float] = []
    for h in range(24):
        vals = [mean.get((h + d) % 24, 0.0) for d in (-1, 0, 1)]
        smooth.append(sum(vals) / 3.0)

    ranked = sorted(range(24), key=lambda h: -smooth[h])
    picked: list[int] = []
    for h in ranked:
        if h not in picked:
            picked.append(h)
        if len(picked) >= n:
            break

    hours = [
        {"hour": h, "minute": random.randint(0, 24), "score": round(smooth[h], 2)}
        for h in sorted(picked)
    ]
    return hours, smooth


def build_hour_plan(files, hours, start_date, tz, tags, caption) -> list[dict]:
    """Chaque jour : une video par meilleure heure, heure espacees d'au moins 30 min.
    start_date : date du premier jour possible (jamais dans le passe, jamais avant maintenant)."""
    now = datetime.now(tz)
    start_midnight = datetime.combine(start_date, datetime.min.time(), tzinfo=tz)
    base = max(now, start_midnight)
    hours_sorted = sorted(hours, key=lambda x: (x["hour"], x["minute"]))
    slots: list[datetime] = []
    day = 0
    while len(slots) < len(files):
        for h in hours_sorted:
            dt = now.replace(hour=h["hour"], minute=h["minute"], second=0, microsecond=0) + timedelta(days=day)
            if dt > base:
                slots.append(dt)
            if len(slots) >= len(files):
                break
        day += 1

    plan = []
    for i, f in enumerate(files):
        plan.append({
            "file": f,
            "category": f.parent.name,
            "at": slots[i],
            "cfg": {"tags": tags, "caption": caption},
        })
    return plan


@app.get("/")
def index() -> Response:
    return send_file(HTML_FILE)


@app.get("/api/videos")
def api_videos() -> Response:
    load_data()
    return jsonify(_cache["videos"])


# ---------------------------------------------------------------- session (login)
@app.get("/api/session")
def api_session() -> Response:
    cfg = load_session_config()
    if not cfg.get("authorization"):
        return jsonify({"configured": False})
    username = _check_session(cfg)
    if username is None:
        return jsonify({"configured": False, "error": "session expiree ou invalide"})
    return jsonify({"configured": True, "username": username})


@app.post("/api/session")
def api_session_save() -> Response:
    body = request.get_json(force=True) or {}
    parsed = parse_session_paste(body.get("raw", "")) if body.get("raw") else {}
    cfg = {
        "authorization": (body.get("authorization") or parsed.get("authorization") or "").strip(),
        "cookie": (body.get("cookie") or parsed.get("cookie") or "").strip(),
        "client_id": (body.get("client_id") or parsed.get("client_id") or "").strip(),
        "session_id": (body.get("session_id") or parsed.get("session_id") or "").strip(),
    }
    if not cfg["authorization"]:
        return jsonify({"error": "authorization introuvable dans ce qui a ete colle"}), 400
    try:
        username = FanslyClient(cfg).get_me().get("username")
    except FanslyError as exc:
        return jsonify({"error": f"session invalide ou expiree : {exc}"}), 400
    cfg["client_checks"] = {}
    SESSION_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    _session_cache.update(username=username, checked_at=time.time())
    return jsonify({"ok": True, "username": username})


@app.post("/api/session/logout")
def api_session_logout() -> Response:
    if SESSION_CONFIG.exists():
        SESSION_CONFIG.unlink()
    _session_cache.update(username=None, checked_at=0.0)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- publication
@app.get("/api/settings")
def api_settings() -> Response:
    cfg = load_sched_config()
    cats = _categories_cfg(cfg)
    reels_dir = resolve_reels_dir(cfg)
    folders = {c: v.get("folder", c) for c, v in cats.items()}
    counts = sched_mod.collect_reels(reels_dir, folders)
    return jsonify({
        "config": cfg,
        "reels_dir": str(reels_dir),
        "reels": {c: [str(f) for f in files] for c, files in counts.items()},
        "reels_count": {c: len(files) for c, files in counts.items()},
    })


@app.post("/api/settings")
def api_settings_save() -> Response:
    body = request.get_json(force=True)
    cfg = body.get("config")
    if not isinstance(cfg, dict):
        abort(400, description="config invalide")
    SCHED_CONFIG.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify({"ok": True})


@app.get("/api/tags/trending")
def api_trending_tags() -> Response:
    n = request.args.get("n", 5, type=int)
    return jsonify(sched_mod.trending_tags(n))


@app.get("/api/tags/best")
def api_best_tags() -> Response:
    n = request.args.get("n", 5, type=int)
    return jsonify(sched_mod.opportunity_tags(n))


def _load_videos_dict() -> dict:
    if DATA_FILE.exists():
        return {v["mediaOfferId"]: v for v in json.loads(DATA_FILE.read_text(encoding="utf-8"))}
    return {}


def _save_videos(videos: dict) -> None:
    import fetch_fyp_live as fyp  # noqa: PLC0415

    now = time.time()
    ordered = sorted(videos.values(), key=lambda v: fyp.rank_key(v, now))
    DATA_FILE.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
    _cache["videos"] = ordered
    _cache["by_id"] = {v["mediaOfferId"]: v for v in ordered}


def write_best_tags(tag_stats: dict) -> list:
    rows = []
    for tag, s in tag_stats.items():
        vc = s.get("viewCount") or 0
        pc = s.get("postCount") or 0
        rows.append({
            "tag": tag,
            "freq_fyp": s.get("freq", 0),
            "viewCount": int(vc),
            "postCount": int(pc),
            "opportunity": round(vc / (pc + 1), 1) if vc else 0,
        })
    rows.sort(key=lambda r: -r["opportunity"])
    (BASE / "best_tags.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def _analyze_async() -> None:
    st = _analyze_state
    st.update(running=True, log=[], error=None, report=None)
    try:
        cfg = load_session_config()
        tag_stats: dict = {}
        if cfg.get("authorization"):
            st["log"].append("Scraping FYP (rafraichissement)...")
            try:
                import fetch_fyp_live as fyp  # noqa: PLC0415
                session = fyp.make_session(cfg)
                headers = fyp.build_headers(cfg)
                videos = _load_videos_dict()
                before = len(videos)
                streams = fyp.make_streams(50)
                for s in streams:
                    for _ in range(6):
                        r = session.get(s["url"](s["after"]), headers=headers, timeout=20)
                        if r.status_code != 200:
                            break
                        resp = r.json()
                        records, last_id = fyp.extract(resp)
                        for tag, ts in fyp.extract_tag_stats(resp).items():
                            d = tag_stats.setdefault(tag, {"viewCount": 0, "postCount": 0, "freq": 0})
                            d["viewCount"] = max(d["viewCount"], ts["viewCount"])
                            d["postCount"] = max(d["postCount"], ts["postCount"])
                            d["freq"] += ts["freq"]
                        if not records:
                            break
                        for v in records:
                            videos.setdefault(v["mediaOfferId"], v)
                        if last_id is None or last_id <= s["after"]:
                            break
                        s["after"] = last_id
                        time.sleep(1)
                _save_videos(videos)
                st["log"].append(f"  -> {len(videos)} reels (+{len(videos) - before})")
            except Exception as exc:  # noqa: BLE001
                st["log"].append(f"  [warn] scraping: {exc}")
        else:
            st["log"].append("[skip] pas de session -> analyse sur les donnees existantes")

        st["log"].append("Analyse des meilleurs tags...")
        if tag_stats:
            rows = write_best_tags(tag_stats)
        else:
            try:
                rows = json.loads((BASE / "best_tags.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                rows = []
        top = [r["tag"] for r in rows[:5]]
        st["log"].append(f"  -> {len(rows)} tags, top: {', '.join(top)}")

        st["log"].append("Analyse des meilleures heures...")
        tz = app_tz()
        hours, _ = compute_best_hours(5, tz)
        hour_strs = [f"{h['hour']:02d}:{h['minute']:02d}" for h in hours]
        st["log"].append(f"  -> {', '.join(hour_strs)}")

        st["report"] = {
            "total_reels": len(_cache["videos"]),
            "top_tags": [r["tag"] for r in rows[:10]],
            "best_hours": hour_strs,
        }
        st["log"].append("Analyse terminee")
    except Exception as exc:  # noqa: BLE001
        st["error"] = str(exc)
    finally:
        st["running"] = False


@app.post("/api/analyze")
def api_analyze() -> Response:
    if _analyze_state["running"]:
        return jsonify({"error": "analyse deja en cours"}), 409
    threading.Thread(target=_analyze_async, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/analyze/status")
def api_analyze_status() -> Response:
    return jsonify(_analyze_state)


@app.post("/api/pick-folder")
def api_pick_folder() -> Response:
    script = 'POSIX path of (choose folder with prompt "Choisir le dossier des reels")'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"cancelled": True})
    if proc.returncode != 0:
        return jsonify({"cancelled": True})
    path = proc.stdout.strip()
    if not path:
        return jsonify({"cancelled": True})
    return jsonify({"path": path})


@app.get("/api/browse")
def api_browse() -> Response:
    p = request.args.get("path", str(Path.home()))
    d = Path(p).expanduser()
    if not d.is_dir():
        return jsonify({"error": "dossier introuvable"}), 404
    dirs: list[dict] = []
    videos: list[dict] = []
    try:
        for child in sorted(d.iterdir(), key=lambda c: c.name.lower()):
            if child.is_dir():
                dirs.append({"name": child.name, "path": str(child)})
            elif child.suffix.lower() in sched_mod.VIDEO_EXTS:
                videos.append({"name": child.name, "path": str(child)})
    except PermissionError:
        return jsonify({"error": "permission refusee"}), 403
    parent = str(d.parent) if d.parent != d else None
    return jsonify({
        "path": str(d),
        "parent": parent,
        "dirs": dirs,
        "videos": videos,
    })


@app.get("/api/best-hours")
def api_best_hours() -> Response:
    n = request.args.get("n", 5, type=int)
    n = max(1, min(n, 24))
    tz = app_tz()
    hours, curve = compute_best_hours(n, tz)
    return jsonify({"hours": hours, "curve": [round(x, 2) for x in curve]})


@app.post("/api/schedule")
def api_schedule() -> Response:
    if _run_state["running"]:
        return jsonify({"error": "deja en cours"}), 409
    body = request.get_json(force=True) or {}
    folder = (body.get("folder") or "").strip()
    posts_per_day = int(body.get("posts_per_day") or 0)
    tz = app_tz()
    start_date_str = (body.get("start_date") or "").strip()
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "start_date invalide (attendu AAAA-MM-JJ)"}), 400
    else:
        start_date = datetime.now(tz).date()
    raw_tags = body.get("tags") or []
    if isinstance(raw_tags, str):
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    else:
        tags = [t for t in raw_tags if str(t).strip()]
    caption = (body.get("caption") or "").strip()

    d = Path(folder).expanduser()
    if not d.is_dir():
        return jsonify({"error": "dossier introuvable"}), 400
    files = sorted(
        f for f in d.iterdir()
        if f.is_file() and f.suffix.lower() in sched_mod.VIDEO_EXTS
    )
    if not files:
        return jsonify({"error": "aucune video dans ce dossier"}), 400
    if posts_per_day <= 0:
        return jsonify({"error": "posts_per_day doit etre > 0"}), 400

    hours, _ = compute_best_hours(posts_per_day, tz)
    if not hours:
        return jsonify({"error": "pas assez de donnees pour analyser les horaires"}), 400

    plan = build_hour_plan(files, hours, start_date, tz, tags, caption)
    threading.Thread(target=_run_async, args=(plan,), daemon=True).start()
    return jsonify({
        "ok": True,
        "total": len(plan),
        "hours": [f"{h['hour']:02d}:{h['minute']:02d}" for h in hours],
        "first_slots": [p["at"].strftime("%Y-%m-%d %H:%M") for p in plan[:posts_per_day]],
    })


@app.post("/api/scheduled/<post_id>/delete")
def api_scheduled_delete(post_id: str) -> Response:
    cfg = load_session_config()
    if not cfg.get("authorization"):
        return jsonify({"error": "config.json (session) manquant"}), 400
    client = FanslyClient(cfg)
    try:
        client.cancel_scheduled_post(post_id)
    except FanslyError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True})


def _attachments_of(p: dict) -> list[dict]:
    atts = p.get("attachments") or []
    if not atts and p.get("postTemplate"):
        try:
            atts = json.loads(p["postTemplate"]).get("attachments", [])
        except Exception:  # noqa: BLE001
            pass
    return atts


@app.post("/api/scheduled/delete-all")
def api_scheduled_delete_all() -> Response:
    cfg = load_session_config()
    if not cfg.get("authorization"):
        return jsonify({"error": "config.json (session) manquant"}), 400
    client = FanslyClient(cfg)
    try:
        posts = client.list_scheduled()
    except FanslyError as exc:
        return jsonify({"error": str(exc)}), 502

    # Resout quel fichier local correspond a chaque post programme, pour ne retirer
    # du journal QUE ce qui est annule ici (jamais les deja-publies : sinon ils
    # redeviennent "a faire" et se reuploadent en double au prochain lancement).
    ids = [a["contentId"] for p in posts for a in _attachments_of(p)]
    media_by_id = client.account_media_batch(ids)
    stems_to_clear = set()
    for p in posts:
        atts = _attachments_of(p)
        if not atts:
            continue
        m = media_by_id.get(atts[0]["contentId"])
        fn = (m.get("media") or {}).get("filename") if m else None
        if fn:
            stems_to_clear.add(Path(fn).stem.lower())

    deleted = 0
    errors = 0
    for p in posts:
        pid = p.get("postId") or p.get("id")
        if not pid:
            continue
        try:
            client.cancel_scheduled_post(pid)
            deleted += 1
        except FanslyError:
            errors += 1
        time.sleep(0.3)

    journal = sched_mod.load_log()
    pruned = {f for f in journal if Path(f).stem.lower() not in stems_to_clear}
    sched_mod.save_log(pruned)
    return jsonify({"ok": True, "deleted": deleted, "errors": errors, "total": len(posts)})


@app.get("/api/scheduled")
def api_scheduled() -> Response:
    cfg = load_session_config()
    if not cfg.get("authorization"):
        return jsonify({"error": "config.json (session) manquant"}), 400
    client = FanslyClient(cfg)
    try:
        rows = client.list_scheduled()
    except FanslyError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(rows)


def _post_thumbnail(media_entry: dict | None) -> str | None:
    media = (media_entry or {}).get("media") or {}
    for v in media.get("variants", []):
        if str(v.get("mimetype", "")).startswith("image/"):
            locs = v.get("locations") or []
            if locs:
                return locs[0].get("location")
    return None


def _content_parts(content: str) -> tuple[str, list[str]]:
    tags = re.findall(r"#(\w+)", content or "")
    desc = re.sub(r"#\w+", "", content or "")
    desc = re.sub(r"\s{2,}", " ", desc).strip()
    return desc, tags


@app.get("/api/recap")
def api_recap() -> Response:
    cfg = load_session_config()
    if not cfg.get("authorization"):
        return jsonify({"error": "config.json (session) manquant"}), 400
    client = FanslyClient(cfg)
    try:
        me = client.get_me()
        scheduled = client.list_scheduled()
        published = client.own_timeline(me["id"])
    except FanslyError as exc:
        return jsonify({"error": str(exc)}), 502

    def _extract(items: list[dict], is_scheduled: bool) -> list[dict]:
        rows = []
        for p in items:
            content = p.get("content", "")
            attachments = p.get("attachments") or []
            if not content or not attachments:
                try:
                    tpl = json.loads(p.get("postTemplate") or "{}")
                    content = content or tpl.get("content", "")
                    attachments = attachments or tpl.get("attachments", [])
                except Exception:  # noqa: BLE001
                    pass
            content_id = attachments[0]["contentId"] if attachments else None
            rows.append({
                "content_id": content_id,
                "content": content,
                "when": p.get("scheduledFor") if is_scheduled else p.get("createdAt"),
                "postId": p.get("postId") or p.get("id"),
                "likeCount": None if is_scheduled else p.get("mediaLikeCount"),
            })
        return rows

    sched_rows = _extract(scheduled, True)
    pub_rows = _extract(published, False)

    all_ids = [r["content_id"] for r in sched_rows + pub_rows if r["content_id"]]
    media_by_id = client.account_media_batch(all_ids)

    def _index_by_stem(rows: list[dict]) -> dict[str, dict]:
        """Cle = nom de fichier SANS extension : Fansly retranscode les .mov en
        .mp4 et renomme le fichier cote serveur, donc matcher sur l'extension
        casse le rapprochement pour tout ce qui n'etait pas deja .mp4."""
        idx = {}
        for r in rows:
            m = media_by_id.get(r["content_id"])
            fn = (m.get("media") or {}).get("filename") if m else None
            if fn:
                idx[Path(fn).stem.lower()] = r
        return idx

    by_filename_pub = _index_by_stem(pub_rows)
    by_filename_sched = _index_by_stem(sched_rows)

    tz = app_tz()
    out = []
    for fpath in sorted(sched_mod.load_log()):
        name = Path(fpath).name
        key = Path(name).stem.lower()
        row = by_filename_pub.get(key)
        status, when, content, like_count, post_id, thumb = "introuvable", None, "", None, None, None
        if row:
            status = "publie"
            when = datetime.fromtimestamp(row["when"], tz).strftime("%Y-%m-%d %H:%M") if row["when"] else None
            content, like_count, post_id = row["content"], row["likeCount"], row["postId"]
            thumb = _post_thumbnail(media_by_id.get(row["content_id"]))
        else:
            row = by_filename_sched.get(key)
            if row:
                status = "programme"
                when = datetime.fromtimestamp(row["when"] / 1000, tz).strftime("%Y-%m-%d %H:%M") if row["when"] else None
                content, post_id = row["content"], row["postId"]
                thumb = _post_thumbnail(media_by_id.get(row["content_id"]))

        desc, tags = _content_parts(content)
        out.append({
            "file": name, "status": status, "when": when,
            "description": desc, "tags": tags, "thumbnail": thumb,
            "likeCount": like_count, "postId": post_id,
        })

    return jsonify(out)


@app.post("/api/scheduler/plan")
def api_scheduler_plan() -> Response:
    cfg = load_sched_config()
    cats_cfg = _categories_cfg(cfg)
    reels_dir = resolve_reels_dir(cfg)
    folders = {c: v.get("folder", c) for c, v in cats_cfg.items()}
    videos = sched_mod.collect_reels(reels_dir, folders)
    if not any(videos.values()):
        return jsonify({"error": "aucune video dans les dossiers reels", "total": 0, "plan": []})
    start_date = start_date_of(cfg)
    plan = sched_mod.build_plan(videos, cats_cfg, start_date, int(cfg.get("min_gap_minutes", 0)))
    return jsonify({
        "total": len(plan),
        "plan": [
            {
                "category": it["category"],
                "file": it["file"].name,
                "path": str(it["file"]),
                "at": it["at"].strftime("%Y-%m-%d %H:%M"),
                "caption": sched_mod.caption_for(it),
            }
            for it in plan
        ],
    })


def _run_async(plan: list[dict]) -> None:
    _run_state.update(running=True, log=[], done=0, total=len(plan), error=None, stop_requested=False)
    try:
        cfg = load_session_config()
        if not cfg.get("authorization"):
            _run_state["error"] = "config.json (session) manquant"
            return
        client = FanslyClient(cfg)
        done = sched_mod.load_log()
        total = len(plan)
        for i, item in enumerate(plan, 1):
            if _run_state.get("stop_requested"):
                _run_state["log"].append(f"[STOP] arrete par l'utilisateur ({i - 1}/{total} traites)")
                break
            key = str(item["file"])
            when = item["at"].strftime("%Y-%m-%d %H:%M")
            if key in done:
                _run_state["log"].append(f"[{i}/{total}] SKIP (deja fait) {item['file'].name}")
                _run_state["done"] = i
                continue
            caption = sched_mod.caption_for(item)
            scheduled_ms = int(item["at"].timestamp() * 1000)
            try:
                _, am_id = client.upload_media(item["file"], tags=sched_mod.effective_tags(item["cfg"]))
                client.create_post(caption, am_id, scheduled_for_ms=scheduled_ms)
                done.add(key)
                sched_mod.save_log(done)
                _run_state["log"].append(f"[{i}/{total}] OK {item['file'].name} -> {when}")
            except FanslyError as exc:
                _run_state["log"].append(f"[{i}/{total}] ERR {item['file'].name} : {exc}")
            _run_state["done"] = i
            time.sleep(1)
    except Exception as exc:  # noqa: BLE001
        _run_state["error"] = str(exc)
    finally:
        _run_state["running"] = False


@app.post("/api/scheduler/reset-log")
def api_reset_log() -> Response:
    sched_mod.save_log(set())
    return jsonify({"ok": True})


@app.post("/api/scheduler/run")
def api_scheduler_run() -> Response:
    if _run_state["running"]:
        return jsonify({"error": "deja en cours"}), 409
    cfg = load_sched_config()
    cats_cfg = _categories_cfg(cfg)
    reels_dir = resolve_reels_dir(cfg)
    folders = {c: v.get("folder", c) for c, v in cats_cfg.items()}
    videos = sched_mod.collect_reels(reels_dir, folders)
    start_date = start_date_of(cfg)
    plan = sched_mod.build_plan(videos, cats_cfg, start_date, int(cfg.get("min_gap_minutes", 0)))
    if not plan:
        return jsonify({"error": "aucune video a programmer"}), 400
    threading.Thread(target=_run_async, args=(plan,), daemon=True).start()
    return jsonify({"ok": True, "total": len(plan)})


@app.get("/api/scheduler/status")
def api_scheduler_status() -> Response:
    return jsonify(_run_state)


@app.post("/api/scheduler/stop")
def api_scheduler_stop() -> Response:
    if not _run_state["running"]:
        return jsonify({"error": "aucune programmation en cours"}), 400
    _run_state["stop_requested"] = True
    return jsonify({"ok": True})


# ---------------------------------------------------------------- streaming
def _fetch_stream(url: str):
    range_header = request.headers.get("Range")
    headers = {"User-Agent": request.headers.get("User-Agent", "") or "curl/8.0"}
    if range_header:
        headers["Range"] = range_header
    r = requests.get(url, stream=True, timeout=30, headers=headers)
    if r.status_code not in (200, 206):
        abort(502, description=f"CDN a repondu {r.status_code}")
    return r


def _stream_response(r: requests.Response) -> Response:
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": r.headers.get("Content-Type", "video/mp4"),
    }
    if r.status_code == 206:
        if r.headers.get("Content-Range"):
            headers["Content-Range"] = r.headers["Content-Range"]
        if r.headers.get("Content-Length"):
            headers["Content-Length"] = r.headers["Content-Length"]
    elif r.headers.get("Content-Length"):
        headers["Content-Length"] = r.headers["Content-Length"]

    def generate():
        for chunk in r.iter_content(chunk_size=1024 * 256):
            yield chunk

    return Response(generate(), status=r.status_code, headers=headers)


@app.get("/stream/<moid>")
def stream(moid: str) -> Response:
    load_data()
    v = _cache["by_id"].get(moid)
    if not v or not v.get("downloadUrl"):
        abort(404, description="Video verrouillee ou introuvable")
    return _stream_response(_fetch_stream(v["downloadUrl"]))


@app.get("/download/<moid>")
def download(moid: str) -> Response:
    load_data()
    v = _cache["by_id"].get(moid)
    if not v or not v.get("downloadUrl"):
        abort(404, description="Video verrouillee ou introuvable")

    name = f"{v.get('username', 'video')}_{moid}.mp4"
    r = _fetch_stream(v["downloadUrl"])
    resp = _stream_response(r)
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
