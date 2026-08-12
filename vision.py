"""Extraccion de datos de discos a partir de imagenes o PDFs usando Claude API.

Soporta imagenes (PNG/JPG/GIF/WEBP) y PDFs nativos (con texto seleccionable).
Los PDFs se renderizan a imagen antes de enviar a la API.

Usa el modelo claude-sonnet-4-5 que tiene capacidades de vision.
"""
import base64
import io
import json
import re
from dataclasses import dataclass, field
from typing import Optional

import anthropic

import config


# Modelo con vision.
VISION_MODEL = "claude-sonnet-4-5"

# Limites
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_PDF_BYTES = 20 * 1024 * 1024    # 20 MB (PDFs pueden ser un poco mas grandes)
ALLOWED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_MEDIA_TYPE = "application/pdf"


def convert_pdf_to_png(pdf_bytes: bytes, max_pages: int = 5) -> tuple[bytes | None, str | None]:
    """Convierte la primera pagina (o varias) de un PDF a PNG.

    Si el PDF tiene varias paginas las concatena verticalmente en una sola
    imagen (hasta max_pages), porque Claude API recibe una imagen sola.

    Returns:
        (png_bytes, error_msg). En exito error_msg es None.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None, (
            "Falta la libreria pypdfium2. Ejecutar: pip install pypdfium2"
        )

    try:
        from PIL import Image
    except ImportError:
        return None, (
            "Falta la libreria Pillow. Ejecutar: pip install Pillow"
        )

    try:
        pdf = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
    except Exception as e:
        return None, f"No se pudo abrir el PDF: {e}"

    n_pages = min(len(pdf), max_pages)
    if n_pages == 0:
        return None, "El PDF no tiene paginas."

    # Renderizar cada pagina a PIL.Image
    images = []
    for i in range(n_pages):
        page = pdf[i]
        # scale=2.0 da ~144 DPI, balance entre legibilidad y tamano
        pil_img = page.render(scale=2.0).to_pil()
        # Convertir a RGB (Claude no acepta CMYK ni alpha en JPEG, RGB es seguro)
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
        images.append(pil_img)
        page.close()

    pdf.close()

    # Concatenar verticalmente
    if len(images) == 1:
        final_img = images[0]
    else:
        widths = [img.width for img in images]
        heights = [img.height for img in images]
        max_w = max(widths)
        total_h = sum(heights)
        final_img = Image.new("RGB", (max_w, total_h), "white")
        y = 0
        for img in images:
            # Si la pagina es mas estrecha que la max, la centramos
            x = (max_w - img.width) // 2
            final_img.paste(img, (x, y))
            y += img.height
            img.close()

    # Limitar dimension maxima para no mandar imagenes gigantes
    # Claude API acepta hasta ~8000px pero ~3000 es mas que suficiente
    MAX_DIM = 3000
    if final_img.width > MAX_DIM or final_img.height > MAX_DIM:
        ratio = min(MAX_DIM / final_img.width, MAX_DIM / final_img.height)
        new_w = int(final_img.width * ratio)
        new_h = int(final_img.height * ratio)
        final_img = final_img.resize((new_w, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    final_img.save(buf, format="PNG", optimize=True)
    final_img.close()
    return buf.getvalue(), None


def prepare_image_payload(file_bytes: bytes, media_type: str) -> tuple[bytes | None, str | None, str | None]:
    """Recibe bytes + media_type y devuelve (image_bytes, image_media_type, error).

    Si es PDF, lo convierte a PNG. Si es imagen, la pasa tal cual.
    """
    media_type = (media_type or "").lower()

    if media_type == PDF_MEDIA_TYPE:
        if len(file_bytes) > MAX_PDF_BYTES:
            return None, None, (
                f"El PDF pesa {len(file_bytes) // 1024 // 1024} MB. "
                f"Maximo permitido: {MAX_PDF_BYTES // 1024 // 1024} MB."
            )
        png_bytes, err = convert_pdf_to_png(file_bytes)
        if err:
            return None, None, err
        return png_bytes, "image/png", None

    if media_type in ALLOWED_MEDIA_TYPES:
        if len(file_bytes) > MAX_IMAGE_BYTES:
            return None, None, (
                f"La imagen pesa {len(file_bytes) // 1024} KB. "
                f"Maximo permitido: {MAX_IMAGE_BYTES // 1024 // 1024} MB."
            )
        return file_bytes, media_type, None

    return None, None, (
        f"Tipo de archivo no soportado: {media_type}. "
        "Usar JPG, PNG, GIF, WEBP o PDF."
    )

EXTRACTION_PROMPT = """\
Sos un asistente experto en catalogar discos de vinilo. Te paso un screenshot \
(generalmente de Deejay.de, Discogs, Hardwax, Bandcamp u otra tienda online de \
vinilos).

