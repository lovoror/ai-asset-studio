# Pixal3D worker: TRELLIS.2 base (torch 2.11.0+cu128, sm_120 extensions) + Pixal3D master + NATTEN.
# Base image trellis2:rtx5090 is built from C:\Users\Zorro\TRELLIS.2\Dockerfile (nvidia/cuda:12.8.1-devel-ubuntu22.04,
# conda python 3.11, cuda-nvcc 12.9, flash-attn 2.8.3, nvdiffrast 0.4.0, CuMesh, FlexGEMM, o-voxel).
FROM trellis2:rtx5090

ARG NATTEN_VERSION=0.21.0
ARG NATTEN_CUDA_ARCH=12.0
ARG NATTEN_N_WORKERS=10
ARG PIXAL3D_COMMIT=f7cf38429b0bd264f1995f0f8743a88b1c728b94
ARG MOGE_COMMIT=74fbce054ebed49800de42d0ad0e83495065719a
ARG UTILS3D_MOGE_COMMIT=62f09d58509485564e24d5d9f6aac9ee9ebc0c37
ARG PIPELINE_COMMIT=1c511390d90226c00c101f34b84df26a0f8789b4
ENV PIP_ROOT_USER_ACTION=ignore PIP_NO_CACHE_DIR=0

# 1) NATTEN with libnatten compiled for the RTX 5090 (compute capability 12.0). Separate layer: long compile.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install cmake==4.1.0 && \
    NATTEN_CUDA_ARCH=${NATTEN_CUDA_ARCH} NATTEN_N_WORKERS=${NATTEN_N_WORKERS} \
    python -m pip install natten==${NATTEN_VERSION} --no-build-isolation

# 2) Pixal3D python requirements (upstream requirements.txt minus gradio/MoGe, which are handled explicitly).
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install \
        pillow==12.0.0 imageio==2.37.2 imageio-ffmpeg==0.6.0 tqdm==4.67.1 easydict==1.13 \
        opencv-python-headless==4.12.0.88 trimesh==4.10.1 transformers==4.57.3 zstandard==0.25.0 \
        kornia==0.8.2 timm==1.0.22 diffusers==0.37.1 accelerate==1.13.0 plyfile==1.1.3 einops psutil && \
    python -m pip install https://github.com/LDYang694/Storages/releases/download/20260430/utils3d-0.0.2-py3-none-any.whl

# 3) MoGe-2 (camera estimation) pinned; its git deps installed explicitly so the base image's compiled FlexGEMM is kept.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install click scipy matplotlib && \
    python -m pip install --no-deps "git+https://github.com/EasternJournalist/utils3d-moge.git@${UTILS3D_MOGE_COMMIT}" \
        "git+https://github.com/EasternJournalist/pipeline.git@${PIPELINE_COMMIT}" && \
    python -m pip install --no-deps "git+https://github.com/microsoft/MoGe.git@${MOGE_COMMIT}"

# 4) Pixal3D source at the pinned master commit (NOT the `paper` branch).
RUN git clone https://github.com/TencentARC/Pixal3D.git /opt/pixal3d && \
    cd /opt/pixal3d && git checkout ${PIXAL3D_COMMIT} && rm -rf .git &&     ln -s /models/pixal3d-flex_gemm_autotune_cache.json /opt/pixal3d/autotune_cache.json

ENV PYTHONPATH=/opt/pixal3d:/app \
    HF_HOME=/models/hf TORCH_HOME=/models/torch \
    ATTN_BACKEND=flash_attn \
    OPENCV_IO_ENABLE_OPENEXR=1 \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    RUNNER_NAME=pixal3d-worker
WORKDIR /app
COPY services/runner /app/runner
COPY services/pixal3d_worker /app/pixal3d_worker
CMD ["python", "-m", "runner.runner", "--port", "8702"]
