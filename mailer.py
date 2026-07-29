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
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

import config


def is_configured() -> bool:
    """True si todas las variables SMTP necesarias estan seteadas."""
    return all([
        config.SMTP_HOST,
        config.SMTP_USER,
        config.SMTP_PASS,
        config.SMTP_FROM,
        config.SMTP_TO,
    ])


def send_email(subject: str, body_text: str, body_html: str = None) -> tuple[bool, str]:
    """Envia un email. Devuelve (ok, mensaje)."""
    if not is_configured():
        return False, "SMTP no configurado en .env"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Arcano Record Store", config.SMTP_FROM))
    msg["To"] = config.SMTP_TO

    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.sendmail(config.SMTP_FROM, [config.SMTP_TO], msg.as_string())
        return True, "Enviado"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"Autenticacion SMTP fallida: {e}"
    except smtplib.SMTPException as e:
        return False, f"Error SMTP: {e}"
    except Exception as e:
        return False, f"Error inesperado: {type(e).__name__}: {e}"


def send_reserva_notification(reserva, disc) -> tuple[bool, str]:
    """Envia notificacion de nueva reserva al admin."""
    subject = f"[Arcano] Nueva reserva: {disc.artista} - {disc.titulo}"

    precio_uyu = (disc.precio_uyu or 0)
    body_text = f"""Hola Facundo,

Nueva reserva en Arcano Record Store:

DISCO
  Artista:  {disc.artista}
  Titulo:   {disc.titulo}
  Sello:    {disc.sello or '—'}
  Precio:   $ {precio_uyu:,.0f} UYU

CLIENTE
  Nombre:   {reserva.nombre}
  WhatsApp: {reserva.whatsapp}
  Email:    {reserva.email or '—'}

NOTA DEL CLIENTE
{reserva.nota or '(sin nota)'}

Gestionar en: /admin/reservas/{reserva.id}
"""

    body_html = f"""<html><body style="font-family: 'Space Grotesk', sans-serif; max-width: 600px; margin: auto; padding: 20px; background: #0a0a0a; color: #fff;">
<h1 style="letter-spacing: 4px; font-weight: 300;">ARCANO</h1>
<h2 style="color: #10b981; font-size: 16px;">Nueva reserva</h2>

<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Disco</h3>
<p><strong>{disc.artista}</strong><br>
{disc.titulo}<br>
<span style="color: #888;">{disc.sello or '—'}</span><br>
<span style="color: #10b981;">$ {precio_uyu:,.0f} UYU</span></p>

<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Cliente</h3>
<p><strong>{reserva.nombre}</strong><br>
WhatsApp: {reserva.whatsapp}<br>
Email: {reserva.email or '—'}</p>

{f'<h3 style="color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Nota</h3><p>{reserva.nota}</p>' if reserva.nota else ''}

<hr style="border: none; border-top: 1px solid #333; margin: 30px 0;">
<p style="font-size: 11px; color: #666;">Reserva #{reserva.id}</p>
</body></html>"""

    return send_email(subject, body_text, body_html)
