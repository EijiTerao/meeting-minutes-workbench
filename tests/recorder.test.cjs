const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { RATE, MAX_SAMPLES, PART_SECONDS, TEXT_BLOCK_SECONDS, approvedBridgePath, partName, LongPartWriter, encodeWav, isSilent, hasAudioTracks, isCurrentGeneration, shouldWarnBeforeUnload, workletSource } = require('../capture/recorder.js');
function missingEntry() { const error = new Error('not found'); error.name = 'NotFoundError'; return error; }
async function waitFor(predicate, message) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail(message);
}

test('text-only is default, and opting into WAV resets consent and exposes WAV modes', () => {
  const html = fs.readFileSync(require.resolve('../capture/recorder.html'), 'utf8');
  assert.match(html, /id="textMode"/);
  assert.match(html, /id="saveAudio" type="checkbox"/);
  assert.match(html, /id="includeMic" type="checkbox" checked/);
  const page = simulatedPage({ raw: false, showDirectoryPicker: async () => ({}) });
  assert.equal(page.element('saveAudio').checked, false);
  assert.equal(page.element('rawModes').hidden, true);
  page.element('consent').checked = true;
  page.toggle('saveAudio', true);
  assert.equal(page.element('consent').checked, false);
  assert.equal(page.element('rawModes').hidden, false);
  assert.match(page.element('consentText').textContent, /WAVを保存/);
  page.element('shortMode').checked = true;
  page.element('longMode').checked = false;
  page.element('shortMode').handlers.change();
  assert.equal(page.element('consent').checked, false);
  assert.match(page.element('consentText').textContent, /停止後に自分でWAVを保存/);
  page.element('consent').checked = true;
  page.element('longMode').checked = true;
  page.element('shortMode').checked = false;
  page.element('longMode').handlers.change();
  assert.equal(page.element('consent').checked, false);
  assert.match(page.element('consentText').textContent, /録音中に選択先へWAVパートを保存/);
});

test('pending directory picker blocks duplicate prepare and recording start', async () => {
  let resolvePicker, pickerCalls = 0, captureCalls = 0;
  const pickerResult = new Promise((resolve) => { resolvePicker = resolve; });
  const page = simulatedPage({ showDirectoryPicker: () => { pickerCalls++; return pickerResult; }, mediaDevices: { getUserMedia: async () => { captureCalls++; return simulatedStream(); } } });
  const preparing = page.element('prepare').handlers.click({ isTrusted: true });
  await page.element('prepare').handlers.click({ isTrusted: true });
  assert.equal(page.element('modeChoices').disabled, true);
  await page.start();
  assert.equal(pickerCalls, 1);
  assert.equal(captureCalls, 0);
  resolvePicker({ getDirectoryHandle: async (_name, options) => { if (!options?.create) throw missingEntry(); return { async getFileHandle() { throw missingEntry(); } }; } });
  await preparing;
  assert.equal(page.element('modeChoices').disabled, false);
  assert.equal(captureCalls, 0);
  assert.match(page.element('longReady').textContent, /保存先を準備しました/);
});

test('existing session folder candidate is skipped before creating a fresh folder', async () => {
  let probes = 0, creates = 0;
  const page = simulatedPage({ showDirectoryPicker: async () => ({ async getDirectoryHandle(_name, options) {
    if (options?.create) { creates++; return { async getFileHandle() { throw missingEntry(); } }; }
    if (++probes === 1) return {};
    throw missingEntry();
  } }) });
  await page.element('prepare').handlers.click({ isTrusted: true });
  assert.equal(probes, 2);
  assert.equal(creates, 1);
  assert.match(page.element('longReady').textContent, /保存先を準備しました/);
});

test('long writer serializes correctly named WAV parts and counts only durable closes', async () => {
  const files = new Map();
  const directory = { async getFileHandle(name, options) { if (!options?.create) throw missingEntry(); return { async createWritable() { return {
    async write(blob) { files.set(name, Buffer.from(typeof blob === 'string' ? blob : await blob.arrayBuffer())); }, async close() {}, async abort() {},
  }; } }; } };
  const writer = new LongPartWriter(directory);
  await Promise.all([writer.enqueue([Int16Array.of(1)]), writer.enqueue([Int16Array.of(2)])]);
  assert.deepEqual([...files.keys()], ['part-000001.wav', 'part-000002.wav']);
  assert.equal(writer.closed, 2);
  assert.equal(files.get(partName(2)).toString('ascii', 0, 4), 'RIFF');
  await writer.finalize();
  assert.deepEqual(JSON.parse(files.get('recording-complete.json').toString()), { version: 1, parts: 2 });
});

