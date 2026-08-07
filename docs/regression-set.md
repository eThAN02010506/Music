# 中英文回归集

对应 PRD 阶段 B「为中文和英文建立固定回归集」。这些样例音频与问题用于在
接入新 Provider、升级解析逻辑或改动双语契约后，快速验证中英文质量没有回退。
音频位于 `test_samples/`（本地提供，不随 Git 分发）。

## 样例音频

| 语言 | 样例 | 特征 |
| --- | --- | --- |
| 中文 | `chinese_internationale_16k.wav` | 人声 + 管弦乐，革命歌曲 |
| 中文 | `我宁愿有耶稣_16k单声道.wav` | 人声 + 钢琴/吉他等，圣诗 |
| 英文 | `johnny_cash_new_mexico_30s.wav` | 人声 + 吉他，乡村叙事 |
| 英文 | `english_rule_britannia_16k.wav` | 人声合唱 + 铜管 |
| 纯器乐 | `beethoven_eroica_3min_16k_mono.wav` | 管弦乐（验证纯器乐边界） |
| 纯打击 | `raksan_bendir_120bpm_cc0.ogg` | 手鼓独奏（最极端纯器乐） |

## 每语言固定问题集

### 中文

- 这里主要有哪些乐器？
- 这段的情绪或气氛是什么样的？
- 这段里最先发生变化的是什么？
- 这一部分在全曲中起什么作用？
- 这里唱了什么内容？

### 英文

- What instruments are most prominent here?
- How would you describe the mood or atmosphere of this passage?
- What changes first in this passage?
- What role does this section play in the whole piece?
- What is being sung here?

### 跨语言契约检查

- 界面语言为中文时，回答正文（answer、时间标签、复听任务、追问、不确定性）
  应为中文；英文同理。引用的歌词原文可保留原语言。
- 同一问题切换界面语言后，回答应整体切换语言，不混排。

## 执行方式

- 对每个样例建对话，用上面的固定问题逐条提问（`relisten_policy=never`），
  检查：无结构化输出拒绝、无保守降级、回答语言与界面一致、引用证据在范围内。
- 记录降级率与结构化输出成功率到 `docs/provider-matrix.md`。
- 脚本参考：`scripts/provider_matrix.py`（分析级）、后续可扩展对话级回归。
