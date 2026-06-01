import re
import asyncio
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

from app.config import IMAGES_DIR, HOST, PORT
from app.database import (
    init_db, get_entry, search_entries, list_entries,
    delete_entry, get_stats, update_entry, get_entries_count,
)
from app.capture import process_capture
from app.ai_rename import generate_title


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    init_db()
    yield


app = FastAPI(
    title="Knowledge Base API",
    description="Personal knowledge base for tweets and articles",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount static files (images)
if IMAGES_DIR.exists():
    app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")

# Templates
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def markdown_to_html(text: str) -> str:
    """Convert markdown-like text to HTML."""
    lines = text.split('\n')
    html_parts = []
    in_code_block = False
    code_lines = []
    
    for line in lines:
        stripped = line.strip()
        
        # Code block toggle
        if stripped.startswith('```'):
            if in_code_block:
                # End code block
                html_parts.append(f'<pre><code>{"&#10;".join(code_lines)}</code></pre>')
                code_lines = []
                in_code_block = False
            else:
                # Start code block
                in_code_block = True
            continue
        
        if in_code_block:
            code_lines.append(line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
            continue
        
        if not stripped:
            continue
        
        # Horizontal rule
        if stripped == '---':
            html_parts.append('<hr class="thread-divider">')
            continue
        
        # Markdown image: ![alt](url)
        img_match = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)$', stripped)
        if img_match:
            alt, src = img_match.groups()
            html_parts.append(f'<img src="{src}" alt="{alt}">')
            continue
        
        # Headings
        if stripped.startswith('### '):
            html_parts.append(f'<h4>{stripped[4:]}</h4>')
            continue
        if stripped.startswith('## '):
            html_parts.append(f'<h3>{stripped[3:]}</h3>')
            continue
        if stripped.startswith('# '):
            html_parts.append(f'<h2>{stripped[2:]}</h2>')
            continue
        
        # Blockquote
        if stripped.startswith('> '):
            html_parts.append(f'<blockquote>{stripped[2:]}</blockquote>')
            continue
        
        # Regular paragraph — escape HTML then linkify URLs
        escaped = stripped.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        linked = re.sub(
            r'(https?://[^\s<>&"\u3000-\u303F\uFF00-\uFFEF]+)',
            r'<a href="\1" target="_blank" rel="noopener">\1</a>',
            escaped
        )
        html_parts.append(f'<p>{linked}</p>')
    
    # Close any unclosed code block
    if in_code_block and code_lines:
        html_parts.append(f'<pre><code>{"&#10;".join(code_lines)}</code></pre>')
    
    return '\n'.join(html_parts)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Landing page with search and capture."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/capture")
async def api_capture(request: Request):
    """Capture a URL - main entry point. Optional 'title' overrides auto-generated title."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "Missing 'url' field")

    custom_title = body.get("title", "").strip() or None
    result = await process_capture(url, title_override=custom_title)
    return JSONResponse(result)


@app.patch("/api/entries/{entry_id}")
async def api_update_entry(entry_id: int, request: Request):
    """Update entry fields (title, summary, tags, etc.)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    updated = update_entry(entry_id, **body)
    if not updated:
        raise HTTPException(404, "Entry not found or no valid fields")
    return {"status": "updated", "entry_id": entry_id}


@app.get("/api/search")
async def api_search(
    q: str = Query(..., description="Search query"),
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    """Full-text search across all entries."""
    if not q.strip():
        raise HTTPException(400, "Empty search query")

    results = search_entries(q.strip(), limit=limit, offset=offset)
    return {"query": q, "count": len(results), "results": results}


@app.get("/api/entries")
async def api_list_entries(
    content_type: str = Query(None, description="Filter: tweet or article"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List entries, optionally filtered by type."""
    results = list_entries(content_type=content_type, limit=limit, offset=offset)
    total = get_entries_count(content_type=content_type)
    return {"count": len(results), "total": total, "results": results}


@app.get("/api/entries/{entry_id}")
async def api_get_entry(entry_id: int):
    """Get full entry detail."""
    entry = get_entry(entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")
    return entry


@app.delete("/api/entries/{entry_id}")
async def api_delete_entry(entry_id: int):
    """Delete an entry."""
    deleted = delete_entry(entry_id)
    if not deleted:
        raise HTTPException(404, "Entry not found")
    return {"status": "deleted", "entry_id": entry_id}


@app.get("/api/stats")
async def api_stats():
    """Get knowledge base statistics."""
    return get_stats()


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/detail/{entry_id}", response_class=HTMLResponse)
async def detail_page(request: Request, entry_id: int):
    """Render detail page for an entry."""
    entry = get_entry(entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")
    
    # Pre-convert full_text to HTML
    entry["html_content"] = markdown_to_html(entry.get("full_text", ""))
    
    return templates.TemplateResponse("detail.html", {
        "request": request,
        "entry": entry,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)


@app.post("/api/entries/{entry_id}/ai-rename")
async def api_ai_rename(entry_id: int):
    """Generate a new title using AI based on entry content."""
    entry = get_entry(entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")
    
    # Use full_text or summary for title generation
    content = entry.get("full_text") or entry.get("summary") or entry.get("title", "")
    if not content:
        raise HTTPException(400, "No content available for title generation")
    
    try:
        new_title = await generate_title(content, entry.get("content_type", "article"))
        # Update the entry with the new title
        update_entry(entry_id, title=new_title)
        return {"status": "renamed", "entry_id": entry_id, "title": new_title}
    except Exception as e:
        raise HTTPException(500, f"AI title generation failed: {str(e)}")
