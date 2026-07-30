"""
instagram_uploader.py
---------------------
Uploads a generated Short video + thumbnail as an Instagram Reel using
instagrapi with an Instagram username/ID and password.

Handles Shorts only: generates an Instagram-optimized caption with hashtags
and publishes immediately to the linked Instagram account.

Robustness notes:
  * Missing credentials raise an actionable error before any network calls.
  * Formats hashtags and respects Instagram's 2,200 character caption limit.
  * Retries transient network/API errors (5xx, connection timeouts) with
    exponential backoff.
  * Logs successful uploads to data/instagram_upload_log.json.
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

RETRYABLE_TERMS = (
    "connection",
    "timeout",
    "incomplete",
    "throttled",
    "ratelimit",
    "500",
    "502",
    "503",
    "504",
)


class InstagramUploadError(RuntimeError):
    pass


def _require_credentials():
    missing = [name for name, value in (
        ("INSTAGRAM_USERNAME / INSTA_ID", config.INSTAGRAM_USERNAME),
        ("INSTAGRAM_PASSWORD", config.INSTAGRAM_PASSWORD),
    ) if not value]
    if missing:
        raise InstagramUploadError(
            "Missing Instagram credentials: " + ", ".join(missing) +
            ". Add them as GitHub Actions secrets (Settings -> Secrets and "
            "variables -> Actions)."
        )


def _is_retryable_error(exc: Exception) -> bool:
    err_str = str(exc).lower()
    err_type = type(exc).__name__.lower()
    for term in RETRYABLE_TERMS:
        if term in err_str or term in err_type:
            return True
    return False


def _format_hashtags(tags, budget: int = 300) -> str:
    """Format a list of tag strings into Instagram hashtags (#Tag), within a character budget."""
    formatted = []
    seen = set()
    for tag in tags or []:
        raw = str(tag).strip("#").strip()
        if not raw:
            continue
        words = raw.split()
        if len(words) > 1:
            clean = "".join(w.capitalize() for w in words if w.isalnum() or w == "_")
        else:
            clean = "".join(c for c in raw if c.isalnum() or c == "_")
        if not clean:
            continue
        tag_str = "#" + clean
        key = tag_str.lower()
        if key not in seen:
            seen.add(key)
            formatted.append(tag_str)

    for default_tag in ("#Reels", "#Shorts"):
        if default_tag.lower() not in seen:
            seen.add(default_tag.lower())
            formatted.append(default_tag)

    result = []
    used = 0
    for h in formatted:
        cost = len(h) + (1 if result else 0)
        if used + cost > budget:
            break
        result.append(h)
        used += cost
    return " ".join(result)


def _build_caption(content_item: dict) -> str:
    """Build an Instagram Reel caption from title, description, and hashtags."""
    title = " ".join(str(content_item.get("title", "")).split()).strip()
    description = str(content_item.get("description", "")).strip()

    parts = [title] if title else []
    if description and description.lower() != title.lower() and "#shorts" not in description.lower():
        parts.append(description)

    hashtags = _format_hashtags(content_item.get("tags"))
    if hashtags:
        parts.append(hashtags)

    caption = "\n\n".join(p for p in parts if p)
    return caption[:2200]


def _get_client():
    _require_credentials()
    from instagrapi import Client

    cl = Client()
    errors = 0
    while True:
        try:
            success = cl.login(config.INSTAGRAM_USERNAME, config.INSTAGRAM_PASSWORD)
            if not success:
                raise InstagramUploadError(
                    "Instagram login returned False — check INSTAGRAM_USERNAME "
                    "and INSTAGRAM_PASSWORD credentials."
                )
            return cl
        except Exception as exc:
            if errors < 3 and _is_retryable_error(exc):
                errors += 1
                wait = min(60, (2 ** errors) + random.random())
                print(f"  [transient error logging into Instagram ({type(exc).__name__}), "
                      f"retrying in {wait:.1f}s ({errors}/3)]")
                time.sleep(wait)
                continue
            raise InstagramUploadError(
                f"Instagram login failed ({type(exc).__name__}): {exc}. "
                "Check INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD in GitHub Actions secrets."
            ) from exc


def _load_log():
    if os.path.exists(config.INSTAGRAM_UPLOAD_LOG_FILE):
        try:
            with open(config.INSTAGRAM_UPLOAD_LOG_FILE) as f:
                log = json.load(f)
            if isinstance(log, list):
                return log
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[instagram_upload_log.json unreadable ({exc}), starting fresh]")
    return []


def _log_upload(content_item: dict, media_code: str):
    log = _load_log()
    log.append({
        "content_id": content_item.get("id"),
        "media_code": media_code,
        "title": content_item.get("title"),
        "format": content_item.get("format"),
        "niche": content_item.get("niche"),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "url": f"https://www.instagram.com/reel/{media_code}/" if media_code else "",
    })
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp_path = config.INSTAGRAM_UPLOAD_LOG_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(log, f, indent=2)
    os.replace(tmp_path, config.INSTAGRAM_UPLOAD_LOG_FILE)  # atomic


def upload_reel(video_path: str, thumbnail_path: str, content_item: dict) -> str:
    """Upload an MP4 Short video as an Instagram Reel using instagrapi."""
    if not os.path.exists(video_path):
        raise InstagramUploadError(f"video file not found: {video_path}")

    _require_credentials()
    caption = _build_caption(content_item)

    thumb_p = Path(thumbnail_path) if (thumbnail_path and os.path.exists(thumbnail_path)) else None
    video_p = Path(video_path)

    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    print(f"Uploading Instagram Reel '{content_item.get('title', 'Untitled')}' ({size_mb:.1f}MB)...")

    cl = _get_client()

    errors = 0
    while True:
        try:
            media = cl.clip_upload(
                video_p,
                caption=caption,
                thumbnail=thumb_p,
            )
            media_code = str(getattr(media, "code", "") or getattr(media, "pk", "") or getattr(media, "id", "") or "")
            print(f"Uploaded Instagram Reel. Code/ID: {media_code}")
            return media_code
        except Exception as exc:
            if errors < 3 and _is_retryable_error(exc):
                errors += 1
                wait = min(60, (2 ** errors) + random.random())
                print(f"  [transient error from Instagram ({type(exc).__name__}), "
                      f"retrying in {wait:.1f}s ({errors}/3)]")
                time.sleep(wait)
                continue
            raise InstagramUploadError(f"Instagram Reel upload failed: {exc}") from exc


if __name__ == "__main__":
    item_path, video_path = sys.argv[1], sys.argv[2]
    thumb_path = sys.argv[3] if len(sys.argv) > 3 else None
    with open(item_path) as f:
        item = json.load(f)
    code = upload_reel(video_path, thumb_path, item)
    _log_upload(item, code)
