#!/bin/bash
set -e

echo "=== Knowledge Base Init ==="

# Install Python dependencies
pip install --quiet fastapi==0.115.0 uvicorn==0.30.0 jinja2==3.1.4 \
    trafilatura==1.12.0 beautifulsoup4==4.12.3 lxml==5.3.0 httpx==0.27.0 gallery-dl

# Create data directories
mkdir -p /data/images

# Initialize database
python3 -c "
import sys
sys.path.insert(0, '/app')
from app.database import init_db
init_db()
print('Database initialized')
"

echo "=== Starting server ==="
exec uvicorn app.main:app --host 0.0.0.0 --port 8501
