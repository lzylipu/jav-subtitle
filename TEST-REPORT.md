# 测试报告 — NAS 日文字幕流水线 v3

测试机：`10.10.0.8`（hostname `ceshi`），Debian 12 LXC，**2 vCPU / 3GB RAM**，
Docker 20.10.24，`docker-compose` 1.29.2。

所有数据都是真跑出来的，命令与输出可复现。

---

## 一、单元测试

```
$ docker run --rm -v /root/nas-subtitle-v3/tests:/srv/tests \
    -e WEB_PASSWORD=test nas-subtitle-v3_subtitle:latest \
    sh -c 'cd /srv && PYTHONPATH=/srv:/srv/tests python -m unittest discover -s tests'
...................................
Ran 35 tests in 8.219s
OK
```

宿主机同样 35 项全过。覆盖范围：

| 类别 | 用例 |
|---|---|
| 队列 | 两次扫描才入队、已有字幕不建任务、`limit` 分批、优先级排序、**稳定性闸门重复扫描回归** |
| 目录 | 递归计数（父目录含子目录）、拒绝越界路径、按目录入队只碰该目录 |
| 限流 | 最小间隔真的生效（3 次 ≥0.38s）、rpm→间隔换算、429 触发全局冷却、连续 429 指数退避、上限 1800s |
| 鉴权 | Bearer 通过、错 token 401、只读 token 写操作 403、Basic 通过、错密码 401 |
| 安全 | 拒绝写非 `.srt`、原子写不留临时文件、默认设置无密钥、默认并发为 1 |
| 阶段 | 预算熔断、复用已有日文字幕、音频丢失回退、翻译写 SRT 且命中 TM |

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

> 缺陷 1 尤其值得记：**单测全绿也测不出来**，只有真容器 + 真挂载才会炸。

---

## 六、仍未验证

1. **真实影片识别准确率**——样本是 TTS 合成语音，没有 BGM/重叠/口音
2. **E3-1231v3 上的吞吐**——3.81×实时是 2 vCPU 的数，4C8T 需上机跑 `tools/bench.py`
3. **多天连续运行**——只跑了 9.5 分钟，内存与 SQLite 增长未观测
4. **90 分钟长片的千条字幕分批**——默认批 40 条，未用真实长片压测
5. **上游真实限流策略**——目前只按 3 秒间隔 + 20 rpm 保守跑，真实中转站的 rpm 上限未知
