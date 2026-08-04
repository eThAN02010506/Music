import type { Span } from "../../types";
import {
  clampTime,
  limitSelectionRange,
  normalizeRange,
  sanitizePlayerAction,
} from "./playerController.ts";
import type {
  ActivePlayback,
  PlayerAction,
  PlayerRangeSlot,
  PlayerSnapshot,
} from "./playerTypes";

type Listener = () => void;

export interface PlayerFrameScheduler {
  request(callback: FrameRequestCallback): number;
  cancel(handle: number): void;
}

const DEFAULT_FRAME_SCHEDULER: PlayerFrameScheduler = {
  request(callback) {
    if (typeof globalThis.requestAnimationFrame === "function") {
      return globalThis.requestAnimationFrame(callback);
    }
    return globalThis.setTimeout(
      () => callback(globalThis.performance?.now() ?? Date.now()),
      16,
    );
  },
  cancel(handle) {
    if (typeof globalThis.cancelAnimationFrame === "function") {
      globalThis.cancelAnimationFrame(handle);
    } else {
      globalThis.clearTimeout(handle);
    }
  },
};

const EMPTY_SNAPSHOT: PlayerSnapshot = {
  currentTime: 0,
  duration: 0,
  playing: false,
  selectedRange: null,
  rangeA: null,
  rangeB: null,
  activePlayback: null,
};

export class PlayerStore {
  private media: HTMLMediaElement | null = null;
  private mediaCleanup: (() => void) | null = null;
  private readonly followers = new Set<HTMLMediaElement>();
  private stemMixActive = false;
  private masterMutedBeforeStemMix = false;
  private snapshot: PlayerSnapshot;
  private readonly listeners = new Set<Listener>();
  private readonly scheduler: PlayerFrameScheduler;
  private frameHandle: number | null = null;
  private durationFallback: number;

  constructor(
    durationFallback = 0,
    scheduler: PlayerFrameScheduler = DEFAULT_FRAME_SCHEDULER,
  ) {
    this.durationFallback = Math.max(0, durationFallback);
    this.scheduler = scheduler;
    this.snapshot = {
      ...EMPTY_SNAPSHOT,
      duration: this.durationFallback,
    };
  }

  readonly getSnapshot = (): PlayerSnapshot => this.snapshot;

  readonly subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  setDurationFallback(duration: number): void {
    this.durationFallback = Number.isFinite(duration)
      ? Math.max(0, duration)
      : 0;
    if (!this.media || !Number.isFinite(this.media.duration)) {
      this.patch({ duration: this.durationFallback });
    }
  }

  attach(media: HTMLMediaElement): () => void {
    this.detach();
    this.media = media;
    if (this.stemMixActive) {
      this.masterMutedBeforeStemMix = media.muted;
      media.muted = true;
    }
    const updateTime = () => {
      this.enforcePlaybackBoundary(media);
      this.syncFollowers(media);
      this.patch({
        currentTime: clampTime(
          media.currentTime,
          this.effectiveDuration(media),
        ),
        playing: !media.paused,
      });
    };
    const updateMetadata = () => {
      this.patch({
        duration: this.effectiveDuration(media),
        currentTime: clampTime(
          media.currentTime,
          this.effectiveDuration(media),
        ),
      });
    };
    const updatePlayback = () => {
      this.syncFollowers(media, true);
      this.patch({ playing: !media.paused });
      if (!media.paused) {
        this.startScheduler();
      } else {
        this.stopScheduler();
      }
    };
    const updateVolume = () => {
      if (this.stemMixActive && !media.muted) media.muted = true;
      this.syncFollowerVolumes(media);
    };
    const updateEnded = () => {
      this.enforcePlaybackBoundary(media);
      updatePlayback();
      updateTime();
    };
    media.addEventListener("timeupdate", updateTime);
    const updateSeeking = () => {
      this.enforcePlaybackBoundary(media);
      this.syncFollowers(media, true);
      updateTime();
    };
    media.addEventListener("seeking", updateSeeking);
    media.addEventListener("loadedmetadata", updateMetadata);
    media.addEventListener("durationchange", updateMetadata);
    media.addEventListener("play", updatePlayback);
    media.addEventListener("pause", updatePlayback);
    media.addEventListener("ended", updateEnded);
    media.addEventListener("volumechange", updateVolume);
    let cleaned = false;
    const cleanup = () => {
      if (cleaned) return;
      cleaned = true;
      media.removeEventListener("timeupdate", updateTime);
      media.removeEventListener("seeking", updateSeeking);
      media.removeEventListener("loadedmetadata", updateMetadata);
      media.removeEventListener("durationchange", updateMetadata);
      media.removeEventListener("play", updatePlayback);
      media.removeEventListener("pause", updatePlayback);
      media.removeEventListener("ended", updateEnded);
      media.removeEventListener("volumechange", updateVolume);
      if (this.media === media) {
        if (this.stemMixActive) {
          media.muted = this.masterMutedBeforeStemMix;
        }
        this.pauseFollowers();
        this.stopScheduler();
        this.media = null;
        this.mediaCleanup = null;
        this.patch({ playing: false, activePlayback: null });
      }
    };
    this.mediaCleanup = cleanup;
    updateMetadata();
    updateTime();
    return cleanup;
  }

