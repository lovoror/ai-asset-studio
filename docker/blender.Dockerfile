# Headless Blender via the official bpy wheel (Blender 4.5 LTS). CPU only.
FROM python:3.11-slim-bookworm
ENV PIP_ROOT_USER_ACTION=ignore PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 libegl1 libsm6 libxkbcommon0 libgomp1 libxext6 g++ \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install bpy==4.5.13 numpy pillow trimesh==4.10.1 pygltflib psutil fast-simplification==0.2.0 meshoptimizer==0.2.30a0
ENV PYTHONPATH=/app RUNNER_NAME=blender
WORKDIR /app
COPY services/runner /app/runner
COPY services/blender /app/blender
CMD ["python", "-m", "runner.runner", "--port", "8703"]
