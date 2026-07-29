/* Gestor de galeria de fotos de un disco (admin).
   Sube, lista, borra y reordena fotos via las rutas /admin/discs/N/images*.
*/
(function () {
    'use strict';

    var editor = document.getElementById('gallery-editor');
    if (!editor) return;

    var discId = editor.dataset.discId;
    var grid = document.getElementById('gallery-grid');
    var loading = document.getElementById('gallery-loading');
    var uploadInput = document.getElementById('gallery-upload-input');
    var uploadBtn = document.getElementById('gallery-upload-btn');
    var statusEl = document.getElementById('gallery-status');

    function base(path) { return '/admin' + path; }

    function setStatus(msg, isError) {
        statusEl.textContent = msg || '';
        statusEl.className = 'gallery-status' + (isError ? ' error' : '');
        if (msg) {
            setTimeout(function () {
                if (statusEl.textContent === msg) { statusEl.textContent = ''; statusEl.className = 'gallery-status'; }
            }, 4000);
        }
    }

    function renderImages(imagenes) {
        if (loading) loading.remove();
        grid.innerHTML = '';
        if (!imagenes || !imagenes.length) {
            grid.innerHTML = '<div class="gallery-empty">Sin fotos todavia.</div>';
            return;
        }
        imagenes.forEach(function (img) {
            var cell = document.createElement('div');
            cell.className = 'gallery-cell';
            cell.dataset.imageId = img.id;
            cell.innerHTML =
                '<span class="gallery-drag-handle" title="Arrastra para reordenar">&#8942;&#8942;</span>' +
                '<img src="' + img.url + '" alt="Foto ' + (img.posicion + 1) + '">' +
                '<button type="button" class="gallery-del" data-id="' + img.id + '" title="Eliminar">&times;</button>';
            grid.appendChild(cell);
        });
        bindDelete();
        initSortable();
    }

    function loadImages() {
        fetch(base('/discs/' + discId + '/images'), {
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        })
            .then(function (r) { return r.json(); })
            .then(function (data) { renderImages(data.imagenes || []); })
            .catch(function () { if (loading) loading.textContent = 'Error al cargar fotos.'; });
    }

    // ---- Subir ----
    uploadBtn.addEventListener('click', function () { uploadInput.click(); });
    uploadInput.addEventListener('change', function () {
        if (!uploadInput.files.length) return;
        var fd = new FormData();
        for (var i = 0; i < uploadInput.files.length; i++) {
            fd.append('images', uploadInput.files[i]);
        }
        setStatus('Subiendo...');
        uploadBtn.disabled = true;
        fetch(base('/discs/' + discId + '/images/upload'), {
            method: 'POST',
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
            body: fd
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                uploadBtn.disabled = false;
                uploadInput.value = '';
                if (data.ok) {
                    setStatus((data.imagenes ? data.imagenes.length : 0) + ' foto(s) agregada(s).');
                    loadImages();
                } else {
                    setStatus(data.error || 'Error al subir.', true);
                }
            })
            .catch(function () {
                uploadBtn.disabled = false;
                setStatus('Error al subir.', true);
            });
    });

    // ---- Borrar ----
    function bindDelete() {
        grid.querySelectorAll('.gallery-del').forEach(function (btn) {
            btn.addEventListener('click', function () {
                if (!confirm('Eliminar esta foto?')) return;
                var id = btn.dataset.id;
                fetch(base('/images/' + id + '/delete'), {
                    method: 'POST',
                    headers: { 'X-Requested-With': 'XMLHttpRequest' }
                })
                    .then(function (r) { return r.json(); })
                    .then(function () { loadImages(); })
                    .catch(function () { setStatus('Error al borrar.', true); });
            });
        });
    }

    // ---- Reordenar (drag & drop) ----
    var sortableInstance = null;
    function initSortable() {
        if (typeof Sortable === 'undefined') return;
        if (sortableInstance) sortableInstance.destroy();
        sortableInstance = Sortable.create(grid, {
            handle: '.gallery-drag-handle',
            animation: 180,
            ghostClass: 'gallery-cell-ghost',
            onEnd: function () {
                var cells = grid.querySelectorAll('.gallery-cell');
                var ids = Array.from(cells).map(function (c) { return c.dataset.imageId; });
                var fd = new FormData();
                fd.append('ids', ids.join(','));
                fetch(base('/discs/' + discId + '/images/reorder'), {
                    method: 'POST',
                    headers: { 'X-Requested-With': 'XMLHttpRequest' },
                    body: fd
                });
            }
        });
    }

    loadImages();
})();
