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

    function flashCell(td, ok) {
        td.classList.remove('cell-saved', 'cell-error');
        void td.offsetWidth; // reflow para reiniciar animacion
        td.classList.add(ok ? 'cell-saved' : 'cell-error');
        setTimeout(function () { td.classList.remove('cell-saved', 'cell-error'); }, 1200);
    }

    // ---- Precio y stock (input inline) ----
    tabla.querySelectorAll('.col-editable[data-edit]').forEach(function (td) {
        td.addEventListener('click', function () {
            if (td.querySelector('input')) return; // ya en edicion
            var campo = td.dataset.edit;
            var discId = td.dataset.discId;
            var actual = td.dataset.raw || '0';
            var textoOriginal = td.textContent;

            var input = document.createElement('input');
            input.type = 'number';
            input.className = 'cell-edit-input';
            input.value = actual;
            input.min = '0';
            if (campo === 'precio_uyu') input.step = '10';
            td.textContent = '';
            td.appendChild(input);
            input.focus();
            input.select();

            var terminado = false;
            function cerrar(guardando) {
                if (terminado) return;
                terminado = true;
                var nuevo = input.value.trim();
                if (!guardando || nuevo === '' || nuevo === actual) {
                    // sin cambios: restaurar
                    td.textContent = textoOriginal;
                    return;
                }
                td.textContent = '...';
                guardar(discId, campo, nuevo).then(function (res) {
                    if (res.ok && res.data.ok) {
                        td.dataset.raw = res.data.valor;
                        // Formatear segun el campo
                        if (campo === 'precio_uyu') {
                            td.textContent = fmtMiles(res.data.valor);
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
                        td.textContent = textoOriginal;
                        flashCell(td, false);
                    }
                }).catch(function () {
                    td.textContent = textoOriginal;
                    flashCell(td, false);
                });
            }

            input.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') { e.preventDefault(); cerrar(true); }
                else if (e.key === 'Escape') { cerrar(false); }
            });
            input.addEventListener('blur', function () { cerrar(true); });
        });
    });

    // ---- Estado (select inline) ----
    tabla.querySelectorAll('.col-editable-estado').forEach(function (td) {
        td.addEventListener('click', function () {
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
            select.focus();

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
                        // recargar para reflejar badge con su color correcto y filtros
                        window.location.reload();
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
        });
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
    tabla.querySelectorAll('.col-editable-audio').forEach(function (td) {
        td.addEventListener('click', function () {
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
            select.focus();

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
        });
    });

    // ---- Origen (toggle Arcano / Personal) ----
    tabla.querySelectorAll('.col-editable-origen').forEach(function (td) {
        td.addEventListener('click', function () {
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
        });
    });

    // ---- Campos de texto (artista, titulo, sello, genero) ----
    tabla.querySelectorAll('.col-editable-text[data-edit]').forEach(function (el) {
        el.addEventListener('click', function () {
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
            input.focus();
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
        });
    });

    // ---- Recalcular TC: actualiza el disco al TC global actual y recalcula el costo UYU ----
    tabla.querySelectorAll('.tc-recalc-btn').forEach(function (btn) {
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
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
        });
    });
})();

/* Marcado rapido para las secciones de la home (destacado / nuevos ingresos) */
(function () {
    'use strict';
    document.querySelectorAll('.mark-home').forEach(function (chk) {
        chk.addEventListener('change', function () {
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
        });
    });
    // Select de cartel "Nuevo" (auto/si/no)
    document.querySelectorAll('.cartel-nuevo-select').forEach(function (sel) {
        sel.addEventListener('change', function () {
            var discId = sel.dataset.discId;
            fetch('/admin/discs/' + discId + '/quick-update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({ campo: 'cartel_nuevo', valor: sel.value })
            }).then(function (r) { return r.json(); }).then(function (d) {
                if (!d.ok) { /* revertir no critico */ }
            });
        });
    });

    // Selector de pedido en el dashboard
    document.querySelectorAll('.pedido-select').forEach(function (sel) {
        sel.addEventListener('change', function () {
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
        });
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

    document.querySelectorAll('.sena-cell').forEach(function (cell) {
        var discId = cell.dataset.discId;
        var chipHtml = null; // guarda el chip actual al abrir el resolver (para cancelar)

        function cobrar() {
            var inp = cell.querySelector('.sena-input');
            if (!inp) return;
            var val = ('' + inp.value).replace(/[^0-9.,]/g, '');
            if (!val) { inp.focus(); return; }
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

        cell.addEventListener('click', function (e) {
            e.stopPropagation(); // no abrir el editor de estado de esta celda
            var t = e.target;
            if (t.closest('.sena-add')) {
                cell.innerHTML = SENA_INPUT;
                var inp = cell.querySelector('.sena-input');
                if (inp) inp.focus();
            } else if (t.closest('.sena-ok')) {
                cobrar();
            } else if (t.closest('.sena-chip')) {
                chipHtml = t.closest('.sena-chip').outerHTML;
                cell.innerHTML = SENA_RESOLVE;
            } else if (t.closest('.sena-res')) {
                resolver(t.closest('.sena-res').dataset.res);
            } else if (t.closest('.sena-cancel')) {
                cell.innerHTML = chipHtml || SENA_ADD;
                chipHtml = null;
            }
        });
        cell.addEventListener('keydown', function (e) {
            if (!e.target.classList || !e.target.classList.contains('sena-input')) return;
            if (e.key === 'Enter') { e.preventDefault(); cobrar(); }
            else if (e.key === 'Escape') { cell.innerHTML = SENA_ADD; }
        });
    });
})();
