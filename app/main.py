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

    return templates.TemplateResponse("detail.html", {
        "request": request,
        "entry": entry,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
