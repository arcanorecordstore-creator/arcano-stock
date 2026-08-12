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


def _ffmpeg_exe():
    """Ruta al ejecutable de ffmpeg (el bundleado por imageio-ffmpeg, o del sistema)."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            return exe
    except Exception:
        pass
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def extract_frames_from_path(path, n_frames: int = 1, max_width: int = 900, timeout_s: int = 45):
    """Extrae hasta `n_frames` cuadros del video LLAMANDO A FFMPEG DIRECTO.

    ffmpeg hace seek rapido (-ss antes de -i), decodifica un solo cuadro ya
    escalado y lo saca por stdout como JPEG. Casi no usa memoria (no carga el
    video ni depende de numpy/imageio-reader). Devuelve (list_jpg_bytes, error).
    """
    import subprocess
    exe = _ffmpeg_exe()
    # El disco gira, cualquier frame sirve. 1s salta un posible intro/negro; 0 es respaldo.
    seek_secs = ["1", "3", "0"]
    frames = []
    err_last = None
    for ss in seek_secs:
        if len(frames) >= n_frames:
            break
        cmd = [
            exe, "-nostdin", "-loglevel", "error", "-y",
            "-ss", ss, "-i", path,
            "-frames:v", "1", "-an", "-sn",
            "-vf", f"scale='min({max_width},iw)':-2",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "pipe:1",
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return frames, "El video tardo demasiado en procesarse (timeout)."
        except FileNotFoundError:
            return frames, "No se encontro ffmpeg para procesar el video."
        except Exception as e:
            err_last = f"Error de ffmpeg: {e}"
            continue
        out = proc.stdout or b""
        if out[:2] == b"\xff\xd8":  # magic de JPEG -> cuadro valido
            frames.append(out)
        else:
            err_last = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200] or "cuadro vacio"
    if not frames:
        return [], ("No se pudo extraer un cuadro del video. " + (err_last or "")).strip()
    return frames, None


def extract_frames_png(video_bytes: bytes, n_frames: int = 1, max_width: int = 900):
    """Compat/tests: recibe bytes, los escribe a un temp y extrae con ffmpeg."""
    tmp = os.path.join(tempfile.gettempdir(), f"arcano_vid_{uuid.uuid4().hex}.mp4")
    try:
        with open(tmp, "wb") as f:
            f.write(video_bytes)
        return extract_frames_from_path(tmp, n_frames=n_frames, max_width=max_width)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
