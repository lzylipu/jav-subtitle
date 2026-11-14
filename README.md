# 🎬 NAS 日文字幕流水线

**简体中文** | [English](README_EN.md)

给自己的 NAS 做的一套**全自动日文影片 → 简中字幕**流水线。放进一个目录，网页上点两下，它自己一部一部把字幕做完。

- 不改动、不删除、不重命名原视频（只会在影片旁新建 `*.srt`，写入前有后缀白名单校验）
- 已有中文字幕自动跳过，不会覆盖
- 单文件处理：`片名.mkv` + `片名.zh-CN.srt`，同目录并列
- 适合**一次性几百到几千部**的长跑场景，崩了重启能续上
- **GPU 自动识别**：有可用的 NVIDIA 显卡就用 GPU 转写，没有就回退 CPU；不需要改配置，网页上能看到实际用的是哪个

> 本项目**不包含任何 API 密钥**。翻译接口地址、模型、密钥全部在网页上填，只存在你本地数据库里。

---

## ✨ v3 相比 v2 新增

| 能力 | 说明 |
|---|---|
| 🗂 **媒体库按目录浏览** | 索引后可以在网页上逐层点进去，看每个文件夹「多少部、已完成、未处理、队列中」 |
| ▶️ **选目录开跑** | 只对选中的文件夹（含子目录）入队，不必整库一起跑；可填「只跑前 N 部」先试水 |
| 🎚 **全部参数网页可调** | 并发、模型、beam、批大小、限流、预算都在 UI 改，存库即生效，不用重启容器 |
| 🚦 **主动限流保护中转站** | 固定间隔发车 + 每分钟上限 + 429 全局冷却，绝不突发打爆 key |
| 🔑 **API key 不内置** | 默认空值，网页填，存数据库；仓库/.env/镜像里都没有 |
| 🤖 **AI 接入 token** | 首次启动自动生成，可在 UI 查看/复制/重置；给内网 Agent 用 Bearer 认证控制与监控 |

---

## 🚀 部署：只改端口和路径

整个部署只需要改**两行**——端口和影片目录。其余（登录口令、数据库、模型目录、翻译接口、并发、限流）全部**首次运行自动创建**，之后在 Web UI 里配。

### 方式 A：拉现成镜像（不构建，最快）

```bash
mkdir -p /opt/nas-subtitle && cd /opt/nas-subtitle
curl -O https://raw.githubusercontent.com/lzylipu/nas-subtitle-pipeline/main/compose.image.yaml
```

打开 `compose.image.yaml`，只改这两处：

```yaml
    ports:
      - "8000:8000"                 # ① 左边换成你要用的端口
    volumes:
      - /volume1/video:/media       # ② 左边换成你的影片目录
```

```bash
docker compose -f compose.image.yaml up -d
docker compose -f compose.image.yaml logs subtitle | grep -A6 首次启动
```

日志里会打印首次启动自动生成的登录口令（形如 `用户名：admin  密码：xxxxxxxx`），同时写进 `./data/admin-password.txt`，重启不会变。

| 仓库 | 地址 |
|---|---|
| Docker Hub（主） | `lzylipu/nas-subtitle-pipeline:latest` |
| GHCR（镜像） | `ghcr.io/lzylipu/nas-subtitle-pipeline:latest` |

### 方式 B：本地构建

```bash
git clone https://github.com/lzylipu/nas-subtitle-pipeline.git
cd nas-subtitle-pipeline
# 同样只改 compose.yaml 里的端口和路径
docker compose up -d --build
docker compose logs subtitle | grep -A6 首次启动
```

### 首次启动会自动创建什么

| 自动创建 | 位置 |
|---|---|
| 登录口令 | `./data/admin-password.txt`（`WEB_PASSWORD` 环境变量优先） |
| 数据库（全部设置、任务、翻译记忆） | `./data/pipeline.db` |
| whisper 模型 | `./data/models`（首次下 460MB） |
| 音频中间文件 | `./data/audio` |

想换口令：改 `./data/admin-password.txt` 里的那一行，`docker compose restart` 即可。想彻底重来：停容器后删 `./data`。

首次启动会自动下载 whisper 模型（`small` 约 460MB），国内建议加环境变量 `HF_ENDPOINT=https://hf-mirror.com`。

