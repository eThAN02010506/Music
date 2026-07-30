import { useCallback, useEffect, useRef, useState } from "react";

import { api, isAbortError } from "../../api";
import type {
  TeachingChatRequest,
  TeachingConversation,
  TeachingMessage,
} from "../../types";

export interface FailedSongConversationSend {
  conversationId: string | null;
  payload: TeachingChatRequest;
  error: string;
}

export function useSongConversation(
  historyId: string | null,
  outputLanguage: "zh" | "en",
) {
  const [conversations, setConversations] = useState<TeachingConversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(
    null,
  );
  const [messages, setMessages] = useState<TeachingMessage[]>([]);
  const [failedSend, setFailedSend] =
    useState<FailedSongConversationSend | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const generationRef = useRef(0);
  const messagesGenerationRef = useRef(0);
  const sendingRef = useRef(false);
  const sendTaskRef = useRef(0);
  const activeConversationRef = useRef<string | null>(null);
  const failedSendRef = useRef<FailedSongConversationSend | null>(null);

  const selectConversation = useCallback((conversationId: string | null) => {
    activeConversationRef.current = conversationId;
    setActiveConversationId(conversationId);
  }, []);

  const rememberFailedSend = useCallback(
    (failed: FailedSongConversationSend | null) => {
      failedSendRef.current = failed;
      setFailedSend(failed);
    },
    [],
  );

  useEffect(() => {
    generationRef.current += 1;
    sendTaskRef.current += 1;
    const generation = generationRef.current;
    setConversations([]);
    selectConversation(null);
    setMessages([]);
    rememberFailedSend(null);
    setError("");
    sendingRef.current = false;
    setSending(false);
    setLoading(false);
    if (!historyId) return;
    const controller = new AbortController();
    setLoading(true);
    void api.teachingConversations(historyId, controller.signal)
      .then((items) => {
        if (generation !== generationRef.current) return;
        setConversations(items);
        selectConversation(items[0]?.id ?? null);
      })
      .catch((cause: unknown) => {
        if (isAbortError(cause) || generation !== generationRef.current) return;
        setError(
          cause instanceof Error ? cause.message : "无法读取歌曲对话",
        );
      })
      .finally(() => {
        if (generation === generationRef.current) setLoading(false);
      });
    return () => controller.abort();
  }, [historyId, rememberFailedSend, selectConversation]);

  useEffect(() => {
    messagesGenerationRef.current += 1;
    const messagesGeneration = messagesGenerationRef.current;
    const generation = generationRef.current;
    setMessages([]);
    setError("");
    if (!historyId || !activeConversationId) return;
    const controller = new AbortController();
    setLoading(true);
    void api.teachingMessages(
      historyId,
      activeConversationId,
      controller.signal,
    )
      .then((items) => {
        if (
          generation === generationRef.current
          && messagesGeneration === messagesGenerationRef.current
        ) {
          setMessages(items.filter(
            (item) => item.request.output_language === outputLanguage,
          ));
        }
      })
      .catch((cause: unknown) => {
        if (
          isAbortError(cause)
          || generation !== generationRef.current
          || messagesGeneration !== messagesGenerationRef.current
        ) return;
        setError(
          cause instanceof Error ? cause.message : "无法读取对话消息",
        );
      })
      .finally(() => {
        if (
          generation === generationRef.current
          && messagesGeneration === messagesGenerationRef.current
        ) setLoading(false);
      });
    return () => controller.abort();
  }, [activeConversationId, historyId, outputLanguage]);

  const createConversation = useCallback(async () => {
    if (!historyId) return null;
    const generation = generationRef.current;
    setError("");
    try {
      const created = await api.createTeachingConversation(historyId);
      if (generation !== generationRef.current) return null;
      setConversations((items) => [created, ...items]);
      selectConversation(created.id);
      setMessages([]);
      return created;
    } catch (cause) {
      if (generation === generationRef.current) {
        setError(cause instanceof Error ? cause.message : "无法创建歌曲对话");
      }
      return null;
    }
  }, [historyId, selectConversation]);

  const deleteConversation = useCallback(async (conversationId: string) => {
    if (!historyId) return;
    const generation = generationRef.current;
    setError("");
    try {
      await api.deleteTeachingConversation(historyId, conversationId);
      if (generation !== generationRef.current) return;
      const remaining = conversations.filter(
        (item) => item.id !== conversationId,
      );
      setConversations(remaining);
      if (activeConversationRef.current === conversationId) {
        const nextId = remaining[0]?.id ?? null;
        selectConversation(nextId);
      }
    } catch (cause) {
      if (generation === generationRef.current) {
        setError(cause instanceof Error ? cause.message : "无法删除歌曲对话");
      }
    }
  }, [conversations, historyId, selectConversation]);

  const performSend = useCallback(async (
    payload: TeachingChatRequest,
    retry: FailedSongConversationSend | null,
  ) => {
    if (!historyId || sendingRef.current) return null;
    const sendTask = ++sendTaskRef.current;
    const requestPayload = retry?.payload ?? payload;
    let conversationId = retry
      ? retry.conversationId
      : activeConversationRef.current;
    sendingRef.current = true;
    setSending(true);
    setError("");
    if (!retry) rememberFailedSend(null);
    const generation = generationRef.current;
    try {
      if (!conversationId) {
        const created = await api.createTeachingConversation(historyId);
        if (generation !== generationRef.current) return null;
        conversationId = created.id;
        setConversations((items) => [created, ...items]);
        selectConversation(created.id);
      }
      const message = await api.sendTeachingMessage(
        historyId,
        conversationId,
        requestPayload,
      );
      if (generation !== generationRef.current) return null;
      if (message.status === "failed") {
        const messageError = message.error || "导赏回答失败";
        rememberFailedSend({
          conversationId,
          payload: requestPayload,
          error: messageError,
        });
        setError(messageError);
        return null;
      }
      if (activeConversationRef.current === conversationId) {
        setMessages((items) => {
          const withoutRetry = items.filter(
            (item) => item.client_request_id !== message.client_request_id,
          );
          return [...withoutRetry, message].sort(
            (first, second) => first.sequence - second.sequence,
          );
        });
      }
      setConversations((items) => items.map((item) =>
        item.id === conversationId
          ? { ...item, updated_at: message.updated_at }
          : item
      ));
      if (
        failedSendRef.current?.payload.client_request_id
        === requestPayload.client_request_id
      ) {
        rememberFailedSend(null);
      }
      return message;
    } catch (cause) {
      if (generation === generationRef.current) {
        const sendError = cause instanceof Error
          ? cause.message
          : "导赏回答失败";
        rememberFailedSend({
          conversationId,
          payload: requestPayload,
          error: sendError,
        });
        setError(sendError);
      }
      return null;
    } finally {
      if (sendTask === sendTaskRef.current) {
        sendingRef.current = false;
        if (generation === generationRef.current) setSending(false);
      }
    }
  }, [historyId, rememberFailedSend, selectConversation]);

  const send = useCallback(
    (payload: TeachingChatRequest) => performSend(payload, null),
    [performSend],
  );

  const retryFailedSend = useCallback(() => {
    const failed = failedSendRef.current;
    if (!failed) return Promise.resolve(null);
    if (
      failed.conversationId
      && activeConversationRef.current !== failed.conversationId
    ) {
      selectConversation(failed.conversationId);
    }
    return performSend(failed.payload, failed);
  }, [performSend, selectConversation]);

  return {
    conversations,
    activeConversationId,
    selectConversation,
    messages,
    failedSend,
    loading,
    sending,
    error,
    createConversation,
    deleteConversation,
    send,
    retryFailedSend,
  };
}
