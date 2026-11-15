# 交接简报 — NAS 日文字幕流水线 v3

> 面向接手的人（或下一个 AI 会话）。所有数字都来自测试机 10.10.0.8 的真跑，不是估算；
> 估算的地方会明确写「估算」。

---

## 1. 这套东西是什么

放进 NAS 一个目录，网页上索引 → 选文件夹 → 点「加入队列」，它一部接一部把
日文影片转成简体中文字幕，写在影片旁边：`片名.mkv` + `片名.zh-CN.srt`。

- 不改、不删、不重命名原视频（每次写文件前后缀白名单校验，只允许 `.srt`）
- 已有中文字幕跳过，默认不覆盖
- **只用 CPU**，不需要显卡、不带 CUDA 库，N100 这类小主机直接跑
- 面向几百到几千部长跑，崩了重启能续

## 2. 代码在哪

| 位置 | 说明 |
|---|---|
| `app/pipeline.py` | 核心：三阶段 worker 池、TM 查写、预算闸门、二分拆分 |
| `app/ratelimit.py` | 全局限流器（固定间隔发车 + 429 冷却） |
| `app/scanner.py` | 递归索引 + 目录树递归计数 |
| `app/translation.py` | 四个翻译渠道（openai / google / bing / deepl）、密钥清洗、按渠道隔离的 TM 命名空间、错误分类与二分拆分 |
| `app/asr.py` | ffmpeg 抽音频 + faster-whisper |
| `app/db.py` | SQLite schema、`BEGIN IMMEDIATE` 原子领取、TM、计数器 |
| `app/main.py` | HTTP 控制面，Cookie 会话 / Bearer / Basic 三套鉴权 + `/login` 登录页 |
| `app/static/index.html` | 单文件前端：玻璃拟态侧栏 + **三个页签（库与任务 / 日志 / 设置）**；顶部状态是**一条整体横栏**；操作反馈走右下角 toast（不会被 4 秒刷新冲掉）；刷新页面会停在上次那页、那个目录、那组筛选；设置页第一个分组是「登录账号」，性能并发改只读卡片，AI 接入并入设置页；按翻译渠道切换字段，带「测试当前渠道」按钮 |
| `tests/` | 35 项单元测试 |
| `tools/bench.py` | 实机吞吐基准 |

## 3. 队列逻辑（这是本次重写的重点）

```
索引 ──► 抽音频队列 ──► 转写队列 ──► 翻译队列 ──► 写 .zh-CN.srt
          (ffmpeg)      (whisper)    (OpenAI 兼容)
```

- 三阶段**各自独立排队、各有 worker 池**；`MAX_POOL=16` 个线程常驻，实际并发由配置决定
- 单条片子内部严格顺序，不会产出半截字幕
- 默认三个并发都是 1；网页上随时改，2 秒缓存生效，不用重启
- 重启后 `recover_stale()` 把 running 放回 pending

**为什么转写给 1、翻译可以给多个**：转写吃满 CPU，4 核开 4 个进程总吞吐几乎不变、
内存翻 4 倍；翻译是纯网络等待，加并发几乎零成本，但**受上游限流约束，不是受机器约束**。

## 4. 限流：不要打爆中转站

三道叠加闸门，全部在网页「设置 → 限流」可调：

1. `translate_min_interval`（默认 3 秒）——任意两次请求最小间隔，固定间隔发车，不允许突发
2. `translate_rpm`（默认 20）——换算成 3 秒间隔，和上一条取较大值
3. `translate_429_backoff`（默认 120 秒）——收到 429 后**所有 worker 一起停手**，
   优先听 `Retry-After`，连续 429 指数翻倍，上限 30 分钟；恢复后自动续跑，不记失败

实机验证（见 TEST-REPORT）：88 次请求，最小间隔 **3.0 秒**，注入的 429 触发 **15 秒**
全局冷却（上游要求多少就等多少），任务没失败、自动续完。

## 5. 索引 / 目录 / 入队

