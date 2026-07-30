import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError, isAbortError } from "../../api";
import type {
  ListenerLevel,
  ListenerProfile,
  TeachingGuideResponse,
  TeachingGuideStrategy,
} from "../../types";

const DEFAULT_PROFILE: ListenerProfile = {
  level: "beginner",
  preferences: {},
  learned_concepts: [],
};

const GUIDE_POLL_MS = 2_000;

export interface TeachingGenerationOptions {
  force?: boolean;
  strategy?: TeachingGuideStrategy;
}

export function useTeachingExperience(historyId: string | null) {
  const [guide, setGuide] = useState<TeachingGuideResponse | null>(null);
  const [profile, setProfile] = useState<ListenerProfile>(DEFAULT_PROFILE);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const generationRef = useRef(0);
  const generatingRef = useRef(false);
  const generationTaskRef = useRef(0);

  useEffect(() => {
    generationRef.current += 1;
    generationTaskRef.current += 1;
    const generation = generationRef.current;
    setGuide(null);
    setProfile(DEFAULT_PROFILE);
    setError("");
    generatingRef.current = false;
    setGenerating(false);
    if (!historyId) {
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    const current = () => (
      !controller.signal.aborted && generation === generationRef.current
    );
    const schedulePoll = () => {
      if (!current() || pollTimer !== null) return;
      pollTimer = setTimeout(() => {
        pollTimer = null;
        void refreshGuide();
      }, GUIDE_POLL_MS);
    };
    const bootstrapEvidenceGuide = async () => {
      if (!current()) return;
      if (generatingRef.current) {
        schedulePoll();
        return;
      }
      const task = ++generationTaskRef.current;
      generatingRef.current = true;
      setGenerating(true);
      setError("");
      try {
        const next = await api.generateTeachingGuide(historyId, {
          strategy: "evidence",
        });
        if (current()) setGuide(next);
      } catch (cause) {
        if (!current() || isAbortError(cause)) return;
        if (
          cause instanceof ApiError
          && (cause.status === 409 || cause.status === 429)
        ) {
          schedulePoll();
          return;
        }
        setError(
          cause instanceof Error ? cause.message : "无法准备基础音乐导赏",
        );
      } finally {
        if (task === generationTaskRef.current) {
          generatingRef.current = false;
          if (current()) setGenerating(false);
        }
      }
    };
    const refreshGuide = async () => {
      try {
        const next = await api.teachingGuide(historyId, controller.signal);
        if (!current()) return;
        setGuide(next);
        setError("");
        if (next.status === "pending") {
          schedulePoll();
        } else if (!next.understanding_map) {
          await bootstrapEvidenceGuide();
        }
      } catch (cause) {
        if (!current() || isAbortError(cause)) return;
        if (cause instanceof ApiError && cause.status === 404) {
          await bootstrapEvidenceGuide();
          return;
        }
        setError(
          cause instanceof Error
            ? cause.message
            : "无法读取音乐导赏地图",
        );
      } finally {
        if (current()) setLoading(false);
      }
    };

    setLoading(true);
    void refreshGuide();
    void api.listenerProfile(controller.signal).then((next) => {
      if (current()) setProfile(next);
    }).catch((cause) => {
      if (!current() || isAbortError(cause)) return;
      setError((existing) => existing || (
        cause instanceof Error
          ? cause.message
          : "无法读取听觉学习档案"
      ));
    });
    return () => {
      controller.abort();
      if (pollTimer !== null) clearTimeout(pollTimer);
    };
  }, [historyId]);

  const generate = useCallback(async (
    options: TeachingGenerationOptions = {},
  ) => {
    if (!historyId || generatingRef.current) return null;
    const generation = generationRef.current;
    const task = ++generationTaskRef.current;
    generatingRef.current = true;
    setGenerating(true);
    setError("");
    try {
      const next = await api.generateTeachingGuide(historyId, options);
      if (generation === generationRef.current) setGuide(next);
      return next;
    } catch (cause) {
      if (generation === generationRef.current) {
        setError(cause instanceof Error ? cause.message : "导赏地图生成失败");
      }
      return null;
    } finally {
      if (task === generationTaskRef.current) {
        generatingRef.current = false;
        if (generation === generationRef.current) setGenerating(false);
      }
    }
  }, [historyId]);

  const updateLevel = useCallback(async (level: ListenerLevel) => {
    const next = await api.updateListenerProfile(
      level,
      profile.preferences,
      profile.learned_concepts,
    );
    setProfile(next);
  }, [profile.learned_concepts, profile.preferences]);

  return {
    guide,
    profile,
    loading,
    generating,
    error,
    generate,
    updateLevel,
  };
}
