from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

from music_insight.api.history import HistoryStore
from music_insight.api.jobs import AnalysisJobStore
from music_insight.config import Settings


def debug_state(
    jobs: AnalysisJobStore,
    history: HistoryStore,
    settings: Settings,
) -> dict[str, Any]:
    recent_history = history.list(limit=20)
    recent_jobs = jobs.list(limit=20)
    issues: list[dict[str, str]] = []
    for job in recent_jobs:
        if job.persistence_error:
            issues.append(
                {
                    "severity": "error",
                    "source": job.id,
                    "message": f"分析结果可用，但历史记录持久化失败：{job.persistence_error}",
                    "time": job.updated_at.isoformat(),
                }
            )
    for entry in recent_history:
        if entry.error:
            issues.append(
                {
                    "severity": "error",
                    "source": entry.title,
                    "message": entry.error,
                    "time": entry.updated_at.isoformat(),
                }
            )
        elif entry.state == "cancelled":
            issues.append(
                {
                    "severity": "warning",
                    "source": entry.title,
                    "message": "任务在完成前被取消；可能由用户取消或后端服务停止引起。",
                    "time": entry.updated_at.isoformat(),
                }
            )
        detail = history.get(entry.id)
        if detail and detail.result:
            for warning in detail.result.warnings:
                issues.append(
                    {
                        "severity": "warning",
                        "source": entry.title,
                        "message": warning,
                        "time": entry.updated_at.isoformat(),
                    }
                )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "service": {
            "status": "ok",
            "api_endpoint": "http://127.0.0.1:8000",
            "model_endpoint": settings.omni_endpoint,
            "chunk_seconds": settings.omni_chunk_seconds,
            "model_max_concurrency": settings.omni_max_concurrency,
            "workspace": str(settings.workspace_dir.resolve()),
        },
        "jobs": [item.model_dump(mode="json") for item in recent_jobs],
        "history": [item.model_dump(mode="json") for item in recent_history],
        "issues": issues[:50],
        "counts": {
            "active": sum(item.state.value in {"queued", "running"} for item in recent_jobs),
            "failed": sum(item.state.value == "failed" for item in recent_jobs),
            "history": len(recent_history),
            "issues": len(issues),
        },
    }


def diagnostic_report(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2)


def task_detail(
    task_id: str,
    jobs: AnalysisJobStore,
    history: HistoryStore,
) -> dict[str, Any] | None:
    snapshot = jobs.get(task_id)
    history_entry = history.get(task_id)
    if snapshot is None and history_entry is None:
        return None
    result = jobs.result(task_id) or (history_entry.result if history_entry else None)
    return {
        "id": task_id,
        "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
        "events": [event.model_dump(mode="json") for event in jobs.events(task_id)],
        "history": history_entry.model_dump(mode="json") if history_entry else None,
        "result": result.model_dump(mode="json") if result else None,
    }