---

## 🔄 自动构建

`.github/workflows/build-and-push.yml`：push 到 `main` 或打 `v*` tag 时触发。

1. **test** — Python 3.12 跑全部单元测试，不通过就不构建
2. **build** — 构建 `linux/amd64` 镜像，同时推 Docker Hub 和 GHCR
3. tag 规则：`latest`（默认分支）、`v3.0.0`（git tag）、`sha-45b9da7`（每次提交）

需要两个仓库 secret：`DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN`（Docker Hub → Account Settings → Security → New Access Token，权限选 **Read & Write**）。

---

## 📖 用法：索引 → 选目录 → 排队

### 1️⃣ 索引

点「媒体库」→「重新索引」。程序递归扫描 `MEDIA_DIR`，把每个视频记进索引表。
这一步只读元数据（`stat`），不打开视频文件，几百 GB 也很快。

### 2️⃣ 浏览

索引完目录会显示成一张表：

```
目录            视频   已完成   未处理   队列中
电影/            4       0        4        0
剧集/            6       0        6        0
```

点目录名进下一层，点「上一级」回去。计数是**递归**的——`电影/` 的 4 部包含它所有子目录里的片子。

### 3️⃣ 开跑

勾选一个或多个目录 → 点「加入队列」。也可以进入某个目录后不勾选直接点，表示处理当前目录。

- 「只处理前 N 部」填 0 = 全部，填 5 = 只把 5 部推进队列（**先拿几部试水**）
- 入队后任务按 `优先级 → 入队顺序` 一部一部往下跑，网页上能看到实时进度

> 手动点「加入队列」会**跳过**「文件已稳定 120 秒」的防抖闸门——你自己点的，说明文件已经拷完了。
> 定时扫描那条路仍然保留这个闸门，避免把还在拷贝的半截文件排队。

---

## ⚙️ 三个阶段怎么排队

```
索引 ──► 抽音频队列 ──► 转写队列 ──► 翻译队列 ──► 写 .zh-CN.srt
          (ffmpeg)      (whisper)     (OpenAI 兼容)
```

三个阶段**各自独立排队、各有自己的 worker**：

- 一条片子在做转写时，别的片子可以同时抽音频、同时翻译
- **单条片子内部严格顺序**，不会出现半截字幕
- 默认三个并发都是 `1`；跑顺了再往上加

为什么这么设计：

| 阶段 | 瓶颈 | 建议并发 |
|---|---|---|
| 抽音频 | 磁盘 IO | 1–2 |
| 转写（CPU） | **CPU**，占满所有核 | 核数 ÷ 1（4 核就 1） |
| 转写（GPU） | 显存 / 显卡算力 | 1–2，显存够再往上 |
| 翻译 | **网络等待**，CPU 几乎不动 | 受上游限流约束，不是受机器约束 |

---

## 🚦 限流：怎么保证不打爆中转站

这是 v3 的重点。有三个叠加的闸门：

1. **固定间隔发车**（`translate_min_interval`，默认 3 秒）
   任意两次翻译请求之间至少隔这么久。不是令牌桶——令牌桶允许攒满后一次性花掉（突发），固定间隔不允许。

2. **每分钟上限**（`translate_rpm`，默认 20）
   换算成间隔 `60/20 = 3` 秒，和上一条取较大值。

3. **429 全局冷却**（`translate_429_backoff`，默认 120 秒）
   一旦收到 429，**所有 worker 一起停手**（不是各自轮流去撞墙），冷却时间优先听上游的 `Retry-After`；连续 429 会翻倍，上限 30 分钟。冷却结束后自动续跑，任务不记为失败。

网页顶部有 `近1分钟请求` 和 `限流冷却` 两个标签，随时能看到限流器状态。

---

## 💰 成本控制

- **翻译记忆（TM）**：每句台词按 `模型+原文` 哈希入库，重复台词直接复用，**不再计费**。系列片的固定台词、片头片尾几乎全免费。
- **每日字数预算** `daily_char_budget`：当天送去翻译的字数超了，只停翻译阶段，转写照跑，第二天自动继续。0 = 不限。
- **失败不无限重试**：单阶段最多 3 次，退避 30/60/120 秒；密钥错误、模型不存在直接判死，不浪费次数。
- **断点续翻**：每批翻译完成立刻进 TM，重跑那批 0 成本。

