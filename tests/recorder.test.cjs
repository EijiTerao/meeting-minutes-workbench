const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { RATE, MAX_SAMPLES, encodeWav, isSilent, hasAudioTracks, isCurrentGeneration, shouldWarnBeforeUnload, workletSource } = require('../capture/recorder.js');

test('encodes canonical mono 16-bit PCM WAV header and samples', async () => {
  const wav = encodeWav(Float32Array.from([-1, 0, 1]));
  assert.equal(wav.type, 'audio/wav');
  const bytes = Buffer.from(await wav.arrayBuffer());
  assert.equal(bytes.toString('ascii', 0, 4), 'RIFF');
  assert.equal(bytes.readUInt32LE(4), 42);
  assert.equal(bytes.toString('ascii', 8, 12), 'WAVE');
  assert.equal(bytes.toString('ascii', 12, 16), 'fmt ');
  assert.equal(bytes.readUInt32LE(16), 16);
  assert.equal(bytes.readUInt16LE(20), 1);
  assert.equal(bytes.readUInt16LE(22), 1);
  assert.equal(bytes.readUInt32LE(24), RATE);
  assert.equal(bytes.readUInt32LE(28), RATE * 2);
  assert.equal(bytes.readUInt16LE(32), 2);
  assert.equal(bytes.readUInt16LE(34), 16);
  assert.equal(bytes.toString('ascii', 36, 40), 'data');
  assert.equal(bytes.readUInt32LE(40), 6);
  assert.deepEqual([bytes.readInt16LE(44), bytes.readInt16LE(46), bytes.readInt16LE(48)], [-32768, 0, 32767]);
});

test('rejects empty audio and recordings over the memory/time cap', () => {
  assert.throws(() => encodeWav(new Float32Array(0)), /音声データ/);
  assert.throws(() => encodeWav(new Float32Array(MAX_SAMPLES + 1)), /上限/);
});

test('labels silence for explicit retention confirmation', () => {
  assert.equal(isSilent(new Float32Array(16000)), true);
  assert.equal(isSilent(Float32Array.from([0.5, -0.5])), false);
});

test('encodes retained Int16 chunks without requiring a Float32 merge', async () => {
  const wav = encodeWav([Int16Array.from([-32768, 0]), Int16Array.from([32767])]);
  const bytes = Buffer.from(await wav.arrayBuffer());
  assert.equal(bytes.readUInt32LE(40), 6);
  assert.deepEqual([bytes.readInt16LE(44), bytes.readInt16LE(46), bytes.readInt16LE(48)], [-32768, 0, 32767]);
  assert.equal(isSilent([new Int16Array([0, 0])]), true);
});

test('rejects streams without audio and applies stale-session/unload guards', () => {
  assert.equal(hasAudioTracks({ getAudioTracks: () => [] }), false);
  assert.equal(hasAudioTracks({ getAudioTracks: () => [{}] }), true);
  assert.equal(isCurrentGeneration(3, 3), true);
  assert.equal(isCurrentGeneration(2, 3), false);
  assert.equal(shouldWarnBeforeUnload(true, 0, true), true);
  assert.equal(shouldWarnBeforeUnload(false, 10, false), true);
  assert.equal(shouldWarnBeforeUnload(false, 10, true), false);
  assert.equal(shouldWarnBeforeUnload(false, 0, false), false);
});

function createWorklet() {
  let Processor;
  vm.runInNewContext(workletSource(), { sampleRate: 48000, Float32Array, Float64Array, Math,
    AudioWorkletProcessor: class { constructor() { this.port = { onmessage: null, posted: [], postMessage(value) { this.posted.push(value); } }; } },
    registerProcessor: (_name, processor) => { Processor = processor; } });
  return new Processor();
}

