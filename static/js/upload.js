// Arcano Stock - Drag & drop de imagenes para extraccion via vision
(function () {
  'use strict';

  var dropzone = document.getElementById('dropzone');
  var input = document.getElementById('screenshot');
  var previewWrap = document.getElementById('preview-wrap');
  var previewImg = document.getElementById('preview-img');
  var previewName = document.getElementById('preview-name');
  var previewSize = document.getElementById('preview-size');
  var previewClear = document.getElementById('preview-clear');
  var submitBtn = document.getElementById('upload-submit');
  var form = document.getElementById('upload-form');

  if (!dropzone || !input) return;

  var MAX_IMG_BYTES = 10 * 1024 * 1024;
  var MAX_PDF_BYTES = 20 * 1024 * 1024;
  var ALLOWED_IMG = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
  var ALLOWED_PDF = 'application/pdf';

  function humanSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(2) + ' MB';
  }

  function validateFile(file) {
    if (!file) return 'No se selecciono ningun archivo.';
    var isImg = ALLOWED_IMG.indexOf(file.type) !== -1;
    var isPdf = file.type === ALLOWED_PDF;
    if (!isImg && !isPdf) {
      return 'Tipo no soportado: ' + (file.type || 'desconocido') + '. Usar PNG, JPG, GIF, WEBP o PDF.';
    }
    if (isImg && file.size > MAX_IMG_BYTES) {
      return 'La imagen pesa ' + humanSize(file.size) + '. Maximo: 10 MB.';
    }
    if (isPdf && file.size > MAX_PDF_BYTES) {
      return 'El PDF pesa ' + humanSize(file.size) + '. Maximo: 20 MB.';
    }
    return null;
  }

  function setFile(file) {
    var err = validateFile(file);
    if (err) {
      window.alert(err);
      return;
    }
    var dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;

    previewName.textContent = file.name;
    previewSize.textContent = humanSize(file.size);
    previewWrap.hidden = false;
    dropzone.classList.add('has-file');

    if (file.type === ALLOWED_PDF) {
      // PDF: mostrar icono en vez de preview
      previewImg.style.display = 'none';
      var icon = document.getElementById('preview-pdf-icon');
      if (!icon) {
        icon = document.createElement('div');
        icon.id = 'preview-pdf-icon';
        icon.className = 'preview-pdf-icon';
        icon.textContent = 'PDF';
        previewImg.parentNode.insertBefore(icon, previewImg);
      }
      icon.style.display = 'flex';
    } else {
      // Imagen: preview
      previewImg.style.display = 'block';
      var icon = document.getElementById('preview-pdf-icon');
      if (icon) icon.style.display = 'none';
      var reader = new FileReader();
      reader.onload = function (ev) {
        previewImg.src = ev.target.result;
      };
      reader.readAsDataURL(file);
    }
  }

  function clearFile() {
    input.value = '';
    previewWrap.hidden = true;
    previewImg.src = '';
    dropzone.classList.remove('has-file');
  }

  // Click en la dropzone abre el selector
  dropzone.addEventListener('click', function (ev) {
    if (ev.target === previewClear) return;
    input.click();
  });

  // Cambio en el input
  input.addEventListener('change', function () {
    var f = input.files && input.files[0];
    if (f) setFile(f);
  });

  // Drag events
  ['dragenter', 'dragover'].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add('drag-over');
    });
  });
  ['dragleave', 'drop'].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove('drag-over');
    });
  });
  dropzone.addEventListener('drop', function (e) {
    var files = e.dataTransfer && e.dataTransfer.files;
    if (files && files[0]) setFile(files[0]);
  });

  // Quitar archivo
  if (previewClear) {
    previewClear.addEventListener('click', function (ev) {
      ev.stopPropagation();
      clearFile();
    });
  }

  // Indicador de carga al submitear
  if (form && submitBtn) {
    form.addEventListener('submit', function () {
      if (!input.files || !input.files[0]) return;
      submitBtn.disabled = true;
      submitBtn.textContent = 'Procesando, aguarda...';
    });
  }
})();
