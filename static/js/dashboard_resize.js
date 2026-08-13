/* Columnas redimensionables del dashboard (v3.65).
   Al pasar el mouse por el borde derecho de un encabezado aparece el cursor de
   redimension; arrastrando se hace la columna mas o menos ancha. El ancho de
   cada columna se recuerda en localStorage (por indice de columna).

   Enfoque a proposito conservador: NO usa table-layout:fixed (romperia los
   popovers del quick-edit) — solo setea width/min-width del <th>, que en el
   layout automatico fuerza el ancho de la columna. Widening siempre anda;
   angostar llega hasta el ancho natural del contenido. */
(function () {
    'use strict';

    var KEY = 'arcano-col-widths-v1';
    var MIN = 42; // px minimo por columna

    function cargarGuardados() {
        try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; }
        catch (e) { return {}; }
    }
    function guardar(obj) {
        try { localStorage.setItem(KEY, JSON.stringify(obj)); } catch (e) {}
    }

    function aplicarAncho(th, w) {
        w = Math.max(MIN, Math.round(w));
        th.style.width = w + 'px';
        th.style.minWidth = w + 'px';
    }

    function init() {
        var table = document.querySelector('.stock-table');
        if (!table) return;
        // En celular la tabla se muestra como tarjetas apiladas: no aplica.
        if (window.matchMedia && window.matchMedia('(max-width: 900px)').matches) return;

        var headRow = table.querySelector('thead tr');
        if (!headRow) return;
        var ths = Array.prototype.slice.call(headRow.children);
        if (!ths.length) return;

        var guardados = cargarGuardados();

        // Restaurar anchos guardados
        ths.forEach(function (th, i) {
            if (guardados[i]) aplicarAncho(th, guardados[i]);
        });

        ths.forEach(function (th, i) {
            if (th.querySelector('.col-resizer')) return; // ya tiene handle
            th.style.position = 'relative';
            var handle = document.createElement('div');
            handle.className = 'col-resizer';
            handle.setAttribute('aria-hidden', 'true');
            th.appendChild(handle);

            var startX = 0, startW = 0;

            function onMove(ev) {
                var dx = (ev.touches ? ev.touches[0].clientX : ev.clientX) - startX;
                aplicarAncho(th, startW + dx);
            }
            function onUp() {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                document.removeEventListener('touchmove', onMove);
                document.removeEventListener('touchend', onUp);
                document.body.classList.remove('col-resizing');
                // Guardar el ancho real resultante de cada columna
                var obj = {};
                ths.forEach(function (x, j) { obj[j] = Math.round(x.getBoundingClientRect().width); });
                guardar(obj);
            }
            function onDown(ev) {
                ev.preventDefault();
                ev.stopPropagation();
                startX = (ev.touches ? ev.touches[0].clientX : ev.clientX);
                startW = th.getBoundingClientRect().width;
                document.body.classList.add('col-resizing');
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
                document.addEventListener('touchmove', onMove, { passive: false });
                document.addEventListener('touchend', onUp);
            }
            handle.addEventListener('mousedown', onDown);
            handle.addEventListener('touchstart', onDown, { passive: false });
            // Que un click en el handle no dispare el orden de la columna
            handle.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); });
            // Doble click en el handle: resetea esa columna a automatico
            handle.addEventListener('dblclick', function (e) {
                e.preventDefault(); e.stopPropagation();
                th.style.width = '';
                th.style.minWidth = '';
                var obj = {};
                ths.forEach(function (x, j) { obj[j] = Math.round(x.getBoundingClientRect().width); });
                guardar(obj);
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
