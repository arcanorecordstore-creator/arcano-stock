"""Protecciones del admin, independientes de los formularios de negocio."""
import hmac
import secrets
from urllib.parse import urlsplit

from flask import abort, request, session
import auth


def csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_urlsafe(32)
    return session['_csrf_token']


def init_security(app):
    app.jinja_env.globals['csrf_token'] = csrf_token

    @app.before_request
    def protect_admin():
        # Comprobantes antiguos en static tambien son privados.
        if request.path.startswith('/static/comprobantes/'):
            abort(404)
        if not request.path.startswith('/admin/'):
            return
        if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return
        if request.endpoint != 'admin_login' and not auth.is_logged_in():
            return  # el decorador mantiene su redireccion al login
        origin = request.headers.get('Origin') or request.headers.get('Referer')
        if origin:
            source = urlsplit(origin)
            target = urlsplit(request.host_url)
            if (source.scheme, source.netloc.lower()) != (target.scheme, target.netloc.lower()):
                abort(403, description='Origen inválido. Volvé a abrir el admin.')
        expected = session.get('_csrf_token', '')
        supplied = request.headers.get('X-CSRF-Token') or request.form.get('_csrf_token', '')
        if not expected or not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
            abort(403, description='La sesión del formulario venció. Recargá la página y volvé a intentar.')

    @app.after_request
    def private_responses(response):
        if request.path.startswith('/admin/'):
            response.headers['Cache-Control'] = 'no-store, private'
        return response
