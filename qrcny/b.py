# IMPORTACIONES
import hashlib
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
import extra_streamlit_components as stx
import numpy as np
import pandas as pd
import plotly.express as px
import qrcode
import streamlit as st
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# CONFIGURACIÓN
DB_PATH = "asistencia.db"

MESES_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

COLOR_PRIMARIO = "#E65100"
COLOR_PRIMARIO_HOVER = "#BF360C"

PBKDF2_ITERACIONES = 260_000
PBKDF2_ALGORITMO = "sha256"

DIAS_TOKEN_SESION = 30
COOKIE_KEY = "asistencia_ie_yarinacocha_token"
COOKIE_NOMBRE = "asistencia_token"
# UTILIDADES DE FECHA/HORA
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

# HASH DE CONTRASEÑAS (PBKDF2 con salt)
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITMO, password.encode("utf-8"), salt, PBKDF2_ITERACIONES
    )
    return f"pbkdf2_{PBKDF2_ALGORITMO}${PBKDF2_ITERACIONES}${salt.hex()}${dk.hex()}"

def verificar_password(password: str, hash_guardado: str) -> bool:
    if hash_guardado.startswith("pbkdf2_"):
        try:
            _, iteraciones, salt_hex, hash_hex = hash_guardado.split("$")
            iteraciones = int(iteraciones)
            salt = bytes.fromhex(salt_hex)
            dk = hashlib.pbkdf2_hmac(
                PBKDF2_ALGORITMO, password.encode("utf-8"), salt, iteraciones
            )
            return secrets.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    return secrets.compare_digest(
        hashlib.sha256(password.encode()).hexdigest(), hash_guardado
    )

def es_hash_antiguo(hash_guardado: str) -> bool:
    return not hash_guardado.startswith("pbkdf2_")

# BASE DE DATOS
_conn = None

