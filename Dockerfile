# Stage 1: Build stage
# We use the official uv image which includes Python and the uv binary
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app

# Enable bytecode compilation for faster startup
ENV UV_COMPILE_BYTECODE=1
# Copy from the cache instead of linking since it's a multi-stage build
ENV UV_LINK_MODE=copy

# Install dependencies first (cached layer)
# We copy only the files needed for dependency resolution first to leverage Docker layer caching
COPY pyproject.toml uv.lock ./

# Sync dependencies only (not the project itself yet):
# --frozen: enforce lock file usage
# --no-install-project: strictly install dependencies, not the project itself
# --no-dev: exclude development dependencies
RUN uv sync --frozen --no-install-project --no-dev

# Copy the source and install the project itself.
# --no-editable installs a real copy into .venv so the runtime stage only needs
# the venv (no src/ copy) and the `app` console script is baked in.
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Stage 2: Runtime stage
FROM python:3.13-slim-bookworm

RUN apt-get update && apt-get install -y \
    vim \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the virtual environment from the builder stage (the project itself is
# installed into it, so no application source needs to be copied separately).
COPY --from=builder /app/.venv /app/.venv

# Ensure the virtual environment is used
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Run the installed console script.
CMD ["app"]