/* Registro de inicializadores de contenido.
   Cada modulo que engancha elementos DENTRO de <main> registra su init aca; se
   corre al cargar y de nuevo despues de cada navegacion pjax (cambio de pagina
   sin recargar, con el reproductor sonando). Los listeners a nivel document/
   window se registran una sola vez, aparte. */
window.Arcano = window.Arcano || {};
(function () {
    'use strict';
    var fns = [];
    window.Arcano.onContent = function (fn) { fns.push(fn); try { fn(); } catch (e) {} };
    window.Arcano.runContent = function () { fns.forEach(function (fn) { try { fn(); } catch (e) {} }); };
})();

/* Evento a Google Analytics 4 + Meta Pixel (embudo de conversion). No hace nada si
   GA o el pixel no estan cargados, asi que es seguro en local y en el sandbox.
   Recibe el formato de GA4 (items con item_id / item_name / price) y arma solo los
   datos de producto que pide Meta (content_ids, contents, value...): con eso la
   publicidad puede optimizar por valor y mostrarle a cada persona el disco que vio. */
window.arcanoGA = function (name, params) {
    params = params || {};
    var items = Array.isArray(params.items) ? params.items : [];
    // Valor: si no vino, la suma de los precios de los discos
    if (params.value == null && items.length) {
        var suma = 0, hayPrecio = false;
        items.forEach(function (it) {
            if (it && it.price != null && !isNaN(it.price)) { suma += Number(it.price) * (it.quantity || 1); hayPrecio = true; }
        });
        if (hayPrecio) params.value = suma;
    }
    if (params.currency == null && (params.value != null || items.length)) params.currency = 'UYU';
    if (typeof window.gtag === 'function') {
        try { window.gtag('event', name, params); } catch (e) {}
    }
    if (typeof window.fbq === 'function') {
        try {
            var mapa = {
                view_item: 'ViewContent', add_to_cart: 'AddToCart', add_to_wishlist: 'AddToWishlist',
                begin_checkout: 'InitiateCheckout', purchase: 'Purchase',
                generate_lead: 'Lead', contact: 'Contact'
            };
            var evento = mapa[name];
            if (evento) {
                var datos = {};
                if (params.value != null) { datos.value = Number(params.value) || 0; datos.currency = params.currency || 'UYU'; }
                if (items.length) {
                    var ids = items.map(function (it) { return String(it.item_id || ''); }).filter(Boolean);
                    if (ids.length) {
                        datos.content_ids = ids;
                        datos.content_type = 'product';
                        datos.contents = items.filter(function (it) { return it.item_id; }).map(function (it) {
                            var c = { id: String(it.item_id), quantity: it.quantity || 1 };
                            if (it.price != null) c.item_price = Number(it.price) || 0;
                            return c;
                        });
                        datos.num_items = ids.length;
                    }
                    if (items.length === 1 && items[0].item_name) datos.content_name = items[0].item_name;
                }
                if (params.method) datos.content_category = params.method;
                var opciones = params.transaction_id ? { eventID: String(params.transaction_id) } : undefined;
                window.fbq('track', evento, datos, opciones);
            }
        } catch (e) {}
    }
};

/* Clics para escribir por WhatsApp o Instagram: son ventas que se cierran por mensaje
   (lo que trae un reel). GA4 'contact' + Meta 'Contact'. */
