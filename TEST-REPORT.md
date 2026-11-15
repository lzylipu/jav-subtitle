# 测试报告 — NAS 日文字幕流水线 v3

测试机：`10.10.0.8`（hostname `ceshi`），Debian 12 LXC，**2 vCPU / 3GB RAM**，
Docker 20.10.24，`docker-compose` 1.29.2。

所有数据都是真跑出来的，命令与输出可复现。

---

## 一、单元测试

```
$ docker run --rm -v $PWD:/src -w /src jav-subtitle:test python -m unittest discover -s tests
...................................................................................................
Ran 154 tests in 136.2s
OK
```

容器里跑（宿主机没有 `faster_whisper`），GitHub Actions 的 `test` job 再跑一遍，均 154 项全过。

> 测试目录不打包进镜像，跑测试要挂载源码：`-v $PWD:/src -w /src`。覆盖范围：

| 类别 | 用例 |
|---|---|
| 队列 | 两次扫描才入队、已有字幕不建任务、`limit` 分批、优先级排序、**稳定性闸门重复扫描回归** |
| 目录 | 递归计数（父目录含子目录）、拒绝越界路径、按目录入队只碰该目录 |
| 限流 | 最小间隔真的生效（3 次 ≥0.38s）、rpm→间隔换算、429 触发全局冷却、连续 429 指数退避、上限 1800s |
| 鉴权 | Bearer 通过、错 token 401、只读 token 写操作 403、Basic 通过、错密码 401 |
| 安全 | 拒绝写非 `.srt`、原子写不留临时文件、默认设置无密钥、默认并发为 1 |
| 阶段 | 预算熔断、复用已有日文字幕、音频丢失回退、翻译写 SRT 且命中 TM |
| **只用 CPU（5 项）** | 模型必须建在 cpu+int8、自动线程上限 min(4,核数)、核数为 0 时至少 1 线程、显式线程被尊重、settings 里不许再出现 `device`/`compute_type` 字段 |
| **翻译渠道（30 项）** | 密钥清洗（空格/引号/`Bearer ` 前缀/`DeepL-Auth-Key `）、渠道归一化、TM 命名空间按渠道隔离、空行不发上游但保留位置、失败批自动二分但不无限拆、致命错误不重试、谷歌/必应/DeepL 请求形态、谷歌 403 自动换 key、DeepL `:fx` 选免费端点、456 额度耗尽判致命、`config_ready` 不误报免密钥渠道 |
| **日志文案（4 项，v3.5 新增）** | 三个阶段映射成「抽音频/转写/翻译」、状态映射成「完成/失败/跳过」、`finish()` 写的事件里不许出现 `done`/`asr` 这类内部值 |
| **翻译容错（7 项，v3.5 新增）** | 模型返回裸数组 / 字符串 id / 换外壳都能解析、条数不符抛 `ShapeError` 触发拆批、半截 JSON 不当成条数问题、译文为空算条数问题、一批里有一句毒字幕只留那一句原文其余照译、整批全废仍报错（不当成功）、连续失败即放弃抢救不再空打请求、请求体带「共 N 条」与 `max_tokens` |
| **账号（14 项）** | 改账号要验当前口令、改完当场换票、旧 cookie 失效、`WEB_PASSWORD` 锁定时拒绝并说明原因、长度下限、用户名不能带空格、老 `admin-password.txt` 自动迁移 |
| **登录页（13 项）** | 登录页匿名可访问、首页 303、API 401 且**无 `WWW-Authenticate`**、对口令种 Cookie、错口令 401、退出清 Cookie、篡改/过期 Cookie 拒绝、改口令后旧 Cookie 立即失效 |
| **备用渠道降级（19 项，v3.7 新增）** | 主渠道 403/额度光/没配置时自动切备用、切过去粘住不再撞死渠道、只播报一次、429 与条数不符不切渠道、链上全挂才报错且带上每个渠道的原因、`parse_fallback` 去重去空格去未知名、主渠道不会重复出现在备用列表里 |
| **DeepL 403 自查（6 项）** | `:fx` 决定走 `api-free` 还是 `api.deepl.com`、报错里带密钥形状（长度/后缀/端点）但**绝不出现密钥原文**、403 文案必须写明「与一次发多少条无关」、额度查询端点跟着渠道走 |
| **品牌与界面（10 项）** | logo/favicon 匿名可访问、`/static/` 只认白名单（`../` 打不出去）、登录页与主页面都引用新 logo、设置分组标题不再是 `<legend>`、三个保存按钮统一叫「保存」、常驻黄条 `#notice` 已删除、toast 3 秒消失 |
| **自动口令（6 项）** | 缺口令自动生成、重启复用同一口令、env 优先于文件、文件权限 0600、目录不可写退内存、生成的口令真能通过 Basic 鉴权 |

