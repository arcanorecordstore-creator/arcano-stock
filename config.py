"""Carga variables de entorno desde .env."""
import os

from dotenv import load_dotenv

load_dotenv()

# API key de Anthropic (para Claude vision)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Flask
FLASK_PORT = int(os.getenv("FLASK_PORT", "5001") or 5001)
# 0.0.0.0 = escucha en todas las interfaces de red, para que se pueda entrar
# desde el celular (mismo wifi) con la IP de la PC. En localhost sigue andando.
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0").strip()
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")

# Credenciales del admin (opcional; si faltan, el admin queda sin login - modo dev)
ADMIN_USER = os.getenv("ADMIN_USER", "").strip()
ADMIN_PASS = os.getenv("ADMIN_PASS", "")

# Email (configuracion para Fase 2 - se completa cuando lo configuremos)
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
SMTP_TO = os.getenv("SMTP_TO", "").strip()  # destinatario de notificaciones de reserva
