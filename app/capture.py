import json
import re
import hashlib
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
import httpx
import trafilatura
from bs4 import BeautifulSoup

from app.config import IMAGES_DIR
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


def url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


async def download_image(url: str, entry_id: int, index: int, client: httpx.AsyncClient) -> str | None:
    """Download an image and return the local relative path."""
    try:
        resp = await client.get(url, follow_redirects=True, timeout=30)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("content-type", "")
        if "webp" in content_type:
            ext = ".webp"
        elif "png" in content_type:
            ext = ".png"
        elif "gif" in content_type:
            ext = ".gif"
        else:
            ext = ".jpg"
        entry_dir = IMAGES_DIR / str(entry_id)
        entry_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{index:02d}{ext}"
        filepath = entry_dir / filename
        filepath.write_bytes(resp.content)
        return f"images/{entry_id}/{filename}"
    except Exception:
        return None


async def capture_tweet(url: str) -> dict:
    """Capture a tweet using FxTwitter API (no auth needed)."""
    # Extract tweet ID
    match = None
    for pattern in TWITTER_PATTERNS:
        match = pattern.search(url)
        if match:
            break

    if not match:
        return {"error": "Could not extract tweet ID from URL"}

    tweet_id = match.group(1)
    # Extract username from URL
    username_match = re.search(r"(?:twitter\.com|x\.com)/(\w+)/status/", url)
    username = username_match.group(1) if username_match else "i"

    # Use FxTwitter API
    api_url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        try:
            resp = await client.get(api_url, headers={"User-Agent": "KnowledgeBase/1.0"})
            if resp.status_code != 200:
                return {"error": f"FxTwitter API returned {resp.status_code}"}
            data = resp.json()
        except httpx.HTTPError as e:
            return {"error": f"FxTwitter fetch failed: {e}"}
        except json.JSONDecodeError:
            return {"error": "FxTwitter returned invalid JSON"}

    # Parse response
    tweet = data.get("tweet", {})
    if not tweet:
        return {"error": "No tweet data in response"}

    author = tweet.get("author", {})
    author_name = author.get("name", "")
    author_handle = author.get("screen_name", "") or username
    date = tweet.get("created_at", "")
    full_text = tweet.get("text", "")
    lang = tweet.get("lang", "")

    # Extract media
    all_images = []
    media = tweet.get("media", {})
    if media:
        for photo in media.get("photos", []):
            img_url = photo.get("url", "")
            if img_url:
                all_images.append(img_url)
        # Also check videos (just get thumbnail)
        for video in media.get("videos", []):
            thumb = video.get("thumbnail_url", "") or video.get("url", "")
            if thumb:
                all_images.append(thumb)

    # Check for quoted tweet
    quoted = tweet.get("quote", {})
    if quoted:
        qt_text = quoted.get("text", "")
        qt_author = quoted.get("author", {}).get("screen_name", "")
        if qt_text:
            full_text += f"\n\n--- Quoted @{qt_author} ---\n{qt_text}"
        qt_media = quoted.get("media", {})
        if qt_media:
            for photo in qt_media.get("photos", []):
                img_url = photo.get("url", "")
                if img_url:
                    all_images.append(img_url)

    # Handle X Article (long-form content)
    article = tweet.get("article", {})
    if article and not full_text.strip():
        # This is an X Article - extract content from blocks/entityMap
        article_content = extract_x_article(article)
        full_text = article_content["text"]
        all_images.extend(article_content["images"])

    title = f"@{author_handle}: {full_text[:80]}..." if full_text else f"Tweet by @{author_handle}"
    summary = full_text[:300] if full_text else ""

    return {
        "title": title,
        "summary": summary,
        "full_text": full_text,
        "author": f"@{author_handle}",
        "date": date,
        "images": all_images,
        "tags": extract_tags(full_text),
    }


