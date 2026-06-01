import httpx
import json
from app.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL


async def generate_title(content: str, content_type: str = "article") -> str:
    """Generate a concise title using AI based on content."""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY not configured")
    
    # Truncate content to avoid token limits
    max_chars = 3000
    truncated = content[:max_chars] + "..." if len(content) > max_chars else content
    
    prompt = f"""请根据以下{content_type}内容，生成一个简洁、准确的中文标题。

要求：
1. 标题应该概括核心内容，不超过30个字
2. 保持专业性和吸引力
3. 只输出标题本身，不要任何解释

内容：
{truncated}"""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": "你是一个专业的标题生成助手，擅长提炼文章核心内容生成简洁标题。"},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 500,
        "temperature": 0.7,
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers=headers,
            json=payload
        )
        response.raise_for_status()
        data = response.json()
        
    title = data["choices"][0]["message"]["content"].strip()
    # Remove quotes if present
    if title.startswith('"') and title.endswith('"'):
        title = title[1:-1]
    if title.startswith("'") and title.endswith("'"):
        title = title[1:-1]
    
    return title
