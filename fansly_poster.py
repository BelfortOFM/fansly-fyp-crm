#!/usr/bin/env python3
"""Outil de publication + scheduling Fansly (FYP).

Usage :
  python3 fansly_poster.py publish video.mp4 --caption "texte #tag" [--schedule "2026-09-20 10:00"]
  python3 fansly_poster.py publish video.mp4 --caption "texte" --tags teen,blonde --schedule "2026-09-20 10:00"
  python3 fansly_poster.py batch manifest.json
  python3 fansly_poster.py list-scheduled

Config (JSON, par defaut config.json dans le meme dossier) :
  { "authorization": "...", "cookie": "...", "client_id": "...", "session_id": "..." }
  (a copier depuis les headers/cookies de ta session navigateur)

Manifest (batch) :
  [ { "file": "v1.mp4", "caption": "...", "tags": ["teen"], "schedule": "2026-09-20 10:00" } ]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from fansly_api import FanslyClient, FanslyError

HERE = Path(__file__).resolve().parent


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    required = ["authorization"]
    for k in required:
        if not cfg.get(k):
            sys.exit(f"[ERR] config {path} : champ '{k}' manquant")
    return cfg


def parse_schedule(s: str | None) -> int:
    """'YYYY-MM-DD HH:MM' (heure locale) -> epoch ms ; None/''/0 -> publier maintenant."""
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        return int(dt.timestamp() * 1000)


def apply_tags(content: str, tags: list[str]) -> str:
    extra = " ".join(f"#{t.strip().lstrip('#')}" for t in tags if t.strip())
    if not extra:
        return content
    return f"{content.rstrip()} {extra}"


def cmd_publish(client: FanslyClient, args) -> None:
    fp = Path(args.file)
    if not fp.exists():
        sys.exit(f"[ERR] fichier introuvable : {fp}")
    scheduled = parse_schedule(args.schedule)
    caption = apply_tags(args.caption, args.tags or [])

    print(f"[1/3] upload de {fp.name} ({fp.stat().st_size / 1e6:.1f} Mo) ...")
    media_id, am_id = client.upload_media(fp, tags=args.tags or [])
    print(f"      mediaId={media_id} accountMediaId={am_id}")

    print("[2/3] creation du post ...")
    resp = client.create_post(caption, am_id, scheduled_for_ms=scheduled)
    when = datetime.fromtimestamp(scheduled / 1000).strftime("%Y-%m-%d %H:%M") if scheduled else "maintenant"
    print(f"[3/3] OK postId={resp.get('postId')}  planifie={when}")
    print(f"      {caption[:80]}")


def cmd_batch(client: FanslyClient, args) -> None:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    print(f"[batch] {len(manifest)} items")
    for i, item in enumerate(manifest, 1):
        fp = Path(item["file"])
        caption = apply_tags(item.get("caption", ""), item.get("tags", []) or [])
        scheduled = parse_schedule(item.get("schedule"))
        try:
            print(f"[{i}/{len(manifest)}] {fp.name} ...")
            _, am_id = client.upload_media(fp, tags=item.get("tags", []))
            resp = client.create_post(caption, am_id, scheduled_for_ms=scheduled)
            print(f"        -> postId={resp.get('postId')} "
                  f"{'planifie=' + item['schedule'] if scheduled else 'publie'}")
        except FanslyError as exc:
            print(f"        [ERR] {exc}", file=sys.stderr)
            continue
        if args.delay:
            time.sleep(args.delay)


def cmd_list(client: FanslyClient, _args) -> None:
    rows = client.list_scheduled()
    if not rows:
        print("Aucun post planifie.")
        return
    for p in rows:
        print(f"  {p.get('id')}  @ {p.get('scheduledFor')}  {(p.get('content') or '')[:60]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Poster / scheduler sur Fansly (FYP)")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("publish")
    p.add_argument("file")
    p.add_argument("--caption", default="", required=True)
    p.add_argument("--tags", help="tags separes par des virgules (ajoutes en #hashtag)")
    p.add_argument("--schedule", help="'YYYY-MM-DD HH:MM' ou timestamp ms ; vide = publier maintenant")

    b = sub.add_parser("batch")
    b.add_argument("manifest")
    b.add_argument("--delay", type=int, default=30, help="delai (s) entre 2 posts")

    sub.add_parser("list-scheduled")

    args = ap.parse_args()
    config = load_config(Path(args.config))
    client = FanslyClient(config)

    if args.cmd == "publish":
        cmd_publish(client, args)
    elif args.cmd == "batch":
        cmd_batch(client, args)
    elif args.cmd == "list-scheduled":
        cmd_list(client, args)


if __name__ == "__main__":
    main()
