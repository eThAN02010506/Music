import { useCallback, useEffect, useRef, useState } from "react";
import {
  AudioRecorderController,
  type AudioRecorderDependencies,
  type AudioRecorderSnapshot,
  type AudioStreamLike,
  type RecorderLike,
} from "./audioRecorderController";

export { stopMediaStream } from "./audioRecorderController";

const defaultDependencies: AudioRecorderDependencies = {
  getUserMedia: () => navigator.mediaDevices.getUserMedia({ audio: true }),
  createRecorder: (stream) =>
    new MediaRecorder(stream as MediaStream) as unknown as RecorderLike,
};

type UseAudioRecorderOptions = {
  onRecorded: (blob: Blob, fileName: string) => void;
  onError?: (error: Error) => void;
  getUserMedia?: () => Promise<AudioStreamLike>;
  createRecorder?: (stream: AudioStreamLike) => RecorderLike;
  controller?: AudioRecorderController;
};

export function useAudioRecorder({
  onRecorded,
  onError = () => {},
  getUserMedia = defaultDependencies.getUserMedia,
  createRecorder = defaultDependencies.createRecorder,
  controller: injectedController,
}: UseAudioRecorderOptions) {
  const controllerRef = useRef<AudioRecorderController | null>(null);
  const lifecycleRef = useRef(0);
  if (!controllerRef.current) {
    controllerRef.current = injectedController || new AudioRecorderController(
      { getUserMedia, createRecorder },
      { onRecorded, onError },
    );
  }
  const controller = controllerRef.current;
  const [snapshot, setSnapshot] = useState<AudioRecorderSnapshot>(
    controller.getSnapshot,
  );

  useEffect(() => {
    controller.setCallbacks({ onRecorded, onError });
  }, [onError, onRecorded]);

  useEffect(() => {
    const lifecycle = ++lifecycleRef.current;
    setSnapshot(controller.getSnapshot());
    const unsubscribe = controller.subscribe(setSnapshot);
    return () => {
      unsubscribe();
      // React StrictMode immediately replays effects in development. Deferring
      // disposal by one microtask distinguishes that replay from a real unmount.
      queueMicrotask(() => {
        if (lifecycleRef.current === lifecycle) controller.dispose();
      });
    };
  }, [controller]);

  const start = useCallback(() => controller.start(), [controller]);
  const stop = useCallback(() => controller.stop(), [controller]);

  return {
    ...snapshot,
    start,
    stop,
  };
}
