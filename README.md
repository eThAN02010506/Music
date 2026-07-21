# Music Insight

演唱音乐分析应用。默认模型服务：

- Qwen3-Omni 音频理解：`http://192.168.1.97:8004/v1/chat/completions`
- 本地 Qwen2.5-Omni-3B Q8 权重位于 `src/model/`，可作为离线后备

正式界面使用 React + TypeScript + Vite，FastAPI 提供异步任务与 SSE
进度流；Streamlit 仅保留为调试台。模型权重、上传缓存和本地测试音频
不会提交到 Git。

处理流水线：

1. 预处理生成 16 kHz 单声道 WAV。
2. 本地 librosa 计算整首音乐的 BPM、调性与能量曲线。
3. 音频按默认 30 秒切块，依次提交给统一模型。
4. 模型每块同时提取歌词、乐器/声源、声音事件、人声情绪和局部描述。
5. 时间戳转换成整首音乐坐标，并裁剪或丢弃越界事件。
6. 所有分块完成后，再由同一个模型根据分块证据和 DSP 生成最终报告。
7. Fusion 合并观察、计算、推断和解释证据，并执行歌词语言质量门。

某个音频分块失败不会丢失其他分块；若统一模型整体不可用，仍返回本地 DSP 结果并在 `warnings` 中说明降级。

可靠性策略：

- 优先使用 JSON Schema 约束模型输出；服务不支持时自动回退到 JSON object。
- 本地修复常见的单引号 JSON 和未加引号字段名。
- 拒绝提示词占位值、裁剪越界时间戳，并对歌词、声源和事件去重。
- 歌词缺失时执行一次定向重听；仍无法确认则保守留空。
- 上传在流式写入过程中执行大小限制，超限文件不会保留。

## 当前能力与限制

- 已验证 30 秒和约 4–5 分钟的中文、英文音频可完成上传、DSP、分块分析、
  证据融合和页面展示。
- 长音频默认按 30 秒串行处理。当前进度在分块阶段统一显示为 36%，直到
  全部分块完成后才进入融合；约 4–5 分钟音频可能需要数分钟。
- WAV 是当前最稳定的输入格式。MP3、FLAC、M4A、OGG 等格式依赖 PyAV
  解码；少数带有损坏或非 UTF-8 元数据的文件可能标准化失败，建议先转换为
  16 kHz、单声道、16-bit PCM WAV。
- Qwen Omni 生成的歌词属于模型推断，不应在缺少置信度或独立 ASR 验证时
  视为准确转写。长音频尤其可能出现跨分块重复、补写或与实际人声不一致。
- 生产级歌词链路建议由专用 ASR 负责转写，Qwen Omni 只分析乐器、声景、
  情绪与主题；ASR 未确认人声时歌词应保持为空。当前版本尚未接入这层验证。
- BPM 与调性会显示本地 DSP 置信度。低置信度结果仅供参考，不应当作确定结论。

## 运行

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
PYTHONPATH=src uvicorn music_insight.api.app:app --host 127.0.0.1 --port 8000
```

另开终端启动 React 正式前端：

```bash
cd frontend
pnpm install
pnpm dev
```

访问：`http://127.0.0.1:5174`

Streamlit 保留为调试台：

```bash
PYTHONPATH=src streamlit run src/music_insight/ui/streamlit_app.py --server.port 8501
```

调试台：`http://127.0.0.1:8501`

## API

```bash
curl -F "file=@/path/to/song.m4a" \
  -F "language=en" \
  http://127.0.0.1:8000/analyze
```

歌词语言支持：`zh`、`en`；不传则自动判断。

正式前端使用后台任务接口：

```text
POST /jobs
GET  /jobs/{id}
GET  /jobs/{id}/events
GET  /jobs/{id}/result
POST /jobs/{id}/cancel
```

任务状态包括 `queued`、`running`、`completed`、`failed` 和 `cancelled`。
前端通过 `/jobs/{id}/events` 接收 SSE 进度，完成后读取
`/jobs/{id}/result`。

## 配置

```bash
export MUSIC_INSIGHT_OMNI_ENDPOINT=http://192.168.1.97:8004
export MUSIC_INSIGHT_OMNI_CHUNK_SECONDS=30
```

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/pytest -q
cd frontend && pnpm build
```

## 目录

```text
frontend/        # React + TypeScript + Vite 正式前端
src/model/       # 本地 GGUF 主模型与音频投影
src/music_insight/
  adapters/      # 统一音频分析、本地 DSP 与 OpenAI 兼容工具
  api/           # FastAPI
  pipeline/      # 音频预处理、编排与融合
  reporting/     # Markdown 报告
  storage/       # 上传文件
  ui/            # Streamlit 调试台
```
