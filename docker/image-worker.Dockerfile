# Reference-image worker: Qwen-Image-2512 via diffusers. Independent from the 3D env (torch cu128 wheels bundle CUDA libs).
FROM python:3.11-slim-bookworm
ENV PIP_ROOT_USER_ACTION=ignore PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install torch==2.11.0 torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cu128
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install diffusers==0.40.0 transformers==5.17.0 accelerate==1.15.0 safetensors \
        "huggingface_hub[hf_xet]" pillow==12.0.0 numpy scipy sentencepiece protobuf psutil
ENV HF_HOME=/models/hf PYTHONPATH=/app RUNNER_NAME=image-worker PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
WORKDIR /app
COPY services/runner /app/runner
COPY services/image_worker /app/image_worker
CMD ["python", "-m", "runner.runner", "--port", "8701"]
