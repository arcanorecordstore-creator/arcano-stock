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

    subject = f"[Arcano] Nueva reserva #{reserva.id}: {disc.artista} - {disc.titulo}" + (f" (+{n - 1})" if n > 1 else "")

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

    body_text = f"""Hola Facundo,

Nueva reserva en Arcano Record Store (#{reserva.id}):

DISCOS ({n})
{discos_txt}
  TOTAL: $ {total_uyu:,.0f} UYU

CLIENTE
  Nombre:   {reserva.nombre}
  WhatsApp: {reserva.whatsapp}
  Email:    {reserva.email or '—'}

PAGO
  Metodo:   {metodo_label}
{('  ' + comp_txt) if comp_txt else ''}
NOTA DEL CLIENTE
{reserva.nota or '(sin nota)'}

Gestionar en: /admin/reservas/{reserva.id}
"""

    body_html = f"""<html><body style="font-family: 'Space Grotesk', sans-serif; max-width: 600px; margin: auto; padding: 20px; background: #0a0a0a; color: #fff;">
<h1 style="letter-spacing: 4px; font-weight: 300;">ARCANO</h1>
<h2 style="color: #10b981; font-size: 16px;">Nueva reserva #{reserva.id}</h2>

<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Discos ({n})</h3>
{discos_html}
<p style="margin-top:8px;"><strong>Total: $ {total_uyu:,.0f} UYU</strong></p>

<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Cliente</h3>
<p><strong>{_e(reserva.nombre or '')}</strong><br>
WhatsApp: {_e(reserva.whatsapp or '')}<br>
Email: {_e(reserva.email or '—')}</p>

<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Pago</h3>
<p>Metodo: <strong>{metodo_label}</strong></p>
{comp_html}

{f'<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Nota</h3><p>{_e(reserva.nota)}</p>' if reserva.nota else ''}

<hr style="border: none; border-top: 1px solid #333; margin: 30px 0;">
<p style="font-size: 11px; color: #666;">Reserva #{reserva.id}</p>
</body></html>"""

    attachments = [comprobante_path] if comprobante_path else None
    return send_email(subject, body_text, body_html, attachments=attachments)


def send_reserva_confirmacion_cliente(reserva, disc) -> tuple[bool, str]:
    """Envia un email de confirmacion al CLIENTE que reservo."""
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

    body_html = f"""<html><body style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; background: #0a0a0a; color: #fff;">
<h1 style="letter-spacing: 4px; font-weight: 300;">ARCANO</h1>
<h2 style="color: #10b981; font-size: 16px;">Confirmamos tu reserva</h2>

<p>Hola <strong>{_e(reserva.nombre or '')}</strong>, recibimos tu reserva. Estos son los detalles:</p>

<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Disco</h3>
<p><strong>{_e(disc.artista or '')}</strong><br>
{_e(disc.titulo or '')}<br>
<span style="color: #888;">{_e(disc.sello or '—')}</span><br>
<span style="color: #10b981;">$ {precio_uyu:,.0f} UYU</span></p>

<p>Nos vamos a contactar con vos por WhatsApp (<strong>{_e(reserva.whatsapp or '')}</strong>) para coordinar el pago y la entrega o retiro.</p>

<p style="margin-top: 24px;">Gracias por elegir Arcano.</p>

<hr style="border: none; border-top: 1px solid #333; margin: 30px 0;">
<p style="font-size: 11px; color: #666;">Arcano Record Store &middot; Montevideo, Uruguay<br>Reserva #{reserva.id}</p>
</body></html>"""

    return send_email(subject, body_text, body_html, to=reserva.email)


def send_reserva_confirmacion_cliente_multi(reserva, discs) -> tuple[bool, str]:
    """Confirmacion al cliente para una reserva con varios discos (carrito)."""
    if not reserva.email:
        return False, "El cliente no dejo email"

    total = sum((d.precio_mostrar or 0) for d in discs)
    lineas = "\n".join(
        f"  - {d.artista} - {d.titulo} ($ {(d.precio_mostrar or 0):,.0f} UYU)"
        for d in discs
    )
    subject = f"Confirmamos tu reserva - {len(discs)} disco(s)"
    body_text = f"""Hola {reserva.nombre},

Recibimos tu reserva en Arcano Record Store. Estos son los discos:

{lineas}

TOTAL: $ {total:,.0f} UYU

Nos vamos a contactar con vos por WhatsApp ({reserva.whatsapp}) para
coordinar el pago y la entrega o retiro.

Gracias por elegir Arcano.

Arcano Record Store
Montevideo, Uruguay
"""

    filas_html = "".join(
        f'<p style="margin:4px 0;"><strong>{_e(d.artista or "")}</strong> - {_e(d.titulo or "")} '
        f'<span style="color:#10b981;">$ {(d.precio_mostrar or 0):,.0f} UYU</span></p>'
        for d in discs
    )
    body_html = f"""<html><body style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; background: #0a0a0a; color: #fff;">
<h1 style="letter-spacing: 4px; font-weight: 300;">ARCANO</h1>
<h2 style="color: #10b981; font-size: 16px;">Confirmamos tu reserva</h2>
<p>Hola <strong>{_e(reserva.nombre or '')}</strong>, recibimos tu reserva de estos discos:</p>
{filas_html}
<p style="margin-top:12px;"><strong>Total: $ {total:,.0f} UYU</strong></p>
<p>Nos vamos a contactar con vos por WhatsApp (<strong>{_e(reserva.whatsapp or '')}</strong>) para coordinar el pago y la entrega o retiro.</p>
<p style="margin-top: 24px;">Gracias por elegir Arcano.</p>
<hr style="border: none; border-top: 1px solid #333; margin: 30px 0;">
<p style="font-size: 11px; color: #666;">Arcano Record Store &middot; Montevideo, Uruguay<br>Reserva #{reserva.id}</p>
</body></html>"""

    return send_email(subject, body_text, body_html, to=reserva.email)


