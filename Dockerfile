FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/data/models \
    TZ=Asia/Shanghai

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- GPU 库（默认就装，镜像只有这一个，不分 CPU / CUDA 版本）----
# ctranslate2 的 PyPI wheel 自带 CUDA 后端，是运行时 dlopen 加载 cublas / cudnn。
# 把它们放进镜像后：
#   有 NVIDIA 显卡 + 容器能看到显卡 → 自动用 GPU
#   没有显卡 / 容器看不到显卡     → 自动回退 CPU（程序自己判断，不会崩）
# 代价是镜像大 1.2GB，换来「一条命令跑两种机器」，不做版本区分。
#
# 固定路径 + LD_LIBRARY_PATH，不能用 import 取路径：
# nvidia/cublas/lib 没有 __init__.py，不是 python 包。
ENV NVIDIA_LIB_BASE=/usr/local/lib/python3.12/site-packages/nvidia
RUN pip install --no-cache-dir nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" \
    && ls $NVIDIA_LIB_BASE/cublas/lib/libcublas.so.12 \
          $NVIDIA_LIB_BASE/cudnn/lib/libcudnn.so.9

ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.12/site-packages/nvidia/cuda_nvrtc/lib

COPY app ./app
# tools/bench.py 要能直接在容器里跑，README 和交接文档里都承诺了这条命令
COPY tools ./tools

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
