/* Edicion rapida de valores desde el dashboard (sin entrar a la ficha).
   - Precio y stock: click en la celda -> input -> Enter/blur guarda via AJAX.
   - Estado: click en el badge -> select -> cambia y guarda.
*/
(function () {
    'use strict';

    var tabla = document.querySelector('.stock-table');
    if (!tabla) return;

    var estados = (tabla.dataset.estados || '').split('|').filter(Boolean);

    function fmtMiles(n) {
        return Math.round(n).toLocaleString('es-UY').replace(/,/g, '.');
    }

    function guardar(discId, campo, valor) {
        return fetch('/admin/discs/' + discId + '/quick-update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ campo: campo, valor: valor })
        }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); });
    }

    var ESTADO_CLASES = {
        'Disponible': 'badge-disponible', 'Pre-order': 'badge-preorder', 'Encargo': 'badge-encargo',
        'Reservado': 'badge-reservado', 'Vendido': 'badge-vendido',
        'Apartado para mi': 'badge-apartado', 'Descartado': 'badge-descartado'
    };
    function badgeEstado(e) {
        var sp = document.createElement('span');
        sp.className = 'badge ' + (ESTADO_CLASES[e] || 'badge-default');
        sp.textContent = e;
        return sp.outerHTML;
    }

    // Precio de lista; si el disco esta en SALE (data-oferta menor al precio), el de
    // lista va tachado y al lado el que se cobra, en el mismo renglon.
    function pintarPrecio(td, valor) {
        var oferta = Number(td.dataset.oferta || 0);
        var lista = Number(valor) || 0;
        td.innerHTML = '';
        if (oferta > 0 && oferta < lista) {
            var s = document.createElement('s');
            s.className = 'precio-tachado';
            s.textContent = fmtMiles(lista);
            var n = document.createElement('span');
            n.className = 'precio-sale';
            n.textContent = fmtMiles(oferta);
            td.appendChild(s);
            td.appendChild(n);
            td.classList.add('col-precio-sale');
        } else {
            td.textContent = fmtMiles(lista);
            td.classList.remove('col-precio-sale');
        }
    }

    function flashCell(td, ok) {
        td.classList.remove('cell-saved', 'cell-error');
        void td.offsetWidth; // reflow para reiniciar animacion
        td.classList.add(ok ? 'cell-saved' : 'cell-error');
        setTimeout(function () { td.classList.remove('cell-saved', 'cell-error'); }, 1200);
    }

    // ---- Precio y stock (input inline) ----
    tabla.addEventListener('click', function (ev) {
        var td = ev.target.closest('.col-editable[data-edit]');
        if (!td || !tabla.contains(td)) return;
        (function () {
            if (td.querySelector('input')) return; // ya en edicion
            var campo = td.dataset.edit;
            var discId = td.dataset.discId;
            var actual = td.dataset.raw || '0';
            var htmlOriginal = td.innerHTML; // puede traer el precio tachado (SALE)

            var input = document.createElement('input');
            // Precio y stock en texto: un campo numerico del navegador leia "1.800" como
            // 1,8 (y el disco quedaba a $1). El servidor entiende el punto de miles.
            var esEntero = (campo === 'precio_uyu' || campo === 'stock');
            input.type = esEntero ? 'text' : 'number';
            if (esEntero) input.inputMode = 'numeric';
            input.className = 'cell-edit-input';
            input.value = actual;
            if (!esEntero) input.min = '0';
            td.textContent = '';
            td.appendChild(input);
            input.focus({ preventScroll: true });
            input.select();

            var terminado = false;
            function cerrar(guardando) {
                if (terminado) return;
                terminado = true;
                var nuevo = input.value.trim();
                if (!guardando || nuevo === '' || nuevo === actual) {
                    // sin cambios: restaurar
                    td.innerHTML = htmlOriginal;
                    return;
                }
                if (campo === 'precio_uyu') {
                    // "1.800" o "1800" -> 1800 (punto = miles); "$" y espacios afuera
                    var limpio = nuevo.replace(/[\s$]/g, '');
                    var num = /^\d{1,3}(\.\d{3})+$/.test(limpio) ? Number(limpio.replace(/\./g, ''))
                              : Number(limpio.replace(',', '.'));
                    var ant = Number(actual) || 0;
                    if (!isFinite(num) || num < 0) {
                        alert('Ese precio no es valido. Escribilo solo con numeros, ej: 1800 o 1.800');
                        td.innerHTML = htmlOriginal;
                        return;
                    }
                    if (ant > 0 && Math.round(num) < ant * 0.1 &&
                        !confirm('Vas a cambiar el precio de $' + fmtMiles(ant) + ' a $' + fmtMiles(Math.round(num)) + '. ¿Seguro?')) {
                        td.innerHTML = htmlOriginal;
                        return;
                    }
                    nuevo = String(Math.round(num));
                    if (nuevo === actual) { td.innerHTML = htmlOriginal; return; }
                }
                td.textContent = '...';
                guardar(discId, campo, nuevo).then(function (res) {
                    if (res.ok && res.data.ok) {
                        td.dataset.raw = res.data.valor;
                        // Formatear segun el campo
                        if (campo === 'precio_uyu') {
                            pintarPrecio(td, res.data.valor);
                        } else if (campo === 'costo_eur' || campo === 'envio_eur') {
                            td.textContent = Number(res.data.valor).toFixed(2);
                            td.dataset.raw = Number(res.data.valor).toFixed(2);
                        } else {
                            td.textContent = res.data.valor;
                        }
                        flashCell(td, true);
                        var fila = td.closest('tr');
                        // Precio, costo y envio afectan ganancia/margen
                        if (campo === 'precio_uyu' || campo === 'costo_eur' || campo === 'envio_eur') {
                            var celG = fila.querySelector('.col-ganancia');
                            var celM = fila.querySelector('.col-margen');
                            if (celG && res.data.ganancia_uyu !== undefined) {
                                celG.textContent = fmtMiles(res.data.ganancia_uyu);
                                celG.classList.toggle('gain', res.data.ganancia_uyu >= 0);
                                celG.classList.toggle('loss', res.data.ganancia_uyu < 0);
                            }
                            if (celM && res.data.margen_pct !== undefined) {
                                celM.textContent = res.data.margen_pct + '%';
                                celM.classList.toggle('loss', res.data.margen_pct < 0);
                            }
                        }
                        // Costo y envio ademas actualizan el costo final
                        if ((campo === 'costo_eur' || campo === 'envio_eur') && res.data.costo_final_uyu !== undefined) {
                            var celCF = fila.querySelector('.col-costofinal .col-costofinal-val') || fila.querySelector('.col-costofinal');
                            if (celCF) celCF.textContent = fmtMiles(res.data.costo_final_uyu);
                            var celTC = fila.querySelector('.col-dif-tc');
                            if (celTC && res.data.ajuste_tc_uyu !== undefined) {
                                var aj = res.data.ajuste_tc_uyu;
                                celTC.textContent = aj ? ((aj > 0 ? '+' : '') + fmtMiles(aj)) : '—';
                            }
                        }
                    } else {
                        td.innerHTML = htmlOriginal;
                        flashCell(td, false);
                        if (res.data && res.data.error) alert(res.data.error);
                    }
                }).catch(function () {
                    td.innerHTML = htmlOriginal;
                    flashCell(td, false);
                });
            }

            input.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') { e.preventDefault(); cerrar(true); }
                else if (e.key === 'Escape') { cerrar(false); }
            });
            input.addEventListener('blur', function () { cerrar(true); });
        })();
    });

    // ---- Estado (select inline) ----
    tabla.addEventListener('click', function (ev) {
        var td = ev.target.closest('.col-editable-estado');
        if (!td || !tabla.contains(td)) return;
        if (ev.target.closest('.sena-cell')) return;   // la sena tiene su propio manejo
        (function () {
            if (td.querySelector('select')) return;
            var discId = td.dataset.discId;
            var actual = td.dataset.estado;
            var badgeOriginal = td.innerHTML;

            var select = document.createElement('select');
            select.className = 'cell-edit-select';
            estados.forEach(function (e) {
                var opt = document.createElement('option');
                opt.value = e; opt.textContent = e;
                if (e === actual) opt.selected = true;
                select.appendChild(opt);
            });
            td.innerHTML = '';
            td.appendChild(select);
            select.focus({ preventScroll: true });

            var terminado = false;
            function cerrar(guardar_) {
                if (terminado) return;
                terminado = true;
                var nuevo = select.value;
                if (!guardar_ || nuevo === actual) {
                    td.innerHTML = badgeOriginal;
                    return;
                }
                guardar(discId, 'estado', nuevo).then(function (res) {
                    if (res.ok && res.data.ok) {
                        // Sin recargar: el cartel cambia ya y despues se trae la fila
                        // actualizada (stock, fechas, sena, totales) sin moverte de lugar.
                        td.dataset.estado = res.data.valor;
                        td.innerHTML = badgeEstado(res.data.valor);
                        flashCell(td, true);
                        if (window.arcanoStock) window.arcanoStock.refrescar([discId]);
                    } else {
                        td.innerHTML = badgeOriginal;
                        flashCell(td, false);
                    }
                }).catch(function () {
                    td.innerHTML = badgeOriginal;
                    flashCell(td, false);
                });
            }

            select.addEventListener('change', function () { cerrar(true); });
            // El blur GUARDA (no descarta): en un <select> nativo el blur puede
            // dispararse antes que el change y perdiamos la eleccion (el estado
            // "volvia" a Pre-order). Si no cambiaste nada (nuevo === actual) cerrar()
            // revierte sin guardar igual. Escape sigue cancelando explicitamente.
            select.addEventListener('blur', function () { cerrar(true); });
            select.addEventListener('keydown', function (e) {
                if (e.key === 'Escape') cerrar(false);
            });
        })();
    });

    // ---- Audio (estado de digitalizacion: select inline con badge de color) ----
    var AUDIO_CLASES = {
        'En digital': 'badge-audio-digital',
        'Grabado': 'badge-audio-grabado',
        'Para grabar': 'badge-audio-paragrabar',
        'F': 'badge-audio-f'
    };
    var AUDIO_OPCIONES = ['', 'En digital', 'Grabado', 'Para grabar', 'F'];
    function audioBadge(valor) {
        if (!valor) return '<span class="audio-sindef">—</span>';
        return '<span class="badge ' + (AUDIO_CLASES[valor] || 'badge-default') + '">' + valor + '</span>';
    }
    tabla.addEventListener('click', function (ev) {
        var td = ev.target.closest('.col-editable-audio');
        if (!td || !tabla.contains(td)) return;
        (function () {
            if (td.querySelector('select')) return;
            var discId = td.dataset.discId;
            var actual = td.dataset.audio || '';
            var badgeOriginal = td.innerHTML;

            var select = document.createElement('select');
            select.className = 'cell-edit-select';
            AUDIO_OPCIONES.forEach(function (v) {
                var opt = document.createElement('option');
                opt.value = v; opt.textContent = v || '— (sin definir)';
                if (v === actual) opt.selected = true;
                select.appendChild(opt);
            });
            td.innerHTML = '';
            td.appendChild(select);
            select.focus({ preventScroll: true });

            var terminado = false;
            function cerrar(guardar_) {
                if (terminado) return;
                terminado = true;
                var nuevo = select.value;
                if (!guardar_ || nuevo === actual) {
                    td.innerHTML = badgeOriginal;
                    return;
                }
                guardar(discId, 'audio_estado', nuevo).then(function (res) {
                    if (res.ok && res.data.ok) {
                        td.dataset.audio = res.data.valor;
                        td.innerHTML = audioBadge(res.data.valor);
                        flashCell(td, true);
                    } else {
                        td.innerHTML = badgeOriginal;
                        flashCell(td, false);
                    }
                }).catch(function () {
                    td.innerHTML = badgeOriginal;
                    flashCell(td, false);
                });
            }

            select.addEventListener('change', function () { cerrar(true); });
            // Igual que en Estado: el blur guarda (no descarta) para no perder la eleccion.
            select.addEventListener('blur', function () { cerrar(true); });
            select.addEventListener('keydown', function (e) {
                if (e.key === 'Escape') cerrar(false);
            });
        })();
    });

    // ---- Origen (toggle Arcano / Personal) ----
    tabla.addEventListener('click', function (ev) {
        var td = ev.target.closest('.col-editable-origen');
        if (!td || !tabla.contains(td)) return;
        (function () {
            var discId = td.dataset.discId;
            var actual = td.dataset.origen;
            var nuevo = (actual === 'personal') ? 'arcano' : 'personal';
            var original = td.innerHTML;
            td.style.opacity = '0.5';
            guardar(discId, 'origen', nuevo).then(function (res) {
                td.style.opacity = '';
                if (res.ok && res.data.ok) {
                    td.dataset.origen = res.data.valor;
                    if (res.data.valor === 'personal') {
                        td.innerHTML = '<span class="badge badge-personal">Personal</span>';
                    } else {
                        td.innerHTML = '<span class="badge badge-arcano">Arcano</span>';
                    }
                    flashCell(td, true);
                } else {
                    td.innerHTML = original;
                    flashCell(td, false);
                }
            }).catch(function () {
                td.style.opacity = '';
                td.innerHTML = original;
                flashCell(td, false);
            });
        })();
    });

    // ---- Campos de texto (artista, titulo, sello, genero) ----
    tabla.addEventListener('click', function (ev) {
        var el = ev.target.closest('.col-editable-text[data-edit]');
        if (!el || !tabla.contains(el)) return;
        (function () {
            if (el.querySelector('input')) return;
            var campo = el.dataset.edit;
            var discId = el.dataset.discId;
            var actual = el.dataset.raw || '';
            var textoOriginal = el.textContent;

            var input = document.createElement('input');
            input.type = 'text';
            input.className = 'cell-edit-input cell-edit-text';
            input.value = actual;
            el.textContent = '';
            el.appendChild(input);
            input.focus({ preventScroll: true });
            input.select();

            var terminado = false;
            function cerrar(guardando) {
                if (terminado) return;
                terminado = true;
                var nuevo = input.value.trim();
                if (!guardando || nuevo === actual) {
                    el.textContent = textoOriginal;
                    return;
                }
                el.textContent = '...';
                guardar(discId, campo, nuevo).then(function (res) {
                    if (res.ok && res.data.ok) {
                        el.dataset.raw = res.data.valor;
                        el.textContent = res.data.valor || (campo === 'genero' ? '+ genero' : '—');
                        flashCell(el, true);
                    } else {
                        el.textContent = textoOriginal;
                        flashCell(el, false);
                        if (res.data && res.data.error) alert(res.data.error);
                    }
                }).catch(function () {
                    el.textContent = textoOriginal;
                    flashCell(el, false);
                });
            }

            input.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') { e.preventDefault(); cerrar(true); }
                else if (e.key === 'Escape') { cerrar(false); }
            });
            input.addEventListener('blur', function () { cerrar(true); });
        })();
    });

    // ---- Recalcular TC: actualiza el disco al TC global actual y recalcula el costo UYU ----
    tabla.addEventListener('click', function (ev) {
        var btn = ev.target.closest('.tc-recalc-btn');
        if (!btn || !tabla.contains(btn)) return;
        (function () {
            ev.stopPropagation();
            var discId = btn.dataset.discId;
            // Confirmar antes de cambiar el TC: un click sin querer cambiaba el costo de
            // un disco comprado hace tiempo (se rige por el TC de cuando lo compraste).
            var tcU = parseFloat(btn.dataset.tcUsado || '0'), tcA = parseFloat(btn.dataset.tcActual || '0');
            var eur = parseFloat(btn.dataset.eur || '0');
            if (tcU && tcA && Math.abs(tcU - tcA) < 0.005) {
                alert('Este disco ya está calculado con el tipo de cambio de hoy (' + tcA.toFixed(2) + '). No hay nada para actualizar.');
                return;
            }
            var msg = 'Este disco está calculado con el tipo de cambio ' + tcU.toFixed(2) +
                ' (el de cuando lo cargaste).' + String.fromCharCode(10, 10) +
                '¿Pasarlo al de hoy (' + tcA.toFixed(2) + ')?' + String.fromCharCode(10) +
                'Su costo pasaría de $' + fmtMiles(eur * tcU) + ' a $' + fmtMiles(eur * tcA) + '.';
            if (btn.dataset.enPedido === '1') {
                msg += String.fromCharCode(10, 10) + 'Ojo: está en un pedido, así que su costo real ya sale de lo ' +
                    'que pagaste por el pedido. Normalmente NO hace falta.';
            }
            if (!window.confirm(msg)) return;
            var td = btn.closest('.col-costofinal');
            var val = td ? td.querySelector('.col-costofinal-val') : null;
            btn.disabled = true;
            btn.classList.add('spinning');
            guardar(discId, 'recalcular_tc', '1').then(function (res) {
                btn.disabled = false;
                btn.classList.remove('spinning');
                if (res.ok && res.data.ok) {
                    if (val) val.textContent = fmtMiles(res.data.costo_final_uyu);
                    var fila = td.closest('tr');
                    var celG = fila.querySelector('.col-ganancia');
                    var celM = fila.querySelector('.col-margen');
                    if (celG && res.data.ganancia_uyu !== undefined) {
                        celG.textContent = fmtMiles(res.data.ganancia_uyu);
                        celG.classList.toggle('gain', res.data.ganancia_uyu >= 0);
                        celG.classList.toggle('loss', res.data.ganancia_uyu < 0);
                    }
                    if (celM && res.data.margen_pct !== undefined) {
                        celM.textContent = res.data.margen_pct + '%';
                        celM.classList.toggle('loss', res.data.margen_pct < 0);
                    }
                    flashCell(td, true);
                } else {
                    flashCell(td, false);
                    if (res.data && res.data.error) alert(res.data.error);
                }
            }).catch(function () {
                btn.disabled = false;
                btn.classList.remove('spinning');
                if (td) flashCell(td, false);
            });
        })();
    });
})();

