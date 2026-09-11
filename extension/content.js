// Karaoke Live for YouTube - content script
// Routes the YouTube <video> audio through Web Audio (so it can be silenced), plays AI-separated stems
// from the local server in sync with the video's own clock, and shows synced lyrics.
(() => {
  const SERVER = 'http://127.0.0.1:8765';
  const settings = { enabled: true, vocal: 0, music: 100, min: false, lyrics: true, lang: '', waitReady: true };
  try { Object.assign(settings, JSON.parse(localStorage.getItem('kl-settings') || '{}')); } catch {}
  const saveSettings = () => { try { localStorage.setItem('kl-settings', JSON.stringify(settings)); } catch {} };

  let video = null, ctx = null, passGain, accGain, vocGain;
  let vid = null, status = null, lastPoll = 0, serverFail = 0, serverErr = '';
  let nextChunk = 0, scheduledUntil = 0, anchorCtx = 0, anchorTrack = 0, rate = 1;
  let sources = [], fetching = false, gen = 0, autoPaused = false, passthrough = true, waitOverride = false;
  let chunkErr = '', lastChunkErrAt = 0, fallbackUntil = 0, stalledSince = 0;
  let lyricsLines = null, lyricsState = '', lyricsProgress = 0, lastLyricsPoll = 0, lyricsIdx = -2, lyricsFinalFlag = false;
  const bufCache = new Map();

  // ---------- panel ----------
  const panel = document.createElement('div');
  panel.id = 'kl-panel';
  panel.innerHTML = `
    <div class="kl-head"><span class="kl-dot"></span><b>🎤 Karaoke Live</b><span class="kl-chev">▾</span></div>
    <div class="kl-body">
      <div class="kl-row kl-main"><span class="kl-lbl">ตัดเสียงร้อง</span><span class="kl-mode" id="kl-mode"></span><label class="kl-sw"><input type="checkbox" id="kl-on"><span></span></label></div>
      <div class="kl-prog"><div class="kl-bar"><div id="kl-fill"></div></div><div id="kl-status"></div></div>
      <div class="kl-row"><span class="kl-lbl">เสียงร้อง</span><input type="range" id="kl-vocal" min="0" max="100"><span class="kl-val" id="kl-vocal-v"></span></div>
      <div class="kl-row"><span class="kl-lbl">ดนตรี</span><input type="range" id="kl-music" min="0" max="100"><span class="kl-val" id="kl-music-v"></span></div>
      <div class="kl-row"><span class="kl-lbl">คำร้อง</span><label class="kl-sw sm"><input type="checkbox" id="kl-lyr"><span></span></label>
        <select id="kl-lang" title="ภาษาของเพลง"><option value="">ภาษา: อัตโนมัติ</option><option value="th">ภาษา: ไทย</option><option value="en">ภาษา: อังกฤษ</option><option value="ja">ภาษา: ญี่ปุ่น</option><option value="ko">ภาษา: เกาหลี</option><option value="zh">ภาษา: จีน</option></select></div>
      <div class="kl-row"><span class="kl-lbl" title="หยุดวิดีโอไว้จนเสียงและคำร้องพร้อม (เพลงถัดไปใน playlist จะพร้อมล่วงหน้า)">รอให้พร้อมก่อนเล่น</span><label class="kl-sw sm"><input type="checkbox" id="kl-wait"><span></span></label></div>
      <div class="kl-more"><button id="kl-redo" title="ไม่สนใจ subtitle ของ YouTube ถอดจากเสียงร้องด้วย AI ใหม่" hidden>ถอดคำร้องใหม่ (AI)</button><button id="kl-setup" class="hot" hidden>ตั้งค่าการเชื่อมต่อ</button></div>
      <div id="kl-diag"></div>
    </div>`;
  document.documentElement.appendChild(panel);
  const $ = s => panel.querySelector(s);
  const dot = $('.kl-dot');
  panel.classList.toggle('kl-min', settings.min);
  $('.kl-head').onclick = () => { settings.min = !settings.min; panel.classList.toggle('kl-min', settings.min); saveSettings(); };
  $('#kl-on').checked = settings.enabled;
  $('#kl-lyr').checked = settings.lyrics;
  $('#kl-wait').checked = settings.waitReady;
  $('#kl-lang').value = settings.lang;
  $('#kl-vocal').value = settings.vocal; $('#kl-music').value = settings.music;
  $('#kl-vocal-v').textContent = settings.vocal + '%'; $('#kl-music-v').textContent = settings.music + '%';
  $('#kl-on').onchange = () => { settings.enabled = $('#kl-on').checked; saveSettings(); if (settings.enabled && vid) onVideoChange(vid); else applyMode(); };
  $('#kl-lyr').onchange = () => { settings.lyrics = $('#kl-lyr').checked; saveSettings(); };
  $('#kl-wait').onchange = () => { settings.waitReady = $('#kl-wait').checked; saveSettings(); if (!settings.waitReady) releaseHold(); };
  for (const k of ['vocal', 'music']) {
    const el = $('#kl-' + k), v = $('#kl-' + k + '-v');
    el.oninput = () => { settings[k] = +el.value; v.textContent = el.value + '%'; saveSettings(); applyGains(); };
  }
  $('#kl-lang').onchange = async () => {
    settings.lang = $('#kl-lang').value; saveSettings();
    if (!vid) return;
    try { await api('/api/prepare', { ids: [vid, ...upcomingIds(vid)], lang: settings.lang }); } catch {}
    if (status?.state === 'done') redoLyrics();
  };
  async function redoLyrics() {
    if (!vid) return;
    try { await api('/api/lyrics/' + vid + '/redo', { source: 'whisper', lang: settings.lang }); lyricsLines = null; lyricsState = 'pending'; lyricsIdx = -2; lyricsFinalFlag = false; }
    catch (e) { console.warn('[karaoke] redo failed', e); }
  }
  $('#kl-redo').onclick = redoLyrics;
  $('#kl-setup').onclick = () => { try { chrome.runtime.sendMessage({ open: 'setup' }); } catch {} };
  const setStatus = (text, cls, fill, fillOk) => {
    $('#kl-status').textContent = text; $('#kl-status').className = cls === 'err' ? 'err' : '';
    dot.className = 'kl-dot ' + (cls || '');
    const f = $('#kl-fill'); f.style.width = Math.round((fill ?? 0) * 100) + '%'; f.className = fillOk ? 'ok' : '';
  };

  // ---------- helpers ----------
  const getVideoId = () => new URLSearchParams(location.search).get('v');
  const isAd = () => !!document.querySelector('.html5-video-player.ad-showing, .html5-video-player.ad-interrupting');
  const chunkSec = () => status?.chunk_sec || 10;
  const userActive = () => navigator.userActivation ? navigator.userActivation.hasBeenActive : true;
  const lyricsFinal = () => (lyricsState === 'done' && lyricsFinalFlag) || lyricsState === 'none' || lyricsState === 'error';
  const serverDown = () => serverFail >= 2;

  function upcomingIds(cur) {
    const ids = [];
    document.querySelectorAll('ytd-playlist-panel-video-renderer a#wc-endpoint, ytd-compact-video-renderer a#thumbnail, ytd-compact-playlist-renderer a#thumbnail, ytmusic-player-queue-item a')
      .forEach(a => { const m = (a.href || '').match(/[?&]v=([\w-]{11})/); if (m && m[1] !== cur && !ids.includes(m[1])) ids.push(m[1]); });
    const panelIds = [...document.querySelectorAll('ytd-playlist-panel-video-renderer a#wc-endpoint')].map(a => ((a.href || '').match(/[?&]v=([\w-]{11})/) || [])[1]).filter(Boolean);
    const i = panelIds.indexOf(cur);
    const after = i >= 0 ? panelIds.slice(i + 1) : [];
    return after.length ? after.slice(0, 3) : ids.slice(0, 1);
  }

  // All server traffic goes through the background service worker (see bg.js).
  function bgFetch(url, init, binary) {
    return new Promise((res, rej) => {
      try {
        chrome.runtime.sendMessage({ url, init, binary }, resp => {
          if (chrome.runtime.lastError) return rej(new Error(chrome.runtime.lastError.message));
          if (!resp || resp.error) return rej(new Error(resp?.error || 'no response'));
          res(resp);
        });
      } catch (e) { rej(e); }
    });
  }
  async function api(path, body) {
    const init = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined;
    return (await bgFetch(SERVER + path, init, false)).json;
  }
  async function fetchChunkBuffer(v, n, stem) {
    const { b64 } = await bgFetch(`${SERVER}/api/chunk/${v}/${n}/${stem}`, undefined, true);
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }

  // ---------- audio graph ----------
  function ensureAudio() {
    if (ctx || !video || !userActive()) return false;
    try {
      ctx = new AudioContext();
      const src = ctx.createMediaElementSource(video);
      passGain = ctx.createGain(); src.connect(passGain); passGain.connect(ctx.destination);
      accGain = ctx.createGain(); accGain.connect(ctx.destination);
      vocGain = ctx.createGain(); vocGain.connect(ctx.destination);
      applyGains();
      if (ctx.state === 'suspended') ctx.resume().catch(() => {});
      return true;
    } catch (e) { console.warn('[karaoke] audio setup failed', e); ctx = null; return false; }
  }
  function applyGains() {
    if (!ctx) return;
    const vol = video && !video.muted ? video.volume : 0;
    passGain.gain.value = passthrough ? 1 : 0;
    accGain.gain.value = passthrough ? 0 : settings.music / 100 * vol;
    vocGain.gain.value = passthrough ? 0 : settings.vocal / 100 * vol;
  }
  function stopSources() {
    gen++;
    sources.forEach(s => { try { s.stop(); } catch {} });
    sources = []; fetching = false; scheduledUntil = 0; stalledSince = 0;
  }
  function setPassthrough(on) {
    if (on === passthrough) return;
    passthrough = on;
    if (on) stopSources();
    applyGains();
    if (!on) resync();
  }
  function applyMode() {
    const want = !settings.enabled || !video || !ctx || isAd() || !status || status.state === 'error' || serverDown() || Date.now() < fallbackUntil;
    setPassthrough(want);
    if (autoPaused && want) releaseHold();
  }
  function releaseHold() { if (autoPaused) { autoPaused = false; video?.play().catch(() => {}); } }

  // ---------- "wait until ready" hold ----------
  function needHold() {
    if (!settings.waitReady || waitOverride || !status || passthrough) return false;
    if (status.state !== 'done') return true;                       // separation still running
    if (settings.lyrics && !lyricsFinal()) return true;             // lyrics still transcribing
    return false;
  }

  // ---------- scheduling (video.currentTime is the master clock) ----------
  function resync() {
    if (!video || !ctx || passthrough) return;
    stopSources();
    const t = video.currentTime;
    nextChunk = Math.floor(t / chunkSec());
    anchorCtx = ctx.currentTime; anchorTrack = t; rate = video.playbackRate || 1;
    pump();
  }
  async function getChunk(v, n) {
    const key = v + '/' + n;
    if (bufCache.has(key)) return bufCache.get(key);
    const [a, b] = await Promise.all(['acc', 'voc'].map(async st => ctx.decodeAudioData(await fetchChunkBuffer(v, n, st))));
    if (bufCache.size > 80) bufCache.delete(bufCache.keys().next().value);
    bufCache.set(key, [a, b]);
    return [a, b];
  }
  async function pump() {
    if (!video || !ctx || passthrough || fetching || (video.paused && !autoPaused)) return;
    if (!status || !status.total_chunks) return;
    if (nextChunk >= status.total_chunks) return;
    if (Date.now() - lastChunkErrAt < 1500) return;
    const cs = chunkSec();
    if (scheduledUntil - ctx.currentTime > cs * 1.2 / rate) return;
    if (nextChunk >= (status.chunks_ready || 0)) {
      if (scheduledUntil <= ctx.currentTime + 0.05 && !video.paused) { autoPaused = true; video.pause(); }
      return;
    }
    fetching = true; const g = gen, n = nextChunk, v = vid;
    try {
      const [a, b] = await getChunk(v, n);
      if (g !== gen) { fetching = false; return; }
      chunkErr = '';
      if (autoPaused) { fetching = false; if (needHold()) return; autoPaused = false; await video.play().catch(() => {}); return; }
      let when = scheduledUntil, offset = 0;
      if (when < ctx.currentTime + 0.05) {
        const t = video.currentTime + 0.06;
        const k = Math.floor(t / cs);
        if (k !== n) { nextChunk = k; fetching = false; return pump(); }
        offset = t - k * cs; when = ctx.currentTime + 0.06;
        anchorCtx = when; anchorTrack = t;
      }
      for (const [buf, gnode] of [[a, accGain], [b, vocGain]]) {
        const s = ctx.createBufferSource(); s.buffer = buf; s.playbackRate.value = rate; s.connect(gnode);
        s.start(when, offset); sources.push(s);
        s.onended = () => { sources = sources.filter(x => x !== s); };
      }
      scheduledUntil = when + (a.duration - offset) / rate;
      nextChunk = n + 1;
    } catch (e) {
      chunkErr = e.message; lastChunkErrAt = Date.now();
      console.warn('[karaoke] chunk failed', e);
    }
    fetching = false;
    pump();
  }

  // ---------- video events ----------
  function attach(v) {
    if (v === video) return;
    video = v; ctx = null; passthrough = true;
    v.addEventListener('play', () => {
      if (ctx?.state === 'suspended') ctx.resume().catch(() => {});
      if (autoPaused && needHold()) { waitOverride = true; }   // the user pressed play while we were holding: let them
      autoPaused = false;
      if (!passthrough) resync();
    });
    v.addEventListener('pause', () => { if (!autoPaused) stopSources(); });
    v.addEventListener('seeking', () => { if (!passthrough) resync(); });
    v.addEventListener('ratechange', () => { if (!passthrough) resync(); });
    v.addEventListener('volumechange', applyGains);
  }
  const onGesture = () => { if (ctx?.state === 'suspended') ctx.resume().catch(() => {}); };
  document.addEventListener('pointerdown', onGesture, { capture: true, passive: true });
  document.addEventListener('keydown', onGesture, { capture: true, passive: true });
  document.addEventListener('fullscreenchange', () => (document.fullscreenElement || document.documentElement).appendChild(panel));

  // ---------- lyrics overlay ----------
  const lyr = document.createElement('div');
  lyr.id = 'kl-lyrics'; lyr.hidden = true;
  lyr.innerHTML = '<div class="kl-cur"></div><div class="kl-next"></div>';
  const lyrCur = lyr.querySelector('.kl-cur'), lyrNext = lyr.querySelector('.kl-next');
  const esc = x => x.replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
  const isThai = s => /[฀-๿]/.test(s);
  const sep = (a, b) => (isThai(a.slice(-1)) && isThai(b[0])) ? '' : ' ';
  const wordText = ws => ws.reduce((acc, w) => acc + (acc ? sep(acc, w.w) : '') + w.w, '');
  function mountLyrics() {
    const player = document.querySelector('#movie_player, .html5-video-player');
    if (player && lyr.parentElement !== player) player.appendChild(lyr);
    if (player) { const fs = Math.max(14, Math.round(player.clientWidth * 0.028)); if (lyr.style.fontSize !== fs + 'px') lyr.style.fontSize = fs + 'px'; }
    return !!player;
  }
  async function pollLyrics() {
    if (!vid || !settings.lyrics || !status || serverDown() || lyricsFinal()) return;
    try {
      const d = await api('/api/lyrics/' + vid);
      if (d.state === 'done') {
        const lines = d.lines || [];
        if (!lyricsLines || lines.length !== lyricsLines.length || d.final !== lyricsFinalFlag) { lyricsLines = lines; lyricsIdx = -2; }
        lyricsState = lines.length || d.final ? (lines.length ? 'done' : 'none') : 'pending'; lyricsFinalFlag = !!d.final; lyricsProgress = d.progress || 0;
      } else { lyricsState = d.state; lyricsProgress = d.progress || 0; }
    } catch {}
  }
  function showInfo(text) { lyr.hidden = false; lyrNext.textContent = ''; lyricsIdx = -3; lyrCur.className = 'kl-cur kl-info'; lyrCur.textContent = text; }
  function renderLyrics() {
    requestAnimationFrame(renderLyrics);
    if (!video || !vid || !settings.enabled || isAd() || !mountLyrics()) { lyr.hidden = true; return; }
    if (autoPaused && needHold()) { showInfo(prepText() + '  ·  กดเล่นเพื่อข้ามการรอ'); return; }
    if (!settings.lyrics) { lyr.hidden = true; return; }
    if (!lyricsLines) {
      if (status?.state === 'done' && (lyricsState === 'working' || lyricsState === 'pending')) showInfo(lyricsState === 'working' ? `กำลังถอดคำร้อง ${Math.round(lyricsProgress * 100)}%` : 'รอถอดคำร้อง...');
      else if (status && status.state !== 'done' && !lyricsFinal()) showInfo('คำร้องจะตามมาหลังแยกเสียงเสร็จ');
      else lyr.hidden = true;
      return;
    }
    if (!lyricsLines.length) { lyr.hidden = true; return; }
    const t = video.currentTime + 0.1;
    let idx = -1;
    for (let i = 0; i < lyricsLines.length && lyricsLines[i].s <= t; i++) idx = i;
    const cur = lyricsLines[idx], next = lyricsLines[idx + 1];
    const gap = cur && t > cur.e + 3 && (!next || next.s - t > 3);
    lyr.hidden = false;
    if (idx !== lyricsIdx || lyrCur.classList.contains('kl-info')) {
      lyricsIdx = idx;
      lyrCur.className = 'kl-cur';
      if (!cur || gap) lyrCur.textContent = next ? '♪ ♪ ♪' : '';
      else if (cur.words && cur.words.length > 1) lyrCur.innerHTML = cur.words.map((w, i) => `<span>${esc(w.w)}${i < cur.words.length - 1 ? sep(w.w, cur.words[i + 1].w) : ''}</span>`).join('');
      else lyrCur.innerHTML = `<span>${esc(cur.text)}</span>`;
      lyrNext.textContent = next ? (next.words && next.words.length > 1 ? wordText(next.words) : next.text) : '';
    } else if (cur && gap && next && lyrCur.textContent !== '♪ ♪ ♪') lyrCur.textContent = '♪ ♪ ♪';
    if (cur && !gap) {
      const spans = lyrCur.children;
      if (cur.words && cur.words.length > 1) for (let i = 0; i < spans.length; i++) spans[i].classList.toggle('on', cur.words[i].s <= t);
      else if (spans[0]) spans[0].classList.toggle('on', t >= cur.s);
    }
  }
  requestAnimationFrame(renderLyrics);
  function prepText() {
    if (!status) return 'กำลังเตรียมเพลง...';
    if (status.state === 'downloading' || status.state === 'queued' || status.state === 'idle') return 'กำลังเตรียมเพลง: โหลดเสียง...';
    if (status.state === 'processing') return `กำลังเตรียมเพลง: แยกเสียงร้อง ${Math.round(100 * (status.chunks_ready || 0) / (status.total_chunks || 1))}%`;
    if (status.state === 'done' && settings.lyrics && !lyricsFinal()) return lyricsProgress > 0 ? `กำลังเตรียมเพลง: จับจังหวะคำร้อง ${Math.round(lyricsProgress * 100)}%` : 'กำลังเตรียมเพลง: ถอดคำร้อง...';
    return 'กำลังเตรียมเพลง...';
  }

  // ---------- main loop ----------
  async function poll() {
    if (!vid) return;
    try { status = await api('/api/status/' + vid); serverFail = 0; serverErr = ''; }
    catch (e) { serverFail++; serverErr = e.message; if (serverFail === 2) { console.warn('[karaoke] server unreachable:', e.message); status = null; } }
  }
  async function onVideoChange(newId) {
    vid = newId; status = null; autoPaused = false; waitOverride = false; nextChunk = 0; chunkErr = ''; fallbackUntil = 0;
    lyricsLines = null; lyricsState = ''; lyricsProgress = 0; lyricsIdx = -2; lyricsFinalFlag = false; lyr.hidden = true;
    stopSources(); setPassthrough(true);
    if (!vid || !settings.enabled) return;
    try { await api('/api/prepare', { ids: [vid, ...upcomingIds(vid)], lang: settings.lang }); serverFail = 0; } catch (e) { serverFail = 2; serverErr = e.message; }
    await poll();
    if (vid === newId) applyMode();
  }
  let lastPrepare = 0, lastPlayhead = 0;
  setInterval(async () => {
    const v = document.querySelector('video.html5-main-video') || document.querySelector('video');
    if (v) attach(v);
    const id = getVideoId();
    if (id !== vid) await onVideoChange(id);
    if (!vid || !video) { setStatus('เปิดวิดีโอเพื่อเริ่ม', '', 0); return; }
    if (!ctx && settings.enabled) ensureAudio();
    const now = Date.now();
    if (now - lastPoll > 700 && settings.enabled) { lastPoll = now; await poll(); }
    if (now - lastLyricsPoll > 2000) { lastLyricsPoll = now; pollLyrics(); }
    if (now - lastPrepare > 15000 && settings.enabled && status?.state === 'done') { lastPrepare = now; api('/api/prepare', { ids: [vid, ...upcomingIds(vid)], lang: settings.lang }).catch(() => {}); }
    if (now - lastPlayhead > 2000 && settings.enabled && ctx) { lastPlayhead = now; api('/api/playhead', { id: vid, t: video.currentTime, playing: !video.paused }).catch(() => {}); }
    if (ctx && ctx.state === 'suspended' && !video.paused) ctx.resume().catch(() => {});
    applyMode();

    // status line + progress
    const sepP = status ? (status.chunks_ready || 0) / (status.total_chunks || 1) : 0;
    $('#kl-mode').textContent = !settings.enabled ? 'ปิด' : passthrough ? 'เสียงต้นฉบับ' : 'ทำงานอยู่';
    if (!settings.enabled) setStatus('ปิดอยู่: ได้ยินเสียงต้นฉบับ', '', 0);
    else if (!ctx) setStatus(userActive() ? 'กำลังเริ่มระบบเสียง...' : 'คลิกที่หน้าเว็บหนึ่งครั้งเพื่อเริ่ม', 'work', 0);
    else if (ctx.state === 'suspended') setStatus('เสียงถูกพักโดยเบราว์เซอร์: คลิกที่หน้าเว็บ', 'err', 0);
    else if (serverDown()) setStatus('ต่อ server ไม่ได้: เปิด run.bat แล้วกดปุ่มตั้งค่าการเชื่อมต่อ', 'err', 0);
    else if (now < fallbackUntil) setStatus('โหลดเสียงที่แยกไม่ทัน เล่นเสียงต้นฉบับชั่วคราว: ' + chunkErr.slice(0, 50), 'err', sepP);
    else if (isAd()) setStatus('โฆษณา: เสียงต้นฉบับ', '', sepP);
    else if (!status) setStatus('กำลังติดต่อ server...', 'work', 0);
    else if (status.state === 'error') setStatus('ผิดพลาด: ' + (status.error || '').slice(0, 70), 'err', 0);
    else if (status.state === 'processing') setStatus(`แยกเสียงร้อง ${Math.round(sepP * 100)}%` + (autoPaused ? ' · หยุดรอ' : ''), 'work', sepP);
    else if (status.state === 'downloading') setStatus('กำลังโหลดเสียงเพลง...', 'work', 0.02);
    else if (status.state === 'done' && settings.lyrics && !lyricsFinal()) setStatus((lyricsState === 'working' ? `ถอดคำร้อง ${Math.round(lyricsProgress * 100)}%` : 'รอถอดคำร้อง...') + (autoPaused ? ' · หยุดรอ' : ''), 'work', 1, true);
    else if (status.state === 'done') setStatus('พร้อม: ตัดเสียงร้องแล้ว' + (settings.lyrics ? (lyricsState === 'done' ? ' + คำร้อง' : ' (ไม่พบคำร้อง)') : ''), 'on', 1, true);
    else setStatus('รอคิว...', 'work', 0);
    $('#kl-setup').hidden = !(settings.enabled && serverDown());
    $('#kl-redo').hidden = !(settings.enabled && settings.lyrics && status?.state === 'done' && lyricsFinal());
    $('#kl-diag').textContent = [ctx ? 'audio:' + ctx.state : 'audio:-', passthrough ? 'ต้นฉบับ' : (scheduledUntil > ctx.currentTime ? 'ตัดร้อง▶' : 'ตัดร้อง…'),
      status ? `chunk ${nextChunk}/${status.total_chunks || '?'} (พร้อม ${status.chunks_ready || 0})` : '', 'lyr:' + (lyricsState || '-'),
      serverErr ? 'srv:' + serverErr.slice(0, 30) : '', chunkErr ? 'chunk-err:' + chunkErr.slice(0, 30) : ''].filter(Boolean).join(' · ');
    panel.dataset.debug = JSON.stringify({ vid, passthrough, autoPaused, waitOverride, nextChunk, sources: sources.length, scheduledUntil: +scheduledUntil.toFixed(2), ctxTime: +(ctx?.currentTime || 0).toFixed(2), ctxState: ctx?.state, videoTime: +video.currentTime.toFixed(2), paused: video.paused, chunksReady: status?.chunks_ready, state: status?.state, lyricsState, serverErr, chunkErr });

    if (passthrough || !ctx) return;
    if (!video.paused && needHold()) { autoPaused = true; video.pause(); }
    if (video.paused) { if (autoPaused) pump(); return; }
    // safety: nothing scheduled for a while although we should be playing separated audio -> original sound for 10 s, then retry
    if (scheduledUntil <= ctx.currentTime && !autoPaused && !fetching) {
      if (!stalledSince) stalledSince = now;
      else if (now - stalledSince > 6000) { fallbackUntil = now + 10000; stalledSince = 0; applyMode(); return; }
    } else stalledSince = 0;
    if (scheduledUntil > ctx.currentTime) {
      const expected = anchorTrack + (ctx.currentTime - anchorCtx) * rate;
      if (Math.abs(video.currentTime - expected) > 0.25) resync();
    }
    pump();
  }, 250);

  window.addEventListener('yt-navigate-finish', () => { const id = getVideoId(); if (id !== vid) onVideoChange(id); });
})();
