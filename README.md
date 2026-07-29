# Music Insight

演唱音乐分析应用。默认模型服务：

- Qwen3-Omni 音频理解：`http://192.168.1.97:8004/v1/chat/completions`
- MiniCPM-o 4.5 Comni Gateway：`ws://192.168.1.97:8005/ws/chat`
- 本地 Qwen2.5-Omni-3B Q8 权重位于 `src/model/`，可作为离线后备

正式界面使用 React + TypeScript + Vite，FastAPI 提供异步任务与 SSE
进度流；Streamlit 仅保留为调试台。正式界面支持多个持久化分析、重命名、
删除和双结果对比，并提供本地账号、用户独立历史与演唱最高分排行榜。
模型权重、上传缓存和本地测试音频不会提交到 Git。

处理流水线：

1. 预处理生成 16 kHz 单声道 WAV。
2. 本地 librosa 计算整首能量曲线，并从均匀覆盖全曲、具有固定内存上限的
   连续窗口估计 BPM 与调性。
3. OpenAI 音频协议默认按 30 秒切块，Comni Gateway 默认按 15 秒切块；
   相邻块保留 1.5 秒重叠后依次提交给统一模型；
   重叠区歌词按时间中点归属单个分块，兼顾边界上下文和去重。同一模型地址
   默认只运行一个分析任务，其他任务等待模型资源，避免长音频互相争抢。
4. 模型每块同时提取歌词、乐器/声源、声音事件、人声情绪和局部描述。
5. 时间戳转换成整首音乐坐标，并裁剪或丢弃越界事件。
6. 所有分块完成后，再由同一个模型根据分块证据和 DSP 生成最终报告。
7. Fusion 合并观察、计算、推断和解释证据，并执行歌词语言质量门。

某个音频分块失败不会丢失其他分块；若统一模型整体不可用，仍返回本地 DSP 结果并在 `warnings` 中说明降级。

可靠性策略：

- OpenAI 兼容 Provider 优先使用 JSON Schema，服务不支持时回退到 JSON
  object；Comni Provider 使用严格提示和一次有界 JSON 修复，不伪装成
  OpenAI structured output。
- 本地修复常见的单引号 JSON 和未加引号字段名。
- 拒绝提示词占位值、裁剪越界时间戳，并对歌词、声源和事件去重。
- 歌词缺失时执行一次定向重听；仍无法确认则保守留空。
- 每个分块会检查歌词文字密度、明显重叠、近邻重复和过短时间段；异常时只
  重听对应分块，仍不可靠则仅剔除异常行，并在结果证据中保留处理原因。
- 上传在 multipart 解析前即按 `Content-Length` 和实际接收字节执行硬限制；
  128 MB 单文件额度、两文件对比额度与加权并发接收上限相互独立。写入后还会
  验证音频容器和 20 分钟时长，解码阶段再次限制，失败、取消或超限文件不保留。
- 上传、任务注册或客户端请求被取消时，会补偿删除部分文件、任务和未完成历史，
  不会留下永久等待的后台任务。
- 模型分块与最终综合分别上报进度和耗时；Debug Console 可以查看每个阶段。
- 浏览器进度流支持断线重连和任务轮询兜底；终态只在历史持久化完成后发布。
- 默认内存任务仅保留最近 100 个终态任务。可切换到 Redis/Celery 分布式
  后端：Redis 跨进程保存任务、进度、取消和结果，Celery Worker 可部署在
  多台机器；历史删除也会同步清理相应任务状态。
- 默认最多接收 8 个活动任务、每个用户最多 3 个；内存模式按进程限制，
  Redis 模式使用 Lua 原子脚本执行全局限制，多个提交进程不能绕过容量。
- 同步分析、歌词重听和演唱评分最多同时执行 2 个、每用户 1 个；音频预处理、
  DSP 与演唱特征提取共享最多 2 个本地计算槽。取消请求会先等待无法中止的
  工作线程结算再释放槽位，避免用反复取消绕过内存上限。
- SQLite 使用带版本号的幂等迁移；升级旧数据库前自动保留
  `history.pre-v*.sqlite3.bak`，迁移失败会回滚。
- 密码使用 OWASP 推荐参数档位的带随机盐 scrypt 保存，重型 KDF 同时最多
  执行 4 个；浏览器只持有 HttpOnly 会话 Cookie，
  SQLite 中只保存会话令牌摘要。历史、任务、SSE、音频和调试报告均由后端
  按当前用户校验，前端不会提交或信任 `user_id`。

## 当前能力与限制