Tu tarea: extraer los datos del disco que se ven en la imagen y devolverlos \
como un objeto JSON valido, sin texto adicional antes o despues.

Esquema exacto del JSON a devolver:

{
  "artista": string | null,
  "titulo": string | null,
  "sello": string | null,
  "anio": number | null,
  "formato": string | null,        // ej: "12\\"", "7\\"", "LP", "EP"
  "pais": string | null,           // pais de origen/edicion del disco si es visible (ej: "Alemania", "Reino Unido")
  "genero": string | null,         // generos musicales detectados
  "tracklist": string | null,      // tracklist completo, una linea por track con formato "A1. Artista - Titulo"
  "costo_eur": number | null,      // precio en EUROS si esta visible. Solo numero, sin simbolo. Si esta en USD u otra moneda, dejar null
  "cover_url": string | null,      // URL de la imagen de portada si esta visible
  "sample_url": string | null,     // URL de audio sample si esta visible
  "source_url": string | null,     // URL original del disco si es visible en la barra del navegador
  "confianza": "alta" | "media" | "baja",  // tu confianza global en la extraccion
  "notas_extraccion": string | null  // cualquier nota relevante: campos ambiguos, errores potenciales, etc.
}

Reglas importantes:
- Si un campo no es visible o no podes leerlo, devolve null. NO inventes.
- El campo "anio" debe ser un numero entero entre 1900 y 2100. Si no esta visible, null.
- El campo "costo_eur" SOLO debe llenarse si el precio esta claramente en euros (simbolo EUR). Si esta en otra moneda, dejar null.
- El "tracklist" debe respetar el orden y nombres de tracks que veas. Una linea por track.
- "confianza" reflejar tu certeza global. "baja" si la imagen es de baja calidad o muchos campos quedan en null.
- Respondes UNICAMENTE con el JSON. Sin markdown, sin ```json, sin explicaciones previas.
"""


INVOICE_PROMPT = """\
Sos un asistente experto en catalogar discos de vinilo. Te paso un screenshot \
o foto de una FACTURA o LISTADO de compra de Deejay.de (u otra tienda similar) \
que contiene VARIOS discos comprados juntos.

Tu tarea: extraer todos los discos visibles + el costo de envio total, y \
devolverlos como un objeto JSON valido, sin texto adicional antes o despues.

Esquema exacto del JSON a devolver:

{
  "discos": [
    {
      "artista": string | null,
      "titulo": string | null,
      "sello": string | null,
      "anio": number | null,
      "formato": string | null,
      "pais": string | null,        // pais de origen/edicion si es visible
      "costo_eur": number | null   // precio individual de este disco en EUROS
    },
    ...
  ],
  "envio_total_eur": number | null,  // costo de envio TOTAL de la factura, separado del precio de los discos
  "moneda": string,                  // "EUR", "USD", etc.
  "fuente_probable": string | null,  // "Deejay.de", "Discogs", "Hardwax", etc.
  "subtotal_discos_eur": number | null,  // suma de los precios de los discos (sin envio), si esta visible
  "total_factura_eur": number | null,    // total final de la factura, si esta visible
  "confianza": "alta" | "media" | "baja",
  "notas_extraccion": string | null
}

Reglas importantes:
- Devolve TODOS los discos que veas en la factura. Si hay 7 discos, devolve 7 entradas en el array.
- Para cada disco extrae lo que puedas. Si solo se ve el titulo y el precio (no el sello ni el anio), dejar esos campos en null.
- "costo_eur" de cada disco debe ser su precio UNITARIO en euros, no el total con envio.
- "envio_total_eur" es el costo de envio SEPARADO. Si la factura no tiene linea de envio explicita, dejar null.
- Si la factura esta en otra moneda (USD, GBP), dejar todos los costos en null y aclarar en notas_extraccion.
- Si no podes identificar ningun disco, devolve "discos": [] (array vacio) y aclara por que en notas_extraccion.
- "confianza" reflejar tu certeza global.
- Respondes UNICAMENTE con el JSON. Sin markdown, sin explicaciones.
"""


VIDEO_FRAMES_PROMPT = """\
Sos un asistente experto en catalogar discos de vinilo. Te paso cuadro(s) de un \
video corto donde suena UN track de un disco. El video muestra el ARTISTA y el \
NOMBRE DEL TRACK que esta sonando (ademas del disco/portada). NO suele mostrar el \
nombre del EP/lanzamiento.

Tu tarea: leer estos datos y devolverlos como un objeto JSON valido, sin texto \
adicional.

Esquema exacto del JSON:

{
  "artista": string | null,        // el ARTISTA de este track (ej: "Llip", "Admo")
  "track": string | null,          // el NOMBRE de este track (ej: "Boards", "Roger T")
  "sello": string | null,          // el sello/label SOLO si aparece escrito
  "anio": number | null,           // solo si aparece
  "genero": string | null,         // solo si aparece escrito (ej: "House, Electro")
  "catalogo": string | null,       // codigo de catalogo si aparece (ej: "LEMAIA002")
  "precio_uyu": number | null,     // precio si aparece en pantalla (ej: $1.300 -> 1300)
  "confianza": "alta" | "media" | "baja",
  "notas_extraccion": string | null
}

Reglas:
- Lo mas importante y casi siempre presente son "artista" y "track". El resto \
(sello, genero, ano, catalogo, precio) va SOLO si esta escrito; si no, null.
- NO devuelvas el nombre del EP/lanzamiento: ese dato NO esta en el video.
- Si te paso el NOMBRE DEL ARCHIVO del video, suele venir como "Artista - Track": \
usalo como pista fuerte para "artista" y "track".
- "precio_uyu" sin simbolo ni puntos de miles (1.300 -> 1300). "anio" entero 1900-2100.
- Respondes UNICAMENTE con el JSON. Sin markdown, sin explicaciones.
"""


@dataclass
class VisionResult:
    """Resultado de una extraccion via vision."""
    ok: bool
    data: dict = field(default_factory=dict)
    error: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    raw_response: Optional[str] = None


def _calculate_cost(input_tokens: int, output_tokens: int) -> float:
    """Calcula costo aproximado en USD para claude-sonnet-4-5.

    Pricing publico: $3 / millon input tokens, $15 / millon output tokens.
    """
    input_cost = (input_tokens / 1_000_000) * 3.0
    output_cost = (output_tokens / 1_000_000) * 15.0
    return round(input_cost + output_cost, 6)


def _extract_json_from_text(text: str) -> Optional[dict]:
    """Intenta parsear JSON del texto de respuesta de Claude.

    Maneja casos donde el modelo encierra el JSON en bloques markdown
    o agrega texto antes/despues a pesar de las instrucciones.
    """
    if not text:
        return None
    # Caso ideal: el texto entero es JSON
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Caso markdown: ```json ... ```
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    # Caso "texto antes... { ... } ...texto despues"
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _normalize_data(data: dict) -> dict:
    """Normaliza los datos extraidos para que matcheen los campos del Disc."""
    if not isinstance(data, dict):
        return {}

    out = {}

    def s(value):
        if value is None:
            return None
        if isinstance(value, str):
            v = value.strip()
            return v if v else None
        return str(value)

    def f(value):
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def i(value):
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    out["artista"] = s(data.get("artista"))
    out["titulo"] = s(data.get("titulo"))
    out["sello"] = s(data.get("sello"))
    out["anio"] = i(data.get("anio"))
    out["formato"] = s(data.get("formato"))
    out["pais"] = s(data.get("pais"))
    out["genero"] = s(data.get("genero"))
    out["tracklist"] = s(data.get("tracklist"))
    out["costo_eur"] = f(data.get("costo_eur"))
    out["cover_url"] = s(data.get("cover_url"))
    out["sample_url"] = s(data.get("sample_url"))
    out["source_url"] = s(data.get("source_url"))
    out["confianza"] = s(data.get("confianza")) or "media"
    out["notas_extraccion"] = s(data.get("notas_extraccion"))

    # Validacion suave del anio
    if out["anio"] is not None and not (1900 <= out["anio"] <= 2100):
        out["anio"] = None

    return out


def extract_disc_from_image(
    image_bytes: bytes,
    media_type: str,
) -> VisionResult:
    """Llama a la API de Claude con la imagen y devuelve datos estructurados.

    Args:
        image_bytes: bytes crudos de la imagen.
        media_type: 'image/png', 'image/jpeg', etc.

    Returns:
        VisionResult con ok=True y data={...} si funciono,
        o ok=False con error="..." si fallo.
    """
    # Validaciones previas
    if not config.ANTHROPIC_API_KEY:
        return VisionResult(
            ok=False,
            error=(
                "Falta la API key de Anthropic. Editar el archivo .env y agregar "
                "ANTHROPIC_API_KEY=sk-ant-..."
            ),
        )

    # Preparar payload (convierte PDF a imagen si hace falta)
    image_bytes, image_media_type, prep_err = prepare_image_payload(
        image_bytes, media_type
    )
    if prep_err:
        return VisionResult(ok=False, error=prep_err)
    media_type = image_media_type

    if not image_bytes:
        return VisionResult(ok=False, error="La imagen esta vacia.")

    # Encodear a base64
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    # Llamar a la API
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=VISION_MODEL,
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": EXTRACTION_PROMPT,
                        },
                    ],
                }
            ],
        )
    except anthropic.AuthenticationError:
        return VisionResult(
            ok=False,
            error=(
                "La API key de Anthropic es invalida o fue revocada. "
                "Verificar el archivo .env."
            ),
        )
    except anthropic.RateLimitError:
        return VisionResult(
            ok=False,
            error="Limite de uso alcanzado en la API. Esperar unos segundos y reintentar.",
        )
    except anthropic.APIStatusError as e:
        # Posibles: credit balance too low, payment required, etc.
        msg = str(e)
        if "credit" in msg.lower() or "balance" in msg.lower() or "billing" in msg.lower():
            return VisionResult(
                ok=False,
                error=(
                    "La cuenta de Anthropic no tiene creditos suficientes. "
                    "Cargar saldo en console.anthropic.com/settings/billing."
                ),
            )
        return VisionResult(ok=False, error=f"Error de la API: {msg}")
    except anthropic.APIConnectionError:
        return VisionResult(
            ok=False,
            error=(
                "No se pudo conectar a la API de Anthropic. Verificar la conexion a internet."
            ),
        )
    except Exception as e:
        return VisionResult(ok=False, error=f"Error inesperado: {type(e).__name__}: {e}")

    # Extraer texto y parsear JSON
    raw_text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            raw_text += block.text

    parsed = _extract_json_from_text(raw_text)
    if parsed is None:
        return VisionResult(
            ok=False,
            error=(
                "El modelo respondio pero no se pudo interpretar como JSON. "
                "Intentar con otra imagen o cargar manual."
            ),
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cost_usd=_calculate_cost(
                message.usage.input_tokens, message.usage.output_tokens
            ),
            raw_response=raw_text,
        )

    normalized = _normalize_data(parsed)

    return VisionResult(
        ok=True,
        data=normalized,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        cost_usd=_calculate_cost(
            message.usage.input_tokens, message.usage.output_tokens
        ),
        raw_response=raw_text,
    )


def _normalize_video_data(data: dict) -> dict:
    """Normaliza datos extraidos desde cuadros de un video."""
    if not isinstance(data, dict):
        return {}

    def s(value):
        if value is None:
            return None
        if isinstance(value, str):
            v = value.strip()
            return v if v else None
        return str(value)

    def f(value):
        if value is None or value == "":
            return None
        try:
            if isinstance(value, str):
                value = value.replace("$", "").replace(".", "").replace(",", ".").strip()
            return float(value)
        except (TypeError, ValueError):
            return None

    def i(value):
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    anio = i(data.get("anio"))
    if anio is not None and not (1900 <= anio <= 2100):
        anio = None

    precio = f(data.get("precio_uyu"))
    return {
        "artista": s(data.get("artista")),
        "track": s(data.get("track")),
        "sello": s(data.get("sello")),
        "anio": anio,
        "genero": s(data.get("genero")),
        "catalogo": s(data.get("catalogo")),
        "precio_uyu": int(precio) if precio is not None else None,
        "confianza": s(data.get("confianza")) or "media",
        "notas_extraccion": s(data.get("notas_extraccion")),
    }


def extract_from_video_frames(frames_png: list, filename: str = None) -> VisionResult:
    """Extrae datos del disco desde una lista de cuadros PNG de un video.

    frames_png: lista de bytes PNG (los cuadros extraidos del video).
    filename: nombre del archivo del video (pista para artista/track).
    """
    if not config.ANTHROPIC_API_KEY:
        return VisionResult(ok=False, error="Falta la API key de Anthropic (ANTHROPIC_API_KEY).")
    if not frames_png:
        return VisionResult(ok=False, error="No se recibieron cuadros del video.")

    # Armar el contenido: todas las imagenes + (nombre de archivo) + el prompt
    content = []
    for png in frames_png[:6]:  # tope de 6 cuadros por llamada
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(png).decode("utf-8"),
            },
        })
    if filename:
        content.append({"type": "text", "text": f"Nombre del archivo del video: {filename}"})
    content.append({"type": "text", "text": VIDEO_FRAMES_PROMPT})

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=VISION_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError:
        return VisionResult(ok=False, error="La API key de Anthropic es invalida o fue revocada.")
    except anthropic.RateLimitError:
        return VisionResult(ok=False, error="Limite de uso alcanzado. Esperar unos segundos.")
    except anthropic.APIStatusError as e:
        msg = str(e)
        if any(w in msg.lower() for w in ("credit", "balance", "billing")):
            return VisionResult(ok=False, error="La cuenta de Anthropic no tiene creditos suficientes.")
        return VisionResult(ok=False, error=f"Error de la API: {msg}")
    except anthropic.APIConnectionError:
        return VisionResult(ok=False, error="No se pudo conectar a la API de Anthropic.")
    except Exception as e:
        return VisionResult(ok=False, error=f"Error inesperado: {type(e).__name__}: {e}")

    raw_text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            raw_text += block.text

    parsed = _extract_json_from_text(raw_text)
    cost = _calculate_cost(message.usage.input_tokens, message.usage.output_tokens)
    if parsed is None:
        return VisionResult(
            ok=False, error="El modelo respondio pero no se pudo interpretar como JSON.",
            input_tokens=message.usage.input_tokens, output_tokens=message.usage.output_tokens,
            cost_usd=cost, raw_response=raw_text,
        )

    return VisionResult(
        ok=True, data=_normalize_video_data(parsed),
        input_tokens=message.usage.input_tokens, output_tokens=message.usage.output_tokens,
        cost_usd=cost, raw_response=raw_text,
    )


def _normalize_invoice_data(data: dict) -> dict:
    """Normaliza datos extraidos desde una factura multi-disco."""
    if not isinstance(data, dict):
        return {"discos": [], "envio_total_eur": None}

    def s(value):
        if value is None:
            return None
        if isinstance(value, str):
            v = value.strip()
            return v if v else None
        return str(value)

    def f(value):
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def i(value):
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    discos_raw = data.get("discos")
    if not isinstance(discos_raw, list):
        discos_raw = []

    discos_norm = []
    for d in discos_raw:
        if not isinstance(d, dict):
            continue
        anio = i(d.get("anio"))
        if anio is not None and not (1900 <= anio <= 2100):
            anio = None
        discos_norm.append({
            "artista": s(d.get("artista")),
            "titulo": s(d.get("titulo")),
            "sello": s(d.get("sello")),
            "anio": anio,
            "formato": s(d.get("formato")),
            "pais": s(d.get("pais")),
            "costo_eur": f(d.get("costo_eur")),
        })

    return {
        "discos": discos_norm,
        "envio_total_eur": f(data.get("envio_total_eur")),
        "moneda": s(data.get("moneda")) or "EUR",
        "fuente_probable": s(data.get("fuente_probable")),
        "subtotal_discos_eur": f(data.get("subtotal_discos_eur")),
        "total_factura_eur": f(data.get("total_factura_eur")),
        "confianza": s(data.get("confianza")) or "media",
        "notas_extraccion": s(data.get("notas_extraccion")),
    }


def extract_invoice_from_image(
    image_bytes: bytes,
    media_type: str,
) -> VisionResult:
    """Extrae VARIOS discos + envio total desde una imagen de factura.

    Devuelve VisionResult.data con estructura:
        {
            "discos": [{"artista":..., "titulo":..., "costo_eur":...}, ...],
            "envio_total_eur": float | None,
            "moneda": "EUR",
            ...
        }
    """
    if not config.ANTHROPIC_API_KEY:
        return VisionResult(
            ok=False,
            error=(
                "Falta la API key de Anthropic. Editar el archivo .env y agregar "
                "ANTHROPIC_API_KEY=sk-ant-..."
            ),
        )

    # Preparar payload (convierte PDF a imagen si hace falta)
    image_bytes, image_media_type, prep_err = prepare_image_payload(
        image_bytes, media_type
    )
    if prep_err:
        return VisionResult(ok=False, error=prep_err)
    media_type = image_media_type

    if not image_bytes:
        return VisionResult(ok=False, error="La imagen esta vacia.")

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=VISION_MODEL,
            max_tokens=4096,  # mas tokens porque puede haber varios discos
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": INVOICE_PROMPT,
                        },
                    ],
                }
            ],
        )
    except anthropic.AuthenticationError:
        return VisionResult(
            ok=False,
            error="La API key de Anthropic es invalida o fue revocada.",
        )
    except anthropic.RateLimitError:
        return VisionResult(
            ok=False,
            error="Limite de uso alcanzado. Esperar unos segundos y reintentar.",
        )
    except anthropic.APIStatusError as e:
        msg = str(e)
        if "credit" in msg.lower() or "balance" in msg.lower() or "billing" in msg.lower():
            return VisionResult(
                ok=False,
                error=(
                    "La cuenta de Anthropic no tiene creditos suficientes. "
                    "Cargar saldo en console.anthropic.com/settings/billing."
                ),
            )
        return VisionResult(ok=False, error=f"Error de la API: {msg}")
    except anthropic.APIConnectionError:
        return VisionResult(
            ok=False,
            error="No se pudo conectar a la API de Anthropic.",
        )
    except Exception as e:
        return VisionResult(ok=False, error=f"Error inesperado: {type(e).__name__}: {e}")

    raw_text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            raw_text += block.text

    parsed = _extract_json_from_text(raw_text)
    if parsed is None:
        return VisionResult(
            ok=False,
            error="El modelo respondio pero no se pudo interpretar como JSON.",
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cost_usd=_calculate_cost(
                message.usage.input_tokens, message.usage.output_tokens
            ),
            raw_response=raw_text,
        )

    normalized = _normalize_invoice_data(parsed)

    # Validar que al menos hay 1 disco
    if not normalized["discos"]:
        return VisionResult(
            ok=False,
            error=(
                "No se identifico ningun disco en la factura. "
                + (normalized.get("notas_extraccion") or "")
            ),
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cost_usd=_calculate_cost(
                message.usage.input_tokens, message.usage.output_tokens
            ),
            raw_response=raw_text,
        )

    return VisionResult(
        ok=True,
        data=normalized,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        cost_usd=_calculate_cost(
            message.usage.input_tokens, message.usage.output_tokens
        ),
        raw_response=raw_text,
    )
