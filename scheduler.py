#!/usr/bin/env python3
"""Scheduler auto Fansly : planifie les reels de dossiers categories.

Logique par defaut (configurable dans scheduler_config.json) :
  - Normaux : 4/jour entre 06:00 et 23:00
  - porn    : 2/jour entre 18:00 et 23:00

Les videos sont uploadees maintenant, et le post est planifie (scheduledFor)
au creneau genere. Un journal (scheduled_log.json) evite de re-planifier
un fichier deja traite.

Usage :
  python3 scheduler.py --dry-run            # affiche le plan sans rien envoyer
  python3 scheduler.py --go                 # execute vraiment (upload + schedule)
  python3 scheduler.py --config mon.json --go
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fansly_api import FanslyClient, FanslyError

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "scheduler_config.json"
LOG_FILE = HERE / "scheduled_log.json"

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
BEST_TAGS = HERE / "best_tags.json"


def trending_tags(n: int = 5) -> list[str]:
    """Les n tags les plus en tendance (par viewCount) depuis best_tags.json."""
    if not BEST_TAGS.exists():
        return []
    try:
        rows = json.loads(BEST_TAGS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    rows.sort(key=lambda r: (-(r.get("viewCount") or 0)))
    return [r.get("tag") for r in rows[:n] if r.get("tag")]


def opportunity_tags(n: int = 5) -> list[str]:
    """Les n tags les plus rentables (opportunite = engagement relatif aux abonnes).
    Pour un petit compte, ces tags de niche sont bien plus efficaces que les tags
    satures (teen/pussy) pour etre decouvert."""
    if not BEST_TAGS.exists():
        return []
    try:
        rows = json.loads(BEST_TAGS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    rows.sort(key=lambda r: (-(r.get("opportunity") or 0), -(r.get("viewCount") or 0)))
    return [r.get("tag") for r in rows[:n] if r.get("tag")]


CAPTIONS = [
    "just a lazy afternoon mood",
    "felt cute, had to share",
    "little behind the scenes moment",
    "just doing my thing today",
    "golden hour hits different",
    "sharing a bit of my world",
    "this one's just for fun",
    "caught in a candid moment",
    "soft and a little mischievous today",
    "just an exhibitionist who likes to share",
    "new favorite spot to film",
    "nothing serious, just vibes",
    "a little peek into my day",
    "filming whenever the mood strikes",
    "just me being me",
    "sharing some art today",
    "another one for the collection",
    "just existing out loud",
    "quiet moment, thought i'd share it",
    "here's a little something",
    "casual post, no agenda",
    "just enjoying the process",
    "a glimpse behind the curtain",
    "sunday mood, apparently",
    "just felt like sharing this one",
]


def random_caption() -> str:
    return random.choice(CAPTIONS)


CASUAL_FALLBACK_TAGS = ["petite", "amateur", "homemade", "solo", "lingerie"]


def effective_tags(cfg: dict) -> list[str]:
    """Tags configures (sinon un set neutre 'casual'), avec 'fyp' toujours inclus.
    Le fallback n'utilise plus opportunity_tags() : ces tags viennent du scraping
    brut du FYP d'autres comptes et peuvent contenir n'importe quoi (meme des tags
    a eviter) sans rapport avec le ton qu'on veut donner au compte."""
    tags = [t.lstrip("#") for t in (cfg.get("tags") or []) if str(t).strip()]
    if not tags:
        tags = list(CASUAL_FALLBACK_TAGS)
    if "fyp" not in tags:
        tags = ["fyp"] + tags
    return tags


def local_tz():
    return datetime.now().astimezone().tzinfo


def parse_hhmm(s: str) -> timedelta:
    if not s or ":" not in s:
        return timedelta(0)
    h, m = s.split(":")
    return timedelta(hours=int(h), minutes=int(m))


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("timezone", None)
    cfg.setdefault("start_date", datetime.now().strftime("%Y-%m-%d"))
    return cfg


def load_log() -> set[str]:
    if LOG_FILE.exists():
        return set(json.loads(LOG_FILE.read_text(encoding="utf-8")))
    return set()


def save_log(done: set[str]) -> None:
    LOG_FILE.write_text(json.dumps(sorted(done), ensure_ascii=False, indent=2), encoding="utf-8")


def collect_reels(reels_dir: Path, folders: dict[str, str]) -> dict[str, list[Path]]:
    """Retourne {categorie: [fichiers videos triés]}."""
    result: dict[str, list[Path]] = {}
    for cat, folder in folders.items():
        d = reels_dir / folder
        files = sorted(
            f for f in d.glob("*") if f.is_file() and f.suffix.lower() in VIDEO_EXTS
        )
        result[cat] = files
    return result


def generate_day_slots(start: str, end: str, per_day: int) -> list[timedelta]:
    """N créneaux répartis dans [start,end], un point aléatoire par sous-intervalle."""
    if per_day <= 0:
        return []
    window = parse_hhmm(end) - parse_hhmm(start)
    if window <= timedelta(0):
        window = timedelta(hours=24)
    seg = window / per_day
    slots: list[timedelta] = []
    for i in range(per_day):
        lo = parse_hhmm(start) + seg * i
        jitter = random.uniform(0, seg.total_seconds())
        slots.append(lo + timedelta(seconds=jitter))
    return sorted(slots)


