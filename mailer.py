"""Envio de notificaciones por email.

Configuracion via .env (todas opcionales; si faltan, no se envia nada):
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=tu_email@gmail.com
  SMTP_PASS=tu_app_password
  SMTP_FROM=tu_email@gmail.com
  SMTP_TO=tu_email@gmail.com

Si alguna falta, las funciones devuelven False sin error (modo silencioso).
"""
import os
import mimetypes
import smtplib
from html import escape as _e
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr

import config


def _money(n) -> str:
    """Formatea un monto en pesos estilo uruguayo (punto de miles): 1.500, 2.000.000."""
    return f"{(n or 0):,.0f}".replace(",", ".")


# URL publica del sitio, para armar links absolutos (ficha + portada) en los mails.
SITE_URL = (os.environ.get("SITE_URL") or "https://arcanorecordstore.com").rstrip("/")


def _cover_url_abs(disc):
    """URL absoluta de la portada del disco (o None si no tiene)."""
    if disc is None:
        return None
    cl = getattr(disc, "cover_local", None)
    if cl:
        return f"{SITE_URL}/covers/{cl}"
    cu = getattr(disc, "cover_url", None)
    if cu:
        return cu
    return None


def _fila_html(artista, titulo, disc=None, precio=None):
    """Una fila de disco para los mails al cliente: portada + nombre (linkeado a la
    ficha si el disco existe) + precio opcional. Pensada para fondo oscuro (#0a0a0a).
    Usa tablas porque muchos clientes de mail no soportan flexbox."""
    disc_id = getattr(disc, "id", None) if disc is not None else None
    nombre = f'<strong>{_e(artista or "")}</strong> - {_e(titulo or "")}'
    cover = _cover_url_abs(disc)
    if cover:
        img_inner = (f'<img src="{_e(cover)}" width="48" height="48" alt="" '
                     f'style="width:48px;height:48px;border-radius:6px;object-fit:cover;display:block;border:0;">')
    else:
        img_inner = ('<div style="width:48px;height:48px;border-radius:6px;background:#1a1a1a;'
                     'color:#555;text-align:center;line-height:48px;font-size:20px;">&#9834;</div>')
    if disc_id:
        href = f"{SITE_URL}/disco/{disc_id}"
        img_cell = f'<a href="{href}" style="text-decoration:none;">{img_inner}</a>'
        nombre = f'<a href="{href}" style="color:#fff;text-decoration:underline;">{nombre}</a>'
    else:
        img_cell = img_inner
    precio_cell = ''
    if precio is not None:
        precio_cell = ('<td style="vertical-align:middle;padding-left:14px;color:#10b981;'
                       f'font-weight:600;white-space:nowrap;">$ {_money(precio)} UYU</td>')
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0;border-collapse:collapse;"><tr>'
        f'<td style="padding-right:12px;vertical-align:middle;width:48px;">{img_cell}</td>'
        f'<td style="vertical-align:middle;font-size:14px;color:#fff;">{nombre}</td>'
        f'{precio_cell}'
        '</tr></table>'
    )


def _lineas_pedido(reserva, discs):
    """(total, lineas_texto, filas_html) con los precios REALES que paga el
    cliente: los snapshots de la reserva (Fire Sale/oferta ya aplicados). Cae a
    los discos (precio_mostrar) solo si la reserva no tiene items (reservas viejas)."""
    items = list(getattr(reserva, "items_efectivos", []) or [])
    if items:
        total = sum((it.precio_uyu_snap or 0) for it in items)
        lineas = "\n".join(
            f"  - {it.artista_snap or ''} - {it.titulo_snap or ''} ($ {_money(it.precio_uyu_snap)} UYU)"
            for it in items
        )
        filas_html = "".join(
            _fila_html(it.artista_snap, it.titulo_snap, disc=it.disco, precio=it.precio_uyu_snap)
            for it in items
        )
        return total, lineas, filas_html
    total = sum((d.precio_mostrar or 0) for d in discs)
    lineas = "\n".join(
        f"  - {d.artista} - {d.titulo} ($ {_money(d.precio_mostrar)} UYU)"
        for d in discs
    )
    filas_html = "".join(
        _fila_html(d.artista, d.titulo, disc=d, precio=d.precio_mostrar)
        for d in discs
    )
    return total, lineas, filas_html


def _bloque_cobro(reserva, subtotal):
    """(texto, html) con el desglose REAL de lo que paga el cliente: discos, envio,
    recargo de Mercado Pago y total. Antes el mail decia solo el total de los discos
    (el cliente pagaba $1.441 y el mail decia $1.300)."""
    envio = float(getattr(reserva, "envio_costo_uyu", 0) or 0)
    recargo = float(getattr(reserva, "recargo_mp", 0) or 0)
    total = float(getattr(reserva, "total_final", 0) or 0) or (float(subtotal or 0) + envio)
    t, h = [], []
    if envio > 0 or recargo > 0:
        t.append(f"  Discos:  $ {_money(subtotal)} UYU")
        h.append(f'<p style="margin:2px 0;color:#bbb;">Discos: $ {_money(subtotal)} UYU</p>')
    if envio > 0:
        t.append(f"  Envio:   $ {_money(envio)} UYU")
        h.append(f'<p style="margin:2px 0;color:#bbb;">Env&iacute;o: $ {_money(envio)} UYU</p>')
    if recargo > 0:
        t.append(f"  Recargo Mercado Pago: $ {_money(recargo)} UYU")
        h.append(f'<p style="margin:2px 0;color:#bbb;">Recargo Mercado Pago: $ {_money(recargo)} UYU</p>')
    t.append(f"TOTAL: $ {_money(total)} UYU")
    h.append(f'<p style="margin-top:8px;"><strong>Total: $ {_money(total)} UYU</strong></p>')
    # Entrega elegida por el cliente (con la direccion si la cargo)
    if getattr(reserva, "entrega_tipo", None):
        _et = reserva.entrega_label
        if reserva.entrega_tipo == "interior" and not envio:
            _et += " (el envio lo cobra la agencia)"
        _dir = (getattr(reserva, "envio_direccion", "") or "").strip()
        if getattr(reserva, "envio_agencia", None):
            _dir = (_dir + f" - Agencia: {reserva.envio_agencia}").strip(" -")
        t.append("")
        t.append("ENTREGA: " + _et + (f" - {_dir}" if _dir else ""))
        h.append('<p style="margin-top:14px;"><strong>Entrega:</strong> ' + _e(_et)
                 + (f'<br>&#128205; {_e(_dir)}' if _dir else '') + '</p>')
    return "\n".join(t), "".join(h)