document.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a[href]') : null;
    if (!a) return;
    var h = a.getAttribute('href') || '';
    var via = /wa\.me|api\.whatsapp|whatsapp:\/\//i.test(h) ? 'whatsapp'
            : (/ig\.me|instagram\.com\/direct/i.test(h) ? 'instagram' : null);
    if (via && window.arcanoGA) window.arcanoGA('contact', { method: via, link_url: h.slice(0, 200) });
}, true);

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

    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // Marcar todos como no visibles inicialmente + cascada por posicion (solo cards)
    targets.forEach(function (el) {
        el.classList.add('will-fade');
        if (!reduce && el.classList.contains('public-card') && el.parentElement) {
            var idx = Array.prototype.indexOf.call(el.parentElement.children, el);
            el.style.transitionDelay = Math.min((idx % 6) * 0.06, 0.3) + 's';
        }
    });

    var observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
            if (entry.isIntersecting) {
                var el = entry.target;
                el.classList.add('is-visible');
                observer.unobserve(el);
                // Al terminar la entrada, sacar will-fade y el delay para que el
                // hover quede instantaneo (no arrastre la transicion lenta de entrada).
                setTimeout(function () {
                    el.classList.remove('will-fade');
                    el.style.transitionDelay = '';
                }, 900);
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
                // Si hay sesion de cliente, guardar el favorito en la cuenta
                if (document.body.dataset.loggedIn === '1') {
                    fetch('/cuenta/api/favoritos/toggle', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: id, fav: added })
                    }).catch(function () {});
                }
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

    // Sync con la cuenta si el cliente esta logueado: fusiona los favoritos de
    // este navegador con los de la cuenta, para que viajen entre dispositivos.
    if (document.body && document.body.dataset.loggedIn === '1') {
        try {
            fetch('/cuenta/api/favoritos/sync', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ids: getFavs() })
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.ok && Array.isArray(data.ids)) {
                    setFavs(data.ids);
                    paintButtons();
                    updateCount();
                }
            }).catch(function () {});
        } catch (e) {}
    }
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

    // Un id de carrito valido es el id numerico de un disco. Todo lo demas
    // ('undefined', null, vacio) se descarta: si entra basura, infla el contador y
    // rompe el cajon. Paso por un boton mal configurado, asi que se filtra siempre.
    function idValido(id) {
        return id != null && /^\d+$/.test(String(id));
    }
    function getCart() {
        try {
            var arr = JSON.parse(localStorage.getItem(KEY) || '[]');
            if (!Array.isArray(arr)) return [];
            var limpio = arr.filter(idValido).map(String);
            // Auto-reparacion: si habia basura guardada, se limpia sola.
            if (limpio.length !== arr.length) {
                try { localStorage.setItem(KEY, JSON.stringify(limpio)); } catch (e) {}
            }
            return limpio;
        } catch (e) { return []; }
    }
    function setCart(arr) {
        try { localStorage.setItem(KEY, JSON.stringify((arr || []).filter(idValido).map(String))); } catch (e) {}
    }
    function inCart(id) { return getCart().indexOf(String(id)) !== -1; }
    function toggle(id) {
        if (!idValido(id)) return false;
        id = String(id);
        var cart = getCart();
        var i = cart.indexOf(id);
        if (i === -1) { cart.push(id); } else { cart.splice(i, 1); }
        setCart(cart);
        return i === -1;
    }

    function hasMbox() {
        try { return !!JSON.parse(localStorage.getItem('arcano-mbox') || 'null'); }
        catch (e) { return false; }
    }
    function updateCount() {
        var n = getCart().length + (hasMbox() ? 1 : 0);
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
                // Sincroniza TODOS los botones del mismo disco (el de la ficha y
                // el de la barra de compra fija del celu), no solo el tocado.
                paintButtons();
                updateCount();
                btn.classList.remove('cart-pop');
                void btn.offsetWidth;
                btn.classList.add('cart-pop');
                // Al agregar: abrir el menu lateral. Al quitar: solo un toast.
                if (added) {
                    // Tarjeta del catalogo/carrusel; en la ficha (o su barra de compra), la
                    // ficha misma. Antes caia en "document" y tomaba el nombre del primer
                    // disco de "Similares" (el id era el correcto, el nombre no).
                    var card = btn.closest('.public-card') || btn.closest('.carrusel-card')
                        || (!btn.closest('.public-related') && document.querySelector('.public-disc')) || document;
                    // Animacion "vuela al carrito" (solo al agregar)
                    var _flyImg = card.querySelector('.public-card-cover img') || card.querySelector('.carrusel-cover img') || card.querySelector('.public-disc-cover img');
                    if (window.ArcanoFlyToCart) window.ArcanoFlyToCart(_flyImg);
                    var tit = ((card.querySelector('.public-card-title') || card.querySelector('.carrusel-title-disc') || card.querySelector('.public-disc-title') || {}).textContent || '').trim();
                    var art = ((card.querySelector('.public-card-artist') || card.querySelector('.carrusel-artist') || card.querySelector('.public-disc-artist') || {}).textContent || '').trim();
                    var _precio = parseFloat(btn.dataset.cartPrecio || '');
                    var _item = { item_id: String(id), item_name: (art ? art + ' - ' : '') + tit, quantity: 1 };
                    if (!isNaN(_precio)) _item.price = _precio;
                    if (window.arcanoGA) window.arcanoGA('add_to_cart', { currency: 'UYU', items: [_item] });
                    if (window.ArcanoCartDrawer) window.ArcanoCartDrawer.open((art ? art + ' - ' : '') + tit);
                    else if (window.arcanoToast) window.arcanoToast('\u2713 Agregado al carrito');
                } else if (window.arcanoToast) {
                    window.arcanoToast('Quitado del carrito');
                }
            });
        });
    }

    window.ArcanoCart = {
        rebind: function () { bind(); paintButtons(); updateCount(); },
        get: getCart,
        has: inCart,
        add: function (id) {
            if (!idValido(id)) return false;
            id = String(id);
            var cart = getCart();
            if (cart.indexOf(id) === -1) { cart.push(id); setCart(cart); }
            updateCount();
            paintButtons();
            return true;
        },
        remove: function (id) {
            id = String(id);
            var cart = getCart();
            var i = cart.indexOf(id);
            if (i !== -1) { cart.splice(i, 1); setCart(cart); }
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
        // Accesibilidad: al abrir, mandar el foco al boton de cerrar
        if (closeBtn) setTimeout(function () { try { closeBtn.focus(); } catch (e) {} }, 60);
    }
    function cerrar() {
        drawer.classList.remove('open');
        overlay.classList.remove('visible');
        setTimeout(function () { overlay.hidden = true; }, 250);
        hamburger.setAttribute('aria-expanded', 'false');
        drawer.setAttribute('aria-hidden', 'true');
        document.body.style.overflow = '';
        // Accesibilidad: devolver el foco a la hamburguesa que lo abrio
        try { hamburger.focus(); } catch (e) {}
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
    // Cerrar al tocar afuera (una sola vez; busca los elementos actuales)
    document.addEventListener('click', function (e) {
        if (window.innerWidth > 860) return;
        var sidebar = document.getElementById('public-sidebar');
        var toggle = document.getElementById('sidebar-toggle');
        if (!sidebar || !toggle) return;
        if (!sidebar.classList.contains('open')) return;
        if (sidebar.contains(e.target) || toggle.contains(e.target)) return;
        sidebar.classList.remove('open');
    });
    Arcano.onContent(function () {
        var toggle = document.getElementById('sidebar-toggle');
        var sidebar = document.getElementById('public-sidebar');
        if (!toggle || !sidebar || toggle._sbBound) return;
        toggle._sbBound = true;
        toggle.addEventListener('click', function () { sidebar.classList.toggle('open'); });
    });
})();

