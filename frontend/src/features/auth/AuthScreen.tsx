import { useState } from "react";
import type { FormEvent, KeyboardEvent as ReactKeyboardEvent } from "react";
import {
  api,
  ApiError,
  API_BASE,
} from "../../api";
import { announceAuthChange } from "../../authSession";
import { SignalMark } from "../../components/SignalMark";
import { tabIndexAfterKey } from "../../hooks/tabKeyboard";
import type { HealthResult, User } from "../../types";

function readableApiError(cause: unknown, fallback: string) {
  if (!(cause instanceof ApiError)) {
    return cause instanceof Error ? cause.message : fallback;
  }
  if (cause.status === 401) return "用户名或密码不正确。";
  if (cause.status === 409) return "这个用户名已经被使用。";
  try {
    const parsed = JSON.parse(cause.detail) as {
      detail?: string | Array<{ msg?: string }>;
    };
    if (typeof parsed.detail === "string") return parsed.detail;
    const first = Array.isArray(parsed.detail) ? parsed.detail[0]?.msg : "";
    if (first) return first.replace(/^Value error,\s*/i, "");
  } catch {
    // The backend may return a plain-text error.
  }
  return fallback;
}

export function AuthScreen({
  health,
  healthChecked,
  healthChecking,
  notice,
  onRetryHealth,
  onAuthenticated,
}: {
  health: HealthResult | null;
  healthChecked: boolean;
  healthChecking: boolean;
  notice: string;
  onRetryHealth: () => void;
  onAuthenticated: (user: User) => void;
}) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const switchMode = (next: "login" | "register") => {
    setMode(next);
    setPassword("");
    setConfirmPassword("");
    setError("");
  };

  const handleTabKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
  ) => {
    const modes = ["login", "register"] as const;
    const currentIndex = modes.indexOf(mode);
    const nextIndex = tabIndexAfterKey(currentIndex, modes.length, event.key);
    if (nextIndex === null) return;
    event.preventDefault();
    switchMode(modes[nextIndex]);
    const tabs = event.currentTarget.parentElement
      ?.querySelectorAll<HTMLButtonElement>('[role="tab"]');
    tabs?.[nextIndex]?.focus();
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const cleanUsername = username.trim();
    if (cleanUsername.length < 2) {
      setError("用户名至少需要 2 个字符。");
      return;
    }
    if (password.length < 8) {
      setError("密码至少需要 8 个字符。");
      return;
    }
    if (mode === "register" && password !== confirmPassword) {
      setError("两次输入的密码不一致。");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const user = mode === "login"
        ? await api.login(cleanUsername, password)
        : await api.register(cleanUsername, password);
      announceAuthChange();
      onAuthenticated(user);
    } catch (cause) {
      setError(readableApiError(
        cause,
        mode === "login" ? "登录失败，请稍后重试。" : "注册失败，请稍后重试。",
      ));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="auth-shell">
      <section className="auth-intro">
        <div className="auth-brand"><SignalMark /><span>Music Insight</span></div>
        <div>
          <span className="section-kicker">LOCAL MUSIC WORKSPACE</span>
          <h1>你的音乐分析，<br />只属于你的账号。</h1>
          <p>
            分析记录、音频和演唱成绩按本地用户隔离保存。
            登录后可以继续已有分析、比较报告，也可以参与演唱最高分榜。
          </p>
        </div>
        <div className="auth-points" aria-label="功能概览">
          <span><i>01</i> 独立历史与音频</span>
          <span><i>02</i> 演唱评分与排行</span>
          <span><i>03</i> 本地优先保存</span>
        </div>
      </section>
      <section className="auth-card" aria-labelledby="auth-title">
        <div className="auth-service">
          <span className={health
            ? "online"
            : healthChecking || !healthChecked
              ? "checking"
              : "offline"} />
          <div>
            <strong>{health
              ? "分析服务在线"
              : healthChecking || !healthChecked
                ? "正在连接分析服务"
                : "分析服务未连接"}</strong>
            <small>{API_BASE}</small>
          </div>
          {!health && healthChecked && !healthChecking && (
            <button
              type="button"
              className="auth-health-retry"
              onClick={onRetryHealth}
            >
              重试连接
            </button>
          )}
        </div>
        <div className="auth-tabs" role="tablist" aria-label="账号操作">
          <button
            type="button"
            role="tab"
            id="auth-tab-login"
            aria-controls="auth-panel"
            aria-selected={mode === "login"}
            tabIndex={mode === "login" ? 0 : -1}
            className={mode === "login" ? "active" : ""}
            onClick={() => switchMode("login")}
            onKeyDown={handleTabKeyDown}
          >
            登录
          </button>
          <button
            type="button"
            role="tab"
            id="auth-tab-register"
            aria-controls="auth-panel"
            aria-selected={mode === "register"}
            tabIndex={mode === "register" ? 0 : -1}
            className={mode === "register" ? "active" : ""}
            onClick={() => switchMode("register")}
            onKeyDown={handleTabKeyDown}
          >
            创建账号
          </button>
        </div>
        <div
          id="auth-panel"
          role="tabpanel"
          aria-labelledby={`auth-tab-${mode}`}
        >
          <div className="auth-card-copy">
            <span className="section-kicker">{mode === "login" ? "WELCOME BACK" : "NEW LOCAL USER"}</span>
            <h2 id="auth-title">{mode === "login" ? "继续你的工作区" : "创建本地工作区"}</h2>
            <p>{mode === "login" ? "使用本机账号登录。" : "账号只创建在当前这台设备上。"}</p>
          </div>
          <form onSubmit={(event) => void submit(event)}>
            <label>
              <span>用户名</span>
              <input
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                minLength={2}
                maxLength={40}
                disabled={submitting}
                autoFocus
              />
            </label>
            <label>
              <span>密码</span>
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                minLength={8}
                maxLength={128}
                disabled={submitting}
              />
            </label>
            {mode === "register" && (
              <label>
                <span>再次输入密码</span>
                <input
                  type="password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  autoComplete="new-password"
                  minLength={8}
                  maxLength={128}
                  disabled={submitting}
                />
              </label>
            )}
            {(error || notice) && (
              <p className={error ? "auth-error" : "auth-notice"} role="status">
                {error || notice}
              </p>
            )}
            <button
              type="submit"
              className="auth-submit"
              disabled={submitting || !health}
            >
              {submitting ? "请稍候…" : mode === "login" ? "登录工作区" : "创建并进入"}
              <span>→</span>
            </button>
          </form>
          <p className="auth-privacy">密码以带盐单向摘要保存；浏览器不会保存明文密码。</p>
        </div>
      </section>
    </main>
  );
}