test('long writer reports slow destination and write faults without overwriting durable parts', async () => {
  const files = new Map(); let release;
  const directory = { async getFileHandle(name, options) { if (!options?.create) throw missingEntry(); return { async createWritable() { return {
    async write(blob) { if (name.endsWith('000001.wav')) await new Promise((resolve) => { release = resolve; }); files.set(name, blob); },
    async close() {}, async abort() {},
  }; } }; } };
  const writer = new LongPartWriter(directory);
  const first = writer.enqueue([Int16Array.of(1)]);
  await waitFor(() => release, 'slow test writer did not start');
  const second = writer.enqueue([Int16Array.of(2)]);
  const third = writer.enqueue([Int16Array.of(3)]);
  await assert.rejects(third, /追いつかない/);
  release();
  await first;
  await assert.rejects(second);
  assert.equal(writer.closed, 1);

  const failing = new LongPartWriter({ async getFileHandle(_name, options) { if (!options?.create) throw missingEntry(); return { async createWritable() { return {
    async write() { throw new Error('disk full'); }, async close() {}, async abort() {},
  }; } }; } });
  await assert.rejects(failing.enqueue([Int16Array.of(1)]), /disk full/);
  assert.equal(failing.closed, 0);
});

test('existing part and completion marker are rejected without replacing prior files', async () => {
  const existing = new Map([['part-000001.wav', Buffer.from('prior-part')], ['recording-complete.json', Buffer.from('prior-marker')]]);
  let creates = 0;
  const directory = { async getFileHandle(name, options) {
    if (!options?.create && existing.has(name)) return { async getFile() { return { size: existing.get(name).length }; } };
    if (!options?.create) throw missingEntry();
    creates++;
    return { async createWritable() { return { async write(data) { existing.set(name, Buffer.from(typeof data === 'string' ? data : await data.arrayBuffer())); }, async close() {}, async abort() {} }; } };
  } };
  const writer = new LongPartWriter(directory);
  await assert.rejects(writer.enqueue([Int16Array.of(1)]), /既存パート/);
  assert.equal(existing.get('part-000001.wav').toString(), 'prior-part');
  assert.equal(creates, 0);
  writer.error = null; writer.chain = Promise.resolve(); writer.closed = 1;
  await assert.rejects(writer.finalize(), /完了マーカーが既に存在/);
  assert.equal(existing.get('recording-complete.json').toString(), 'prior-marker');
  assert.equal(creates, 0);
});

test('writer removes only a provably empty failed output and preserves nonempty data', async () => {
  const removed = [];
  const directory = {
    async getFileHandle(_name, options) { if (!options?.create) throw missingEntry(); return {
      async createWritable() { return { async write() { throw new Error('write failed'); }, async close() {}, async abort() {} }; },
      async getFile() { return { size: 0 }; },
    }; },
    async removeEntry(name) { removed.push(name); },
  };
  const writer = new LongPartWriter(directory);
  await assert.rejects(writer.enqueue([Int16Array.of(1)]), /write failed/);
  assert.deepEqual(removed, ['part-000001.wav']);
});

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

function simulatedPage({ source = 'mic', mediaDevices = {}, consent = true, showDirectoryPicker, raw = true, location, fetch: fetchMock } = {}) {
  const page = { gains: [] }, nodes = {}, timers = new Map();
  const initialChecks = { consent, inPerson: source === 'mic', online: source !== 'mic',
    tabOnly: ['tab', 'both'].includes(source), includeMic: ['mic', 'both', 'both-monitor'].includes(source), saveAudio: raw, shortMode: true, textMode: !raw };
  let timerId = 0;
  function element(id) {
    return nodes[id] ??= { handlers: {}, checked: initialChecks[id] || false, textContent: '', disabled: id === 'stop' || id === 'save', hidden: false,
      addEventListener(type, handler) { this.handlers[type] = handler; }, appendChild(child) { this.children ??= []; this.children.push(child); }, insertAdjacentText(_where, text) { this.textContent += text; } };
  }
  const gain = () => ({ gain: { value: 1 }, connect() {} });
  const global = {
    document: { getElementById: element, createElement: () => ({ textContent: '', click() {} }) },
    window: { confirm: () => true, addEventListener() {}, showDirectoryPicker },
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
    URL: class extends URL { static createObjectURL() { return 'blob:synthetic'; } static revokeObjectURL() {} },
    Blob, Float32Array, Int16Array,
    MediaStream: class {},
    location,
    ...(fetchMock ? { fetch: fetchMock } : {}),
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
    selectMeeting(id) {
      element('online').checked = id === 'online';
      element('inPerson').checked = id === 'inPerson';
      element(id).handlers.change();
    },
    toggle(id, checked) {
      element(id).checked = checked;
      element(id).handlers.change();
    },
  });
}

