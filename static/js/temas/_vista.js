/* VISTA GRILLA / LISTA (copia de diseno)
   Agrega el boton "Grilla | Lista" arriba del catalogo. La eleccion se guarda en
   el navegador del cliente (localStorage) y se aplica con la clase
   body.vista-lista; el CSS esta en static/css/temas/_vista.css.
   En la opcion Q (Disqueria) arranca en Lista si el cliente no eligio nada. */
(function () {
    'use strict';
    var CLAVE = 'arcano_vista';
    var ICONOS = {
        grilla: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="6" height="6" rx="1"/><rect x="9" y="1" width="6" height="6" rx="1"/><rect x="1" y="9" width="6" height="6" rx="1"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg>',
        lista: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="2" width="14" height="2" rx="1"/><rect x="1" y="7" width="14" height="2" rx="1"/><rect x="1" y="12" width="14" height="2" rx="1"/></svg>'
    };

    function leer() {
        try { var v = localStorage.getItem(CLAVE); if (v === 'lista' || v === 'grilla') return v; } catch (e) {}
        return document.body.classList.contains('tema-disqueria') ? 'lista' : 'grilla';
    }
    function aplicar(v) {
        // E ya es una lista con su propio diseno y L es el carrete: ahi no se aplica
        if (document.body.classList.contains('tema-lista') || document.body.classList.contains('tema-carrete')) v = 'grilla';
        document.body.classList.toggle('vista-lista', v === 'lista');
        document.querySelectorAll('.vista-toggle button').forEach(function (b) {
            var on = b.dataset.vista === v;
            b.classList.toggle('is-on', on);
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
        try { document.dispatchEvent(new CustomEvent('arcano:vista', { detail: v })); } catch (e) {}
    }

    function init() {
        var head = document.querySelector('.public-results-head');
        if (head && !head.querySelector('.vista-toggle')) {
            var t = document.createElement('div');
            t.className = 'vista-toggle';
            t.setAttribute('role', 'group');
            t.setAttribute('aria-label', 'Cómo ver el catálogo');
            ['grilla', 'lista'].forEach(function (v) {
                var b = document.createElement('button');
                b.type = 'button';
                b.dataset.vista = v;
                b.innerHTML = ICONOS[v] + '<span>' + (v === 'grilla' ? 'Grilla' : 'Lista') + '</span>';
                b.addEventListener('click', function () {
                    try { localStorage.setItem(CLAVE, v); } catch (e) {}
                    aplicar(v);
                });
                t.appendChild(b);
            });
            head.appendChild(t);
        }
        armarIndice();
        aplicar(leer());
    }

    // ---- Diseno de lista de Q: indice A-Z + encabezado de columnas ----
    function inicial(t) {
        var c = (t || '').trim().charAt(0).toUpperCase();
        c = c.normalize ? c.normalize('NFD').replace(/[̀-ͯ]/g, '') : c;
        return /[A-Z]/.test(c) ? c : '#';
    }
    function armarIndice() {
        var grid = document.querySelector('.public-catalog .public-grid');
        if (!grid || grid.parentNode.querySelector('.vl-indice')) return;
        var primera = {};
        grid.querySelectorAll(':scope > .public-card').forEach(function (c) {
            var a = c.querySelector('.public-card-artist');
            var l = inicial(a && a.textContent);
            if (!primera[l]) primera[l] = c;
        });
        var letras = Object.keys(primera).sort(function (a, b) { return a === '#' ? 1 : b === '#' ? -1 : a.localeCompare(b); });
        var ind = document.createElement('nav');
        ind.className = 'vl-indice';
        ind.setAttribute('aria-label', 'Saltar por artista');
        letras.forEach(function (l) {
            var b = document.createElement('button');
            b.type = 'button';
            b.textContent = l;
            b.addEventListener('click', function () {
                var c = primera[l];
                c.scrollIntoView({ behavior: 'smooth', block: 'center' });
                c.classList.add('vl-salto');
                setTimeout(function () { c.classList.remove('vl-salto'); }, 1400);
            });
            ind.appendChild(b);
        });
        var cab = document.createElement('div');
        cab.className = 'vl-cab';
        cab.setAttribute('aria-hidden', 'true');
        cab.innerHTML = '<span></span><span>Artista</span><span>Título</span><span>Sello · año</span><span>Precio</span>';
        grid.parentNode.insertBefore(ind, grid);
        grid.parentNode.insertBefore(cab, grid);
    }
    // el disco que suena gira en la lista (se mira cada medio segundo, sin tocar player.js)
    setInterval(function () {
        if (!document.body.classList.contains('vista-lista')) return;
        var activo = document.querySelector('.public-grid .card-play-btn.is-playing');
        var pausa = document.querySelector('#fp-playpause .fp-ico-pause');
        var fp = document.getElementById('floating-player');
        var suena = !!(activo && fp && !fp.hidden && pausa && !pausa.hidden);
        document.querySelectorAll('.public-grid > .public-card').forEach(function (c) { c.classList.toggle('vl-sonando', suena && c.contains(activo)); });
    }, 500);

    if (window.Arcano && window.Arcano.onContent) window.Arcano.onContent(init); else document.addEventListener('DOMContentLoaded', init);
})();
