
#importaciones
import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, timedelta, timezone
from io import BytesIO
import hashlib
import qrcode
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
import numpy as np
import re

st.set_page_config(page_title="Asistencia I.E. Yarinacocha", page_icon="escudo.png", layout="wide")
st.markdown("""

""", unsafe_allow_html=True)
DB_PATH = "asistencia.db"

# 2. UTILIDADES
def ahora():
    return datetime.now(timezone.utc) - timedelta(hours=5)

def hoy_str():
    return ahora().strftime("%Y-%m-%d")

def hora_str():
    return ahora().strftime("%H:%M:%S")

def suma_min(hhmm_, mins):
    t = datetime.strptime(hhmm_, "%H:%M") + timedelta(minutes=mins)
    return t.strftime("%H:%M")

def es_dia_laboral(fecha=None):
    fecha = fecha or ahora()
    return fecha.weekday() < 5

MESES_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

# BASE DE DATOS 
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS turnos (
        id INTEGER PRIMARY KEY, nombre TEXT UNIQUE,
        hora_entrada TEXT, tolerancia_min INTEGER DEFAULT 7
    );
    CREATE TABLE IF NOT EXISTS grados (
        id INTEGER PRIMARY KEY, nombre TEXT UNIQUE
    );
    CREATE TABLE IF NOT EXISTS secciones (
        id INTEGER PRIMARY KEY, nombre TEXT, grado_id INTEGER, turno_id INTEGER,
        FOREIGN KEY (grado_id) REFERENCES grados(id),
        FOREIGN KEY (turno_id) REFERENCES turnos(id),
        UNIQUE(nombre, grado_id, turno_id)
    );
    CREATE TABLE IF NOT EXISTS alumnos (
        id INTEGER PRIMARY KEY, dni TEXT UNIQUE NOT NULL,
        nombres TEXT NOT NULL, apellido_paterno TEXT NOT NULL,
        apellido_materno TEXT, seccion_id INTEGER,
        nombre_apoderado TEXT, telefono_apoderado TEXT,
        FOREIGN KEY (seccion_id) REFERENCES secciones(id)
    );
    CREATE TABLE IF NOT EXISTS asistencias (
        id INTEGER PRIMARY KEY, alumno_id INTEGER NOT NULL,
        fecha TEXT NOT NULL, hora TEXT, estado TEXT NOT NULL,
        justificada INTEGER DEFAULT 0, observacion TEXT,
        FOREIGN KEY (alumno_id) REFERENCES alumnos(id),
        UNIQUE(alumno_id, fecha)
    );
    CREATE TABLE IF NOT EXISTS tardanzas (
        id INTEGER PRIMARY KEY, alumno_id INTEGER NOT NULL,
        fecha TEXT NOT NULL, hora TEXT NOT NULL, numero INTEGER NOT NULL,
        accion TEXT NOT NULL, justificada INTEGER DEFAULT 0,
        observacion TEXT, registrado_por TEXT, timestamp TEXT NOT NULL,
        FOREIGN KEY (alumno_id) REFERENCES alumnos(id)
    );
    CREATE TABLE IF NOT EXISTS actas_compromiso (
        id INTEGER PRIMARY KEY, alumno_id INTEGER NOT NULL,
        fecha TEXT NOT NULL, motivo TEXT, observacion TEXT,
        registrado_por TEXT, timestamp TEXT NOT NULL,
        FOREIGN KEY (alumno_id) REFERENCES alumnos(id)
    );
    CREATE TABLE IF NOT EXISTS observados (
        id INTEGER PRIMARY KEY, alumno_id INTEGER NOT NULL,
        fecha_ingreso TEXT NOT NULL, motivo TEXT,
        activo INTEGER DEFAULT 1, fecha_salida TEXT,
        FOREIGN KEY (alumno_id) REFERENCES alumnos(id)
    );
    CREATE TABLE IF NOT EXISTS dias_especiales (
        id INTEGER PRIMARY KEY, fecha TEXT NOT NULL, descripcion TEXT,
        turno_id INTEGER, hora_entrada TEXT NOT NULL,
        activo INTEGER DEFAULT 1, tipo TEXT DEFAULT 'evento',
        FOREIGN KEY (turno_id) REFERENCES turnos(id),
        UNIQUE(fecha, turno_id)
    );
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY, usuario TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL, rol TEXT NOT NULL, nombres TEXT NOT NULL,
        turno_asignado INTEGER, activo INTEGER DEFAULT 1,
        FOREIGN KEY (turno_asignado) REFERENCES turnos(id)
    );
    CREATE TABLE IF NOT EXISTS auditoria (
        id INTEGER PRIMARY KEY, usuario TEXT, accion TEXT, fecha TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_asist_fecha ON asistencias(fecha);
    CREATE INDEX IF NOT EXISTS idx_asist_alumno ON asistencias(alumno_id);
    CREATE INDEX IF NOT EXISTS idx_tard_alumno ON tardanzas(alumno_id);
    CREATE INDEX IF NOT EXISTS idx_tard_fecha ON tardanzas(fecha);
    CREATE INDEX IF NOT EXISTS idx_alumnos_seccion ON alumnos(seccion_id);
    CREATE INDEX IF NOT EXISTS idx_alumnos_dni ON alumnos(dni);
    CREATE INDEX IF NOT EXISTS idx_actas_alumno ON actas_compromiso(alumno_id);
    CREATE INDEX IF NOT EXISTS idx_obs_alumno ON observados(alumno_id, activo);
    """)

    cols = [r["name"] for r in c.execute("PRAGMA table_info(alumnos)").fetchall()]
    if "nombre_apoderado" not in cols:
        c.execute("ALTER TABLE alumnos ADD COLUMN nombre_apoderado TEXT")
    if "telefono_apoderado" not in cols:
        c.execute("ALTER TABLE alumnos ADD COLUMN telefono_apoderado TEXT")

    cols_de = [r["name"] for r in c.execute("PRAGMA table_info(dias_especiales)").fetchall()]
    if "tipo" not in cols_de:
        c.execute("ALTER TABLE dias_especiales ADD COLUMN tipo TEXT DEFAULT 'evento'")

    if c.execute("SELECT COUNT(*) FROM turnos").fetchone()[0] == 0:
        c.execute("INSERT INTO turnos (nombre, hora_entrada, tolerancia_min) VALUES ('Mañana', '06:45', 7)")
        c.execute("INSERT INTO turnos (nombre, hora_entrada, tolerancia_min) VALUES ('Tarde', '12:20', 7)")

    if c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        h = lambda p: hashlib.sha256(p.encode()).hexdigest()
        c.executemany(
            "INSERT INTO usuarios (usuario, password, rol, nombres, turno_asignado) VALUES (?,?,?,?,?)",
            [
                ("admin",     h("admin2026"),  "Admin",     "Administrador",      None),
                ("toece",     h("toece2026"),  "TOECE",     "Coordinador TOECE",  None),
                ("direccion", h("dir2026"),    "Direccion", "Dirección",          None),
                ("aux_m",     h("auxm2026"),   "Auxiliar",  "Auxiliar Mañana",    1),
                ("aux_t",     h("auxt2026"),   "Auxiliar",  "Auxiliar Tarde",     2),
            ]
        )
    conn.commit()
    conn.close()

@st.cache_resource
def _init_once():
    init_db()
    return True

_init_once()
# 4. SERVICIOS

def autenticar(usuario, password):
    conn = get_db()
    r = conn.execute("SELECT * FROM usuarios WHERE usuario=? AND activo=1", (usuario,)).fetchone()
    conn.close()
    if not r:
        return None
    if r["password"] != hashlib.sha256(password.encode()).hexdigest():
        return None
    return dict(r)

def auditar(usuario, accion):
    conn = get_db()
    conn.execute("INSERT INTO auditoria (usuario, accion, fecha) VALUES (?,?,?)",
                 (usuario, accion, ahora().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

@st.cache_data(ttl=30)
def turnos():
    conn = get_db()
    rows = conn.execute("SELECT * FROM turnos ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@st.cache_data(ttl=30)
def grados_lista():
    conn = get_db()
    rows = conn.execute("SELECT * FROM grados ORDER BY nombre").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@st.cache_data(ttl=30)
def secciones_por_grado(grado_id):
    conn = get_db()
    rows = conn.execute("SELECT * FROM secciones WHERE grado_id=? ORDER BY nombre", (grado_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@st.cache_data(ttl=60)
def horario_del_dia(turno_id, fecha=None):
    fecha = fecha or hoy_str()
    conn = get_db()
    esp = conn.execute(
        "SELECT hora_entrada FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento' "
        "AND (turno_id=? OR turno_id IS NULL) ORDER BY turno_id DESC LIMIT 1",
        (fecha, turno_id)
    ).fetchone()
    if esp:
        entrada = esp["hora_entrada"]
        conn.close()
        return {"hora_entrada": entrada, "hora_limite": suma_min(entrada, 7), "especial": True}
    t = conn.execute("SELECT hora_entrada, tolerancia_min FROM turnos WHERE id=?", (turno_id,)).fetchone()
    conn.close()
    if not t:
        return {"hora_entrada": "08:00", "hora_limite": "08:07", "especial": False}
    return {
        "hora_entrada": t["hora_entrada"],
        "hora_limite": suma_min(t["hora_entrada"], t["tolerancia_min"]),
        "especial": False,
    }

def registrar_entrada(dni, usuario):
    dni = (dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni):
        return False, "ERROR", "❌ DNI inválido", {}

    conn = get_db()
    al = conn.execute("""
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               s.id AS seccion_id, s.nombre AS seccion,
               g.nombre AS grado, t.id AS turno_id, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.dni = ?
    """, (dni,)).fetchone()
    if not al:
        conn.close()
        return False, "ERROR", "❌ DNI no encontrado", {}
    al = dict(al)

    if usuario["rol"] == "Auxiliar" and usuario["turno_asignado"]:
        if al["turno_id"] != usuario["turno_asignado"]:
            conn.close()
            return False, "ERROR", f"❌ Este alumno es turno {al['turno']}. Tu turno es otro.", {}

    hoy = hoy_str()
    ya = conn.execute("SELECT estado FROM asistencias WHERE alumno_id=? AND fecha=?", (al["id"], hoy)).fetchone()
    if ya:
        conn.close()
        return False, "ERROR", f"⚠️ Ya registrado hoy como {ya['estado']}", {}

    h = horario_del_dia(al["turno_id"], hoy)
    hora_actual = hora_str()
    nombre_completo = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}".strip(", ")

    if hora_actual <= h["hora_limite"] + ":59":
        conn.execute("INSERT INTO asistencias (alumno_id, fecha, hora, estado) VALUES (?,?,?,?)",
                     (al["id"], hoy, hora_actual, "Puntual"))
        conn.commit()
        conn.close()
        auditar(usuario["usuario"], f"Entrada PUNTUAL DNI {dni}")
        return True, "PUNTUAL", f" {nombre_completo} | {al['grado']}{al['seccion']} | PUNTUAL {hora_actual}", al

    ult_acta = conn.execute("SELECT fecha FROM actas_compromiso WHERE alumno_id=? ORDER BY fecha DESC LIMIT 1",
                            (al["id"],)).fetchone()
    desde = ult_acta["fecha"] if ult_acta else "1900-01-01"
    numero = conn.execute("SELECT COUNT(*) FROM tardanzas WHERE alumno_id=? AND fecha > ?",
                          (al["id"], desde)).fetchone()[0] + 1

    if numero <= 2:
        accion = "PERDONADO"
        mensaje = f"🟡 {nombre_completo} | Tardanza {numero}ª (perdonada) {hora_actual}"
    elif numero == 3:
        accion = "DERIVADO_TOECE"
        mensaje = f"🟠 {nombre_completo} | Tardanza 3ª → DERIVAR A TOECE {hora_actual}"
    else:
        accion = "RETENIDO_APODERADO"
        mensaje = f"🔴 {nombre_completo} | Tardanza {numero}ª → NO PASA hasta apoderado {hora_actual}"

    conn.execute("""INSERT INTO tardanzas (alumno_id, fecha, hora, numero, accion, registrado_por, timestamp)
                    VALUES (?,?,?,?,?,?,?)""",
                 (al["id"], hoy, hora_actual, numero, accion, usuario["usuario"],
                  ahora().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    auditar(usuario["usuario"], f"Tardanza {numero}ª DNI {dni} -> {accion}")
    return True, "TARDANZA", mensaje, {**al, "numero": numero, "accion": accion}

def marcar_faltas_al_cierre():
    ult = st.session_state.get("_ult_faltas")
    if ult and (ahora() - ult).total_seconds() < 300:
        return
    st.session_state["_ult_faltas"] = ahora()
    hoy = hoy_str()
    conn = get_db()
    if not es_dia_laboral():
        esp = conn.execute("SELECT tipo FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento'",
                           (hoy,)).fetchone()
        if not esp:
            conn.close()
            return
    for t in turnos():
        h = horario_del_dia(t["id"], hoy)
        if hora_str() < h["hora_limite"]:
            continue
        conn.execute("""INSERT OR IGNORE INTO asistencias (alumno_id, fecha, hora, estado)
                        SELECT a.id, ?, ?, 'Falta' FROM alumnos a
                        JOIN secciones s ON a.seccion_id = s.id
                        WHERE s.turno_id = ?""",
                     (hoy, hora_str(), t["id"]))
    conn.commit()
    conn.close()

def justificar_falta(alumno_id, justificada, observacion, usuario):
    conn = get_db()
    al = conn.execute("SELECT apellido_paterno, apellido_materno, nombres FROM alumnos WHERE id=?",
                      (alumno_id,)).fetchone()
    if not al:
        conn.close()
        return False, "Alumno no encontrado"
    falta = conn.execute("SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND estado='Falta'",
                         (alumno_id, hoy_str())).fetchone()
    if not falta:
        conn.close()
        return False, "⚠️ Ese alumno no tiene una Falta registrada hoy"
    conn.execute("UPDATE asistencias SET justificada=?, observacion=? WHERE id=?",
                 (1 if justificada else 0, observacion, falta["id"]))
    conn.commit()
    conn.close()
    nombre = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}".strip(", ")
    estado_txt = "JUSTIFICADA ✅" if justificada else "INJUSTIFICADA ❌"
    auditar(usuario["usuario"], f"Falta de alumno_id={alumno_id} -> {estado_txt}")
    return True, f"Falta de {nombre} marcada como {estado_txt}"

def registrar_acta(alumno_id, motivo, observacion, usuario):
    conn = get_db()
    ts = ahora().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""INSERT INTO actas_compromiso (alumno_id, fecha, motivo, observacion, registrado_por, timestamp)
                    VALUES (?,?,?,?,?,?)""",
                 (alumno_id, hoy_str(), motivo, observacion, usuario["usuario"], ts))
    ya = conn.execute("SELECT id FROM observados WHERE alumno_id=? AND activo=1", (alumno_id,)).fetchone()
    if not ya:
        conn.execute("""INSERT INTO observados (alumno_id, fecha_ingreso, motivo, activo)
                        VALUES (?,?,?,1)""", (alumno_id, hoy_str(), motivo or "Acta de compromiso"))
    conn.commit()
    conn.close()
    auditar(usuario["usuario"], f"Acta firmada alumno_id={alumno_id}")

