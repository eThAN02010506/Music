# Redis / Celery 分布式部署

Music Insight 的后台 `/jobs` 分析支持两种模式：

- `memory`：默认开发模式，一个 FastAPI 进程直接执行任务。
- `redis`：API 将 JSON 任务投递到 Celery；Redis 统一保存任务归属、状态、
  进度、取消标记、结果和全局容量，任意 Worker 机器都可消费。

## 拓扑

```text
浏览器 ──> FastAPI（SQLite 唯一写入者）
              │
              ├── Redis：broker、任务状态、容量与终态待提交集合
              │       ├── Celery Worker A
              │       ├── Celery Worker B
              │       └── Celery Worker C
              │
              └── /srv/music-insight-audio（共享 POSIX 文件目录）
```

Worker 不会打开 API 的 SQLite。Worker 完成后先把终态与结果原子写入 Redis，
API 的后台协调器再以至少一次语义写入 SQLite，然后确认该终态。因此计算节点
可以跨机器，而 SQLite 仍安全地只由 API 主机管理。

上传音频必须位于共享 POSIX 文件系统，例如 NFS、CephFS 或同等产品。该目录
只应存放 Music Insight 管理的音频，因为历史删除和安全 GC 会在此目录内删除
无引用文件。所有节点必须把它挂载为相同的绝对路径。

## Redis

开发机可启动：

```bash
redis-server --appendonly yes --bind 127.0.0.1 --port 6379
```

生产环境不要把未认证的 Redis 暴露到公网。应使用私有网络、防火墙、ACL，
跨主机链路不能完全信任时使用 `rediss://` TLS 地址。Redis 应启用 AOF 或受管
服务提供的持久化和故障转移。

## 环境变量

API 和所有 Worker 使用相同的 Redis、队列名、共享音频路径以及分析配置：

```bash
export MUSIC_INSIGHT_JOB_BACKEND=redis
export MUSIC_INSIGHT_REDIS_URL=redis://127.0.0.1:6379/0
export MUSIC_INSIGHT_REDIS_KEY_PREFIX=music-insight
export MUSIC_INSIGHT_SHARED_AUDIO_DIR=/srv/music-insight-audio
export MUSIC_INSIGHT_REDIS_JOB_TTL_SECONDS=604800
export MUSIC_INSIGHT_DIRECT_WORK_LEASE_TTL_SECONDS=3600
export MUSIC_INSIGHT_CELERY_QUEUE_NAME=music-insight.analysis
export MUSIC_INSIGHT_CELERY_VISIBILITY_TIMEOUT_SECONDS=14400
# 可选：所有 Worker 必须使用相同的歌词验证配置
# export MUSIC_INSIGHT_ASR_VERIFIER_ENABLED=true
# export MUSIC_INSIGHT_ASR_VERIFIER_ENDPOINT=http://192.168.1.97:8003
# export MUSIC_INSIGHT_ASR_VERIFIER_DIALECT=crisp_asr
# export MUSIC_INSIGHT_ASR_VERIFIER_VAD=false
```

生产 API/Worker 使用 `pip install .`；需要在 API 节点生成分轨时使用
`pip install ".[stems]"`，开发与完整测试环境使用
`pip install ".[dev,stems]"`。当前 macOS Python 3.13 会跳过名称
以 `__editable__` 开头的 `.pth` 文件，因此不要依赖 setuptools editable
安装来提供 `music-insight-worker` 命令；源码开发仍可显式使用
`PYTHONPATH=src`。

`MUSIC_INSIGHT_CELERY_VISIBILITY_TIMEOUT_SECONDS` 必须长于最慢任务；否则 Redis
broker 可能在原 Worker 仍运行时重新投递。任务发布采用幂等终态写入，即使发生
至少一次投递也不会覆盖已经发布的终态，但重复推理仍会浪费模型资源。

## 启动

API：

```bash
PYTHONPATH=src uvicorn music_insight.api.app:app \
  --host 0.0.0.0 --port 8000 --workers 1
```

每台 Worker：

```bash
PYTHONPATH=src MUSIC_INSIGHT_WORKER_CONCURRENCY=1 \
  .venv/bin/python -m music_insight.worker
```

安装 wheel 并激活对应虚拟环境后，也可以使用入口命令
`MUSIC_INSIGHT_WORKER_CONCURRENCY=1 music-insight-worker`。或者直接使用
Celery CLI：

```bash
PYTHONPATH=src celery \
  -A music_insight.distributed.celery_app:celery_app worker \
  --queues music-insight.analysis --concurrency 1 --prefetch-multiplier 1
```

