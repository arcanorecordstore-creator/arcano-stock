/* OPCION H · TOCADISCOS
   Arma la BANDEJA arriba del catalogo. Pasar el mouse (o tocar la etiqueta de
   un disco) lo apoya en la bandeja; cuando el reproductor suena, la bandeja
   muestra ese disco girando y baja el brazo. Botones 33/45 cambian la velocidad
   (lento a proposito: 33 = 5,4 s por vuelta; 45 = 4 s). Escuchar / Agregar usan los
   botones reales de la card, asi que el carrito y el reproductor funcionan igual. */
(function () {
    'use strict';
    var deck, actual = null;

    function q(el, sel) { var n = el.querySelector(sel); return n ? n.textContent.trim() : ''; }
    function precio(card) {
        var n = card.querySelector('.precio-oferta, .precio-fire') || card.querySelector('.public-card-price');
        if (!n) return '';
        var m = n.textContent.match(/\$\s*[\d.]+(\s*[–-]\s*\$\s*[\d.]+)?/);
        return m ? m[0] : n.textContent.trim();
    }

    function crearDeck() {
        var d = document.createElement('section');
        d.className = 'td-deck';
        d.setAttribute('aria-label', 'Bandeja');
        d.innerHTML =
            '<div class="td-plato">' +
                '<div class="td-plinto"></div>' +
                '<div class="td-giradiscos"><div class="td-disco"><img alt=""></div><span class="td-etiqueta">Arcano</span><span class="td-eje"></span></div>' +
                '<svg class="td-brazo" viewBox="0 0 100 230" aria-hidden="true">' +
                    '<circle cx="74" cy="25" r="17" fill="#2c2828" stroke="rgba(233,209,175,.25)"/>' +
                    '<circle cx="74" cy="25" r="7" fill="#c9b8a3"/>' +
                    '<rect x="66" y="0" width="16" height="12" rx="3" fill="#2c2828"/>' +
                    '<path d="M74 25 L74 150 L44 196" fill="none" stroke="#c9b8a3" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>' +
                    '<rect x="28" y="190" width="24" height="30" rx="4" transform="rotate(32 40 205)" fill="#E9D1AF"/>' +
                '</svg>' +
                '<div class="td-velocidad"><button type="button" data-v="5.4s" class="is-on">33</button><button type="button" data-v="4s">45</button></div>' +
                '<span class="td-luz"></span>' +
            '</div>' +
            '<div class="td-info">' +
                '<span class="td-kicker">En la bandeja</span>' +
                '<div class="td-art"></div><div class="td-tit"></div><div class="td-meta"></div>' +
                '<div class="td-precio"></div>' +
                '<div class="td-acciones"><button type="button" class="td-play">&#9654; Escuchar</button><button type="button" class="td-cart">Agregar al carrito</button><a class="td-ver" href="#">Ver ficha</a></div>' +
            '</div>';
        var im = d.querySelector('.td-disco img');
        im.addEventListener('error', function () { im.style.visibility = 'hidden'; });
        im.addEventListener('load', function () { im.style.visibility = ''; });
        d.querySelectorAll('.td-velocidad button').forEach(function (b) {
            b.addEventListener('click', function () {
                d.querySelectorAll('.td-velocidad button').forEach(function (o) { o.classList.toggle('is-on', o === b); });
                document.body.style.setProperty('--td-vuelta', b.dataset.v);
            });
        });
        d.querySelector('.td-play').addEventListener('click', function () {
            var b = actual && actual.querySelector('.card-play-btn'); if (b) b.click();
        });
        d.querySelector('.td-cart').addEventListener('click', function () {
            var b = actual && actual.querySelector('button.cart-btn-inline'); if (b) b.click();
            setTimeout(pintarBotones, 50);
        });
        return d;
    }

    function pintarBotones() {
        if (!actual || !deck) return;
        var play = deck.querySelector('.td-play'), cart = deck.querySelector('.td-cart');
        var pb = actual.querySelector('.card-play-btn');
        play.disabled = !pb;
        play.innerHTML = pb ? (pb.classList.contains('is-playing') ? '&#9724; Pausa' : '&#9654; Escuchar') : 'Sin muestra';
        var cb = actual.querySelector('button.cart-btn-inline');
        cart.hidden = !cb;
        if (cb) cart.textContent = cb.classList.contains('is-in-cart') || cb.classList.contains('in-cart') ? '✓ En el carrito' : 'Agregar al carrito';
    }

    function apoyar(card) {
        if (!deck || card === actual) return;
        actual = card;
        var disco = deck.querySelector('.td-disco'), img = disco.querySelector('img');
        var src = card.querySelector('.public-card-cover img');
        disco.style.opacity = '0';
        setTimeout(function () {
            img.src = src ? src.currentSrc || src.src : '';
            disco.style.opacity = '1';
        }, 180);
        deck.querySelector('.td-art').textContent = card.classList.contains('public-card-mbox') ? 'Mystery Box' : q(card, '.public-card-artist');
        deck.querySelector('.td-tit').textContent = q(card, '.public-card-title');
        deck.querySelector('.td-meta').textContent = q(card, '.public-card-label');
        deck.querySelector('.td-precio').textContent = precio(card);
        deck.querySelector('.td-ver').href = card.getAttribute('href') || '#';
        deck.querySelector('.td-etiqueta').textContent = (q(card, '.public-card-label').split('·')[0] || 'Arcano').trim().slice(0, 18);
        pintarBotones();
    }

    // Estado del reproductor: se mira cada medio segundo (sin tocar player.js)
    function vigilar() {
        if (!deck || !document.body.contains(deck)) return;
        var activo = document.querySelector('.public-grid .card-play-btn.is-playing');
        var fp = document.getElementById('floating-player');
        var pausa = document.querySelector('#fp-playpause .fp-ico-pause');
        var suena = !!(activo && fp && !fp.hidden && pausa && !pausa.hidden);
        document.querySelectorAll('.public-card.td-sonando').forEach(function (c) { if (!suena || !c.contains(activo)) c.classList.remove('td-sonando'); });
        if (suena) {
            var card = activo.closest('.public-card');
            if (card) { card.classList.add('td-sonando'); apoyar(card); }
        }
        deck.classList.toggle('is-sonando', suena);
        pintarBotones();
    }

    function init() {
        var grid = document.querySelector('.public-grid');
        var head = document.querySelector('.public-results-head');
        if (!grid || !head) { deck = null; return; }
        if (!document.querySelector('.td-deck')) { deck = crearDeck(); head.parentNode.insertBefore(deck, head); }
        else deck = document.querySelector('.td-deck');
        actual = null;
        var cards = grid.querySelectorAll(':scope > .public-card');
        cards.forEach(function (c) {
            c.addEventListener('mouseenter', function () { if (!document.querySelector('.td-deck.is-sonando')) apoyar(c); });
        });
        var primero = grid.querySelector(':scope > .public-card:not(.public-card-mbox)') || cards[0];
        if (primero) apoyar(primero);
    }

    setInterval(vigilar, 500);
    if (window.Arcano && window.Arcano.onContent) window.Arcano.onContent(init); else document.addEventListener('DOMContentLoaded', init);
})();
