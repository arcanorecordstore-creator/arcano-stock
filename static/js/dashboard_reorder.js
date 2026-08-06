/* Reordenar discos (drag-and-drop) en el dashboard.
   El nuevo orden se refleja en el orden de la tienda. Usa SortableJS (CDN).
   Solo se activa cuando la tabla esta en "orden manual" (data-reorder="1");
   si el usuario ordena por una columna, el arrastre queda desactivado. */
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

    Sortable.create(tbody, {
        handle: '.disc-drag-handle',
        animation: 180,
        ghostClass: 'disc-row-ghost',
        chosenClass: 'disc-row-chosen',
        dragClass: 'disc-row-drag',
        onEnd: function () {
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
    });
})();