def extract_x_article(article: dict) -> dict:
    """Extract content from X Article (Draft.js format)."""
    blocks = article.get("content", {}).get("blocks", [])
    entity_map = article.get("content", {}).get("entityMap", [])
    media_entities = article.get("media_entities", [])

    # Extract code blocks from entityMap
    md_by_index = {}
    for idx, e in enumerate(entity_map):
        if e.get("value", {}).get("type") == "MARKDOWN":
            md_by_index[idx] = e["value"]["data"]["markdown"]

    # Map image URLs from media_entities
    media_entity_order = [(i, e) for i, e in enumerate(entity_map) if e.get("value", {}).get("type") == "MEDIA"]
    media_url_by_entitymap_idx = {}
    for seq_idx, (emap_idx, _) in enumerate(media_entity_order):
        if seq_idx < len(media_entities):
            url = media_entities[seq_idx].get("media_info", {}).get("original_img_url", "")
            if url:
                media_url_by_entitymap_idx[emap_idx] = url

    # Build content
    output_lines = []
    all_images = []
    img_count = 0

    for block in blocks:
        btype = block.get("type", "")
        text = block.get("text", "").strip()
        er = block.get("entityRanges", [])

        if btype == "atomic" and er:
            ek = int(er[0]["key"])
            if ek in md_by_index:
                output_lines.append("")
                output_lines.append(md_by_index[ek])
                output_lines.append("")
            elif ek in media_url_by_entitymap_idx:
                img_count += 1
                img_url = media_url_by_entitymap_idx[ek]
                all_images.append(img_url)
                output_lines.append("")
                output_lines.append(f"![图片{img_count}]({img_url})")
                output_lines.append("")
            continue

        if text:
            if btype == "header-two":
                output_lines.append(f"\n## {text}")
            elif btype == "header-three":
                output_lines.append(f"\n### {text}")
            elif btype == "blockquote":
                output_lines.append(f"> {text}")
            elif btype == "ordered-list-item":
                output_lines.append(f"1. {text}")
            elif btype == "unordered-list-item":
                output_lines.append(f"- {text}")
            else:
                output_lines.append(text)

    return {
        "text": "\n".join(output_lines),
        "images": all_images,
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
            soup = BeautifulSoup(html, "lxml")
            title_tag = soup.find("title")
            title = title_tag.get_text().strip() if title_tag else url
            body = soup.find("article") or soup.find("main") or soup.find("body")
            full_text = body.get_text(separator="\n", strip=True) if body else ""
            author = ""
            date = ""
        else:
            if isinstance(result, dict):
                title = result.get("title", "")
                full_text = result.get("text", "")
                author = result.get("author", "")
                date = result.get("date", "")
            else:
                full_text = result
                soup = BeautifulSoup(html, "lxml")
                title_tag = soup.find("title")
                title = title_tag.get_text().strip() if title_tag else url
                author = ""
                date = ""

        soup = BeautifulSoup(html, "lxml")
        images = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src:
                continue
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                parsed = urlparse(base_url)
                src = f"{parsed.scheme}://{parsed.netloc}{src}"
            elif not src.startswith("http"):
                continue
            width = img.get("width", "")
            height = img.get("height", "")
            if width and height:
                try:
                    if int(width) < 100 or int(height) < 100:
                        continue
                except ValueError:
                    pass
            images.append(src)

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
            "images": unique_images[:20],
            "tags": extract_tags(full_text),
        }


def extract_tags(text: str) -> list:
    """Extract simple tags from text based on keywords."""
    if not text:
        return []
    tags = set()
    text_lower = text.lower()
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
    existing = get_entry_by_url(url)
    if existing:
        return {"status": "exists", "entry_id": existing["id"], "title": existing["title"]}

    url_type = detect_url_type(url)

    if url_type == "tweet":
        result = await capture_tweet(url)
    else:
        result = await capture_article(url)

    if "error" in result:
        entry_id = insert_entry(
            url=url, content_type=url_type, title="", summary="",
            tags=[], full_text="", status="error",
            error_message=result["error"],
        )
        return {"status": "error", "error": result["error"], "entry_id": entry_id}

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
