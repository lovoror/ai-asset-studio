# API + orchestrator worker + CLI. Lightweight, no CUDA.
FROM python:3.12-slim-bookworm
ENV PIP_ROOT_USER_ACTION=ignore PYTHONUNBUFFERED=1
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install fastapi "uvicorn[standard]" pydantic httpx pyyaml \
        numpy pillow trimesh==4.10.1 pygltflib pytest
WORKDIR /app
COPY services/studio /app
ENV PYTHONPATH=/app
CMD ["python", "-m", "studio.api"]
