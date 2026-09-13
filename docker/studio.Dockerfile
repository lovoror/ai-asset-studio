# API + orchestrator worker + CLI + web portal. Lightweight, no CUDA.
# Stage 1: build the React portal (fully bundled, no CDNs at runtime).
FROM node:20-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# Stage 2: python service
FROM python:3.12-slim-bookworm
ENV PIP_ROOT_USER_ACTION=ignore PYTHONUNBUFFERED=1
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install fastapi "uvicorn[standard]" pydantic httpx pyyaml \
        numpy pillow trimesh==4.10.1 pygltflib pytest
WORKDIR /app
COPY services/studio /app
COPY --from=web /web/dist /app/web/dist
ENV PYTHONPATH=/app STUDIO_WEB_DIST=/app/web/dist
CMD ["python", "-m", "studio.api"]