test('online selection defaults to whole-PC playback and microphone ON, independently', async () => {
  const html = fs.readFileSync(require.resolve('../capture/recorder.html'), 'utf8');
  assert.match(html, /id="inPerson"[^>]+checked/);
  assert.match(html, /id="includeMic"[^>]+checked/);
  assert.doesNotMatch(html, /id="tabOnly"[^>]+checked/);
  let options;
  let microphoneCalls = 0;
  const page = simulatedPage({ mediaDevices: {
    getDisplayMedia: async (value) => { options = value; return simulatedStream({ display: 'monitor' }); },
    getUserMedia: async () => { microphoneCalls++; return simulatedStream(); },
  } });
  assert.equal(page.element('onlineChoices').hidden, true);
  page.selectMeeting('online');
  assert.equal(page.element('onlineChoices').hidden, false);
  assert.match(page.element('consentText').textContent, /PCの再生音.*通知音/);
  assert.match(page.element('consentText').textContent, /マイク/);
  assert.equal(page.element('consent').checked, false);
  await page.start();
  assert.equal(options, undefined);
  page.element('consent').checked = true;
  await page.start();
  assert.equal(options.video.displaySurface, 'monitor');
  assert.equal(options.systemAudio, 'include');
  assert.equal(microphoneCalls, 1);
  assert.equal(page.element('choices').disabled, true);
  page.stop(); page.update({ flushed: true });
});

test('text-only capture posts bounded PCM chunks and confirms host persistence on stop', async () => {
  const calls = [], pageLocation = { protocol: 'http:', hostname: '127.0.0.1', pathname: '/r/test-token/', origin: 'http://127.0.0.1:8765', href: 'http://127.0.0.1:8765/r/test-token/' };
  const page = simulatedPage({ raw: false, location: pageLocation, mediaDevices: { getUserMedia: async () => simulatedStream() }, fetch: async (path, options = {}) => {
    calls.push({ path, options });
    if (path === 'api/status') return { ok: true, json: async () => ({ ready: true, message: '認識準備済み' }) };
    if (path === 'api/start') return { ok: true, json: async () => ({ id: 'session-1', filename: 'meeting.txt' }) };
    if (path === 'api/chunk') return { ok: true, json: async () => ({ text: '保存された発言', filename: 'meeting.txt' }) };
    if (path === 'api/stop') return { ok: true, json: async () => ({ status: 'complete', filename: 'meeting.txt' }) };
    throw new Error(`unexpected path ${path}`);
  } });
  await waitFor(() => page.element('bridgeStatus').textContent === '認識準備済み', `bridge status was not read: ${page.element('bridgeStatus').textContent}`);
  page.element('consent').checked = true;
  await page.start();
  assert.equal(page.element('status').textContent, '文字起こし中（原音は保存しません）');
  page.update(new Float32Array(RATE * TEXT_BLOCK_SECONDS).fill(0.1));
  await waitFor(() => calls.some((call) => call.path === 'api/chunk'), 'text chunk was not sent');
  page.stop(); page.update({ flushed: true });
  await waitFor(() => /文字起こしの保存が完了/.test(page.element('status').textContent), 'host did not confirm completed save');
  assert.deepEqual(calls.map((call) => call.path), ['api/status', 'api/start', 'api/chunk', 'api/stop']);
  assert.equal(calls[1].options.body, '{}');
  assert.equal(calls[2].options.headers['X-Recording-Session'], 'session-1');
  assert.equal(calls[2].options.headers['Content-Type'], 'application/octet-stream');
  assert.equal(calls[2].options.body.byteLength, RATE * TEXT_BLOCK_SECONDS * 2);
  assert.equal(calls[3].options.headers['X-Recording-Session'], 'session-1');
  assert.match(page.element('detail').children[0].textContent, /meeting.txt: 保存された発言/);
  assert.equal(page.element('save').disabled, true);
});