def _adjuntar_archivo(msg, path):
    """Adjunta un archivo (imagen o PDF) al mensaje. Silencioso si el archivo no existe."""
    if not path or not os.path.isfile(path):
        return
    ctype, encoding = mimetypes.guess_type(path)
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)
    with open(path, "rb") as f:
        part = MIMEBase(maintype, subtype)
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=os.path.basename(path))
    msg.attach(part)


def is_configured() -> bool:
    """True si todas las variables SMTP necesarias estan seteadas (para notificar al admin)."""
    return all([
        config.SMTP_HOST,
        config.SMTP_USER,
        config.SMTP_PASS,
        config.SMTP_FROM,
        config.SMTP_TO,
    ])


def can_send() -> bool:
    """True si se puede enviar mail a un destinatario dado (no requiere SMTP_TO)."""
    return all([
        config.SMTP_HOST,
        config.SMTP_USER,
        config.SMTP_PASS,
        config.SMTP_FROM,
    ])


def send_email(subject: str, body_text: str, body_html: str = None, to: str = None, attachments=None) -> tuple[bool, str]:
    """Envia un email. Si 'to' no se especifica, usa SMTP_TO (el admin).

    attachments: lista opcional de rutas de archivos a adjuntar (imagenes o PDF).
    """
    if not can_send():
        return False, "SMTP no configurado"

    destinatario = to or config.SMTP_TO
    if not destinatario:
        return False, "Sin destinatario"

    # Defensa anti header-injection: un destinatario o asunto con salto de linea
    # permitiria inyectar headers (Bcc:, etc.). Se rechaza de plano.
    if any(c in (destinatario or "") for c in ("\r", "\n")):
        return False, "Destinatario invalido"
    subject = (subject or "").replace("\r", " ").replace("\n", " ")

    # Cuerpo del mensaje (texto plano + HTML alternativo)
    cuerpo = MIMEMultipart("alternative")
    cuerpo.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        cuerpo.attach(MIMEText(body_html, "html", "utf-8"))

    adjuntos = [a for a in (attachments or []) if a]
    if adjuntos:
        # Con adjuntos: contenedor mixed = cuerpo + archivos
        msg = MIMEMultipart("mixed")
        msg.attach(cuerpo)
        for path in adjuntos:
            try:
                _adjuntar_archivo(msg, path)
            except Exception:
                pass  # si un adjunto falla, mandamos igual el mail
    else:
        msg = cuerpo

    msg["Subject"] = subject
    msg["From"] = formataddr(("Arcano Record Store", config.SMTP_FROM))
    msg["To"] = destinatario

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.sendmail(config.SMTP_FROM, [destinatario], msg.as_string())
        return True, "Enviado"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"Autenticacion SMTP fallida: {e}"
    except smtplib.SMTPException as e:
        return False, f"Error SMTP: {e}"
    except Exception as e:
        return False, f"Error inesperado: {type(e).__name__}: {e}"


def send_bulk(recipients, subject: str, body_text: str, body_html: str = None) -> tuple[int, int]:
    """Envia el MISMO mail a varios destinatarios, cada uno en su propio 'To'
    (no se exponen entre si, no hay BCC visible). Abre una sola conexion SMTP.
    Best-effort. Devuelve (enviados, fallidos)."""
    if not can_send():
        return 0, 0
    # Limpiar/deduplicar destinatarios validos (sin header-injection)
    limpios, vistos = [], set()
    for r in recipients or []:
        r = (r or "").strip()
        low = r.lower()
        if (r and "@" in r and low not in vistos
                and not any(c in r for c in ("\r", "\n"))):
            vistos.add(low)
            limpios.append(r)
    if not limpios:
        return 0, 0

    subject = (subject or "").replace("\r", " ").replace("\n", " ")
    enviados = fallidos = 0
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASS)
            for dest in limpios:
                try:
                    cuerpo = MIMEMultipart("alternative")
                    cuerpo.attach(MIMEText(body_text, "plain", "utf-8"))
                    if body_html:
                        cuerpo.attach(MIMEText(body_html, "html", "utf-8"))
                    cuerpo["Subject"] = subject
                    cuerpo["From"] = formataddr(("Arcano Record Store", config.SMTP_FROM))
                    cuerpo["To"] = dest
                    server.sendmail(config.SMTP_FROM, [dest], cuerpo.as_string())
                    enviados += 1
                except Exception:
                    fallidos += 1
    except Exception:
        fallidos += len(limpios) - enviados
    return enviados, fallidos


