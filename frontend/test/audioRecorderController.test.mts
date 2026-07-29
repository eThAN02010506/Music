import assert from "node:assert/strict";
import test from "node:test";

import {
  AudioRecorderController,
  type AudioStreamLike,
  type RecorderDataEventLike,
  type RecorderLike,
} from "../src/hooks/audioRecorderController.ts";

class FakeStream implements AudioStreamLike {
  stops = 0;
  getTracks() {
    return [{ stop: () => { this.stops += 1; } }];
  }
}

class FakeRecorder implements RecorderLike {
  state = "inactive";
  mimeType = "audio/webm";
  ondataavailable: ((event: RecorderDataEventLike) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  starts = 0;
  stops = 0;

  start() {
    this.starts += 1;
    this.state = "recording";
  }

  stop() {
    this.stops += 1;
    this.state = "inactive";
  }

  data(value: string) {
    this.ondataavailable?.({ data: new Blob([value]) });
  }

  stopped() {
    this.onstop?.();
  }

  error() {
    this.onerror?.();
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

test("late getUserMedia after dispose releases tracks without creating a recorder", async () => {
  const media = deferred<AudioStreamLike>();
  const stream = new FakeStream();
  let factories = 0;
  const controller = new AudioRecorderController({
    getUserMedia: () => media.promise,
    createRecorder: () => {
      factories += 1;
      return new FakeRecorder();
    },
  });

  const starting = controller.start();
  assert.equal(controller.getSnapshot().phase, "starting");
  controller.dispose();
  media.resolve(stream);

  assert.equal(await starting, false);
  assert.equal(stream.stops, 1);
  assert.equal(factories, 0);
});

test("double start shares the exclusive starting phase and asks permission once", async () => {
  const media = deferred<AudioStreamLike>();
  const recorder = new FakeRecorder();
  let requests = 0;
  const controller = new AudioRecorderController({
    getUserMedia: () => {
      requests += 1;
      return media.promise;
    },
    createRecorder: () => recorder,
  });

  const first = controller.start();
  assert.equal(await controller.start(), false);
  media.resolve(new FakeStream());

  assert.equal(await first, true);
  assert.equal(requests, 1);
  assert.equal(recorder.starts, 1);
  controller.dispose();
});

test("stop enters finalizing until the recorder emits data and stop", async () => {
  const stream = new FakeStream();
  const recorder = new FakeRecorder();
  const recordings: Array<{ blob: Blob; name: string }> = [];
  const phases: string[] = [];
  const controller = new AudioRecorderController(
    {
      getUserMedia: async () => stream,
      createRecorder: () => recorder,
    },
    { onRecorded: (blob, name) => recordings.push({ blob, name }) },
  );
  controller.subscribe((snapshot) => phases.push(snapshot.phase));

  await controller.start();
  controller.stop();
  assert.equal(controller.getSnapshot().phase, "finalizing");
  recorder.data("voice");
  recorder.stopped();

  assert.equal(controller.getSnapshot().phase, "idle");
  assert.deepEqual(phases, ["starting", "recording", "finalizing", "idle"]);
  assert.equal(await recordings[0].blob.text(), "voice");
  assert.equal(recordings[0].name, "my-singing.webm");
  assert.equal(stream.stops, 1);
});

test("error followed by dataavailable and stop reports once and never publishes audio", async () => {
  const stream = new FakeStream();
  const recorder = new FakeRecorder();
  let errors = 0;
  let recordings = 0;
  const controller = new AudioRecorderController(
    {
      getUserMedia: async () => stream,
      createRecorder: () => recorder,
    },
    {
      onError: () => { errors += 1; },
      onRecorded: () => { recordings += 1; },
    },
  );
  await controller.start();
  const lateData = recorder.ondataavailable!;
  const lateStop = recorder.onstop!;

  recorder.error();
  lateData({ data: new Blob(["corrupt"]) });
  lateStop();

  assert.equal(errors, 1);
  assert.equal(recordings, 0);
  assert.equal(stream.stops, 1);
  assert.equal(controller.getSnapshot().phase, "idle");
});

test("stale events from an old session cannot settle a newer recording", async () => {
  const streams = [new FakeStream(), new FakeStream()];
  const recorders = [new FakeRecorder(), new FakeRecorder()];
  const recordingSizes: number[] = [];
  let index = 0;
  const controller = new AudioRecorderController(
    {
      getUserMedia: async () => streams[index],
      createRecorder: () => recorders[index++],
    },
    { onRecorded: (blob) => recordingSizes.push(blob.size) },
  );

  await controller.start();
  const oldStop = recorders[0].onstop!;
  recorders[0].error();
  await controller.start();
  oldStop();
  assert.equal(controller.getSnapshot().phase, "recording");
  recorders[1].data("new");
  recorders[1].stopped();
  assert.deepEqual(recordingSizes, [3]);
  assert.equal(streams[0].stops, 1);
  assert.equal(streams[1].stops, 1);
});

test("stop while permission is pending invalidates and cleans the late stream", async () => {
  const media = deferred<AudioStreamLike>();
  const stream = new FakeStream();
  const controller = new AudioRecorderController({
    getUserMedia: () => media.promise,
    createRecorder: () => new FakeRecorder(),
  });

  const starting = controller.start();
  controller.stop();
  assert.equal(controller.getSnapshot().phase, "idle");
  media.resolve(stream);

  assert.equal(await starting, false);
  assert.equal(stream.stops, 1);
});

test("recorder start failure releases every track and restores idle", async () => {
  const stream = new FakeStream();
  const recorder = new FakeRecorder();
  recorder.start = () => {
    throw new Error("start failed");
  };
  const controller = new AudioRecorderController({
    getUserMedia: async () => stream,
    createRecorder: () => recorder,
  });

  await assert.rejects(controller.start(), /start failed/);
  assert.equal(stream.stops, 1);
  assert.equal(controller.getSnapshot().phase, "idle");
});
