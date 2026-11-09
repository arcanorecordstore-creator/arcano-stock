/* Modo scroll de la tienda (estilo Wax Merchant): un disco por pantalla.
   - Arriba/abajo (scroll con "enganche") cambia de disco; el disco que queda en
     pantalla suena solo (despues del primer toque: el celular no deja sonar antes).
   - A los costados (deslizar o flechas) cambia de track: carrusel con scroll nativo.
   La musica la toca el reproductor de siempre (window.ArcanoPlayer, player.js): aca
   solo se le dice que tocar y se pinta lo que avisa (evento 'arcano:player'). */
(function () {
    'use strict';

    var estado = null;   // datos del feed activo (null si la pagina no es el modo scroll)
    var CLAVE_AYUDA = 'arcano-feed-ayuda-vista';

    function puedeSonar() {
        // "El usuario ya toco la pagina": sin eso el navegador bloquea el sonido
        var ua = navigator.userActivation;
        return estado && (estado.tocado || (ua && ua.hasBeenActive));
    }

    function temasDe(card) {
        if (!card._temas) {
            try { card._temas = JSON.parse(card.dataset.tracks || '[]'); } catch (e) { card._temas = []; }
        }
        return card._temas;
    }
    function indiceDe(card) { return parseInt(card.dataset.i || '0', 10) || 0; }

    function esDuenio(card) {
        var P = window.ArcanoPlayer;
        return !!(P && P.clave && P.clave() === card.dataset.clave);
    }

    function fmt(s) { s = Math.max(0, Math.floor(s || 0)); var m = Math.floor(s / 60), r = s % 60; return m + ':' + (r < 10 ? '0' : '') + r; }
    function pintarTiempos(card, cur, dur) {
        var c = card.querySelector('[data-feed-cur]'), d = card.querySelector('[data-feed-dur]');
        if (c) c.textContent = fmt(cur);
        if (d) d.textContent = dur > 0 ? fmt(dur) : '–:––';
    }
    function pintarTema(card, i) {
        var temas = temasDe(card);
        var t = temas[i]; if (!t) return;
        var num = card.querySelector('[data-feed-num]');
        if (num) num.textContent = 'Track ' + (i + 1) + ' / ' + temas.length;
        var el = card.querySelector('[data-feed-tema]');
        var txt = (t.artist ? t.artist + ' - ' : '') + (t.title || '');
        if (el && el.textContent !== txt) {
            el.textContent = txt;
            el.classList.remove('entra'); void el.offsetWidth; el.classList.add('entra');
        }
        card.querySelectorAll('.feed-punto').forEach(function (p, k) { p.classList.toggle('on', k === i); });
        var prev = card.querySelector('[data-feed-prev]'), next = card.querySelector('[data-feed-next]');
        if (prev) prev.disabled = (i <= 0);
        if (next) next.disabled = (i >= temas.length - 1);
        irAPista(card, i, true);
    }
    function pintarPlay(card, sonando) {
        var b = card.querySelector('[data-feed-play]'); if (!b) return;
        var pl = b.querySelector('.feed-ico-play'), pa = b.querySelector('.feed-ico-pausa');
        if (pl) pl.toggleAttribute('hidden', !!sonando);
        if (pa) pa.toggleAttribute('hidden', !sonando);
        b.setAttribute('aria-label', sonando ? 'Pausar' : 'Reproducir');
        card.classList.toggle('is-playing', !!sonando);
    }
    function pintarAvance(card, pct) {
        var r = card.querySelector('[data-feed-relleno]');
        if (r) r.style.width = Math.max(0, Math.min(100, pct)) + '%';
        var a = card.querySelector('[data-feed-avance]');
        if (a) a.setAttribute('aria-valuenow', Math.round(pct));
    }

    // Tocar el tema i de un disco (o el que quedo elegido)
    function tocar(card, i) {
        var temas = temasDe(card); if (!temas.length || !window.ArcanoPlayer) return;
        if (typeof i !== 'number') i = indiceDe(card);
        i = Math.max(0, Math.min(i, temas.length - 1));
        card.dataset.i = String(i);
        pintarTema(card, i);
        if (esDuenio(card) && window.ArcanoPlayer.irA) window.ArcanoPlayer.irA(i);
        else window.ArcanoPlayer.reproducir(JSON.parse(JSON.stringify(temas)), i, card.dataset.cover || '', card.dataset.clave);
    }
    // Carrusel de tracks: cada track es una hoja de .feed-pistas y se pasa con el
    // scroll nativo (sigue al dedo y engancha como un carrusel de verdad). Las flechas,
    // el teclado y el reproductor mueven ese mismo scroll.
    var reducirMov = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    function pistasDe(card) { return card.querySelector('[data-feed-pistas]'); }
    function irAPista(card, i, suave) {
        var sc = pistasDe(card); if (!sc || sc._usuario) return;
        var hoja = sc.children[i]; if (!hoja) return;
        if (Math.abs(sc.scrollLeft - hoja.offsetLeft) < 2) return;
        sc.scrollTo({ left: hoja.offsetLeft, behavior: (suave && !reducirMov) ? 'smooth' : 'auto' });
    }
    function pistaVisible(sc) {
        var paso = sc.children[1] ? sc.children[1].offsetLeft - sc.children[0].offsetLeft : sc.clientWidth;
        return Math.max(0, Math.min(sc.children.length - 1, Math.round(sc.scrollLeft / (paso || 1))));
    }
    function elegirTema(card, i) {
        if (i === indiceDe(card)) return;
        if (puedeSonar()) tocar(card, i);
        else { card.dataset.i = String(i); pintarTema(card, i); }
    }
    function cambiarTema(card, paso) {
        var temas = temasDe(card); if (temas.length < 2) return;
        var i = indiceDe(card) + paso;
        if (i < 0 || i >= temas.length) return;
        var sc = pistasDe(card); if (sc) sc._usuario = false;   // flechas / teclado
        elegirTema(card, i);
    }
    // Cuando el carrusel termina de moverse (y el dedo ya se solto), suena esa hoja.
    // Mientras la persona lo mueve, el codigo NO toca el scroll (si no, pelea con el
    // dedo y el enganche y parece trabado); y el reproductor arranca recien cuando
    // el carrusel quedo quieto, para no frenar la animacion.
    function escucharPistas(card) {
        var sc = pistasDe(card); if (!sc) return;
        var espera = null, dedo = false;
        function asentar() {
            espera = null;
            if (dedo) return;
            sc._usuario = false;
            var i = pistaVisible(sc);
            if (i === indiceDe(card)) return;
            card.dataset.i = String(i);
            pintarTema(card, i);
            if (puedeSonar()) {
                var pedir = function () { if (indiceDe(card) === i) tocar(card, i); };
                if (window.requestAnimationFrame) requestAnimationFrame(function () { setTimeout(pedir, 30); });
                else setTimeout(pedir, 30);
            }
        }
        var tocando = function () { dedo = true; sc._usuario = true; clearTimeout(espera); espera = null; };
        sc.addEventListener('touchstart', tocando, { passive: true });
        sc.addEventListener('pointerdown', function (e) { if (e.pointerType !== 'mouse') tocando(); }, { passive: true });
        var ultimoScroll = 0;
        sc.addEventListener('scroll', function () { ultimoScroll = Date.now(); }, { passive: true });
        // Si solo toco (sin deslizar), el carrusel vuelve a seguir al reproductor
        var soltar = function () {
            dedo = false;
            setTimeout(function () {
                var hoja = sc.children[pistaVisible(sc)];
                if (!dedo && Date.now() - ultimoScroll > 150 && hoja && Math.abs(sc.scrollLeft - hoja.offsetLeft) < 2) sc._usuario = false;
            }, 220);
        };
        sc.addEventListener('touchend', soltar, { passive: true });
        sc.addEventListener('touchcancel', soltar, { passive: true });
        sc.addEventListener('wheel', function () { sc._usuario = true; }, { passive: true });
        if ('onscrollend' in window) {
            sc.addEventListener('scrollend', function () { if (!dedo) asentar(); });
        } else {
            // Navegadores sin "scrollend": esperar a que deje de moverse
            sc.addEventListener('scroll', function () {
                clearTimeout(espera);
                espera = setTimeout(function () { if (!dedo) asentar(); else espera = null; }, 180);
            }, { passive: true });
            sc.addEventListener('touchend', function () { clearTimeout(espera); espera = setTimeout(asentar, 220); }, { passive: true });
        }
    }

    // El disco que quedo en pantalla
    function activar(card) {
        if (!estado || estado.activa === card) return;
        estado.activa = card;
        try { sessionStorage.setItem('arcano-feed-pos', (estado.raiz.dataset.semilla || '') + ':' + card.dataset.clave); } catch (e) {}
        estado.cards.forEach(function (c) { c.classList.toggle('is-activa', c === card); });
        if (!temasDe(card).length) {            // disco sin temas: que deje de sonar el anterior
            if (window.ArcanoPlayer && window.ArcanoPlayer.clave && window.ArcanoPlayer.clave()) window.ArcanoPlayer.pausar();
            return;
        }
        if (esDuenio(card)) return;
        if (puedeSonar()) tocar(card, indiceDe(card));
        else mostrarArrancar();
    }

    function mostrarArrancar() {
        if (!estado || !estado.botonArrancar) return;
        var ayuda = estado.ayuda;
        if (ayuda && !ayuda.hidden) return;   // la ayuda ya invita a tocar
        estado.botonArrancar.hidden = false;
    }
    function arrancar() {
        if (!estado) return;
        estado.tocado = true;
        if (estado.botonArrancar) estado.botonArrancar.hidden = true;
        if (estado.ayuda) estado.ayuda.hidden = true;
        try { localStorage.setItem(CLAVE_AYUDA, '1'); } catch (e) {}
        if (estado.activa && temasDe(estado.activa).length && !esDuenio(estado.activa)) tocar(estado.activa);
    }

    // Lo que avisa el reproductor
    function alAvisar(ev) {
        if (!estado) return;
        var d = ev.detail || {};
        estado.cards.forEach(function (card) {
            var mio = d.clave && d.clave === card.dataset.clave;
            if (!mio) {
                if (card.classList.contains('is-playing') || d.tipo === 'cierre') { pintarPlay(card, false); }
                if (d.tipo === 'tema' || d.tipo === 'cierre') pintarAvance(card, 0);
                return;
            }
            if (d.tipo === 'tema') { card.dataset.i = String(d.index); pintarTema(card, d.index); pintarAvance(card, 0); pintarTiempos(card, 0, 0); }
            else if (d.tipo === 'avance') { if (!card.querySelector('.feed-avance.arrastrando')) pintarAvance(card, d.dur > 0 ? d.cur / d.dur * 100 : 0); pintarTiempos(card, d.cur, d.dur); }
            else if (d.tipo === 'cierre') { pintarPlay(card, false); pintarAvance(card, 0); }
            if (d.tipo !== 'avance') pintarPlay(card, d.playing && d.tipo !== 'cierre');
        });
    }
    document.addEventListener('arcano:player', alAvisar);

    // Alto de la lista = lo que queda de pantalla (menos la barra de abajo en el celu)
    function medir() {
        if (!estado) return;
        var lista = estado.lista;
        var top = lista.getBoundingClientRect().top + (window.scrollY || 0);
        var nav = document.querySelector('.public-bottom-nav');
        var navAlto = (nav && getComputedStyle(nav).display !== 'none') ? nav.getBoundingClientRect().height : 0;
        var alto = Math.max(380, window.innerHeight - (top - (window.scrollY || 0)) - navAlto);
        estado.raiz.style.setProperty('--feed-alto', alto + 'px');
        estado.cards.forEach(function (c) { irAPista(c, indiceDe(c), false); });
    }

    function desmontar() {
        if (!estado) return;
        if (estado.obs) estado.obs.disconnect();
        window.removeEventListener('resize', medir);
        document.removeEventListener('keydown', alTeclado);
        document.body.classList.remove('modo-feed');
        estado = null;
    }

    function alTeclado(e) {
        if (!estado || e.defaultPrevented) return;
        var tag = (e.target && e.target.tagName) || '';
        if (/INPUT|TEXTAREA|SELECT/.test(tag)) return;
        var card = estado.activa; if (!card) return;
        var idx = estado.cards.indexOf(card);
        if (e.key === 'ArrowDown' || e.key === 'PageDown') { e.preventDefault(); irADisco(idx + 1); }
        else if (e.key === 'ArrowUp' || e.key === 'PageUp') { e.preventDefault(); irADisco(idx - 1); }
        else if (e.key === 'ArrowRight') { e.preventDefault(); cambiarTema(card, 1); }
        else if (e.key === 'ArrowLeft') { e.preventDefault(); cambiarTema(card, -1); }
        else if (e.key === ' ' && !/BUTTON|A/.test(tag)) { e.preventDefault(); if (esDuenio(card)) window.ArcanoPlayer.toggle(); else { estado.tocado = true; tocar(card); } }
    }
    function irADisco(i) {
        if (!estado) return;
        var c = estado.cards[i]; if (!c) return;
        estado.tocado = true;
        c.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function montar() {
        var raiz = document.querySelector('[data-feed]');
        if (!raiz) { desmontar(); return; }
        if (estado && estado.raiz === raiz) return;
        desmontar();
        var lista = raiz.querySelector('[data-feed-lista]');
        estado = {
            raiz: raiz, lista: lista, activa: null, tocado: false,
            cards: [].slice.call(raiz.querySelectorAll('[data-feed-card]')),
            ayuda: raiz.querySelector('[data-feed-ayuda]'),
            botonArrancar: raiz.querySelector('[data-feed-arrancar]')
        };
        document.body.classList.add('modo-feed');
        // Anotar el orden de esta visita en la direccion (?s=): "atras" desde una ficha
        // vuelve al mismo orden; entrar de nuevo desde la tienda arma uno nuevo.
        try {
            var u = new URL(location.href);
            if (raiz.dataset.s && u.searchParams.get('s') !== raiz.dataset.s) {
                u.searchParams.set('s', raiz.dataset.s);
                history.replaceState(history.state, '', u.pathname + u.search + u.hash);
            }
        } catch (e) {}
        medir();
        window.addEventListener('resize', medir);
        document.addEventListener('keydown', alTeclado);

        // Ayuda: solo la primera vez
        var vista = false;
        try { vista = localStorage.getItem(CLAVE_AYUDA) === '1'; } catch (e) {}
        if (estado.ayuda && !vista) estado.ayuda.hidden = false;
        if (estado.ayuda) estado.ayuda.addEventListener('click', arrancar);
        if (estado.botonArrancar) estado.botonArrancar.addEventListener('click', arrancar);

        estado.cards.forEach(function (card) {
            var temas = temasDe(card);
            if (temas.length) pintarTema(card, 0);
            // Si este disco ya estaba sonando (volviste al feed), reflejarlo
            if (esDuenio(card)) pintarPlay(card, window.ArcanoPlayer.sonando && window.ArcanoPlayer.sonando());

            var bPlay = card.querySelector('[data-feed-play]');
            if (bPlay) bPlay.addEventListener('click', function (e) {
                e.preventDefault(); estado.tocado = true;
                if (estado.botonArrancar) estado.botonArrancar.hidden = true;
                if (esDuenio(card)) window.ArcanoPlayer.toggle(); else tocar(card);
            });
            var bPrev = card.querySelector('[data-feed-prev]'), bNext = card.querySelector('[data-feed-next]');
            if (bPrev) bPrev.addEventListener('click', function (e) { e.preventDefault(); estado.tocado = true; cambiarTema(card, -1); });
            if (bNext) bNext.addEventListener('click', function (e) { e.preventDefault(); estado.tocado = true; cambiarTema(card, 1); });

            // Barra de avance: tocar para saltar
            var av = card.querySelector('[data-feed-avance]');
            if (av) {
                var arrastre = false;
                var pct = function (e) { var r = av.getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)); };
                av.addEventListener('pointerdown', function (e) {
                    if (!esDuenio(card)) return;
                    arrastre = true; av.classList.add('arrastrando');
                    try { av.setPointerCapture(e.pointerId); } catch (err) {}
                    pintarAvance(card, pct(e) * 100); e.preventDefault(); e.stopPropagation();
                });
                av.addEventListener('pointermove', function (e) { if (arrastre) pintarAvance(card, pct(e) * 100); });
                var soltar = function (e) {
                    if (!arrastre) return;
                    arrastre = false; av.classList.remove('arrastrando');
                    if (window.ArcanoPlayer.seekPct) window.ArcanoPlayer.seekPct(pct(e));
                };
                av.addEventListener('pointerup', soltar);
                av.addEventListener('pointercancel', function () { arrastre = false; av.classList.remove('arrastrando'); });
                av.addEventListener('click', function (e) { e.stopPropagation(); });
                av.addEventListener('keydown', function (e) {
                    if (!esDuenio(card) || !window.ArcanoPlayer.seekPct) return;
                    var actual = parseFloat((card.querySelector('[data-feed-relleno]') || {}).style.width) || 0;
                    if (e.key === 'ArrowRight') { e.preventDefault(); e.stopPropagation(); window.ArcanoPlayer.seekPct(Math.min(1, actual / 100 + 0.05)); }
                    if (e.key === 'ArrowLeft') { e.preventDefault(); e.stopPropagation(); window.ArcanoPlayer.seekPct(Math.max(0, actual / 100 - 0.05)); }
                });
            }

            // Tocar la tapa (fuera de los botones) = pausa / sigue
            var tapa = card.querySelector('.feed-cover');
            if (tapa && temas.length) tapa.addEventListener('click', function (e) {
                if (e.target.closest('button, a, [data-feed-avance]')) return;
                estado.tocado = true;
                if (estado.botonArrancar) estado.botonArrancar.hidden = true;
                if (esDuenio(card)) window.ArcanoPlayer.toggle(); else tocar(card);
            });

            // Deslizar a los costados = carrusel de tracks (scroll nativo)
            escucharPistas(card);
        });

        // Volviste de una ficha: quedarte en el disco donde estabas (mismo orden)
        try {
            var pos = (sessionStorage.getItem('arcano-feed-pos') || '').split(':');
            if (pos[0] && pos[0] === (raiz.dataset.semilla || '') && pos[1]) {
                var donde = raiz.querySelector('[data-clave="' + pos[1] + '"]');
                if (donde) lista.scrollTop = donde.offsetTop - lista.offsetTop;
            }
        } catch (e) {}

        // El disco que queda en pantalla (mas de la mitad visible) es el "activo"
        estado.obs = new IntersectionObserver(function (entradas) {
            entradas.forEach(function (en) {
                if (en.isIntersecting && en.intersectionRatio >= 0.6) activar(en.target);
            });
        }, { root: lista, threshold: [0.6] });
        estado.cards.forEach(function (c) { estado.obs.observe(c); });

    }

    if (window.Arcano && window.Arcano.onContent) window.Arcano.onContent(montar);
    else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', montar);
    else montar();
})();
