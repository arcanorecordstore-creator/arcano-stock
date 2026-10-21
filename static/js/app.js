// Arcano Stock - JS de la app
(function () {
  'use strict';

  // -------------------------------------------------------------------------
  // Calculo en vivo en el formulario de disco
  // -------------------------------------------------------------------------

  function fmtUyu(n) {
    if (!isFinite(n)) return '—';
    return Math.round(n).toLocaleString('es-UY').replace(/,/g, '.');
  }

  function fmtPct(n) {
    if (!isFinite(n)) return '—';
    return n.toFixed(1) + '%';
  }

  function parseNum(value) {
    if (value === null || value === undefined) return 0;
    var s = String(value).trim().replace(',', '.');
    if (!s) return 0;
    var n = parseFloat(s);
    return isNaN(n) ? 0 : n;
  }

  function recalc(form) {
    var tc = parseNum(form.dataset.tc || '47');
    var costo = parseNum(form.querySelector('#costo_eur').value);
    var envio = parseNum(form.querySelector('#envio_eur').value);
    var precio = parseNum(form.querySelector('#precio_uyu').value);

    var costoFinalUyu = (costo + envio) * tc;
    var ganancia = precio - costoFinalUyu;
    var margen = precio > 0 ? (ganancia / precio) * 100 : 0;

    var elCosto = document.getElementById('calc-costo-uyu');
    var elGan = document.getElementById('calc-ganancia');
    var elMar = document.getElementById('calc-margen');

    if (elCosto) elCosto.textContent = fmtUyu(costoFinalUyu);
    if (elGan) {
      elGan.textContent = fmtUyu(ganancia);
      elGan.classList.toggle('calc-loss', ganancia < 0);
    }
    if (elMar) {
      elMar.textContent = fmtPct(margen);
      elMar.classList.toggle('calc-loss', margen < 0);
    }
  }

  function initFormCalc() {
    var form = document.querySelector('.disc-form');
    if (!form) return;
    var inputs = form.querySelectorAll('[data-calc="cost"]');
    inputs.forEach(function (inp) {
      inp.addEventListener('input', function () { recalc(form); });
    });
    recalc(form);
  }

  // -------------------------------------------------------------------------
  // Confirmaciones (data-confirm en formularios de eliminacion)
  // -------------------------------------------------------------------------

  function initConfirm() {
    document.querySelectorAll('form[data-confirm]').forEach(function (form) {
      form.addEventListener('submit', function (ev) {
        var msg = form.getAttribute('data-confirm');
        if (!window.confirm(msg)) {
          ev.preventDefault();
        }
      });
    });
  }

  // -------------------------------------------------------------------------
  // Menus de accion (cambio de estado)
  // -------------------------------------------------------------------------

  function closeAllMenus() {
    document.querySelectorAll('[data-menu-list]').forEach(function (m) {
      m.classList.remove('open');
    });
  }

  function initMenus() {
    document.querySelectorAll('[data-menu-trigger]').forEach(function (btn) {
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var list = btn.parentElement.querySelector('[data-menu-list]');
        var wasOpen = list.classList.contains('open');
        closeAllMenus();
        if (!wasOpen) list.classList.add('open');
      });
    });
    document.addEventListener('click', function (ev) {
      if (!ev.target.closest('[data-menu-list]') && !ev.target.matches('[data-menu-trigger]')) {
        closeAllMenus();
      }
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') closeAllMenus();
    });
  }

  // -------------------------------------------------------------------------
  // Auto-dismiss de flash messages
  // -------------------------------------------------------------------------

  function initFlashes() {
    document.querySelectorAll('.flash').forEach(function (el) {
      setTimeout(function () {
        el.classList.add('flash-fade');
        setTimeout(function () { el.remove(); }, 400);
      }, 4500);
    });
  }

  // -------------------------------------------------------------------------
  // Selección múltiple en dashboard
  // -------------------------------------------------------------------------

  function initBulkSelect() {
    var selectAll = document.getElementById('select-all');
    var rowChecks = document.querySelectorAll('.row-check');
    var bulkBar = document.getElementById('bulk-bar');
    var bulkCountNum = document.getElementById('bulk-count-num');
    var bulkClear = document.getElementById('bulk-clear');
    var bulkFieldIds = document.getElementById('bulk-field-ids');
    var bulkDeleteIds = document.getElementById('bulk-delete-ids');

    if (!selectAll || rowChecks.length === 0 || !bulkBar) return;

    var fieldForm = document.getElementById('bulk-field-form');
    var fieldSelector = document.getElementById('bulk-field-selector');
    var valEstado = document.getElementById('bulk-val-estado');
    var valBool = document.getElementById('bulk-val-bool');
    var valNuevo = document.getElementById('bulk-val-nuevo');
    var valSale = document.getElementById('bulk-val-sale');
    var valPct = document.getElementById('bulk-val-pct');
    var valHasta = document.getElementById('bulk-val-hasta');

    function getSelected() {
      var ids = [];
      rowChecks.forEach(function (cb) { if (cb.checked) ids.push(cb.value); });
      return ids;
    }

    function syncBar() {
      var ids = getSelected();
      var n = ids.length;
      if (n > 0) {
        bulkBar.hidden = false;
        bulkCountNum.textContent = n;
        var html = ids.map(function (id) {
          return '<input type="hidden" name="ids[]" value="' + id + '">';
        }).join('');
        bulkFieldIds.innerHTML = html;
        bulkDeleteIds.innerHTML = html;
      } else {
        bulkBar.hidden = true;
        bulkFieldIds.innerHTML = '';
        bulkDeleteIds.innerHTML = '';
      }
      if (n === 0) {
        selectAll.checked = false; selectAll.indeterminate = false;
      } else if (n === rowChecks.length) {
        selectAll.checked = true; selectAll.indeterminate = false;
      } else {
        selectAll.checked = false; selectAll.indeterminate = true;
      }
    }

    // Mostrar el selector de valor correcto segun el campo elegido
    function updateValueSelector() {
      if (!fieldSelector) return;
      var campo = fieldSelector.value;
      // Ocultar todos
      [valEstado, valBool, valNuevo].forEach(function (el) {
        if (el) { el.hidden = true; el.disabled = true; }
      });
      if (valSale) valSale.hidden = true;
      if (valPct) valPct.disabled = true;
      if (valHasta) valHasta.disabled = true;
      if (campo === 'status' && valEstado) {
        valEstado.hidden = false; valEstado.disabled = false;
      } else if (campo === 'cartel_nuevo' && valNuevo) {
        valNuevo.hidden = false; valNuevo.disabled = false;
      } else if (campo === 'poner_oferta' && valSale) {
        valSale.hidden = false;
        if (valPct) { valPct.disabled = false; valPct.focus(); }
        if (valHasta) valHasta.disabled = false;
      } else if (campo === 'quitar_oferta') {
        // no necesita valor
      } else if (campo && valBool) {
        // campos booleanos: destacado, en_nuevos, es_preorder, cartel_ultima, mostrar_agotado
        valBool.hidden = false; valBool.disabled = false;
      }
    }

    if (fieldSelector) {
      fieldSelector.addEventListener('change', updateValueSelector);
      updateValueSelector();
    }

    // Al enviar el form, armar los campos action/campo/valor correctos
    if (fieldForm) {
      fieldForm.addEventListener('submit', function (e) {
        var campo = fieldSelector ? fieldSelector.value : '';
        if (!campo) { e.preventDefault(); alert('Elegi que queres cambiar.'); return; }
        if (campo === 'poner_oferta' && !(parseFloat(valPct && valPct.value) >= 1 && parseFloat(valPct.value) <= 90)) {
          e.preventDefault(); alert('Poné el % de descuento (entre 1 y 90).'); if (valPct) valPct.focus(); return;
        }
        // Confirmacion: la edicion masiva toca varios discos a la vez
        var nSel = getSelected().length;
        if (!window.confirm('Vas a aplicar este cambio a ' + nSel + ' disco' + (nSel === 1 ? '' : 's') + '. Continuar?')) {
          e.preventDefault();
          return;
        }
        // Limpiar hidden previos
        fieldForm.querySelectorAll('.bulk-dyn').forEach(function (el) { el.remove(); });
        function addHidden(name, value) {
          var i = document.createElement('input');
          i.type = 'hidden'; i.name = name; i.value = value; i.className = 'bulk-dyn';
          fieldForm.appendChild(i);
        }
        if (campo === 'status') {
          addHidden('action', 'status');
          addHidden('nuevo_estado', valEstado.value);
        } else if (campo === 'poner_oferta') {
          addHidden('action', 'set_field');
          addHidden('campo', 'poner_oferta');
          addHidden('pct', valPct ? valPct.value : '');
          addHidden('hasta', valHasta ? valHasta.value : '');
        } else if (campo === 'quitar_oferta') {
          addHidden('action', 'set_field');
          addHidden('campo', 'quitar_oferta');
        } else if (campo === 'cartel_nuevo') {
          addHidden('action', 'set_field');
          addHidden('campo', 'cartel_nuevo');
          addHidden('valor', valNuevo.value);
        } else {
          addHidden('action', 'set_field');
          addHidden('campo', campo);
          addHidden('valor', valBool.value);
        }
      });
    }

    selectAll.addEventListener('change', function () {
      rowChecks.forEach(function (cb) { cb.checked = selectAll.checked; });
      syncBar();
    });
    rowChecks.forEach(function (cb) { cb.addEventListener('change', syncBar); });
    if (bulkClear) {
      bulkClear.addEventListener('click', function () {
        rowChecks.forEach(function (cb) { cb.checked = false; });
        syncBar();
      });
    }
  }

  // -------------------------------------------------------------------------
  // Init
  // -------------------------------------------------------------------------

  document.addEventListener('DOMContentLoaded', function () {
    initFormCalc();
    initConfirm();
    initMenus();
    initFlashes();
    initBulkSelect();
  });

})();
