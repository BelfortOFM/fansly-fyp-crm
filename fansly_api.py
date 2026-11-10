#!/usr/bin/env python3
"""Client API Fansly — upload media (S3 multipart) + creation/scheduling de posts.

Flow reconstruit depuis le HAR :
  1. POST mediav2 /api/v1/media/upload/create        -> uploadId + URLs S3 signees
  2. PUT  chaque part sur son uploadUrl               -> recuperer l'ETag
  3. POST mediav2 /api/v1/media/upload/complete       -> ETags
  4. GET  mediav2 /api/v1/media/upload/{id} (poll)    -> status 6 => mediaId
  5. POST apiv3  /api/v1/account/media                -> accountMedia.id
  6. POST apiv3  /api/v1/post                         -> postId (+ scheduledFor si planifie)
"""
from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Optional

import requests

API = "https://apiv3.fansly.com"
UPLOAD = "https://mediav2.fansly.com"

# fansly-client-check : hash deterministe par endpoint (extrait du HAR, stable).
CHECKS: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/account/media"): "13c2c60f97baa7",
    ("POST", "/api/v1/post"): "175536a91eace7",
    ("POST", "/api/v1/status"): "1e87f24a1e9801",
    ("POST", "/api/v1/it/fyp"): "1a60fd13788c3a",
    ("POST", "/api/v1/media/upload/create"): "1d093072800ae3",
    ("POST", "/api/v1/media/upload/complete"): "b1210cd1d2a62",
    ("GET", "/api/v1/media/upload/"): "1e50c3c85fc870",  # prefixe
    ("GET", "/api/v1/post/scheduled"): "1408ad84d89e83",
    ("GET", "/api/v1/account/walls"): "1c6f93da7c71a6",
}

CHECK_KEY = "necvac-govry3-tybkYz"


def _s32(x: int) -> int:
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def _imul(a: int, b: int) -> int:
    return _s32(a * b)


def _cyrb53(s: str, seed: int = 0) -> int:
    h1 = _s32(0xDEADBEEF ^ seed)
    h2 = _s32(0x41C6CE57 ^ seed)
    for ch in s:
        c = ord(ch)
        h1 = _imul(h1 ^ c, 2654435761)
        h2 = _imul(h2 ^ c, 1597334677)
    h1 = _imul(h1 ^ ((h1 & 0xFFFFFFFF) >> 16), 2246822507)
    h1 = _s32(h1 ^ _imul(h2 ^ ((h2 & 0xFFFFFFFF) >> 13), 3266489909))
    h2 = _imul(h2 ^ ((h2 & 0xFFFFFFFF) >> 16), 2246822507)
    h2 = _s32(h2 ^ _imul(h1 ^ ((h1 & 0xFFFFFFFF) >> 13), 3266489909))
    return 4294967296 * (2097151 & h2) + (h1 & 0xFFFFFFFF)


def client_check(path: str, device_id: str) -> str:
    return format(_cyrb53(f"{CHECK_KEY}_{path}_{device_id}"), "x")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/26.5.2 Safari/605.1.15"
)

UPLOAD_FORM_DATA = {
    "formInputs": [
        {"type": 1001, "value": "false"},
        {"type": 1002, "value": "false"},
        {"type": 1003, "value": ""},
        {"type": 1004, "value": ""},
        {"type": 1005, "value": '""'},
    ]
}


class FanslyError(RuntimeError):
    pass


