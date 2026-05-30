import json
import subprocess
import re
import hashlib
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
import httpx
import trafilatura
from bs4 import BeautifulSoup

from app.config import (
    IMAGES_DIR, GALLERY_DL_CONFIG, TWITTER_AUTH_TOKEN, TWITTER_CT0,
    get_gallery_dl_config,
)
from app.database import (
    insert_entry, insert_image, get_entry_by_url,
)

# URL patterns
TWITTER_PATTERNS = [
    re.compile(r"(?:twitter\.com|x\.com)/\w+/status/(\d+)"),
]


def detect_url_type(url: str) -> str:
    """Detect if URL is a tweet, article, or unknown."""
    for pattern in TWITTER_PATTERNS:
        if pattern.search(url):
            return "tweet"
    return "article"


def write_gallery_dl_config():
    """Write gallery-dl config file with Twitter cookies."""
    config = get_gallery_dl_config()
    GALLERY_DL_CONFIG.write_text(json.dumps(config, indent=2))


def url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


async def download_image(url: str, entry_id: int, index: int, client: httpx.AsyncClient) -> str | None:
    """Download an image and return the local relative path."""
    try:
        resp = await client.get(url, follow_redirects=True, timeout=30)
        if resp.status_code != 200:
            return None
        # Determine extension from content-type or URL
        content_type = resp.headers.get("content-type", "")
        if "webp" in content_type:
            ext = ".webp"
        elif "png" in content_type:
            ext = ".png"
        elif "gif" in content_type:
            ext = ".gif"
        else:
            ext = ".jpg"
        # Build filename
        entry_dir = IMAGES_DIR / str(entry_id)
        entry_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{index:02d}{ext}"
        filepath = entry_dir / filename
        filepath.write_bytes(resp.content)
        return f"images/{entry_id}/{filename}"
    except Exception:
        return None