def get_db():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS turnos (
        id INTEGER PRIMARY KEY, nombre TEXT UNIQUE,
        hora_entrada TEXT, hora_salida TEXT, tolerancia_min INTEGER DEFAULT 7,
        ref_hora_inicio TEXT, ref_hora_fin TEXT,
        ref_tolerancia INTEGER DEFAULT 7
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
        accion TEXT NOT NULL, observacion TEXT,
        registrado_por TEXT, timestamp TEXT NOT NULL,
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
        FOREIGN KEY (turno_id) REFERENCES turnos(id)
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
    CREATE TABLE IF NOT EXISTS sesiones_tokens (
        id INTEGER PRIMARY KEY,
        token TEXT UNIQUE NOT NULL,
        usuario_id INTEGER NOT NULL,
        expira TEXT NOT NULL,
        creado TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
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
    CREATE INDEX IF NOT EXISTS idx_token ON sesiones_tokens(token);
    """)
    # Índice único parcial para dias_especiales
    try:
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_dia_especial_unico
            ON dias_especiales(fecha, COALESCE(turno_id, -1))
        """)
    except sqlite3.OperationalError as e:
        print(f"[init_db] No se pudo crear índice único parcial: {e}")

    # --- Migraciones ---
    cols_turnos = [r["name"] for r in c.execute("PRAGMA table_info(turnos)").fetchall()]
    if "hora_salida" not in cols_turnos:
        c.execute("ALTER TABLE turnos ADD COLUMN hora_salida TEXT")
    if "ref_hora_inicio" not in cols_turnos:
        c.execute("ALTER TABLE turnos ADD COLUMN ref_hora_inicio TEXT")
    if "ref_hora_fin" not in cols_turnos:
        c.execute("ALTER TABLE turnos ADD COLUMN ref_hora_fin TEXT")
    if "ref_tolerancia" not in cols_turnos:
        c.execute("ALTER TABLE turnos ADD COLUMN ref_tolerancia INTEGER DEFAULT 7")

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

    # Valores por defecto de turnos
    c.execute("UPDATE turnos SET hora_salida='12:20' "
              "WHERE nombre='Mañana' AND (hora_salida IS NULL OR hora_salida='')")
    c.execute("UPDATE turnos SET hora_salida='17:00' "
              "WHERE nombre='Tarde' AND (hora_salida IS NULL OR hora_salida='')")
    c.execute("""UPDATE turnos
                 SET ref_hora_inicio='12:20', ref_hora_fin='14:00', ref_tolerancia=7
                 WHERE nombre='Mañana' AND (ref_hora_inicio IS NULL OR ref_hora_inicio='')""")
    c.execute("""UPDATE turnos
                 SET ref_hora_inicio='11:00', ref_hora_fin='12:20', ref_tolerancia=7
                 WHERE nombre='Tarde' AND (ref_hora_inicio IS NULL OR ref_hora_inicio='')""")

    if c.execute("SELECT COUNT(*) FROM turnos").fetchone()[0] == 0:
        c.execute("""INSERT INTO turnos
                     (nombre, hora_entrada, hora_salida, tolerancia_min,
                      ref_hora_inicio, ref_hora_fin, ref_tolerancia)
                     VALUES ('Mañana', '06:00', '08:00', 45, '12:20', '14:00', 7)""")
        c.execute("""INSERT INTO turnos
                     (nombre, hora_entrada, hora_salida, tolerancia_min,
                      ref_hora_inicio, ref_hora_fin, ref_tolerancia)
                     VALUES ('Tarde', '12:00', '14:00', 45, '11:00', '12:20', 7)""")

    if c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        c.executemany(
            "INSERT INTO usuarios (usuario, password, rol, nombres, turno_asignado) "
            "VALUES (?,?,?,?,?)",
            [
                ("admin",     hash_password("admin2026"),  "Admin",     "Administrador",      None),
                ("toece",     hash_password("toece2026"),  "TOECE",     "Coordinador TOECE",  None),
                ("direccion", hash_password("dir2026"),    "Direccion", "Dirección",          None),
                ("aux_m",     hash_password("auxm2026"),   "Auxiliar",  "Auxiliar Mañana",    1),
                ("aux_t",     hash_password("auxt2026"),   "Auxiliar",  "Auxiliar Tarde",     2),
            ]
        )
    conn.commit()
    
# TOKENS DE SESIÓN (COOKIES)
def get_cookie_manager():
    return stx.CookieManager(key=COOKIE_KEY)

def crear_token_sesion(user):
    token = secrets.token_urlsafe(32)
    conn = get_db()
    expira = (ahora() + timedelta(days=DIAS_TOKEN_SESION)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO sesiones_tokens (token, usuario_id, expira)
        VALUES (?,?,?)
    """, (token, user["id"], expira))
    conn.commit()
    return token

def restaurar_sesion_por_token(token):
    if not token:
        return None
    conn = get_db()
    row = conn.execute("""
        SELECT u.* FROM usuarios u
        JOIN sesiones_tokens st ON u.id = st.usuario_id
        WHERE st.token = ? AND st.expira > ? AND u.activo = 1
    """, (token, ahora().strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    if row:
        return dict(row)
    return None

def limpiar_token(token):
    if not token:
        return
    conn = get_db()
    conn.execute("DELETE FROM sesiones_tokens WHERE token = ?", (token,))
    conn.commit()

# AUTENTICACIÓN Y AUDITORÍA
def autenticar(usuario, password):
    conn = get_db()
    r = conn.execute(
        "SELECT * FROM usuarios WHERE usuario=? AND activo=1", (usuario,)
    ).fetchone()
    if not r:
        return None
    if not verificar_password(password, r["password"]):
        return None
    if es_hash_antiguo(r["password"]):
        try:
            conn.execute("UPDATE usuarios SET password=? WHERE id=?",
                         (hash_password(password), r["id"]))
            conn.commit()
        except Exception as e:
            print(f"[autenticar] Error migrando hash: {e}")
    return dict(r)

def auditar(usuario, accion):
    conn = get_db()
    conn.execute(
        "INSERT INTO auditoria (usuario, accion, fecha) VALUES (?,?,?)",
        (usuario, accion, ahora().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()


def listar_usuarios() -> pd.DataFrame:
    conn = get_db()
    return pd.read_sql("""
        SELECT u.id, u.usuario, u.rol, u.nombres, u.turno_asignado,
               COALESCE(t.nombre,'-') AS turno, u.activo
        FROM usuarios u LEFT JOIN turnos t ON u.turno_asignado = t.id
        ORDER BY u.usuario
    """, conn)

def crear_usuario(usuario, password, rol, nombres, turno_asignado=None):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO usuarios (usuario, password, rol, nombres, turno_asignado) "
            "VALUES (?,?,?,?,?)",
            (usuario, hash_password(password), rol, nombres, turno_asignado)
        )
        conn.commit()
        return True, f"Usuario {usuario} creado."
    except Exception as e:
        return False, str(e)


def actualizar_usuario(user_id, usuario, rol, nombres, turno_asignado,
                        nueva_password=None):
    conn = get_db()
    if nueva_password:
        conn.execute(
            "UPDATE usuarios SET usuario=?, password=?, rol=?, nombres=?, "
            "turno_asignado=? WHERE id=?",
            (usuario, hash_password(nueva_password), rol, nombres,
             turno_asignado, user_id)
        )
    else:
        conn.execute(
            "UPDATE usuarios SET usuario=?, rol=?, nombres=?, turno_asignado=? "
            "WHERE id=?",
            (usuario, rol, nombres, turno_asignado, user_id)
        )
    conn.commit()

def eliminar_usuario(user_id):
    conn = get_db()
    conn.execute("DELETE FROM sesiones_tokens WHERE usuario_id=?", (user_id,))
    conn.execute("DELETE FROM usuarios WHERE id=?", (user_id,))
    conn.commit()

# HORARIOS
def turnos():
    conn = get_db()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM turnos ORDER BY id"
    ).fetchall()]


def horario_del_dia(turno_id, fecha=None, seccion_id=None):
    fecha = fecha or hoy_str()
    conn = get_db()

    t = conn.execute("SELECT * FROM turnos WHERE id=?", (turno_id,)).fetchone()
    if not t:
        return {"hora_entrada": "08:00", "hora_limite": "08:07",
                "hora_salida": "13:00", "especial": False, "reforzamiento": None}

    horario_normal = {
        "hora_entrada": t["hora_entrada"],
        "hora_limite": suma_min(t["hora_entrada"], t["tolerancia_min"]),
        "hora_salida": t["hora_salida"] or "17:00",
    }

    refuerzo = None
    if seccion_id:
        ref_row = conn.execute("""
            SELECT d.id, d.tipo_reforzamiento, d.descripcion
            FROM dias_especiales d
            JOIN dias_especiales_secciones ds ON d.id = ds.dia_especial_id
            WHERE d.fecha=? AND d.activo=1 AND d.tipo='evento'
              AND d.tipo_reforzamiento IS NOT NULL AND ds.seccion_id=?
            LIMIT 1
        """, (fecha, seccion_id)).fetchone()

        if ref_row:
            refuerzo = {
                "id": ref_row["id"],
                "tipo_reforzamiento": ref_row["tipo_reforzamiento"],
                "descripcion": ref_row["descripcion"],
                "hora_reforzamiento": t["ref_hora_inicio"],
                "hora_salida_reforzamiento": t["ref_hora_fin"],
                "tolerancia_reforzamiento": t["ref_tolerancia"] or 7,
            }

    esp_general = conn.execute(
        "SELECT hora_entrada FROM dias_especiales "
        "WHERE fecha=? AND activo=1 AND tipo='evento' "
        "AND tipo_reforzamiento IS NULL AND (turno_id=? OR turno_id IS NULL) "
        "ORDER BY turno_id DESC LIMIT 1",
        (fecha, turno_id)
    ).fetchone()

    if refuerzo:
        return {**horario_normal, "reforzamiento": refuerzo, "especial": True}

    if esp_general:
        entrada = esp_general["hora_entrada"]
        return {
            "hora_entrada": entrada,
            "hora_limite": suma_min(entrada, t["tolerancia_min"]),
            "hora_salida": horario_normal["hora_salida"],
            "especial": True,
            "reforzamiento": None,
        }

    return {**horario_normal, "especial": False, "reforzamiento": None}

def detectar_tipo_escaneo(h, hora_actual_corta):
    hora_entrada = h["hora_entrada"]
    hora_salida = h["hora_salida"]
    refuerzo = h.get("reforzamiento")

    if not refuerzo:
        if hora_entrada <= hora_actual_corta <= hora_salida:
            return "clases"
        return "fuera"

    tipo_ref = refuerzo.get("tipo_reforzamiento")
    hora_inicio_ref = refuerzo.get("hora_reforzamiento")
    hora_fin_ref = refuerzo.get("hora_salida_reforzamiento") or "23:59"

    if tipo_ref == "antes":
        if hora_inicio_ref <= hora_actual_corta < hora_entrada:
            return "reforzamiento"
        if hora_entrada <= hora_actual_corta <= hora_salida:
            return "clases"
        return "fuera"
    elif tipo_ref == "despues":
        if hora_entrada <= hora_actual_corta < hora_salida:
            return "clases"
        if hora_salida <= hora_actual_corta <= hora_fin_ref:
            return "reforzamiento"
        return "fuera"
    return "fuera"

def calcular_estado_refuerzo(refuerzo, hora_actual_corta):
    hora_inicio = refuerzo.get("hora_reforzamiento")
    tolerancia = refuerzo.get("tolerancia_reforzamiento") or 7
    hora_limite = suma_min(hora_inicio, tolerancia)
    return "Puntual" if hora_actual_corta <= hora_limite else "Tardanza"


def actualizar_horario(turno_id, hora_entrada, hora_salida, tolerancia,
                        ref_inicio=None, ref_fin=None, ref_tol=None):
    conn = get_db()
    conn.execute(
        """UPDATE turnos SET hora_entrada=?, hora_salida=?, tolerancia_min=?,
           ref_hora_inicio=?, ref_hora_fin=?, ref_tolerancia=? WHERE id=?""",
        (hora_entrada, hora_salida, tolerancia,
         ref_inicio, ref_fin, ref_tol, turno_id)
    )
    conn.commit()
#################################################################################################################################################################3
# ASISTENCIA#############################################################################################################################
def alumno_en_reforzamiento(dia_especial_id, alumno_id):
    conn = get_db()
    r = conn.execute(
        "SELECT id FROM reforzamiento_alumnos WHERE dia_especial_id=? AND alumno_id=?",
        (dia_especial_id, alumno_id)
    ).fetchone()
    return r is not None

def _buscar_alumno_por_dni(conn, dni):
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
    return dict(al) if al else None

def _nombre_completo(al):
    return (f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, "
            f"{al['nombres']}").strip(", ")


def _validar_turno_auxiliar(al, usuario):
    if usuario["rol"] == "Auxiliar" and usuario["turno_asignado"]:
        if al["turno_id"] != usuario["turno_asignado"]:
            return f"Este alumno es del turno {al['turno']}. Tu turno es otro."
    return None

def _registrar_reforzamiento(conn, al, h, hora_actual, hora_actual_corta,
                              hoy, usuario):
    refuerzo = h["reforzamiento"]

    if not alumno_en_reforzamiento(refuerzo["id"], al["id"]):
        return False, "ERROR", (
            f"{al['nombres']} {al['apellido_paterno']} "
            f"NO está en la lista de este reforzamiento"
        ), {}

    existente = conn.execute(
        "SELECT * FROM asistencias WHERE alumno_id=? AND fecha=?",
        (al["id"], hoy)
    ).fetchone()

    if (existente and existente["hora_reforzamiento"]
            and existente["estado_reforzamiento"] != "Falta"):
        return False, "ERROR", (
            f"Ya registró reforzamiento hoy como "
            f"{existente['estado_reforzamiento']}"
        ), {}

    estado_ref = calcular_estado_refuerzo(refuerzo, hora_actual_corta)

    if existente:
        conn.execute(
            "UPDATE asistencias SET hora_reforzamiento=?, estado_reforzamiento=? "
            "WHERE id=?",
            (hora_actual, estado_ref, existente["id"])
        )
    else:
        conn.execute(
            "INSERT INTO asistencias (alumno_id, fecha, hora_reforzamiento, "
            "estado_reforzamiento) VALUES (?,?,?,?)",
            (al["id"], hoy, hora_actual, estado_ref)
        )
    conn.commit()
    auditar(usuario["usuario"], f"Reforzamiento {estado_ref} DNI {al['dni']}")
    return True, "REFORZAMIENTO", (
        f"{_nombre_completo(al)} | {al['grado']}{al['seccion']} | "
        f"REFORZAMIENTO {estado_ref} {hora_actual_corta}"
    ), al


def _registrar_puntual(conn, al, hora_actual, hoy, usuario):
    existente = conn.execute(
        "SELECT * FROM asistencias WHERE alumno_id=? AND fecha=?",
        (al["id"], hoy)
    ).fetchone()

    if (existente and existente["hora"]
            and existente["estado"] != "Falta"):
        return False, "ERROR", (
            f"Ya registrado hoy en clases como {existente['estado']}"
        ), {}

    if existente:
        conn.execute(
            "UPDATE asistencias SET hora=?, estado='Puntual' WHERE id=?",
            (hora_actual, existente["id"])
        )
    else:
        conn.execute(
            "INSERT INTO asistencias (alumno_id, fecha, hora, estado) "
            "VALUES (?,?,?,?)",
            (al["id"], hoy, hora_actual, "Puntual")
        )
    conn.commit()
    auditar(usuario["usuario"], f"Entrada PUNTUAL DNI {al['dni']}")
    return True, "PUNTUAL", (
        f"{_nombre_completo(al)} | {al['grado']}{al['seccion']} | "
        f"PUNTUAL {hora_corta()}"
    ), al

def _registrar_tardanza(conn, al, hora_actual, hora_actual_corta, hoy, usuario):
    existente = conn.execute(
        "SELECT * FROM asistencias WHERE alumno_id=? AND fecha=?",
        (al["id"], hoy)
    ).fetchone()

    if (existente and existente["hora"]
            and existente["estado"] != "Falta"):
        return False, "ERROR", (
            f"Ya registrado hoy en clases como {existente['estado']}"
        ), {}

    ult_acta = conn.execute(
        "SELECT fecha FROM actas_compromiso WHERE alumno_id=? "
        "ORDER BY fecha DESC LIMIT 1",
        (al["id"],)
    ).fetchone()
    desde = ult_acta["fecha"] if ult_acta else "1900-01-01"
    numero = conn.execute(
        "SELECT COUNT(*) FROM tardanzas WHERE alumno_id=? AND fecha > ?",
        (al["id"], desde)
    ).fetchone()[0] + 1

    if numero <= 2:
        accion = "PERDONADO"
        mensaje = (f"{_nombre_completo(al)} | Tardanza {numero}ª "
                   f"(perdonada) {hora_actual_corta}")
    elif numero == 3:
        accion = "DERIVADO_TOECE"
        mensaje = (f"{_nombre_completo(al)} | Tardanza 3ª -> "
                   f"DERIVAR A TOECE {hora_actual_corta}")
    else:
        accion = "RETENIDO_APODERADO"
        mensaje = (f"{_nombre_completo(al)} | Tardanza {numero}ª -> "
                   f"NO PASA hasta apoderado {hora_actual_corta}")

    if existente:
        conn.execute(
            "UPDATE asistencias SET hora=?, estado='Tardanza' WHERE id=?",
            (hora_actual, existente["id"])
        )
    else:
        conn.execute(
            "INSERT INTO asistencias (alumno_id, fecha, hora, estado) "
            "VALUES (?,?,?,?)",
            (al["id"], hoy, hora_actual, "Tardanza")
        )

    conn.execute(
        "INSERT INTO tardanzas (alumno_id, fecha, hora, numero, accion, "
        "registrado_por, timestamp) VALUES (?,?,?,?,?,?,?)",
        (al["id"], hoy, hora_actual, numero, accion, usuario["usuario"],
         ahora().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    auditar(usuario["usuario"],
            f"Tardanza {numero}ª DNI {al['dni']} -> {accion}")
    return True, "TARDANZA", mensaje, {**al, "numero": numero, "accion": accion}

def registrar_entrada(dni, usuario):
    dni = (dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni):
        return False, "ERROR", "DNI inválido (debe tener 8 dígitos)", {}

    conn = get_db()
    al = _buscar_alumno_por_dni(conn, dni)
    if not al:
        return False, "ERROR", "DNI no encontrado", {}

    error_turno = _validar_turno_auxiliar(al, usuario)
    if error_turno:
        return False, "ERROR", error_turno, {}

    hoy = hoy_str()
    hora_actual = hora_str()
    hora_actual_corta = hora_corta()
    h = horario_del_dia(al["turno_id"], hoy, al["seccion_id"])
    tipo_escaneo = detectar_tipo_escaneo(h, hora_actual_corta)

    if tipo_escaneo == "fuera":
        if h.get("reforzamiento"):
            ref = h["reforzamiento"]
            return False, "ERROR", (
                f"Puerta cerrada. Ventanas hoy: "
                f"clases {h['hora_entrada']}-{h['hora_salida']}, "
                f"reforzamiento {ref['hora_reforzamiento']}-"
                f"{ref.get('hora_salida_reforzamiento') or '?'}"
            ), {}
        return False, "ERROR", (
            f"Puerta cerrada. Ventana de clases: "
            f"{h['hora_entrada']}-{h['hora_salida']}"
        ), {}

    if tipo_escaneo == "reforzamiento":
        return _registrar_reforzamiento(
            conn, al, h, hora_actual, hora_actual_corta, hoy, usuario
        )

    if hora_actual_corta <= h["hora_limite"]:
        return _registrar_puntual(conn, al, hora_actual, hoy, usuario)

    return _registrar_tardanza(
        conn, al, hora_actual, hora_actual_corta, hoy, usuario
    )

def marcar_faltas_al_cierre():
    hoy = hoy_str()
    hora_actual_corta = hora_corta()
    conn = get_db()

    if not es_dia_laboral():
        esp = conn.execute(
            "SELECT tipo FROM dias_especiales WHERE fecha=? AND activo=1 "
            "AND tipo='evento'",
            (hoy,)
        ).fetchone()
        if not esp:
            return

    for t in turnos():
        h = horario_del_dia(t["id"], hoy)
        if hora_actual_corta < h["hora_salida"]:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO asistencias (alumno_id, fecha, hora, estado) "
            "SELECT a.id, ?, ?, 'Falta' FROM alumnos a "
            "JOIN secciones s ON a.seccion_id = s.id "
            "WHERE s.turno_id = ?",
            (hoy, hora_str(), t["id"])
        )

    reforzamientos = conn.execute("""
        SELECT d.id AS dia_id, d.tipo_reforzamiento,
               t.ref_hora_fin AS hora_fin_ref
        FROM dias_especiales d
        JOIN dias_especiales_secciones ds ON d.id = ds.dia_especial_id
        JOIN secciones s ON ds.seccion_id = s.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE d.fecha=? AND d.activo=1 AND d.tipo='evento'
          AND d.tipo_reforzamiento IS NOT NULL
        GROUP BY d.id
    """, (hoy,)).fetchall()

    for ref in reforzamientos:
        hora_fin_ref = ref["hora_fin_ref"] or "23:59"
        if hora_actual_corta < hora_fin_ref:
            continue

        conn.execute("""
            UPDATE asistencias
            SET estado_reforzamiento='Falta', hora_reforzamiento=?
            WHERE fecha=? AND estado_reforzamiento IS NULL
              AND alumno_id IN (
                  SELECT alumno_id FROM reforzamiento_alumnos
                  WHERE dia_especial_id=?
              )
        """, (hora_str(), hoy, ref["dia_id"]))

        conn.execute("""
            INSERT OR IGNORE INTO asistencias
            (alumno_id, fecha, hora_reforzamiento, estado_reforzamiento)
            SELECT ra.alumno_id, ?, ?, 'Falta'
            FROM reforzamiento_alumnos ra
            WHERE ra.dia_especial_id=?
              AND ra.alumno_id NOT IN (
                  SELECT alumno_id FROM asistencias WHERE fecha=?
              )
        """, (hoy, hora_str(), ref["dia_id"], hoy))

    conn.commit()

def justificar_falta(alumno_id, justificada, observacion, usuario):
    conn = get_db()
    al = conn.execute(
        "SELECT apellido_paterno, apellido_materno, nombres FROM alumnos WHERE id=?",
        (alumno_id,)
    ).fetchone()
    if not al:
        return False, "Alumno no encontrado"

    falta = conn.execute(
        "SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND estado='Falta'",
        (alumno_id, hoy_str())
    ).fetchone()
    if not falta:
        return False, "Ese alumno no tiene una Falta registrada hoy"

    conn.execute(
        "UPDATE asistencias SET justificada=?, observacion=? WHERE id=?",
        (1 if justificada else 0, observacion, falta["id"])
    )
    conn.commit()
    nombre = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}".strip(", ")
    estado_txt = "JUSTIFICADA" if justificada else "INJUSTIFICADA"
    auditar(usuario["usuario"], f"Falta de alumno_id={alumno_id} -> {estado_txt}")
    return True, f"Falta de {nombre} marcada como {estado_txt}"


def editar_estado_asistencia(alumno_id, nuevo_estado, observacion, usuario):
    if usuario["rol"] != "Admin":
        return False, "Solo el Admin puede editar asistencias"

    conn = get_db()
    al = conn.execute(
        "SELECT apellido_paterno, apellido_materno, nombres FROM alumnos WHERE id=?",
        (alumno_id,)
    ).fetchone()
    if not al:
        return False, "Alumno no encontrado"

    hoy = hoy_str()
    asist = conn.execute(
        "SELECT id, estado FROM asistencias WHERE alumno_id=? AND fecha=?",
        (alumno_id, hoy)
    ).fetchone()
    if asist:
        conn.execute(
            "UPDATE asistencias SET estado=?, observacion=? WHERE id=?",
            (nuevo_estado, observacion, asist["id"])
        )
        accion_txt = f"Editó asistencia: {asist['estado']} -> {nuevo_estado}"
    else:
        conn.execute(
            "INSERT INTO asistencias (alumno_id, fecha, hora, estado, observacion) "
            "VALUES (?,?,?,?,?)",
            (alumno_id, hoy, hora_str(), nuevo_estado, observacion)
        )
        accion_txt = f"Creó asistencia manual: {nuevo_estado}"
    conn.commit()
    nombre = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}".strip(", ")
    auditar(usuario["usuario"], f"{accion_txt} alumno_id={alumno_id}")
    return True, f"Asistencia de {nombre} actualizada a {nuevo_estado}"


def registrar_acta(alumno_id, motivo, observacion, usuario):
    conn = get_db()
    ts = ahora().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO actas_compromiso (alumno_id, fecha, motivo, observacion, "
        "registrado_por, timestamp) VALUES (?,?,?,?,?,?)",
        (alumno_id, hoy_str(), motivo, observacion, usuario["usuario"], ts)
    )
    ya = conn.execute(
        "SELECT id FROM observados WHERE alumno_id=? AND activo=1",
        (alumno_id,)
    ).fetchone()
    if not ya:
        conn.execute(
            "INSERT INTO observados (alumno_id, fecha_ingreso, motivo, activo) "
            "VALUES (?,?,?,1)",
            (alumno_id, hoy_str(), motivo or "Acta de compromiso")
        )
    conn.commit()
    auditar(usuario["usuario"], f"Acta firmada alumno_id={alumno_id}")


def liberar_observado(alumno_id, usuario):
    conn = get_db()
    conn.execute(
        "UPDATE observados SET activo=0, fecha_salida=? "
        "WHERE alumno_id=? AND activo=1",
        (hoy_str(), alumno_id)
    )
    conn.commit()
    auditar(usuario["usuario"], f"Liberó observado alumno_id={alumno_id}")

# ALUMNOS###################################################################################################################################################
###############################################################################################################################################33333
def grados_lista():
    conn = get_db()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM grados ORDER BY nombre"
    ).fetchall()]


def secciones_por_grado(grado_id):
    conn = get_db()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM secciones WHERE grado_id=? ORDER BY nombre",
        (grado_id,)
    ).fetchall()]


def alumnos_de_seccion(seccion_id):
    conn = get_db()
    return pd.read_sql("""
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'')
               || ', ' || a.nombres AS nombre_completo
        FROM alumnos a WHERE a.seccion_id = ?
        ORDER BY a.apellido_paterno, a.apellido_materno, a.nombres
    """, conn, params=[seccion_id])


def buscar_alumnos_por_nombre(texto, grado_id=None, seccion_id=None, limite=200):
    conn = get_db()
    q = """
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               g.id AS grado_id, g.nombre AS grado,
               s.id AS seccion_id, s.nombre AS seccion,
               t.nombre AS turno,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'')
               || ', ' || a.nombres AS nombre_completo
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE 1=1
    """
    params = []
    if texto:
        for palabra in [p.strip() for p in texto.split() if p.strip()]:
            q += """ AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ?
                          OR a.apellido_materno LIKE ?
                          OR (a.apellido_paterno || ' ' ||
                              COALESCE(a.apellido_materno,'') || ' ' ||
                              a.nombres) LIKE ?)"""
            like = f"%{palabra}%"
            params += [like, like, like, like]
    if grado_id:
        q += " AND g.id = ?"; params.append(grado_id)
    if seccion_id:
        q += " AND s.id = ?"; params.append(seccion_id)
    q += " ORDER BY a.apellido_paterno, a.apellido_materno, a.nombres LIMIT ?"
    params.append(limite)
    return pd.read_sql(q, conn, params=params)


def estado_asistencia_hoy(alumno_ids):
    if not alumno_ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(alumno_ids))
    rows = conn.execute(
        f"SELECT alumno_id, estado, estado_reforzamiento FROM asistencias "
        f"WHERE fecha=? AND alumno_id IN ({placeholders})",
        [hoy_str()] + list(alumno_ids)
    ).fetchall()
    return {r["alumno_id"]: {"estado": r["estado"],
                              "estado_ref": r["estado_reforzamiento"]}
            for r in rows}


def validar_importacion(df, mapeo):
    errores, validas = [], []
    conn = get_db()
    dnis_bd = {r["dni"] for r in conn.execute("SELECT dni FROM alumnos").fetchall()}
    dnis_en_excel = {}

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
            validas.append({
                "dni": dni, "nombres": nom, "apellido_paterno": pat,
                "apellido_materno": mat, "grado": gra, "seccion": sec,
                "turno": turno_nombre, "apoderado_nombre": apo_nom,
                "apoderado_telefono": apo_tel, "fila": fila_num,
            })
        except Exception as e:
            errores.append({"fila": fila_num, "motivo": f"Error: {e}"})

    return validas, errores, {"total": len(df), "validas": len(validas),
                               "errores": len(errores)}

def insertar_validas(validas):
    conn = get_db()
    c = conn.cursor()
    turnos_map = {r["nombre"]: r["id"]
                  for r in c.execute("SELECT id, nombre FROM turnos").fetchall()}
    insertados = 0
    errores = []

    for idx, v in enumerate(validas):
        try:
            g = c.execute("SELECT id FROM grados WHERE nombre=?",
                          (v["grado"],)).fetchone()
            g_id = g["id"] if g else c.execute(
                "INSERT INTO grados (nombre) VALUES (?)", (v["grado"],)
            ).lastrowid
            t_id = turnos_map.get(v["turno"])
            if not t_id:
                errores.append(f"Fila {idx+1}: Turno '{v['turno']}' no encontrado")
                continue
            s = c.execute(
                "SELECT id FROM secciones WHERE nombre=? AND grado_id=? AND turno_id=?",
                (v["seccion"], g_id, t_id)
            ).fetchone()
            s_id = s["id"] if s else c.execute(
                "INSERT INTO secciones (nombre, grado_id, turno_id) VALUES (?,?,?)",
                (v["seccion"], g_id, t_id)
            ).lastrowid
            c.execute("""
                INSERT OR IGNORE INTO alumnos
                (dni, nombres, apellido_paterno, apellido_materno, seccion_id,
                 nombre_apoderado, telefono_apoderado)
                VALUES (?,?,?,?,?,?,?)
            """, (v["dni"], v["nombres"], v["apellido_paterno"],
                  v["apellido_materno"], s_id,
                  v["apoderado_nombre"] or None,
                  v["apoderado_telefono"] or None))
            if c.rowcount > 0:
                insertados += 1
        except Exception as e:
            errores.append(f"Fila {idx+1}: {str(e)}")

    conn.commit()
    return insertados, errores
#crear al alumno##########################################################################################################3
def crear_alumno(dni, nombres, ap_pat, ap_mat, seccion_id,
                  apo_nom, apo_tel, usuario):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO alumnos
            (dni, nombres, apellido_paterno, apellido_materno, seccion_id,
             nombre_apoderado, telefono_apoderado)
            VALUES (?,?,?,?,?,?,?)
        """, (dni, nombres, ap_pat, ap_mat or None, seccion_id,
              apo_nom or None, apo_tel or None))
        conn.commit()
        auditar(usuario["usuario"], f"Creó alumno manual DNI {dni}")
        return True, f"Alumno {nombres} {ap_pat} creado correctamente."
    except Exception as e:
        return False, str(e)

