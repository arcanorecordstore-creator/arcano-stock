/* Edicion inline en los paneles de gestion.
 *
 * Convencion:
 *   - La fila editable lleva  data-edit-row
 *   - Los controles de accion (guardar / anular / cancelar) van dentro de un
 *     elemento con clase  .edit-actions  (o llevan esa clase ellos mismos).
 *   - El CSS oculta  .edit-actions  por defecto y los muestra cuando la fila
 *     tiene la clase  is-dirty  (que agrega este script).
 *
 * Resultado: los datos se ven limpios; los botones aparecen solo cuando el
 * usuario cambio algun campo respecto a su valor original.
 */
(function () {
    'use strict';

    function fieldVal(f) {
        if (f.type === 'checkbox' || f.type === 'radio') return f.checked ? '1' : '0';
        return f.value;
    }

    function initRow(row) {
        var fields = row.querySelectorAll(
            'input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea'
        );
        if (!fields.length) return;

        var initial = [];
        fields.forEach(function (f) { initial.push(fieldVal(f)); });

        function check() {
            var dirty = false;
            for (var i = 0; i < fields.length; i++) {
                if (fieldVal(fields[i]) !== initial[i]) { dirty = true; break; }
            }
            row.classList.toggle('is-dirty', dirty);
        }

        fields.forEach(function (f) {
            f.addEventListener('input', check);
            f.addEventListener('change', check);
        });
    }

    function init() {
        // data-edit-row  = look "dato limpio" + botones al cambiar
        // data-edit-watch = solo botones al cambiar (sin tocar el estilo)
        document.querySelectorAll('[data-edit-row], [data-edit-watch]').forEach(initRow);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();


/* Ctrl+Z (o Cmd+Z) global en el admin: deshace la ultima operacion.
 * Si el foco esta en un campo editable NO interfiere (deja el undo nativo). */
(function () {
    'use strict';

    function esEditable(el) {
        if (!el) return false;
        var tag = el.tagName;
        return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
    }

    function deshacer() {
        fetch('/admin/deshacer', { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (!d || !d.hay) {
                    alert('No hay ninguna operación reciente para deshacer.');
                    return;
                }
                if (!confirm('¿Deshacer la última operación?\n\n' + d.resumen + '  ·  ' + d.fecha)) return;
                var f = document.createElement('form');
                f.method = 'POST';
                f.action = '/admin/deshacer';
                var meta = document.querySelector('meta[name="csrf-token"]');
                if (meta) {
                    var i = document.createElement('input');
                    i.type = 'hidden';
                    i.name = '_csrf_token';
                    i.value = meta.getAttribute('content') || '';
                    f.appendChild(i);
                }
                document.body.appendChild(f);
                f.submit();
            })
            .catch(function () { alert('No se pudo consultar la última operación.'); });
    }

    document.addEventListener('keydown', function (e) {
        if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;
        if ((e.key || '').toLowerCase() !== 'z') return;
        if (esEditable(e.target)) return;   // respetar el deshacer nativo de los campos
        e.preventDefault();
        deshacer();
    });
})();
