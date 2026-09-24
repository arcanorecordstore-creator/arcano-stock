"""Scraping de paginas de tiendas online de vinilos.

Estrategia:
- Usa cloudscraper para esquivar la proteccion anti-bot de Cloudflare.
- Parsea con BeautifulSoup la estructura real de Deejay.de:
  - Meta tags (og:title, og:image, og:description) como fuente principal.
  - h1/h2/h3 para artista, titulo y sello.
  - Texto plano con regex para precio, formato, release date.
  - Listas con items "A1: ..." para tracklist.
- Devuelve un dict con la misma estructura que vision.py.
"""
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import cloudscraper
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 25
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_COVER_BYTES = 8 * 1024 * 1024

ALLOWED_DOMAINS = {"deejay.de", "www.deejay.de"}


@dataclass
class ScrapeResult:
    ok: bool
    data: dict = field(default_factory=dict)
    error: Optional[str] = None
    http_status: Optional[int] = None
    raw_html_excerpt: Optional[str] = None


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def is_supported_url(url: str) -> tuple[bool, str]:
    if not url or not isinstance(url, str):
        return False, "URL vacia."
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False, "URL invalida."
    if parsed.scheme not in ("http", "https"):
        return False, "La URL debe empezar con http:// o https://"
    domain = (parsed.netloc or "").lower()
    if not domain:
        return False, "URL invalida (sin dominio)."
    if domain not in ALLOWED_DOMAINS:
        return False, (
            f"Dominio no soportado: {domain}. Solo se soporta deejay.de por ahora. "
            "Para otros sitios usar 'Desde screenshot'."
        )
    return True, domain


def _clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        v = " ".join(value.split())
        return v if v else None
    return value


def _to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        m = re.search(r"\d{4}", str(value))
        return int(m.group(0)) if m else None


