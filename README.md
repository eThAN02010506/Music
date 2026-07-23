# Music Insight

演唱音乐分析应用。默认模型服务：

- Qwen3-Omni 音频理解：`http://192.168.1.97:8004/v1/chat/completions`
- 本地 Qwen2.5-Omni-3B Q8 权重位于 `src/model/`，可作为离线后备

正式界面使用 React + TypeScript + Vite，FastAPI 提供异步任务与 SSE
进度流；Streamlit 仅保留为调试台。正式界面支持多个持久化分析、重命名、
删除和双结果对比。模型权重、上传缓存和本地测试音频不会提交到 Git。

处理流水线：

1. 预处理生成 16 kHz 单声道 WAV。
2. 本地 librosa 计算整首音乐的 BPM、调性与能量曲线。
3. 音频按默认 30 秒切块，依次提交给统一模型；同一模型地址默认只运行一个
   分析任务，其他任务等待模型资源，避免长音频互相争抢。
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
- 模型分块与最终综合分别上报进度和耗时；Debug Console 可以查看每个阶段。
- 内存任务仅保留最近 100 个终态任务，历史删除也会同步清理内存结果。
- 历史记录持久化失败会单独报告，不会把已经成功的模型分析改写为失败。

## 当前能力与限制

- 已验证 30 秒和约 4–5 分钟的中文、英文音频可完成上传、DSP、分块分析、
  证据融合和页面展示。
- 长音频默认按 30 秒串行处理。页面会显示当前分块序号、总分块数和最近一块
  的耗时；约 4–5 分钟音频仍可能需要数分钟。
- WAV 是当前最稳定的输入格式。MP3、FLAC、M4A、OGG 等格式依赖 PyAV
  解码；少数带有损坏或非 UTF-8 元数据的文件可能标准化失败，建议先转换为
  16 kHz、单声道、16-bit PCM WAV。
- Qwen Omni 生成的歌词属于模型推断，不应在缺少置信度或独立 ASR 验证时
  视为准确转写。长音频尤其可能出现跨分块重复、补写或与实际人声不一致。
- 生产级歌词链路建议由专用 ASR 负责转写，Qwen Omni 只分析乐器、声景、
  情绪与主题；ASR 未确认人声时歌词应保持为空。当前版本尚未接入这层验证。
- BPM 与调性会显示本地 DSP 置信度。低置信度结果仅供参考，不应当作确定结论。
- BPM 估计会检查常见的半速/双速歧义。当高 BPM 与其半速候选具有近似
  tempogram 支持时，界面优先显示更接近人类拍点感知的半速值，并保留原始
  倍频候选供核查。
- 默认模型仍为 `192.168.1.97:8004`。每次新分析可以在“模型设置”中改用
  其他 OpenAI 兼容地址，或选择后端本机的 GGUF 权重；选择只影响该次任务。
- 模型设置提供 8004 Qwen3-Omni 与 8005 MiniCPM-o-4.5 预设。8005 当前是
  实验入口：其 `/v1/chat/completions` 文本请求可用，但当前部署未在该路由
  加载音频模态，会返回 `audio input is not supported`。MiniCPM Gateway
  的 `/api/chat` 尚未接入，因此现阶段不能据此比较模型本身的音频准确度。
- 模型设置中的“测试模型连接”会在上传前读取模型名称和音频模态状态；
  探测不会创建分析任务。音频明确关闭时应先修复模型服务再上传。
- 本地权重模式需要支持该 Qwen Omni GGUF 的 `llama-server`。后端会在
  `MUSIC_INSIGHT_LOCAL_MODEL_ROOT` 内查找主 GGUF 和 `mmproj*.gguf`，并在
  `127.0.0.1:8010` 启动服务。运行器缺失或路径无效时创建任务会明确报错，
  不会悄悄回退到局域网模型。

## 历史、缓存与对比

- 新任务创建后立即出现在左侧历史列表；结果、状态、原始音频路径和模型来源
  持久化到 `.music_insight/history.sqlite3`，重启 FastAPI 后仍可读取。
- 历史项可以打开、重命名或删除。删除会移除 SQLite 记录及工作区内缓存的
  原始上传音频；正在运行的任务必须先取消。
- 勾选两个已完成项目后可并排比较 BPM、调性、歌词数量、乐器、主题、直接
  情绪、推断氛围和摘要。

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

FastAPI 根地址 `http://127.0.0.1:8000/` 是开发监控台，可实时查看任务阶段、
进度、最近历史、失败原因和分析警告，并可下载 JSON 诊断报告。点击任一任务
可查看阶段事件时间线、模型与文件信息、技术指标、歌词、证据和原始 JSON。
原服务信息 JSON 位于 `/api/info`，交互式 API 文档位于 `/docs`。

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
GET  /history
GET  /history/{id}
GET  /history/{id}/audio
PATCH /history/{id}
DELETE /history/{id}
GET  /debug/state
GET  /debug/report
GET  /debug/tasks/{id}
GET  /api/info
POST /models/probe
```

任务状态包括 `queued`、`running`、`completed`、`failed` 和 `cancelled`。
前端通过 `/jobs/{id}/events` 接收 SSE 进度，完成后读取
`/jobs/{id}/result`。

`POST /jobs` 还接受以下可选表单字段：

```text
model_source=network | local
model_endpoint=http://host:port       # network 模式；留空使用默认 8004
local_model_path=/allowed/path        # local 模式；目录或主 GGUF 文件
```

## 配置

```bash
export MUSIC_INSIGHT_OMNI_ENDPOINT=http://192.168.1.97:8004
export MUSIC_INSIGHT_OMNI_CHUNK_SECONDS=30
export MUSIC_INSIGHT_OMNI_MAX_CONCURRENCY=1
export MUSIC_INSIGHT_LOCAL_MODEL_ROOT=src/model
export MUSIC_INSIGHT_LOCAL_OMNI_ENDPOINT=http://127.0.0.1:8010
export MUSIC_INSIGHT_LOCAL_LLAMA_SERVER=llama-server
```

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/pytest -q
cd frontend && pnpm test && pnpm build
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

模型结构化输出协议位于 `adapters/qwen_omni_schemas.py`，网络请求和页面状态
调用分别集中在后端适配器与 `frontend/src/api.ts`，避免 UI 组件重复处理
HTTP 错误和响应解析。
