#!/usr/bin/env python3
"""Prepare une copie propre et complete du projet, sans identifiants ni
donnees personnelles, prete a etre envoyee a un eleve coache.

Chaque eleve se connecte ensuite avec SON PROPRE compte Fansly via l'ecran
de connexion de crm.html (voir LISEZMOI.txt) -> aucune donnee partagee entre
coaches.

Usage :
  python3 export_for_student.py
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "export_student"

# Donnees perso / identifiants / cache specifiques a CE compte -> jamais copies.
EXCLUDE_NAMES = {
    "config.json",            # token de session Fansly reel
    "videos_crm.json",        # cache de reels scrapes
    "best_tags.json",
    "best_tags.csv",
    "top_videos.json",
    "top_videos.csv",
    "fyp_suggestions_sample.json",
    "scheduled_log.json",     # journal de publication reel
    "scheduler_config.json",  # tags/niche personnalises a ce compte
    "crm_server.log",
    "export_student",         # l'export lui-meme (pas de recursion)
    "coach_package",          # ancien export, remplace par celui-ci
    "__pycache__",
}

# Sections a anonymiser dans analyse_fyp.md (compte/session de test reels -> generiques).
ACCOUNT_BLOCK = re.compile(r"## Compte observé\n.*?\n\n---\n", re.DOTALL)
ACCOUNT_BLOCK_REPLACEMENT = (
    "## Compte observé\n"
    "- Analyse faite depuis un compte de test (identifiants retires de cette copie).\n\n"
    "---\n"
)

AUTH_EXAMPLE = re.compile(
    r"ex\. décodé → `[0-9]+:1:2:[0-9a-f]+` \(sessionId, pas accountId\)"
)
AUTH_EXAMPLE_REPLACEMENT = (
    "ex. décodé → `<sessionId>:1:2:<token>` (sessionId, pas accountId)"
)


def sanitize_markdown(text: str) -> str:
    text = ACCOUNT_BLOCK.sub(ACCOUNT_BLOCK_REPLACEMENT, text, count=1)
    text = AUTH_EXAMPLE.sub(AUTH_EXAMPLE_REPLACEMENT, text, count=1)
    return text


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    copied: list[str] = []
    skipped: list[str] = []
    for item in sorted(HERE.iterdir()):
        if item.name.startswith(".") or item.name in EXCLUDE_NAMES:
            skipped.append(item.name)
            continue
        if item.is_dir():
            skipped.append(item.name + "/")
            continue
        if item.name == "analyse_fyp.md":
            text = sanitize_markdown(item.read_text(encoding="utf-8"))
            (OUT / item.name).write_text(text, encoding="utf-8")
        else:
            shutil.copy2(item, OUT / item.name)
        copied.append(item.name)

    print(f"Export pret : {OUT}")
    print(f"\n{len(copied)} fichiers copies :")
    for name in copied:
        print(f"  - {name}")
    print(f"\n{len(skipped)} exclus (donnees perso / identifiants / dossiers) :")
    for name in skipped:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
