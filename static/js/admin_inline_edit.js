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
        document.querySelectorAll('[data-edit-row]').forEach(initRow);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