def _to_float(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    s = s.replace("€", "").replace("EUR", "").replace("eur", "").strip()
    # Manejar "1.234,56" -> "1234.56"
    if re.match(r"^\d{1,3}(\.\d{3})+,\d+$", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


# -----------------------------------------------------------------------------
# Parser de Deejay.de (basado en estructura real)
# -----------------------------------------------------------------------------

def _parse_deejay(soup: BeautifulSoup, page_url: str) -> dict:
    """Parser para deejay.de."""
    out = {"source_url": page_url}

    # ---- Meta tags ----
    meta = {}
    for tag in soup.find_all("meta"):
        prop = tag.get("property") or tag.get("name")
        content = tag.get("content")
        if prop and content:
            meta[prop.lower()] = content

    og_title = meta.get("og:title")
    og_image = meta.get("og:image") or meta.get("og:image:secure_url")
    og_description = meta.get("og:description") or meta.get("description")

    # og:title observado:
    #   "Various - Memories. Untold / TAMIZDAT / ТАМИЗДАТ TMZ004 - Vinyl"
    if og_title:
        parts = og_title.split(" - ")
        if len(parts) >= 2:
            out["artista"] = _clean(parts[0])
            rest = " - ".join(parts[1:])

            # Recortar "- Vinyl" al final si esta
            formato_fallback = None
            if rest.endswith(" - Vinyl"):
                formato_fallback = "Vinyl"
                rest = rest[: -len(" - Vinyl")]
            elif " - " in rest:
                # Ultimo segmento puede ser " - LP" o " - 12 Inch"
                last_dash = rest.rsplit(" - ", 1)
                if last_dash[1].strip() and len(last_dash[1].strip()) < 30:
                    formato_fallback = last_dash[1].strip()
                    rest = last_dash[0]

            if " / " in rest:
                slash_parts = rest.split(" / ")
                out["titulo"] = _clean(slash_parts[0])
                # El sello suele ser slash_parts[1] (puede haber variantes en otros idiomas en [2], [3]...)
                if len(slash_parts) >= 2:
                    sello = _clean(slash_parts[1])
                    if sello and len(sello) < 100:
                        # Quitar el catalog number al final si esta pegado
                        # ej: "ТАМИЗДАТ TMZ004" del ultimo segmento
                        out["sello"] = sello
                # Si hay un ultimo segmento con catnum, no nos importa, ya tenemos sello
            else:
                out["titulo"] = _clean(rest)

            if formato_fallback and not out.get("formato"):
                out["formato"] = formato_fallback

    if og_image and not out.get("cover_url"):
        cover = og_image.replace("/l2/", "/xl/")
        out["cover_url"] = cover

    # ---- Precio desde meta tags (mas confiable que el texto) ----
    # Deejay/OpenGraph suele exponer product:price:amount
    for price_key in ("product:price:amount", "og:price:amount", "twitter:data1"):
        if out.get("costo_eur"):
            break
        raw = meta.get(price_key)
        if raw:
            val = _to_float(raw)
            if val and 0.5 <= val <= 500:
                out["costo_eur"] = val

    # ---- Tracklist desde meta description ----
    if og_description:
        track_matches = re.findall(
            r"\b([ABCDEFGH][12345]):\s*([^\n]+?)(?=\s*(?:[ABCDEFGH][12345]:|$))",
            og_description,
            re.DOTALL,
        )
        if not track_matches:
            track_matches = re.findall(
                r"\b([ABCDEFGH][12345]):\s*([^\n]+)",
                og_description,
            )
        if track_matches:
            lines = []
            for side, text in track_matches:
                text = _clean(text)
                if text:
                    lines.append(f"{side}. {text}")
            if lines:
                out["tracklist"] = "\n".join(lines)

    # ---- h1 / h3 fallback para artista, titulo, sello ----
    h1s = soup.find_all("h1")
    h1_texts = [_clean(h.get_text()) for h in h1s]
    h1_texts = [t for t in h1_texts if t and 1 < len(t) < 200]

    if not out.get("artista") and len(h1_texts) >= 1:
        out["artista"] = h1_texts[0]
    if not out.get("titulo") and len(h1_texts) >= 2:
        out["titulo"] = h1_texts[1]

    if not out.get("sello"):
        h3 = soup.find("h3")
        if h3:
            sello_text = _clean(h3.get_text())
            if sello_text and len(sello_text) < 100:
                out["sello"] = sello_text

    # ---- Texto completo para regex ----
    raw_page_text = soup.get_text(" ", strip=True)
    # Normalizar todos los tipos de espacio (no-break, saltos, tabs) a espacio simple
    page_text = re.sub(r"[\s\u00a0\u202f\u2009\u2007]+", " ", raw_page_text)

    # ---- Precio (EUR) ----
    # Deejay muestra el precio de varias formas posibles:
    #   "13,87 €" / "13,87€" / "€ 13,87" / "EUR 13,87" / "13.87 EUR"
    # El simbolo € puede venir separado por espacios o estar pegado.
    if not out.get("costo_eur"):
        # Lista de patrones en orden de preferencia
        precio_patterns = [
            r"(\d{1,4}[.,]\d{2})\s*€",       # 13,87 €
            r"€\s*(\d{1,4}[.,]\d{2})",       # € 13,87
            r"(\d{1,4}[.,]\d{2})\s*EUR",     # 13,87 EUR
            r"EUR\s*(\d{1,4}[.,]\d{2})",     # EUR 13,87
        ]
        precios_encontrados = []
        for pat in precio_patterns:
            for m in re.finditer(pat, page_text):
                val = _to_float(m.group(1))
                if val and val > 0:
                    precios_encontrados.append(val)
        if precios_encontrados:
            # Tomar el menor precio razonable (suele ser el precio de venta,
            # no totales del carrito ni sumas). Pero filtrar valores absurdos.
            candidatos = [p for p in precios_encontrados if 0.5 <= p <= 500]
            if candidatos:
                out["costo_eur"] = min(candidatos)
            else:
                out["costo_eur"] = precios_encontrados[0]

    # ---- Formato (despues de "Format:") ----
    if not out.get("formato"):
        m = re.search(r"Format:\s*([^\n\r|]+?)(?:\s+Release:|\s+Features:|\s+\d{1,2}[.,]\d{2}\s*€|$)", page_text)
        if m:
            fmt = _clean(m.group(1))
            if fmt and len(fmt) < 50:
                out["formato"] = fmt

    # ---- Anio (despues de "Release:") ----
    if not out.get("anio"):
        m = re.search(r"Release:\s*\d{1,2}[.\-/]\d{1,2}[.\-/](\d{4})", page_text)
        if not m:
            m = re.search(r"Release:\s*(\d{4})[.\-/]\d{1,2}[.\-/]\d{1,2}", page_text)
        if not m:
            m = re.search(r"Release:[^\d]{0,5}(\d{4})", page_text)
        if m:
            out["anio"] = _to_int(m.group(1))

    # ---- Genero (de los chips/links de categoria) ----
    if not out.get("genero"):
        genres = []
        # Tomar links a /m_<Cat>/sm_News/substyles_X o /m_<Cat>/sm_News al final del producto
        for link in soup.find_all("a", href=re.compile(r"/m_[^/]+/sm_News(?:/substyles_\d+)?$")):
            txt = _clean(link.get_text())
            if not txt or len(txt) > 30:
                continue
            # Filtrar links de menu principal (que suelen ser los mismos pero en nav superior)
            low = txt.lower()
            if low in (
                "all", "exclusive", "charts", "sale", "pop", "hip-hop",
                "equipment", "fashion", "pop & rock",
            ):
                continue
            if txt not in genres:
                genres.append(txt)
        if genres:
            # Tomamos los ultimos 5 (los del header se filtran arriba; los del producto quedan al final)
            # Separados por " - " (no coma): el separador de generos que usa Arcano.
            out["genero"] = " - ".join(genres[:6])

    # ---- Tracklist fallback desde lista HTML ----
    if not out.get("tracklist"):
        lines = []
        for li in soup.find_all("li"):
            text = _clean(li.get_text())
            if not text:
                continue
            m = re.match(r"^([ABCDEFGH][12345])\s*[:.]\s*(.+)", text)
            if m:
                side = m.group(1)
                rest = _clean(m.group(2))
                if rest and len(rest) < 200:
                    lines.append(f"{side}. {rest}")
        if lines:
            seen = set()
            uniq = []
            for line in lines:
                if line not in seen:
                    seen.add(line)
                    uniq.append(line)
            out["tracklist"] = "\n".join(uniq[:20])

    # ---- Limpieza final: titulo con "/" residual ----
    if out.get("titulo") and "/" in out["titulo"]:
        out["titulo"] = _clean(out["titulo"].split("/")[0])

    # ---- Formato: refinar si dice "Vinyl" generico ----
    if out.get("formato") and out["formato"].lower() in ("vinyl", "excl vinyl"):
        if "12 Inch" in page_text or '12"' in page_text:
            out["formato"] = '12"'
        elif "7 Inch" in page_text or '7"' in page_text:
            out["formato"] = '7"'

    return out


# -----------------------------------------------------------------------------
# Descarga de portada
# -----------------------------------------------------------------------------

def download_cover(url: str, dest_dir: str) -> Optional[str]:
    if not url:
        return None
    try:
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.deejay.de/"},
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
        if resp.status_code != 200:
            return None
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "image" not in content_type:
            return None

        ext = ".jpg"
        if "png" in content_type:
            ext = ".png"
        elif "gif" in content_type:
            ext = ".gif"
        elif "webp" in content_type:
            ext = ".webp"

        filename = f"cover_{uuid.uuid4().hex[:12]}{ext}"
        dest_path = os.path.join(dest_dir, filename)
        os.makedirs(dest_dir, exist_ok=True)

        total = 0
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    total += len(chunk)
                    if total > MAX_COVER_BYTES:
                        fh.close()
                        os.remove(dest_path)
                        return None
                    fh.write(chunk)

        if total == 0:
            os.remove(dest_path)
            return None

        return filename
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def scrape_url(url: str) -> ScrapeResult:
    url = (url or "").strip()
    ok, domain_or_msg = is_supported_url(url)
    if not ok:
        return ScrapeResult(ok=False, error=domain_or_msg)

    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        )
        resp = scraper.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except cloudscraper.exceptions.CloudflareChallengeError:
        return ScrapeResult(
            ok=False,
            error=(
                "Cloudflare bloqueo el acceso a la pagina. "
                "Usar 'Desde screenshot' como alternativa."
            ),
        )
    except Exception as e:
        return ScrapeResult(ok=False, error=f"Error de red: {type(e).__name__}: {e}")

    if resp.status_code == 403:
        return ScrapeResult(
            ok=False,
            http_status=403,
            error=(
                "El sitio rechazo el acceso (403). Probablemente Cloudflare. "
                "Usar 'Desde screenshot' como alternativa."
            ),
        )
    if resp.status_code == 404:
        return ScrapeResult(ok=False, http_status=404, error="La pagina no existe (404).")
    if resp.status_code != 200:
        return ScrapeResult(
            ok=False,
            http_status=resp.status_code,
            error=f"El sitio devolvio status HTTP {resp.status_code}.",
        )

    html = resp.text or ""
    if len(html) > MAX_HTML_BYTES:
        html = html[:MAX_HTML_BYTES]

    lower_html = html.lower()
    if "cf-challenge" in lower_html or "just a moment" in lower_html or "checking your browser" in lower_html:
        return ScrapeResult(
            ok=False,
            error=(
                "La pagina devolvio un desafio anti-bot de Cloudflare. "
                "Usar 'Desde screenshot' como alternativa."
            ),
        )

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        return ScrapeResult(ok=False, error=f"No se pudo parsear el HTML: {e}")

    data = _parse_deejay(soup, url)
    data["source_url"] = url

    campos_clave = ["artista", "titulo", "sello", "costo_eur"]
    presentes = sum(1 for k in campos_clave if data.get(k))
    if presentes >= 3:
        data["confianza"] = "alta"
    elif presentes >= 2:
        data["confianza"] = "media"
    else:
        data["confianza"] = "baja"

    if presentes == 0:
        return ScrapeResult(
            ok=False,
            error=(
                "Se pudo cargar la pagina pero no se extrajo ningun dato util. "
                "Probablemente la estructura del sitio cambio. Usar 'Desde screenshot'."
            ),
            raw_html_excerpt=html[:500],
        )

    return ScrapeResult(ok=True, data=data, http_status=200)


# -----------------------------------------------------------------------------
# Busqueda por artista + titulo
# -----------------------------------------------------------------------------

def search_deejay(artista: str, titulo: str) -> Optional[str]:
    """Busca un disco en deejay.de por artista+titulo.

    Returns:
        URL del primer resultado encontrado, o None si nada matchea / falla.
    """
    if not artista or not titulo:
        return None

    query = f"{artista} {titulo}".strip()
    # URL de busqueda de Deejay.de
    from urllib.parse import quote_plus
    search_url = f"https://www.deejay.de/?sb={quote_plus(query)}&type=Vinyl"

    try:
        scraper_session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        )
        resp = scraper_session.get(
            search_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except Exception:
        return None

    if resp.status_code != 200:
        return None

    html = resp.text or ""
    if "cf-challenge" in html.lower() or "just a moment" in html.lower():
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    # Los resultados son links a /<Artist>_<Title>_<CatNum>_Vinyl__<id>
    # Buscamos el primer link de producto
    for link in soup.find_all("a", href=True):
        href = link["href"]
        # Patron tipico: /<algo>_Vinyl__<numero>
        if re.search(r"_Vinyl__\d+($|\?)", href):
            if not href.startswith("http"):
                href = "https://www.deejay.de" + href if href.startswith("/") else "https://www.deejay.de/" + href
            return href

    return None


def find_cover_and_year(artista: str, titulo: str, dest_dir: str) -> dict:
    """Busca en Deejay.de un disco por artista+titulo y devuelve:

    {
        "cover_local": "cover_abc.jpg" | None,
        "anio": 2024 | None,
        "found_url": "https://..." | None,
    }
    """
    out = {"cover_local": None, "anio": None, "found_url": None}

    url = search_deejay(artista, titulo)
    if not url:
        return out

    out["found_url"] = url

    # Scrapear la pagina para obtener cover_url y anio
    result = scrape_url(url)
    if not result.ok:
        return out

    data = result.data or {}
    if data.get("anio"):
        out["anio"] = data["anio"]

    cover_url = data.get("cover_url")
    if cover_url:
        local = download_cover(cover_url, dest_dir)
        if local:
            out["cover_local"] = local

    return out
