"""Buscar un disco del stock a partir de lo que la IA leyo en una foto (v6.20).

La IA lee la etiqueta / tapa / sticker y devuelve {artista, titulo, sello, catalogo,
tracks}. Aca se compara eso contra TODOS los discos con coincidencia flexible:
minusculas, sin tildes, palabras parciales ("vernis" encuentra "Verniß") y errores
chicos de lectura ("Deyayo" encuentra "Deyayu"). Cada disco suma puntos por artista,
titulo, numero de catalogo, sello y temas; se devuelven los mejores.

Es Python puro (no toca la base): app.py le pasa los discos y sus tracks.
"""
import difflib
import re
import unicodedata
from urllib.parse import unquote, urlsplit

# Estados en los que el disco esta "en la mano" (tiene sentido venderlo / que llegue).
ESTADOS_ACTIVOS = ("Disponible", "Reservado", "Pre-order", "Encargo")

# Palabras que no ayudan a distinguir un disco de otro (estan en todos lados).
_RELLENO = {
    "ep", "lp", "the", "a", "an", "of", "and", "feat", "featuring", "ft", "vs",
    "records", "recordings", "record", "recs", "music", "musique", "vinyl", "label",
    "various", "artists", "artist", "va", "v", "remix", "remixes", "rmx", "mix",
    "de", "la", "el", "los", "las", "y", "del", "en", "vol", "volume", "part", "pt",
    "edition", "presents", "pres", "w", "with", "inc", "ltd", "x",
}
# Titulos "de relleno" que se cargan cuando el disco no tiene nombre.
_TITULOS_VACIOS = {"sin titulo", "untitled", "s t", "st", "no title"}

# Pesos de cada dato al comparar.
PESO_ARTISTA = 35
PESO_TITULO = 35
PESO_CATALOGO = 45
PESO_SELLO = 15
PESO_TRACKS = 25
PESO_GENERAL = 15     # todas las palabras leidas contra todo el disco (cubre cruces)

# Minimo para mostrar un candidato (fraccion del puntaje maximo posible).
MINIMO_FOTO = 0.22
MINIMO_TEXTO = 0.5


# ---------------------------------------------------------------------------
# Normalizacion
# ---------------------------------------------------------------------------

def normalizar(texto):
    """Minusculas, sin tildes ni signos: 'Verniß — Black Shape' -> 'verniss black shape'."""
    t = str(texto or "").lower()
    t = (t.replace("ß", "ss").replace("ø", "o").replace("æ", "ae").replace("œ", "oe")
          .replace("đ", "d").replace("ł", "l").replace("&", " "))
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def compacto(texto):
    """Sin espacios: 'JK 47' y 'JK47' quedan iguales ('jk47')."""
    return normalizar(texto).replace(" ", "")


def palabras(texto):
    """Palabras utiles (sin relleno). 'Various - Night Drive EP' -> ['night', 'drive']."""
    out = []
    for p in normalizar(texto).split():
        if p in _RELLENO:
            continue
        if len(p) < 2 and not p.isdigit():
            continue
        if p not in out:
            out.append(p)
    return out


def _es_catalogo(token):
    """Una palabra que parece numero de catalogo: letras Y numeros (SDT002, PARPILL05)."""
    t = compacto(token)
    return len(t) >= 3 and bool(re.search(r"[a-z]", t)) and bool(re.search(r"\d", t))


def _partes_catalogo(clave):
    """'parpill005' -> ('parpill', 5, ''); 'lnoe196v' -> ('lnoe', 196, 'v')."""
    m = re.match(r"^([a-z]+)0*(\d+)([a-z]*)$", clave or "")
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3)


# ---------------------------------------------------------------------------
# Similitud
# ---------------------------------------------------------------------------

def _sim_palabra(a, b, cache):
    """1 = igual; 0.85 = una empieza con la otra; hasta ~0.85 = parecida (error de lectura)."""
    if a == b:
        return 1.0
    clave = (a, b) if a < b else (b, a)
    if clave in cache:
        return cache[clave]
    s = 0.0
    if len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)):
        s = 0.85
    elif len(a) >= 4 and len(b) >= 4 and abs(len(a) - len(b)) <= 3:
        sm = difflib.SequenceMatcher(None, a, b)
        if sm.quick_ratio() >= 0.8:
            r = sm.ratio()
            if r >= 0.8:
                s = r * 0.9
    cache[clave] = s
    return s


