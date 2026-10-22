"""Conversions API de Meta: medicion de conversiones DESDE EL SERVIDOR.

El pixel del navegador se pierde muchas compras (bloqueadores de anuncios, iPhone
con proteccion de rastreo, el cliente que cierra la pestana antes de volver de
Mercado Pago...). Con la Conversions API la propia web le avisa a Meta "entro un
Lead / una Purchase" sin depender del navegador.

Para que Meta no cuente dos veces el mismo pedido (pixel + servidor), los dos
mandan el MISMO event_id ("reserva-<id>"): Meta los deduplica solo.

Reglas de oro de este modulo:
- Si no esta la variable de entorno META_CAPI_TOKEN, NO hace nada (la web anda igual).
- Nunca rompe ni demora el checkout ni el webhook: el envio va en un hilo aparte,
  con timeout corto y todo envuelto en try/except.
- Los datos personales (email, celular, nombre) viajan hasheados con SHA-256, que es
  lo que pide Meta. Nunca en texto plano.
"""

import hashlib
import logging
import os
import re
import threading
import time
import unicodedata

import requests

GRAPH_VERSION = "v21.0"
TIMEOUT_SEGUNDOS = 5

log = logging.getLogger("meta_capi")

# Ultimo hilo lanzado (solo para que las pruebas puedan esperar a que termine).
ultimo_hilo = None

_FBCLID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,500}$")
_COOKIE_FB_RE = re.compile(r"^fb\.[0-9]\.[0-9]{9,16}\.[A-Za-z0-9_\-\.]{1,560}$")


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

def token() -> str:
    """Token de acceso de la Conversions API (variable de entorno en Render)."""
    return (os.environ.get("META_CAPI_TOKEN") or "").strip()


def test_event_code() -> str:
    """Codigo opcional para ver los eventos en "Probar eventos" del Administrador de
    eventos de Meta (META_TEST_EVENT_CODE). En operacion normal va vacio."""
    return (os.environ.get("META_TEST_EVENT_CODE") or "").strip()


def configurado() -> bool:
    """True si hay token cargado (la medicion desde el servidor esta prendida)."""
    return bool(token())


def pixel_valido(pixel_id) -> str:
    """Devuelve el ID del pixel limpio (solo digitos) o "" si no sirve."""
    p = str(pixel_id or "").strip()
    return p if p.isdigit() else ""


# ---------------------------------------------------------------------------
# Normalizacion + hash (formato que pide Meta)
# ---------------------------------------------------------------------------

def _sha256(valor: str) -> str:
    return hashlib.sha256(valor.encode("utf-8")).hexdigest()


def normalizar_email(email) -> str:
    """Email en minusculas y sin espacios."""
    return "".join(str(email or "").split()).lower()


def normalizar_celular(tel) -> str:
    """Celular uruguayo: solo digitos, con 598 y sin el 0 inicial
    ('099 123 456' -> '59899123456'). Vacio si no parece un numero."""
    d = "".join(ch for ch in str(tel or "") if ch.isdigit())
    if len(d) < 7:
        return ""
    if d.startswith("598"):
        return d
    if d.startswith("0"):
        d = d[1:]
    return "598" + d


def normalizar_nombre(nombre) -> str:
    """Nombre o apellido: minusculas, sin signos ni espacios (Meta acepta tildes en UTF-8)."""
    t = unicodedata.normalize("NFC", str(nombre or "")).lower()
    return "".join(ch for ch in t if ch.isalpha())


def limpiar_fbclid(fbclid) -> str:
    """El fbclid que agrega Meta a los links de los anuncios (solo caracteres seguros)."""
    v = str(fbclid or "").strip()
    return v if _FBCLID_RE.match(v) else ""


def limpiar_cookie_fb(valor) -> str:
    """Valor de las cookies _fbp / _fbc (formato fb.1.<timestamp>.<algo>), o "" si no
    tiene pinta de ser una cookie real de Meta."""
    v = str(valor or "").strip()
    return v if _COOKIE_FB_RE.match(v) else ""


