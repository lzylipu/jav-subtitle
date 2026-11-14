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

# 转写固定走 CPU + int8，镜像里不带任何 CUDA 库。
# 原因：CTranslate2 只有 CPU / NVIDIA CUDA / AMD ROCm 三种后端，Intel 核显、AMD 核显
# 都不支持；为显卡装 1.2GB 的 cublas/cudnn 只会让镜像变大、对 N100 这类机器毫无用处。
COPY app ./app
# tools/bench.py 要能直接在容器里跑，README 里承诺了这条命令
COPY tools ./tools

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