test('text save failure is not reported as success and only the approved loopback path is allowed', async () => {
  assert.equal(approvedBridgePath({ protocol: 'http:', hostname: '127.0.0.1', pathname: '/r/abc/' }), 'api/');
  assert.equal(approvedBridgePath({ protocol: 'https:', hostname: '127.0.0.1', pathname: '/r/abc/' }), null);
  assert.equal(approvedBridgePath({ protocol: 'http:', hostname: 'localhost', pathname: '/r/abc/' }), null);
  assert.equal(approvedBridgePath({ protocol: 'http:', hostname: '127.0.0.1', pathname: '/other/' }), null);
  const calls = [], page = simulatedPage({ raw: false, location: { protocol: 'http:', hostname: '127.0.0.1', pathname: '/r/abc/', origin: 'http://127.0.0.1:1234', href: 'http://127.0.0.1:1234/r/abc/' }, mediaDevices: { getUserMedia: async () => simulatedStream() }, fetch: async (path, options = {}) => {
    calls.push(path);
    if (path.endsWith('/status')) return { ok: true, json: async () => ({ ready: true }) };
    if (path.endsWith('/start')) return { ok: true, json: async () => ({ id: 'failed-session' }) };
    if (path.endsWith('/stop')) return { ok: false, status: 503, json: async () => ({}) };
    return { ok: false, status: 503, json: async () => ({}) };
  } });
  await waitFor(() => page.element('bridgeStatus').textContent === '文字起こし保存を利用できます。', `bridge readiness was not checked: ${page.element('bridgeStatus').textContent}`);
  page.element('consent').checked = true; await page.start();
  page.update(new Float32Array(RATE * TEXT_BLOCK_SECONDS).fill(0.1));
  await waitFor(() => calls.includes('api/chunk'), 'chunk failure was not attempted');
  page.stop(); page.update({ flushed: true });
  await waitFor(() => /成功扱いにはしていません/.test(page.element('status').textContent), 'failed save was incorrectly accepted');
  assert.notEqual(page.element('status').textContent, '文字起こしの保存が完了しました。');
});

test('unconfigured text-only mode never requests audio or implicitly switches to WAV', async () => {
  let micRequests = 0;
  const page = simulatedPage({ raw: false, location: { protocol: 'http:', hostname: '127.0.0.1', pathname: '/r/unconfigured/', origin: 'http://127.0.0.1:8765', href: 'http://127.0.0.1:8765/r/unconfigured/' }, mediaDevices: { getUserMedia: async () => { micRequests++; return simulatedStream(); } }, fetch: async (path) => {
    assert.equal(path, 'api/status');
    return { ok: true, json: async () => ({ ready: false, message: '認識器を設定してください。' }) };
  } });
  await waitFor(() => page.element('bridgeStatus').textContent === '認識器を設定してください。', 'unconfigured status was not shown');
  page.element('consent').checked = true; await page.start();
  assert.equal(micRequests, 0);
  assert.equal(page.element('saveAudio').checked, false);
  assert.equal(page.element('save').disabled, true);
  assert.match(page.element('status').textContent, /原音保存を明示選択/);
  page.toggle('saveAudio', true);
  assert.equal(page.element('consent').checked, false);
  page.element('consent').checked = true; await page.start();
  assert.equal(micRequests, 1);
  assert.match(page.element('status').textContent, /WAVのみ録音中/);
  page.stop(); page.update({ flushed: true });
});

test('empty recognition is not called a successful transcript save', async () => {
  const page = simulatedPage({ raw: false, location: { protocol: 'http:', hostname: '127.0.0.1', pathname: '/r/empty/', origin: 'http://127.0.0.1:8765', href: 'http://127.0.0.1:8765/r/empty/' }, mediaDevices: { getUserMedia: async () => simulatedStream() }, fetch: async (path) => {
    if (path === 'api/status') return { ok: true, json: async () => ({ ready: true }) };
    if (path === 'api/start') return { ok: true, json: async () => ({ id: 'empty-session' }) };
    if (path === 'api/chunk') return { ok: true, json: async () => ({ text: '', filename: 'empty.txt' }) };
    return { ok: true, json: async () => ({ status: 'complete', has_text: false, filename: 'empty.txt' }) };
  } });
  await waitFor(() => page.element('bridgeStatus').textContent === '文字起こし保存を利用できます。', 'bridge did not become ready');
  page.element('consent').checked = true; await page.start();
  page.update(new Float32Array(RATE).fill(0)); page.stop(); page.update({ flushed: true });
  await waitFor(() => page.element('status').textContent === '認識できた文字はありません。', 'empty recognition status was not shown');
  assert.doesNotMatch(page.element('status').textContent, /保存が完了/);
});

