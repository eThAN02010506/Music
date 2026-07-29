export type RecorderPhase = "idle" | "starting" | "recording" | "finalizing";

export interface AudioTrackLike {
  stop(): void;
}

export interface AudioStreamLike {
  getTracks(): AudioTrackLike[];
}

export interface RecorderDataEventLike {
  data: Blob;
}

export interface RecorderLike {
  readonly state: string;
  readonly mimeType: string;
  ondataavailable: ((event: RecorderDataEventLike) => void) | null;
  onstop: (() => void) | null;
  onerror: (() => void) | null;
  start(): void;
  stop(): void;
}

export interface AudioRecorderSnapshot {
  phase: RecorderPhase;
  starting: boolean;
  recording: boolean;
  finalizing: boolean;
  busy: boolean;
}

type RecorderSession = {
  generation: number;
  stream: AudioStreamLike;
  recorder: RecorderLike;
  chunks: Blob[];
  failed: boolean;
  settled: boolean;
  tracksStopped: boolean;
};

export type AudioRecorderCallbacks = {
  onRecorded: (blob: Blob, fileName: string) => void;
  onError: (error: Error) => void;
};

export type AudioRecorderDependencies = {
  getUserMedia: () => Promise<AudioStreamLike>;
  createRecorder: (stream: AudioStreamLike) => RecorderLike;
};

const NOOP_CALLBACKS: AudioRecorderCallbacks = {
  onRecorded: () => {},
  onError: () => {},
};

function fileNameForMimeType(mimeType: string): string {
  const extension = mimeType.includes("mp4")
    ? "m4a"
    : mimeType.includes("ogg") ? "ogg" : "webm";
  return `my-singing.${extension}`;
}

export function stopMediaStream(stream: AudioStreamLike | null): void {
  stream?.getTracks().forEach((track) => track.stop());
}

export class AudioRecorderController {
  private readonly dependencies: AudioRecorderDependencies;
  private phase: RecorderPhase = "idle";
  private generation = 0;
  private disposed = false;
  private session: RecorderSession | null = null;
  private callbacks: AudioRecorderCallbacks;
  private readonly listeners = new Set<(snapshot: AudioRecorderSnapshot) => void>();

  constructor(
    dependencies: AudioRecorderDependencies,
    callbacks: Partial<AudioRecorderCallbacks> = {},
  ) {
    this.dependencies = dependencies;
    this.callbacks = { ...NOOP_CALLBACKS, ...callbacks };
  }

  getSnapshot = (): AudioRecorderSnapshot => ({
    phase: this.phase,
    starting: this.phase === "starting",
    recording: this.phase === "recording",
    finalizing: this.phase === "finalizing",
    busy: this.phase !== "idle",
  });

  subscribe = (listener: (snapshot: AudioRecorderSnapshot) => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  setCallbacks(callbacks: Partial<AudioRecorderCallbacks>): void {
    this.callbacks = { ...this.callbacks, ...callbacks };
  }

  async start(): Promise<boolean> {
    if (this.disposed || this.phase !== "idle") return false;
    const generation = ++this.generation;
    this.setPhase("starting");

    let stream: AudioStreamLike;
    try {
      stream = await this.dependencies.getUserMedia();
    } catch (cause) {
      if (!this.isGenerationCurrent(generation)) return false;
      this.setPhase("idle");
      throw cause;
    }

    if (!this.isGenerationCurrent(generation)) {
      stopMediaStream(stream);
      return false;
    }

    let recorder: RecorderLike;
    try {
      recorder = this.dependencies.createRecorder(stream);
    } catch (cause) {
      stopMediaStream(stream);
      if (this.isGenerationCurrent(generation)) this.setPhase("idle");
      throw cause;
    }

    const session: RecorderSession = {
      generation,
      stream,
      recorder,
      chunks: [],
      failed: false,
      settled: false,
      tracksStopped: false,
    };
    this.session = session;
    recorder.ondataavailable = (event) => this.handleData(session, event);
    recorder.onerror = () => this.handleError(session);
    recorder.onstop = () => this.handleStop(session);

    try {
      recorder.start();
    } catch (cause) {
      this.settleSession(session);
      if (this.isGenerationCurrent(generation)) this.setPhase("idle");
      throw cause;
    }

    if (!this.isSessionCurrent(session)) {
      this.settleSession(session);
      return false;
    }
    this.setPhase("recording");
    return true;
  }

  stop(): void {
    if (this.disposed) return;
    if (this.phase === "starting") {
      this.generation += 1;
      this.setPhase("idle");
      return;
    }
    const session = this.session;
    if (!session || this.phase === "idle" || this.phase === "finalizing") return;
    this.setPhase("finalizing");
    try {
      if (session.recorder.state === "inactive") {
        this.handleStop(session);
      } else {
        session.recorder.stop();
      }
    } catch (cause) {
      this.settleSession(session);
      this.setPhase("idle");
      throw cause;
    }
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.generation += 1;
    const session = this.session;
    if (session) {
      this.detach(session);
      if (session.recorder.state !== "inactive") {
        try {
          session.recorder.stop();
        } catch {
          // Releasing tracks below is the authoritative cleanup.
        }
      }
      this.settleSession(session);
    }
    this.phase = "idle";
    this.listeners.clear();
  }

  private handleData(session: RecorderSession, event: RecorderDataEventLike): void {
    if (!this.isSessionCurrent(session) || session.failed || session.settled) return;
    if (event.data.size > 0) session.chunks.push(event.data);
  }

  private handleError(session: RecorderSession): void {
    if (!this.isSessionCurrent(session) || session.settled) return;
    session.failed = true;
    this.settleSession(session);
    this.setPhase("idle");
    this.callbacks.onError(new Error("录音设备发生错误，请重试。"));
  }

  private handleStop(session: RecorderSession): void {
    if (!this.isSessionCurrent(session) || session.settled) return;
    const mimeType = session.recorder.mimeType || "audio/webm";
    const blob = new Blob(session.chunks, { type: mimeType });
    const failed = session.failed;
    this.settleSession(session);
    this.setPhase("idle");
    if (!failed && !this.disposed) {
      this.callbacks.onRecorded(blob, fileNameForMimeType(mimeType));
    }
  }

  private settleSession(session: RecorderSession): void {
    if (session.settled) return;
    session.settled = true;
    this.detach(session);
    if (!session.tracksStopped) {
      session.tracksStopped = true;
      stopMediaStream(session.stream);
    }
    if (this.session === session) this.session = null;
  }

  private detach(session: RecorderSession): void {
    session.recorder.ondataavailable = null;
    session.recorder.onerror = null;
    session.recorder.onstop = null;
  }

  private isGenerationCurrent(generation: number): boolean {
    return !this.disposed && generation === this.generation;
  }

  private isSessionCurrent(session: RecorderSession): boolean {
    return (
      !this.disposed
      && !session.settled
      && this.session === session
      && this.isGenerationCurrent(session.generation)
    );
  }

  private setPhase(phase: RecorderPhase): void {
    if (this.disposed || this.phase === phase) return;
    this.phase = phase;
    const snapshot = this.getSnapshot();
    this.listeners.forEach((listener) => listener(snapshot));
  }
}
