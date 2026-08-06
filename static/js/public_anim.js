/* Animaciones sutiles para la tienda publica.
   Usa IntersectionObserver para revelar elementos al scroll.
*/
(function () {
    'use strict';

    // Fade in al scroll para cards y secciones
    var targets = document.querySelectorAll('.public-card, .public-disc-section, .public-labels-strip, .public-related, .public-sobre-block');
    if (targets.length === 0) return;

    // Fallback: si no hay IntersectionObserver, mostrar todo
    if (!('IntersectionObserver' in window)) {
        targets.forEach(function (el) { el.classList.add('is-visible'); });
        return;
    }

    // Marcar todos como no visibles inicialmente
    targets.forEach(function (el) { el.classList.add('will-fade'); });

    var observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
            if (entry.isIntersecting) {
                entry.target.classList.add('is-visible');
                observer.unobserve(entry.target);
            }
        });
    }, {
        rootMargin: '0px 0px -50px 0px',
        threshold: 0.05
    });

    targets.forEach(function (el) { observer.observe(el); });
})();

/* Toggle de tema claro/oscuro */
(function () {
    'use strict';
    var toggle = document.getElementById('theme-toggle');
    if (!toggle) return;

    function currentTheme() {
        return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    }

    toggle.addEventListener('click', function () {
        var next = currentTheme() === 'light' ? 'dark' : 'light';
        if (next === 'light') {
            document.documentElement.setAttribute('data-theme', 'light');
        } else {
            document.documentElement.removeAttribute('data-theme');
        }
        try { localStorage.setItem('arcano-theme', next); } catch (e) {}
    });
})();

/* Wishlist / Favoritos (localStorage) */
(function () {
    'use strict';
    var KEY = 'arcano-favs';

    function getFavs() {
        try { return JSON.parse(localStorage.getItem(KEY) || '[]'); }
        catch (e) { return []; }
    }
    function setFavs(arr) {
        try { localStorage.setItem(KEY, JSON.stringify(arr)); } catch (e) {}
    }
    function isFav(id) { return getFavs().indexOf(String(id)) !== -1; }
    function toggle(id) {
        id = String(id);
        var favs = getFavs();
        var i = favs.indexOf(id);
        if (i === -1) { favs.push(id); } else { favs.splice(i, 1); }
        setFavs(favs);
        return i === -1; // true si quedo agregado
    }

    function updateCount() {
        var n = getFavs().length;
        // Actualiza todos los contadores de favoritos (nav, drawer, bottom-nav)
        document.querySelectorAll('.fav-count').forEach(function (el) {
            if (n > 0) { el.textContent = n; el.hidden = false; }
            else { el.hidden = true; }
        });
    }

    function paintButtons() {
        document.querySelectorAll('.fav-btn').forEach(function (btn) {
            var id = btn.dataset.favId;
            if (isFav(id)) btn.classList.add('is-fav');
            else btn.classList.remove('is-fav');
        });
    }

    function bind() {
        document.querySelectorAll('.fav-btn').forEach(function (btn) {
            if (btn._favBound) return;
            btn._favBound = true;
            btn.addEventListener('click', function (e) {
                e.preventDefault();
                e.stopPropagation();
                var id = btn.dataset.favId;
                var added = toggle(id);
                btn.classList.toggle('is-fav', added);
                updateCount();
                // Feedback visual
                btn.classList.remove('fav-pop');
                void btn.offsetWidth;
                btn.classList.add('fav-pop');
            });
        });
    }

    window.ArcanoFavs = {
        rebind: function () { bind(); paintButtons(); updateCount(); }
    };

    document.addEventListener('DOMContentLoaded', function () {
        bind();
        paintButtons();
        updateCount();
    });
    // Por si el DOM ya cargo
    bind(); paintButtons(); updateCount();
})();

/* Toggle del panel de filtros avanzados */
(function () {
    'use strict';
    var toggle = document.getElementById('filters-toggle');
    var panel = document.getElementById('filters-panel');
    if (!toggle || !panel) return;
    toggle.addEventListener('click', function () {
        panel.classList.toggle('open');
    });
})();