def liberar_observado(alumno_id, usuario):
    conn = get_db()
    conn.execute("UPDATE observados SET activo=0, fecha_salida=? WHERE alumno_id=? AND activo=1",
                 (hoy_str(), alumno_id))
    conn.commit()
    conn.close()
    auditar(usuario["usuario"], f"Liberó observado alumno_id={alumno_id}")

@st.cache_data(ttl=30)
def observados_dataframe(solo_activos=True):
    conn = get_db()
    q = """
        SELECT o.id, a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
               a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               o.fecha_ingreso, COALESCE(o.motivo,'') AS motivo,
               o.activo, COALESCE(o.fecha_salida,'') AS fecha_salida
        FROM observados o
        JOIN alumnos a ON o.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
    """
    if solo_activos:
        q += " WHERE o.activo = 1"
    q += " ORDER BY o.fecha_ingreso DESC"
    df = pd.read_sql(q, conn)
    conn.close()
    return df

@st.cache_data(ttl=10)
def metricas_dia(fecha):
    conn = get_db()
    r = conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM alumnos)                                        AS total,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado='Puntual')  AS puntuales,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado='Falta')    AS faltas,
          (SELECT COUNT(*) FROM tardanzas   WHERE fecha=?)                       AS tardanzas,
          (SELECT COUNT(*) FROM tardanzas   WHERE fecha=? AND accion='DERIVADO_TOECE')      AS derivados,
          (SELECT COUNT(*) FROM tardanzas   WHERE fecha=? AND accion='RETENIDO_APODERADO') AS retenidos
    """, (fecha,)*5).fetchone()
    conn.close()
    return dict(r)

def ultimos_registros(fecha, limite=10):
    conn = get_db()
    df = pd.read_sql("""
        SELECT a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
               a.nombres, g.nombre AS grado, s.nombre AS seccion,
               ast.hora, ast.estado
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        WHERE ast.fecha = ?
        ORDER BY ast.hora DESC LIMIT ?
    """, conn, params=[fecha, limite])
    conn.close()
    return df

# 4.7 HELPERS REUTILIZABLES

def filtros_grado_seccion_nombre(key_prefix, placeholder_nombre="Buscar por nombre",
                                  mostrar_todos=True):
    grados = grados_lista()
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        opciones_grado = ([{"id": None, "nombre": "Todos"}] if mostrar_todos else []) + grados
        grado_sel = st.selectbox("Grado", opciones_grado,
                                 format_func=lambda g: g["nombre"],
                                 key=f"{key_prefix}_grado")
    with c2:
        if grado_sel and grado_sel["id"]:
            secs = ([{"id": None, "nombre": "Todas"}] if mostrar_todos else []) + secciones_por_grado(grado_sel["id"])
        else:
            secs = [{"id": None, "nombre": "Todas"}] if mostrar_todos else []
        seccion_sel = st.selectbox("Sección", secs,
                                   format_func=lambda s: s["nombre"],
                                   key=f"{key_prefix}_seccion") if secs else {"id": None, "nombre": "—"}
    with c3:
        texto = st.text_input("Buscar por nombre",
                              placeholder=placeholder_nombre,
                              key=f"{key_prefix}_nombre")

    grado_id = grado_sel["id"] if grado_sel else None
    seccion_id = seccion_sel["id"] if (grado_sel and grado_sel["id"] and seccion_sel) else None
    return grado_id, seccion_id, texto.strip()

def buscar_alumnos_por_nombre(texto, grado_id=None, seccion_id=None, limite=200):
    conn = get_db()
    q = """
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               g.id AS grado_id, g.nombre AS grado, s.id AS seccion_id, s.nombre AS seccion,
               t.nombre AS turno,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') || ', ' || a.nombres AS nombre_completo
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE 1=1
    """
    params = []
    if texto:
        palabras = [p.strip() for p in texto.split() if p.strip()]
        for palabra in palabras:
            q += """ AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ?
                          OR a.apellido_materno LIKE ?
                          OR (a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') || ' ' || a.nombres) LIKE ?)"""
            like = f"%{palabra}%"
            params += [like, like, like, like]
    if grado_id:
        q += " AND g.id = ?"; params.append(grado_id)
    if seccion_id:
        q += " AND s.id = ?"; params.append(seccion_id)
    q += " ORDER BY a.apellido_paterno, a.apellido_materno, a.nombres LIMIT ?"
    params.append(limite)
    df = pd.read_sql(q, conn, params=params)
    conn.close()
    return df

def alumnos_de_seccion(seccion_id):
    conn = get_db()
    df = pd.read_sql("""
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') || ', ' || a.nombres AS nombre_completo
        FROM alumnos a WHERE a.seccion_id = ?
        ORDER BY a.apellido_paterno, a.apellido_materno, a.nombres
    """, conn, params=[seccion_id])
    conn.close()
    return df

def estado_asistencia_hoy(alumno_ids):
    if not alumno_ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(alumno_ids))
    rows = conn.execute(
        f"SELECT alumno_id, estado FROM asistencias WHERE fecha=? AND alumno_id IN ({placeholders})",
        [hoy_str()] + list(alumno_ids)
    ).fetchall()
    conn.close()
    return {r["alumno_id"]: r["estado"] for r in rows}