/* v3.18 - Auto-aplicar filtros al cambiarlos (con un respiro para agrupar varios
   toques seguidos). pjax intercepta el submit GET, asi que filtra suave sin cortar
   el reproductor y sin recargar toda la pagina. El buscador de texto se excluye
   (se envia con Enter o el boton Buscar). */
(function () {
    'use strict';
    Arcano.onContent(function () {
        var form = document.getElementById('public-filters');
        if (!form || form._autoFilter) return;
        form._autoFilter = true;
        var t = null;
        form.addEventListener('change', function (e) {
            var el = e.target;
            if (!el || !el.name || el.name === 'q') return;
            // En celular NO auto-aplicar los filtros del panel lateral: al re-renderizar
            // (pjax) el panel se cerraria y la pagina saltaria arriba. Ahi se usa el boton
            // "Aplicar filtros". El selector "Orden" (fuera del panel) si auto-aplica.
            if (window.innerWidth <= 860 && el.closest('.public-sidebar')) return;
            if (t) clearTimeout(t);
            t = setTimeout(function () {
                if (typeof form.requestSubmit === 'function') form.requestSubmit();
                else form.submit();
            }, 600);
        });
    });
})();

/* Carruseles de la home — scroll nativo deslizable con el dedo (swipe en mobile),
   flechas en desktop. SIN autoplay: la gente desliza y no se mueve solo (feedback UX
   de Mica: el auto-movimiento mareaba y no era responsive al tacto). */