def _cobertura(q_pal, d_pal, cache):
    """Que parte de las palabras de q aparecen en d (0 a 1)."""
    if not q_pal or not d_pal:
        return 0.0
    total = 0.0
    for q in q_pal:
        mejor = 0.0
        for d in d_pal:
            s = _sim_palabra(q, d, cache)
            if s > mejor:
                mejor = s
                if mejor >= 1.0:
                    break
        total += mejor
    return total / len(q_pal)


def similitud(leido, guardado, cache=None):
    """Parecido entre dos textos (0 a 1). Mira sobre todo que lo leido este en lo
    guardado, y un poco lo inverso (para no premiar un disco que tiene mucho mas)."""
    cache = {} if cache is None else cache
    if not leido or not guardado:
        return 0.0
    if compacto(leido) and compacto(leido) == compacto(guardado):
        return 1.0
    qp, dp = palabras(leido), palabras(guardado)
    if not qp or not dp:
        return 0.0
    return 0.75 * _cobertura(qp, dp, cache) + 0.25 * _cobertura(dp, qp, cache)


# ---------------------------------------------------------------------------
# Datos del disco
# ---------------------------------------------------------------------------

def catalogos_del_disco(disco):
    """Numeros de catalogo que se le conocen al disco: el que viene pegado al sello
    ('Parti Pillz PARPILL03'), el titulo si ES un catalogo ('JCE002') y el que trae
    el link de Deejay.de ('..._SDT002_Vinyl__1228873')."""
    out = set()
    for tok in (getattr(disco, "sello", None) or "").split():
        if _es_catalogo(tok):
            out.add(compacto(tok))
    titulo = (getattr(disco, "titulo", None) or "").strip()
    if titulo and len(titulo.split()) <= 2 and _es_catalogo(titulo):
        out.add(compacto(titulo))
    url = getattr(disco, "source_url", None) or ""
    if "deejay." in url.lower():
        try:
            path = unquote(urlsplit(url).path or "")
        except Exception:
            path = ""
        m = re.search(r"_([A-Za-z0-9\-.]+)_(?:Vinyl|LP|CD)", path)
        if m and _es_catalogo(m.group(1)):
            out.add(compacto(m.group(1)))
    return out


def _catalogos_leidos(leido):
    """Lo que la IA leyo como catalogo, mas cualquier palabra con pinta de catalogo
    que haya quedado en el sello (a veces lo pega)."""
    out = []
    cat = compacto(leido.get("catalogo"))
    if cat and len(cat) >= 3:
        out.append(cat)
    for tok in (leido.get("sello") or "").split():
        if _es_catalogo(tok) and len(compacto(tok)) >= 4 and compacto(tok) not in out:
            out.append(compacto(tok))
    return out


def _sim_catalogo(q, c):
    if q == c:
        return 1.0
    pq, pc = _partes_catalogo(q), _partes_catalogo(c)
    if pq and pc and pq[0] == pc[0] and pq[1] == pc[1]:
        return 0.95                       # 'PARPILL005' vs 'PARPILL05', 'LNOE196V' vs 'LNOE196'
    if len(q) >= 4 and len(c) >= 4 and (q in c or c in q):
        return 0.8
    if len(q) >= 5 and len(c) >= 5:
        r = difflib.SequenceMatcher(None, q, c).ratio()
        if r >= 0.85:
            return 0.7                    # un caracter mal leido
    if pq and pc and pq[0] == pc[0] and len(pq[0]) >= 3:
        return 0.3                        # mismo sello / serie, otro numero
    return 0.0


def _titulo_util(titulo):
    return None if normalizar(titulo) in _TITULOS_VACIOS else titulo


def _sello_base(sello):
    """Sello sin el numero de catalogo pegado."""
    partes = [p for p in (sello or "").split() if not _es_catalogo(p)]
    return " ".join(partes) or (sello or "")