def editar_alumno(alumno_id, apo_nom, apo_tel, seccion_id,
                   dni_original, usuario):
    conn = get_db()
    conn.execute("""
        UPDATE alumnos SET nombre_apoderado=?, telefono_apoderado=?,
        seccion_id=? WHERE id=?
    """, (apo_nom or None, apo_tel or None, seccion_id, alumno_id))
    conn.commit()
    auditar(usuario["usuario"], f"Editó alumno {dni_original}")
    return True, "Alumno actualizado correctamente."


def eliminar_alumno(alumno_id, dni, usuario):
    conn = get_db()
    try:
        conn.execute("DELETE FROM reforzamiento_alumnos WHERE alumno_id=?",
                     (alumno_id,))
        conn.execute("DELETE FROM asistencias WHERE alumno_id=?", (alumno_id,))
        conn.execute("DELETE FROM tardanzas WHERE alumno_id=?", (alumno_id,))
        conn.execute("DELETE FROM actas_compromiso WHERE alumno_id=?",
                     (alumno_id,))
        conn.execute("DELETE FROM observados WHERE alumno_id=?", (alumno_id,))
        conn.execute("DELETE FROM alumnos WHERE id=?", (alumno_id,))
        conn.commit()
        auditar(usuario["usuario"], f"Eliminó alumno DNI {dni}")
        return True, "Alumno eliminado correctamente."
    except Exception as e:
        return False, str(e)

# REPORTES E INFORMES
def metricas_dia(fecha):
    conn = get_db()
    sql = """
        SELECT
          (SELECT COUNT(*) FROM alumnos) AS total,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado='Puntual') AS puntuales,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado='Falta') AS faltas,
          (SELECT COUNT(*) FROM tardanzas WHERE fecha=?) AS tardanzas,
          (SELECT COUNT(*) FROM tardanzas WHERE fecha=? AND accion='DERIVADO_TOECE') AS derivados,
          (SELECT COUNT(*) FROM tardanzas WHERE fecha=? AND accion='RETENIDO_APODERADO') AS retenidos,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado_reforzamiento='Puntual') AS puntuales_ref,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado_reforzamiento='Tardanza') AS tardanzas_ref,
          (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado_reforzamiento='Falta') AS faltas_ref
    """
    n = sql.count("?")
    r = conn.execute(sql, (fecha,) * n).fetchone()
    return dict(r)


def ultimos_registros(fecha, limite=20):
    conn = get_db()
    return pd.read_sql("""
        SELECT a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
               a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               ast.hora, ast.estado, ast.hora_reforzamiento, ast.estado_reforzamiento
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ast.fecha = ?
          AND (ast.hora IS NOT NULL OR ast.hora_reforzamiento IS NOT NULL)
        ORDER BY COALESCE(ast.hora_reforzamiento, ast.hora) DESC LIMIT ?
    """, conn, params=[fecha, limite])

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
    return pd.read_sql(q, conn)


def _aplicar_filtros_comunes(q, params, turno_sel, grado_id, seccion_id, texto):
    if turno_sel != "Todos":
        q += " AND t.nombre = ?"; params.append(turno_sel)
    if grado_id:
        q += " AND g.id = ?"; params.append(grado_id)
    if seccion_id:
        q += " AND s.id = ?"; params.append(seccion_id)
    if texto:
        for palabra in [p.strip() for p in texto.split() if p.strip()]:
            q += (" AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? "
                  "OR a.apellido_materno LIKE ?)")
            like = f"%{palabra}%"
            params += [like, like, like]
    return q, params