(function () {
    'use strict';
    Arcano.onContent(function () {
    document.querySelectorAll('.public-carrusel').forEach(function (carrusel) {
        if (carrusel._carInit) return;
        carrusel._carInit = true;
        var viewport = carrusel.querySelector('.public-carrusel-viewport');
        var cards = carrusel.querySelectorAll('.carrusel-card');
        var prev = carrusel.querySelector('.carrusel-prev');
        var next = carrusel.querySelector('.carrusel-next');
        var progressBar = carrusel.querySelector('.public-carrusel-progress-bar');
        if (!viewport || cards.length === 0) return;

        // Un "paso" de flecha = casi todo el ancho visible (una tanda de tapas)
        function paso() { return Math.max(viewport.clientWidth * 0.9, 200); }

        // La barra de abajo es una barra de desplazamiento: el tramo claro mide lo que
        // se ve del carrusel y se mueve con el scroll. Se arrastra (mouse o dedo) y
        // tocando la linea salta a ese punto.
        var track = progressBar ? progressBar.parentElement : null;
        function medidas() {
            var total = viewport.scrollWidth, visible = viewport.clientWidth;
            var frac = total > 0 ? Math.min(1, visible / total) : 1;
            return { max: Math.max(0, total - visible), frac: Math.max(frac, 0.08) };
        }
        function actualizarProgreso() {
            if (!progressBar) return;
            var m = medidas();
            var pos = m.max <= 1 ? 0 : Math.min(1, viewport.scrollLeft / m.max);
            progressBar.style.width = (m.frac * 100) + '%';
            progressBar.style.marginLeft = (pos * (1 - m.frac) * 100) + '%';
            if (track) track.classList.toggle('sin-scroll', m.max <= 1);
        }
        if (track) {
            var arrastrando = false, inicioX = 0, inicioScroll = 0;
            track.addEventListener('pointerdown', function (e) {
                var m = medidas();
                if (m.max <= 1) return;
                var r = track.getBoundingClientRect();
                var br = progressBar.getBoundingClientRect();
                if (e.clientX < br.left || e.clientX > br.right) {
                    // Toco fuera del tramo: centrarlo donde toco y seguir arrastrando desde ahi
                    var util = r.width * (1 - m.frac);
                    var x = e.clientX - r.left - (r.width * m.frac) / 2;
                    viewport.scrollLeft = util > 0 ? Math.max(0, Math.min(1, x / util)) * m.max : 0;
                }
                arrastrando = true;
                inicioX = e.clientX;
                inicioScroll = viewport.scrollLeft;
                track.classList.add('arrastrando');
                viewport.classList.add('sin-snap');
                try { track.setPointerCapture(e.pointerId); } catch (err) {}
                e.preventDefault();
            });
            track.addEventListener('pointermove', function (e) {
                if (!arrastrando) return;
                var m = medidas();
                var util = track.getBoundingClientRect().width * (1 - m.frac);
                if (util <= 0) return;
                viewport.scrollLeft = inicioScroll + (e.clientX - inicioX) * (m.max / util);
            });
            function soltar() {
                if (!arrastrando) return;
                arrastrando = false;
                track.classList.remove('arrastrando');
                viewport.classList.remove('sin-snap');
            }
            track.addEventListener('pointerup', soltar);
            track.addEventListener('pointercancel', soltar);
            window.addEventListener('resize', actualizarProgreso);
        }

        if (next) next.addEventListener('click', function () {
            viewport.scrollBy({ left: paso(), behavior: 'smooth' });
        });
        if (prev) prev.addEventListener('click', function () {
            viewport.scrollBy({ left: -paso(), behavior: 'smooth' });
        });

        // La barra de progreso sigue la posicion real del scroll (throttle con rAF)
        viewport.addEventListener('scroll', function () {
            if (carrusel._raf) return;
            carrusel._raf = requestAnimationFrame(function () {
                carrusel._raf = null;
                actualizarProgreso();
            });
        }, { passive: true });

        actualizarProgreso();
    });
    });
})();

/* Fade-in de portadas al cargar (quita el shimmer). Se re-corre tras cada pjax
   porque las tapas arrancan en opacity:0 hasta que se les pone .loaded. */