function captureTone(frequency, quantumSizes, seconds = 1) {
  const processor = createWorklet(), inputCount = 48000 * seconds;
  let cursor = 0, quantum = 0;
  while (cursor < inputCount) {
    const length = Math.min(quantumSizes[quantum++ % quantumSizes.length], inputCount - cursor);
    const block = Float32Array.from({ length }, (_, i) => Math.sin(2 * Math.PI * frequency * (cursor + i) / 48000));
    processor.process([[block]]); cursor += length;
  }
  processor.flush();
  const output = processor.port.posted.filter((part) => part instanceof Float32Array).flatMap((part) => [...part]);
  return { processor, output };
}

function rmsAt(samples, frequency, start = 300, end = samples.length - 300) {
  let sum = 0;
  for (let i = start; i < end; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / (end - start));
}

test('worklet low-passes before 3:1 decimation and keeps phase across arbitrary render quanta', () => {
  const pass = captureTone(1000, [128, 257, 64, 511]);
  const stop = captureTone(11000, [192, 63, 256, 129]);
  assert.equal(pass.output.length, RATE + 10); // 31 zero samples drain the FIR tail
  assert.equal(stop.output.length, pass.output.length);
  const passRms = rmsAt(pass.output, 1000);
  const stopRms = rmsAt(stop.output, 5000); // 11 kHz aliases to 5 kHz without filtering
  const attenuationDb = 20 * Math.log10(stopRms / passRms);
  assert.ok(passRms > 0.68 && passRms < 0.73, `1 kHz RMS=${passRms}`);
  assert.ok(attenuationDb < -40, `11 kHz alias attenuation=${attenuationDb.toFixed(2)} dB`);
  assert.equal(pass.processor.port.posted.at(-1).flushed, true);
});

test('mixes stereo to mono and flush tail remains bounded', () => {
  const processor = createWorklet();
  processor.process([[Float32Array.from([1, 0, 0, 0]), Float32Array.from([1, 0, 0, 0])]]);
  processor.flush();
  const output = processor.port.posted.find((part) => part instanceof Float32Array);
  assert.equal(output.length, 11);
  assert.ok([...output].every((sample) => Math.abs(sample) <= 1));
});

const recorderSource = fs.readFileSync(require.resolve('../capture/recorder.js'), 'utf8');