def send_compra_confirmacion_cliente(reserva, discs, metodo="mercadopago") -> tuple[bool, str]:
    """Confirmacion al cliente que YA PAGO (transferencia o Mercado Pago).
    A diferencia de la reserva a coordinar, agradece la COMPRA y solo coordina
    la ENTREGA (no el pago)."""
    if not reserva.email:
        return False, "El cliente no dejo email"

    total = sum((d.precio_mostrar or 0) for d in discs)
    lineas = "\n".join(
        f"  - {d.artista} - {d.titulo} ($ {(d.precio_mostrar or 0):,.0f} UYU)"
        for d in discs
    )
    if metodo == "transferencia":
        pago_txt = "Recibimos tu comprobante de transferencia. En cuanto lo verifiquemos, coordinamos la entrega."
        pago_html = "Recibimos tu <strong>comprobante de transferencia</strong>. En cuanto lo verifiquemos, coordinamos la entrega."
    else:
        pago_txt = "Tu pago quedo confirmado. Ahora coordinamos la entrega."
        pago_html = "Tu <strong>pago quedo confirmado</strong>. Ahora coordinamos la entrega."

    subject = f"Gracias por tu compra en Arcano - {len(discs)} disco(s)"
    body_text = f"""Hola {reserva.nombre},

Gracias por comprar en Arcano Record Store!

{lineas}

TOTAL: $ {total:,.0f} UYU

{pago_txt}
Nos contactamos por WhatsApp ({reserva.whatsapp}) para coordinar la entrega o el retiro
(envio a todo Uruguay o pick up en Pocitos).

Gracias por elegir Arcano.

Arcano Record Store
Montevideo, Uruguay
"""

    filas_html = "".join(
        f'<p style="margin:4px 0;"><strong>{_e(d.artista or "")}</strong> - {_e(d.titulo or "")} '
        f'<span style="color:#10b981;">$ {(d.precio_mostrar or 0):,.0f} UYU</span></p>'
        for d in discs
    )
    body_html = f"""<html><body style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; background: #0a0a0a; color: #fff;">
<h1 style="letter-spacing: 4px; font-weight: 300;">ARCANO</h1>
<h2 style="color: #10b981; font-size: 16px;">&iexcl;Gracias por tu compra!</h2>
<p>Hola <strong>{_e(reserva.nombre or '')}</strong>, gracias por comprar en Arcano Record Store. Esto es lo que llevaste:</p>
{filas_html}
<p style="margin-top:12px;"><strong>Total: $ {total:,.0f} UYU</strong></p>
<p>{pago_html}</p>
<p>Nos contactamos por WhatsApp (<strong>{_e(reserva.whatsapp or '')}</strong>) para coordinar la entrega o el retiro (env&iacute;o a todo Uruguay o pick up en Pocitos).</p>
<p style="margin-top: 24px;">Gracias por elegir Arcano.</p>
<hr style="border: none; border-top: 1px solid #333; margin: 30px 0;">
<p style="font-size: 11px; color: #666;">Arcano Record Store &middot; Montevideo, Uruguay<br>Compra #{reserva.id}</p>
</body></html>"""

    return send_email(subject, body_text, body_html, to=reserva.email)


# Titulo + texto por defecto de cada estado (editable desde el admin al enviar)
RESERVA_ESTADO_MENSAJES = {
    "Confirmada": ("Confirmamos tu pedido",
                   "Confirmamos tu pedido. En breve coordinamos la entrega por WhatsApp."),
    "Completada": ("Tu pedido esta completo",
                   "Tu pedido se completo. Gracias por comprar en Arcano!"),
    "Cancelada": ("Tu pedido fue cancelado",
                  "Tu pedido fue cancelado. Si fue un error o queres retomarlo, escribinos por WhatsApp."),
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

    filas_html = "".join(
        f'<p style="margin:4px 0;"><strong>{_e(d.artista or "")}</strong> - {_e(d.titulo or "")}</p>'
        for d in discs
    )
    body_html = f"""<html><body style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; background: #0a0a0a; color: #fff;">
<h1 style="letter-spacing: 4px; font-weight: 300;">ARCANO</h1>
<h2 style="color: #10b981; font-size: 16px;">{titulo}</h2>
<p>Hola <strong>{_e(reserva.nombre or '')}</strong>, {texto}</p>
{filas_html}
<hr style="border: none; border-top: 1px solid #333; margin: 30px 0;">
<p style="font-size: 11px; color: #666;">Arcano Record Store &middot; Montevideo, Uruguay<br>Pedido #{reserva.id}</p>
</body></html>"""

    return send_email(subject, body_text, body_html, to=reserva.email)