(function () {
    'use strict';
    function marcarCargada(img) {
        img.classList.add('loaded');
        var cover = img.closest('.public-card-cover, .carrusel-cover');
        if (cover) cover.classList.add('img-loaded');
    }
    Arcano.onContent(function () {
        document.querySelectorAll('.public-card-cover img, .carrusel-cover img').forEach(function (img) {
            if (img._fadeBound) return;
            img._fadeBound = true;
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
    var timer = null;
    var tipoLabel = { artista: 'Artista', sello: 'Sello', disco: 'Disco' };

    function boxEl() { return document.getElementById('search-suggestions'); }
    function cerrar() { var box = boxEl(); if (box) { box.hidden = true; box.innerHTML = ''; } }

    function buscar(q) {
        fetch('/api/buscar-sugerencias?q=' + encodeURIComponent(q))
            .then(function (r) { return r.json(); })
            .then(function (items) {
                var box = boxEl();
                if (!box) return;
                if (!items.length) { cerrar(); return; }
                box.innerHTML = '';
                items.forEach(function (item) {
                    var el = document.createElement('a');
                    el.className = 'search-suggestion';
                    if (item.tipo === 'disco' && (item.url || item.id)) {
                        el.href = item.url || ('/disco/' + item.id);
                    } else {
                        el.href = '/?q=' + encodeURIComponent(item.texto);
                    }
                    // Usar textContent (NO innerHTML) para no ejecutar HTML que venga
                    // en el texto del disco (artista/sello/titulo del scraper o la IA).
                    var _t = document.createElement('span');
                    _t.className = 'suggestion-text';
                    _t.textContent = item.texto;
                    var _ty = document.createElement('span');
                    _ty.className = 'suggestion-type';
                    _ty.textContent = tipoLabel[item.tipo] || '';
                    el.appendChild(_t);
                    el.appendChild(_ty);
                    box.appendChild(el);
                });
                box.hidden = false;
            }).catch(cerrar);
    }

    // Cerrar al tocar afuera (una sola vez)
    document.addEventListener('click', function (e) {
        var input = document.getElementById('public-search-input');
        var box = boxEl();
        if (!input || !box) return;
        if (!input.contains(e.target) && !box.contains(e.target)) cerrar();
    });

    Arcano.onContent(function () {
        var input = document.getElementById('public-search-input');
        if (!input || input._searchBound) return;
        input._searchBound = true;
        input.addEventListener('input', function () {
            var q = input.value.trim();
            if (timer) clearTimeout(timer);
            if (q.length < 2) { cerrar(); return; }
            timer = setTimeout(function () { buscar(q); }, 250);
        });
        input.addEventListener('blur', function () { setTimeout(cerrar, 200); });
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
    // Links a fichas: las tarjetas del catalogo, los carruseles y las sugerencias.
    // (Las fichas ya no viven en /disco/: su direccion es /artista-titulo.)
    var SEL_FICHA = 'a.public-card[href], a.carrusel-cover-link, a.carrusel-info-link, a.search-suggestion[href^="/"]:not([href^="/?"])';
    document.addEventListener('mouseover', function (e) {
        var a = e.target.closest(SEL_FICHA);
        if (a) prefetch(a.getAttribute('href'));
    });
    document.addEventListener('touchstart', function (e) {
        var a = e.target.closest(SEL_FICHA);
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
            clone.style.transform = 'translate(' + dx + 'px,' + dy + 'px) scale(0.12)';
            clone.style.opacity = '0.25';
        });
        setTimeout(function () {
            clone.remove();
            document.querySelectorAll('.cart-count').forEach(function (c) {
                c.classList.remove('bounce');
                void c.offsetWidth; // reiniciar la animacion
                c.classList.add('bounce');
            });
        }, 1620);   // = duracion del vuelo en CSS (1,6 s; pedido de Facundo: mucho mas lenta)
    }
    // La animacion la dispara el handler del boton (ArcanoCart), que SI sabe si fue
    // agregar o quitar. El listener global de antes usaba una clase mal escrita
    // ('in-cart' en vez de 'is-in-cart'), asi que volaba tambien al quitar.
    window.ArcanoFlyToCart = volarAlCarrito;
})();

/* Menu lateral que aparece al agregar un disco al carrito */
(function () {
    'use strict';
    var drawer = document.getElementById('cart-drawer');
    var overlay = document.getElementById('cart-drawer-overlay');
    if (!drawer || !overlay) return;
    var listEl = document.getElementById('cart-drawer-list');
    var totalEl = document.getElementById('cart-drawer-total');
    var closeBtn = document.getElementById('cart-drawer-close');
    var continueBtn = document.getElementById('cart-drawer-continue');

    function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

    // --- Mystery Box: vive aparte del array de discos (localStorage 'arcano-mbox') ---
    function getBox() {
        try { return JSON.parse(localStorage.getItem('arcano-mbox') || 'null'); }
        catch (e) { return null; }
    }
    function boxPrecio(b) {
        var cfg = window.ARCANO_MBOX || {};
        return (b && cfg.precios && cfg.precios[b.cantidad]) || 0;
    }
    function boxRowHtml(b) {
        var cfg = window.ARCANO_MBOX || {};
        var cover = cfg.cover ? '<img src="' + cfg.cover + '" alt="">' : '<div class="cart-drawer-noimg">&#127873;</div>';
        return '<div class="cart-drawer-row">' +
            '<div class="cart-drawer-cover">' + cover + '</div>' +
            '<div class="cart-drawer-rowinfo">' +
            '<div class="cart-drawer-rowtitle">&#127873; Mystery Box &middot; ' + b.cantidad + ' discos</div>' +
            '<div class="cart-drawer-rowprice">$ ' + boxPrecio(b).toLocaleString('es-UY') + '</div></div>' +
            '<button type="button" class="cart-drawer-remove" data-remove-mbox="1" aria-label="Quitar del carrito" title="Quitar">&times;</button>' +
            '</div>';
    }
    function bindRemoves() {
        listEl.querySelectorAll('.cart-drawer-remove').forEach(function (btn) {
            btn.addEventListener('click', function () {
                if (btn.dataset.removeMbox) {
                    try { localStorage.removeItem('arcano-mbox'); } catch (e) {}
                    if (window.ArcanoCart && window.ArcanoCart.rebind) window.ArcanoCart.rebind();
                } else if (window.ArcanoCart && window.ArcanoCart.remove) {
                    window.ArcanoCart.remove(btn.dataset.removeId);
                }
                renderList();
            });
        });
    }
    function vacio() {
        listEl.innerHTML = '<p class="cart-drawer-empty">Tu carrito está vacío.</p>';
        totalEl.textContent = '';
    }

    function renderList() {
        var ids = (window.ArcanoCart && window.ArcanoCart.get) ? window.ArcanoCart.get() : [];
        var box = getBox();
        if (!ids.length && !box) { vacio(); return; }
        // Solo Mystery Box: no hace falta pedir discos al server.
        if (!ids.length) {
            listEl.innerHTML = boxRowHtml(box);
            totalEl.textContent = 'Total estimado: $ ' + boxPrecio(box).toLocaleString('es-UY');
            bindRemoves();
            return;
        }
        listEl.innerHTML = '<p class="cart-drawer-empty">Cargando...</p>';
        fetch('/api/discos-por-id?ids=' + ids.join(','))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var discos = (data && data.discos) || [];
                if (!discos.length && !box) { vacio(); return; }
                var total = 0, html = '';
                if (box) { total += boxPrecio(box); html += boxRowHtml(box); }
                discos.forEach(function (d) {
                    if (d.disponible && d.precio_uyu) total += d.precio_uyu;
                    var cover = d.cover ? '<img src="' + d.cover + '" alt="">' : '<div class="cart-drawer-noimg">&#9834;</div>';
                    var precio = d.precio_uyu ? ('$ ' + d.precio_uyu.toLocaleString('es-UY')) : (d.disponible ? 'A consultar' : 'No disponible');
                    html += '<div class="cart-drawer-row">' +
                        '<div class="cart-drawer-cover">' + cover + '</div>' +
                        '<div class="cart-drawer-rowinfo">' +
                        '<div class="cart-drawer-rowtitle">' + esc(d.artista) + ' - ' + esc(d.titulo) + '</div>' +
                        '<div class="cart-drawer-rowprice">' + precio + '</div></div>' +
                        '<button type="button" class="cart-drawer-remove" data-remove-id="' + d.id + '" aria-label="Quitar del carrito" title="Quitar">&times;</button>' +
                        '</div>';
                });
                listEl.innerHTML = html;
                bindRemoves();
                totalEl.textContent = 'Total estimado: $ ' + total.toLocaleString('es-UY');
            })
            .catch(function () { listEl.innerHTML = '<p class="cart-drawer-empty">No se pudo cargar el carrito.</p>'; });
    }

    function abrir() {
        overlay.hidden = false;
        setTimeout(function () { overlay.classList.add('visible'); drawer.classList.add('open'); }, 10);
        drawer.setAttribute('aria-hidden', 'false');
        renderList();
    }
    function cerrar() {
        drawer.classList.remove('open');
        overlay.classList.remove('visible');
        setTimeout(function () { overlay.hidden = true; }, 260);
        drawer.setAttribute('aria-hidden', 'true');
    }
    overlay.addEventListener('click', cerrar);
    if (closeBtn) closeBtn.addEventListener('click', cerrar);
    if (continueBtn) continueBtn.addEventListener('click', cerrar);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && drawer.classList.contains('open')) cerrar(); });

    window.ArcanoCartDrawer = { open: abrir, close: cerrar };
})();

