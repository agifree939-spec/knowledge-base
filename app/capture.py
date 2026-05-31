import json
import re
import hashlib
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
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


def normalize_url(url: str) -> str:
    """Normalize URL: strip tracking params for dedup."""
    parsed = urlparse(url)
    # For Twitter/X URLs, strip all query params (s=52, t=xx, etc.)
    if any(p.search(url) for p in TWITTER_PATTERNS):
        clean = parsed._replace(query="", fragment="")
        return urlunparse(clean)
    return url


# Emoji pattern (covers all emoji ranges)
EMOJI_RE = re.compile(
    "["
    "\U0001F100-\U0001F1FF"  # Enclosed Alphanumeric Supplement
    "\U0001F200-\U0001F2FF"  # Enclosed Ideographic Supplement
    "\U0001F300-\U0001F5FF"  # Miscellaneous Symbols and Pictographs
    "\U0001F600-\U0001F64F"  # Emoticons
    "\U0001F680-\U0001F6FF"  # Transport and Map
    "\U0001F700-\U0001F77F"  # Alchemical Symbols
    "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
    "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
    "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
    "\U0001FA00-\U0001FA6F"  # Chess Symbols
    "\U0001FA70-\U0001FAFF"  # Symbols Extended-A
    "\U00002702-\U000027B0"  # Dingbats
    "\U0000FE00-\U0000FE0F"  # Variation Selectors
    "\U0000200D"             # Zero Width Joiner
    "\U00002600-\U000026FF"  # Miscellaneous Symbols
    "\U00002B50-\U00002B55"  # Additional symbols
    "]+",
    flags=re.UNICODE,
)


def generate_title(text: str, author: str = "") -> str:
    """Generate a concise one-sentence title from text. No author prefix."""
    if not text:
        return f"@{author} 的推文" if author else "Untitled"

    # Clean up: remove emojis, URLs, quoted tweet markers, markdown formatting
    clean = EMOJI_RE.sub("", text)
    clean = re.sub(r"https?://\S+", "", clean)
    clean = re.sub(r"\n--- Quoted @\w+ ---.*", "", clean, flags=re.DOTALL)
    # Remove markdown quote blocks (> entire line content)
    clean = re.sub(r"^>.*$", "", clean, flags=re.MULTILINE)
    # Remove markdown headings (# ## ###)
    clean = re.sub(r"^#{1,6}\s*", "", clean, flags=re.MULTILINE)
    # Remove markdown bold/italic
    clean = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", clean)
    # Remove markdown code blocks
    clean = re.sub(r"```[\s\S]*?```", "", clean)
    clean = re.sub(r"`([^`]+)`", r"\1", clean)
    # Remove markdown links [text](url) → text
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
    # Collapse whitespace
    clean = re.sub(r"\s+", " ", clean).strip()

    # Remove leading @mentions (common in retweets/quotes)
    clean = re.sub(r"^@\w+\s*", "", clean).strip()

    if not clean:
        return f"@{author} 的推文" if author else "Untitled"

    # Take first sentence (up to 60 chars for a punchy title)
    # Try to break at sentence-ending punctuation first
    m = re.match(r"^(.{10,60}[。！？!?\.])", clean)
    if m:
        return m.group(1).strip()

    # Try to break at natural pauses (，、；:)
    m = re.match(r"^(.{10,60}[，、；：,:])", clean)
    if m:
        return m.group(1).strip()

    # Fallback: truncate at 60 chars, try to break at word boundary
    if len(clean) > 60:
        # For Chinese text, just cut at 60
        truncated = clean[:60]
        # Try to break at last space for mixed content
        last_space = truncated.rfind(" ")
        if last_space > 30:
            truncated = truncated[:last_space]
        return truncated.rstrip("，、；：, ") + "…"

    return clean


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
    verification = None
    article = tweet.get("article", {})
    if article and not full_text.strip():
        # This is an X Article - extract content from blocks/entityMap
        article_content = extract_x_article(article)
        full_text = article_content["text"]
        all_images.extend(article_content["images"])
        
        # Verify capture completeness
        blocks = article.get("content", {}).get("blocks", [])
        entity_map = article.get("content", {}).get("entityMap", [])
        verification = verify_article_capture(blocks, entity_map, full_text, all_images)

    title = generate_title(full_text, author_handle)
    summary = full_text[:300] if full_text else ""

    ret = {
        "title": title,
        "summary": summary,
        "full_text": full_text,
        "author": f"@{author_handle}",
        "date": date,
        "images": all_images,
        "tags": extract_tags(full_text),
    }
    if verification and not verification["ok"]:
        ret["_verification"] = verification
    return ret