test('text plus long WAV finalizes both durable outputs before reporting completion', async () => {
  const calls = [], files = new Map();
  const directory = { async getFileHandle(name, options) {
    if (!options?.create && !files.has(name)) throw missingEntry();
    return { async getFile() { return { size: files.get(name)?.length || 0 }; }, async createWritable() { return {
      async write(value) { files.set(name, Buffer.from(typeof value === 'string' ? value : await value.arrayBuffer())); }, async close() {}, async abort() {},
    }; } };
  } };
  const page = simulatedPage({ location: { protocol: 'http:', hostname: '127.0.0.1', pathname: '/r/long/', origin: 'http://127.0.0.1:8765', href: 'http://127.0.0.1:8765/r/long/' }, showDirectoryPicker: async () => ({ async getDirectoryHandle(_name, options) { if (!options?.create) throw missingEntry(); return directory; } }), mediaDevices: { getUserMedia: async () => simulatedStream() }, fetch: async (path, options = {}) => {
    calls.push({ path, options });
    if (path === 'api/status') return { ok: true, json: async () => ({ ready: true }) };
    if (path === 'api/start') return { ok: true, json: async () => ({ id: 'long-session' }) };
    if (path === 'api/chunk') return { ok: true, json: async () => ({ text: '区間の発言', filename: 'long.txt' }) };
    return { ok: true, json: async () => ({ status: 'complete', has_text: true, filename: 'long.txt' }) };
  } });
  await waitFor(() => page.element('bridgeStatus').textContent === '文字起こし保存を利用できます。', 'bridge did not become ready');
  page.toggle('longMode', true); page.element('saveAudio').checked = true; await page.element('prepare').handlers.click({ isTrusted: true });
  page.element('consent').checked = true; await page.start();
  page.update(new Float32Array(RATE * PART_SECONDS).fill(0.1)); page.stop(); page.update({ flushed: true });
  await waitFor(() => /文字起こしとWAVの保存が完了/.test(page.element('status').textContent), 'both outputs were not finalized');
  assert.ok(files.has('part-000001.wav'));
  assert.ok(files.has('recording-complete.json'));
  assert.equal(calls.at(-1).path, 'api/stop');
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), { complete: true });
});

test('text plus short WAV leaves the audio for explicit manual saving', async () => {
  const page = simulatedPage({ location: { protocol: 'http:', hostname: '127.0.0.1', pathname: '/r/short/', origin: 'http://127.0.0.1:8765', href: 'http://127.0.0.1:8765/r/short/' }, mediaDevices: { getUserMedia: async () => simulatedStream() }, fetch: async (path) => {
    if (path === 'api/status') return { ok: true, json: async () => ({ ready: true }) };
    if (path === 'api/start') return { ok: true, json: async () => ({ id: 'short-session' }) };
    if (path === 'api/chunk') return { ok: true, json: async () => ({ text: '区間の発言' }) };
    return { ok: true, json: async () => ({ status: 'complete', has_text: true }) };
  } });
  await waitFor(() => page.element('bridgeStatus').textContent === '文字起こし保存を利用できます。', 'bridge did not become ready');
  page.element('consent').checked = true; await page.start();
  page.update(new Float32Array(RATE).fill(0.1)); page.stop(); page.update({ flushed: true });
  await waitFor(() => page.element('status').textContent === '文字起こし保存済み。WAVは手動保存できます。', 'manual WAV handoff was not shown');
  assert.equal(page.element('save').disabled, false);
  assert.match(page.element('detail').children.map((item) => item.textContent).join('\n'), /WAV: 音声を手動保存できます/);
});

test('audio source ending closes a text session as incomplete', async () => {
  const requests = [], stream = simulatedStream();
  const page = simulatedPage({ raw: false, location: { protocol: 'http:', hostname: '127.0.0.1', pathname: '/r/ended/', origin: 'http://127.0.0.1:8765', href: 'http://127.0.0.1:8765/r/ended/' }, mediaDevices: { getUserMedia: async () => stream }, fetch: async (path, options = {}) => {
    requests.push({ path, options });
    if (path === 'api/status') return { ok: true, json: async () => ({ ready: true }) };
    if (path === 'api/start') return { ok: true, json: async () => ({ id: 'ended-session' }) };
    if (path === 'api/chunk') return { ok: true, json: async () => ({ text: '部分発言' }) };
    return { ok: true, json: async () => ({ status: 'incomplete', filename: 'ended.txt', has_text: true }) };
  } });
  await waitFor(() => page.element('bridgeStatus').textContent === '文字起こし保存を利用できます。', 'bridge did not become ready');
  page.element('consent').checked = true; await page.start(); page.update(new Float32Array(RATE).fill(0.1));
  const track = stream.getAudioTracks()[0]; track.readyState = 'ended'; track.listeners.ended(); page.update({ flushed: true });
  await waitFor(() => requests.some((request) => request.path === 'api/stop'), 'incomplete stop was not sent');
  assert.deepEqual(JSON.parse(requests.find((request) => request.path === 'api/stop').options.body), { complete: false });
  await waitFor(() => /部分文字起こしは区間ごとに保存済み/.test(page.element('status').textContent), `partial text status unexpected: ${page.element('status').textContent}`);
});