def send_reserva_notification(reserva, disc, comprobante_path=None) -> tuple[bool, str]:
    """Envia notificacion de nueva reserva al admin, con el comprobante adjunto si lo hay."""
    # Lista de discos del pedido (soporta carrito con varios)
    items = list(getattr(reserva, "items_efectivos", None) or [])
    if items:
        discos_txt = "\n".join(
            f"  - {it.artista_snap or '(disco)'} - {it.titulo_snap or ''} ($ {(it.precio_uyu_snap or 0):,.0f} UYU)"
            for it in items
        )
        discos_html = "".join(
            f'<p style="margin:4px 0;"><strong>{_e(it.artista_snap or "(disco)")}</strong> - {_e(it.titulo_snap or "")} '
            f'<span style="color:#10b981;">$ {(it.precio_uyu_snap or 0):,.0f} UYU</span></p>'
            for it in items
        )
        total_uyu = sum((it.precio_uyu_snap or 0) for it in items)
        n = len(items)
    else:
        precio = (disc.precio_mostrar or 0)
        discos_txt = f"  - {disc.artista} - {disc.titulo} ($ {precio:,.0f} UYU)"
        discos_html = (f'<p style="margin:4px 0;"><strong>{_e(disc.artista or "")}</strong> - {_e(disc.titulo or "")} '
                       f'<span style="color:#10b981;">$ {precio:,.0f} UYU</span></p>')
        total_uyu = precio
        n = 1

    if disc is not None:
        _primero = f"{disc.artista} - {disc.titulo}"
    elif items:
        _primero = f"{items[0].artista_snap or ''} - {items[0].titulo_snap or ''}".strip(" -")
    else:
        _primero = "pedido"
    subject = f"[Arcano] Nueva reserva #{reserva.id}: {_primero}" + (f" (+{n - 1})" if n > 1 else "")
    _cobro_txt, _cobro_html = _bloque_cobro(reserva, total_uyu)

    metodo_label = {
        "transferencia": "Transferencia bancaria",
        "mercadopago": "Mercado Pago",
        "coordinar": "Coordinar por mensaje",
    }.get(reserva.metodo_pago or "", "Coordinar por mensaje")

    if getattr(reserva, "pagado", False):
        comp_txt = "Estado:   PAGADO (Mercado Pago)"
        comp_html = '<p style="color:#10b981; font-weight:700;">&#10003; PAGADO por Mercado Pago</p>'
    elif reserva.comprobante_file:
        comp_txt = "Comprobante:  ADJUNTO a este mail"
        comp_html = '<p style="color:#10b981; font-weight:600;">&#128206; Comprobante de transferencia adjunto a este mail</p>'
    elif (reserva.metodo_pago or "") == "transferencia":
        comp_txt = "Comprobante:  el cliente todavia NO adjunto"
        comp_html = '<p style="color:#f59e0b;">El cliente todavia no adjunto comprobante</p>'
    else:
        comp_txt = ""
        comp_html = ""

    # Entrega / envio (solo compras). En "coordinar" queda vacio.
    entrega_txt = ""
    entrega_html = ""
    if getattr(reserva, "entrega_tipo", None):
        _et = reserva.entrega_label
        _costo = getattr(reserva, "envio_costo_uyu", 0) or 0
        if _costo > 0:
            _et += f" - $ {_costo:,.0f} UYU de envio"
        elif reserva.entrega_tipo == "interior":
            _et += " - lo cobra la agencia"
        _dir = (reserva.envio_direccion or "").strip()
        if reserva.envio_agencia:
            _dir = (_dir + f" - Agencia: {reserva.envio_agencia}").strip(" -")
        entrega_txt = "ENTREGA\n  " + _et + "\n" + (f"  Direccion: {_dir}\n" if _dir else "")
        entrega_html = ('<h3 style="color:#888;font-size:11px;text-transform:uppercase;letter-spacing:2px;">Entrega</h3>'
                        f'<p><strong>{_e(_et)}</strong>' + (f'<br>&#128205; {_e(_dir)}' if _dir else '') + '</p>')

    body_text = f"""Hola Facundo,

Nueva reserva en Arcano Record Store (#{reserva.id}):

DISCOS ({n})
{discos_txt}
{_cobro_txt}

CLIENTE
  Nombre:   {reserva.nombre}
  WhatsApp: {reserva.whatsapp}
  Email:    {reserva.email or '—'}

PAGO
  Metodo:   {metodo_label}
{('  ' + comp_txt) if comp_txt else ''}
{entrega_txt}NOTA DEL CLIENTE
{reserva.nota or '(sin nota)'}

Gestionar en: /admin/reservas/{reserva.id}
"""

    body_html = f"""<html><body style="font-family: 'Space Grotesk', sans-serif; max-width: 600px; margin: auto; padding: 20px; background: #0a0a0a; color: #fff;">
<h1 style="letter-spacing: 4px; font-weight: 300;">ARCANO</h1>
<h2 style="color: #10b981; font-size: 16px;">Nueva reserva #{reserva.id}</h2>

<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Discos ({n})</h3>
{discos_html}
{_cobro_html}

<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Cliente</h3>
<p><strong>{_e(reserva.nombre or '')}</strong><br>
WhatsApp: {_e(reserva.whatsapp or '')}<br>
Email: {_e(reserva.email or '—')}</p>

<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Pago</h3>
<p>Metodo: <strong>{metodo_label}</strong></p>
{comp_html}

{entrega_html}

{f'<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Nota</h3><p>{_e(reserva.nota)}</p>' if reserva.nota else ''}

<hr style="border: none; border-top: 1px solid #333; margin: 30px 0;">
<p style="font-size: 11px; color: #666;">Reserva #{reserva.id}</p>
</body></html>"""

    attachments = [comprobante_path] if comprobante_path else None
    return send_email(subject, body_text, body_html, attachments=attachments)