---

## 二、接口验收（测试机实调）

| 接口 | 结果 |
|---|---|
| `POST /api/scan`（默认不自动入队） | `{"discovered":10,...,"queued":0,"auto_enqueue":false}` |
| `GET /api/library` | 根层 3 个子目录，递归计数 2/4/4 = 10 ✔ |
| `POST /api/enqueue {"path":"/media","limit":0}` | `queued:10`，`counts` 回读 `in_queue:10` |
| `POST /api/enqueue {... "limit":1}` | `queued:1`，其余 3 部 status 仍是未入队 ✔ |
| `GET /api/stats` | `indexed_videos / total / pending / running / done / failed / asr_ratio / eta / chars / translate_api` 齐全 |
| 无凭证访问 | `401` |
| Bearer 读 | `200` |
| Bearer 写（暂停/恢复） | `200` |
| 只读 token 读 / 写 | `200` / **`403`** |
| 错 token | `401` |
| `GET /api/ai/overview` | 返回 `web_url / state / running / failed / recent_events / hint` |
| `GET /api/logs` | 正序返回，`level/message/job_id` 齐全 |

---

## 二·五、v3.5 升级验证（旧库换新镜像）

用正式环境同样的路径验证「已有数据的库能不能直接换镜像」：

```
1. 拉 lzylipu/nas-subtitle-pipeline:v3.0.0 起一个容器，/root/mig1 里生成 admin-password.txt + pipeline.db
2. 扫描 /media（10 部）→ discovered 10
3. 停掉，同一份 /root/mig1 换 lzylipu/jav-subtitle:latest
4. 结果：
   /api/health            {"ok":true,"version":"3.5"}
   /login                 200（表单页，不再是浏览器弹窗）
   匿名 /api/stats        401，且响应头无 WWW-Authenticate
   用老口令登录           login=200（admin-password.txt 自动迁移成 account.json，0600）
   /api/settings          媒体根目录 /media、模型 small 原样保留
   /api/scan              discovered 10 / queued 0
```

再跑一遍完整链路（同一个容器，切到免密钥的谷歌渠道）：

```
入队 /media/剧集 前 1 部 → 抽音频 → 转写 33 条（57s）→ 翻译 → done 100
统计：done 1 / failed 0 / chars_today 471 / asr_ratio 2.89×
日志页 8 条，全部中文，例如「进入「翻译」阶段：转写完成 33 条（57s） — 任务 #1」
```

### 批大小实测（同一批 120 条真实字幕，走中转站）

| 每批条数 | 总耗时 | 折合 | 整片估算（120 分钟片约 1500 条） |
|---|---|---|---|
| 40 | 313.5s | 2.61s/条 | 约 65 分钟 |
| 20 | 694.8s | 5.79s/条 | 约 145 分钟 |

结论：上游每次请求本身就要几十秒，**批越大越划算**，默认保持 40；某一批条数对不上时引擎自己拆成两小批重发。

---

## 三、端到端：10 部片子全流程

素材：10 个 `.mp4`（同一段日语对白，各 165 秒），分布：

```
/media/电影/日影/{A,B,C}.mp4
/media/电影/欧美/D.mp4
/media/剧集/S1/{E01,E02}.mp4
/media/剧集/S2/E03.mp4
/media/剧集/S2/SP/E04.mp4
/media/其他/E.mp4
/media/其他/深层/更深/F.mp4      ← 三级子目录
```