DEBUG_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Music Insight · Debug</title>
  <style>
    :root{color-scheme:dark;--bg:#09100e;--panel:#111a17;--line:#293630;--ink:#e9efeb;--muted:#849189;--green:#a7f07b;--yellow:#f0cb7b;--red:#ff846f}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px system-ui,-apple-system,"Noto Sans SC",sans-serif}
    header{height:72px;position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;padding:0 30px;border-bottom:1px solid var(--line);background:rgba(9,16,14,.94);backdrop-filter:blur(14px)}
    header h1{font-size:17px;margin:0}header h1 span{color:var(--green)}nav{display:flex;gap:9px}a,button{color:var(--ink);text-decoration:none;border:1px solid var(--line);background:#132019;border-radius:8px;padding:9px 12px;cursor:pointer;font:inherit}a:hover,button:hover{border-color:var(--green)}
    main{width:min(1240px,calc(100% - 32px));margin:30px auto 70px}.status-line{display:flex;align-items:center;gap:9px;color:var(--muted);margin-bottom:20px}.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px rgba(167,240,123,.08)}
    .metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.metric,.panel{border:1px solid var(--line);background:var(--panel);border-radius:13px}.metric{padding:18px}.metric span{display:block;color:var(--muted);font-size:11px}.metric strong{display:block;font-size:25px;margin-top:8px}.grid{display:grid;grid-template-columns:1.35fr 1fr;gap:16px;margin-top:16px}.panel{padding:20px;min-width:0}.panel h2{font-size:14px;margin:0 0 16px}.empty{color:var(--muted);font-size:12px;padding:18px 0}.item{border-top:1px solid var(--line);padding:14px 0}.item:first-of-type{border-top:0}.item-head{display:flex;align-items:center;justify-content:space-between;gap:14px}.item strong{font-size:12px}.meta{color:var(--muted);font:10px ui-monospace,monospace;margin-top:6px;overflow-wrap:anywhere}.badge{font-size:9px;padding:4px 7px;border-radius:10px;background:#253129;color:var(--muted)}.badge.running,.badge.completed{color:var(--green)}.badge.failed,.issue.error{color:var(--red)}
    .track{height:5px;background:#26312c;border-radius:4px;margin-top:10px;overflow:hidden}.track i{display:block;height:100%;background:var(--green)}.issue{border-left:2px solid var(--yellow);padding:9px 11px;margin:8px 0;background:#161b17;font-size:11px;line-height:1.55}.issue.error{border-color:var(--red)}.issue small{display:block;color:var(--muted);margin-top:5px}.service-table{display:grid;grid-template-columns:120px 1fr;gap:10px;font-size:11px}.service-table dt{color:var(--muted)}.service-table dd{margin:0;font-family:ui-monospace,monospace;overflow-wrap:anywhere}.wide{grid-column:1/-1}.open-task{width:100%;border:0;background:transparent;padding:0;text-align:left}.open-task:hover{border:0}.open-task:hover strong{color:var(--green)}
    dialog{width:min(900px,calc(100% - 30px));max-height:88vh;padding:0;border:1px solid #3a4b43;border-radius:15px;background:#0e1714;color:var(--ink);box-shadow:0 30px 90px rgba(0,0,0,.6)}dialog::backdrop{background:rgba(0,0,0,.68);backdrop-filter:blur(4px)}.detail-head{position:sticky;top:0;z-index:2;display:flex;justify-content:space-between;align-items:center;padding:18px 22px;border-bottom:1px solid var(--line);background:#101a16}.detail-head div{display:flex;flex-direction:column}.detail-head strong{font-size:15px}.detail-head small{color:var(--muted);font:9px ui-monospace,monospace;margin-top:5px}.detail-head button{padding:7px 11px}.detail-body{padding:22px;overflow:auto}.detail-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:20px}.detail-stat{padding:12px;border:1px solid var(--line);border-radius:9px;background:#111b17}.detail-stat span{display:block;color:var(--muted);font-size:9px}.detail-stat strong{display:block;font-size:12px;margin-top:6px;overflow-wrap:anywhere}.detail-section{margin-top:20px}.detail-section h3{font-size:12px;margin:0 0 10px}.event-row{display:grid;grid-template-columns:90px 85px 1fr;gap:10px;padding:10px 0;border-top:1px solid var(--line);font-size:10px}.event-row time,.event-row span{color:var(--muted);font-family:ui-monospace,monospace}.detail-tags{display:flex;flex-wrap:wrap;gap:6px}.detail-tags span{padding:5px 8px;border:1px solid var(--line);border-radius:6px;color:#b9c8be;font-size:10px}.detail-copy{color:#bdc8c1;font-size:11px;line-height:1.75}.detail-warning{padding:9px 11px;border-left:2px solid var(--yellow);background:#171b16;color:#d9c898;font-size:10px;margin:7px 0}.evidence-row{padding:10px 0;border-top:1px solid var(--line);font-size:10px;line-height:1.6}.evidence-row small{display:block;color:var(--muted);font-family:ui-monospace,monospace}details.raw{margin-top:20px;border:1px solid var(--line);border-radius:9px}details.raw summary{padding:11px 13px;cursor:pointer;font-size:10px}pre{margin:0;padding:14px;border-top:1px solid var(--line);max-height:320px;overflow:auto;background:#09100e;color:#a9b8ae;font:9px/1.55 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
    @media(max-width:800px){header{padding:0 16px}.metrics{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.service-table{grid-template-columns:95px 1fr}nav a:first-child{display:none}.detail-grid{grid-template-columns:1fr 1fr}.event-row{grid-template-columns:70px 70px 1fr}}
  </style>
</head>
<body>
  <header><h1><span>●</span> Music Insight / Debug</h1><nav><a href="http://127.0.0.1:5174/">打开正式界面</a><a href="/docs">API 文档</a><a href="/debug/report" download>下载诊断报告</a><button id="refresh">刷新</button></nav></header>
  <main>
    <div class="status-line"><span class="dot"></span><span id="status">正在读取服务状态…</span></div>
    <section class="metrics"><div class="metric"><span>运行中</span><strong id="active">—</strong></div><div class="metric"><span>最近失败</span><strong id="failed">—</strong></div><div class="metric"><span>历史记录</span><strong id="history-count">—</strong></div><div class="metric"><span>问题与警告</span><strong id="issue-count">—</strong></div></section>
    <div class="grid">
      <section class="panel"><h2>任务进度</h2><div id="jobs"></div></section>
      <section class="panel"><h2>服务配置</h2><dl class="service-table" id="service"></dl></section>
      <section class="panel"><h2>最近分析</h2><div id="history"></div></section>
      <section class="panel"><h2>问题反馈</h2><div id="issues"></div></section>
    </div>
  </main>
  <dialog id="task-detail"><div class="detail-head"><div><strong id="detail-title">任务详情</strong><small id="detail-id"></small></div><button id="detail-close">关闭</button></div><div class="detail-body" id="detail-body"></div></dialog>
  <script>
    const esc=value=>String(value??"").replace(/[&<>\"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
    const when=value=>value?new Date(value).toLocaleString("zh-CN"):"—";
    function render(data){
      document.querySelector("#status").textContent=`API 正常 · 最后更新 ${when(data.generated_at)} · 每 3 秒自动刷新`;
      document.querySelector("#active").textContent=data.counts.active;
      document.querySelector("#failed").textContent=data.counts.failed;
      document.querySelector("#history-count").textContent=data.counts.history;
      document.querySelector("#issue-count").textContent=data.counts.issues;
      document.querySelector("#service").innerHTML=Object.entries(data.service).map(([key,value])=>`<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("");
      document.querySelector("#jobs").innerHTML=data.jobs.length?data.jobs.map(job=>`<article class="item"><button class="open-task" data-task-id="${esc(job.id)}"><div class="item-head"><strong>${esc(job.message)}</strong><span class="badge ${esc(job.state)}">${esc(job.state)} · ${Math.round(job.progress*100)}%</span></div><div class="meta">${esc(job.id)} · ${esc(job.stage)} · ${when(job.updated_at)} · 点击查看详情</div><div class="track"><i style="width:${Math.round(job.progress*100)}%"></i></div>${job.error?`<div class="issue error">${esc(job.error)}</div>`:""}</button></article>`).join(""):'<p class="empty">当前进程尚无任务。新分析开始后会实时显示在这里。</p>';
      document.querySelector("#history").innerHTML=data.history.length?data.history.map(item=>`<article class="item"><button class="open-task" data-task-id="${esc(item.id)}"><div class="item-head"><strong>${esc(item.title)}</strong><span class="badge ${esc(item.state)}">${esc(item.state)}</span></div><div class="meta">${when(item.updated_at)} · ${esc(item.model_location||item.model_source)}${item.bpm?` · ${esc(item.bpm)} BPM`:""} · 点击查看详情</div></button></article>`).join(""):'<p class="empty">暂无持久化分析记录。</p>';
      document.querySelector("#issues").innerHTML=data.issues.length?data.issues.map(issue=>`<div class="issue ${esc(issue.severity)}"><strong>${esc(issue.source)}</strong><br>${esc(issue.message)}<small>${when(issue.time)}</small></div>`).join(""):'<p class="empty">最近分析没有报告问题。</p>';
    }
    function stat(label,value){return `<div class="detail-stat"><span>${esc(label)}</span><strong>${esc(value??"—")}</strong></div>`}
    function tags(items){return items?.length?`<div class="detail-tags">${items.map(item=>`<span>${esc(typeof item==="string"?item:item.text)}</span>`).join("")}</div>`:'<p class="empty">暂无</p>'}
    async function openTask(id){
      const dialog=document.querySelector("#task-detail"),body=document.querySelector("#detail-body");body.innerHTML='<p class="empty">正在读取任务详情…</p>';dialog.showModal();
      try{const response=await fetch(`/debug/tasks/${encodeURIComponent(id)}`,{cache:"no-store"});if(!response.ok)throw new Error(`HTTP ${response.status}`);const data=await response.json(),job=data.snapshot||{},history=data.history||{},result=data.result;
        document.querySelector("#detail-title").textContent=history.title||job.message||"任务详情";document.querySelector("#detail-id").textContent=id;
        const metrics=result?.technical_metrics||{};const events=data.events?.length?data.events.map(event=>`<div class="event-row"><time>${when(event.timestamp)}</time><span>${esc(event.stage)} · ${Math.round(event.progress*100)}%</span><strong>${esc(event.message)}${event.error?` · ${esc(event.error)}`:""}</strong></div>`).join(""):'<p class="empty">任务来自服务重启前，没有内存阶段事件。</p>';
        const warnings=result?.warnings?.length?result.warnings.map(item=>`<div class="detail-warning">${esc(item)}</div>`).join(""):'<p class="empty">没有报告 warning</p>';
        const evidence=result?.evidence?.length?result.evidence.slice(0,100).map(item=>`<div class="evidence-row"><strong>${esc(item.text)}</strong><small>${esc(item.source)} · ${esc(item.kind)} · confidence ${esc(item.confidence??"—")}${item.span?` · ${esc(item.span.start_s)}–${esc(item.span.end_s)}s`:""}</small></div>`).join(""):'<p class="empty">暂无证据</p>';
        body.innerHTML=`<div class="detail-grid">${stat("状态",job.state||history.state)}${stat("阶段",job.stage)}${stat("进度",job.progress==null?"—":`${Math.round(job.progress*100)}%`)}${stat("文件",history.file_name)}${stat("语言",history.language||"自动")}${stat("模型",history.model_location||history.model_source)}${stat("BPM",metrics.bpm)}${stat("调性",metrics.key)}${stat("歌词片段",result?.lyrics?.length??0)}</div>${job.error||history.error?`<div class="issue error">${esc(job.error||history.error)}</div>`:""}<section class="detail-section"><h3>阶段事件</h3>${events}</section><section class="detail-section"><h3>分析摘要</h3><p class="detail-copy">${esc(result?.summary||history.summary||"尚无结果")}</p></section><section class="detail-section"><h3>问题与警告</h3>${warnings}</section><section class="detail-section"><h3>乐器 / 主题</h3>${tags([...(result?.instruments||[]),...(result?.themes||[])])}</section><section class="detail-section"><h3>歌词</h3>${tags(result?.lyrics)}</section><section class="detail-section"><h3>证据（最多显示 100 条）</h3>${evidence}</section><details class="raw"><summary>查看原始 JSON</summary><pre>${esc(JSON.stringify(data,null,2))}</pre></details>`;
      }catch(error){body.innerHTML=`<div class="issue error">任务详情读取失败：${esc(error.message)}</div>`}
    }
    async function load(){try{const response=await fetch("/debug/state",{cache:"no-store"});if(!response.ok)throw new Error(`HTTP ${response.status}`);render(await response.json())}catch(error){document.querySelector("#status").textContent=`监控数据读取失败：${error.message}`}}
    document.addEventListener("click",event=>{const button=event.target.closest("[data-task-id]");if(button)openTask(button.dataset.taskId)});document.querySelector("#detail-close").addEventListener("click",()=>document.querySelector("#task-detail").close());document.querySelector("#refresh").addEventListener("click",load);load();setInterval(load,3000);
  </script>
</body>
</html>"""
