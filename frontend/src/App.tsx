import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  AUTH_EXPIRED_EVENT,
} from "./api";
import { isAuthSyncStorageKey } from "./authSession";
import { SignalMark } from "./components/SignalMark";
import { AuthScreen } from "./features/auth/AuthScreen";
import { AuthenticatedWorkspace } from "./features/workspace/AuthenticatedWorkspace";
import { LatestRequest } from "./hooks/latestRequest";
import { useHealthCheck } from "./hooks/useHealthCheck";
import type { User } from "./types";

export default function App() {
  const {
    health,
    healthChecked,
    healthChecking,
    retryHealth,
  } = useHealthCheck();
  const [authLoading, setAuthLoading] = useState(true);
  const [user, setUser] = useState<User | null>(null);
  const [authNotice, setAuthNotice] = useState("");
  const authRequestsRef = useRef(new LatestRequest());

  const verifySession = useCallback(async (suspendWorkspace: boolean) => {
    const requestId = authRequestsRef.current.begin();
    if (suspendWorkspace) {
      setUser(null);
      setAuthLoading(true);
    }
    try {
      const next = await api.authMe();
      if (!authRequestsRef.current.isCurrent(requestId)) return;
      setUser((current) => current?.id === next.id ? current : next);
      setAuthNotice("");
    } catch (cause) {
      if (!authRequestsRef.current.isCurrent(requestId)) return;
      if (
        suspendWorkspace
        || (cause instanceof ApiError && cause.status === 401)
      ) {
        setUser(null);
        setAuthNotice("登录状态已变化，请重新登录。");
      }
    } finally {
      if (authRequestsRef.current.isCurrent(requestId)) {
        setAuthLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void verifySession(true);
    return () => authRequestsRef.current.invalidate();
  }, [verifySession]);

  useEffect(() => {
    const expire = () => {
      authRequestsRef.current.invalidate();
      setUser(null);
      setAuthLoading(false);
      setAuthNotice("登录状态已过期，请重新登录。");
    };
    const syncAcrossTabs = (event: StorageEvent) => {
      if (isAuthSyncStorageKey(event.key)) void verifySession(true);
    };
    const verifyOnFocus = () => {
      void verifySession(false);
    };
    const verifyWhenVisible = () => {
      if (document.visibilityState === "visible") void verifySession(false);
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, expire);
    window.addEventListener("storage", syncAcrossTabs);
    window.addEventListener("focus", verifyOnFocus);
    document.addEventListener("visibilitychange", verifyWhenVisible);
    return () => {
      window.removeEventListener(AUTH_EXPIRED_EVENT, expire);
      window.removeEventListener("storage", syncAcrossTabs);
      window.removeEventListener("focus", verifyOnFocus);
      document.removeEventListener("visibilitychange", verifyWhenVisible);
    };
  }, [verifySession]);

  if (authLoading) {
    return (
      <main className="app-loading" aria-label="正在打开本地工作区">
        <SignalMark />
        <strong>Music Insight</strong>
        <span>正在打开本地工作区…</span>
      </main>
    );
  }

  if (!user) {
    return (
      <AuthScreen
        health={health}
        healthChecked={healthChecked}
        healthChecking={healthChecking}
        notice={authNotice}
        onRetryHealth={retryHealth}
        onAuthenticated={(next) => {
          authRequestsRef.current.invalidate();
          setAuthNotice("");
          setUser(next);
        }}
      />
    );
  }

  return (
    <AuthenticatedWorkspace
      key={user.id}
      user={user}
      health={health}
      onLoggedOut={() => {
        authRequestsRef.current.invalidate();
        setAuthNotice("已安全退出本地账号。");
        setUser(null);
      }}
    />
  );
}
