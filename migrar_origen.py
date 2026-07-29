"""
Migracion: agrega la columna 'origen' a la tabla de discos.

Marca todos los discos existentes como 'arcano' por defecto.
Despues podes cambiar los que sean de tu coleccion personal a 'personal'
desde el dashboard.

USO: parar el servidor, correr este script una vez, volver a arrancar.
    python migrar_origen.py
"""
import sqlite3
import os

DB_PATH = os.path.join("instance", "arcano_stock.db")

def migrar():
    if not os.path.exists(DB_PATH):
        print(f"No se encontro la base en {DB_PATH}. Nada que migrar.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Ver si la columna ya existe
    cur.execute("PRAGMA table_info(discs)")
    columnas = [row[1] for row in cur.fetchall()]

    if "origen" in columnas:
        print("La columna 'origen' ya existe. No hace falta migrar.")
        conn.close()
        return

    print("Agregando columna 'origen'...")
    cur.execute("ALTER TABLE discs ADD COLUMN origen VARCHAR(20) NOT NULL DEFAULT 'arcano'")
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM discs")
    total = cur.fetchone()[0]
    print(f"Listo. {total} discos marcados como 'arcano' (Arcano) por defecto.")
    print("Ahora podes cambiar los que sean de tu coleccion personal desde el dashboard.")

    conn.close()

if __name__ == "__main__":
    migrar()
