/* Editor de tracks para la pantalla de ALTA (el disco todavia no existe).
   Trabaja en memoria: parsea el tracklist en texto, deja buscar el audio de
   cada track, y serializa todo a un input hidden (tracks_json) que viaja al
   guardar el disco.
*/
(function () {
    'use strict';

    var editor = document.getElementById('tracks-new-editor');
    if (!editor) return;

    var listEl = document.getElementById('tracks-new-list');
    var jsonInput = document.getElementById('tracks-new-json');
    var statusEl = document.getElementById('tracks-new-status');
    var loadBtn = document.getElementById('tracks-new-load');
    var searchAllBtn = document.getElementById('tracks-new-search-all');
    var tracklistTextarea = document.getElementById('tracklist');
    var artistaInput = document.querySelector('[name="artista"]');

    // Estado en memoria: array de {lado, titulo, artista, youtube_url}
    var tracks = [];

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function artistaDisco() {
        return artistaInput ? artistaInput.value.trim() : '';
    }

    function syncJson() {
        jsonInput.value = JSON.stringify(tracks);
    }

    function setStatus(msg, tipo) {
        if (!statusEl) return;
        statusEl.textContent = msg || '';
        statusEl.className = 'tracks-search-all-status' + (tipo ? ' ' + tipo : '');
    }

    function plataformaDeUrl(url) {
        if (!url) return null;
        var u = url.toLowerCase();
        if (u.indexOf('soundcloud.com') !== -1) return 'soundcloud';
        if (u.indexOf('youtu') !== -1) return 'youtube';
        return 'otro';
    }

    function render() {
        if (!tracks.length) {
            listEl.innerHTML = '<div class="tracks-empty">Escribi o scrapea el tracklist arriba y toca "Cargar tracks del tracklist".</div>';
            syncJson();
            return;
        }
        var html = '';
        tracks.forEach(function (t, i) {
            var plat = plataformaDeUrl(t.youtube_url);
            var estado = '';
            if (t.youtube_url) {
                var badge = plat === 'soundcloud' ? '<span class="track-src-badge sc">SC</span>'
                    : (plat === 'youtube' ? '<span class="track-src-badge yt">YT</span>' : '');
                estado = '<span class="tnew-has-audio">' + badge + ' audio cargado</span>';
            } else {
                estado = '<span class="tnew-no-audio">sin audio</span>';
            }
            html +=
                '<div class="tnew-row" data-idx="' + i + '">' +
                    '<div class="tnew-row-main">' +
                        '<span class="tnew-lado">' + esc(t.lado || ('#' + (i + 1))) + '</span>' +
                        '<input type="text" class="track-input tnew-titulo" data-field="titulo" value="' + esc(t.titulo) + '" placeholder="Titulo del track">' +
                        '<input type="text" class="track-input tnew-artista" data-field="artista" value="' + esc(t.artista || '') + '" placeholder="Artista (opcional)">' +
                        estado +
                        '<button type="button" class="btn btn-ghost btn-sm" data-action="search">&#128269; Buscar</button>' +
                    '</div>' +
                    '<div class="tnew-url-row">' +
                        '<input type="text" class="track-input tnew-url" data-field="youtube_url" value="' + esc(t.youtube_url || '') + '" placeholder="URL de YouTube o SoundCloud (o busca automatico)">' +
                        (t.youtube_url ? '<button type="button" class="btn btn-ghost btn-sm danger" data-action="clear">Quitar</button>' : '') +
                    '</div>' +
                    '<div class="track-search-results" data-results hidden></div>' +
                '</div>';
        });
        listEl.innerHTML = html;
        bindRows();
        syncJson();
    }

    function bindRows() {
        listEl.querySelectorAll('.tnew-row').forEach(function (row) {
            var idx = parseInt(row.dataset.idx, 10);

            // Editar campos en vivo
            row.querySelectorAll('[data-field]').forEach(function (inp) {
                inp.addEventListener('input', function () {
                    tracks[idx][inp.dataset.field] = inp.value;
                    syncJson();
                });
            });

            // Quitar audio
            var clearBtn = row.querySelector('[data-action="clear"]');
            if (clearBtn) clearBtn.addEventListener('click', function () {
                tracks[idx].youtube_url = '';
                render();
            });

            // Buscar (individual)
            var searchBtn = row.querySelector('[data-action="search"]');
            var resultsEl = row.querySelector('[data-results]');
            if (searchBtn && resultsEl) {
                searchBtn.addEventListener('click', function () {
                    buscarUno(idx, resultsEl, searchBtn);
                });
            }
        });
    }

    function buscarUno(idx, resultsEl, btn, onDone) {
        var t = tracks[idx];
        var titulo = (t.titulo || '').trim();
        if (!titulo) {
            if (resultsEl) { resultsEl.hidden = false; resultsEl.innerHTML = '<div class="track-search-msg">Falta el titulo.</div>'; }
            if (onDone) onDone(false);
            return;
        }
        var artista = (t.artista && t.artista.trim()) ? t.artista.trim() : artistaDisco();
        if (resultsEl) { resultsEl.hidden = false; resultsEl.innerHTML = '<div class="track-search-msg">Buscando...</div>'; }
        if (btn) btn.disabled = true;

        fetch('/admin/track-search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ artista: artista, titulo: titulo })
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (btn) btn.disabled = false;
            if (resultsEl) renderResults(resultsEl, idx, data);
            if (onDone) onDone(data && data.ok && data.candidatos && data.candidatos.length);
        })
        .catch(function () {
            if (btn) btn.disabled = false;
            if (resultsEl) resultsEl.innerHTML = '<div class="track-search-msg error">Error al buscar.</div>';
            if (onDone) onDone(false);
        });
    }

    function renderResults(container, idx, data) {
        if (!data || !data.candidatos || !data.candidatos.length) {
            container.innerHTML = '<div class="track-search-msg error">Sin resultados. Pega el link a mano.</div>';
            return;
        }
        var html = '<div class="track-search-head">Elegi un resultado:</div>';
        data.candidatos.forEach(function (c) {
            var badge = c.plataforma === 'soundcloud' ? '<span class="track-src-badge sc">SC</span>' : '<span class="track-src-badge yt">YT</span>';
            var dur = c.duracion ? '<span class="track-res-dur">' + esc(c.duracion) + '</span>' : '';
            html += '<button type="button" class="track-search-result" data-url="' + esc(c.url) + '">' +
                badge + '<span class="track-res-info"><span class="track-res-title">' + esc(c.titulo) + '</span>' +
                '<span class="track-res-chan">' + esc(c.canal || '') + '</span></span>' + dur +
                '<span class="track-res-use">Usar</span></button>';
        });
        html += '<button type="button" class="track-search-close" data-close>Cerrar</button>';
        container.innerHTML = html;

        container.querySelectorAll('.track-search-result').forEach(function (b) {
            b.addEventListener('click', function () {
                tracks[idx].youtube_url = b.dataset.url;
                render();
            });
        });
        var cl = container.querySelector('[data-close]');
        if (cl) cl.addEventListener('click', function () { container.hidden = true; container.innerHTML = ''; });
    }

    // Cargar tracks desde el textarea (via endpoint de parseo del backend)
    function cargarDesdeTexto() {
        var texto = tracklistTextarea ? tracklistTextarea.value.trim() : '';
        if (!texto) {
            setStatus('El tracklist de arriba esta vacio.', 'error');
            return;
        }
        // Preservar audios ya cargados si los titulos coinciden
        var previos = {};
        tracks.forEach(function (t) { if (t.youtube_url) previos[(t.titulo || '').toLowerCase()] = t.youtube_url; });

        setStatus('Cargando...');
        fetch('/admin/parse-tracklist', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ texto: texto })
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            setStatus('');
            tracks = (data.tracks || []).map(function (it) {
                var key = (it.titulo || '').toLowerCase();
                return {
                    lado: it.lado || '',
                    titulo: it.titulo || '',
                    artista: it.artista || '',
                    youtube_url: previos[key] || ''
                };
            });
            render();
            if (!tracks.length) setStatus('No se pudo parsear el tracklist.', 'error');
        })
        .catch(function () { setStatus('Error al cargar los tracks.', 'error'); });
    }

    if (loadBtn) loadBtn.addEventListener('click', cargarDesdeTexto);

    // Buscar audios de TODOS (secuencial)
    if (searchAllBtn) {
        searchAllBtn.addEventListener('click', function () {
            if (!tracks.length) {
                setStatus('Primero carga los tracks del tracklist.', 'error');
                return;
            }
            var pendientes = [];
            tracks.forEach(function (t, i) {
                if (!t.youtube_url && t.titulo && t.titulo.trim()) pendientes.push(i);
            });
            if (!pendientes.length) {
                setStatus('Todos los tracks ya tienen audio.', 'ok');
                return;
            }
            searchAllBtn.disabled = true;
            var encontrados = 0, fallidos = 0;

            function paso(k) {
                if (k >= pendientes.length) {
                    searchAllBtn.disabled = false;
                    setStatus('Listo: ' + encontrados + ' cargado(s), ' + fallidos + ' sin resultado.', 'ok');
                    render();
                    return;
                }
                var idx = pendientes[k];
                setStatus('Buscando ' + (k + 1) + ' de ' + pendientes.length + ': ' + tracks[idx].titulo + '...');
                var t = tracks[idx];
                var artista = (t.artista && t.artista.trim()) ? t.artista.trim() : artistaDisco();
                fetch('/admin/track-search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                    body: JSON.stringify({ artista: artista, titulo: t.titulo })
                })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (data && data.ok && data.candidatos && data.candidatos.length) {
                        tracks[idx].youtube_url = data.candidatos[0].url;
                        encontrados++;
                    } else { fallidos++; }
                    syncJson();
                    paso(k + 1);
                })
                .catch(function () { fallidos++; paso(k + 1); });
            }
            paso(0);
        });
    }

    // Auto-cargar si ya viene un tracklist scrapeado al abrir la pantalla
    if (tracklistTextarea && tracklistTextarea.value.trim()) {
        cargarDesdeTexto();
    }
})();