/* Barra de anuncio Y banner (hero) -> bajan al catalogo con el mismo movimiento.
   Listener a nivel document (una sola vez): sobrevive a los cambios de pagina pjax.
   - Si estamos en el home (existe #catalogo): scroll SUAVE hasta el catalogo, sin
     recargar. preventDefault() hace que pjax (que corre despues) lo ignore.
   - Si estamos en otra pagina (no hay #catalogo): la barra (un <a href=/#catalogo>)
     navega normal y pjax al cargar el home baja al ancla. El banner solo vive en el
     home, asi que siempre encuentra el #catalogo. */
(function () {
    'use strict';
    document.addEventListener('click', function (e) {
        if (!e.target || !e.target.closest) return;
        var trigger = e.target.closest('.public-anuncio-bar, .public-hero, .ir-catalogo');
        if (!trigger) return;
        var target = document.getElementById('catalogo');
        if (!target) return;  // otra pagina: que el link navegue a /#catalogo
        // "Catalogo" del menu con un filtro o busqueda puesta: ir al catalogo COMPLETO
        // (navega a /#catalogo y sin filtros); sin filtros, solo bajar suave como el banner.
        if (trigger.classList.contains('ir-catalogo') && location.search) return;
        e.preventDefault();
        // Si se toco desde el menu lateral del celular, cerrarlo antes de bajar
        var drawer = document.getElementById('public-drawer');
        if (drawer && drawer.classList.contains('open') && trigger.closest('#public-drawer')) {
            drawer.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true');
            var ov = document.getElementById('public-drawer-overlay');
            if (ov) { ov.classList.remove('visible'); ov.hidden = true; }
            document.body.style.overflow = '';
        }
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        if (history.replaceState) { try { history.replaceState(null, '', '#catalogo'); } catch (x) {} }
    });
})();

