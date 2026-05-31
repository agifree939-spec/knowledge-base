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
    delete_entry, get_stats,
)
from app.capture import process_capture


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
        
        # Regular paragraph
        escaped = stripped.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        html_parts.append(f'<p>{escaped}</p>')
    
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
    """Capture a URL - main entry point."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "Missing 'url' field")

    result = await process_capture(url)
    return JSONResponse(result)


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
    return {"count": len(results), "results": results}


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