def exportar_excel_pdf(df, titulo, nombre_base, key_prefix="export"):
    if df.empty:
        return
    c1, c2 = st.columns(2)
    with c1:
        ex = BytesIO()
        with pd.ExcelWriter(ex, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name="Datos")
        st.download_button("⬇Excel", ex.getvalue(), f"{nombre_base}.xlsx",
                           key=f"{key_prefix}_xlsx", use_container_width=True)
    with c2:
        st.download_button("⬇PDF", pdf_tabla(df, titulo),
                           f"{nombre_base}.pdf", "application/pdf",
                           key=f"{key_prefix}_pdf", use_container_width=True)

# 5. QR / PDF

def qr_de_dni(dni):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(str(dni).strip())
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")

def leer_qr(img):
    try:
        import cv2
        arr = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        detector = cv2.QRCodeDetector()
        variantes = [gray]
        for escala in (1.5, 2.0, 3.0):
            variantes.append(cv2.resize(gray, None, fx=escala, fy=escala, interpolation=cv2.INTER_CUBIC))
        for base in list(variantes):
            th = cv2.adaptiveThreshold(base, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, 31, 5)
            variantes.append(th)
        for base in list(variantes[:4]):
            _, otsu = cv2.threshold(base, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            variantes.append(otsu)
        for v in variantes:
            data, _, _ = detector.detectAndDecode(v)
            if data:
                m = re.search(r"\b(\d{8})\b", data)
                return m.group(1) if m else data.strip()
    except Exception as e:
        st.error(f"Error leyendo QR: {e}")
    return None

def color_estado(v):
    if v == "Puntual":  return "background-color:#d4edda;color:#155724;font-weight:bold"
    if v == "Tardanza": return "background-color:#fff3cd;color:#856404;font-weight:bold"
    if v == "Falta":    return "background-color:#f8d7da;color:#721c24;font-weight:bold"
    return ""

def _pdf_base(titulo, subtitulo=None, paisaje=False):
    buf = BytesIO()
    size = A4 if not paisaje else (A4[1], A4[0])
    doc = SimpleDocTemplate(buf, pagesize=size, rightMargin=25, leftMargin=25, topMargin=25, bottomMargin=25)
    estilos = getSampleStyleSheet()
    el = []

    def _pdf_base(titulo, subtitulo=None, paisaje=False):
        buf = BytesIO()
        size = A4 if not paisaje else (A4[1], A4[0])
        doc = SimpleDocTemplate(buf, pagesize=size, rightMargin=25, leftMargin=25, topMargin=25, bottomMargin=25)
        estilos = getSampleStyleSheet()
        el = [Paragraph(f"<b>{titulo}</b>", estilos["Heading1"])]
        if subtitulo:
            el.append(Paragraph(subtitulo, estilos["Normal"]))
        el.append(Paragraph(f"Generado: {ahora().strftime('%Y-%m-%d %H:%M')}", estilos["Normal"]))
        el.append(Spacer(1, 15))
        return buf, doc, el, estilos

def pdf_tabla(df, titulo, subtitulo=None):
    buf, doc, el, estilos = _pdf_base(titulo, subtitulo, paisaje=len(df.columns) > 6)
    if not df.empty:
        data = [df.columns.tolist()] + df.values.tolist()
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("FONTSIZE", (0, 1), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        el.append(t)
    doc.build(el)
    buf.seek(0)
    return buf.getvalue()

def _render_pdf_carnets(rows):
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=10, leftMargin=10,
        topMargin=10, bottomMargin=10
    )
    estilos = getSampleStyleSheet()

    if len(rows) > 1:
        titulo = f"Carnets - {rows[0]['grado']}{rows[0]['seccion']} ({rows[0]['turno']})"
    else:
        titulo = f"Carnet - {rows[0]['apellido_paterno']} {rows[0]['apellido_materno'] or ''}, {rows[0]['nombres']}"

    el = [Paragraph(titulo, estilos["Heading1"]), Spacer(1, 10)]

    # ═══════════════════════════════════════════════════════════════
    # AHORA: 9 carnets por hoja (3 columnas × 3 filas)
    # ═══════════════════════════════════════════════════════════════
    CARNTS_POR_FILA = 3
    FILAS_POR_PAGINA = 3
    CARNTS_POR_PAGINA = CARNTS_POR_FILA * FILAS_POR_PAGINA  # = 9

    # Ancho y alto disponibles
    ancho_disponible = doc.width   # ≈ 575 puntos
    alto_disponible = doc.height   # ≈ 822 puntos

    # Reservamos espacio para el título en la primera página
    alto_util = alto_disponible - 40  # dejamos 40 puntos para el título

    ancho_carnet = ancho_disponible / CARNTS_POR_FILA  # ≈ 191 puntos
    alto_carnet = alto_util / FILAS_POR_PAGINA         # ≈ 260 puntos

    for i in range(0, len(rows), CARNTS_POR_PAGINA):
        lote = rows[i:i + CARNTS_POR_PAGINA]
        tabla = []

        for j in range(0, len(lote), CARNTS_POR_FILA):
            fila = []
            for a in lote[j:j + CARNTS_POR_FILA]:
                qb = BytesIO()
                qr_de_dni(a["dni"]).save(qb, format="PNG")
                qb.seek(0)

                celda = [
                    Paragraph(f"<b>{a['apellido_paterno']} {a['apellido_materno'] or ''}</b>",
                              estilos["Normal"]),
                    Paragraph(a["nombres"], estilos["Normal"]),
                    Paragraph(f"DNI: {a['dni']}", estilos["Normal"]),
                    Paragraph(f"{a['grado']}{a['seccion']} - {a['turno']}", estilos["Normal"]),
                    RLImage(qb, width=80, height=80),
                ]
                fila.append(celda)

            # Rellenar con celdas vacías si es la última fila incompleta
            while len(fila) < CARNTS_POR_FILA:
                fila.append([])
            tabla.append(fila)

        # Si la última página tiene menos de 3 filas, rellenar
        while len(tabla) < FILAS_POR_PAGINA:
            tabla.append([[] for _ in range(CARNTS_POR_FILA)])

        # Crear la tabla con las dimensiones calculadas
        col_widths = [ancho_carnet] * CARNTS_POR_FILA
        row_heights = [alto_carnet] * len(tabla)

        t = Table(tabla, colWidths=col_widths, rowHeights=row_heights)
        t.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOX", (0, 0), (-1, -1), 1, colors.black),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        el.append(t)

        # Salto de página entre bloques
        if i + CARNTS_POR_PAGINA < len(rows):
            el.append(PageBreak())

    doc.build(el)
    buf.seek(0)
    return buf.getvalue()
def pdf_carnets_por_seccion(seccion_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM alumnos a JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id JOIN turnos t ON s.turno_id = t.id
        WHERE a.seccion_id = ?
        ORDER BY a.apellido_paterno, a.apellido_materno
    """, (seccion_id,)).fetchall()
    conn.close()
    if not rows:
        return None
    return _render_pdf_carnets(rows)

def pdf_carnet_alumno(dni):
    conn = get_db()
    rows = conn.execute("""
        SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM alumnos a JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id JOIN turnos t ON s.turno_id = t.id
        WHERE a.dni = ?
    """, (dni,)).fetchall()
    conn.close()
    if not rows:
        return None
    return _render_pdf_carnets(rows)

def pdf_reporte_observados(df, titulo="Reporte de Observados"):
    buf, doc, el, estilos = _pdf_base(titulo, "I.E. Yarinacocha")
    if df.empty:
        el.append(Paragraph("Sin registros.", estilos["Normal"]))
    else:
        cols = ["DNI", "Apellidos", "Nombres", "Grado", "Sección", "Turno", "Ingreso", "Motivo"]
        data = [cols] + df[["dni", "apellidos", "nombres", "grado", "seccion", "turno",
                             "fecha_ingreso", "motivo"]].values.tolist()
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("FONTSIZE", (0, 1), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ]))
        el.append(t)
    doc.build(el)
    buf.seek(0)
    return buf.getvalue()

def _df_to_xlsx(df, sheet_name="Datos"):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=sheet_name)
    return buf.getvalue()

# 6. IMPORTACIÓN

def validar_importacion(df, mapeo):
    errores, validas, dnis_en_excel = [], [], {}
    conn = get_db()
    dnis_bd = {r["dni"] for r in conn.execute("SELECT dni FROM alumnos").fetchall()}
    conn.close()

    for i, row in df.iterrows():
        fila_num = i + 2
        try:
            dni = str(row[mapeo['dni']]).strip()
            nom = str(row[mapeo['nombres']]).strip()
            pat = str(row[mapeo['apellido_paterno']]).strip()
            mat = str(row[mapeo['apellido_materno']]).strip() if mapeo.get('apellido_materno') else ""
            gra = str(row[mapeo['grado']]).strip()
            sec = str(row[mapeo['seccion']]).strip()
            tur = str(row[mapeo['turno']]).strip().lower()
            apo_nom = str(row[mapeo['apoderado_nombre']]).strip() if mapeo.get('apoderado_nombre') else ""
            apo_tel = str(row[mapeo['apoderado_telefono']]).strip() if mapeo.get('apoderado_telefono') else ""

            if not dni:
                errores.append({"fila": fila_num, "motivo": "DNI vacío"}); continue
            if not re.fullmatch(r"\d{8}", dni):
                errores.append({"fila": fila_num, "motivo": f"DNI inválido '{dni}'"}); continue
            if dni in dnis_bd:
                errores.append({"fila": fila_num, "motivo": f"DNI {dni} ya existe"}); continue
            if dni in dnis_en_excel:
                errores.append({"fila": fila_num, "motivo": f"DNI {dni} duplicado"}); continue
            if not nom or not pat or not gra or not sec:
                errores.append({"fila": fila_num, "motivo": "Campos obligatorios vacíos"}); continue

            if tur in ("mañana", "manana", "m", "am", "mñ"):
                turno_nombre = "Mañana"
            elif tur in ("tarde", "t", "tm", "pm"):
                turno_nombre = "Tarde"
            else:
                errores.append({"fila": fila_num, "motivo": f"Turno inválido '{tur}'"}); continue

            dnis_en_excel[dni] = fila_num
            validas.append({"dni": dni, "nombres": nom, "apellido_paterno": pat,
                            "apellido_materno": mat, "grado": gra, "seccion": sec,
                            "turno": turno_nombre, "apoderado_nombre": apo_nom,
                            "apoderado_telefono": apo_tel, "fila": fila_num})
        except Exception as e:
            errores.append({"fila": fila_num, "motivo": f"Error: {e}"})

    return validas, errores, {"total": len(df), "validas": len(validas), "errores": len(errores)}

def insertar_validas(validas):
    conn = get_db()
    c = conn.cursor()
    turnos_map = {r["nombre"]: r["id"] for r in c.execute("SELECT id, nombre FROM turnos").fetchall()}
    insertados = 0
    for v in validas:
        try:
            g = c.execute("SELECT id FROM grados WHERE nombre=?", (v["grado"],)).fetchone()
            g_id = g["id"] if g else c.execute("INSERT INTO grados (nombre) VALUES (?)", (v["grado"],)).lastrowid
            t_id = turnos_map[v["turno"]]
            s = c.execute("SELECT id FROM secciones WHERE nombre=? AND grado_id=? AND turno_id=?",
                          (v["seccion"], g_id, t_id)).fetchone()
            s_id = s["id"] if s else c.execute(
                "INSERT INTO secciones (nombre, grado_id, turno_id) VALUES (?,?,?)",
                (v["seccion"], g_id, t_id)).lastrowid
            c.execute("""INSERT OR IGNORE INTO alumnos
                (dni, nombres, apellido_paterno, apellido_materno, seccion_id, nombre_apoderado, telefono_apoderado)
                VALUES (?,?,?,?,?,?,?)""",
                (v["dni"], v["nombres"], v["apellido_paterno"], v["apellido_materno"],
                 s_id, v["apoderado_nombre"] or None, v["apoderado_telefono"] or None))
            if c.rowcount > 0:
                insertados += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    st.cache_data.clear()
    return insertados

# 7. VISTAS

def vista_login():
    st.title("📚 Sistema de Asistencia - I.E. Yarinacocha")
    st.caption("Ingresa con tu usuario y contraseña")
    st.markdown("---")
    with st.form("login"):
        col1, col2, col3 = st.columns([1, 1.2, 1])
        with col2:
            u = st.text_input("Usuario")
            p = st.text_input("Contraseña", type="password")
            ok = st.form_submit_button("Ingresar", type="primary", use_container_width=True)
        if ok:
            user = autenticar(u, p)
            if user:
                st.session_state.user = user
                auditar(user["usuario"], "Login")
                st.rerun()
            else:
                st.error("Credenciales incorrectas")

# 7.2 Puerta 
@st.fragment
def _frag_lista_marcar(usuario, seccion_id):
    df = alumnos_de_seccion(seccion_id)
    if df.empty:
        st.info("No hay alumnos en esta sección.")
        return
    estados = estado_asistencia_hoy(df["id"].tolist())
    for _, al in df.iterrows():
        estado = estados.get(al["id"])
        c1, c2 = st.columns([5, 1])
        with c1:
            if estado == "Puntual":
                st.markdown(f"🟢 **{al['nombre_completo']}** — ✅ Puntual")
            elif estado == "Tardanza":
                st.markdown(f"🟡 **{al['nombre_completo']}** — 🟡 Tardanza")
            elif estado == "Falta":
                st.markdown(f"🔴 **{al['nombre_completo']}** — 🔴 Falta")
            else:
                st.markdown(f"⚪ **{al['nombre_completo']}** — sin registrar")
        with c2:
            if estado is None:
                if st.button("✅ Marcar", key=f"m_{al['id']}", use_container_width=True):
                    ok, tipo, msg, extra = registrar_entrada(al["dni"], usuario)
                    if ok:
                        st.success(msg)
                    else:
                        st.warning(msg)
                    st.rerun(scope="fragment")

@st.fragment
def _frag_escaner_qr(usuario):
    st.caption("Muestra el QR del alumno a la cámara")
    img_file = st.camera_input("Escanea el QR", key="camara_qr_fija")
    if img_file:
        dni = leer_qr(Image.open(BytesIO(img_file.getvalue())))
        if not dni:
            st.error("No se detectó QR. Prueba con mejor luz o más cerca.")
        else:
            _procesar_entrada(dni, usuario)

def _procesar_entrada(dni, usuario):
    ok, tipo, msg, extra = registrar_entrada(dni, usuario)
    if not ok:
        st.error(msg)
        return
    if tipo == "PUNTUAL":
        st.success(msg)
        st.balloons()
    elif tipo == "TARDANZA":
        accion = extra.get("accion")
        if accion == "PERDONADO":
            st.warning(msg)
        elif accion == "DERIVADO_TOECE":
            st.error(msg)
            st.info("Derivar al alumno al salón TOECE.")
        else:
            st.error(msg)
            st.info("Retener al alumno hasta que llegue su apoderado.")

def vista_puerta():
    st.title("Control de Puerta")
    usuario = st.session_state.user
    hoy = hoy_str()
    conn = get_db()
    esp = conn.execute("SELECT descripcion, hora_entrada, tipo FROM dias_especiales WHERE fecha=? AND activo=1 LIMIT 1",
                       (hoy,)).fetchone()
    conn.close()

    if esp and esp["tipo"] == "evento":
        st.success(f"Evento escolar: **{esp['descripcion']}** (entrada {esp['hora_entrada']})")
    elif esp and esp["tipo"] == "feriado":
        st.info(f"Feriado / sin clases: **{esp['descripcion']}** — No se toma asistencia.")
        return
    elif not es_dia_laboral():
        st.warning("Hoy no es día laboral. No se toma asistencia.")
        return
    else:
        st.info(f"Día laboral — {hoy}")

    marcar_faltas_al_cierre()

    m = metricas_dia(hoy)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("📚 Alumnos", m["total"])
    c2.metric("✅ Puntuales", m["puntuales"])
    c3.metric("🟡 Tardanzas", m["tardanzas"])
    c4.metric("🟠 Derivados TOECE", m["derivados"])
    c5.metric("🔴 Retenidos", m["retenidos"])
    st.markdown("---")
    modo = st.radio("Método", ["📷 Escanear QR", "Lista por sección"],
                    horizontal=True, key="modo_puerta")

    if modo == "📷 Escanear QR":
        _frag_escaner_qr(usuario)
    else:
        grados = grados_lista()
        if not grados:
            st.warning("No hay grados registrados.")
        else:
            c1, c2 = st.columns(2)
            with c1:
                grado_sel = st.selectbox("Grado", grados, format_func=lambda g: g["nombre"], key="pt_grado")
            with c2:
                secs = secciones_por_grado(grado_sel["id"]) if grado_sel else []
                if not secs:
                    st.warning("Ese grado no tiene secciones.")
                else:
                    seccion_sel = st.selectbox("Sección", secs, format_func=lambda s: s["nombre"], key="pt_seccion")
                    st.markdown(f"### Alumnos de {grado_sel['nombre']}{seccion_sel['nombre']}")
                    _frag_lista_marcar(usuario, seccion_sel["id"])

    st.markdown("---")
    st.subheader("Últimos registros del día")
    df = ultimos_registros(hoy)
    if df.empty:
        st.info("Sin registros aún.")
    else:
        st.dataframe(df.style.map(color_estado, subset=["estado"]), use_container_width=True)


#7.3 Faltas 
@st.fragment
def _frag_tabla_faltas(usuario, grado_id, seccion_id, texto, solo_inj):
    conn = get_db()
    q = """
        SELECT ast.id AS asist_id, a.id AS alumno_id, a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
               a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               ast.justificada, COALESCE(ast.observacion,'') AS observacion
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ast.fecha = ? AND ast.estado = 'Falta'
    """
    params = [hoy_str()]
    if usuario["rol"] == "Auxiliar" and usuario["turno_asignado"]:
        q += " AND s.turno_id = ?"; params.append(usuario["turno_asignado"])
    if grado_id:
        q += " AND g.id = ?"; params.append(grado_id)
    if seccion_id:
        q += " AND s.id = ?"; params.append(seccion_id)
    if texto:
        palabras = [p.strip() for p in texto.split() if p.strip()]
        for palabra in palabras:
            q += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
            like = f"%{palabra}%"
            params += [like, like, like]
    if solo_inj:
        q += " AND ast.justificada = 0"
    q += " ORDER BY t.nombre, g.nombre, s.nombre, a.apellido_paterno LIMIT 200"
    df = pd.read_sql(q, conn, params=params)
    conn.close()

    if df.empty:
        st.info("No hay faltas con esos filtros.")
        return

    st.write(f"**{len(df)} faltas encontradas**")
    for _, r in df.iterrows():
        estado_txt = "Justificada ✅" if r["justificada"] else "Injustificada ❌"
        with st.expander(f"**{r['apellidos']}, {r['nombres']}** — {r['grado']}{r['seccion']} ({r['turno']}) — {estado_txt}"):
            st.write(f"**DNI:** {r['dni']}")
            st.write(f"**Observación:** {r['observacion'] or '—'}")
            obs = st.text_area("Observación", value=r["observacion"], key=f"o_{r['asist_id']}")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ Justificar", key=f"j_{r['asist_id']}", use_container_width=True):
                    ok, msg = justificar_falta(r["alumno_id"], True, obs, usuario)
                    if ok:
                        st.success(msg)
                    else:
                        st.error(msg)
                    st.rerun(scope="fragment")
            with c2:
                if st.button("❌ Injustificar", key=f"i_{r['asist_id']}", use_container_width=True):
                    ok, msg = justificar_falta(r["alumno_id"], False, obs, usuario)
                    if ok:
                        st.success(msg)
                    else:
                        st.error(msg)
                    st.rerun(scope="fragment")

    df_export = df.drop(columns=["asist_id", "alumno_id"])
    exportar_excel_pdf(df_export, f"Faltas {hoy_str()}", f"faltas_{hoy_str()}", key_prefix="faltas")

def vista_faltas():
    st.title("Faltas del día")
    usuario = st.session_state.user
    st.caption("Aplica al menos un filtro para ver las faltas.")
    grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
        key_prefix="faltas", placeholder_nombre="Ej: Pérez"
    )
    solo_inj = st.checkbox("Solo injustificadas", key="faltas_solo_inj")

    hay_filtro = bool(grado_id) or bool(seccion_id) or bool(texto) or solo_inj
    st.markdown("---")

    if not hay_filtro:
        st.info("👆 Aplica al menos un filtro (grado, sección, nombre o 'solo injustificadas').")
    else:
        _frag_tabla_faltas(usuario, grado_id, seccion_id, texto, solo_inj)


# 7.4 TOECE 
def vista_toece():
    st.title("📋 TOECE — Derivados y Observados")
    usuario = st.session_state.user
    hoy = hoy_str()

    tab1, tab2, tab3 = st.tabs(["Derivados hoy", "Firmar Acta", "Observados"])

    with tab1:
        conn = get_db()
        df = pd.read_sql("""
            SELECT t.id, t.numero AS `N°`, a.dni,
                   a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
                   a.nombres, g.nombre AS grado, s.nombre AS seccion, t.hora, t.accion, t.observacion
            FROM tardanzas t
            JOIN alumnos a ON t.alumno_id = a.id
            JOIN secciones s ON a.seccion_id = s.id
            JOIN grados g ON s.grado_id = g.id
            WHERE t.fecha = ? AND t.accion IN ('DERIVADO_TOECE','RETENIDO_APODERADO')
            ORDER BY t.hora DESC
        """, conn, params=[hoy])
        conn.close()
        if df.empty:
            st.info("No hay derivados hoy.")
        else:
            st.dataframe(df, use_container_width=True)

    with tab2:
        st.subheader("Firmar acta de compromiso")
        conn = get_db()
        der = pd.read_sql("""
            SELECT t.id, a.id AS alumno_id, a.dni,
                   a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') || ', ' || a.nombres AS nombre_completo,
                   t.numero, t.accion
            FROM tardanzas t JOIN alumnos a ON t.alumno_id = a.id
            WHERE t.fecha = ? AND t.accion IN ('DERIVADO_TOECE','RETENIDO_APODERADO')
              AND a.id NOT IN (SELECT alumno_id FROM actas_compromiso WHERE fecha = ?)
        """, conn, params=[hoy, hoy])
        conn.close()
        if der.empty:
            st.info("No hay alumnos pendientes de acta hoy.")
        else:
            with st.form("acta"):
                opciones = {f"{r['nombre_completo']} (DNI {r['dni']}) — {r['accion']}": r["alumno_id"]
                            for _, r in der.iterrows()}
                sel = st.selectbox("Alumno", list(opciones.keys()))
                motivo = st.text_input("Motivo", value="Reincidencia en tardanzas")
                obs = st.text_area("Observaciones")
                ok = st.form_submit_button("Firmar acta", type="primary", use_container_width=True)
            if ok:
                registrar_acta(opciones[sel], motivo, obs, usuario)
                st.success("✅ Acta registrada.")
                st.cache_data.clear()
                st.rerun()

    with tab3:
        st.subheader("Alumnos observados")
        solo_activos = st.checkbox("Solo activos", value=True, key="obs_act")
        df = observados_dataframe(solo_activos=solo_activos)
        if df.empty:
            st.info("No hay observados con ese filtro.")
        else:
            df_show = df[["dni", "apellidos", "nombres", "grado", "seccion", "turno",
                          "fecha_ingreso", "motivo", "fecha_salida"]]
            st.dataframe(df_show, use_container_width=True)
            exportar_excel_pdf(df_show, "Reporte de Observados", "observados", key_prefix="obs")
            st.markdown("---")
            st.subheader("✅ Liberar de lista de observados")
            with st.form("liberar_obs"):
                op = {f"{r['apellidos']}, {r['nombres']} (DNI {r['dni']})": r["id"] for _, r in df.iterrows()}
                sel = st.selectbox("Alumno", list(op.keys()))
                liberar = st.form_submit_button("Liberar", type="primary", use_container_width=True)
            if liberar:
                conn = get_db()
                row = conn.execute("SELECT alumno_id FROM observados WHERE id=?", (op[sel],)).fetchone()
                conn.close()
                if row:
                    liberar_observado(row["alumno_id"], usuario)
                    st.success("Alumno liberado.")
                    st.cache_data.clear()
                    st.rerun()


#7.5 Panel Dirección 
def vista_panel_direccion():
    import plotly.express as px
    st.title("Panel Dirección")
    hoy = hoy_str()
    marcar_faltas_al_cierre()
    m = metricas_dia(hoy)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📚 Total alumnos", m["total"])
    c2.metric("✅ Puntuales hoy", m["puntuales"])
    c3.metric("🟡 Tardanzas hoy", m["tardanzas"])
    c4.metric("🔴 Faltas hoy", m["faltas"])
    c1, c2 = st.columns(2)
    c1.metric("🟠 Derivados TOECE", m["derivados"])
    c2.metric("🔴 Retenidos", m["retenidos"])

    st.markdown("---")
    st.subheader("Asistencia últimos 7 días")
    conn = get_db()
    df7 = pd.read_sql("""
        SELECT fecha,
               SUM(CASE WHEN estado='Puntual' THEN 1 ELSE 0 END) AS Puntual,
               SUM(CASE WHEN estado='Falta' THEN 1 ELSE 0 END) AS Falta
        FROM asistencias WHERE fecha >= date('now', '-7 days')
        GROUP BY fecha ORDER BY fecha
    """, conn)
    conn.close()
    if not df7.empty:
        df_long = df7.melt(id_vars="fecha", var_name="Estado", value_name="Cantidad")
        fig = px.bar(df_long, x="fecha", y="Cantidad", color="Estado",
                     color_discrete_map={"Puntual": "#28a745", "Falta": "#dc3545"},
                     barmode="stack", title="Asistencia diaria")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Sin datos de los últimos 7 días.")

    st.markdown("---")
    st.subheader("Top 5 con más tardanzas hoy")
    conn = get_db()
    df_top = pd.read_sql("""
        SELECT g.nombre || s.nombre AS salon, t.nombre AS turno, COUNT(*) AS tardanzas
        FROM tardanzas ta JOIN alumnos a ON ta.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ta.fecha = ? GROUP BY salon, turno ORDER BY tardanzas DESC LIMIT 5
    """, conn, params=[hoy])
    conn.close()
    if df_top.empty:
        st.info("Sin tardanzas hoy.")
    else:
        st.dataframe(df_top, use_container_width=True)


#7.6 Perfil Alumno
def vista_perfil_alumno():
    st.title("Perfil del alumno")

    dni = st.session_state.get("perfil_dni")
    if dni:
        _mostrar_perfil_completo(dni)
        return

    st.caption("Busca al alumno por nombre, grado o sección. Haz clic en él para ver su perfil.")
    grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
        key_prefix="perfil", placeholder_nombre="Ej: Quispe"
    )

    st.markdown("---")
    hay_filtro = bool(texto) or bool(grado_id) or bool(seccion_id)
    if not hay_filtro:
        st.info("Escribe un nombre o selecciona grado/sección.")
        df = buscar_alumnos_por_nombre("", None, None, limite=200)
    else:
        df = buscar_alumnos_por_nombre(texto, grado_id, seccion_id, limite=200)

    if df.empty:
        st.warning("No se encontraron alumnos.")
        return

    st.write(f"**{len(df)} alumnos** — haz clic para ver el perfil:")
    for _, al in df.iterrows():
        if st.button(f"👤 {al['nombre_completo']} — {al['grado']}{al['seccion']} ({al['turno']})",
                     key=f"pa_{al['id']}", use_container_width=True):
            st.session_state["perfil_dni"] = al["dni"]
            st.rerun()

def _mostrar_perfil_completo(dni):
    import plotly.express as px

    col1, col2 = st.columns([1, 5])
    with col1:
        if st.button("⬅Volver", type="primary", use_container_width=True):
            st.session_state.pop("perfil_dni", None)
            st.rerun()
    with col2:
        st.caption("← Volver a la búsqueda")

    st.markdown("---")
    conn = get_db()
    al = conn.execute("""
        SELECT a.*, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
        FROM alumnos a JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id JOIN turnos t ON s.turno_id = t.id
        WHERE a.dni=?
    """, (dni,)).fetchone()
    if not al:
        conn.close()
        st.warning("Alumno no encontrado.")
        return
    al = dict(al)

    df_asist = pd.read_sql("""SELECT fecha, estado, justificada, COALESCE(observacion,'') AS observacion
                              FROM asistencias WHERE alumno_id=? ORDER BY fecha DESC""", conn, params=[al["id"]])
    df_tard = pd.read_sql("""SELECT fecha, hora, numero AS `N°`, accion
                             FROM tardanzas WHERE alumno_id=? ORDER BY fecha DESC, hora DESC""", conn, params=[al["id"]])
    df_obs = pd.read_sql("""SELECT fecha_ingreso, COALESCE(fecha_salida,'—') AS fecha_salida,
                                   COALESCE(motivo,'') AS motivo, activo
                            FROM observados WHERE alumno_id=? ORDER BY fecha_ingreso DESC""", conn, params=[al["id"]])
    df_act = pd.read_sql("""SELECT fecha, COALESCE(motivo,'') AS motivo
                            FROM actas_compromiso WHERE alumno_id=? ORDER BY fecha DESC""", conn, params=[al["id"]])
    conn.close()

    nombre_completo = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}".strip(", ")
    st.markdown(f"###{nombre_completo}")
    c1, c2, c3 = st.columns(3)
    c1.write(f"**DNI:** {al['dni']}")
    c1.write(f"**Grado:** {al['grado']}{al['seccion']}")
    c2.write(f"**Turno:** {al['turno']}")
    c2.write(f"**Apoderado:** {al['nombre_apoderado'] or '—'}")
    c3.write(f"**Teléfono:** {al['telefono_apoderado'] or '—'}")
    if not df_obs.empty and any(r["activo"] == 1 for _, r in df_obs.iterrows()):
        c3.error("OBSERVADO")

    st.markdown("---")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Puntuales", (df_asist["estado"] == "Puntual").sum())
    m2.metric("Faltas", (df_asist["estado"] == "Falta").sum())
    m3.metric("Tardanzas", len(df_tard))
    m4.metric("Actas firmadas", len(df_act))

    st.markdown("---")
    st.subheader("Asistencia por mes")
    if not df_asist.empty:
        df_asist["fecha_dt"] = pd.to_datetime(df_asist["fecha"])
        df_asist["mes"] = df_asist["fecha_dt"].dt.to_period("M").astype(str)
        resumen = df_asist.groupby(["mes", "estado"]).size().reset_index(name="Cantidad")
        fig = px.bar(resumen, x="mes", y="Cantidad", color="estado",
                     color_discrete_map={"Puntual": "#28a745", "Falta": "#dc3545"},
                     barmode="stack", title="Asistencia mensual")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Sin registros.")

    st.markdown("---")
    st.subheader("Historial de tardanzas")
    if not df_tard.empty:
        st.dataframe(df_tard, use_container_width=True)
    else:
        st.info("Sin tardanzas.")

    st.markdown("---")
    st.subheader("Historial completo de asistencias")
    if df_asist.empty:
        st.info("Sin registros.")
    else:
        st.dataframe(df_asist[["fecha", "estado", "justificada", "observacion"]].style.map(
            color_estado, subset=["estado"]), use_container_width=True)


#7.7 REPORTES Y CONSULTAS
def vista_reportes():
    st.title("Reportes y Consultas")
    tab1, tab2, tab3 = st.tabs([" Detalle de asistencias",
                                 "Conteo de faltas",
                                 "Cierre mensual"])
    with tab1:
        _rep_detalle()
    with tab2:
        _rep_conteo()
    with tab3:
        _rep_cierre()

def _rep_detalle():
    import plotly.express as px

    c1, c2 = st.columns(2)
    with c1:
        periodo = st.selectbox("Período", ["Diario", "Semanal", "Mensual", "Bimestral"], key="rep_per")
    with c2:
        turno_opts = ["Todos"] + [t["nombre"] for t in turnos()]
        turno_sel = st.selectbox("Turno", turno_opts, key="rep_tur")

    grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
        key_prefix="rep_det", placeholder_nombre="Ej: Quispe"
    )

    dias = {"Diario": 0, "Semanal": 7, "Mensual": 30, "Bimestral": 60}[periodo]
    fin = ahora().date()
    ini = fin - timedelta(days=dias)
    st.caption(f"Rango: {ini} → {fin}")

    q = """
        SELECT a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
               a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               ast.fecha, ast.hora, ast.estado, ast.justificada
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ast.fecha BETWEEN ? AND ?
    """
    params = [ini.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")]
    if turno_sel != "Todos":
        q += " AND t.nombre = ?"; params.append(turno_sel)
    if grado_id:
        q += " AND g.id = ?"; params.append(grado_id)
    if seccion_id:
        q += " AND s.id = ?"; params.append(seccion_id)
    if texto:
        palabras = [p.strip() for p in texto.split() if p.strip()]
        for palabra in palabras:
            q += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
            like = f"%{palabra}%"
            params += [like, like, like]
    q += " ORDER BY ast.fecha DESC, t.nombre, g.nombre, s.nombre LIMIT 500"

    conn = get_db()
    df = pd.read_sql(q, conn, params=params)
    conn.close()

    if df.empty:
        st.warning("Sin datos para los filtros.")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Registros", len(df))
    m2.metric("Puntuales", (df["estado"] == "Puntual").sum())
    m3.metric("Faltas", (df["estado"] == "Falta").sum())
    m4.metric("Tardanzas", (df["estado"] == "Tardanza").sum())

    st.markdown("---")
    col_a, col_b = st.columns(2)
    with col_a:
        cnt = df["estado"].value_counts().reset_index()
        cnt.columns = ["Estado", "Cantidad"]
        fig = px.pie(cnt, names="Estado", values="Cantidad", title="Estados", hole=0.45,
                     color="Estado", color_discrete_map={"Puntual": "#28a745", "Falta": "#dc3545"})
        st.plotly_chart(fig, use_container_width=True)
    with col_b:
        por_dia = df.groupby(["fecha", "estado"]).size().reset_index(name="n")
        fig = px.bar(por_dia, x="fecha", y="n", color="estado", title="Asistencia por día",
                     color_discrete_map={"Puntual": "#28a745", "Falta": "#dc3545"}, barmode="stack")
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Detalle")
    st.dataframe(df.style.map(color_estado, subset=["estado"]), use_container_width=True)
    exportar_excel_pdf(df, f"Reporte {periodo}", f"reporte_{periodo}", key_prefix="rep_det")

def _rep_conteo():
    st.subheader("Conteo de faltas")
    c1, c2 = st.columns(2)
    with c1:
        dias = st.selectbox("Período", [30, 60, 90, 180],
                            format_func=lambda x: f"Últimos {x} días", key="con_dias")
    with c2:
        turno_opts = ["Todos"] + [t["nombre"] for t in turnos()]
        turno_sel = st.selectbox("Turno", turno_opts, key="con_tur")

    grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
        key_prefix="conteo", placeholder_nombre="Ej: Mamani"
    )
    solo_inj = st.checkbox("Solo faltas injustificadas", key="con_solo_inj")

    fin = ahora().date()
    ini = fin - timedelta(days=dias)

    q = """
        SELECT a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
               a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               SUM(CASE WHEN ast.justificada=1 THEN 1 ELSE 0 END) AS faltas_just,
               SUM(CASE WHEN ast.justificada=0 THEN 1 ELSE 0 END) AS faltas_injust,
               COUNT(*) AS total_faltas
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ast.estado='Falta' AND ast.fecha BETWEEN ? AND ?
    """
    params = [ini.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")]
    if turno_sel != "Todos":
        q += " AND t.nombre = ?"; params.append(turno_sel)
    if grado_id:
        q += " AND g.id = ?"; params.append(grado_id)
    if seccion_id:
        q += " AND s.id = ?"; params.append(seccion_id)
    if texto:
        palabras = [p.strip() for p in texto.split() if p.strip()]
        for palabra in palabras:
            q += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
            like = f"%{palabra}%"
            params += [like, like, like]
    if solo_inj:
        q += " AND ast.justificada = 0"
    q += " GROUP BY a.id ORDER BY total_faltas DESC LIMIT 500"

    conn = get_db()
    df = pd.read_sql(q, conn, params=params)
    conn.close()

    if df.empty:
        st.info("Sin faltas en el período.")
        return

    m1, m2, m3 = st.columns(3)
    m1.metric("Alumnos con faltas", len(df))
    m2.metric("Faltas justificadas", int(df["faltas_just"].sum()))
    m3.metric("Faltas injustificadas", int(df["faltas_injust"].sum()))
    st.dataframe(df, use_container_width=True)
    exportar_excel_pdf(df, f"Conteo de faltas últimos {dias} días", f"conteo_{dias}d", key_prefix="conteo")

def _rep_cierre():
    st.subheader("Cierre mensual de Asistencia")
    hoy_dt = ahora()
    c1, c2, c3 = st.columns(3)
    with c1:
        mes = st.selectbox("Mes", list(range(1, 13)), index=hoy_dt.month - 1,
                          format_func=lambda m: MESES_ES[m], key="cie_mes")
    with c2:
        anio = st.number_input("Año", min_value=2020, max_value=2100, value=hoy_dt.year, key="cie_anio")
    with c3:
        turno_opts = ["Todos"] + [t["nombre"] for t in turnos()]
        turno_sel = st.selectbox("Turno", turno_opts, key="cie_tur")

    grado_id, seccion_id, _ = filtros_grado_seccion_nombre(
        key_prefix="cierre", placeholder_nombre="(no aplica en cierre)", mostrar_todos=True
    )

    from calendar import monthrange
    ultimo_dia = monthrange(anio, mes)[1]
    inicio = f"{anio:04d}-{mes:02d}-01"
    fin = f"{anio:04d}-{mes:02d}-{ultimo_dia:02d}"
    st.caption(f"Rango: {inicio} → {fin}")

    if st.button("Generar reporte", type="primary"):
        q = """
            SELECT g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
                   a.dni,
                   a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
                   a.nombres,
                   SUM(CASE WHEN ast.estado='Puntual' THEN 1 ELSE 0 END) AS puntuales,
                   SUM(CASE WHEN ast.estado='Falta' AND ast.justificada=1 THEN 1 ELSE 0 END) AS faltas_just,
                   SUM(CASE WHEN ast.estado='Falta' AND ast.justificada=0 THEN 1 ELSE 0 END) AS faltas_injust,
                   COUNT(*) AS total_dias
            FROM asistencias ast
            JOIN alumnos a ON ast.alumno_id = a.id
            JOIN secciones s ON a.seccion_id = s.id
            JOIN grados g ON s.grado_id = g.id
            JOIN turnos t ON s.turno_id = t.id
            WHERE ast.fecha BETWEEN ? AND ?
        """
        params = [inicio, fin]
        if turno_sel != "Todos":
            q += " AND t.nombre = ?"; params.append(turno_sel)
        if grado_id:
            q += " AND g.id = ?"; params.append(grado_id)
        if seccion_id:
            q += " AND s.id = ?"; params.append(seccion_id)
        q += " GROUP BY g.nombre, s.nombre, t.nombre, a.id ORDER BY t.nombre, g.nombre, s.nombre, a.apellido_paterno"

        conn = get_db()
        df = pd.read_sql(q, conn, params=params)
        conn.close()

        if df.empty:
            st.warning("No hay datos para ese mes.")
        else:
            st.success(f"Reporte: {len(df)} alumnos")
            st.dataframe(df.head(50), use_container_width=True)
            st.download_button("Bajar Excel", _df_to_xlsx(df, f"Cierre_{MESES_ES[mes]}_{anio}"),
                               f"cierre_{anio}_{mes:02d}.xlsx")


#7.10 Días especiales 
def vista_dias_especiales():
    st.title("Días especiales")
    tab1, tab2 = st.tabs(["Crear", "Listar / eliminar"])

    with tab1:
        with st.form("dia_esp"):
            col1, col2 = st.columns(2)
            with col1:
                f = st.date_input("Fecha", min_value=datetime.now().date())
                desc = st.text_input("Descripción", placeholder="Aniversario, feriado, etc.")
                tipo = st.radio("Tipo de día",
                                ["Evento escolar (con clases)", "Feriado / sin clases"],
                                help="Evento: cuenta como día de clases. Feriado: NO cuenta.")
            with col2:
                t_opts = {"Ambos turnos": None}
                t_opts.update({t["nombre"]: t["id"] for t in turnos()})
                turno_lbl = st.selectbox("Aplica a", list(t_opts.keys()))
                h = st.time_input("Hora de entrada", value=datetime.strptime("08:00", "%H:%M").time())
            ok = st.form_submit_button("Crear", type="primary", use_container_width=True)
        if ok:
            tipo_val = "evento" if "Evento" in tipo else "feriado"
            conn = get_db()
            try:
                conn.execute("""INSERT OR REPLACE INTO dias_especiales
                                (fecha, descripcion, turno_id, hora_entrada, activo, tipo)
                                VALUES (?,?,?,?,1,?)""",
                             (f.strftime("%Y-%m-%d"), desc, t_opts[turno_lbl],
                              h.strftime("%H:%M"), tipo_val))
                conn.commit()
                st.success(f"Día especial creado como {tipo_val}.")
                st.cache_data.clear()
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")
            conn.close()

    with tab2:
        conn = get_db()
        df = pd.read_sql("""
            SELECT d.id, d.fecha, d.descripcion, COALESCE(t.nombre,'Ambos') AS turno,
                   d.hora_entrada, COALESCE(d.tipo,'evento') AS tipo
            FROM dias_especiales d LEFT JOIN turnos t ON d.turno_id = t.id
            WHERE d.fecha >= date('now') ORDER BY d.fecha
        """, conn)
        conn.close()
        if df.empty:
            st.info("Sin días especiales programados.")
        else:
            st.dataframe(df, use_container_width=True)
            op = {f"{r['fecha']} — {r['descripcion']} ({r['turno']}, {r['tipo']})": r["id"]
                  for _, r in df.iterrows()}
            sel = st.selectbox("Eliminar", list(op.keys()))
            if st.button("Eliminar", type="primary"):
                conn = get_db()
                conn.execute("DELETE FROM dias_especiales WHERE id=?", (op[sel],))
                conn.commit()
                conn.close()
                st.success("Eliminado.")
                st.cache_data.clear()
                st.rerun()

#  7.11 Alumnos 
def vista_alumnos():
    st.title("Alumnos")
    tab1, tab2, tab3, tab4 = st.tabs(["Importar Excel", "Listar", "Editar", "Eliminar"])

    with tab1:
        st.info("Columnas: DNI, Nombres, Apellido Paterno, Apellido Materno, Grado, Sección, Turno.")
        f = st.file_uploader("Sube el Excel", type=["xlsx", "xls"])
        if f:
            df = pd.read_excel(f)
            st.write(f"**{len(df)} filas** detectadas.")
            with st.form("mapeo"):
                cols = list(df.columns)
                c1, c2 = st.columns(2)
                with c1:
                    m_dni = st.selectbox("DNI *", cols)
                    m_nom = st.selectbox("Nombres *", cols)
                    m_pat = st.selectbox("Apellido Paterno *", cols)
                with c2:
                    m_mat = st.selectbox("Apellido Materno", [""] + cols)
                    m_gra = st.selectbox("Grado *", cols)
                    m_sec = st.selectbox("Sección *", cols)
                    m_tur = st.selectbox("Turno *", cols)
                c3, c4 = st.columns(2)
                with c3:
                    m_apo_nom = st.selectbox("Nombre Apoderado", [""] + cols)
                with c4:
                    m_apo_tel = st.selectbox("Teléfono Apoderado", [""] + cols)
                validar = st.form_submit_button("🔍 Validar", type="primary", use_container_width=True)
            if validar:
                mapeo = {"dni": m_dni, "nombres": m_nom, "apellido_paterno": m_pat,
                         "apellido_materno": m_mat, "grado": m_gra, "seccion": m_sec,
                         "turno": m_tur, "apoderado_nombre": m_apo_nom,
                         "apoderado_telefono": m_apo_tel}
                validas, errores, stats = validar_importacion(df, mapeo)
                st.session_state["_imp_validas"] = validas
                st.session_state["_imp_errores"] = errores
                st.session_state["_imp_stats"] = stats

            if "_imp_stats" in st.session_state:
                stats = st.session_state["_imp_stats"]
                c1, c2, c3 = st.columns(3)
                c1.metric("Total", stats["total"])
                c2.metric("✅ Válidas", stats["validas"])
                c3.metric("❌ Errores", stats["errores"])
                if st.session_state["_imp_errores"]:
                    with st.expander("⚠️ Errores"):
                        st.dataframe(pd.DataFrame(st.session_state["_imp_errores"]), use_container_width=True)
                if st.session_state["_imp_validas"]:
                    if st.button("✅ Importar SOLO válidas", type="primary", use_container_width=True):
                        n = insertar_validas(st.session_state["_imp_validas"])
                        st.success(f"✅ {n} alumnos importados.")
                        for k in ["_imp_validas", "_imp_errores", "_imp_stats"]:
                            st.session_state.pop(k, None)
                        st.rerun()

    with tab2:
        grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
            key_prefix="al_list", placeholder_nombre="Ej: Torres"
        )
        conn = get_db()
        q = """
            SELECT a.dni, a.apellido_paterno, a.apellido_materno, a.nombres,
                   g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
                   COALESCE(a.nombre_apoderado,'—') AS apoderado,
                   COALESCE(a.telefono_apoderado,'—') AS telefono
            FROM alumnos a
            JOIN secciones s ON a.seccion_id = s.id
            JOIN grados g ON s.grado_id = g.id
            JOIN turnos t ON s.turno_id = t.id
            WHERE 1=1
        """
        params = []
        if grado_id:
            q += " AND g.id = ?"; params.append(grado_id)
        if seccion_id:
            q += " AND s.id = ?"; params.append(seccion_id)
        if texto:
            palabras = [p.strip() for p in texto.split() if p.strip()]
            for palabra in palabras:
                q += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
                like = f"%{palabra}%"
                params += [like, like, like]
        q += " ORDER BY g.nombre, s.nombre, a.apellido_paterno LIMIT 3000"
        df = pd.read_sql(q, conn, params=params)
        conn.close()
        st.write(f"**{len(df)} alumnos**")
        st.dataframe(df, use_container_width=True)

    with tab3:
        grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
            key_prefix="al_edit", placeholder_nombre="Ej: Flores"
        )
        if texto or grado_id:
            df = buscar_alumnos_por_nombre(texto, grado_id, seccion_id, limite=50)
            for _, al in df.iterrows():
                if st.button(f"✏️ {al['nombre_completo']} — {al['grado']}{al['seccion']}",
                             key=f"e_{al['id']}", use_container_width=True):
                    st.session_state["editar_dni"] = al["dni"]
                    st.rerun()

        dni_edit = st.session_state.get("editar_dni")
        if dni_edit:
            st.markdown("---")
            conn = get_db()
            al = pd.read_sql("""SELECT a.*, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
                                FROM alumnos a JOIN secciones s ON a.seccion_id = s.id
                                JOIN grados g ON s.grado_id = g.id JOIN turnos t ON s.turno_id = t.id
                                WHERE a.dni=?""", conn, params=[dni_edit])
            conn.close()
            if al.empty:
                st.warning("No encontrado.")
                st.session_state.pop("editar_dni", None)
            else:
                al = al.iloc[0]
                st.markdown(f"### Editando: {al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}")
                with st.form("edit_al"):
                    n = st.text_input("Nombres", value=al["nombres"])
                    p = st.text_input("Apellido Paterno", value=al["apellido_paterno"])
                    m = st.text_input("Apellido Materno", value=al["apellido_materno"] or "")
                    apo_nom = st.text_input("Nombre Apoderado", value=al["nombre_apoderado"] or "")
                    apo_tel = st.text_input("Teléfono Apoderado", value=al["telefono_apoderado"] or "")
                    c1, c2 = st.columns(2)
                    with c1:
                        guardar = st.form_submit_button("💾 Guardar", type="primary", use_container_width=True)
                    with c2:
                        cancelar = st.form_submit_button("❌ Cancelar", use_container_width=True)
                if guardar:
                    conn = get_db()
                    conn.execute("""UPDATE alumnos SET nombres=?, apellido_paterno=?, apellido_materno=?,
                                    nombre_apoderado=?, telefono_apoderado=? WHERE id=?""",
                                 (n, p, m, apo_nom, apo_tel, al["id"]))
                    conn.commit()
                    conn.close()
                    auditar(st.session_state.user["usuario"], f"Editó alumno {dni_edit}")
                    st.success("Actualizado.")
                    st.cache_data.clear()
                    st.session_state.pop("editar_dni", None)
                    st.rerun()
                if cancelar:
                    st.session_state.pop("editar_dni", None)
                    st.rerun()

    with tab4:
        st.subheader("Eliminar alumno")
        st.warning("⚠️ No se puede deshacer")
        grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
            key_prefix="al_del", placeholder_nombre="Ej: Rojas"
        )
        if texto or grado_id:
            df = buscar_alumnos_por_nombre(texto, grado_id, seccion_id, limite=50)
            if df.empty:
                st.info("Sin coincidencias.")
            else:
                for _, al in df.iterrows():
                    if st.button(f"{al['nombre_completo']} — {al['grado']}{al['seccion']}",
                                 key=f"d_{al['id']}", use_container_width=True):
                        st.session_state["eliminar_alumno"] = {
                            "id": al["id"], "nombre": al["nombre_completo"], "dni": al["dni"]
                        }
                        st.rerun()

        eliminar = st.session_state.get("eliminar_alumno")
        if eliminar:
            st.markdown("---")
            st.error(f"¿Está seguro que desea eliminar a **{eliminar['nombre']}** (DNI {eliminar['dni']})?")
            st.warning("Esta acción **no se puede deshacer**.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("SÍ, ELIMINAR", type="primary", use_container_width=True):
                    conn = get_db()
                    try:
                        conn.execute("DELETE FROM asistencias WHERE alumno_id=?", (eliminar["id"],))
                        conn.execute("DELETE FROM tardanzas WHERE alumno_id=?", (eliminar["id"],))
                        conn.execute("DELETE FROM actas_compromiso WHERE alumno_id=?", (eliminar["id"],))
                        conn.execute("DELETE FROM observados WHERE alumno_id=?", (eliminar["id"],))
                        conn.execute("DELETE FROM alumnos WHERE id=?", (eliminar["id"],))
                        conn.commit()
                        auditar(st.session_state.user["usuario"], f"Eliminó alumno DNI {eliminar['dni']}")
                        st.success("✅ Alumno eliminado.")
                        st.cache_data.clear()
                        st.session_state.pop("eliminar_alumno", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")
                    finally:
                        conn.close()
            with c2:
                if st.button("❌ Cancelar", use_container_width=True):
                    st.session_state.pop("eliminar_alumno", None)
                    st.rerun()


#7.12 Carnets
def vista_carnets():
    st.title("Carnets")
    modo = st.radio("Modo", ["Por sección", "Por alumno"], horizontal=True)

    if modo == "Por sección":
        grados = grados_lista()
        c1, c2 = st.columns(2)
        with c1:
            grado_sel = st.selectbox("Grado", grados, format_func=lambda g: g["nombre"], key="carn_g")
        with c2:
            secs = secciones_por_grado(grado_sel["id"]) if grado_sel else []
            if not secs:
                st.warning("Sin secciones.")
            else:
                seccion_sel = st.selectbox("Sección", secs, format_func=lambda s: s["nombre"], key="carn_s")
                if st.button("Generar PDF de la sección", type="primary"):
                    pdf = pdf_carnets_por_seccion(seccion_sel["id"])
                    if pdf:
                        st.download_button("⬇ Descargar", pdf,
                                           f"carnets_{grado_sel['nombre']}{seccion_sel['nombre']}.pdf",
                                           "application/pdf")
    else:
        st.caption("Busca al alumno que perdió su carnet.")
        grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
            key_prefix="carn_al", placeholder_nombre="Ej: Vargas"
        )
        if texto or grado_id:
            df = buscar_alumnos_por_nombre(texto, grado_id, seccion_id, limite=50)
            for _, al in df.iterrows():
                if st.button(f"{al['nombre_completo']} — {al['grado']}{al['seccion']}",
                             key=f"c_{al['id']}", use_container_width=True):
                    st.session_state["carnet_dni"] = al["dni"]
        if "carnet_dni" in st.session_state:
            st.markdown("---")
            pdf = pdf_carnet_alumno(st.session_state["carnet_dni"])
            if pdf:
                st.download_button("⬇Descargar carnet individual", pdf,
                                   f"carnet_{st.session_state['carnet_dni']}.pdf",
                                   "application/pdf")


# 7.13 Usuarios 
def vista_usuarios():
    st.title("Usuarios")
    tab1, tab2 = st.tabs(["Listar", "Crear"])

    with tab1:
        conn = get_db()
        df = pd.read_sql("""SELECT u.id, u.usuario, u.rol, u.nombres,
                                   COALESCE(t.nombre,'—') AS turno, u.activo
                            FROM usuarios u LEFT JOIN turnos t ON u.turno_asignado = t.id
                            ORDER BY u.usuario""", conn)
        conn.close()
        st.dataframe(df, use_container_width=True)

    with tab2:
        with st.form("nuevo_user"):
            c1, c2 = st.columns(2)
            with c1:
                u = st.text_input("Usuario*")
                p = st.text_input("Password* (mín 6)", type="password")
                n = st.text_input("Nombres*")
            with c2:
                rol = st.selectbox("Rol", ["Admin", "TOECE", "Auxiliar", "Direccion"])
                turno_sel = None
                if rol == "Auxiliar":
                    t_opts = {t["nombre"]: t["id"] for t in turnos()}
                    turno_lbl = st.selectbox("Turno", list(t_opts.keys()))
                    turno_sel = t_opts[turno_lbl]
            ok = st.form_submit_button("Crear", type="primary", use_container_width=True)
        if ok:
            if not (u and p and n):
                st.error("Completa todos los campos.")
            elif len(p) < 6:
                st.error("Password muy corta.")
            else:
                conn = get_db()
                try:
                    conn.execute("""INSERT INTO usuarios (usuario, password, rol, nombres, turno_asignado)
                                    VALUES (?,?,?,?,?)""",
                                 (u, hashlib.sha256(p.encode()).hexdigest(), rol, n, turno_sel))
                    conn.commit()
                    auditar(st.session_state.user["usuario"], f"Creó usuario {u}")
                    st.success(f"Usuario {u} creado.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Ese usuario ya existe.")
                finally:
                    conn.close()

# 7.14 Horarios 
def vista_horarios():
    st.title("Horarios de turno")
    conn = get_db()
    for t in turnos():
        st.subheader(f"{t['nombre']}")
        with st.form(f"hor_{t['id']}"):
            c1, c2 = st.columns(2)
            with c1:
                h = st.text_input("Hora entrada (HH:MM)", value=t["hora_entrada"])
            with c2:
                tol = st.number_input("Tolerancia (min)", min_value=0, max_value=60, value=t["tolerancia_min"])
            ok = st.form_submit_button("Guardar", type="primary")
        if ok:
            conn.execute("UPDATE turnos SET hora_entrada=?, tolerancia_min=? WHERE id=?", (h, tol, t["id"]))
            conn.commit()
            st.cache_data.clear()
            st.success("Actualizado.")
            st.rerun()
    conn.close()


# 7.15 Auditoría 
def vista_auditoria():
    st.title("Auditoría")
    conn = get_db()
    df = pd.read_sql("SELECT * FROM auditoria ORDER BY id DESC LIMIT 200", conn)
    conn.close()
    st.write(f"{len(df)} registros (últimos 200)")
    st.dataframe(df, use_container_width=True)


# 8. MAIN
def menu_lateral():
    user = st.session_state.user
    rol = user["rol"]

    with st.sidebar:
        st.markdown(f"### {user['nombres']}")
        st.caption(f"Rol: **{rol}**")
        if user["turno_asignado"]:
            turno = next((t["nombre"] for t in turnos() if t["id"] == user["turno_asignado"]), "—")
            st.caption(f"Turno: **{turno}**")
        st.markdown("---")

        opciones_por_rol = {
            "Admin": ["🚪 Puerta", "🚫 Faltas", "📋 TOECE", "🏛️ Panel Dirección",
                      "📊 Reportes y Consultas", "🔍 Perfil Alumno",
                      "👥 Alumnos", "🪪 Carnets", "📅 Días especiales",
                      "⏰ Horarios", "👤 Usuarios", "📋 Auditoría"],
            "TOECE": ["🚫 Faltas", "📋 TOECE", "🏛️ Panel Dirección",
                      "📊 Reportes y Consultas", "🔍 Perfil Alumno",
                      "👥 Alumnos", "📅 Días especiales", "⏰ Horarios"],
            "Auxiliar": ["🚪 Puerta", "🚫 Faltas", "🔍 Perfil Alumno"],
            "Direccion": ["🏛️ Panel Dirección", "📊 Reportes y Consultas",
                          "🔍 Perfil Alumno", "👥 Alumnos", "📅 Días especiales"],
        }
        opciones = opciones_por_rol.get(rol, [])

        if "menu" not in st.session_state or st.session_state.menu not in opciones:
            st.session_state.menu = opciones[0]
        opcion = st.radio("Menú", opciones, key="menu")

        st.markdown("---")
        if st.button("🚪 Cerrar sesión", use_container_width=True):
            auditar(user["usuario"], "Logout")
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()

    return opcion

def main():
    if "user" not in st.session_state:
        vista_login()
        return
    opcion = menu_lateral()
    vistas = {
        "🚪 Puerta":              vista_puerta,
        "🚫 Faltas":              vista_faltas,
        "📋 TOECE":               vista_toece,
        "🏛️ Panel Dirección":    vista_panel_direccion,
        "📊 Reportes y Consultas": vista_reportes,
        "🔍 Perfil Alumno":       vista_perfil_alumno,
        "👥 Alumnos":             vista_alumnos,
        "🪪 Carnets":             vista_carnets,
        "📅 Días especiales":     vista_dias_especiales,
        "⏰ Horarios":            vista_horarios,
        "👤 Usuarios":            vista_usuarios,
        "📋 Auditoría":           vista_auditoria,
    }
    vistas.get(opcion, lambda: st.warning("Vista no disponible"))()
if __name__ == "__main__":
    main()
