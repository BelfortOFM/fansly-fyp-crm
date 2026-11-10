#!/usr/bin/env python3
"""Construit videos_crm.json (metadonnees + URLs de telechargement + vignettes)
a partir du HAR, pour alimenter le CRM."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

HAR = Path("ss-prod-ew1-ns.aws.adobess.com_09-17-2026-16-19-27.har")


def load_responses(har: Path) -> list[dict]:
    jq_expr = (
        '.log.entries[]'
        ' | select(.request.url | test("contentdiscovery/media/suggestionsnew"))'
        ' | .response.content.text'
    )
    proc = subprocess.run(["jq", "-r", jq_expr, str(har)], capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        sys.exit(1)
    out = []
    for line in proc.stdout.splitlines():
        if line.strip():
            out.append(json.loads(base64.b64decode(line.strip())))
    return out


def first_thumbnail(variants: list[dict]) -> str | None:
    for v in variants:
        if (v.get("mimetype") or "").startswith("image/"):
            for loc in v.get("locations", []):
                if loc.get("location"):
                    return loc["location"]
    return None


def main() -> None:
    responses = load_responses(HAR)
    videos: dict[str, dict] = {}

    for resp in responses:
        body = resp.get("response", {})
        agg = body.get("aggregationData", {})
        accounts = {a["id"]: a for a in agg.get("accounts", [])}
        account_media = {am["id"]: am for am in agg.get("accountMedia", [])}
        posts_by_content = {}
        for p in agg.get("posts", []):
            for att in p.get("attachments", []):
                posts_by_content[att.get("contentId")] = p

        for idx, offer in enumerate(body.get("mediaOfferSuggestions", [])):
            moid = offer.get("mediaOfferId")
            am = account_media.get(moid, {})
            post = posts_by_content.get(moid, {})
            account = accounts.get(am.get("accountId", ""), {})
            media = am.get("media", {}) or {}

            if moid in videos:
                videos[moid]["fyp_position"] = min(videos[moid]["fyp_position"], idx + 1)
                continue

            access = bool(am.get("access", True))
            mp4 = None
            if access:
                for loc in media.get("locations", []):
                    if loc.get("location"):
                        mp4 = loc["location"]
                        break

            videos[moid] = {
                "fyp_position": idx + 1,
                "mediaOfferId": moid,
                "mediaId": offer.get("mediaId"),
                "postId": post.get("id"),
                "username": account.get("username"),
                "displayName": account.get("displayName"),
                "followCount": account.get("followCount", 0),
                "mediaLikeCount": post.get("mediaLikeCount", 0),
                "postLikeCount": post.get("likeCount", 0),
                "duration": _duration(media.get("metadata")),
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
            }

    ordered = sorted(
        videos.values(),
        key=lambda v: (-v["mediaLikeCount"], -v["followCount"]),
    )
    Path("active/videos_crm.json").write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    unlocked = [v for v in ordered if v["downloadUrl"]]
    print(f"[OK] {len(ordered)} videos, {len(unlocked)} telechargeables → active/videos_crm.json")


def _duration(metadata: str | None) -> float | None:
    if not metadata:
        return None
    try:
        return float(json.loads(metadata).get("duration", 0))
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    main()
