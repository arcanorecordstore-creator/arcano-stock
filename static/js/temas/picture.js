/* OPCION S · PICTURE
   Marca el disco que esta sonando (.ps-sonando) para que su picture disc gire
   un poco mas rapido (igual lento: 5,4 s por vuelta). Sin tocar player.js. */
(function () {
    'use strict';
    setInterval(function () {
        var activo = document.querySelector('.public-grid .card-play-btn.is-playing');
        var pausa = document.querySelector('#fp-playpause .fp-ico-pause');
        var fp = document.getElementById('floating-player');
        var suena = !!(activo && fp && !fp.hidden && pausa && !pausa.hasAttribute('hidden'));
        document.querySelectorAll('.public-grid > .public-card').forEach(function (c) { c.classList.toggle('ps-sonando', suena && c.contains(activo)); });
    }, 500);
})();