def reporte_detalle(ini, fin, turno_sel, grado_id, seccion_id, texto,
                     tipo_asistencia):
    q = """
        SELECT a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
               a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               ast.fecha, ast.hora, ast.estado, ast.justificada,
               ast.hora_reforzamiento, ast.estado_reforzamiento,
               ast.justificada_reforzamiento
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ast.fecha BETWEEN ? AND ?
    """
    params = [ini.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")]
    q, params = _aplicar_filtros_comunes(q, params, turno_sel, grado_id,
                                          seccion_id, texto)
    q += " ORDER BY ast.fecha DESC, t.nombre, g.nombre, s.nombre"

    conn = get_db()
    df = pd.read_sql(q, conn, params=params)

    if tipo_asistencia == "Clases normales":
        df = df[df["hora"].notna()]
    elif tipo_asistencia == "Reforzamiento":
        df = df[df["hora_reforzamiento"].notna()]

    return df


def reporte_conteo_faltas(ini, fin, turno_sel, grado_id, seccion_id,
                            texto, tipo_asistencia):
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
    q, params = _aplicar_filtros_comunes(q, params, turno_sel, grado_id,
                                          seccion_id, texto)
    q += " GROUP BY a.id ORDER BY total_faltas DESC"

    conn = get_db()
    return pd.read_sql(q, conn, params=params)


def cierre_mensual(mes, anio, turno_sel, grado_id, seccion_id):
    from calendar import monthrange
    ultimo_dia = monthrange(anio, mes)[1]
    inicio = f"{anio:04d}-{mes:02d}-01"
    fin = f"{anio:04d}-{mes:02d}-{ultimo_dia:02d}"

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
    q, params = _aplicar_filtros_comunes(q, params, turno_sel, grado_id,
                                          seccion_id, "")
    q += (" GROUP BY g.nombre, s.nombre, t.nombre, a.id "
          "ORDER BY t.nombre, g.nombre, s.nombre, a.apellido_paterno")

    conn = get_db()
    return pd.read_sql(q, conn, params=params)


def obtener_auditoria(limite=200):
    conn = get_db()
    return pd.read_sql(
        f"SELECT * FROM auditoria ORDER BY id DESC LIMIT {int(limite)}", conn
    )

# escnaer el qr####################################################################################################################################3
############################################################################################################################################################33
########################################################################################################################################33
def qr_de_dni(dni):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(str(dni).strip())
    qr.make(fit=True)
    return qr.make_image(fill_color="black",
                          back_color="white").convert("RGB")

def leer_qr(img):
    try:
        import cv2
        arr = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        detector = cv2.QRCodeDetector()

        variantes = [gray]
        for escala in (1.5, 2.0, 3.0):
            variantes.append(
                cv2.resize(gray, None, fx=escala, fy=escala,
                            interpolation=cv2.INTER_CUBIC)
            )
        for base in list(variantes):
            th = cv2.adaptiveThreshold(
                base, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 31, 5
            )
            variantes.append(th)
        for base in list(variantes[:4]):
            _, otsu = cv2.threshold(base, 0, 255,
                                     cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            variantes.append(otsu)

        for v in variantes:
            data, _, _ = detector.detectAndDecode(v)
            if data:
                m = re.search(r"\b(\d{8})\b", data)
                return m.group(1) if m else data.strip()
    except Exception as e:
        print(f"[leer_qr] Error: {e}")
    return None

# PDF para generar los reportes #######################################################################################################################################################33
#############################################################################################################################################################3333

def _pdf_base(titulo, subtitulo=None, paisaje=False):
    buf = BytesIO()
    size = A4 if not paisaje else (A4[1], A4[0])
    doc = SimpleDocTemplate(buf, pagesize=size, rightMargin=25, leftMargin=25,
                            topMargin=25, bottomMargin=25)
    estilos = getSampleStyleSheet()
    el = [Paragraph(f"<b>{titulo}</b>", estilos["Heading1"])]
    if subtitulo:
        el.append(Paragraph(subtitulo, estilos["Normal"]))
    el.append(Paragraph(f"Generado: {ahora().strftime('%Y-%m-%d %H:%M')}",
                         estilos["Normal"]))
    el.append(Spacer(1, 15))
    return buf, doc, el, estilos

def pdf_tabla(df, titulo, subtitulo=None):
    buf, doc, el, estilos = _pdf_base(titulo, subtitulo,
                                        paisaje=len(df.columns) > 6)
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
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=MARGEN,
                            leftMargin=MARGEN, topMargin=MARGEN, bottomMargin=MARGEN)
    estilos = getSampleStyleSheet()

    if len(rows) > 1:
        titulo = (f"Carnets - {rows[0]['grado']}{rows[0]['seccion']} "
                  f"({rows[0]['turno']})")
    else:
        titulo = (f"Carnet - {rows[0]['apellido_paterno']} "
                  f"{rows[0]['apellido_materno'] or ''}, {rows[0]['nombres']}")

    el = [Paragraph(titulo, estilos["Heading1"]), Spacer(1, 6)]

    COLUMNAS = 3
    FILAS = 3
    POR_PAGINA = COLUMNAS * FILAS
    ancho_util = A4[0] - (2 * MARGEN)
    alto_util = A4[1] - (2 * MARGEN)
    alto_disponible = alto_util - 50
    ancho_carnet = ancho_util / COLUMNAS
    alto_carnet = alto_disponible / FILAS

    for i in range(0, len(rows), POR_PAGINA):
        lote = rows[i:i + POR_PAGINA]
        tabla = []
        for j in range(0, len(lote), COLUMNAS):
            fila = []
            for a in lote[j:j + COLUMNAS]:
                qb = BytesIO()
                qr_de_dni(a["dni"]).save(qb, format="PNG")
                qb.seek(0)
                celda = [
                    Paragraph(
                        f"<b><font size=11>{a['apellido_paterno']} "
                        f"{a['apellido_materno'] or ''}</font></b>",
                        estilos["Normal"]
                    ),
                    Paragraph(f"<font size=10>{a['nombres']}</font>",
                              estilos["Normal"]),
                    Paragraph(f"<font size=10>DNI: {a['dni']}</font>",
                              estilos["Normal"]),
                    Paragraph(
                        f"<font size=9>{a['grado']}{a['seccion']} - "
                        f"{a['turno']}</font>",
                        estilos["Normal"]
                    ),
                    RLImage(qb, width=150, height=150),
                ]
                fila.append(celda)
            while len(fila) < COLUMNAS:
                fila.append([])
            tabla.append(fila)
        while len(tabla) < FILAS:
            tabla.append([[] for _ in range(COLUMNAS)])

        t = Table(tabla, colWidths=[ancho_carnet] * COLUMNAS,
                  rowHeights=[alto_carnet] * FILAS)
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
        if i + POR_PAGINA < len(rows):
            el.append(PageBreak())

    doc.build(el)
    buf.seek(0)
    return buf.getvalue()

def pdf_carnets_por_seccion(seccion_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.seccion_id = ?
        ORDER BY a.apellido_paterno, a.apellido_materno
    """, (seccion_id,)).fetchall()
    if not rows:
        return None
    return _render_pdf_carnets(rows)

def pdf_carnet_alumno(dni):
    conn = get_db()
    rows = conn.execute("""
        SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.dni = ?
    """, (dni,)).fetchall()
    if not rows:
        return None
    return _render_pdf_carnets(rows)

def df_a_xlsx(df, sheet_name="Datos"):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=sheet_name)
    buf.seek(0)
    return buf.getvalue()
# ESTILOS Y HELPERS UI##############################################################
###############################################################################################33333

def aplicar_estilos():
    st.markdown(f"""
    <style>
        .stButton > button,
        .stFormSubmitButton > button,
        .stDownloadButton > button {{
            background: {COLOR_PRIMARIO} !important;
            color: #FFFFFF !important;
            border-radius: 10px !important;
            font-weight: 600 !important;
            border: none !important;
        }}
        .stButton > button:hover,
        .stFormSubmitButton > button:hover,
        .stDownloadButton > button:hover {{
            background: {COLOR_PRIMARIO_HOVER} !important;
            color: #FFFFFF !important;
        }}
        .stButton > button[kind="primary"],
        .stFormSubmitButton > button[kind="primary"] {{
            background: {COLOR_PRIMARIO} !important;
            color: #FFFFFF !important;
        }}
    </style>
    """, unsafe_allow_html=True)

def color_estado(v):
    if v == "Puntual":
        return "background-color:#d4edda;color:#155724;font-weight:bold"
    if v == "Tardanza":
        return "background-color:#fff3cd;color:#856404;font-weight:bold"
    if v == "Falta":
        return "background-color:#f8d7da;color:#721c24;font-weight:bold"
    return ""

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
            secs = ([{"id": None, "nombre": "Todas"}] if mostrar_todos else []) + \
                   secciones_por_grado(grado_sel["id"])
        else:
            secs = [{"id": None, "nombre": "Todas"}] if mostrar_todos else []
        seccion_sel = (st.selectbox("Sección", secs,
                                     format_func=lambda s: s["nombre"],
                                     key=f"{key_prefix}_seccion")
                       if secs else {"id": None, "nombre": "—"})
    with c3:
        texto = st.text_input("Buscar por nombre",
                               placeholder=placeholder_nombre,
                               key=f"{key_prefix}_nombre")

    grado_id = grado_sel["id"] if grado_sel else None
    seccion_id = (seccion_sel["id"]
                  if (grado_sel and grado_sel["id"] and seccion_sel) else None)
    return grado_id, seccion_id, texto.strip()


def exportar_excel_pdf(df, titulo, nombre_base, key_prefix="export"):
    if df.empty:
        return
    c1, c2 = st.columns(2)
    with c1:
        st.download_button("Excel", df_a_xlsx(df), f"{nombre_base}.xlsx",
                            key=f"{key_prefix}_xlsx",
                            use_container_width=True)
    with c2:
        st.download_button("PDF", pdf_tabla(df, titulo),
                            f"{nombre_base}.pdf", "application/pdf",
                            key=f"{key_prefix}_pdf",
                            use_container_width=True)

# galletas###################################################################################################################################################333
##########################################################################################################################
def vista_login():
    cookies = get_cookie_manager()

    # Intentar restaurar sesión desde cookie
    if "user" not in st.session_state:
        token = cookies.get(COOKIE_NOMBRE)
        if token:
            user = restaurar_sesion_por_token(token)
            if user:
                st.session_state.user = user
                st.rerun()

    st.title("Sistema de Asistencia - I.E. Yarinacocha")
    st.caption("Ingresa con tu usuario y contraseña")
    st.markdown("---")

    with st.form("login"):
        col1, col2, col3 = st.columns([1, 1.2, 1])
        with col2:
            u = st.text_input("Usuario")
            p = st.text_input("Contraseña", type="password")
            recordarme = st.checkbox("Recordarme en este dispositivo", value=True)
            ok = st.form_submit_button("Ingresar", type="primary",
                                        use_container_width=True)
        if ok:
            user = autenticar(u, p)
            if user:
                st.session_state.user = user
                if recordarme:
                    token = crear_token_sesion(user)
                    try:
                        cookies.set(COOKIE_NOMBRE, token,
                                     expires_at=datetime.now() + timedelta(days=DIAS_TOKEN_SESION))
                    except Exception as e:
                        print(f"[login] Error guardando cookie: {e}")
                auditar(user["usuario"], "Login")
                st.rerun()
            else:
                st.error("Credenciales incorrectas")


# sidebar organizado por los roles##########################################################################################################################################3333
#####################################################################################################################################################33

def menu_lateral():
    user = st.session_state.user
    rol = user["rol"]

    with st.sidebar:
        st.markdown(f"### 👤 {user['nombres']}")
        st.caption(f"Rol: **{rol}**")
        if user["turno_asignado"]:
            turno = next((t["nombre"] for t in turnos()
                          if t["id"] == user["turno_asignado"]), "-")
            st.caption(f"Turno: **{turno}**")
        st.markdown("---")

        opciones_por_rol = {
            "Admin": [
                "Puerta", "TOECE", "Panel Dirección",
                "Reportes y Consultas", "Alumnos", "Carnets",
                "Días especiales", "Seleccionar Alumnos Reforzamiento",
                "Horarios", "Usuarios", "Auditoría",
            ],
            "TOECE": [
                "Puerta", "TOECE", "Escanear Reforzamiento",
                "Días especiales", "Reportes y Consultas",
            ],
            "Direccion": [
                "Puerta", "TOECE", "Panel Dirección", "Carnets",
                "Escanear Reforzamiento", "Días especiales",
                "Reportes y Consultas",
            ],
            "Auxiliar": [
                "Puerta", "Reportes y Consultas",
            ],
            "Docente Reforzamiento": [
                "Escanear Reforzamiento",
            ],
        }
        opciones = opciones_por_rol.get(rol, [])

        if not opciones:
            st.error(f"Rol desconocido: {rol}")
            return None

        if ("menu" not in st.session_state
                or st.session_state.menu not in opciones):
            st.session_state.menu = opciones[0]

        opcion = st.radio("Menú", opciones, key="menu")

        st.markdown("---")
        if st.button("Cerrar sesión", use_container_width=True):
            auditar(user["usuario"], "Logout")
            try:
                cookies = get_cookie_manager()
                token = cookies.get(COOKIE_NOMBRE)
                if token:
                    limpiar_token(token)
                    cookies.delete(COOKIE_NOMBRE)
            except Exception as e:
                print(f"[logout] Error limpiando cookie: {e}")
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()

    return opcion

# puerta para escanear los qr ##########################################################################################################################333
########################################################################################################################################33
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
            etiqueta = {"Puntual": "Puntual", "Tardanza": "Tardanza",
                        "Falta": "Falta"}.get(estado, "sin registrar")
            st.markdown(f"**{al['nombre_completo']}** - {etiqueta}")
            if estado_ref:
                st.caption(f"Reforzamiento: {estado_ref}")
        with c2:
            if estado is None:
                if st.button("Marcar", key=f"m_{al['id']}",
                              use_container_width=True):
                    ok, tipo, msg, extra = registrar_entrada(al["dni"], usuario)
                    st.session_state[f"_msg_m_{al['id']}"] = msg
                    st.session_state[f"_ok_m_{al['id']}"] = ok
                    st.rerun(scope="fragment")
            else:
                msg_key = f"_msg_m_{al['id']}"
                if msg_key in st.session_state:
                    m = st.session_state.pop(msg_key)
                    o = st.session_state.pop(f"_ok_m_{al['id']}", False)
                    if o:
                        st.success(m)
                    else:
                        st.warning(m)

@st.fragment
def _frag_escaner_qr(usuario):
    st.caption("Muestra el QR del alumno a la cámara")

    # Mostrar mensaje del escaneo anterior
    if "_qr_msg" in st.session_state:
        msg = st.session_state.pop("_qr_msg")
        ok = st.session_state.pop("_qr_ok", False)
        tipo = st.session_state.pop("_qr_tipo", None)
        accion = st.session_state.pop("_qr_accion", None)

        if not ok:
            st.error(msg)
        elif tipo in ("PUNTUAL", "REFORZAMIENTO"):
            st.success(msg)
            st.balloons()
        elif tipo == "TARDANZA":
            if accion == "PERDONADO":
                st.warning(msg)
            elif accion == "DERIVADO_TOECE":
                st.error(msg)
                st.info("Derivar al alumno al salón TOECE.")
            else:
                st.error(msg)
                st.info("Retener al alumno hasta que llegue su apoderado.")

    # Key dinámica para forzar re-render de la cámara
    if "_cam_key_counter" not in st.session_state:
        st.session_state["_cam_key_counter"] = 0

    cam_key = f"camara_qr_{st.session_state['_cam_key_counter']}"
    img_file = st.camera_input("Escanea el QR", key=cam_key)

    if not img_file:
        return

    dni = leer_qr(Image.open(BytesIO(img_file.getvalue())))
    if not dni:
        st.error("No se detectó QR. Prueba con mejor luz o más cerca.")
        st.session_state["_cam_key_counter"] += 1
        st.rerun(scope="fragment")
        return

    ok, tipo, msg, extra = registrar_entrada(dni, usuario)
    st.session_state["_qr_msg"] = msg
    st.session_state["_qr_ok"] = ok
    st.session_state["_qr_tipo"] = tipo
    st.session_state["_qr_accion"] = (extra.get("accion") if extra else None)
    st.session_state["_cam_key_counter"] += 1
    st.rerun(scope="fragment")

def vista_puerta():
    st.title("Control de Puerta")
    usuario = st.session_state.user
    es_admin = usuario["rol"] == "Admin"
    hoy = hoy_str()

    conn = get_db()
    esp = conn.execute(
        "SELECT descripcion, hora_entrada, tipo FROM dias_especiales "
        "WHERE fecha=? AND activo=1 LIMIT 1",
        (hoy,)
    ).fetchone()

    if esp and esp["tipo"] == "evento":
        st.success(f"Evento escolar: {esp['descripcion']} "
                   f"(entrada {esp['hora_entrada']})")
    elif esp and esp["tipo"] == "feriado":
        st.info(f"Feriado / sin clases: {esp['descripcion']} - "
                f"No se toma asistencia.")
        return
    elif not es_dia_laboral():
        st.warning("Hoy no es día laboral. No se toma asistencia.")
        return
    else:
        st.info(f"Día laboral - {hoy}")

    marcar_faltas_al_cierre()
    st.markdown("---")

    if es_admin:
        modo = st.radio("Método", ["Escanear QR", "Lista manual"],
                         horizontal=True, key="modo_puerta")
    else:
        modo = "Escanear QR"
        st.caption("Modo de escaneo QR activo.")

    if modo == "Escanear QR":
        _frag_escaner_qr(usuario)
        return

    grados = grados_lista()
    if not grados:
        st.warning("No hay grados registrados.")
        return

    c1, c2 = st.columns(2)
    with c1:
        grado_sel = st.selectbox("Grado", grados,
                                  format_func=lambda g: g["nombre"],
                                  key="pt_grado")
    with c2:
        secs = secciones_por_grado(grado_sel["id"]) if grado_sel else []
        if not secs:
            st.warning("Ese grado no tiene secciones.")
            return
        seccion_sel = st.selectbox("Sección", secs,
                                     format_func=lambda s: s["nombre"],
                                     key="pt_seccion")
        st.markdown(f"### Alumnos de {grado_sel['nombre']}{seccion_sel['nombre']}")
        _frag_lista_marcar(usuario, seccion_sel["id"])


#panel apra la dirección

def vista_panel_direccion():
    st.title("Panel Dirección")
    hoy = hoy_str()
    marcar_faltas_al_cierre()

    if "last_refresh_panel" not in st.session_state:
        st.session_state.last_refresh_panel = time.time()

    c1, c2 = st.columns([1, 5])
    with c1:
        if st.button("Actualizar", use_container_width=True,
                      key="refresh_panel"):
            st.session_state.last_refresh_panel = time.time()
            st.rerun()
    with c2:
        seg = int(time.time() - st.session_state.last_refresh_panel)
        st.caption(f"Auto-refresh cada 30s (hace {seg}s)")

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
    df_ult = ultimos_registros(hoy, limite=20)
    if df_ult.empty:
        st.info("Sin escaneos registrados hoy.")
    else:
        df_show = df_ult.copy()
        df_show["tipo"] = df_show.apply(
            lambda r: "Reforzamiento" if r["estado_reforzamiento"] else "Clases",
            axis=1
        )
        df_show["estado_final"] = df_show.apply(
            lambda r: r["estado_reforzamiento"] or r["estado"], axis=1
        )
        df_show["hora_final"] = df_show.apply(
            lambda r: r["hora_reforzamiento"] or r["hora"], axis=1
        )
        st.dataframe(
            df_show[["tipo", "apellidos", "nombres", "grado", "seccion",
                     "hora_final", "estado_final"]]
            .style.map(color_estado, subset=["estado_final"]),
            use_container_width=True,
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
    if not df7.empty:
        df_long = df7.melt(id_vars="fecha", var_name="Estado",
                            value_name="Cantidad")
        fig = px.bar(df_long, x="fecha", y="Cantidad", color="Estado",
                     color_discrete_map={"Puntual": "#28a745",
                                          "Falta": "#dc3545"},
                     barmode="stack", title="Asistencia diaria")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Sin datos de los últimos 7 días.")


#vista para el coordinador de toece##############################################################3333

def vista_toece():
    st.title("TOECE - Derivados y Observados")
    usuario = st.session_state.user
    hoy = hoy_str()

    if "last_refresh_toece" not in st.session_state:
        st.session_state.last_refresh_toece = time.time()

    c1, c2 = st.columns([1, 5])
    with c1:
        if st.button("Actualizar", use_container_width=True,
                      key="refresh_toece"):
            st.session_state.last_refresh_toece = time.time()
            st.rerun()
    with c2:
        seg = int(time.time() - st.session_state.last_refresh_toece)
        st.caption(f"Auto-refresh cada 30s (hace {seg}s)")

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
                   a.nombres, g.nombre AS grado, s.nombre AS seccion,
                   t.hora, t.accion, t.observacion
            FROM tardanzas t
            JOIN alumnos a ON t.alumno_id = a.id
            JOIN secciones s ON a.seccion_id = s.id
            JOIN grados g ON s.grado_id = g.id
            WHERE t.fecha = ?
              AND t.accion IN ('DERIVADO_TOECE','RETENIDO_APODERADO')
            ORDER BY t.hora DESC
        """, conn, params=[hoy])
        if df.empty:
            st.info("No hay derivados hoy.")
        else:
            st.dataframe(df, use_container_width=True)

    with tab2:
        st.subheader("Firmar acta de compromiso")
        conn = get_db()
        der = pd.read_sql("""
            SELECT t.id, a.id AS alumno_id, a.dni,
                   a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'')
                   || ', ' || a.nombres AS nombre_completo,
                   t.numero, t.accion
            FROM tardanzas t
            JOIN alumnos a ON t.alumno_id = a.id
            WHERE t.fecha = ?
              AND t.accion IN ('DERIVADO_TOECE','RETENIDO_APODERADO')
              AND a.id NOT IN (SELECT alumno_id FROM actas_compromiso WHERE fecha = ?)
        """, conn, params=[hoy, hoy])
        if der.empty:
            st.info("No hay alumnos pendientes de acta hoy.")
        else:
            with st.form("acta"):
                opciones = {
                    f"{r['nombre_completo']} (DNI {r['dni']}) - {r['accion']}": r["alumno_id"]
                    for _, r in der.iterrows()
                }
                sel = st.selectbox("Alumno", list(opciones.keys()))
                motivo = st.text_input("Motivo",
                                        value="Reincidencia en tardanzas")
                obs = st.text_area("Observaciones")
                ok = st.form_submit_button("Firmar acta", type="primary",
                                             use_container_width=True)
            if ok:
                registrar_acta(opciones[sel], motivo, obs, usuario)
                st.session_state["_msg_acta"] = "Acta registrada correctamente."
                st.rerun()

    with tab3:
        st.subheader("Alumnos observados")
        solo_activos = st.checkbox("Solo activos", value=True, key="obs_act")
        df = observados_dataframe(solo_activos=solo_activos)
        if df.empty:
            st.info("No hay observados con ese filtro.")
        else:
            df_show = df[["dni", "apellidos", "nombres", "grado", "seccion",
                          "turno", "fecha_ingreso", "motivo", "fecha_salida"]]
            st.dataframe(df_show, use_container_width=True)
            exportar_excel_pdf(df_show, "Reporte de Observados",
                               "observados", key_prefix="obs")
            st.markdown("---")
            st.subheader("Liberar de lista de observados")
            with st.form("liberar_obs"):
                op = {f"{r['apellidos']}, {r['nombres']} (DNI {r['dni']})": r["id"]
                      for _, r in df.iterrows()}
                sel = st.selectbox("Alumno", list(op.keys()))
                liberar = st.form_submit_button("Liberar", type="primary",
                                                  use_container_width=True)
            if liberar:
                conn = get_db()
                row = conn.execute(
                    "SELECT alumno_id FROM observados WHERE id=?", (op[sel],)
                ).fetchone()
                if row:
                    liberar_observado(row["alumno_id"], usuario)
                    st.session_state["_msg_liberar"] = "Alumno liberado."
                    st.rerun()
# vista apra los reportes##########################################################################################################################################3333
def _rep_detalle(ini, fin, turno_sel, grado_id, seccion_id, texto,
                  periodo, tipo_asistencia):
    df = reporte_detalle(ini, fin, turno_sel, grado_id, seccion_id,
                           texto, tipo_asistencia)
    if df.empty:
        st.warning("Sin datos para los filtros.")
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
        col_estado = ("estado_reforzamiento"
                      if tipo_asistencia == "Reforzamiento" else "estado")
        cnt = df[col_estado].value_counts().reset_index()
        cnt.columns = ["Estado", "Cantidad"]
        fig = px.pie(cnt, names="Estado", values="Cantidad",
                     title="Distribución de estados", hole=0.45,
                     color="Estado",
                     color_discrete_map={"Puntual": "#28a745",
                                          "Falta": "#dc3545",
                                          "Tardanza": "#ffc107"})
        st.plotly_chart(fig, use_container_width=True)

    with col_b:
        if tipo_asistencia == "Reforzamiento":
            por_dia = (df.groupby(["fecha", "estado_reforzamiento"])
                       .size().reset_index(name="Cantidad"))
            por_dia.columns = ["fecha", "estado", "Cantidad"]
        else:
            por_dia = (df.groupby(["fecha", "estado"])
                       .size().reset_index(name="Cantidad"))
        fig = px.bar(por_dia, x="fecha", y="Cantidad", color="estado",
                     title="Asistencia por día",
                     color_discrete_map={"Puntual": "#28a745",
                                          "Falta": "#dc3545",
                                          "Tardanza": "#ffc107"},
                     barmode="stack")
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    st.subheader("Resumen por salón")
    col_estado = ("estado_reforzamiento"
                  if tipo_asistencia == "Reforzamiento" else "estado")
    df_f = df[df[col_estado].notna()]
    resumen = df_f.groupby(["grado", "seccion", "turno"]).agg(
        puntuales=(col_estado, lambda x: (x == "Puntual").sum()),
        faltas=(col_estado, lambda x: (x == "Falta").sum()),
        tardanzas=(col_estado, lambda x: (x == "Tardanza").sum()),
        total=(col_estado, "count"),
    ).reset_index()
    resumen["salon"] = resumen["grado"] + resumen["seccion"]
    st.dataframe(
        resumen[["salon", "turno", "puntuales", "faltas", "tardanzas", "total"]],
        use_container_width=True,
    )

    exportar_excel_pdf(
        df, f"Reporte {periodo} - {tipo_asistencia}",
        f"reporte_{periodo}_{tipo_asistencia.replace(' ', '_')}",
        key_prefix="rep_det",
    )


def _rep_conteo(ini, fin, turno_sel, grado_id, seccion_id, texto,
                 periodo, tipo_asistencia):
    st.subheader("Conteo de faltas")
    df = reporte_conteo_faltas(ini, fin, turno_sel, grado_id, seccion_id,
                                 texto, tipo_asistencia)
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
        fig = px.bar(top10, x="total_faltas", y="nombre_completo",
                     orientation="h", color="total_faltas",
                     color_continuous_scale="Reds",
                     title="Top 10 alumnos con más faltas")
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, use_container_width=True)
    with col_b:
        dist = pd.DataFrame({
            "Tipo": ["Justificadas", "Injustificadas"],
            "Cantidad": [int(df["faltas_just"].sum()),
                          int(df["faltas_injust"].sum())],
        })
        fig = px.pie(dist, names="Tipo", values="Cantidad", hole=0.45,
                     color="Tipo",
                     color_discrete_map={"Justificadas": "#28a745",
                                          "Injustificadas": "#dc3545"})
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    st.subheader("Faltas por grado")
    por_grado = df.groupby("grado").agg(total=("total_faltas", "sum")).reset_index()
    fig = px.bar(por_grado, x="grado", y="total", color="total",
                 color_continuous_scale="Oranges", title="Faltas por grado")
    st.plotly_chart(fig, use_container_width=True)

    exportar_excel_pdf(
        df, f"Conteo de faltas - {periodo} - {tipo_asistencia}",
        f"conteo_{periodo}_{tipo_asistencia.replace(' ', '_')}",
        key_prefix="conteo",
    )