# ---------------------------------------------------------------------------
# Mails al CLIENTE con la identidad de marca (manual set. 2026), opcion "Papel":
# fondo crema, logo negro, lineas beige, pie negro con el logo vinilo. Solo Space
# Grotesk (Gmail la cambia por Helvetica/Arial), titulos Medium en mayuscula, sin
# negritas y solo los 5 colores de la paleta. El mail al admin queda aparte.
# ---------------------------------------------------------------------------
_CREMA, _BEIGE, _BLANCO, _MARRON, _NEGRO = "#FFF7EC", "#E9D1AF", "#FFFFFF", "#3D3033", "#1E1C1C"
_FF = "'Space Grotesk', Helvetica, Arial, sans-serif"


def _etiqueta(txt, color=_MARRON):
    return (f'<div style="font-family:{_FF};font-size:11px;font-weight:500;letter-spacing:4px;'
            f'text-transform:uppercase;color:{color};">{txt}</div>')


def _p(html_txt, extra=""):
    return (f'<p style="margin:16px 0 0;font-family:{_FF};font-size:16px;line-height:1.55;'
            f'font-weight:300;color:{_NEGRO};{extra}">{html_txt}</p>')


def _seccion(html_txt, arriba=32):
    return f'<tr><td style="padding-top:{arriba}px;">{html_txt}</td></tr>'


def _boton(texto, url):
    return (f'<a href="{_e(url)}" style="display:inline-block;background:{_NEGRO};color:{_BLANCO};'
            f'font-family:{_FF};font-size:12px;font-weight:500;letter-spacing:3px;text-transform:uppercase;'
            f'text-decoration:none;padding:15px 26px;border-radius:4px;">{texto}</a>')


def marco_cliente(etiqueta, titulo, cuerpo_html, boton=None, ref=None, despues_html=""):
    """HTML completo de un mail al cliente. `cuerpo_html` = filas <tr> (usar _seccion).
    `boton` = (texto, url). `ref` = texto chico arriba a la derecha (ej. "Pedido #12")."""
    logo = f"{SITE_URL}/static/brand/logo-primary-negro.png"
    vinilo = f"{SITE_URL}/static/brand/logo-vinilo-blanco.png"
    ref_html = _etiqueta(ref) if ref else ""
    boton_html = (f'<tr><td style="padding:34px 0 0;">{_boton(*boton)}</td></tr>' if boton else "")
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500&display=swap" rel="stylesheet">
</head><body style="margin:0;padding:0;background:{_CREMA};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_CREMA};"><tr><td align="center" style="padding:0 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;">
  <tr><td style="padding:36px 0 28px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td><a href="{SITE_URL}"><img src="{logo}" width="150" alt="Arcano Record Store" style="display:block;width:150px;height:auto;border:0;"></a></td>
      <td style="text-align:right;vertical-align:middle;">{ref_html}</td>
    </tr></table>
  </td></tr>
  <tr><td style="border-top:1px solid {_BEIGE};padding-top:36px;">
    {_etiqueta(etiqueta)}
    <h1 style="margin:14px 0 0;font-family:{_FF};font-size:28px;line-height:1.15;font-weight:500;letter-spacing:1px;text-transform:uppercase;color:{_NEGRO};">{titulo}</h1>
  </td></tr>
  {cuerpo_html}
  {boton_html}
  {despues_html}
  <tr><td style="padding-bottom:40px;"></td></tr>
</table>
</td></tr>
<tr><td align="center" style="background:{_NEGRO};padding:34px 16px;">
  <a href="{SITE_URL}"><img src="{vinilo}" width="48" alt="Arcano Record Store" style="display:block;width:48px;height:auto;border:0;margin:0 auto;"></a>
  <div style="font-family:{_FF};font-size:13px;font-weight:300;color:{_BEIGE};margin-top:18px;">
    <a href="{SITE_URL}" style="color:{_BEIGE};text-decoration:none;">arcanorecordstore.com</a> &nbsp;&middot;&nbsp;
    <a href="https://instagram.com/arcanorecordstore" style="color:{_BEIGE};text-decoration:none;">@arcanorecordstore</a>
  </div>
  <div style="font-family:{_FF};font-size:11px;font-weight:300;color:{_BEIGE};margin-top:10px;">Arcano Record Store &middot; Montevideo, Uruguay</div>
