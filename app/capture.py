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

# Common intro fluff patterns to skip in tweets
INTRO_PATTERNS = [
    re.compile(r"^推荐一下"),
    re.compile(r"^分享一下"),
    re.compile(r"^今天给大家"),
    re.compile(r"^给大家推荐"),
    re.compile(r"^有人用"),
    re.compile(r"^你可能"),
    re.compile(r"^很多人"),
    re.compile(r"^最近"),
    re.compile(r"^刚刚"),
    re.compile(r"^终于"),
    re.compile(r"^真香"),
    re.compile(r"^🆓"),
    re.compile(r"^推荐"),
    re.compile(r"^分享"),
    re.compile(r"^介绍"),
    re.compile(r"^安利"),
]


def _is_intro_sentence(text: str) -> bool:
    """Check if text looks like an intro/fluff sentence."""
    for pattern in INTRO_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _extract_sentences(text: str) -> list:
    """Split text into sentences."""
    # Split on common sentence endings
    sentences = re.split(r'[。！？!?\n]+', text)
    return [s.strip() for s in sentences if s.strip() and len(s.strip()) > 5]


def _clean_text(text: str) -> str:
    """Clean text for title generation."""
    clean = EMOJI_RE.sub("", text)
    clean = re.sub(r"https?://\S+", "", clean)
    clean = re.sub(r"\n--- Quoted @\w+ ---.*", "", clean, flags=re.DOTALL)
    clean = re.sub(r"^>.*$", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"^#{1,6}\s*", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", clean)
    clean = re.sub(r"```[\s\S]*?```", "", clean)
    clean = re.sub(r"`([^`]+)`", r"\1", clean)
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = re.sub(r"^@\w+\s*", "", clean).strip()
    return clean


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


def generate_title(text: str, author: str = "") -> str:
    """Generate a concise title from text. Prioritizes content over intro fluff."""
    if not text:
        return f"@{author} 的推文" if author else "Untitled"

    clean = _clean_text(text)
    if not clean:
        return f"@{author} 的推文" if author else "Untitled"

    # Strategy 1: Look for frontmatter title (for articles)
    frontmatter_match = re.search(r"^---\s*\n.*?title:\s*(.+?)\n.*?---", text, re.DOTALL | re.MULTILINE)
    if frontmatter_match:
        title = frontmatter_match.group(1).strip()
        if title and len(title) > 5:
            return title[:80] + ("..." if len(title) > 80 else "")

    # Strategy 2: Extract sentences and find the best one
    sentences = _extract_sentences(clean)
    
    if not sentences:
        return clean[:60] + ("..." if len(clean) > 60 else "")

    # Topic indicators (sentences describing what something IS)
    topic_indicators = ['：', ':', '是', '叫做', '名为', '就是']

    # First pass: sentences with topic indicators (not intro)
    for sentence in sentences:
        if len(sentence) < 10 or len(sentence) > 60:
            continue
        
        if _is_intro_sentence(sentence):
            continue
        
        for indicator in topic_indicators:
            if indicator in sentence:
                return sentence

    # Second pass: sentences that are not intro
    for sentence in sentences:
        if len(sentence) < 10 or len(sentence) > 60:
            continue
        
        if not _is_intro_sentence(sentence):
            return sentence

    # Third pass: any sentence with reasonable length
    for sentence in sentences:
        if 15 <= len(sentence) <= 50:
            return sentence

    # Fallback: first sentence, truncated
    best = sentences[0]
    if len(best) > 60:
        best = best[:60]
        last_pause = max(best.rfind('，'), best.rfind('：'), best.rfind('、'))
        if last_pause > 30:
            best = best[:last_pause]
        else:
            best = best.rstrip("，、；：, ") + "..."
    return best


def detect_url_type(url: str) -> str:
    """Detect if URL is a tweet, article, or unknown."""
    for pattern in TWITTER_PATTERNS:
        if pattern.search(url):
            return "tweet"
    return "article"


async def capture_tweet(url: str) -> dict:
    """Capture a tweet using FxTwitter API."""
    # Extract tweet ID
    tweet_id = None
    for pattern in TWITTER_PATTERNS:
        m = pattern.search(url)
        if m:
            tweet_id = m.group(1)
            break
    
    if not tweet_id:
        return {"error": "Invalid tweet URL"}
    
    # Use FxTwitter API
    api_url = f"https://api.fxtwitter.com/status/{tweet_id}"
    
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        try:
            response = await client.get(api_url)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            return {"error": f"Failed to fetch tweet: {str(e)}"}
    
    tweet = data.get("tweet", {})
    if not tweet:
        return {"error": "Tweet not found"}
    
    # Extract content
    text = tweet.get("text", "")
    author = tweet.get("author", {}).get("screen_name", "")
    date = tweet.get("created_at", "")
    
    # Extract images
    all_images = []
    media = tweet.get("media", {})
    
    # media can be a dict with "all", "photos", "videos" keys
    if isinstance(media, dict):
        media_items = media.get("all", [])
    elif isinstance(media, list):
        media_items = media
    else:
        media_items = []
    
    for item in media_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "photo":
            all_images.append(item.get("url", ""))
        elif item.get("type") == "video":
            # Use thumbnail for video
            all_images.append(item.get("thumbnail_url", ""))
    
    # Handle X Article (long tweets)
    article = tweet.get("article", {})
    if article:
        # Article content is in Draft.js blocks
        content = article.get("content", {})
        blocks = content.get("blocks", [])
        entity_map = content.get("entityMap", [])
        
        # Extract text from blocks with proper formatting
        full_text_parts = []
        for block in blocks:
            btype = block.get("type", "")
            block_text = block.get("text", "")
            
            if btype == "atomic":
                # Check for media/code in entityMap
                for entity_range in block.get("entityRanges", []):
                    eidx = entity_range.get("key", -1)
                    if isinstance(eidx, int) and 0 <= eidx < len(entity_map):
                        entity = entity_map[eidx]
                        # entityMap entries have {"key": "N", "value": {...}} structure
                        entity_val = entity.get("value", entity) if isinstance(entity, dict) else {}
                        etype = entity_val.get("type", "")
                        edata = entity_val.get("data", {})
                        if etype == "IMAGE":
                            img_url = edata.get("media_url_https") or edata.get("url", "")
                            if img_url:
                                all_images.append(img_url)
                                full_text_parts.append(f"![图片]({img_url})")
                        elif etype == "MEDIA":
                            # MEDIA type — could be image or video
                            img_url = edata.get("media_url_https") or edata.get("url", "")
                            if img_url:
                                all_images.append(img_url)
                                full_text_parts.append(f"![图片]({img_url})")
                        elif etype == "MARKDOWN":
                            md = edata.get("markdown", "")
                            if md:
                                full_text_parts.append(md)
            elif btype == "header-two":
                if block_text:
                    full_text_parts.append(f"## {block_text}")
            elif btype == "header-three":
                if block_text:
                    full_text_parts.append(f"### {block_text}")
            elif btype == "unordered-list-item":
                if block_text:
                    full_text_parts.append(f"- {block_text}")
            elif btype == "ordered-list-item":
                if block_text:
                    full_text_parts.append(f"1. {block_text}")
            elif btype == "blockquote":
                if block_text:
                    full_text_parts.append(f"> {block_text}")
            elif btype == "code":
                if block_text:
                    full_text_parts.append(f"```\n{block_text}\n```")
            else:
                if block_text:
                    full_text_parts.append(block_text)
        
        full_text = "\n\n".join(full_text_parts)
        
        # Also check for cover media
        cover_media = article.get("cover_media", {})
        if cover_media:
            cover_url = cover_media.get("media_url_https") or cover_media.get("url", "")
            if cover_url and cover_url not in all_images:
                all_images.insert(0, cover_url)
    else:
        full_text = text
    
    # Verify capture completeness
    blocks = article.get("content", {}).get("blocks", []) if article else []
    entity_map = article.get("content", {}).get("entityMap", []) if article else []
    verification = verify_article_capture(blocks, entity_map, full_text, all_images)

    title = generate_title(full_text, author)
    summary = full_text[:300] if full_text else ""

    ret = {
        "title": title,
        "summary": summary,
        "full_text": full_text,
        "author": f"@{author}",
        "date": date,
        "images": all_images,
        "tags": extract_tags(full_text),
    }
    if verification:
        ret["_verification"] = verification
    return ret


def verify_article_capture(blocks: list, entity_map: list, captured_text: str, captured_images: list) -> dict:
    """Verify captured article content matches original Draft.js blocks."""
    original_segments = []
    expected_images = 0
    
    for block in blocks:
        btype = block.get("type", "")
        text = block.get("text", "").strip()
        er = block.get("entityRanges", [])
        
        if btype == "atomic":
            for entity_range in er:
                key = str(entity_range.get("key", ""))
                entity = entity_map[int(key)] if int(key) < len(entity_map) else {}
                if entity.get("type") == "IMAGE":
                    expected_images += 1
        else:
            if text and len(text) > 5:
                original_segments.append(text)
    
    if not original_segments:
        return None
    
    # Check coverage
    found = 0
    missing = []
    for segment in original_segments:
        # Check if segment (first 20 chars) appears in captured text
        if segment[:20] in captured_text:
            found += 1
        else:
            missing.append(segment[:50])
    
    coverage = found / len(original_segments) if original_segments else 0
    
    return {
        "ok": coverage >= 0.8 and len(captured_images) >= expected_images * 0.5,
        "coverage": round(coverage, 2),
        "total_segments": len(original_segments),
        "missing_blocks": missing[:5],
        "expected_images": expected_images,
        "captured_images": len(captured_images),
    }


def extract_tags(text: str) -> list:
    """Extract tags from text."""
    tags = []
    # Common tech/crypto tags
    tag_patterns = {
        "crypto": ["比特币", "BTC", "以太坊", "ETH", "加密货币", "区块链", "Web3", "DeFi", "NFT"],
        "ai": ["AI", "人工智能", "GPT", "Claude", "机器学习", "深度学习", "LLM"],
        "programming": ["Python", "JavaScript", "Rust", "Go", "编程", "代码", "GitHub"],
        "vps": ["VPS", "服务器", "部署", "Docker", "容器"],
    }
    
    text_lower = text.lower()
    for tag, keywords in tag_patterns.items():
        for keyword in keywords:
            if keyword.lower() in text_lower:
                tags.append(tag)
                break
    
    return tags[:3]  # Limit to 3 tags


async def capture_article(url: str) -> dict:
    """Capture a web article."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text
        except Exception as e:
            return {"error": f"Failed to fetch article: {str(e)}"}
    
    # Use trafilatura for content extraction
    extracted = trafilatura.extract(html, include_comments=False, include_tables=True)
    
    if not extracted:
        # Fallback to BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        
        # Try to find title
        title_tag = soup.find('title')
        title = title_tag.get_text().strip() if title_tag else ""
        
        # Try to find main content
        article = soup.find('article') or soup.find('main') or soup.find('body')
        if article:
            # Remove scripts and styles
            for tag in article.find_all(['script', 'style', 'nav', 'header', 'footer']):
                tag.decompose()
            extracted = article.get_text(separator='\n', strip=True)
        else:
            extracted = ""
    
    # Extract title from HTML
    soup = BeautifulSoup(html, 'html.parser')
    title_tag = soup.find('title')
    html_title = title_tag.get_text().strip() if title_tag else ""
    
    # Extract images
    all_images = []
    for img in soup.find_all('img'):
        src = str(img.get('src', ''))
        if src and not src.startswith('data:'):
            # Make absolute URL
            if not src.startswith('http'):
                from urllib.parse import urljoin
                src = urljoin(url, src)
            all_images.append(src)
    
    # Limit images
    all_images = all_images[:10]
    
    # Generate title
    title = html_title or generate_title(extracted)
    
    return {
        "title": title,
        "summary": extracted[:300] if extracted else "",
        "full_text": extracted or "",
        "author": "",
        "date": "",
        "images": all_images,
        "tags": extract_tags(extracted or ""),
    }


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

    # Generate better title using AI (unless title_override was provided)
    if not title_override and result.get("full_text"):
        try:
            from app.ai_rename import generate_title as ai_generate_title
            ai_title = await ai_generate_title(
                result["full_text"], 
                content_type=url_type,
                url=url
            )
            if ai_title and ai_title != "Untitled":
                result["title"] = ai_title
        except Exception as e:
            print(f"AI title generation failed, using local title: {e}")
            # Keep the local-generated title as fallback

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