class FanslyClient:
    def __init__(self, config: dict[str, Any]):
        self.authorization = config["authorization"]
        self.cookie = config.get("cookie", "")
        self.client_id = config.get("client_id", "")
        self.session_id = config.get("session_id", "")
        self.checks = dict(CHECKS)
        self.checks.update(config.get("client_checks", {}))
        self.session = requests.Session()

    # ---- helpers ---------------------------------------------------------
    def _path(self, url: str) -> str:
        return url.split(".com", 1)[1].split("?", 1)[0]

    def _check(self, method: str, url: str) -> str:
        path = self._path(url)
        if (method, path) in self.checks:
            return self.checks[(method, path)]
        if self.client_id:
            return client_check(path, self.client_id)
        return ""

    def _headers(self, method: str, url: str) -> dict[str, str]:
        h = {
            "Authorization": self.authorization,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://fansly.com",
            "Referer": "https://fansly.com/",
            "User-Agent": UA,
        }
        if self.client_id:
            h["fansly-client-id"] = self.client_id
        if self.session_id:
            h["fansly-session-id"] = self.session_id
        check = self._check(method, url)
        if check:
            h["fansly-client-check"] = check
        h["fansly-client-ts"] = str(int(time.time() * 1000))
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    def _json(self, method: str, url: str, payload: Any) -> dict:
        r = self.session.request(
            method, url, headers=self._headers(method, url),
            json=payload, timeout=60,
        )
        if r.status_code >= 400:
            raise FanslyError(f"{method} {self._path(url)} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    # ---- upload flow -----------------------------------------------------
    def upload_create(self, file_path: Path) -> dict:
        size = file_path.stat().st_size
        mime = mimetypes.guess_type(file_path.name)[0] or "video/mp4"
        media_type = 1 if mime.startswith("image/") else 2
        payload = {
            "fileSize": size,
            "mimeType": mime,
            "fileName": file_path.name,
            "uploadFormData": UPLOAD_FORM_DATA,
        }
        data = self._json("POST", f"{UPLOAD}/api/v1/media/upload/create", payload)
        if not data.get("success"):
            raise FanslyError(f"upload/create: {data}")
        resp = data["response"]
        resp["_media_type"] = media_type
        return resp

    def _upload_part(self, upload_url: str, chunk: bytes) -> str:
        r = self.session.put(upload_url, data=chunk, timeout=300)
        if r.status_code not in (200, 201):
            raise FanslyError(f"S3 PUT part -> HTTP {r.status_code}")
        etag = r.headers.get("ETag", "")
        if not etag:
            raise FanslyError("S3 PUT part : ETag absent de la reponse")
        return etag

    def upload_parts(self, file_path: Path, create_resp: dict) -> list[dict]:
        part_size = create_resp["partSize"]
        parts = create_resp["parts"]
        etags: list[dict] = []
        with open(file_path, "rb") as fh:
            for part in parts:
                chunk = fh.read(part_size)
                if not chunk and part["index"] != 0:
                    break
                etag = self._upload_part(part["uploadUrl"], chunk)
                etags.append({"index": part["index"], "eTag": etag})
        return etags

    def upload_complete(self, create_resp: dict, etags: list[dict]) -> dict:
        payload = {
            "id": create_resp["id"],
            "type": create_resp.get("_media_type", 2),
            "partSize": create_resp["partSize"],
            "status": 0,
            "parts": etags,
            "waitForComplete": 0,
        }
        return self._json("POST", f"{UPLOAD}/api/v1/media/upload/complete", payload)

    def upload_status(self, upload_id: str) -> dict:
        return self._json("GET", f"{UPLOAD}/api/v1/media/upload/{upload_id}", None)["response"]

    def wait_for_media(self, upload_id: str, timeout: int = 300) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.upload_status(upload_id)
            if st.get("status") == 6 and st.get("mediaId"):
                return st["mediaId"]
            time.sleep(3)
        raise FanslyError(f"media pas pret apres {timeout}s (upload {upload_id})")

    def register_media(self, media_id: str, price: int = 0, tags: list[str] | None = None) -> str:
        payload = [{
            "mediaId": media_id,
            "previewId": None,
            "permissionFlags": 0,
            "price": price,
            "whitelist": [],
            "permissions": {"permissionFlags": []},
            "tags": tags or [],
        }]
        data = self._json("POST", f"{API}/api/v1/account/media", payload)
        return data["response"][0]["id"]

    def upload_media(self, file_path: Path, price: int = 0, tags: list[str] | None = None) -> tuple[str, str]:
        """Upload complet -> (mediaId, accountMediaId)."""
        create = self.upload_create(file_path)
        etags = self.upload_parts(file_path, create)
        self.upload_complete(create, etags)
        media_id = self.wait_for_media(create["id"])
        am_id = self.register_media(media_id, price=price, tags=tags)
        return media_id, am_id

    # ---- post flow -------------------------------------------------------
    def create_post(
        self,
        content: str,
        account_media_id: str,
        scheduled_for_ms: int = 0,
        wall_ids: list[str] | None = None,
    ) -> dict:
        content_lower = (content or "").lower()
        fyp_flags = 2 if "#fyp" in content_lower else 0
        payload = {
            "content": content,
            "fypFlags": fyp_flags,
            "inReplyTo": None,
            "quotedPostId": None,
            "attachments": [{"contentId": account_media_id, "contentType": 1, "pos": 0}],
            "scheduledFor": scheduled_for_ms,
            "expiresAt": 0,
            "postReplyPermissionFlags": [],
            "pinned": 0,
            "wallIds": wall_ids or [],
            "pinWallIds": [],
        }
        data = self._json("POST", f"{API}/api/v1/post", payload)
        return data["response"]

    def list_scheduled(self) -> list[dict]:
        data = self._json("GET", f"{API}/api/v1/post/scheduled", None)
        return data.get("response", {}).get("scheduledPosts", [])

    def delete_post(self, post_id: str) -> dict:
        return self._json("POST", f"{API}/api/v1/post/{post_id}/delete", None)

    def cancel_scheduled_post(self, post_id: str) -> dict:
        return self._json("POST", f"{API}/api/v1/post/scheduled/{post_id}/cancel", None)

    # ---- lecture compte / recap ------------------------------------------
    def get_me(self) -> dict:
        data = self._json("GET", f"{API}/api/v1/account/me", None)
        return data["response"]["account"]

    def account_media_batch(self, ids: list[str]) -> dict[str, dict]:
        """{accountMediaId: accountMedia} pour un lot d'ids (media uploades)."""
        out: dict[str, dict] = {}
        ids = [i for i in dict.fromkeys(ids) if i]
        for i in range(0, len(ids), 40):
            chunk = ids[i:i + 40]
            data = self._json("GET", f"{API}/api/v1/account/media?ids={','.join(chunk)}", None)
            for m in data.get("response", []):
                out[m["id"]] = m
        return out

    def own_timeline(self, account_id: str, max_pages: int = 10) -> list[dict]:
        """Tous les posts publies (timeline) du compte, pagines par postId decroissant."""
        posts: list[dict] = []
        seen: set[str] = set()
        before = 0
        for _ in range(max_pages):
            data = self._json(
                "GET",
                f"{API}/api/v1/timelinenew/{account_id}?before={before}&after=0&wallId=&contentSearch=",
                None,
            )
            batch = data.get("response", {}).get("posts", [])
            new = [p for p in batch if p["id"] not in seen]
            if not new:
                break
            for p in new:
                seen.add(p["id"])
            posts.extend(new)
            before = min(int(p["id"]) for p in new)
        return posts