- `POST /api/scan` **只建索引**（`auto_enqueue` 默认 false，绝不偷偷把整库排队）
- `GET /api/library?path=` 返回该层子目录的**递归**计数：`videos / done / running / pending / failed / new`
- `POST /api/enqueue {path, force, limit}` 只把选中目录（含子目录）入队；
  `limit>0` 只推前 N 部（试水用）
- 手动入队 `force=true` 跳过「文件稳定 120 秒」防抖闸门；定时扫描仍保留该闸门

## 6. 密钥与 AI 接入（去敏）

- 仓库、`.env`、镜像里**没有任何 key**。`OPENAI_BASE_URL / MODEL / KEY` 默认空，
  网页填，落库（`/data/pipeline.db`），API 返回时 key 掩码为 `********`
- **Web 登录口令不需要用户配**：`WEB_PASSWORD` 没设时，首次启动自动生成
  （`secrets.token_urlsafe(12)`），写 `/data/admin-password.txt`（0600，原子替换），
  并在日志里打印一次；重启读同一文件，口令不变。数据目录不可写则退回内存口令并在日志告警。
  优先级：`WEB_PASSWORD` 环境变量 > 已有口令文件 > 自动生成
- 首次启动随机生成 `hs_` + 43 字符 AI token，同样只存数据库
- 三套鉴权并存：浏览器用 `/login` 表单页换 HttpOnly Cookie（30 天，签名由当前口令派生，改口令即失效），
  服务端**绝不主动发 `WWW-Authenticate`** 因此不会再弹浏览器原生对话框；老脚本的 Basic 仍然认；AI 用
  `Authorization: Bearer <api_token>`
- token 可设「只读」：GET 放行，写操作 403
- **部署面只剩端口和路径**：`compose.yaml` / `compose.image.yaml` 里没有 `environment:`
  整段，`MEDIA_ROOT` 走默认 `/media`，时区写进 Dockerfile 的 `ENV TZ`

## 6.1 转写固定走 CPU

**没有 GPU 分支，也不探测显卡**。v3.2 之前的 `app/devices.py`（CUDA 探测、`--gpus all` 提示、
精度自动降级）已整个删除，`Settings` 里的 `device` / `compute_type` 也删了。

原因（已查证 + 实测）：faster-whisper 底下是 CTranslate2，官方后端只有 CPU / NVIDIA CUDA /
AMD ROCm，**没有 Intel GPU 后端**；`get_supported_compute_types("rocm"/"hip"/"opencl")` 实测全部
返回 `unsupported device`。用户主力机型是 N100 这类只有 Intel 核显的小主机，而要让核显参与
转写必须换成 OpenVINO 版引擎（另一套代码、镜像 5GB+、参考实测连 Arc 独显也只快 1.3×），
不划算 —— 所以决定：**只做 CPU，项目保持精简**。

| 项 | 实现 |
|---|---|
| 设备 | `WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=N)`，写死在 `app/asr.py` |
| 线程 | `asr.threads_for(cpu_threads)`：设置为 0 时自动取 `min(4, 核数)` |
| 可调项 | 只剩 `cpu_threads` / `beam_size` / `asr_batch_size` / `whisper_model`，都在 Web UI |
| 换模型/线程 | 服务端调 `asr.clear_cache()`，旧实例不会继续被用 |
| `GET /api/device` | 只回 `{device: "cpu", compute_type: "int8", cpu_threads, cpu_count}`，给人看的 |
| 镜像 | 纯 CPU，**不含 cublas/cudnn**，约 1.1GB（之前含 CUDA 库时 2.3GB） |
| 约束测试 | `tests/test_asr_cpu.py` 钉死「必须 cpu + int8」和「settings 不许再出现 device 字段」 |

## 7. 实机实测（测试机：2 vCPU / 3GB / Debian 12 LXC）

10 部片子，散在 6 个目录、最深 3 层，每部 165 秒：

