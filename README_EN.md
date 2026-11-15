# 🎬 NAS Japanese Subtitle Pipeline

[简体中文](README.md) | **English**

An end-to-end pipeline that turns Japanese films/dramas on your NAS into Simplified Chinese subtitles — automatically, one file after another.

- **Never touches the originals**: only creates `*.srt` sidecar files (suffix whitelist enforced before every write)
- Skips files that already have Chinese subtitles; never overwrites by default
- Output layout: `Movie.mkv` + `Movie.zh-CN.srt` in the same folder
- Built for **hundreds to thousands** of files running for days or weeks; survives restarts
- Runs on **CPU only** — no GPU or CUDA libraries required anywhere

> This project ships with **no API keys**. Endpoint, model and key are configured in the web UI and stored only in your local SQLite database.

---

## ✨ What's new in v3

| Capability | Detail |
|---|---|
| 🗂 **Browse the library by folder** | After indexing, drill into any folder and see videos / done / not started / queued |
| ▶️ **Start per folder** | Queue only the folder you select (recursively), plus an optional "first N files only" for a trial run |
| 🎚 **Everything tunable in the UI** | Concurrency, model, beam, batch size, rate limits, budget — changed in the browser, applied without restart |
| 🚦 **Built-in rate limiting** | Fixed-interval dispatch + per-minute cap + global 429 cooldown; never bursts your provider |
| 🔑 **No bundled secrets** | Empty by default; typed in the UI; never in git, `.env` or the image |
| 🤖 **AI access token** | Auto-generated on first boot; view/copy/rotate in the UI; Bearer auth for a LAN agent |

---

## 🚀 Deployment: change only the port and the path

You edit **two lines** — the port and your video folder. Everything else (login password, database, model folder, translation endpoint, concurrency, rate limiting) is **created automatically on first run** and then configured in the Web UI.

### Option A: pull the prebuilt image (no build needed)

```bash
mkdir -p /opt/jav-subtitle && cd /opt/jav-subtitle
curl -O https://raw.githubusercontent.com/lzylipu/jav-subtitle/main/compose.image.yaml
```

Open `compose.image.yaml` and change just these two places:

```yaml
    ports:
      - "8000:8000"                 # ① left side = the port you want to expose
    volumes:
      - /volume1/video:/media       # ② left side = your video folder
```

```bash
docker compose -f compose.image.yaml up -d
docker compose -f compose.image.yaml logs subtitle | grep -A6 "首次启动"
```

The log prints the auto-generated login password on first boot; it is also written to `./data/admin-password.txt` and survives restarts.

| Registry | Image |
|---|---|
| Docker Hub (primary) | `lzylipu/jav-subtitle:latest` |
| GHCR (mirror) | `ghcr.io/lzylipu/jav-subtitle:latest` |

### Option B: build locally

```bash
git clone https://github.com/lzylipu/jav-subtitle.git
cd jav-subtitle
# still only the port and the path in compose.yaml
docker compose up -d --build
docker compose logs subtitle | grep -A6 "首次启动"
```

### What first boot creates for you

| Created automatically | Location |
|---|---|
| Login password | `./data/admin-password.txt` (`WEB_PASSWORD` env wins if set) |
| Database (settings, jobs, translation memory) | `./data/pipeline.db` |
| Whisper models | `./data/models` (~460 MB on first run) |
| Audio scratch files | `./data/audio` |

To change the password, edit that one line in `./data/admin-password.txt` and `docker compose restart`. To start over, stop the container and delete `./data`.

First boot downloads the Whisper model (`small`, ~460 MB); inside mainland China set `HF_ENDPOINT=https://hf-mirror.com`.

---

## 🔄 Automated builds

`.github/workflows/build-and-push.yml` runs on every push to `main` and on `v*` tags:

1. **test** — runs the full unit-test suite on Python 3.12; no green tests, no image
2. **build** — builds `linux/amd64`, pushes to both Docker Hub and GHCR
3. tags: `latest` (default branch), `v3.0.0` (git tag), `sha-45b9da7` (per commit)

Requires two repo secrets: `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (Docker Hub → Account Settings → Security → New Access Token, scope **Read & Write**).

---

## 📖 Usage: index → pick folder → queue

**1. Index** — open *Library* and click *重新索引*. Only `stat()` metadata is read; hundreds of GB is fast.

**2. Browse** — folders appear as a table with recursive counts:

```
Folder      Videos  Done  Not started  Queued
Movies/       4      0        4          0
Series/       6      0        6          0
```

**3. Run** — tick one or more folders → *加入队列*. Leave "first N" at 0 for all, or set e.g. 5 for a trial run.

Manual enqueue **skips** the 120-second "file is stable" gate — you clicked it, so the copy is done. Scheduled scans still honour that gate.

---

## ⚙️ Three-stage queue

```
index ──► extract queue ──► transcribe queue ──► translate queue ──► .zh-CN.srt
           (ffmpeg)          (whisper)            (OpenAI-compatible)
