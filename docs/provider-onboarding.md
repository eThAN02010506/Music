# 可接入的服务形态

Music Insight 通过统一 Provider 适配层接入网络音频模型。以下服务形态已由现有
代码支持或可直接接入，前提是能力探测能识别其协议。

## OpenAI-compatible（标准 `input_audio`）

已支持。`OpenAIChatAudioAdapter` 使用标准 Chat Completions + `input_audio`
（base64 WAV），能力探测从 `/props` 的 `modalities.audio` 判断音频可用性。

- **vLLM-Omni（Qwen2.5-Omni）**：README 引用的 vLLM-Omni 示例支持
  `input_audio`。接入时若服务不返回 `/props` 或字段不同，探测会报告
  「未定（None）」并建议短音频验证——此时仍需确认服务实际支持
  `input_audio` 才会真正可用。
- **Qwen3-Omni / 兼容 llama.cpp 服务**：已验证（`docs/provider-matrix.md`
  的 8004 端点）。
- **Ollama**：若部署的模型暴露 OpenAI 兼容音频输入且 `/props` 声明音频，
  可直接接入；Ollama 官方 OpenAI 兼容范围不含音频时不会通过能力探测。

协议契约由 `tests/test_provider_contract.py` 锁定（标准 `input_audio` 格式、
能力分类、适配器教学能力）。

## MiniCPM-o Comni Turn-based Gateway（WebSocket）

已支持。`MiniCpmGatewayAdapter` 通过 `/ws/chat` 接收 16 kHz Float32 PCM。
能力探测从 `/api/apps` 的 `app_id=="turnbased"` 识别。已验证（8005 端点）。

## 本地 GGUF（llama-server）

已支持。`ManagedLocalOmniAdapter` 启动受控的 `llama-server` 子进程，走
OpenAI-compatible 协议。需主 GGUF + `mmproj*.gguf`。

## 接入新服务

见 [provider-onboarding.md](provider-onboarding.md) 的四步流程与判定标准。