  detach(): void {
    if (this.mediaCleanup) {
      this.mediaCleanup();
      return;
    }
    this.stopScheduler();
    this.pauseFollowers();
    this.media = null;
    this.patch({ playing: false, activePlayback: null });
  }

  seek(time: number, autoplay = true): void {
    const next = clampTime(time, this.snapshot.duration);
    this.clearPlayback();
    if (this.media) {
      this.media.currentTime = next;
      this.syncFollowers(this.media, true);
      if (autoplay) this.playMedia(this.media);
    }
    this.patch({ currentTime: next });
  }

  playRange(range: Span, loop = false): void {
    const normalized = normalizeRange(range, this.snapshot.duration);
    if (!normalized) return;
    this.beginControlledPlayback(
      loop
        ? { mode: "loop", range: normalized }
        : { mode: "once", range: normalized },
      normalized.start_s,
    );
  }

  playAB(a: Span, b: Span): void {
    const rangeA = normalizeRange(a, this.snapshot.duration);
    const rangeB = normalizeRange(b, this.snapshot.duration);
    if (!rangeA || !rangeB) return;
    this.patch({ rangeA, rangeB });
    this.beginControlledPlayback(
      { mode: "ab", rangeA, rangeB, phase: "a" },
      rangeA.start_s,
    );
  }

  setRange(slot: PlayerRangeSlot, range: Span | null): void {
    const normalized = range
      ? slot === "selection"
        ? limitSelectionRange(range, this.snapshot.duration)
        : normalizeRange(range, this.snapshot.duration)
      : null;
    if (slot === "selection") {
      this.patch({ selectedRange: normalized });
    } else if (slot === "a") {
      this.patch({ rangeA: normalized });
    } else {
      this.patch({ rangeB: normalized });
    }
  }

  clearPlayback(): void {
    this.stopScheduler();
    this.patch({ activePlayback: null });
  }

  attachFollower(media: HTMLMediaElement): () => void {
    this.followers.add(media);
    if (this.media && this.stemMixActive) {
      this.syncFollower(this.media, media, true);
    } else {
      media.pause();
    }
    return () => {
      if (!this.followers.delete(media)) return;
      media.pause();
    };
  }

  setStemMixActive(active: boolean): void {
    if (active === this.stemMixActive) return;
    this.stemMixActive = active;
    if (!this.media) {
      if (!active) this.pauseFollowers();
      return;
    }
    if (active) {
      this.masterMutedBeforeStemMix = this.media.muted;
      this.media.muted = true;
      this.syncFollowers(this.media, true);
    } else {
      this.media.muted = this.masterMutedBeforeStemMix;
      this.pauseFollowers();
    }
  }

  execute(action: PlayerAction): void {
    const safe = sanitizePlayerAction(action, this.snapshot.duration);
    if (!safe) return;
    if (safe.type === "seek") {
      this.seek(safe.time_s);
    } else if (safe.type === "play_range") {
      this.playRange(safe);
    } else if (safe.type === "loop_range") {
      this.playRange(safe, true);
    } else {
      this.playAB(safe.a, safe.b);
    }
  }

  reset(): void {
    this.stopScheduler();
    if (this.media) {
      this.media.pause();
      this.media.currentTime = 0;
      if (this.stemMixActive) {
        this.media.muted = this.masterMutedBeforeStemMix;
      }
    }
    this.stemMixActive = false;
    for (const follower of this.followers) {
      follower.pause();
      follower.currentTime = 0;
    }
    this.snapshot = {
      ...EMPTY_SNAPSHOT,
      duration: this.durationFallback,
    };
    this.emit();
  }

  private effectiveDuration(media: HTMLMediaElement): number {
    return Number.isFinite(media.duration) && media.duration > 0
      ? media.duration
      : this.durationFallback;
  }

