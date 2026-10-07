"""Carga variables de entorno desde .env."""
import os

from dotenv import load_dotenv

load_dotenv()

# API key de Anthropic (para Claude vision)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Flask
FLASK_PORT = int(os.getenv("FLASK_PORT", "5001") or 5001)
# Por defecto la copia local escucha SOLO en esta compu (127.0.0.1). Antes usaba
# 0.0.0.0 (toda la red): en un bar u hotel cualquiera en el mismo wifi entraba al
# admin sin contrasena y podia bajar la base. Para usarla desde el celular, poner en
# .env FLASK_HOST=0.0.0.0 Y ademas ADMIN_USER/ADMIN_PASS (sin login no se abre a la red).
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1").strip()
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")

# Credenciales del admin (opcional; si faltan, el admin queda sin login - modo dev)
ADMIN_USER = os.getenv("ADMIN_USER", "").strip()
ADMIN_PASS = os.getenv("ADMIN_PASS", "")

# Candado: sin usuario/contrasena de admin, la app nunca se abre a la red.
if FLASK_HOST not in ("127.0.0.1", "localhost") and not (ADMIN_USER and ADMIN_PASS):
    print("AVISO: FLASK_HOST pide abrir la app a la red pero no hay ADMIN_USER/ADMIN_PASS. "
          "Se usa 127.0.0.1 (solo esta compu) para no dejar el admin abierto.")
    FLASK_HOST = "127.0.0.1"

# Email (configuracion para Fase 2 - se completa cuando lo configuremos)
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
SMTP_TO = os.getenv("SMTP_TO", "").strip()  # destinatario de notificaciones de reserva