| 指标 | 实测 |
|---|---|
| 任务完成 | **10/10 完成，0 失败** |
| 素材总时长 | 1647.5 秒 |
| 墙钟 | **569 秒（9.5 分钟）** |
| 端到端吞吐 | **2.90 × 实时** |
| 转写吞吐 | **3.81 × 实时**（432.1 秒转完 1647.5 秒素材） |
| 翻译耗时合计 | 522.0 秒（并行 1 路） |
| 翻译请求数 | 171 次（含 1 次人为注入的 429） |
| 请求最小间隔 | **3.0 秒**（配置值，无双发） |
| 原视频 sha256 | 前后完全一致，10 个文件 1 个哈希值→**字节未改** |
| 临时文件残留 | 0 |
| 输出 | 10 个 `.zh-CN.srt` + 10 个 `.ja.srt`，时间轴逐条对应 |

## 8. 测试覆盖

- 单测 **35 项全过**（宿主机 + 镜像内各跑一遍）
- 覆盖：稳定性闸门回归、按目录入队、`limit` 分批、目录递归计数、
  限流间隔 / 429 冷却 / 指数退避 / 上限、Bearer 与 Basic 双鉴权、只读 token 拒绝写、
  拒绝写非 .srt、原子写不留临时文件、TM 命中、预算熔断
- 接口验收：scan / library / enqueue / stats / jobs 分页 / events / retry /
  priority / pause / resume / settings / token / logs / overview，全部 200 或预期码

## 9. 未验证 / 已知坑

1. **真实影片准确率未测**：样本是 TTS 合成的干净日语。有 BGM、重叠说话、口音时准确率
   会降多少不知道。需要 2–3 部真片做 B 档验收。
2. **E3-1231v3 未上机**：本机 2 vCPU 实测 3.81×实时；4C8T 预计 10–18×是**估算**。
   上机后跑 `docker compose exec subtitle python tools/bench.py /media/某片.mp4` 校准。
3. **长时稳定性**：只连跑 9.5 分钟，没跑几天。内存/SQLite 膨胀未观测。
4. **90 分钟长片**的 900–1200 条字幕分批翻译未实测（默认批 40 条）。
5. **模型下载**：`HF_HOME=/data/models`，首次要下 460MB。国内建议先配镜像源，
   否则会像本次测试一样卡在下载上（现象是任务一直 running、progress 不动）。
6. **媒体目录必须可写**（字幕写在影片旁）。曾试过 `:ro` 挂载，结果 asr 阶段
   `[Errno 30] Read-only file system` —— 已改回可写，靠后缀白名单保证不碰视频。

## 10. 镜像与自动构建（已上线）

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/lzylipu/jav-subtitle |
| Docker Hub（主） | `lzylipu/jav-subtitle:latest` |
| GHCR（镜像） | `ghcr.io/lzylipu/jav-subtitle:latest` |
| 平台 | `linux/amd64`，9 层，348.7 MB |
| 现有 tag | `latest` / `v3.0.0` / `sha-1368f55` — 三者同 digest `sha256:3e190970…` |

`.github/workflows/build-and-push.yml`：**push `main` 或打 `v*` tag 自动触发**
→ `test` job 先跑全部单测（不绿不构建）→ `build` job 构建并同时推 Docker Hub + GHCR。

Repo secrets 已写入：`DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN`（Read & Write）。

实测验证：
- run `43605883246`(@45b9da7) / `43606686027`(@1368f55) / `43607217163`(@v3.0.0) 均 **success**
- 匿名 `docker.io` 拉取：9 层全部 206/200 可读，无需任何凭据
- 改一次 README 再 push，`latest` digest 立刻变化 → **"改代码自动出新镜像"链路已确认**

## 11. 下一步建议顺序

1. 上真机二选一：
   - 拉现成镜像 `docker compose -f compose.image.yaml up -d`
   - 或本地构建 `docker compose up -d --build`
   先在 UI 里把「只跑前 N 部」设 3，跑通再放开
2. 跑 `tools/bench.py` 拿到真实的 ×实时，回填工期
3. 拿 2–3 部真片验收准确率，决定 `small` 还是 `medium`
4. 观察 24 小时，看内存与 `data/pipeline.db` 增长
5. 需要的话把并发往上调：转写上限 = 物理核数，翻译上限看上游限流
