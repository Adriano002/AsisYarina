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
import time

st.set_page_config(page_title="Asistencia I.E. Yarinacocha", page_icon="escudo.png", layout="wide")

st.markdown("""
<style>
    .stButton > button {
        background: #E65100 !important;
        color: #FFFFFF !important;
        border-radius: 10px;
        font-weight: 600;
        border: none;
    }
    .stButton > button:hover {
        background: #BF360C !important;
        color: #FFFFFF !important;
    }
    .stButton > button[kind="primary"] {
        background: #E65100 !important;
        color: #FFFFFF !important;
    }
    .stFormSubmitButton > button {
        background: #E65100 !important;
        color: #FFFFFF !important;
        border-radius: 10px;
        font-weight: 600;
        border: none;
    }
    .stFormSubmitButton > button:hover {
        background: #BF360C !important;
        color: #FFFFFF !important;
    }
    .stDownloadButton > button {
        background: #E65100 !important;
        color: #FFFFFF !important;
        border-radius: 10px;
        font-weight: 600;
        border: none;
    }
    .stDownloadButton > button:hover {
        background: #BF360C !important;
        color: #FFFFFF !important;
    }
</style>
""", unsafe_allow_html=True)

DB_PATH = "asistencia.db"

def ahora():
    return datetime.now(timezone.utc) - timedelta(hours=5)

def hoy_str():
    return ahora().strftime("%Y-%m-%d")

def hora_str():
    return ahora().strftime("%H:%M:%S")

def hora_corta():
    return ahora().strftime("%H:%M")

def suma_min(hhmm_, mins):
    t = datetime.strptime(hhmm_, "%H:%M") + timedelta(minutes=mins)
    return t.strftime("%H:%M")

def es_dia_laboral(fecha=None):
    fecha = fecha or ahora()
    return fecha.weekday() < 5

MESES_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

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
        hora_entrada TEXT, hora_salida TEXT, tolerancia_min INTEGER DEFAULT 7
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
        fecha TEXT NOT NULL,
        hora TEXT, estado TEXT,
        justificada INTEGER DEFAULT 0, observacion TEXT,
        hora_reforzamiento TEXT,
        estado_reforzamiento TEXT,
        justificada_reforzamiento INTEGER DEFAULT 0,
        observacion_reforzamiento TEXT,
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
        turno_id INTEGER, hora_entrada TEXT,
        activo INTEGER DEFAULT 1, tipo TEXT DEFAULT 'evento',
        tipo_reforzamiento TEXT,
        hora_salida_reforzamiento TEXT,
        tolerancia_reforzamiento INTEGER DEFAULT 7,
        FOREIGN KEY (turno_id) REFERENCES turnos(id),
        UNIQUE(fecha, turno_id)
    );
    CREATE TABLE IF NOT EXISTS dias_especiales_secciones (
        id INTEGER PRIMARY KEY,
        dia_especial_id INTEGER NOT NULL,
        seccion_id INTEGER NOT NULL,
        FOREIGN KEY (dia_especial_id) REFERENCES dias_especiales(id) ON DELETE CASCADE,
        FOREIGN KEY (seccion_id) REFERENCES secciones(id),
        UNIQUE(dia_especial_id, seccion_id)
    );
    CREATE TABLE IF NOT EXISTS reforzamiento_alumnos (
        id INTEGER PRIMARY KEY,
        dia_especial_id INTEGER NOT NULL,
        alumno_id INTEGER NOT NULL,
        FOREIGN KEY (dia_especial_id) REFERENCES dias_especiales(id) ON DELETE CASCADE,
        FOREIGN KEY (alumno_id) REFERENCES alumnos(id),
        UNIQUE(dia_especial_id, alumno_id)
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
    CREATE INDEX IF NOT EXISTS idx_des_secciones ON dias_especiales_secciones(dia_especial_id);
    CREATE INDEX IF NOT EXISTS idx_ref_alumnos ON reforzamiento_alumnos(dia_especial_id);
    """)

    cols_turnos = [r["name"] for r in c.execute("PRAGMA table_info(turnos)").fetchall()]
    if "hora_salida" not in cols_turnos:
        c.execute("ALTER TABLE turnos ADD COLUMN hora_salida TEXT")

    cols_asist = [r["name"] for r in c.execute("PRAGMA table_info(asistencias)").fetchall()]
    if "hora_reforzamiento" not in cols_asist:
        c.execute("ALTER TABLE asistencias ADD COLUMN hora_reforzamiento TEXT")
    if "estado_reforzamiento" not in cols_asist:
        c.execute("ALTER TABLE asistencias ADD COLUMN estado_reforzamiento TEXT")
    if "justificada_reforzamiento" not in cols_asist:
        c.execute("ALTER TABLE asistencias ADD COLUMN justificada_reforzamiento INTEGER DEFAULT 0")
    if "observacion_reforzamiento" not in cols_asist:
        c.execute("ALTER TABLE asistencias ADD COLUMN observacion_reforzamiento TEXT")

    cols_de = [r["name"] for r in c.execute("PRAGMA table_info(dias_especiales)").fetchall()]
    if "tipo" not in cols_de:
        c.execute("ALTER TABLE dias_especiales ADD COLUMN tipo TEXT DEFAULT 'evento'")
    if "tipo_reforzamiento" not in cols_de:
        c.execute("ALTER TABLE dias_especiales ADD COLUMN tipo_reforzamiento TEXT")
    if "hora_salida_reforzamiento" not in cols_de:
        c.execute("ALTER TABLE dias_especiales ADD COLUMN hora_salida_reforzamiento TEXT")
    if "tolerancia_reforzamiento" not in cols_de:
        c.execute("ALTER TABLE dias_especiales ADD COLUMN tolerancia_reforzamiento INTEGER DEFAULT 7")

    cols_al = [r["name"] for r in c.execute("PRAGMA table_info(alumnos)").fetchall()]
    if "nombre_apoderado" not in cols_al:
        c.execute("ALTER TABLE alumnos ADD COLUMN nombre_apoderado TEXT")
    if "telefono_apoderado" not in cols_al:
        c.execute("ALTER TABLE alumnos ADD COLUMN telefono_apoderado TEXT")

    c.execute("UPDATE turnos SET hora_salida='12:20' WHERE nombre='Mañana' AND (hora_salida IS NULL OR hora_salida='')")
    c.execute("UPDATE turnos SET hora_salida='17:00' WHERE nombre='Tarde' AND (hora_salida IS NULL OR hora_salida='')")

    if c.execute("SELECT COUNT(*) FROM turnos").fetchone()[0] == 0:
        c.execute("INSERT INTO turnos (nombre, hora_entrada, hora_salida, tolerancia_min) VALUES ('Mañana', '06:45', '12:20', 7)")
        c.execute("INSERT INTO turnos (nombre, hora_entrada, hora_salida, tolerancia_min) VALUES ('Tarde', '12:20', '17:00', 7)")

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

def alumno_en_reforzamiento(dia_especial_id, alumno_id):
    conn = get_db()
    r = conn.execute(
        "SELECT id FROM reforzamiento_alumnos WHERE dia_especial_id=? AND alumno_id=?",
        (dia_especial_id, alumno_id)
    ).fetchone()
    conn.close()
    return r is not None

def horario_del_dia(turno_id, fecha=None, seccion_id=None):
    fecha = fecha or hoy_str()
    conn = get_db()
    
    refuerzo = None
    if seccion_id:
        ref_row = conn.execute("""
            SELECT d.id, d.tipo_reforzamiento, d.hora_entrada AS hora_reforzamiento,
                   d.hora_salida_reforzamiento, d.tolerancia_reforzamiento, d.descripcion
            FROM dias_especiales d
            JOIN dias_especiales_secciones ds ON d.id = ds.dia_especial_id
            WHERE d.fecha=? AND d.activo=1 AND d.tipo='evento'
              AND d.tipo_reforzamiento IS NOT NULL AND ds.seccion_id=?
            LIMIT 1
        """, (fecha, seccion_id)).fetchone()
        if ref_row:
            refuerzo = dict(ref_row)
    
    esp_general = conn.execute(
        "SELECT hora_entrada FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento' "
        "AND tipo_reforzamiento IS NULL AND (turno_id=? OR turno_id IS NULL) ORDER BY turno_id DESC LIMIT 1",
        (fecha, turno_id)
    ).fetchone()
    
    t = conn.execute("SELECT hora_entrada, hora_salida, tolerancia_min FROM turnos WHERE id=?", (turno_id,)).fetchone()
    conn.close()
    
    if not t:
        return {
            "hora_entrada": "08:00", "hora_limite": "08:07", "hora_salida": "13:00",
            "especial": False, "reforzamiento": None,
        }
    
    horario_normal = {
        "hora_entrada": t["hora_entrada"],
        "hora_limite": suma_min(t["hora_entrada"], t["tolerancia_min"]),
        "hora_salida": t["hora_salida"] or "17:00",
    }
    
    if refuerzo:
        return {**horario_normal, "reforzamiento": refuerzo, "especial": True}
    
    if esp_general:
        entrada = esp_general["hora_entrada"]
        return {
            "hora_entrada": entrada, "hora_limite": suma_min(entrada, 7),
            "hora_salida": horario_normal["hora_salida"],
            "especial": True, "reforzamiento": None,
        }
    
    return {**horario_normal, "especial": False, "reforzamiento": None}

def detectar_tipo_escaneo(h, hora_actual_corta, alumno_id=None):
    """
    Determina si el escaneo es de clases o reforzamiento.
    - Reforzamiento solo si el alumno está asignado y está en su ventana.
    - Clases: se acepta desde cualquier hora temprana hasta hora_salida.
    """
    hora_entrada = h["hora_entrada"]
    hora_salida = h["hora_salida"]
    refuerzo = h.get("reforzamiento")
    
    # 1) Si hay reforzamiento y está en su ventana
    if refuerzo and alumno_id:
        hora_inicio_ref = refuerzo.get("hora_reforzamiento")
        hora_fin_ref = refuerzo.get("hora_salida_reforzamiento") or "23:59"
        if hora_inicio_ref <= hora_actual_corta <= hora_fin_ref:
            if alumno_en_reforzamiento(refuerzo["id"], alumno_id):
                return "reforzamiento"
            else:
                # No asignado, pero ¿está dentro de clases?
                if hora_actual_corta <= hora_salida:
                    return "clases"
                return "fuera"
    
    # 2) Clases: desde cualquier hora temprana hasta hora_salida
    if hora_actual_corta <= hora_salida:
        return "clases"
    
    # 3) Fuera
    return "fuera"

def calcular_estado_refuerzo(refuerzo, hora_actual_corta):
    hora_inicio = refuerzo.get("hora_reforzamiento")
    tolerancia = refuerzo.get("tolerancia_reforzamiento") or 7
    hora_limite = suma_min(hora_inicio, tolerancia)
    if hora_actual_corta <= hora_limite:
        return "Puntual"
    return "Tardanza"

def registrar_entrada(dni, usuario):
    dni = (dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni):
        return False, "ERROR", "DNI inválido (debe tener 8 dígitos)", {}
    
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
        return False, "ERROR", "DNI no encontrado", {}
    al = dict(al)
    
    if usuario["rol"] == "Auxiliar" and usuario["turno_asignado"]:
        if al["turno_id"] != usuario["turno_asignado"]:
            conn.close()
            return False, "ERROR", f"Este alumno es del turno {al['turno']}. Tu turno es otro.", {}
    
    hoy = hoy_str()
    hora_actual_corta = hora_corta()
    hora_actual = hora_str()
    h = horario_del_dia(al["turno_id"], hoy, al["seccion_id"])
    tipo_escaneo = detectar_tipo_escaneo(h, hora_actual_corta, al["id"])
    
    existente = conn.execute("SELECT * FROM asistencias WHERE alumno_id=? AND fecha=?", (al["id"], hoy)).fetchone()
    nombre_completo = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}".strip(", ")
    
    if tipo_escaneo == "fuera":
        conn.close()
        return False, "ERROR", f"Fuera de horario (clases {h['hora_entrada']}-{h['hora_salida']})", {}
    
    # ===== REFORZAMIENTO =====
    if tipo_escaneo == "reforzamiento":
        refuerzo = h["reforzamiento"]
        
        if not alumno_en_reforzamiento(refuerzo["id"], al["id"]):
            conn.close()
            return False, "ERROR", f"{al['nombres']} {al['apellido_paterno']} NO está en la lista de este reforzamiento", {}
        
        if existente and existente["hora_reforzamiento"]:
            conn.close()
            return False, "ERROR", f"Ya registró reforzamiento hoy como {existente['estado_reforzamiento']}", {}
        
        estado_ref = calcular_estado_refuerzo(refuerzo, hora_actual_corta)
        
        if existente:
            conn.execute("""UPDATE asistencias 
                            SET hora_reforzamiento=?, estado_reforzamiento=?
                            WHERE id=?""",
                         (hora_actual, estado_ref, existente["id"]))
        else:
            conn.execute("""INSERT INTO asistencias 
                            (alumno_id, fecha, hora_reforzamiento, estado_reforzamiento)
                            VALUES (?,?,?,?)""",
                         (al["id"], hoy, hora_actual, estado_ref))
        
        conn.commit()
        conn.close()
        auditar(usuario["usuario"], f"Reforzamiento {estado_ref} DNI {dni}")
        return True, "REFORZAMIENTO", f"{nombre_completo} | {al['grado']}{al['seccion']} | REFORZAMIENTO {estado_ref} {hora_actual_corta}", al
    
    # ===== CLASES NORMALES =====
    if existente and existente["hora"]:
        conn.close()
        return False, "ERROR", f"Ya registrado hoy en clases como {existente['estado']}", {}
    
    if hora_actual_corta <= h["hora_limite"]:
        estado = "Puntual"
        if existente:
            conn.execute("UPDATE asistencias SET hora=?, estado=? WHERE id=?",
                         (hora_actual, estado, existente["id"]))
        else:
            conn.execute("INSERT INTO asistencias (alumno_id, fecha, hora, estado) VALUES (?,?,?,?)",
                         (al["id"], hoy, hora_actual, estado))
        conn.commit()
        conn.close()
        auditar(usuario["usuario"], f"Entrada PUNTUAL DNI {dni}")
        return True, "PUNTUAL", f"{nombre_completo} | {al['grado']}{al['seccion']} | PUNTUAL {hora_actual_corta}", al
    
    ult_acta = conn.execute("SELECT fecha FROM actas_compromiso WHERE alumno_id=? ORDER BY fecha DESC LIMIT 1",
                            (al["id"],)).fetchone()
    desde = ult_acta["fecha"] if ult_acta else "1900-01-01"
    numero = conn.execute("SELECT COUNT(*) FROM tardanzas WHERE alumno_id=? AND fecha > ?",
                          (al["id"], desde)).fetchone()[0] + 1
    
    if numero <= 2:
        accion = "PERDONADO"
        mensaje = f"{nombre_completo} | Tardanza {numero}ª (perdonada) {hora_actual_corta}"
    elif numero == 3:
        accion = "DERIVADO_TOECE"
        mensaje = f"{nombre_completo} | Tardanza 3ª -> DERIVAR A TOECE {hora_actual_corta}"
    else:
        accion = "RETENIDO_APODERADO"
        mensaje = f"{nombre_completo} | Tardanza {numero}ª -> NO PASA hasta apoderado {hora_actual_corta}"
    
    if existente:
        conn.execute("UPDATE asistencias SET hora=?, estado='Tardanza' WHERE id=?",
                     (hora_actual, existente["id"]))
    else:
        conn.execute("INSERT INTO asistencias (alumno_id, fecha, hora, estado) VALUES (?,?,?,?)",
                     (al["id"], hoy, hora_actual, "Tardanza"))
    
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
    hora_actual_corta = hora_corta()
    conn = get_db()
    
    if not es_dia_laboral():
        esp = conn.execute("SELECT tipo FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento'",
                           (hoy,)).fetchone()
        if not esp:
            conn.close()
            return
    
    # Faltas de CLASES NORMALES: al pasar hora_salida
    for t in turnos():
        h = horario_del_dia(t["id"], hoy)
        if hora_actual_corta < h["hora_salida"]:
            continue
        conn.execute("""INSERT OR IGNORE INTO asistencias (alumno_id, fecha, hora, estado)
                        SELECT a.id, ?, ?, 'Falta' FROM alumnos a
                        JOIN secciones s ON a.seccion_id = s.id
                        WHERE s.turno_id = ?""",
                     (hoy, hora_str(), t["id"]))
    
    # Faltas de REFORZAMIENTO: al pasar hora_salida_reforzamiento, solo asignados
    reforzamientos = conn.execute("""
        SELECT d.id AS dia_id, d.hora_salida_reforzamiento
        FROM dias_especiales d
        WHERE d.fecha=? AND d.activo=1 AND d.tipo='evento'
          AND d.tipo_reforzamiento IS NOT NULL
    """, (hoy,)).fetchall()
    
    for ref in reforzamientos:
        hora_fin_ref = ref["hora_salida_reforzamiento"] or "23:59"
        if hora_actual_corta < hora_fin_ref:
            continue
        
        conn.execute("""UPDATE asistencias 
                        SET estado_reforzamiento='Falta', hora_reforzamiento=?
                        WHERE fecha=? AND estado_reforzamiento IS NULL
                          AND alumno_id IN (
                              SELECT alumno_id FROM reforzamiento_alumnos 
                              WHERE dia_especial_id=?
                          )""",
                     (hora_str(), hoy, ref["dia_id"]))
        
        conn.execute("""INSERT OR IGNORE INTO asistencias 
                        (alumno_id, fecha, hora_reforzamiento, estado_reforzamiento)
                        SELECT ra.alumno_id, ?, ?, 'Falta'
                        FROM reforzamiento_alumnos ra
                        WHERE ra.dia_especial_id=?
                          AND ra.alumno_id NOT IN (
                              SELECT alumno_id FROM asistencias WHERE fecha=?
                          )""",
                     (hoy, hora_str(), ref["dia_id"], hoy))
    
    conn.commit()
    conn.close()

def justificar_falta(alumno_id, fecha, justificada, observacion, usuario):
    if usuario["rol"] not in ("Admin", "TOECE"):
        return False, "Solo Admin o TOECE pueden justificar faltas"
    
    fecha_dt = datetime.strptime(fecha, "%Y-%m-%d")
    ahora_dt = ahora().replace(tzinfo=None)
    diferencia = (ahora_dt.date() - fecha_dt.date()).days
    if diferencia > 2:
        return False, f"No se puede justificar una falta con más de 48 horas ({diferencia} días de antigüedad)"
    if diferencia < 0:
        return False, "No se puede justificar una falta con fecha futura"
    
    conn = get_db()
    al = conn.execute("SELECT apellido_paterno, apellido_materno, nombres FROM alumnos WHERE id=?",
                      (alumno_id,)).fetchone()
    if not al:
        conn.close()
        return False, "Alumno no encontrado"
    
    falta = conn.execute("SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND estado='Falta'",
                         (alumno_id, fecha)).fetchone()
    if not falta:
        conn.close()
        return False, f"Ese alumno no tiene una Falta registrada el {fecha}"
    
    conn.execute("UPDATE asistencias SET justificada=?, observacion=? WHERE id=?",
                 (1 if justificada else 0, observacion, falta["id"]))
    conn.commit()
    conn.close()
    
    nombre = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}".strip(", ")
    estado_txt = "JUSTIFICADA" if justificada else "INJUSTIFICADA"
    auditar(usuario["usuario"], f"Falta de {nombre} ({fecha}) -> {estado_txt}")
    return True, f"Falta de {nombre} ({fecha}) marcada como {estado_txt}"

def editar_estado_asistencia(alumno_id, nuevo_estado, observacion, usuario):
    if usuario["rol"] != "Admin":
        return False, "Solo el Admin puede editar asistencias"
    conn = get_db()
    al = conn.execute("SELECT apellido_paterno, apellido_materno, nombres FROM alumnos WHERE id=?",
                      (alumno_id,)).fetchone()
    if not al:
        conn.close()
        return False, "Alumno no encontrado"
    hoy = hoy_str()
    asist = conn.execute("SELECT id, estado FROM asistencias WHERE alumno_id=? AND fecha=?",
                         (alumno_id, hoy)).fetchone()
    if asist:
        conn.execute("UPDATE asistencias SET estado=?, observacion=? WHERE id=?",
                     (nuevo_estado, observacion, asist["id"]))
        accion_txt = f"Editó asistencia: {asist['estado']} -> {nuevo_estado}"
    else:
        conn.execute("INSERT INTO asistencias (alumno_id, fecha, hora, estado, observacion) VALUES (?,?,?,?,?)",
                     (alumno_id, hoy, hora_str(), nuevo_estado, observacion))
        accion_txt = f"Creó asistencia manual: {nuevo_estado}"
    conn.commit()
    conn.close()
    nombre = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}".strip(", ")
    auditar(usuario["usuario"], f"{accion_txt} alumno_id={alumno_id}")
    return True, f"Asistencia de {nombre} actualizada a {nuevo_estado}"

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

@st.cache_data(ttl=5)
def metricas_dia(fecha):
    conn = get_db()
    r = conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM alumnos)                                             AS total,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado='Puntual')      AS puntuales,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado='Falta')        AS faltas,
          (SELECT COUNT(*) FROM tardanzas   WHERE fecha=?)                           AS tardanzas,
          (SELECT COUNT(*) FROM tardanzas   WHERE fecha=? AND accion='DERIVADO_TOECE')      AS derivados,
          (SELECT COUNT(*) FROM tardanzas   WHERE fecha=? AND accion='RETENIDO_APODERADO') AS retenidos,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado_reforzamiento='Puntual') AS puntuales_ref,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado_reforzamiento='Tardanza') AS tardanzas_ref,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado_reforzamiento='Falta') AS faltas_ref
    """, (fecha, fecha, fecha, fecha, fecha, fecha, fecha, fecha)).fetchone()
    conn.close()
    return dict(r)

