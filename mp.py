"""Integracion con Mercado Pago (Checkout Pro).

Se activa SOLO si el setting 'mercadopago_activo' == '1' Y hay una env var
MP_ACCESS_TOKEN cargada. Si falta cualquiera de las dos, la tienda funciona
como siempre (reserva sin pago online). Las credenciales son secretos: viven
en variables de entorno (Render), nunca en la base ni en el codigo.
"""
import os
import requests

from models import Setting

MP_API = "https://api.mercadopago.com"


def access_token() -> str:
    return (os.environ.get("MP_ACCESS_TOKEN") or "").strip()


def es_sandbox() -> bool:
    """Las credenciales de prueba de MP empiezan con 'TEST-'."""
    return access_token().startswith("TEST-")


def recargo_pct() -> float:
    """Porcentaje que se le suma al total (la comision que paga el cliente)."""
    try:
        return float(Setting.get("mp_recargo_pct", "7") or 7)
    except (TypeError, ValueError):
        return 7.0


def activo() -> bool:
    """True si Mercado Pago esta prendido en Ajustes Y hay credencial cargada."""
    return Setting.get("mercadopago_activo", "0") == "1" and bool(access_token())


def calcular_recargo(subtotal: float) -> float:
    """Monto del recargo (comision que paga el cliente), redondeado a pesos."""
    return round(subtotal * recargo_pct() / 100.0)


def crear_preferencia(items, back_urls, notification_url, external_reference,
                      payer_email=None) -> dict:
    """Crea una preferencia de pago en Mercado Pago (Checkout Pro).

    items = [{"title": str, "quantity": int, "unit_price": float}, ...]
    Devuelve el dict de MP (con 'id', 'init_point', 'sandbox_init_point')
    o levanta excepcion si la API falla.
    """
    token = access_token()
    if not token:
        raise RuntimeError("Falta MP_ACCESS_TOKEN")

    body = {
        "items": [
            {
                "title": str(it["title"])[:250],
                "quantity": int(it.get("quantity", 1)),
                "unit_price": float(it["unit_price"]),
                "currency_id": "UYU",
            }
            for it in items
        ],
        "back_urls": back_urls,
        "auto_return": "approved",
        "notification_url": notification_url,
        "external_reference": str(external_reference),
        "statement_descriptor": "ARCANO",
    }
    if payer_email:
        body["payer"] = {"email": payer_email}

    resp = requests.post(
        f"{MP_API}/checkout/preferences",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def obtener_pago(payment_id) -> dict | None:
    """Consulta el estado de un pago en MP. Devuelve el dict del pago o None."""
    token = access_token()
    if not token:
        return None
    try:
        resp = requests.get(
            f"{MP_API}/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def url_checkout(pref: dict) -> str:
    """URL a la que redirigir al cliente para pagar. Usa la sandbox si el token
    es de prueba, y la de produccion si es real."""
    if es_sandbox():
        return pref.get("sandbox_init_point") or pref.get("init_point") or ""
    return pref.get("init_point") or pref.get("sandbox_init_point") or ""
