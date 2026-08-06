/* Reordenar discos (drag-and-drop) en el dashboard.
   El nuevo orden se refleja en el orden de la tienda. Usa SortableJS (CDN).
   Solo se activa en "orden manual" (data-reorder="1"); si se ordena por una
   columna, el arrastre queda desactivado.

   Multi-seleccion: si tildas varios discos con el checkbox y arrastras uno de
   los tildados, se mueven todos juntos (conservando su orden relativo). */
(function () {
    'use strict';

    var table = document.querySelector('.stock-table');
    if (!table) return;
    if (table.dataset.reorder !== '1') return;      // ordenado por columna: no arrastrable
    var tbody = table.querySelector('tbody');
    if (!tbody || typeof Sortable === 'undefined') return;

    function feedback(ok) {
        var el = document.createElement('div');
        el.className = 'reorder-toast' + (ok ? '' : ' reorder-toast-err');
        el.textContent = ok ? '✓ Orden guardado' : 'No se pudo guardar el orden';
        document.body.appendChild(el);
        void el.offsetWidth;                        // reflow para la transicion
        el.classList.add('visible');
        setTimeout(function () {
            el.classList.remove('visible');
            setTimeout(function () {
                if (el.parentNode) el.parentNode.removeChild(el);
            }, 300);
        }, 1600);
    }

    // Filas actualmente tildadas (checkbox marcado), en orden del DOM
    function filasTildadas() {
        return Array.prototype.filter.call(
            tbody.querySelectorAll('tr[data-disc-id]'),
            function (r) { var cb = r.querySelector('.row-check'); return cb && cb.checked; }
        );
    }

    function guardarOrden() {
        var rows = tbody.querySelectorAll('tr[data-disc-id]');
        var ids = Array.prototype.map.call(rows, function (r) { return r.dataset.discId; });
        var fd = new FormData();
        fd.append('ids', ids.join(','));
        fetch('/admin/discs/reorder', {
            method: 'POST',
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
            body: fd
        })
        .then(function (r) { return r.json(); })
        .then(function (data) { feedback(!!(data && data.ok)); })
        .catch(function () { feedback(false); });
    }

    var grupo = null;   // filas a mover juntas, capturadas al empezar el arrastre

    Sortable.create(tbody, {
        handle: '.disc-drag-handle',
        animation: 180,
        ghostClass: 'disc-row-ghost',
        chosenClass: 'disc-row-chosen',
        dragClass: 'disc-row-drag',
        onStart: function (evt) {
            grupo = null;
            var cb = evt.item.querySelector('.row-check');
            if (cb && cb.checked) {
                var seleccion = filasTildadas();   // incluye la fila arrastrada
                if (seleccion.length > 1) grupo = seleccion;
            }
        },
        onEnd: function (evt) {
            var dragged = evt.item;
            if (grupo && grupo.length > 1 && grupo.indexOf(dragged) !== -1) {
                var idx = grupo.indexOf(dragged);
                // Las que iban antes: delante de la arrastrada, en orden
                var ref = dragged;
                for (var i = idx - 1; i >= 0; i--) {
                    dragged.parentNode.insertBefore(grupo[i], ref);
                    ref = grupo[i];
                }
                // Las que iban despues: detras de la arrastrada, en orden
                ref = dragged;
                for (var j = idx + 1; j < grupo.length; j++) {
                    if (ref.nextSibling) dragged.parentNode.insertBefore(grupo[j], ref.nextSibling);
                    else dragged.parentNode.appendChild(grupo[j]);
                    ref = grupo[j];
                }
            }
            grupo = null;
            guardarOrden();
        }
    });
})();
