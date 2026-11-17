(function (root) {
  'use strict';

  const RATE = 16000;
  const MAX_SECONDS = 15 * 60;
  const MAX_SAMPLES = RATE * MAX_SECONDS;
  const SILENCE_RMS = 0.0001;
  const PART_SECONDS = 5;
  const MAX_PENDING_PARTS = 2;
  const TEXT_BLOCK_SECONDS = 20;
  const MAX_TEXT_WAITING = 2;
  const partName = (sequence) => `part-${String(sequence).padStart(6, '0')}.wav`;

  // Writes are serialized; a slow destination never creates an unbounded audio queue.
  class LongPartWriter {
    constructor(directory) { this.directory = directory; this.sequence = 0; this.closed = 0; this.pending = 0; this.chain = Promise.resolve(); this.error = null; }
    enqueue(samples) {
      if (this.error) return Promise.reject(this.error);
      if (!samples.length) return Promise.resolve();
      if (this.pending >= MAX_PENDING_PARTS) {
        this.error = new Error('保存先への書込みが追いつかないため録音を停止しました。未保存の末尾があります。');
        return Promise.reject(this.error);
      }
      const sequence = ++this.sequence;
      this.pending++;
      this.chain = this.chain.then(async () => {
        if (this.error) throw this.error;
        const name = partName(sequence);
        let handle = null, writable = null;
        try {
          try {
            await this.directory.getFileHandle(name);
            throw new Error(`既存パート ${name} を上書きしません。`);
          } catch (error) {
            if (error.message === `既存パート ${name} を上書きしません。` || error.name !== 'NotFoundError') throw error;
          }
          handle = await this.directory.getFileHandle(name, { create: true });
          writable = await handle.createWritable();
          await writable.write(encodeWav(samples)); await writable.close();
        } catch (error) {
          if (writable) try { await writable.abort(); } catch (_) {}
          // A session directory is freshly allocated and single-use. Remove only a
          // provably empty uncommitted entry; never remove nonempty or unknown data.
          if (handle && this.directory.removeEntry) try {
            const file = await handle.getFile();
            if (file.size === 0) await this.directory.removeEntry(name);
          } catch (_) {}
          throw error;
        }
        this.closed++;
      }).catch((error) => { this.error = error; throw error; }).finally(() => { this.pending--; });
      return this.chain;
    }
    async flush(samples) { if (samples.length && !this.error) this.enqueue(samples); await this.chain; if (this.error) throw this.error; }
    async finalize() {
      await this.chain;
      if (this.error) throw this.error;
      const name = 'recording-complete.json';
      let handle = null, writable = null;
      try {
        try {
          await this.directory.getFileHandle(name);
          throw new Error('完了マーカーが既に存在するため上書きしません。');
        } catch (error) { if (error.message === '完了マーカーが既に存在するため上書きしません。' || error.name !== 'NotFoundError') throw error; }
        handle = await this.directory.getFileHandle(name, { create: true });
        writable = await handle.createWritable();
        await writable.write(JSON.stringify({ version: 1, parts: this.closed })); await writable.close();
      } catch (error) {
        if (writable) try { await writable.abort(); } catch (_) {}
        if (handle && this.directory.removeEntry) try {
          const file = await handle.getFile();
          if (file.size === 0) await this.directory.removeEntry(name);
        } catch (_) {}
        throw error;
      }
    }
  }
  const hasAudioTracks = (stream) => !!stream && stream.getAudioTracks().length > 0;
  const isCurrentGeneration = (candidate, current) => candidate === current;
  const shouldWarnBeforeUnload = (recording, samples, saved) => recording || (samples > 0 && !saved);
  function approvedBridgePath(location) {
    if (!location || location.protocol !== 'http:' || location.hostname !== '127.0.0.1' || !/^\/r\/[A-Za-z0-9_-]+\/$/.test(location.pathname)) return null;
    return 'api/';
  }

  function encodeWav(samples, sampleRate = RATE) {
    const parts = Array.isArray(samples) ? samples : [samples];
    if (!parts.length || parts.some((part) => !(part instanceof Float32Array) && !(part instanceof Int16Array))) throw new Error('音声データがありません');
    const length = parts.reduce((total, part) => total + part.length, 0);
    if (length === 0) throw new Error('音声データがありません');
    if (length > sampleRate * MAX_SECONDS) throw new Error('録音上限を超えています');
    const buffer = new ArrayBuffer(44 + length * 2);
    const view = new DataView(buffer);
    const put = (offset, value) => { for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i)); };
    put(0, 'RIFF'); view.setUint32(4, 36 + length * 2, true); put(8, 'WAVE');
    put(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true);
    view.setUint16(34, 16, true); put(36, 'data'); view.setUint32(40, length * 2, true);
    let offset = 0;
    for (const part of parts) {
      const isFloat = part instanceof Float32Array;
      for (let i = 0; i < part.length; i++, offset++) {
        if (!isFloat) { view.setInt16(44 + offset * 2, part[i], true); continue; }
        const value = Math.max(-1, Math.min(1, part[i]));
        view.setInt16(44 + offset * 2, value < 0 ? value * 32768 : value * 32767, true);
      }
    }
    return new Blob([buffer], { type: 'audio/wav' });
  }

  function isSilent(samples) {
    let energy = 0;
    const parts = Array.isArray(samples) ? samples : [samples];
    let count = 0;
    for (const part of parts) {
      const scale = part instanceof Int16Array ? 32768 : 1;
      for (let i = 0; i < part.length; i++) { const value = part[i] / scale; energy += value * value; }
      count += part.length;
    }
    return count === 0 || Math.sqrt(energy / count) < SILENCE_RMS;
  }

  function workletSource() {
    return `class LocalPcmRecorder extends AudioWorkletProcessor {
      constructor() {
        super(); this.ratio = sampleRate / ${RATE}; this.pos = 0; this.chunk = [];
        // 63-tap Blackman-windowed low-pass, 6 kHz cutoff at 48 kHz.
        // This removes energy above the 8 kHz output Nyquist before 3:1 decimation.
        this.taps = 63; this.delay = new Float64Array(this.taps); this.write = 0;
        this.coeff = new Float64Array(this.taps);
        const middle = (this.taps - 1) / 2, cutoff = 6000 / sampleRate;
        let total = 0;
        for (let k = 0; k < this.taps; k++) {
          const x = k - middle;
          const sinc = x === 0 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * x) / (Math.PI * x);
          const window = 0.42 - 0.5 * Math.cos(2 * Math.PI * k / (this.taps - 1)) + 0.08 * Math.cos(4 * Math.PI * k / (this.taps - 1));
          this.coeff[k] = sinc * window; total += this.coeff[k];
        }
        for (let k = 0; k < this.taps; k++) this.coeff[k] /= total;
        this.port.onmessage = (event) => { if (event.data && event.data.flush) this.flush(); };
      }
      push(value) {
        this.delay[this.write] = value;
        this.write = (this.write + 1) % this.taps;
        this.pos++;
        if (this.pos < this.ratio) return;
        this.pos -= this.ratio;
        let filtered = 0, index = this.write;
        for (let k = 0; k < this.taps; k++) {
          index = (index - 1 + this.taps) % this.taps;
          filtered += this.delay[index] * this.coeff[k];
        }
        this.chunk.push(filtered);
      }
      process(inputs) {
        const channels = inputs[0]; if (!channels || !channels.length) return true;
        const n = channels[0].length;
        for (let i = 0; i < n; i++) {
          let mono = 0; for (let c = 0; c < channels.length; c++) mono += channels[c][i] || 0;
          this.push(mono / channels.length);
        }
        if (this.chunk.length >= ${RATE}) { this.port.postMessage(Float32Array.from(this.chunk)); this.chunk.length = 0; }
        return true;
      }
      flush() {
        // Drain the causal FIR with zeros; the bounded extension retains the real input tail.
        for (let i = 0; i < (this.taps - 1) / 2; i++) this.push(0);
        this.port.postMessage(Float32Array.from(this.chunk)); this.chunk.length = 0; this.port.postMessage({ flushed: true });
      }
    }
    registerProcessor('local-pcm-recorder', LocalPcmRecorder);`;
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { RATE, MAX_SECONDS, MAX_SAMPLES, PART_SECONDS, MAX_PENDING_PARTS, TEXT_BLOCK_SECONDS, MAX_TEXT_WAITING, approvedBridgePath, partName, LongPartWriter, encodeWav, isSilent, hasAudioTracks, isCurrentGeneration, shouldWarnBeforeUnload, workletSource };
    return;
  }

  const $ = (id) => document.getElementById(id);
  const startButton = $('start'), stopButton = $('stop'), saveButton = $('save'), prepareButton = $('prepare');
  const status = $('status'), detail = $('detail'), timer = $('timer');
  let context = null, node = null, streams = [], chunks = [], sampleCount = 0, startedAt = 0, interval = null;
  let active = false, starting = false, preparing = false, persisting = false, partial = false, pendingFinish = null;
  let setupTimeout = null, finishTimeout = null, limitTimeout = null;
  let generation = 0, savedConfirmed = false, awaitingSaveConfirmation = false;
  let longDirectory = null, longWriter = null, longMode = false, partChunks = [], partSamples = 0;
  let bridgePath = approvedBridgePath(root.location), bridgeReady = false, bridgeMessage = '文字起こし保存はこのページでは利用できません。';
  let textSession = null, textQueue = [], textBlock = [], textBlockSamples = 0, textDrain = null;
  const rawSelected = () => $('saveAudio').checked;
  const relativeApi = (path) => {
    if (!bridgePath) throw new Error('文字起こし保存はこのページでは利用できません。');
    const url = new URL(`${bridgePath}${path}`, root.location.href);
    if (url.origin !== root.location.origin || !url.pathname.startsWith(root.location.pathname)) throw new Error('安全な接続先を確認できません。');
    return `${bridgePath}${path}`;
  };
  async function bridgeRequest(path, options = {}) {
    const response = await root.fetch(relativeApi(path), { credentials: 'same-origin', cache: 'no-store', redirect: 'error', ...options });
    if (!response.ok) throw new Error(`文字起こし保存に失敗しました（HTTP ${response.status}）。`);
    return response.json();
  }
  if (bridgePath && typeof root.fetch === 'function') {
    bridgeRequest('status').then((result) => {
      bridgeReady = result.ready === true;
      bridgeMessage = result.message || (bridgeReady ? '文字起こし保存を利用できます。' : '文字起こし保存は準備できていません。');
      $('bridgeStatus').textContent = bridgeMessage;
      updateConsentText(true);
    }).catch((error) => {
      bridgeReady = false; bridgeMessage = `文字起こし保存を利用できません: ${error.message}`;
      $('bridgeStatus').textContent = bridgeMessage;
      updateConsentText(true);
    });
  } else $('bridgeStatus').textContent = bridgeMessage;

  $('longMode').checked = false;
  $('shortMode').checked = true;
  $('rawModes').hidden = true;
  $('prepare').hidden = true;
  const captureChoices = () => {
    const online = $('online').checked;
    return { online, tabOnly: online && $('tabOnly').checked, microphone: !online || $('includeMic').checked };
  };
  const updateConsentText = (reset = false) => {
    if (reset) $('consent').checked = false;
    const { online, tabOnly, microphone } = captureChoices();
    $('onlineChoices').hidden = !online;
    $('captureScopeHelp').textContent = tabOnly
      ? '選んだブラウザータブを共有します。画面全体の音声は選びません。'
      : 'PCの再生音を録音します。アプリの指定は不要です。通知音や他のアプリの音も入り得ます。';
    const audio = !online ? 'マイク' : (tabOnly ? '共有タブの音声' : 'PCの再生音（通知音・他のアプリの音を含む場合あり）');
    const input = online && microphone ? `${audio}とマイク` : audio;
    const storage = rawSelected()
      ? `${bridgeReady ? '文字起こしを保存し、' : 'WAVのみを'}${$('longMode').checked ? '録音中に選択先へWAVパートを保存' : '停止後に自分でWAVを保存'}`
      : '原音は保存せず文字起こしを保存';
    $('consentText').textContent = `${input}を録音し、${storage}することを参加者と確認しました`;
  };
  updateConsentText();
  $('online').addEventListener('change', () => {
    if ($('online').checked) $('tabOnly').checked = false;
    updateConsentText(true);
  });
  for (const id of ['inPerson', 'includeMic', 'tabOnly']) {
    $(id).addEventListener('change', () => updateConsentText(true));
  }
  $('saveAudio').addEventListener('change', () => {
    $('rawModes').hidden = !rawSelected(); $('prepare').hidden = !rawSelected() || !$('longMode').checked;
    updateConsentText(true);
    if (rawSelected() && !bridgeReady) $('bridgeStatus').textContent = `${bridgeMessage} 原音保存を選択中のため、文字起こしなしのWAV限定で録音できます。`;
    else $('bridgeStatus').textContent = bridgeMessage;
  });
  $('shortMode').addEventListener('change', () => {
    updateConsentText(true);
    $('prepare').hidden = true;
    if ($('shortMode').checked) $('longReady').textContent = '短時間モードを選択中（最大15分・メモリー保持）';
  });
  $('longMode').addEventListener('change', () => {
    updateConsentText(true);
    $('prepare').hidden = !rawSelected() || !$('longMode').checked;
    if ($('longMode').checked && !longDirectory) $('longReady').textContent = '長時間モードは先に保存先の準備が必要です。';
  });

  prepareButton.addEventListener('click', async (event) => {
    if (!event.isTrusted || active || starting || preparing || persisting) return;
    if (!rawSelected()) return;
    if (!window.showDirectoryPicker) { setStatus('長時間保存はこのブラウザーで利用できません。'); return; }
    preparing = true; prepareButton.disabled = true; startButton.disabled = true; $('modeChoices').disabled = true;
    try {
      const rootDirectory = await window.showDirectoryPicker({ mode: 'readwrite' });
      let directory = null, name = '';
      for (let attempt = 0; attempt < 8 && !directory; attempt++) {
        const sessionId = root.crypto && root.crypto.randomUUID ? root.crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        name = `recording-${new Date().toISOString().replace(/[:.]/g, '-')}-${sessionId}`;
        try { await rootDirectory.getDirectoryHandle(name); continue; }
        catch (error) { if (error.name !== 'NotFoundError') throw error; }
        directory = await rootDirectory.getDirectoryHandle(name, { create: true });
      }
      if (!directory) throw new Error('新しい保存用フォルダーを確保できませんでした。');
      longDirectory = directory;
      longWriter = null;
      $('longReady').textContent = `保存先を準備しました: ${name}（録音開始前）`;
      setStatus('長時間保存先を準備しました。');
    } catch (error) { setStatus(`保存先を準備できません: ${error.message || '選択が中止されました'}`); }
    finally { preparing = false; prepareButton.disabled = false; startButton.disabled = false; $('modeChoices').disabled = false; }
  });

  function setStatus(message) { status.textContent = message; }
  function releaseInputs() {
    stopTracks();
    if (node) { try { node.disconnect(); } catch (_) {} node = null; }
    if (context && context.state !== 'closed') context.close().catch(() => {});
    context = null;
  }
  function stopTracks() {
    for (const stream of streams) for (const track of stream.getTracks()) {
      try { track.stop(); } catch (_) {}
    }
    streams = [];
  }
  function collect(data) {
    const pcm = new Int16Array(data.length);
    for (let i = 0; i < data.length; i++) { const value = Math.max(-1, Math.min(1, data[i])); pcm[i] = value < 0 ? value * 32768 : value * 32767; }
    if (textSession) {
      if (!active || textFailure) return;
      textBlock.push(pcm); textBlockSamples += pcm.length;
      if (textBlockSamples >= RATE * TEXT_BLOCK_SECONDS) queueTextBlock();
      if (!rawSelected()) return;
    }
    if (longMode) {
      if (!active || longWriter?.error) return;
      partChunks.push(pcm); partSamples += pcm.length;
      if (partSamples >= RATE * PART_SECONDS) queueLongPart();
      return;
    }
    if (!active || sampleCount >= MAX_SAMPLES) return;
    const remaining = MAX_SAMPLES - sampleCount;
    const piece = data.length > remaining ? data.slice(0, remaining) : data;
    const rawPiece = piece.length === data.length ? pcm : pcm.slice(0, piece.length);
    chunks.push(rawPiece); sampleCount += rawPiece.length;
    if (sampleCount >= MAX_SAMPLES) finish('上限15分に達したため録音を停止しました（部分音声を保存できます）', true);
  }
  let textFailure = null;
  function queueTextBlock() {
    if (!textBlockSamples || !textSession || textFailure) return;
    if (textQueue.length >= MAX_TEXT_WAITING) {
      textFailure = new Error('文字起こし保存が追いつかず、待機上限に達しました。');
      if (active) flushAndFinish(`${textFailure.message} 保存済み文字起こしを確認してください。`, true);
      return;
    }
    textQueue.push(textBlock); textBlock = []; textBlockSamples = 0;
    if (!textDrain) textDrain = drainTextQueue();
  }
  async function drainTextQueue() {
    try {
      while (textQueue.length) {
        const pieces = textQueue.shift();
        const size = pieces.reduce((sum, piece) => sum + piece.length, 0);
        const bytes = new Uint8Array(size * 2); const view = new DataView(bytes.buffer); let offset = 0;
        for (const piece of pieces) for (let i = 0; i < piece.length; i++, offset++) view.setInt16(offset * 2, piece[i], true);
        const result = await bridgeRequest('chunk', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Recording-Session': textSession }, body: bytes.buffer });
        const unit = document.createElement('p');
        unit.textContent = `${result.filename || '文字起こしを保存'}: ${result.text || '（この区間の文字起こしなし）'}`;
        detail.appendChild(unit);
      }
    } catch (error) {
      textFailure = error;
      textQueue = []; textBlock = []; textBlockSamples = 0;
      if (active) flushAndFinish(`文字起こし保存に失敗しました。${error.message}`, true);
    } finally { textDrain = null; }
  }
  function queueLongPart() {
    if (!partSamples || !longWriter || longWriter.error) return;
    const samples = partChunks; partChunks = []; partSamples = 0;
    const writer = longWriter;
    writer.enqueue(samples).then(() => {
      detail.textContent = `保存済み: ${writer.closed} パート（${partName(writer.closed)} まで）`;
    }).catch((error) => {
      detail.textContent = `保存失敗。保存済み ${writer.closed} パート。未保存の末尾は失われる可能性があります。: ${error.message}`;
      if (active) flushAndFinish('保存先エラーのため録音を停止しました。保存済みパートは残っています。', false);
    });
  }
  function finish(message, keepPartial) {
    if (!active) return;
    active = false; partial = !!keepPartial && sampleCount > 0;
    clearInterval(interval); interval = null;
    clearTimeout(finishTimeout); finishTimeout = null;
    clearTimeout(limitTimeout); limitTimeout = null;
    pendingFinish = null;
    releaseInputs();
    startButton.disabled = false; stopButton.disabled = true;
    saveButton.disabled = longMode || !rawSelected() || sampleCount === 0;
    $('choices').disabled = false;
    setStatus(sampleCount ? message : '停止しました。保存できる音声がありません。');
    if (textSession) {
      setStatus('停止しました。文字起こしと選択した保存先の確定を待っています。');
      persisting = true; startButton.disabled = true; prepareButton.disabled = true;
      if (textBlockSamples) queueTextBlock();
      (async () => {
        let textSaved = false, rawSaved = false, rawError = null, finalResult = null, stopAcknowledged = false;
        try {
          while (textDrain || textQueue.length) {
            if (textDrain) await textDrain;
            else textDrain = drainTextQueue();
            if (textFailure) throw textFailure;
          }
          if (textFailure) throw textFailure;
          finalResult = await bridgeRequest('stop', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Recording-Session': textSession }, body: JSON.stringify({ complete: !keepPartial }) });
          stopAcknowledged = true;
          textSaved = !keepPartial && finalResult.has_text !== false && (finalResult.status === 'complete' || finalResult.complete === true);
          if (!keepPartial && !textSaved && finalResult.has_text !== false) throw new Error(finalResult.message || 'ホスト側の保存完了を確認できません。');
        } catch (error) {
          if (!stopAcknowledged) try { await bridgeRequest('stop', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Recording-Session': textSession }, body: JSON.stringify({ complete: false }) }); } catch (_) {}
          detail.appendChild(Object.assign(document.createElement('p'), { textContent: `文字起こし未完了: ${error.message}。既に保存された文字起こしはホスト側に残っています。` }));
        }
        try {
        if (rawSelected()) {
          if (longMode) {
            const tail = partChunks; partChunks = []; partSamples = 0;
            try {
              if (longWriter) {
                if (tail.length && !longWriter.error) await longWriter.flush(tail);
                else await longWriter.chain.catch(() => {});
                if (longWriter.error) throw longWriter.error;
                if (longWriter.closed > 0 && !keepPartial) await longWriter.finalize();
                rawSaved = longWriter.closed > 0 && !keepPartial;
                detail.appendChild(Object.assign(document.createElement('p'), { textContent: `WAV: 保存済み ${longWriter.closed} パート${rawSaved ? '（完了マーカーあり）' : '（部分・完了マーカーなし）'}` }));
              }
            } catch (error) { rawError = error; }
          } else if (sampleCount > 0) {
            detail.appendChild(Object.assign(document.createElement('p'), { textContent: `WAV: ${partial ? '部分音声を' : '音声を'}手動保存できます。` }));
          }
        }
        if (rawError) {
          detail.appendChild(Object.assign(document.createElement('p'), { textContent: `WAV保存未完了: ${rawError.message}。保存済み ${longWriter ? longWriter.closed : 0} パートを確認してください。` }));
        }
        if (keepPartial && finalResult?.has_text === true) {
          setStatus('部分文字起こしは区間ごとに保存済みです。完了セッションではありません。');
          detail.appendChild(Object.assign(document.createElement('p'), { textContent: `部分文字起こし保存済み: ${finalResult?.filename || 'ホストの応答を確認してください'}` }));
        } else if (finalResult?.has_text === false) {
          setStatus(!rawSelected() ? '認識できた文字はありません。' : longMode
            ? rawSaved ? '原音WAVは保存済みです。認識できた文字はありません。' : '認識できた文字はありません。WAV保存も未完了です。'
            : '認識できた文字はありません。WAVは手動保存できます。');
        } else if (!textSaved) setStatus('文字起こしの保存完了を確認できません。成功扱いにはしていません。');
        else if (!rawSelected()) {
          setStatus('文字起こしの保存が完了しました。原音は保存していません。');
          detail.appendChild(Object.assign(document.createElement('p'), { textContent: `文字起こし保存済み: ${finalResult.filename || 'ホストの応答を確認してください'}` }));
        } else if (longMode) {
          setStatus(rawSaved ? '文字起こしとWAVの保存が完了しました。' : '文字起こしは保存済みです。WAVは未完了です。');
          detail.appendChild(Object.assign(document.createElement('p'), { textContent: `文字起こし保存済み: ${finalResult.filename || 'ホストの応答を確認してください'}` }));
        } else {
          setStatus('文字起こし保存済み。WAVは手動保存できます。');
          detail.appendChild(Object.assign(document.createElement('p'), { textContent: `文字起こし保存済み: ${finalResult.filename || 'ホストの応答を確認してください'}` }));
        }
        } finally {
          textSession = null; persisting = false; prepareButton.disabled = false; startButton.disabled = false; $('modeChoices').disabled = false;
        }
      })();
    } else if (longMode) {
      setStatus('停止しました。保存パートの確定を待っています。');
      persisting = true;
      startButton.disabled = true; prepareButton.disabled = true;
      const tail = partChunks; partChunks = []; partSamples = 0;
      const writer = longWriter;
      (async () => {
        try {
          if (writer) {
            if (tail.length && !writer.error) await writer.flush(tail);
            else await writer.chain.catch(() => {});
            if (!keepPartial && !writer.error && writer.closed > 0) await writer.finalize();
            detail.textContent = writer.error
              ? `保存失敗。保存済み ${writer.closed} パート。未保存の末尾があります。`
              : writer.closed === 0
                ? '保存パートはありません。完了マーカーは作成していません。'
                : `保存済み: ${writer.closed} パート${keepPartial ? '（部分録音・完了マーカーなし）' : '（完了マーカーあり）'}`;
            setStatus(writer.error ? '保存が完了していません。保存済みパートを確認してください。'
              : writer.closed === 0 ? '保存できる音声はありません。完了マーカーはありません。'
                : keepPartial ? '部分録音を停止しました。完了マーカーはありません。' : '録音・保存が完了しました。');
          }
        } catch (error) {
          detail.textContent = `保存失敗。保存済み ${writer ? writer.closed : 0} パート。完了マーカーはありません: ${error.message}`;
          setStatus('保存が完了していません。保存済みパートを確認してください。');
        } finally {
          persisting = false;
          prepareButton.disabled = false;
          startButton.disabled = false;
          $('modeChoices').disabled = false;
        }
      })();
    } else {
      $('modeChoices').disabled = false;
      if (partial) detail.textContent = '録音が中断されました。部分音声を保存できます。';
    }
  }
  function flushAndFinish(message, keepPartial) {
    if (!active) return;
    const requestedGeneration = generation;
    pendingFinish = { message, keepPartial, generation: requestedGeneration };
    // Stop capture immediately; only the already-buffered processor frames may flush.
    stopTracks();
    clearTimeout(finishTimeout);
    finishTimeout = setTimeout(() => {
      if (isCurrentGeneration(generation, requestedGeneration) && active) {
        finish('停止しました。末尾の処理が完了せず、取得済みの部分音声だけ保存できます。', true);
      }
    }, 1000);
    if (node && context && context.state === 'running') node.port.postMessage({ flush: true });
    else finish('停止しました。末尾の処理が完了せず、取得済みの部分音声だけ保存できます。', true);
  }
  function fail(message) {
    starting = false;
    clearTimeout(setupTimeout); setupTimeout = null;
    if (active) finish(message, sampleCount > 0);
    else {
      releaseInputs();
      startButton.disabled = false; stopButton.disabled = true;
      saveButton.disabled = !rawSelected() || sampleCount === 0; $('choices').disabled = false;
      $('modeChoices').disabled = false;
      setStatus(message);
    }
    if (!sampleCount) { chunks = []; detail.textContent = ''; }
  }

  function cancelStart(message) {
    if (!starting) return;
    generation++;
    fail(message);
  }

  async function start(event) {
    if (!event.isTrusted || starting || active || preparing || persisting) return;
    if (!$('consent').checked) { setStatus('確認欄にチェックしてください'); return; }
    if (!bridgeReady && !rawSelected()) { setStatus(`${bridgeMessage} 原音保存を明示選択すると、文字起こしなしのWAV限定で録音できます。`); return; }
    const textEnabled = bridgeReady;
    longMode = rawSelected() && $('longMode').checked;
    if (longMode && !longDirectory) { setStatus('先に長時間保存先を準備してください。'); return; }
    const { online, tabOnly, microphone } = captureChoices();
    if (!navigator.mediaDevices ||
      (microphone && !navigator.mediaDevices.getUserMedia) ||
      (online && !navigator.mediaDevices.getDisplayMedia)) {
      setStatus(online ? 'このブラウザーでは選んだ音声を録音できません。READMEのMac・Windows別の録音手順を確認してください。'
        : 'このブラウザーではマイクを使えません。'); return;
    }
    if (sampleCount > 0 && !savedConfirmed && !window.confirm('前の音声は未保存扱いです。開始に成功した場合は置き換えます。続けますか？')) return;
    startButton.disabled = true; stopButton.disabled = false; saveButton.disabled = true;
    $('choices').disabled = true; $('modeChoices').disabled = true;
    starting = true;
    const requestedGeneration = ++generation;
    textSession = null; textQueue = []; textBlock = []; textBlockSamples = 0; textDrain = null; textFailure = null;
    setStatus('音声の許可と準備を待っています。');
    const stillStarting = () => starting && isCurrentGeneration(requestedGeneration, generation);
    const ended = () => {
      if (!isCurrentGeneration(requestedGeneration, generation)) return;
      if (starting) cancelStart('準備中に音声ソースが終了しました。');
      else if (active) flushAndFinish('音声ソースが終了しました。取得済みの部分音声を保存できます', true);
    };
    const acceptStream = (stream) => {
      if (!stillStarting()) {
        for (const track of stream.getTracks()) { try { track.stop(); } catch (_) {} }
        return false;
      }
      streams.push(stream);
      for (const track of stream.getTracks()) track.addEventListener('ended', ended, { once: true });
      return true;
    };
    setupTimeout = setTimeout(() => {
      if (stillStarting()) cancelStart('音声の準備が時間切れになりました。共有や録音許可を選び直してください。');
    }, 60_000);
    try {
      // Requests are made only in direct response to this trusted user click.
      // getDisplayMedia must be invoked while the trusted click activation is fresh.
      if (online) {
        const monitor = !tabOnly;
        const stream = await navigator.mediaDevices.getDisplayMedia({
          // Hints narrow the picker where supported; returned settings remain authoritative.
          video: { displaySurface: monitor ? 'monitor' : 'browser' }, audio: true,
          systemAudio: monitor ? 'include' : 'exclude', windowAudio: 'exclude',
          monitorTypeSurfaces: monitor ? 'include' : 'exclude',
          surfaceSwitching: 'exclude', selfBrowserSurface: 'exclude',
        });
        if (!acceptStream(stream)) return;
        const display = stream.getVideoTracks()[0]?.getSettings().displaySurface;
        if (display !== (monitor ? 'monitor' : 'browser')) throw new Error(monitor
          ? '画面全体を選んでください。別の画面種別へ自動切替しません。'
          : '対象のブラウザタブを選んでください。画面全体やウィンドウの音声は録音しません。');
        if (!hasAudioTracks(stream)) throw new Error('共有対象に音声トラックがありません。音声共有を選び直してください。共有できない環境ではREADMEのMac・Windows別の録音手順を確認してください。');
      }
      if (microphone) {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 }, video: false });
        if (!acceptStream(stream)) return;
        if (!hasAudioTracks(stream)) throw new Error('マイク音声トラックがありません。別ソースへ自動切替しません。');
      }
      if (streams.some((stream) => !stream.getAudioTracks().some((track) => track.readyState === 'live'))) {
        throw new Error('選択した音声ソースが終了しています。録音を開始しません。');
      }
      const audioTracks = streams.flatMap((stream) => stream.getAudioTracks()).filter((track) => track.readyState === 'live');
      if (!audioTracks.length) throw new Error('音声トラックがありません。録音を開始しません。');
      context = new AudioContext({ sampleRate: 48000 });
      const preparationContext = context;
      if (context.sampleRate !== 48000) throw new Error(`音声処理に必要な48kHzを利用できません（実際: ${context.sampleRate}Hz）`);
      await context.resume();
      if (!stillStarting()) return;
      const moduleUrl = URL.createObjectURL(new Blob([workletSource()], { type: 'text/javascript' }));
      try { await preparationContext.audioWorklet.addModule(moduleUrl); } finally { URL.revokeObjectURL(moduleUrl); }
      if (!stillStarting()) return;
      if (audioTracks.some((track) => track.readyState !== 'live')) throw new Error('準備中に音声ソースが終了しました。');
      node = new AudioWorkletNode(context, 'local-pcm-recorder', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
      node.port.onmessage = (event) => {
        if (!isCurrentGeneration(requestedGeneration, generation)) return;
        if (event.data && event.data.flushed) {
          if (pendingFinish && pendingFinish.generation === requestedGeneration) {
            const pending = pendingFinish; pendingFinish = null; finish(pending.message, pending.keepPartial);
          }
        } else if (event.data instanceof Float32Array) collect(event.data);
      };
      const mix = context.createGain(); mix.gain.value = 1 / audioTracks.length;
      for (const track of audioTracks) {
        const sourceNode = context.createMediaStreamSource(new MediaStream([track]));
        sourceNode.connect(mix);
      }
      mix.connect(node);
      // Keep processing alive without audible monitoring or an output loop.
      const silentSink = context.createGain(); silentSink.gain.value = 0;
      node.connect(silentSink); silentSink.connect(context.destination);
      let bridgeSession = null;
      if (textEnabled) {
        const result = await bridgeRequest('start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        if (!stillStarting()) {
          try { await bridgeRequest('stop', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Recording-Session': result.id }, body: JSON.stringify({ complete: false }) }); } catch (_) {}
          return;
        }
        if (!result.id) throw new Error('文字起こし保存セッションを確認できません。');
        bridgeSession = result.id;
      }
      // All requested streams and audio processing are now verified/configured.
      // Commit the newly configured session only now; failed/cancelled starts preserve prior audio.
      chunks = []; sampleCount = 0; partial = false; savedConfirmed = false;
      textSession = bridgeSession;
      awaitingSaveConfirmation = false; saveButton.textContent = 'WAVを保存'; detail.textContent = '';
      clearTimeout(setupTimeout); setupTimeout = null;
      starting = false; active = true; startedAt = Date.now();
      $('choices').disabled = true;
      stopButton.disabled = false; setStatus(textEnabled ? (rawSelected() ? '録音・文字起こし中（文字を区間ごとに保存）' : '文字起こし中（原音は保存しません）') : 'WAVのみ録音中（文字起こしは行いません）');
      interval = setInterval(() => {
        const seconds = Math.floor((Date.now() - startedAt) / 1000);
        timer.textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
      }, 250);
      longWriter = longMode ? new LongPartWriter(longDirectory) : null; partChunks = []; partSamples = 0;
      if (longMode) { longDirectory = null; $('longReady').textContent = '保存先はこの録音セッションで使用中です。次回は改めて準備してください。'; }
    if (rawSelected() && !longMode) limitTimeout = setTimeout(() => flushAndFinish('上限15分に達したため録音を停止しました（部分音声を保存できます）', true), MAX_SECONDS * 1000);
    } catch (error) {
      if (stillStarting()) fail(`録音を開始できません: ${error && error.message ? error.message : '取得に失敗しました'}`);
    }
  }

  startButton.addEventListener('click', start);
  stopButton.addEventListener('click', () => {
    if (starting) { cancelStart('録音の準備を取り消しました。'); return; }
    stopButton.disabled = true;
    flushAndFinish('停止しました。WAVを保存できます', false);
  });
  saveButton.addEventListener('click', () => {
    if (!sampleCount || sampleCount > MAX_SAMPLES) return;
    if (awaitingSaveConfirmation) {
      if (window.confirm('ダウンロード先を開いて、WAVファイルが作成されたことを確認しましたか？')) {
        savedConfirmed = true; awaitingSaveConfirmation = false; saveButton.textContent = 'WAVを保存';
        detail.textContent = '保存先のWAV作成を確認しました。';
      } else {
        detail.textContent = '保存確認は保留中です。音声はページ内に残っています。';
      }
      return;
    }
    if (isSilent(chunks) && !window.confirm('録音はほぼ無音です。無音WAVを保存しますか？')) return;
    const blob = encodeWav(chunks);
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = `recording-${new Date().toISOString().replace(/[:.]/g, '-')}.wav`;
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    savedConfirmed = false; awaitingSaveConfirmation = true;
    saveButton.textContent = '保存を確認';
    detail.textContent = `${partial ? '部分' : ''}WAVのダウンロードを開始しました。保存先を確認後、「保存を確認」を押してください。`;
  });
  window.addEventListener('beforeunload', (event) => {
    if (shouldWarnBeforeUnload(active || starting || persisting, sampleCount, savedConfirmed)) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('pagehide', () => {
    if (starting) cancelStart('ページを閉じたため、録音の準備を止めました。');
    if (active) finish('ページを離れたため録音を止めました。', true);
  });
})(typeof globalThis !== 'undefined' ? globalThis : this);
