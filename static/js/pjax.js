/* Navegacion sin recargar (pjax) para la tienda publica.
   Cambia solo <main class="public-main"> y deja vivo el "cascaron" (reproductor,
   carrito, header), asi el reproductor SIGUE SONANDO al cambiar de pagina.

   - Intercepta clicks en links internos y submits de forms GET (busqueda/filtros).
   - Re-ejecuta los <script> inline del contenido nuevo y re-engancha el JS
     (Arcano.runContent + ArcanoCart/ArcanoFavs.rebind).
   - Al entrar a una pagina marcada [data-no-player] (checkout en adelante),
     cierra el reproductor.
   - Formularios POST (checkout, login, comprobante) navegan normal (recarga). */
(function () {
    'use strict';

    if (!window.history || !window.history.pushState || !window.fetch) return;
    if (!document.querySelector('.public-main')) return;

    var ORIGEN = location.origin;
    var cargando = false;

    // --- Barra de progreso arriba ---
    var barra = document.createElement('div');
    barra.className = 'pjax-bar';
    barra.hidden = true;
    document.body.appendChild(barra);
    function mostrarBarra() { barra.hidden = false; barra.classList.remove('done'); void barra.offsetWidth; barra.classList.add('run'); }
    function ocultarBarra() { barra.classList.remove('run'); barra.classList.add('done'); setTimeout(function () { barra.hidden = true; barra.classList.remove('done'); }, 250); }

    function esNavegable(a) {
        if (!a || !a.getAttribute('href')) return false;
        if (a.target && a.target !== '' && a.target !== '_self') return false;
        if (a.hasAttribute('download')) return false;
        if (a.getAttribute('href').charAt(0) === '#') return false;   // ancla en la misma pagina
        var url;
        try { url = new URL(a.href, location.href); } catch (e) { return false; }
        if (url.origin !== ORIGEN) return false;
        if (url.protocol !== 'http:' && url.protocol !== 'https:') return false;
        if (url.pathname.indexOf('/admin') === 0) return false;       // admin: recarga normal
        if (/\.(zip|pdf|csv|xlsx?|db|jpe?g|png|webp|gif|svg|mp3|mp4|xml|txt)$/i.test(url.pathname)) return false;
        // Solo ancla (mismo path+query, distinto hash): que lo maneje el navegador
        if (url.pathname === location.pathname && url.search === location.search && url.hash) return false;
        return true;
    }

    function ejecutarScripts(contenedor) {
        contenedor.querySelectorAll('script').forEach(function (viejo) {
            var nuevo = document.createElement('script');
            for (var i = 0; i < viejo.attributes.length; i++) {
                nuevo.setAttribute(viejo.attributes[i].name, viejo.attributes[i].value);
            }
            nuevo.textContent = viejo.textContent;
            viejo.parentNode.replaceChild(nuevo, viejo);
        });
    }

    function reengancharJS() {
        try { if (window.ArcanoCart && window.ArcanoCart.rebind) window.ArcanoCart.rebind(); } catch (e) {}
        try { if (window.ArcanoFavs && window.ArcanoFavs.rebind) window.ArcanoFavs.rebind(); } catch (e) {}
        try { if (window.ArcanoPlayer && window.ArcanoPlayer.rebind) window.ArcanoPlayer.rebind(); } catch (e) {}
        try { if (window.Arcano && window.Arcano.runContent) window.Arcano.runContent(); } catch (e) {}
    }

    // Cerrar overlays que quedarian tapando la pagina nueva (menu hamburguesa,
    // cajon del carrito) y liberar el scroll del body.
    function cerrarOverlays() {
        ['public-drawer', 'cart-drawer'].forEach(function (id) {
            var d = document.getElementById(id);
            if (d) { d.classList.remove('open'); d.setAttribute('aria-hidden', 'true'); }
        });
        ['public-drawer-overlay', 'cart-drawer-overlay'].forEach(function (id) {
            var o = document.getElementById(id);
            if (o) { o.classList.remove('visible'); o.hidden = true; }
        });
        document.body.style.overflow = '';
    }

    function swap(html, url, push) {
        var doc;
        try { doc = new DOMParser().parseFromString(html, 'text/html'); } catch (e) { location.href = url; return; }
        var nuevoMain = doc.querySelector('.public-main');
        var actual = document.querySelector('.public-main');
        if (!nuevoMain || !actual) { location.href = url; return; }   // no es pagina de tienda

        // Frenar los autoplay de los carruseles salientes (si no, siguen corriendo
        // sobre nodos ya sacados del DOM = fuga de timers).
        actual.querySelectorAll('.public-carrusel').forEach(function (c) {
            if (c._timer) { clearInterval(c._timer); c._timer = null; }
        });

        actual.parentNode.replaceChild(nuevoMain, actual);
        cerrarOverlays();
        if (doc.title) document.title = doc.title;
        var canNuevo = doc.querySelector('link[rel="canonical"]');
        var canActual = document.querySelector('link[rel="canonical"]');
        if (canNuevo && canActual) canActual.setAttribute('href', canNuevo.getAttribute('href'));

        if (push) history.pushState({ pjax: true }, '', url);

        // Scroll: si la URL trae #ancla, bajar a esa seccion; si no, arriba de todo.
        var hash = '';
        try { hash = new URL(url, location.href).hash; } catch (e) {}
        var anchor = hash && hash.length > 1 ? document.getElementById(hash.slice(1)) : null;
        if (anchor) anchor.scrollIntoView(); else window.scrollTo(0, 0);

        // El checkout ("finalizar compra") en adelante no lleva reproductor
        if (nuevoMain.querySelector('[data-no-player]') || nuevoMain.hasAttribute('data-no-player')) {
            if (window.ArcanoPlayer && window.ArcanoPlayer.cerrar) window.ArcanoPlayer.cerrar();
        }

        ejecutarScripts(nuevoMain);
        reengancharJS();

        if (window.gtag) { try { gtag('event', 'page_view', { page_location: url, page_title: document.title }); } catch (e) {} }
    }

    function navegar(url, push) {
        if (cargando) return;
        cargando = true;
        mostrarBarra();
        fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' })
            .then(function (r) {
                if (!r.ok) { location.href = url; return null; }
                if (r.redirected) {
                    var u; try { u = new URL(r.url); } catch (e) { u = null; }
                    if (!u || u.origin !== ORIGEN || u.pathname.indexOf('/admin') === 0) { location.href = r.url; return null; }
                    url = r.url;  // seguir la redireccion interna
                }
                return r.text();
            })
            .then(function (html) { if (html != null) swap(html, url, push); })
            .catch(function () { location.href = url; })
            .then(function () { cargando = false; ocultarBarra(); });
    }

    // Links internos
    document.addEventListener('click', function (e) {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        var a = e.target.closest('a');
        if (!esNavegable(a)) return;
        e.preventDefault();
        navegar(new URL(a.href, location.href).href, true);
    });

    // Forms GET (busqueda / filtros). Los POST navegan normal.
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form || form.tagName !== 'FORM') return;
        if ((form.method || 'get').toLowerCase() !== 'get') return;
        var url;
        try { url = new URL(form.getAttribute('action') || location.pathname, location.href); } catch (err) { return; }
        if (url.origin !== ORIGEN || url.pathname.indexOf('/admin') === 0) return;
        e.preventDefault();
        url.search = new URLSearchParams(new FormData(form)).toString();
        navegar(url.href, true);
    });

    // Atras / adelante del navegador
    window.addEventListener('popstate', function () { navegar(location.href, false); });

    history.replaceState({ pjax: true }, '', location.href);
})();