</td></tr>
</table></body></html>"""


def mail_simple_html(etiqueta, titulo, parrafos_html, boton=None, nota=None):
    """Mail al cliente de solo texto (activar cuenta, cambiar contrasena, Fire Sale...)
    con el mismo diseno. `parrafos_html` = lista de parrafos ya escapados."""
    cuerpo = "".join(_p(x) for x in parrafos_html)
    filas = f'<tr><td style="padding-top:2px;">{cuerpo}</td></tr>'
    nota_html = (f'<tr><td style="padding-top:22px;font-family:{_FF};font-size:13px;line-height:1.5;'
                 f'font-weight:300;color:{_MARRON};">{nota}</td></tr>') if nota else ""
    return marco_cliente(etiqueta, titulo, filas, boton=boton, despues_html=nota_html)


_DIAS = ["lunes", "martes", "mi&eacute;rcoles", "jueves", "viernes", "s&aacute;bado", "domingo"]
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "setiembre", "octubre", "noviembre", "diciembre"]


def texto_fin_fire_sale(fin, ahora=None):
    """'Termina hoy a las 23:59' / 'manana' / 'el domingo 12 de octubre a las 23:59'.
    `fin` es la hora de Uruguay que se carga en Ajustes. None si no hay reloj."""
    if not fin:
        return None
    from datetime import timedelta
    hora = fin.strftime("%H:%M")
    hoy = (ahora or fin).date()
    if fin.date() == hoy:
        return f"Termina hoy a las {hora}"
    if fin.date() == hoy + timedelta(days=1):
        return f"Termina ma&ntilde;ana a las {hora}"
    return f"Termina el {_DIAS[fin.weekday()]} {fin.day} de {_MESES[fin.month - 1]} a las {hora}"


def mail_fire_sale_html(pct, fin_txt, alcance, discos, mensaje=None, url_tienda=None):
    """Aviso de Fire Sale (opcion "Marca"): banda negra con el % gigante en beige,
    reloj si lo hay, hasta 4 discos con precio tachado y precio con descuento.
    El % y la fecha salen de lo que esta activo en Ajustes."""
    url_tienda = url_tienda or SITE_URL
    donde = "en toda la tienda" if alcance == "todo" else "en discos seleccionados"
    chip = (f'<span style="display:inline-block;background:{_NEGRO};color:{_BEIGE};font-family:{_FF};'
            f'font-size:10px;font-weight:500;letter-spacing:2px;padding:4px 8px;border-radius:3px;">-{pct}%</span>')

    def tarjeta(d):
        base = float(getattr(d, "precio_mostrar", None) or getattr(d, "precio_uyu", 0) or 0)
        nuevo = round(base * (1 - pct / 100.0))
        cover = _cover_url_abs(d)
        href = f"{SITE_URL}/disco/{d.id}"
        img = (f'<a href="{href}"><img src="{_e(cover)}" width="268" alt="" style="display:block;width:100%;'
               f'height:auto;border-radius:4px;border:0;"></a>' if cover else
               f'<div style="padding-top:100%;background:{_BEIGE};border-radius:4px;"></div>')
        return f"""<td width="50%" style="vertical-align:top;padding:0 8px 22px;">
  {img}
  <div style="margin-top:10px;">{chip}</div>
  <div style="font-family:{_FF};font-size:11px;font-weight:500;letter-spacing:1.5px;text-transform:uppercase;color:{_NEGRO};margin-top:10px;">{_e(d.artista or "")}</div>
  <div style="font-family:{_FF};font-size:15px;font-weight:300;color:{_NEGRO};margin-top:2px;"><a href="{href}" style="color:{_NEGRO};text-decoration:none;">{_e(d.titulo or "")}</a></div>
  <div style="font-family:{_FF};margin-top:8px;"><span style="font-size:13px;font-weight:300;color:{_MARRON};text-decoration:line-through;">$ {_money(base)}</span>
  &nbsp;<span style="font-size:18px;font-weight:500;color:{_NEGRO};">$ {_money(nuevo)}</span></div>
</td>"""

    discos = list(discos or [])[:4]
    filas = "".join(
        "<tr>" + tarjeta(discos[i]) + (tarjeta(discos[i + 1]) if i + 1 < len(discos) else "<td></td>") + "</tr>"
        for i in range(0, len(discos), 2))
    bloque_discos = (f"""<tr><td style="padding-top:30px;">{_etiqueta('Algunos de la tienda' if alcance == 'todo' else 'Discos en Fire Sale')}</td></tr>
    <tr><td style="padding-top:14px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{filas}</table></td></tr>"""
                     if discos else "")
    reloj = (f"""<tr><td style="padding:26px 0 0;">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="border:1px solid {_BLANCO};border-radius:4px;padding:11px 16px;font-family:{_FF};font-size:13px;font-weight:400;letter-spacing:1px;color:{_BLANCO};">&#9201;&nbsp; {fin_txt}</td></tr></table>
    </td></tr>""" if fin_txt else "")
    extra = (f'<p style="margin:14px 0 0;font-family:{_FF};font-size:16px;line-height:1.55;font-weight:300;'
             f'color:{_NEGRO};">{_e(mensaje).replace(chr(10), "<br>")}</p>' if mensaje else "")
    logo = f"{SITE_URL}/static/brand/logo-primary-blanco.png"
    vinilo = f"{SITE_URL}/static/brand/logo-vinilo-blanco.png"
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500&display=swap" rel="stylesheet">
</head><body style="margin:0;padding:0;background:{_CREMA};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_CREMA};">
<tr><td align="center" style="background:{_NEGRO};padding:0 16px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;">
    <tr><td style="padding:30px 0 0;"><a href="{SITE_URL}"><img src="{logo}" width="130" alt="Arcano Record Store" style="display:block;width:130px;height:auto;border:0;"></a></td></tr>
    <tr><td style="padding:44px 0 8px;">{_etiqueta('Fire Sale &middot; Solo para clientes', _BLANCO)}</td></tr>
    <tr><td style="font-family:{_FF};font-size:128px;line-height:0.95;font-weight:500;letter-spacing:-3px;color:{_BEIGE};">{pct}%</td></tr>
    <tr><td style="font-family:{_FF};font-size:30px;line-height:1.1;font-weight:500;letter-spacing:6px;text-transform:uppercase;color:{_BEIGE};padding-top:6px;">Off {donde}</td></tr>
    {reloj}
    <tr><td style="padding-bottom:40px;"></td></tr>
  </table>
</td></tr>
<tr><td align="center" style="padding:0 16px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;">
    <tr><td style="padding-top:36px;font-family:{_FF};font-size:16px;line-height:1.55;font-weight:300;color:{_NEGRO};">Hola. Como ten&eacute;s cuenta en Arcano, ten&eacute;s {pct}% de descuento {donde}. Inici&aacute; sesi&oacute;n en la tienda y los precios ya aparecen con el descuento.{extra}</td></tr>
    {bloque_discos}
    <tr><td style="padding:8px 0 44px;">
      <a href="{_e(url_tienda)}" style="display:block;text-align:center;background:{_NEGRO};color:{_BLANCO};font-family:{_FF};font-size:13px;font-weight:500;letter-spacing:4px;text-transform:uppercase;text-decoration:none;padding:18px 26px;border-radius:4px;">Ver el Fire Sale</a>
      <div style="font-family:{_FF};font-size:13px;font-weight:300;color:{_MARRON};text-align:center;margin-top:12px;">Los precios con descuento se ven al iniciar sesi&oacute;n.</div>
    </td></tr>
  </table>