def _rep_cierre(grado_id, seccion_id, turno_sel):
    st.subheader("Cierre mensual de Asistencia")
    hoy_dt = ahora()
    c1, c2 = st.columns(2)
    with c1:
        mes = st.selectbox("Mes", list(range(1, 13)),
                            index=hoy_dt.month - 1,
                            format_func=lambda m: MESES_ES[m],
                            key="cie_mes")
    with c2:
        anio = st.number_input("Año", min_value=2020, max_value=2100,
                                value=hoy_dt.year, key="cie_anio")

    from calendar import monthrange
    ultimo_dia = monthrange(anio, mes)[1]
    st.caption(f"Rango: {anio:04d}-{mes:02d}-01 a "
               f"{anio:04d}-{mes:02d}-{ultimo_dia:02d}")

    if st.button("Generar reporte", type="primary"):
        df = cierre_mensual(mes, anio, turno_sel, grado_id, seccion_id)
        if df.empty:
            st.warning("No hay datos para ese mes.")
            return
        st.success(f"Reporte: {len(df)} alumnos")
        st.dataframe(df, use_container_width=True)
        st.download_button(
            "Bajar Excel",
            df_a_xlsx(df, f"Cierre_{MESES_ES[mes]}_{anio}"),
            f"cierre_{anio}_{mes:02d}.xlsx",
        )


def vista_reportes():
    st.title("Reportes y Consultas")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        periodo = st.selectbox("Período",
                                ["Diario", "Semanal", "Mensual", "Bimestral"],
                                key="rep_periodo")
    with c2:
        turno_opts = ["Todos"] + [t["nombre"] for t in turnos()]
        turno_sel = st.selectbox("Turno", turno_opts, key="rep_turno")
    with c3:
        tipo_asistencia = st.selectbox(
            "Tipo de asistencia",
            ["Clases normales", "Reforzamiento", "Ambos"],
            key="rep_tipo_asist",
        )
    with c4:
        tipo_reporte = st.selectbox(
            "Tipo de reporte",
            ["Detalle", "Conteo de faltas", "Cierre mensual"],
            key="rep_tipo",
        )

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
        _rep_detalle(ini, fin, turno_sel, grado_id, seccion_id, texto,
                      periodo, tipo_asistencia)
    else:
        _rep_conteo(ini, fin, turno_sel, grado_id, seccion_id, texto,
                     periodo, tipo_asistencia)

#vista de alumnos##############################################################################################################################################33

