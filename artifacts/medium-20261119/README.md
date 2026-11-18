# Medium 五部整片转写产物

本目录保存 2026-11-19 五部 medium 整片无缓存转写的清单与结构指标。

| 文件 | 说明 |
|---|---|
| `manifest.json` | 五部片的时长、条数、占用率、sha256、运行环境、VAD 档位 |
| `structure.json` | 五份新字幕 × 五份参考字幕的完整结构指标 |
| `*.medium.full.srt` | 修复前的原始转写（按仓库 `.gitignore` 的 `*.srt` 规则不入库，但文件在本机与测试机保留） |
| `formatted/*.srt` | 把本轮修复（两行排版 + 1.5s 补尾）套回同一批 raw 后的对照产物，同样不入库 |

原始产物在测试机的路径：

```
/root/demo310data/final-medium-20261117-131439/*.medium.full.srt
```

结构对照报告见 [`../../docs/ASR-MEDIUM-REPORT.md`](../../docs/ASR-MEDIUM-REPORT.md)。