def ultimos_registros(fecha, limite=20):
    conn = get_db()
    df = pd.read_sql("""
        SELECT a.dni, a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
               a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               ast.hora, ast.estado, ast.hora_reforzamiento, ast.estado_reforzamiento
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ast.fecha = ? AND (ast.hora IS NOT NULL OR ast.hora_reforzamiento IS NOT NULL)
        ORDER BY COALESCE(ast.hora_reforzamiento, ast.hora) DESC LIMIT ?
    """, conn, params=[fecha, limite])
    conn.close()
    return df

def filtros_grado_seccion_nombre(key_prefix, placeholder_nombre="Buscar por nombre", mostrar_todos=True):
    grados = grados_lista()
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        opciones_grado = ([{"id": None, "nombre": "Todos"}] if mostrar_todos else []) + grados
        grado_sel = st.selectbox("Grado", opciones_grado, format_func=lambda g: g["nombre"], key=f"{key_prefix}_grado")
    with c2:
        if grado_sel and grado_sel["id"]:
            secs = ([{"id": None, "nombre": "Todas"}] if mostrar_todos else []) + secciones_por_grado(grado_sel["id"])
        else:
            secs = [{"id": None, "nombre": "Todas"}] if mostrar_todos else []
        seccion_sel = st.selectbox("Sección", secs, format_func=lambda s: s["nombre"],
                                   key=f"{key_prefix}_seccion") if secs else {"id": None, "nombre": "—"}
    with c3:
        texto = st.text_input("Buscar por nombre", placeholder=placeholder_nombre, key=f"{key_prefix}_nombre")
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
        f"SELECT alumno_id, estado, estado_reforzamiento FROM asistencias WHERE fecha=? AND alumno_id IN ({placeholders})",
        [hoy_str()] + list(alumno_ids)
    ).fetchall()
    conn.close()
    return {r["alumno_id"]: {"estado": r["estado"], "estado_ref": r["estado_reforzamiento"]} for r in rows}

def exportar_excel_pdf(df, titulo, nombre_base, key_prefix="export"):
    if df.empty:
        return
    c1, c2 = st.columns(2)
    with c1:
        ex = BytesIO()
        with pd.ExcelWriter(ex, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name="Datos")
        st.download_button("Excel", ex.getvalue(), f"{nombre_base}.xlsx",
                           key=f"{key_prefix}_xlsx", width="stretch")
    with c2:
        st.download_button("PDF", pdf_tabla(df, titulo),
                           f"{nombre_base}.pdf", "application/pdf",
                           key=f"{key_prefix}_pdf", width="stretch")

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
            th = cv2.adaptiveThreshold(base, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5)
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
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E65100")),
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
    MARGEN = 8
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=MARGEN, leftMargin=MARGEN, topMargin=MARGEN, bottomMargin=MARGEN)
    estilos = getSampleStyleSheet()
    if len(rows) > 1:
        titulo = f"Carnets - {rows[0]['grado']}{rows[0]['seccion']} ({rows[0]['turno']})"
    else:
        titulo = f"Carnet - {rows[0]['apellido_paterno']} {rows[0]['apellido_materno'] or ''}, {rows[0]['nombres']}"
    el = [Paragraph(titulo, estilos["Heading1"]), Spacer(1, 6)]
    COLUMNAS = 3
    FILAS = 3
    CARNTS_POR_PAGINA = COLUMNAS * FILAS
    ancho_hoja = A4[0]
    alto_hoja = A4[1]
    ancho_util = ancho_hoja - (2 * MARGEN)
    alto_util = alto_hoja - (2 * MARGEN)
    alto_disponible = alto_util - 50
    ancho_carnet = ancho_util / COLUMNAS
    alto_carnet = alto_disponible / FILAS
    for i in range(0, len(rows), CARNTS_POR_PAGINA):
        lote = rows[i:i + CARNTS_POR_PAGINA]
        tabla = []
        for j in range(0, len(lote), COLUMNAS):
            fila = []
            for a in lote[j:j + COLUMNAS]:
                qb = BytesIO()
                qr_de_dni(a["dni"]).save(qb, format="PNG")
                qb.seek(0)
                celda = [
                    Paragraph(f"<b><font size=11>{a['apellido_paterno']} {a['apellido_materno'] or ''}</font></b>", estilos["Normal"]),
                    Paragraph(f"<font size=10>{a['nombres']}</font>", estilos["Normal"]),
                    Paragraph(f"<font size=10>DNI: {a['dni']}</font>", estilos["Normal"]),
                    Paragraph(f"<font size=9>{a['grado']}{a['seccion']} - {a['turno']}</font>", estilos["Normal"]),
                    RLImage(qb, width=150, height=150),
                ]
                fila.append(celda)
            while len(fila) < COLUMNAS:
                fila.append([])
            tabla.append(fila)
        while len(tabla) < FILAS:
            tabla.append([[] for _ in range(COLUMNAS)])
        col_widths = [ancho_carnet] * COLUMNAS
        row_heights = [alto_carnet] * FILAS
        t = Table(tabla, colWidths=col_widths, rowHeights=row_heights)
        t.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOX", (0, 0), (-1, -1), 1.5, colors.black),
            ("INNERGRID", (0, 0), (-1, -1), 1, colors.black),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        el.append(t)
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

