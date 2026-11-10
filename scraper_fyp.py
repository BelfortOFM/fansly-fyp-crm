#!/usr/bin/env python3
"""
Scraper FYP Fansly — extrait depuis un HAR (ou en direct) :
  1. Les meilleurs tags a utiliser (viewCount vs postCount + frequence dans la FYP)
  2. Les 30 videos les mieux rankees (par mediaLikeCount), avec tags, hashtags,
     createur, followers, duree, fypFlags — pretes a servir d'exemples.

Sorties (dans active/) :
  - best_tags.csv / best_tags.json
  - top_videos.csv / top_videos.json

Usage :
  python3 scraper_fyp.py                          # parse le HAR du dossier
  python3 scraper_fyp.py --har chemin.har         # HAR specifique
  python3 scraper_fyp.py --top 50                 # changer le nombre de videos
  python3 scraper_fyp.py --live --auth "OTk...WY=" --cookie "f-s-c=..."  # mode direct API
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HAR_DEFAULT = Path("ss-prod-ew1-ns.aws.adobess.com_09-17-2026-16-19-27.har")
API = "https://apiv3.fansly.com/api/v1/contentdiscovery/media/suggestionsnew"


def jq_decode_responses(har_path: Path, url_pattern: str) -> list[dict]:
    """Extrait et decode les corps de reponse JSON (base64) d'un HAR via jq."""
    jq_expr = (
        '.log.entries[]'
        f' | select(.request.url | test("{url_pattern}"))'
        ' | .response.content.text'
    )
    proc = subprocess.run(
        ["jq", "-r", jq_expr, str(har_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"[ERR] jq a echoue : {proc.stderr.strip()}", file=sys.stderr)
        return []
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = base64.b64decode(line)
            out.append(json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] reponse illisible : {exc}", file=sys.stderr)
    return out


def parse_duration(metadata: str | None) -> float | None:
    if not metadata:
        return None
    try:
        d = json.loads(metadata).get("duration")
        return float(d) if d is not None else None
    except Exception:  # noqa: BLE001
        return None


def extract_hashtags(content: str | None) -> list[str]:
    if not content:
        return []
    return [w[1:].lower() for w in content.split() if w.startswith("#")]


def process_responses(responses: list[dict]) -> dict:
    """Agrege tags et posts depuis toutes les pages de suggestionsnew."""
    tags: Counter = Counter()          # tag -> nb d'occurrences dans la FYP
    tag_stats: dict[str, dict] = {}    # tag -> viewCount/postCount
    videos: list[dict] = []

    for resp in responses:
        body = resp.get("response", {})
        offers = body.get("mediaOfferSuggestions", [])
        agg = body.get("aggregationData", {})

        accounts = {a["id"]: a for a in agg.get("accounts", [])}
        account_media = {am["id"]: am for am in agg.get("accountMedia", [])}
        posts_by_content = {}
        for p in agg.get("posts", []):
            for att in p.get("attachments", []):
                posts_by_content[att.get("contentId")] = p

        for idx, offer in enumerate(offers):
            moid = offer.get("mediaOfferId")
            am = account_media.get(moid, {})
            post = posts_by_content.get(moid, {})
            account = accounts.get(am.get("accountId", ""), {})

            # --- tags (postTags) ---
            for pt in offer.get("postTags", []):
                tag = (pt.get("tag") or "").lower()
                if not tag:
                    continue
                tags[tag] += 1
                tag_stats.setdefault(tag, {
                    "viewCount": pt.get("viewCount", 0),
                    "postCount": pt.get("postCount", 0),
                })

            hashtags = extract_hashtags(post.get("content"))
            for h in hashtags:
                tags[h] += 1
                tag_stats.setdefault(h, {"viewCount": 0, "postCount": 0})

            media = am.get("media", {}) or {}
            videos.append({
                "fyp_position": idx + 1,
                "mediaOfferId": moid,
                "mediaId": offer.get("mediaId"),
                "postId": post.get("id"),
                "username": account.get("username"),
                "displayName": account.get("displayName"),
                "followCount": account.get("followCount", 0),
                "mediaLikeCount": post.get("mediaLikeCount", 0),
                "postLikeCount": post.get("likeCount", 0),
                "mediaOfferType": offer.get("mediaOfferType"),
                "mediaType": offer.get("mediaType"),
                "mimetype": media.get("mimetype"),
                "duration": parse_duration(media.get("metadata")),
                "fypFlags": post.get("fypFlags"),
                "createdAt": post.get("createdAt"),
                "tags": [t.get("tag") for t in offer.get("postTags", [])],
                "hashtags": hashtags,
                "caption": (post.get("content") or "")[:200],
            })

    dedup: dict[str, dict] = {}
    for v in videos:
        dedup.setdefault(v["mediaOfferId"], v)
    videos = list(dedup.values())

    videos.sort(key=lambda v: (-v["mediaLikeCount"], -v["followCount"]))
    return {"tags": tags, "tag_stats": tag_stats, "videos": videos}


def write_tags(out_dir: Path, tags: Counter, tag_stats: dict) -> list[dict]:
    rows = []
    for tag, freq in tags.items():
        st = tag_stats.get(tag, {})
        vc = st.get("viewCount", 0)
        pc = st.get("postCount", 0)
        rows.append({
            "tag": tag,
            "freq_fyp": freq,
            "viewCount": vc,
            "postCount": pc,
            "opportunity": round(vc / (pc + 1), 1) if vc else 0,
        })
    rows.sort(key=lambda r: (-r["opportunity"], -r["freq_fyp"], -r["viewCount"]))

    with open(out_dir / "best_tags.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["tag"])
        w.writeheader()
        w.writerows(rows)
    with open(out_dir / "best_tags.json", "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return rows


def write_videos(out_dir: Path, videos: list[dict], top: int) -> list[dict]:
    top_videos = videos[:top]
    with open(out_dir / "top_videos.csv", "w", newline="") as f:
        if top_videos:
            w = csv.DictWriter(f, fieldnames=list(top_videos[0].keys()))
            w.writeheader()
            for v in top_videos:
                row = dict(v)
                row["tags"] = "|".join(row["tags"])
                row["hashtags"] = "|".join(row["hashtags"])
                w.writerow(row)
    with open(out_dir / "top_videos.json", "w") as f:
        json.dump(top_videos, f, ensure_ascii=False, indent=2)
    return top_videos


def live_fetch(auth: str, cookie: str, pages: int = 10) -> list[dict]:
    """Mode direct : interroge l'API avec une session fournie (cookies du navigateur)."""
    import requests  # noqa: PLC0415

    headers = {
        "Authorization": auth,
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://fansly.com",
        "Referer": "https://fansly.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5.2 Safari/605.1.15"
        ),
    }
    responses: list[dict] = []
    after = 0
    for _ in range(pages):
        url = f"{API}?before=0&after={after}&tagIds=&limit=10&offset=0"
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            print(f"[ERR] HTTP {r.status_code} : {r.text[:200]}", file=sys.stderr)
            break
        data = r.json()
        responses.append(data)
        offers = data.get("response", {}).get("mediaOfferSuggestions", [])
        if not offers:
            break
        after = int(offers[-1].get("id", 0))
        time.sleep(0.5)
    return responses


def main() -> None:
    ap = argparse.ArgumentParser(description="Scraper FYP Fansly")
    ap.add_argument("--har", default=str(HAR_DEFAULT), help="Chemin du fichier HAR")
    ap.add_argument("--top", type=int, default=30, help="Nombre de videos a sortir")
    ap.add_argument("--out", default="active", help="Dossier de sortie")
    ap.add_argument("--live", action="store_true", help="Interroger l'API en direct")
    ap.add_argument("--auth", default="", help="Header Authorization (base64) pour --live")
    ap.add_argument("--cookie", default="", help="Header Cookie complet pour --live")
    ap.add_argument("--pages", type=int, default=10, help="Pages a charger en --live")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.live:
        if not (args.auth and args.cookie):
            print("[ERR] --live exige --auth et --cookie", file=sys.stderr)
            sys.exit(1)
        responses = live_fetch(args.auth, args.cookie, args.pages)
    else:
        har = Path(args.har)
        if not har.exists():
            print(f"[ERR] HAR introuvable : {har}", file=sys.stderr)
            sys.exit(1)
        responses = jq_decode_responses(har, "contentdiscovery/media/suggestionsnew")

    if not responses:
        print("[ERR] aucune reponse suggestionsnew trouvee", file=sys.stderr)
        sys.exit(1)

    data = process_responses(responses)
    print(f"[OK] {len(data['videos'])} videos analysees, {len(data['tags'])} tags uniques\n")

    tags = write_tags(out_dir, data["tags"], data["tag_stats"])
    videos = write_videos(out_dir, data["videos"], args.top)

    print("=== TOP TAGS (par opportunite : viewCount / postCount) ===")
    for t in tags[:20]:
        print(f"  {t['tag']:<18} vue={t['viewCount']:>9} posts={t['postCount']:>6} "
              f"freq={t['freq_fyp']:>2} opportunite={t['opportunity']}")

    print(f"\n=== TOP {len(videos)} VIDEOS (par mediaLikeCount) ===")
    for v in videos:
        dur = f"{v['duration']:.1f}s" if v["duration"] else "?"
        print(f"  #{v['fyp_position']:>2} likes={v['mediaLikeCount']:>6} "
              f"@{v['username']:<18} ({v['followCount']:>6} abo) {dur:>6} "
              f"tags={','.join(v['tags'][:4])}")

    print(f"\nFichiers ecrits dans {out_dir.resolve()}/")


if __name__ == "__main__":
    main()
