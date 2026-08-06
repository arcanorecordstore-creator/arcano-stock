/* Reproductor flotante compartido de Arcano.
   Funciona con cualquier boton [data-play="local"|"youtube"] en la pagina:
   - Tracks en la ficha de disco (.public-track-play-btn)
   - Boton de escuchar en las cards del catalogo (.card-play-btn)

   Cada boton debe traer estos data-attributes:
     data-play="local"           data-audio-url="..."
     data-play="youtube"         data-youtube-id="..."  data-youtube-start="0"
     data-cover-url, data-track-title, data-track-artist
*/
(function () {
    'use strict';

    var player = document.getElementById('floating-player');
    if (!player) return;

    var fpCover = document.getElementById('fp-cover');
    var fpTitle = document.getElementById('fp-title');
    var fpArtist = document.getElementById('fp-artist');
    var fpAudio = document.getElementById('fp-audio');
    var fpYtSlot = document.getElementById('fp-youtube-slot');

    // ---- Contador de escuchas (fire & forget) ----
    function registrarPlay(trackId) {
        if (!trackId) return;
        try {
            fetch('/api/track/' + trackId + '/play', { method: 'POST', keepalive: true });
        } catch (e) {}
    }

    // ---- Modo "Escuchar todo" ----
    var modoEscucharTodo = false;
    var autoNextTimer = null;
    function limpiarAutoNext() {
        if (autoNextTimer) { clearTimeout(autoNextTimer); autoNextTimer = null; }
    }
    function sincronizarBotonListenAll() {
        var b = document.getElementById('listen-all-btn');
        if (!b) return;
        b.querySelector('.listen-all-play').hidden = modoEscucharTodo;
        b.querySelector('.listen-all-stop').hidden = !modoEscucharTodo;
        b.classList.toggle('activo', modoEscucharTodo);
    }
    var fpClose = document.getElementById('fp-close');
    var fpNav = document.getElementById('fp-nav');
    var fpPrev = document.getElementById('fp-prev');
    var fpNext = document.getElementById('fp-next');
    var fpPos = document.getElementById('fp-pos');
    var fpVolume = document.getElementById('fp-volume');
    var fpVolumeIcon = document.getElementById('fp-volume-icon');

    // Volumen guardado (0-1), persiste en localStorage
    var volumenActual = 1;
    try {
        var vSaved = localStorage.getItem('arcano-volume');
        if (vSaved !== null) volumenActual = parseFloat(vSaved);
    } catch (e) {}

    var activeBtn = null;

    // Estado de la playlist actual (tracks del disco que se esta escuchando)
    var playlist = [];      // array de objetos track
    var playIndex = 0;      // indice del track actual
    var playCover = '';     // portada del disco actual

    function allPlayButtons() {
        return document.querySelectorAll('[data-play]');
    }

    function resetPlayIcons() {
        allPlayButtons().forEach(function (b) {
            b.classList.remove('is-playing');
            var play = b.querySelector('.play-icon');
            var pause = b.querySelector('.pause-icon');
            if (play) play.hidden = false;
            if (pause) pause.hidden = true;
        });
    }

    function markPlaying(btn) {
        resetPlayIcons();
        btn.classList.add('is-playing');
        var play = btn.querySelector('.play-icon');
        var pause = btn.querySelector('.pause-icon');
        if (play) play.hidden = true;
        if (pause) pause.hidden = false;
        activeBtn = btn;
    }

    function showPlayer(coverUrl, title, artist) {
        if (coverUrl) {
            fpCover.src = coverUrl;
            fpCover.style.display = '';
        } else {
            fpCover.style.display = 'none';
        }
        fpTitle.textContent = title || '';
        fpArtist.textContent = artist || '';
        if (typeof fpCart !== 'undefined' && fpCart) fpCart.hidden = true;
        player.hidden = false;
        setTimeout(function () { player.classList.add('visible'); }, 10);
    }

    function stopAll() {
        try {
            fpAudio.pause();
            fpAudio.removeAttribute('src');
            fpAudio.load();
        } catch (e) {}
        fpAudio.hidden = true;
        fpYtSlot.innerHTML = '';
        fpYtSlot.hidden = true;
        resetPlayIcons();
        activeBtn = null;
    }

    function closePlayer() {
        stopAll();
        playlist = [];
        playIndex = 0;
        if (fpNav) fpNav.hidden = true;
        if (fpPos) fpPos.hidden = true;
        player.classList.remove('visible');
        setTimeout(function () { player.hidden = true; }, 250);
        modoEscucharTodo = false;
        limpiarAutoNext();
        sincronizarBotonListenAll();
    }

    fpClose.addEventListener('click', closePlayer);

    // ---- Boton "Al carrito" dentro del player (solo si el disco esta Disponible) ----
    var fpCart = document.getElementById('fp-cart');
    function actualizarBotonCarrito(discId, disponible) {
        if (!fpCart) return;
        if (!discId || !disponible) { fpCart.hidden = true; return; }
        fpCart.hidden = false;
        fpCart.dataset.discId = String(discId);
        var yaEsta = !!(window.ArcanoCart && window.ArcanoCart.has && window.ArcanoCart.has(discId));
        var add = fpCart.querySelector('.fp-cart-add');
        var inc = fpCart.querySelector('.fp-cart-in');
        if (add) add.hidden = yaEsta;
        if (inc) inc.hidden = !yaEsta;
        fpCart.classList.toggle('is-in-cart', yaEsta);
    }
    if (fpCart) {
        fpCart.addEventListener('click', function () {
            var id = fpCart.dataset.discId;
            if (!id || !window.ArcanoCart || !window.ArcanoCart.add) return;
            window.ArcanoCart.add(id);
            actualizarBotonCarrito(id, true);
            if (window.arcanoToast) window.arcanoToast('✓ Agregado al carrito');
        });
    }

    function playLocal(btn) {
        if (activeBtn === btn && !fpAudio.paused) {
            fpAudio.pause();
            resetPlayIcons();
            return;
        }
        stopAll();
        showPlayer(btn.dataset.coverUrl, btn.dataset.trackTitle, btn.dataset.trackArtist);
        registrarPlay(btn.dataset.trackId);
        fpAudio.hidden = false;
        fpYtSlot.hidden = true;
        fpAudio.src = btn.dataset.audioUrl;
        fpAudio.play().then(function () {
            markPlaying(btn);
        }).catch(function (e) { console.error('Play failed', e); });

        fpAudio.addEventListener('ended', resetPlayIcons, { once: true });
    }

    function playYouTube(btn) {
        if (activeBtn === btn) {
            closePlayer();
            return;
        }
        stopAll();
        showPlayer(btn.dataset.coverUrl, btn.dataset.trackTitle, btn.dataset.trackArtist);
        registrarPlay(btn.dataset.trackId);
        fpAudio.hidden = true;
        fpYtSlot.hidden = false;

        var videoId = btn.dataset.youtubeId;
        var start = parseInt(btn.dataset.youtubeStart || '0', 10);
        var params = 'autoplay=1&rel=0&modestbranding=1&enablejsapi=1';
        if (start > 0) params += '&start=' + start;
        var iframe = document.createElement('iframe');
        iframe.width = '280';
        iframe.height = '157';
        iframe.frameBorder = '0';
        iframe.allow = 'autoplay; encrypted-media';
        iframe.className = 'fp-yt-iframe';
        iframe.src = 'https://www.youtube-nocookie.com/embed/' + videoId + '?' + params;

        // Link de respaldo: si el video esta bloqueado para embeber, el cliente
        // igual tiene un boton claro para abrirlo en YouTube.
        var ytWatch = 'https://www.youtube.com/watch?v=' + videoId;
        if (start > 0) ytWatch += '&t=' + start + 's';
        var fallback = document.createElement('a');
        fallback.href = ytWatch;
        fallback.target = '_blank';
        fallback.rel = 'noopener';
        fallback.className = 'fp-youtube-fallback';
        fallback.innerHTML = 'Si no suena, <span>abrir en YouTube &nearr;</span>';

        fpYtSlot.innerHTML = '';
        fpYtSlot.appendChild(iframe);
        fpYtSlot.appendChild(fallback);
        markPlaying(btn);
    }

    function playSoundCloud(btn) {
        if (activeBtn === btn) {
            closePlayer();
            return;
        }
        stopAll();
        showPlayer(btn.dataset.coverUrl, btn.dataset.trackTitle, btn.dataset.trackArtist);
        registrarPlay(btn.dataset.trackId);
        fpAudio.hidden = true;
        fpYtSlot.hidden = false;

        var scUrl = btn.dataset.soundcloudUrl;
        var embedSrc = 'https://w.soundcloud.com/player/?url=' + encodeURIComponent(scUrl) +
                       '&auto_play=true&hide_related=true&show_comments=false&show_user=true&show_reposts=false&visual=false';
        var iframe = document.createElement('iframe');
        iframe.width = '100%';
        iframe.height = '120';
        iframe.frameBorder = '0';
        iframe.allow = 'autoplay';
        iframe.scrolling = 'no';
        iframe.className = 'fp-sc-iframe';
        iframe.src = embedSrc;

        var fallback = document.createElement('a');
        fallback.href = scUrl;
        fallback.target = '_blank';
        fallback.rel = 'noopener';
        fallback.className = 'fp-youtube-fallback';
        fallback.innerHTML = 'Si no suena, <span>abrir en SoundCloud &nearr;</span>';

        fpYtSlot.innerHTML = '';
        fpYtSlot.appendChild(iframe);
        fpYtSlot.appendChild(fallback);
        markPlaying(btn);
    }

    // ---- Playlist: reproducir un track por indice ----
    function actualizarNav() {
        if (playlist.length > 1) {
            fpNav.hidden = false;
            fpPos.hidden = false;
            fpPos.textContent = 'Track ' + (playIndex + 1) + ' de ' + playlist.length;
            fpPrev.disabled = (playIndex === 0);
            fpNext.disabled = (playIndex === playlist.length - 1);
        } else {
            fpNav.hidden = true;
            fpPos.hidden = true;
        }
    }

    function reproducirTrackActual() {
        var t = playlist[playIndex];
        if (!t) return;
        // En la ficha de disco, resaltar el boton del track que suena ahora
        if (t.btn) markPlaying(t.btn);
        stopAudioYt();
        showPlayer(t.cover || playCover, t.title, t.artist);
        actualizarBotonCarrito(t.discId, t.disponible);
        actualizarNav();
        registrarPlay(t.id);
        // En modo escuchar-todo, los samples de YouTube/SoundCloud no avisan
        // cuando terminan (iframe), asi que avanzamos solos a los 60s.
        if (modoEscucharTodo && t.kind !== 'local') {
            autoNextTimer = setTimeout(function () {
                if (playIndex < playlist.length - 1) { playIndex++; reproducirTrackActual(); }
                else { closePlayer(); }
            }, 60000);
        }

        if (t.kind === 'local') {
            fpAudio.hidden = false;
            fpYtSlot.hidden = true;
            fpAudio.src = t.audioUrl;
            fpAudio.volume = volumenActual;
            fpAudio.play().catch(function (e) { console.error('Play failed', e); });
            fpAudio.onended = function () {
                if (playIndex < playlist.length - 1) { playIndex++; reproducirTrackActual(); }
                else if (modoEscucharTodo) { closePlayer(); }
            };
        } else if (t.kind === 'youtube') {
            montarYouTube(t.youtubeId, t.youtubeStart);
            setTimeout(function () { aplicarVolumen(volumenActual); }, 1200);
        } else if (t.kind === 'soundcloud') {
            montarSoundCloud(t.soundcloudUrl);
            setTimeout(function () { aplicarVolumen(volumenActual); }, 1200);
        }
    }

    function stopAudioYt() {
        limpiarAutoNext();
        try { fpAudio.pause(); fpAudio.removeAttribute('src'); fpAudio.onended = null; fpAudio.load(); } catch (e) {}
        fpAudio.hidden = true;
        fpYtSlot.innerHTML = '';
        fpYtSlot.hidden = true;
    }

    function montarYouTube(videoId, start) {
        fpAudio.hidden = true;
        fpYtSlot.hidden = false;
        start = parseInt(start || '0', 10);
        var params = 'autoplay=1&rel=0&modestbranding=1&enablejsapi=1';
        if (start > 0) params += '&start=' + start;
        var iframe = document.createElement('iframe');
        iframe.width = '280'; iframe.height = '157'; iframe.frameBorder = '0';
        iframe.allow = 'autoplay; encrypted-media'; iframe.className = 'fp-yt-iframe';
        iframe.src = 'https://www.youtube-nocookie.com/embed/' + videoId + '?' + params;
        var ytWatch = 'https://www.youtube.com/watch?v=' + videoId + (start > 0 ? '&t=' + start + 's' : '');
        var fallback = document.createElement('a');
        fallback.href = ytWatch; fallback.target = '_blank'; fallback.rel = 'noopener';
        fallback.className = 'fp-youtube-fallback';
        fallback.innerHTML = 'Si no suena, <span>abrir en YouTube &nearr;</span>';
        fpYtSlot.innerHTML = '';
        fpYtSlot.appendChild(iframe);
        fpYtSlot.appendChild(fallback);
    }

    function montarSoundCloud(scUrl) {
        fpAudio.hidden = true;
        fpYtSlot.hidden = false;
        var embedSrc = 'https://w.soundcloud.com/player/?url=' + encodeURIComponent(scUrl) +
                       '&auto_play=true&hide_related=true&show_comments=false&show_user=true&show_reposts=false&visual=false';
        var iframe = document.createElement('iframe');
        iframe.width = '100%'; iframe.height = '120'; iframe.frameBorder = '0';
        iframe.allow = 'autoplay'; iframe.scrolling = 'no'; iframe.className = 'fp-sc-iframe';
        iframe.src = embedSrc;
        var fallback = document.createElement('a');
        fallback.href = scUrl; fallback.target = '_blank'; fallback.rel = 'noopener';
        fallback.className = 'fp-youtube-fallback';
        fallback.innerHTML = 'Si no suena, <span>abrir en SoundCloud &nearr;</span>';
        fpYtSlot.innerHTML = '';
        fpYtSlot.appendChild(iframe);
        fpYtSlot.appendChild(fallback);
    }

    // Navegacion con flechas
    if (fpPrev) fpPrev.addEventListener('click', function () {
        if (playIndex > 0) { playIndex--; reproducirTrackActual(); }
    });
    if (fpNext) fpNext.addEventListener('click', function () {
        if (playIndex < playlist.length - 1) { playIndex++; reproducirTrackActual(); }
    });

    // ---- Control de volumen (afecta audio local, YouTube y SoundCloud) ----
    function aplicarVolumen(v) {
        volumenActual = v;
        try { localStorage.setItem('arcano-volume', String(v)); } catch (e) {}
        // Icono segun nivel
        if (fpVolumeIcon) {
            if (v === 0) fpVolumeIcon.innerHTML = '&#128263;';       // mute
            else if (v < 0.5) fpVolumeIcon.innerHTML = '&#128265;';  // bajo
            else fpVolumeIcon.innerHTML = '&#128266;';               // alto
        }
        // Audio local
        try { fpAudio.volume = v; } catch (e) {}
        // YouTube (postMessage a la iframe API)
        var ytIframe = fpYtSlot.querySelector('.fp-yt-iframe');
        if (ytIframe && ytIframe.contentWindow) {
            ytIframe.contentWindow.postMessage(JSON.stringify({
                event: 'command', func: 'setVolume', args: [Math.round(v * 100)]
            }), '*');
        }
        // SoundCloud (Widget API)
        var scIframe = fpYtSlot.querySelector('.fp-sc-iframe');
        if (scIframe && scIframe.contentWindow) {
            scIframe.contentWindow.postMessage(JSON.stringify({
                method: 'setVolume', value: Math.round(v * 100)
            }), '*');
        }
    }

    if (fpVolume) {
        fpVolume.value = Math.round(volumenActual * 100);
        aplicarVolumen(volumenActual);
        fpVolume.addEventListener('input', function () {
            aplicarVolumen(parseInt(fpVolume.value, 10) / 100);
        });
    }
    // Click en el icono = mute/unmute
    if (fpVolumeIcon) {
        fpVolumeIcon.style.cursor = 'pointer';
        var volAntesMute = volumenActual;
        fpVolumeIcon.addEventListener('click', function () {
            if (volumenActual > 0) {
                volAntesMute = volumenActual;
                aplicarVolumen(0);
                if (fpVolume) fpVolume.value = 0;
            } else {
                aplicarVolumen(volAntesMute || 1);
                if (fpVolume) fpVolume.value = Math.round((volAntesMute || 1) * 100);
            }
        });
    }

    // Iniciar playlist desde una card (con lista de tracks)
    function iniciarPlaylist(btn) {
        var tracks;
        try { tracks = JSON.parse(btn.dataset.tracks || '[]'); } catch (e) { tracks = []; }
        if (!tracks.length) return false;
        // Si es el mismo boton que ya suena, togglear cierre
        if (activeBtn === btn) { closePlayer(); return true; }
        playlist = tracks;
        playIndex = 0;
        playCover = btn.dataset.coverUrl || '';
        markPlaying(btn);
        reproducirTrackActual();
        return true;
    }

    // Ficha de disco: los botones de cada track (.public-track-play-btn) forman
    // una playlist, para que las flechas prev/siguiente recorran los tracks.
    function trackDesdeBtnTrack(btn) {
        var kind = btn.dataset.play;
        var t = {
            id: btn.dataset.trackId, kind: kind, btn: btn,
            cover: btn.dataset.coverUrl, title: btn.dataset.trackTitle, artist: btn.dataset.trackArtist
        };
        if (kind === 'local') t.audioUrl = btn.dataset.audioUrl;
        else if (kind === 'youtube') { t.youtubeId = btn.dataset.youtubeId; t.youtubeStart = btn.dataset.youtubeStart; }
        else if (kind === 'soundcloud') t.soundcloudUrl = btn.dataset.soundcloudUrl;
        return t;
    }
    function iniciarTracklist(btn) {
        var btns = Array.prototype.slice.call(document.querySelectorAll('.public-track-play-btn'));
        if (btns.length <= 1) return false;   // un solo track: comportamiento clasico
        if (activeBtn === btn) { closePlayer(); return true; }
        playlist = btns.map(trackDesdeBtnTrack);
        playIndex = btns.indexOf(btn);
        playCover = btn.dataset.coverUrl || '';
        reproducirTrackActual();
        return true;
    }

    // Delegacion: captura clicks en cualquier [data-play], presente o futuro
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-play]');
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();
        // Si el boton trae una lista de tracks (card del catalogo), usar playlist
        if (btn.dataset.tracks) {
            iniciarPlaylist(btn);
            return;
        }
        // Ficha de disco: recorrer los tracks del disco con prev/siguiente
        if (btn.classList.contains('public-track-play-btn') && iniciarTracklist(btn)) {
            return;
        }
        // Sino, comportamiento clasico (un solo track)
        var kind = btn.dataset.play;
        if (kind === 'local') playLocal(btn);
        else if (kind === 'youtube') playYouTube(btn);
        else if (kind === 'soundcloud') playSoundCloud(btn);
    });

    // ---- Boton "Escuchar todo": recorre los samples del catalogo en secuencia ----
    (function initEscucharTodo() {
        var boton = document.getElementById('listen-all-btn');
        if (!boton) return;
        var cardBtns = document.querySelectorAll('.card-play-btn[data-tracks]');
        if (!cardBtns.length) return;
        boton.hidden = false;

        boton.addEventListener('click', function () {
            if (modoEscucharTodo) {
                closePlayer();
                return;
            }
            // Armar la super-playlist con todos los tracks de la pagina, en orden
            var lista = [];
            cardBtns.forEach(function (b) {
                var ts;
                try { ts = JSON.parse(b.dataset.tracks || '[]'); } catch (e) { ts = []; }
                ts.forEach(function (t) {
                    t.cover = b.dataset.coverUrl || '';
                    lista.push(t);
                });
            });
            if (!lista.length) return;
            modoEscucharTodo = true;
            playlist = lista;
            playIndex = 0;
            playCover = lista[0].cover || '';
            sincronizarBotonListenAll();
            reproducirTrackActual();
        });
    })();
})();
