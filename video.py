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

MAX_VIDEO_BYTES = 60 * 1024 * 1024  # 60 MB por video


def extract_frames_png(video_bytes: bytes, positions=(0.08, 0.25, 0.5, 0.75, 0.95),
                       max_dim: int = 1400):
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

        # Cantidad de frames: intentar count_frames; si falla, estimar por duracion*fps
        n = None
        try:
            n = reader.count_frames()
        except Exception:
            n = None
        if not n or n <= 0:
            try:
                meta = reader.get_meta_data()
                dur = meta.get("duration")
                fps = meta.get("fps")
                if dur and fps:
                    n = int(dur * fps)
            except Exception:
                n = None

        def _grab(idx):
            return reader.get_data(idx)

        indices = []
        if n and n > 0:
            for p in positions:
                idx = min(n - 1, max(0, int(n * p)))
                if idx not in indices:
                    indices.append(idx)
        else:
            # No sabemos la cantidad: leemos secuencialmente hasta un tope y sampleamos
            buff = []
            for i, fr in enumerate(reader):
                buff.append(fr)
                if i >= 1200:
                    break
            n = len(buff)
            if n == 0:
                return [], "El video no tiene cuadros legibles."
            for p in positions:
                idx = min(n - 1, max(0, int(n * p)))
                if idx not in indices:
                    indices.append(idx)
            _grab = lambda idx: buff[idx]  # noqa: E731

        for idx in indices:
            try:
                arr = _grab(idx)
            except Exception:
                continue
            try:
                img = Image.fromarray(arr)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                if max(img.size) > max_dim:
                    r = max_dim / max(img.size)
                    img = img.resize((int(img.width * r), int(img.height * r)), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                frames_png.append(buf.getvalue())
            except Exception:
                continue

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
