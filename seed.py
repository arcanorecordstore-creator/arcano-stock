"""Script de seed: inserta 2 discos de prueba en la DB.

Uso (desde PowerShell con venv activo, en la carpeta del proyecto):

    python seed.py

Si los discos de prueba ya existen (mismo artista + titulo), los saltea.
"""
from datetime import datetime, timedelta

from app import app
from models import db, Disc, StatusHistory, Setting


DISCOS_DEMO = [
    {
        "artista": "Jhobei",
        "titulo": "Electronic EP",
        "sello": "Talman Records",
        "anio": 2023,
        "formato": '12"',
        "genero": "Minimal / Deep Tech",
        "tracklist": (
            "A1. Jhobei - Electronic\n"
            "A2. Jhobei - Untitled\n"
            "B1. Jhobei - Movements\n"
            "B2. Jhobei - Closing"
        ),
        "cover_url": None,
        "source_url": "https://www.deejay.de/Jhobei_Electronic_EP_TALMAN18_Vinyl__1158955",
        "costo_eur": 12.50,
        "envio_eur": 4.00,
        "precio_uyu": 1400.0,
        "stock": 1,
        "estado": "Disponible",
        "condicion": "Nuevo / Sellado",
        "notas": "Disco de prueba sembrado por seed.py",
    },
    {
        "artista": "Rhadow",
        "titulo": "Sunset Tales",
        "sello": "Sublunar",
        "anio": 2024,
        "formato": '12"',
        "genero": "Minimal House",
        "tracklist": (
            "A1. Rhadow - Sunset\n"
            "A2. Rhadow - Tales\n"
            "B1. Rhadow - Afterglow"
        ),
        "cover_url": None,
        "source_url": None,
        "costo_eur": 14.00,
        "envio_eur": 4.50,
        "precio_uyu": 1600.0,
        "stock": 2,
        "estado": "Disponible",
        "condicion": "Nuevo / Sellado",
        "notas": "Segundo disco de prueba",
    },
]


def seed():
    with app.app_context():
        tc = Setting.get_tc()
        print(f"Tipo de cambio actual: {tc}")

        creados = 0
        salteados = 0

        for data in DISCOS_DEMO:
            existe = Disc.query.filter_by(
                artista=data["artista"], titulo=data["titulo"]
            ).first()
            if existe:
                print(f"  [SKIP] Ya existe: {data['artista']} - {data['titulo']}")
                salteados += 1
                continue

            disc = Disc(**data)
            disc.tc_usado = tc
            disc.recalcular_costo_final()
            # Fecha de ingreso un poco distinta para cada uno
            disc.fecha_ingreso = datetime.utcnow() - timedelta(days=creados)
            db.session.add(disc)
            db.session.flush()  # para obtener disc.id

            # Registrar evento de ingreso en el historial
            evento = StatusHistory(
                disc_id=disc.id,
                estado=disc.estado,
                nota="Ingreso al stock (seed)",
                fecha=disc.fecha_ingreso,
            )
            db.session.add(evento)

            creados += 1
            print(
                f"  [OK]   Creado #{disc.id}: {disc.artista} - {disc.titulo} "
                f"(costo final UYU: {disc.costo_final_uyu})"
            )

        db.session.commit()
        print(f"\nResultado: {creados} creados, {salteados} salteados.")


if __name__ == "__main__":
    seed()