</td></tr>
<tr><td align="center" style="background:{_NEGRO};padding:34px 16px;">
  <a href="{SITE_URL}"><img src="{vinilo}" width="48" alt="Arcano Record Store" style="display:block;width:48px;height:auto;border:0;margin:0 auto;"></a>
  <div style="font-family:{_FF};font-size:13px;font-weight:300;color:{_BEIGE};margin-top:18px;">
    <a href="{SITE_URL}" style="color:{_BEIGE};text-decoration:none;">arcanorecordstore.com</a> &nbsp;&middot;&nbsp;
    <a href="https://instagram.com/arcanorecordstore" style="color:{_BEIGE};text-decoration:none;">@arcanorecordstore</a>
  </div>
  <div style="font-family:{_FF};font-size:11px;font-weight:300;color:{_BEIGE};margin-top:10px;">Arcano Record Store &middot; Montevideo, Uruguay</div>
</td></tr>
</table></body></html>"""


def _items_cliente(reserva, discs):
    """[(artista, titulo, disco|None, precio)] con los precios REALES (snapshots)."""
    items = list(getattr(reserva, "items_efectivos", []) or [])
    if items:
        return [(it.artista_snap, it.titulo_snap, getattr(it, "disco", None), it.precio_uyu_snap) for it in items]
    return [(d.artista, d.titulo, d, d.precio_mostrar) for d in discs]


def _fila_cliente(artista, titulo, disc=None, precio=None):
    disc_id = getattr(disc, "id", None) if disc is not None else None
    href = f"{SITE_URL}/disco/{disc_id}" if disc_id else None
    cover = _cover_url_abs(disc)
    if cover:
        img = (f'<img src="{_e(cover)}" width="76" height="76" alt="" style="display:block;width:76px;'
               f'height:76px;object-fit:cover;border-radius:4px;border:0;">')
    else:
        img = (f'<div style="width:76px;height:76px;border-radius:4px;background:{_BEIGE};"></div>')
    if href:
        img = f'<a href="{href}" style="text-decoration:none;">{img}</a>'
    detalle = " &middot; ".join(_e(str(x)) for x in (getattr(disc, "sello", None), getattr(disc, "anio", None)) if x)
    tit = _e(titulo or "")
    if href:
        tit = f'<a href="{href}" style="color:{_NEGRO};text-decoration:none;">{tit}</a>'
    precio_td = (f'<td width="90" style="vertical-align:top;text-align:right;font-family:{_FF};font-size:15px;'
                 f'font-weight:400;color:{_NEGRO};white-space:nowrap;">$ {_money(precio)}</td>'
                 if precio is not None else "")
    return f"""<tr><td style="padding:16px 0;border-bottom:1px solid {_BEIGE};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td width="76" style="vertical-align:top;">{img}</td>
    <td style="vertical-align:top;padding-left:16px;font-family:{_FF};">
      <div style="font-size:12px;font-weight:500;letter-spacing:1.5px;text-transform:uppercase;color:{_NEGRO};">{_e(artista or "")}</div>
      <div style="font-size:16px;font-weight:300;color:{_NEGRO};margin-top:3px;">{tit}</div>
      {f'<div style="font-size:12px;font-weight:300;color:{_MARRON};margin-top:6px;">{detalle}</div>' if detalle else ''}
    </td>
    {precio_td}
  </tr></table>
</td></tr>"""


def _lista_cliente(filas, etiqueta="Tu pedido"):
    return _seccion(f"""{_etiqueta(etiqueta)}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;border-top:1px solid {_BEIGE};">
{''.join(_fila_cliente(*f) for f in filas)}
</table>""", arriba=36)


def _totales_cliente(reserva, subtotal):
    envio = float(getattr(reserva, "envio_costo_uyu", 0) or 0)
    recargo = float(getattr(reserva, "recargo_mp", 0) or 0)
    total = float(getattr(reserva, "total_final", 0) or 0) or (float(subtotal or 0) + envio)
    et = getattr(reserva, "entrega_tipo", None)

    def linea(k, v, grande=False):
        st = ("font-size:18px;font-weight:500;padding-top:12px;" if grande
              else "font-size:14px;font-weight:300;padding:3px 0;")
        borde = f"border-top:1px solid {_NEGRO};" if grande else ""
        return (f'<tr><td style="font-family:{_FF};{st}{borde}color:{_NEGRO};">{k}</td>'
                f'<td style="font-family:{_FF};{st}{borde}color:{_NEGRO};text-align:right;white-space:nowrap;">{v}</td></tr>')

    filas = []
    if et == "montevideo":
        filas.append(linea("Env&iacute;o", f"$ {_money(envio)}" if envio > 0 else "Gratis (3 discos o m&aacute;s)"))
    elif et == "interior":
        filas.append(linea("Env&iacute;o", "Lo cobra la agencia"))
    elif envio > 0:
        filas.append(linea("Env&iacute;o", f"$ {_money(envio)}"))
    if recargo > 0:
        filas.append(linea("Recargo Mercado Pago", f"$ {_money(recargo)}"))
    if filas:   # con envio o recargo se muestra el detalle; si no, solo el total
        filas.insert(0, linea("Discos", f"$ {_money(subtotal)}"))
    filas.append(linea("Total", f"$ {_money(total)} UYU", grande=True))
    return (f'<tr><td><table role="presentation" cellpadding="0" cellspacing="0" align="right" '
            f'style="width:340px;max-width:100%;margin-top:18px;">{"".join(filas)}</table></td></tr>')


def _entrega_cliente(reserva):
    et = getattr(reserva, "entrega_tipo", None)
    if not et:
        return ""
    titulo = {"retiro": "Retiro en Pocitos", "montevideo": "Env&iacute;o en Montevideo",
              "interior": "Env&iacute;o al interior"}.get(et, _e(reserva.entrega_label or ""))
    dire = (getattr(reserva, "envio_direccion", "") or "").strip()
    agencia = (getattr(reserva, "envio_agencia", "") or "").strip()
    linea2 = _e(dire) if dire else ""
    if agencia:
        linea2 = (linea2 + " &middot; " if linea2 else "") + "Agencia: " + _e(agencia)
    if et == "retiro" and not linea2:
        linea2 = "Coordinamos d&iacute;a y horario por WhatsApp."
    return _seccion(f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BEIGE};border-radius:4px;"><tr><td style="padding:20px 22px;">
  {_etiqueta('Entrega', _NEGRO)}
  <div style="font-family:{_FF};font-size:15px;font-weight:400;color:{_NEGRO};margin-top:10px;">{titulo}</div>
  {f'<div style="font-family:{_FF};font-size:14px;font-weight:300;color:{_NEGRO};margin-top:4px;">{linea2}</div>' if linea2 else ''}
</td></tr></table>""")