def _perfil_completo(dni, usuario):
    col1, col2 = st.columns([1, 5])
    with col1:
        if st.button("Volver", type="primary", use_container_width=True):
            st.session_state.pop("perfil_dni", None)
            st.rerun()
    with col2:
        st.caption("Volver a la búsqueda")

    st.markdown("---")
    conn = get_db()
    al = conn.execute("""
        SELECT a.*, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.dni=?
    """, (dni,)).fetchone()
    if not al:
        st.warning("Alumno no encontrado.")
        return
    al = dict(al)

    df_asist = pd.read_sql("""
        SELECT fecha, estado, justificada,
               COALESCE(observacion,'') AS observacion,
               hora_reforzamiento, estado_reforzamiento,
               justificada_reforzamiento,
               COALESCE(observacion_reforzamiento,'') AS observacion_reforzamiento
        FROM asistencias WHERE alumno_id=? ORDER BY fecha DESC
    """, conn, params=[al["id"]])

    df_tard = pd.read_sql("""
        SELECT fecha, hora, numero AS "N", accion
        FROM tardanzas WHERE alumno_id=? ORDER BY fecha DESC, hora DESC
    """, conn, params=[al["id"]])

    df_obs = pd.read_sql("""
        SELECT fecha_ingreso, COALESCE(fecha_salida,'-') AS fecha_salida,
               COALESCE(motivo,'') AS motivo, activo
        FROM observados WHERE alumno_id=? ORDER BY fecha_ingreso DESC
    """, conn, params=[al["id"]])

    df_act = pd.read_sql("""
        SELECT fecha, COALESCE(motivo,'') AS motivo
        FROM actas_compromiso WHERE alumno_id=? ORDER BY fecha DESC
    """, conn, params=[al["id"]])

    nombre = (f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, "
              f"{al['nombres']}").strip(", ")
    st.markdown(f"### {nombre}")

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

    total_puntuales = int((df_asist["estado"] == "Puntual").sum())
    total_faltas = int((df_asist["estado"] == "Falta").sum())
    total_tardanzas = len(df_tard)
    total_actas = len(df_act)
    total_ref_puntuales = int((df_asist["estado_reforzamiento"] == "Puntual").sum())
    total_ref_tardanzas = int((df_asist["estado_reforzamiento"] == "Tardanza").sum())
    total_ref_faltas = int((df_asist["estado_reforzamiento"] == "Falta").sum())

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
        resumen = (df_asist.groupby(["mes", "estado"])
                   .size().reset_index(name="Cantidad"))
        fig = px.bar(resumen, x="mes", y="Cantidad", color="estado",
                     color_discrete_map={"Puntual": "#28a745",
                                          "Falta": "#dc3545"},
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
        df_hist = df_asist[["fecha", "estado", "justificada", "observacion",
                             "hora_reforzamiento", "estado_reforzamiento"]].copy()
        df_hist.columns = ["Fecha", "Estado clases", "Justificada",
                            "Observación", "Hora reforz.", "Estado reforz."]
        st.dataframe(
            df_hist.style.map(color_estado, subset=["Estado clases"]),
            use_container_width=True,
        )

    if usuario["rol"] == "Admin":
        st.markdown("---")
        st.subheader("Editar asistencia de hoy")

        if "_msg_edit_asist" in st.session_state:
            st.success(st.session_state["_msg_edit_asist"])
            del st.session_state["_msg_edit_asist"]

        asist = conn.execute("""
            SELECT id, estado, COALESCE(observacion,'') AS observacion,
                   hora_reforzamiento, estado_reforzamiento
            FROM asistencias WHERE alumno_id=? AND fecha=?
        """, (al["id"], hoy_str())).fetchone()

        if asist:
            st.write(f"**Estado actual clases:** {asist['estado'] or '-'}")
            if asist["estado_reforzamiento"]:
                st.write(f"**Estado actual reforzamiento:** "
                          f"{asist['estado_reforzamiento']}")
            with st.form("editar_asist_hoy"):
                estados = ["(sin cambio)", "Puntual", "Tardanza", "Falta"]
                idx = (estados.index(asist["estado"])
                       if asist["estado"] in estados else 0)
                nuevo = st.selectbox("Nuevo estado (clases)", estados, index=idx)
                obs = st.text_input("Observación (clases)",
                                     value=asist["observacion"])
                guardar = st.form_submit_button("Guardar cambio clases",
                                                  type="primary",
                                                  use_container_width=True)
            if guardar and nuevo != "(sin cambio)":
                ok, msg = editar_estado_asistencia(al["id"], nuevo, obs, usuario)
                if ok:
                    st.session_state["_msg_edit_asist"] = msg
                    st.rerun()
                else:
                    st.error(msg)
        else:
            st.info("Este alumno no tiene asistencia registrada hoy.")
            with st.form("crear_asist_hoy"):
                nuevo = st.selectbox("Estado", ["Puntual", "Tardanza", "Falta"])
                obs = st.text_input("Observación")
                guardar = st.form_submit_button("Crear asistencia",
                                                  type="primary",
                                                  use_container_width=True)
            if guardar:
                ok, msg = editar_estado_asistencia(al["id"], nuevo, obs, usuario)
                if ok:
                    st.session_state["_msg_edit_asist"] = msg
                    st.rerun()
                else:
                    st.error(msg)

@st.fragment
def _frag_importar_excel():
    st.info("Columnas: DNI, Nombres, Apellido Paterno, Apellido Materno, "
            "Grado, Sección, Turno.")

    if "_msg_import" in st.session_state:
        st.success(st.session_state["_msg_import"])
        del st.session_state["_msg_import"]

    f = st.file_uploader("Sube el Excel", type=["xlsx", "xls"],
                          key="import_excel")
    if not f:
        return

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
            m_apo_nom = st.selectbox("Nombre Apoderado", [""] + cols,
                                      key="map_apo_nom")
        with c4:
            m_apo_tel = st.selectbox("Teléfono Apoderado", [""] + cols,
                                      key="map_apo_tel")
        validar = st.form_submit_button("Validar", type="primary",
                                          use_container_width=True)

    if validar:
        mapeo = {
            "dni": m_dni, "nombres": m_nom, "apellido_paterno": m_pat,
            "apellido_materno": m_mat, "grado": m_gra, "seccion": m_sec,
            "turno": m_tur, "apoderado_nombre": m_apo_nom,
            "apoderado_telefono": m_apo_tel,
        }
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
                st.dataframe(pd.DataFrame(st.session_state["_imp_errores"]),
                              use_container_width=True)

        if st.session_state["_imp_validas"]:
            if st.button("Importar SOLO válidas", type="primary",
                          use_container_width=True):
                n, errs = insertar_validas(st.session_state["_imp_validas"])
                st.session_state["_msg_import"] = f"{n} alumnos importados."
                if errs:
                    st.session_state["_imp_errores_insert"] = errs
                for k in ["_imp_validas", "_imp_errores", "_imp_stats"]:
                    st.session_state.pop(k, None)
                st.rerun()

    if "_imp_errores_insert" in st.session_state:
        with st.expander(f"{len(st.session_state['_imp_errores_insert'])} "
                          f"errores al insertar"):
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
        return

    mostrar_todos = st.checkbox("Mostrar todos", value=False, key="list_all")
    limite = len(df) if mostrar_todos else 50
    for _, al in df.head(limite).iterrows():
        if st.button(
            f"{al['nombre_completo']} - {al['grado']}{al['seccion']} ({al['turno']})",
            key=f"perfil_btn_{al['id']}", use_container_width=True
        ):
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
        st.warning("No hay grados registrados.")
        return

    with st.form("crear_alumno_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            nuevo_dni = st.text_input("DNI * (8 dígitos)", max_chars=8)
            nuevo_nombres = st.text_input("Nombres *")
            nuevo_ap_pat = st.text_input("Apellido Paterno *")
        with c2:
            nuevo_ap_mat = st.text_input("Apellido Materno")
            grado_sel = st.selectbox("Grado *", grados,
                                       format_func=lambda g: g["nombre"])
            secs = secciones_por_grado(grado_sel["id"]) if grado_sel else []
            if not secs:
                st.warning("Ese grado no tiene secciones.")
            seccion_sel = (st.selectbox("Sección *", secs,
                                          format_func=lambda s: s["nombre"])
                           if secs else None)
        c3, c4 = st.columns(2)
        with c3:
            nuevo_apo_nom = st.text_input("Nombre Apoderado (opcional)")
        with c4:
            nuevo_apo_tel = st.text_input("Teléfono Apoderado (opcional)")
        crear = st.form_submit_button("Crear Alumno", type="primary",
                                        use_container_width=True)

    if not crear:
        return

    if (not nuevo_dni or not nuevo_nombres or not nuevo_ap_pat
            or not seccion_sel):
        st.error("Completa los campos obligatorios (*)")
        return
    if not re.fullmatch(r"\d{8}", nuevo_dni.strip()):
        st.error("DNI inválido (debe tener 8 dígitos)")
        return

    ok, msg = crear_alumno(
        nuevo_dni.strip(), nuevo_nombres.strip(), nuevo_ap_pat.strip(),
        nuevo_ap_mat.strip(), seccion_sel["id"],
        nuevo_apo_nom.strip(), nuevo_apo_tel.strip(),
        st.session_state.user,
    )
    if ok:
        st.session_state["_msg_crear_alumno"] = msg
        st.rerun()
    else:
        st.error(msg)


@st.fragment
def _frag_editar_alumno():
    st.caption("Solo se puede editar el apoderado, grado y sección. "
               "El nombre y DNI son inmutables.")

    if "_msg_editar_alumno" in st.session_state:
        st.success(st.session_state["_msg_editar_alumno"])
        del st.session_state["_msg_editar_alumno"]

    dni_edit = st.session_state.get("editar_dni")
    if not dni_edit:
        grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
            key_prefix="al_edit", placeholder_nombre="Ej: Flores"
        )
        if not (texto or grado_id):
            return
        df = buscar_alumnos_por_nombre(texto, grado_id, seccion_id, limite=50)
        if df.empty:
            st.info("Sin coincidencias.")
            return
        for _, al in df.iterrows():
            if st.button(
                f"{al['nombre_completo']} - {al['grado']}{al['seccion']}",
                key=f"e_{al['id']}", use_container_width=True
            ):
                st.session_state["editar_dni"] = al["dni"]
                st.rerun()
        return

    conn = get_db()
    al = pd.read_sql("""
        SELECT a.*, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.dni=?
    """, conn, params=[dni_edit])

    if al.empty:
        st.warning("No encontrado.")
        st.session_state.pop("editar_dni", None)
        return

    al = al.iloc[0]
    st.markdown(f"### Editando: {al['apellido_paterno']} "
                f"{al['apellido_materno'] or ''}, {al['nombres']}")
    st.info(f"**DNI:** {al['dni']} (no editable)")

    grados = grados_lista()

    if st.button("Cancelar edición", key="cancelar_edit"):
        st.session_state.pop("editar_dni", None)
        st.rerun()

    with st.form("edit_al"):
        st.text_input("DNI", value=al["dni"], disabled=True)
        st.text_input("Nombres", value=al["nombres"], disabled=True)
        st.text_input("Apellido Paterno", value=al["apellido_paterno"],
                       disabled=True)
        st.text_input("Apellido Materno", value=al["apellido_materno"] or "",
                       disabled=True)
        st.markdown("---")
        st.markdown("**Campos editables:**")
        apo_nom = st.text_input("Nombre Apoderado",
                                 value=al["nombre_apoderado"] or "")
        apo_tel = st.text_input("Teléfono Apoderado",
                                 value=al["telefono_apoderado"] or "")
        idx_grado = next(
            (i for i, g in enumerate(grados) if g["nombre"] == al["grado"]), 0
        )
        grado_nuevo = st.selectbox("Grado", grados, index=idx_grado,
                                     format_func=lambda g: g["nombre"])
        secs_nuevas = (secciones_por_grado(grado_nuevo["id"])
                       if grado_nuevo else [])
        idx_sec = next(
            (i for i, s in enumerate(secs_nuevas)
             if s["nombre"] == al["seccion"] and s["id"] == al["seccion_id"]),
            0,
        )
        seccion_nueva = (st.selectbox("Sección", secs_nuevas, index=idx_sec,
                                        format_func=lambda s: s["nombre"])
                         if secs_nuevas else None)
        guardar = st.form_submit_button("Guardar cambios", type="primary",
                                          use_container_width=True)

    if guardar:
        if not seccion_nueva:
            st.error("Debes seleccionar una sección.")
            return
        ok, msg = editar_alumno(al["id"], apo_nom, apo_tel,
                                 seccion_nueva["id"], al["dni"],
                                 st.session_state.user)
        if ok:
            st.session_state["_msg_editar_alumno"] = msg
            st.session_state.pop("editar_dni", None)
            st.rerun()
        else:
            st.error(msg)


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
        st.error(f"¿Seguro que desea eliminar a **{eliminar['nombre']}** "
                 f"(DNI {eliminar['dni']})?")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("SÍ, ELIMINAR", type="primary",
                          use_container_width=True, key="confirm_del"):
                ok, msg = eliminar_alumno(eliminar["id"], eliminar["dni"],
                                            st.session_state.user)
                if ok:
                    st.session_state["_msg_eliminar_alumno"] = msg
                    st.session_state.pop("eliminar_alumno", None)
                    st.rerun()
                else:
                    st.error(msg)
        with c2:
            if st.button("Cancelar", use_container_width=True,
                          key="cancel_del"):
                st.session_state.pop("eliminar_alumno", None)
                st.rerun()
        return

    grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
        key_prefix="al_del", placeholder_nombre="Ej: Rojas"
    )
    if not (texto or grado_id):
        return
    df = buscar_alumnos_por_nombre(texto, grado_id, seccion_id, limite=50)
    if df.empty:
        st.info("Sin coincidencias.")
        return
    for _, al in df.iterrows():
        if st.button(
            f"{al['nombre_completo']} - {al['grado']}{al['seccion']}",
            key=f"d_{al['id']}", use_container_width=True
        ):
            st.session_state["eliminar_alumno"] = {
                "id": al["id"],
                "nombre": al["nombre_completo"],
                "dni": al["dni"],
            }
            st.rerun()


def vista_alumnos():
    st.title("Alumnos")

    dni_perfil = st.session_state.get("perfil_dni")
    if dni_perfil:
        _perfil_completo(dni_perfil, st.session_state.user)
        return

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["Importar Excel", "Listar", "Crear", "Editar", "Eliminar"]
    )
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

# vista de los carnets
def vista_carnets():
    st.title("Carnets")
    modo = st.radio("Modo", ["Por sección", "Por alumno"], horizontal=True)

    if modo == "Por sección":
        grados = grados_lista()
        c1, c2 = st.columns(2)
        with c1:
            grado_sel = st.selectbox("Grado", grados,
                                       format_func=lambda g: g["nombre"],
                                       key="carn_g")
        with c2:
            secs = secciones_por_grado(grado_sel["id"]) if grado_sel else []
            if not secs:
                st.warning("Sin secciones.")
                return
            seccion_sel = st.selectbox("Sección", secs,
                                         format_func=lambda s: s["nombre"],
                                         key="carn_s")
            if st.button("Generar PDF de la sección", type="primary"):
                pdf = pdf_carnets_por_seccion(seccion_sel["id"])
                if pdf:
                    st.download_button(
                        "Descargar",
                        pdf,
                        f"carnets_{grado_sel['nombre']}{seccion_sel['nombre']}.pdf",
                        "application/pdf",
                    )
        return

    st.caption("Busca al alumno que perdió su carnet.")
    grado_id, seccion_id, texto = filtros_grado_seccion_nombre(
        key_prefix="carn_al", placeholder_nombre="Ej: Vargas"
    )
    if not (texto or grado_id):
        return

    df = buscar_alumnos_por_nombre(texto, grado_id, seccion_id, limite=50)
    for _, al in df.iterrows():
        if st.button(
            f"{al['nombre_completo']} - {al['grado']}{al['seccion']}",
            key=f"c_{al['id']}", use_container_width=True
        ):
            st.session_state["carnet_dni"] = al["dni"]

    if "carnet_dni" in st.session_state:
        st.markdown("---")
        pdf = pdf_carnet_alumno(st.session_state["carnet_dni"])
        if pdf:
            st.download_button(
                "Descargar carnet individual",
                pdf,
                f"carnet_{st.session_state['carnet_dni']}.pdf",
                "application/pdf",
            )

# vista de horarios
@st.fragment
def _frag_horario(t):
    st.subheader(f"{t['nombre']}")

    with st.form(f"hor_{t['id']}"):
        st.markdown("**Clases normales**")
        c1, c2, c3 = st.columns(3)
        with c1:
            h_apertura = st.text_input("Apertura de puerta (HH:MM)",
                                         value=t["hora_entrada"])
        with c2:
            tol = st.number_input("Tolerancia (min)", min_value=0,
                                    max_value=120, value=t["tolerancia_min"])
        with c3:
            h_cierre = st.text_input("Cierre de puerta (HH:MM)",
                                       value=t["hora_salida"] or "17:00")

        st.markdown("---")
        st.markdown("**Reforzamiento (turno completo)**")
        c1, c2, c3 = st.columns(3)
        with c1:
            ref_inicio = st.text_input("Inicio de reforzamiento (HH:MM)",
                                         value=t.get("ref_hora_inicio") or "00:00")
        with c2:
            ref_tol = st.number_input("Tolerancia de reforzamiento (min)",
                                        min_value=0, max_value=60,
                                        value=t.get("ref_tolerancia") or 7)
        with c3:
            ref_fin = st.text_input("Fin de reforzamiento (HH:MM)",
                                      value=t.get("ref_hora_fin") or "00:00")

        st.caption(
            "**¿Cómo funciona?**\n"
            "- **Apertura de puerta**: desde esta hora el sistema acepta escaneos QR.\n"
            "- **Tolerancia**: minutos de gracia. Después de esto el escaneo se marca como Tardanza.\n"
            "- **Cierre de puerta**: hasta esta hora se aceptan escaneos. Después, quien no haya "
            "escaneado se marca como Falta automáticamente.\n"
            "- **Reforzamiento**: se aplica cuando una sección tiene un día especial de reforzamiento. "
            "El sistema usa estas horas en lugar de las horas de clases."
        )

        ok = st.form_submit_button("Guardar", type="primary")

    if ok:
        actualizar_horario(t["id"], h_apertura, h_cierre, tol,
                            ref_inicio, ref_fin, ref_tol)
        st.session_state["_msg_horario"] = f"Horario de {t['nombre']} actualizado."
        st.rerun()


