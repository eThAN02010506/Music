import { useCallback, useEffect, useState } from "react";
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
  const [lifecycle] = useState(() => ({ generation: 0 }));
  const [controller] = useState(() => (
    injectedController || new AudioRecorderController(
      { getUserMedia, createRecorder },
      { onRecorded, onError },
    )
  ));
  const [snapshot, setSnapshot] = useState<AudioRecorderSnapshot>(
    controller.getSnapshot,
  );

  useEffect(() => {
    controller.setCallbacks({ onRecorded, onError });
  }, [controller, onError, onRecorded]);

  useEffect(() => {
    const generation = ++lifecycle.generation;
    setSnapshot(controller.getSnapshot());
    const unsubscribe = controller.subscribe(setSnapshot);
    return () => {
      unsubscribe();
      // React StrictMode immediately replays effects in development. Deferring
      // disposal by one microtask distinguishes that replay from a real unmount.
      queueMicrotask(() => {
        if (lifecycle.generation === generation) controller.dispose();
      });
    };
  }, [controller, lifecycle]);

  const start = useCallback(() => controller.start(), [controller]);
  const stop = useCallback(() => controller.stop(), [controller]);

  return {
    ...snapshot,
    start,
    stop,
  };
}