def _pasos_cliente(pasos):
    filas = "".join(
        f'<tr><td width="36" style="font-family:{_FF};font-size:13px;font-weight:500;color:{_MARRON};'
        f'padding:6px 0;vertical-align:top;">{i:02d}</td><td style="font-family:{_FF};font-size:15px;'
        f'font-weight:300;color:{_NEGRO};padding:6px 0;line-height:1.45;">{t}</td></tr>'
        for i, t in enumerate(pasos, 1))
    return _seccion(f"""{_etiqueta('Qu&eacute; sigue')}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;">{filas}</table>""")


def _boton_pedido(reserva):
    """Con cuenta: a su pedido. Sin cuenta: a la tienda."""
    if getattr(reserva, "cliente_id", None):
        return ("Ver mi pedido", f"{SITE_URL}/cuenta")
    return ("Ir a la tienda", SITE_URL)


def _nombre_pila(reserva):
    return _e(((getattr(reserva, "nombre", "") or "").strip().split(" ") or [""])[0])


def _paso_entrega(reserva):
    if getattr(reserva, "entrega_tipo", None) == "retiro":
        return "Coordinamos d&iacute;a y horario para que los retires en Pocitos."
    return "Coordinamos d&iacute;a y horario de entrega."


def send_reserva_confirmacion_cliente(reserva, disc) -> tuple[bool, str]:
    """Envia un email de confirmacion al CLIENTE que reservo (un disco)."""
    if not reserva.email:
        return False, "El cliente no dejo email"

    subject = f"Confirmamos tu reserva - {disc.artista} - {disc.titulo}"

    precio_uyu = (disc.precio_mostrar or 0)
    body_text = f"""Hola {reserva.nombre},

Recibimos tu reserva en Arcano Record Store. Estos son los detalles:

DISCO
  Artista:  {disc.artista}
  Titulo:   {disc.titulo}
  Sello:    {disc.sello or '—'}
  Precio:   $ {precio_uyu:,.0f} UYU

Nos vamos a contactar con vos por WhatsApp ({reserva.whatsapp}) para
coordinar el pago y la entrega o retiro.

Gracias por elegir Arcano.

Arcano Record Store
Montevideo, Uruguay
"""
    wa = _e(reserva.whatsapp or "")
    cuerpo = (
        _seccion(_p(f"Hola {_nombre_pila(reserva)}, recibimos tu reserva. El disco queda reservado para vos "
                    "mientras coordinamos.", "margin-top:0;"), arriba=16)
        + _lista_cliente([(disc.artista, disc.titulo, disc, precio_uyu)], "Tu reserva")
        + _pasos_cliente([f"Te escribimos por WhatsApp al {wa}.",
                          "Coordinamos el pago y la entrega o el retiro.",
                          "Tu disco llega revisado y bien embalado."])
    )
    body_html = marco_cliente("Reserva recibida", "Tu disco est&aacute; reservado.", cuerpo,
                              boton=_boton_pedido(reserva), ref=f"Reserva #{reserva.id}")
    return send_email(subject, body_text, body_html, to=reserva.email)


def send_reserva_confirmacion_cliente_multi(reserva, discs) -> tuple[bool, str]:
    """Confirmacion al cliente para una reserva a coordinar con varios discos (carrito)."""
    if not reserva.email:
        return False, "El cliente no dejo email"

    total, lineas, _filas_viejas = _lineas_pedido(reserva, discs)
    cobro_txt, _cobro_viejo = _bloque_cobro(reserva, total)
    subject = f"Confirmamos tu reserva - {reserva.cantidad_discos or len(discs)} item(s)"
    body_text = f"""Hola {reserva.nombre},

Recibimos tu reserva en Arcano Record Store. Estos son los discos:

{lineas}

{cobro_txt}

Nos vamos a contactar con vos por WhatsApp ({reserva.whatsapp}) para
coordinar el pago y la entrega o retiro.

Gracias por elegir Arcano.

Arcano Record Store
Montevideo, Uruguay
"""
    wa = _e(reserva.whatsapp or "")
    cuerpo = (
        _seccion(_p(f"Hola {_nombre_pila(reserva)}, recibimos tu reserva. Los discos quedan reservados para vos "
                    "mientras coordinamos.", "margin-top:0;"), arriba=16)
        + _lista_cliente(_items_cliente(reserva, discs), "Tu reserva")
        + _totales_cliente(reserva, total)
        + _entrega_cliente(reserva)
        + _pasos_cliente([f"Te escribimos por WhatsApp al {wa}.",
                          "Coordinamos el pago y la entrega o el retiro.",
                          "Tus discos llegan revisados y bien embalados."])
    )
    body_html = marco_cliente("Reserva recibida", "Tus discos est&aacute;n reservados.", cuerpo,
                              boton=_boton_pedido(reserva), ref=f"Reserva #{reserva.id}")
    return send_email(subject, body_text, body_html, to=reserva.email)