def vista_horarios():
    st.title("Horarios y puertas")
    st.caption("Configura las horas de clases y de reforzamiento por turno.")

    if "_msg_horario" in st.session_state:
        st.success(st.session_state["_msg_horario"])
        del st.session_state["_msg_horario"]

    for t in turnos():
        _frag_horario(t)

# vista para los usuarios : CRUD

def _usuarios_tab_listar():
    for k in ["_msg_crear_user", "_msg_editar_user", "_msg_eliminar_user"]:
        if k in st.session_state:
            st.success(st.session_state[k])
            del st.session_state[k]
    st.dataframe(listar_usuarios(), use_container_width=True)


def _usuarios_tab_crear():
    st.subheader("Crear nuevo usuario")
    with st.form("nuevo_user", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            u = st.text_input("Usuario *")
            p = st.text_input("Password * (mínimo 6)", type="password")
            n = st.text_input("Nombres *")
        with c2:
            rol = st.selectbox("Rol", ["Admin", "TOECE", "Auxiliar",
                                         "Direccion", "Docente Reforzamiento"])
            turno_sel = None
            if rol == "Auxiliar":
                t_opts = {t["nombre"]: t["id"] for t in turnos()}
                turno_lbl = st.selectbox("Turno asignado *",
                                           list(t_opts.keys()))
                turno_sel = t_opts[turno_lbl]
            elif rol == "Docente Reforzamiento":
                st.info("Este rol solo puede escanear QR en reforzamientos.")
            else:
                st.info("El turno solo aplica para el rol Auxiliar")
        crear = st.form_submit_button("Crear Usuario", type="primary",
                                        use_container_width=True)

    if not crear:
        return
    if not (u and p and n):
        st.error("Completa todos los campos.")
        return
    if len(p) < 6:
        st.error("Password muy corta.")
        return

    ok, msg = crear_usuario(u, p, rol, n, turno_sel)
    if ok:
        st.session_state["_msg_crear_user"] = msg
        st.rerun()
    else:
        st.error(msg)


def _usuarios_tab_editar():
    st.subheader("Editar o eliminar usuario")
    usuarios = listar_usuarios()
    usuarios = usuarios[usuarios["usuario"] != "admin"]
    if usuarios.empty:
        st.info("No hay usuarios para editar (además del admin).")
        return

    opciones = {f"{r['usuario']} ({r['rol']}) - {r['nombres']}": r["id"]
                for _, r in usuarios.iterrows()}
    sel = st.selectbox("Selecciona un usuario", list(opciones.keys()),
                        key="edit_user_sel")
    user_id = opciones[sel]
    user_data = usuarios[usuarios["id"] == user_id].iloc[0]

    with st.form("editar_usuario"):
        c1, c2 = st.columns(2)
        with c1:
            edit_usuario = st.text_input("Usuario", value=user_data["usuario"])
            edit_password = st.text_input(
                "Nueva contraseña (vacío = no cambiar)", type="password"
            )
            edit_nombres = st.text_input("Nombres", value=user_data["nombres"])
        with c2:
            roles = ["Admin", "TOECE", "Auxiliar", "Direccion",
                     "Docente Reforzamiento"]
            idx_rol = (roles.index(user_data["rol"])
                       if user_data["rol"] in roles else 0)
            edit_rol = st.selectbox("Rol", roles, index=idx_rol)
            edit_turno_sel = None
            if edit_rol == "Auxiliar":
                t_opts = {t["nombre"]: t["id"] for t in turnos()}
                turno_actual = (user_data["turno"]
                                if user_data["turno"] != "-" else "Mañana")
                idx_turno = (list(t_opts.keys()).index(turno_actual)
                             if turno_actual in t_opts else 0)
                edit_turno_lbl = st.selectbox("Turno asignado",
                                                list(t_opts.keys()),
                                                index=idx_turno)
                edit_turno_sel = t_opts[edit_turno_lbl]

        c1, c2 = st.columns(2)
        with c1:
            guardar = st.form_submit_button("Guardar cambios", type="primary",
                                              use_container_width=True)
        with c2:
            eliminar = st.form_submit_button("Eliminar usuario",
                                               use_container_width=True)

    if guardar:
        try:
            actualizar_usuario(user_id, edit_usuario, edit_rol, edit_nombres,
                                edit_turno_sel, edit_password or None)
            st.session_state["_msg_editar_user"] = "Usuario actualizado."
            st.rerun()
        except Exception as e:
            st.error(f"Error: {e}")

    if eliminar:
        if st.button("SÍ, ELIMINAR", type="primary",
                      use_container_width=True, key="confirm_del_user"):
            eliminar_usuario(user_id)
            st.session_state["_msg_eliminar_user"] = "Usuario eliminado."
            st.rerun()


def vista_usuarios():
    st.title("Usuarios")
    tab1, tab2, tab3 = st.tabs(["Listar", "Crear", "Editar / Eliminar"])
    with tab1:
        _usuarios_tab_listar()
    with tab2:
        _usuarios_tab_crear()
    with tab3:
        _usuarios_tab_editar()

# vista para la auditoria de los usurios ########################################################################################################################3

def vista_auditoria():
    st.title("Auditoría")
    st.caption("Se actualiza al instante.")

    c1, _ = st.columns([1, 5])
    with c1:
        if st.button("Actualizar ahora", type="primary",
                      use_container_width=True):
            st.rerun()

    df = obtener_auditoria(limite=200)
    st.write(f"**{len(df)} registros** (últimos 200)")
    st.dataframe(df, use_container_width=True)

# VISTA: DÍAS ESPECIALES################################################################################################################################################3
@st.fragment
def _frag_listar_dias():
    conn = get_db()
    df = pd.read_sql("""
        SELECT d.id, d.fecha, d.descripcion, COALESCE(t.nombre,'Ambos') AS turno,
               d.hora_entrada, COALESCE(d.tipo,'evento') AS tipo,
               COALESCE(d.tipo_reforzamiento, '') AS tipo_ref
        FROM dias_especiales d
        LEFT JOIN turnos t ON d.turno_id = t.id
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
            df.at[idx, "secciones"] = ", ".join(
                f"{r['gr_nombre']}{r['sec_nombre']}" for _, r in secs.iterrows()
            )

    if df.empty:
        st.info("Sin días especiales programados.")
        return

    st.dataframe(df, use_container_width=True)

    op = {f"{r['fecha']} - {r['descripcion']} ({r['turno']}, {r['tipo']})": r["id"]
          for _, r in df.iterrows()}
    sel = st.selectbox("Eliminar", list(op.keys()), key="del_dia_esp")
    if st.button("Eliminar", type="primary", key="btn_del_dia_esp"):
        conn.execute("DELETE FROM reforzamiento_alumnos WHERE dia_especial_id=?",
                     (op[sel],))
        conn.execute("DELETE FROM dias_especiales_secciones WHERE dia_especial_id=?",
                     (op[sel],))
        conn.execute("DELETE FROM dias_especiales WHERE id=?", (op[sel],))
        conn.commit()
        st.session_state["_msg_dia_esp"] = "Día especial eliminado."
        st.rerun()

def _crear_feriado():
    with st.form("dia_feriado"):
        col1, col2 = st.columns(2)
        with col1:
            f = st.date_input("Fecha", min_value=ahora().date())
            desc = st.text_input("Descripción",
                                  placeholder="Ej: Día del Maestro")
        ok = st.form_submit_button("Crear Feriado", type="primary",
                                     use_container_width=True)
    if not ok:
        return
    conn = get_db()
    fecha_str = f.strftime("%Y-%m-%d")
    try:
        existente = conn.execute("""
            SELECT id FROM dias_especiales
            WHERE fecha=? AND turno_id IS NULL
        """, (fecha_str,)).fetchone()

        if existente:
            conn.execute("""
                UPDATE dias_especiales
                SET descripcion=?, hora_entrada='00:00', activo=1, tipo='feriado',
                    tipo_reforzamiento=NULL
                WHERE id=?
            """, (desc, existente["id"]))
        else:
            conn.execute("""
                INSERT INTO dias_especiales
                (fecha, descripcion, turno_id, hora_entrada, activo, tipo)
                VALUES (?,?,NULL,'00:00',1,'feriado')
            """, (fecha_str, desc))
        conn.commit()
        st.session_state["_msg_dia_esp"] = f"Feriado '{desc}' creado."
        st.rerun()
    except Exception as e:
        st.error(f"Error: {e}")

def _crear_evento():
    with st.form("dia_evento"):
        col1, col2 = st.columns(2)
        with col1:
            f = st.date_input("Fecha", min_value=ahora().date())
            desc = st.text_input("Descripción",
                                  placeholder="Ej: Aniversario del colegio")
        with col2:
            t_opts = {"Ambos turnos": None}
            t_opts.update({t["nombre"]: t["id"] for t in turnos()})
            turno_lbl = st.selectbox("Aplica al turno", list(t_opts.keys()))
            h = st.time_input("Hora de apertura especial",
                              value=datetime.strptime("08:00", "%H:%M").time())
        ok = st.form_submit_button("Crear Evento", type="primary",
                                     use_container_width=True)
    if not ok:
        return

    conn = get_db()
    turno_id = t_opts[turno_lbl]
    fecha_str = f.strftime("%Y-%m-%d")
    hora_str_ = h.strftime("%H:%M")

    try:
        existente = conn.execute("""
            SELECT id FROM dias_especiales
            WHERE fecha = ? AND COALESCE(turno_id, -1) = COALESCE(?, -1)
        """, (fecha_str, turno_id)).fetchone()

        if existente:
            conn.execute("""
                UPDATE dias_especiales
                SET descripcion=?, hora_entrada=?, activo=1, tipo='evento',
                    tipo_reforzamiento=NULL
                WHERE id=?
            """, (desc, hora_str_, existente["id"]))
        else:
            conn.execute("""
                INSERT INTO dias_especiales
                (fecha, descripcion, turno_id, hora_entrada, activo, tipo)
                VALUES (?,?,?,?,1,'evento')
            """, (fecha_str, desc, turno_id, hora_str_))
        conn.commit()
        st.session_state["_msg_dia_esp"] = f"Evento '{desc}' creado."
        st.rerun()
    except Exception as e:
        st.error(f"Error: {e}")


def _crear_dia_refuerzo_turno(conn, fecha_str, desc, turno_nombre, secciones_ids):
    """Crea un día especial de reforzamiento para un turno específico."""
    tipo_val = "despues" if turno_nombre == "Mañana" else "antes"

    existente = conn.execute("""
        SELECT d.id FROM dias_especiales d
        JOIN dias_especiales_secciones ds ON d.id = ds.dia_especial_id
        JOIN secciones s ON ds.seccion_id = s.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE d.fecha = ? AND d.tipo_reforzamiento IS NOT NULL
          AND t.nombre = ?
        LIMIT 1
    """, (fecha_str, turno_nombre)).fetchone()

    if existente:
        dia_id = existente["id"]
        conn.execute("""
            UPDATE dias_especiales
            SET descripcion=?, activo=1, tipo='evento',
                tipo_reforzamiento=?
            WHERE id=?
        """, (desc, tipo_val, dia_id))
    else:
        cursor = conn.execute("""
            INSERT INTO dias_especiales
            (fecha, descripcion, turno_id, hora_entrada, activo, tipo,
             tipo_reforzamiento)
            VALUES (?,?,NULL,'00:00',1,'evento',?)
        """, (fecha_str, desc, tipo_val))
        dia_id = cursor.lastrowid

    for sec_id in secciones_ids:
        conn.execute("""
            INSERT OR IGNORE INTO dias_especiales_secciones
            (dia_especial_id, seccion_id) VALUES (?,?)
        """, (dia_id, sec_id))

def _crear_reforzamiento():
    st.markdown("**Configurar reforzamiento:**")
    st.info(
        "Marca las secciones que tendrán reforzamiento ese día. "
        "El sistema usará automáticamente las horas de reforzamiento "
        "configuradas en **Horarios** según el turno de cada sección."
    )

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
                        if st.checkbox(f"{gr['nombre']}{sec['nombre']}",
                                        key=f"sec_chk_{sec['id']}"):
                            secciones_sel.append(sec["id"])

    with st.form("dia_reforzamiento"):
        col1, col2 = st.columns([1, 1])
        with col1:
            f = st.date_input("Fecha", min_value=ahora().date())
        with col2:
            desc = st.text_input("Descripción", value="Reforzamiento")
        ok = st.form_submit_button("Crear Reforzamiento", type="primary",
                                     use_container_width=True)

    if not ok:
        return
    if not secciones_sel:
        st.error("Debes seleccionar al menos una sección.")
        return

    conn = get_db()
    fecha_str = f.strftime("%Y-%m-%d")

    turnos_por_seccion = {}
    for sec_id in secciones_sel:
        row = conn.execute("""
            SELECT t.id AS turno_id, t.nombre AS turno_nombre,
                   g.nombre || s.nombre AS seccion_nombre
            FROM secciones s
            JOIN grados g ON s.grado_id = g.id
            JOIN turnos t ON s.turno_id = t.id
            WHERE s.id = ?
        """, (sec_id,)).fetchone()
        if row:
            turnos_por_seccion[sec_id] = dict(row)

    duplicadas = []
    for sec_id in secciones_sel:
        ya = conn.execute("""
            SELECT ds.seccion_id
            FROM dias_especiales d
            JOIN dias_especiales_secciones ds ON d.id = ds.dia_especial_id
            WHERE d.fecha=? AND d.activo=1 AND d.tipo='evento'
              AND d.tipo_reforzamiento IS NOT NULL AND ds.seccion_id=?
            LIMIT 1
        """, (fecha_str, sec_id)).fetchone()
        if ya:
            duplicadas.append(turnos_por_seccion[sec_id]["seccion_nombre"])

    if duplicadas:
        st.error(f"Ya existe un reforzamiento para esa fecha en: "
                 f"{', '.join(duplicadas)}")
        return

    secciones_manana = [sid for sid, info in turnos_por_seccion.items()
                        if info["turno_nombre"] == "Mañana"]
    secciones_tarde = [sid for sid, info in turnos_por_seccion.items()
                       if info["turno_nombre"] == "Tarde"]

    try:
        if secciones_manana:
            _crear_dia_refuerzo_turno(conn, fecha_str, desc, "Mañana",
                                        secciones_manana)
        if secciones_tarde:
            _crear_dia_refuerzo_turno(conn, fecha_str, desc, "Tarde",
                                       secciones_tarde)
        conn.commit()
        st.session_state["_msg_dia_esp"] = (
            f"Reforzamiento creado para {len(secciones_sel)} secciones. "
            f"Ahora asigna los alumnos en 'Seleccionar Alumnos Reforzamiento'."
        )
        st.rerun()
    except Exception as e:
        st.error(f"Error: {e}")


def vista_dias_especiales():
    st.title("Días especiales")

    if "_msg_dia_esp" in st.session_state:
        st.success(st.session_state["_msg_dia_esp"])
        del st.session_state["_msg_dia_esp"]

    tab1, tab2 = st.tabs(["Crear", "Listar / eliminar"])

    with tab1:
        st.markdown("### Crear día especial")
        st.caption("Configura feriados, eventos o reforzamiento por sección.")
        tipo_dia = st.radio(
            "Tipo de día especial",
            ["Feriado (sin clases)",
             "Evento con horario especial",
             "Reforzamiento por sección"],
            key="tipo_dia_esp",
        )
        if tipo_dia == "Feriado (sin clases)":
            _crear_feriado()
        elif tipo_dia == "Evento con horario especial":
            _crear_evento()
        else:
            _crear_reforzamiento()

    with tab2:
        _frag_listar_dias()

# VISTA: REFORZAMIENTO ######################################################################################################################################333
######################################################################################################################33

@st.fragment
def _frag_escaner_reforzamiento(usuario):
    st.caption("Muestra el QR del alumno a la cámara")

    if "_ref_msg" in st.session_state:
        msg = st.session_state.pop("_ref_msg")
        ok = st.session_state.pop("_ref_ok", False)
        tipo = st.session_state.pop("_ref_tipo", None)

        if not ok:
            st.error(msg)
        elif tipo == "REFORZAMIENTO":
            st.success(msg)
            st.balloons()
        elif tipo == "PUNTUAL":
            st.info(msg + " (registrado en clases normales)")
        elif tipo == "TARDANZA":
            st.warning(msg + " (registrado en clases normales)")

    if "_ref_cam_key_counter" not in st.session_state:
        st.session_state["_ref_cam_key_counter"] = 0

    cam_key = f"camara_qr_ref_{st.session_state['_ref_cam_key_counter']}"
    img_file = st.camera_input("Escanea el QR", key=cam_key)

    if not img_file:
        return

    dni = leer_qr(Image.open(BytesIO(img_file.getvalue())))
    if not dni:
        st.error("No se detectó QR. Prueba con mejor luz o más cerca.")
        st.session_state["_ref_cam_key_counter"] += 1
        st.rerun(scope="fragment")
        return

    ok, tipo, msg, extra = registrar_entrada(dni, usuario)
    st.session_state["_ref_msg"] = msg
    st.session_state["_ref_ok"] = ok
    st.session_state["_ref_tipo"] = tipo
    st.session_state["_ref_cam_key_counter"] += 1
    st.rerun(scope="fragment")

def vista_escanear_reforzamiento():
    st.title("Escanear Reforzamiento")
    usuario = st.session_state.user
    hoy = hoy_str()
    marcar_faltas_al_cierre()

    conn = get_db()
    refs_hoy = conn.execute("""
        SELECT d.id, d.descripcion, d.tipo_reforzamiento,
               GROUP_CONCAT(DISTINCT g.nombre || s.nombre) AS secciones,
               GROUP_CONCAT(DISTINCT t.nombre) AS turnos,
               MIN(t.ref_hora_inicio) AS hora_inicio,
               MAX(t.ref_hora_fin) AS hora_fin
        FROM dias_especiales d
        LEFT JOIN dias_especiales_secciones ds ON d.id = ds.dia_especial_id
        LEFT JOIN secciones s ON ds.seccion_id = s.id
        LEFT JOIN grados g ON s.grado_id = g.id
        LEFT JOIN turnos t ON s.turno_id = t.id
        WHERE d.fecha = ? AND d.activo = 1 AND d.tipo = 'evento'
          AND d.tipo_reforzamiento IS NOT NULL
        GROUP BY d.id
    """, (hoy,)).fetchall()

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
                st.markdown(f"**{ref['hora_inicio']} - "
                            f"{ref['hora_fin'] or '?'}**")
            with c3:
                st.caption(f"Turnos: {ref['turnos']}")

    st.markdown("---")
    st.subheader("Escanear QR")
    _frag_escaner_reforzamiento(usuario)

    st.markdown("---")
    st.subheader("Resumen de hoy")

    for ref in refs_hoy:
        ref = dict(ref)
        st.markdown(f"**{ref['descripcion']}**")

        df = pd.read_sql("""
            SELECT a.dni,
                   a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos,
                   a.nombres, g.nombre AS grado, s.nombre AS seccion,
                   t.nombre AS turno,
                   ast.hora_reforzamiento AS hora,
                   ast.estado_reforzamiento AS estado
            FROM reforzamiento_alumnos ra
            JOIN alumnos a ON ra.alumno_id = a.id
            JOIN secciones s ON a.seccion_id = s.id
            JOIN grados g ON s.grado_id = g.id
            JOIN turnos t ON s.turno_id = t.id
            LEFT JOIN asistencias ast
                ON ast.alumno_id = a.id AND ast.fecha = ?
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
            df[["apellidos", "nombres", "grado", "seccion", "turno",
                "hora", "estado"]]
              .style.map(color_estado, subset=["estado"]),
            use_container_width=True,
        )


def vista_seleccionar_alumnos_reforzamiento():
    st.title("Seleccionar Alumnos para Reforzamiento")

    if "_msg_ref_alumnos" in st.session_state:
        st.success(st.session_state["_msg_ref_alumnos"])
        del st.session_state["_msg_ref_alumnos"]

    conn = get_db()
    refs = pd.read_sql("""
        SELECT d.id, d.fecha, d.descripcion, d.tipo_reforzamiento,
               GROUP_CONCAT(DISTINCT g.nombre || s.nombre) AS secciones,
               GROUP_CONCAT(DISTINCT t.nombre) AS turnos
        FROM dias_especiales d
        LEFT JOIN dias_especiales_secciones ds ON d.id = ds.dia_especial_id
        LEFT JOIN secciones s ON ds.seccion_id = s.id
        LEFT JOIN grados g ON s.grado_id = g.id
        LEFT JOIN turnos t ON s.turno_id = t.id
        WHERE d.fecha >= date('now') AND d.activo = 1
          AND d.tipo = 'evento' AND d.tipo_reforzamiento IS NOT NULL
        GROUP BY d.id ORDER BY d.fecha
    """, conn)

    if refs.empty:
        st.info("No hay reforzamientos programados. Crea uno primero "
                "en Días Especiales.")
        return

    opciones = {
        f"{r['fecha']} - {r['descripcion']} "
        f"({r['secciones'] or 'sin secciones'})": r["id"]
        for _, r in refs.iterrows()
    }
    sel = st.selectbox("Selecciona el reforzamiento", list(opciones.keys()))
    ref_id = opciones[sel]
    ref_info = refs[refs["id"] == ref_id].iloc[0]

    # NUEVO: Obtener las secciones asociadas a este reforzamiento
    secciones_permitidas = conn.execute("""
        SELECT ds.seccion_id,
               g.nombre AS grado, s.nombre AS seccion,
               g.nombre || s.nombre AS nombre_completo,
               t.nombre AS turno
        FROM dias_especiales_secciones ds
        JOIN secciones s ON ds.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ds.dia_especial_id = ?
        ORDER BY g.nombre, s.nombre
    """, (ref_id,)).fetchall()

    if not secciones_permitidas:
        st.warning("Este reforzamiento no tiene secciones asignadas. "
                   "Edítalo en Días Especiales.")
        return

    secciones_ids = [s["seccion_id"] for s in secciones_permitidas]
    secciones_nombres = [s["nombre_completo"] for s in secciones_permitidas]

    st.markdown(f"**Fecha:** {ref_info['fecha']} | "
                f"**Turnos:** {ref_info['turnos']}")
    st.info(f"**Secciones permitidas:** {', '.join(secciones_nombres)}")

    st.markdown("---")
    st.subheader("Seleccionar alumnos")
    st.caption("Solo se muestran los alumnos de las secciones asignadas "
               "a este reforzamiento. Los asignados que NO escaneen tendrán "
               "Falta en reforzamiento.")

    # NUEVO: Filtros limitados a las secciones permitidas
    c1, c2 = st.columns(2)

    # Obtener los grados únicos de las secciones permitidas
    grados_permitidos = sorted(set(s["grado"] for s in secciones_permitidas))
    with c1:
        grado_sel = st.selectbox(
            "Grado",
            ["Todos"] + grados_permitidos,
            key="ref_sel_grado"
        )

    # Filtrar secciones según el grado elegido
    if grado_sel == "Todos":
        secciones_filtradas = secciones_permitidas
    else:
        secciones_filtradas = [s for s in secciones_permitidas
                                if s["grado"] == grado_sel]

    with c2:
        opciones_seccion = ["Todas"] + [s["nombre_completo"]
                                         for s in secciones_filtradas]
        seccion_sel = st.selectbox(
            "Sección",
            opciones_seccion,
            key="ref_sel_seccion"
        )

    texto = st.text_input("Buscar por nombre (opcional)",
                           placeholder="Ej: Pérez",
                           key="ref_sel_nombre")

    # Construir query limitada a las secciones permitidas
    q = """
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               g.id AS grado_id, g.nombre AS grado,
               s.id AS seccion_id, s.nombre AS seccion,
               t.nombre AS turno,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'')
               || ', ' || a.nombres AS nombre_completo
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE s.id IN ({})
    """.format(",".join("?" * len(secciones_ids)))

    params = list(secciones_ids)

    # Filtrar por sección específica si se eligió una
    if seccion_sel != "Todas":
        seccion_elegida = next(
            (s for s in secciones_filtradas
             if s["nombre_completo"] == seccion_sel), None
        )
        if seccion_elegida:
            q += " AND s.id = ?"
            params.append(seccion_elegida["seccion_id"])

    # Filtrar por texto
    if texto.strip():
        for palabra in [p.strip() for p in texto.split() if p.strip()]:
            q += """ AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ?
                          OR a.apellido_materno LIKE ?)"""
            like = f"%{palabra}%"
            params += [like, like, like]

    q += " ORDER BY a.apellido_paterno, a.apellido_materno, a.nombres"

    df = pd.read_sql(q, conn, params=params)

    if df.empty:
        st.info("No hay alumnos en las secciones permitidas con esos filtros.")
        return

    st.write(f"**{len(df)} alumnos** en las secciones permitidas")

    # Alumnos ya seleccionados
    ya_sel = {r["alumno_id"] for r in conn.execute(
        "SELECT alumno_id FROM reforzamiento_alumnos WHERE dia_especial_id=?",
        (ref_id,)
    ).fetchall()}

    with st.form("form_sel_alumnos_ref"):
        seleccionados = []
        for _, al in df.iterrows():
            marcado = al["id"] in ya_sel
            if st.checkbox(
                f"{al['nombre_completo']} - {al['grado']}{al['seccion']} "
                f"({al['turno']})",
                value=marcado, key=f"ref_chk_{al['id']}"
            ):
                seleccionados.append(al["id"])
        guardar = st.form_submit_button("Guardar selección", type="primary",
                                          use_container_width=True)

    if not guardar:
        return
    try:
        conn.execute("DELETE FROM reforzamiento_alumnos "
                     "WHERE dia_especial_id=?", (ref_id,))
        for al_id in seleccionados:
            conn.execute("INSERT INTO reforzamiento_alumnos "
                         "(dia_especial_id, alumno_id) VALUES (?,?)",
                         (ref_id, al_id))
        conn.commit()
        st.session_state["_msg_ref_alumnos"] = f"{len(seleccionados)} alumnos asignados."
        st.rerun()
    except Exception as e:
        st.error(f"Error: {e}")
# menu#########################################################################################################################33
def _control_faltas():
    ult = st.session_state.get("_ult_faltas_ts")
    ahora_ts = time.time()
    if ult and (ahora_ts - ult) < 300:
        return
    st.session_state["_ult_faltas_ts"] = ahora_ts
    marcar_faltas_al_cierre()


def _enrutar(opcion):
    rutas = {
        "Puerta": vista_puerta,
        "TOECE": vista_toece,
        "Panel Dirección": vista_panel_direccion,
        "Reportes y Consultas": vista_reportes,
        "Alumnos": vista_alumnos,
        "Carnets": vista_carnets,
        "Días especiales": vista_dias_especiales,
        "Seleccionar Alumnos Reforzamiento": vista_seleccionar_alumnos_reforzamiento,
        "Escanear Reforzamiento": vista_escanear_reforzamiento,
        "Horarios": vista_horarios,
        "Usuarios": vista_usuarios,
        "Auditoría": vista_auditoria,
    }
    fn = rutas.get(opcion)
    if fn:
        fn()
    else:
        st.warning("Vista no disponible")

def main():
    st.set_page_config(
        page_icon="escudo.png",
        page_title="Asistencia I.E. Yarinacocha",
        layout="wide",
    )

    init_db()
    aplicar_estilos()

    if "user" not in st.session_state:
        vista_login()
        return

    _control_faltas()

    opcion = menu_lateral()
    if opcion is None:
        return

    _enrutar(opcion)


if __name__ == "__main__":
    main()