test('online mic and optional tab scope change independently, with fresh consent', async () => {
  let options;
  let microphoneCalls = 0;
  const page = simulatedPage({ mediaDevices: {
    getDisplayMedia: async (value) => { options = value; return simulatedStream({ display: 'browser' }); },
    getUserMedia: async () => { microphoneCalls++; return simulatedStream(); },
  } });
  page.selectMeeting('online');
  page.toggle('includeMic', true);
  assert.equal(page.element('tabOnly').checked, false);
  assert.match(page.element('consentText').textContent, /PCの再生音.*とマイク/);
  page.element('consent').checked = true;
  page.toggle('tabOnly', true);
  assert.equal(page.element('includeMic').checked, true);
  assert.equal(page.element('consent').checked, false);
  assert.match(page.element('consentText').textContent, /共有タブの音声とマイク/);
  page.element('consent').checked = true;
  await page.start();
  assert.equal(options.video.displaySurface, 'browser');
  assert.equal(options.systemAudio, 'exclude');
  assert.equal(microphoneCalls, 1);
  page.stop(); page.update({ flushed: true });
  page.selectMeeting('inPerson');
  assert.equal(page.element('onlineChoices').hidden, true);
  page.selectMeeting('online');
  assert.equal(page.element('tabOnly').checked, false);
  assert.equal(page.element('includeMic').checked, true);
  assert.equal(page.element('consent').checked, false);
});

test('online with microphone on does not open the mic if the PC audio is unavailable', async () => {
  const stream = simulatedStream({ display: 'monitor', audio: false });
  let microphoneCalls = 0;
  const page = simulatedPage({ source: 'both-monitor', mediaDevices: {
    getDisplayMedia: async () => stream,
    getUserMedia: async () => { microphoneCalls++; return simulatedStream(); },
  } });
  await page.start();
  assert.match(page.element('status').textContent, /音声トラックがありません/);
  assert.equal(microphoneCalls, 0);
  assert.ok(stream.getTracks().every((track) => track.stopCount === 1));
});

function simulatedStream({ display, audio = true } = {}) {
  function track(settings = {}) {
    return { readyState: 'live', stopCount: 0, listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; },
      stop() { this.readyState = 'ended'; this.stopCount++; },
      getSettings() { return settings; } };
  }
  const audioTracks = audio ? [track()] : [];
  const screens = display ? [track({ displaySurface: display })] : [];
  const all = [...audioTracks, ...screens];
  return { getTracks: () => all, getAudioTracks: () => audioTracks, getVideoTracks: () => screens };
}