---

## 🔌 AI 接入（给内网 Agent 用）

「AI 接入」页可以拿到一套现成的控制面：

- 服务首次启动随机生成 `hs_...` 形式的 token，**只存在数据库**，网页可见可复制可重置
- 请求头用 `Authorization: Bearer <token>`，浏览器仍走 Basic 登录，两套并存
- 勾选「只读」后，这个 token 只能查询，不能暂停/重试/入队

典型用法：把这个地址和 token 丢给内网的 AI，它就能自己盯着队列、看日志、发现卡住就暂停。

```bash
# 一屏快照：计数 + 运行中 + 失败 + 最近日志 + 限流状态
curl -H "Authorization: Bearer hs_xxx" http://10.10.0.5:8000/api/ai/overview

# 完整接口清单
curl -H "Authorization: Bearer hs_xxx" http://10.10.0.5:8000/api/ai/help

# 全局日志
curl -H "Authorization: Bearer hs_xxx" "http://10.10.0.5:8000/api/logs?limit=100"

# 暂停队列
curl -X POST -H "Authorization: Bearer hs_xxx" \
     -H "Content-Type: application/json" \
     -d '{"reason":"磁盘要满了"}' http://10.10.0.5:8000/api/queue/pause
```

主要接口：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/ai/overview` | 一屏状态快照 |
| GET | `/api/ai/help` | 自描述接口清单 |
| GET | `/api/stats` | 计数、转写速度、剩余时间估算 |
| GET | `/api/library?path=` | 按目录看视频数量与进度 |
| POST | `/api/enqueue` | 目录入队 `{path, limit}` |
| POST | `/api/scan` | 重新索引 |
| GET | `/api/jobs?...` | 任务分页列表 |
| GET | `/api/jobs/{id}/events` | 单任务日志 |
| POST | `/api/jobs/{id}/retry` | 重试单个 |
| POST | `/api/jobs/retry-all` → `/api/jobs/retry-failed` | 重试全部失败 |
| POST | `/api/queue/pause` / `resume` | 暂停 / 恢复 |
| GET | `/api/logs` | 全局日志 |

---

## 🖥 硬件：GPU 优先，CPU 兜底

程序每次启动都会探测一次硬件，结果在「设置 → 转写」顶部那行字里显示：

```
当前会使用：CPU ｜ 精度 int8 ｜ 线程 4 ｜ 机器 8 线程
自动识别未发现可用 CUDA 设备，使用 CPU
```

| 场景 | 结果 |
|---|---|
| 有 NVIDIA 显卡 + 容器加了 `--gpus all` | 自动用 GPU，精度 `float16` |
| 有 NVIDIA 显卡但没加 `--gpus all` | 容器看不到显卡 → 用 CPU，页面会提示要加这个参数 |
| 显存 < 4GB | 自动降到 `int8_float16`，防 OOM |
| 完全没有显卡（E3-1231v3 这类） | CPU + `int8`，线程数自动取 `min(4, 核数)` |
| 手动「强制 GPU」但没显卡 | 回退 CPU，并在那行字里写明原因 |
| **Intel 核显（N100 / N305 这类小主机）** | **用 CPU，不用核显**。理由见下 |
| **AMD 显卡** | **不支持**。要另装 OpenNMT 单独发布的 ROCm wheel，跟 CUDA 版互斥，装不进同一个镜像 |

`device` 和 `compute_type` 的默认值都是 `auto`，一般不用动。

### 为什么 N100 的核显不参与转写

faster-whisper 底下是 CTranslate2，它官方只提供三种后端：CPU、NVIDIA CUDA、AMD ROCm，**没有 Intel GPU 后端**。要让 Intel 核显跑 whisper，得把整个转写引擎换成 OpenVINO 版——那是另一套代码，镜像从 2.3GB 涨到 5GB 以上。

而收益几乎没有：参考实测里，连 Intel Arc 独显（比 N100 的 24EU 核显强得多）用 OpenVINO 也只比 faster-whisper 纯 CPU 快 1.3 倍，而且编码器还得留在 CPU 跑。N100 是 4 核，纯 CPU 跑 `small`+`int8` 已经够用，核显大概率帮不上忙。

所以：**N100 这类机器直接跑，什么都不用配**，程序会自动选 CPU + int8 + 4 线程。设置页那行字会写「检测到 Intel/AMD 显卡（renderD128），本程序只支持 NVIDIA 显卡加速，改用 CPU 硬算」，确认它没识别错就行。


### 纯 CPU 的现实（E3-1231v3 这类）

| 项 | 说明 |
|---|---|
| 模型 | `small` + `int8` 是 4 核机器的合理选择；`medium` 大概慢 2.5–3 倍 |
| 转写速度 | 参考实测：2 vCPU 上 5× 实时；4 核 8 线程预计 10–18× 实时（**需真机实测**） |
| 内存 | `small`+int8 单实例约 500MB–1GB，转写并发开 2 就要翻倍 |
| 线程数 | 默认自动（`min(4, 核数)`）；想手动压就填具体数字，0 表示自动 |
| 批推理 | 默认关。实测只快 34%，却会把 30 句并成 5 条 35 秒长字幕，**时间轴毁了** |
| 厚度 | 一部 90 分钟片：转写约 5–9 分钟，翻译取决于接口 |

### 想用显卡

镜像不用换，只有一件事要做：让容器能看到显卡。

```bash
# docker run 加一个参数
docker run -d --name nas-subtitle --gpus all ...