/* Marcado rapido para las secciones de la home (destacado / nuevos ingresos) */
(function () {
    'use strict';
    document.addEventListener('change', function (ev) {
        var chk = ev.target.closest ? ev.target.closest('.mark-home') : null;
        if (!chk) return;
        (function () {
            var discId = chk.dataset.discId;
            var campo = chk.dataset.campo;
            var valor = chk.checked ? '1' : '0';
            fetch('/admin/discs/' + discId + '/quick-update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({ campo: campo, valor: valor })
            }).then(function (r) { return r.json(); }).then(function (d) {
                if (!d.ok) { chk.checked = !chk.checked; }
                // "NO mostrar en la web": cartelito debajo del titulo
                if (campo === 'oculto_web') {
                    var chip = document.querySelector('[data-oculto-chip="' + discId + '"]');
                    if (chip) chip.hidden = !chk.checked;
                }
            }).catch(function () { chk.checked = !chk.checked; });
        })();
    });
    // Select de cartel "Nuevo" (auto/si/no)
    document.addEventListener('change', function (ev) {
        var sel = ev.target.closest ? ev.target.closest('.cartel-nuevo-select') : null;
        if (!sel) return;
        (function () {
            var discId = sel.dataset.discId;
            fetch('/admin/discs/' + discId + '/quick-update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({ campo: 'cartel_nuevo', valor: sel.value })
            }).then(function (r) { return r.json(); }).then(function (d) {
                if (!d.ok) { /* revertir no critico */ }
            });
        })();
    });

    // Selector de pedido en el dashboard
    document.addEventListener('change', function (ev) {
        var sel = ev.target.closest ? ev.target.closest('.pedido-select') : null;
        if (!sel) return;
        (function () {
            var discId = sel.dataset.discId;
            var original = sel.dataset.original || '';
            fetch('/admin/discs/' + discId + '/quick-update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({ campo: 'pedido_id', valor: sel.value })
            }).then(function (r) { return r.json(); }).then(function (d) {
                if (d.ok) {
                    sel.dataset.original = sel.value;
                    // Feedback visual breve
                    sel.classList.add('saved');
                    setTimeout(function () { sel.classList.remove('saved'); }, 800);
                } else {
                    sel.value = original; // revertir si fallo
                }
            }).catch(function () { sel.value = original; });
        })();
    });

    // ---- Sena de reserva (chip en la celda de estado) ----
    // Usa un input/botones INLINE en la celda (no prompt(): los dialogos nativos
    // se bloquean en la PWA del celu y no aparecian).
    var SENA_ADD = '<button type="button" class="sena-add" title="Marca que pago sena para reservar">+ sena</button>';
    var SENA_INPUT = '<span class="sena-edit">' +
        '<input type="number" class="sena-input" inputmode="numeric" min="0" step="1" placeholder="$ sena">' +
        '<button type="button" class="sena-ok" title="Guardar">✓</button>' +
        '<button type="button" class="sena-cancel" title="Cancelar">×</button></span>';
    var SENA_RESOLVE = '<span class="sena-edit">' +
        '<button type="button" class="sena-res" data-res="devolver" title="Se la devolvi al cliente (sale de la caja)">Devolvi</button>' +
        '<button type="button" class="sena-res" data-res="quedar" title="Me la quede (queda como ingreso)">Me la quede</button>' +
        '<button type="button" class="sena-cancel" title="Cancelar">×</button></span>';

    function chipSena(monto) {
        return '<button type="button" class="sena-chip" title="Sena cobrada. Click para devolver o quedartela.">sena $ ' + monto + '</button>';
    }
    // guardar propio: este IIFE no tiene acceso al del quick-edit (por eso la
    // sena nunca guardaba: referenciaba variables de otro scope).
    function guardar(discId, campo, valor) {
        return fetch('/admin/discs/' + discId + '/quick-update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ campo: campo, valor: valor })
        }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); });
    }

    function senaCelda(cell) {
        var discId = cell.dataset.discId;

        function cobrar() {
            var inp = cell.querySelector('.sena-input');
            if (!inp) return;
            var val = ('' + inp.value).replace(/[^0-9.,]/g, '');
            if (!val) { inp.focus({ preventScroll: true }); return; }
            guardar(discId, 'sena_cobrar', val).then(function (res) {
                if (res.ok && res.data && res.data.ok) {
                    cell.innerHTML = chipSena(res.data.sena_monto || val);
                } else {
                    alert((res.data && res.data.error) || 'No se pudo guardar la sena.');
                }
            });
        }
        function resolver(accion) {
            guardar(discId, 'sena_resolver', accion).then(function (res) {
                if (res.ok && res.data && res.data.ok) { cell.innerHTML = SENA_ADD; }
                else { alert((res.data && res.data.error) || 'No se pudo.'); }
            });
        }

        return { cobrar: cobrar, resolver: resolver };
    }
    // chipHtml: el chip actual al abrir el resolver (para cancelar), guardado en la celda
    document.addEventListener('click', function (e) {
        var cell = e.target.closest ? e.target.closest('.sena-cell') : null;
        if (!cell) return;
        var acc = senaCelda(cell);
        (function () {
            var t = e.target;
            if (t.closest('.sena-add')) {
                cell.innerHTML = SENA_INPUT;
                var inp = cell.querySelector('.sena-input');
                if (inp) inp.focus({ preventScroll: true });
            } else if (t.closest('.sena-ok')) {
                acc.cobrar();
            } else if (t.closest('.sena-chip')) {
                cell._chipHtml = t.closest('.sena-chip').outerHTML;
                cell.innerHTML = SENA_RESOLVE;
            } else if (t.closest('.sena-res')) {
                acc.resolver(t.closest('.sena-res').dataset.res);
            } else if (t.closest('.sena-cancel')) {
                cell.innerHTML = cell._chipHtml || SENA_ADD;
                cell._chipHtml = null;
            }
        })();
    });
    document.addEventListener('keydown', function (e) {
        if (!e.target.classList || !e.target.classList.contains('sena-input')) return;
        var cell = e.target.closest('.sena-cell');
        if (!cell) return;
        if (e.key === 'Enter') { e.preventDefault(); senaCelda(cell).cobrar(); }
        else if (e.key === 'Escape') { cell.innerHTML = SENA_ADD; }
    });
})();

