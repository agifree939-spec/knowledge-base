FROM python:3.13-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gallery-dl

# Copy application code
COPY app/ ./app/
COPY templates/ ./templates/
COPY init.sh .

# Create data directory
RUN mkdir -p /data/images

EXPOSE 8501

CMD ["bash", "init.sh"]