一次 `POST /api/enqueue {"path":"/media"}` 全部入队，之后不再干预。

### 结果

| 指标 | 数值 |
|---|---|
| 完成 / 失败 | **10 / 0** |
| 素材总时长 | 1647.5 秒 |
| 墙钟 | **569.0 秒（9.5 分钟）** |
| 端到端吞吐 | **2.90 × 实时** |
| 转写合计 | 432.1 秒 → **3.81 × 实时** |
| 翻译合计 | 522.0 秒 |
| 翻译请求 | 171 次 |
| 翻译字数 | 4710 |
| 单文件 | 转写 41.5–44.5 秒，翻译 50.9–63.1 秒 |

### 队列推进采样（每 15 秒）

```
t=  0s 待9 跑1 成0 败0 {'asr': 5, 'extract': 5}
t= 15s 待9 跑1 成0 败0 {'asr': 10}          ← 抽音频全部转完，转写接上
t=135s 待7 跑2 成1 败0 {'asr': 7, 'translate': 2}
t=210s 待5 跑2 成3 败0 {'asr': 6, 'translate': 1}
t=270s 待4 跑2 成4 败0 {'asr': 4, 'translate': 2}
t=570s 待0 跑0 成10 败0 {}                   ← 全部完成
```

可以看到 `asr` 与 `translate` 阶段**同时在跑**——三阶段独立队列确实生效，不是串行。

### 文件不变性

```
$ sha256sum 所有非 .srt 文件 > before
（处理 10 部）
$ sha256sum 所有非 .srt 文件 > after
$ diff before after && echo VIDEO_BYTES_UNCHANGED
VIDEO_BYTES_UNCHANGED
```

10 个文件哈希去重后只剩 **1 个值**（素材是同一段的副本），说明原视频字节完全未动。

### 输出抽样

```
$ head -8 /root/media10/电影/日影/A.zh-CN.srt
1
00:00:00,000 --> 00:00:04,940
译文1：おい聞こえるか ここで待

$ head -6 /root/media10/电影/日影/A.ja.srt
1
00:00:00,000 --> 00:00:04,940
おい聞こえるか ここで待っていろ
```

时间轴逐条对齐，中文与日文一一对应，无临时文件残留（`find -name ".*tmp"` = 0）。

---

## 四、限流验证（重点）

用假接口记录每次请求时间戳，并在第 3 次请求注入 `429 + Retry-After: 15`。

```
requests: 171（一次跑完；另一轮单独观测到 88 次样本）
first gaps: [3.0, 3.0, 15.0, 3.0, 3.0, 3.0, ...]
min gap: 3.0   max gap: 15.0
gaps > 10s: [(3, 15.0)]
```

结论：

- **最小间隔 3.0 秒**，与 `translate_min_interval=3` 完全一致 → 没有任何突发
- 第 3 次请求被注入 429 后，间隔变成 **15.0 秒**，正好是上游 `Retry-After` 给的值
  → 客户端听了服务端的，而不是自己拍脑袋
- 该任务没有失败，冷却结束后自动续跑并完成 → 429 不会打断任务

---

## 五、本轮修掉的缺陷

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | asr 阶段全部失败，日志 `[Errno 30] Read-only file system` | compose 把媒体目录挂成 `:ro`，但字幕要写在影片旁 | 去掉 `:ro`；同时给 `atomic_write` 加 `.srt` 后缀白名单，保证不写视频 |
| 2 | 「索引」顺手把全库排队，用户没得选 | `scan` 里无条件调用 `queue_indexed()` | 新增 `auto_enqueue`，默认 **false**；扫描只建索引 |
| 3 | 「只处理前 N 部」实现成先插后删，可能删错其他目录任务 | 用 `LIKE path + DELETE` 收尾 | `limit` 下推进 `queue_indexed()`，超量直接不插 |
| 4 | 设置页 fieldset 挤成一列 / AI 页只读勾选框语义错位 | `fieldset` 未继承 grid；勾选框被放在两列里的第二列 | fieldset 自身设 grid；AI 页关键字段 `grid-column:1/-1` |
| 5 | 成功任务「消息」列仍显示旧报错 | `finish()` 没清 `error` | 完成时 `error=""` |
| 6 | 任务阶段显示英文 `done` | 前端映射缺 key | 补 `done:'已完成'` 等 |
| 7 | 页签无选中态，切换后不知道在哪一页 | 只切 `hidden` | 加 `.on` 类高亮 |