function simulatedPage({ source = 'mic', mediaDevices = {}, consent = true } = {}) {
  const page = { gains: [] }, nodes = {}, timers = new Map();
  let timerId = 0;
  function element(id) {
    return nodes[id] ??= { handlers: {}, checked: consent, textContent: '', disabled: false,
      addEventListener(type, handler) { this.handlers[type] = handler; } };
  }
  const gain = () => ({ gain: { value: 1 }, connect() {} });
  const global = {
    document: { getElementById: element, querySelector: () => ({ value: source }) },
    window: { confirm: () => true, addEventListener() {} },
    navigator: { mediaDevices },
    AudioContext: class {
      constructor() {
        this.state = 'running'; this.sampleRate = 48000; this.destination = {};
        this.audioWorklet = { addModule: async () => {} }; page.audioContext = this;
      }
      resume() { return Promise.resolve(); }
      close() { this.state = 'closed'; return Promise.resolve(); }
      createGain() { const node = gain(); page.gains.push(node); return node; }
      createMediaStreamSource() { return gain(); }
    },
    AudioWorkletNode: class {
      constructor() { this.port = { onmessage: null, postMessage() {} }; page.worklet = this; }
      connect() {}
      disconnect() {}
    },
    URL: { createObjectURL() { return 'blob:synthetic'; }, revokeObjectURL() {} },
    Blob, Float32Array, Int16Array,
    MediaStream: class {},
    setInterval() { return 1; }, clearInterval() {},
    setTimeout(action, delay) { const id = ++timerId; timers.set(id, { action, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.runInNewContext(recorderSource, global);
  return Object.assign(page, {
    start: (isTrusted = true) => element('start').handlers.click({ isTrusted }),
    stop: () => element('stop').handlers.click(),
    update: (data) => page.worklet.port.onmessage({ data }),
    runTimeout: (delay) => {
      for (const [id, timer] of [...timers]) if (timer.delay === delay) {
        timers.delete(id);
        timer.action();
      }
    },
    element,
  });
}

function simulatedStream({ display, audio = true } = {}) {
  function track() {
    return { readyState: 'live', stopCount: 0, addEventListener() {},
      stop() { this.readyState = 'ended'; this.stopCount++; },
      getSettings() { return { displaySurface: display }; } };
  }
  const microphones = audio ? [track()] : [];
  const screens = display ? [track()] : [];
  const all = [...microphones, ...screens];
  return { getTracks: () => all, getAudioTracks: () => microphones, getVideoTracks: () => screens };
}

test('no consent or untrusted click never asks for a device', async () => {
  let requests = 0;
  const page = simulatedPage({ consent: false, mediaDevices: { getUserMedia: async () => { requests++; } } });
  await page.start();
  page.element('consent').checked = true;
  await page.start(false);
  assert.equal(requests, 0);
});

test('shared-tab mode rejects capture of an entire screen and releases every track', async () => {
  const stream = simulatedStream({ display: 'monitor' });
  const page = simulatedPage({ source: 'tab', mediaDevices: { getDisplayMedia: async () => stream } });
  await page.start();
  assert.match(page.element('status').textContent, /画面全体/);
  assert.ok(stream.getTracks().every((t) => t.stopCount === 1));
  assert.ok(page.element('save').disabled);
});

test('cancelling acquisition immediately stops sharing and stops a late microphone', async () => {
  const tab = simulatedStream({ display: 'browser' });
  const mic = simulatedStream();
  let supplyMicrophone;
  const delayed = new Promise((resolve) => { supplyMicrophone = resolve; });
  let markMicrophoneRequested;
  const microphoneRequested = new Promise((resolve) => { markMicrophoneRequested = resolve; });
  const page = simulatedPage({ source: 'both', mediaDevices: {
    getDisplayMedia: async () => tab, getUserMedia: () => { markMicrophoneRequested(); return delayed; },
  }});
  const starting = page.start();
  await microphoneRequested;
  page.stop();
  assert.ok(tab.getTracks().every((t) => t.stopCount === 1));
  supplyMicrophone(mic);
  await starting;
  assert.ok(mic.getTracks().every((t) => t.stopCount === 1));
  assert.match(page.element('status').textContent, /取り消し/);
});

test('stop releases capture before flushing, labels lost tail, and retains old audio if restart fails', async () => {
  const stream = simulatedStream();
  let calls = 0;
  const page = simulatedPage({ mediaDevices: { getUserMedia: async () => {
    if (++calls > 1) throw new Error('synthetic startup failure');
    return stream;
  } } });
  await page.start();
  assert.equal(page.element('status').textContent, '録音中');
  page.update(Float32Array.from([0.1, -0.1]));
  page.stop();
  assert.ok(stream.getTracks().every((t) => t.stopCount === 1));
  page.runTimeout(1000);
  assert.match(page.element('status').textContent, /部分音声/);
  assert.equal(page.audioContext.state, 'closed');
  assert.equal(page.element('save').disabled, false);
  await page.start();
  assert.match(page.element('status').textContent, /開始できません/);
  assert.equal(page.element('save').disabled, false);
});

test('simultaneous microphone and tab sources use shared conservative mix gain', async () => {
  const tab = simulatedStream({ display: 'browser' }), mic = simulatedStream();
  const page = simulatedPage({ source: 'both', mediaDevices: {
    getDisplayMedia: async () => tab, getUserMedia: async () => mic,
  } });
  await page.start();
  assert.equal(page.gains[0].gain.value, 0.5);
  page.stop();
  assert.ok(tab.getTracks().every((track) => track.stopCount === 1));
  assert.ok(mic.getTracks().every((track) => track.stopCount === 1));
});