/* Carrito (localStorage) */
(function () {
    'use strict';
    var KEY = 'arcano-cart';

    function getCart() {
        try { return JSON.parse(localStorage.getItem(KEY) || '[]'); }
        catch (e) { return []; }
    }
    function setCart(arr) {
        try { localStorage.setItem(KEY, JSON.stringify(arr)); } catch (e) {}
    }
    function inCart(id) { return getCart().indexOf(String(id)) !== -1; }
    function toggle(id) {
        id = String(id);
        var cart = getCart();
        var i = cart.indexOf(id);
        if (i === -1) { cart.push(id); } else { cart.splice(i, 1); }
        setCart(cart);
        return i === -1;
    }

    function updateCount() {
        var n = getCart().length;
        // Actualiza todos los contadores de carrito (nav, drawer, topbar, bottom-nav)
        document.querySelectorAll('.cart-count').forEach(function (el) {
            if (n > 0) { el.textContent = n; el.hidden = false; }
            else { el.hidden = true; }
        });
    }

    function paintButtons() {
        document.querySelectorAll('.cart-btn').forEach(function (btn) {
            var id = btn.dataset.cartId;
            btn.classList.toggle('is-in-cart', inCart(id));
        });
    }

    function bind() {
        document.querySelectorAll('.cart-btn').forEach(function (btn) {
            if (btn._cartBound) return;
            btn._cartBound = true;
            btn.addEventListener('click', function (e) {
                e.preventDefault();
                e.stopPropagation();
                var id = btn.dataset.cartId;
                var added = toggle(id);
                btn.classList.toggle('is-in-cart', added);
                updateCount();
                btn.classList.remove('cart-pop');
                void btn.offsetWidth;
                btn.classList.add('cart-pop');
                // Feedback visual (toast)
                if (window.arcanoToast) {
                    window.arcanoToast(added ? '\u2713 Agregado al carrito' : 'Quitado del carrito');
                }
                // Actualizar label si el boton lo tiene
                var lblAdd = btn.querySelector('.cart-label-add');
                var lblIn = btn.querySelector('.cart-label-in');
            });
        });
    }

    window.ArcanoCart = {
        rebind: function () { bind(); paintButtons(); updateCount(); },
        get: getCart,
        has: inCart,
        add: function (id) {
            id = String(id);
            var cart = getCart();
            if (cart.indexOf(id) === -1) { cart.push(id); setCart(cart); }
            updateCount();
            paintButtons();
            return true;
        },
        clear: function () { setCart([]); updateCount(); }
    };

    document.addEventListener('DOMContentLoaded', function () {
        bind(); paintButtons(); updateCount();
    });
    bind(); paintButtons(); updateCount();
})();

/* Banner de cookies (informativo, localStorage) */
(function () {
    'use strict';
    var banner = document.getElementById('cookie-banner');
    var accept = document.getElementById('cookie-accept');
    if (!banner || !accept) return;
    var KEY = 'arcano-cookies-ok';
    try {
        if (!localStorage.getItem(KEY)) {
            banner.hidden = false;
        }
    } catch (e) {
        banner.hidden = false;
    }
    accept.addEventListener('click', function () {
        try { localStorage.setItem(KEY, '1'); } catch (e) {}
        banner.hidden = true;
    });
})();

/* Menu lateral (drawer) con hamburguesa */
(function () {
    'use strict';
    var hamburger = document.getElementById('public-hamburger');
    var drawer = document.getElementById('public-drawer');
    var overlay = document.getElementById('public-drawer-overlay');
    var closeBtn = document.getElementById('public-drawer-close');
    if (!hamburger || !drawer || !overlay) return;

    function abrir() {
        drawer.classList.add('open');
        overlay.hidden = false;
        setTimeout(function () { overlay.classList.add('visible'); }, 10);
        hamburger.setAttribute('aria-expanded', 'true');
        drawer.setAttribute('aria-hidden', 'false');
        document.body.style.overflow = 'hidden';
    }
    function cerrar() {
        drawer.classList.remove('open');
        overlay.classList.remove('visible');
        setTimeout(function () { overlay.hidden = true; }, 250);
        hamburger.setAttribute('aria-expanded', 'false');
        drawer.setAttribute('aria-hidden', 'true');
        document.body.style.overflow = '';
    }
    hamburger.addEventListener('click', abrir);
    overlay.addEventListener('click', cerrar);
    if (closeBtn) closeBtn.addEventListener('click', cerrar);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && drawer.classList.contains('open')) cerrar();
    });

    // El toggle de tema dentro del drawer replica el de arriba
    var drawerTheme = document.getElementById('theme-toggle-drawer');
    if (drawerTheme) {
        drawerTheme.addEventListener('click', function () {
            var isLight = document.documentElement.getAttribute('data-theme') === 'light';
            if (isLight) {
                document.documentElement.removeAttribute('data-theme');
                try { localStorage.setItem('arcano-theme', 'dark'); } catch (e) {}
            } else {
                document.documentElement.setAttribute('data-theme', 'light');
                try { localStorage.setItem('arcano-theme', 'light'); } catch (e) {}
            }
        });
    }
})();