  private patch(update: Partial<PlayerSnapshot>): void {
    const next = { ...this.snapshot, ...update };
    if (
      next.currentTime === this.snapshot.currentTime
      && next.duration === this.snapshot.duration
      && next.playing === this.snapshot.playing
      && next.selectedRange === this.snapshot.selectedRange
      && next.rangeA === this.snapshot.rangeA
      && next.rangeB === this.snapshot.rangeB
      && next.activePlayback === this.snapshot.activePlayback
    ) return;
    this.snapshot = next;
    this.emit();
  }

  private beginControlledPlayback(
    activePlayback: ActivePlayback,
    startTime: number,
  ): void {
    this.stopScheduler();
    this.patch({
      activePlayback,
      currentTime: clampTime(startTime, this.snapshot.duration),
    });
    if (!this.media) return;
    this.media.currentTime = startTime;
    this.syncFollowers(this.media, true);
    this.playMedia(this.media);
  }

  private playMedia(media: HTMLMediaElement): void {
    void media.play()
      .then(() => {
        if (this.media === media && this.snapshot.activePlayback) {
          this.startScheduler();
        }
      })
      .catch(() => {
        if (this.media === media) {
          this.stopScheduler();
          this.patch({ playing: !media.paused });
        }
      });
  }

  private startScheduler(): void {
    if (
      this.frameHandle !== null
      || !this.media
      || this.media.paused
    ) return;
    this.frameHandle = this.scheduler.request(() => {
      this.frameHandle = null;
      const media = this.media;
      if (!media) return;
      this.enforcePlaybackBoundary(media);
      this.syncFollowers(media);
      this.patch({
        currentTime: clampTime(
          media.currentTime,
          this.effectiveDuration(media),
        ),
        playing: !media.paused,
      });
      if (!media.paused) {
        this.startScheduler();
      }
    });
  }

  private stopScheduler(): void {
    if (this.frameHandle === null) return;
    this.scheduler.cancel(this.frameHandle);
    this.frameHandle = null;
  }

  private enforcePlaybackBoundary(media: HTMLMediaElement): void {
    const active = this.snapshot.activePlayback;
    if (!active) return;
    if (active.mode === "once") {
      if (media.currentTime < active.range.start_s) {
        media.currentTime = active.range.start_s;
      } else if (media.currentTime >= active.range.end_s) {
        this.finishControlledPlayback(media, active.range.end_s);
      }
      return;
    }
    if (active.mode === "loop") {
      if (
        media.currentTime < active.range.start_s
        || media.currentTime >= active.range.end_s
      ) {
        media.currentTime = active.range.start_s;
        this.patch({ currentTime: active.range.start_s });
        if (media.paused) this.playMedia(media);
      }
      return;
    }
    const range = active.phase === "a" ? active.rangeA : active.rangeB;
    if (media.currentTime < range.start_s) {
      media.currentTime = range.start_s;
      return;
    }
    if (media.currentTime < range.end_s) return;
    if (active.phase === "a") {
      const next: ActivePlayback = { ...active, phase: "b" };
      this.patch({
        activePlayback: next,
        currentTime: active.rangeB.start_s,
      });
      media.currentTime = active.rangeB.start_s;
      if (media.paused) this.playMedia(media);
      return;
    }
    this.finishControlledPlayback(media, active.rangeB.end_s);
  }

  private finishControlledPlayback(
    media: HTMLMediaElement,
    endTime: number,
  ): void {
    this.stopScheduler();
    this.patch({
      activePlayback: null,
      currentTime: endTime,
      playing: false,
    });
    media.pause();
    media.currentTime = endTime;
    this.syncFollowers(media, true);
  }

  private syncFollowers(
    master: HTMLMediaElement,
    force = false,
  ): void {
    if (!this.stemMixActive) return;
    for (const follower of this.followers) {
      this.syncFollower(master, follower, force);
    }
  }

  private syncFollower(
    master: HTMLMediaElement,
    follower: HTMLMediaElement,
    force: boolean,
  ): void {
    try {
      follower.volume = master.volume;
      if (
        force
        || !Number.isFinite(follower.currentTime)
        || Math.abs(follower.currentTime - master.currentTime) > 0.08
      ) {
        follower.currentTime = master.currentTime;
      }
    } catch {
      // Metadata may not be loaded yet. The next media event retries.
    }
    if (master.paused) {
      follower.pause();
      return;
    }
    void follower.play().catch(() => {
      // The media element reports its own load/play error to the mixer UI.
    });
  }

  private pauseFollowers(): void {
    for (const follower of this.followers) follower.pause();
  }

  private syncFollowerVolumes(master: HTMLMediaElement): void {
    if (!this.stemMixActive) return;
    for (const follower of this.followers) {
      follower.volume = master.volume;
    }
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }
}