def _df_to_xlsx(df, sheet_name="Datos"):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=sheet_name)
    return buf.getvalue()

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
                errores.append({"fila": fila_num, "motivo": f"DNI {dni} duplicado en Excel"}); continue
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
    errores = []
    for idx, v in enumerate(validas):
        try:
            g = c.execute("SELECT id FROM grados WHERE nombre=?", (v["grado"],)).fetchone()
            g_id = g["id"] if g else c.execute("INSERT INTO grados (nombre) VALUES (?)", (v["grado"],)).lastrowid
            t_id = turnos_map.get(v["turno"])
            if not t_id:
                errores.append(f"Fila {idx+1}: Turno '{v['turno']}' no encontrado")
                continue
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
        except Exception as e:
            errores.append(f"Fila {idx+1}: {str(e)}")
    conn.commit()
    conn.close()
    st.cache_data.clear()
    if errores:
        st.session_state["_imp_errores_insert"] = errores
    return insertados

def vista_login():
    st.title("Sistema de Asistencia - I.E. Yarinacocha")
    st.caption("Ingresa con tu usuario y contraseña")
    st.markdown("---")
    with st.form("login"):
        col1, col2, col3 = st.columns([1, 1.2, 1])
        with col2:
            u = st.text_input("Usuario")
            p = st.text_input("Contraseña", type="password")
            ok = st.form_submit_button("Ingresar", type="primary", width="stretch")
        if ok:
            user = autenticar(u, p)
            if user:
                st.session_state.user = user
                auditar(user["usuario"], "Login")
                st.rerun()
            else:
                st.error("Credenciales incorrectas")

@st.fragment
def _frag_lista_marcar(usuario, seccion_id):
    df = alumnos_de_seccion(seccion_id)
    if df.empty:
        st.info("No hay alumnos en esta sección.")
        return
    estados = estado_asistencia_hoy(df["id"].tolist())
    for _, al in df.iterrows():
        info = estados.get(al["id"], {})
        estado = info.get("estado")
        estado_ref = info.get("estado_ref")
        
        c1, c2 = st.columns([5, 1])
        with c1:
            if estado == "Puntual":
                st.markdown(f"**{al['nombre_completo']}** - Puntual")
            elif estado == "Tardanza":
                st.markdown(f"**{al['nombre_completo']}** - Tardanza")
            elif estado == "Falta":
                st.markdown(f"**{al['nombre_completo']}** - Falta")
            else:
                st.markdown(f"**{al['nombre_completo']}** - sin registrar")
            if estado_ref:
                st.caption(f"Reforzamiento: {estado_ref}")
        with c2:
            if estado is None:
                if st.button("Marcar", key=f"m_{al['id']}", width="stretch"):
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
            ok, tipo, msg, extra = registrar_entrada(dni, usuario)
            if not ok:
                st.error(msg)
            elif tipo in ("PUNTUAL", "REFORZAMIENTO"):
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
        st.success(f"Evento escolar: {esp['descripcion']} (entrada {esp['hora_entrada']})")
    elif esp and esp["tipo"] == "feriado":
        st.info(f"Feriado / sin clases: {esp['descripcion']} - No se toma asistencia.")
        return
    elif not es_dia_laboral():
        st.warning("Hoy no es día laboral. No se toma asistencia.")
        return
    else:
        st.info(f"Día laboral - {hoy}")
    
    marcar_faltas_al_cierre()
    st.markdown("---")
    modo = st.radio("Método", ["Escanear QR", "Lista por sección"],
                    horizontal=True, key="modo_puerta")
    
    if modo == "Escanear QR":
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

@st.fragment
def _frag_escaner_reforzamiento(usuario):
    st.caption("Muestra el QR del alumno a la cámara")
    img_file = st.camera_input("Escanea el QR", key="camara_qr_reforzamiento")
    if img_file:
        dni = leer_qr(Image.open(BytesIO(img_file.getvalue())))
        if not dni:
            st.error("No se detectó QR. Prueba con mejor luz o más cerca.")
        else:
            ok, tipo, msg, extra = registrar_entrada(dni, usuario)
            if not ok:
                st.error(msg)
            elif tipo == "REFORZAMIENTO":
                st.success(msg)
                st.balloons()
            elif tipo == "PUNTUAL":
                st.info(msg + " (registrado en clases normales)")
            elif tipo == "TARDANZA":
                st.warning(msg + " (registrado en clases normales)")

def vista_escanear_reforzamiento():
    st.title("🎯 Escanear Reforzamiento")
    usuario = st.session_state.user
    hoy = hoy_str()
    marcar_faltas_al_cierre()
    
    conn = get_db()
    refs_hoy = conn.execute("""
        SELECT d.id, d.descripcion, d.hora_entrada AS hora_inicio,
               d.hora_salida_reforzamiento AS hora_fin, d.tolerancia_reforzamiento,
               d.tipo_reforzamiento,
               GROUP_CONCAT(DISTINCT g.nombre || s.nombre) AS secciones
        FROM dias_especiales d
        LEFT JOIN dias_especiales_secciones ds ON d.id = ds.dia_especial_id
        LEFT JOIN secciones s ON ds.seccion_id = s.id
        LEFT JOIN grados g ON s.grado_id = g.id
        WHERE d.fecha = ? AND d.activo = 1 AND d.tipo = 'evento'
          AND d.tipo_reforzamiento IS NOT NULL
        GROUP BY d.id
    """, (hoy,)).fetchall()
    conn.close()
    
    if not refs_hoy:
        st.info("Hoy no hay reforzamientos programados.")
        return
    
    st.subheader("Reforzamiento de hoy")
    for ref in refs_hoy:
        ref = dict(ref)
        with st.container(border=True):
            c1, c2, c3 = st.columns([2, 1, 2])
            with c1:
                st.markdown(f"**{ref['descripcion']}**")
                st.caption(f"Secciones: {ref['secciones']}")
            with c2:
                st.markdown(f"**{ref['hora_inicio']} - {ref['hora_fin'] or '?'}**")
            with c3:
                st.caption(f"Tolerancia: {ref['tolerancia_reforzamiento']} min")
    
    st.markdown("---")
    st.subheader("Escanear QR")
    _frag_escaner_reforzamiento(usuario)
    
    st.markdown("---")
    st.subheader("Resumen de hoy")
    
    conn = get_db()
    for ref in refs_hoy:
        ref = dict(ref)
        st.markdown(f"**{ref['descripcion']}**")
        
        df = pd.read_sql("""
            SELECT a.dni,
                   a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
                   a.nombres, g.nombre AS grado, s.nombre AS seccion,
                   ast.hora_reforzamiento AS hora, ast.estado_reforzamiento AS estado
            FROM reforzamiento_alumnos ra
            JOIN alumnos a ON ra.alumno_id = a.id
            JOIN secciones s ON a.seccion_id = s.id
            JOIN grados g ON s.grado_id = g.id
            LEFT JOIN asistencias ast ON ast.alumno_id = a.id AND ast.fecha = ?
            WHERE ra.dia_especial_id = ?
            ORDER BY a.apellido_paterno, a.apellido_materno, a.nombres
        """, conn, params=[hoy, ref["id"]])
        
        if df.empty:
            st.info("No hay alumnos asignados a este reforzamiento.")
            continue
        
        df["estado"] = df["estado"].fillna("Sin registrar")
        cnt = df["estado"].value_counts()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Asignados", len(df))
        c2.metric("Puntuales", int(cnt.get("Puntual", 0)))
        c3.metric("Tardanzas", int(cnt.get("Tardanza", 0)))
        c4.metric("Faltas", int(cnt.get("Falta", 0)))
        
        st.dataframe(
            df[["apellidos", "nombres", "grado", "seccion", "hora", "estado"]].style.map(
                color_estado, subset=["estado"]
            ),
            width="stretch"
        )
    conn.close()

def vista_faltas():
    st.title("Faltas - Justificar / Editar")
    usuario = st.session_state.user
    
    if usuario["rol"] not in ("Admin", "TOECE"):
        st.error("Solo Admin y TOECE pueden acceder a esta vista.")
        return
    
    if "_msg_falta" in st.session_state:
        st.success(st.session_state["_msg_falta"])
        del st.session_state["_msg_falta"]
    
    hoy_dt = ahora().date()
    limite_dt = hoy_dt - timedelta(days=2)
    st.caption(f"⚠️ Solo se pueden justificar faltas desde {limite_dt.strftime('%Y-%m-%d')} hasta hoy (48 horas).")
    
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        fecha_filtro = st.date_input("Fecha de la falta",
                                     value=hoy_dt,
                                     min_value=limite_dt,
                                     max_value=hoy_dt,
                                     key="falt_fecha")
    with c2:
        grados = grados_lista()
        opciones_grado = [{"id": None, "nombre": "Todos"}] + grados
        grado_sel = st.selectbox("Grado", opciones_grado,
                                 format_func=lambda g: g["nombre"], key="falt_grado")
    with c3:
        if grado_sel and grado_sel["id"]:
            secs = [{"id": None, "nombre": "Todas"}] + secciones_por_grado(grado_sel["id"])
        else:
            secs = [{"id": None, "nombre": "Todas"}]
        seccion_sel = st.selectbox("Sección", secs,
                                   format_func=lambda s: s["nombre"], key="falt_seccion")
    
    grado_id = grado_sel["id"] if grado_sel else None
    seccion_id = seccion_sel["id"] if (grado_sel and grado_sel["id"] and seccion_sel) else None
    fecha_str = fecha_filtro.strftime("%Y-%m-%d")
    
    q = """
        SELECT ast.id AS asist_id, a.id AS alumno_id, a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
               a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               ast.fecha, ast.justificada, COALESCE(ast.observacion,'') AS observacion
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ast.estado='Falta' AND ast.fecha = ?
    """
    params = [fecha_str]
    if grado_id:
        q += " AND g.id = ?"; params.append(grado_id)
    if seccion_id:
        q += " AND s.id = ?"; params.append(seccion_id)
    q += " ORDER BY t.nombre, g.nombre, s.nombre, a.apellido_paterno"
    
    conn = get_db()
    df = pd.read_sql(q, conn, params=params)
    conn.close()
    
    if df.empty:
        st.info(f"No hay faltas registradas para el {fecha_str} con esos filtros.")
    else:
        st.write(f"**{len(df)} faltas** encontradas")
        df["estado"] = df["justificada"].map({1: "Justificada", 0: "Injustificada"})
        st.dataframe(
            df[["fecha", "dni", "apellidos", "nombres", "grado", "seccion", "turno",
                "estado", "observacion"]],
            width="stretch"
        )
    
    st.markdown("---")
    st.subheader("Justificar o injustificar una falta")
    
    if not df.empty:
        opciones_alumnos = {}
        for _, r in df.iterrows():
            etiqueta = f"{r['apellidos']}, {r['nombres']} - {r['grado']}{r['seccion']} ({r['estado']})"
            opciones_alumnos[etiqueta] = r["alumno_id"]
        
        sel_al = st.selectbox("Selecciona un alumno de la lista",
                              list(opciones_alumnos.keys()), key="falt_j_alumno")
        alumno_id_sel = opciones_alumnos[sel_al]
        
        fila = df[df["alumno_id"] == alumno_id_sel].iloc[0]
        
        with st.form("form_justificar"):
            justificada_actual = bool(fila["justificada"])
            nueva_just = st.radio("Estado",
                                  ["Justificada", "Injustificada"],
                                  index=0 if justificada_actual else 1,
                                  horizontal=True,
                                  key="falt_j_estado")
            obs_nueva = st.text_area("Observación (motivo)",
                                     value=fila["observacion"],
                                     placeholder="Ej: Presentó certificado médico",
                                     key="falt_j_obs")
            guardar = st.form_submit_button("Guardar cambio", type="primary", width="stretch")
        
        if guardar:
            ok, msg = justificar_falta(alumno_id_sel, fecha_str,
                                       nueva_just == "Justificada",
                                       obs_nueva, usuario)
            if ok:
                st.session_state["_msg_falta"] = msg
                st.cache_data.clear()
                st.rerun()
            else:
                st.error(msg)
    else:
        st.info("No hay faltas para justificar con los filtros seleccionados.")