def verify_article_capture(blocks: list, entity_map: list, captured_text: str, captured_images: list) -> dict:
    """Verify captured article content matches original Draft.js blocks.
    
    Returns dict with:
      - ok: bool
      - missing_blocks: list of text segments not found in captured_text
      - missing_images: count of expected images not captured
      - coverage: float (0-1) of text blocks found in captured content
    """
    # Extract all text segments from original blocks
    original_segments = []
    expected_images = 0
    
    for block in blocks:
        btype = block.get("type", "")
        text = block.get("text", "").strip()
        er = block.get("entityRanges", [])
        
        # Count MEDIA entities
        for r in er:
            ek = int(r.get("key", -1))
            if 0 <= ek < len(entity_map):
                if entity_map[ek].get("value", {}).get("type") == "MEDIA":
                    expected_images += 1
        
        # Collect text content (skip atomic blocks and empty text)
        if btype != "atomic" and text and len(text) > 5:
            # Strip leading emoji/symbols for comparison
            clean = re.sub(r'^[🚨🛠💾🛑💡⚡🔧🎯📊🔍]+\s*', '', text)
            if clean:
                original_segments.append(clean)
    
    # Check each segment against captured text
    captured_clean = re.sub(r'[#*`\[\]()>-]', '', captured_text)  # strip markdown
    captured_clean = re.sub(r'\s+', ' ', captured_clean)
    
    missing = []
    for seg in original_segments:
        # Check if the first 20 chars of the segment appear in captured text
        check = seg[:20].strip()
        if check and check not in captured_clean:
            missing.append(seg[:80])
    
    coverage = 1 - (len(missing) / max(len(original_segments), 1))
    img_ok = len(captured_images) >= min(expected_images, 1)
    
    result = {
        "ok": coverage >= 0.8 and img_ok,
        "coverage": round(coverage, 2),
        "total_segments": len(original_segments),
        "missing_blocks": missing,
        "expected_images": expected_images,
        "captured_images": len(captured_images),
    }
    
    if not result["ok"]:
        import logging
        logging.warning(f"Article capture verification FAILED: coverage={coverage:.0%}, "
                       f"missing={len(missing)} segments, images={len(captured_images)}/{expected_images}")
        for m in missing[:3]:
            logging.warning(f"  Missing: {m}...")
    
    return result


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
    deferred_media_urls = []  # MEDIA from non-atomic blocks, inserted at end

    for block in blocks:
        btype = block.get("type", "")
        text = block.get("text", "").strip()
        er = block.get("entityRanges", [])

        # Check for MEDIA entities in this block
        media_in_block = None
        if er:
            for r in er:
                ek = int(r["key"])
                if ek in media_url_by_entitymap_idx:
                    media_in_block = media_url_by_entitymap_idx[ek]
                    break

        if btype == "atomic" and er:
            ek = int(er[0]["key"])
            if ek in md_by_index:
                output_lines.append("")
                output_lines.append(md_by_index[ek])
                output_lines.append("")
            elif media_in_block:
                # Atomic MEDIA block — insert image inline here
                img_count += 1
                all_images.append(media_in_block)
                output_lines.append("")
                output_lines.append(f"![图片{img_count}]({media_in_block})")
                output_lines.append("")
            continue

        # Non-atomic block with MEDIA — defer to end of article
        if media_in_block and media_in_block not in deferred_media_urls:
            deferred_media_urls.append(media_in_block)

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

    # Insert deferred MEDIA images at end (before cover)
    for img_url in deferred_media_urls:
        img_count += 1
        all_images.append(img_url)
        output_lines.append("")
        output_lines.append(f"![图片{img_count}]({img_url})")

    # Also add cover image if present
    cover_url = article.get("cover_media", {}).get("media_info", {}).get("original_img_url", "")
    if cover_url and cover_url not in all_images:
        all_images.insert(0, cover_url)

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


async def process_capture(url: str, title_override: str | None = None) -> dict:
    """Main entry point: detect type, capture, store. title_override replaces auto-generated title."""
    # Normalize URL to strip tracking params (e.g. ?s=52)
    url = normalize_url(url)
    existing = get_entry_by_url(url)
    if existing:
        return {"status": "exists", "entry_id": existing["id"], "title": existing["title"]}

    url_type = detect_url_type(url)

    if url_type == "tweet":
        result = await capture_tweet(url)
    else:
        result = await capture_article(url)

    # Override title if provided
    if title_override:
        result["title"] = title_override

    if "error" in result:
        entry_id = insert_entry(
            url=url, content_type=url_type, title="", summary="",
            tags=[], full_text="", status="error",
            error_message=result["error"],
        )
        return {"status": "error", "error": result["error"], "entry_id": entry_id}

    # Verify web article completeness (simple word count check)
    verification = result.get("_verification")
    if url_type == "article" and not verification:
        ft = result.get("full_text", "")
        # Web articles: check we got meaningful content (not just title/empty)
        word_count = len(ft.split())
        verification = {
            "ok": word_count >= 50,
            "coverage": 1.0 if word_count >= 50 else round(word_count / 50, 2),
            "total_segments": 0,
            "missing_blocks": [],
            "expected_images": 0,
            "captured_images": len(result.get("images", [])),
            "word_count": word_count,
        }

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
        verification=verification,
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
