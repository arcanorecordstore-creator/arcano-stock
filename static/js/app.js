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
    var bulkStatusIds = document.getElementById('bulk-status-ids');
    var bulkDeleteIds = document.getElementById('bulk-delete-ids');

    if (!selectAll || rowChecks.length === 0 || !bulkBar) return;

    function getSelected() {
      var ids = [];
      rowChecks.forEach(function (cb) {
        if (cb.checked) ids.push(cb.value);
      });
      return ids;
    }

    function syncBar() {
      var ids = getSelected();
      var n = ids.length;
      if (n > 0) {
        bulkBar.hidden = false;
        bulkCountNum.textContent = n;
        // Inyectar hidden inputs en ambos forms
        var html = ids.map(function (id) {
          return '<input type="hidden" name="ids[]" value="' + id + '">';
        }).join('');
        bulkStatusIds.innerHTML = html;
        bulkDeleteIds.innerHTML = html;
      } else {
        bulkBar.hidden = true;
        bulkStatusIds.innerHTML = '';
        bulkDeleteIds.innerHTML = '';
      }
      // Estado del checkbox "seleccionar todo"
      if (n === 0) {
        selectAll.checked = false;
        selectAll.indeterminate = false;
      } else if (n === rowChecks.length) {
        selectAll.checked = true;
        selectAll.indeterminate = false;
      } else {
        selectAll.checked = false;
        selectAll.indeterminate = true;
      }
    }

    selectAll.addEventListener('change', function () {
      rowChecks.forEach(function (cb) { cb.checked = selectAll.checked; });
      syncBar();
    });

    rowChecks.forEach(function (cb) {
      cb.addEventListener('change', syncBar);
    });

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