def vista_toece():
    st.title("TOECE - Derivados y Observados")
    usuario = st.session_state.user
    hoy = hoy_str()
    
    if "last_refresh_toece" not in st.session_state:
        st.session_state.last_refresh_toece = time.time()
    
    c1, c2 = st.columns([1, 5])
    with c1:
        if st.button("Actualizar", width="stretch", key="refresh_toece"):
            st.session_state.last_refresh_toece = time.time()
            st.rerun()
    with c2:
        segundos_desde = int(time.time() - st.session_state.last_refresh_toece)
        st.caption(f"Auto-refresh cada 30s (hace {segundos_desde}s)")
    
    if time.time() - st.session_state.last_refresh_toece > 30:
        st.session_state.last_refresh_toece = time.time()
        st.rerun()
    
    if "_msg_acta" in st.session_state:
        st.success(st.session_state["_msg_acta"])
        del st.session_state["_msg_acta"]
    if "_msg_liberar" in st.session_state:
        st.success(st.session_state["_msg_liberar"])
        del st.session_state["_msg_liberar"]
    
    tab1, tab2, tab3 = st.tabs(["Derivados hoy", "Firmar Acta", "Observados"])
    
    with tab1:
        conn = get_db()
        df = pd.read_sql("""
            SELECT t.id, t.numero AS "N", a.dni,
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
            st.dataframe(df, width="stretch")
    
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
                opciones = {f"{r['nombre_completo']} (DNI {r['dni']}) - {r['accion']}": r["alumno_id"]
                            for _, r in der.iterrows()}
                sel = st.selectbox("Alumno", list(opciones.keys()))
                motivo = st.text_input("Motivo", value="Reincidencia en tardanzas")
                obs = st.text_area("Observaciones")
                ok = st.form_submit_button("Firmar acta", type="primary", width="stretch")
            if ok:
                registrar_acta(opciones[sel], motivo, obs, usuario)
                st.session_state["_msg_acta"] = "Acta registrada correctamente."
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
            st.dataframe(df_show, width="stretch")
            exportar_excel_pdf(df_show, "Reporte de Observados", "observados", key_prefix="obs")
            st.markdown("---")
            st.subheader("Liberar de lista de observados")
            with st.form("liberar_obs"):
                op = {f"{r['apellidos']}, {r['nombres']} (DNI {r['dni']})": r["id"] for _, r in df.iterrows()}
                sel = st.selectbox("Alumno", list(op.keys()))
                liberar = st.form_submit_button("Liberar", type="primary", width="stretch")
            if liberar:
                conn = get_db()
                row = conn.execute("SELECT alumno_id FROM observados WHERE id=?", (op[sel],)).fetchone()
                conn.close()
                if row:
                    liberar_observado(row["alumno_id"], usuario)
                    st.session_state["_msg_liberar"] = "Alumno liberado."
                    st.cache_data.clear()
                    st.rerun()

def vista_panel_direccion():
    import plotly.express as px
    st.title("Panel Dirección")
    hoy = hoy_str()
    marcar_faltas_al_cierre()
    
    if "last_refresh_panel" not in st.session_state:
        st.session_state.last_refresh_panel = time.time()
    
    c1, c2 = st.columns([1, 5])
    with c1:
        if st.button("Actualizar", width="stretch", key="refresh_panel"):
            st.session_state.last_refresh_panel = time.time()
            st.rerun()
    with c2:
        segundos_desde = int(time.time() - st.session_state.last_refresh_panel)
        st.caption(f"Auto-refresh cada 30s (hace {segundos_desde}s)")
    
    if time.time() - st.session_state.last_refresh_panel > 30:
        st.session_state.last_refresh_panel = time.time()
        st.rerun()
    
    m = metricas_dia(hoy)
    
    st.subheader("Asistencia en Clases")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total alumnos", m["total"])
    c2.metric("Puntuales", m["puntuales"])
    c3.metric("Tardanzas", m["tardanzas"])
    c4.metric("Faltas", m["faltas"])
    c1, c2 = st.columns(2)
    c1.metric("Derivados TOECE", m["derivados"])
    c2.metric("Retenidos", m["retenidos"])
    
    st.markdown("---")
    st.subheader("Asistencia en Reforzamiento")
    c1, c2, c3 = st.columns(3)
    c1.metric("Puntuales", m.get("puntuales_ref", 0))
    c2.metric("Tardanzas", m.get("tardanzas_ref", 0))
    c3.metric("Faltas", m.get("faltas_ref", 0))
    
    st.markdown("---")
    st.subheader("Alertas en tiempo real")
    st.caption("Últimos escaneos de los estudiantes")
    df_ultimos = ultimos_registros(hoy, limite=20)
    if df_ultimos.empty:
        st.info("Sin escaneos registrados hoy.")
    else:
        df_show = df_ultimos.copy()
        df_show["tipo"] = df_show.apply(
            lambda r: "Reforzamiento" if r["estado_reforzamiento"] else "Clases", axis=1
        )
        df_show["estado_final"] = df_show.apply(
            lambda r: r["estado_reforzamiento"] if r["estado_reforzamiento"] else r["estado"], axis=1
        )
        df_show["hora_final"] = df_show.apply(
            lambda r: r["hora_reforzamiento"] if r["hora_reforzamiento"] else r["hora"], axis=1
        )
        st.dataframe(
            df_show[["tipo", "apellidos", "nombres", "grado", "seccion", "hora_final", "estado_final"]].style.map(
                color_estado, subset=["estado_final"]
            ),
            width="stretch"
        )
    
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

def _mostrar_perfil_completo(dni):
    import plotly.express as px
    
    col1, col2 = st.columns([1, 5])
    with col1:
        if st.button("Volver", type="primary", width="stretch"):
            st.session_state.pop("perfil_dni", None)
            st.rerun()
    with col2:
        st.caption("Volver a la búsqueda")
    
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
    
    df_asist = pd.read_sql("""SELECT fecha, estado, justificada,
                                     COALESCE(observacion,'') AS observacion,
                                     hora_reforzamiento, estado_reforzamiento,
                                     justificada_reforzamiento,
                                     COALESCE(observacion_reforzamiento,'') AS observacion_reforzamiento
                              FROM asistencias WHERE alumno_id=? ORDER BY fecha DESC""", conn, params=[al["id"]])
    df_tard = pd.read_sql("""SELECT fecha, hora, numero AS "N", accion
                             FROM tardanzas WHERE alumno_id=? ORDER BY fecha DESC, hora DESC""", conn, params=[al["id"]])
    df_obs = pd.read_sql("""SELECT fecha_ingreso, COALESCE(fecha_salida,'-') AS fecha_salida,
                                   COALESCE(motivo,'') AS motivo, activo
                            FROM observados WHERE alumno_id=? ORDER BY fecha_ingreso DESC""", conn, params=[al["id"]])
    df_act = pd.read_sql("""SELECT fecha, COALESCE(motivo,'') AS motivo
                            FROM actas_compromiso WHERE alumno_id=? ORDER BY fecha DESC""", conn, params=[al["id"]])
    conn.close()
    
    nombre_completo = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}".strip(", ")
    st.markdown(f"### {nombre_completo}")
    c1, c2, c3 = st.columns(3)
    c1.write(f"**DNI:** {al['dni']}")
    c1.write(f"**Grado:** {al['grado']}{al['seccion']}")
    c2.write(f"**Turno:** {al['turno']}")
    c2.write(f"**Apoderado:** {al['nombre_apoderado'] or '-'}")
    c3.write(f"**Teléfono:** {al['telefono_apoderado'] or '-'}")
    if not df_obs.empty and any(r["activo"] == 1 for _, r in df_obs.iterrows()):
        c3.error("OBSERVADO")
    
    st.markdown("---")
    
    st.subheader("Resumen")
    total_puntuales = (df_asist["estado"] == "Puntual").sum()
    total_faltas = (df_asist["estado"] == "Falta").sum()
    total_tardanzas = len(df_tard)
    total_actas = len(df_act)
    total_ref_puntuales = (df_asist["estado_reforzamiento"] == "Puntual").sum()
    total_ref_tardanzas = (df_asist["estado_reforzamiento"] == "Tardanza").sum()
    total_ref_faltas = (df_asist["estado_reforzamiento"] == "Falta").sum()
    
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Puntuales (clases)", total_puntuales)
    m2.metric("Faltas (clases)", total_faltas)
    m3.metric("Tardanzas", total_tardanzas)
    m4.metric("Actas firmadas", total_actas)
    
    m1, m2, m3 = st.columns(3)
    m1.metric("Puntuales (reforz.)", total_ref_puntuales)
    m2.metric("Tardanzas (reforz.)", total_ref_tardanzas)
    m3.metric("Faltas (reforz.)", total_ref_faltas)
    
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
        st.dataframe(df_tard, width="stretch")
    else:
        st.info("Sin tardanzas.")
    
    st.markdown("---")
    st.subheader("Historial completo de asistencias")
    if df_asist.empty:
        st.info("Sin registros.")
    else:
        df_hist = df_asist[["fecha", "estado", "justificada", "observacion",
                             "hora_reforzamiento", "estado_reforzamiento"]].copy()
        df_hist.columns = ["Fecha", "Estado clases", "Justificada", "Observación",
                            "Hora reforz.", "Estado reforz."]
        st.dataframe(df_hist.style.map(color_estado, subset=["Estado clases"]), width="stretch")
    
    if st.session_state.user["rol"] == "Admin":
        st.markdown("---")
        st.subheader("Editar asistencia de hoy")
        
        if "_msg_edit_asist" in st.session_state:
            st.success(st.session_state["_msg_edit_asist"])
            del st.session_state["_msg_edit_asist"]
        
        conn = get_db()
        asist_hoy = conn.execute("""SELECT id, estado, COALESCE(observacion,'') AS observacion,
                                            hora_reforzamiento, estado_reforzamiento
                                    FROM asistencias WHERE alumno_id=? AND fecha=?""",
                                 (al["id"], hoy_str())).fetchone()
        conn.close()
        
        if asist_hoy:
            st.write(f"**Estado actual clases:** {asist_hoy['estado'] or '-'}")
            if asist_hoy["estado_reforzamiento"]:
                st.write(f"**Estado actual reforzamiento:** {asist_hoy['estado_reforzamiento']}")
            with st.form("editar_asist_hoy"):
                estados_posibles = ["(sin cambio)", "Puntual", "Tardanza", "Falta"]
                idx = estados_posibles.index(asist_hoy["estado"]) if asist_hoy["estado"] in estados_posibles else 0
                nuevo_estado = st.selectbox("Nuevo estado (clases)", estados_posibles, index=idx)
                obs_edit = st.text_input("Observación (clases)", value=asist_hoy["observacion"])
                guardar_edit = st.form_submit_button("Guardar cambio clases", type="primary", width="stretch")
            if guardar_edit and nuevo_estado != "(sin cambio)":
                ok, msg = editar_estado_asistencia(al["id"], nuevo_estado, obs_edit, st.session_state.user)
                if ok:
                    st.session_state["_msg_edit_asist"] = msg
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.error(msg)
        else:
            st.info("Este alumno no tiene asistencia registrada hoy.")
            with st.form("crear_asist_hoy"):
                nuevo_estado = st.selectbox("Estado", ["Puntual", "Tardanza", "Falta"])
                obs_edit = st.text_input("Observación")
                guardar_edit = st.form_submit_button("Crear asistencia", type="primary", width="stretch")
            if guardar_edit:
                ok, msg = editar_estado_asistencia(al["id"], nuevo_estado, obs_edit, st.session_state.user)
                if ok:
                    st.session_state["_msg_edit_asist"] = msg
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.error(msg)

def vista_reportes():
    import plotly.express as px
    st.title("Reportes y Consultas")
    
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        periodo = st.selectbox("Período", ["Diario", "Semanal", "Mensual", "Bimestral"], key="rep_periodo")
    with c2:
        turno_opts = ["Todos"] + [t["nombre"] for t in turnos()]
        turno_sel = st.selectbox("Turno", turno_opts, key="rep_turno")
    with c3:
        tipo_asistencia = st.selectbox("Tipo de asistencia",
                                        ["Clases normales", "Reforzamiento", "Ambos"],
                                        key="rep_tipo_asist")
    with c4:
        tipo_reporte = st.selectbox("Tipo de reporte",
                                     ["Detalle", "Conteo de faltas", "Cierre mensual"],
                                     key="rep_tipo")
    
    grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
        key_prefix="rep", placeholder_nombre="Ej: Quispe"
    )
    
    if tipo_reporte == "Cierre mensual":
        _rep_cierre(grado_id, seccion_id, turno_sel)
        return
    
    dias = {"Diario": 0, "Semanal": 7, "Mensual": 30, "Bimestral": 60}[periodo]
    fin = ahora().date()
    ini = fin - timedelta(days=dias)
    st.caption(f"Rango: {ini} a {fin}")
    
    if tipo_reporte == "Detalle":
        _rep_detalle(ini, fin, turno_sel, grado_id, seccion_id, texto, periodo, tipo_asistencia)
    else:
        _rep_conteo(ini, fin, turno_sel, grado_id, seccion_id, texto, periodo, tipo_asistencia)


def _rep_detalle(ini, fin, turno_sel, grado_id, seccion_id, texto, periodo, tipo_asistencia):
    import plotly.express as px
    q = """
        SELECT a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
               a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               ast.fecha, ast.hora, ast.estado, ast.justificada,
               ast.hora_reforzamiento, ast.estado_reforzamiento, ast.justificada_reforzamiento
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
    q += " ORDER BY ast.fecha DESC, t.nombre, g.nombre, s.nombre"
    conn = get_db()
    df = pd.read_sql(q, conn, params=params)
    conn.close()
    
    if df.empty:
        st.warning("Sin datos para los filtros.")
        return
    
    if tipo_asistencia == "Clases normales":
        df = df[df["hora"].notna()]
    elif tipo_asistencia == "Reforzamiento":
        df = df[df["hora_reforzamiento"].notna()]
    
    if df.empty:
        st.warning("Sin datos para el tipo de asistencia seleccionado.")
        return
    
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Registros", len(df))
    if tipo_asistencia == "Reforzamiento":
        m2.metric("Puntuales", (df["estado_reforzamiento"] == "Puntual").sum())
        m3.metric("Tardanzas", (df["estado_reforzamiento"] == "Tardanza").sum())
        m4.metric("Faltas", (df["estado_reforzamiento"] == "Falta").sum())
    else:
        m2.metric("Puntuales", (df["estado"] == "Puntual").sum())
        m3.metric("Faltas", (df["estado"] == "Falta").sum())
        m4.metric("Tardanzas", (df["estado"] == "Tardanza").sum())
    
    st.markdown("---")
    col_a, col_b = st.columns(2)
    with col_a:
        if tipo_asistencia == "Reforzamiento":
            cnt = df["estado_reforzamiento"].value_counts().reset_index()
        else:
            cnt = df["estado"].value_counts().reset_index()
        cnt.columns = ["Estado", "Cantidad"]
        fig = px.pie(cnt, names="Estado", values="Cantidad", title="Distribución de estados",
                     hole=0.45, color="Estado",
                     color_discrete_map={"Puntual": "#28a745", "Falta": "#dc3545", "Tardanza": "#ffc107"})
        st.plotly_chart(fig, use_container_width=True)
    with col_b:
        if tipo_asistencia == "Reforzamiento":
            por_dia = df.groupby(["fecha", "estado_reforzamiento"]).size().reset_index(name="Cantidad")
            por_dia.columns = ["fecha", "estado", "Cantidad"]
        else:
            por_dia = df.groupby(["fecha", "estado"]).size().reset_index(name="Cantidad")
        fig = px.bar(por_dia, x="fecha", y="Cantidad", color="estado", title="Asistencia por día",
                     color_discrete_map={"Puntual": "#28a745", "Falta": "#dc3545", "Tardanza": "#ffc107"},
                     barmode="stack")
        st.plotly_chart(fig, use_container_width=True)
    
    st.markdown("---")
    st.subheader("Resumen por salón")
    if tipo_asistencia == "Reforzamiento":
        resumen_salon = df[df["estado_reforzamiento"].notna()].groupby(["grado", "seccion", "turno"]).agg(
            puntuales=("estado_reforzamiento", lambda x: (x == "Puntual").sum()),
            faltas=("estado_reforzamiento", lambda x: (x == "Falta").sum()),
            tardanzas=("estado_reforzamiento", lambda x: (x == "Tardanza").sum()),
            total=("estado_reforzamiento", "count")
        ).reset_index()
    else:
        resumen_salon = df[df["estado"].notna()].groupby(["grado", "seccion", "turno"]).agg(
            puntuales=("estado", lambda x: (x == "Puntual").sum()),
            faltas=("estado", lambda x: (x == "Falta").sum()),
            tardanzas=("estado", lambda x: (x == "Tardanza").sum()),
            total=("estado", "count")
        ).reset_index()
    resumen_salon["salon"] = resumen_salon["grado"] + resumen_salon["seccion"]
    st.dataframe(resumen_salon[["salon", "turno", "puntuales", "faltas", "tardanzas", "total"]],
                 width="stretch")
    
    exportar_excel_pdf(df, f"Reporte {periodo} - {tipo_asistencia}",
                       f"reporte_{periodo}_{tipo_asistencia.replace(' ', '_')}", key_prefix="rep_det")


def _rep_conteo(ini, fin, turno_sel, grado_id, seccion_id, texto, periodo, tipo_asistencia):
    import plotly.express as px
    st.subheader("Conteo de faltas")
    
    if tipo_asistencia == "Reforzamiento":
        q = """
            SELECT a.dni,
                   a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
                   a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
                   SUM(CASE WHEN ast.justificada_reforzamiento=1 THEN 1 ELSE 0 END) AS faltas_just,
                   SUM(CASE WHEN ast.justificada_reforzamiento=0 THEN 1 ELSE 0 END) AS faltas_injust,
                   COUNT(*) AS total_faltas
            FROM asistencias ast
            JOIN alumnos a ON ast.alumno_id = a.id
            JOIN secciones s ON a.seccion_id = s.id
            JOIN grados g ON s.grado_id = g.id
            JOIN turnos t ON s.turno_id = t.id
            WHERE ast.estado_reforzamiento='Falta' AND ast.fecha BETWEEN ? AND ?
        """
    else:
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
    q += " GROUP BY a.id ORDER BY total_faltas DESC"
    conn = get_db()
    df = pd.read_sql(q, conn, params=params)
    conn.close()
    
    if df.empty:
        st.info("Sin faltas en el período seleccionado.")
        return
    
    m1, m2, m3 = st.columns(3)
    m1.metric("Alumnos con faltas", len(df))
    m2.metric("Faltas justificadas", int(df["faltas_just"].sum()))
    m3.metric("Faltas injustificadas", int(df["faltas_injust"].sum()))
    
    st.markdown("---")
    col_a, col_b = st.columns(2)
    with col_a:
        top10 = df.head(10).copy()
        top10["nombre_completo"] = top10["apellidos"] + ", " + top10["nombres"]
        fig = px.bar(top10, x="total_faltas", y="nombre_completo", orientation="h",
                     color="total_faltas", color_continuous_scale="Reds",
                     title="Top 10 alumnos con más faltas")
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, use_container_width=True)
    with col_b:
        total_just = int(df["faltas_just"].sum())
        total_injust = int(df["faltas_injust"].sum())
        dist_df = pd.DataFrame({"Tipo": ["Justificadas", "Injustificadas"],
                                 "Cantidad": [total_just, total_injust]})
        fig = px.pie(dist_df, names="Tipo", values="Cantidad", hole=0.45,
                     color="Tipo", color_discrete_map={"Justificadas": "#28a745", "Injustificadas": "#dc3545"})
        st.plotly_chart(fig, use_container_width=True)
    
    st.markdown("---")
    st.subheader("Faltas por grado")
    por_grado = df.groupby("grado").agg(total=("total_faltas", "sum")).reset_index()
    fig = px.bar(por_grado, x="grado", y="total", color="total",
                 color_continuous_scale="Oranges", title="Faltas por grado")
    st.plotly_chart(fig, use_container_width=True)
    
    exportar_excel_pdf(df, f"Conteo de faltas - {periodo} - {tipo_asistencia}",
                       f"conteo_{periodo}_{tipo_asistencia.replace(' ', '_')}", key_prefix="conteo")


