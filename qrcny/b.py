import hashlib
import logging
import re
import secrets
import sqlite3
import threading
import time
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import cv2
import extra_streamlit_components as stx
import numpy as np
import pandas as pd
import qrcode
import streamlit as st
from camera_input_live import camera_input_live
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

# ------------------- logs -------------------
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("asistencia")

# ------------------- config -------------------
DB_PATH = "asistencia.db"
MESES_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

PBKDF2_ITERACIONES = 260000
PBKDF2_ALGORITMO = "sha256"
DIAS_TOKEN_SESION = 30
COOKIE_KEY = "asistencia_ie_yarinacocha_token"
COOKIE_NOMBRE = "asistencia_token"
MAX_INTENTOS_LOGIN = 3
MINUTOS_BLOQUEO = 10

ESTADO_PUNTUAL = "Puntual"
ESTADO_TARDANZA = "Tardanza"
ESTADO_FALTA = "Falta"
ESTADO_REF_ASISTIO = "Asistio"
ESTADO_REF_NO_ASISTIO = "No asistio"

ACCION_PERDONADO = "PERDONADO"
ACCION_DERIVADO = "DERIVADO_TOECE"
ACCION_RETENIDO = "RETENIDO_APODERADO"

ROLES_VALIDOS = ("Admin", "TOECE", "Auxiliar", "Direccion")
VENTANA_CLASES = "clases"
VENTANA_REFORZAMIENTO = "reforzamiento"


# ------------------- utilidades -------------------
def ahora():
    return datetime.now(timezone.utc) - timedelta(hours=5)


def hoy_str():
    return ahora().strftime("%Y-%m-%d")


def hora_str():
    return ahora().strftime("%H:%M:%S")


def hora_corta():
    return ahora().strftime("%H:%M")


def timestamp_str():
    return ahora().strftime("%Y-%m-%d %H:%M:%S")


def es_fin_de_semana(fecha=None):
    if fecha is None:
        fecha = ahora().date()
    return fecha.weekday() >= 5


def nombre_mes(mes):
    if 1 <= mes <= 12:
        return MESES_ES[mes]
    return ""


# ------------------- seguridad -------------------
def hashear_password(password):
    salt = secrets.token_bytes(16)
    derivada = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITMO, password.encode("utf-8"), salt, PBKDF2_ITERACIONES
    )
    return ("pbkdf2_" + PBKDF2_ALGORITMO + "$" + str(PBKDF2_ITERACIONES) + "$"
            + salt.hex() + "$" + derivada.hex())


def verificar_password(password, hash_guardado):
    if hash_guardado.startswith("pbkdf2_"):
        try:
            partes = hash_guardado.split("$")
            iteraciones = int(partes[1])
            salt = bytes.fromhex(partes[2])
            hash_hex = partes[3]
            derivada = hashlib.pbkdf2_hmac(
                PBKDF2_ALGORITMO, password.encode("utf-8"), salt, iteraciones
            )
            return secrets.compare_digest(derivada.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    return secrets.compare_digest(
        hashlib.sha256(password.encode()).hexdigest(), hash_guardado)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verificar_password_admin(password):
    con = obtener_conexion()
    filas = con.execute(
        "SELECT password FROM usuarios WHERE rol = 'Admin' AND activo = 1"
    ).fetchall()
    for fila in filas:
        if verificar_password(password, fila["password"]):
            return True
    return False


# ------------------- base de datos -------------------
_conexion = None
_lock = threading.Lock()


def obtener_conexion():
    global _conexion
    if _conexion is None:
        _conexion = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        _conexion.row_factory = sqlite3.Row
        _conexion.execute("PRAGMA journal_mode=WAL")
        _conexion.execute("PRAGMA synchronous=NORMAL")
        _conexion.execute("PRAGMA foreign_keys=ON")
        _conexion.execute("PRAGMA busy_timeout=30000")
        log.info("Conexion SQLite inicializada")
    return _conexion


def existe_columna(cursor, tabla, columna):
    filas = cursor.execute("PRAGMA table_info(" + tabla + ")").fetchall()
    for fila in filas:
        if fila["name"] == columna:
            return True
    return False


def tabla_existe(cursor, tabla):
    fila = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (tabla,)
    ).fetchone()
    if fila:
        return True
    return False


