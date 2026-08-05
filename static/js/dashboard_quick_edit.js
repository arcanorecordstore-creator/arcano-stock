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
                            var celCF = fila.querySelector('.col-costofinal');
                            if (celCF) celCF.textContent = fmtMiles(res.data.costo_final_uyu);
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
            select.addEventListener('blur', function () { cerrar(false); });
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
})();
