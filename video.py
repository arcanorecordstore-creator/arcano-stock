# -*- coding: utf-8 -*-
"""Extraccion de cuadros (frames) de un video para leerlos con la IA de vision.

Se usa en el flujo "Desde video": el usuario sube los videos de Instagram del
disco, sacamos algunos cuadros (donde suele estar la placa con los datos) y se
los pasamos a vision.py para extraer artista/titulo/sello/genero/precio/track.

Usa imageio + imageio-ffmpeg (traen su propio ffmpeg, sin dependencia de sistema).
"""
import io
import os
import re
import tempfile
import uuid


def clean_track_name_from_filename(filename: str):
    """Saca un nombre de track legible del nombre del archivo del video.

    Los reels suelen venir como 'reel_<hash>_Artista_-_Track_(Official_Audio).mp4'.
    Devuelve 'Artista - Track' (o None si no queda nada util)."""
    if not filename:
        return None
    name = os.path.splitext(str(filename))[0]
    # Sacar prefijos tipo 'reel_762df59b_' o '762df59b_' (hash de descarga)
    name = re.sub(r"^(reel|sample|video|vid|ig)[_-]", "", name, flags=re.I)
    name = re.sub(r"^[0-9a-fA-F]{6,}[_-]", "", name)
    name = name.replace("_", " ").strip()
    # Sacar parentesis del final: (Official Audio), (39 REC01), etc.
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    name = re.sub(r"\s*\[[^\]]*\]\s*$", "", name).strip()
    name = re.sub(r"\s+", " ", name)
    # Normalizar el separador ' - '
    name = re.sub(r"\s*-\s*", " - ", name).strip(" -")
    return name or None


def split_artist_track(name: str):
    """De 'Artista - Track' devuelve (artista, track). Sin separador -> (None, name)."""
    if not name:
        return None, None
    if " - " in name:
        a, t = name.split(" - ", 1)
        return (a.strip() or None), (t.strip() or None)
    return None, (name.strip() or None)

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


def extract_frames_png(video_bytes: bytes, positions=(0.5, 0.35, 0.65, 0.15, 0.85),
                       max_dim: int = 1100, n_frames: int = 1):
    """Extrae hasta `n_frames` cuadros del video.

    La placa con los datos es la misma en todo el video, asi que por defecto se
    saca 1 solo cuadro (del medio). Las demas posiciones son respaldo por si ese
    cuadro sale negro o en transicion. Devuelve (list_de_png_bytes, error).
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

        want = max(1, n_frames)
        if nframes and nframes > 1:
            # Seek eficiente (ffmpeg -ss) a cada posicion; corta al tener los que
            # necesita. Con n_frames=1 saca solo el cuadro del medio.
            seen = set()
            for p in positions:
                if len(frames_png) >= want:
                    break
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
            # Sin metadata: leer secuencial y quedarnos con los primeros cuadros
            # utiles (a ~1s del inicio para saltar transiciones), sin acumular todo.
            stride = max(1, int(fps))
            MAX_READ = 600
            try:
                for i, fr in enumerate(reader):
                    if i >= stride and i % stride == 0:
                        frames_png.append(_to_png(fr))
                        if len(frames_png) >= want:
                            break
                    if i >= MAX_READ:
                        break
            except Exception:
                pass

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