# ---------------------------------------------------------------------------
# Busqueda
# ---------------------------------------------------------------------------

def puntuar(leido, disco, tracks=None, cache=None):
    """Compara lo leido contra un disco. Devuelve (puntaje, maximo, detalle) donde
    detalle = {campo: 0..1} con lo que coincidio."""
    cache = {} if cache is None else cache
    tracks = tracks or []
    art_l = leido.get("artista")
    tit_l = leido.get("titulo")
    sel_l = leido.get("sello")
    trk_l = [t for t in (leido.get("tracks") or []) if t]
    cats_l = _catalogos_leidos(leido)

    art_d = disco.artista or ""
    tit_d = _titulo_util(disco.titulo) or ""
    sel_d = _sello_base(disco.sello)
    titulos_tracks = [t.titulo for t in tracks if getattr(t, "titulo", None)]

    puntaje, maximo, detalle = 0.0, 0.0, {}

    if palabras(art_l):     # "Various" / "VA" no cuentan: no distinguen nada
        # Tambien cruzado (la IA a veces confunde artista con titulo).
        s = max(similitud(art_l, art_d, cache), 0.8 * similitud(art_l, tit_d, cache))
        maximo += PESO_ARTISTA
        puntaje += PESO_ARTISTA * s
        detalle["artista"] = s

    if palabras(tit_l):
        s = max(similitud(tit_l, tit_d, cache), 0.8 * similitud(tit_l, art_d, cache))
        # En las etiquetas muchas veces el "titulo" es un tema del disco.
        for tt in titulos_tracks:
            s = max(s, 0.85 * similitud(tit_l, tt, cache))
        maximo += PESO_TITULO
        puntaje += PESO_TITULO * s
        detalle["titulo"] = s

    if cats_l:
        cats_d = catalogos_del_disco(disco)
        s = 0.0
        for q in cats_l:
            for c in cats_d:
                s = max(s, _sim_catalogo(q, c))
        maximo += PESO_CATALOGO
        puntaje += PESO_CATALOGO * s
        detalle["catalogo"] = s

    if palabras(sel_l):
        s = similitud(sel_l, sel_d, cache)
        maximo += PESO_SELLO
        puntaje += PESO_SELLO * s
        detalle["sello"] = s

    trk_validos = [t for t in trk_l if palabras(t)]
    if trk_validos:
        if titulos_tracks:
            s = sum(max(similitud(t, tt, cache) for tt in titulos_tracks) for t in trk_validos) / len(trk_validos)
        else:
            s = 0.0
        maximo += PESO_TRACKS
        puntaje += PESO_TRACKS * s
        detalle["tracks"] = s

    # Todas las palabras leidas contra todo el disco (cubre lecturas cruzadas o raras).
    todas_l = palabras(" ".join([art_l or "", tit_l or "", sel_l or ""] + trk_l))
    if todas_l:
        todas_d = palabras(" ".join([art_d, tit_d, sel_d] + titulos_tracks
                                    + [getattr(t, "artista", None) or "" for t in tracks]))
        s = _cobertura(todas_l, todas_d, cache)
        maximo += PESO_GENERAL
        puntaje += PESO_GENERAL * s
        detalle["general"] = s

    return puntaje, maximo, detalle


def _nivel(confianza, detalle):
    art, tit, cat = detalle.get("artista", 0), detalle.get("titulo", 0), detalle.get("catalogo", 0)
    if confianza >= 0.7 or cat >= 0.95 or (art >= 0.85 and tit >= 0.85):
        return "alta"
    if confianza >= 0.45 or cat >= 0.7:
        return "media"
    return "baja"


_MOTIVOS = (("artista", "artista"), ("titulo", "título"), ("catalogo", "catálogo"),
            ("sello", "sello"), ("tracks", "temas"))