- 已验证 30 秒和约 4–5 分钟的中文、英文音频可完成上传、DSP、分块分析、
  证据融合和页面展示；默认接受的单文件上限是 128 MB 且最长 20 分钟。
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
  其他局域网模型地址，或选择后端本机的 GGUF 权重；选择只影响该次任务。
- 网络模型通过能力探测和 Provider 注册表接入，不按端口或模型名称猜协议。
  当前内置 OpenAI `input_audio` Provider（适用于 Qwen 等兼容服务）和
  MiniCPM-o Comni `/ws/chat` Provider；后续模型只需注册新的协议适配器，
  无需修改分析 Pipeline、历史或分布式任务结构。
- 8005 的 OpenAI 路由虽然声明 `audio:false`，但 Comni Turn-based Gateway
  可接收 16 kHz 单声道 Float32 PCM。客户端会明确发送
  `tts.enabled=false`，并丢弃服务端意外返回的音频数据。
- 模型设置中的“测试模型连接”只读取 `/health`、`/api/apps`、`/v1/models`
  和 `/props`，不会发送音频或创建推理任务。页面分别显示选中的协议、
  综合分析能力和 OpenAI 路由的音频状态。
- Comni 当前仍属于实验 Provider：默认单并发、15 秒分块，局域网 8005 的
  首次响应可能明显慢于 8004。客户端超时会关闭 WebSocket，但上游 Gateway
  对仍在 FIFO 队列中的请求不保证立即取消。
- 本地权重模式需要支持该 Qwen Omni GGUF 的 `llama-server`。后端会在
  `MUSIC_INSIGHT_LOCAL_MODEL_ROOT` 内查找主 GGUF 和 `mmproj*.gguf`，并在
  `127.0.0.1:8011` 启动服务。运行器缺失或路径无效时创建任务会明确报错，
  不会悄悄回退到局域网模型。

## 历史、缓存与对比

- 首次打开正式界面需要创建本地账号。每个账号只能看到自己的分析、缓存音频
  和演唱记录；退出或切换账号时前端会卸载当前工作区并清空内存状态，其他
  浏览器标签页也会立即重新校验账号，避免混用两个用户的数据。
- 从旧版本升级时，在运行服务的本机创建的第一个账号会自动认领原有无归属
  历史。也可登录后在本机调用 `POST /auth/claim-legacy` 进行一次性认领；
  局域网远端账号不能认领旧数据。
- 新任务创建后立即出现在左侧历史列表；结果、状态、原始音频路径和模型来源
  持久化到 `.music_insight/history.sqlite3`，新音频位于
  `.music_insight/users/<user-id>/uploads/`，重启 FastAPI 后仍可读取。
- 历史项可以打开、重命名或删除。删除会移除 SQLite 记录及工作区内缓存的
  原始上传音频；正在运行的任务必须先取消。标准化音频等共享派生缓存由带
  保留期的安全 GC 回收，避免误删仍被其他分析使用的内容。
- 服务启动时会先扫描上传、标准化音频、旧 stems 和崩溃遗留的临时评分文件，
  完成安全 GC 后才接受请求；只删除超过保留期且不再被历史引用的文件。GC
  失败会记录到 Debug Console，但不会阻止服务继续启动。
- 已完成结果可以在歌词时间轴中人工修改文本和起止秒数。每次保存都会先把
  旧结果归档到 `analysis_revisions`，页面可切换查看修订前版本；模型自动
  重听过的分块会显示异常原因以及初次、重听结果。
- 每条歌词可以单独触发所在 30 秒分块的模型重听。新结果先在页面预览，
  只有用户确认后才会替换该范围的歌词并保存为一个可回溯的新版本。
- “演唱对比”支持浏览器麦克风录音或上传个人演唱，使用本地 DSP 对照当前
  历史歌曲计算音准、节奏、完整度和稳定性。综合分权重分别为 50%、25%、
  15% 和 10%。音高与起音强度使用带窗口约束的动态时间规整（DTW）对齐，
  允许合理的局部快慢差异；稳定性依据相邻音高变化计算，而不是把“是否发声”
  直接当作稳定。页面同时显示对齐后的音高误差时间轴，大模型不参与总分。
- 首页还提供“独立演唱对比”：无需先分析歌曲，分别上传任意参考音频与个人
  录音（个人录音也可直接使用浏览器麦克风采集）。两份文件只用于本次评分，
  返回结果后立即从后端临时目录删除。
- 每次服务端评分都会保存到当前账号。排行榜每个账号只取历史最高综合分，并
  展示四项分数和尝试次数。由于不同用户可以选择不同参考音频，且独立模式可
  上传相同文件，当前总榜明确属于“娱乐最高分榜”，不能视为同曲竞技成绩。