def send_compra_confirmacion_cliente(reserva, discs, metodo="mercadopago") -> tuple[bool, str]:
    """Confirmacion al cliente que YA PAGO (transferencia o Mercado Pago).
    A diferencia de la reserva a coordinar, agradece la COMPRA y solo coordina
    la ENTREGA (no el pago)."""
    if not reserva.email:
        return False, "El cliente no dejo email"

    total, lineas, _filas_viejas = _lineas_pedido(reserva, discs)
    wa = _e(reserva.whatsapp or "")
    if metodo == "transferencia":
        pago_txt = "Recibimos tu comprobante de transferencia. En cuanto lo verifiquemos, coordinamos la entrega."
        etiqueta = "Comprobante recibido"
        intro = ("Recibimos tu comprobante de transferencia. En cuanto verifiquemos el pago, "
                 "coordinamos la entrega por WhatsApp.")
        pasos = ["Verificamos que la transferencia haya llegado.",
                 f"Te escribimos por WhatsApp al {wa}.", _paso_entrega(reserva)]
    else:
        pago_txt = "Tu pago quedo confirmado. Ahora coordinamos la entrega."
        etiqueta = "Compra confirmada"
        medio = "con Mercado Pago " if metodo == "mercadopago" else ""
        intro = f"Tu pago {medio}qued&oacute; confirmado. Ahora coordinamos la entrega por WhatsApp."
        pasos = [f"Te escribimos por WhatsApp al {wa}.", _paso_entrega(reserva),
                 "Tus discos llegan revisados y bien embalados."]

    cobro_txt, _cobro_viejo = _bloque_cobro(reserva, total)
    subject = f"Gracias por tu compra en Arcano - {reserva.cantidad_discos or len(discs)} item(s)"
    body_text = f"""Hola {reserva.nombre},

Gracias por comprar en Arcano Record Store!

{lineas}

{cobro_txt}

{pago_txt}
Nos contactamos por WhatsApp ({reserva.whatsapp}) para coordinar la entrega o el retiro
(envio a todo Uruguay o pick up en Pocitos).

Gracias por elegir Arcano.

Arcano Record Store
Montevideo, Uruguay
"""
    cuerpo = (
        _seccion(_p(intro, "margin-top:0;"), arriba=16)
        + _lista_cliente(_items_cliente(reserva, discs))
        + _totales_cliente(reserva, total)
        + _entrega_cliente(reserva)
        + _pasos_cliente(pasos)
    )
    body_html = marco_cliente(etiqueta, f"Gracias por tu compra, {_nombre_pila(reserva)}.", cuerpo,
                              boton=_boton_pedido(reserva), ref=f"Pedido #{reserva.id}")
    return send_email(subject, body_text, body_html, to=reserva.email)


# Titulo + texto por defecto de cada estado (editable desde el admin al enviar)
RESERVA_ESTADO_MENSAJES = {
    "Pendiente": ("Recibimos tu pedido",
                  "Recibimos tu pedido y te lo reservamos. Coordinamos el pago y la entrega por WhatsApp."),
    "Pagada": ("Confirmamos tu pago",
               "Confirmamos tu pago. ¡Gracias por comprar en Arcano! Coordinamos la entrega por WhatsApp."),
    "Cancelada": ("Tu pedido fue cancelado",
                  "Tu pedido fue cancelado. Si fue un error o querés retomarlo, escribinos por WhatsApp."),
}


def texto_estado_default(estado) -> str:
    """Texto por defecto del mail para un estado (para prellenar el campo editable)."""
    par = RESERVA_ESTADO_MENSAJES.get(estado)
    return par[1] if par else ""


def send_reserva_estado_cliente(reserva, nuevo_estado, discs, texto_custom=None) -> tuple[bool, str]:
    """Avisa al cliente que su pedido cambio de estado (confirmado / completado /
    cancelado). Lo dispara Facundo con el boton del admin. Si `texto_custom` viene,
    usa ese texto en vez del por defecto."""
    if not reserva.email:
        return False, "El cliente no dejo email"

    if nuevo_estado not in RESERVA_ESTADO_MENSAJES:
        return False, "Ese estado no manda aviso"
    titulo, texto = RESERVA_ESTADO_MENSAJES[nuevo_estado]
    if texto_custom and texto_custom.strip():
        texto = texto_custom.strip()

    lineas = "\n".join(f"  - {d.artista} - {d.titulo}" for d in discs)
    subject = f"{titulo} - pedido #{reserva.id} - Arcano Record Store"
    body_text = f"""Hola {reserva.nombre},

{texto}

Pedido #{reserva.id}:
{lineas}

Arcano Record Store
Montevideo, Uruguay
"""
    texto_html = _e(texto).replace("\n", "<br>")
    cuerpo = (
        _seccion(_p(f"Hola {_nombre_pila(reserva)}.", "margin-top:0;") + _p(texto_html, "margin-top:8px;"), arriba=16)
        + (_lista_cliente([(d.artista, d.titulo, d, None) for d in discs]) if discs else "")
    )
    etiqueta = {"Pendiente": "Pedido recibido", "Pagada": "Pago confirmado",
                "Cancelada": "Pedido cancelado"}.get(nuevo_estado, "Tu pedido")
    boton = None if nuevo_estado == "Cancelada" else _boton_pedido(reserva)
    body_html = marco_cliente(etiqueta, _e(titulo) + ".", cuerpo, boton=boton, ref=f"Pedido #{reserva.id}")
    return send_email(subject, body_text, body_html, to=reserva.email)
