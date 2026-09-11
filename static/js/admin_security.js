/* Todos los POST del admin, incluidos los editores AJAX, usan el token de sesion. */
(function () {
    'use strict';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (!meta) return;
    var token = meta.content;
    var originalFetch = window.fetch;
    window.fetch = function (input, options) {
        var url = new URL(input instanceof Request ? input.url : input, window.location.href);
        var method = ((options && options.method) || (input instanceof Request && input.method) || 'GET').toUpperCase();
        if (url.origin === window.location.origin && url.pathname.indexOf('/admin/') === 0 && !/^(GET|HEAD|OPTIONS)$/.test(method)) {
            options = Object.assign({}, options || {});
            var headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
            headers.set('X-CSRF-Token', token);
            options.headers = headers;
        }
        return originalFetch.call(this, input, options);
    };
    document.addEventListener('submit', function (event) {
        var form = event.target;
        if (form.method.toLowerCase() === 'get') return;
        var url = new URL(form.action || location.href, location.href);
        if (url.origin !== location.origin || url.pathname.indexOf('/admin/') !== 0) return;
        if (!form.querySelector('input[name="_csrf_token"]')) {
            var input = document.createElement('input');
            input.type = 'hidden'; input.name = '_csrf_token'; input.value = token;
            form.appendChild(input);
        }
    }, true);
})();