- 勾选两个已完成项目后可并排比较 BPM、调性、歌词数量、乐器、主题、直接
  情绪、推断氛围和摘要。

## 运行

```bash
python -m venv .venv
. .venv/bin/activate
pip install ".[dev]"
PYTHONPATH=src uvicorn music_insight.api.app:app --host 127.0.0.1 --port 8000
```

另开终端启动 React 正式前端：

```bash
cd frontend
pnpm install
pnpm dev
```

访问：`http://127.0.0.1:5174`

开发前端默认通过同源 `/api` 代理访问本机 `8000` 后端，因此浏览器只需连接
`5174`，不会直接跨端口请求 API。若要供局域网其他设备使用，仍应把实际前端
Origin 加入后端白名单；纯 HTTP 局域网无法保护密码、Cookie 或上传的录音，
正式部署应使用 HTTPS 反向代理：

```bash
export MUSIC_INSIGHT_WEB_ORIGINS=http://192.168.1.97:5174
PYTHONPATH=src uvicorn music_insight.api.app:app --host 0.0.0.0 --port 8000
cd frontend && pnpm dev
```

FastAPI 根地址 `http://127.0.0.1:8000/` 是开发监控台，可实时查看任务阶段、
进度、最近历史、失败原因和分析警告，并可下载 JSON 诊断报告。点击任一任务
可查看阶段事件时间线、模型与文件信息、技术指标、歌词、证据和原始 JSON。
监控数据按当前浏览器已登录账号隔离；未登录时应先在正式界面登录。原服务
信息 JSON 位于 `/api/info`，交互式 API 文档位于 `/docs`。

Streamlit 保留为调试台：

```bash
PYTHONPATH=src streamlit run src/music_insight/ui/streamlit_app.py --server.port 8501
```

调试台：`http://127.0.0.1:8501`

Streamlit 调试台也需要填写已在正式界面创建的本地账号和密码，才能调用受
保护的 `/analyze` 接口。

## API

公开接口只有 `/health`、`/api/info`、API 文档、`/auth/register` 和
`/auth/login`；`/auth/logout` 可匿名幂等调用。`/auth/me`、
`/auth/claim-legacy` 以及其他业务与调试接口均需要会话 Cookie。命令行可
使用 cookie jar：

```bash
curl -c /tmp/music-insight.cookies \
  -H "Content-Type: application/json" \
  -d '{"username":"local-user","password":"replace-this-password"}' \
  http://127.0.0.1:8000/auth/register

curl -b /tmp/music-insight.cookies \
  -F "file=@/path/to/song.m4a" \
  -F "language=en" \
  http://127.0.0.1:8000/analyze
```

歌词语言支持：`zh`、`en`；不传则自动判断。

正式前端使用后台任务接口：

