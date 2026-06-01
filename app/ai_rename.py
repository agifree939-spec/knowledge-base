import httpx
import json
from app.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL
from app.capture import generate_title as local_generate_title


async def generate_title(content: str, content_type: str = "article") -> str:
    """Generate a concise title. Uses local function first, API as fallback."""
    # Primary: use local generate_title (fast, no API call)
    title = local_generate_title(content)
    if title and title != "Untitled":
        return title
    
    # Fallback: call AI API (for edge cases where local fails)
    if not OPENROUTER_API_KEY:
        return title or "Untitled"
    
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
        "max_tokens": 2000,
        "temperature": 0.7,
    }
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()
            
        api_title = data["choices"][0]["message"]["content"].strip()
        # Remove quotes if present
        if api_title.startswith('"') and api_title.endswith('"'):
            api_title = api_title[1:-1]
        if api_title.startswith("'") and api_title.endswith("'"):
            api_title = api_title[1:-1]
        
        return api_title if api_title else title or "Untitled"
    except Exception:
        return title or "Untitled"
