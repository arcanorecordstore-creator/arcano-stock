/* Estrategia -> Preguntar: "Preguntale a tus datos". Manda la pregunta al servidor,
   muestra "pensando" mientras la IA contesta y agrega la respuesta arriba del historial.
   La respuesta llega como HTML ya escapado por el servidor. */
(function () {
    'use strict';
    var raiz = document.getElementById('pq');
    if (!raiz) return;
    var form = document.getElementById('pq-form');
    var input = document.getElementById('pq-input');
    var btn = document.getElementById('pq-btn');
    var uso = document.getElementById('pq-uso');
    var errorEl = document.getElementById('pq-error');
    var lista = document.getElementById('pq-lista');
    var vacio = document.getElementById('pq-vacio');
    var urlPreguntar = raiz.dataset.url;
    var urlBorrar = raiz.dataset.borrar;   // .../preguntar/0/borrar
    var ocupado = false;

    function mostrarError(txt) {
        errorEl.textContent = txt || '';
        errorEl.hidden = !txt;
    }

    function pintarUso(usadas, tope) {
        if (typeof usadas !== 'number' || !uso) return;
        uso.textContent = usadas + ' de ' + (tope || raiz.dataset.tope) + ' preguntas en la última hora';
    }

    function setOcupado(si) {
        ocupado = si;
        btn.disabled = si;
        input.readOnly = si;
        raiz.querySelectorAll('.pq-sug').forEach(function (b) { b.disabled = si; });
        btn.textContent = si ? 'Pensando…' : 'Preguntar';
    }

    function crearItem(pregunta) {
        var det = document.createElement('details');
        det.className = 'pq-item';
        det.open = true;
        var sum = document.createElement('summary');
        sum.className = 'pq-item-head';
        var q = document.createElement('span');
        q.className = 'pq-q';
        q.textContent = pregunta;
        var when = document.createElement('span');
        when.className = 'pq-when';
        sum.appendChild(q);
        sum.appendChild(when);
        det.appendChild(sum);
        var a = document.createElement('div');
        a.className = 'pq-a';
        det.appendChild(a);
        return det;
    }

    // Si el item quedo fuera de la pantalla, llevarlo a la vista (suave, sin saltos)
    function verItem(el) {
        var r = el.getBoundingClientRect();
        var alto = window.innerHeight || document.documentElement.clientHeight;
        if (r.top < 0 || r.top > alto - 140) {
            var suave = !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
            el.scrollIntoView({ behavior: suave ? 'smooth' : 'auto', block: 'start' });
        }
    }

    function botonBorrar() {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'pq-del';
        b.setAttribute('aria-label', 'Borrar esta pregunta del historial');
        b.title = 'Borrar del historial';
        b.innerHTML = '&times;';
        return b;
    }

    function preguntar(texto) {
        texto = (texto || '').replace(/\s+/g, ' ').trim();
        if (ocupado) return;
        if (texto.length < 3) {
            mostrarError('Escribí una pregunta.');
            input.focus();
            return;
        }
        mostrarError('');
        setOcupado(true);
        // Cerrar las respuestas anteriores: la nueva queda arriba y abierta
        lista.querySelectorAll('.pq-item[open]').forEach(function (d) { d.open = false; });
        var item = crearItem(texto);
        item.classList.add('is-pensando');
        var cuerpo = item.querySelector('.pq-a');
        var t0 = Date.now();
        cuerpo.innerHTML = '<p class="pq-pensando" aria-live="polite"><span class="pq-dots" aria-hidden="true"><i></i><i></i><i></i></span> <span class="pq-pensando-t">Mirando tus números…</span></p>';
        var tTxt = cuerpo.querySelector('.pq-pensando-t');
        var reloj = setInterval(function () {
            var s = Math.round((Date.now() - t0) / 1000);
            tTxt.textContent = s < 8 ? 'Mirando tus números…' : 'Pensando… ' + s + ' s (puede tardar hasta un minuto)';
        }, 1000);
        lista.insertBefore(item, lista.firstChild);
        vacio.hidden = true;
        // En el celular: cerrar el teclado y mostrar la respuesta (queda abajo del campo)
        if (window.matchMedia && window.matchMedia('(hover: none)').matches) input.blur();
        verItem(item);

        var ctrl = ('AbortController' in window) ? new AbortController() : null;
        var corte = setTimeout(function () { if (ctrl) ctrl.abort(); }, 125000);

        fetch(urlPreguntar, {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ pregunta: texto }),
            signal: ctrl ? ctrl.signal : undefined
        }).then(function (r) {
            return r.json().catch(function () { return { ok: false }; }).then(function (d) {
                return { ok: r.ok && d.ok, status: r.status, data: d };
            });
        }).then(function (res) {
            var d = res.data || {};
            pintarUso(d.usadas, d.tope);
            if (!res.ok) {
                item.remove();
                if (!lista.querySelector('.pq-item')) vacio.hidden = false;
                mostrarError(d.error || (res.status === 401 || res.status === 302
                    ? 'Se cerró tu sesión. Recargá la página y volvé a entrar.'
                    : 'No se pudo responder. Probá de nuevo.'));
                input.value = texto;
                return;
            }
            var it = d.item || {};
            item.classList.remove('is-pensando');
            item.dataset.id = it.id;
            item.querySelector('.pq-when').textContent = it.fecha_txt || '';
            item.querySelector('.pq-item-head').appendChild(botonBorrar());
            cuerpo.innerHTML = it.html || '';
            input.value = '';
        }).catch(function () {
            item.remove();
            if (!lista.querySelector('.pq-item')) vacio.hidden = false;
            mostrarError('Se cortó la conexión o la IA tardó demasiado. Probá de nuevo.');
            input.value = texto;
        }).then(function () {
            clearInterval(reloj);
            clearTimeout(corte);
            setOcupado(false);
        });
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        preguntar(input.value);
    });
    // Enter manda; Shift+Enter hace un salto de linea
    input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            preguntar(input.value);
        }
    });
    raiz.querySelectorAll('.pq-sug').forEach(function (b) {
        b.addEventListener('click', function () {
            input.value = b.dataset.pregunta || b.textContent;
            preguntar(input.value);
        });
    });

    // Borrar una pregunta del historial
    lista.addEventListener('click', function (e) {
        var b = e.target.closest('.pq-del');
        if (!b) return;
        e.preventDefault();          // que no abra/cierre el desplegable
        e.stopPropagation();
        var item = b.closest('.pq-item');
        var id = item && item.dataset.id;
        if (!id || !window.confirm('¿Borrar esta pregunta del historial?')) return;
        b.disabled = true;
        fetch(urlBorrar.replace('/0/', '/' + encodeURIComponent(id) + '/'), {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: '{}'
        }).then(function (r) {
            if (!r.ok) throw new Error('http ' + r.status);
            item.remove();
            if (!lista.querySelector('.pq-item')) vacio.hidden = false;
        }).catch(function () {
            b.disabled = false;
            mostrarError('No se pudo borrar. Probá de nuevo.');
        });
    });
})();