# docker compose 加这段
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

前置条件：宿主机的 NVIDIA 驱动 + nvidia-container-toolkit 装好。装好后启动，看「设置 → 转写」那行字是不是 `GPU：显卡型号`。

没加 `--gpus all` 也不是错误，程序会用 CPU 跑完所有任务，只是慢。真正的报错只有一种：机器上装了 nvidia-container-toolkit 但没有 N 卡时加 `--gpus all`，docker 会拒绝启动——那种机器本来就不该加。

真机上一条命令就能量出准数：

```bash
docker compose exec subtitle python tools/bench.py /media/某部片.mp4
```

---

## 🧱 技术栈

- Python 3.12 + FastAPI + Uvicorn
- SQLite（WAL）+ `BEGIN IMMEDIATE` 原子领取任务，重启不丢队列
- FFmpeg 抽音轨（16k 单声道 PCM）
- faster-whisper（CTranslate2；有 GPU 用 GPU `float16`，没有用 CPU `int8`，运行时自动判定）
- httpx 调 OpenAI 兼容接口，JSON 结构化返回，二分拆分隔离毒批次
- 单文件前端（无构建步骤），轮询刷新

## 📁 目录

```
nas-subtitle-v3/
├── app/
│   ├── config.py       # 参数定义（默认值全部保守）
│   ├── db.py           # SQLite schema / 队列原子领取 / TM / 计数
│   ├── pipeline.py     # 三阶段 worker 池
│   ├── ratelimit.py    # 固定间隔发车 + 429 全局冷却
│   ├── scanner.py      # 递归索引 + 目录树统计
│   ├── asr.py          # ffmpeg 抽音频 + faster-whisper
│   ├── translation.py  # OpenAI 兼容翻译 + 重试 + 批次拆分
│   ├── subtitles.py    # SRT 解析/渲染/原子写
│   ├── main.py         # HTTP 控制面（Basic + Bearer 双鉴权）
│   └── static/index.html
├── tests/              # 41 项单元测试
├── tools/bench.py      # 实机吞吐基准
├── compose.yaml        # 本地构建版（只含端口 + 路径）
├── compose.image.yaml  # 拉现成镜像版
└── Dockerfile
```

## ⚠️ 已知限制

1. **只认日语对白**，唱歌、背景人声、方言口音会明显掉准确率
2. 模型是 `small`，台词密度高、语速快的片子会漏句；要更准换 `medium`，代价是 2.5–3 倍时间
3. 翻译质量取决于你接的模型，本项目只保证结构合法与顺序对齐
4. 时间轴来自 whisper 的 VAD 分段，不做二次对齐；长句会偏高
5. 单机单进程设计，没有分布式；5000 部以上建议按年份拆多个实例

## 📄 许可

自用项目，随你喜欢。
