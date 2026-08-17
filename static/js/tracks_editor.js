/* Editor de tracks estructurados en la ficha admin de un disco.
   Requiere que el DOM tenga #tracks-editor con data-disc-id="N".
   Usa fetch para hablar con /admin/discs/N/tracks y /admin/tracks/N/...
*/
(function () {
    'use strict';

    var editor = document.getElementById('tracks-editor');
    if (!editor) return;

    var discId = editor.dataset.discId;
    var listEl = document.getElementById('tracks-list');
    var addBtn = document.getElementById('tracks-add-btn');
    var parseBtn = document.getElementById('tracks-parse-btn');
    var searchAllBtn = document.getElementById('tracks-search-all-btn');
    var searchAllStatus = document.getElementById('tracks-search-all-status');
    var tracklistTextarea = document.getElementById('tracklist');
    var tracksActuales = [];

    function url(path) {
        return '/admin' + path;
    }

    function loadTracks() {
        fetch(url('/discs/' + discId + '/tracks'), {
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.ok) render(data.tracks);
        })
        .catch(function (e) {
            listEl.innerHTML = '<div class="tracks-empty">Error de red: ' + e + '</div>';
        });
    }

    function render(tracks) {
        tracksActuales = tracks || [];
        if (!tracks || tracks.length === 0) {
            listEl.innerHTML = '<div class="tracks-empty">Sin tracks. Click en "Agregar track" o "Parsear desde texto" para empezar.</div>';
            return;
        }
        var html = '';
        tracks.forEach(function (t) {
            html += renderRow(t);
        });
        listEl.innerHTML = html;
        bindRowEvents();
        initSortable();
    }

    var sortableInstance = null;
    function initSortable() {
        if (typeof Sortable === 'undefined') return;
        if (sortableInstance) {
            sortableInstance.destroy();
        }
        sortableInstance = Sortable.create(listEl, {
            handle: '.track-drag-handle',
            animation: 200,
            ghostClass: 'track-row-ghost',
            chosenClass: 'track-row-chosen',
            dragClass: 'track-row-drag',
            onEnd: function () {
                var rows = listEl.querySelectorAll('.track-row');
                var ids = Array.from(rows).map(function (r) { return r.dataset.trackId; });
                var fd = new FormData();
                fd.append('ids', ids.join(','));
                fetch(url('/discs/' + discId + '/tracks/reorder'), {
                    method: 'POST',
                    headers: { 'X-Requested-With': 'XMLHttpRequest' },
                    body: fd
                });
            }
        });
    }

    function renderRow(t) {
        var kind = t.sample_kind;
        var kindBadge = '';
        if (kind === 'local') kindBadge = '<span class="track-badge track-badge-mp3">MP3</span>';
        else if (kind === 'youtube') kindBadge = '<span class="track-badge track-badge-yt">YT</span>';
        else kindBadge = '<span class="track-badge track-badge-none">sin sample</span>';

        return (
            '<div class="track-row" data-track-id="' + t.id + '">' +
                '<div class="track-row-head">' +
                    '<span class="track-drag-handle" title="Arrastra para reordenar">&#8942;&#8942;</span>' +
                    '<input type="text" class="track-input track-input-lado" data-field="lado" placeholder="A1" value="' + esc(t.lado || '') + '" maxlength="4" title="Lado (A1, A2, B1...)">' +
                    '<input type="text" class="track-input track-input-titulo" data-field="titulo" placeholder="Titulo del track" value="' + esc(t.titulo || '') + '">' +
                    '<input type="text" class="track-input track-input-artista" data-field="artista" placeholder="Artista (opcional)" value="' + esc(t.artista || '') + '">' +
                    '<input type="text" class="track-input track-input-dur" data-field="duracion" placeholder="mm:ss" value="' + esc(t.duracion || '') + '" maxlength="8" title="Duracion mm:ss (opcional)">' +
                    kindBadge +
                    '<button type="button" class="track-btn-del" data-action="delete" title="Eliminar">&times;</button>' +
                '</div>' +
                '<div class="track-row-sample">' +
                    '<label class="track-sample-label">URL del track</label>' +
                    '<input type="text" class="track-input track-input-yt" data-field="youtube_url" placeholder="YouTube o SoundCloud (o busca automatico)" value="' + esc(t.youtube_url || '') + '">' +
                    '<label class="track-sample-label">Empieza en (seg)</label>' +
                    '<input type="number" class="track-input track-input-start" data-field="youtube_start" placeholder="0" value="' + (t.youtube_start !== null && t.youtube_start !== undefined ? t.youtube_start : '') + '" min="0" style="width:80px;">' +
                    '<button type="button" class="btn btn-ghost btn-sm" data-action="search-track" title="Buscar en YouTube y SoundCloud">&#128269; Buscar</button>' +
                    '<button type="button" class="btn btn-ghost btn-sm" data-action="upload-mp3" title="Subi un MP3, o un video (mp4/mov) de los que subis a Instagram: suena igual">Subir audio/video</button>' +
                    (t.sample_local ? '<button type="button" class="btn btn-ghost btn-sm danger" data-action="clear-mp3">Borrar archivo</button>' : '') +
                    '<span class="track-sample-status"></span>' +
                    '<input type="file" hidden data-file-input accept="audio/*,video/mp4,video/quicktime,video/webm">' +
                '</div>' +
                '<div class="track-search-results" data-search-results hidden></div>' +
                ((t.youtube_url && !t.sample_local && String(t.youtube_url).toLowerCase().indexOf('soundcloud.com') === -1)
                    ? '<div class="track-yt-warn">&#9432; Algunos videos de YouTube (sobre todo de sellos) no se pueden reproducir dentro de la web. Para asegurar la escucha, subi un audio o video propio, o usa SoundCloud.</div>'
                    : '') +
            '</div>'
        );
    }

    function esc(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function bindRowEvents() {
        listEl.querySelectorAll('.track-row').forEach(function (row) {
            var trackId = row.dataset.trackId;

            // Autosave en cambio de input
            row.querySelectorAll('.track-input').forEach(function (inp) {
                var timer = null;
                inp.addEventListener('input', function () {
                    clearTimeout(timer);
                    timer = setTimeout(function () {
                        saveField(trackId, inp.dataset.field, inp.value, inp);
                    }, 500);
                });
                inp.addEventListener('blur', function () {
                    clearTimeout(timer);
                    saveField(trackId, inp.dataset.field, inp.value, inp);
                });
            });

            // Eliminar
            var delBtn = row.querySelector('[data-action="delete"]');
            if (delBtn) {
                delBtn.addEventListener('click', function () {
                    if (!confirm('Eliminar este track?')) return;
                    fetch(url('/tracks/' + trackId + '/delete'), {
                        method: 'POST',
                        headers: { 'X-Requested-With': 'XMLHttpRequest' }
                    })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (data.ok) loadTracks();
                    });
                });
            }

            // Subir MP3
            var uploadBtn = row.querySelector('[data-action="upload-mp3"]');
            var fileInput = row.querySelector('[data-file-input]');
            var statusEl = row.querySelector('.track-sample-status');
            if (uploadBtn && fileInput) {
                uploadBtn.addEventListener('click', function () { fileInput.click(); });
                fileInput.addEventListener('change', function () {
                    if (!fileInput.files || !fileInput.files[0]) return;
                    var fd = new FormData();
                    fd.append('sample', fileInput.files[0]);
                    statusEl.textContent = 'Subiendo...';
                    fetch(url('/tracks/' + trackId + '/upload-sample'), {
                        method: 'POST',
                        headers: { 'X-Requested-With': 'XMLHttpRequest' },
                        body: fd
                    })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (data.ok) {
                            statusEl.textContent = 'Subido!';
                            statusEl.style.color = 'var(--gain)';
                            setTimeout(loadTracks, 400);
                        } else {
                            statusEl.textContent = 'Error: ' + (data.error || 'desconocido');
                            statusEl.style.color = 'var(--loss)';
                        }
                    });
                });
            }

            // Borrar MP3
            var clearBtn = row.querySelector('[data-action="clear-mp3"]');
            if (clearBtn) {
                clearBtn.addEventListener('click', function () {
                    if (!confirm('Borrar el MP3 subido?')) return;
                    fetch(url('/tracks/' + trackId + '/clear-sample'), {
                        method: 'POST',
                        headers: { 'X-Requested-With': 'XMLHttpRequest' }
                    })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (data.ok) loadTracks();
                    });
                });
            }

            // Buscar en YouTube + SoundCloud
            var searchBtn = row.querySelector('[data-action="search-track"]');
            var resultsEl = row.querySelector('[data-search-results]');
            if (searchBtn && resultsEl) {
                searchBtn.addEventListener('click', function () {
                    var tituloInp = row.querySelector('[data-field="titulo"]');
                    var artistaInp = row.querySelector('[data-field="artista"]');
                    var urlInp = row.querySelector('[data-field="youtube_url"]');
                    var titulo = tituloInp ? tituloInp.value.trim() : '';
                    var artista = artistaInp ? artistaInp.value.trim() : '';
                    // Si el track no tiene artista propio, usar el del disco
                    if (!artista) artista = editor.dataset.discArtista || '';

                    if (!titulo) {
                        resultsEl.hidden = false;
                        resultsEl.innerHTML = '<div class="track-search-msg">Escribi primero el titulo del track.</div>';
                        return;
                    }

                    resultsEl.hidden = false;
                    resultsEl.innerHTML = '<div class="track-search-msg">Buscando en YouTube y SoundCloud...</div>';
                    searchBtn.disabled = true;

                    fetch(url('/track-search'), {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-Requested-With': 'XMLHttpRequest'
                        },
                        body: JSON.stringify({ artista: artista, titulo: titulo })
                    })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        searchBtn.disabled = false;
                        renderSearchResults(resultsEl, urlInp, trackId, data);
                    })
                    .catch(function () {
                        searchBtn.disabled = false;
                        resultsEl.innerHTML = '<div class="track-search-msg error">Error al buscar. Proba de nuevo.</div>';
                    });
                });
            }
        });
    }

    function renderSearchResults(container, urlInp, trackId, data) {
        if (!data || (!data.ok && (!data.candidatos || !data.candidatos.length))) {
            var msg = 'No se encontraron resultados.';
            if (data && data.error) msg = data.error;
            else if (data && data.errores && data.errores.length) {
                msg += ' (las plataformas no respondieron; proba de nuevo o pega el link a mano)';
            }
            container.innerHTML = '<div class="track-search-msg error">' + esc(msg) + '</div>';
            return;
        }

        var html = '<div class="track-search-head">Elegi un resultado (o cerra y pega el link a mano):</div>';
        data.candidatos.forEach(function (c, i) {
            var badge = c.plataforma === 'soundcloud'
                ? '<span class="track-src-badge sc">SC</span>'
                : '<span class="track-src-badge yt">YT</span>';
            var dur = c.duracion ? '<span class="track-res-dur">' + esc(c.duracion) + '</span>' : '';
            html +=
                '<button type="button" class="track-search-result" data-url="' + esc(c.url) + '">' +
                    badge +
                    '<span class="track-res-info">' +
                        '<span class="track-res-title">' + esc(c.titulo) + '</span>' +
                        '<span class="track-res-chan">' + esc(c.canal || '') + '</span>' +
                    '</span>' +
                    dur +
                    '<span class="track-res-use">Usar</span>' +
                '</button>';
        });
        html += '<button type="button" class="track-search-close" data-search-close>Cerrar</button>';
        container.innerHTML = html;

        // Bind: al elegir un resultado, rellenar el input y guardar
        container.querySelectorAll('.track-search-result').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var chosenUrl = btn.dataset.url;
                if (urlInp) urlInp.value = chosenUrl;
                saveField(trackId, 'youtube_url', chosenUrl);
                container.innerHTML = '<div class="track-search-msg ok">Link cargado. Guardando...</div>';
                setTimeout(loadTracks, 500);
            });
        });
        var closeBtn = container.querySelector('[data-search-close]');
        if (closeBtn) closeBtn.addEventListener('click', function () {
            container.hidden = true;
            container.innerHTML = '';
        });
    }

    function markSave(inp, ok) {
        if (!inp) {
            if (!ok) alert('No se pudo guardar el cambio del track. Revisa la conexion e intenta de nuevo.');
            return;
        }
        inp.style.transition = 'border-color .2s';
        inp.style.borderColor = ok ? '#10b981' : '#ef4444';
        if (ok) { inp.removeAttribute('title'); }
        else { inp.title = 'No se guardo. Revisa la conexion e intenta de nuevo.'; }
        setTimeout(function () { inp.style.borderColor = ''; }, ok ? 900 : 2500);
    }

    function saveField(trackId, field, value, inp) {
        var fd = new FormData();
        fd.append(field, value);
        fetch(url('/tracks/' + trackId), {
            method: 'POST',
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
            body: fd
        }).then(function (r) {
            if (!r.ok) throw new Error('status ' + r.status);
            markSave(inp, true);
        }).catch(function () {
            markSave(inp, false);
        });
    }

    // Agregar track
    if (addBtn) {
        addBtn.addEventListener('click', function () {
            fetch(url('/discs/' + discId + '/tracks/add'), {
                method: 'POST',
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.ok) loadTracks();
            });
        });
    }

    // Parsear texto arriba
    if (parseBtn) {
        parseBtn.addEventListener('click', function () {
            if (!tracklistTextarea) return;
            var texto = tracklistTextarea.value.trim();
            if (!texto) { alert('El textarea Tracklist esta vacio.'); return; }
            var modo = 'reemplazar';
            if (listEl.querySelector('.track-row')) {
                if (!confirm('Reemplazar los tracks actuales con lo parseado del texto?')) return;
            }
            var fd = new FormData();
            fd.append('texto', texto);
            fd.append('modo', modo);
            fetch(url('/discs/' + discId + '/tracks/parse'), {
                method: 'POST',
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                body: fd
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.ok) loadTracks();
                else alert('Error: ' + (data.error || 'desconocido'));
            });
        });
    }

    // Buscar samples de TODOS los tracks (batch, secuencial)
    if (searchAllBtn) {
        searchAllBtn.addEventListener('click', function () {
            // Tracks sin sample todavia
            var pendientes = tracksActuales.filter(function (t) {
                return !t.youtube_url && !t.sample_local && t.titulo && t.titulo.trim();
            });
            if (!tracksActuales.length) {
                setSearchAllStatus('No hay tracks cargados.', 'error');
                return;
            }
            if (!pendientes.length) {
                setSearchAllStatus('Todos los tracks ya tienen sample.', 'ok');
                return;
            }
            if (!confirm('Buscar samples para ' + pendientes.length + ' track(s)? Se va a cargar el mejor resultado de cada uno (priorizando SoundCloud). Despues podes revisar y cambiar.')) return;

            searchAllBtn.disabled = true;
            var artistaDisco = editor.dataset.discArtista || '';
            var encontrados = 0;
            var sinResultado = 0;

            function procesar(i) {
                if (i >= pendientes.length) {
                    searchAllBtn.disabled = false;
                    setSearchAllStatus('Listo: ' + encontrados + ' cargado(s), ' + sinResultado + ' sin resultado. Revisa abajo.', 'ok');
                    loadTracks();
                    return;
                }
                var t = pendientes[i];
                var artista = (t.artista && t.artista.trim()) ? t.artista.trim() : artistaDisco;
                setSearchAllStatus('Buscando ' + (i + 1) + ' de ' + pendientes.length + ': ' + t.titulo + '...', '');

                fetch(url('/track-search'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                    body: JSON.stringify({ artista: artista, titulo: t.titulo })
                })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (data && data.ok && data.candidatos && data.candidatos.length) {
                        // El primero ya viene priorizado (SoundCloud antes que YouTube)
                        var mejor = data.candidatos[0];
                        var fd = new FormData();
                        fd.append('youtube_url', mejor.url);
                        return fetch(url('/tracks/' + t.id), {
                            method: 'POST',
                            headers: { 'X-Requested-With': 'XMLHttpRequest' },
                            body: fd
                        }).then(function () { encontrados++; procesar(i + 1); });
                    } else {
                        sinResultado++;
                        procesar(i + 1);
                    }
                })
                .catch(function () {
                    sinResultado++;
                    procesar(i + 1);
                });
            }
            procesar(0);
        });
    }

    function setSearchAllStatus(msg, tipo) {
        if (!searchAllStatus) return;
        searchAllStatus.textContent = msg || '';
        searchAllStatus.className = 'tracks-search-all-status' + (tipo ? ' ' + tipo : '');
    }

    loadTracks();
})();
