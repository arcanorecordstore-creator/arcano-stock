/* Columnas redimensionables en TODAS las tablas del admin (v3.67 en Stock,
   v5.69 Tareas, v5.70 todo el admin).
   Al pasar el mouse por el borde derecho de un encabezado aparece el cursor de
   redimension; arrastrando se hace la columna mas o menos ancha (agrandar Y
   achicar). Doble click en ese borde: vuelve todas las columnas al ancho automatico.

   Cada tabla guarda sus anchos aparte en localStorage: el Stock en
   'arcano-col-widths-v2' (la clave de siempre, no se pierden los anchos guardados)
   y el resto en 'arcano-col-widths-<data-resize-key>' (si la tabla no tiene
   data-resize-key, se usa la pagina + el numero de tabla).

   Usa table-layout:fixed para que los anchos se respeten EXACTO (asi se puede
   achicar por debajo del ancho del contenido, que con el layout automatico no se
   podia). Las celdas recortan con "..." lo que no entra. Los <select> e <input>
   nativos de las celdas no se ven afectados (su desplegable se dibuja por encima).

   No aplica en celular (<= 900px): ahi varias tablas se muestran como tarjetas.
   Las tablas que arrancan ocultas (ej: la lista de discos desplegable de cada
   pedido) se preparan recien cuando se ven, para medir bien sus anchos. */
(function () {
    'use strict';

    var KEY_STOCK = 'arcano-col-widths-v2';
    var MIN = 32; // px minimo por columna

    function cargarGuardados(KEY) {
        try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; }
        catch (e) { return {}; }
    }

    function esCelular() {
        return !!(window.matchMedia && window.matchMedia('(max-width: 900px)').matches);
    }

    function initTabla(table, KEY) {
        if (!table || table._colsInit) return;
        if (esCelular()) return;

        var headRow = table.querySelector('thead tr');
        if (!headRow) return;
        var ths = Array.prototype.slice.call(headRow.children);
        if (!ths.length) return;
        // Oculta (display:none o dentro de algo cerrado): se prepara cuando se vea.
        if (!table.getBoundingClientRect().width) return;
        table._colsInit = true;

        var guardados = cargarGuardados(KEY);
        // Si la estructura de columnas cambio (se agrego o saco una), los anchos
        // guardados ya no corresponden: se ignoran.
        if (guardados._n && guardados._n !== ths.length) guardados = {};

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
            // Si la tabla queda mas ancha que su caja, la caja pasa a tener scroll
            // horizontal (asi nunca se desborda la pagina).
            var p = table.parentElement;
            if (p && t > p.clientWidth + 1 && getComputedStyle(p).overflowX === 'visible') {
                p.style.overflowX = 'auto';
            }
        }

        // Fijar el layout ANTES de aplicar (asi los anchos mandan exacto).
        table.classList.add('cols-resizable');
        table.style.tableLayout = 'fixed';
        aplicar();

        function guardar() {
            var obj = { _n: ths.length };
            widths.forEach(function (w, i) { obj[i] = Math.round(w); });
            try { localStorage.setItem(KEY, JSON.stringify(obj)); } catch (e) {}
        }

        ths.forEach(function (th, i) {
            if (th.querySelector('.col-resizer')) return;
            if (getComputedStyle(th).position === 'static') th.style.position = 'relative';
            var handle = document.createElement('div');
            handle.className = 'col-resizer';
            handle.setAttribute('aria-hidden', 'true');
            handle.title = 'Arrastrá para cambiar el ancho · doble clic para volver al automático';
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
            // Doble click: resetea TODAS las columnas de esta tabla al ancho automatico.
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

    function claveDe(table, idx) {
        if (table.classList.contains('stock-table')) return KEY_STOCK;
        var k = table.getAttribute('data-resize-key');
        return 'arcano-col-widths-' + (k || (location.pathname + '#' + idx));
    }

    function init() {
        var tablas = Array.prototype.slice.call(document.querySelectorAll('.app table'));
        var pendientes = [];
        tablas.forEach(function (t, idx) {
            if (t.closest('table') !== t) return;   // tablas dentro de tablas: no
            if (!t.querySelector('thead tr')) return;
            initTabla(t, claveDe(t, idx));
            if (!t._colsInit) pendientes.push([t, idx]);
        });
        // Las que estaban ocultas se preparan apenas tienen tamaño.
        if (pendientes.length && window.ResizeObserver) {
            var ro = new ResizeObserver(function (entries) {
                entries.forEach(function (en) {
                    var t = en.target;
                    if (t._colsInit || !en.contentRect.width) return;
                    initTabla(t, claveDe(t, t._resizeIdx));
                    if (t._colsInit) ro.unobserve(t);
                });
            });
            pendientes.forEach(function (p) { p[0]._resizeIdx = p[1]; ro.observe(p[0]); });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
