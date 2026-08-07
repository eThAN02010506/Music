# Provider 兼容矩阵

本文档记录 Music Insight 已验证过的模型服务端点、能力与回归方式，对应 PRD
FR-AN-008（为每种 Provider 建立固定真实样例兼容测试，并记录首包时间、总耗时、
超时、忙碌和结构化输出成功率）。它不替代 `model_capabilities.py` 的能力探测，
而是把「当前真实服务表现」留档，供部署者对照。

## 如何复现一次矩阵验证

```bash
PYTHONPATH=src python scripts/provider_matrix.py \
  http://192.168.1.97:8004 \
  http://192.168.1.97:8005
```

脚本对每个端点：

1. 清空探测缓存后重新探测 `/health`、`/api/apps`、`/version`、`/v1/models`、
   `/props`，记录协议、音频能力与探测耗时；
2. 若端点在线且支持分析，用固定的短 WAV（默认 `test_samples/johnny_cash_new_mexico_30s.wav`）
   跑一次真实分块分析，记录总耗时、歌词/乐器/声音事件数量与阶段序列；
3. 输出 JSON 报告。

## 已验证端点（2026-08）

### 8004 · OpenAI-compatible（Qwen3-Omni）

| 项 | 值 |
| --- | --- |
| 协议 | `openai-chat` |
| 模型 | Qwen3-Omni-30B-A3B-Instruct (GGUF, llama.cpp) |
| 音频能力 | OpenAI `input_audio` 已启用 |
| 探测耗时 | ~68 ms |
| 30s 音频分析 | 53.4 s；6 段歌词、3 乐器、6 声音事件 |
| 结构化输出 | JSON Schema 成功（无重试降级） |

### 8005 · Comni Turn-based Gateway（MiniCPM-o）

| 项 | 值 |
| --- | --- |
| 协议 | `comni-ws-chat` |
| 模型 | MiniCPM-o-4_5 (GGUF) |
| 音频能力 | Comni Gateway 可用；OpenAI 音频路由关闭 |
| 探测耗时 | ~374 ms |
| 30s 音频分析 | **120 s 超时**（首次响应慢于 8004，客户端超时关闭连接） |
| 说明 | README 已注明「8005 首次响应可能明显慢于 8004」；矩阵按超时记录，非误报 |

## 结构化的输出契约

所有 Provider 输出经统一 JSON Schema 校验（`structured_teaching_schemas.py` /
`structured_omni_schemas.py`），客户端用同一份 Schema 复验。语法正确但字段错误
的响应只重试一次，连续失败记录为模型错误并降级。因此「结构化输出成功率」≈
「未触发保守降级的问答比例」，由 `scripts/chat_regression.py`（B3）按对话统计。

## 新 Provider 接入检查单

见 [docs/provider-onboarding.md](provider-onboarding.md)（B1）。
