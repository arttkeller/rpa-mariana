FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for OCR and PDF processing
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-por \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers and system dependencies
RUN playwright install --with-deps chromium

COPY . .

# Expose the port (default 8000, but Easypanel might use 80 or 3000)
EXPOSE 8000

# Use shell form to allow variable expansion for PORT
CMD sh -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"