test('no consent or untrusted click never asks for a device', async () => {
  let requests = 0;
  const page = simulatedPage({ consent: false, mediaDevices: { getUserMedia: async () => { requests++; } } });
  assert.equal(page.element('longMode').checked, false);
  assert.equal(page.element('shortMode').checked, true);
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

test('monitor mode requests only explicitly selected display audio and never requests microphone', async () => {
  const monitor = simulatedStream({ display: 'monitor' });
  let displayOptions, microphoneCalls = 0;
  const page = simulatedPage({ source: 'monitor', mediaDevices: {
    getDisplayMedia: async (options) => { displayOptions = options; return monitor; },
    getUserMedia: async () => { microphoneCalls++; return simulatedStream(); },
  } });
  await page.start();
  assert.equal(page.element('status').textContent, 'WAVのみ録音中（文字起こしは行いません）');
  assert.equal(displayOptions.video.displaySurface, 'monitor');
  assert.equal(displayOptions.systemAudio, 'include');
  assert.equal(displayOptions.windowAudio, 'exclude');
  assert.equal(displayOptions.monitorTypeSurfaces, 'include');
  assert.equal(microphoneCalls, 0);
  assert.equal(page.gains[0].gain.value, 1);
  page.stop(); page.update({ flushed: true });
});

test('monitor capture can explicitly mix microphone when that combined mode is selected', async () => {
  const monitor = simulatedStream({ display: 'monitor' }), mic = simulatedStream();
  let microphoneCalls = 0;
  const page = simulatedPage({ source: 'both-monitor', mediaDevices: {
    getDisplayMedia: async () => monitor,
    getUserMedia: async () => { microphoneCalls++; return mic; },
  } });
  await page.start();
  assert.equal(microphoneCalls, 1);
  assert.equal(page.gains[0].gain.value, 0.5);
  page.stop(); page.update({ flushed: true });
});

test('browser-tab audio stays scoped to a browser surface and excludes system audio', async () => {
  const tab = simulatedStream({ display: 'browser' });
  let options, microphoneCalls = 0;
  const page = simulatedPage({ source: 'tab', mediaDevices: {
    getDisplayMedia: async (value) => { options = value; return tab; },
    getUserMedia: async () => { microphoneCalls++; return simulatedStream(); },
  } });
  await page.start();
  assert.equal(options.video.displaySurface, 'browser');
  assert.equal(options.systemAudio, 'exclude');
  assert.equal(options.windowAudio, 'exclude');
  assert.equal(options.monitorTypeSurfaces, 'exclude');
  assert.equal(microphoneCalls, 0);
  page.stop(); page.runTimeout(1000);
});

test('screen mode rejects a distinct selected surface and missing returned audio without fallback', async () => {
  let micCalls = 0;
  const wrongSurface = simulatedPage({ source: 'monitor', mediaDevices: {
    getDisplayMedia: async () => simulatedStream({ display: 'window' }),
    getUserMedia: async () => { micCalls++; return simulatedStream(); },
  } });
  await wrongSurface.start();
  assert.match(wrongSurface.element('status').textContent, /自動切替しません/);
  assert.equal(micCalls, 0);

  const noAudio = simulatedPage({ source: 'monitor', mediaDevices: {
    getDisplayMedia: async () => simulatedStream({ display: 'monitor', audio: false }),
  } });
  await noAudio.start();
  assert.match(noAudio.element('status').textContent, /音声トラックがありません/);
  assert.equal(noAudio.element('save').disabled, true);
});

test('unexpected display audio track end stops the session and keeps captured partial audio', async () => {
  const monitor = simulatedStream({ display: 'monitor' });
  const page = simulatedPage({ source: 'monitor', mediaDevices: { getDisplayMedia: async () => monitor } });
  await page.start();
  page.update(Float32Array.from([0.2, -0.2]));
  monitor.getAudioTracks()[0].listeners.ended();
  assert.ok(monitor.getTracks().every((track) => track.stopCount === 1));
  page.runTimeout(1000);
  assert.match(page.element('status').textContent, /部分音声/);
  assert.equal(page.element('save').disabled, false);
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
  assert.equal(page.element('status').textContent, 'WAVのみ録音中（文字起こしは行いません）');
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

test('long mode has no 15 minute stop timer and flushes its final short tail', async () => {
  const saved = [];
  const directory = { async getFileHandle(name, options) { if (!options?.create) throw missingEntry(); return { async createWritable() { return {
    async write(blob) { saved.push([name, Buffer.from(typeof blob === 'string' ? blob : await blob.arrayBuffer())]); }, async close() {}, async abort() {},
  }; } }; } };
  const page = simulatedPage({ showDirectoryPicker: async () => ({ getDirectoryHandle: async (_name, options) => { if (!options?.create) throw missingEntry(); return directory; } }), mediaDevices: { getUserMedia: async () => simulatedStream() } });
  assert.equal(page.element('longMode').checked, false);
  page.element('saveAudio').checked = true;
  page.element('longMode').checked = true;
  await page.element('prepare').handlers.click({ isTrusted: true });
  page.element('longMode').checked = true;
  await page.start();
  assert.equal(page.element('status').textContent, 'WAVのみ録音中（文字起こしは行いません）');
  assert.equal(typeof page.element('start').handlers.click, 'function');
  page.update(new Float32Array(RATE * PART_SECONDS).fill(0.1));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(saved.length, 1);
  assert.equal(saved[0][0], 'part-000001.wav');
  page.runTimeout(15 * 60 * 1000);
  assert.equal(page.element('status').textContent, 'WAVのみ録音中（文字起こしは行いません）');
  page.update(Float32Array.from([0.2, 0.3]));
  page.stop();
  page.update({ flushed: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(saved.length, 3);
  assert.equal(saved[1][0], 'part-000002.wav');
  assert.equal(saved[2][0], 'recording-complete.json');
  assert.match(page.element('detail').textContent, /完了マーカーあり/);
  assert.equal(page.element('save').disabled, true);
  assert.equal(saved.some(([name]) => name === 'recording-complete.json'), true);
});

test('long session folder is consumed once; repeated recording needs a fresh directory', async () => {
  const dirs = [new Map(), new Map()]; let pickerCalls = 0;
  const makeDir = (files) => ({ async getFileHandle(name, options) { if (!options?.create) throw missingEntry(); return { async createWritable() { return {
    async write(blob) { files.set(name, Buffer.from(typeof blob === 'string' ? blob : await blob.arrayBuffer())); }, async close() {}, async abort() {},
  }; } }; } });
  const page = simulatedPage({ showDirectoryPicker: async () => ({ getDirectoryHandle: async (_name, options) => { if (!options?.create) throw missingEntry(); return makeDir(dirs[pickerCalls++]); } }), mediaDevices: { getUserMedia: async () => simulatedStream() } });
  await page.element('prepare').handlers.click({ isTrusted: true });
  page.element('longMode').checked = true;
  await page.start(); page.update(new Float32Array(RATE * PART_SECONDS).fill(0.1));
  assert.equal(page.element('modeChoices').disabled, true);
  assert.equal(page.element('modeChoices').disabled, true);
  await new Promise((resolve) => setImmediate(resolve));
  page.stop(); page.update({ flushed: true }); await new Promise((resolve) => setImmediate(resolve));
  const originalPart = dirs[0].get('part-000001.wav');
  assert.ok(originalPart);
  await page.start();
  assert.match(page.element('status').textContent, /先に長時間保存先/);
  await page.element('prepare').handlers.click({ isTrusted: true });
  await page.start(); page.update(new Float32Array(RATE * PART_SECONDS).fill(0.2));
  await new Promise((resolve) => setImmediate(resolve));
  page.stop(); page.update({ flushed: true }); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(dirs[0].get('part-000001.wav'), originalPart);
  assert.ok(dirs[1].has('part-000001.wav'));
});

test('delayed writer blocks recording and directory preparation until settle; zero parts get no marker', async () => {
  let releaseWrite, pickerCalls = 0;
  const files = new Map();
  const directory = { async getFileHandle(name, options) { if (!options?.create) throw missingEntry(); return { async createWritable() { return {
    async write(blob) { if (name.endsWith('.wav')) await new Promise((resolve) => { releaseWrite = resolve; }); files.set(name, Buffer.from(typeof blob === 'string' ? blob : await blob.arrayBuffer())); }, async close() {}, async abort() {},
  }; } }; } };
  const page = simulatedPage({ showDirectoryPicker: async () => { pickerCalls++; return { getDirectoryHandle: async (_name, options) => { if (!options?.create) throw missingEntry(); return directory; } }; }, mediaDevices: { getUserMedia: async () => simulatedStream() } });
  await page.element('prepare').handlers.click({ isTrusted: true });
  page.element('longMode').checked = true;
  await page.start(); page.update(new Float32Array(RATE * PART_SECONDS).fill(0.1));
  await waitFor(() => releaseWrite, 'recorder writer did not start');
  page.stop(); page.update({ flushed: true });
  assert.equal(page.element('modeChoices').disabled, true);
  await page.start();
  await page.element('prepare').handlers.click({ isTrusted: true });
  assert.equal(pickerCalls, 1);
  assert.match(page.element('status').textContent, /保存パートの確定を待っています/);
  releaseWrite(); await waitFor(() => !page.element('modeChoices').disabled, 'finish did not unlock mode selection');
  assert.equal(files.has('recording-complete.json'), true);
  assert.equal(page.element('modeChoices').disabled, false);

  let emptyCalls = 0;
  const empty = simulatedPage({ showDirectoryPicker: async () => ({ getDirectoryHandle: async (_name, options) => { if (!options?.create) throw missingEntry(); return { async getFileHandle(name, createOptions) { if (!createOptions?.create) throw missingEntry(); emptyCalls++; return { async createWritable() { return { async write() {}, async close() {}, async abort() {} }; } }; } }; } }), mediaDevices: { getUserMedia: async () => simulatedStream() } });
  await empty.element('prepare').handlers.click({ isTrusted: true });
  empty.element('longMode').checked = true; await empty.start(); empty.stop(); empty.update({ flushed: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(emptyCalls, 0);
  assert.match(empty.element('detail').textContent, /完了マーカーは作成していません/);
  assert.match(empty.element('status').textContent, /保存できる音声はありません/);
});
