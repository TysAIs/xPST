# xPST Dockerfile
# Multi-stage build for minimal image size

# Stage 1: Build
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md LICENSE ./
COPY assets/fonts/ assets/fonts/
COPY src/ src/

# Install dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/xpst /usr/local/bin/xpst

# Create a non-root user to run xPST (least privilege)
RUN groupadd --gid 1000 xpst \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/xpst --shell /bin/bash xpst

# Create the data directory under the non-root user's home and chown it
RUN mkdir -p /home/xpst/.xpst/credentials /home/xpst/.xpst/downloads \
        /home/xpst/.xpst/logs /home/xpst/.xpst/backups \
    && chown -R xpst:xpst /home/xpst/.xpst /app

# Copy entrypoint
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# xPST resolves its config dir from $HOME/.xpst; HOME is /home/xpst for USER xpst.
ENV HOME=/home/xpst

# Volume for persistent config (owned by the non-root user)
VOLUME ["/home/xpst/.xpst"]

# Run as the non-root user
USER xpst

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD xpst status || exit 1

# Default command
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["watch"]