/* Toggle del sidebar de filtros en movil */
(function () {
    'use strict';
    var toggle = document.getElementById('sidebar-toggle');
    var sidebar = document.getElementById('public-sidebar');
    if (!toggle || !sidebar) return;
    toggle.addEventListener('click', function () {
        sidebar.classList.toggle('open');
    });
    // Cerrar al tocar afuera en movil
    document.addEventListener('click', function (e) {
        if (window.innerWidth > 860) return;
        if (!sidebar.classList.contains('open')) return;
        if (sidebar.contains(e.target) || toggle.contains(e.target)) return;
        sidebar.classList.remove('open');
    });
})();

/* Carruseles de la home (Destacados / Nuevos ingresos) — 2 tapas por slide */
(function () {
    'use strict';
    document.querySelectorAll('.public-carrusel').forEach(function (carrusel) {
        var track = carrusel.querySelector('.public-carrusel-track');
        var cards = carrusel.querySelectorAll('.carrusel-card');
        var prev = carrusel.querySelector('.carrusel-prev');
        var next = carrusel.querySelector('.carrusel-next');
        var progressBar = carrusel.querySelector('.public-carrusel-progress-bar');
        if (!track || cards.length === 0) return;

        // Velocidad configurable (segundos entre slides), viene del admin
        var velocidadSeg = parseFloat(carrusel.dataset.velocidad || '7');
        if (isNaN(velocidadSeg) || velocidadSeg < 2) velocidadSeg = 7;
        var velocidadMs = velocidadSeg * 1000;

        function calcPorSlide() { return window.innerWidth <= 720 ? 1 : 2; }
        function totalSlides() { return Math.ceil(cards.length / calcPorSlide()); }
        var slideActual = 0;

        function actualizarProgreso() {
            if (!progressBar) return;
            var slides = totalSlides();
            var pct = slides <= 1 ? 100 : ((slideActual + 1) / slides) * 100;
            progressBar.style.width = pct + '%';
        }

        function irASlide(n) {
            var slides = totalSlides();
            if (n < 0) n = slides - 1;
            if (n >= slides) n = 0;
            slideActual = n;
            track.style.transform = 'translateX(-' + (n * 100) + '%)';
            actualizarProgreso();
        }

        if (next) next.addEventListener('click', function () { irASlide(slideActual + 1); reiniciarAuto(); });
        if (prev) prev.addEventListener('click', function () { irASlide(slideActual - 1); reiniciarAuto(); });

        // Autoplay con la velocidad configurada (se pausa al pasar el mouse)
        var timer = null;
        function iniciarAuto() {
            if (cards.length <= calcPorSlide()) return;
            timer = setInterval(function () { irASlide(slideActual + 1); }, velocidadMs);
        }
        function reiniciarAuto() { if (timer) clearInterval(timer); iniciarAuto(); }
        carrusel.addEventListener('mouseenter', function () { if (timer) clearInterval(timer); });
        carrusel.addEventListener('mouseleave', iniciarAuto);
        window.addEventListener('resize', function () { irASlide(0); });

        actualizarProgreso();
        iniciarAuto();
    });
})();

/* Fade-in de portadas al cargar (quita el shimmer) */
(function () {
    'use strict';
    function marcarCargada(img) {
        img.classList.add('loaded');
        var cover = img.closest('.public-card-cover, .carrusel-cover');
        if (cover) cover.classList.add('img-loaded');
    }
    document.querySelectorAll('.public-card-cover img, .carrusel-cover img').forEach(function (img) {
        if (img.complete && img.naturalWidth > 0) {
            marcarCargada(img);
        } else {
            img.addEventListener('load', function () { marcarCargada(img); });
            img.addEventListener('error', function () {
                var cover = img.closest('.public-card-cover, .carrusel-cover');
                if (cover) cover.classList.add('img-loaded');
            });
        }
    });
})();

/* Feedback visual al agregar al carrito / favoritos (toast) */
(function () {
    'use strict';
    window.arcanoToast = function (mensaje, tipo) {
        var toast = document.createElement('div');
        toast.className = 'arcano-toast' + (tipo === 'error' ? ' arcano-toast-error' : '');
        toast.textContent = mensaje;
        document.body.appendChild(toast);
        setTimeout(function () { toast.classList.add('visible'); }, 10);
        setTimeout(function () {
            toast.classList.remove('visible');
            setTimeout(function () { toast.remove(); }, 300);
        }, 2200);
    };
})();

