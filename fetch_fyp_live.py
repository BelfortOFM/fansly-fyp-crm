#!/usr/bin/env python3
"""Scrape la FYP Fansly en direct, en pivotant sur plusieurs endpoints pour
contourner le rate-limit (chaque endpoint a son propre quota cote serveur).

Endpoints utilises (tous retournent mediaOfferSuggestions + aggregationData) :
  - contentdiscovery/media/suggestionsnew  (flux FYP courant)
  - contentdiscovery/media/suggestions     (flux legacy, quota separe)

Usage :
  python3 fetch_fyp_live.py --target 1000
  python3 fetch_fyp_live.py --target 500 --limit 50 --delay 4
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
API = "https://apiv3.fansly.com/api/v1/contentdiscovery"
OUT = HERE / "videos_crm.json"
CHECK = "1b76d5db9ffa38"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/26.5.2 Safari/605.1.15"
)


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for k in ("authorization", "cookie", "client_id", "session_id"):
        if not cfg.get(k):
            sys.exit(f"[ERR] config {path} : champ '{k}' manquant")
    return cfg


def first_thumbnail(variants: list[dict]) -> str | None:
    for v in variants or []:
        if (v.get("mimetype") or "").startswith("image/"):
            for loc in v.get("locations", []):
                if loc.get("location"):
                    return loc["location"]
    return None


def parse_duration(metadata: str | None) -> float | None:
    if not metadata:
        return None
    try:
        return float(json.loads(metadata).get("duration", 0))
    except Exception:  # noqa: BLE001
        return None


def make_session(cfg: dict) -> requests.Session:
    s = requests.Session()
    for part in cfg["cookie"].split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            s.cookies.set(k.strip(), v, domain=".fansly.com")
    return s


def build_headers(cfg: dict) -> dict:
    return {
        "Authorization": cfg["authorization"],
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://fansly.com",
        "Referer": "https://fansly.com/",
        "User-Agent": UA,
        "fansly-client-id": cfg["client_id"],
        "fansly-session-id": cfg["session_id"],
        "fansly-client-check": CHECK,
        "fansly-client-ts": str(int(time.time() * 1000)),
    }


def extract(resp: dict) -> tuple[list[dict], int | None]:
    body = resp.get("response", {})
    agg = body.get("aggregationData", {})
    accounts = {a["id"]: a for a in agg.get("accounts", [])}
    account_media = {am["id"]: am for am in agg.get("accountMedia", [])}
    posts_by_content = {}
    for p in agg.get("posts", []):
        for att in p.get("attachments", []):
            posts_by_content[att.get("contentId")] = p

    records: list[dict] = []
    offers = body.get("mediaOfferSuggestions", [])
    last_id: int | None = None
    for idx, offer in enumerate(offers):
        moid = offer.get("mediaOfferId")
        am = account_media.get(moid, {})
        post = posts_by_content.get(moid, {})
        account = accounts.get(am.get("accountId", ""), {})
        media = am.get("media", {}) or {}

        access = bool(am.get("access", True))
        mp4 = None
        if access:
            for loc in media.get("locations", []):
                if loc.get("location"):
                    mp4 = loc["location"]
                    break

        records.append({
            "fyp_position": idx + 1,
            "mediaOfferId": moid,
            "mediaId": offer.get("mediaId"),
            "postId": post.get("id"),
            "username": account.get("username"),
            "displayName": account.get("displayName"),
            "followCount": account.get("followCount", 0),
            "mediaLikeCount": post.get("mediaLikeCount", 0),
            "postLikeCount": post.get("likeCount", 0),
            "duration": parse_duration(media.get("metadata")),
            "mediaOfferType": offer.get("mediaOfferType"),
            "mediaType": offer.get("mediaType"),
            "mimetype": media.get("mimetype"),
            "fypFlags": post.get("fypFlags"),
            "createdAt": post.get("createdAt"),
            "tags": [t.get("tag") for t in offer.get("postTags", [])],
            "caption": post.get("content"),
            "access": access,
            "price": am.get("price", 0),
            "downloadUrl": mp4,
            "thumbnailUrl": first_thumbnail(media.get("variants", [])),
        })

    if offers:
        try:
            last_id = int(offers[-1].get("id"))
        except (TypeError, ValueError):
            last_id = None
    return records, last_id


def extract_tag_stats(resp: dict) -> dict[str, dict]:
    """tag -> {viewCount, postCount} globaux, depuis les postTags d'une page."""
    body = resp.get("response", {})
    stats: dict[str, dict] = {}
    for offer in body.get("mediaOfferSuggestions", []):
        for pt in offer.get("postTags", []):
            tag = (pt.get("tag") or "").lower().strip()
            if not tag or tag == "fyp":
                continue
            stats.setdefault(tag, {
                "viewCount": pt.get("viewCount", 0),
                "postCount": pt.get("postCount", 0),
                "freq": 0,
            })
            stats[tag]["freq"] += 1
    return stats


