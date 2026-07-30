import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";

export type UiLocale = "zh-CN" | "en";

const STORAGE_KEY = "music-insight.ui-locale";

const english: Record<string, string> = {
  "界面语言": "Interface language",
  "切换界面语言": "Change interface language",
  "正在打开本地工作区": "Opening local workspace",
  "正在打开本地工作区…": "Opening local workspace…",
  "登录状态已变化，请重新登录。": "Your sign-in state changed. Please sign in again.",
  "登录状态已过期，请重新登录。": "Your session expired. Please sign in again.",
  "已安全退出本地账号。": "You have safely signed out of the local account.",
  "用户名或密码不正确。": "Incorrect username or password.",
  "这个用户名已经被使用。": "This username is already in use.",
  "用户名至少需要 2 个字符。": "Username must contain at least 2 characters.",
  "密码至少需要 8 个字符。": "Password must contain at least 8 characters.",
  "两次输入的密码不一致。": "The passwords do not match.",
  "登录失败，请稍后重试。": "Sign-in failed. Please try again.",
  "注册失败，请稍后重试。": "Registration failed. Please try again.",
  "你的音乐分析，": "Your music analysis,",
  "只属于你的账号。": "private to your account.",
  "分析记录、音频和演唱成绩按本地用户隔离保存。登录后可以继续已有分析、比较报告，也可以参与演唱最高分榜。":
    "Analyses, audio, and singing scores are stored separately for each local user. Sign in to continue previous work, compare reports, and join the singing leaderboard.",
  "功能概览": "Feature overview",
  "独立历史与音频": "Private history and audio",
  "演唱评分与排行": "Singing scores and ranking",
  "本地优先保存": "Local-first storage",
  "正在连接分析服务": "Connecting to analysis service",
  "分析服务未连接": "Analysis service unavailable",
  "重试连接": "Retry connection",
  "账号操作": "Account actions",
  "登录": "Sign in",
  "创建账号": "Create account",
  "继续你的工作区": "Continue your workspace",
  "创建本地工作区": "Create a local workspace",
  "使用本机账号登录。": "Sign in with an account on this device.",
  "账号只创建在当前这台设备上。": "This account exists only on this device.",
  "用户名": "Username",
  "密码": "Password",
  "再次输入密码": "Confirm password",
  "请稍候…": "Please wait…",
  "登录工作区": "Sign in to workspace",
  "创建并进入": "Create and continue",
  "密码以带盐单向摘要保存；浏览器不会保存明文密码。":
    "Passwords are stored as salted one-way hashes; the browser never stores plaintext passwords.",
  "分析服务在线": "Analysis service online",
  "后端未连接": "Backend disconnected",
  "新建音乐导赏": "New guided listening",
  "双曲对比": "Track comparison",
  "分析详情": "Analysis details",
  "演唱对比": "Singing comparison",
  "演唱记录": "Singing history",
  "个人评分历史": "Personal score history",
  "排行榜": "Leaderboard",
  "演唱最高分": "Top singing scores",
  "打开我的演唱记录": "Open my singing history",
  "打开演唱排行榜": "Open singing leaderboard",
  "开始方式": "Start mode",
  "音乐分析": "Music analysis",
  "上传歌曲并生成完整报告": "Upload a track and generate a complete report",
  "独立演唱对比": "Standalone singing comparison",
  "参考音频与个人录音直接打分": "Score a recording against a reference",
  "出现问题": "Something went wrong",
  "连接恢复中": "Reconnecting",
  "本地优先的音乐证据分析": "Local-first, evidence-based music analysis",
  "无法读取后端运行配置": "Could not read backend runtime configuration",
  "退出失败，请稍后再试": "Sign-out failed. Please try again.",
  "NEW LISTENING SESSION": "NEW LISTENING SESSION",
  "先听懂整首歌，": "Understand the whole track first,",
  "再追问每个瞬间。": "then explore every moment.",
  "上传歌曲后，从歌词、段落与声音证据出发，生成可以边听边问、随时跳转复听的音乐导赏。":
    "Upload a track to build a guided listening experience from lyrics, sections, and audible evidence—then ask questions and jump back to any moment.",
  "分析流程": "Analysis flow",
  "理解全曲": "Understand the whole track",
  "气氛、结构与情绪弧线": "Atmosphere, structure, and emotional arc",
  "定位证据": "Locate the evidence",
  "歌词、乐器与声音变化": "Lyrics, instruments, and sonic changes",
  "带着问题复听": "Listen again with a question",
  "时间地图与持续对话": "Timeline map and ongoing conversation",
  "音频来源": "Audio source",
  "本地文件": "Local file",
  "直接音频链接": "Direct audio link",
  "点击更换": "Click to replace",
  "拖放音频到这里": "Drop audio here",
  "或点击选择 WAV、MP3、FLAC、M4A、OGG": "or click to choose WAV, MP3, FLAC, M4A, or OGG",
  "公开的直接音频 URL": "Public direct audio URL",
  "仅支持直接返回音频的公网 HTTP(S) 链接。不会抓取 YouTube 等网页，也不会绕过登录、版权、付费或 DRM 限制。":
    "Only public HTTP(S) URLs that return audio directly are supported. Web pages such as YouTube are not scraped, and sign-in, copyright, paywall, or DRM restrictions are never bypassed.",
  "歌词语言": "Lyrics language",
  "自动识别": "Auto-detect",
  "中文": "Chinese",
  "分析进行中": "Analyzing",
  "开始分析": "Start analysis",
  "等待处理": "Waiting",
  "启动分析": "Starting analysis",
  "音频预处理": "Audio preprocessing",
  "声学计算": "Acoustic analysis",
  "模型聆听": "Model listening",
  "等待模型": "Waiting for model",
  "模型综合": "Model synthesis",
  "证据融合": "Evidence fusion",
  "整理报告": "Preparing report",
  "分析完成": "Analysis complete",
  "分析失败": "Analysis failed",
  "已取消": "Cancelled",
  "音乐分析进度": "Music analysis progress",
  "取消任务": "Cancel task",
  "模型设置": "Model settings",
  "模型": "Model",
  "本地权重": "Local weights",
  "影响后续新分析": "Applies to future analyses",
  "模型来源": "Model source",
  "模型接口": "Model endpoint",
  "模型预设": "Model presets",
  "默认 OpenAI 音频接口": "Default OpenAI audio endpoint",
  "Comni Gateway 音频接口": "Comni Gateway audio endpoint",
  "自定义": "Custom",
  "自动探测服务协议": "Auto-detect service protocol",
  "模型服务地址": "Model service URL",
  "8005 使用 Comni WebSocket；服务会自动选择专用音频协议。":
    "Port 8005 uses Comni WebSocket; the service automatically selects its dedicated audio protocol.",
  "留空使用后端默认地址；其他地址会自动探测 OpenAI 或专用 Gateway 协议。":
    "Leave blank to use the backend default. Other addresses are probed for OpenAI-compatible or dedicated Gateway protocols.",
  "正在测试…": "Testing…",
  "测试模型连接": "Test model connection",
  "连接失败": "Connection failed",
  "模型连接测试失败": "Model connection test failed",
  "本地模型目录或主 GGUF 路径": "Local model directory or main GGUF path",
  "允许目录：": "Allowed directory:",
  "自动配对 mmproj。": "mmproj is paired automatically.",
  "当前未检测到 llama-server。": "llama-server is not currently available.",
  "分析进行中，模型设置暂时锁定": "Model settings are locked while analysis is running",
  "GUIDED LISTENING READY": "GUIDED LISTENING READY",
  "纯器乐": "Instrumental",
  "有人声": "Vocals",
  "未确认": "Unconfirmed",
  "可信度": "Confidence",
  "候选": "Candidates",
  "段歌词": "lyric segments",
  "同步歌词": "Synchronized lyrics",
  "歌词修订版本": "Lyrics revision",
  "当前版本": "Current version",
  "修订前": "Before revision",
  "取消": "Cancel",
  "保存中…": "Saving…",
  "保存修订": "Save revision",
  "校对歌词": "Edit lyrics",
  "纯器乐模式": "Instrumental mode",
  "导赏将优先关注主题材料、声部、织体、和声、力度与音色。如果判断有误，仍可在这里人工添加歌词进行纠正。":
    "The guide will focus on thematic material, voices, texture, harmony, dynamics, and timbre. If this classification is wrong, you can still add lyrics manually.",
  "自动质量检查处理了": "Automatic quality checks processed",
  "个分块": "chunks",
  "初次结果": "Initial result",
  "重听结果": "Re-listen result",
  "未获得可靠替代结果": "No reliable replacement was found",
  "模型正在重新聆听…": "The model is listening again…",
  "重新聆听此分块": "Re-listen to this chunk",
  "重听预览": "Re-listen preview",
  "本次重听没有确认到可靠歌词，不会覆盖当前结果。":
    "This pass did not confirm reliable lyrics, so the current result will not be replaced.",
  "放弃": "Discard",
  "应用并保存为新版本": "Apply and save as a new version",
  "添加歌词行": "Add lyric line",
  "已重听": "Re-listened",
  "重新聆听": "Re-listen",
  "没有确认到可靠歌词": "No reliable lyrics confirmed",
  "无法读取修订历史": "Could not load revision history",
  "歌词不能为空，结束时间也不能早于开始时间。":
    "Lyrics cannot be empty, and the end time cannot be earlier than the start time.",
  "歌词保存失败": "Could not save lyrics",
  "分块重听失败": "Could not re-listen to this chunk",
  "重听结果保存失败": "Could not save the re-listen result",
  "第": "Line",
  "行开始时间": "start time",
  "行结束时间": "end time",
  "行歌词": "lyrics",
  "删除第": "Delete line",
  "分析提醒": "Analysis notice",
  "这份旧分析没有保存人声状态，需要重新分析后确认。":
    "This older analysis did not save vocal-presence data. Re-run it to confirm.",
  "暂无可靠结果": "No reliable result",
  "技术证据附录": "Technical evidence appendix",
  "乐器、主题、情绪和氛围推断": "Instruments, themes, emotion, and atmosphere",
  "展开": "Expand",
  "乐器与声源": "Instruments and sound sources",
  "没有确认到具体乐器": "No specific instrument confirmed",
  "主题": "Themes",
  "直接情绪证据": "Direct emotion evidence",
  "来自音色、力度与演唱方式": "From timbre, dynamics, and vocal delivery",
  "模型没有确认直接情绪；这不等于音乐没有氛围。":
    "The model did not confirm direct emotion evidence; this does not mean the music has no atmosphere.",
  "推断氛围": "Inferred atmosphere",
  "非直接听觉证据": "Not a direct audible fact",
  "由歌词、节奏和声音描述综合推断": "Inferred from lyrics, rhythm, and sound descriptions",
  "证据不足，未生成推断氛围": "Insufficient evidence to infer atmosphere",
  "排队中": "Queued",
  "分析中": "Analyzing",
  "已完成": "Completed",
  "失败": "Failed",
  "本地账号": "Local account",
  "独立本机工作区": "Private workspace on this device",
  "我的演唱记录": "My singing history",
  "仅显示当前账号保存的成绩": "Only scores saved by the current account",
  "刷新中…": "Refreshing…",
  "关闭演唱记录": "Close singing history",
  "正在读取个人演唱记录…": "Loading personal singing history…",
  "暂时无法读取演唱记录": "Could not load singing history",
  "还没有演唱成绩": "No singing scores yet",
  "完成一次分析歌曲演唱或独立演唱对比后，记录会出现在这里。":
    "Records appear here after scoring a performance from an analysis or a standalone comparison.",
  "重新同步": "Sync again",
  "打开参考分析": "Open reference analysis",
  "删除中…": "Deleting…",
  "已加载": "Loaded",
  "条记录": "records",
  "加载中…": "Loading…",
  "参考：": "Reference:",
  "演唱：": "Performance:",
  "未保留音频文件名": "Audio filenames were not retained",
  "确定删除": "Delete",
  "的演唱成绩吗？": "singing score?",
  "删除后排行榜也会立即按剩余成绩重新计算。":
    "The leaderboard will immediately be recalculated from the remaining scores.",
  "演唱记录加载失败": "Could not load singing history",
  "演唱记录删除失败": "Could not delete singing record",
  "录音设备发生错误，请重试。": "The recording device reported an error. Please try again.",
  "请求失败": "Request failed",
  "退出登录": "Sign out",
  "的账号菜单": "account menu",
  "新分析": "New analysis",
  "分析历史": "Analysis history",
  "默认 8004": "Default 8004",
  "加入对比": "Add to comparison",
  "对比": "Compare",
  "分析完成后会保存在这里": "Completed analyses will appear here",
  "对比分析": "Compare analyses",
  "仅显示": "Only showing",
  "的本机记录": "’s local records",
  "时长": "Duration",
  "调性": "Key",
  "形式": "Format",
  "歌词": "Lyrics",
  "乐器": "Instruments",
  "直接情绪": "Direct emotion",
  "个片段": "segments",
  "并排比较": "Side-by-side comparison",
  "指标": "Metric",
  "暂无摘要": "No summary",
  "重命名": "Rename",
  "正在删除": "Deleting",
  "删除": "Delete",
  "无法读取历史分析": "Could not load analysis history",
  "无法读取对比结果": "Could not load comparison results",
  "请先取消正在运行的任务": "Cancel the running task first",
  "删除失败": "Delete failed",
  "重命名失败": "Rename failed",
  "确定永久删除“{{title}}”吗？": "Permanently delete “{{title}}”?",
  "分析记录和关联的源音频将被删除，此操作无法撤销。":
    "The analysis and its source audio will be deleted. This action cannot be undone.",
  "重命名分析": "Rename analysis",
  "歌曲波形；拖动可选择最多 30 秒": "Track waveform; drag to select up to 30 seconds",
  "拖动波形框选，点击段落或回答中的时间可跳转":
    "Drag on the waveform to select a range; click a section or answer timestamp to jump",
  "波形生成失败": "Waveform generation failed",
  "播放器": "player",
  "正在准备可框选波形…": "Preparing selectable waveform…",
  "波形暂不可用，仍可使用下方时间输入选区。":
    "Waveform unavailable; use the time fields below to select a range.",
  "退出": "Exit",
  "选区播放": "Play selection",
  "选区循环": "Loop selection",
  "选取当前 15 秒": "Select current 15 seconds",
  "开始": "Start",
  "结束": "End",
  "播放选区": "Play selection",
  "循环选区": "Loop selection",
  "设为 A": "Set as A",
  "设为 B": "Set as B",
  "未设置": "Not set",
  "播放 A": "Play A",
  "播放 B": "Play B",
  "连续播放 A→B": "Play A → B",
  "正在播放 A": "playing A",
  "正在播放 B": "playing B",
  "循环播放这段音频": "Loop this audio range",
  "跳转并播放这段音频": "Jump to and play this range",
  "音乐理解地图": "Music understanding map",
  "按时间复听：感受、声音事实、表达作用与开放理解":
    "Revisit the track by time: feeling, audible facts, expressive role, and open interpretation",
  "个理解节点": "listening nodes",
  "理解节点": "Listening node",
  "感受到什么": "What you may feel",
  "听到了什么": "What you can hear",
  "它在表达中起什么作用": "Its expressive role",
  "附近歌词": "Nearby lyrics",
  "立即复听任务": "Listen-again task",
  "循环完成任务": "Loop to complete the task",
  "其他可能理解": "Other possible readings",
  "解释的证据支持度": "Evidence support for this reading",
  "当前歌曲缺少足够的时间证据，暂时没有生成详细理解节点。":
    "This track does not yet have enough time-based evidence for detailed listening nodes.",
  "这首歌在表达什么？": "What is this track expressing?",
  "证据支持度": "Evidence support",
  "不是“唯一答案概率”": "Not the probability of one correct answer",
  "整体意境": "Overall atmosphere",
  "情绪发展弧线": "Emotional arc",
  "支持度": "Support",
  "段落在承担什么作用？": "What role does each section play?",
  "也可能理解为：": "It may also be heard as:",
  "最值得重听的关键时刻": "Key moments worth revisiting",
  "重听": "Replay",
  "任务：": "Task:",
  "查看导赏的不确定性说明": "View uncertainty notes",
  "导赏老师": "Listening guide",
  "证据不足模式": "limited-evidence mode",
  "已局部重听": "segment re-analyzed",
  "基于已有证据": "based on existing evidence",
  "听觉证据": "Audible evidence",
  "马上复听": "Listen again now",
  "循环这个任务": "Loop this task",
  "回答的不确定性与降级说明": "Answer uncertainty and fallback notes",
  "可以继续问": "Suggested follow-ups",
  "边听边问": "Ask while listening",
  "问": "Ask",
  "刚开始认真听音乐": "Beginning to listen closely",
  "会主动比较声音变化": "I compare changes in sound",
  "了解一些常见乐理": "I know some common music theory",
  "有较系统的音乐基础": "I have a systematic music background",
  "未确认段落": "Unconfirmed section",
  "未指定片段时，会以当前位置自动建立 15 秒范围。":
    "When no range is selected, a 15-second range is created around the current position.",
  "请解释当前这 15 秒：我应该先听什么，以及这些声音产生了什么表达作用？":
    "Explain these 15 seconds: what should I listen for first, and what expressive effect do these sounds create?",
  "已自动选取当前位置附近 15 秒，可在播放器中继续调整。":
    "A 15-second range around the current position was selected automatically. You can adjust it in the player.",
  "将携带播放器中已经框选的时间范围。": "The selected player range will be included.",
  "已自动建立相邻的 A/B 两段，可在播放器中重新框选并覆盖。":
    "Adjacent A/B ranges were created automatically. You can replace them in the player.",
  "将比较播放器中已经设置的 A/B 两段。": "The A/B ranges set in the player will be compared.",
  "请比较 A/B 两段的声音变化和表达作用。":
    "Compare the sonic changes and expressive roles of ranges A and B.",
  "无法更新音乐基础设置": "Could not update music experience",
  "我的音乐基础": "My music experience",
  "已保留原来的音乐基础设置。": "Your previous music experience setting was preserved.",
  "歌曲对话": "Song conversations",
  "新对话": "New conversation",
  "导赏对话": "Listening conversation",
  "询问框选片段": "Ask about selection",
  "比较 A/B": "Compare A/B",
  "跟随当前位置": "Follow current position",
  "正在读取歌曲对话…": "Loading song conversation…",
  "从你的感受开始也可以": "You can start with how it feels",
  "例如：“我觉得这里突然变得很开阔，具体是什么声音造成的？”":
    "For example: “This suddenly feels more spacious—what sounds create that feeling?”",
  "回答失败": "Answer failed",
  "老师正在整理附近的听觉证据…": "The listening guide is organizing nearby audible evidence…",
  "正在重试…": "Retrying…",
  "重试同一问题": "Retry same question",
  "你的问题或理解": "Your question or interpretation",
  "将比较 A/B 两段": "Comparing ranges A and B",
  "将携带当前框选范围": "Including the current selection",
  "将携带当前播放位置": "Including the current playback position",
  "我为什么会在这里产生这种感觉？": "Why does this moment make me feel this way?",
  "正在聆听证据…": "Listening to the evidence…",
  "提问": "Ask",
  "当前播放位置": "Current position",
  "框选片段": "Selected range",
  "解释当前 15 秒": "Explain the current 15 seconds",
  "比较两个时间段": "Compare two time ranges",
  "发送": "Send",
  "正在回答…": "Answering…",
  "创建新对话": "New conversation",
  "删除对话": "Delete conversation",
  "还没有对话": "No conversations yet",
  "音乐基础": "Music experience",
  "入门": "Beginner",
  "进阶": "Intermediate",
  "专业": "Advanced",
  "基础证据地图与边听边问已经可用。": "The evidence map and listening chat are ready.",
  "保存分析后，开始可交互导赏": "Save the analysis to start an interactive guide",
  "导赏地图和歌曲对话需要一个稳定的歌曲 ID；分析保存后即可生成，不会重复分析整首歌曲。":
    "The guide map and song conversation need a stable track ID. They become available after saving without re-analyzing the entire track.",
  "正在读取这首歌的导赏地图…": "Loading this track’s listening guide…",
  "基础分析仍可正常查看和播放。": "The base analysis remains available to view and play.",
  "正在准备可交互的音乐导赏": "Preparing an interactive listening guide",
  "把分析变成一堂可复听的音乐导赏课": "Turn this analysis into a replayable listening lesson",
  "正在整理现有歌词、DSP 与时间证据；完成后会自动打开边听边问，不会重复提交。":
    "Organizing existing lyrics, DSP, and time-based evidence. Listening chat opens automatically when ready without duplicate submissions.",
  "系统会先用现有歌词、DSP 与带时间的听觉证据快速建立基础地图；失败时不会影响原有分析。":
    "The system first builds a basic map from existing lyrics, DSP, and time-based evidence. A failure will not affect the original analysis.",
  "正在整理时间证据…": "Organizing time-based evidence…",
  "立即准备基础导赏": "Prepare a basic guide",
  "当前地图可用，模型正在增强": "The current map is ready; model enhancement is running",
  "导赏地图需要更新": "The guide map needs an update",
  "部分功能暂不可用": "Some features are temporarily unavailable",
  "边听边问会继续使用现有时间证据，增强完成后自动采用新地图。":
    "Listening chat will continue using current time-based evidence and adopt the enhanced map automatically.",
  "歌词或基础分析已经变化，旧地图仍可查看，但不再作为最新证据。":
    "Lyrics or the base analysis changed. The old map remains viewable but is no longer the latest evidence.",
  "正在重建…": "Rebuilding…",
  "按最新证据重建": "Rebuild from latest evidence",
  "模型正在增强…": "Model enhancement in progress…",
  "用当前模型增强导赏": "Enhance guide with current model",
  "我的听觉训练进度": "My listening progress",
  "只有你确认“已经能听出”后才会记录；系统不会因为回答过一次就自动判定学会。":
    "A concept is recorded only after you confirm you can hear it; one answered question never marks it as learned automatically.",
  "下一项建议关注：": "Suggested next focus:",
  "地图": "Map",
  "个核心概念": "core concepts",
  "段落结构": "Section structure",
  "节奏与律动": "Rhythm and groove",
  "旋律走向": "Melodic contour",
  "和声色彩": "Harmonic color",
  "音色": "Timbre",
  "力度变化": "Dynamic change",
  "配器层次": "Instrumentation layers",
  "空间感": "Space",
  "歌词与音乐关系": "Lyrics–music relationship",
  "直接听觉事实": "Direct audible fact",
  "DSP 计算事实": "DSP-computed fact",
  "有依据的解释": "Evidence-grounded interpretation",
  "可能的理解": "Possible reading",
  "旋律": "Melody",
  "和声": "Harmony",
  "节奏": "Rhythm",
  "力度": "Dynamics",
  "配器": "Instrumentation",
  "曲式": "Form",
  "其他": "Other",
  "分轨独听 / 静音": "Stem solo / mute",
  "正在检查分轨缓存…": "Checking stem cache…",
  "分轨独听暂不可用": "Stem listening is unavailable",
  "服务端没有可用的分轨后端。": "No stem-separation backend is available.",
  "正在生成四个分轨": "Generating four stems",
  "首次运行可能需要下载模型；完成后会自动刷新。":
    "The first run may need to download a model. This view refreshes automatically when ready.",
  "生成可单独控制的人声、鼓、低音和其他乐器轨。":
    "Generate independently controllable vocal, drum, bass, and other stems.",
  "正在分离，可能需要数分钟…": "Separating stems; this may take several minutes…",
  "生成四轨": "Generate four stems",
  "主播放器继续控制时间与 A/B 循环": "The main player still controls time and A/B looping",
  "还原原曲": "Restore original mix",
  "启用分轨混音": "Enable stem mix",
  "正在载入四轨…": "Loading stems…",
  "纯器乐提示": "Instrumental note",
  "独听": "Solo",
  "静音": "Mute",
  "分轨是模型估计结果，复杂混音中可能出现串音或少量伪影。":
    "Stems are model estimates; dense mixes may contain bleed or minor artifacts.",
  "无法读取分轨状态": "Could not read stem status",
  "分轨生成失败": "Stem generation failed",
  "{{track}}分轨无法载入。": "Could not load the {{track}} stem.",
  "古典音乐通常主要落在“其他乐器”和“低音”；“人声”轨若有声音，更可能是串音或分离伪影，不代表作品包含歌唱。":
    "Classical music usually appears mainly in “Other” and “Bass.” Sound in the “Vocals” stem is more likely bleed or a separation artifact, not proof of singing.",
  "人声": "Vocals",
  "鼓": "Drums",
  "低音": "Bass",
  "其他乐器": "Other",
  "综合得分": "Overall score",
  "音准": "Pitch",
  "完整度": "Completeness",
  "稳定性": "Stability",
  "音高误差时间轴": "Pitch error timeline",
  "无可比音高": "No comparable pitch",
  "偏差": "Deviation",
  "半音": "semitones",
  "音高误差中位数：": "Median pitch error:",
  "证据不足": "Insufficient evidence",
  "半音内命中": "Within one semitone",
  "参考": "Reference",
  "演唱": "Performance",
  "优先练习的时间段": "Priority practice ranges",
  "本地声学评分 · 不由大模型决定总分": "Local acoustic scoring · the overall score is not decided by an LLM",
  "等待麦克风授权…": "Waiting for microphone permission…",
  "正在生成录音…": "Preparing recording…",
  "正在对齐音高与节奏…": "Aligning pitch and rhythm…",
  "无法使用麦克风": "Could not access the microphone",
  "无法停止录音": "Could not stop recording",
  "演唱评分失败": "Singing score failed",
  "演唱对比失败": "Singing comparison failed",
  "开始评分": "Start scoring",
  "上传录音": "Upload recording",
  "开始演唱": "Start singing",
  "停止录音": "Stop recording",
  "上传示范，再唱一遍": "Upload a reference, then sing it",
  "分别提供参考音频和你的演唱。系统会自动去除首尾静音、统一时间尺度，并比较音准、节奏、完整度与稳定性。":
    "Provide a reference and your performance. The system trims leading and trailing silence, aligns timing, and compares pitch, rhythm, completeness, and stability.",
  "参考音频": "Reference audio",
  "原唱、标准示范或纯人声均可。纯人声参考通常最准确。":
    "Use an original vocal, a standard demonstration, or isolated vocals. A clean vocal reference is usually most accurate.",
  "更换参考音频": "Replace reference audio",
  "上传参考音频": "Upload reference audio",
  "你的演唱": "Your performance",
  "可以上传已有录音，也可以直接使用浏览器麦克风录制。":
    "Upload an existing recording or record directly with your browser microphone.",
  "更换录音": "Replace recording",
  "麦克风录音": "Record with microphone",
  "两份音频只用于本次计算": "Both audio files are used only for this comparison",
  "评分完成后，后端会立即删除临时文件。":
    "The backend deletes temporary files immediately after scoring.",
  "正在提取旋律并对齐…": "Extracting and aligning melodies…",
  "开始打分对比": "Score and compare",
  "刷新": "Refresh",
  "重新加载": "Reload",
  "加载更多": "Load more",
  "已经到底了": "End of results",
  "演唱最高分榜": "Top singing scores",
  "关闭排行榜": "Close leaderboard",
  "把我的最高分加入排行榜": "Include my best score on the leaderboard",
  "娱乐最高分榜，每人仅取最高分；不同参考音频仅供娱乐。":
    "For fun only: each user’s best score is shown, and scores from different references are not directly comparable.",
  "默认不公开；关闭后会立即从其他用户可见的榜单移除。":
    "Private by default. Turning this off removes your score from other users’ leaderboard immediately.",
  "排行榜加载失败": "Could not load leaderboard",
  "排行榜隐私设置失败": "Could not update leaderboard privacy",
  "正在汇总最高成绩…": "Collecting top scores…",
  "名次 / 用户": "Rank / user",
  "四项得分": "Score breakdown",
  "最高分": "Best score",
  "分析记录演唱": "Singing from an analysis",
  "演唱评分": "Singing score",
  "分析歌曲演唱": "Singing from an analyzed track",
  "次评分": "attempts",
  "完整": "Complete",
  "稳定": "Stable",
  "排行榜还没有成绩": "No leaderboard scores yet",
  "完成一次演唱打分后，最高分会出现在这里。":
    "Your best score will appear here after completing a singing comparison.",
  "无法准备基础音乐导赏": "Could not prepare the basic listening guide",
  "无法读取音乐导赏地图": "Could not load the listening guide map",
  "无法读取听觉学习档案": "Could not load the listening profile",
  "导赏地图生成失败": "Could not generate the listening guide map",
  "无法保存听觉训练进度": "Could not save listening progress",
  "无法读取歌曲对话": "Could not load song conversations",
  "无法读取对话消息": "Could not load conversation messages",
  "无法创建歌曲对话": "Could not create a song conversation",
  "无法删除歌曲对话": "Could not delete the song conversation",
  "导赏回答失败": "The listening guide could not answer",
  "无法读取分析结果": "Could not load the analysis result",
  "取消失败": "Could not cancel the task",
  "当前分析尚未保存到历史记录。": "This analysis has not been saved to history yet.",
  "后端没有返回修订结果。": "The backend did not return a revised result.",
  "无法创建分析任务": "Could not create the analysis task",
  "远程音频": "Remote audio",
  "收到无法解析的进度消息，正在重新同步…":
    "Received an unreadable progress event; synchronizing again…",
  "进度连接暂时中断，正在自动恢复…":
    "The progress connection was interrupted and is recovering automatically…",
  "你": "You",
  "原始证据使用另一种语言；这里保留其类型、时间与支持度。":
    "The source evidence uses another language; its type, time, and support level are preserved here.",
};

