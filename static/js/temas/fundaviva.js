/* OPCION M · FUNDA VIVA
   Pone un vinilo detras de cada tapa con la portada como etiqueta central.
   Mira cada medio segundo que disco esta sonando (sin tocar player.js) y le
   agrega .fv-sonando: el vinilo sale mas de la funda y gira despacio. */
(function () {
    'use strict';

    function armar() {
        document.querySelectorAll('.public-grid > .public-card:not(.public-card-mbox)').forEach(function (card) {
            if (card.querySelector('.fv-disco')) return;
            var d = document.createElement('span');
            d.className = 'fv-disco';
            d.setAttribute('aria-hidden', 'true');
            d.innerHTML = '<span class="fv-giro"><span class="fv-etiqueta"><img alt="" loading="lazy"></span></span>';
            var src = card.querySelector('.public-card-cover img');
            var img = d.querySelector('img');
            img.addEventListener('error', function () { img.style.visibility = 'hidden'; });
            if (src) img.src = src.getAttribute('src') || '';
            card.insertBefore(d, card.firstChild);
        });
    }

    function sonando() {
        var activo = document.querySelector('.public-grid .card-play-btn.is-playing');
        var pausa = document.querySelector('#fp-playpause .fp-ico-pause');
        var fp = document.getElementById('floating-player');
        var suena = !!(activo && fp && !fp.hidden && pausa && !pausa.hasAttribute('hidden'));
        document.querySelectorAll('.public-grid > .public-card').forEach(function (c) {
            c.classList.toggle('fv-sonando', suena && c.contains(activo));
        });
    }

    setInterval(sonando, 500);
    if (window.Arcano && window.Arcano.onContent) window.Arcano.onContent(armar); else document.addEventListener('DOMContentLoaded', armar);
})();