def fbc_desde_fbclid(fbclid, ms=None) -> str:
    """Arma el valor de _fbc a partir del fbclid de la URL: fb.1.<milisegundos>.<fbclid>."""
    limpio = limpiar_fbclid(fbclid)
    if not limpio:
        return ""
    if ms is None:
        ms = int(time.time() * 1000)
    return "fb.1.%d.%s" % (int(ms), limpio)


def armar_user_data(email=None, celular=None, nombre=None, ip=None, user_agent=None,
                    fbp=None, fbc=None) -> dict:
    """user_data de Meta: datos personales hasheados + datos tecnicos del navegador."""
    ud = {}
    em = normalizar_email(email)
    if em:
        ud["em"] = [_sha256(em)]
    ph = normalizar_celular(celular)
    if ph:
        ud["ph"] = [_sha256(ph)]
    partes = str(nombre or "").strip().split(None, 1)
    if partes:
        fn = normalizar_nombre(partes[0])
        if fn:
            ud["fn"] = [_sha256(fn)]
        if len(partes) > 1:
            ln = normalizar_nombre(partes[1])
            if ln:
                ud["ln"] = [_sha256(ln)]
    # La tienda vende en Uruguay: pais fijo (codigo ISO en minusculas, hasheado)
    ud["country"] = [_sha256("uy")]
    if ip:
        ud["client_ip_address"] = str(ip)[:64]
    if user_agent:
        ud["client_user_agent"] = str(user_agent)[:500]
    fbp = limpiar_cookie_fb(fbp)
    if fbp:
        ud["fbp"] = fbp
    fbc = limpiar_cookie_fb(fbc)
    if fbc:
        ud["fbc"] = fbc
    return ud


def armar_evento(event_name, event_id, event_source_url, user_data, custom_data,
                 event_time=None) -> dict:
    """Un evento con el formato de la Conversions API."""
    return {
        "event_name": event_name,
        "event_time": int(event_time or time.time()),
        "event_id": str(event_id),
        "event_source_url": event_source_url,
        "action_source": "website",
        "user_data": user_data,
        "custom_data": custom_data,
    }


# ---------------------------------------------------------------------------
# Envio (en un hilo aparte: nunca frena al cliente)
# ---------------------------------------------------------------------------

def _url_eventos(pixel_id) -> str:
    return "https://graph.facebook.com/%s/%s/events" % (GRAPH_VERSION, pixel_id)


def _post(pixel_id, tk, payload):
    """Hace el POST a Meta. Corre en el hilo: cualquier error se registra y listo."""
    try:
        r = requests.post(_url_eventos(pixel_id), params={"access_token": tk},
                          json=payload, timeout=TIMEOUT_SEGUNDOS)
        if getattr(r, "status_code", 200) >= 400:
            log.warning("Meta CAPI: Meta respondio %s: %s",
                        r.status_code, (getattr(r, "text", "") or "")[:300].replace(tk, "***"))
    except Exception as ex:
        # El mensaje de requests puede incluir la URL con el token: se tapa.
        log.warning("Meta CAPI: no se pudo enviar (%s)", str(ex).replace(tk, "***")[:300])


def enviar(pixel_id, eventos):
    """Manda los eventos a Meta en un hilo aparte. Devuelve el hilo (o None si no se
    mando nada: falta el token, el pixel o los eventos)."""
    global ultimo_hilo
    try:
        tk = token()
        pid = pixel_valido(pixel_id)
        if not tk or not pid or not eventos:
            return None
        payload = {"data": list(eventos)}
        tc = test_event_code()
        if tc:
            payload["test_event_code"] = tc
        hilo = threading.Thread(target=_post, args=(pid, tk, payload),
                                name="meta-capi", daemon=True)
        hilo.start()
        ultimo_hilo = hilo
        return hilo
    except Exception as ex:
        log.warning("Meta CAPI: no se pudo lanzar el envio (%s)", ex)
        return None
