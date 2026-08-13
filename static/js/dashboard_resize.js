/* Columnas redimensionables del dashboard (v3.67).
   Al pasar el mouse por el borde derecho de un encabezado aparece el cursor de
   redimension; arrastrando se hace la columna mas o menos ancha (agrandar Y
   achicar). El ancho de cada columna se recuerda en localStorage.

   Usa table-layout:fixed para que los anchos se respeten EXACTO (asi se puede
   achicar por debajo del ancho del contenido, que con el layout automatico no se
   podia). El quick-edit usa <input>/<select> nativos dentro de la celda, asi que
   el overflow:hidden de las celdas no rompe nada (el desplegable nativo del
   <select> se dibuja por encima, no lo recorta). */
(function () {
    'use strict';

    var KEY = 'arcano-col-widths-v2';
    var MIN = 32; // px minimo por columna

    function cargarGuardados() {
        try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; }
        catch (e) { return {}; }
    }

    function init() {
        var table = document.querySelector('.stock-table');
        if (!table) return;
        if (table._colsInit) return;
        // En celular la tabla se muestra como tarjetas apiladas: no aplica.
        if (window.matchMedia && window.matchMedia('(max-width: 900px)').matches) return;

        var headRow = table.querySelector('thead tr');
        if (!headRow) return;
        var ths = Array.prototype.slice.call(headRow.children);
        if (!ths.length) return;
        table._colsInit = true;

        var guardados = cargarGuardados();

        // Anchos iniciales: los guardados, o los que tiene ahora la tabla.
        var widths = ths.map(function (th, i) {
            return Math.max(MIN, Math.round(guardados[i] || th.getBoundingClientRect().width));
        });

        function total() {
            return widths.reduce(function (a, b) { return a + b; }, 0);
        }
        function aplicar() {
            ths.forEach(function (th, i) { th.style.width = widths[i] + 'px'; });
            var t = total();
            table.style.width = t + 'px';
            table.style.minWidth = t + 'px';
        }

        // Fijar el layout ANTES de aplicar (asi los anchos mandan exacto).
        table.classList.add('cols-resizable');
        table.style.tableLayout = 'fixed';
        aplicar();

        function guardar() {
            var obj = {};
            widths.forEach(function (w, i) { obj[i] = Math.round(w); });
            try { localStorage.setItem(KEY, JSON.stringify(obj)); } catch (e) {}
        }

        ths.forEach(function (th, i) {
            if (th.querySelector('.col-resizer')) return;
            th.style.position = 'relative';
            var handle = document.createElement('div');
            handle.className = 'col-resizer';
            handle.setAttribute('aria-hidden', 'true');
            th.appendChild(handle);

            var startX = 0, startW = 0;
            function onMove(ev) {
                var x = ev.touches ? ev.touches[0].clientX : ev.clientX;
                widths[i] = Math.max(MIN, Math.round(startW + (x - startX)));
                aplicar();  // achica Y agranda: con table-layout:fixed se respeta exacto
            }
            function onUp() {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                document.removeEventListener('touchmove', onMove);
                document.removeEventListener('touchend', onUp);
                document.body.classList.remove('col-resizing');
                guardar();
            }
            function onDown(ev) {
                ev.preventDefault();
                ev.stopPropagation();
                startX = ev.touches ? ev.touches[0].clientX : ev.clientX;
                startW = widths[i];
                document.body.classList.add('col-resizing');
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
                document.addEventListener('touchmove', onMove, { passive: false });
                document.addEventListener('touchend', onUp);
            }
            handle.addEventListener('mousedown', onDown);
            handle.addEventListener('touchstart', onDown, { passive: false });
            handle.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); });
            // Doble click: resetea TODAS las columnas al ancho automatico.
            handle.addEventListener('dblclick', function (e) {
                e.preventDefault(); e.stopPropagation();
                table.classList.remove('cols-resizable');
                table.style.tableLayout = '';
                table.style.width = '';
                table.style.minWidth = '';
                ths.forEach(function (t) { t.style.width = ''; });
                try { localStorage.removeItem(KEY); } catch (er) {}
                // Volver a medir y re-fijar con los anchos automaticos nuevos.
                requestAnimationFrame(function () {
                    widths = ths.map(function (t) { return Math.max(MIN, Math.round(t.getBoundingClientRect().width)); });
                    table.classList.add('cols-resizable');
                    table.style.tableLayout = 'fixed';
                    aplicar();
                });
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
