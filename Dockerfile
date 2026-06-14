FROM python:3.13-slim

# libzbar0 is required by pyzbar for barcode scanning
RUN apt-get update && apt-get install -y --no-install-recommends \
    libzbar0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install dependencies before copying source for better layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# Writable dirs for SQLite DB and uploaded images
RUN mkdir -p data/uploads

EXPOSE 7860

ENV PORT=7860
ENV PYTHONUNBUFFERED=1
# Use the venv Python directly — avoids uv re-syncing dev deps on every startup
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-m", "frontend.app"]