def inicializar_bd():
    con = obtener_conexion()
    cur = con.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS periodos (
        id INTEGER PRIMARY KEY, nombre TEXT NOT NULL,
        fecha_inicio TEXT, fecha_fin TEXT,
        activo INTEGER DEFAULT 0, fecha_cierre TEXT,
        cerrado INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS turnos (
        id INTEGER PRIMARY KEY, nombre TEXT UNIQUE,
        hora_entrada TEXT, hora_salida TEXT,
        tolerancia_min INTEGER DEFAULT 10
    );
    CREATE TABLE IF NOT EXISTS grados (
        id INTEGER PRIMARY KEY, nombre TEXT UNIQUE
    );
    CREATE TABLE IF NOT EXISTS secciones (
        id INTEGER PRIMARY KEY, nombre TEXT,
        grado_id INTEGER, turno_id INTEGER,
        FOREIGN KEY (grado_id) REFERENCES grados(id),
        FOREIGN KEY (turno_id) REFERENCES turnos(id),
        UNIQUE(nombre, grado_id, turno_id)
    );
    CREATE TABLE IF NOT EXISTS apoderados (
        id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, telefono TEXT
    );
    CREATE TABLE IF NOT EXISTS alumnos (
        id INTEGER PRIMARY KEY, dni TEXT UNIQUE NOT NULL,
        nombres TEXT NOT NULL, apellido_paterno TEXT NOT NULL,
        apellido_materno TEXT, seccion_id INTEGER, apoderado_id INTEGER,
        nombre_apoderado TEXT, telefono_apoderado TEXT, periodo_id INTEGER,
        activo INTEGER DEFAULT 1, retirado_en TEXT,
        FOREIGN KEY (seccion_id) REFERENCES secciones(id),
        FOREIGN KEY (apoderado_id) REFERENCES apoderados(id),
        FOREIGN KEY (periodo_id) REFERENCES periodos(id)
    );
    CREATE TABLE IF NOT EXISTS ventanas (
        id INTEGER PRIMARY KEY, turno_id INTEGER NOT NULL,
        tipo TEXT NOT NULL CHECK (tipo IN ('clases','reforzamiento')),
        nombre TEXT NOT NULL, hora_apertura TEXT NOT NULL,
        hora_limite_puntual TEXT, hora_cierre TEXT NOT NULL,
        tolerancia_min INTEGER DEFAULT 0, orden INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1,
        FOREIGN KEY (turno_id) REFERENCES turnos(id)
    );
    CREATE TABLE IF NOT EXISTS asistencias (
        id INTEGER PRIMARY KEY, alumno_id INTEGER NOT NULL,
        fecha TEXT NOT NULL, ventana_id INTEGER,
        tipo TEXT NOT NULL DEFAULT 'clases',
        hora TEXT, estado TEXT NOT NULL,
        justificada INTEGER DEFAULT 0, observacion TEXT, periodo_id INTEGER,
        FOREIGN KEY (alumno_id) REFERENCES alumnos(id) ON DELETE CASCADE,
        FOREIGN KEY (ventana_id) REFERENCES ventanas(id),
        FOREIGN KEY (periodo_id) REFERENCES periodos(id),
        UNIQUE(alumno_id, fecha, tipo)
    );
    CREATE TABLE IF NOT EXISTS tardanzas (
        id INTEGER PRIMARY KEY, alumno_id INTEGER NOT NULL,
        fecha TEXT NOT NULL, hora TEXT NOT NULL, numero INTEGER NOT NULL,
        accion TEXT NOT NULL, observacion TEXT,
        justificada INTEGER DEFAULT 0, registrado_por TEXT,
        timestamp TEXT NOT NULL, periodo_id INTEGER,
        FOREIGN KEY (alumno_id) REFERENCES alumnos(id) ON DELETE CASCADE,
        FOREIGN KEY (periodo_id) REFERENCES periodos(id),
        UNIQUE(alumno_id, fecha)
    );
    CREATE TABLE IF NOT EXISTS actas_compromiso (
        id INTEGER PRIMARY KEY, alumno_id INTEGER NOT NULL,
        fecha TEXT NOT NULL, motivo TEXT, observacion TEXT,
        registrado_por TEXT, timestamp TEXT NOT NULL, periodo_id INTEGER,
        FOREIGN KEY (alumno_id) REFERENCES alumnos(id) ON DELETE CASCADE,
        FOREIGN KEY (periodo_id) REFERENCES periodos(id)
    );
    CREATE TABLE IF NOT EXISTS observados (
        id INTEGER PRIMARY KEY, alumno_id INTEGER NOT NULL,
        fecha_ingreso TEXT NOT NULL, motivo TEXT,
        activo INTEGER DEFAULT 1, fecha_salida TEXT,
        observacion_cierre TEXT, periodo_id INTEGER,
        FOREIGN KEY (alumno_id) REFERENCES alumnos(id) ON DELETE CASCADE,
        FOREIGN KEY (periodo_id) REFERENCES periodos(id)
    );
    CREATE TABLE IF NOT EXISTS bloqueos (
        id INTEGER PRIMARY KEY, alumno_id INTEGER NOT NULL,
        motivo TEXT, activo INTEGER DEFAULT 1,
        fecha_inicio TEXT NOT NULL, fecha_fin TEXT,
        liberado_por TEXT, creado_por TEXT, origen TEXT DEFAULT 'automatico',
        FOREIGN KEY (alumno_id) REFERENCES alumnos(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS justificaciones_previas (
        id INTEGER PRIMARY KEY, alumno_id INTEGER NOT NULL,
        fecha_objetivo TEXT NOT NULL,
        tipo TEXT NOT NULL CHECK (tipo IN ('Falta','Tardanza')),
        motivo TEXT, creado_por TEXT, timestamp TEXT NOT NULL,
        aplicada INTEGER DEFAULT 0,
        FOREIGN KEY (alumno_id) REFERENCES alumnos(id) ON DELETE CASCADE,
        UNIQUE(alumno_id, fecha_objetivo, tipo)
    );
    CREATE TABLE IF NOT EXISTS dias_especiales (
        id INTEGER PRIMARY KEY, fecha TEXT NOT NULL, descripcion TEXT,
        turno_id INTEGER, hora_entrada TEXT, activo INTEGER DEFAULT 1,
        tipo TEXT DEFAULT 'evento' CHECK (tipo IN ('evento','feriado')),
        periodo_id INTEGER,
        FOREIGN KEY (turno_id) REFERENCES turnos(id),
        FOREIGN KEY (periodo_id) REFERENCES periodos(id)
    );
    CREATE TABLE IF NOT EXISTS dias_especiales_secciones (
        id INTEGER PRIMARY KEY, dia_especial_id INTEGER NOT NULL,
        seccion_id INTEGER NOT NULL,
        FOREIGN KEY (dia_especial_id) REFERENCES dias_especiales(id) ON DELETE CASCADE,
        FOREIGN KEY (seccion_id) REFERENCES secciones(id),
        UNIQUE(dia_especial_id, seccion_id)
    );
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY, usuario TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        rol TEXT NOT NULL CHECK (rol IN ('Admin','TOECE','Auxiliar','Direccion')),
        nombres TEXT NOT NULL, turno_asignado INTEGER,
        activo INTEGER DEFAULT 1, intentos_fallidos INTEGER DEFAULT 0,
        bloqueado_hasta TEXT, debe_cambiar_password INTEGER DEFAULT 0,
        ultimo_login TEXT, ultimo_ip TEXT,
        FOREIGN KEY (turno_asignado) REFERENCES turnos(id)
    );
    CREATE TABLE IF NOT EXISTS auxiliar_secciones (
        id INTEGER PRIMARY KEY, usuario_id INTEGER NOT NULL,
        seccion_id INTEGER NOT NULL,
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE,
        FOREIGN KEY (seccion_id) REFERENCES secciones(id) ON DELETE CASCADE,
        UNIQUE(usuario_id, seccion_id)
    );
    CREATE TABLE IF NOT EXISTS auditoria (
        id INTEGER PRIMARY KEY, usuario TEXT, accion TEXT, fecha TEXT,
        valor_anterior TEXT, valor_nuevo TEXT,
        tabla_afectada TEXT, registro_id INTEGER, ip TEXT
    );
    CREATE TABLE IF NOT EXISTS sesiones_tokens (
        id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL,
        usuario_id INTEGER NOT NULL, expira TEXT NOT NULL,
        creado TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS cierres_anuales (
        id INTEGER PRIMARY KEY, periodo_id INTEGER NOT NULL,
        fecha_cierre TEXT NOT NULL, generado_por TEXT, reporte_json TEXT,
        FOREIGN KEY (periodo_id) REFERENCES periodos(id)
    );
    CREATE INDEX IF NOT EXISTS idx_asistencias_fecha ON asistencias(fecha);
    CREATE INDEX IF NOT EXISTS idx_asistencias_alumno ON asistencias(alumno_id);
    CREATE INDEX IF NOT EXISTS idx_tardanzas_alumno ON tardanzas(alumno_id);
    CREATE INDEX IF NOT EXISTS idx_tardanzas_fecha ON tardanzas(fecha);
    CREATE INDEX IF NOT EXISTS idx_alumnos_dni ON alumnos(dni);
    CREATE INDEX IF NOT EXISTS idx_alumnos_seccion ON alumnos(seccion_id);
    CREATE INDEX IF NOT EXISTS idx_ventanas_turno ON ventanas(turno_id, tipo);
    CREATE INDEX IF NOT EXISTS idx_bloqueos_alumno ON bloqueos(alumno_id, activo);
    CREATE INDEX IF NOT EXISTS idx_just_previas ON justificaciones_previas(alumno_id, fecha_objetivo);
    CREATE INDEX IF NOT EXISTS idx_tokens ON sesiones_tokens(token);
    CREATE INDEX IF NOT EXISTS idx_auditoria_fecha ON auditoria(fecha);
    """)

    migrar_esquema(cur)
    seed_inicial(cur)
    con.commit()
    log.info("Base de datos inicializada")


def migrar_esquema(cur):
    migraciones = [
        ("alumnos", "activo", "ALTER TABLE alumnos ADD COLUMN activo INTEGER DEFAULT 1"),
        ("alumnos", "retirado_en", "ALTER TABLE alumnos ADD COLUMN retirado_en TEXT"),
        ("usuarios", "ultimo_login", "ALTER TABLE usuarios ADD COLUMN ultimo_login TEXT"),
        ("usuarios", "ultimo_ip", "ALTER TABLE usuarios ADD COLUMN ultimo_ip TEXT"),
        ("auditoria", "ip", "ALTER TABLE auditoria ADD COLUMN ip TEXT"),
        ("asistencias", "tipo", "ALTER TABLE asistencias ADD COLUMN tipo TEXT DEFAULT 'clases'"),
        ("asistencias", "ventana_id", "ALTER TABLE asistencias ADD COLUMN ventana_id INTEGER"),
        ("periodos", "cerrado", "ALTER TABLE periodos ADD COLUMN cerrado INTEGER DEFAULT 0"),
        ("observados", "observacion_cierre", "ALTER TABLE observados ADD COLUMN observacion_cierre TEXT"),
        ("bloqueos", "origen", "ALTER TABLE bloqueos ADD COLUMN origen TEXT DEFAULT 'automatico'"),
    ]
    for item in migraciones:
        tabla = item[0]
        columna = item[1]
        sql = item[2]
        if tabla_existe(cur, tabla) and not existe_columna(cur, tabla, columna):
            try:
                cur.execute(sql)
            except sqlite3.Error as err:
                log.warning("no se pudo migrar " + tabla + "." + columna + ": " + str(err))


def seed_inicial(cur):
    r = cur.execute("SELECT COUNT(*) FROM turnos").fetchone()
    if r[0] == 0:
        cur.execute("INSERT INTO turnos (nombre, hora_entrada, hora_salida, tolerancia_min) VALUES ('Mañana','06:00','12:25',10)")
        cur.execute("INSERT INTO turnos (nombre, hora_entrada, hora_salida, tolerancia_min) VALUES ('Tarde','12:00','18:10',10)")

    r = cur.execute("SELECT COUNT(*) FROM ventanas").fetchone()
    if r[0] == 0:
        tm = cur.execute("SELECT id FROM turnos WHERE nombre='Mañana'").fetchone()
        tt = cur.execute("SELECT id FROM turnos WHERE nombre='Tarde'").fetchone()
        if tm:
            cur.execute("INSERT INTO ventanas (turno_id, tipo, nombre, hora_apertura, hora_limite_puntual, hora_cierre, tolerancia_min, orden) VALUES (?, 'clases','Clases mañana','06:00','06:55','07:10',10,1)", (tm["id"],))
            cur.execute("INSERT INTO ventanas (turno_id, tipo, nombre, hora_apertura, hora_limite_puntual, hora_cierre, tolerancia_min, orden) VALUES (?, 'reforzamiento','Reforzamiento mañana','08:00','08:00','14:00',0,2)", (tm["id"],))
        if tt:
            cur.execute("INSERT INTO ventanas (turno_id, tipo, nombre, hora_apertura, hora_limite_puntual, hora_cierre, tolerancia_min, orden) VALUES (?, 'reforzamiento','Reforzamiento tarde','10:00','10:00','10:20',0,1)", (tt["id"],))
            cur.execute("INSERT INTO ventanas (turno_id, tipo, nombre, hora_apertura, hora_limite_puntual, hora_cierre, tolerancia_min, orden) VALUES (?, 'clases','Clases tarde','12:00','12:49','18:10',10,2)", (tt["id"],))

    r = cur.execute("SELECT COUNT(*) FROM grados").fetchone()
    if r[0] == 0:
        for g in ["1ro", "2do", "3ro", "4to", "5to"]:
            cur.execute("INSERT INTO grados (nombre) VALUES (?)", (g,))

    r = cur.execute("SELECT COUNT(*) FROM usuarios").fetchone()
    if r[0] == 0:
        pwd = secrets.token_urlsafe(9)
        cur.execute("INSERT INTO usuarios (usuario, password, rol, nombres, debe_cambiar_password) VALUES (?, ?, 'Admin', 'Administrador', 1)",
                    ("admin", hashear_password(pwd)))
        log.warning("Usuario admin creado. Password temporal: " + pwd)


# ------------------- sesion / cookies -------------------
def get_cookie_manager():
    return stx.CookieManager(key=COOKIE_KEY)


def crear_token_sesion(usuario):
    token = secrets.token_urlsafe(32)
    token_hash = hash_token(token)
    con = obtener_conexion()
    expira = (ahora() + timedelta(days=DIAS_TOKEN_SESION)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("INSERT INTO sesiones_tokens (token, usuario_id, expira) VALUES (?, ?, ?)",
                (token_hash, usuario["id"], expira))
    con.commit()
    return token


def restaurar_sesion(token):
    if not token:
        return None
    token_hash = hash_token(token)
    con = obtener_conexion()
    fila = con.execute(
        "SELECT u.* FROM usuarios u JOIN sesiones_tokens st ON u.id = st.usuario_id WHERE st.token = ? AND st.expira > ? AND u.activo = 1",
        (token_hash, timestamp_str())
    ).fetchone()
    if fila:
        return dict(fila)
    return None


def eliminar_token(token):
    if not token:
        return
    token_hash = hash_token(token)
    con = obtener_conexion()
    con.execute("DELETE FROM sesiones_tokens WHERE token = ?", (token_hash,))
    con.commit()


def leer_token_cookie():
    try:
        return st.context.cookies.get(COOKIE_NOMBRE)
    except Exception:
        return None


def leer_token_query():
    try:
        return st.query_params.get("t")
    except Exception:
        return None


def guardar_token_cookie(token):
    try:
        cookies = get_cookie_manager()
        cookies.set(COOKIE_NOMBRE, token,
                    expires_at=datetime.now() + timedelta(days=DIAS_TOKEN_SESION))
    except Exception as err:
        log.warning("error guardando cookie: " + str(err))


def borrar_token_cookie():
    try:
        cookies = get_cookie_manager()
        cookies.delete(COOKIE_NOMBRE)
    except Exception as err:
        log.warning("error borrando cookie: " + str(err))


def inicializar_sesion():
    if "user" in st.session_state and st.session_state["user"]:
        return
    token = leer_token_cookie()
    if not token:
        try:
            cookies = get_cookie_manager()
            token = cookies.get(COOKIE_NOMBRE)
        except Exception:
            token = None
    if not token:
        token = leer_token_query()
    if token:
        usuario = restaurar_sesion(token)
        if usuario:
            st.session_state["user"] = usuario
            st.session_state["_token"] = token
            st.session_state["_token_expira"] = time.time() + 300


def refrescar_sesion_si_necesario():
    if "user" not in st.session_state or not st.session_state["user"]:
        return
    expira = st.session_state.get("_token_expira", 0)
    if time.time() < expira - 60:
        return
    try:
        usuario = st.session_state["user"]
        viejo = st.session_state.get("_token")
        if viejo:
            eliminar_token(viejo)
        nuevo = crear_token_sesion(usuario)
        st.session_state["_token"] = nuevo
        st.session_state["_token_expira"] = time.time() + 300
        guardar_token_cookie(nuevo)
    except Exception as err:
        log.warning("error refrescando sesion: " + str(err))


# ------------------- login / auth -------------------
def esta_bloqueado(usuario):
    if not usuario.get("bloqueado_hasta"):
        return False
    try:
        limite = datetime.strptime(usuario["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S")
        return ahora() < limite
    except (ValueError, TypeError):
        return False


def ip_actual():
    try:
        return st.context.headers.get("X-Forwarded-For", "local")
    except Exception:
        return "local"


def autenticar(nombre_usuario, password):
    con = obtener_conexion()
    fila = con.execute("SELECT * FROM usuarios WHERE usuario = ? AND activo = 1",
                       (nombre_usuario,)).fetchone()
    if not fila:
        return None, "Usuario no encontrado o inactivo"
    usuario = dict(fila)
    if esta_bloqueado(usuario):
        limite = datetime.strptime(usuario["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S")
        minutos = int((limite - ahora()).total_seconds() / 60) + 1
        return None, "Cuenta bloqueada. Intenta en " + str(minutos) + " minuto(s)"
    if not verificar_password(password, usuario["password"]):
        intentos = (usuario.get("intentos_fallidos") or 0) + 1
        if intentos >= MAX_INTENTOS_LOGIN:
            bh = (ahora() + timedelta(minutes=MINUTOS_BLOQUEO)).strftime("%Y-%m-%d %H:%M:%S")
            con.execute("UPDATE usuarios SET intentos_fallidos = 0, bloqueado_hasta = ? WHERE id = ?",
                        (bh, usuario["id"]))
            con.commit()
            auditar(nombre_usuario, "Cuenta bloqueada por intentos fallidos")
            return None, "Cuenta bloqueada por " + str(MINUTOS_BLOQUEO) + " minutos"
        con.execute("UPDATE usuarios SET intentos_fallidos = ? WHERE id = ?",
                    (intentos, usuario["id"]))
        con.commit()
        return None, "Credenciales incorrectas. Te quedan " + str(MAX_INTENTOS_LOGIN - intentos) + " intento(s)"
    con.execute("UPDATE usuarios SET intentos_fallidos = 0, bloqueado_hasta = NULL, ultimo_login = ?, ultimo_ip = ? WHERE id = ?",
                (timestamp_str(), ip_actual(), usuario["id"]))
    con.commit()
    return usuario, ""


def auditar(usuario, accion, valor_anterior=None, valor_nuevo=None,
            tabla_afectada=None, registro_id=None):
    try:
        con = obtener_conexion()
        con.execute(
            "INSERT INTO auditoria (usuario, accion, fecha, valor_anterior, valor_nuevo, tabla_afectada, registro_id, ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (usuario, accion, timestamp_str(), valor_anterior, valor_nuevo,
             tabla_afectada, registro_id, ip_actual())
        )
        con.commit()
    except Exception as err:
        log.warning("error auditando: " + str(err))


# ------------------- periodos -------------------
def obtener_periodo_activo():
    con = obtener_conexion()
    fila = con.execute("SELECT * FROM periodos WHERE activo = 1 LIMIT 1").fetchone()
    if fila:
        return dict(fila)
    return None


def periodo_tiene_alumnos(id_periodo):
    con = obtener_conexion()
    r = con.execute("SELECT COUNT(*) FROM alumnos WHERE periodo_id = ? AND activo = 1",
                    (id_periodo,)).fetchone()
    if r[0] and r[0] > 0:
        return True
    return False


def sistema_bloqueado():
    periodo = obtener_periodo_activo()
    if not periodo:
        return True
    if not periodo_tiene_alumnos(periodo["id"]):
        return True
    return False


def listar_periodos():
    con = obtener_conexion()
    return pd.read_sql("SELECT id, nombre, fecha_inicio, fecha_fin, activo, cerrado FROM periodos ORDER BY id DESC", con)


def listar_periodos_cerrados():
    con = obtener_conexion()
    return pd.read_sql("SELECT id, nombre, fecha_inicio, fecha_fin, fecha_cierre FROM periodos WHERE cerrado = 1 ORDER BY id DESC", con)


def crear_periodo(nombre, fecha_inicio, fecha_fin, usuario):
    con = obtener_conexion()
    try:
        cur = con.execute("INSERT INTO periodos (nombre, fecha_inicio, fecha_fin, activo, cerrado) VALUES (?, ?, ?, 1, 0)",
                          (nombre, fecha_inicio, fecha_fin))
        id_nuevo = cur.lastrowid
        con.execute("UPDATE periodos SET activo = 0 WHERE id != ?", (id_nuevo,))
        con.commit()
        auditar(usuario["usuario"], "Creo periodo " + nombre)
        return True, "Periodo " + nombre + " creado y activado."
    except sqlite3.Error as err:
        return False, "Error: " + str(err)


def activar_periodo(id_periodo, usuario):
    con = obtener_conexion()
    fila = con.execute("SELECT cerrado FROM periodos WHERE id = ?", (id_periodo,)).fetchone()
    if not fila:
        return False, "Periodo no encontrado."
    if fila["cerrado"]:
        return False, "Ese periodo esta cerrado y no se puede reactivar."
    con.execute("UPDATE periodos SET activo = 0")
    con.execute("UPDATE periodos SET activo = 1 WHERE id = ?", (id_periodo,))
    con.commit()
    auditar(usuario["usuario"], "Activo periodo id=" + str(id_periodo))
    return True, "Periodo activado correctamente."


# ------------------- turnos / ventanas -------------------
def listar_turnos():
    con = obtener_conexion()
    filas = con.execute("SELECT * FROM turnos ORDER BY id").fetchall()
    lista = []
    for f in filas:
        lista.append(dict(f))
    return lista


def listar_ventanas(id_turno=None):
    con = obtener_conexion()
    if id_turno:
        filas = con.execute("SELECT * FROM ventanas WHERE turno_id = ? AND activo = 1 ORDER BY orden, id",
                            (id_turno,)).fetchall()
    else:
        filas = con.execute("SELECT * FROM ventanas WHERE activo = 1 ORDER BY turno_id, orden").fetchall()
    lista = []
    for f in filas:
        lista.append(dict(f))
    return lista


def dia_especial_hoy(id_turno, fecha, id_seccion=None):
    con = obtener_conexion()
    fila = con.execute(
        "SELECT * FROM dias_especiales WHERE fecha = ? AND activo = 1 AND (turno_id = ? OR turno_id IS NULL) ORDER BY turno_id DESC LIMIT 1",
        (fecha, id_turno)
    ).fetchone()
    if not fila:
        return None
    dia = dict(fila)
    if id_seccion:
        secciones_dia = con.execute(
            "SELECT seccion_id FROM dias_especiales_secciones WHERE dia_especial_id = ?",
            (dia["id"],)
        ).fetchall()
        if secciones_dia:
            ids = []
            for s in secciones_dia:
                ids.append(s["seccion_id"])
            if id_seccion not in ids:
                return None
    return dia


def ventana_activa_para_alumno(id_turno, fecha, id_seccion=None):
    dia = dia_especial_hoy(id_turno, fecha, id_seccion)
    if dia and dia["tipo"] == "feriado":
        return None
    hora_actual = hora_corta()
    ventanas = listar_ventanas(id_turno)
    hora_especial = None
    if dia and dia["tipo"] == "evento":
        hora_especial = dia["hora_entrada"]
    for v in ventanas:
        apertura = v["hora_apertura"]
        if v["tipo"] == VENTANA_CLASES and hora_especial:
            apertura = hora_especial
        limite = v["hora_limite_puntual"] or apertura
        if apertura <= hora_actual <= v["hora_cierre"]:
            resultado = dict(v)
            resultado["hora_apertura_efectiva"] = apertura
            resultado["hora_limite_efectiva"] = limite
            return resultado
    return None


# ------------------- grados / secciones / alumnos -------------------
def listar_grados():
    con = obtener_conexion()
    filas = con.execute("SELECT * FROM grados ORDER BY nombre").fetchall()
    lista = []
    for f in filas:
        lista.append(dict(f))
    return lista


def secciones_por_grado(id_grado):
    con = obtener_conexion()
    filas = con.execute("SELECT * FROM secciones WHERE grado_id = ? ORDER BY nombre",
                        (id_grado,)).fetchall()
    lista = []
    for f in filas:
        lista.append(dict(f))
    return lista


def secciones_por_turno(id_turno):
    con = obtener_conexion()
    filas = con.execute(
        "SELECT s.*, g.nombre AS grado FROM secciones s JOIN grados g ON s.grado_id = g.id WHERE s.turno_id = ? ORDER BY g.nombre, s.nombre",
        (id_turno,)
    ).fetchall()
    lista = []
    for f in filas:
        lista.append(dict(f))
    return lista


def listar_todas_secciones():
    con = obtener_conexion()
    filas = con.execute(
        "SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno, g.id AS grado_id, t.id AS turno_id FROM secciones s JOIN grados g ON s.grado_id = g.id JOIN turnos t ON s.turno_id = t.id ORDER BY t.nombre, g.nombre, s.nombre"
    ).fetchall()
    lista = []
    for f in filas:
        lista.append(dict(f))
    return lista


def alumnos_de_seccion(id_seccion):
    con = obtener_conexion()
    return pd.read_sql(
        "SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno, a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') || ', ' || a.nombres AS nombre_completo FROM alumnos a WHERE a.seccion_id = ? AND a.activo = 1 ORDER BY a.apellido_paterno, a.apellido_materno, a.nombres",
        con, params=[id_seccion]
    )


def buscar_alumnos(texto, id_grado=None, id_seccion=None, limite=200):
    con = obtener_conexion()
    consulta = ("SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno, "
                "g.id AS grado_id, g.nombre AS grado, s.id AS seccion_id, s.nombre AS seccion, "
                "t.nombre AS turno, "
                "a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') || ', ' || a.nombres AS nombre_completo "
                "FROM alumnos a "
                "JOIN secciones s ON a.seccion_id = s.id "
                "JOIN grados g ON s.grado_id = g.id "
                "JOIN turnos t ON s.turno_id = t.id "
                "WHERE a.activo = 1")
    params = []
    if texto:
        for palabra in texto.split():
            palabra = palabra.strip()
            if palabra:
                consulta += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
                patron = "%" + palabra + "%"
                params.append(patron)
                params.append(patron)
                params.append(patron)
    if id_grado:
        consulta += " AND g.id = ?"
        params.append(id_grado)
    if id_seccion:
        consulta += " AND s.id = ?"
        params.append(id_seccion)
    consulta += " ORDER BY a.apellido_paterno LIMIT ?"
    params.append(limite)
    return pd.read_sql(consulta, con, params=params)


def buscar_alumno_por_dni(con, dni):
    fila = con.execute(
        "SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno, s.id AS seccion_id, s.nombre AS seccion, g.nombre AS grado, t.id AS turno_id, t.nombre AS turno FROM alumnos a JOIN secciones s ON a.seccion_id = s.id JOIN grados g ON s.grado_id = g.id JOIN turnos t ON s.turno_id = t.id WHERE a.dni = ? AND a.activo = 1",
        (dni,)
    ).fetchone()
    if fila:
        return dict(fila)
    return None


def nombre_completo(alumno):
    materno = alumno["apellido_materno"] or ""
    return (alumno["apellido_paterno"] + " " + materno + ", " + alumno["nombres"]).strip(", ")


def crear_alumno(dni, nombres, apellido_paterno, apellido_materno, id_seccion,
                 apoderado_nombre, apoderado_telefono, usuario):
    con = obtener_conexion()
    periodo = obtener_periodo_activo()
    if not periodo:
        return False, "No hay periodo activo."
    try:
        cur = con.cursor()
        id_apoderado = None
        if apoderado_nombre:
            fila = cur.execute("SELECT id FROM apoderados WHERE nombre = ? AND COALESCE(telefono,'') = ?",
                               (apoderado_nombre, apoderado_telefono or "")).fetchone()
            if fila:
                id_apoderado = fila["id"]
            else:
                id_apoderado = cur.execute("INSERT INTO apoderados (nombre, telefono) VALUES (?, ?)",
                                           (apoderado_nombre, apoderado_telefono or None)).lastrowid
        cur.execute(
            "INSERT INTO alumnos (dni, nombres, apellido_paterno, apellido_materno, seccion_id, apoderado_id, nombre_apoderado, telefono_apoderado, periodo_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dni, nombres, apellido_paterno, apellido_materno or None, id_seccion,
             id_apoderado, apoderado_nombre or None, apoderado_telefono or None, periodo["id"])
        )
        con.commit()
        auditar(usuario["usuario"], "Creo alumno DNI " + dni)
        return True, "Alumno " + nombres + " creado correctamente."
    except sqlite3.IntegrityError:
        return False, "Ya existe un alumno con DNI " + dni + "."
    except sqlite3.Error as err:
        log.error("error creando alumno: " + str(err))
        return False, "Error al crear el alumno."


def editar_alumno(id_alumno, apoderado_nombre, apoderado_telefono, id_seccion, dni, usuario):
    con = obtener_conexion()
    cur = con.cursor()
    id_apoderado = None
    if apoderado_nombre:
        fila = cur.execute("SELECT id FROM apoderados WHERE nombre = ? AND COALESCE(telefono,'') = ?",
                           (apoderado_nombre, apoderado_telefono or "")).fetchone()
        if fila:
            id_apoderado = fila["id"]
        else:
            id_apoderado = cur.execute("INSERT INTO apoderados (nombre, telefono) VALUES (?, ?)",
                                       (apoderado_nombre, apoderado_telefono or None)).lastrowid
    con.execute("UPDATE alumnos SET apoderado_id = ?, nombre_apoderado = ?, telefono_apoderado = ?, seccion_id = ? WHERE id = ?",
                (id_apoderado, apoderado_nombre or None, apoderado_telefono or None, id_seccion, id_alumno))
    con.commit()
    auditar(usuario["usuario"], "Edito alumno " + dni)
    return True, "Alumno editado correctamente."


def retirar_alumno(id_alumno, dni, usuario):
    con = obtener_conexion()
    con.execute("UPDATE alumnos SET activo = 0, retirado_en = ? WHERE id = ?",
                (timestamp_str(), id_alumno))
    con.commit()
    auditar(usuario["usuario"], "Desactivo alumno DNI " + dni)
    return True, "Alumno desactivado correctamente."


def reactivar_alumno(id_alumno, dni, usuario):
    con = obtener_conexion()
    con.execute("UPDATE alumnos SET activo = 1, retirado_en = NULL WHERE id = ?", (id_alumno,))
    con.commit()
    auditar(usuario["usuario"], "Reactivo alumno DNI " + dni)
    return True, "Alumno reactivado correctamente."


# ------------------- importar excel -------------------
def normalizar_grado(nombre):
    nombre = (nombre or "").strip().title()
    reemplazos = {
        "1°": "1ro", "2°": "2do", "3°": "3ro", "4°": "4to", "5°": "5to",
        "1o": "1ro", "2o": "2do", "3o": "3ro", "4o": "4to", "5o": "5to",
        "1ero": "1ro", "3ero": "3ro",
    }
    if nombre in reemplazos:
        return reemplazos[nombre]
    return nombre


def validar_importacion(df, mapeo):
    errores = []
    validas = []
    con = obtener_conexion()
    dnis_vistos = {}
    for indice, fila in df.iterrows():
        nf = indice + 2
        try:
            dni = str(fila[mapeo["dni"]]).strip()
            nombres = str(fila[mapeo["nombres"]]).strip()
            ap = str(fila[mapeo["apellido_paterno"]]).strip()
            am = ""
            if mapeo.get("apellido_materno"):
                am = str(fila[mapeo["apellido_materno"]]).strip()
            grado = normalizar_grado(str(fila[mapeo["grado"]]))
            seccion = str(fila[mapeo["seccion"]]).strip().upper()
            turno = str(fila[mapeo["turno"]]).strip().lower()
            an = ""
            if mapeo.get("apoderado_nombre"):
                an = str(fila[mapeo["apoderado_nombre"]]).strip()
            at = ""
            if mapeo.get("apoderado_telefono"):
                at = str(fila[mapeo["apoderado_telefono"]]).strip()

            if not dni:
                errores.append({"fila": nf, "motivo": "DNI vacio"})
                continue
            if not re.fullmatch(r"\d{8}", dni):
                errores.append({"fila": nf, "motivo": "DNI invalido '" + dni + "'"})
                continue
            if dni in dnis_vistos:
                errores.append({"fila": nf, "motivo": "DNI " + dni + " duplicado"})
                continue
            if not nombres or not ap or not grado or not seccion:
                errores.append({"fila": nf, "motivo": "Faltan campos"})
                continue

            fg = con.execute("SELECT id FROM grados WHERE nombre = ?", (grado,)).fetchone()
            if not fg:
                errores.append({"fila": nf, "motivo": "Grado '" + grado + "' no existe"})
                continue

            if turno in ("mañana", "manana", "m", "am", "mñ"):
                tn = "Mañana"
            elif turno in ("tarde", "t", "tm", "pm"):
                tn = "Tarde"
            else:
                errores.append({"fila": nf, "motivo": "Turno '" + turno + "'"})
                continue

            dnis_vistos[dni] = nf
            validas.append({
                "dni": dni, "nombres": nombres, "apellido_paterno": ap,
                "apellido_materno": am, "grado": grado, "seccion": seccion,
                "turno": tn, "apoderado_nombre": an, "apoderado_telefono": at
            })
        except (KeyError, ValueError, TypeError) as err:
            errores.append({"fila": nf, "motivo": "Error: " + str(err)})

    resumen = {"total": len(df), "validas": len(validas), "errores": len(errores)}
    return validas, errores, resumen


def insertar_alumnos_validos(validas):
    con = obtener_conexion()
    cur = con.cursor()

    mapa_turnos = {}
    for f in cur.execute("SELECT id, nombre FROM turnos").fetchall():
        mapa_turnos[f["nombre"]] = f["id"]

    periodo = obtener_periodo_activo()
    if not periodo:
        return 0, 0, ["No hay periodo activo."]
    id_periodo = periodo["id"]

    insertados = 0
    reactivados = 0
    errores = []

    for i, datos in enumerate(validas):
        try:
            fg = cur.execute("SELECT id FROM grados WHERE nombre = ?", (datos["grado"],)).fetchone()
            if not fg:
                errores.append("fila " + str(i + 1) + ": grado no reconocido")
                continue
            id_grado = fg["id"]

            id_turno = mapa_turnos.get(datos["turno"])
            if not id_turno:
                errores.append("fila " + str(i + 1) + ": turno no encontrado")
                continue

            fs = cur.execute("SELECT id FROM secciones WHERE nombre = ? AND grado_id = ? AND turno_id = ?",
                             (datos["seccion"], id_grado, id_turno)).fetchone()
            if fs:
                id_seccion = fs["id"]
            else:
                id_seccion = cur.execute("INSERT INTO secciones (nombre, grado_id, turno_id) VALUES (?, ?, ?)",
                                         (datos["seccion"], id_grado, id_turno)).lastrowid

            id_apoderado = None
            if datos["apoderado_nombre"]:
                fa = cur.execute("SELECT id FROM apoderados WHERE nombre = ? AND COALESCE(telefono,'') = ?",
                                 (datos["apoderado_nombre"], datos["apoderado_telefono"] or "")).fetchone()
                if fa:
                    id_apoderado = fa["id"]
                else:
                    id_apoderado = cur.execute("INSERT INTO apoderados (nombre, telefono) VALUES (?, ?)",
                                               (datos["apoderado_nombre"],
                                                datos["apoderado_telefono"] or None)).lastrowid

            existente = cur.execute("SELECT id FROM alumnos WHERE dni = ?", (datos["dni"],)).fetchone()
            if existente:
                cur.execute(
                    "UPDATE alumnos SET nombres = ?, apellido_paterno = ?, apellido_materno = ?, seccion_id = ?, apoderado_id = ?, nombre_apoderado = ?, telefono_apoderado = ?, periodo_id = ?, activo = 1, retirado_en = NULL WHERE id = ?",
                    (datos["nombres"], datos["apellido_paterno"], datos["apellido_materno"],
                     id_seccion, id_apoderado, datos["apoderado_nombre"] or None,
                     datos["apoderado_telefono"] or None, id_periodo, existente["id"])
                )
                reactivados += 1
            else:
                cur.execute(
                    "INSERT INTO alumnos (dni, nombres, apellido_paterno, apellido_materno, seccion_id, apoderado_id, nombre_apoderado, telefono_apoderado, periodo_id, activo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (datos["dni"], datos["nombres"], datos["apellido_paterno"],
                     datos["apellido_materno"], id_seccion, id_apoderado,
                     datos["apoderado_nombre"] or None,
                     datos["apoderado_telefono"] or None, id_periodo)
                )
                insertados += 1
        except sqlite3.Error as err:
            errores.append("fila " + str(i + 1) + ": " + str(err))

    con.commit()
    return insertados, reactivados, errores


# ------------------- bloqueos -------------------
def alumno_bloqueado(id_alumno):
    con = obtener_conexion()
    fila = con.execute("SELECT * FROM bloqueos WHERE alumno_id = ? AND activo = 1 ORDER BY id DESC LIMIT 1",
                       (id_alumno,)).fetchone()
    if fila:
        return dict(fila)
    return None


def crear_bloqueo(id_alumno, motivo, usuario, origen="automatico"):
    con = obtener_conexion()
    con.execute("INSERT INTO bloqueos (alumno_id, motivo, activo, fecha_inicio, creado_por, origen) VALUES (?, ?, 1, ?, ?, ?)",
                (id_alumno, motivo, hoy_str(), usuario["usuario"], origen))
    con.commit()
    auditar(usuario["usuario"], "Bloqueo " + origen + " alumno_id=" + str(id_alumno))


def liberar_bloqueo(id_alumno, usuario, observacion=""):
    con = obtener_conexion()
    con.execute("UPDATE bloqueos SET activo = 0, fecha_fin = ?, liberado_por = ? WHERE alumno_id = ? AND activo = 1",
                (timestamp_str(), usuario["usuario"], id_alumno))
    con.commit()
    auditar(usuario["usuario"], "Libero bloqueo alumno_id=" + str(id_alumno) + ". Obs: " + observacion)


# ------------------- asistencia -------------------
def contar_tardanzas_injustificadas(id_alumno, id_periodo=None):
    con = obtener_conexion()
    consulta = "SELECT COUNT(*) FROM tardanzas WHERE alumno_id = ? AND justificada = 0"
    params = [id_alumno]
    if id_periodo is not None:
        consulta += " AND periodo_id = ?"
        params.append(id_periodo)
    r = con.execute(consulta, params).fetchone()
    if r[0]:
        return r[0]
    return 0


def aplicar_justificacion_previa(con, alumno_id, fecha, tipo_estado):
    fila = con.execute(
        "SELECT id FROM justificaciones_previas WHERE alumno_id = ? AND fecha_objetivo = ? AND tipo = ? AND aplicada = 0",
        (alumno_id, fecha, tipo_estado)
    ).fetchone()
    if fila:
        con.execute("UPDATE justificaciones_previas SET aplicada = 1 WHERE id = ?", (fila["id"],))
        return True
    return False


def registrar_entrada(dni, usuario):
    dni = (dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni):
        return False, "ERROR", "DNI invalido", {}

    con = obtener_conexion()
    alumno = buscar_alumno_por_dni(con, dni)
    if not alumno:
        return False, "ERROR", "DNI no encontrado", {}

    bloq = alumno_bloqueado(alumno["id"])
    if bloq:
        auditar(usuario["usuario"], "Intento escaneo bloqueado DNI " + dni)
        return False, "BLOQUEADO", nombre_completo(alumno) + " | BLOQUEADO - retener y llevar a TOECE", {"alumno": alumno}

    fecha = hoy_str()
    hora_actual = hora_corta()
    hora_completa = hora_str()
    periodo = obtener_periodo_activo()
    id_periodo = periodo["id"] if periodo else None

    dia = dia_especial_hoy(alumno["turno_id"], fecha, alumno["seccion_id"])
    if dia and dia["tipo"] == "feriado":
        return False, "ERROR", "Hoy es feriado, no se registra asistencia", {}

    ventana = ventana_activa_para_alumno(alumno["turno_id"], fecha, alumno["seccion_id"])
    if not ventana:
        return False, "ERROR", "Sin ventana activa (" + hora_actual + ")", {}

    existente = con.execute("SELECT id, estado FROM asistencias WHERE alumno_id = ? AND fecha = ? AND tipo = ?",
                            (alumno["id"], fecha, ventana["tipo"])).fetchone()
    if existente:
        return False, "ERROR", nombre_completo(alumno) + " ya registro " + ventana["tipo"] + " hoy (" + existente["estado"] + ")", {}

    if ventana["tipo"] == VENTANA_REFORZAMIENTO:
        con.execute(
            "INSERT INTO asistencias (alumno_id, fecha, ventana_id, tipo, hora, estado, periodo_id) VALUES (?, ?, ?, 'reforzamiento', ?, ?, ?)",
            (alumno["id"], fecha, ventana["id"], hora_completa, ESTADO_REF_ASISTIO, id_periodo)
        )
        if alumno["turno"] == "Tarde":
            ya_clases = con.execute("SELECT id FROM asistencias WHERE alumno_id = ? AND fecha = ? AND tipo = 'clases'",
                                    (alumno["id"], fecha)).fetchone()
            if not ya_clases:
                con.execute(
                    "INSERT INTO asistencias (alumno_id, fecha, ventana_id, tipo, hora, estado, periodo_id) VALUES (?, ?, NULL, 'clases', ?, 'Puntual', ?)",
                    (alumno["id"], fecha, hora_completa, id_periodo)
                )
        con.commit()
        auditar(usuario["usuario"], "Reforzamiento DNI " + dni)
        if alumno["turno"] == "Tarde":
            msg = nombre_completo(alumno) + " | " + alumno["grado"] + " " + alumno["seccion"] + " | Reforzamiento + Clases Puntual " + hora_actual
        else:
            msg = nombre_completo(alumno) + " | " + alumno["grado"] + " " + alumno["seccion"] + " | Asistio a reforzamiento " + hora_actual
        return True, "REFORZAMIENTO", msg, {"alumno": alumno}

    if alumno["turno"] == "Tarde":
        ya_clases = con.execute("SELECT id, estado, hora FROM asistencias WHERE alumno_id = ? AND fecha = ? AND tipo = 'clases'",
                                (alumno["id"], fecha)).fetchone()
        if ya_clases:
            return False, "ERROR", nombre_completo(alumno) + " ya tiene clases registradas hoy (" + ya_clases["estado"] + " " + str(ya_clases["hora"]) + ").", {}

    limite = ventana["hora_limite_efectiva"]
    if hora_actual <= limite:
        estado = ESTADO_PUNTUAL
    else:
        estado = ESTADO_TARDANZA

    justificada_previa = aplicar_justificacion_previa(con, alumno["id"], fecha, "Falta")
    if not justificada_previa:
        justificada_previa = aplicar_justificacion_previa(con, alumno["id"], fecha, "Tardanza")

    con.execute(
        "INSERT INTO asistencias (alumno_id, fecha, ventana_id, tipo, hora, estado, justificada, periodo_id) VALUES (?, ?, ?, 'clases', ?, ?, ?, ?)",
        (alumno["id"], fecha, ventana["id"], hora_completa, estado,
         1 if justificada_previa else 0, id_periodo)
    )

    if estado == ESTADO_TARDANZA:
        numero = contar_tardanzas_injustificadas(alumno["id"], id_periodo) + 1
        if numero <= 2:
            accion = ACCION_PERDONADO
        elif numero == 3:
            accion = ACCION_DERIVADO
        else:
            accion = ACCION_RETENIDO
        con.execute(
            "INSERT INTO tardanzas (alumno_id, fecha, hora, numero, accion, justificada, registrado_por, timestamp, periodo_id) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (alumno["id"], fecha, hora_completa, numero, accion, usuario["usuario"], timestamp_str(), id_periodo)
        )
        if numero >= 4:
            if not alumno_bloqueado(alumno["id"]):
                crear_bloqueo(alumno["id"], str(numero) + "ta tardanza injustificada (" + fecha + ")", usuario, "automatico")
        con.commit()
        auditar(usuario["usuario"], "Tardanza " + str(numero) + "a DNI " + dni + " -> " + accion)
        msg = nombre_completo(alumno) + " | " + alumno["grado"] + " " + alumno["seccion"] + " | Tardanza " + str(numero) + "a (" + accion + ") " + hora_actual
        return True, "TARDANZA", msg, {"alumno": alumno, "numero": numero, "accion": accion}

    con.commit()
    auditar(usuario["usuario"], "Entrada Puntual DNI " + dni)
    msg = nombre_completo(alumno) + " | " + alumno["grado"] + " " + alumno["seccion"] + " | Puntual " + hora_actual
    return True, "PUNTUAL", msg, {"alumno": alumno}


def marcar_faltas_al_cierre():
    fecha = hoy_str()
    hora_actual = hora_corta()
    con = obtener_conexion()
    periodo = obtener_periodo_activo()
    id_periodo = periodo["id"] if periodo else None

    for turno in listar_turnos():
        if es_fin_de_semana():
            esp = con.execute("SELECT tipo FROM dias_especiales WHERE fecha = ? AND activo = 1 AND tipo = 'evento'",
                              (fecha,)).fetchone()
            if not esp:
                continue
        for v in listar_ventanas(turno["id"]):
            if hora_actual < v["hora_cierre"]:
                continue
            if v["tipo"] == VENTANA_CLASES:
                alumnos = con.execute(
                    "SELECT a.id FROM alumnos a JOIN secciones s ON a.seccion_id = s.id WHERE s.turno_id = ? AND a.activo = 1",
                    (turno["id"],)
                ).fetchall()
                for al in alumnos:
                    ya = con.execute("SELECT id FROM asistencias WHERE alumno_id = ? AND fecha = ? AND tipo = 'clases'",
                                     (al["id"], fecha)).fetchone()
                    if not ya:
                        con.execute(
                            "INSERT INTO asistencias (alumno_id, fecha, ventana_id, tipo, hora, estado, periodo_id) VALUES (?, ?, ?, 'clases', ?, 'Falta', ?)",
                            (al["id"], fecha, v["id"], hora_str(), id_periodo)
                        )
            else:
                alumnos = con.execute(
                    "SELECT a.id FROM alumnos a JOIN secciones s ON a.seccion_id = s.id WHERE s.turno_id = ? AND a.activo = 1",
                    (turno["id"],)
                ).fetchall()
                for al in alumnos:
                    ya = con.execute("SELECT id FROM asistencias WHERE alumno_id = ? AND fecha = ? AND tipo = 'reforzamiento'",
                                     (al["id"], fecha)).fetchone()
                    if not ya:
                        con.execute(
                            "INSERT INTO asistencias (alumno_id, fecha, ventana_id, tipo, hora, estado, periodo_id) VALUES (?, ?, ?, 'reforzamiento', ?, 'No asistio', ?)",
                            (al["id"], fecha, v["id"], hora_str(), id_periodo)
                        )
    con.commit()


def justificar_asistencia(id_asistencia, justificada, observacion, usuario):
    con = obtener_conexion()
    fila = con.execute("SELECT * FROM asistencias WHERE id = ?", (id_asistencia,)).fetchone()
    if not fila:
        return False, "Registro no encontrado."
    con.execute("UPDATE asistencias SET justificada = ?, observacion = ? WHERE id = ?",
                (1 if justificada else 0, observacion, id_asistencia))
    con.commit()
    auditar(usuario["usuario"], "Justifico id=" + str(id_asistencia))
    return True, "Asistencia actualizada correctamente."


def crear_justificacion_previa(id_alumno, fecha_objetivo, tipo, motivo, usuario):
    con = obtener_conexion()
    try:
        con.execute(
            "INSERT INTO justificaciones_previas (alumno_id, fecha_objetivo, tipo, motivo, creado_por, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (id_alumno, fecha_objetivo, tipo, motivo, usuario["usuario"], timestamp_str())
        )
        con.commit()
        auditar(usuario["usuario"], "Justif. previa " + tipo + " " + fecha_objetivo)
        return True, "Justificacion previa registrada correctamente."
    except sqlite3.IntegrityError:
        return False, "Ya existe una justificacion previa para ese dia y tipo."


# ------------------- escaner qr -------------------
def procesar_escaneo(dni):
    usuario = st.session_state.get("user")
    if not usuario:
        return
    exito, tipo, mensaje, extra = registrar_entrada(dni, usuario)
    if "_qr_mensajes" not in st.session_state:
        st.session_state["_qr_mensajes"] = []
    st.session_state["_qr_mensajes"].insert(0, {
        "dni": dni, "tipo": tipo, "mensaje": mensaje,
        "extra": extra, "ts": time.time()
    })
    st.session_state["_qr_mensajes"] = st.session_state["_qr_mensajes"][:10]


def mostrar_mensaje_qr(msg):
    tipo = msg["tipo"]
    mensaje = msg["mensaje"]
    if tipo == "TARDANZA":
        accion = (msg.get("extra") or {}).get("accion")
        if accion == ACCION_DERIVADO:
            mensaje += " -> Derivar a TOECE"
        elif accion == ACCION_RETENIDO:
            mensaje += " -> Retener hasta apoderado"
    st.write("[" + tipo + "] " + mensaje)


def escaner_qr_continuo(key="qr_scanner"):
    st.write("Escaneo QR")
    st.write("Acerca el codigo del alumno a la camara")

    imagen = camera_input_live(debounce=100, key="cam_" + key)

    if imagen is not None:
        try:
            bytes_data = imagen.getvalue()
            cv_img = cv2.imdecode(np.frombuffer(bytes_data, np.uint8), cv2.IMREAD_COLOR)
            if cv_img is not None:
                detector = cv2.QRCodeDetector()
                data, bbox, _ = detector.detectAndDecode(cv_img)
                if data:
                    dni_str = str(data).strip()
                    match = re.search(r"\b(\d{8})\b", dni_str)
                    if match:
                        dni_final = match.group(1)
                        ultimo = st.session_state.get("_ultimo_qr_scan", {})
                        if not (ultimo.get("dni") == dni_final and (time.time() - ultimo.get("ts", 0)) < 3):
                            st.session_state["_ultimo_qr_scan"] = {"dni": dni_final, "ts": time.time()}
                            procesar_escaneo(dni_final)
                            st.rerun()
        except Exception as err:
            log.warning("error leyendo QR: " + str(err))

    if "_qr_mensajes" in st.session_state and st.session_state["_qr_mensajes"]:
        st.write("Ultimos escaneos")
        for msg in st.session_state["_qr_mensajes"][:5]:
            mostrar_mensaje_qr(msg)


# ------------------- reportes -------------------
def metricas_dia(fecha, id_periodo=None):
    if id_periodo is None:
        p = obtener_periodo_activo()
        id_periodo = p["id"] if p else None
    con = obtener_conexion()
    filtro = ""
    base = []
    if id_periodo is not None:
        filtro = " AND periodo_id = ?"
        base = [id_periodo]

    fila = con.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM alumnos WHERE activo = 1) AS total, "
        "(SELECT COUNT(*) FROM asistencias WHERE fecha = ? AND tipo='clases' AND estado='Puntual'" + filtro + ") AS puntuales, "
        "(SELECT COUNT(*) FROM asistencias WHERE fecha = ? AND tipo='clases' AND estado='Falta'" + filtro + ") AS faltas, "
        "(SELECT COUNT(*) FROM tardanzas WHERE fecha = ?" + filtro + ") AS tardanzas, "
        "(SELECT COUNT(*) FROM asistencias WHERE fecha = ? AND tipo='reforzamiento' AND estado='Asistio'" + filtro + ") AS ref_asistio, "
        "(SELECT COUNT(*) FROM bloqueos WHERE activo = 1) AS bloqueados",
        base + [fecha] + base + [fecha] + base + [fecha] + base + [fecha]
    ).fetchone()
    return dict(fila)


def ultimos_registros(fecha, limite=20):
    con = obtener_conexion()
    return pd.read_sql(
        "SELECT a.dni, a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') AS apellidos, a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, ast.tipo, ast.hora, ast.estado "
        "FROM asistencias ast "
        "JOIN alumnos a ON ast.alumno_id = a.id "
        "JOIN secciones s ON a.seccion_id = s.id "
        "JOIN grados g ON s.grado_id = g.id "
        "JOIN turnos t ON s.turno_id = t.id "
        "WHERE ast.fecha = ? AND ast.hora IS NOT NULL "
        "ORDER BY ast.hora DESC LIMIT ?",
        con, params=[fecha, limite]
    )


def reporte_detalle(inicio, fin, turno, id_grado=None, id_seccion=None,
                    texto="", tipo="clases", ids_secciones=None, id_periodo=None):
    con = obtener_conexion()
    consulta = ("SELECT a.dni, a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') AS apellidos, "
                "a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
                "ast.fecha, ast.hora, ast.tipo, ast.estado, ast.justificada "
                "FROM asistencias ast "
                "JOIN alumnos a ON ast.alumno_id = a.id "
                "JOIN secciones s ON a.seccion_id = s.id "
                "JOIN grados g ON s.grado_id = g.id "
                "JOIN turnos t ON s.turno_id = t.id "
                "WHERE ast.fecha BETWEEN ? AND ? AND ast.hora IS NOT NULL")
    params = [inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")]
    if tipo != "todas":
        consulta += " AND ast.tipo = ?"
        params.append(tipo)
    if turno != "Todos":
        consulta += " AND t.nombre = ?"
        params.append(turno)
    if id_grado:
        consulta += " AND g.id = ?"
        params.append(id_grado)
    if id_seccion:
        consulta += " AND s.id = ?"
        params.append(id_seccion)
    if ids_secciones:
        marcas = ",".join(["?"] * len(ids_secciones))
        consulta += " AND s.id IN (" + marcas + ")"
        params.extend(ids_secciones)
    if id_periodo is not None:
        consulta += " AND ast.periodo_id = ?"
        params.append(id_periodo)
    if texto:
        for p in texto.split():
            if p.strip():
                consulta += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
                pat = "%" + p.strip() + "%"
                params.append(pat)
                params.append(pat)
                params.append(pat)
    consulta += " ORDER BY ast.fecha DESC, ast.hora DESC"
    return pd.read_sql(consulta, con, params=params)


def reporte_conteo_faltas(inicio, fin, turno, id_grado=None, id_seccion=None,
                          texto="", ids_secciones=None, id_periodo=None):
    con = obtener_conexion()
    consulta = ("SELECT a.dni, a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') AS apellidos, "
                "a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
                "SUM(CASE WHEN ast.justificada = 1 THEN 1 ELSE 0 END) AS faltas_just, "
                "SUM(CASE WHEN ast.justificada = 0 THEN 1 ELSE 0 END) AS faltas_injust, "
                "COUNT(*) AS total_faltas "
                "FROM asistencias ast "
                "JOIN alumnos a ON ast.alumno_id = a.id "
                "JOIN secciones s ON a.seccion_id = s.id "
                "JOIN grados g ON s.grado_id = g.id "
                "JOIN turnos t ON s.turno_id = t.id "
                "WHERE ast.estado = 'Falta' AND ast.tipo = 'clases' "
                "AND ast.fecha BETWEEN ? AND ?")
    params = [inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")]
    if turno != "Todos":
        consulta += " AND t.nombre = ?"
        params.append(turno)
    if id_grado:
        consulta += " AND g.id = ?"
        params.append(id_grado)
    if id_seccion:
        consulta += " AND s.id = ?"
        params.append(id_seccion)
    if ids_secciones:
        marcas = ",".join(["?"] * len(ids_secciones))
        consulta += " AND s.id IN (" + marcas + ")"
        params.extend(ids_secciones)
    if id_periodo is not None:
        consulta += " AND ast.periodo_id = ?"
        params.append(id_periodo)
    if texto:
        for p in texto.split():
            if p.strip():
                consulta += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
                pat = "%" + p.strip() + "%"
                params.append(pat)
                params.append(pat)
                params.append(pat)
    consulta += " GROUP BY a.id ORDER BY faltas_injust DESC, total_faltas DESC"
    return pd.read_sql(consulta, con, params=params)


def casos_toece(id_periodo=None):
    con = obtener_conexion()
    filtro = ""
    params = []
    if id_periodo is not None:
        filtro = " AND t2.periodo_id = ?"
        params.append(id_periodo)
    return pd.read_sql(
        "SELECT a.id, a.dni, a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') AS apellidos, "
        "a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
        "a.nombre_apoderado, a.telefono_apoderado, "
        "COUNT(t2.id) AS tard_injust, "
        "(SELECT COUNT(*) FROM actas_compromiso WHERE alumno_id = a.id) AS total_actas, "
        "CASE WHEN EXISTS (SELECT 1 FROM bloqueos WHERE alumno_id=a.id AND activo=1) THEN 'SI' ELSE 'NO' END AS bloqueado "
        "FROM tardanzas t2 "
        "JOIN alumnos a ON t2.alumno_id = a.id "
        "JOIN secciones s ON a.seccion_id = s.id "
        "JOIN grados g ON s.grado_id = g.id "
        "JOIN turnos t ON s.turno_id = t.id "
        "WHERE t2.justificada = 0 " + filtro + " "
        "GROUP BY a.id HAVING COUNT(t2.id) >= 3 "
        "ORDER BY tard_injust DESC",
        con, params=params
    )


def listar_observados(solo_activos=True):
    con = obtener_conexion()
    consulta = ("SELECT o.id, a.id AS alumno_id, a.dni, "
                "a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') AS apellidos, "
                "a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
                "o.fecha_ingreso, COALESCE(o.motivo, '') AS motivo, "
                "o.activo, COALESCE(o.fecha_salida, '') AS fecha_salida, "
                "COALESCE(o.observacion_cierre, '') AS observacion_cierre "
                "FROM observados o "
                "JOIN alumnos a ON o.alumno_id = a.id "
                "JOIN secciones s ON a.seccion_id = s.id "
                "JOIN grados g ON s.grado_id = g.id "
                "JOIN turnos t ON s.turno_id = t.id")
    if solo_activos:
        consulta += " WHERE o.activo = 1"
    consulta += " ORDER BY o.fecha_ingreso DESC"
    return pd.read_sql(consulta, con)


def listar_bloqueados():
    con = obtener_conexion()
    return pd.read_sql(
        "SELECT b.id, a.id AS alumno_id, a.dni, "
        "a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') || ', ' || a.nombres AS alumno, "
        "g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
        "b.motivo, b.fecha_inicio, COALESCE(b.origen,'automatico') AS origen "
        "FROM bloqueos b "
        "JOIN alumnos a ON b.alumno_id = a.id "
        "JOIN secciones s ON a.seccion_id = s.id "
        "JOIN grados g ON s.grado_id = g.id "
        "JOIN turnos t ON s.turno_id = t.id "
        "WHERE b.activo = 1 ORDER BY b.fecha_inicio DESC",
        con
    )


def obtener_auditoria(limite=500):
    con = obtener_conexion()
    return pd.read_sql("SELECT id, usuario, accion, fecha, ip FROM auditoria ORDER BY id DESC LIMIT " + str(int(limite)), con)


# ------------------- perfil alumno -------------------
def perfil_alumno_datos(id_alumno):
    con = obtener_conexion()
    alumno = con.execute(
        "SELECT a.*, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, s.id AS seccion_id, t.id AS turno_id "
        "FROM alumnos a "
        "JOIN secciones s ON a.seccion_id = s.id "
        "JOIN grados g ON s.grado_id = g.id "
        "JOIN turnos t ON s.turno_id = t.id "
        "WHERE a.id = ?",
        (id_alumno,)
    ).fetchone()
    if not alumno:
        return {}
    alumno = dict(alumno)

    df_asist = pd.read_sql(
        "SELECT fecha, hora, tipo, estado, justificada, COALESCE(observacion, '') AS observacion FROM asistencias WHERE alumno_id = ? ORDER BY fecha DESC, hora DESC",
        con, params=[id_alumno]
    )
    df_tard = pd.read_sql(
        "SELECT fecha, hora, numero AS 'N', accion, justificada, COALESCE(observacion, '') AS observacion FROM tardanzas WHERE alumno_id = ? ORDER BY fecha DESC, hora DESC",
        con, params=[id_alumno]
    )
    df_obs = pd.read_sql(
        "SELECT fecha_ingreso, COALESCE(fecha_salida, '-') AS fecha_salida, COALESCE(motivo, '') AS motivo, activo FROM observados WHERE alumno_id = ? ORDER BY fecha_ingreso DESC",
        con, params=[id_alumno]
    )
    df_actas = pd.read_sql(
        "SELECT fecha, COALESCE(motivo, '') AS motivo, COALESCE(observacion, '') AS observacion, COALESCE(registrado_por, '') AS registrado_por FROM actas_compromiso WHERE alumno_id = ? ORDER BY fecha DESC",
        con, params=[id_alumno]
    )
    df_bloqueos = pd.read_sql(
        "SELECT fecha_inicio, COALESCE(fecha_fin, '-') AS fecha_fin, COALESCE(motivo, '') AS motivo, activo FROM bloqueos WHERE alumno_id = ? ORDER BY fecha_inicio DESC",
        con, params=[id_alumno]
    )
    df_just_prev = pd.read_sql(
        "SELECT fecha_objetivo, tipo, COALESCE(motivo, '') AS motivo, aplicada, timestamp FROM justificaciones_previas WHERE alumno_id = ? ORDER BY fecha_objetivo DESC",
        con, params=[id_alumno]
    )

    total_puntuales = 0
    total_faltas = 0
    total_tardanzas = 0
    total_ref = 0
    if not df_asist.empty:
        total_puntuales = int((df_asist["estado"] == ESTADO_PUNTUAL).sum())
        total_faltas = int((df_asist["estado"] == ESTADO_FALTA).sum())
        total_tardanzas = int((df_asist["estado"] == ESTADO_TARDANZA).sum())
        total_ref = int(((df_asist["tipo"] == "reforzamiento") & (df_asist["estado"] == ESTADO_REF_ASISTIO)).sum())

    return {
        "alumno": alumno,
        "asistencias": df_asist,
        "tardanzas": df_tard,
        "observados": df_obs,
        "actas": df_actas,
        "bloqueos": df_bloqueos,
        "just_previas": df_just_prev,
        "total_puntuales": total_puntuales,
        "total_faltas": total_faltas,
        "total_tardanzas": total_tardanzas,
        "total_ref_asistio": total_ref,
        "tard_injust": contar_tardanzas_injustificadas(id_alumno),
        "bloqueado": alumno_bloqueado(id_alumno) is not None,
    }


# ------------------- cierre anual -------------------
def reporte_cierre_anual(id_periodo):
    con = obtener_conexion()
    return pd.read_sql(
        "SELECT g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
        "COUNT(DISTINCT a.id) AS total_alumnos, "
        "(SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id = ? AND ast.alumno_id IN (SELECT id FROM alumnos WHERE seccion_id = s.id AND periodo_id = ?) AND ast.estado = 'Puntual') AS puntuales, "
        "(SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id = ? AND ast.alumno_id IN (SELECT id FROM alumnos WHERE seccion_id = s.id AND periodo_id = ?) AND ast.estado = 'Tardanza') AS tardanzas, "
        "(SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id = ? AND ast.alumno_id IN (SELECT id FROM alumnos WHERE seccion_id = s.id AND periodo_id = ?) AND ast.estado = 'Falta' AND ast.justificada = 0) AS faltas_injust, "
        "(SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id = ? AND ast.alumno_id IN (SELECT id FROM alumnos WHERE seccion_id = s.id AND periodo_id = ?) AND ast.estado = 'Falta' AND ast.justificada = 1) AS faltas_just "
        "FROM secciones s "
        "JOIN grados g ON s.grado_id = g.id "
        "JOIN turnos t ON s.turno_id = t.id "
        "LEFT JOIN alumnos a ON a.seccion_id = s.id AND a.periodo_id = ? "
        "GROUP BY s.id ORDER BY t.nombre, g.nombre, s.nombre",
        con, params=[id_periodo] * 9
    )


def reporte_detallado_por_mes(id_periodo):
    con = obtener_conexion()
    periodo = con.execute("SELECT * FROM periodos WHERE id = ?", (id_periodo,)).fetchone()
    if not periodo:
        return {}
    fi = datetime.strptime(periodo["fecha_inicio"], "%Y-%m-%d").date()
    ff = datetime.strptime(periodo["fecha_fin"], "%Y-%m-%d").date()
    resultado = {}
    cursor = date(fi.year, fi.month, 1)
    while cursor <= ff:
        ultimo = monthrange(cursor.year, cursor.month)[1]
        inicio_mes = cursor.replace(day=1)
        fin_mes = cursor.replace(day=ultimo)
        if fin_mes > ff:
            fin_mes = ff
        if inicio_mes < fi:
            inicio_mes = fi
        df = pd.read_sql(
            "SELECT a.dni, a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') AS apellidos, "
            "a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
            "ast.fecha, ast.hora, ast.tipo, ast.estado, ast.justificada, COALESCE(ast.observacion,'') AS observacion "
            "FROM asistencias ast "
            "JOIN alumnos a ON ast.alumno_id = a.id "
            "JOIN secciones s ON a.seccion_id = s.id "
            "JOIN grados g ON s.grado_id = g.id "
            "JOIN turnos t ON s.turno_id = t.id "
            "WHERE ast.periodo_id = ? AND ast.fecha BETWEEN ? AND ? "
            "ORDER BY ast.fecha, t.nombre, g.nombre, s.nombre, a.apellido_paterno",
            con, params=[id_periodo, inicio_mes.strftime("%Y-%m-%d"), fin_mes.strftime("%Y-%m-%d")]
        )
        resultado[str(cursor.year) + "-" + str(cursor.month).zfill(2)] = df
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return resultado


def cerrar_anio_escolar(usuario, id_periodo, nuevo_nombre, fecha_inicio, fecha_fin):
    con = obtener_conexion()
    periodo = con.execute("SELECT * FROM periodos WHERE id = ?", (id_periodo,)).fetchone()
    if not periodo:
        return False, "Periodo no encontrado."
    if periodo["cerrado"]:
        return False, "Ese periodo ya esta cerrado."
    reporte_json = None
    try:
        rep = reporte_cierre_anual(id_periodo)
        if not rep.empty:
            reporte_json = rep.to_json(orient="records", force_ascii=False)
    except Exception:
        reporte_json = None
    fecha_cierre = timestamp_str()
    try:
        con.execute("INSERT INTO cierres_anuales (periodo_id, fecha_cierre, generado_por, reporte_json) VALUES (?, ?, ?, ?)",
                    (id_periodo, fecha_cierre, usuario["usuario"], reporte_json))
        con.execute("UPDATE periodos SET activo = 0, fecha_cierre = ?, cerrado = 1 WHERE id = ?",
                    (fecha_cierre, id_periodo))
        con.execute("UPDATE alumnos SET activo = 0, retirado_en = ? WHERE periodo_id = ?",
                    (fecha_cierre, id_periodo))
        con.execute("INSERT INTO periodos (nombre, fecha_inicio, fecha_fin, activo, cerrado) VALUES (?, ?, ?, 1, 0)",
                    (nuevo_nombre, fecha_inicio, fecha_fin))
        con.commit()
    except sqlite3.Error as err:
        con.rollback()
        log.error("error cerrando anio: " + str(err))
        return False, "Error al cerrar el anio."
    auditar(usuario["usuario"], "Cerro periodo " + periodo["nombre"])
    return True, "Periodo '" + periodo["nombre"] + "' cerrado. Nuevo: '" + nuevo_nombre + "'."


def listar_cierres_anuales():
    con = obtener_conexion()
    return pd.read_sql(
        "SELECT c.id, p.nombre AS periodo, c.fecha_cierre, c.generado_por FROM cierres_anuales c JOIN periodos p ON c.periodo_id = p.id ORDER BY c.id DESC",
        con
    )


# ------------------- pdf / excel / qr -------------------
def pdf_base(titulo, subtitulo=None, paisaje=False):
    buffer = BytesIO()
    if paisaje:
        tamano = (A4[1], A4[0])
    else:
        tamano = A4
    doc = SimpleDocTemplate(buffer, pagesize=tamano, rightMargin=25, leftMargin=25, topMargin=25, bottomMargin=25)
    estilos = getSampleStyleSheet()
    elementos = [Paragraph("<b>" + titulo + "</b>", estilos["Heading1"])]
    if subtitulo:
        elementos.append(Paragraph(subtitulo, estilos["Normal"]))
    elementos.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"), estilos["Normal"]))
    elementos.append(Spacer(1, 15))
    return buffer, doc, elementos, estilos


def generar_pdf_tabla(df, titulo, subtitulo=None):
    buffer, doc, elementos, _ = pdf_base(titulo, subtitulo, paisaje=len(df.columns) > 6)
    if not df.empty:
        datos = [df.columns.tolist()] + df.astype(str).values.tolist()
        tabla = Table(datos, repeatRows=1)
        tabla.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E65100")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
        ]))
        elementos.append(tabla)
    doc.build(elementos)
    buffer.seek(0)
    return buffer.getvalue()


def generar_qr(dni):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(str(dni).strip())
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def render_carnets(filas, titulo=None):
    buffer = BytesIO()
    margen = 8
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=margen, leftMargin=margen, topMargin=margen, bottomMargin=margen)
    estilos = getSampleStyleSheet()
    if titulo is None:
        if len(filas) > 1:
            titulo = "Carnets - " + filas[0]["grado"] + " " + filas[0]["seccion"]
        else:
            titulo = "Carnet - " + filas[0]["apellido_paterno"] + " " + (filas[0]["apellido_materno"] or "") + ", " + filas[0]["nombres"]
    elementos = [Paragraph(titulo, estilos["Heading1"]), Spacer(1, 6)]
    columnas = 3
    filas_por_pagina = 3
    por_pagina = columnas * filas_por_pagina
    ancho_util = A4[0] - (2 * margen)
    alto_util = A4[1] - (2 * margen)
    ancho_carnet = ancho_util / columnas
    alto_carnet = (alto_util - 50) / filas_por_pagina

    for inicio in range(0, len(filas), por_pagina):
        lote = filas[inicio:inicio + por_pagina]
        tabla_datos = []
        for j in range(0, len(lote), columnas):
            fila = []
            for alumno in lote[j:j + columnas]:
                qr_buffer = BytesIO()
                generar_qr(alumno["dni"]).save(qr_buffer, format="PNG")
                qr_buffer.seek(0)
                celda = [
                    Paragraph("<b>" + alumno["apellido_paterno"] + " " + (alumno["apellido_materno"] or "") + "</b>", estilos["Normal"]),
                    Paragraph(alumno["nombres"], estilos["Normal"]),
                    Paragraph("DNI: " + alumno["dni"], estilos["Normal"]),
                    Paragraph(alumno["grado"] + " " + alumno["seccion"] + " - " + alumno["turno"], estilos["Normal"]),
                    RLImage(qr_buffer, width=150, height=150),
                ]
                fila.append(celda)
            while len(fila) < columnas:
                fila.append([])
            tabla_datos.append(fila)
        while len(tabla_datos) < filas_por_pagina:
            tabla_datos.append([[] for _ in range(columnas)])
        tabla = Table(tabla_datos, colWidths=[ancho_carnet] * columnas, rowHeights=[alto_carnet] * filas_por_pagina)
        tabla.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOX", (0, 0), (-1, -1), 1.5, colors.HexColor("#E65100")),
            ("INNERGRID", (0, 0), (-1, -1), 1, colors.grey),
        ]))
        elementos.append(tabla)
        if inicio + por_pagina < len(filas):
            elementos.append(PageBreak())
    doc.build(elementos)
    buffer.seek(0)
    return buffer.getvalue()


def filas_alumnos_por_seccion(id_seccion):
    con = obtener_conexion()
    return con.execute(
        "SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno FROM alumnos a JOIN secciones s ON a.seccion_id = s.id JOIN grados g ON s.grado_id = g.id JOIN turnos t ON s.turno_id = t.id WHERE a.seccion_id = ? AND a.activo = 1 ORDER BY a.apellido_paterno, a.apellido_materno",
        (id_seccion,)
    ).fetchall()


def pdf_carnets_por_seccion(id_seccion):
    filas = filas_alumnos_por_seccion(id_seccion)
    if filas:
        return render_carnets(filas)
    return None


def pdf_carnets_seleccionados(ids_alumnos, titulo=None):
    if not ids_alumnos:
        return None
    con = obtener_conexion()
    marcas = ",".join(["?"] * len(ids_alumnos))
    filas = con.execute(
        "SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno FROM alumnos a JOIN secciones s ON a.seccion_id = s.id JOIN grados g ON s.grado_id = g.id JOIN turnos t ON s.turno_id = t.id WHERE a.id IN (" + marcas + ") AND a.activo = 1 ORDER BY a.apellido_paterno, a.apellido_materno",
        ids_alumnos
    ).fetchall()
    if filas:
        return render_carnets(filas, titulo)
    return None


def pdf_carnet_alumno(dni):
    con = obtener_conexion()
    filas = con.execute(
        "SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno FROM alumnos a JOIN secciones s ON a.seccion_id = s.id JOIN grados g ON s.grado_id = g.id JOIN turnos t ON s.turno_id = t.id WHERE a.dni = ? AND a.activo = 1",
        (dni,)
    ).fetchall()
    if filas:
        return render_carnets(filas)
    return None


def df_a_xlsx(df, hoja="Datos"):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=hoja)
    buffer.seek(0)
    return buffer.getvalue()


def df_a_xlsx_multilhoja(hojas):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for nombre, df in hojas.items():
            nombre_ok = nombre[:31] if len(nombre) > 31 else nombre
            df.to_excel(writer, index=False, sheet_name=nombre_ok)
    buffer.seek(0)
    return buffer.getvalue()


# ------------------- ui: login -------------------
def vista_login():
    st.write("Sistema de Asistencia - I.E. Yarinacocha")
    with st.form("login"):
        usuario = st.text_input("Usuario")
        password = st.text_input("Contrasena", type="password")
        recordarme = st.checkbox("Recordarme", value=True)
        enviado = st.form_submit_button("Ingresar")
        if enviado:
            datos, err = autenticar(usuario, password)
            if datos:
                st.session_state["user"] = datos
                if recordarme:
                    token = crear_token_sesion(datos)
                    st.session_state["_token"] = token
                    st.session_state["_token_expira"] = time.time() + 300
                    guardar_token_cookie(token)
                auditar(datos["usuario"], "Login")
                st.rerun()
            else:
                st.write("Error: " + (err or "Credenciales incorrectas"))


def vista_cambio_password_obligatorio():
    usuario = st.session_state["user"]
    st.write("Cambio obligatorio de contrasena")
    st.write("Tu cuenta tiene una contrasena temporal.")
    with st.form("cambio_pwd"):
        nueva = st.text_input("Nueva contrasena", type="password")
        confirmar = st.text_input("Confirmar", type="password")
        ok = st.form_submit_button("Cambiar")
    if ok:
        if len(nueva) < 6:
            st.write("Minimo 6 caracteres.")
        elif nueva != confirmar:
            st.write("No coinciden.")
        else:
            con = obtener_conexion()
            con.execute("UPDATE usuarios SET password = ?, debe_cambiar_password = 0 WHERE id = ?",
                        (hashear_password(nueva), usuario["id"]))
            con.commit()
            st.session_state["user"]["debe_cambiar_password"] = 0
            auditar(usuario["usuario"], "Cambio pwd obligatorio")
            st.rerun()


# ------------------- ui: puerta -------------------
def vista_puerta():
    usuario = st.session_state["user"]
    fecha = hoy_str()
    st.write("Control de Puerta")
    st.write(fecha)

    con = obtener_conexion()
    especial = con.execute(
        "SELECT descripcion, hora_entrada, tipo FROM dias_especiales WHERE fecha = ? AND activo = 1 LIMIT 1",
        (fecha,)
    ).fetchone()

    if especial and especial["tipo"] == "evento":
        st.write("Evento escolar: " + especial["descripcion"] + " (entrada " + especial["hora_entrada"] + ")")
    elif especial and especial["tipo"] == "feriado":
        st.write("Feriado / sin clases: " + especial["descripcion"])
        return
    elif es_fin_de_semana():
        st.write("Hoy no es dia laboral. No se toma asistencia.")
        return

    marcar_faltas_al_cierre()

    hora_actual = hora_corta()
    ventanas_activas = []
    for turno in listar_turnos():
        ventanas = listar_ventanas(turno["id"])
        for v in ventanas:
            if v["hora_apertura"] <= hora_actual <= v["hora_cierre"]:
                ventanas_activas.append(turno["nombre"] + ": " + v["nombre"])

    if ventanas_activas:
        st.write("Ventanas activas: " + " | ".join(ventanas_activas))

    escaner_qr_continuo("puerta_qr")

    if usuario["rol"] == "Admin":
        st.write("---")
        with st.expander("Lista manual (solo Admin)"):
            grados = listar_grados()
            if not grados:
                return
            c1, c2 = st.columns(2)
            with c1:
                g = st.selectbox("Grado", grados, format_func=lambda x: x["nombre"], key="pt_g")
            with c2:
                secs = secciones_por_grado(g["id"]) if g else []
                if not secs:
                    st.write("Sin secciones")
                    return
                s = st.selectbox("Seccion", secs, format_func=lambda x: x["nombre"], key="pt_s")
            df = alumnos_de_seccion(s["id"])
            for _, al in df.iterrows():
                c1, c2 = st.columns([5, 1])
                c1.write(al["nombre_completo"])
                if c2.button("Marcar", key="m_" + str(al["id"])):
                    _, _, msg, _ = registrar_entrada(al["dni"], usuario)
                    st.write(msg)


# ------------------- ui: toece -------------------
def vista_toece():
    st.write("TOECE")
    usuario = st.session_state["user"]
    tabs = st.tabs(["Casos activos", "Bloqueados", "Observados", "Firmar acta", "Justificar previo"])

    with tabs[0]:
        df = casos_toece()
        if df.empty:
            st.write("Sin casos activos.")
        else:
            st.dataframe(df, use_container_width=True)
            st.download_button("Excel", df_a_xlsx(df), "casos_toece.xlsx")

    with tabs[1]:
        st.write("Alumnos bloqueados")
        st.write("Bloqueo automatico a la 4ta tardanza. TOECE puede desbloquear.")
        df = listar_bloqueados()
        if df.empty:
            st.write("Sin bloqueados.")
        else:
            st.dataframe(df, use_container_width=True)
            opciones = {}
            for _, r in df.iterrows():
                opciones[r["alumno"] + " (" + r["dni"] + ") - " + r["motivo"]] = r["alumno_id"]
            sel = st.selectbox("Liberar bloqueo", list(opciones.keys()))
            obs = st.text_input("Observacion de liberacion", key="lib_obs")
            if st.button("Liberar bloqueo"):
                liberar_bloqueo(opciones[sel], usuario, obs)
                st.write("Bloqueo liberado")
                st.rerun()

        st.write("---")
        st.write("Bloquear manualmente")
        id_g, id_s, texto = filtros_grado_seccion_nombre("bloq_man")
        if texto or id_g:
            df_b = buscar_alumnos(texto, id_g, id_s, 50)
            if not df_b.empty:
                opciones_b = {}
                for _, r in df_b.iterrows():
                    opciones_b[r["nombre_completo"] + " (" + r["dni"] + ")"] = r["id"]
                sel_b = st.selectbox("Alumno a bloquear", list(opciones_b.keys()), key="bloq_sel")
                motivo_b = st.text_input("Motivo del bloqueo", key="bloq_mot")
                if st.button("Bloquear", key="bloq_btn"):
                    if not motivo_b.strip():
                        st.write("Ingresa un motivo.")
                    else:
                        crear_bloqueo(opciones_b[sel_b], motivo_b.strip(), usuario, "manual")
                        st.write("Alumno bloqueado")
                        st.rerun()

    with tabs[2]:
        st.write("Alumnos observados")
        solo_act = st.checkbox("Solo activos", value=True, key="obs_act")
        df = listar_observados(solo_act)
        if df.empty:
            st.write("Sin observados.")
        else:
            st.dataframe(df, use_container_width=True)
            st.download_button("Excel", df_a_xlsx(df), "observados.xlsx")

        st.write("---")
        st.write("Crear observado")
        id_g, id_s, texto = filtros_grado_seccion_nombre("obs_crear")
        if texto or id_g:
            df_a = buscar_alumnos(texto, id_g, id_s, 50)
            if not df_a.empty:
                opciones_a = {}
                for _, r in df_a.iterrows():
                    opciones_a[r["nombre_completo"] + " (" + r["dni"] + ")"] = r["id"]
                with st.form("form_obs"):
                    sel_a = st.selectbox("Alumno", list(opciones_a.keys()))
                    motivo_a = st.text_input("Motivo")
                    submit_obs = st.form_submit_button("Crear observado")
                if submit_obs:
                    if not motivo_a.strip():
                        st.write("Ingresa un motivo.")
                    else:
                        con = obtener_conexion()
                        periodo = obtener_periodo_activo()
                        id_periodo = periodo["id"] if periodo else None
                        con.execute("INSERT INTO observados (alumno_id, fecha_ingreso, motivo, activo, periodo_id) VALUES (?, ?, ?, 1, ?)",
                                    (opciones_a[sel_a], hoy_str(), motivo_a.strip(), id_periodo))
                        con.commit()
                        auditar(usuario["usuario"], "Creo observado alumno_id=" + str(opciones_a[sel_a]))
                        st.write("Observado creado")
                        st.rerun()

        st.write("---")
        st.write("Cerrar observado")
        df_act = listar_observados(True)
        if not df_act.empty:
            opciones_c = {}
            for _, r in df_act.iterrows():
                opciones_c[r["apellidos"] + ", " + r["nombres"] + " (" + r["dni"] + ")"] = r["id"]
            with st.form("cerrar_obs"):
                sel_c = st.selectbox("Observado a cerrar", list(opciones_c.keys()))
                obs_c = st.text_input("Observacion de cierre")
                submit_cerrar = st.form_submit_button("Cerrar observado")
            if submit_cerrar:
                con = obtener_conexion()
                con.execute("UPDATE observados SET activo = 0, fecha_salida = ?, observacion_cierre = ? WHERE id = ?",
                            (timestamp_str(), obs_c, opciones_c[sel_c]))
                con.commit()
                auditar(usuario["usuario"], "Cerro observado id=" + str(opciones_c[sel_c]))
                st.write("Observado cerrado")
                st.rerun()

    with tabs[3]:
        st.write("Firmar acta de compromiso")
        df = casos_toece()
        if df.empty:
            st.write("Sin casos.")
        else:
            opciones = {}
            for _, r in df.iterrows():
                opciones[r["apellidos"] + ", " + r["nombres"] + " (" + r["dni"] + ")"] = r["id"]
            with st.form("firmar_acta_form"):
                sel = st.selectbox("Alumno", list(opciones.keys()))
                motivo = st.text_input("Motivo", value="Reincidencia en tardanzas")
                obs = st.text_area("Observacion")
                submit_acta = st.form_submit_button("Firmar acta")
            if submit_acta:
                con = obtener_conexion()
                con.execute("INSERT INTO actas_compromiso (alumno_id, fecha, motivo, observacion, registrado_por, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                            (opciones[sel], hoy_str(), motivo, obs, usuario["usuario"], timestamp_str()))
                con.commit()
                auditar(usuario["usuario"], "Firmo acta alumno_id=" + str(opciones[sel]))
                st.write("Acta registrada")
                st.rerun()

    with tabs[4]:
        st.write("Justificacion previa (48h antes)")
        st.write("Solo se puede justificar ANTES del dia.")
        id_g, id_s, texto = filtros_grado_seccion_nombre("jp")
        df = buscar_alumnos(texto, id_g, id_s, 100)
        if df.empty:
            st.write("Sin alumnos.")
        else:
            opciones = {}
            for _, r in df.iterrows():
                opciones[r["nombre_completo"] + " (" + r["dni"] + ")"] = r["dni"]
            with st.form("just_prev_form"):
                sel = st.selectbox("Alumno", list(opciones.keys()))
                fecha_obj = st.date_input("Fecha a justificar", min_value=ahora().date())
                tipo = st.selectbox("Tipo", ["Falta", "Tardanza"])
                motivo = st.text_input("Motivo")
                submit_jp = st.form_submit_button("Registrar")
            if submit_jp:
                con = obtener_conexion()
                fila = con.execute("SELECT id FROM alumnos WHERE dni = ?", (opciones[sel],)).fetchone()
                if fila:
                    ok, msg = crear_justificacion_previa(fila["id"], fecha_obj.strftime("%Y-%m-%d"), tipo, motivo, usuario)
                    if ok:
                        st.write(msg)
                    else:
                        st.write("Error: " + msg)


# ------------------- ui: panel direccion -------------------
def vista_panel_direccion():
    st.write("Panel Direccion")
    fecha = hoy_str()
    marcar_faltas_al_cierre()
    if st.button("Actualizar", key="refresh_panel"):
        st.rerun()
    m = metricas_dia(fecha)
    st.write("Resumen del dia")
    c1, c2, c3, c4 = st.columns(4)
    c1.write("Total alumnos: " + str(m["total"]))
    c2.write("Puntuales: " + str(m["puntuales"]))
    c3.write("Tardanzas: " + str(m["tardanzas"]))
    c4.write("Faltas: " + str(m["faltas"]))
    c1, c2, c3 = st.columns(3)
    c1.write("Reforzamiento asistio: " + str(m["ref_asistio"]))
    c2.write("Bloqueados: " + str(m["bloqueados"]))

    st.write("---")
    st.write("Ultimos escaneos")
    df = ultimos_registros(fecha, 30)
    if df.empty:
        st.write("Sin escaneos hoy.")
    else:
        st.dataframe(df, use_container_width=True)


# ------------------- ui: filtros -------------------
def filtros_grado_seccion_nombre(clave, placeholder="Buscar"):
    grados = listar_grados()
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        ops = [{"id": None, "nombre": "Todos"}] + grados
        g = st.selectbox("Grado", ops, format_func=lambda x: x["nombre"], key=clave + "_g")
    with c2:
        if g and g["id"]:
            secs = [{"id": None, "nombre": "Todas"}] + secciones_por_grado(g["id"])
        else:
            secs = [{"id": None, "nombre": "Todas"}]
        s = st.selectbox("Seccion", secs, format_func=lambda x: x["nombre"], key=clave + "_s")
    with c3:
        t = st.text_input("Buscar", placeholder=placeholder, key=clave + "_t")
    return (g["id"] if g else None,
            s["id"] if (g and g["id"] and s) else None,
            t.strip())


# ------------------- ui: reportes -------------------
def selector_secciones_multiple(clave, permitidas=None):
    secciones = listar_todas_secciones()
    if permitidas is not None:
        secciones = [s for s in secciones if s["id"] in permitidas]
    if not secciones:
        st.write("No hay secciones disponibles.")
        return [], {}
    etiquetas = {}
    for s in secciones:
        etiquetas[s["id"]] = s["grado"] + " " + s["seccion"] + " (" + s["turno"] + ")"
    st.write("Selecciona las secciones:")
    seleccionadas = []
    cols = st.columns(4)
    for i, s in enumerate(secciones):
        with cols[i % 4]:
            if st.checkbox(etiquetas[s["id"]], key=clave + "_" + str(s["id"])):
                seleccionadas.append(s["id"])
    return seleccionadas, etiquetas


def cierre_mensual(mes, anio, turno, ids_secciones=None, id_periodo=None):
    ultimo_dia = monthrange(anio, mes)[1]
    inicio = str(anio) + "-" + str(mes).zfill(2) + "-01"
    fin = str(anio) + "-" + str(mes).zfill(2) + "-" + str(ultimo_dia)
    con = obtener_conexion()
    consulta = ("SELECT g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, a.dni, "
                "a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') AS apellidos, a.nombres, "
                "SUM(CASE WHEN ast.estado = 'Puntual' THEN 1 ELSE 0 END) AS puntuales, "
                "SUM(CASE WHEN ast.estado = 'Falta' AND ast.justificada = 1 THEN 1 ELSE 0 END) AS faltas_just, "
                "SUM(CASE WHEN ast.estado = 'Falta' AND ast.justificada = 0 THEN 1 ELSE 0 END) AS faltas_injust, "
                "SUM(CASE WHEN ast.estado = 'Tardanza' THEN 1 ELSE 0 END) AS tardanzas, "
                "COUNT(*) AS total_dias "
                "FROM asistencias ast "
                "JOIN alumnos a ON ast.alumno_id = a.id "
                "JOIN secciones s ON a.seccion_id = s.id "
                "JOIN grados g ON s.grado_id = g.id "
                "JOIN turnos t ON s.turno_id = t.id "
                "WHERE ast.fecha BETWEEN ? AND ? AND ast.tipo = 'clases'")
    params = [inicio, fin]
    if turno != "Todos":
        consulta += " AND t.nombre = ?"
        params.append(turno)
    if ids_secciones:
        marcas = ",".join(["?"] * len(ids_secciones))
        consulta += " AND s.id IN (" + marcas + ")"
        params.extend(ids_secciones)
    if id_periodo is not None:
        consulta += " AND ast.periodo_id = ?"
        params.append(id_periodo)
    consulta += " GROUP BY a.id ORDER BY t.nombre, g.nombre, s.nombre, a.apellido_paterno"
    return pd.read_sql(consulta, con, params=params)


def vista_reportes():
    st.write("Reportes y Consultas")
    usuario = st.session_state["user"]
    rol = usuario["rol"]
    permitidas = None

    if rol == "Auxiliar":
        con = obtener_conexion()
        filas = con.execute("SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id = ?",
                            (usuario["id"],)).fetchall()
        permitidas = []
        for f in filas:
            permitidas.append(f["seccion_id"])
        if not permitidas:
            st.write("No tienes secciones asignadas. Contacta al Admin.")
            return

    id_periodo = None
    if rol == "Admin":
        df_per = listar_periodos()
        if not df_per.empty:
            opciones_p = {}
            for _, r in df_per.iterrows():
                etiqueta = r["nombre"] + " (" + str(r["fecha_inicio"]) + " - " + str(r["fecha_fin"]) + ")"
                if r["cerrado"]:
                    etiqueta += " [CERRADO]"
                elif r["activo"]:
                    etiqueta += " [ACTIVO]"
                opciones_p[etiqueta] = r["id"]
            sel_p = st.selectbox("Periodo", list(opciones_p.keys()))
            id_periodo = opciones_p[sel_p]

    st.write("---")
    st.write("1. Selecciona las secciones")
    ids_sel, etiquetas = selector_secciones_multiple("rep_sec", permitidas)
    if not ids_sel:
        st.write("Selecciona al menos una seccion.")
        return

    st.write("---")
    st.write("2. Configura el reporte")
    c1, c2, c3 = st.columns(3)
    with c1:
        tipo_rep = st.selectbox("Tipo de reporte", ["Detalle", "Conteo faltas", "Cierre mensual"])
    with c2:
        desde = st.date_input("Desde", ahora().date() - timedelta(days=7))
    with c3:
        hasta = st.date_input("Hasta", ahora().date())

    tipo_asist = st.selectbox("Asistencia", ["clases", "reforzamiento", "todas"])
    turnos_nombres = ["Todos"]
    for t in listar_turnos():
        turnos_nombres.append(t["nombre"])
    turno = st.selectbox("Turno", turnos_nombres)

    if st.button("Generar reporte"):
        if tipo_rep == "Cierre mensual":
            mes = ahora().month
            anio = ahora().year
            df = cierre_mensual(mes, anio, turno, ids_sel, id_periodo)
            if df.empty:
                st.write("Sin datos.")
            else:
                st.dataframe(df, use_container_width=True)
                st.download_button("Excel", df_a_xlsx(df), "cierre_" + str(anio) + "_" + str(mes).zfill(2) + ".xlsx")
            return

        if tipo_rep == "Conteo faltas":
            df = reporte_conteo_faltas(desde, hasta, turno, None, None, "", ids_sel, id_periodo)
            st.write(str(len(df)) + " alumnos con faltas")
        else:
            df = reporte_detalle(desde, hasta, turno, None, None, "", tipo_asist, ids_sel, id_periodo)
            st.write(str(len(df)) + " registros")

        if df.empty:
            st.write("Sin datos.")
            return

        st.dataframe(df, use_container_width=True)
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("Excel", df_a_xlsx(df), "reporte.xlsx", use_container_width=True)
        with c2:
            st.download_button("PDF", generar_pdf_tabla(df, "Reporte"), "reporte.pdf",
                               "application/pdf", use_container_width=True)


# ------------------- ui: alumnos -------------------
def fragmento_crear_alumno():
    st.write("Crear alumno manualmente")
    grados = listar_grados()
    if not grados:
        st.write("No hay grados.")
        return
    with st.form("crear_al", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            dni = st.text_input("DNI * (8 digitos)", max_chars=8)
            nombres = st.text_input("Nombres *")
            pat = st.text_input("Apellido Paterno *")
        with c2:
            mat = st.text_input("Apellido Materno")
            g = st.selectbox("Grado *", grados, format_func=lambda x: x["nombre"])
            secs = secciones_por_grado(g["id"]) if g else []
            s = None
            if secs:
                s = st.selectbox("Seccion *", secs, format_func=lambda x: x["nombre"])
        c3, c4 = st.columns(2)
        with c3:
            apo_n = st.text_input("Apoderado (opcional)")
        with c4:
            apo_t = st.text_input("Telefono (opcional)")
        if st.form_submit_button("Crear"):
            if not dni or not nombres or not pat or not s:
                st.write("Completa obligatorios.")
                return
            if not re.fullmatch(r"\d{8}", dni.strip()):
                st.write("DNI invalido.")
                return
            ok, msg = crear_alumno(dni.strip(), nombres.strip(), pat.strip(),
                                   mat.strip(), s["id"], apo_n.strip(),
                                   apo_t.strip(), st.session_state["user"])
            if ok:
                st.write(msg)
                st.rerun()
            else:
                st.write("Error: " + msg)


def fragmento_editar_alumno():
    st.write("Editar alumno")
    id_g, id_s, texto = filtros_grado_seccion_nombre("ed_al")
    if not (texto or id_g):
        return
    df = buscar_alumnos(texto, id_g, id_s, 50)
    if df.empty:
        st.write("Sin coincidencias.")
        return
    opciones = {}
    for _, r in df.iterrows():
        opciones[r["nombre_completo"] + " - " + r["grado"] + " " + r["seccion"]] = r["id"]
    sel = st.selectbox("Alumno", list(opciones.keys()), key="ed_sel")
    id_al = opciones[sel]

    con = obtener_conexion()
    datos = con.execute(
        "SELECT a.*, g.nombre AS grado, s.nombre AS seccion FROM alumnos a "
        "JOIN secciones s ON a.seccion_id = s.id "
        "JOIN grados g ON s.grado_id = g.id WHERE a.id = ?",
        (id_al,)
    ).fetchone()
    if not datos:
        return

    grados = listar_grados()
    with st.form("ed_form"):
        st.write("DNI: " + datos["dni"] + " (no editable)")
        apo_n = st.text_input("Apoderado", value=datos["nombre_apoderado"] or "")
        apo_t = st.text_input("Telefono", value=datos["telefono_apoderado"] or "")
        idx_g = 0
        for i, gr in enumerate(grados):
            if gr["nombre"] == datos["grado"]:
                idx_g = i
                break
        g = st.selectbox("Grado", grados, index=idx_g, format_func=lambda x: x["nombre"])
        secs = secciones_por_grado(g["id"]) if g else []
        idx_s = 0
        for i, se in enumerate(secs):
            if se["id"] == datos["seccion_id"]:
                idx_s = i
                break
        s = st.selectbox("Seccion", secs, index=idx_s, format_func=lambda x: x["nombre"])
        if st.form_submit_button("Guardar"):
            ok, msg = editar_alumno(id_al, apo_n, apo_t, s["id"], datos["dni"], st.session_state["user"])
            if ok:
                st.write(msg)
                st.rerun()
            else:
                st.write("Error: " + msg)


def fragmento_listar_alumnos():
    id_g, id_s, texto = filtros_grado_seccion_nombre("list_al")
    df = buscar_alumnos(texto, id_g, id_s, 5000)
    st.write(str(len(df)) + " alumnos")
    if df.empty:
        st.write("Sin resultados.")
        return
    mostrar_todos = st.checkbox("Mostrar todos", value=False)
    limite = len(df) if mostrar_todos else 50
    for _, al in df.head(limite).iterrows():
        c1, c2 = st.columns([5, 1])
        c1.write(al["nombre_completo"] + " - " + al["grado"] + " " + al["seccion"] + " (" + al["turno"] + ")")
        if c2.button("Ver perfil", key="perfil_" + str(al["id"])):
            st.session_state["perfil_alumno_id"] = al["id"]
            st.rerun()


def perfil_alumno_ui(id_alumno):
    datos = perfil_alumno_datos(id_alumno)
    if not datos:
        st.write("Alumno no encontrado.")
        st.session_state.pop("perfil_alumno_id", None)
        return
    al = datos["alumno"]
    usuario = st.session_state["user"]
    if st.button("Volver a la lista", key="volver_perfil"):
        st.session_state.pop("perfil_alumno_id", None)
        st.rerun()

    nombre = (al["apellido_paterno"] + " " + (al["apellido_materno"] or "") + ", " + al["nombres"]).strip(", ")

    if datos["bloqueado"]:
        st.write("ESTADO: BLOQUEADO")
    elif not datos["observados"].empty and any(datos["observados"]["activo"] == 1):
        st.write("ESTADO: OBSERVADO")

    st.write(nombre)
    st.write("DNI: " + al["dni"])
    st.write("Grado: " + al["grado"] + " | Seccion: " + al["seccion"] + " | Turno: " + al["turno"])
    st.write("Apoderado: " + (al["nombre_apoderado"] or "-") + " | Telefono: " + (al["telefono_apoderado"] or "-"))

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.write("Puntuales: " + str(datos["total_puntuales"]))
    c2.write("Tardanzas: " + str(datos["total_tardanzas"]))
    c3.write("Faltas: " + str(datos["total_faltas"]))
    c4.write("Reforz.: " + str(datos["total_ref_asistio"]))
    c5.write("Tard.inj.: " + str(datos["tard_injust"]))
    c6.write("Actas: " + str(len(datos["actas"])))

    st.write("Codigo QR")
    st.image(generar_qr(al["dni"]), width=180)

    c1, c2 = st.columns(2)
    with c1:
        pdf = pdf_carnet_alumno(al["dni"])
        if pdf:
            st.download_button("Descargar carnet QR", pdf, "carnet_" + al["dni"] + ".pdf", "application/pdf", use_container_width=True)
    with c2:
        st.download_button("Historial (Excel)", df_a_xlsx(datos["asistencias"], "Historial"),
                           "historial_" + al["dni"] + ".xlsx", use_container_width=True)

    if usuario["rol"] == "Admin":
        st.write("---")
        if al.get("activo", 1) == 1:
            with st.expander("Desactivar alumno"):
                st.write("Estas seguro que desea desactivar al alumno?")
                pwd = st.text_input("Ingrese su contrasena de Admin", type="password", key="pwd_desac")
                if st.button("Confirmar desactivacion", key="btn_desac"):
                    if not pwd:
                        st.write("Ingrese su contrasena.")
                    elif not verificar_password_admin(pwd):
                        st.write("Contrasena incorrecta.")
                    else:
                        retirar_alumno(al["id"], al["dni"], usuario)
                        st.rerun()
        else:
            with st.expander("Reactivar alumno"):
                pwd = st.text_input("Ingrese su contrasena de Admin", type="password", key="pwd_react")
                if st.button("Confirmar reactivacion", key="btn_react"):
                    if not pwd:
                        st.write("Ingrese su contrasena.")
                    elif not verificar_password_admin(pwd):
                        st.write("Contrasena incorrecta.")
                    else:
                        reactivar_alumno(al["id"], al["dni"], usuario)
                        st.rerun()

    st.write("---")
    tabs = st.tabs(["Asistencias", "Tardanzas", "Actas", "Observados", "Bloqueos", "Just. previas"])
    with tabs[0]:
        if datos["asistencias"].empty:
            st.write("Sin asistencias registradas.")
        else:
            st.dataframe(datos["asistencias"], use_container_width=True)
    with tabs[1]:
        if datos["tardanzas"].empty:
            st.write("Sin tardanzas registradas.")
        else:
            st.dataframe(datos["tardanzas"], use_container_width=True)
    with tabs[2]:
        if datos["actas"].empty:
            st.write("Sin actas firmadas.")
        else:
            st.dataframe(datos["actas"], use_container_width=True)
    with tabs[3]:
        if datos["observados"].empty:
            st.write("Sin registros de observados.")
        else:
            st.dataframe(datos["observados"], use_container_width=True)
    with tabs[4]:
        if datos["bloqueos"].empty:
            st.write("Sin bloqueos registrados.")
        else:
            st.dataframe(datos["bloqueos"], use_container_width=True)
    with tabs[5]:
        if datos["just_previas"].empty:
            st.write("Sin justificaciones previas.")
        else:
            st.dataframe(datos["just_previas"], use_container_width=True)


def vista_alumnos():
    st.write("Alumnos")
    id_perfil = st.session_state.get("perfil_alumno_id")
    if id_perfil:
        perfil_alumno_ui(id_perfil)
        return
    tabs = st.tabs(["Listar", "Crear", "Editar"])
    with tabs[0]:
        fragmento_listar_alumnos()
    with tabs[1]:
        fragmento_crear_alumno()
    with tabs[2]:
        fragmento_editar_alumno()


# ------------------- ui: grados y secciones -------------------
def vista_grados_secciones():
    st.write("Grados y Secciones")
    st.write("Las secciones se crean automaticamente al importar el Excel de alumnos.")
    con = obtener_conexion()
    grados = listar_grados()
    st.write("Grados")
    if grados:
        st.dataframe(pd.DataFrame(grados), use_container_width=True)
    else:
        st.write("Sin grados.")
    st.write("Secciones")
    df = pd.read_sql(
        "SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno, "
        "(SELECT COUNT(*) FROM alumnos a WHERE a.seccion_id = s.id AND a.activo = 1) AS alumnos_activos "
        "FROM secciones s "
        "JOIN grados g ON s.grado_id = g.id "
        "JOIN turnos t ON s.turno_id = t.id "
        "ORDER BY t.nombre, g.nombre, s.nombre",
        con
    )
    if df.empty:
        st.write("Sin secciones.")
    else:
        st.dataframe(df, use_container_width=True)


# ------------------- ui: carnets -------------------
def vista_carnets():
    st.write("Carnets QR")
    grados = listar_grados()
    if not grados:
        st.write("No hay grados.")
        return
    c1, c2 = st.columns(2)
    with c1:
        g = st.selectbox("Grado", grados, format_func=lambda x: x["nombre"], key="carn_g")
    with c2:
        secs = secciones_por_grado(g["id"]) if g else []
        if not secs:
            st.write("Sin secciones.")
            return
        s = st.selectbox("Seccion", secs, format_func=lambda x: x["nombre"], key="carn_s")
    df = alumnos_de_seccion(s["id"])
    if df.empty:
        st.write("Sin alumnos en esa seccion.")
        return

    st.write(str(len(df)) + " alumnos en " + g["nombre"] + " " + s["nombre"])
    seleccionados = []
    cols = st.columns(3)
    for i, (_, al) in enumerate(df.iterrows()):
        with cols[i % 3]:
            if st.checkbox(al["nombre_completo"], key="carn_" + str(al["id"])):
                seleccionados.append(al["id"])

    c1, c2 = st.columns(2)
    with c1:
        if seleccionados:
            if st.button("Descargar " + str(len(seleccionados)) + " carnet(s)", key="carn_sel"):
                pdf = pdf_carnets_seleccionados(seleccionados,
                    titulo="Carnets seleccionados - " + g["nombre"] + " " + s["nombre"])
                if pdf:
                    st.download_button("Descargar PDF", pdf,
                                       "carnets_sel_" + g["nombre"] + s["nombre"] + ".pdf",
                                       "application/pdf")
        else:
            st.write("Marca al menos un alumno.")
    with c2:
        if st.button("Descargar todo el salon", key="carn_todo"):
            pdf = pdf_carnets_por_seccion(s["id"])
            if pdf:
                st.download_button("Descargar PDF", pdf,
                                   "carnets_" + g["nombre"] + s["nombre"] + ".pdf",
                                   "application/pdf")


# ------------------- ui: ventanas -------------------
def vista_ventanas():
    st.write("Ventanas de asistencia")
    st.write("Configura apertura, limite puntual y cierre por turno y tipo.")
    for turno in listar_turnos():
        st.write("Turno " + turno["nombre"])
        ventanas = listar_ventanas(turno["id"])
        for v in ventanas:
            with st.expander(v["nombre"] + " (" + v["tipo"] + ")"):
                with st.form("v_" + str(v["id"])):
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        ap = st.text_input("Apertura", value=v["hora_apertura"])
                    with c2:
                        lim = st.text_input("Limite puntual", value=v["hora_limite_puntual"] or "")
                    with c3:
                        ci = st.text_input("Cierre", value=v["hora_cierre"])
                    if st.form_submit_button("Guardar"):
                        con = obtener_conexion()
                        con.execute("UPDATE ventanas SET hora_apertura = ?, hora_limite_puntual = ?, hora_cierre = ? WHERE id = ?",
                                    (ap, lim or ap, ci, v["id"]))
                        con.commit()
                        auditar(st.session_state["user"]["usuario"], "Edito ventana id=" + str(v["id"]))
                        st.write("Ventana actualizada")
                        st.rerun()


# ------------------- ui: usuarios -------------------
def vista_usuarios():
    st.write("Usuarios")
    usuario = st.session_state["user"]
    con = obtener_conexion()
    tabs = st.tabs(["Listar", "Crear", "Editar", "Asignar secciones"])

    with tabs[0]:
        df = pd.read_sql("SELECT id, usuario, rol, nombres, activo, ultimo_login FROM usuarios ORDER BY usuario", con)
        st.dataframe(df, use_container_width=True)

    with tabs[1]:
        with st.form("crear_u"):
            c1, c2 = st.columns(2)
            with c1:
                u = st.text_input("Usuario")
                p = st.text_input("Contrasena", type="password")
            with c2:
                n = st.text_input("Nombres")
                r = st.selectbox("Rol", list(ROLES_VALIDOS))
                turnos_opts = ["Ninguno"]
                for t in listar_turnos():
                    turnos_opts.append(t["nombre"])
                t_lbl = st.selectbox("Turno (solo Auxiliar)", turnos_opts)
            if st.form_submit_button("Crear"):
                if not u or not p or not n:
                    st.write("Completa campos.")
                elif len(p) < 6:
                    st.write("Contrasena min 6.")
                else:
                    id_turno = None
                    if t_lbl != "Ninguno" and r == "Auxiliar":
                        for x in listar_turnos():
                            if x["nombre"] == t_lbl:
                                id_turno = x["id"]
                                break
                    try:
                        con.execute("INSERT INTO usuarios (usuario, password, rol, nombres, turno_asignado) VALUES (?, ?, ?, ?, ?)",
                                    (u, hashear_password(p), r, n, id_turno))
                        con.commit()
                        auditar(usuario["usuario"], "Creo usuario " + u)
                        st.write("Usuario " + u + " creado")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.write("Usuario ya existe.")

    with tabs[2]:
        df = pd.read_sql("SELECT id, usuario, rol, nombres FROM usuarios WHERE usuario != 'admin'", con)
        if df.empty:
            st.write("Sin usuarios.")
        else:
            opciones = {}
            for _, r in df.iterrows():
                opciones[r["usuario"] + " (" + r["rol"] + ")"] = r["id"]
            sel = st.selectbox("Usuario", list(opciones.keys()), key="edit_u_sel")
            id_u = opciones[sel]
            datos = con.execute("SELECT * FROM usuarios WHERE id = ?", (id_u,)).fetchone()
            with st.form("edit_u"):
                u = st.text_input("Usuario", value=datos["usuario"])
                n = st.text_input("Nombres", value=datos["nombres"])
                p = st.text_input("Nueva contrasena (opcional)", type="password")
                r = st.selectbox("Rol", list(ROLES_VALIDOS), index=list(ROLES_VALIDOS).index(datos["rol"]))
                activo = st.checkbox("Activo", value=bool(datos["activo"]))
                submit_u = st.form_submit_button("Guardar")
            if submit_u:
                if p:
                    con.execute("UPDATE usuarios SET usuario=?, nombres=?, rol=?, password=?, activo=? WHERE id=?",
                                (u, n, r, hashear_password(p), 1 if activo else 0, id_u))
                else:
                    con.execute("UPDATE usuarios SET usuario=?, nombres=?, rol=?, activo=? WHERE id=?",
                                (u, n, r, 1 if activo else 0, id_u))
                con.commit()
                auditar(usuario["usuario"], "Edito usuario " + u)
                st.write("Usuario editado")
                st.rerun()

    with tabs[3]:
        st.write("Asignar secciones a Auxiliares")
        df_aux = pd.read_sql("SELECT id, usuario, nombres, turno_asignado FROM usuarios WHERE rol='Auxiliar' AND activo=1", con)
        if df_aux.empty:
            st.write("Sin auxiliares.")
            return
        opciones = {}
        for _, r in df_aux.iterrows():
            opciones[r["nombres"] + " (" + r["usuario"] + ")"] = r["id"]
        sel = st.selectbox("Auxiliar", list(opciones.keys()))
        id_aux = opciones[sel]
        turno_aux = con.execute("SELECT turno_asignado FROM usuarios WHERE id = ?", (id_aux,)).fetchone()
        if turno_aux and turno_aux["turno_asignado"]:
            secs = secciones_por_turno(turno_aux["turno_asignado"])
        else:
            st.write("Este auxiliar no tiene turno asignado.")
            return
        asignadas = []
        for r in con.execute("SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id = ?", (id_aux,)).fetchall():
            asignadas.append(r["seccion_id"])
        st.write("Secciones disponibles:")
        seleccionadas = []
        for s in secs:
            marcado = st.checkbox(s["grado"] + " " + s["nombre"], value=s["id"] in asignadas,
                                  key="asig_" + str(id_aux) + "_" + str(s["id"]))
            if marcado:
                seleccionadas.append(s["id"])
        if st.button("Guardar asignaciones"):
            con.execute("DELETE FROM auxiliar_secciones WHERE usuario_id = ?", (id_aux,))
            for sec_id in seleccionadas:
                con.execute("INSERT INTO auxiliar_secciones (usuario_id, seccion_id) VALUES (?, ?)", (id_aux, sec_id))
            con.commit()
            auditar(usuario["usuario"], "Asigno " + str(len(seleccionadas)) + " secciones a usuario_id=" + str(id_aux))
            st.write("Asignaciones guardadas")
            st.rerun()


# ------------------- ui: auditoria -------------------
def fragmento_importar_excel_periodo():
    st.write("Cargar alumnos al periodo")
    st.write("Columnas: DNI, Nombres, Apellido Paterno, Apellido Materno, Grado, Seccion, Turno, Apoderado, Telefono.")
    st.write("Si un DNI ya existe, se reactiva y actualiza grado/seccion.")
    archivo = st.file_uploader("Sube el Excel", type=["xlsx", "xls"], key="import_excel_periodo")
    if not archivo:
        return
    df = pd.read_excel(archivo)
    st.write(str(len(df)) + " filas detectadas.")
    columnas = list(df.columns)
    with st.form("mapeo_periodo"):
        c1, c2 = st.columns(2)
        with c1:
            m_dni = st.selectbox("DNI *", columnas)
            m_nom = st.selectbox("Nombres *", columnas)
            m_pat = st.selectbox("Apellido Paterno *", columnas)
            m_mat = st.selectbox("Apellido Materno", [""] + columnas)
        with c2:
            m_gra = st.selectbox("Grado *", columnas)
            m_sec = st.selectbox("Seccion *", columnas)
            m_tur = st.selectbox("Turno *", columnas)
            m_apo_n = st.selectbox("Nombre Apoderado", [""] + columnas)
            m_apo_t = st.selectbox("Telefono Apoderado", [""] + columnas)
        validar = st.form_submit_button("Validar")
    if validar:
        mapeo = {"dni": m_dni, "nombres": m_nom, "apellido_paterno": m_pat,
                 "apellido_materno": m_mat, "grado": m_gra, "seccion": m_sec,
                 "turno": m_tur, "apoderado_nombre": m_apo_n,
                 "apoderado_telefono": m_apo_t}
        validas, errores, resumen = validar_importacion(df, mapeo)
        st.session_state["_imp_validas"] = validas
        st.session_state["_imp_errores"] = errores
        st.session_state["_imp_resumen"] = resumen

    if "_imp_resumen" in st.session_state:
        r = st.session_state["_imp_resumen"]
        c1, c2, c3 = st.columns(3)
        c1.write("Total: " + str(r["total"]))
        c2.write("Validas: " + str(r["validas"]))
        c3.write("Errores: " + str(r["errores"]))
        if st.session_state["_imp_errores"]:
            with st.expander("Errores"):
                st.dataframe(pd.DataFrame(st.session_state["_imp_errores"]))
        if st.session_state["_imp_validas"]:
            if st.button("Importar validas"):
                ins, react, errs = insertar_alumnos_validos(st.session_state["_imp_validas"])
                st.write(str(ins) + " importados, " + str(react) + " reactivados.")
                if errs:
                    st.write(str(len(errs)) + " errores al insertar")
                for k in ["_imp_validas", "_imp_errores", "_imp_resumen"]:
                    st.session_state.pop(k, None)
                st.rerun()


def vista_auditoria():
    st.write("Auditoria y Periodos")
    usuario = st.session_state["user"]
    tabs = st.tabs(["Registros", "Periodos", "Cierre de año"])

    with tabs[0]:
        df = obtener_auditoria(500)
        st.write(str(len(df)) + " registros")
        if not df.empty:
            st.dataframe(df, use_container_width=True)

    with tabs[1]:
        st.write("Periodos")
        df = listar_periodos()
        st.dataframe(df, use_container_width=True)
        st.write("Crear nuevo periodo")
        with st.form("nuevo_periodo"):
            c1, c2, c3 = st.columns(3)
            with c1:
                nombre = st.text_input("Nombre (ej: 2026)")
            with c2:
                fi = st.date_input("Inicio", ahora().date())
            with c3:
                ff = st.date_input("Fin", ahora().date() + timedelta(days=270))
            if st.form_submit_button("Crear y activar"):
                if not nombre.strip():
                    st.write("Ingresa un nombre.")
                elif (ff - fi).days < 30:
                    st.write("El periodo debe durar minimo 1 mes.")
                else:
                    ok, msg = crear_periodo(nombre.strip(), fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"), usuario)
                    if ok:
                        st.write(msg)
                        st.rerun()
                    else:
                        st.write("Error: " + msg)
        st.write("---")
        st.write("Cargar alumnos al periodo activo")
        periodo = obtener_periodo_activo()
        if periodo:
            if periodo_tiene_alumnos(periodo["id"]):
                st.write("El periodo '" + periodo["nombre"] + "' ya tiene alumnos cargados.")
            else:
                st.write("El periodo '" + periodo["nombre"] + "' NO tiene alumnos. Sube el Excel.")
            fragmento_importar_excel_periodo()
        else:
            st.write("No hay periodo activo.")
        st.write("---")
        st.write("Activar periodo")
        df2 = listar_periodos()
        df2_act = df2[df2["cerrado"] == 0]
        if not df2_act.empty:
            opciones = {}
            for _, r in df2_act.iterrows():
                etiqueta = r["nombre"] + " (" + str(r["fecha_inicio"]) + " - " + str(r["fecha_fin"]) + ")"
                if r["activo"]:
                    etiqueta += " ACTIVO"
                opciones[etiqueta] = r["id"]
            sel = st.selectbox("Periodo a activar", list(opciones.keys()))
            if st.button("Activar"):
                ok, msg = activar_periodo(opciones[sel], usuario)
                if ok:
                    st.write(msg)
                    st.rerun()
                else:
                    st.write("Error: " + msg)

    with tabs[2]:
        st.write("Cierre de año escolar")
        st.write("Al cerrar el periodo se desactivan TODOS los alumnos de ese periodo.")
        periodo = obtener_periodo_activo()
        if not periodo:
            st.write("No hay periodo activo.")
            return
        st.write("Periodo activo: " + periodo["nombre"] + " (" + str(periodo["fecha_inicio"]) + " - " + str(periodo["fecha_fin"]) + ")")
        st.write("Reporte resumen del periodo:")
        df_rep = reporte_cierre_anual(periodo["id"])
        if not df_rep.empty:
            st.dataframe(df_rep, use_container_width=True)
        st.write("---")
        st.write("Paso 1: Descargar reporte anual detallado (OBLIGATORIO)")
        if st.button("Generar y descargar reporte anual", key="btn_desc_anual"):
            hojas = reporte_detallado_por_mes(periodo["id"])
            if not hojas:
                st.write("Sin datos para el reporte.")
            else:
                xlsx = df_a_xlsx_multilhoja(hojas)
                st.download_button("Descargar Excel anual", xlsx, "reporte_anual_" + periodo["nombre"] + ".xlsx",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                st.session_state["_reporte_descargado"] = True
                st.write("Reporte generado.")
        st.write("---")
        st.write("Paso 2: Cerrar periodo (requiere contrasena de Admin)")
        descargado = st.session_state.get("_reporte_descargado", False)
        if not descargado:
            st.write("Debes descargar el reporte anual antes de cerrar el periodo.")
        with st.form("cerrar_anio"):
            c1, c2, c3 = st.columns(3)
            with c1:
                nuevo_nombre = st.text_input("Nombre nuevo periodo", value=str(ahora().year + 1))
            with c2:
                fi = st.date_input("Inicio nuevo", date(ahora().year + 1, 3, 1))
            with c3:
                ff = st.date_input("Fin nuevo", date(ahora().year + 1, 12, 31))
            pwd = st.text_input("Contrasena de Admin", type="password")
            confirm = st.text_input("Escribe 'CERRAR' para confirmar")
            submit_cerrar = st.form_submit_button("Cerrar año escolar")
        if submit_cerrar:
            if not descargado:
                st.write("Primero debes descargar el reporte anual.")
            elif confirm.strip() != "CERRAR":
                st.write("Debes escribir exactamente 'CERRAR'.")
            elif not pwd:
                st.write("Ingresa la contrasena de Admin.")
            elif not verificar_password_admin(pwd):
                st.write("Contrasena incorrecta.")
            else:
                ok, msg = cerrar_anio_escolar(usuario, periodo["id"], nuevo_nombre,
                                              fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"))
                if ok:
                    st.session_state.pop("_reporte_descargado", None)
                    st.write(msg)
                    st.rerun()
                else:
                    st.write("Error: " + msg)
        st.write("---")
        st.write("Cierres anteriores:")
        df_cierres = listar_cierres_anuales()
        if not df_cierres.empty:
            st.dataframe(df_cierres, use_container_width=True)
        st.write("Periodos cerrados (solo consulta):")
        df_cerr = listar_periodos_cerrados()
        if not df_cerr.empty:
            st.dataframe(df_cerr, use_container_width=True)


# ------------------- ui: dias especiales -------------------
def vista_dias_especiales():
    st.write("Dias especiales")
    usuario = st.session_state["user"]
    con = obtener_conexion()
    tabs = st.tabs(["Crear", "Listar / eliminar"])

    with tabs[0]:
        if "dia_tipo" not in st.session_state:
            st.session_state["dia_tipo"] = "Evento"
        tipo = st.radio("Tipo", ["Evento", "Feriado"], key="dia_tipo", horizontal=True)

        with st.form("crear_dia"):
            c1, c2 = st.columns(2)
            with c1:
                fecha = st.date_input("Fecha", min_value=ahora().date())
                desc = st.text_input("Descripcion")
            with c2:
                if tipo == "Evento":
                    turnos_opts = {"Ambos": None}
                    for t in listar_turnos():
                        turnos_opts[t["nombre"]] = t["id"]
                    t_lbl = st.selectbox("Turno", list(turnos_opts.keys()))
                    hora = st.text_input("Hora entrada", value="08:00")
                    lim_puntual = st.text_input("Limite puntual (vacio = hereda)", value="")
                else:
                    turnos_opts = {"Ambos": None}
                    t_lbl = "Ambos"
                    hora = "00:00"
                    lim_puntual = ""
                    st.write("Los feriados no tienen horario ni turno.")

            st.write("Alcance del dia especial:")
            st.write("Si no marcas nada, aplica a TODO el colegio.")
            with st.expander("Filtrar por grados y secciones", expanded=False):
                grados = listar_grados()
                selecciones_secciones = []
                for g in grados:
                    st.checkbox("Todo " + g["nombre"], key="dia_grado_" + str(g["id"]))
                    secs = secciones_por_grado(g["id"])
                    if secs:
                        cols = st.columns(3)
                        for i, s in enumerate(secs):
                            with cols[i % 3]:
                                if st.checkbox(g["nombre"] + " " + s["nombre"],
                                               key="dia_sec_" + str(g["id"]) + "_" + str(s["id"])):
                                    selecciones_secciones.append(s["id"])

            submit_dia = st.form_submit_button("Crear")

        if submit_dia:
            if not desc.strip():
                st.write("Descripcion requerida.")
            elif tipo == "Evento" and not hora.strip():
                st.write("Hora de entrada requerida.")
            else:
                id_turno = turnos_opts.get(t_lbl) if tipo == "Evento" else None
                periodo = obtener_periodo_activo()
                id_periodo = periodo["id"] if periodo else None
                cur = con.execute(
                    "INSERT INTO dias_especiales (fecha, descripcion, turno_id, hora_entrada, tipo, periodo_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (fecha.strftime("%Y-%m-%d"), desc.strip(), id_turno,
                     hora if tipo == "Evento" else "00:00",
                     "evento" if tipo == "Evento" else "feriado", id_periodo)
                )
                id_dia = cur.lastrowid
                for sec_id in selecciones_secciones:
                    con.execute("INSERT INTO dias_especiales_secciones (dia_especial_id, seccion_id) VALUES (?, ?)",
                                (id_dia, sec_id))
                con.commit()
                auditar(usuario["usuario"], "Creo dia especial " + desc)
                st.write("Dia especial creado")
                st.rerun()

    with tabs[1]:
        fecha_hoy = hoy_str()
        df = pd.read_sql(
            "SELECT d.id, d.fecha, d.descripcion, COALESCE(t.nombre, 'Ambos') AS turno, "
            "d.hora_entrada, d.tipo, "
            "(SELECT COUNT(*) FROM dias_especiales_secciones WHERE dia_especial_id = d.id) AS num_secciones "
            "FROM dias_especiales d LEFT JOIN turnos t ON d.turno_id = t.id "
            "WHERE d.fecha >= ? AND d.activo = 1 ORDER BY d.fecha",
            con, params=[fecha_hoy]
        )
        if df.empty:
            st.write("Sin dias especiales.")
        else:
            st.dataframe(df, use_container_width=True)
            opciones = {}
            for _, r in df.iterrows():
                opciones[r["fecha"] + " - " + r["descripcion"] + " (" + r["tipo"] + ")"] = r["id"]
            sel = st.selectbox("Eliminar", list(opciones.keys()))
            if st.button("Eliminar"):
                con.execute("DELETE FROM dias_especiales WHERE id = ?", (opciones[sel],))
                con.commit()
                auditar(usuario["usuario"], "Elimino dia especial id=" + str(opciones[sel]))
                st.write("Dia especial eliminado")
                st.rerun()


# ------------------- menu y rutas -------------------
OPCIONES_POR_ROL = {
    "Admin": ["Puerta", "TOECE", "Panel Direccion", "Reportes",
              "Alumnos", "Grados y Secciones", "Carnets",
              "Dias especiales", "Ventanas", "Usuarios", "Auditoria"],
    "TOECE": ["Puerta", "TOECE", "Reportes", "Dias especiales"],
    "Direccion": ["Puerta", "TOECE", "Panel Direccion", "Reportes",
                  "Carnets", "Auditoria"],
    "Auxiliar": ["Puerta", "Reportes"],
}

RUTAS = {
    "Puerta": vista_puerta,
    "TOECE": vista_toece,
    "Panel Direccion": vista_panel_direccion,
    "Reportes": vista_reportes,
    "Alumnos": vista_alumnos,
    "Grados y Secciones": vista_grados_secciones,
    "Carnets": vista_carnets,
    "Dias especiales": vista_dias_especiales,
    "Ventanas": vista_ventanas,
    "Usuarios": vista_usuarios,
    "Auditoria": vista_auditoria,
}


def menu_lateral():
    usuario = st.session_state["user"]
    rol = usuario["rol"]
    opciones = OPCIONES_POR_ROL.get(rol, [])
    with st.sidebar:
        st.write(usuario["nombres"])
        st.write("Rol: " + rol)
        if "menu" not in st.session_state or st.session_state["menu"] not in opciones:
            st.session_state["menu"] = opciones[0]
        opcion = st.radio("Menu", opciones, key="menu")
        st.write("---")
        if st.button("Cerrar sesion", use_container_width=True):
            auditar(usuario["usuario"], "Logout")
            token = st.session_state.get("_token")
            if token:
                eliminar_token(token)
            borrar_token_cookie()
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()
    return opcion


def enrutar(opcion, usuario):
    vista = RUTAS.get(opcion)
    if not vista:
        st.write("Vista no disponible.")
        return
    permitidos = OPCIONES_POR_ROL.get(usuario["rol"], [])
    if opcion not in permitidos:
        st.write("Sin permisos.")
        auditar(usuario["usuario"], "Intento acceso no autorizado a " + opcion)
        return
    vista()


# ------------------- main -------------------
def control_faltas_periodico():
    ultimo = st.session_state.get("_ultimo_control_faltas")
    ahora_ts = time.time()
    if ultimo and (ahora_ts - ultimo) < 300:
        return
    st.session_state["_ultimo_control_faltas"] = ahora_ts
    marcar_faltas_al_cierre()


def main():
    st.set_page_config(
        page_title="Asistencia I.E. Yarinacocha",
        page_icon="escudo.png",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inicializar_bd()
    inicializar_sesion()

    if "user" not in st.session_state or not st.session_state["user"]:
        vista_login()
        return

    refrescar_sesion_si_necesario()

    if st.session_state["user"].get("debe_cambiar_password"):
        vista_cambio_password_obligatorio()
        return

    if sistema_bloqueado():
        st.write("El sistema no esta configurado. No hay periodo activo con alumnos cargados.")
        if st.session_state["user"]["rol"] == "Admin":
            st.write("Ve a Auditoria -> Periodos para crear un periodo y subir el Excel de alumnos.")
            st.session_state["menu"] = "Auditoria"
            enrutar("Auditoria", st.session_state["user"])
        else:
            st.write("Contacta al Administrador para que configure el periodo.")
        return

    control_faltas_periodico()
    opcion = menu_lateral()
    if opcion:
        enrutar(opcion, st.session_state["user"])


if __name__ == "__main__":
    main()
