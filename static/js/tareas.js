/* Tareas e Ideas de contenido: edicion en la celda. Cada cambio se guarda solo via
   <data-base de la tabla><id>/campo (/admin/tareas/ o /admin/ideas/) y se actualizan
   las columnas calculadas de esa fila. */
(function () {
    'use strict';
    var tablas = document.querySelectorAll('.tk-table[data-base]');
    if (!tablas.length) return;

    function guardar(base, id, campo, valor) {
        return fetch(base + id + '/campo', {
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

    function refrescarFila(fila, d) {
        if (!fila) return;
        var dias = fila.querySelector('.tk-dias');
        if (dias) {
            dias.querySelector('.tk-dias-n').textContent = d.dias_n || '';
            dias.querySelector('.tk-dias-u').textContent = d.dias_u || '';
            dias.className = 'tk-dias' + (d.dias_tono ? ' is-' + d.dias_tono : '');
            fila.classList.remove('row-late', 'row-today', 'row-soon');
            if (d.dias_tono && !d.hecha) fila.classList.add('row-' + d.dias_tono);
        }
        var est = fila.querySelector('[data-campo="estado"]');
        if (est) {
            est.value = d.estado;
            if (d.estado_key) est.closest('.tk-estado').dataset.estado = d.estado_key;
        }
        var cat = fila.querySelector('[data-campo="categoria"]');
        if (cat && d.cat_rgb !== undefined) {
            if (d.cat_rgb) cat.style.setProperty('--tag-rgb', d.cat_rgb);
            else cat.style.removeProperty('--tag-rgb');
        }
        var prio = fila.querySelector('[data-campo="prioridad"]');
        if (prio) prio.closest('.tk-prio').dataset.prio = prio.value || '0';
        var prog = fila.querySelector('[data-campo="progreso"]');
        if (prog && document.activeElement !== prog) prog.value = (d.progreso === null || d.progreso === undefined) ? '' : d.progreso;
        var fecha = fila.querySelector('[data-campo="fecha_limite"]');
        if (fecha) fecha.classList.toggle('is-empty', !fecha.value);
        var av = fila.querySelector('.tk-av');
        if (av) {
            av.classList.toggle('is-off', !d.meta);
            av.title = d.meta ? ((d.progreso || 0) + ' de ' + d.meta) : 'Poné una meta para ver el avance';
            av.querySelector('.tk-av-fill').style.width = (d.avance || 0) + '%';
            av.querySelector('.tk-av-pct').textContent = d.meta ? (d.avance + '%') : '—';
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

    Array.prototype.forEach.call(tablas, function (tabla) {
        var base = tabla.dataset.base;

        function filaDe(id) { return tabla.querySelector('tr.tk-row[data-id="' + id + '"]'); }

        function enviar(el) {
            var tr = el.closest('tr[data-id]');
            var id = el.dataset.id || (tr && tr.dataset.id);
            var campo = el.dataset.campo;
            var valor = valorDe(el);
            if (el.dataset.ultimo === String(valor)) return;   // no cambio nada
            var anterior = el.dataset.ultimo;
            el.dataset.ultimo = String(valor);
            guardar(base, id, campo, valor).then(function (res) {
                if (res.ok) {
                    if (res.data.recargar) { window.location.reload(); return; }
                    if (res.data.grid && window.__ctRender) window.__ctRender(filaDe(id), res.data);
                    refrescarFila(filaDe(id), res.data);
                    if (campo === 'detalle') {
                        var fila = filaDe(id);
                        var btn = fila && fila.querySelector('.tk-nota-btn');
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

        Array.prototype.forEach.call(tabla.querySelectorAll('[data-campo]'), function (el) {
            el.dataset.ultimo = String(valorDe(el));
            var inmediato = el.tagName === 'SELECT' || el.type === 'checkbox' || el.type === 'date';
            if (inmediato) {
                el.addEventListener('change', function () { enviar(el); });
            } else {
                // Texto y numeros: se guarda al salir de la celda o con Enter.
                el.addEventListener('blur', function () { enviar(el); });
                el.addEventListener('keydown', function (e) {
                    if (e.key === 'Enter' && (el.tagName !== 'TEXTAREA' || el.dataset.campo === 'titulo')) { e.preventDefault(); el.blur(); }
                    if (e.key === 'Escape') { el.value = el.dataset.ultimo; el.blur(); }
                });
                if (el.list) el.addEventListener('change', function () { enviar(el); });
            }
        });

        // Nota: la flechita abre/cierra una fila debajo con el texto.
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
    });
})();

/* Constancia: contador de hoy (- / +) y casilleros de la semana. */
(function () {
    'use strict';
    var tabla = document.getElementById('tk-constancia');

    function render(fila, d) {
        if (!fila || !d) return;
        var n = fila.querySelector('.ct-hoy-n');
        if (n) {
            n.innerHTML = '<b>' + d.hoy_n + '</b>' + (d.hoy_meta ? '<small>/' + d.hoy_meta + '</small>' : '');
            n.classList.toggle('is-ok', !!(d.hoy_meta && d.hoy_n >= d.hoy_meta));
        }
        var menos = fila.querySelector('.ct-step[data-delta="-1"]');
        if (menos) menos.disabled = !d.hoy_n;
        (d.grid || []).forEach(function (g) {
            var b = fila.querySelector('.ct-dia[data-fecha="' + g.fecha + '"]');
            if (!b) return;
            b.className = 'ct-dia is-' + g.estado + (g.hoy ? ' is-hoy' : '');
        });
        var per = fila.querySelector('.ct-periodo');
        if (per) per.textContent = d.periodo;
        var est = fila.querySelector('.ct-estado');
        if (est) { est.textContent = d.estado_txt; est.className = 'ct-estado ct-estado-' + d.estado; }
        fila.className = fila.className.replace(/ct-(ok|camino|atrasado)/, 'ct-' + d.estado);
        resumen();
    }

    // Franja de arriba: se recalcula con lo que muestran las filas.
    function resumen() {
        var filas = document.querySelectorAll('tr.ct-row');
        var diarias = 0, hoyOk = 0, alDia = 0, atr = 0;
        Array.prototype.forEach.call(filas, function (f) {
            var n = f.querySelector('.ct-hoy-n');
            if (n && n.querySelector('small')) { diarias++; if (n.classList.contains('is-ok')) hoyOk++; }
            if (f.classList.contains('ct-atrasado')) atr++; else alDia++;
        });
        var set = function (k, v) { var el = document.querySelector('[data-ct="' + k + '"]'); if (el) el.textContent = v; };
        set('hoy', hoyOk + '/' + diarias); set('aldia', alDia); set('atr', atr);
        var box = document.querySelector('[data-ct-box="atr"]');
        if (box) box.classList.toggle('is-late', atr > 0);
    }
    window.__ctRender = render;
    if (!tabla) return;

    function marcar(fila, body, btn) {
        if (btn) btn.disabled = true;
        fetch('/admin/constancia/' + fila.dataset.id + '/marcar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify(body)
        }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok && d.ok, data: d }; }); })
          .then(function (res) {
              if (btn) btn.disabled = false;
              if (res.ok) render(fila, res.data);
              else alert((res.data && res.data.error) || 'No se pudo guardar.');
          })
          .catch(function () { if (btn) btn.disabled = false; });
    }

    tabla.addEventListener('click', function (e) {
        var step = e.target.closest('.ct-step');
        var dia = e.target.closest('.ct-dia');
        var fila = e.target.closest('tr.ct-row');
        if (!fila) return;
        if (step) marcar(fila, { delta: parseInt(step.dataset.delta, 10) }, step);
        else if (dia && !dia.disabled) marcar(fila, { fecha: dia.dataset.fecha, toggle: true }, null);
    });

    // Alta: mostrar el campo extra segun la frecuencia elegida
    var frec = document.querySelector('.ct-new-frec');
    if (frec) {
        var sync = function () {
            document.querySelectorAll('.ct-new-extra').forEach(function (x) { x.hidden = x.dataset.para !== frec.value; });
        };
        frec.addEventListener('change', sync);
        sync();
    }
})();

