/* Buscar un disco con la camara (v6.20).
   1) Sacas una foto (o la elegis de la galeria) -> se achica en el navegador
      (canvas a 1600px, JPEG) para que suba rapido con datos moviles.
   2) El servidor se la manda a la IA, que lee artista / titulo / sello / catalogo,
      y busca en el stock. Vuelven los 5 mejores candidatos.
   3) En cada resultado: Abrir ficha, Marcar vendido y Ya llego (segun el estado).
   Si no encuentra nada, queda el buscador de texto para buscar a mano. */
(function () {
    'use strict';

    var raiz = document.getElementById('bf');
    if (!raiz) return;

    var URL_FOTO = raiz.dataset.urlFoto;
    var URL_TEXTO = raiz.dataset.urlTexto;
    var URL_ESTADO = raiz.dataset.urlEstado;      // .../buscar-foto/0/estado
    var URL_VENTAS = raiz.dataset.urlVentas;
    var HAY_CLAVE = raiz.dataset.api === '1';

    var camara = document.getElementById('bf-camara');
    var galeria = document.getElementById('bf-galeria');
    var disparo = document.getElementById('bf-disparo');
    var disparoTxt = document.getElementById('bf-disparo-txt');
    var proceso = document.getElementById('bf-proceso');
    var preview = document.getElementById('bf-preview');
    var procesoMsg = document.getElementById('bf-proceso-msg');
    var errorBox = document.getElementById('bf-error');
    var leidoBox = document.getElementById('bf-leido');
    var chips = document.getElementById('bf-chips');
    var lista = document.getElementById('bf-resultados');
    var form = document.getElementById('bf-form');
    var inputQ = document.getElementById('bf-q');
    var listaTexto = document.getElementById('bf-resultados-texto');

    var MAX_LADO = 1600;
    var CALIDAD = 0.82;
    var CLAVE_MEMORIA = 'arcano-bf-ultimo';
    var ocupado = false;
    var previewUrl = null;

    var ESTADO_CLASES = {
        'Disponible': 'badge-disponible', 'Pre-order': 'badge-preorder', 'Encargo': 'badge-encargo',
        'Reservado': 'badge-reservado', 'Vendido': 'badge-vendido',
        'Apartado para mi': 'badge-apartado', 'Descartado': 'badge-descartado'
    };

    // ---------- utilidades ----------
    function el(tag, cls, texto) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (texto !== undefined && texto !== null) n.textContent = texto;
        return n;
    }
    function plata(n) {
        return '$ ' + Math.round(n || 0).toLocaleString('es-UY').replace(/,/g, '.');
    }
    function mostrarError(msg) {
        errorBox.textContent = msg || '';
        errorBox.hidden = !msg;
    }
    function urlEstado(id) {
        return URL_ESTADO.replace(/\/0\/estado$/, '/' + id + '/estado');
    }
    function guardarMemoria(datos) {
        try { sessionStorage.setItem(CLAVE_MEMORIA, JSON.stringify(datos)); } catch (e) {}
    }
    function leerMemoria() {
        try {
            var d = JSON.parse(sessionStorage.getItem(CLAVE_MEMORIA) || 'null');
            if (d && d.t && (Date.now() - d.t) < 30 * 60 * 1000) return d;
        } catch (e) {}
        return null;
    }
    // Lee la respuesta como JSON; si vino otra cosa (sesion vencida, foto enorme,
    // error del servidor) devuelve un mensaje claro en vez de romperse.
    function leerJson(r) {
        var tipo = r.headers.get('Content-Type') || '';
        if (tipo.indexOf('application/json') === -1) {
            var msg = 'Algo salió mal en el servidor. Probá de nuevo.';
            if (r.status === 413) msg = 'La foto es demasiado pesada. Probá sacarla de nuevo.';
            else if (r.redirected || r.status === 401 || r.status === 403) msg = 'Tu sesión venció. Recargá la página y volvé a entrar.';
            return Promise.resolve({ ok: false, error: msg });
        }
        return r.json();
    }

    // ---------- 1) achicar la foto en el navegador ----------
    function comprimir(file) {
        return new Promise(function (resolve) {
            if (!file || !window.URL || !document.createElement('canvas').getContext) { resolve(file); return; }
            var url = URL.createObjectURL(file);
            var img = new Image();
            img.onload = function () {
                try {
                    var w = img.naturalWidth, h = img.naturalHeight;
                    var esc = Math.min(1, MAX_LADO / Math.max(w, h));
                    var cw = Math.max(1, Math.round(w * esc)), ch = Math.max(1, Math.round(h * esc));
                    var c = document.createElement('canvas');
                    c.width = cw; c.height = ch;
                    var ctx = c.getContext('2d');
                    ctx.fillStyle = '#fff';   // fondo para PNG con transparencia
                    ctx.fillRect(0, 0, cw, ch);
                    ctx.drawImage(img, 0, 0, cw, ch);
                    c.toBlob(function (b) {
                        URL.revokeObjectURL(url);
                        // JPEG achicado; si el original ya era un JPEG mas liviano, va ese.
                        if (b && (file.type !== 'image/jpeg' || b.size < file.size)) resolve(b);
                        else resolve(file);
                    }, 'image/jpeg', CALIDAD);
                } catch (e) { URL.revokeObjectURL(url); resolve(file); }
            };
            img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
            img.src = url;
        });
    }

    function setOcupado(si, msg) {
        ocupado = si;
        camara.disabled = si || !HAY_CLAVE;
        galeria.disabled = si || !HAY_CLAVE;
        disparo.classList.toggle('is-busy', si);
        disparoTxt.textContent = si ? 'Leyendo…' : (lista.children.length ? 'Sacar otra foto' : 'Sacar foto');
        if (msg) procesoMsg.textContent = msg;
        proceso.classList.toggle('is-busy', si);
    }

    // ---------- 2) mandar la foto ----------
    function procesarArchivo(file) {
        if (!file || ocupado) return;
        mostrarError('');
        leidoBox.hidden = true;
        lista.innerHTML = '';
        proceso.hidden = false;
        setOcupado(true, 'Preparando la foto…');
        if (previewUrl) { try { URL.revokeObjectURL(previewUrl); } catch (e) {} }
        previewUrl = URL.createObjectURL(file);
        preview.src = previewUrl;
        proceso.scrollIntoView({ behavior: 'smooth', block: 'start' });

        comprimir(file).then(function (blob) {
            procesoMsg.textContent = 'Leyendo la etiqueta con la IA…';
            var fd = new FormData();
            fd.append('foto', blob, 'foto.jpg');
            var ctrl = window.AbortController ? new AbortController() : null;
            var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 75000) : null;
            return fetch(URL_FOTO, {
                method: 'POST', body: fd, credentials: 'same-origin',
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                signal: ctrl ? ctrl.signal : undefined
            }).then(function (r) {
                if (timer) clearTimeout(timer);
                return leerJson(r);
            });
        }).then(function (data) {
            setOcupado(false);
            proceso.hidden = true;
            if (!data || !data.ok) {
                mostrarError((data && data.error) || 'No se pudo leer la foto. Probá de nuevo.');
                return;
            }
            mostrarResultadoFoto(data, true);
        }).catch(function (e) {
            setOcupado(false);
            proceso.hidden = true;
            mostrarError(e && e.name === 'AbortError'
                ? 'La IA tardó demasiado. Revisá la señal y probá de nuevo.'
                : 'No hay conexión con el servidor. Revisá la señal y probá de nuevo.');
        });
    }

    function alElegir(ev) {
        var f = ev.target.files && ev.target.files[0];
        ev.target.value = '';   // permite elegir la misma foto otra vez
        procesarArchivo(f);
    }
    camara.addEventListener('change', alElegir);
    galeria.addEventListener('change', alElegir);

    // ---------- 3) mostrar lo leido y los resultados ----------
    function mostrarLeido(leido, nadaLeido) {
        chips.innerHTML = '';
        var pares = [['Artista', leido.artista], ['Título', leido.titulo], ['Sello', leido.sello],
                     ['Catálogo', leido.catalogo]];
        if (leido.tracks && leido.tracks.length) pares.push(['Temas', leido.tracks.join(' · ')]);
        var alguno = false;
        pares.forEach(function (p) {
            if (!p[1]) return;
            alguno = true;
            var c = el('span', 'bf-chip');
            c.appendChild(el('span', 'bf-chip-k', p[0]));
            c.appendChild(el('span', 'bf-chip-v', p[1]));
            chips.appendChild(c);
        });
        if (!alguno || nadaLeido) {
            chips.innerHTML = '';
            chips.appendChild(el('span', 'bf-nada', 'No pude leer nada claro en la foto.'));
        }
        leidoBox.hidden = false;
    }

    // Para el buscador a mano se sugiere UN dato (el artista, o el titulo si es un
    // compilado): con todo junto casi nunca aparece nada si la foto no lo encontro.
    function textoParaBuscar(leido) {
        var art = leido.artista && !/^(various|va|v\/a|various artists)$/i.test(leido.artista) ? leido.artista : '';
        return art || leido.titulo || leido.catalogo || leido.sello || '';
    }

    function mostrarResultadoFoto(data, guardar) {
        var leido = data.leido || {};
        mostrarLeido(leido, data.nada_leido);
        lista.innerHTML = '';
        var res = data.resultados || [];
        if (!res.length) {
            var vacio = el('div', 'bf-vacio');
            vacio.appendChild(el('p', 'bf-vacio-t', data.nada_leido
                ? 'Probá más cerca, con buena luz y sin reflejos.'
                : 'No encontré este disco en el stock.'));
            vacio.appendChild(el('p', 'bf-vacio-s', 'Podés buscarlo escribiendo acá abajo.'));
            lista.appendChild(vacio);
            var sug = textoParaBuscar(leido);
            if (sug && !inputQ.value) inputQ.value = sug;
            if (guardar) setTimeout(function () { form.scrollIntoView({ behavior: 'smooth', block: 'center' }); }, 60);
        } else {
            res.forEach(function (it, i) { lista.appendChild(tarjeta(it, i === 0)); });
            // Recien sacada la foto: llevar la vista a lo que leyo y los resultados.
            if (guardar) setTimeout(function () { leidoBox.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 60);
        }
        disparoTxt.textContent = 'Sacar otra foto';
        if (guardar) guardarMemoria({ t: Date.now(), tipo: 'foto', data: data });
    }

    function badge(estado) {
        return el('span', 'badge ' + (ESTADO_CLASES[estado] || 'badge-default'), estado);
    }

    function tarjeta(it, primero) {
        var card = el('article', 'bf-card');
        card.dataset.id = it.id;
        if (primero && it.nivel === 'alta') card.classList.add('is-top');

        var tapa = el('a', 'bf-tapa');
        tapa.href = it.url_ficha;
        tapa.setAttribute('aria-label', 'Abrir ficha de ' + it.artista + ' — ' + it.titulo);
        if (it.cover) {
            var img = el('img', 'cover-thumb');
            img.src = it.cover; img.alt = ''; img.loading = 'lazy';
            tapa.appendChild(img);
        } else {
            tapa.appendChild(el('span', 'bf-tapa-vacia', '♪'));
        }
        card.appendChild(tapa);

        var info = el('div', 'bf-info');
        if (it.nivel) {
            var etiqueta = { alta: 'Coincide', media: 'Puede ser', baja: 'Se parece' }[it.nivel] || '';
            if (primero && it.nivel === 'alta') etiqueta = 'Es este, casi seguro';
            var cab = el('div', 'bf-nivel bf-nivel-' + it.nivel, etiqueta);
            if (it.motivos && it.motivos.length) cab.appendChild(el('span', 'bf-motivos', ' · ' + it.motivos.join(', ')));
            info.appendChild(cab);
        }
        var tit = el('a', 'bf-titulo');
        tit.href = it.url_ficha;
        tit.appendChild(el('span', 'bf-artista', it.artista));
        tit.appendChild(document.createTextNode(' — ' + it.titulo));
        info.appendChild(tit);
        if (it.sello) info.appendChild(el('div', 'bf-sello', it.sello));
        var meta = el('div', 'bf-meta');
        meta.appendChild(badge(it.estado));
        var precio = el('span', 'bf-precio', plata(it.precio));
        meta.appendChild(precio);
        if (it.precio_lista && it.precio_lista !== it.precio && it.estado !== 'Vendido') {
            meta.appendChild(el('s', 'bf-precio-lista', plata(it.precio_lista)));
        }
        info.appendChild(meta);
        card.appendChild(info);

        var acciones = el('div', 'bf-acciones');
        var abrir = el('a', 'btn btn-ghost', 'Abrir ficha');
        abrir.href = it.url_ficha;
        acciones.appendChild(abrir);
        if (it.puede_vender) {
            var bv = el('button', 'btn btn-primary', 'Marcar vendido');
            bv.type = 'button';
            bv.addEventListener('click', function () { accion(card, it, 'vendido', bv); });
            acciones.appendChild(bv);
        }
        if (it.puede_llegar) {
            var bl = el('button', 'btn btn-ghost bf-llego', 'Ya llegó');
            bl.type = 'button';
            bl.addEventListener('click', function () { accion(card, it, 'llego', bl); });
            acciones.appendChild(bl);
        }
        card.appendChild(acciones);
        card.appendChild(el('div', 'bf-aviso', ''));
        card.lastChild.hidden = true;
        return card;
    }

    function avisoEnTarjeta(card, texto, tipo, link) {
        var box = card.querySelector('.bf-aviso');
        box.className = 'bf-aviso' + (tipo ? ' is-' + tipo : '');
        box.textContent = texto;
        if (link) {
            box.appendChild(document.createTextNode(' '));
            var a = el('a', 'bf-aviso-link', link.texto);
            a.href = link.href;
            box.appendChild(a);
        }
        box.hidden = !texto;
    }

    // ---------- 4) acciones rapidas ----------
    function accion(card, it, tipo, boton) {
        var nombre = it.artista + ' — ' + it.titulo;
        var pregunta;
        if (tipo === 'vendido') {
            pregunta = '¿Marcar como VENDIDO?\n\n' + nombre + '\n' + plata(it.precio);
            if (it.aviso_vender) pregunta += '\n\n' + it.aviso_vender;
        } else {
            pregunta = (it.llego_destino === 'Reservado'
                ? '¿Ya llegó? Pasa a Reservado (no sale a la venta).\n\n'
                : '¿Ya llegó? Pasa a Disponible y se puede comprar en la tienda.\n\n') + nombre;
            if (it.aviso_llego) pregunta += '\n\n' + it.aviso_llego;
        }
        if (!window.confirm(pregunta)) return;
        var botones = card.querySelectorAll('.bf-acciones button');
        for (var i = 0; i < botones.length; i++) botones[i].disabled = true;
        boton.textContent = 'Guardando…';
        fetch(urlEstado(it.id), {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ accion: tipo })
        }).then(leerJson).then(function (data) {
            if (data && data.disco) {
                var nueva = tarjeta(data.disco, card.classList.contains('is-top'));
                if (card.classList.contains('is-top')) nueva.classList.add('is-top');
                // Conservar la etiqueta de coincidencia de la tarjeta original
                var nivelViejo = card.querySelector('.bf-nivel');
                if (nivelViejo) {
                    var info = nueva.querySelector('.bf-info');
                    info.insertBefore(nivelViejo, info.firstChild);
                }
                card.parentNode.replaceChild(nueva, card);
                card = nueva;
                actualizarMemoria(data.disco);
            }
            if (data && data.ok) {
                if (tipo === 'vendido') {
                    avisoEnTarjeta(card, 'Listo, quedó vendido.', 'ok',
                        { texto: 'Cargar canal y comprador en Ventas →', href: URL_VENTAS });
                } else {
                    avisoEnTarjeta(card, (data.disco && data.disco.estado === 'Reservado')
                        ? 'Listo, llegó y quedó Reservado para el cliente.'
                        : 'Listo, ya está Disponible en la tienda.', 'ok');
                }
            } else {
                if (!data || !data.disco) {
                    for (var j = 0; j < botones.length; j++) botones[j].disabled = false;
                    boton.textContent = tipo === 'vendido' ? 'Marcar vendido' : 'Ya llegó';
                }
                avisoEnTarjeta(card, (data && data.error) || 'No se pudo guardar. Probá de nuevo.', 'error');
            }
        }).catch(function () {
            for (var j = 0; j < botones.length; j++) botones[j].disabled = false;
            boton.textContent = tipo === 'vendido' ? 'Marcar vendido' : 'Ya llegó';
            avisoEnTarjeta(card, 'Sin conexión: no se guardó. Probá de nuevo.', 'error');
        });
    }

    // Si volvés de una ficha, la lista se restaura con el estado nuevo del disco.
    function actualizarMemoria(disco) {
        var m = leerMemoria();
        if (!m || !m.data || !m.data.resultados) return;
        m.data.resultados = m.data.resultados.map(function (r) {
            if (r.id !== disco.id) return r;
            var nuevo = {};
            Object.keys(disco).forEach(function (k) { nuevo[k] = disco[k]; });
            nuevo.nivel = r.nivel; nuevo.confianza = r.confianza; nuevo.motivos = r.motivos;
            return nuevo;
        });
        guardarMemoria(m);
    }

    // ---------- 5) buscador a mano ----------
    function buscarTexto(q, guardar) {
        q = (q || '').trim();
        listaTexto.innerHTML = '';
        if (!q) return;
        listaTexto.appendChild(el('p', 'bf-cargando', 'Buscando…'));
        fetch(URL_TEXTO + '?q=' + encodeURIComponent(q), {
            credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' }
        }).then(leerJson).then(function (data) {
            listaTexto.innerHTML = '';
            if (!data || !data.ok) {
                listaTexto.appendChild(el('p', 'bf-vacio-s', (data && data.error) || 'No se pudo buscar.'));
                return;
            }
            var res = data.resultados || [];
            if (!res.length) {
                var v = el('div', 'bf-vacio');
                v.appendChild(el('p', 'bf-vacio-t', 'Nada en el stock con “' + q + '”.'));
                v.appendChild(el('p', 'bf-vacio-s', 'Probá con una sola palabra: el artista, el sello o el número de catálogo.'));
                listaTexto.appendChild(v);
            } else {
                res.forEach(function (it) {
                    it.nivel = null;   // en la busqueda a mano no hay "coincidencia" que mostrar
                    listaTexto.appendChild(tarjeta(it, false));
                });
            }
            if (guardar) {
                var m = leerMemoria() || { t: Date.now() };
                m.q = q; m.t = Date.now();
                guardarMemoria(m);
            }
        }).catch(function () {
            listaTexto.innerHTML = '';
            listaTexto.appendChild(el('p', 'bf-vacio-s', 'Sin conexión. Probá de nuevo.'));
        });
    }
    form.addEventListener('submit', function (ev) {
        ev.preventDefault();
        if (inputQ.blur) inputQ.blur();   // baja el teclado del celu
        buscarTexto(inputQ.value, true);
    });

    // ---------- al abrir: lo que venia en la URL o lo ultimo que buscaste ----------
    var qInicial = inputQ.value;
    var memoria = leerMemoria();
    if (memoria && memoria.data && memoria.tipo === 'foto') mostrarResultadoFoto(memoria.data, false);
    if (qInicial) buscarTexto(qInicial, false);
    else if (memoria && memoria.q) { inputQ.value = memoria.q; buscarTexto(memoria.q, false); }
})();