def build_plan(
    videos: dict[str, list[Path]],
    cats_cfg: dict[str, dict],
    start_date: datetime,
    min_gap_min: int = 0,
) -> list[dict]:
    """Plan jour par jour, avec ecart minimum global entre posts."""
    per_cat: dict[str, list[tuple]] = {}
    for cat, files in videos.items():
        cfg = cats_cfg[cat]
        per_day = int(cfg.get("per_day") or 0)
        if per_day <= 0 or not files:
            per_cat[cat] = []
            continue
        per_cat[cat] = [(f, i // per_day, i % per_day) for i, f in enumerate(files)]

    max_days = 1
    for items in per_cat.values():
        for _, d, _ in items:
            max_days = max(max_days, d + 1)

    plan: list[dict] = []
    for day in range(max_days):
        day_slots: list[dict] = []
        for cat, items in per_cat.items():
            cfg = cats_cfg[cat]
            per_day = int(cfg.get("per_day") or 0)
            if per_day <= 0:
                continue
            slots = generate_day_slots(
                cfg.get("start") or "00:00", cfg.get("end") or "23:59", per_day
            )
            for file, d, pos in items:
                if d == day:
                    at = start_date + timedelta(days=day) + slots[pos]
                    day_slots.append({"file": file, "category": cat, "at": at, "cfg": cfg})
        day_slots.sort(key=lambda x: x["at"])
        if min_gap_min > 0:
            _enforce_gap(day_slots, min_gap_min)
        plan.extend(day_slots)
    return plan


def _enforce_gap(day_slots: list[dict], min_gap_min: int) -> None:
    if not day_slots:
        return
    gap = timedelta(minutes=min_gap_min)
    day_end = day_slots[0]["at"].replace(hour=23, minute=59, second=0, microsecond=0)
    for i in range(1, len(day_slots)):
        prev = day_slots[i - 1]["at"]
        if day_slots[i]["at"] - prev < gap:
            new = prev + gap
            day_slots[i]["at"] = min(new, day_end)


def caption_for(item: dict) -> str:
    cfg = item["cfg"]
    base = (cfg.get("caption") or "").strip()
    if not base:
        base = random_caption()
    tags = effective_tags(cfg)
    hashes = " ".join(f"#{t.strip().lstrip('#')}" for t in tags if t.strip())
    return f"{base.strip()} {hashes}".strip()


def run(client: FanslyClient, plan: list[dict], dry_run: bool) -> None:
    done = load_log()
    for i, item in enumerate(plan, 1):
        key = str(item["file"])
        when = item["at"].strftime("%Y-%m-%d %H:%M")
        if key in done:
            print(f"[{i}/{len(plan)}] SKIP (deja fait) {item['file'].name}")
            continue
        caption = caption_for(item)
        scheduled_ms = int(item["at"].timestamp() * 1000)
        print(f"[{i}/{len(plan)}] {item['category']:<8} {item['file'].name:<32} -> {when}")
        if dry_run:
            print(f"          caption: {caption}")
            continue
        try:
            _, am_id = client.upload_media(item["file"], tags=effective_tags(item["cfg"]))
            resp = client.create_post(caption, am_id, scheduled_for_ms=scheduled_ms)
            print(f"          postId={resp.get('postId')} OK")
            done.add(key)
            save_log(done)
        except FanslyError as exc:
            print(f"          [ERR] {exc}", file=sys.stderr)
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Scheduler auto Fansly")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--dry-run", action="store_true", help="affiche le plan seulement (defaut)")
    ap.add_argument("--go", action="store_true", help="execute upload + schedule")
    args = ap.parse_args()

    if not args.go:
        args.dry_run = True

    cfg = load_config(Path(args.config))
    reels_dir = Path(cfg.get("reels_dir", "../reels"))
    cats_cfg = cfg["categories"]

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cfg["timezone"]) if cfg.get("timezone") else local_tz()
    except Exception:  # noqa: BLE001
        tz = local_tz()
    start_date = datetime.strptime(cfg["start_date"], "%Y-%m-%d").replace(tzinfo=tz)

    folders = {cat: c["folder"] for cat, c in cats_cfg.items()}
    videos = collect_reels(reels_dir, folders)

    total = sum(len(v) for v in videos.values())
    for cat, files in videos.items():
        print(f"[scan] {cat}: {len(files)} reels")
    if total == 0:
        sys.exit("[ERR] aucune video trouvee dans les dossiers categories")

    plan = build_plan(videos, cats_cfg, start_date, int(cfg.get("min_gap_minutes", 0)))
    print(f"[plan] {len(plan)} posts a programmer "
          f"du {plan[0]['at'].strftime('%Y-%m-%d')} au {plan[-1]['at'].strftime('%Y-%m-%d')}\n")

    if args.dry_run:
        print(f"--- DRY RUN (aucun envoi) ---")
        run(None, plan, dry_run=True)
        print(f"\nPour executer vraiment : python3 scheduler.py --go")
        return

    cfg_session = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    client = FanslyClient(cfg_session)
    run(client, plan, dry_run=False)


if __name__ == "__main__":
    main()
