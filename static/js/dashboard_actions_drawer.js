/* Cajon lateral de acciones por disco en el dashboard.
 * Un boton "Acciones" por fila (siempre visible en la celda del artista) abre
 * un panel a la derecha con Ver / Editar / Duplicar / Eliminar del disco. */
(function () {
    'use strict';

    var overlay = document.getElementById('row-drawer-overlay');
    var drawer = document.getElementById('row-drawer');
    if (!overlay || !drawer) return;

    var titleEl = document.getElementById('row-drawer-title');
    var actionsEl = document.getElementById('row-drawer-actions');
    var closeBtn = document.getElementById('row-drawer-close');
    var lastTrigger = null;

    function csrf() {
        var m = document.querySelector('meta[name="csrf-token"]');
        return m ? (m.getAttribute('content') || '') : '';
    }

    function linkAction(href, label, cls) {
        var a = document.createElement('a');
        a.href = href;
        a.className = 'drawer-action' + (cls ? ' ' + cls : '');
        a.textContent = label;
        return a;
    }

    function formAction(action, label, cls, confirmMsg) {
        var f = document.createElement('form');
        f.method = 'post';
        f.action = action;
        if (confirmMsg) f.setAttribute('data-confirm', confirmMsg);
        var tok = document.createElement('input');
        tok.type = 'hidden'; tok.name = '_csrf_token'; tok.value = csrf();
        f.appendChild(tok);
        var b = document.createElement('button');
        b.type = 'submit';
        b.className = 'drawer-action' + (cls ? ' ' + cls : '');
        b.textContent = label;
        f.appendChild(b);
        // Confirmacion (app.js maneja data-confirm en submit, pero por las dudas:)
        f.addEventListener('submit', function (e) {
            if (confirmMsg && !window.confirm(confirmMsg)) e.preventDefault();
        });
        return f;
    }

    function abrir(btn) {
        lastTrigger = btn;
        var nombre = btn.getAttribute('data-nombre') || 'este disco';
        titleEl.textContent = nombre;
        actionsEl.innerHTML = '';
        actionsEl.appendChild(linkAction(btn.getAttribute('data-ver'), 'Ver ficha'));
        actionsEl.appendChild(linkAction(btn.getAttribute('data-editar'), 'Editar'));
        actionsEl.appendChild(formAction(
            btn.getAttribute('data-duplicar'), 'Duplicar', '',
            '¿Duplicar «' + nombre + '»? Se crea una copia idéntica que podés editar.'));
        actionsEl.appendChild(formAction(
            btn.getAttribute('data-eliminar'), 'Eliminar', 'danger',
            '¿Eliminar «' + nombre + '»? Podés deshacerlo con Ctrl+Z o desde Historial.'));
        overlay.hidden = false;
        drawer.hidden = false;
        drawer.setAttribute('aria-hidden', 'false');
        // fuerza reflow para la transicion
        void drawer.offsetWidth;
        overlay.classList.add('is-open');
        drawer.classList.add('is-open');
    }

    function cerrar() {
        overlay.classList.remove('is-open');
        drawer.classList.remove('is-open');
        drawer.setAttribute('aria-hidden', 'true');
        setTimeout(function () {
            overlay.hidden = true;
            drawer.hidden = true;
        }, 220);
        if (lastTrigger) { try { lastTrigger.focus(); } catch (e) {} }
    }

    document.addEventListener('click', function (e) {
        var btn = e.target.closest ? e.target.closest('.row-menu-btn') : null;
        if (btn) { e.preventDefault(); abrir(btn); }
    });
    overlay.addEventListener('click', cerrar);
    if (closeBtn) closeBtn.addEventListener('click', cerrar);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !drawer.hidden) cerrar();
    });
})();