async def capture_tweet(url: str) -> dict:
    """Capture a tweet using gallery-dl."""
    write_gallery_dl_config()

    # Normalize URL
    clean_url = url.split("?")[0]

    # Run gallery-dl with JSON output
    try:
        result = subprocess.run(
            ["gallery-dl", "--config", str(GALLERY_DL_CONFIG), "-j", clean_url],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        return {"error": "gallery-dl not installed"}
    except subprocess.TimeoutExpired:
        return {"error": "gallery-dl timeout"}

    if result.returncode != 0:
        stderr = result.stderr.strip()
        return {"error": f"gallery-dl failed: {stderr[:200]}"}

    # Parse JSON output - gallery-dl outputs one JSON array per tweet
    try:
        # gallery-dl -j outputs multiple JSON arrays, one per tweet in thread
        raw = result.stdout.strip()
        if not raw:
            return {"error": "gallery-dl returned empty output"}
        # Each line is a JSON array [category, metadata]
        items = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            parsed = json.loads(line)
            if isinstance(parsed, list) and len(parsed) >= 2:
                items.append(parsed[1])
            elif isinstance(parsed, dict):
                items.append(parsed)
        if not items:
            return {"error": "No tweet data found"}
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}"}

    # Combine thread tweets
    first = items[0]
    author = first.get("author", {}).get("name", "") if isinstance(first.get("author"), dict) else first.get("author", "")
    handle = first.get("author", {}).get("nick", "") if isinstance(first.get("author"), dict) else ""
    date = first.get("date", "")

    # Build full text from all tweets in thread
    texts = []
    all_images = []
    for item in items:
        tweet_text = item.get("content", "") or item.get("description", "")
        if tweet_text:
            texts.append(tweet_text)
        # Collect image URLs
        if item.get("url"):
            img_url = item["url"]
            if isinstance(img_url, str) and any(img_url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                all_images.append(img_url)
        # Also check for multiple media
        for media_key in ["media", "images"]:
            media = item.get(media_key, [])
            if isinstance(media, list):
                for m in media:
                    if isinstance(m, str):
                        all_images.append(m)
                    elif isinstance(m, dict) and m.get("url"):
                        all_images.append(m["url"])

    full_text = "\n\n---\n\n".join(texts) if texts else ""
    title = f"@{handle}: {texts[0][:80]}..." if texts else f"Tweet by @{handle}"
    summary = texts[0][:300] if texts else ""

    # Deduplicate images
    seen = set()
    unique_images = []
    for img in all_images:
        if img not in seen:
            seen.add(img)
            unique_images.append(img)

    return {
        "title": title,
        "summary": summary,
        "full_text": full_text,
        "author": f"@{handle}" if handle else author,
        "date": date,
        "images": unique_images,
        "tags": extract_tags(full_text),
    }


async def capture_article(url: str) -> dict:
    """Capture a web article using trafilatura."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            return {"error": f"Fetch failed: {e}"}

        html = resp.text
        base_url = str(resp.url)

        # Extract with trafilatura
        result = trafilatura.extract(
            html,
            include_links=True,
            include_images=True,
            include_tables=True,
            include_formatting=True,
            output_format="txt",
            with_metadata=True,
        )

        if not result:
            # Fallback: basic BeautifulSoup extraction
            soup = BeautifulSoup(html, "lxml")
            title_tag = soup.find("title")
            title = title_tag.get_text().strip() if title_tag else url
            body = soup.find("article") or soup.find("main") or soup.find("body")
            full_text = body.get_text(separator="\n", strip=True) if body else ""
            author = ""
            date = ""
        else:
            # trafilatura returns metadata dict + text
            # When with_metadata=True, it returns a dict
            if isinstance(result, dict):
                title = result.get("title", "")
                full_text = result.get("text", "")
                author = result.get("author", "")
                date = result.get("date", "")
            else:
                # Plain text
                full_text = result
                # Extract title from HTML
                soup = BeautifulSoup(html, "lxml")
                title_tag = soup.find("title")
                title = title_tag.get_text().strip() if title_tag else url
                author = ""
                date = ""

        # Extract images from HTML
        soup = BeautifulSoup(html, "lxml")
        images = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src:
                continue
            # Make absolute URL
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                parsed = urlparse(base_url)
                src = f"{parsed.scheme}://{parsed.netloc}{src}"
            elif not src.startswith("http"):
                continue
            # Skip tiny images (icons, tracking pixels)
            width = img.get("width", "")
            height = img.get("height", "")
            if width and height:
                try:
                    if int(width) < 100 or int(height) < 100:
                        continue
                except ValueError:
                    pass
            images.append(src)

        # Deduplicate
        seen = set()
        unique_images = []
        for img in images:
            if img not in seen:
                seen.add(img)
                unique_images.append(img)

        title = title or url
        summary = full_text[:300] if full_text else ""

        return {
            "title": title,
            "summary": summary,
            "full_text": full_text,
            "author": author,
            "date": date,
            "images": unique_images[:20],  # Limit to 20 images
            "tags": extract_tags(full_text),
        }


def extract_tags(text: str) -> list:
    """Extract simple tags from text based on keywords."""
    if not text:
        return []
    tags = set()
    text_lower = text.lower()
    # Crypto tags
    crypto_keywords = {
        "bitcoin": "bitcoin", "btc": "bitcoin",
        "ethereum": "ethereum", "eth": "ethereum",
        "solana": "solana", "sol": "solana",
        "defi": "defi", "nft": "nft", "web3": "web3",
        "crypto": "crypto", "blockchain": "blockchain",
        "token": "crypto", "dao": "dao",
    }
    for keyword, tag in crypto_keywords.items():
        if keyword in text_lower:
            tags.add(tag)
    # Tech tags
    tech_keywords = {
        "python": "python", "javascript": "javascript",
        "rust": "rust", "golang": "golang", "go ": "golang",
        "docker": "docker", "kubernetes": "kubernetes",
        "ai": "ai", "machine learning": "ai", "llm": "ai",
        "api": "api",
    }
    for keyword, tag in tech_keywords.items():
        if keyword in text_lower:
            tags.add(tag)
    return sorted(tags)[:10]


async def process_capture(url: str) -> dict:
    """Main entry point: detect type, capture, store."""
    # Check if already captured
    existing = get_entry_by_url(url)
    if existing:
        return {"status": "exists", "entry_id": existing["id"], "title": existing["title"]}

    url_type = detect_url_type(url)

    if url_type == "tweet":
        result = await capture_tweet(url)
    else:
        result = await capture_article(url)

    if "error" in result:
        # Store failed entry
        entry_id = insert_entry(
            url=url, content_type=url_type, title="", summary="",
            tags=[], full_text="", status="error",
            error_message=result["error"],
        )
        return {"status": "error", "error": result["error"], "entry_id": entry_id}

    # Insert into database
    entry_id = insert_entry(
        url=url,
        content_type=url_type,
        title=result["title"],
        summary=result["summary"],
        tags=result.get("tags", []),
        full_text=result["full_text"],
        source_author=result.get("author", ""),
        source_date=result.get("date", ""),
        status="done",
    )

    # Download images
    downloaded = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        for i, img_url in enumerate(result.get("images", [])):
            local_path = await download_image(img_url, entry_id, i + 1, client)
            if local_path:
                insert_image(entry_id, f"{i+1:02d}", local_path, img_url)
                downloaded += 1

    return {
        "status": "ok",
        "entry_id": entry_id,
        "title": result["title"],
        "summary": result["summary"][:150],
        "tags": result.get("tags", []),
        "content_type": url_type,
        "images_count": downloaded,
    }