/* Stock sin recargar (v6.07): despues de un cambio se trae la pagina en segundo
   plano y se actualizan SOLO las filas tocadas y los totales. La ventana queda donde
   estaba (sin saltar arriba ni perder filtros). Tambien la edicion masiva y Eliminar. */
(function () {
    'use strict';
    var tabla = document.querySelector('.stock-table');
    if (!tabla) return;

    function traerPagina() {
        return fetch(window.location.href, {
            credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' }
        }).then(function (r) { return r.text(); })
          .then(function (t) { return new DOMParser().parseFromString(t, 'text/html'); });
    }

    // Copia el contenido de la fila nueva celda por celda. La celda del tilde de
    // seleccion se deja como esta (para no perder lo que tenias seleccionado).
    function copiarFila(vieja, nueva) {
        var cv = vieja.children, cn = nueva.children;
        if (cv.length !== cn.length) return;
        vieja.className = nueva.className;
        for (var i = 0; i < cv.length; i++) {
            var a = cv[i], b = cn[i];
            if (a.querySelector('.row-check')) continue;
            if (a.querySelector('input:focus, select:focus')) continue;   // estas editando ahi
            if (a.innerHTML !== b.innerHTML) a.innerHTML = b.innerHTML;
            a.className = b.className;
            Array.prototype.forEach.call(b.attributes, function (at) {
                if (at.name.indexOf('data-') === 0 || at.name === 'title') a.setAttribute(at.name, at.value);
            });
        }
    }

    function copiar(sel, doc) {
        var a = document.querySelector(sel), b = doc.querySelector(sel);
        if (a && b && a.innerHTML !== b.innerHTML) a.innerHTML = b.innerHTML;
    }

    function refrescar(ids) {
        return traerPagina().then(function (doc) {
            (ids || []).forEach(function (id) {
                var vieja = tabla.querySelector('tbody tr[data-disc-id="' + id + '"]');
                var nueva = doc.querySelector('.stock-table tbody tr[data-disc-id="' + id + '"]');
                if (vieja && nueva) copiarFila(vieja, nueva);
            });
            copiar('.stock-table tfoot', doc);
            copiar('.dash-kpis-top', doc);
            copiar('.summary-cards', doc);
        }).catch(function () { /* si falla, la fila ya muestra el cambio basico */ });
    }

    function aviso(texto, tipo) {
        var stack = document.querySelector('.flash-stack');
        if (!stack) {
            stack = document.createElement('div');
            stack.className = 'flash-stack';
            var main = document.querySelector('.app-main');
            if (main && main.parentNode) main.parentNode.insertBefore(stack, main);
            else document.body.insertBefore(stack, document.body.firstChild);
        }
        var f = document.createElement('div');
        f.className = 'flash flash-' + (tipo || 'success');
        var sp = document.createElement('span');
        sp.textContent = texto;
        f.appendChild(sp);
        stack.appendChild(f);
        if (tipo !== 'error' && tipo !== 'warning') {
            setTimeout(function () {
                f.classList.add('flash-fade');
                setTimeout(function () { if (f.parentNode) f.parentNode.removeChild(f); }, 420);
            }, 3800);
        }
    }

    // Avisos que dejo el servidor en la respuesta (ya renderizados en su HTML)
    function avisosDe(html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        doc.querySelectorAll('.flash-stack .flash').forEach(function (f) {
            var m = (f.className.match(/flash-(\w+)/g) || []).filter(function (c) { return c !== 'flash-stack'; });
            var tipo = m.length ? m[m.length - 1].replace('flash-', '') : 'success';
            aviso((f.textContent || '').trim(), tipo);
        });
    }

    function enviar(form) {
        // getAttribute: el form tiene un campo oculto llamado "action" que tapa form.action
        return fetch(form.getAttribute('action'), {
            method: 'POST', credentials: 'same-origin', body: new FormData(form),
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        }).then(function (r) {
            return r.text().then(function (t) { if (!r.ok) throw new Error('status ' + r.status); return t; });
        });
    }

    function idsDe(form) {
        return Array.prototype.map.call(form.querySelectorAll('input[name="ids[]"]'), function (i) { return i.value; });
    }

    function limpiarSeleccion() {
        var b = document.getElementById('bulk-clear');
        if (b) b.click();
    }

    function sacarFilas(ids) {
        ids.forEach(function (id) {
            var tr = tabla.querySelector('tbody tr[data-disc-id="' + id + '"]');
            if (!tr) return;
            var cb = tr.querySelector('.row-check');
            if (cb) cb.checked = false;
            tr.parentNode.removeChild(tr);
        });
    }

    // Se engancha despues de app.js (que arma los campos ocultos y pide confirmacion):
    // si cancelaste, defaultPrevented ya viene en true y no se hace nada.
    document.addEventListener('DOMContentLoaded', function () {
        var fieldForm = document.getElementById('bulk-field-form');
        if (fieldForm) {
            fieldForm.addEventListener('submit', function (e) {
                if (e.defaultPrevented) return;
                e.preventDefault();
                var ids = idsDe(fieldForm);
                var btn = document.getElementById('bulk-apply-btn');
                if (btn) btn.disabled = true;
                enviar(fieldForm).then(function (html) {
                    avisosDe(html);
                    limpiarSeleccion();
                    return refrescar(ids);
                }).catch(function () {
                    aviso('No se pudo aplicar el cambio. Proba de nuevo.', 'error');
                }).then(function () { if (btn) btn.disabled = false; });
            });
        }
        var delForm = document.getElementById('bulk-delete-form');
        if (delForm) {
            delForm.addEventListener('submit', function (e) {
                if (e.defaultPrevented) return;
                e.preventDefault();
                var ids = idsDe(delForm);
                enviar(delForm).then(function (html) {
                    avisosDe(html);
                    sacarFilas(ids);
                    limpiarSeleccion();
                    return refrescar([]);
                }).catch(function () { aviso('No se pudo eliminar. Proba de nuevo.', 'error'); });
            });
        }
    });

    // "Eliminar" del menu de acciones de cada disco: sin recargar
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (e.defaultPrevented || !form.closest || !form.closest('#row-drawer')) return;
        var m = (form.getAttribute('action') || '').match(/\/admin\/discs\/(\d+)\/delete/);
        if (!m) return;   // Duplicar sigue llevandote a editar la copia
        e.preventDefault();
        enviar(form).then(function (html) {
            avisosDe(html);
            sacarFilas([m[1]]);
            var ov = document.getElementById('row-drawer-overlay');
            if (ov) ov.click();
            return refrescar([]);
        }).catch(function () { aviso('No se pudo eliminar. Proba de nuevo.', 'error'); });
    });

    window.arcanoStock = { refrescar: refrescar, aviso: aviso };
})();
