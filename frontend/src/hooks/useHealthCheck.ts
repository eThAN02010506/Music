import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { HealthResult } from "../types";
import {
  HealthCheckController,
  type HealthCheckSnapshot,
} from "./healthCheckController";

const INITIAL_HEALTH: HealthCheckSnapshot<HealthResult> = {
  value: null,
  checked: false,
  checking: false,
};

export function useHealthCheck() {
  const [status, setStatus] = useState(INITIAL_HEALTH);
  const controllerRef = useRef<HealthCheckController<HealthResult> | null>(null);

  useEffect(() => {
    const controller = new HealthCheckController<HealthResult>(
      {
        probe: api.health,
        setTimer: (callback, delay) => window.setTimeout(callback, delay),
        clearTimer: (handle) => window.clearTimeout(handle as number),
      },
      setStatus,
    );
    controllerRef.current = controller;
    const retry = () => controller.retryNow();
    window.addEventListener("focus", retry);
    window.addEventListener("online", retry);
    controller.start();
    return () => {
      window.removeEventListener("focus", retry);
      window.removeEventListener("online", retry);
      controller.dispose();
      if (controllerRef.current === controller) controllerRef.current = null;
    };
  }, []);

  const retry = useCallback(() => controllerRef.current?.retryNow(), []);

  return {
    health: status.value,
    healthChecked: status.checked,
    healthChecking: status.checking,
    retryHealth: retry,
  };
}
