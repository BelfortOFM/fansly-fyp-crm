#!/usr/bin/env python3
"""Analyse les meilleures heures de posting depuis un/des HAR Fansly.

Methode :
  1. extrait les posts de la FYP (createdAt + mediaLikeCount + accountId)
  2. corrige l'accumulation : v = mediaLikeCount / age_heures  (vitesse d'engagement)
  3. ne garde que les posts ages de 6h a 14 jours (evite fresh/vieux biaises)
  4. agrege la vitesse par heure de la journee (fuseau local)
  5. trouve les fenetres contigues les plus rentables
  6. met a jour scheduler_config.json (start/end par categorie)

Usage :
  python3 analyze_hours.py                    # analyse les 2 HAR du projet
  python3 analyze_hours.py --har A.har B.har  # HAR specifiques
  python3 analyze_hours.py --tz Europe/Paris --max-age-days 14
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_HARS = sorted(HERE.parent.glob("*.har"))
SCHED_CONF = HERE / "scheduler_config.json"


def extract_posts(har: Path) -> list[dict]:
    jq_expr = (
        '.log.entries[]'
        ' | select(.request.url | test("contentdiscovery/media/suggestionsnew"))'
        ' | .response.content.text'
    )
    proc = subprocess.run(["jq", "-r", jq_expr, str(har)], capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[WARN] jq impossible sur {har.name}", file=sys.stderr)
        return []
    posts: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            body = json.loads(base64.b64decode(line.strip())).get("response", {})
        except Exception:  # noqa: BLE001
            continue
        for p in body.get("aggregationData", {}).get("posts", []):
            posts[p["id"]] = {
                "createdAt": p.get("createdAt"),
                "mediaLikeCount": p.get("mediaLikeCount", 0),
                "followCount": 0,
                "accountId": p.get("accountId"),
            }
        # enrichir followCount depuis les comptes
        for a in body.get("aggregationData", {}).get("accounts", []):
            pass
    return list(posts.values())


def velocity_by_hour(posts: list[dict], tz, min_age_h: float, max_age_days: float) -> dict[int, list[float]]:
    now = datetime.now(tz).timestamp()
    hourly: dict[int, list[float]] = defaultdict(list)
    for p in posts:
        if not p["createdAt"]:
            continue
        age_h = (now - p["createdAt"]) / 3600.0
        if age_h < min_age_h or age_h > max_age_days * 24:
            continue
        v = p["mediaLikeCount"] / max(age_h, 1.0)
        hour = datetime.fromtimestamp(p["createdAt"], tz).hour
        hourly[hour].append(v)
    return hourly


def smoothed(hourly: dict[int, list[float]]) -> list[float]:
    mean = {h: (sum(v) / len(v)) for h, v in hourly.items()}
    out = []
    for h in range(24):
        vals = [mean.get((h + d) % 24, 0.0) for d in (-1, 0, 1)]
        out.append(sum(vals) / 3.0)
    return out


def best_window(smooth: list[float], length: int) -> tuple[int, int, float]:
    """Fenetre contigue de `length` heures (sans traverser minuit) maximisant la somme."""
    best_sum, best_start = -1.0, 0
    for start in range(0, 24 - length + 1):
        s = sum(smooth[start + i] for i in range(length))
        if s > best_sum:
            best_sum, best_start = s, start
    return best_start, best_start + length, best_sum


def bar(value: float, peak: float) -> str:
    n = int(round(value / peak * 20)) if peak > 0 else 0
    return "#" * n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--har", nargs="+", default=[str(h) for h in DEFAULT_HARS if h.exists()])
    ap.add_argument("--tz", default="Europe/Paris")
    ap.add_argument("--min-age-hours", type=float, default=6.0)
    ap.add_argument("--max-age-days", type=float, default=14.0)
    args = ap.parse_args()

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(args.tz)
    except Exception:  # noqa: BLE001
        tz = datetime.now().astimezone().tzinfo

    posts: list[dict] = []
    for h in args.har:
        got = extract_posts(Path(h))
        print(f"[har] {Path(h).name}: {len(got)} posts")
        posts.extend(got)
    seen = set()
    posts = [p for p in posts if not (p["createdAt"] in seen or seen.add(p["createdAt"]))]
    if not posts:
        sys.exit("[ERR] aucun post exploitable")

    hourly = velocity_by_hour(posts, tz, args.min_age_hours, args.max_age_days)
    smooth = smoothed(hourly)
    peak = max(smooth)

    print(f"\n=== Engagement/heure ({args.tz}, {len(posts)} posts, age 6h-{args.max_age_days:g}j) ===\n")
    for h in range(24):
        n = len(hourly.get(h, []))
        print(f"  {h:02d}:00  {bar(smooth[h], peak):<20} {smooth[h]:7.1f}  ({n} posts)")

    # fenetres recommandees
    norm_len = 13   # Normaux : 4 posts etales sur ~13h
    porn_len = 4    # porn : 2 posts sur ~4h (soir)
    n_start, n_end, _ = best_window(smooth, norm_len)
    p_start, p_end, _ = best_window(smooth, porn_len)
    print(f"\n=== Fenetres recommandees ===")
    print(f"  Normaux : {n_start:02d}:00 -> {n_end:02d}:00  ({norm_len}h)")
    print(f"  porn    : {p_start:02d}:00 -> {p_end:02d}:00  ({porn_len}h)")

    if SCHED_CONF.exists():
        cfg = json.loads(SCHED_CONF.read_text(encoding="utf-8"))
        cats = cfg.get("categories", {})
        if "Normaux" in cats:
            cats["Normaux"]["start"] = f"{n_start:02d}:00"
            cats["Normaux"]["end"] = f"{n_end:02d}:00"
        if "porn" in cats:
            cats["porn"]["start"] = f"{p_start:02d}:00"
            cats["porn"]["end"] = f"{p_end:02d}:00"
        SCHED_CONF.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[OK] scheduler_config.json mis a jour")
    else:
        print("\n[INFO] scheduler_config.json absent -> cree les fenetres manuellement")


if __name__ == "__main__":
    main()