### v3.2 精简（去掉显卡支持）

用户要求项目尽量精简，并明确「N100 这类集显用不上就纯 CPU」。查证 + 实测后删除整套 GPU 分支：

| 改动 | 依据 |
|---|---|
| 删除 `app/devices.py`（202 行）与 `tests/test_devices.py`（14 项） | CTranslate2 官方后端只有 CPU / NVIDIA CUDA / AMD ROCm，**没有 Intel GPU 后端**；`get_supported_compute_types("rocm"/"hip"/"opencl")` 实测全部 `unsupported device` |
| `Settings` 删除 `device` / `compute_type` 字段，设置页删除两个下拉框 | 不可调项不该出现在界面上 |
| `asr.py` 固定 `device="cpu"`、`compute_type="int8"`，新增 `threads_for()` | 0 = 自动取 `min(4, 核数)` |
| Dockerfile 不再装 `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` | 纯 CPU 镜像不需要；镜像回到 **1.04GB**（含 CUDA 库那版压缩后 1.76GB） |
| 新增 `tests/test_asr_cpu.py`（5 项）钉死「必须 cpu+int8」「settings 不许再出现 device」 | 防止以后回潮 |

v3.2 实测（测试机 2 vCPU / 3GB，纯 CPU 镜像 `ns32:test`，1.04GB）：

```
/api/device → {"device":"cpu","compute_type":"int8","cpu_threads":4,"cpu_count":4}
/api/settings → device 字段 False，compute_type 字段 False
/api/scan   → {"discovered":10,...,"queued":0,"auto_enqueue":false}
bench.py    → 164.8s 素材，转写 33.2s，4.96×实时，5 片段
```

> 缺陷 1 尤其值得记：**单测全绿也测不出来**，只有真容器 + 真挂载才会炸。

---

## 六、仍未验证

1. **真实影片识别准确率**——样本是 TTS 合成语音，没有 BGM/重叠/口音
2. **E3-1231v3 上的吞吐**——3.81×实时是 2 vCPU 的数，4C8T 需上机跑 `tools/bench.py`
3. **多天连续运行**——只跑了 9.5 分钟，内存与 SQLite 增长未观测
4. **90 分钟长片的千条字幕分批**——默认批 40 条，未用真实长片压测
5. **上游真实限流策略**——目前只按 3 秒间隔 + 20 rpm 保守跑，真实中转站的 rpm 上限未知

## 刮削（v3.9，26 项）

固定 HTML 片段 + 真实行为断言，不打网络：

- `test_scraping.py`（26 项）：番号提取、噪声剔除、候选打分（番号一致 1.0 / 不同番号必须被拒）、
  详情页语言直链选择（zh-CN > zh-TW > ja，且 **URL 不能被二次编码**）、
  广告与署名行剔除（含「先删文本再解析会留空块导致整份失败」的回归）、
  片长校验（太短 / 超长 / 未知片长）、平台顺序去重、**短路**（主平台命中后不再请求备用平台）、
  Shift-JIS 解码、ASS/VTT 转 SRT。

真网络实测（8 个真实番号）：SubtitleCat **8/8** 命中且 8/8 直接拿到中文；
SubDL 0/8；OpenSubtitles 网页 0/8；Subf2m 3/8 但拿不到下载链；Kitsunekko 动漫可用。

端到端（本机 v3.9 + 124 分钟测试片）：`PPPD-424.mp4` → 刮削命中 1588 条中文 →
写出 `PPPD-424.zh-CN.srt`（89 KB），**全程 20 秒**，完全跳过转写与翻译。
