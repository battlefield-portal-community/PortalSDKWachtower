# Stage 1: Build stage
# We use the official uv image which includes Python and the uv binary
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app

# Enable bytecode compilation for faster startup
ENV UV_COMPILE_BYTECODE=1
# Copy from the cache instead of linking since it's a multi-stage build
ENV UV_LINK_MODE=copy

# Install dependencies
# We copy only the files needed for dependency resolution first to leverage Docker layer caching
COPY pyproject.toml uv.lock ./

# Sync dependencies:
# --frozen: enforce lock file usage
# --no-install-project: strictly install dependencies, not the project itself (if it were a package)
# --no-dev: exclude development dependencies
RUN uv sync --frozen --no-install-project --no-dev

# Stage 2: Runtime stage
FROM python:3.13-slim-bookworm

WORKDIR /app

# Copy the virtual environment from the builder stage
COPY --from=builder /app/.venv /app/.venv

# Copy the application code
COPY main.py .
# If you have other source files or folders, copy them here or use COPY . . 
# COPY . .

# Ensure the virtual environment is used
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1