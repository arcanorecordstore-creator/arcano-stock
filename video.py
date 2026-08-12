# -*- coding: utf-8 -*-
"""Extraccion de cuadros (frames) de un video para leerlos con la IA de vision.

Se usa en el flujo "Desde video": el usuario sube los videos de Instagram del
disco, sacamos algunos cuadros (donde suele estar la placa con los datos) y se
los pasamos a vision.py para extraer artista/titulo/sello/genero/precio/track.

Usa imageio + imageio-ffmpeg (traen su propio ffmpeg, sin dependencia de sistema).
"""
import io
import os
import tempfile
import uuid

# Tipos de video aceptados -> extension del archivo guardado como sample
VIDEO_EXT_MAP = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-m4v": ".m4v",
    "video/x-matroska": ".mkv",
    "video/3gpp": ".3gp",
}

MAX_VIDEO_BYTES = 50 * 1024 * 1024  # 50 MB por video


def extract_frames_png(video_bytes: bytes, positions=(0.1, 0.3, 0.55, 0.8),
                       max_dim: int = 1100):
    """Extrae cuadros del video en las posiciones dadas (fraccion 0..1).

    Devuelve (list_de_png_bytes, error). En exito error es None.
    """
    try:
        import imageio.v2 as imageio
    except ImportError:
        return [], "Falta la libreria imageio. Ejecutar: pip install imageio imageio-ffmpeg"
    try:
        from PIL import Image
    except ImportError:
        return [], "Falta la libreria Pillow."

    # Extension .mp4 para que imageio rutee al backend ffmpeg; ffmpeg igual
    # decodifica el contenido real aunque sea mov/webm/etc.
    tmp = os.path.join(tempfile.gettempdir(), f"arcano_vid_{uuid.uuid4().hex}.mp4")
    try:
        with open(tmp, "wb") as f:
            f.write(video_bytes)
    except Exception as e:
        return [], f"No se pudo guardar el video temporal: {e}"

    frames_png = []
    reader = None
    try:
        try:
            # format="ffmpeg" fuerza el backend de video (el archivo temporal no
            # tiene extension conocida, asi que no se puede inferir solo).
            reader = imageio.get_reader(tmp, format="ffmpeg")
        except Exception as e:
            return [], f"No se pudo abrir el video (formato no soportado?): {e}"

        # Ubicar los cuadros por TIEMPO usando la metadata (NO contar todos los
        # frames ni leer el video entero: eso revienta la memoria en Render).
        fps = 25.0
        nframes = None
        try:
            meta = reader.get_meta_data()
            fps = float(meta.get("fps") or 25.0) or 25.0
            nf = meta.get("nframes")
            if nf and nf != float("inf") and nf > 0:
                nframes = int(nf)
            else:
                dur = meta.get("duration")
                if dur and dur > 0:
                    nframes = int(dur * fps)
        except Exception:
            nframes = None

        def _to_png(arr):
            img = Image.fromarray(arr)
            if img.mode != "RGB":
                img = img.convert("RGB")
            if max(img.size) > max_dim:
                r = max_dim / max(img.size)
                img = img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))), Image.LANCZOS)
            b = io.BytesIO()
            img.save(b, format="PNG", optimize=True)
            return b.getvalue()

        if nframes and nframes > 1:
            # Seek eficiente (ffmpeg -ss) a cada posicion; un frame a la vez
            seen = set()
            for p in positions:
                idx = min(nframes - 1, max(0, int(nframes * p)))
                if idx in seen:
                    continue
                seen.add(idx)
                try:
                    arr = reader.get_data(idx)
                    frames_png.append(_to_png(arr))
                    del arr
                except Exception:
                    continue
        else:
            # Sin metadata confiable: leer secuencial SIN acumular todo. Guardamos
            # un cuadro (ya comprimido) cada ~1s, con tope de lecturas y de guardados.
            stride = max(1, int(fps))
            MAX_READ = 900
            MAX_KEEP = 8
            kept = []
            try:
                for i, fr in enumerate(reader):
                    if i % stride == 0:
                        kept.append(_to_png(fr))
                        if len(kept) >= MAX_KEEP:
                            break
                    if i >= MAX_READ:
                        break
            except Exception:
                pass
            if kept:
                if len(kept) <= len(positions):
                    frames_png = kept
                else:
                    step = len(kept) / float(len(positions))
                    for j in range(len(positions)):
                        frames_png.append(kept[min(len(kept) - 1, int(j * step))])

        if not frames_png:
            return [], "No se pudieron extraer cuadros del video."
        return frames_png, None
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass
        try:
            os.remove(tmp)
        except OSError:
            pass