```text
POST /auth/register
POST /auth/login
POST /auth/logout
GET  /auth/me
POST /auth/claim-legacy              # 仅服务所在本机
POST /jobs
GET  /jobs/{id}
GET  /jobs/{id}/events
GET  /jobs/{id}/result
POST /jobs/{id}/cancel
GET  /history
GET  /history/{id}
GET  /history/{id}/audio
PATCH /history/{id}
PATCH /history/{id}/lyrics
POST  /history/{id}/lyrics/retry
GET  /history/{id}/revisions
POST /history/{id}/singing/score
POST /singing/compare
GET  /singing/attempts
GET  /leaderboard
DELETE /history/{id}
GET  /debug/state
GET  /debug/report
GET  /debug/tasks/{id}
GET  /api/info
POST /models/probe
GET  /runtime-config
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
export MUSIC_INSIGHT_WORKSPACE_DIR=.music_insight
export MUSIC_INSIGHT_MAX_UPLOAD_MB=128
export MUSIC_INSIGHT_MAX_AUDIO_MINUTES=20
export MUSIC_INSIGHT_ASSET_GC_GRACE_HOURS=24
export MUSIC_INSIGHT_MAX_ACTIVE_JOBS=8
export MUSIC_INSIGHT_MAX_ACTIVE_JOBS_PER_USER=3
export MUSIC_INSIGHT_JOB_BACKEND=memory
# 分布式模式：
# export MUSIC_INSIGHT_JOB_BACKEND=redis
# export MUSIC_INSIGHT_REDIS_URL=redis://127.0.0.1:6379/0
# export MUSIC_INSIGHT_SHARED_AUDIO_DIR=/srv/music-insight-audio
# export MUSIC_INSIGHT_REDIS_KEY_PREFIX=music-insight
# export MUSIC_INSIGHT_REDIS_JOB_TTL_SECONDS=604800
# export MUSIC_INSIGHT_CELERY_QUEUE_NAME=music-insight.analysis
# export MUSIC_INSIGHT_CELERY_VISIBILITY_TIMEOUT_SECONDS=14400
export MUSIC_INSIGHT_MAX_DIRECT_WORK=2
export MUSIC_INSIGHT_MAX_DIRECT_WORK_PER_USER=1
export MUSIC_INSIGHT_MAX_UPLOAD_UNITS=2
export MUSIC_INSIGHT_AUTH_KDF_MAX_CONCURRENCY=4
export MUSIC_INSIGHT_DSP_MAX_CONCURRENCY=2
export MUSIC_INSIGHT_OMNI_ENDPOINT=http://192.168.1.97:8004
export MUSIC_INSIGHT_OMNI_CHUNK_SECONDS=30
export MUSIC_INSIGHT_OMNI_CHUNK_OVERLAP_SECONDS=1.5
export MUSIC_INSIGHT_OMNI_MAX_CONCURRENCY=1
export MUSIC_INSIGHT_COMNI_CHUNK_SECONDS=15
export MUSIC_INSIGHT_COMNI_OPEN_TIMEOUT_SECONDS=10
export MUSIC_INSIGHT_COMNI_FIRST_EVENT_TIMEOUT_SECONDS=180
export MUSIC_INSIGHT_COMNI_IDLE_TIMEOUT_SECONDS=180
export MUSIC_INSIGHT_COMNI_REQUEST_TIMEOUT_SECONDS=600
export MUSIC_INSIGHT_COMNI_MAX_MESSAGE_MB=8
export MUSIC_INSIGHT_LOCAL_MODEL_ROOT=src/model
export MUSIC_INSIGHT_LOCAL_OMNI_ENDPOINT=http://127.0.0.1:8011
export MUSIC_INSIGHT_LOCAL_LLAMA_SERVER=llama-server
export MUSIC_INSIGHT_WEB_ORIGINS=http://192.168.1.97:5174
```

默认 `memory` 模式仍适合本地开发。设置 `MUSIC_INSIGHT_JOB_BACKEND=redis`
后，后台 `/jobs` 使用 Redis + Celery：任务状态、SSE 数据、取消、结果以及
全局/用户容量均跨进程共享，Worker 可以部署到多台机器。音频通过
`MUSIC_INSIGHT_SHARED_AUDIO_DIR` 共享；Worker 不写 SQLite，终态由 API
后台协调器以至少一次语义持久化。完整拓扑、Redis 安全配置与启动命令见
[`docs/distributed-deployment.md`](docs/distributed-deployment.md)。

账号、历史和排行榜目前仍由一个 API 节点上的 SQLite 管理；后台计算已可横向
扩展，但多台 API 主机 active-active 仍需要后续迁移到 PostgreSQL/对象存储。

前端提交的网络模型地址仅允许 `localhost`、回环地址、链路本地地址或私有
局域网 IP，且不能携带用户凭据、query 或 fragment；本地 GGUF 路径必须位于
`MUSIC_INSIGHT_LOCAL_MODEL_ROOT` 之下。不要把任意公网 URL 或文件系统路径
暴露给该服务。

HTTPS 反向代理或前后端不在同一 Origin 时，还需要在构建前端时设置
`VITE_API_BASE_URL`，例如 `VITE_API_BASE_URL=https://music-api.example.com`。
未设置时，前端默认连接当前主机的 `8000` 端口。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/pytest -q
cd frontend && pnpm test && pnpm build
```

## 目录

```text
frontend/        # React + TypeScript + Vite；按 features 与 hooks 分层
src/model/       # 本地 GGUF 主模型与音频投影
src/music_insight/
  adapters/      # Provider 注册、能力探测、协议传输、结构化工作流与本地 DSP
  api/
    routers/     # 认证、任务、历史、评分、Debug 与系统 HTTP 边界
    services/    # 分析提交、歌词重听、评分与上传用例
    migrations.py # SQLite 版本化迁移
    database.py  # 连接、备份与迁移入口
  pipeline/      # 音频预处理、编排与融合
  reporting/     # Markdown 报告
  storage/       # 上传、资产引用与安全 GC
  ui/            # Streamlit 调试台
```

供应商无关的结构化输出协议、请求构造、解析、音频切块和工作流位于
`adapters/structured_omni_*.py`。`network_omni.py` 根据只读能力探测结果
从 Provider 注册表选择传输；`qwen_omni_unified.py` 负责 OpenAI HTTP，
`minicpm_gateway.py` 负责 Comni WebSocket。网络调用集中在后端适配器，
前端 HTTP 调用集中在 `frontend/src/api.ts`。