def rank_key(v: dict, now: float | None = None) -> tuple:
    """Cle de tri "best reels" : vitesse de likes (likes/age) plutot que les
    likes bruts, pour ne pas laisser un vieux viral ecraser un reel recent qui
    performe mieux ; fyp_position (rang reel donne par Fansly au scrape) en
    tie-break, suivi des followers."""
    now = now if now is not None else time.time()
    age_h = max((now - (v.get("createdAt") or now)) / 3600.0, 1.0)
    velocity = (v.get("mediaLikeCount") or 0) / age_h
    return (-velocity, v.get("fyp_position") or 9999, -(v.get("followCount") or 0))


def save(videos: dict[str, dict]) -> int:
    now = time.time()
    ordered = sorted(videos.values(), key=lambda v: rank_key(v, now))
    OUT.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(ordered)


def make_streams(limit: int) -> list[dict]:
    return [
        {
            "name": "fyp",
            "url": lambda a: f"{API}/media/suggestionsnew?before=0&after={a}&tagIds=&limit={limit}&offset=0",
            "after": 0,
            "empty": 0,
            "cooldown": 0.0,
        },
        {
            "name": "suggestions",
            "url": lambda a: f"{API}/media/suggestions?before=0&after={a}&limit={limit}&offset=0",
            "after": 0,
            "empty": 0,
            "cooldown": 0.0,
        },
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape FYP Fansly multi-endpoint")
    ap.add_argument("--target", type=int, default=1000, help="Nb de reels a collecter")
    ap.add_argument("--delay", type=float, default=4.0, help="Delai (s) entre 2 pages")
    ap.add_argument("--limit", type=int, default=50, help="Reels par page (10 a 50)")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    session = make_session(cfg)
    headers = build_headers(cfg)

    videos: dict[str, dict] = {}
    if OUT.exists():
        for v in json.loads(OUT.read_text(encoding="utf-8")):
            videos[v["mediaOfferId"]] = v
        print(f"[merge] {len(videos)} reels deja presents, on complete.")

    streams = make_streams(args.limit)
    seen_since_save = 0
    stall_rounds = 0

    while len(videos) < args.target:
        made_progress = False
        now = time.time()

        for st in streams:
            if len(videos) >= args.target:
                break
            if now < st["cooldown"]:
                continue

            try:
                r = session.get(st["url"](st["after"]), headers=headers, timeout=20)
                if r.status_code == 429:
                    st["empty"] += 1
                    wait = min(2.0 * (2 ** st["empty"]), 120)
                    st["cooldown"] = now + wait
                    print(f"[warn] {st['name']}: 429, cooldown {wait:.0f}s",
                          file=sys.stderr)
                    continue
                if r.status_code != 200:
                    st["empty"] += 1
                    st["cooldown"] = now + 30
                    print(f"[err] {st['name']}: HTTP {r.status_code}", file=sys.stderr)
                    continue
                resp = r.json()
            except requests.RequestException as exc:
                st["empty"] += 1
                st["cooldown"] = now + 20
                print(f"[err] {st['name']}: {exc}", file=sys.stderr)
                continue

            records, last_id = extract(resp)
            if not records:
                st["empty"] += 1
                wait = min(2.0 * (2 ** st["empty"]), 120)
                st["cooldown"] = now + wait
                print(f"[warn] {st['name']}: vide, cooldown {wait:.0f}s",
                      file=sys.stderr)
                continue

            before = len(videos)
            for v in records:
                videos.setdefault(v["mediaOfferId"], v)
            added = len(videos) - before

            if last_id is None or last_id <= st["after"]:
                st["empty"] += 1
                st["cooldown"] = now + 60
                print(f"[warn] {st['name']}: curseur stable, pause", file=sys.stderr)
                continue

            st["after"] = last_id
            st["empty"] = 0
            made_progress = True
            seen_since_save += added
            print(f"[{len(videos)}/{args.target}] +{added} via {st['name']} "
                  f"(after={st['after']})", flush=True)

            if seen_since_save >= 50:
                save(videos)
                seen_since_save = 0

            time.sleep(args.delay)

        if not made_progress:
            stall_rounds += 1
            if stall_rounds >= 3:
                print("[OK] tous les flux sont en cooldown, pause longue puis arret.")
                break
            time.sleep(15)
        else:
            stall_rounds = 0

    unlocked = save(videos)
    dl = sum(1 for v in videos.values() if v.get("downloadUrl"))
    print(f"[OK] {unlocked} reels ({dl} telechargeables) -> {OUT}")


if __name__ == "__main__":
    main()