function interpolate(template: string, values?: Record<string, string | number>) {
  if (!values) return template;
  return template.replace(/\{\{(\w+)\}\}/g, (_, key: string) => (
    values[key] == null ? "" : String(values[key])
  ));
}

function dynamicEnglish(source: string): string | null {
  const backend = /^后端返回 HTTP (\d+):\s*(.*)$/s.exec(source);
  if (backend) return `Backend returned HTTP ${backend[1]}: ${backend[2]}`;
  return null;
}

export interface I18nValue {
  locale: UiLocale;
  setLocale: (locale: UiLocale) => void;
  t: (source: string, values?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nValue | null>(null);

function storedLocale(): UiLocale {
  if (typeof window === "undefined") return "zh-CN";
  return window.localStorage.getItem(STORAGE_KEY) === "en" ? "en" : "zh-CN";
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<UiLocale>(storedLocale);

  useEffect(() => {
    document.documentElement.lang = locale;
    window.localStorage.setItem(STORAGE_KEY, locale);
  }, [locale]);

  const setLocale = useCallback((next: UiLocale) => {
    setLocaleState(next);
  }, []);

  const t = useCallback((
    source: string,
    values?: Record<string, string | number>,
  ) => interpolate(
    locale === "en" ? english[source] || dynamicEnglish(source) || source : source,
    values,
  ), [
    locale,
  ]);

  const value = useMemo(() => ({ locale, setLocale, t }), [
    locale,
    setLocale,
    t,
  ]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n must be used inside I18nProvider");
  return value;
}

export function matchesUiLanguage(
  text: string,
  locale: UiLocale,
): boolean {
  const cjk = (text.match(/[\u3400-\u9fff]/g) || []).length;
  const latin = (text.match(/[A-Za-z]/g) || []).length;
  if (locale === "en") return cjk === 0;
  return cjk >= 2 && !(latin >= 18 && latin > cjk * 2);
}

export function LanguageSwitcher({ compact = false }: { compact?: boolean }) {
  const { locale, setLocale, t } = useI18n();
  return (
    <div
      className={`language-switcher${compact ? " compact" : ""}`}
      role="group"
      aria-label={t("切换界面语言")}
      title={t("界面语言")}
    >
      <button
        type="button"
        className={locale === "zh-CN" ? "active" : ""}
        aria-pressed={locale === "zh-CN"}
        onClick={() => setLocale("zh-CN")}
      >
        中文
      </button>
      <button
        type="button"
        className={locale === "en" ? "active" : ""}
        aria-pressed={locale === "en"}
        onClick={() => setLocale("en")}
      >
        EN
      </button>
    </div>
  );
}