/* Buscador con sugerencias (autocompletado) */
(function () {
    'use strict';
    var input = document.getElementById('public-search-input');
    var box = document.getElementById('search-suggestions');
    if (!input || !box) return;

    var timer = null;
    var tipoLabel = { artista: 'Artista', sello: 'Sello', disco: 'Disco' };

    function cerrar() { box.hidden = true; box.innerHTML = ''; }

    function buscar(q) {
        fetch('/api/buscar-sugerencias?q=' + encodeURIComponent(q))
            .then(function (r) { return r.json(); })
            .then(function (items) {
                if (!items.length) { cerrar(); return; }
                box.innerHTML = '';
                items.forEach(function (item) {
                    var el = document.createElement('a');
                    el.className = 'search-suggestion';
                    if (item.tipo === 'disco' && item.id) {
                        el.href = '/disco/' + item.id;
                    } else {
                        el.href = '/?q=' + encodeURIComponent(item.texto);
                    }
                    el.innerHTML = '<span class="suggestion-text">' + item.texto +
                        '</span><span class="suggestion-type">' + (tipoLabel[item.tipo] || '') + '</span>';
                    box.appendChild(el);
                });
                box.hidden = false;
            }).catch(cerrar);
    }

    input.addEventListener('input', function () {
        var q = input.value.trim();
        if (timer) clearTimeout(timer);
        if (q.length < 2) { cerrar(); return; }
        timer = setTimeout(function () { buscar(q); }, 250);
    });
    input.addEventListener('blur', function () { setTimeout(cerrar, 200); });
    document.addEventListener('click', function (e) {
        if (!input.contains(e.target) && !box.contains(e.target)) cerrar();
    });
})();

/* ============================================================
   v2.69 - Fluidez: skeleton de covers, prefetch, fly-to-cart
   ============================================================ */
(function () {
    'use strict';

    // --- Skeleton: fade-in de covers cuando terminan de cargar ---
    function prepararImagen(img) {
        if (img.complete && img.naturalWidth > 0) {
            img.classList.add('img-loaded');
            return;
        }
        img.classList.add('img-pending');
        img.addEventListener('load', function () {
            img.classList.remove('img-pending');
            img.classList.add('img-loaded');
        }, { once: true });
        img.addEventListener('error', function () {
            img.classList.remove('img-pending');
            img.classList.add('img-loaded'); // mostrar igual (alt/vacio)
        }, { once: true });
    }
    document.querySelectorAll('.public-card-cover img, .carrusel-cover img').forEach(prepararImagen);

    // --- Prefetch on hover: precargar fichas al pasar el mouse ---
    var prefetched = {};
    function prefetch(url) {
        if (prefetched[url]) return;
        prefetched[url] = true;
        var link = document.createElement('link');
        link.rel = 'prefetch';
        link.href = url;
        document.head.appendChild(link);
    }
    document.addEventListener('mouseover', function (e) {
        var a = e.target.closest('a[href^="/disco/"]');
        if (a) prefetch(a.getAttribute('href'));
    });
    document.addEventListener('touchstart', function (e) {
        var a = e.target.closest('a[href^="/disco/"]');
        if (a) prefetch(a.getAttribute('href'));
    }, { passive: true });

    // --- Fly to cart + bounce del contador ---
    function carritoDestino() {
        // El icono visible: topbar en desktop, bottom-nav en mobile
        var top = document.querySelector('.public-topbar-cart');
        var bottom = document.querySelector('.bottom-nav-item .cart-count');
        if (bottom && bottom.closest('.bottom-nav') &&
            getComputedStyle(bottom.closest('.bottom-nav')).display !== 'none') {
            return bottom.closest('.bottom-nav-item');
        }
        return top;
    }
    function volarAlCarrito(desdeImg) {
        var destino = carritoDestino();
        if (!destino || !desdeImg) return;
        var o = desdeImg.getBoundingClientRect();
        var d = destino.getBoundingClientRect();
        var clone = desdeImg.cloneNode(true);
        clone.className = 'fly-to-cart-clone';
        var size = Math.min(o.width, 90);
        clone.style.width = size + 'px';
        clone.style.height = size + 'px';
        clone.style.left = (o.left + o.width / 2 - size / 2) + 'px';
        clone.style.top = (o.top + o.height / 2 - size / 2) + 'px';
        document.body.appendChild(clone);
        var dx = (d.left + d.width / 2) - (o.left + o.width / 2);
        var dy = (d.top + d.height / 2) - (o.top + o.height / 2);
        requestAnimationFrame(function () {
            clone.style.transform = 'translate(' + dx + 'px,' + dy + 'px) scale(0.12) rotate(12deg)';
            clone.style.opacity = '0.25';
        });
        setTimeout(function () {
            clone.remove();
            document.querySelectorAll('.cart-count').forEach(function (c) {
                c.classList.remove('bounce');
                void c.offsetWidth; // reiniciar la animacion
                c.classList.add('bounce');
            });
        }, 660);
    }
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.cart-btn');
        if (!btn) return;
        // Solo animar cuando AGREGA (no cuando ya estaba en el carrito)
        if (btn.classList.contains('in-cart')) return;
        var card = btn.closest('.public-card') || btn.closest('.public-disc-layout') || document;
        var img = card.querySelector('.public-card-cover img') ||
                  card.querySelector('.public-disc-cover img');
        volarAlCarrito(img);
    });
})();
