(function (root) {
  'use strict';

  const RATE = 16000;
  const MAX_SECONDS = 15 * 60;
  const MAX_SAMPLES = RATE * MAX_SECONDS;
  const SILENCE_RMS = 0.0001;
  const hasAudioTracks = (stream) => !!stream && stream.getAudioTracks().length > 0;
  const isCurrentGeneration = (candidate, current) => candidate === current;
  const shouldWarnBeforeUnload = (recording, samples, saved) => recording || (samples > 0 && !saved);

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
    module.exports = { RATE, MAX_SECONDS, MAX_SAMPLES, encodeWav, isSilent, hasAudioTracks, isCurrentGeneration, shouldWarnBeforeUnload, workletSource };
    return;
  }

  const $ = (id) => document.getElementById(id);
  const startButton = $('start'), stopButton = $('stop'), saveButton = $('save');
  const status = $('status'), detail = $('detail'), timer = $('timer');
  let context = null, node = null, streams = [], chunks = [], sampleCount = 0, startedAt = 0, interval = null;
  let active = false, starting = false, partial = false, pendingFinish = null;
  let setupTimeout = null, finishTimeout = null, limitTimeout = null;
  let generation = 0, savedConfirmed = false, awaitingSaveConfirmation = false;

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
    if (!active || sampleCount >= MAX_SAMPLES) return;
    const remaining = MAX_SAMPLES - sampleCount;
    const piece = data.length > remaining ? data.slice(0, remaining) : data;
    const pcm = new Int16Array(piece.length);
    for (let i = 0; i < piece.length; i++) {
      const value = Math.max(-1, Math.min(1, piece[i]));
      pcm[i] = value < 0 ? value * 32768 : value * 32767;
    }
    chunks.push(pcm); sampleCount += pcm.length;
    if (sampleCount >= MAX_SAMPLES) finish('上限15分に達したため録音を停止しました（部分音声を保存できます）', true);
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
    saveButton.disabled = sampleCount === 0;
    $('choices').disabled = false;
    setStatus(sampleCount ? message : '停止しました。保存できる音声がありません。');
    if (partial) detail.textContent = '録音が中断されました。部分音声を保存できます。';
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
      saveButton.disabled = sampleCount === 0; $('choices').disabled = false;
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
    if (!event.isTrusted || starting || active) return;
    if (!$('consent').checked) { setStatus('確認欄にチェックしてください'); return; }
    const source = document.querySelector('input[name="source"]:checked').value;
    if (!navigator.mediaDevices ||
      ((source === 'mic' || source === 'both') && !navigator.mediaDevices.getUserMedia) ||
      ((source === 'tab' || source === 'both') && !navigator.mediaDevices.getDisplayMedia)) {
      setStatus('選択した音声ソースはこのブラウザーで利用できません'); return;
    }
    if (sampleCount > 0 && !savedConfirmed && !window.confirm('前の音声は未保存扱いです。開始に成功した場合は置き換えます。続けますか？')) return;
    startButton.disabled = true; stopButton.disabled = false; saveButton.disabled = true;
    $('choices').disabled = true;
    starting = true;
    const requestedGeneration = ++generation;
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
      if (source === 'tab' || source === 'both') {
        const stream = await navigator.mediaDevices.getDisplayMedia({
          video: { displaySurface: 'browser' }, audio: true, systemAudio: 'exclude', windowAudio: 'exclude',
        });
        if (!acceptStream(stream)) return;
        const display = stream.getVideoTracks()[0]?.getSettings().displaySurface;
        if (display !== 'browser') throw new Error('対象のブラウザタブを選んでください。画面全体やウィンドウの音声は録音しません。');
        if (!hasAudioTracks(stream)) throw new Error('共有対象に音声トラックがありません。音声共有を選び直してください。');
      }
      if (source === 'mic' || source === 'both') {
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
      // All requested streams and audio processing are now verified/configured.
      // Commit the newly configured session only now; failed/cancelled starts preserve prior audio.
      chunks = []; sampleCount = 0; partial = false; savedConfirmed = false;
      awaitingSaveConfirmation = false; saveButton.textContent = 'WAVを保存'; detail.textContent = '';
      clearTimeout(setupTimeout); setupTimeout = null;
      starting = false; active = true; startedAt = Date.now();
      $('choices').disabled = true;
      stopButton.disabled = false; setStatus('録音中');
      interval = setInterval(() => {
        const seconds = Math.floor((Date.now() - startedAt) / 1000);
        timer.textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
      }, 250);
      limitTimeout = setTimeout(() => flushAndFinish('上限15分に達したため録音を停止しました（部分音声を保存できます）', true), MAX_SECONDS * 1000);
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
    if (shouldWarnBeforeUnload(active || starting, sampleCount, savedConfirmed)) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('pagehide', () => {
    if (starting) cancelStart('ページを閉じたため、録音の準備を止めました。');
    if (active) finish('ページを離れたため録音を止めました。', true);
  });
})(typeof globalThis !== 'undefined' ? globalThis : this);