def _rep_cierre(grado_id, seccion_id, turno_sel):
    st.subheader("Cierre mensual de Asistencia")
    hoy_dt = ahora()
    c1, c2 = st.columns(2)
    with c1:
        mes = st.selectbox("Mes", list(range(1, 13)), index=hoy_dt.month - 1,
                          format_func=lambda m: MESES_ES[m], key="cie_mes")
    with c2:
        anio = st.number_input("Año", min_value=2020, max_value=2100, value=hoy_dt.year, key="cie_anio")
    from calendar import monthrange
    ultimo_dia = monthrange(anio, mes)[1]
    inicio = f"{anio:04d}-{mes:02d}-01"
    fin = f"{anio:04d}-{mes:02d}-{ultimo_dia:02d}"
    st.caption(f"Rango: {inicio} a {fin}")
    
    if st.button("Generar reporte", type="primary"):
        q = """
            SELECT g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
                   a.dni,
                   a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
                   a.nombres,
                   SUM(CASE WHEN ast.estado='Puntual' THEN 1 ELSE 0 END) AS puntuales,
                   SUM(CASE WHEN ast.estado='Falta' AND ast.justificada=1 THEN 1 ELSE 0 END) AS faltas_just,
                   SUM(CASE WHEN ast.estado='Falta' AND ast.justificada=0 THEN 1 ELSE 0 END) AS faltas_injust,
                   SUM(CASE WHEN ast.estado_reforzamiento='Puntual' THEN 1 ELSE 0 END) AS ref_puntuales,
                   SUM(CASE WHEN ast.estado_reforzamiento='Falta' THEN 1 ELSE 0 END) AS ref_faltas,
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
            st.dataframe(df, width="stretch")
            st.download_button("Bajar Excel", _df_to_xlsx(df, f"Cierre_{MESES_ES[mes]}_{anio}"),
                               f"cierre_{anio}_{mes:02d}.xlsx")

def vista_dias_especiales():
    st.title("Días especiales")
    
    if "_msg_dia_esp" in st.session_state:
        st.success(st.session_state["_msg_dia_esp"])
        del st.session_state["_msg_dia_esp"]
    
    tab1, tab2 = st.tabs(["Crear", "Listar / eliminar"])
    
    with tab1:
        st.markdown("### Crear día especial")
        st.caption("Configura feriados, eventos con horario especial o reforzamiento por sección.")
        _frag_crear_dia_especial()
    
    with tab2:
        _frag_listar_dias_especiales()


@st.fragment
def _frag_listar_dias_especiales():
    conn = get_db()
    df = pd.read_sql("""
        SELECT d.id, d.fecha, d.descripcion, COALESCE(t.nombre,'Ambos') AS turno,
               d.hora_entrada, COALESCE(d.tipo,'evento') AS tipo,
               COALESCE(d.tipo_reforzamiento, '') AS tipo_ref,
               COALESCE(d.hora_salida_reforzamiento, '') AS hora_salida_ref
        FROM dias_especiales d LEFT JOIN turnos t ON d.turno_id = t.id
        WHERE d.fecha >= date('now') ORDER BY d.fecha
    """, conn)
    df["secciones"] = ""
    for idx, row in df.iterrows():
        secs = pd.read_sql("""
            SELECT s.nombre AS sec_nombre, g.nombre AS gr_nombre
            FROM dias_especiales_secciones ds
            JOIN secciones s ON ds.seccion_id = s.id
            JOIN grados g ON s.grado_id = g.id
            WHERE ds.dia_especial_id = ?
        """, conn, params=[row["id"]])
        if not secs.empty:
            df.at[idx, "secciones"] = ", ".join([f"{r['gr_nombre']}{r['sec_nombre']}" for _, r in secs.iterrows()])
    conn.close()
    
    if df.empty:
        st.info("Sin días especiales programados.")
        return
    
    st.dataframe(df, width="stretch")
    
    op = {f"{r['fecha']} - {r['descripcion']} ({r['turno']}, {r['tipo']})": r["id"]
          for _, r in df.iterrows()}
    sel = st.selectbox("Eliminar", list(op.keys()), key="del_dia_esp")
    if st.button("Eliminar", type="primary", key="btn_del_dia_esp"):
        conn = get_db()
        conn.execute("DELETE FROM reforzamiento_alumnos WHERE dia_especial_id=?", (op[sel],))
        conn.execute("DELETE FROM dias_especiales_secciones WHERE dia_especial_id=?", (op[sel],))
        conn.execute("DELETE FROM dias_especiales WHERE id=?", (op[sel],))
        conn.commit()
        conn.close()
        st.session_state["_msg_dia_esp"] = "Día especial eliminado."
        st.cache_data.clear()
        st.rerun()


def _frag_crear_dia_especial():
    tipo_dia = st.radio("Tipo de día especial",
                        ["Feriado (sin clases)",
                         "Evento con horario especial (todo el turno)",
                         "Reforzamiento por sección"],
                        key="tipo_dia_esp")
    
    if tipo_dia == "Feriado (sin clases)":
        with st.form("dia_feriado"):
            col1, col2 = st.columns(2)
            with col1:
                f = st.date_input("Fecha", min_value=ahora().date())
                desc = st.text_input("Descripción", placeholder="Ej: Día del Maestro")
            ok = st.form_submit_button("Crear Feriado", type="primary", width="stretch")
            if ok:
                conn = get_db()
                try:
                    conn.execute("""INSERT OR REPLACE INTO dias_especiales
                                    (fecha, descripcion, turno_id, hora_entrada, activo, tipo)
                                    VALUES (?,?,NULL,'00:00',1,'feriado')""",
                                 (f.strftime("%Y-%m-%d"), desc))
                    conn.commit()
                    st.session_state["_msg_dia_esp"] = f"Feriado '{desc}' creado."
                    st.cache_data.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
                conn.close()
    
    elif tipo_dia == "Evento con horario especial (todo el turno)":
        with st.form("dia_evento"):
            col1, col2 = st.columns(2)
            with col1:
                f = st.date_input("Fecha", min_value=ahora().date())
                desc = st.text_input("Descripción", placeholder="Ej: Aniversario del colegio")
            with col2:
                t_opts = {"Ambos turnos": None}
                t_opts.update({t["nombre"]: t["id"] for t in turnos()})
                turno_lbl = st.selectbox("Aplica al turno", list(t_opts.keys()))
                h = st.time_input("Hora de entrada especial", value=datetime.strptime("08:00", "%H:%M").time())
            ok = st.form_submit_button("Crear Evento", type="primary", width="stretch")
            if ok:
                conn = get_db()
                try:
                    conn.execute("""INSERT OR REPLACE INTO dias_especiales
                                    (fecha, descripcion, turno_id, hora_entrada, activo, tipo)
                                    VALUES (?,?,?,?,1,'evento')""",
                                 (f.strftime("%Y-%m-%d"), desc, t_opts[turno_lbl], h.strftime("%H:%M")))
                    conn.commit()
                    st.session_state["_msg_dia_esp"] = f"Evento '{desc}' creado."
                    st.cache_data.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
                conn.close()
    
    else:
        st.markdown("**Configurar reforzamiento:**")
        st.info("Elige las secciones y la ventana de tiempo. Luego, en 'Seleccionar Alumnos "
                "Reforzamiento', asignas los alumnos. Los asignados que NO escaneen tendrán Falta.")
        
        st.markdown("**Selecciona las secciones que tienen reforzamiento:**")
        grados = grados_lista()
        secciones_sel = []
        if not grados:
            st.warning("No hay grados registrados.")
        else:
            for gr in grados:
                secs = secciones_por_grado(gr["id"])
                if secs:
                    st.markdown(f"**{gr['nombre']}**")
                    cols = st.columns(min(len(secs), 5))
                    for i, sec in enumerate(secs):
                        with cols[i % len(cols)]:
                            if st.checkbox(f"{gr['nombre']}{sec['nombre']}", key=f"sec_chk_{sec['id']}"):
                                secciones_sel.append(sec["id"])
        
        with st.form("dia_reforzamiento"):
            col1, col2 = st.columns(2)
            with col1:
                f = st.date_input("Fecha", min_value=ahora().date())
                desc = st.text_input("Descripción", value="Reforzamiento")
            with col2:
                turno_lbl = st.selectbox("Turno del reforzamiento",
                                        ["Mañana", "Tarde"], key="ref_turno")
                tipo_ref = st.radio("El reforzamiento es:",
                                    ["Antes de la entrada normal", "Después de la salida normal"],
                                    key="ref_tipo")
                h_ref = st.time_input("Hora de inicio",
                                     value=datetime.strptime("06:00" if "Antes" in st.session_state.get("ref_tipo", "Antes") else "12:20", "%H:%M").time(),
                                     key="ref_hora")
                h_salida_ref = st.time_input("Hora de fin",
                                            value=datetime.strptime("06:45" if "Antes" in st.session_state.get("ref_tipo", "Antes") else "14:00", "%H:%M").time(),
                                            key="ref_hora_salida")
                tolerancia_ref = st.number_input("Tolerancia (min)", min_value=0, max_value=60, value=7, key="ref_tol")
            ok = st.form_submit_button("Crear Reforzamiento", type="primary", width="stretch")
            
            if ok:
                if not secciones_sel:
                    st.error("Debes seleccionar al menos una sección.")
                else:
                    conn_check = get_db()
                    fecha_str = f.strftime("%Y-%m-%d")
                    secciones_duplicadas = []
                    for sec_id in secciones_sel:
                        ya_existe = conn_check.execute("""
                            SELECT ds.seccion_id
                            FROM dias_especiales d
                            JOIN dias_especiales_secciones ds ON d.id = ds.dia_especial_id
                            WHERE d.fecha=? AND d.activo=1 AND d.tipo='evento'
                              AND d.tipo_reforzamiento IS NOT NULL AND ds.seccion_id=?
                            LIMIT 1
                        """, (fecha_str, sec_id)).fetchone()
                        if ya_existe:
                            sec_info = conn_check.execute("""
                                SELECT g.nombre || s.nombre AS nombre_completo
                                FROM secciones s JOIN grados g ON s.grado_id = g.id
                                WHERE s.id=?
                            """, (sec_id,)).fetchone()
                            if sec_info:
                                secciones_duplicadas.append(sec_info["nombre_completo"])
                    conn_check.close()
                    
                    if secciones_duplicadas:
                        st.error(f"Ya existe un reforzamiento para esa fecha en: {', '.join(secciones_duplicadas)}")
                    else:
                        tipo_val = "antes" if "Antes" in tipo_ref else "despues"
                        conn = get_db()
                        try:
                            cursor = conn.cursor()
                            cursor.execute("""INSERT OR REPLACE INTO dias_especiales
                                              (fecha, descripcion, turno_id, hora_entrada, activo, tipo,
                                               tipo_reforzamiento, hora_salida_reforzamiento, tolerancia_reforzamiento)
                                              VALUES (?,?,?,?,1,'evento',?,?,?)""",
                                           (fecha_str, desc, None,
                                            h_ref.strftime("%H:%M"), tipo_val,
                                            h_salida_ref.strftime("%H:%M"), tolerancia_ref))
                            row = cursor.execute("""SELECT id FROM dias_especiales
                                                    WHERE fecha=? AND turno_id IS NULL""",
                                                 (fecha_str,)).fetchone()
                            if row:
                                dia_id = row["id"]
                                cursor.execute("DELETE FROM dias_especiales_secciones WHERE dia_especial_id=?", (dia_id,))
                                for sec_id in secciones_sel:
                                    cursor.execute("""INSERT INTO dias_especiales_secciones 
                                                      (dia_especial_id, seccion_id) VALUES (?,?)""",
                                                   (dia_id, sec_id))
                            conn.commit()
                            st.session_state["_msg_dia_esp"] = f"Reforzamiento creado para {len(secciones_sel)} secciones. Ahora asigna los alumnos."
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error: {e}")
                        conn.close()

def vista_seleccionar_alumnos_reforzamiento():
    st.title("Seleccionar Alumnos para Reforzamiento")
    
    if "_msg_ref_alumnos" in st.session_state:
        st.success(st.session_state["_msg_ref_alumnos"])
        del st.session_state["_msg_ref_alumnos"]
    
    conn = get_db()
    refs = pd.read_sql("""
        SELECT d.id, d.fecha, d.descripcion,
               d.hora_entrada, d.hora_salida_reforzamiento, d.tipo_reforzamiento,
               GROUP_CONCAT(DISTINCT g.nombre || s.nombre) AS secciones
        FROM dias_especiales d
        LEFT JOIN dias_especiales_secciones ds ON d.id = ds.dia_especial_id
        LEFT JOIN secciones s ON ds.seccion_id = s.id
        LEFT JOIN grados g ON s.grado_id = g.id
        WHERE d.fecha >= date('now') AND d.activo = 1
          AND d.tipo = 'evento' AND d.tipo_reforzamiento IS NOT NULL
        GROUP BY d.id ORDER BY d.fecha
    """, conn)
    conn.close()
    
    if refs.empty:
        st.info("No hay reforzamientos programados. Crea uno primero en Días Especiales.")
        return
    
    opciones = {f"{r['fecha']} - {r['descripcion']} ({r['secciones'] or 'sin secciones'})": r["id"]
                for _, r in refs.iterrows()}
    sel = st.selectbox("Selecciona el reforzamiento", list(opciones.keys()))
    ref_id = opciones[sel]
    
    ref_info = refs[refs["id"] == ref_id].iloc[0]
    st.markdown(f"**Fecha:** {ref_info['fecha']} | "
                f"**Secciones:** {ref_info['secciones']} | "
                f"**Hora:** {ref_info['hora_entrada']} - {ref_info['hora_salida_reforzamiento'] or '?'}")
    
    conn = get_db()
    secs_permitidas = conn.execute("""
        SELECT ds.seccion_id, g.nombre || s.nombre AS nombre_completo,
               g.id AS grado_id, s.id AS seccion_id
        FROM dias_especiales_secciones ds
        JOIN secciones s ON ds.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        WHERE ds.dia_especial_id = ?
    """, (ref_id,)).fetchall()
    conn.close()
    
    if not secs_permitidas:
        st.warning("Este reforzamiento no tiene secciones asignadas. Edítalo en Días Especiales.")
        return
    
    ids_secciones_permitidas = {r["seccion_id"] for r in secs_permitidas}
    nombres_secciones = [r["nombre_completo"] for r in secs_permitidas]
    
    st.info(f"⚠️ Solo puedes asignar alumnos de estas secciones: **{', '.join(nombres_secciones)}**")
    
    st.markdown("---")
    st.subheader("Seleccionar alumnos")
    st.caption("Marca los alumnos que van a este reforzamiento. "
               "Los asignados que NO escaneen tendrán Falta en reforzamiento.")
    
    grados_disponibles_ids = {r["grado_id"] for r in secs_permitidas}
    grados_all = grados_lista()
    grados_disponibles = [g for g in grados_all if g["id"] in grados_disponibles_ids]
    
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        opciones_grado = [{"id": None, "nombre": "Todos"}] + grados_disponibles
        grado_sel = st.selectbox("Grado", opciones_grado,
                                 format_func=lambda g: g["nombre"], key="ref_sel_grado")
    with c2:
        if grado_sel and grado_sel["id"]:
            secs_all = secciones_por_grado(grado_sel["id"])
            secs_filtradas = [s for s in secs_all if s["id"] in ids_secciones_permitidas]
            secs = [{"id": None, "nombre": "Todas"}] + secs_filtradas
        else:
            secs = [{"id": None, "nombre": "Todas"}] + [
                {"id": r["seccion_id"], "nombre": r["nombre_completo"]}
                for r in secs_permitidas
            ]
        seccion_sel = st.selectbox("Sección", secs,
                                   format_func=lambda s: s["nombre"], key="ref_sel_seccion")
    with c3:
        texto = st.text_input("Buscar por nombre", placeholder="Ej: Pérez", key="ref_sel_nombre")
    
    grado_id = grado_sel["id"] if grado_sel else None
    seccion_id = seccion_sel["id"] if (grado_sel and grado_sel["id"] and seccion_sel) else None
    texto = texto.strip()
    
    if not (grado_id or seccion_id or texto):
        st.info("Selecciona un grado, sección o escribe un nombre para ver los alumnos.")
        return
    
    conn = get_db()
    ya_sel = {r["alumno_id"] for r in conn.execute(
        "SELECT alumno_id FROM reforzamiento_alumnos WHERE dia_especial_id=?", (ref_id,)
    ).fetchall()}
    conn.close()
    
    df = buscar_alumnos_por_nombre(texto, grado_id, seccion_id, limite=500)
    if df.empty:
        st.info("Sin alumnos con esos filtros.")
        return
    
    df = df[df["seccion_id"].isin(ids_secciones_permitidas)]
    
    if df.empty:
        st.warning("No hay alumnos de las secciones permitidas que coincidan con los filtros.")
        return
    
    st.write(f"**{len(df)} alumnos** (de las secciones permitidas)")
    
    with st.form("form_sel_alumnos_ref"):
        seleccionados = []
        for _, al in df.iterrows():
            marcado = al["id"] in ya_sel
            if st.checkbox(f"{al['nombre_completo']} - {al['grado']}{al['seccion']} ({al['turno']})",
                           value=marcado, key=f"ref_chk_{al['id']}"):
                seleccionados.append(al["id"])
        
        guardar = st.form_submit_button("Guardar selección", type="primary", width="stretch")
    
    if guardar:
        conn = get_db()
        for al_id in seleccionados:
            sec_al = conn.execute(
                "SELECT seccion_id FROM alumnos WHERE id=?", (al_id,)
            ).fetchone()
            if not sec_al or sec_al["seccion_id"] not in ids_secciones_permitidas:
                conn.close()
                st.error(f"El alumno con ID {al_id} no pertenece a las secciones permitidas. Operación cancelada.")
                return
        conn.close()
        
        conn = get_db()
        try:
            conn.execute("DELETE FROM reforzamiento_alumnos WHERE dia_especial_id=?", (ref_id,))
            for al_id in seleccionados:
                conn.execute("INSERT INTO reforzamiento_alumnos (dia_especial_id, alumno_id) VALUES (?,?)",
                             (ref_id, al_id))
            conn.commit()
            st.session_state["_msg_ref_alumnos"] = f"{len(seleccionados)} alumnos asignados."
            st.cache_data.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Error: {e}")
        conn.close()

def vista_alumnos():
    st.title("Alumnos")
    
    dni_perfil = st.session_state.get("perfil_dni")
    if dni_perfil:
        _mostrar_perfil_completo(dni_perfil)
        return
    
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Importar Excel", "Listar", "Crear", "Editar", "Eliminar"])
    
    with tab1:
        _frag_importar_excel()
    with tab2:
        _frag_listar_alumnos()
    with tab3:
        _frag_crear_alumno()
    with tab4:
        _frag_editar_alumno()
    with tab5:
        _frag_eliminar_alumno()


@st.fragment
def _frag_importar_excel():
    st.info("Columnas: DNI, Nombres, Apellido Paterno, Apellido Materno, Grado, Sección, Turno.")
    
    if "_msg_import" in st.session_state:
        st.success(st.session_state["_msg_import"])
        del st.session_state["_msg_import"]
    
    f = st.file_uploader("Sube el Excel", type=["xlsx", "xls"], key="import_excel")
    if f:
        df = pd.read_excel(f)
        st.write(f"**{len(df)} filas** detectadas.")
        with st.form("mapeo"):
            cols = list(df.columns)
            c1, c2 = st.columns(2)
            with c1:
                m_dni = st.selectbox("DNI *", cols, key="map_dni")
                m_nom = st.selectbox("Nombres *", cols, key="map_nom")
                m_pat = st.selectbox("Apellido Paterno *", cols, key="map_pat")
            with c2:
                m_mat = st.selectbox("Apellido Materno", [""] + cols, key="map_mat")
                m_gra = st.selectbox("Grado *", cols, key="map_gra")
                m_sec = st.selectbox("Sección *", cols, key="map_sec")
                m_tur = st.selectbox("Turno *", cols, key="map_tur")
            c3, c4 = st.columns(2)
            with c3:
                m_apo_nom = st.selectbox("Nombre Apoderado", [""] + cols, key="map_apo_nom")
            with c4:
                m_apo_tel = st.selectbox("Teléfono Apoderado", [""] + cols, key="map_apo_tel")
            validar = st.form_submit_button("Validar", type="primary", width="stretch")
        
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
            c2.metric("Válidas", stats["validas"])
            c3.metric("Errores", stats["errores"])
            if st.session_state["_imp_errores"]:
                with st.expander("Errores"):
                    st.dataframe(pd.DataFrame(st.session_state["_imp_errores"]), width="stretch")
            if st.session_state["_imp_validas"]:
                if st.button("Importar SOLO válidas", type="primary", width="stretch"):
                    n = insertar_validas(st.session_state["_imp_validas"])
                    st.session_state["_msg_import"] = f"{n} alumnos importados correctamente."
                    for k in ["_imp_validas", "_imp_errores", "_imp_stats"]:
                        st.session_state.pop(k, None)
                    st.rerun()
        
        if "_imp_errores_insert" in st.session_state:
            with st.expander(f"{len(st.session_state['_imp_errores_insert'])} errores al insertar"):
                for e in st.session_state["_imp_errores_insert"][:50]:
                    st.write(f"- {e}")
            del st.session_state["_imp_errores_insert"]


@st.fragment
def _frag_listar_alumnos():
    st.caption("Haz clic en el nombre del alumno para ver su perfil completo.")
    grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
        key_prefix="al_list", placeholder_nombre="Ej: Torres"
    )
    df = buscar_alumnos_por_nombre(texto, grado_id, seccion_id, limite=5000)
    st.write(f"**{len(df)} alumnos**")
    if df.empty:
        st.info("No se encontraron alumnos con esos filtros.")
    else:
        mostrar_todos = st.checkbox("Mostrar todos", value=False, key="list_all")
        limite_mostrar = len(df) if mostrar_todos else 50
        for _, al in df.head(limite_mostrar).iterrows():
            if st.button(f"{al['nombre_completo']} - {al['grado']}{al['seccion']} ({al['turno']})",
                         key=f"perfil_btn_{al['id']}", width="stretch"):
                st.session_state["perfil_dni"] = al["dni"]
                st.rerun()


@st.fragment
def _frag_crear_alumno():
    st.subheader("Crear alumno manualmente")
    st.caption("Útil para alumnos que no aparecen en el Excel del colegio.")
    
    if "_msg_crear_alumno" in st.session_state:
        st.success(st.session_state["_msg_crear_alumno"])
        del st.session_state["_msg_crear_alumno"]
    
    grados = grados_lista()
    if not grados:
        st.warning("No hay grados registrados. Importa alumnos primero.")
        return
    
    with st.form("crear_alumno_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            nuevo_dni = st.text_input("DNI * (8 dígitos)", max_chars=8)
            nuevo_nombres = st.text_input("Nombres *")
            nuevo_ap_pat = st.text_input("Apellido Paterno *")
        with c2:
            nuevo_ap_mat = st.text_input("Apellido Materno")
            grado_sel = st.selectbox("Grado *", grados, format_func=lambda g: g["nombre"])
            secs = secciones_por_grado(grado_sel["id"]) if grado_sel else []
            if not secs:
                st.warning("Ese grado no tiene secciones.")
            seccion_sel = st.selectbox("Sección *", secs, format_func=lambda s: s["nombre"]) if secs else None
        c3, c4 = st.columns(2)
        with c3:
            nuevo_apo_nom = st.text_input("Nombre Apoderado (opcional)")
        with c4:
            nuevo_apo_tel = st.text_input("Teléfono Apoderado (opcional)")
        crear = st.form_submit_button("Crear Alumno", type="primary", width="stretch")
    
    if crear:
        if not nuevo_dni or not nuevo_nombres or not nuevo_ap_pat or not seccion_sel:
            st.error("Completa los campos obligatorios (*)")
        elif not re.fullmatch(r"\d{8}", nuevo_dni.strip()):
            st.error("DNI inválido (debe tener 8 dígitos)")
        else:
            conn = get_db()
            try:
                conn.execute("""INSERT INTO alumnos
                    (dni, nombres, apellido_paterno, apellido_materno, seccion_id,
                     nombre_apoderado, telefono_apoderado)
                    VALUES (?,?,?,?,?,?,?)""",
                    (nuevo_dni.strip(), nuevo_nombres.strip(), nuevo_ap_pat.strip(),
                     nuevo_ap_mat.strip() or None, seccion_sel["id"],
                     nuevo_apo_nom.strip() or None, nuevo_apo_tel.strip() or None))
                conn.commit()
                auditar(st.session_state.user["usuario"], f"Creó alumno manual DNI {nuevo_dni}")
                st.session_state["_msg_crear_alumno"] = f"Alumno {nuevo_nombres} {nuevo_ap_pat} creado correctamente."
                st.cache_data.clear()
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Ese DNI ya existe en el sistema.")
            except Exception as e:
                st.error(f"Error: {e}")
            finally:
                conn.close()


@st.fragment
def _frag_editar_alumno():
    st.caption("Solo se puede editar el apoderado, grado y sección. El nombre y DNI son inmutables.")
    
    if "_msg_editar_alumno" in st.session_state:
        st.success(st.session_state["_msg_editar_alumno"])
        del st.session_state["_msg_editar_alumno"]
    
    dni_edit = st.session_state.get("editar_dni")
    if dni_edit:
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
            st.info(f"**DNI:** {al['dni']} (no editable)")
            grados = grados_lista()
            
            if st.button("Cancelar edición", key="cancelar_edit"):
                st.session_state.pop("editar_dni", None)
                st.rerun()
            
            with st.form("edit_al"):
                st.text_input("DNI", value=al["dni"], disabled=True)
                st.text_input("Nombres", value=al["nombres"], disabled=True)
                st.text_input("Apellido Paterno", value=al["apellido_paterno"], disabled=True)
                st.text_input("Apellido Materno", value=al["apellido_materno"] or "", disabled=True)
                st.markdown("---")
                st.markdown("**Campos editables:**")
                apo_nom = st.text_input("Nombre Apoderado", value=al["nombre_apoderado"] or "")
                apo_tel = st.text_input("Teléfono Apoderado", value=al["telefono_apoderado"] or "")
                idx_grado = 0
                for i, g in enumerate(grados):
                    if g["nombre"] == al["grado"]:
                        idx_grado = i
                        break
                grado_nuevo = st.selectbox("Grado", grados, index=idx_grado, format_func=lambda g: g["nombre"])
                secs_nuevas = secciones_por_grado(grado_nuevo["id"]) if grado_nuevo else []
                idx_sec = 0
                for i, s in enumerate(secs_nuevas):
                    if s["nombre"] == al["seccion"] and s["id"] == al["seccion_id"]:
                        idx_sec = i
                        break
                seccion_nueva = st.selectbox("Sección", secs_nuevas, index=idx_sec,
                                             format_func=lambda s: s["nombre"]) if secs_nuevas else None
                guardar = st.form_submit_button("Guardar cambios", type="primary", width="stretch")
            
            if guardar:
                if not seccion_nueva:
                    st.error("Debes seleccionar una sección.")
                else:
                    conn = get_db()
                    conn.execute("""UPDATE alumnos SET nombre_apoderado=?, telefono_apoderado=?,
                                    seccion_id=? WHERE id=?""",
                                 (apo_nom or None, apo_tel or None, seccion_nueva["id"], al["id"]))
                    conn.commit()
                    conn.close()
                    auditar(st.session_state.user["usuario"], f"Editó alumno {dni_edit}")
                    st.session_state["_msg_editar_alumno"] = "Alumno actualizado correctamente."
                    st.cache_data.clear()
                    st.session_state.pop("editar_dni", None)
                    st.rerun()
    else:
        grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
            key_prefix="al_edit", placeholder_nombre="Ej: Flores"
        )
        if texto or grado_id:
            df = buscar_alumnos_por_nombre(texto, grado_id, seccion_id, limite=50)
            if df.empty:
                st.info("Sin coincidencias.")
            else:
                for _, al in df.iterrows():
                    if st.button(f"{al['nombre_completo']} - {al['grado']}{al['seccion']}",
                                 key=f"e_{al['id']}", width="stretch"):
                        st.session_state["editar_dni"] = al["dni"]
                        st.rerun()


@st.fragment
def _frag_eliminar_alumno():
    st.subheader("Eliminar alumno")
    st.warning("No se puede deshacer")
    
    if "_msg_eliminar_alumno" in st.session_state:
        st.success(st.session_state["_msg_eliminar_alumno"])
        del st.session_state["_msg_eliminar_alumno"]
    
    eliminar = st.session_state.get("eliminar_alumno")
    if eliminar:
        st.markdown("---")
        st.error(f"¿Está seguro que desea eliminar a **{eliminar['nombre']}** (DNI {eliminar['dni']})?")
        st.warning("Esta acción no se puede deshacer.")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("SÍ, ELIMINAR", type="primary", width="stretch", key="confirm_del"):
                conn = get_db()
                try:
                    conn.execute("DELETE FROM reforzamiento_alumnos WHERE alumno_id=?", (eliminar["id"],))
                    conn.execute("DELETE FROM asistencias WHERE alumno_id=?", (eliminar["id"],))
                    conn.execute("DELETE FROM tardanzas WHERE alumno_id=?", (eliminar["id"],))
                    conn.execute("DELETE FROM actas_compromiso WHERE alumno_id=?", (eliminar["id"],))
                    conn.execute("DELETE FROM observados WHERE alumno_id=?", (eliminar["id"],))
                    conn.execute("DELETE FROM alumnos WHERE id=?", (eliminar["id"],))
                    conn.commit()
                    auditar(st.session_state.user["usuario"], f"Eliminó alumno DNI {eliminar['dni']}")
                    st.session_state["_msg_eliminar_alumno"] = "Alumno eliminado correctamente."
                    st.cache_data.clear()
                    st.session_state.pop("eliminar_alumno", None)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
                finally:
                    conn.close()
        with c2:
            if st.button("Cancelar", width="stretch", key="cancel_del"):
                st.session_state.pop("eliminar_alumno", None)
                st.rerun()
    else:
        grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
            key_prefix="al_del", placeholder_nombre="Ej: Rojas"
        )
        if texto or grado_id:
            df = buscar_alumnos_por_nombre(texto, grado_id, seccion_id, limite=50)
            if df.empty:
                st.info("Sin coincidencias.")
            else:
                for _, al in df.iterrows():
                    if st.button(f"{al['nombre_completo']} - {al['grado']}{al['seccion']}",
                                 key=f"d_{al['id']}", width="stretch"):
                        st.session_state["eliminar_alumno"] = {
                            "id": al["id"], "nombre": al["nombre_completo"], "dni": al["dni"]
                        }
                        st.rerun()

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
                        st.download_button("Descargar", pdf,
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
                if st.button(f"{al['nombre_completo']} - {al['grado']}{al['seccion']}",
                             key=f"c_{al['id']}", width="stretch"):
                    st.session_state["carnet_dni"] = al["dni"]
        if "carnet_dni" in st.session_state:
            st.markdown("---")
            pdf = pdf_carnet_alumno(st.session_state["carnet_dni"])
            if pdf:
                st.download_button("Descargar carnet individual", pdf,
                                   f"carnet_{st.session_state['carnet_dni']}.pdf",
                                   "application/pdf")

def vista_usuarios():
    st.title("Usuarios")
    tab1, tab2, tab3 = st.tabs(["Listar", "Crear", "Editar / Eliminar"])
    
    with tab1:
        _frag_listar_usuarios()
    with tab2:
        _frag_crear_usuario()
    with tab3:
        _frag_editar_usuario()


@st.fragment
def _frag_listar_usuarios():
    if "_msg_crear_user" in st.session_state:
        st.success(st.session_state["_msg_crear_user"])
        del st.session_state["_msg_crear_user"]
    if "_msg_editar_user" in st.session_state:
        st.success(st.session_state["_msg_editar_user"])
        del st.session_state["_msg_editar_user"]
    if "_msg_eliminar_user" in st.session_state:
        st.success(st.session_state["_msg_eliminar_user"])
        del st.session_state["_msg_eliminar_user"]
    
    conn = get_db()
    df = pd.read_sql("""SELECT u.id, u.usuario, u.rol, u.nombres,
                               COALESCE(t.nombre,'-') AS turno, u.activo
                        FROM usuarios u LEFT JOIN turnos t ON u.turno_asignado = t.id
                        ORDER BY u.usuario""", conn)
    conn.close()
    st.dataframe(df, width="stretch")


@st.fragment
def _frag_crear_usuario():
    st.subheader("Crear nuevo usuario")
    
    with st.form("nuevo_user", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            u = st.text_input("Usuario *")
            p = st.text_input("Password * (mínimo 6)", type="password")
            n = st.text_input("Nombres *")
        with c2:
            rol = st.selectbox("Rol", ["Admin", "TOECE", "Auxiliar", "Direccion", "Docente Reforzamiento"])
            turno_sel = None
            if rol == "Auxiliar":
                t_opts = {t["nombre"]: t["id"] for t in turnos()}
                turno_lbl = st.selectbox("Turno asignado *", list(t_opts.keys()))
                turno_sel = t_opts[turno_lbl]
            elif rol == "Docente Reforzamiento":
                st.info("Este rol solo puede escanear QR en reforzamientos.")
            else:
                st.info("El turno solo aplica para el rol Auxiliar")
        crear = st.form_submit_button("Crear Usuario", type="primary", width="stretch")
    
    if crear:
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
                auditar(st.session_state.user["usuario"], f"Creó usuario {u} rol {rol}")
                st.session_state["_msg_crear_user"] = f"Usuario {u} creado como {rol}."
                st.cache_data.clear()
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Ese usuario ya existe.")
            except Exception as e:
                st.error(f"Error: {e}")
            finally:
                conn.close()


@st.fragment
def _frag_editar_usuario():
    st.subheader("Editar o eliminar usuario")
    
    conn = get_db()
    usuarios = pd.read_sql("""SELECT u.id, u.usuario, u.rol, u.nombres, u.turno_asignado,
                                     COALESCE(t.nombre,'-') AS turno, u.activo
                              FROM usuarios u LEFT JOIN turnos t ON u.turno_asignado = t.id
                              WHERE u.usuario != 'admin'
                              ORDER BY u.usuario""", conn)
    conn.close()
    
    if usuarios.empty:
        st.info("No hay usuarios para editar (además del admin).")
        return
    
    opciones = {f"{r['usuario']} ({r['rol']}) - {r['nombres']}": r["id"] for _, r in usuarios.iterrows()}
    sel = st.selectbox("Selecciona un usuario", list(opciones.keys()), key="edit_user_sel")
    user_id = opciones[sel]
    user_data = usuarios[usuarios["id"] == user_id].iloc[0]
    
    with st.form("editar_usuario"):
        c1, c2 = st.columns(2)
        with c1:
            edit_usuario = st.text_input("Usuario", value=user_data["usuario"])
            edit_password = st.text_input("Nueva contraseña (vacío = no cambiar)", type="password")
            edit_nombres = st.text_input("Nombres", value=user_data["nombres"])
        with c2:
            roles_disp = ["Admin", "TOECE", "Auxiliar", "Direccion", "Docente Reforzamiento"]
            idx_rol = roles_disp.index(user_data["rol"]) if user_data["rol"] in roles_disp else 0
            edit_rol = st.selectbox("Rol", roles_disp, index=idx_rol)
            edit_turno_sel = None
            if edit_rol == "Auxiliar":
                t_opts = {t["nombre"]: t["id"] for t in turnos()}
                turno_actual = user_data["turno"] if user_data["turno"] != "-" else "Mañana"
                idx_turno = list(t_opts.keys()).index(turno_actual) if turno_actual in t_opts else 0
                edit_turno_lbl = st.selectbox("Turno asignado", list(t_opts.keys()), index=idx_turno)
                edit_turno_sel = t_opts[edit_turno_lbl]
        c1, c2 = st.columns(2)
        with c1:
            guardar_user = st.form_submit_button("Guardar cambios", type="primary", width="stretch")
        with c2:
            eliminar_user = st.form_submit_button("Eliminar usuario", width="stretch")
    
    if guardar_user:
        conn = get_db()
        try:
            if edit_password:
                conn.execute("""UPDATE usuarios SET usuario=?, password=?, rol=?, nombres=?, turno_asignado=?
                                WHERE id=?""",
                             (edit_usuario, hashlib.sha256(edit_password.encode()).hexdigest(),
                              edit_rol, edit_nombres, edit_turno_sel, user_id))
            else:
                conn.execute("""UPDATE usuarios SET usuario=?, rol=?, nombres=?, turno_asignado=?
                                WHERE id=?""",
                             (edit_usuario, edit_rol, edit_nombres, edit_turno_sel, user_id))
            conn.commit()
            auditar(st.session_state.user["usuario"], f"Editó usuario {edit_usuario}")
            st.session_state["_msg_editar_user"] = "Usuario actualizado."
            st.cache_data.clear()
            st.rerun()
        except sqlite3.IntegrityError:
            st.error("Ese nombre de usuario ya existe.")
        except Exception as e:
            st.error(f"Error: {e}")
        finally:
            conn.close()
    
    if eliminar_user:
        st.warning(f"¿Eliminar al usuario **{user_data['usuario']}**?")
        if st.button("SÍ, ELIMINAR", type="primary", width="stretch", key="confirm_del_user"):
            conn = get_db()
            try:
                conn.execute("DELETE FROM usuarios WHERE id=?", (user_id,))
                conn.commit()
                auditar(st.session_state.user["usuario"], f"Eliminó usuario {user_data['usuario']}")
                st.session_state["_msg_eliminar_user"] = "Usuario eliminado."
                st.cache_data.clear()
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")
            finally:
                conn.close()

def vista_horarios():
    st.title("Horarios de turno")
    st.caption("La hora de entrada define desde cuándo se acepta QR y la tolerancia para Puntual/Tardanza. "
               "La hora de salida cierra el registro de clases y marca Faltas a los que no escanearon.")
    
    if "_msg_horario" in st.session_state:
        st.success(st.session_state["_msg_horario"])
        del st.session_state["_msg_horario"]
    
    for t in turnos():
        _frag_horario_turno(t)


@st.fragment
def _frag_horario_turno(t):
    st.subheader(f"{t['nombre']}")
    hora_limite_mostrada = suma_min(t["hora_entrada"], t["tolerancia_min"])
    st.caption(f"⏰ Entrada: **{t['hora_entrada']}** · "
               f"Límite puntual: **{hora_limite_mostrada}** (entrada + {t['tolerancia_min']} min) · "
               f"Salida: **{t['hora_salida']}**")
    with st.form(f"hor_{t['id']}"):
        c1, c2, c3 = st.columns(3)
        with c1:
            h_entrada = st.text_input("Hora entrada (HH:MM)", value=t["hora_entrada"])
        with c2:
            h_salida = st.text_input("Hora salida (HH:MM)", value=t["hora_salida"] or "17:00")
        with c3:
            tol = st.number_input("Tolerancia (min)", min_value=0, max_value=60, value=t["tolerancia_min"])
        ok = st.form_submit_button("Guardar", type="primary")
    
    if ok:
        conn = get_db()
        conn.execute("UPDATE turnos SET hora_entrada=?, hora_salida=?, tolerancia_min=? WHERE id=?",
                     (h_entrada, h_salida, tol, t["id"]))
        conn.commit()
        conn.close()
        st.session_state["_msg_horario"] = f"Horario de {t['nombre']} actualizado."
        st.cache_data.clear()
        st.rerun()

def vista_auditoria():
    st.title("Auditoría")
    st.caption("Se actualiza al instante.")
    
    c1, c2 = st.columns([1, 5])
    with c1:
        if st.button("Actualizar ahora", type="primary", width="stretch"):
            st.cache_data.clear()
            st.rerun()
    
    conn = get_db()
    df = pd.read_sql("SELECT * FROM auditoria ORDER BY id DESC LIMIT 200", conn)
    conn.close()
    st.write(f"**{len(df)} registros** (últimos 200)")
    st.dataframe(df, width="stretch")

def menu_lateral():
    user = st.session_state.user
    rol = user["rol"]
    
    with st.sidebar:
        st.markdown(f"###  {user['nombres']}")
        st.caption(f"Rol: **{rol}**")
        if user["turno_asignado"]:
            turno = next((t["nombre"] for t in turnos() if t["id"] == user["turno_asignado"]), "-")
            st.caption(f"Turno: **{turno}**")
        st.markdown("---")
        
        opciones_por_rol = {
            "Admin": ["Puerta", "TOECE", "Faltas", "Panel Dirección",
                      "Reportes y Consultas", "Alumnos", "Carnets",
                      "Días especiales", "Seleccionar Alumnos Reforzamiento",
                      "Horarios", "Usuarios", "Auditoría"],
            "TOECE": ["TOECE", "Faltas", "Panel Dirección",
                      "Reportes y Consultas", "Alumnos",
                      "Días especiales",
                      "Horarios"],
            "Auxiliar": ["Puerta", "Reportes y Consultas"],
            "Direccion": ["Panel Dirección", "Reportes y Consultas",
                          "Alumnos", "Días especiales"],
            "Docente Reforzamiento": ["Escanear Reforzamiento"],
        }
        opciones = opciones_por_rol.get(rol, [])
        
        if not opciones:
            st.error(f"Rol desconocido: {rol}")
            return None
        
        if "menu" not in st.session_state or st.session_state.menu not in opciones:
            st.session_state.menu = opciones[0]
        opcion = st.radio("Menú", opciones, key="menu")
        
        st.markdown("---")
        if st.button("Cerrar sesión", width="stretch"):
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
    if opcion is None:
        return
    vistas = {
        "Puerta":              vista_puerta,
        "TOECE":               vista_toece,
        "Faltas":              vista_faltas,
        "Panel Dirección":    vista_panel_direccion,
        "Reportes y Consultas": vista_reportes,
        "Alumnos":             vista_alumnos,
        "Carnets":             vista_carnets,
        "Días especiales":     vista_dias_especiales,
        "Seleccionar Alumnos Reforzamiento": vista_seleccionar_alumnos_reforzamiento,
        "Escanear Reforzamiento": vista_escanear_reforzamiento,
        "Horarios":            vista_horarios,
        "Usuarios":            vista_usuarios,
        "Auditoría":           vista_auditoria,
    }
    vistas.get(opcion, lambda: st.warning("Vista no disponible"))()


if __name__ == "__main__":
    main()
