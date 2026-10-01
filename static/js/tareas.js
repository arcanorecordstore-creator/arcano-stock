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
        // Duracion: el servidor la devuelve normalizada ('90' -> '1 h 30')
        var dur = fila.querySelector('[data-campo="duracion"]');
        if (dur && d.duracion_txt !== undefined) {
            dur.value = d.duracion_txt || '';
            dur.dataset.ultimo = dur.value;
            dur.classList.toggle('is-empty', !dur.value);
        }
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
            if (el.dataset.ultimo === String(valor)) return Promise.resolve(true);   // no cambio nada
            var anterior = el.dataset.ultimo;
            el.dataset.ultimo = String(valor);
            return guardar(base, id, campo, valor).then(function (res) {
                if (res.ok) {
                    if (res.data.recargar) { window.location.reload(); return; }
                    if (res.data.grid && window.__ctRender) window.__ctRender(filaDe(id), res.data);
                    refrescarFila(filaDe(id), res.data);
                    if (campo === 'detalle') {
                        var fila = filaDe(id);
                        var ic = fila && fila.querySelector('.tk-open-nota');
                        if (ic) ic.classList.toggle('is-empty', !String(valor).trim());
                    }
                    if (campo === 'titulo') {
                        var ft = filaDe(id);
                        var tt = ft && ft.querySelector('.tk-open-t');
                        if (tt) tt.textContent = valor;
                    }
                    marcar(el, true);
                    return true;
                } else {
                    alert((res.data && res.data.error) || 'No se pudo guardar.');
                    if (el.type === 'checkbox') el.checked = anterior === '1';
                    else if (anterior !== undefined) el.value = anterior;
                    el.dataset.ultimo = anterior;
                    marcar(el, false);
                    return false;
                }
            }).catch(function () {
                el.dataset.ultimo = anterior;
                marcar(el, false);
                return false;
            });
        }
        tabla.__enviar = enviar;

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

        // Puntaje de ideas: 1 a 5 estrellas. Tocar la estrella que ya esta marcada lo borra.
        function pintarEstrellas(grupo, n) {
            grupo.dataset.puntaje = n;
            Array.prototype.forEach.call(grupo.querySelectorAll('.tk-estrella'), function (b) {
                var k = +b.dataset.n;
                b.classList.toggle('is-on', k <= n);
                b.setAttribute('aria-pressed', k === n ? 'true' : 'false');
                b.title = k + ' de 5' + (k === n ? ' · tocá de nuevo para sacar el puntaje' : '');
            });
        }
        tabla.addEventListener('click', function (e) {
            var b = e.target.closest('.tk-estrella');
            if (!b) return;
            var grupo = b.closest('.tk-estrellas');
            var inp = grupo.querySelector('[data-campo="puntaje"]');
            var n = +b.dataset.n;
            var nuevo = (+inp.value === n) ? 0 : n;
            inp.value = nuevo;
            pintarEstrellas(grupo, nuevo);
            enviar(inp).then(function (ok) { if (!ok) pintarEstrellas(grupo, +inp.value || 0); });
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

/* Panel lateral: abre una tarea/idea para leer y editar todo el texto comodo. */
(function () {
    'use strict';
    var drawer = document.getElementById('tk-drawer');
    if (!drawer) return;
    var tit = drawer.querySelector('.tkd-titulo');
    var view = drawer.querySelector('.tkd-view');
    var text = drawer.querySelector('.tkd-text');
    var btnEdit = drawer.querySelector('.tkd-edit');
    var meta = drawer.querySelector('.tkd-meta');
    var status = drawer.querySelector('.tkd-status');
    var actual = null;   // {fila, tabla, srcTitulo, srcNota}
    var ultimoFoco = null;

    function esc(t) {
        return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    function inline(t) {
        t = esc(t);
        t = t.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        t = t.replace(/(^|[^*])\*(?!\s)([^*]+?)\*(?!\*)/g, '$1<em>$2</em>');
        t = t.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
        return t;
    }
    // Markdown liviano: titulos (#), listas (- * •), listas numeradas, parrafos.
    function render(md) {
        var lines = (md || '').replace(/\r/g, '').split('\n');
        var html = [], lista = null, parr = [];
        function cerrarParr() { if (parr.length) { html.push('<p>' + parr.join('<br>') + '</p>'); parr = []; } }
        function cerrarLista() { if (lista) { html.push('</' + lista + '>'); lista = null; } }
        lines.forEach(function (ln) {
            var m;
            if (!ln.trim()) { cerrarParr(); cerrarLista(); return; }
            if ((m = ln.match(/^\s*(#{1,3})\s+(.*)$/))) {
                cerrarParr(); cerrarLista();
                var h = m[1].length + 2;
                html.push('<h' + h + '>' + inline(m[2]) + '</h' + h + '>');
                return;
            }
            if ((m = ln.match(/^\s*[-*•]\s+(.*)$/))) {
                cerrarParr();
                if (lista !== 'ul') { cerrarLista(); html.push('<ul>'); lista = 'ul'; }
                html.push('<li>' + inline(m[1]) + '</li>');
                return;
            }
            cerrarLista();
            parr.push(inline(ln));
        });
        cerrarParr(); cerrarLista();
        return html.join('');
    }

    function autoAlto(el) { el.style.height = 'auto'; el.style.height = el.scrollHeight + 'px'; }

    function pintarVista() {
        view.innerHTML = render(text.value) || '<p class="tkd-vacio">Sin nota todavía. Tocá “Editar” para escribir.</p>';
    }

    function modoEditar(on) {
        text.hidden = !on;
        view.hidden = on;
        btnEdit.textContent = on ? 'Listo' : 'Editar';
        if (on) { autoAlto(text); text.focus(); } else { pintarVista(); }
    }

    function guardarCampo(src, valor) {
        if (!src || !actual) return Promise.resolve(true);
        src.value = valor;
        status.textContent = 'Guardando…';
        return actual.tabla.__enviar(src).then(function (ok) {
            status.textContent = ok ? 'Guardado' : 'No se pudo guardar';
            setTimeout(function () { if (status.textContent === 'Guardado') status.textContent = ''; }, 1400);
            return ok;
        });
    }

    function abrir(fila) {
        var tabla = fila.closest('.tk-table');
        var nota = tabla.querySelector('.tk-nota-row[data-for="' + fila.dataset.id + '"] .tk-nota');
        actual = { fila: fila, tabla: tabla, srcTitulo: fila.querySelector('.tk-titulo-src'), srcNota: nota };
        tit.value = actual.srcTitulo ? actual.srcTitulo.value : '';
        text.value = nota ? nota.value : '';
        var partes = [];
        var cat = fila.querySelector('[data-campo="categoria"]');
        if (cat && cat.value) partes.push(cat.value);
        var fmt = fila.querySelector('[data-campo="formato"]');
        if (fmt && fmt.value) partes.push(fmt.value);
        var est = fila.querySelector('[data-campo="estado"]');
        if (est) partes.push(est.value);
        meta.textContent = partes.join(' · ');
        status.textContent = '';
        ultimoFoco = document.activeElement;
        drawer.hidden = false;
        drawer.dispatchEvent(new CustomEvent('tkd:abrir', { detail: { fila: fila, tabla: tabla } }));
        requestAnimationFrame(function () { drawer.classList.add('is-open'); autoAlto(tit); });
        var vacia = !text.value.trim();
        var esTarea = tabla.dataset.base === '/admin/tareas/';
        modoEditar(vacia && !esTarea);
        if (esTarea) { var inp = drawer.querySelector('.tkd-subs-new input'); if (inp) setTimeout(function () { inp.focus(); }, 60); }
        else if (!vacia) view.focus();
    }

    function guardarPendiente() {
        if (!actual) return;
        if (actual.srcTitulo) {
            var v = tit.value.trim();
            if (v && v !== actual.srcTitulo.value) guardarCampo(actual.srcTitulo, v);
        }
        if (actual.srcNota && text.value !== actual.srcNota.value) guardarCampo(actual.srcNota, text.value);
    }

    function cerrar() {
        if (!actual) return;
        guardarPendiente();
        drawer.classList.remove('is-open');
        setTimeout(function () { drawer.hidden = true; }, 220);
        actual = null;
        if (ultimoFoco && ultimoFoco.focus) ultimoFoco.focus();
    }

    document.addEventListener('click', function (e) {
        var b = e.target.closest('.tk-open');
        if (b) { e.preventDefault(); abrir(b.closest('tr.tk-row')); return; }
        if (e.target.closest('[data-tkd-close]')) cerrar();
    });
    btnEdit.addEventListener('click', function () {
        if (!text.hidden) {
            if (actual && actual.srcNota && text.value !== actual.srcNota.value) guardarCampo(actual.srcNota, text.value);
            modoEditar(false);
        } else {
            modoEditar(true);
        }
    });
    view.addEventListener('dblclick', function () { modoEditar(true); });
    text.addEventListener('input', function () { autoAlto(text); });
    text.addEventListener('blur', function () {
        if (actual && actual.srcNota && text.value !== actual.srcNota.value) guardarCampo(actual.srcNota, text.value);
    });
    tit.addEventListener('input', function () { autoAlto(tit); });
    tit.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); tit.blur(); } });
    tit.addEventListener('blur', function () {
        if (!actual || !actual.srcTitulo) return;
        var v = tit.value.trim();
        if (!v) { tit.value = actual.srcTitulo.value; return; }
        if (v !== actual.srcTitulo.value) guardarCampo(actual.srcTitulo, v);
    });
    document.addEventListener('keydown', function (e) {
        if (drawer.hidden) return;
        if (e.key === 'Escape') { e.preventDefault(); cerrar(); }
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !text.hidden) { btnEdit.click(); }
    });
})();

/* Subtareas: checklist dentro del panel lateral (solo tareas, no ideas). */
(function () {
    'use strict';
    var drawer = document.getElementById('tk-drawer');
    if (!drawer) return;
    var sec = drawer.querySelector('.tkd-subs');
    if (!sec) return;
    var lista = sec.querySelector('.tkd-subs-list');
    var prog = sec.querySelector('.tkd-subs-prog');
    var fill = sec.querySelector('.tkd-subs-track span');
    var nueva = sec.querySelector('.tkd-subs-new input');
    var tareaId = null;
    var fila = null;

    function api(url, body) {
        return fetch(url, {
            method: body ? 'POST' : 'GET',
            headers: body ? { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' } : { 'X-Requested-With': 'XMLHttpRequest' },
            body: body ? JSON.stringify(body) : undefined
        }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok && d.ok, data: d }; }); });
    }

    function badge(d) {
        if (!fila) return;
        var b = fila.querySelector('.tk-sub-badge');
        if (!b) return;
        b.hidden = !d.total;
        b.textContent = d.total ? (d.hechas + '/' + d.total) : '';
        b.classList.toggle('is-done', d.total > 0 && d.hechas === d.total);
    }

    function pintar(d) {
        lista.innerHTML = '';
        d.subtareas.forEach(function (s) {
            var li = document.createElement('li');
            li.className = 'tkd-sub' + (s.hecha ? ' is-hecha' : '');
            li.dataset.id = s.id;
            var cb = document.createElement('input');
            cb.type = 'checkbox'; cb.className = 'tkd-sub-cb'; cb.checked = s.hecha;
            cb.setAttribute('aria-label', 'Hecha');
            var tx = document.createElement('input');
            tx.type = 'text'; tx.className = 'tkd-sub-txt'; tx.value = s.texto; tx.maxLength = 300;
            tx.dataset.ultimo = s.texto;
            tx.setAttribute('aria-label', 'Subtarea');
            var del = document.createElement('button');
            del.type = 'button'; del.className = 'tkd-sub-del'; del.innerHTML = '&times;';
            del.title = 'Borrar subtarea'; del.setAttribute('aria-label', 'Borrar subtarea');
            li.appendChild(cb); li.appendChild(tx); li.appendChild(del);
            lista.appendChild(li);
        });
        prog.textContent = d.total ? (d.hechas + ' de ' + d.total) : '';
        fill.style.width = d.total ? Math.round(d.hechas / d.total * 100) + '%' : '0';
        sec.classList.toggle('is-vacia', !d.total);
        badge(d);
    }

    function aplicar(res) {
        if (res.ok) pintar(res.data);
        else alert((res.data && res.data.error) || 'No se pudo guardar.');
    }

    // El panel avisa cuando se abre (evento propio) con la fila y la tabla.
    drawer.addEventListener('tkd:abrir', function (e) {
        var esTarea = e.detail.tabla && e.detail.tabla.dataset.base === '/admin/tareas/';
        sec.hidden = !esTarea;
        tareaId = esTarea ? e.detail.fila.dataset.id : null;
        fila = esTarea ? e.detail.fila : null;
        lista.innerHTML = ''; prog.textContent = ''; fill.style.width = '0'; nueva.value = '';
        if (esTarea) api('/admin/tareas/' + tareaId + '/subtareas').then(aplicar);
    });

    nueva.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        var v = nueva.value.trim();
        if (!v || !tareaId) return;
        nueva.value = '';
        api('/admin/tareas/' + tareaId + '/subtareas', { texto: v }).then(aplicar);
    });

    lista.addEventListener('change', function (e) {
        if (!e.target.classList.contains('tkd-sub-cb')) return;
        var li = e.target.closest('li');
        li.classList.toggle('is-hecha', e.target.checked);
        api('/admin/subtareas/' + li.dataset.id, { hecha: e.target.checked }).then(aplicar);
    });
    lista.addEventListener('click', function (e) {
        var del = e.target.closest('.tkd-sub-del');
        if (!del) return;
        api('/admin/subtareas/' + del.closest('li').dataset.id, { borrar: true }).then(aplicar);
    });
    function guardarTexto(tx) {
        var v = tx.value.trim();
        if (!v) { tx.value = tx.dataset.ultimo; return; }
        if (v === tx.dataset.ultimo) return;
        tx.dataset.ultimo = v;
        api('/admin/subtareas/' + tx.closest('li').dataset.id, { texto: v }).then(function (res) {
            if (!res.ok) aplicar(res); else badge(res.data);
        });
    }
    lista.addEventListener('focusout', function (e) {
        if (e.target.classList.contains('tkd-sub-txt')) guardarTexto(e.target);
    });
    lista.addEventListener('keydown', function (e) {
        if (!e.target.classList.contains('tkd-sub-txt')) return;
        if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); nueva.focus(); }
        if (e.key === 'Escape') { e.stopPropagation(); e.target.value = e.target.dataset.ultimo; e.target.blur(); }
    });
})();
