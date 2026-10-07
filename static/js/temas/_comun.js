/* COMUN A TODOS LOS DISENOS (copia de diseno) — ver static/css/temas/_comun.css
   - Ficha: pone la portada como etiqueta del vinilo y marca cuando suena
     (.cm-sonando) para que suba y gire despacio.
   - Ficha: "Similares" en carrete 3D (las flechas pasan de a un disco; tocar
     uno del costado lo trae al centro).
   - Inicio: con la variante "Carrete" de carruseles, gira las cards segun
     la distancia al centro. */
(function () {
    'use strict';
    var flujos = [];   // {cont, items, sel, actual}

    // ---------- coverflow generico ----------
    function pintar(f) {
        f.pend = false;
        var mitad = f.cont.scrollLeft + f.cont.clientWidth / 2, mejor = 0, dist = 1e9;
        f.items.forEach(function (it, i) {
            var w = it.offsetWidth || 1;
            var d = (it.offsetLeft + w / 2 - mitad) / (w * 0.85);
            var a = Math.min(Math.abs(d), 1);
            it.style.setProperty('--cm-giro', (d > 0 ? -1 : 1) * a * f.grados + 'deg');
            it.style.setProperty('--cm-luz', (1 - a * 0.4).toFixed(3));
            it.style.setProperty('--cm-op', Math.max(0.25, 1 - Math.max(0, Math.abs(d) - 1) * 0.18).toFixed(3));
            it.style.setProperty('--cm-z', String(100 - Math.round(Math.abs(d) * 10)));
            if (Math.abs(d) < dist) { dist = Math.abs(d); mejor = i; }
        });
        f.actual = mejor;
        f.items.forEach(function (it, i) { it.classList.toggle('cm-centro', i === mejor); });
    }
    function centrar(f, i, suave) {
        i = Math.max(0, Math.min(f.items.length - 1, i));
        var it = f.items[i];
        f.cont.scrollTo({ left: it.offsetLeft + it.offsetWidth / 2 - f.cont.clientWidth / 2, behavior: suave === false ? 'auto' : 'smooth' });
    }
    function flujo(cont, items, grados) {
        if (cont._cm) return cont._cm;
        var f = { cont: cont, items: items, actual: 0, grados: grados, pend: false };
        cont._cm = f;
        cont.addEventListener('scroll', function () { if (!f.pend) { f.pend = true; requestAnimationFrame(function () { pintar(f); }); } }, { passive: true });
        cont.addEventListener('click', function (e) {
            var it = e.target.closest && e.target.closest('.cm-centro, .public-related-card, .carrusel-card');
            if (!it || it.classList.contains('cm-centro') || f.items.indexOf(it) < 0) return;
            e.preventDefault(); e.stopPropagation();
            centrar(f, f.items.indexOf(it));
        }, true);
        flujos.push(f);
        pintar(f);
        return f;
    }

    // ---------- Similares de la ficha ----------
    function similares() {
        var sc = document.querySelector('.public-related-scroll');
        if (!sc || sc._cm) return;
        var items = Array.prototype.slice.call(sc.querySelectorAll(':scope > .public-related-card'));
        if (items.length < 2) return;
        sc.classList.add('cm-flow');
        var f = flujo(sc, items, 46);
        // flechas: de a un disco (se reemplazan para sacar el salto de pantalla entera)
        [['prev', -1], ['next', 1]].forEach(function (p) {
            var b = document.querySelector('.public-related-nav.' + p[0]);
            if (!b) return;
            var nb = b.cloneNode(true);
            nb.disabled = false;
            b.parentNode.replaceChild(nb, b);
            nb.addEventListener('click', function () { centrar(f, f.actual + p[1]); });
        });
        centrar(f, Math.min(1, items.length - 1), false);
        pintar(f);
    }

    // ---------- Vinilo de la ficha ----------
    function vinilo() {
        var lab = document.querySelector('.public-disc-vinyl-label');
        var img = document.getElementById('disc-cover-img');
        if (lab && img && !lab.classList.contains('cm-con-tapa')) {
            lab.style.backgroundImage = 'url("' + (img.currentSrc || img.src).replace(/"/g, '%22') + '")';
            lab.classList.add('cm-con-tapa');
        }
    }
    setInterval(function () {
        var w = document.querySelector('.public-disc-coverwrap');
        if (!w) return;
        var b = w.querySelector('.public-disc-cover-play');
        var pausa = document.querySelector('#fp-playpause .fp-ico-pause');
        w.classList.toggle('cm-sonando', !!(b && b.classList.contains('is-playing') && pausa && !pausa.hasAttribute('hidden')));
    }, 500);

    // ---------- Carruseles en carrete ----------
    function carruseles() {
        if (!document.body.classList.contains('carr-carrete')) return;
        document.querySelectorAll('.public-carrusel-viewport').forEach(function (vp) {
            var items = Array.prototype.slice.call(vp.querySelectorAll('.carrusel-card'));
            if (items.length < 2 || vp._cm) return;
            var f = flujo(vp, items, 40);
            centrar(f, Math.min(1, items.length - 1), false);
            pintar(f);
        });
    }

    function init() {
        flujos = flujos.filter(function (f) { return document.body.contains(f.cont); });
        vinilo(); similares(); carruseles();
    }
    window.addEventListener('resize', function () { flujos.forEach(function (f) { pintar(f); }); });
    if (window.Arcano && window.Arcano.onContent) window.Arcano.onContent(init); else document.addEventListener('DOMContentLoaded', init);
})();
