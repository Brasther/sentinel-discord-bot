'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  const number = value => Number(value || 0).toLocaleString('es-CL');
  const clamp = value => Math.max(0, Math.min(100, value));
  const colors = ['#61d4dc', '#bef264', '#c5a1f0', '#f3b568', '#85aaff', '#ef90ac'];
  const state = {users: [], events: [], config: {}, connected: false, snapshot: null, view: 'overview', connectingVoice: false};
  const series = new Map();
  let socket, lastConfig = '', toastTimer, pendingChaos;
  const titles = {
    overview: ['Inicio', 'Tu canal de voz', 'Estado y usuarios, en tiempo real.'],
    settings: ['Ajustes', 'Ajustes', 'Decide cuánto ruido tolerar y cuánto dura el castigo.'],
    history: ['Actividad', 'Actividad', 'Consulta qué ha hecho Sentinel.'],
    diagnostics: ['Diagnóstico', 'Diagnóstico', 'Detalles de conexión y herramientas de prueba.']
  };
  function text(tag, value, className) {
    const node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    return node;
  }
  function toast(message, error = false) {
    const node = $('#toast');
    node.textContent = message;
    node.classList.toggle('error', error);
    node.classList.add('visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove('visible'), 4500);
  }
  function connection(connected) {
    state.connected = connected;
    const node = $('#connection');
    node.className = 'connection ' + (connected ? 'online' : 'offline');
    node.replaceChildren(text('i', ''), text('span', connected ? 'Panel conectado' : 'Panel desconectado'));
    $('#disconnect-banner').hidden = connected;
    document.querySelectorAll('.calibrate, .chaos, #auto-toggle, #auto-join-toggle, #auto-rejoin-toggle, #connect-voice, #audio-sensitivity, #audio-mode, [data-key], #whitelist-form button, .immunity-button, .whitelist-entry button').forEach(button => button.disabled = !connected);
    document.querySelectorAll('.person').forEach(card => {
      const user = state.users.find(item => item.id === card.dataset.userId);
      card.querySelectorAll('.user-setting-control, .profile-select, .sensitivity-slider').forEach(control => { control.disabled = !connected || user?.immune; });
    });
    $('#restart-voice').disabled = !connected || !state.snapshot?.voice_connected;
    $('#connect-voice').disabled = !connected || !state.snapshot || !state.snapshot.connected || state.snapshot.preview || state.snapshot.voice_connected || state.connectingVoice;
    if (!connected) {
      $('#live-pill').textContent = 'SIN CONEXIÓN';
      const dialog = $('#calibration-dialog');
      if (dialog.open) dialog.close();
    }
  }
  function emit(event, payload, success) {
    if (!state.connected) { toast('Espera a que el panel se conecte.', true); return; }
    socket.timeout(5000).emit(event, payload, (error, response) => {
      if (error || !response?.ok) {
        toast(response?.error || 'No llegó una confirmación. Revisa la conexión.', true);
        syncConfig(state.config, true);
        return;
      }
      if (response.config) syncConfig(response.config);
      if (success) success(response);
    });
  }
  function navigate(view) {
    if (!titles[view]) return;
    state.view = view;
    document.querySelectorAll('.view').forEach(node => node.hidden = node.id !== `view-${view}`);
    document.querySelectorAll('.nav-item').forEach(node => node.classList.toggle('active', node.dataset.view === view));
    $('#breadcrumb').textContent = titles[view][0];
    $('#page-title').textContent = titles[view][1];
    $('#page-description').textContent = titles[view][2];
    $('.page-heading .calibrate').hidden = view !== 'overview';
    $('#restart-voice').hidden = view !== 'overview';
    $('#connect-voice').hidden = view !== 'overview' || !state.snapshot || !state.snapshot.connected || state.snapshot.preview || state.snapshot.voice_connected;
    drawChart();
  }
  document.querySelectorAll('[data-view]').forEach(node => node.addEventListener('click', () => navigate(node.dataset.view)));
  const settings = {
    UMBRAL_RMS: ['rms', ' RMS'], CASTIGO_MAX: ['limit', ' puntos'], CONTINUOUS_SECONDS: ['continuous', ' s'],
    TIEMPO_TOLERANCIA: ['tolerance', ' paquetes'], MUTE_TIME: ['duration', ' s']
  };
  function syncConfig(config, force = false) {
    const fingerprint = JSON.stringify(config);
    state.config = config;
    if (!force && fingerprint === lastConfig) return;
    lastConfig = fingerprint;
    for (const [key, [name, unit]] of Object.entries(settings)) {
      if (config[key] === undefined) continue;
      const input = $(`#setting-${name}`);
      if (document.activeElement !== input || force) input.value = config[key];
      $(`#value-${name}`).textContent = number(input.value) + unit;
    }
    $('#threshold-text').textContent = number(config.UMBRAL_RMS) + ' RMS';
    const adaptive = config.AUTO_SENSITIVITY !== false;
    $('#audio-sensitivity').value = adaptive ? 'auto' : 'manual';
    $('#setting-rms').closest('.setting').hidden = adaptive;
    $('#audio-mode').value = config.AUDIO_MODE || 'noise';
    $('#continuous-setting').hidden = config.AUDIO_MODE !== 'continuous';
    $('#audio-mode-help').textContent = config.AUDIO_MODE === 'continuous' ? 'Limita sonidos largos, aunque contengan voz. Úsalo con cuidado.' : 'Protege las voces y castiga el ruido repetido.';
    $('#threshold-text').textContent = adaptive ? 'Automático por usuario' : number(config.UMBRAL_RMS) + ' RMS';
    $('#auto-toggle').setAttribute('aria-checked', String(config.AUTO_CASTIGOS));
    $('#auto-join-toggle').setAttribute('aria-checked', String(config.AUTO_UNIR_VOZ !== false));
    $('#auto-rejoin-toggle').setAttribute('aria-checked', String(config.AUTO_REENTRAR_VOZ !== false));
    $('#mode-title').textContent = config.AUTO_CASTIGOS ? 'Castigos activados' : 'Solo observar';
    $('#mode-description').textContent = config.AUTO_CASTIGOS ? config.AUDIO_MODE === 'continuous' ? 'Limita también el audio continuo con voces.' : 'Mutea al acumular demasiado ruido.' : 'Detecta ruido sin aplicar castigos.';
    renderWhitelist();
  }
  document.querySelectorAll('[data-key]').forEach(input => {
    input.addEventListener('input', () => {
      const [name, unit] = settings[input.dataset.key];
      $(`#value-${name}`).textContent = number(input.value) + unit;
    });
    input.addEventListener('change', () => emit('update_config', {key: input.dataset.key, value: Number(input.value)}, () => toast('Ajuste guardado.')));
  });
  $('#auto-toggle').addEventListener('click', () => emit('update_config', {key: 'AUTO_CASTIGOS', value: !state.config.AUTO_CASTIGOS}, () => toast(state.config.AUTO_CASTIGOS ? 'Moderación automática activada.' : 'Modo observación activado. No se aplicarán nuevos castigos.')));
  $('#auto-join-toggle').addEventListener('click', () => {
    const enabled = state.config.AUTO_UNIR_VOZ === false;
    emit('update_config', {key: 'AUTO_UNIR_VOZ', value: enabled}, () => toast(enabled ? 'Entrada automática a voz activada.' : 'Entrada automática a voz desactivada.'));
  });
  $('#auto-rejoin-toggle').addEventListener('click', () => {
    const enabled = state.config.AUTO_REENTRAR_VOZ === false;
    emit('update_config', {key: 'AUTO_REENTRAR_VOZ', value: enabled}, () => toast(enabled ? 'Defensa de voz activada.' : 'Defensa de voz desactivada. Sentinel respetará la desconexión.'));
  });
  $('#audio-sensitivity').addEventListener('change', event => emit('update_config', {key: 'AUTO_SENSITIVITY', value: event.target.value === 'auto'}, () => toast('Sensibilidad actualizada.')));
  $('#audio-mode').addEventListener('change', event => emit('update_config', {key: 'AUDIO_MODE', value: event.target.value}, () => toast('Modo de audio actualizado.')));
  function renderWhitelist() {
    const list = $('#whitelist-list');
    list.replaceChildren();
    for (const id of state.config.WHITELIST || []) {
      const row = text('div', '', 'whitelist-entry');
      const user = state.users.find(user => user.id === id);
      row.append(text('span', user ? `${user.user} · ${id}` : id));
      const button = text('button', 'Retirar');
      button.disabled = !state.connected;
      button.addEventListener('click', () => emit('set_whitelist', {id, enabled: false}, () => toast('Inmunidad retirada.')));
      row.append(button); list.append(row);
    }
    if (!list.children.length) list.append(text('p', 'Ningún usuario protegido.', 'caption'));
  }
  $('#whitelist-form').addEventListener('submit', event => {
    event.preventDefault();
    const id = $('#whitelist-id').value.trim();
    if (!/^\d{1,20}$/.test(id)) { toast('Introduce un ID de Discord válido.', true); return; }
    emit('set_whitelist', {id, enabled: true}, () => { $('#whitelist-id').value = ''; toast('Usuario protegido.'); });
  });
  function duration(seconds) {
    const hrs = Math.floor(seconds / 3600), mins = Math.floor(seconds % 3600 / 60);
    return hrs ? `${hrs} h ${mins} min` : mins ? `${mins} min` : `${Math.floor(seconds)} s`;
  }
  function statusClass(status) {
    return status.includes('RUIDO') || status === 'AUDIO CONTINUO' ? 'noise' : status === 'INMUNE' ? 'immune' : status === 'SILENCIO' || status === 'GRACIA' ? 'quiet' : '';
  }
  function personCard(user) {
    const card = document.createElement('article');
    card.className = 'person'; card.dataset.userId = user.id;
    // La plantilla es constante. Los nombres de Discord se asignan como texto.
    card.innerHTML = '<div class="person-top"><div class="avatar"></div><div class="person-info"><div class="person-name"></div><div class="person-id" hidden></div></div><span class="status"></span></div><div class="bar-label"><span>Volumen</span><strong class="rms-value"></strong></div><div class="meter"><span class="rms-bar"></span></div><div class="bar-label"><span>Acumulación de ruido</span><strong class="points-value"></strong></div><div class="meter infractions"><span class="points-bar"></span></div><div class="setting user-sensitivity"><div><label>Sensibilidad de penalización</label><output class="sensitivity-value"></output></div><input class="sensitivity-slider" type="range" min="0" max="100" step="1"><div class="range-labels"><span>0 · tolerante</span><span>100 · sensible</span></div></div><div class="individual-profile"><label>Perfil rápido</label><select class="profile-select"><option value="custom" disabled>Personalizado</option><option value="tolerant">Tolerante · 5 s de gracia</option><option value="normal">Normal · 2 s de gracia</option><option value="strict">Estricto · sin gracia y voz sin proteger</option></select></div><details class="individual-settings"><summary>Ajustes individuales</summary><label class="individual-field">Umbral personal <output class="threshold-value"></output><input class="user-setting-control" data-key="threshold_rms" type="range" min="0" max="10000" step="25"><small>0 usa el umbral automático o general.</small></label><label class="individual-field">Velocidad de penalización<select class="user-setting-control" data-key="penalty_rate"><option value="1">Normal</option><option value="2">Rápida</option><option value="3">Muy rápida</option></select></label><label class="individual-field">Tiempo de gracia<select class="user-setting-control" data-key="grace_seconds"><option value="0">Sin gracia</option><option value="1">1 segundo</option><option value="2">2 segundos</option><option value="5">5 segundos</option><option value="10">10 segundos</option><option value="15">15 segundos</option><option value="30">30 segundos</option></select></label><label class="individual-field">Duración del mute<select class="user-setting-control" data-key="mute_duration"><option value="0">Usar ajuste general</option><option value="5">5 segundos</option><option value="10">10 segundos</option><option value="15">15 segundos</option><option value="30">30 segundos</option><option value="60">1 minuto</option><option value="120">2 minutos</option><option value="300">5 minutos</option></select></label><label class="voice-protection"><input class="user-setting-control" data-key="protect_voice" type="checkbox"> Proteger voz detectada <small>Protege la voz del VAD; en modo ruido, el audio persistente se penaliza tras 5 s. Desactívala para penalizar voz antes.</small></label></details><p class="audio-reason caption"></p><details class="audio-details"><summary>Detalles de audio</summary><p class="audio-metrics caption"></p></details><div class="person-bottom"><span class="punishments-value"></span><button class="immunity-button" aria-pressed="false">Proteger</button></div>';
    card.querySelector('.immunity-button').addEventListener('click', () => {
      const latest = state.users.find(item => item.id === user.id);
      emit('set_whitelist', {id: user.id, enabled: !latest?.immune}, () => toast('Inmunidad actualizada.'));
    });
    const slider = card.querySelector('.sensitivity-slider');
    slider.addEventListener('input', () => { card.querySelector('.sensitivity-value').textContent = `${slider.value}/100`; });
    slider.addEventListener('change', () => emit('set_user_sensitivity', {id: user.id, value: Number(slider.value)}, () => toast('Sensibilidad individual guardada.')));
    card.querySelector('[data-key="threshold_rms"]').addEventListener('input', event => {
      const value = Number(event.target.value);
      card.querySelector('.threshold-value').textContent = value ? `${number(value)} RMS` : 'Automático/general';
    });
    card.querySelector('.profile-select').addEventListener('change', event => emit('set_user_profile', {id: user.id, profile: event.target.value}, () => toast('Perfil individual aplicado.')));
    card.querySelectorAll('.user-setting-control').forEach(control => control.addEventListener('change', () => {
      const value = control.type === 'checkbox' ? control.checked : Number(control.value);
      emit('set_user_audio_setting', {id: user.id, key: control.dataset.key, value}, () => toast('Ajuste individual guardado.'));
    }));
    return card;
  }
  function renderPeople() {
    const container = $('#people');
    const query = $('#user-search').value.toLocaleLowerCase();
    const visible = state.users.filter(user => user.user.toLocaleLowerCase().includes(query));
    const ids = new Set(visible.map(user => user.id));
    container.querySelectorAll('.person').forEach(card => { if (!ids.has(card.dataset.userId)) card.remove(); });
    container.querySelectorAll('.empty-state').forEach(node => node.remove());
    for (const user of visible) {
      let card = Array.from(container.children).find(node => node.dataset.userId === user.id);
      if (!card) { card = personCard(user); container.append(card); }
      card.querySelector('.avatar').textContent = user.user.slice(0, 2).toLocaleUpperCase();
      card.querySelector('.person-name').textContent = user.user;
      card.querySelector('.person-name').title = user.user;
      card.querySelector('.person-id').textContent = user.id;
      const status = user.immune ? 'INMUNE' : user.status;
      card.querySelector('.status').textContent = ({VOZ: 'Hablando', SILENCIO: 'En silencio', RUIDO: 'Ruido alto', 'RUIDO CONTINUO': 'Ruido continuo', 'AUDIO CONTINUO': 'Audio continuo', ADAPTANDO: 'Ajustando', GRACIA: 'En periodo de gracia', CASTIGADO: 'Castigado', INMUNE: 'Protegido', CALIBRANDO: 'Calibrando', ANALIZANDO: 'Escuchando'})[status] || status;
      card.querySelector('.status').className = 'status ' + statusClass(status);
      const threshold = user.threshold ?? state.config.UMBRAL_RMS;
      card.querySelector('.rms-value').textContent = number(user.rms) + ' RMS';
      card.querySelector('.rms-value').title = number(user.rms) + ' RMS';
      card.querySelector('.rms-bar').style.width = clamp(user.rms / Math.max(1, threshold * 3) * 100) + '%';
      card.querySelector('.audio-reason').textContent = user.reason || 'Esperando datos de audio.';
      card.querySelector('.audio-metrics').textContent = `Umbral: ${number(threshold)} RMS · Fondo: ${number(user.noise_floor)} RMS · Nivel: ${user.dbfs ?? '—'} dBFS · Saturación: ${number((user.clipping || 0) * 100)} % · Audio continuo: ${number(user.continuous_seconds)} s`;
      card.querySelector('.points-value').textContent = `${number(user.violation)} / ${number(user.limit)}`;
      card.querySelector('.points-bar').style.width = clamp(user.violation / Math.max(1, user.limit) * 100) + '%';
      const sensitivity = Number(user.sensitivity ?? 50), slider = card.querySelector('.sensitivity-slider');
      if (document.activeElement !== slider) slider.value = sensitivity;
      card.querySelector('.sensitivity-value').textContent = `${sensitivity}/100`;
      slider.disabled = !state.connected || user.immune;
      slider.setAttribute('aria-label', `Sensibilidad de penalización para ${user.user}`);
      const settings = user.audio_settings || {profile: 'normal', threshold_rms: 0, penalty_rate: 1, grace_seconds: 2, mute_duration: 0, protect_voice: true};
      const profile = card.querySelector('.profile-select');
      if (document.activeElement !== profile) profile.value = settings.profile || 'custom';
      profile.disabled = !state.connected || user.immune;
      const thresholdSlider = card.querySelector('[data-key="threshold_rms"]');
      if (document.activeElement !== thresholdSlider) thresholdSlider.value = settings.threshold_rms;
      card.querySelector('.threshold-value').textContent = settings.threshold_rms ? `${number(settings.threshold_rms)} RMS` : 'Automático/general';
      for (const [key, value] of [['penalty_rate', settings.penalty_rate], ['grace_seconds', settings.grace_seconds], ['mute_duration', settings.mute_duration]]) {
        const control = card.querySelector(`[data-key="${key}"]`);
        if (document.activeElement !== control) control.value = value;
      }
      const voiceProtection = card.querySelector('[data-key="protect_voice"]');
      if (document.activeElement !== voiceProtection) voiceProtection.checked = settings.protect_voice;
      card.querySelectorAll('.user-setting-control').forEach(control => { control.disabled = !state.connected || user.immune; });
      card.querySelector('.punishments-value').textContent = `${number(user.punishments)} ${user.punishments === 1 ? 'castigo registrado' : 'castigos registrados'}`;
      const button = card.querySelector('.immunity-button');
      button.textContent = user.immune ? 'Protegido ✓' : 'Proteger';
      button.setAttribute('aria-label', (user.immune ? 'Quitar protección a ' : 'Proteger a ') + user.user);
      button.setAttribute('aria-pressed', String(user.immune)); button.disabled = !state.connected;
    }
    if (!visible.length) {
      const empty = text('div', '', 'empty-state');
      empty.append(text('strong', query ? 'No encontramos ese usuario' : 'Aún no hay usuarios en escucha'), text('span', query ? 'Prueba con otro nombre.' : 'Conecta Sentinel con !vigilar para comenzar.'));
      container.append(empty);
    }
  }
  $('#user-search').addEventListener('input', renderPeople);
  function addEvents(events) {
    const existing = new Map(state.events.map(event => [event.id, event]));
    events.forEach(event => existing.set(event.id, event));
    state.events = Array.from(existing.values()).sort((a, b) => b.id - a.id).slice(0, 200);
    renderEvents();
  }
  const eventTime = event => new Date(event.time * 1000).toLocaleTimeString('es-CL');
  const levels = {info: 'Información', warning: 'Aviso', error: 'Error'};
  function renderEvents() {
    $('#event-count').textContent = state.events.length;
    const recent = $('#recent-events'); recent.replaceChildren();
    for (const event of state.events.slice(0, 5)) {
      const row = text('div', '', 'event ' + event.level);
      const details = text('div', '');
      details.append(text('p', event.msg), text('time', eventTime(event)));
      row.append(text('span', '', 'event-mark'), details); recent.append(row);
    }
    if (!state.events.length) recent.append(text('div', 'Los eventos del sistema aparecerán aquí.', 'empty-small'));
    const query = $('#event-search').value.toLocaleLowerCase(), level = $('#event-filter').value;
    const rows = $('#history-rows'); rows.replaceChildren();
    for (const event of state.events.filter(event => (level === 'all' || level === event.level) && event.msg.toLocaleLowerCase().includes(query))) {
      const row = text('tr', '');
      const severity = text('td', ''); severity.append(text('span', levels[event.level] || event.level, 'status ' + (event.level === 'warning' || event.level === 'error' ? 'noise' : 'quiet')));
      row.append(text('td', eventTime(event)), severity, text('td', event.msg)); rows.append(row);
    }
    if (!rows.children.length) { const row = text('tr', ''); const cell = text('td', 'No hay eventos que mostrar.'); cell.colSpan = 3; row.append(cell); rows.append(row); }
  }
  $('#event-search').addEventListener('input', renderEvents);
  $('#event-filter').addEventListener('change', renderEvents);
  $('#export-events').addEventListener('click', () => {
    // Neutraliza fórmulas al abrir nombres o mensajes en Excel.
    const field = value => '"' + String(value).replace(/^[=+\-@]/, "'$&").replaceAll('"', '""') + '"';
    const rows = [['fecha', 'nivel', 'evento'], ...state.events.map(event => [new Date(event.time * 1000).toISOString(), levels[event.level], event.msg])];
    const blob = new Blob(['\ufeff' + rows.map(row => row.map(field).join(';')).join('\r\n')], {type: 'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = 'sentinel-actividad.csv'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  document.querySelectorAll('.calibrate').forEach(button => button.addEventListener('click', () => emit('start_calibration', {}, () => toast('Calibración iniciada. Mantén silencio durante 5 segundos.'))));
  $('#restart-voice').addEventListener('click', () => {
    $('#restart-voice').disabled = true;
    emit('restart_voice', {}, () => toast('Sentinel se reiniciará y volverá al mismo canal.'));
  });
  $('#connect-voice').addEventListener('click', () => {
    if (!state.connected || state.connectingVoice) return;
    state.connectingVoice = true;
    $('#connect-voice').disabled = true;
    socket.timeout(25000).emit('connect_voice', {}, (error, response) => {
      state.connectingVoice = false;
      if (error || !response?.ok) {
        toast(response?.error || 'No llegó una confirmación. Revisa la conexión.', true);
        $('#connect-voice').disabled = !state.connected || state.snapshot?.voice_connected;
        return;
      }
      toast(response.already_connected ? `Sentinel ya está en ${response.channel}.` : `Sentinel conectado a ${response.channel}.`);
    });
    toast('Buscando un canal de voz ocupado…');
  });
  $('#calibration-dialog').addEventListener('cancel', event => event.preventDefault());
  function renderCalibration(calibration) {
    const dialog = $('#calibration-dialog');
    if (calibration.active && !dialog.open && state.connected) dialog.showModal();
    if (!calibration.active && dialog.open) dialog.close();
    $('#calibration-progress').style.width = clamp(calibration.progress) + '%';
    $('#calibration-percent').textContent = number(calibration.progress) + ' %';
    $('#calibration-samples').textContent = number(calibration.samples) + ' muestras';
    document.querySelectorAll('.calibrate').forEach(button => button.disabled = calibration.active || !state.connected);
  }
  document.querySelectorAll('.chaos').forEach(button => button.addEventListener('click', () => {
    pendingChaos = button.dataset.action;
    $('#chaos-description').textContent = pendingChaos === 'kick' ? 'Sentinel se desconectará del canal y su autodefensa podrá reiniciar el proceso.' : 'El bot será muteado y ensordecido. Comprueba después su estado en Discord.';
    $('#chaos-dialog').showModal();
  }));
  $('#chaos-cancel').addEventListener('click', () => $('#chaos-dialog').close());
  $('#chaos-confirm').addEventListener('click', async () => {
    $('#chaos-dialog').close();
    try {
      const response = await fetch('/simulate_attack', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Sentinel-Action': 'chaos'}, body: JSON.stringify({type: pendingChaos})});
      const body = await response.json();
      if (!response.ok) throw new Error(body.error);
      toast('Prueba enviada a Discord.');
    } catch (error) { toast(error.message || 'No se pudo ejecutar la prueba.', true); }
  });
  function renderState(snapshot) {
    state.snapshot = snapshot; state.users = snapshot.users || [];
    $('#restart-voice').disabled = !state.connected || snapshot.preview || !snapshot.voice_connected;
    $('#connect-voice').hidden = state.view !== 'overview' || !snapshot.connected || snapshot.preview || snapshot.voice_connected;
    $('#connect-voice').disabled = !state.connected || !snapshot.connected || snapshot.preview || snapshot.voice_connected || state.connectingVoice;
    syncConfig(snapshot.config);
    if (snapshot.events) addEvents(snapshot.events);
    $('#preview-badge').hidden = !snapshot.preview;
    $('#metric-users').textContent = number(state.users.length);
    $('#metric-noise').textContent = number(state.users.filter(user => (user.status.includes('RUIDO') || user.status === 'AUDIO CONTINUO') && !user.immune).length);
    $('#metric-punishments').textContent = number(Object.values(snapshot.config.STATS || {}).reduce((sum, count) => sum + count, 0));
    $('#metric-latency').textContent = snapshot.latency_ms ?? '—';
    $('#latency-note').textContent = snapshot.connected ? 'Tiempo de respuesta del gateway' : 'Discord sin conexión';
    $('#channel-note').textContent = snapshot.channel || 'Esperando un canal de voz';
    $('#people-count').textContent = state.users.length;
    $('#health-title').textContent = snapshot.voice_connected ? 'La escucha está activa' : snapshot.connected ? 'Listo para entrar al canal' : 'Discord sin conexión';
    $('#health-description').textContent = snapshot.preview ? 'Datos simulados para explorar el panel.' : snapshot.voice_connected ? 'Sentinel está analizando el audio del canal.' : snapshot.connected ? 'Usa !vigilar desde un canal de voz.' : 'Revisa la consola y la configuración del bot.';
    $('#health-discord').textContent = snapshot.connected ? 'Conectado' : 'Desconectado';
    $('#health-voice').textContent = snapshot.channel || 'Sin canal';
    $('#health-voice').title = snapshot.channel || '';
    $('#health-audio').textContent = snapshot.preview ? 'Simulado' : snapshot.voice_connected ? `Hace ${number(snapshot.heartbeat_age)} s` : 'Sin escucha';
    $('#health-uptime').textContent = duration(snapshot.uptime);
    $('#live-pill').textContent = snapshot.preview ? 'DEMOSTRACIÓN' : snapshot.voice_connected ? 'EN ESCUCHA' : 'EN ESPERA';
    $('#chart-empty').hidden = state.users.length > 0;
    renderPeople(); renderCalibration(snapshot.calibration);
    const diagnostic = $('#diagnostic-data'); diagnostic.replaceChildren();
    const values = {'Versión': '26 · Control de voz', 'Python': snapshot.system_diagnostics?.python || '—', 'Plataforma': snapshot.system_diagnostics?.platform || '—', 'Modo': snapshot.preview ? 'Demostración' : snapshot.config.AUTO_CASTIGOS ? 'Moderación automática' : 'Observación', 'Paquetes procesados': number(snapshot.frames), 'Tiempo activo': duration(snapshot.uptime), 'Latencia': `${snapshot.latency_ms ?? '—'} ms`, 'Último paquete': `${number(snapshot.heartbeat_age)} s`, 'Cooldown de castigos': '5 segundos', 'Watchdog': '30 segundos sin audio', 'Actualización del panel': '10 Hz', 'Formato de audio': 'PCM mono · 48 kHz · 16 bits'};
    for (const [label, value] of Object.entries(values)) { const row = text('div', ''); row.append(text('dt', label), text('dd', value)); diagnostic.append(row); }
    const setup = snapshot.system_diagnostics?.setup_check;
    $('#setup-check').textContent = setup ? `${setup.ok ? 'Correcto' : 'Revisar'} · ${setup.message}` : 'No disponible.';
    $('#setup-check').className = 'panel-intro ' + (setup?.ok ? 'check-ok' : 'check-fail');
    const dependencies = $('#dependency-data'); dependencies.replaceChildren();
    for (const [label, version] of Object.entries(snapshot.system_diagnostics?.dependencies || {})) { const row = text('div', ''); row.append(text('dt', label), text('dd', version)); dependencies.append(row); }
    const permissions = $('#permission-data'); permissions.replaceChildren();
    const checks = snapshot.system_diagnostics?.permissions || [];
    if (!checks.length) permissions.append(text('p', snapshot.preview ? 'Vista previa: sin conexión a Discord.' : 'Conecta Discord para comprobar los permisos del bot.', 'caption'));
    for (const guild of checks) { const row = text('div', '', 'diagnostic-permissions'); row.append(text('strong', guild.guild)); for (const [key, label] of [['view_channel','Ver canal'],['connect','Conectar'],['mute_members','Silenciar miembros']]) row.append(text('span', `${label}: ${guild[key] ? 'OK' : 'Falta'}`, guild[key] ? 'check-ok' : 'check-fail')); if (guild.jail_configured) row.append(text('span', `Mover miembros: ${guild.move_members ? 'OK' : 'Falta'}`, guild.move_members ? 'check-ok' : 'check-fail')); permissions.append(row); }
    const errors = $('#recent-errors'); errors.replaceChildren();
    for (const item of snapshot.system_diagnostics?.recent_errors || []) errors.append(text('p', `${new Date(item.time * 1000).toLocaleString('es-CL')} · ${item.message}`, 'caption'));
    if (!errors.children.length) errors.append(text('p', 'No hay errores recientes.', 'caption'));
    for (const [label, value] of Object.entries({'Audio decodificado': snapshot.audio_diagnostics?.decoded ?? 0, 'Paquetes descartados': snapshot.audio_diagnostics?.discarded ?? 0, 'Audio reconstruido (no se castiga)': snapshot.audio_diagnostics?.concealed ?? 0, 'Último error de audio': snapshot.audio_diagnostics?.last_error || 'Ninguno', 'Sensibilidad': snapshot.config.AUTO_SENSITIVITY === false ? 'Manual' : 'Automática por usuario'})) { const row = text('div', ''); row.append(text('dt', label), text('dd', String(value))); diagnostic.append(row); }
    document.querySelectorAll('.chaos').forEach(button => button.disabled = snapshot.preview || !snapshot.voice_connected || !state.connected);
    sampleChart();
  }
  function sampleChart() {
    const now = Date.now(), active = new Set(state.users.map(user => user.id));
    for (const id of series.keys()) if (!active.has(id)) series.delete(id);
    state.users.forEach((user, index) => {
      let item = series.get(user.id);
      if (!item) { item = {name: user.user, color: colors[index % colors.length], data: []}; series.set(user.id, item); }
      item.name = user.user; item.data.push([now, user.rms]);
      while (item.data.length && (item.data[0][0] < now - 30000 || item.data.length > 310)) item.data.shift();
    });
    const legend = $('#chart-legend'); legend.replaceChildren();
    for (const item of series.values()) { const label = text('span', '', 'legend-item'), dot = text('i', '', 'legend-dot'); dot.style.background = item.color; label.append(dot, text('span', item.name)); legend.append(label); }
    if (!series.size) legend.append(text('span', 'Esperando señales de audio'));
    drawChart();
  }
  const canvas = $('#audio-chart'), context = canvas.getContext('2d');
  function drawChart() {
    if ($('#view-overview').hidden || !$('.chart-fold').open) return;
    const rect = canvas.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
    if (!rect.width || !rect.height) return;
    const width = rect.width, height = rect.height;
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) { canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); }
    context.setTransform(ratio, 0, 0, ratio, 0, 0); context.clearRect(0, 0, width, height);
    const left = 45, top = 12, plotWidth = width - left - 8, plotHeight = height - top - 23;
    const samples = Array.from(series.values()).flatMap(item => item.data.map(point => point[1]));
    const automatic = state.config.AUTO_SENSITIVITY !== false;
    const peak = Math.max(50, automatic ? 0 : state.config.UMBRAL_RMS || 1000, ...samples);
    const step = peak < 200 ? 10 : peak < 2000 ? 100 : 1000;
    const max = Math.ceil(peak * 1.12 / step) * step, now = Date.now();
    const y = value => top + plotHeight * (1 - value / max);
    const x = timestamp => left + (timestamp - now + 30000) / 30000 * plotWidth;
    context.font = '9px "Segoe UI", sans-serif'; context.fillStyle = '#637991'; context.textAlign = 'right';
    for (let tick = 0; tick <= 4; tick++) {
      const value = max / 4 * tick, position = y(value);
      context.fillText(number(value), left - 9, position + 3);
      context.beginPath(); context.strokeStyle = '#2b374733'; context.lineWidth = 1; context.moveTo(left, position); context.lineTo(width, position); context.stroke();
    }
    context.textAlign = 'center'; [-30, -20, -10, 0].forEach(second => context.fillText(second ? `${second} s` : 'ahora', Math.min(width - 15, left + (second + 30) / 30 * plotWidth), height - 3));
    context.save(); context.beginPath(); context.rect(left, top, plotWidth, plotHeight); context.clip();
    const thresholdY = y(state.config.UMBRAL_RMS || 1000);
    if (!automatic) { context.beginPath(); context.strokeStyle = '#f3b56877'; context.setLineDash([4, 5]); context.moveTo(left, thresholdY); context.lineTo(width, thresholdY); context.stroke(); context.setLineDash([]); }
    for (const item of series.values()) {
      if (item.data.length < 2) continue;
      context.beginPath(); item.data.forEach(([timestamp, value], index) => { if (index === 0) context.moveTo(x(timestamp), y(value)); else context.lineTo(x(timestamp), y(value)); });
      context.strokeStyle = item.color; context.lineWidth = 1.8; context.lineJoin = 'round'; context.stroke();
    }
    context.restore();
  }
  new ResizeObserver(drawChart).observe(canvas.parentElement);
  $('.chart-fold').addEventListener('toggle', drawChart);
  navigate('overview');
  setInterval(() => { $('#clock').textContent = new Date().toLocaleTimeString('es-CL', {hour: '2-digit', minute: '2-digit'}); }, 1000);
  connection(false);
  if (typeof window.io !== 'function') { toast('No se pudo cargar Socket.IO. Revisa los archivos del panel.', true); return; }
  socket = window.io({reconnection: true, reconnectionDelay: 1000});
  socket.on('connect', () => { connection(true); fetch('/api/status').then(response => response.json()).then(renderState).catch(() => toast('No se pudo obtener el estado inicial.', true)); });
  socket.on('disconnect', () => connection(false));
  socket.on('connect_error', () => connection(false));
  socket.on('state_update', renderState);
  socket.on('config_update', config => syncConfig(config));
  socket.on('log_event', event => { addEvents([event]); if (event.level === 'error' || event.msg.includes('Calibración completada') || event.msg.includes('Calibración cancelada')) toast(event.msg, event.level === 'error' || event.level === 'warning'); });
})();
