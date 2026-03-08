# CASO Comply -- PDF Accessibility Remediation API
# Docker image for deployment on Render.com (or any container host)

FROM python:3.12-slim AS base

# System dependencies for pikepdf (libqpdf) and general build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libqpdf-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py remediation.py ./

# Create working directories for uploads and output
RUN mkdir -p /app/uploads /app/output

EXPOSE 8787

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8787"]