/* Grilla siempre con filas COMPLETAS (pedido de Facundo): la cantidad de discos por
   pagina depende de cuantos entran por fila en esta pantalla (compu 5x5, tablet 3x9,
   celular 2x10). La pagina mide las columnas reales y se las pasa al servidor en una
   cookie; si la pagina que llego no cierra justo (primera visita desde el celular), se
   recarga UNA vez con el numero correcto. */
(function () {
    'use strict';
    function columnasReales() {
        var g = document.querySelector('.public-grid[data-per-page]');
        if (!g) return null;
        var cols = (getComputedStyle(g).gridTemplateColumns || '').split(' ').filter(function (x) {
            return x && x !== 'none';
        }).length;
        return cols > 0 ? { grid: g, cols: cols } : null;
    }
    function guardar(cols) {
        try { document.cookie = 'arcano_cols=' + cols + '; path=/; max-age=31536000; samesite=lax'; } catch (e) {}
    }
    function revisar() {
        var m = columnasReales();
        if (!m) return;
        guardar(m.cols);
        var pp = parseInt(m.grid.dataset.perPage || '0', 10);
        var cards = m.grid.children.length;
        // Solo si la pagina vino llena y no cierra con estas columnas: recargar una vez.
        if (pp && cards === pp && pp % m.cols !== 0) {
            var ya = false;
            try { ya = sessionStorage.getItem('arcano_cols_fix') === String(m.cols); } catch (e) {}
            if (!ya) {
                try { sessionStorage.setItem('arcano_cols_fix', String(m.cols)); } catch (e) {}
                location.replace(location.href);
            }
        }
    }
    window.Arcano.onContent(revisar);
    // Girar el celu o achicar la ventana: actualizar la cookie (vale desde la proxima pagina).
    var t = null;
    window.addEventListener('resize', function () {
        clearTimeout(t);
        t = setTimeout(function () { var m = columnasReales(); if (m) guardar(m.cols); }, 300);
    });
})();
