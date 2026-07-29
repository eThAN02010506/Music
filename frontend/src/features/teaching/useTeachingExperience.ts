import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError, isAbortError } from "../../api";
import type {
  ListenerLevel,
  ListenerProfile,
  TeachingGuideResponse,
} from "../../types";

const DEFAULT_PROFILE: ListenerProfile = {
  level: "beginner",
  preferences: {},
  learned_concepts: [],
};

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
    setLoading(true);
    void Promise.allSettled([
      api.teachingGuide(historyId, controller.signal),
      api.listenerProfile(controller.signal),
    ]).then(([guideResult, profileResult]) => {
      if (controller.signal.aborted || generation !== generationRef.current) return;
      if (guideResult.status === "fulfilled") {
        setGuide(guideResult.value);
      } else if (
        !(guideResult.reason instanceof ApiError)
        || guideResult.reason.status !== 404
      ) {
        setError(
          guideResult.reason instanceof Error
            ? guideResult.reason.message
            : "无法读取音乐导赏地图",
        );
      }
      if (profileResult.status === "fulfilled") {
        setProfile(profileResult.value);
      } else if (!isAbortError(profileResult.reason)) {
        setError((current) => current || (
          profileResult.reason instanceof Error
            ? profileResult.reason.message
            : "无法读取听觉学习档案"
        ));
      }
    }).finally(() => {
      if (generation === generationRef.current) setLoading(false);
    });
    return () => controller.abort();
  }, [historyId]);

  const generate = useCallback(async (force = false) => {
    if (!historyId || generatingRef.current) return null;
    const generation = generationRef.current;
    const task = ++generationTaskRef.current;
    generatingRef.current = true;
    setGenerating(true);
    setError("");
    try {
      const next = await api.generateTeachingGuide(historyId, force);
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