```

Each stage has **its own queue and worker pool**: while one file is transcribing, others can be extracting audio and translating. Within a single file the stages stay strictly sequential, so you never get half a subtitle.

| Stage | Bottleneck | Suggested concurrency |
|---|---|---|
| Extract | disk I/O | 1–2 |
| Transcribe (CPU) | **CPU** (saturates all cores) | 1 on a 4-core box |
| Translate | **network wait** (CPU idle) | bounded by provider limits |

All three default to `1`.

---

## 🚦 Rate limiting — how your key is protected

Three stacked gates:

1. **Fixed interval** (`translate_min_interval`, default 3 s) — at least this long between any two requests. Not a token bucket: buckets allow bursts, this does not.
2. **Per-minute cap** (`translate_rpm`, default 20) → `60/20 = 3 s` spacing; the larger of the two applies.
3. **Global 429 cooldown** (`translate_429_backoff`, default 120 s) — on a 429 **every worker stops together** (they don't take turns hitting the wall). Honours upstream `Retry-After`; consecutive 429s double the cooldown up to 30 minutes. Work resumes automatically; the job is not marked failed.

The header shows `近1分钟请求` and `限流冷却` so you can watch the limiter live.

---

## 💰 Cost control

- **Translation memory (TM)**: every line is hashed by `model + source text`; repeats cost nothing. Running gags, openings and endings are essentially free.
- **Daily character budget** (`daily_char_budget`): when exceeded, only translation pauses; transcription keeps running; resumes next day. `0` = unlimited.
- **Bounded retries**: 3 attempts per stage with 30/60/120 s backoff; auth errors and missing models fail fast without burning attempts.
- **Resumable translation**: each batch is written to TM immediately, so a re-run of that batch is free.

---

## 🔌 AI access (LAN agent)

The *AI 接入* page exposes a control surface:

- First boot generates an `hs_...` token, stored **only** in the database — visible, copyable and rotatable in the UI
- Send it as `Authorization: Bearer <token>`; Basic auth still works for browsers
- Mark it *read-only* to allow queries but forbid pause/retry/enqueue

```bash
curl -H "Authorization: Bearer hs_xxx" http://10.10.0.5:8000/api/ai/overview
curl -H "Authorization: Bearer hs_xxx" http://10.10.0.5:8000/api/ai/help
curl -H "Authorization: Bearer hs_xxx" "http://10.10.0.5:8000/api/logs?limit=100"
curl -X POST -H "Authorization: Bearer hs_xxx" -H "Content-Type: application/json" \
     -d '{"reason":"disk almost full"}' http://10.10.0.5:8000/api/queue/pause
```

---

## 🖥 Hardware: CPU only

Transcription always runs on the CPU at `int8`. Settings → Transcription shows the effective thread count:

```
CPU only, int8 ｜ threads 4 ｜ machine 8 threads
```

**Why no GPU support:** faster-whisper runs on CTranslate2, which ships exactly three backends — CPU, NVIDIA CUDA, AMD ROCm. There is **no Intel GPU backend**.

| Hardware | This project |
|---|---|
| Any x86 CPU (N100 / N305 / E3 etc.) | ✅ Works, on CPU |
| Intel iGPU (N100 UHD Graphics, Arc) | ❌ Unusable |
| AMD iGPU | ❌ Unusable |
| NVIDIA discrete GPU | Technically CUDA-capable, but **deliberately not wired in** — keeping the project lean |

Running whisper on an Intel iGPU means swapping the whole ASR engine for an OpenVINO build: different code, image grows from 1.1 GB to 5 GB+, and the reference benchmark shows even an Intel Arc discrete GPU only reaches ~1.3× over pure CPU with OpenVINO (the encoder still stays on the CPU). Not worth the complexity, so the image is a pure-CPU image with no CUDA libraries at all.

### Reality on CPU

| Item | Note |
|---|---|
| Model | `small` + `int8` is the sweet spot for 4 cores; `medium` is ~2.5–3× slower |
| Throughput | Measured 5× realtime on 2 vCPU; 10–18× expected on 4C/8T (**needs on-site measurement**) |
| Memory | ~0.5–1 GB per instance with `small`+int8 |
| Threads | Automatic by default (`min(4, cores)`); set an explicit number to override, `0` = auto |
| Batch inference | Off by default: 34% faster but merges 30 cues into 5 × 35-second blocks, destroying timing |

Measure on your own box:

```bash
docker compose exec subtitle python tools/bench.py /media/some-movie.mp4
```

## 🧱 Stack

Python 3.12 · FastAPI · SQLite (WAL, `BEGIN IMMEDIATE` atomic claim) · FFmpeg · faster-whisper (CTranslate2, fixed CPU + `int8`) · httpx · single-file frontend, no build step.

## ⚠️ Known limits

1. Japanese dialogue only; singing, overlapping speech and heavy accents degrade accuracy
2. `small` misses lines in dense, fast dialogue — switch to `medium` at 2.5–3× the cost
3. Translation quality depends on the model you plug in; this project guarantees structure and ordering only
4. Timings come from Whisper VAD segments, no forced alignment
5. Single-node, single-process; beyond ~5000 files, run one instance per year/collection

## 📄 License

Personal project — do what you like with it.
