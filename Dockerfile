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
ARG WITH_CUDA=0
RUN if [ "$WITH_CUDA" = "1" ]; then \
        pip install --no-cache-dir nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" && \
        CB=$(python -c "import os, nvidia.cublas.lib as m; print(os.path.dirname(m.__file__))") && \
        CD=$(python -c "import os, nvidia.cudnn.lib as m; print(os.path.dirname(m.__file__))") && \
        for f in "$CB"/*.so* "$CD"/*.so*; do [ -e "$f" ] && ln -sf "$f" /usr/local/lib/; done && \
        ldconfig && echo "CUDA libs installed" ; \
    else echo "CPU-only image（有显卡请用 :latest-cuda 或 build --build-arg WITH_CUDA=1）" ; fi

COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