def buscar(leido, discos, tracks_por_disco=None, limite=5, minimo=MINIMO_FOTO):
    """Los `limite` discos que mas se parecen a lo leido, del mejor al peor.
    Cada item: {disco, puntaje, confianza (0-100), nivel, motivos}."""
    tracks_por_disco = tracks_por_disco or {}
    cache = {}
    candidatos = []
    for d in discos:
        if getattr(d, "es_mystery_box", False):
            continue
        puntaje, maximo, detalle = puntuar(leido, d, tracks_por_disco.get(d.id), cache)
        if maximo <= 0 or puntaje <= 0:
            continue
        conf = puntaje / maximo
        nivel = _nivel(conf, detalle)
        if conf < minimo and nivel == "baja":
            continue
        # Desempate: entre copias iguales, primero la que esta en la mano.
        orden = puntaje + (2.0 if d.estado in ESTADOS_ACTIVOS else 0.0)
        if detalle.get("catalogo", 0) >= 0.95:
            orden += 20     # numero de catalogo exacto: casi seguro es ese
        motivos = [txt for k, txt in _MOTIVOS if detalle.get(k, 0) >= 0.6]
        candidatos.append({
            "disco": d, "puntaje": round(puntaje, 2), "orden": orden,
            "confianza": int(round(min(conf, 1.0) * 100)), "nivel": nivel, "motivos": motivos,
        })
    candidatos.sort(key=lambda c: (-c["orden"], c["disco"].id))
    # Si hay uno que coincide claro, los de coincidencia baja solo meten ruido.
    if candidatos and candidatos[0]["nivel"] == "alta":
        candidatos = [c for c in candidatos if c["nivel"] != "baja"]
    return candidatos[:limite]


def buscar_texto(q, discos, tracks_por_disco=None, limite=8):
    """Buscador a mano: lo que escribas contra artista, titulo, sello, catalogo y temas."""
    tracks_por_disco = tracks_por_disco or {}
    q = " ".join(str(q or "").split())[:120]
    qn = normalizar(q)
    if not qn:
        return []
    qc = qn.replace(" ", "")
    q_pal = palabras(q) or qn.split()
    cats_q = [compacto(t) for t in q.split() if _es_catalogo(t)]
    cache = {}
    out = []
    for d in discos:
        if getattr(d, "es_mystery_box", False):
            continue
        tracks = tracks_por_disco.get(d.id) or []
        texto_d = " ".join([d.artista or "", _titulo_util(d.titulo) or "", d.sello or ""]
                           + [t.titulo or "" for t in tracks] + sorted(catalogos_del_disco(d)))
        dn = normalizar(texto_d)
        d_pal = dn.split()
        s = _cobertura(q_pal, d_pal, cache)
        motivos = []
        if qc and len(qc) >= 2 and qc in dn.replace(" ", ""):
            s = max(s, 0.9 if len(qc) >= 3 else 0.6)
        if cats_q:
            cats_d = catalogos_del_disco(d)
            sc = max((_sim_catalogo(a, b) for a in cats_q for b in cats_d), default=0.0)
            if sc >= 0.7:
                s = max(s, sc)
                motivos.append("catálogo")
        if s < MINIMO_TEXTO:
            continue
        orden = s * 100 + (2.0 if d.estado in ESTADOS_ACTIVOS else 0.0)
        # Que lo escrito este en el artista o el titulo pesa un poco mas que en un tema.
        if q_pal and _cobertura(q_pal, palabras(f"{d.artista} {d.titulo}"), cache) >= 0.85:
            orden += 5
        out.append({"disco": d, "puntaje": round(s * 100, 1), "orden": orden,
                    "confianza": int(round(s * 100)), "nivel": "alta" if s >= 0.85 else "media",
                    "motivos": motivos})
    out.sort(key=lambda c: (-c["orden"], c["disco"].id))
    # Con una coincidencia completa, lo que apenas roza queda afuera.
    if out and out[0]["puntaje"] >= 85:
        out = [c for c in out if c["puntaje"] >= 60]
    return out[:limite]


def hay_algo_leido(leido):
    """True si la IA leyo al menos un dato que sirva para buscar."""
    if not isinstance(leido, dict):
        return False
    return bool(palabras(leido.get("artista")) or palabras(leido.get("titulo"))
                or palabras(leido.get("sello")) or _catalogos_leidos(leido)
                or any(palabras(t) for t in (leido.get("tracks") or [])))
