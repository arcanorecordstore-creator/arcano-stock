"""Busqueda de tracks en YouTube y SoundCloud usando yt-dlp (modo busqueda, sin descarga).

Mismo enfoque que Arcano Drop Hunter: yt-dlp resuelve "ytsearch" y "scsearch"
y devuelve metadata de los candidatos. NO descarga audio (solo busca).

Nota de deploy: local funciona siempre. En servidores cloud (Render) YouTube
puede bloquear la IP; SoundCloud suele tolerarlo. Si en produccion la busqueda
de YouTube falla, se puede migrar esa parte a la YouTube Data API (con API key).
"""
from __future__ import annotations

import concurrent.futures


def _duracion_str(segundos) -> str | None:
    """Convierte segundos a mm:ss."""
    if not segundos:
        return None
    try:
        s = int(segundos)
    except (ValueError, TypeError):
        return None
    return f"{s // 60}:{s % 60:02d}"


def _buscar_en(plataforma: str, query: str, limite: int) -> list[dict]:
    """Busca en una plataforma ('youtube' | 'soundcloud') y devuelve candidatos."""
    import yt_dlp

    prefix = "ytsearch" if plataforma == "youtube" else "scsearch"
    search_query = f"{prefix}{limite}:{query}"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,   # no baja el video, solo lista metadata (rapido)
        "skip_download": True,
        "socket_timeout": 15,
        "noplaylist": True,
    }

    resultados = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
        entries = (info or {}).get("entries", []) or []
        for e in entries:
            if not e:
                continue
            url = e.get("url") or e.get("webpage_url") or ""
            titulo = e.get("title") or "(sin titulo)"
            canal = e.get("uploader") or e.get("channel") or ""
            dur = _duracion_str(e.get("duration"))
            # Normalizar URL de YouTube a formato watch (extract_flat a veces da solo el id)
            if plataforma == "youtube":
                vid = e.get("id") or ""
                if url and not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={url}"
                elif vid and not url:
                    url = f"https://www.youtube.com/watch?v={vid}"
            resultados.append({
                "plataforma": plataforma,
                "titulo": titulo,
                "canal": canal,
                "duracion": dur,
                "url": url,
            })
    except Exception as ex:
        # Devolver el error de esta plataforma sin romper la otra
        return [{"_error": str(ex), "plataforma": plataforma}]

    return resultados


def buscar_track(artista: str, titulo: str,
                 plataformas: list[str] | None = None,
                 limite: int = 4) -> dict:
    """Busca un track en las plataformas indicadas.

    Devuelve: {
        "ok": bool,
        "query": str,
        "candidatos": [ {plataforma, titulo, canal, duracion, url}, ... ],
        "errores": [ {plataforma, error}, ... ],
    }
    """
    if plataformas is None:
        plataformas = ["youtube", "soundcloud"]

    query = " ".join(p for p in [artista, titulo] if p).strip()
    if not query:
        return {"ok": False, "query": "", "candidatos": [], "errores": [{"error": "Falta artista o titulo"}]}

    candidatos = []
    errores = []

    # Buscar en paralelo (una plataforma no espera a la otra)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(plataformas)) as pool:
        futuros = {pool.submit(_buscar_en, p, query, limite): p for p in plataformas}
        for fut in concurrent.futures.as_completed(futuros):
            res = fut.result()
            for r in res:
                if "_error" in r:
                    errores.append({"plataforma": r.get("plataforma"), "error": r["_error"]})
                else:
                    candidatos.append(r)

    # Ordenar: primero SoundCloud (no bloquea embed), despues YouTube
    orden = {"soundcloud": 0, "youtube": 1}
    candidatos.sort(key=lambda c: orden.get(c["plataforma"], 9))

    return {
        "ok": len(candidatos) > 0,
        "query": query,
        "candidatos": candidatos,
        "errores": errores,
    }