多个 Worker 可以在一台或多台机器上运行。每台 Worker 必须：

1. 能连接 Redis。
2. 能读取 `MUSIC_INSIGHT_SHARED_AUDIO_DIR`。
3. 能访问配置的局域网模型地址；Worker 会自动探测 OpenAI 音频或 Comni
   Gateway 协议，因此每台 Worker 到模型服务的网络路径必须一致。
4. 若设置 `MUSIC_INSIGHT_ASR_VERIFIER_ENABLED=true`，还必须能访问相同的
   `MUSIC_INSIGHT_ASR_VERIFIER_ENDPOINT`，并使用相同的 dialect、model 与
   VAD 配置；验证器接收完整标准 WAV，并在每个 Worker 进程内使用独立并发门。
5. 若使用 `model_source=local`，还要安装 `llama-server` 并挂载相同模型路径。

`MUSIC_INSIGHT_WORKER_CONCURRENCY=1` 只限制一个 Worker 进程。API 直接执行的
导赏增强、问答、重听和评分在 Redis 模式下使用带心跳的全局租约，因此多个 API
进程共享 `MUSIC_INSIGHT_MAX_DIRECT_WORK`。Celery 分析 Worker 仍由队列并发数
控制；启动 N 个 Worker 时，同一模型端点仍可能同时收到 N 个后台分析请求。
共享单卡模型时应先只运行一个目标队列 Worker，或增加按 Provider 划分的独立
队列，再逐步压测扩容。

四轨分离当前由接收请求的 API 节点执行，不属于 Celery 分析任务。Redis 模式
仍会通过全局直接工作租约限制请求，API 节点内部另受
`MUSIC_INSIGHT_STEM_MAX_CONCURRENCY` 约束；相同音频使用 POSIX 文件锁和内容
寻址目录避免重复分离。`.music_insight/stems/` 与 Demucs 模型缓存应位于 API
节点的持久磁盘；如果未来部署多个 active-active API 节点，需要先将 SQLite、
stem 缓存和锁目录一起迁移到明确支持一致锁语义的共享存储。

标准化音频缓存在每个 Worker 的 `MUSIC_INSIGHT_WORKSPACE_DIR/normalized/`。
API 启动时的资产 GC 不会扫描远程 Worker 的本地磁盘，因此该目录应使用
可回收的临时卷，或由部署系统按 `MUSIC_INSIGHT_ASSET_GC_GRACE_HOURS` 等保留
策略周期清理。

已启动 Redis 和至少一个 Worker 后，可执行不调用真实模型的跨进程烟雾测试：

```bash
PYTHONPATH=src python scripts/redis_celery_smoke.py
```

脚本故意投递一个缺失音频路径，验证独立 Worker 能消费消息、执行安全校验并
把失败终态写回 Redis；完成后会删除测试任务。

## 可靠性与取消

- Celery 使用 JSON-only serializer，消息中只携带随机任务 ID；Worker 从
  Redis 重新读取服务端创建的规范任务参数，避免信任消息内的路径或模型地址。
  同时启用 late ACK、`reject_on_worker_lost` 和预取 1；Worker 异常退出时
  任务由 broker 重新投递。
- Redis Lua 脚本同时执行全局和每用户容量预留，多个 API 进程不能绕过上限。
- API 直接工作租约跨进程共享，并由心跳延长；API 崩溃后租约会在
  `MUSIC_INSIGHT_DIRECT_WORK_LEASE_TTL_SECONDS` 内自动回收。Celery Worker
  的进程内模型 gate 仍相互独立，后台分析并发由 Worker/队列配置控制。
- 排队任务取消会立即释放容量；运行任务在下一个进度检查点协作取消。外部模型
  请求已经开始后不会被强制杀死，以免留下损坏缓存或不一致结果。
- Worker 不写 SQLite。Redis 终态在 API 恢复后会再次协调；只要恢复发生在
  `MUSIC_INSIGHT_REDIS_JOB_TTL_SECONDS` 内，结果不会丢失。

## 当前横向边界

后台分析 Worker 已可跨进程、跨机器扩展。API 的账号、历史和排行榜仍使用
本机 SQLite，因此生产拓扑应保留一个 API 数据写入节点（可以由反向代理做
健康检查和故障切换，但不要让两台拥有不同 SQLite 的 API 同时对外提供数据）。
如果下一阶段需要多台 API 主机 active-active，应再把 SQLite 迁移到 PostgreSQL，
共享音频迁移到对象存储；这和本次 Worker 横向扩展是独立边界。
