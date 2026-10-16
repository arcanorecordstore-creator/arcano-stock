/* Estrategia -> Notas (v6.14): lista + panel para escribir. Se guarda sola mientras
   escribis. Cuentas en linea: una linea que termina en "=" + Enter completa el resultado
   (formato uruguayo: punto de miles, coma decimal). */
(function () {
    'use strict';
    var tabla = document.getElementById('nt-table');
    var drawer = document.getElementById('nt-drawer');
    if (!tabla || !drawer) return;
    var tbody = tabla.querySelector('tbody');
    var inTit = document.getElementById('nt-titulo');
    var inTxt = document.getElementById('nt-texto');
    var status = document.getElementById('nt-status');
    var btnFijar = document.getElementById('nt-fijar');
    var btnBorrar = document.getElementById('nt-borrar');
    var buscar = document.getElementById('nt-buscar');
    var vacio = document.getElementById('nt-vacio');
    var sinRes = document.getElementById('nt-sin-res');
    var actual = null;        // la fila abierta
    var timer = null;
    var ultimoFoco = null;

    // ---------- fechas ----------
    function fechaCorta(iso) {
        if (!iso) return '';
        var d = new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z');
        if (isNaN(d)) return '';
        var hoy = new Date(), ayer = new Date(); ayer.setDate(hoy.getDate() - 1);
        var hm = ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
        if (d.toDateString() === hoy.toDateString()) return 'hoy ' + hm;
        if (d.toDateString() === ayer.toDateString()) return 'ayer ' + hm;
        return ('0' + d.getDate()).slice(-2) + '/' + ('0' + (d.getMonth() + 1)).slice(-2) + '/' + d.getFullYear();
    }
    function pintarFechas() {
        tabla.querySelectorAll('.nt-c-fecha[data-iso]').forEach(function (td) {
            var f = fechaCorta(td.dataset.iso);
            if (f) td.textContent = f;
        });
    }

    // ---------- servidor ----------
    function post(url, datos) {
        return fetch(url, {
            method: 'POST', credentials: 'same-origin', keepalive: true,
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify(datos || {})
        }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok && d.ok, data: d }; }); });
    }

    // ---------- filas ----------
    function filaHtml(n) {
        var tr = document.createElement('tr');
        tr.className = 'nt-row';
        tr.tabIndex = 0;
        tr.innerHTML = '<td class="nt-c-pin"></td>' +
            '<td class="nt-c-nota"><span class="nt-tit"></span><span class="nt-prev" hidden></span></td>' +
            '<td class="nt-c-fecha"></td>';
        actualizarFila(tr, n);
        return tr;
    }
    function actualizarFila(tr, n) {
        tr.dataset.id = n.id;
        tr.dataset.titulo = n.titulo || '';
        tr.dataset.texto = n.texto || '';
        tr.dataset.fijada = n.fijada ? '1' : '0';
        tr.classList.toggle('is-fijada', !!n.fijada);
        tr.querySelector('.nt-tit').textContent = n.titulo_mostrar || 'Nota sin título';
        var prev = tr.querySelector('.nt-prev');
        prev.textContent = n.vista_previa || '';
        prev.hidden = !n.vista_previa;
        var pin = tr.querySelector('.nt-c-pin');
        pin.innerHTML = n.fijada ? '<span class="nt-pin" title="Fijada arriba" aria-label="Fijada arriba"></span>' : '';
        var f = tr.querySelector('.nt-c-fecha');
        f.dataset.iso = n.actualizada || '';
        f.textContent = fechaCorta(n.actualizada);
    }
    function hayFilas() {
        var n = tbody.querySelectorAll('tr.nt-row').length;
        vacio.hidden = n > 0;
        return n;
    }

    // ---------- guardar ----------
    function guardar() {
        clearTimeout(timer);
        if (!actual) return Promise.resolve(true);
        var tr = actual;
        var datos = { titulo: inTit.value, texto: inTxt.value };
        if (datos.titulo === (tr.dataset.titulo || '') && datos.texto === (tr.dataset.texto || '')) {
            return Promise.resolve(true);
        }
        status.textContent = 'Guardando…';
        tr.dataset.titulo = datos.titulo;     // evita guardar dos veces lo mismo
        tr.dataset.texto = datos.texto;
        return post('/admin/notas/' + tr.dataset.id, datos).then(function (res) {
            if (res.ok) {
                actualizarFila(tr, res.data.nota);
                if (actual === tr) {
                    status.textContent = 'Guardado';
                    setTimeout(function () { if (status.textContent === 'Guardado') status.textContent = ''; }, 1400);
                }
            } else {
                tr.dataset.titulo = ''; tr.dataset.texto = '';   // reintentar en el proximo cambio
                status.textContent = 'No se pudo guardar. Revisá la conexión.';
            }
            return res.ok;
        }).catch(function () {
            tr.dataset.titulo = ''; tr.dataset.texto = '';
            status.textContent = 'No se pudo guardar. Revisá la conexión.';
            return false;
        });
    }
    function programarGuardado() {
        clearTimeout(timer);
        status.textContent = '';
        timer = setTimeout(guardar, 700);
    }

    // ---------- panel ----------
    function autoAlto() {
        inTxt.style.height = 'auto';
        inTxt.style.height = Math.max(inTxt.scrollHeight, 260) + 'px';
    }
    function pintarFijar() {
        var on = actual && actual.dataset.fijada === '1';
        btnFijar.setAttribute('aria-pressed', on ? 'true' : 'false');
        btnFijar.textContent = on ? 'Fijada arriba' : 'Fijar arriba';
        btnFijar.classList.toggle('is-on', !!on);
    }
    function abrir(tr, foco) {
        if (actual && actual !== tr) guardar();
        actual = tr;
        inTit.value = tr.dataset.titulo || '';
        inTxt.value = tr.dataset.texto || '';
        document.getElementById('nt-meta').textContent = 'Nota · ' + (fechaCorta(tr.querySelector('.nt-c-fecha').dataset.iso) || 'nueva');
        status.textContent = '';
        pintarFijar();
        ultimoFoco = document.activeElement;
        drawer.hidden = false;
        requestAnimationFrame(function () {
            drawer.classList.add('is-open');
            autoAlto();
            (foco === 'titulo' ? inTit : inTxt).focus({ preventScroll: true });
        });
    }
    function cerrar() {
        if (!actual) return;
        var tr = actual;
        guardar().then(function () {
            // Nota que quedo vacia (la creaste y no escribiste nada): se borra sola
            if (!(tr.dataset.titulo || '').trim() && !(tr.dataset.texto || '').trim()) {
                post('/admin/notas/' + tr.dataset.id + '/borrar').then(function (res) {
                    if (res.ok && tr.parentNode) { tr.parentNode.removeChild(tr); hayFilas(); }
                });
            }
        });
        drawer.classList.remove('is-open');
        setTimeout(function () { drawer.hidden = true; }, 220);
        actual = null;
        if (ultimoFoco && ultimoFoco.focus) ultimoFoco.focus({ preventScroll: true });
    }

    // ---------- cuentas en linea ----------
    function aNumero(tok) {
        var t = tok;
        if (t.indexOf(',') !== -1) t = t.replace(/\./g, '').replace(',', '.');   // 1.400,50
        else if (/^\d{1,3}(\.\d{3})+$/.test(t)) t = t.replace(/\./g, '');          // 1.400 / 1.250.000
        return parseFloat(t);
    }
    // Parser chico (sin eval): + - x / ( ) y %. "1.800 - 10%" = 1.620 (como una calculadora).
    function calcular(expr) {
        var s = expr.replace(/[x×X*]/g, '*').replace(/÷/g, '/').replace(/\$/g, '').replace(/\s+/g, '');
        var toks = s.match(/\d[\d.,]*|[-+*/()%]/g);
        if (!toks || toks.join('') !== s) return null;
        if (!/[-+*/%]/.test(s.replace(/^-/, ''))) return null;   // sin operacion no hay cuenta
        var i = 0;
        function peek() { return toks[i]; }
        function next() { return toks[i++]; }
        function factor() {
            var t = next();
            if (t === '-') return { v: -factor().v, pct: false };
            if (t === '+') return factor();
            var v;
            if (t === '(') { v = suma(); if (next() !== ')') throw new Error('par'); }
            else if (t && /^\d/.test(t)) v = aNumero(t);
            else throw new Error('tok');
            var pct = false;
            while (peek() === '%') { next(); v = v / 100; pct = true; }
            return { v: v, pct: pct };
        }
        function prod() {
            var f = factor(), v = f.v, solo = true;
            while (peek() === '*' || peek() === '/') {
                var op = next(), w = factor().v;
                v = op === '*' ? v * w : v / w;
                solo = false;
            }
            return { v: v, pct: f.pct && solo };
        }
        function suma() {
            var v = prod().v;
            while (peek() === '+' || peek() === '-') {
                var op = next(), t = prod();
                var w = t.pct ? v * t.v : t.v;    // 1.800 - 10% -> 1.800 - 180
                v = op === '+' ? v + w : v - w;
            }
            return v;
        }
        try {
            var r = suma();
            return (i === toks.length && isFinite(r)) ? r : null;
        } catch (e) { return null; }
    }
    function formatear(n) {
        var r = Math.round(n * 100) / 100;
        return r.toLocaleString('es-UY', { maximumFractionDigits: 2 });
    }
    inTxt.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' || e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
        var v = inTxt.value, pos = inTxt.selectionStart;
        if (pos !== inTxt.selectionEnd) return;
        var ini = v.lastIndexOf('\n', pos - 1) + 1;
        var fin = v.indexOf('\n', pos); if (fin === -1) fin = v.length;
        if (v.slice(pos, fin).trim()) return;                  // el cursor no esta al final de la linea
        var linea = v.slice(ini, pos);
        var m = linea.match(/([-+(]*\s*\$?\s*\d[\d.,\s+\-*/xX×÷()%$]*)=\s*$/);
        if (!m) return;
        var r = calcular(m[1]);
        if (r === null) return;
        e.preventDefault();
        var nueva = linea.replace(/=\s*$/, '= ' + formatear(r));
        inTxt.value = v.slice(0, ini) + nueva + '\n' + v.slice(fin);
        var c = ini + nueva.length + 1;
        inTxt.setSelectionRange(c, c);
        autoAlto();
        programarGuardado();
    });

    // ---------- eventos ----------
    inTxt.addEventListener('input', function () { autoAlto(); programarGuardado(); });
    inTit.addEventListener('input', programarGuardado);
    inTit.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); inTxt.focus(); }
    });
    tbody.addEventListener('click', function (e) {
        var tr = e.target.closest('tr.nt-row');
        if (tr) abrir(tr);
    });
    tbody.addEventListener('keydown', function (e) {
        var tr = e.target.closest('tr.nt-row');
        if (tr && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); abrir(tr); }
    });
    drawer.addEventListener('click', function (e) {
        if (e.target.closest('[data-nt-close]')) cerrar();
    });
    document.addEventListener('keydown', function (e) {
        if (!drawer.hidden && e.key === 'Escape') { e.preventDefault(); cerrar(); }
    });
    btnFijar.addEventListener('click', function () {
        if (!actual) return;
        var tr = actual, on = tr.dataset.fijada !== '1';
        guardar().then(function () {
            return post('/admin/notas/' + tr.dataset.id, { fijada: on });
        }).then(function (res) {
            if (!res || !res.ok) { status.textContent = 'No se pudo guardar.'; return; }
            actualizarFila(tr, res.data.nota);
            // fijadas primero: mover la fila
            if (on) tbody.insertBefore(tr, tbody.firstChild);
            pintarFijar();
        });
    });
    btnBorrar.addEventListener('click', function () {
        if (!actual) return;
        var tr = actual;
        var nombre = tr.querySelector('.nt-tit').textContent;
        if (!window.confirm('¿Borrar la nota «' + nombre + '»? No se puede recuperar.')) return;
        clearTimeout(timer);
        post('/admin/notas/' + tr.dataset.id + '/borrar').then(function (res) {
            if (!res.ok) { status.textContent = 'No se pudo borrar.'; return; }
            actual = null;
            drawer.classList.remove('is-open');
            setTimeout(function () { drawer.hidden = true; }, 220);
            if (tr.parentNode) tr.parentNode.removeChild(tr);
            hayFilas();
        });
    });
    document.getElementById('nt-nueva').addEventListener('click', function () {
        var btn = this;
        btn.disabled = true;
        post('/admin/notas/nueva').then(function (res) {
            btn.disabled = false;
            if (!res.ok) { alert('No se pudo crear la nota. Probá de nuevo.'); return; }
            var tr = filaHtml(res.data.nota);
            // va debajo de las fijadas
            var primeraSuelta = tbody.querySelector('tr.nt-row:not(.is-fijada)');
            tbody.insertBefore(tr, primeraSuelta);
            hayFilas();
            if (buscar.value) { buscar.value = ''; filtrar(); }
            abrir(tr);
        }).catch(function () { btn.disabled = false; });
    });

    // ---------- buscar ----------
    function norm(t) { return (t || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, ''); }
    function filtrar() {
        var q = norm(buscar.value.trim());
        var vistas = 0;
        tbody.querySelectorAll('tr.nt-row').forEach(function (tr) {
            var ok = !q || norm(tr.dataset.titulo + ' ' + tr.dataset.texto + ' ' + tr.querySelector('.nt-tit').textContent).indexOf(q) !== -1;
            tr.hidden = !ok;
            if (ok) vistas++;
        });
        sinRes.hidden = !(q && vistas === 0 && hayFilas() > 0);
    }
    buscar.addEventListener('input', filtrar);

    // Guardar si cerras la pestaña con algo sin guardar
    window.addEventListener('beforeunload', function () { if (actual) guardar(); });

    pintarFechas();
    var abrirId = tabla.dataset.abrir;
    if (abrirId) {
        var tr0 = tbody.querySelector('tr.nt-row[data-id="' + abrirId + '"]');
        if (tr0) abrir(tr0);
    }
})();
