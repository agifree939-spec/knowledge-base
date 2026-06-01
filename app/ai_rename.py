import httpx
import json
from app.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL
from app.capture import generate_title as local_generate_title


async def generate_title(content: str, content_type: str = "article", url: str = "") -> str:
    """Generate a concise title that reflects the article's main topic.
    
    Strategy:
    1. For articles with frontmatter title, use it directly
    2. For tweets with clear topic sentence, use it directly
    3. Otherwise, use AI to understand content and generate a proper title
    """
    # Quick check: if content has a good frontmatter title, use it
    import re
    frontmatter_match = re.search(r"^---\s*\n.*?title:\s*(.+?)\n.*?---", content, re.DOTALL | re.MULTILINE)
    if frontmatter_match:
        title = frontmatter_match.group(1).strip()
        if title and len(title) > 10:
            # Clean up author suffix if present
            title = re.sub(r'\s*[-–—]\s*(作者|author|by).*$', '', title, flags=re.IGNORECASE).strip()
            return title[:80] + ("..." if len(title) > 80 else "")
    
    # For tweets: try local generate first (fast)
    if content_type == "tweet":
        local_title = local_generate_title(content)
        if local_title and local_title != "Untitled" and len(local_title) > 15:
            # Check if it's a good topic sentence (not just intro fluff)
            intro_patterns = ['推荐一下', '分享一下', '很多人', '今天', '刚才', '刚刚']
            if not any(local_title.startswith(p) for p in intro_patterns):
                return local_title
    
    # Use AI to generate a proper title
    if not OPENROUTER_API_KEY:
        # Fallback to local if no API key
        return local_generate_title(content) or "Untitled"
    
    # Prepare content for AI (truncate to save tokens)
    max_chars = 2000
    if len(content) > max_chars:
        # Take first 1500 chars + last 500 chars to capture intro and conclusion
        truncated = content[:1500] + "\n...\n" + content[-500:]
    else:
        truncated = content
    
    content_type_cn = "推文" if content_type == "tweet" else "文章"
    
    prompt = f"""请根据以下{content_type_cn}内容，生成一个简洁、准确的中文标题。

要求：
1. 标题必须反映{content_type_cn}的核心主题/主旨，不是第一句话
2. 标题应该让读者一眼就知道这篇{content_type_cn}讲什么
3. 不超过25个字
4. 不要用"推荐"、"分享"、"很多人"这类开头
5. 直接输出标题，不要任何解释或引号

示例：
- 内容讲 Linux 网络调优 → "Linux 内核网络调优实战指南"
- 内容讲 AI Skills → "Agent Skills：AI 工作流标准化方案"
- 内容讲 Obsidian 配置 → "Obsidian 配置迁移与复用技巧"

{content_type_cn}内容：
{truncated}"""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    
    # Use a cheaper/faster model for title generation
    title_model = "google/gemini-2.0-flash-001"  # Fast and cheap
    
    payload = {
        "model": title_model,
        "messages": [
            {"role": "system", "content": "你是一个专业的标题生成助手。你的任务是理解内容主旨，生成能准确反映文章核心主题的简洁标题。不要取第一句话作为标题，要提炼核心主题。"},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 100,
        "temperature": 0.3,  # Lower temperature for more consistent titles
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()
            
        api_title = data["choices"][0]["message"]["content"].strip()
        # Remove quotes if present
        api_title = api_title.strip('"\'""''')
        
        if api_title and len(api_title) > 5:
            return api_title[:80]
    except Exception as e:
        print(f"AI title generation failed: {e}")
    
    # Final fallback to local
    return local_generate_title(content) or "Untitled"
