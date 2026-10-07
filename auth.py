"""Autenticacion simple para el admin.

Lee credenciales desde .env:
  ADMIN_USER=facundo
  ADMIN_PASS=mi_password_secreto

Guarda la sesion via Flask session (cookie firmada con SECRET_KEY).
Si no hay credenciales en .env, deshabilita el login (acceso libre, modo dev).
"""
import hmac
from functools import wraps

from flask import session, redirect, url_for, request, flash

import config


def _get_admin_credentials():
    """Devuelve (user, password) desde el entorno, o (None, None) si no estan."""
    user = getattr(config, "ADMIN_USER", None)
    password = getattr(config, "ADMIN_PASS", None)
    if user and password:
        return user.strip(), password
    return None, None


def auth_enabled() -> bool:
    """True si hay credenciales configuradas en .env."""
    user, password = _get_admin_credentials()
    return bool(user and password)


def is_logged_in() -> bool:
    """True si el usuario actual paso por login."""
    if not auth_enabled():
        return True  # Modo dev: sin login, todos adentro
    return session.get("admin_logged_in") is True


def check_credentials(user: str, password: str) -> bool:
    """Verifica credenciales con comparacion segura (sin timing attacks)."""
    expected_user, expected_pass = _get_admin_credentials()
    if not expected_user or not expected_pass:
        return False
    user_ok = hmac.compare_digest(user.strip(), expected_user)
    pass_ok = hmac.compare_digest(password, expected_pass)
    return user_ok and pass_ok


def login(user: str) -> None:
    """Marca la sesion como autenticada."""
    session["admin_logged_in"] = True
    session["admin_user"] = user


def logout() -> None:
    """Limpia la sesion."""
    session.pop("admin_logged_in", None)
    session.pop("admin_user", None)


def admin_required(view_func):
    """Decorador para proteger rutas admin."""
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            # Guardar URL para redirigir despues del login
            next_url = request.full_path if request.method == "GET" else None
            return redirect(url_for("admin_login", next=next_url))
        return view_func(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Autenticacion de CLIENTES (tienda publica) - separada del admin
# ---------------------------------------------------------------------------

def cliente_login(cliente_id: int, pv: str = None) -> None:
    """Marca la sesion con el cliente logueado. `pv` = "version" de la contrasena
    (un pedazo del hash): si despues alguien cambia la contrasena, las sesiones
    abiertas con la version vieja se cierran solas (ver _sesion_cliente_vigente)."""
    session["cliente_id"] = cliente_id
    if pv:
        session["cliente_pv"] = pv
    else:
        session.pop("cliente_pv", None)


def cliente_logout() -> None:
    session.pop("cliente_id", None)
    session.pop("cliente_pv", None)


def cliente_actual_id():
    """Devuelve el id del cliente logueado, o None."""
    return session.get("cliente_id")


def cliente_required(view_func):
    """Decorador para rutas que requieren cliente logueado."""
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("cliente_id"):
            next_url = request.full_path if request.method == "GET" else None
            return redirect(url_for("cliente_login_page", next=next_url))
        return view_func(*args, **kwargs)
    return wrapper
