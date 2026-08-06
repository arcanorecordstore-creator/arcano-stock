/* Reproductor flotante unificado de Arcano.
   Una sola interfaz (barra de progreso + prev/play/next + cola) para MP3/MP4,
   YouTube y SoundCloud. El reproductor de cada fuente va oculto: la UI de arriba
   lo maneja via <audio>, la YouTube IFrame API y la SoundCloud Widget API.
   Botones: [data-play="local"|"youtube"|"soundcloud"] con sus data-attributes.
*/
(function () {
    'use strict';

    var player = document.getElementById('floating-player');
    if (!player) return;

    function $(id) { return document.getElementById(id); }
    var fpCover = $('fp-cover'), fpTitle = $('fp-title'), fpSub = $('fp-sub'),
        fpAudio = $('fp-audio'), fpSlot = $('fp-engine-slot'),
        fpPlay = $('fp-playpause'), fpPrev = $('fp-prev'), fpNext = $('fp-next'),
        fpClose = $('fp-close'), fpQueueBtn = $('fp-queue'), fpQueuePanel = $('fp-queue-panel'),
        fpProgress = $('fp-progress'), fpFill = $('fp-progress-fill'), fpKnob = $('fp-progress-knob'),
        fpTimeCur = $('fp-time-cur'), fpTimeTotal = $('fp-time-total'),
        fpCart = $('fp-cart'), fpFallback = $('fp-fallback');

    var playlist = [], playIndex = 0, playCover = '';
    var modoEscucharTodo = false, escucharTodoTimer = null;
    var engine = null, isPlaying = false, activeBtn = null, seeking = false;
    var volumen = 1;
    try { var vs = localStorage.getItem('arcano-volume'); if (vs !== null) volumen = Math.min(1, Math.max(0, parseFloat(vs))); } catch (e) {}

    function fmt(s) { s = Math.max(0, Math.floor(s || 0)); var m = Math.floor(s / 60); var ss = s % 60; return m + ':' + (ss < 10 ? '0' : '') + ss; }
    function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
    function registrarPlay(id) { if (!id) return; try { fetch('/api/track/' + id + '/play', { method: 'POST', keepalive: true }); } catch (e) {} }

    // ---- Disponibilidad de las APIs externas ----
    var ytReady = !!(window.YT && window.YT.Player);
    var ytPending = [];
    var prevYtCb = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = function () {
        ytReady = true;
        if (typeof prevYtCb === 'function') { try { prevYtCb(); } catch (e) {} }
        ytPending.splice(0).forEach(function (fn) { try { fn(); } catch (e) {} });
    };
    function whenYt(fn) { if (ytReady) fn(); else ytPending.push(fn); }
    function scReady() { return !!(window.SC && window.SC.Widget); }

    // ---- UI ----
    function setPlayingUI(p) {
        isPlaying = p;
        var pl = fpPlay.querySelector('.fp-ico-play'), pa = fpPlay.querySelector('.fp-ico-pause');
        if (pl) pl.hidden = p; if (pa) pa.hidden = !p;
        document.querySelectorAll('[data-play]').forEach(function (b) {
            var act = (b === activeBtn);
            b.classList.toggle('is-playing', act);
            var pi = b.querySelector('.play-icon'), pz = b.querySelector('.pause-icon');
            if (pi) pi.hidden = act; if (pz) pz.hidden = !act;
        });
    }
    function setProgress(cur, dur) {
        if (seeking) return;
        fpTimeCur.textContent = fmt(cur);
        fpTimeTotal.textContent = fmt(dur);
        var pct = dur > 0 ? Math.min(100, cur / dur * 100) : 0;
        fpFill.style.width = pct + '%';
        if (fpKnob) fpKnob.style.left = pct + '%';
        fpProgress.setAttribute('aria-valuenow', Math.round(pct));
    }
    function resetProgress() { setProgress(0, 0); }
    function mostrarInfo(t) {
        var cover = t.cover || playCover;
        if (cover) { fpCover.src = cover; fpCover.style.display = ''; } else { fpCover.style.display = 'none'; }
        fpTitle.textContent = (t.artist ? t.artist + ' - ' : '') + (t.title || '');
        fpSub.textContent = playlist.length > 1 ? ('Tema ' + (playIndex + 1) + ' de ' + playlist.length) : '';
    }
    function mostrarPlayer() { player.hidden = false; setTimeout(function () { player.classList.add('visible'); }, 10); }
    function setFallback(url, label) {
        if (!fpFallback) return;
        fpFallback.href = url; fpFallback.textContent = 'Si no suena, abrir en ' + label + ' ↗'; fpFallback.hidden = false;
    }

    // ---- Motores ----
    function destruirEngine() {
        if (escucharTodoTimer) { clearTimeout(escucharTodoTimer); escucharTodoTimer = null; }
        if (engine) { try { engine.destroy(); } catch (e) {} engine = null; }
        try { fpAudio.pause(); fpAudio.onended = null; fpAudio.ontimeupdate = null; fpAudio.onplay = null; fpAudio.onpause = null; fpAudio.removeAttribute('src'); fpAudio.load(); } catch (e) {}
        fpSlot.innerHTML = '';
        if (fpFallback) fpFallback.hidden = true;
    }

    function engineLocal(t) {
        fpAudio.src = t.audioUrl; fpAudio.volume = volumen;
        fpAudio.ontimeupdate = function () { setProgress(fpAudio.currentTime, fpAudio.duration || 0); };
        fpAudio.onended = onEnded;
        fpAudio.onplay = function () { setPlayingUI(true); };
        fpAudio.onpause = function () { setPlayingUI(false); };
        fpAudio.play().catch(function () {});
        return {
            toggle: function () { if (fpAudio.paused) fpAudio.play().catch(function () {}); else fpAudio.pause(); },
            seekPct: function (p) { if (fpAudio.duration) fpAudio.currentTime = p * fpAudio.duration; },
            destroy: function () { fpAudio.onended = null; fpAudio.ontimeupdate = null; fpAudio.onplay = null; fpAudio.onpause = null; }
        };
    }

    function engineSoundCloud(t) {
        var iframe = document.createElement('iframe');
        iframe.allow = 'autoplay'; iframe.className = 'fp-sc-iframe'; iframe.width = '100%'; iframe.height = '120'; iframe.frameBorder = '0';
        iframe.src = 'https://w.soundcloud.com/player/?url=' + encodeURIComponent(t.soundcloudUrl) +
                     '&auto_play=true&hide_related=true&show_comments=false&show_user=false&show_reposts=false&visual=false';
        fpSlot.appendChild(iframe);
        setFallback(t.soundcloudUrl, 'SoundCloud');
        var dur = 0, w = null;
        if (scReady()) {
            w = SC.Widget(iframe);
            w.bind(SC.Widget.Events.READY, function () {
                try { w.setVolume(volumen * 100); } catch (e) {}
                w.getDuration(function (d) { dur = (d || 0) / 1000; });
                w.play();
            });
            w.bind(SC.Widget.Events.PLAY, function () { setPlayingUI(true); });
            w.bind(SC.Widget.Events.PAUSE, function () { setPlayingUI(false); });
            w.bind(SC.Widget.Events.PLAY_PROGRESS, function (e) {
                if (!dur) w.getDuration(function (d) { dur = (d || 0) / 1000; });
                setProgress((e.currentPosition || 0) / 1000, dur);
            });
            w.bind(SC.Widget.Events.FINISH, onEnded);
        }
        return {
            toggle: function () { if (w) w.toggle(); },
            seekPct: function (p) { if (w && dur) w.seekTo(p * dur * 1000); },
            destroy: function () { try { if (w) w.pause(); } catch (e) {} }
        };
    }

    function engineYouTube(t) {
        var holder = document.createElement('div');
        holder.id = 'fp-yt-holder';
        fpSlot.appendChild(holder);
        setFallback('https://www.youtube.com/watch?v=' + t.youtubeId + (t.youtubeStart ? '&t=' + t.youtubeStart + 's' : ''), 'YouTube');
        var yt = null, dur = 0, poll = null;
        whenYt(function () {
            yt = new YT.Player(holder.id, {
                videoId: t.youtubeId, width: '160', height: '90',
                playerVars: { autoplay: 1, start: parseInt(t.youtubeStart || 0, 10), rel: 0, modestbranding: 1, controls: 0, playsinline: 1 },
                events: {
                    onReady: function (ev) {
                        try { ev.target.setVolume(volumen * 100); ev.target.playVideo(); } catch (e) {}
                        dur = ev.target.getDuration() || 0;
                        poll = setInterval(function () { try { setProgress(ev.target.getCurrentTime(), ev.target.getDuration() || dur); } catch (e) {} }, 300);
                    },
                    onStateChange: function (ev) {
                        if (ev.data === YT.PlayerState.PLAYING) setPlayingUI(true);
                        else if (ev.data === YT.PlayerState.PAUSED) setPlayingUI(false);
                        else if (ev.data === YT.PlayerState.ENDED) onEnded();
                    }
                }
            });
        });
        return {
            toggle: function () { if (!yt) return; try { var st = yt.getPlayerState(); if (st === 1) yt.pauseVideo(); else yt.playVideo(); } catch (e) {} },
            seekPct: function (p) { if (yt && yt.seekTo) { var d = yt.getDuration() || dur; if (d) yt.seekTo(p * d, true); } },
            destroy: function () { if (poll) clearInterval(poll); try { if (yt && yt.destroy) yt.destroy(); } catch (e) {} }
        };
    }

    // ---- Reproducir el track actual de la playlist ----
    function reproducirActual() {
        var t = playlist[playIndex]; if (!t) return;
        if (t.btn) activeBtn = t.btn;
        destruirEngine();
        resetProgress();
        mostrarInfo(t);
        actualizarCarrito(t.discId, t.disponible);
        actualizarNav();
        actualizarCola();
        registrarPlay(t.id);
        setPlayingUI(true);
        if (t.kind === 'local') engine = engineLocal(t);
        else if (t.kind === 'soundcloud') engine = engineSoundCloud(t);
        else if (t.kind === 'youtube') engine = engineYouTube(t);
        if (modoEscucharTodo) escucharTodoTimer = setTimeout(function () { siguiente(true); }, 60000);
    }
    function onEnded() {
        if (playIndex < playlist.length - 1) { playIndex++; reproducirActual(); }
        else if (modoEscucharTodo) cerrar();
        else setPlayingUI(false);
    }
    function siguiente(forzar) { if (playIndex < playlist.length - 1) { playIndex++; reproducirActual(); } else if (forzar || modoEscucharTodo) cerrar(); }
    function anterior() { if (playIndex > 0) { playIndex--; reproducirActual(); } }

    // ---- Controles ----
    fpPlay.addEventListener('click', function () { if (engine && engine.toggle) engine.toggle(); });
    fpPrev.addEventListener('click', anterior);
    fpNext.addEventListener('click', function () { siguiente(false); });
    fpClose.addEventListener('click', cerrar);

    function pctFromEvent(ev) {
        var rect = fpProgress.getBoundingClientRect();
        var x = (ev.touches ? ev.touches[0].clientX : ev.clientX) - rect.left;
        var p = Math.min(1, Math.max(0, x / rect.width));
        fpFill.style.width = (p * 100) + '%';
        if (fpKnob) fpKnob.style.left = (p * 100) + '%';
        return p;
    }
    fpProgress.addEventListener('click', function (ev) { if (engine && engine.seekPct) engine.seekPct(pctFromEvent(ev)); });

    function cerrar() {
        destruirEngine();
        playlist = []; playIndex = 0; activeBtn = null;
        modoEscucharTodo = false; sincronizarListenAll();
        resetProgress(); setPlayingUI(false);
        if (fpCart) fpCart.hidden = true;
        if (fpQueuePanel) fpQueuePanel.hidden = true;
        player.classList.remove('visible');
        setTimeout(function () { player.hidden = true; }, 250);
    }

    // Expuesto para el pjax: cerrar el reproductor al entrar al checkout ("finalizar compra")
    window.ArcanoPlayer = {
        cerrar: cerrar,
        estaAbierto: function () { return !player.hidden; }
    };

    // ---- Navegacion / cola ----
    function actualizarNav() {
        var multi = playlist.length > 1;
        fpPrev.disabled = (!multi || playIndex === 0);
        fpNext.disabled = (!multi || playIndex === playlist.length - 1);
        fpPrev.style.display = multi ? '' : 'none';
        fpNext.style.display = multi ? '' : 'none';
        if (fpQueueBtn) fpQueueBtn.style.display = multi ? '' : 'none';
    }
    function actualizarCola() {
        if (!fpQueuePanel) return;
        if (playlist.length <= 1) { fpQueuePanel.innerHTML = ''; fpQueuePanel.hidden = true; return; }
        var html = '';
        playlist.forEach(function (t, i) {
            html += '<button type="button" class="fp-queue-item' + (i === playIndex ? ' actual' : '') + '" data-i="' + i + '">' +
                    '<span class="fp-queue-num">' + (i + 1) + '</span>' +
                    '<span class="fp-queue-name">' + esc((t.artist ? t.artist + ' - ' : '') + (t.title || '')) + '</span></button>';
        });
        fpQueuePanel.innerHTML = html;
    }
    if (fpQueueBtn && fpQueuePanel) {
        fpQueueBtn.addEventListener('click', function () { fpQueuePanel.hidden = !fpQueuePanel.hidden; });
        fpQueuePanel.addEventListener('click', function (ev) {
            var b = ev.target.closest('.fp-queue-item'); if (!b) return;
            playIndex = parseInt(b.dataset.i, 10); reproducirActual(); fpQueuePanel.hidden = true;
        });
    }

    // ---- Boton "Al carrito" ----
    function actualizarCarrito(discId, disp) {
        if (!fpCart) return;
        if (!discId || !disp) { fpCart.hidden = true; return; }
        fpCart.hidden = false; fpCart.dataset.discId = String(discId);
        var ya = !!(window.ArcanoCart && window.ArcanoCart.has && window.ArcanoCart.has(discId));
        var a = fpCart.querySelector('.fp-cart-add'), i = fpCart.querySelector('.fp-cart-in');
        if (a) a.hidden = ya; if (i) i.hidden = !ya; fpCart.classList.toggle('is-in-cart', ya);
    }
    if (fpCart) {
        fpCart.addEventListener('click', function () {
            var id = fpCart.dataset.discId;
            if (!id || !window.ArcanoCart || !window.ArcanoCart.add) return;
            window.ArcanoCart.add(id); actualizarCarrito(id, true);
            if (window.ArcanoCartDrawer && window.ArcanoCartDrawer.open) window.ArcanoCartDrawer.open();
            else if (window.arcanoToast) window.arcanoToast('✓ Agregado al carrito');
        });
    }

    // ---- Entradas de reproduccion ----
    function iniciarLista(tracks, cover, escucharTodo) {
        playlist = tracks; playIndex = 0; playCover = cover || ''; modoEscucharTodo = !!escucharTodo;
        sincronizarListenAll(); mostrarPlayer(); reproducirActual();
    }
    function trackDeBtn(btn) {
        var kind = btn.dataset.play;
        var t = { id: btn.dataset.trackId, kind: kind, btn: btn, cover: btn.dataset.coverUrl, title: btn.dataset.trackTitle, artist: btn.dataset.trackArtist,
                  discId: btn.dataset.discId, disponible: (btn.dataset.disponible === '1') };
        if (kind === 'local') t.audioUrl = btn.dataset.audioUrl;
        else if (kind === 'youtube') { t.youtubeId = btn.dataset.youtubeId; t.youtubeStart = btn.dataset.youtubeStart; }
        else if (kind === 'soundcloud') t.soundcloudUrl = btn.dataset.soundcloudUrl;
        return t;
    }
    function togglearSiMismo(btn) {
        if (activeBtn === btn && !player.hidden) { if (engine && engine.toggle) engine.toggle(); return true; }
        return false;
    }
    function iniciarPlaylist(btn) {
        var tracks; try { tracks = JSON.parse(btn.dataset.tracks || '[]'); } catch (e) { tracks = []; }
        if (!tracks.length) return false;
        if (togglearSiMismo(btn)) return true;
        activeBtn = btn; iniciarLista(tracks, btn.dataset.coverUrl, false); return true;
    }
    function iniciarTracklist(btn) {
        var btns = [].slice.call(document.querySelectorAll('.public-track-play-btn'));
        if (btns.length <= 1) return false;
        if (togglearSiMismo(btn)) return true;
        activeBtn = btn;
        playlist = btns.map(trackDeBtn); playIndex = btns.indexOf(btn); playCover = btn.dataset.coverUrl || ''; modoEscucharTodo = false;
        sincronizarListenAll(); mostrarPlayer(); reproducirActual(); return true;
    }

    document.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-play]'); if (!btn) return;
        e.preventDefault(); e.stopPropagation();
        if (btn.dataset.tracks) { iniciarPlaylist(btn); return; }
        if (btn.classList.contains('public-track-play-btn') && iniciarTracklist(btn)) return;
        if (togglearSiMismo(btn)) return;
        activeBtn = btn; iniciarLista([trackDeBtn(btn)], btn.dataset.coverUrl, false);
    });

    // ---- Escuchar todo ----
    function sincronizarListenAll() {
        var b = document.getElementById('listen-all-btn'); if (!b) return;
        var p = b.querySelector('.listen-all-play'), s = b.querySelector('.listen-all-stop');
        if (p) p.hidden = modoEscucharTodo; if (s) s.hidden = !modoEscucharTodo;
        b.classList.toggle('activo', modoEscucharTodo);
    }
    (function initListenAll() {
        var boton = document.getElementById('listen-all-btn'); if (!boton) return;
        var cardBtns = document.querySelectorAll('.card-play-btn[data-tracks]'); if (!cardBtns.length) return;
        boton.hidden = false;
        boton.addEventListener('click', function () {
            if (modoEscucharTodo) { cerrar(); return; }
            var lista = [];
            cardBtns.forEach(function (b) {
                var ts; try { ts = JSON.parse(b.dataset.tracks || '[]'); } catch (e) { ts = []; }
                ts.forEach(function (t) { t.cover = b.dataset.coverUrl || ''; lista.push(t); });
            });
            if (!lista.length) return;
            activeBtn = null; iniciarLista(lista, lista[0].cover, true);
        });
    })();
})();
