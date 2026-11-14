FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/data/models TZ=Asia/Shanghai
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- GPU 支持（可选）----
# ctranslate2 的 PyPI wheel 已带 CUDA 支持，是运行时 dlopen 加载 cublas/cudnn，
# 所以只要把这两个库放进镜像，同一个程序在有显卡的机器上就会自动用 GPU，
# 没有显卡的机器（或没挂 --gpus）探测到 0 个设备，自动回退 CPU。
# 装 CUDA 库会让镜像多约 1.2GB，纯 CPU 用户用默认值即可。
#
# 注意：这里必须用固定路径 + LD_LIBRARY_PATH。
# nvidia/cublas/lib 没有 __init__.py，不是 python 包，`import nvidia.cublas.lib` 会失败。
ARG WITH_CUDA=0
ENV NVIDIA_LIB_BASE=/usr/local/lib/python3.12/site-packages/nvidia
RUN if [ "$WITH_CUDA" = "1" ]; then \
        pip install --no-cache-dir nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" \
        && ls $NVIDIA_LIB_BASE/cublas/lib/libcublas.so.12 $NVIDIA_LIB_BASE/cudnn/lib/libcudnn.so.9 \
        && echo "CUDA libs installed" ; \
    else echo "CPU-only image（有显卡请用 :latest-cuda 或 build --build-arg WITH_CUDA=1）" ; fi
# 没显卡时这几个目录也存在（只是装了库），ctranslate2 探测到 0 个设备会回退 CPU
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.12/site-packages/nvidia/cuda_nvrtc/lib

COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

