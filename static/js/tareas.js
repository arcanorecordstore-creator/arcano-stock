/* Tareas: edicion en la celda (como una tabla de Notion). Cada cambio se guarda
   solo via /admin/tareas/<id>/campo y se actualizan las columnas calculadas. */
(function () {
    'use strict';
    var tabla = document.getElementById('tk-table');
    if (!tabla) return;

    function guardar(id, campo, valor) {
        return fetch('/admin/tareas/' + id + '/campo', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ campo: campo, valor: valor })
        }).then(function (r) {
            return r.json().then(function (d) { return { ok: r.ok && d.ok, data: d }; });
        });
    }

    function valorDe(el) {
        if (el.type === 'checkbox') return el.checked ? 1 : 0;
        return el.value;
    }

    function claseEstado(estado) {
        return estado === 'En curso' ? 'curso' : (estado === 'Hecha' ? 'hecha' : 'nueva');
    }

    function refrescarFila(fila, d) {
        if (!fila) return;
        var dias = fila.querySelector('.tk-dias');
        if (dias) {
            dias.textContent = d.dias_txt || '';
            dias.className = 'tk-dias' + (d.dias_tono ? ' is-' + d.dias_tono : '');
        }
        var est = fila.querySelector('[data-campo="estado"]');
        if (est) {
            est.value = d.estado;
            est.className = est.className.replace(/tk-est-\w+/, 'tk-est-' + claseEstado(d.estado));
        }
        var prio = fila.querySelector('[data-campo="prioridad"]');
        if (prio) prio.className = prio.className.replace(/tk-prio-\d/, 'tk-prio-' + (prio.value || 0));
        var prog = fila.querySelector('[data-campo="progreso"]');
        if (prog && document.activeElement !== prog) prog.value = (d.progreso === null || d.progreso === undefined) ? '' : d.progreso;
        var cat = fila.querySelector('[data-campo="categoria"]');
        if (cat) {
            cat.style.setProperty('--pill', d.cat_color || 'transparent');
            cat.classList.toggle('is-empty', !cat.value);
        }
        var fecha = fila.querySelector('[data-campo="fecha_limite"]');
        if (fecha) fecha.classList.toggle('is-empty', !fecha.value);
        var av = fila.querySelector('.tk-avance');
        if (av) {
            var llenos = d.avance ? Math.floor(d.avance / 10) : 0;
            av.classList.toggle('is-off', !d.meta);
            av.title = d.meta ? ((d.progreso || 0) + ' de ' + d.meta + ' (' + d.avance + '%)') : 'Poné una meta para ver el avance';
            Array.prototype.forEach.call(av.children, function (sq, i) { sq.classList.toggle('on', i < llenos); });
        }
        fila.classList.toggle('is-hecha', !!d.hecha);
    }

    function marcar(el, ok) {
        var celda = el.closest('td') || el;
        celda.classList.remove('tk-saved', 'tk-error');
        void celda.offsetWidth;
        celda.classList.add(ok ? 'tk-saved' : 'tk-error');
        setTimeout(function () { celda.classList.remove('tk-saved', 'tk-error'); }, 900);
    }

    function enviar(el) {
        var filaId = el.closest('tr[data-id]');
        var id = el.dataset.id || (filaId && filaId.dataset.id);
        var campo = el.dataset.campo;
        var valor = valorDe(el);
        if (el.dataset.ultimo === String(valor)) return;   // no cambio nada
        var anterior = el.dataset.ultimo;
        el.dataset.ultimo = String(valor);
        guardar(id, campo, valor).then(function (res) {
            if (res.ok) {
                refrescarFila(document.getElementById('t' + id), res.data);
                if (campo === 'detalle') {
                    var btn = document.querySelector('#t' + id + ' .tk-nota-btn');
                    if (btn) btn.classList.toggle('has-nota', !!String(valor).trim());
                }
                marcar(el, true);
            } else {
                alert((res.data && res.data.error) || 'No se pudo guardar.');
                if (el.type === 'checkbox') el.checked = anterior === '1';
                else if (anterior !== undefined) el.value = anterior;
                el.dataset.ultimo = anterior;
                marcar(el, false);
            }
        }).catch(function () {
            el.dataset.ultimo = anterior;
            marcar(el, false);
        });
    }

    var campos = document.querySelectorAll('#tk-table [data-campo]');
    Array.prototype.forEach.call(campos, function (el) {
        el.dataset.ultimo = String(valorDe(el));
        var inmediato = el.tagName === 'SELECT' || el.type === 'checkbox' || el.type === 'date';
        if (inmediato) {
            el.addEventListener('change', function () { enviar(el); });
        } else {
            // Texto y numeros: se guarda al salir de la celda o con Enter.
            el.addEventListener('blur', function () { enviar(el); });
            el.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' && el.tagName !== 'TEXTAREA') { e.preventDefault(); el.blur(); }
                if (e.key === 'Escape') { el.value = el.dataset.ultimo; el.blur(); }
            });
            if (el.list) el.addEventListener('change', function () { enviar(el); });
        }
    });

    // Nota: el iconito de hoja abre/cierra una fila debajo con el texto.
    tabla.addEventListener('click', function (e) {
        var btn = e.target.closest('.tk-nota-btn');
        if (!btn) return;
        var fila = btn.closest('tr');
        var nota = tabla.querySelector('.tk-nota-row[data-for="' + fila.dataset.id + '"]');
        if (!nota) return;
        nota.hidden = !nota.hidden;
        btn.setAttribute('aria-expanded', nota.hidden ? 'false' : 'true');
        if (!nota.hidden) nota.querySelector('textarea').focus();
    });
})();
