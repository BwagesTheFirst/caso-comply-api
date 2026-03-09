# CASO Comply -- PDF Accessibility Remediation API
# Multi-stage Docker image for deployment on Render.com (or any container host)

# ---- Builder stage: install Python dependencies ----
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libqpdf-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Runtime stage: lean image with only what we need ----
FROM python:3.12-slim AS runtime

# Runtime system dependencies (libqpdf for pikepdf, libreoffice for doc conversion)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libqpdf-dev \
    libreoffice-nogui \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN useradd -m -r appuser

WORKDIR /app

# Copy application code
COPY main.py remediation.py gemini_verify.py convert.py auth.py ./
COPY startup.sh ./

# Create working directories and ensure appuser owns them
RUN mkdir -p /app/uploads /app/output && \
    chown -R appuser:appuser /app/uploads /app/output

# Switch to non-root user
USER appuser

ENV PORT=10000
EXPOSE 10000

CMD ["bash", "/app/startup.sh"]
