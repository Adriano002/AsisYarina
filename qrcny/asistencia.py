import base64
import hashlib
import json
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

import pandas as pd
import qrcode
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    Image as RLImage, PageBreak, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle
)

from qr_scanner_component import qr_scanner

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    def st_autorefresh(**kwargs):
        pass

try:
    from streamlit_drawable_canvas import st_canvas
except ImportError:
    st_canvas = None


# ============================================================
# LOGS
# ============================================================
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("asistencia")


# ============================================================
# CONFIG
# ============================================================
DB_PATH = "asistencia.db"
MESES_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
C_NARANJA = "#E65100"
C_NARANJA_H = "#BF360C"

PBK_ITER = 260_000
PBK_ALG = "sha256"
MAX_INTENTOS = 3
MIN_BLOQUEO = 10

PUNTUAL = "Puntual"
TARDANZA = "Tardanza"
FALTA = "Falta"
PERMISO = "Permiso"
REF_ASISTIO = "Asistio"
REF_NO_ASISTIO = "No asistio"
ACC_PERDONADO = "PERDONADO"
ACC_DERIVADO = "DERIVADO_TOECE"
ACC_RETENIDO = "RETENIDO_APODERADO"
VENT_CLASES = "clases"
VENT_REF = "reforzamiento"
TIPO_ASIST_EVENTO = "evento"
MAX_DIAS_PERMISO = 7


# ============================================================
# HELPERS TIEMPO
# ============================================================
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
    return (fecha or ahora().date()).weekday() >= 5


# ============================================================
# SEGURIDAD
# ============================================================
def hashear_password(password):
    salt = secrets.token_bytes(16)
    d = hashlib.pbkdf2_hmac(PBK_ALG, password.encode("utf-8"), salt, PBK_ITER)
    return f"pbkdf2_{PBK_ALG}${PBK_ITER}${salt.hex()}${d.hex()}"


def verificar_password(password, hash_guardado):
    if hash_guardado.startswith("pbkdf2_"):
        try:
            _, it, salt_hex, hash_hex = hash_guardado.split("$")
            d = hashlib.pbkdf2_hmac(PBK_ALG, password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(it))
            return secrets.compare_digest(d.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    return secrets.compare_digest(
        hashlib.sha256(password.encode()).hexdigest(), hash_guardado)


def verificar_password_critica(password):
    con = obtener_conexion()
    for fila in con.execute(
        "SELECT password FROM usuarios WHERE rol IN ('Admin','TOECE') AND activo=1"
    ).fetchall():
        if verificar_password(password, fila["password"]):
            return True
    return False


# ============================================================
# CONEXION SQLITE
# ============================================================
_hilos = threading.local()
_lock_escritura = threading.Lock()


def obtener_conexion():
    if not hasattr(_hilos, "con") or _hilos.con is None:
        con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        con.row_factory = sqlite3.Row
        for p in ("journal_mode=WAL", "synchronous=NORMAL",
                  "foreign_keys=ON", "busy_timeout=30000"):
            try:
                con.execute("PRAGMA " + p)
            except sqlite3.Error as e:
                log.warning("pragma %s: %s", p, e)
        _hilos.con = con
        log.info("sqlite conexion creada para hilo %s", threading.get_ident())
    return _hilos.con


def escribir(sql, params=()):
    with _lock_escritura:
        con = obtener_conexion()
        cur = con.execute(sql, params)
        con.commit()
        return cur


def existe_columna(cur, tabla, col):
    return any(f["name"] == col for f in cur.execute(
        "PRAGMA table_info(" + tabla + ")").fetchall())


def _tabla_existe(cur, tabla):
    return cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (tabla,)
    ).fetchone() is not None


# ============================================================
# INICIALIZAR BD
# ============================================================
def inicializar_bd():
    con = obtener_conexion()
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS periodos(id INTEGER PRIMARY KEY,nombre TEXT NOT NULL,fecha_inicio TEXT,fecha_fin TEXT,activo INTEGER DEFAULT 0,fecha_cierre TEXT,cerrado INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS turnos(id INTEGER PRIMARY KEY,nombre TEXT UNIQUE,hora_entrada TEXT,hora_salida TEXT,tolerancia_min INTEGER DEFAULT 10);
    CREATE TABLE IF NOT EXISTS grados(id INTEGER PRIMARY KEY,nombre TEXT UNIQUE);
    CREATE TABLE IF NOT EXISTS secciones(id INTEGER PRIMARY KEY,nombre TEXT,grado_id INTEGER,turno_id INTEGER,UNIQUE(nombre,grado_id,turno_id));
    CREATE TABLE IF NOT EXISTS apoderados(id INTEGER PRIMARY KEY,nombre TEXT NOT NULL,telefono TEXT);
    CREATE TABLE IF NOT EXISTS alumnos(id INTEGER PRIMARY KEY,dni TEXT UNIQUE NOT NULL,nombres TEXT NOT NULL,apellido_paterno TEXT NOT NULL,apellido_materno TEXT,seccion_id INTEGER,apoderado_id INTEGER,nombre_apoderado TEXT,telefono_apoderado TEXT,periodo_id INTEGER,activo INTEGER DEFAULT 1,retirado_en TEXT);
    CREATE TABLE IF NOT EXISTS ventanas(id INTEGER PRIMARY KEY,turno_id INTEGER NOT NULL,tipo TEXT NOT NULL CHECK(tipo IN ('clases','reforzamiento')),nombre TEXT NOT NULL,hora_apertura TEXT NOT NULL,hora_limite_puntual TEXT,hora_cierre TEXT NOT NULL,tolerancia_min INTEGER DEFAULT 0,orden INTEGER DEFAULT 0,activo INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS asistencias(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha TEXT NOT NULL,ventana_id INTEGER,tipo TEXT NOT NULL DEFAULT 'clases',hora TEXT,estado TEXT NOT NULL,justificada INTEGER DEFAULT 0,observacion TEXT,origen TEXT DEFAULT 'qr',justificado_por TEXT,justificado_en TEXT,periodo_id INTEGER,UNIQUE(alumno_id,fecha,tipo));
    CREATE TABLE IF NOT EXISTS tardanzas(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha TEXT NOT NULL,hora TEXT NOT NULL,numero INTEGER NOT NULL,accion TEXT NOT NULL,observacion TEXT,justificada INTEGER DEFAULT 0,origen TEXT DEFAULT 'qr',registrado_por TEXT,timestamp TEXT NOT NULL,periodo_id INTEGER,UNIQUE(alumno_id,fecha));
    CREATE TABLE IF NOT EXISTS actas_compromiso(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha TEXT NOT NULL,motivo TEXT,observacion TEXT,registrado_por TEXT,timestamp TEXT NOT NULL,periodo_id INTEGER);
    CREATE TABLE IF NOT EXISTS observados(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha_ingreso TEXT NOT NULL,motivo TEXT,activo INTEGER DEFAULT 1,fecha_salida TEXT,observacion_cierre TEXT,periodo_id INTEGER);
    CREATE TABLE IF NOT EXISTS bloqueos(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,motivo TEXT,activo INTEGER DEFAULT 1,fecha_inicio TEXT NOT NULL,fecha_fin TEXT,liberado_por TEXT,creado_por TEXT,origen TEXT DEFAULT 'automatico');
    CREATE TABLE IF NOT EXISTS justificaciones_previas(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha_objetivo TEXT NOT NULL,tipo TEXT NOT NULL CHECK(tipo IN ('Falta','Tardanza')),motivo TEXT,creado_por TEXT,timestamp TEXT NOT NULL,aplicada INTEGER DEFAULT 0,UNIQUE(alumno_id,fecha_objetivo));
    CREATE TABLE IF NOT EXISTS permisos(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha_inicio TEXT NOT NULL,fecha_fin TEXT NOT NULL,motivo TEXT,tipo TEXT NOT NULL DEFAULT 'permiso',creado_por TEXT,timestamp TEXT NOT NULL,activo INTEGER DEFAULT 1,periodo_id INTEGER);
    CREATE TABLE IF NOT EXISTS dias_especiales(id INTEGER PRIMARY KEY,fecha TEXT NOT NULL,descripcion TEXT,turno_id INTEGER,hora_entrada TEXT,activo INTEGER DEFAULT 1,tipo TEXT DEFAULT 'evento' CHECK(tipo IN ('evento','feriado')),periodo_id INTEGER);
    CREATE TABLE IF NOT EXISTS dias_especiales_secciones(id INTEGER PRIMARY KEY,dia_especial_id INTEGER NOT NULL,seccion_id INTEGER NOT NULL,UNIQUE(dia_especial_id,seccion_id));
    CREATE TABLE IF NOT EXISTS usuarios(id INTEGER PRIMARY KEY,usuario TEXT UNIQUE NOT NULL,password TEXT NOT NULL,rol TEXT NOT NULL CHECK(rol IN ('Admin','TOECE','Auxiliar','Direccion')),nombres TEXT NOT NULL,turno_asignado INTEGER,activo INTEGER DEFAULT 1,intentos_fallidos INTEGER DEFAULT 0,bloqueado_hasta TEXT,debe_cambiar_password INTEGER DEFAULT 0,ultimo_login TEXT,ultimo_ip TEXT,es_principal INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS auxiliar_secciones(id INTEGER PRIMARY KEY,usuario_id INTEGER NOT NULL,seccion_id INTEGER NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS auditoria(id INTEGER PRIMARY KEY,usuario TEXT,accion TEXT,fecha TEXT,valor_anterior TEXT,valor_nuevo TEXT,tabla_afectada TEXT,registro_id INTEGER,ip TEXT);
    CREATE TABLE IF NOT EXISTS cierres_anuales(id INTEGER PRIMARY KEY,periodo_id INTEGER NOT NULL,fecha_cierre TEXT NOT NULL,generado_por TEXT,reporte_json TEXT);
    CREATE TABLE IF NOT EXISTS config(id INTEGER PRIMARY KEY,clave TEXT UNIQUE NOT NULL,valor TEXT);
    CREATE INDEX IF NOT EXISTS idx_ast_f ON asistencias(fecha);
    CREATE INDEX IF NOT EXISTS idx_ast_a ON asistencias(alumno_id);
    CREATE INDEX IF NOT EXISTS idx_tard_a ON tardanzas(alumno_id);
    CREATE INDEX IF NOT EXISTS idx_tard_f ON tardanzas(fecha);
    CREATE INDEX IF NOT EXISTS idx_al_dni ON alumnos(dni);
    CREATE INDEX IF NOT EXISTS idx_al_sec ON alumnos(seccion_id);
    CREATE INDEX IF NOT EXISTS idx_vent_t ON ventanas(turno_id,tipo);
    CREATE INDEX IF NOT EXISTS idx_bloq_a ON bloqueos(alumno_id,activo);
    CREATE INDEX IF NOT EXISTS idx_jp_a ON justificaciones_previas(alumno_id,fecha_objetivo);
    CREATE INDEX IF NOT EXISTS idx_aud_f ON auditoria(fecha);
    CREATE INDEX IF NOT EXISTS idx_aux_sec_unica ON auxiliar_secciones(seccion_id);
    """)

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS modos_camara(
        id INTEGER PRIMARY KEY,
        codigo TEXT UNIQUE NOT NULL,
        nombre TEXT NOT NULL,
        tabla_destino TEXT NOT NULL CHECK(tabla_destino IN ('asistencias','incidencias','otro')),
        tipo_asistencia TEXT DEFAULT 'ninguno',
        obedece_ventana INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1,
        orden INTEGER DEFAULT 0,
        es_sistema INTEGER DEFAULT 0,
        descripcion TEXT
    );
    CREATE TABLE IF NOT EXISTS tipos_incidencia(
        id INTEGER PRIMARY KEY,
        nombre TEXT UNIQUE NOT NULL,
        es_reincidente_grave INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1,
        orden INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS lugares_incidencia(
        id INTEGER PRIMARY KEY,
        nombre TEXT UNIQUE NOT NULL,
        activo INTEGER DEFAULT 1,
        orden INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS tipos_acta(
        id INTEGER PRIMARY KEY,
        nombre TEXT UNIQUE NOT NULL,
        requiere_firma_estudiante INTEGER DEFAULT 1,
        requiere_firma_apoderado INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1,
        orden INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS cursos(
        id INTEGER PRIMARY KEY,
        nombre TEXT UNIQUE NOT NULL,
        orden INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS incidencias(
        id INTEGER PRIMARY KEY,
        alumno_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        hora TEXT NOT NULL,
        tipo_id INTEGER,
        lugar_id INTEGER,
        antecedentes INTEGER DEFAULT 0,
        descripcion_hechos TEXT,
        descripcion_breve TEXT,
        estado TEXT NOT NULL DEFAULT 'reportada'
            CHECK(estado IN ('reportada','en_revision','acta_pendiente','cerrada','eliminada')),
        reportado_por TEXT,
        creado_por TEXT,
        creado_en TEXT NOT NULL,
        cerrado_por TEXT,
        cerrado_en TEXT,
        eliminada_por TEXT,
        eliminada_en TEXT,
        motivo_eliminacion TEXT,
        periodo_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS incidencias_seguimiento(
        id INTEGER PRIMARY KEY,
        incidencia_id INTEGER NOT NULL,
        tipo TEXT NOT NULL CHECK(tipo IN ('acuerdo','intervencion_docente','observacion','cierre')),
        detalle TEXT NOT NULL,
        registrado_por TEXT NOT NULL,
        timestamp TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS incidencias_firmas(
        id INTEGER PRIMARY KEY,
        incidencia_id INTEGER NOT NULL,
        rol_firmante TEXT NOT NULL CHECK(rol_firmante IN ('toece','auxiliar','docente','apoderado','estudiante')),
        nombre_firmante TEXT,
        imagen_base64 TEXT NOT NULL,
        timestamp TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS actas(
        id INTEGER PRIMARY KEY,
        incidencia_id INTEGER,
        alumno_id INTEGER NOT NULL,
        tipo_acta_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        asunto TEXT,
        detalle TEXT,
        estado TEXT NOT NULL DEFAULT 'pendiente_firmas'
            CHECK(estado IN ('pendiente_firmas','firmada','cerrada','eliminada')),
        creado_por TEXT NOT NULL,
        creado_en TEXT NOT NULL,
        cerrado_por TEXT,
        cerrado_en TEXT,
        periodo_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS actas_firmas(
        id INTEGER PRIMARY KEY,
        acta_id INTEGER NOT NULL,
        rol_firmante TEXT NOT NULL CHECK(rol_firmante IN ('estudiante','apoderado','toece')),
        nombre_firmante TEXT,
        imagen_base64 TEXT NOT NULL,
        timestamp TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS citaciones(
        id INTEGER PRIMARY KEY,
        alumno_id INTEGER NOT NULL,
        motivo TEXT,
        fecha_reunion TEXT,
        hora_reunion TEXT,
        agenda TEXT,
        responsable TEXT,
        estado TEXT NOT NULL DEFAULT 'borrador'
            CHECK(estado IN ('borrador','generada','entregada')),
        creado_por TEXT NOT NULL,
        creado_en TEXT NOT NULL,
        periodo_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS justificaciones_doc(
        id INTEGER PRIMARY KEY,
        alumno_id INTEGER NOT NULL,
        motivo TEXT,
        fecha TEXT NOT NULL,
        estado TEXT NOT NULL DEFAULT 'borrador'
            CHECK(estado IN ('borrador','firmada','impresa')),
        observaciones TEXT,
        creado_por TEXT NOT NULL,
        creado_en TEXT NOT NULL,
        periodo_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS justificaciones_doc_cursos(
        id INTEGER PRIMARY KEY,
        justificacion_id INTEGER NOT NULL,
        curso_id INTEGER NOT NULL,
        docente_nombre TEXT,
        firmado INTEGER DEFAULT 0,
        firmado_en TEXT,
        UNIQUE(justificacion_id, curso_id)
    );
    CREATE TABLE IF NOT EXISTS permisos_impresos(
        id INTEGER PRIMARY KEY,
        alumno_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        motivo TEXT,
        hora_salida TEXT,
        hora_llegada_destino TEXT,
        nombre_padre TEXT,
        cargo_autoriza TEXT,
        estado TEXT NOT NULL DEFAULT 'generado'
            CHECK(estado IN ('generado','entregado','devuelto')),
        creado_por TEXT NOT NULL,
        creado_en TEXT NOT NULL,
        periodo_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS mapeos_excel(
        id INTEGER PRIMARY KEY,
        nombre TEXT UNIQUE NOT NULL,
        mapeo_json TEXT NOT NULL,
        creado_por TEXT,
        creado_en TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS alias_grados(
        id INTEGER PRIMARY KEY,
        alias TEXT UNIQUE NOT NULL,
        valor_real TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS alias_turnos(
        id INTEGER PRIMARY KEY,
        alias TEXT UNIQUE NOT NULL,
        valor_real TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_modos_activo ON modos_camara(activo, orden);
    CREATE INDEX IF NOT EXISTS idx_inc_alumno ON incidencias(alumno_id);
    CREATE INDEX IF NOT EXISTS idx_inc_estado ON incidencias(estado);
    CREATE INDEX IF NOT EXISTS idx_inc_fecha ON incidencias(fecha);
    CREATE INDEX IF NOT EXISTS idx_seg_inc ON incidencias_seguimiento(incidencia_id);
    CREATE INDEX IF NOT EXISTS idx_firm_inc ON incidencias_firmas(incidencia_id);
    CREATE INDEX IF NOT EXISTS idx_actas_alumno ON actas(alumno_id);
    CREATE INDEX IF NOT EXISTS idx_actas_estado ON actas(estado);
    CREATE INDEX IF NOT EXISTS idx_actasf_acta ON actas_firmas(acta_id);
    """)

    _migrar(cur)
    _seed(cur)
    con.commit()
    log.info("bd lista")


def _migrar(cur):
    migs = [
        ("alumnos", "activo", "ALTER TABLE alumnos ADD COLUMN activo INTEGER DEFAULT 1"),
        ("alumnos", "retirado_en", "ALTER TABLE alumnos ADD COLUMN retirado_en TEXT"),
        ("usuarios", "ultimo_login", "ALTER TABLE usuarios ADD COLUMN ultimo_login TEXT"),
        ("usuarios", "ultimo_ip", "ALTER TABLE usuarios ADD COLUMN ultimo_ip TEXT"),
        ("usuarios", "es_principal", "ALTER TABLE usuarios ADD COLUMN es_principal INTEGER DEFAULT 0"),
        ("auditoria", "ip", "ALTER TABLE auditoria ADD COLUMN ip TEXT"),
        ("asistencias", "tipo", "ALTER TABLE asistencias ADD COLUMN tipo TEXT DEFAULT 'clases'"),
        ("asistencias", "ventana_id", "ALTER TABLE asistencias ADD COLUMN ventana_id INTEGER"),
        ("asistencias", "origen", "ALTER TABLE asistencias ADD COLUMN origen TEXT DEFAULT 'qr'"),
        ("asistencias", "justificado_por", "ALTER TABLE asistencias ADD COLUMN justificado_por TEXT"),
        ("asistencias", "justificado_en", "ALTER TABLE asistencias ADD COLUMN justificado_en TEXT"),
        ("tardanzas", "origen", "ALTER TABLE tardanzas ADD COLUMN origen TEXT DEFAULT 'qr'"),
        ("periodos", "cerrado", "ALTER TABLE periodos ADD COLUMN cerrado INTEGER DEFAULT 0"),
        ("observados", "observacion_cierre", "ALTER TABLE observados ADD COLUMN observacion_cierre TEXT"),
        ("bloqueos", "origen", "ALTER TABLE bloqueos ADD COLUMN origen TEXT DEFAULT 'automatico'"),
        ("asistencias", "modo_camara_id", "ALTER TABLE asistencias ADD COLUMN modo_camara_id INTEGER"),
        ("usuarios", "ultima_lectura_notif", "ALTER TABLE usuarios ADD COLUMN ultima_lectura_notif TEXT"),
        ("periodos", "resumen_descargado", "ALTER TABLE periodos ADD COLUMN resumen_descargado INTEGER DEFAULT 0"),
        ("bloqueos", "desbloqueado_en", "ALTER TABLE bloqueos ADD COLUMN desbloqueado_en TEXT"),
        ("bloqueos", "desbloqueado_por", "ALTER TABLE bloqueos ADD COLUMN desbloqueado_por TEXT"),
        ("bloqueos", "motivo_desbloqueo", "ALTER TABLE bloqueos ADD COLUMN motivo_desbloqueo TEXT"),
        ("bloqueos", "acta_id", "ALTER TABLE bloqueos ADD COLUMN acta_id INTEGER"),
    ]
    for t, c, sql in migs:
        if _tabla_existe(cur, t) and not existe_columna(cur, t, c):
            try:
                cur.execute(sql)
            except sqlite3.Error as e:
                log.warning("mig %s.%s: %s", t, c, e)
    try:
        cur.execute("""
            DELETE FROM auxiliar_secciones
            WHERE id NOT IN (
                SELECT MIN(id) FROM auxiliar_secciones GROUP BY seccion_id
            )
        """)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_aux_sec_unica ON auxiliar_secciones(seccion_id)")
    except sqlite3.Error as e:
        log.warning("migracion auxiliar_secciones: %s", e)
    try:
        cur.execute("UPDATE usuarios SET usuario = LOWER(TRIM(usuario)) WHERE usuario != LOWER(TRIM(usuario))")
    except sqlite3.Error as e:
        log.warning("migracion usuarios lowercase: %s", e)


def _seed(cur):
    if cur.execute("SELECT COUNT(*) FROM turnos").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO turnos(nombre,hora_entrada,hora_salida,tolerancia_min) VALUES(?,?,?,?)",
            [("Mañana", "06:00", "12:25", 10), ("Tarde", "12:00", "18:10", 10)]
        )
    if cur.execute("SELECT COUNT(*) FROM ventanas").fetchone()[0] == 0:
        tm = cur.execute("SELECT id FROM turnos WHERE nombre='Mañana'").fetchone()
        tt = cur.execute("SELECT id FROM turnos WHERE nombre='Tarde'").fetchone()
        if tm:
            cur.execute(
                "INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,hora_limite_puntual,hora_cierre,tolerancia_min,orden) "
                "VALUES(?,'clases','Clases mañana','06:00','06:55','07:10',10,1)", (tm["id"],))
            cur.execute(
                "INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,hora_limite_puntual,hora_cierre,tolerancia_min,orden) "
                "VALUES(?,'reforzamiento','Reforzamiento mañana','08:00','08:00','14:00',0,2)", (tm["id"],))
        if tt:
            cur.execute(
                "INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,hora_limite_puntual,hora_cierre,tolerancia_min,orden) "
                "VALUES(?,'reforzamiento','Reforzamiento tarde','10:00','10:00','10:20',0,1)", (tt["id"],))
            cur.execute(
                "INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,hora_limite_puntual,hora_cierre,tolerancia_min,orden) "
                "VALUES(?,'clases','Clases tarde','12:00','12:49','18:10',10,2)", (tt["id"],))
    if cur.execute("SELECT COUNT(*) FROM grados").fetchone()[0] == 0:
        for g in ["1ro", "2do", "3ro", "4to", "5to"]:
            cur.execute("INSERT INTO grados(nombre) VALUES(?)", (g,))
    if cur.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        pwd = secrets.token_urlsafe(9)
        cur.execute(
            "INSERT INTO usuarios(usuario,password,rol,nombres,debe_cambiar_password,es_principal) "
            "VALUES(?,?,'Admin','Administrador',1,1)",
            ("admin", hashear_password(pwd))
        )
        log.warning("admin creado, pass temporal: %s", pwd)

    if cur.execute("SELECT COUNT(*) FROM modos_camara").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO modos_camara(codigo,nombre,tabla_destino,tipo_asistencia,obedece_ventana,activo,orden,es_sistema,descripcion) "
            "VALUES(?,?,?,?,?,1,?,?,?)",
            [
                ("clases", "Asistencia clases", "asistencias", "clases", 1, 1, 1, "Asistencia en horario escolar. Obedece ventana del turno."),
                ("reforzamiento", "Reforzamiento", "asistencias", "reforzamiento", 0, 2, 1, "Asistencia a reforzamiento. No obedece ventana de clases."),
                ("psicologia", "Cita psicologia", "asistencias", "evento", 0, 3, 0, "Asistencia a cita con psicologia."),
                ("evento", "Evento del salon", "asistencias", "evento", 0, 4, 0, "Asistencia a evento especial del salon."),
                ("incidencia", "Incidencia", "incidencias", "ninguno", 0, 5, 1, "Reporte de incidencia. TOECE completa y cierra."),
                ("citacion", "Citacion", "otro", "ninguno", 0, 6, 0, "Genera citacion para apoderado."),
                ("permiso", "Permiso", "otro", "ninguno", 0, 7, 0, "Genera permiso impreso para el estudiante."),
            ]
        )
    if cur.execute("SELECT COUNT(*) FROM tipos_incidencia").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO tipos_incidencia(nombre,es_reincidente_grave,activo,orden) VALUES(?,?,1,?)",
            [
                ("Falta de respeto", 1, 1),
                ("Agresion verbal", 1, 2),
                ("Agresion fisica", 1, 3),
                ("Bullying", 1, 4),
                ("Molestar a companeros", 0, 5),
                ("No trae tareas", 0, 6),
                ("Uso de celular en clase", 0, 7),
                ("Corte de cabello inadecuado", 0, 8),
                ("Llegada tarde reiterada", 0, 9),
                ("Dano a la propiedad", 1, 10),
                ("Indisciplina en clase", 0, 11),
                ("Otro", 0, 99),
            ]
        )
    if cur.execute("SELECT COUNT(*) FROM lugares_incidencia").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO lugares_incidencia(nombre,activo,orden) VALUES(?,1,?)",
            [
                ("Aula", 1), ("Patio", 2), ("Bano", 3), ("Biblioteca", 4),
                ("Laboratorio", 5), ("Pasillo", 6), ("Puerta del colegio", 7),
                ("Loza deportiva", 8), ("Otro", 99),
            ]
        )
    if cur.execute("SELECT COUNT(*) FROM tipos_acta").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO tipos_acta(nombre,requiere_firma_estudiante,requiere_firma_apoderado,activo,orden) VALUES(?,?,?,1,?)",
            [
                ("Acta de compromiso", 1, 0, 1),
                ("Acta de compromiso con apoderado", 1, 1, 2),
                ("Acta por reincidencia", 1, 1, 3),
                ("Acta por agresion", 1, 1, 4),
                ("Acta por dano a la propiedad", 1, 1, 5),
            ]
        )
    if cur.execute("SELECT COUNT(*) FROM cursos").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO cursos(nombre,orden,activo) VALUES(?,?,1)",
            [
                ("Ingles", 1), ("Ciencia y Tecnologia", 2), ("DPCC", 3),
                ("Arte y Cultura", 4), ("Religion", 5), ("Ciencias Sociales", 6),
                ("Matematica", 7), ("Comunicacion", 8), ("Educacion Fisica", 9),
                ("EPT", 10), ("Tutoria", 11),
            ]
        )
    if cur.execute("SELECT COUNT(*) FROM alias_grados").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO alias_grados(alias,valor_real) VALUES(?,?)",
            [
                ("1°", "1ro"), ("1o", "1ro"), ("1ero", "1ro"), ("primero", "1ro"), ("1", "1ro"),
                ("2°", "2do"), ("2o", "2do"), ("2do", "2do"), ("segundo", "2do"), ("2", "2do"),
                ("3°", "3ro"), ("3o", "3ro"), ("3ero", "3ro"), ("tercero", "3ro"), ("3", "3ro"),
                ("4°", "4to"), ("4o", "4to"), ("4to", "4to"), ("cuarto", "4to"), ("4", "4to"),
                ("5°", "5to"), ("5o", "5to"), ("5to", "5to"), ("quinto", "5to"), ("5", "5to"),
            ]
        )
    if cur.execute("SELECT COUNT(*) FROM alias_turnos").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO alias_turnos(alias,valor_real) VALUES(?,?)",
            [
                ("manana", "Mañana"), ("mañana", "Mañana"), ("m", "Mañana"), ("am", "Mañana"),
                ("mñ", "Mañana"), ("morning", "Mañana"), ("turno manana", "Mañana"),
                ("tarde", "Tarde"), ("t", "Tarde"), ("tm", "Tarde"), ("pm", "Tarde"),
                ("turno tarde", "Tarde"), ("afternoon", "Tarde"),
            ]
        )


# ============================================================
# MANTENIMIENTO
# ============================================================
def modo_mantenimiento():
    con = obtener_conexion()
    f = con.execute("SELECT valor FROM config WHERE clave='mantenimiento'").fetchone()
    return bool(f and f["valor"] == "1")


def activar_mantenimiento(usuario, mensaje=""):
    escribir("INSERT OR REPLACE INTO config(clave,valor) VALUES('mantenimiento','1')")
    escribir("INSERT OR REPLACE INTO config(clave,valor) VALUES('mantenimiento_msg',?)", (mensaje or "",))
    auditar(usuario["usuario"], "Activo modo mantenimiento: " + str(mensaje))


def desactivar_mantenimiento(usuario):
    escribir("INSERT OR REPLACE INTO config(clave,valor) VALUES('mantenimiento','0')")
    auditar(usuario["usuario"], "Desactivo modo mantenimiento")


def mensaje_mantenimiento():
    con = obtener_conexion()
    f = con.execute("SELECT valor FROM config WHERE clave='mantenimiento_msg'").fetchone()
    return f["valor"] if f else ""


def vista_mantenimiento():
    st.markdown("""
    <div style="text-align:center; margin-top:100px;">
        <h1 style="font-size:60px;">&#128295;</h1>
        <h1 style="font-size:32px;">Sistema en mantenimiento</h1>
        <p style="font-size:16px; opacity:0.7;">Estamos trabajando para mejorar el servicio.</p>
    </div>
    """, unsafe_allow_html=True)
    msg = mensaje_mantenimiento()
    if msg:
        st.info("Mensaje del Administrador: " + msg)
    if st.button("Cerrar sesion", width='stretch'):
        cerrar_sesion()
        st.rerun()


# ============================================================
# SESION
# ============================================================
def cerrar_sesion():
    usuario = st.session_state.get("user")
    if usuario:
        auditar(usuario["usuario"], "Logout")
    for k in list(st.session_state.keys()):
        del st.session_state[k]


# ============================================================
# AUTH
# ============================================================
def _bloqueado(u):
    if not u.get("bloqueado_hasta"):
        return False
    try:
        return ahora() < datetime.strptime(u["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False


def _ip():
    try:
        return st.context.headers.get("X-Forwarded-For", "local")
    except Exception:
        return "local"


def autenticar(nombre_usuario, password):
    con = obtener_conexion()
    nombre_usuario = (nombre_usuario or "").strip().lower()
    f = con.execute(
        "SELECT * FROM usuarios WHERE LOWER(usuario)=? AND activo=1",
        (nombre_usuario,)
    ).fetchone()
    if not f:
        return None, "Usuario no encontrado o inactivo"
    u = dict(f)
    if _bloqueado(u):
        lim = datetime.strptime(u["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S")
        return None, "Cuenta bloqueada. Intenta en " + str(int((lim - ahora()).total_seconds() / 60) + 1) + " min"
    if not verificar_password(password, u["password"]):
        if u["rol"] == "Admin":
            auditar(nombre_usuario, "Intento fallido de login (Admin no se bloquea)")
            return None, "Credenciales incorrectas."
        it = (u.get("intentos_fallidos") or 0) + 1
        if it >= MAX_INTENTOS:
            bh = (ahora() + timedelta(minutes=MIN_BLOQUEO)).strftime("%Y-%m-%d %H:%M:%S")
            escribir("UPDATE usuarios SET intentos_fallidos=0,bloqueado_hasta=? WHERE id=?", (bh, u["id"]))
            auditar(nombre_usuario, "Cuenta bloqueada por intentos fallidos")
            return None, "Cuenta bloqueada por %d min" % MIN_BLOQUEO
        escribir("UPDATE usuarios SET intentos_fallidos=? WHERE id=?", (it, u["id"]))
        return None, "Credenciales incorrectas. Quedan " + str(MAX_INTENTOS - it) + " intento(s)"
    escribir(
        "UPDATE usuarios SET intentos_fallidos=0,bloqueado_hasta=NULL,ultimo_login=?,ultimo_ip=? WHERE id=?",
        (timestamp_str(), _ip(), u["id"])
    )
    return u, ""


def auditar(usuario, accion, va=None, vn=None, tb=None, rid=None):
    try:
        escribir(
            "INSERT INTO auditoria(usuario,accion,fecha,valor_anterior,valor_nuevo,tabla_afectada,registro_id,ip) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (usuario, accion, timestamp_str(), va, vn, tb, rid, _ip())
        )
    except Exception as e:
        log.warning("audit: %s", e)


def _pedir_password_critica(clave, texto_boton="Confirmar", texto_input="Contrasena de Admin o TOECE"):
    pwd = st.text_input(texto_input, type="password", key="pwd_crit_" + clave)
    conf = st.checkbox("Confirmo esta accion", key="conf_crit_" + clave)
    if st.button(texto_boton, type="primary", key="btn_crit_" + clave, disabled=not conf):
        if not pwd:
            st.error("Ingresa la contrasena.")
            return False
        if not verificar_password_critica(pwd):
            st.error("Contrasena incorrecta.")
            return False
        return True
    return False


def _verificar_admin_activo():
    con = obtener_conexion()
    a = con.execute(
        "SELECT id FROM usuarios WHERE rol='Admin' AND es_principal=1 AND activo=1"
    ).fetchone()
    return a is not None


# ============================================================
# PERIODOS
# ============================================================
def obtener_periodo_activo():
    con = obtener_conexion()
    f = con.execute("SELECT * FROM periodos WHERE activo=1 LIMIT 1").fetchone()
    return dict(f) if f else None


def periodo_tiene_alumnos(pid):
    con = obtener_conexion()
    r = con.execute("SELECT COUNT(*) FROM alumnos WHERE periodo_id=? AND activo=1", (pid,)).fetchone()
    return (r[0] or 0) > 0


def sistema_bloqueado():
    p = obtener_periodo_activo()
    return not p or not periodo_tiene_alumnos(p["id"])


@st.cache_data(ttl=60)
def listar_periodos():
    return pd.read_sql(
        "SELECT id,nombre,fecha_inicio,fecha_fin,activo,cerrado FROM periodos ORDER BY id DESC",
        obtener_conexion()
    )


@st.cache_data(ttl=60)
def listar_periodos_cerrados():
    return pd.read_sql(
        "SELECT id,nombre,fecha_inicio,fecha_fin,fecha_cierre FROM periodos WHERE cerrado=1 ORDER BY id DESC",
        obtener_conexion()
    )


def crear_periodo(nombre, fi, ff, usuario):
    con = obtener_conexion()
    try:
        with _lock_escritura:
            cur = con.execute(
                "INSERT INTO periodos(nombre,fecha_inicio,fecha_fin,activo,cerrado) VALUES(?,?,?,1,0)",
                (nombre, fi, ff)
            )
            idn = cur.lastrowid
            con.execute("UPDATE periodos SET activo=0 WHERE id!=?", (idn,))
            con.commit()
        auditar(usuario["usuario"], "Creo periodo " + nombre, tb="periodos", rid=idn)
        listar_periodos.clear()
        return True, "Periodo " + nombre + " creado y activado."
    except sqlite3.Error as e:
        return False, "Error: " + str(e)


def activar_periodo(idp, usuario):
    con = obtener_conexion()
    f = con.execute("SELECT cerrado FROM periodos WHERE id=?", (idp,)).fetchone()
    if not f:
        return False, "Periodo no encontrado."
    if f["cerrado"]:
        return False, "Ese periodo esta cerrado."
    with _lock_escritura:
        con.execute("UPDATE periodos SET activo=0")
        con.execute("UPDATE periodos SET activo=1 WHERE id=?", (idp,))
        con.commit()
    auditar(usuario["usuario"], "Activo periodo id=" + str(idp), tb="periodos", rid=idp)
    listar_periodos.clear()
    return True, "Periodo activado."


# ============================================================
# VENTANAS
# ============================================================
@st.cache_data(ttl=60)
def listar_turnos():
    con = obtener_conexion()
    return [dict(f) for f in con.execute("SELECT * FROM turnos ORDER BY id").fetchall()]


@st.cache_data(ttl=60)
def listar_ventanas(id_turno=None):
    con = obtener_conexion()
    if id_turno:
        return [dict(f) for f in con.execute(
            "SELECT * FROM ventanas WHERE turno_id=? AND activo=1 ORDER BY orden,id",
            (id_turno,)
        ).fetchall()]
    return [dict(f) for f in con.execute(
        "SELECT * FROM ventanas WHERE activo=1 ORDER BY turno_id,orden"
    ).fetchall()]


def dia_especial_hoy(id_turno, fecha, id_seccion=None):
    con = obtener_conexion()
    f = con.execute(
        "SELECT * FROM dias_especiales WHERE fecha=? AND activo=1 AND (turno_id=? OR turno_id IS NULL) "
        "ORDER BY turno_id DESC LIMIT 1",
        (fecha, id_turno)
    ).fetchone()
    if not f:
        return None
    dia = dict(f)
    if id_seccion:
        secs = con.execute(
            "SELECT seccion_id FROM dias_especiales_secciones WHERE dia_especial_id=?",
            (dia["id"],)
        ).fetchall()
        if secs and id_seccion not in [s["seccion_id"] for s in secs]:
            return None
    return dia


def ventana_activa_para_alumno(id_turno, fecha, id_seccion=None):
    dia = dia_especial_hoy(id_turno, fecha, id_seccion)
    if dia and dia["tipo"] == "feriado":
        return None
    h = hora_corta()
    for v in listar_ventanas(id_turno):
        ap = v["hora_apertura"]
        if v["tipo"] == VENT_CLASES and dia and dia["tipo"] == "evento":
            ap = dia["hora_entrada"]
        lim = v["hora_limite_puntual"] or ap
        if ap <= h <= v["hora_cierre"]:
            return {
                **v,
                "hora_apertura_efectiva": ap,
                "hora_limite_efectiva": lim,
                "es_evento": bool(dia and dia["tipo"] == "evento" and v["tipo"] == VENT_CLASES)
            }
    return None


# ============================================================
# ALUMNOS
# ============================================================
@st.cache_data(ttl=60)
def listar_grados():
    con = obtener_conexion()
    return [dict(f) for f in con.execute("SELECT * FROM grados ORDER BY nombre").fetchall()]


@st.cache_data(ttl=60)
def secciones_por_grado(idg):
    con = obtener_conexion()
    return [dict(f) for f in con.execute(
        "SELECT * FROM secciones WHERE grado_id=? ORDER BY nombre", (idg,)
    ).fetchall()]


@st.cache_data(ttl=60)
def secciones_por_turno(idt):
    con = obtener_conexion()
    return [dict(f) for f in con.execute(
        "SELECT s.*,g.nombre AS grado FROM secciones s JOIN grados g ON s.grado_id=g.id "
        "WHERE s.turno_id=? ORDER BY g.nombre,s.nombre", (idt,)
    ).fetchall()]


def alumnos_de_seccion(idsec):
    con = obtener_conexion()
    return pd.read_sql(
        "SELECT a.id,a.dni,a.nombres,a.apellido_paterno,a.apellido_materno,"
        "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS nombre_completo "
        "FROM alumnos a WHERE a.seccion_id=? AND a.activo=1 "
        "ORDER BY a.apellido_paterno,a.apellido_materno,a.nombres",
        con, params=[idsec]
    )


def buscar_alumnos(texto, idg=None, idsec=None, limite=200):
    con = obtener_conexion()
    q = ("SELECT a.id,a.dni,a.nombres,a.apellido_paterno,a.apellido_materno,"
         "g.id AS grado_id,g.nombre AS grado,s.id AS seccion_id,s.nombre AS seccion,"
         "t.nombre AS turno,"
         "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS nombre_completo "
         "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
         "JOIN turnos t ON s.turno_id=t.id WHERE a.activo=1")
    p = []
    if texto:
        for w in [x.strip() for x in texto.split() if x.strip()]:
            q += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
            pat = "%" + w + "%"
            p += [pat, pat, pat]
    if idg:
        q += " AND g.id=?"
        p.append(idg)
    if idsec:
        q += " AND s.id=?"
        p.append(idsec)
    q += " ORDER BY a.apellido_paterno LIMIT ?"
    p.append(limite)
    return pd.read_sql(q, con, params=p)


def _buscar_alumno_por_dni(con, dni):
    f = con.execute(
        "SELECT a.id,a.dni,a.nombres,a.apellido_paterno,a.apellido_materno,"
        "s.id AS seccion_id,s.nombre AS seccion,g.nombre AS grado,t.id AS turno_id,t.nombre AS turno "
        "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
        "JOIN turnos t ON s.turno_id=t.id WHERE a.dni=? AND a.activo=1",
        (dni,)
    ).fetchone()
    return dict(f) if f else None


def _nombre_completo(a):
    return (a['apellido_paterno'] + " " + (a['apellido_materno'] or "") + ", " + a['nombres']).strip(", ")


def crear_alumno(dni, nombres, ap, am, idsec, apo_n, apo_t, usuario):
    con = obtener_conexion()
    per = obtener_periodo_activo()
    if not per:
        return False, "No hay periodo activo."
    try:
        with _lock_escritura:
            cur = con.cursor()
            ida = None
            if apo_n:
                f = cur.execute(
                    "SELECT id FROM apoderados WHERE nombre=? AND COALESCE(telefono,'')=?",
                    (apo_n, apo_t or "")
                ).fetchone()
                ida = f["id"] if f else cur.execute(
                    "INSERT INTO apoderados(nombre,telefono) VALUES(?,?)",
                    (apo_n, apo_t or None)
                ).lastrowid
            cur.execute(
                "INSERT INTO alumnos(dni,nombres,apellido_paterno,apellido_materno,seccion_id,"
                "apoderado_id,nombre_apoderado,telefono_apoderado,periodo_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (dni, nombres, ap, am or None, idsec, ida, apo_n or None, apo_t or None, per["id"])
            )
            con.commit()
        auditar(usuario["usuario"], "Creo alumno DNI " + dni, tb="alumnos")
        return True, "Alumno " + nombres + " creado."
    except sqlite3.IntegrityError:
        return False, "Ya existe un alumno con DNI " + dni
    except sqlite3.Error as e:
        log.error("crear alumno: %s", e)
        return False, "Error al crear el alumno."


def editar_alumno(idal, apo_n, apo_t, idsec, dni, usuario):
    con = obtener_conexion()
    ida = None
    with _lock_escritura:
        cur = con.cursor()
        if apo_n:
            f = cur.execute(
                "SELECT id FROM apoderados WHERE nombre=? AND COALESCE(telefono,'')=?",
                (apo_n, apo_t or "")
            ).fetchone()
            ida = f["id"] if f else cur.execute(
                "INSERT INTO apoderados(nombre,telefono) VALUES(?,?)",
                (apo_n, apo_t or None)
            ).lastrowid
        con.execute(
            "UPDATE alumnos SET apoderado_id=?,nombre_apoderado=?,telefono_apoderado=?,seccion_id=? WHERE id=?",
            (ida, apo_n or None, apo_t or None, idsec, idal)
        )
        con.commit()
    auditar(usuario["usuario"], "Edito alumno " + dni, tb="alumnos", rid=idal)
    return True, "Alumno editado."


def retirar_alumno(idal, dni, usuario):
    escribir("UPDATE alumnos SET activo=0,retirado_en=? WHERE id=?", (timestamp_str(), idal))
    auditar(usuario["usuario"], "Desactivo alumno DNI " + dni, tb="alumnos", rid=idal)
    return True, "Alumno desactivado."


def reactivar_alumno(idal, dni, usuario):
    escribir("UPDATE alumnos SET activo=1,retirado_en=NULL WHERE id=?", (idal,))
    auditar(usuario["usuario"], "Reactivo alumno DNI " + dni, tb="alumnos", rid=idal)
    return True, "Alumno reactivado."


# ============================================================
# IMPORT EXCEL
# ============================================================
def _normalizar_grado(n):
    n = (n or "").strip()
    con = obtener_conexion()
    f = con.execute("SELECT valor_real FROM alias_grados WHERE LOWER(alias)=?", (n.lower(),)).fetchone()
    if f:
        return f["valor_real"]
    return n.title()


def _normalizar_turno(t):
    t = (t or "").strip()
    con = obtener_conexion()
    f = con.execute("SELECT valor_real FROM alias_turnos WHERE LOWER(alias)=?", (t.lower(),)).fetchone()
    if f:
        return f["valor_real"]
    return t.title()


def validar_importacion(df, mapeo):
    errs = []
    val = []
    con = obtener_conexion()
    vistos = {}

    def _limpiar(v):
        if v is None:
            return ""
        if isinstance(v, float):
            if pd.isna(v):
                return ""
            if v == int(v):
                return str(int(v))
            return str(v)
        if isinstance(v, int):
            return str(v)
        s = str(v).strip()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        if s.lower() == "nan":
            return ""
        return s

    for idx, fila in df.iterrows():
        nf = idx + 2
        try:
            dni = _limpiar(fila[mapeo["dni"]])
            nom = _limpiar(fila[mapeo["nombres"]])
            ap = _limpiar(fila[mapeo["apellido_paterno"]])
            am = _limpiar(fila[mapeo["apellido_materno"]]) if mapeo.get("apellido_materno") else ""
            gr = _normalizar_grado(_limpiar(fila[mapeo["grado"]]))
            sec = _limpiar(fila[mapeo["seccion"]]).upper()
            tur = _normalizar_turno(_limpiar(fila[mapeo["turno"]]))
            an = _limpiar(fila[mapeo["apoderado_nombre"]]) if mapeo.get("apoderado_nombre") else ""
            at = _limpiar(fila[mapeo["apoderado_telefono"]]) if mapeo.get("apoderado_telefono") else ""

            if not dni:
                errs.append({"fila": nf, "motivo": "DNI vacio"})
                continue
            if not re.fullmatch(r"\d{8}", dni):
                errs.append({"fila": nf, "motivo": "DNI invalido '" + dni + "'"})
                continue
            if dni in vistos:
                errs.append({"fila": nf, "motivo": "DNI " + dni + " duplicado"})
                continue
            if not nom or not ap or not gr or not sec:
                errs.append({"fila": nf, "motivo": "Faltan campos"})
                continue
            if not con.execute("SELECT id FROM grados WHERE nombre=?", (gr,)).fetchone():
                errs.append({"fila": nf, "motivo": "Grado '" + gr + "' no existe"})
                continue
            if tur not in ("Mañana", "Tarde"):
                errs.append({"fila": nf, "motivo": "Turno '" + tur + "'"})
                continue

            vistos[dni] = nf
            val.append({
                "dni": dni, "nombres": nom,
                "apellido_paterno": ap, "apellido_materno": am,
                "grado": gr, "seccion": sec, "turno": tur,
                "apoderado_nombre": an, "apoderado_telefono": at
            })
        except (KeyError, ValueError, TypeError) as e:
            errs.append({"fila": nf, "motivo": "Error: " + str(e)})

    return val, errs, {"total": len(df), "validas": len(val), "errores": len(errs)}


def insertar_alumnos_validos(val):
    con = obtener_conexion()
    cur = con.cursor()
    mapa_t = {f["nombre"]: f["id"] for f in cur.execute("SELECT id,nombre FROM turnos").fetchall()}
    per = obtener_periodo_activo()
    if not per:
        return 0, 0, ["No hay periodo activo."]
    pid = per["id"]
    ins = 0
    reac = 0
    errs = []
    with _lock_escritura:
        for i, d in enumerate(val):
            try:
                fg = cur.execute("SELECT id FROM grados WHERE nombre=?", (d["grado"],)).fetchone()
                if not fg:
                    errs.append("Fila " + str(i + 1) + ": grado no reconocido")
                    continue
                it = mapa_t.get(d["turno"])
                if not it:
                    errs.append("Fila " + str(i + 1) + ": turno no encontrado")
                    continue
                fs = cur.execute(
                    "SELECT id FROM secciones WHERE nombre=? AND grado_id=? AND turno_id=?",
                    (d["seccion"], fg["id"], it)
                ).fetchone()
                idsec = fs["id"] if fs else cur.execute(
                    "INSERT INTO secciones(nombre,grado_id,turno_id) VALUES(?,?,?)",
                    (d["seccion"], fg["id"], it)
                ).lastrowid
                ida = None
                if d["apoderado_nombre"]:
                    fa = cur.execute(
                        "SELECT id FROM apoderados WHERE nombre=? AND COALESCE(telefono,'')=?",
                        (d["apoderado_nombre"], d["apoderado_telefono"] or "")
                    ).fetchone()
                    ida = fa["id"] if fa else cur.execute(
                        "INSERT INTO apoderados(nombre,telefono) VALUES(?,?)",
                        (d["apoderado_nombre"], d["apoderado_telefono"] or None)
                    ).lastrowid
                ex = cur.execute("SELECT id FROM alumnos WHERE dni=?", (d["dni"],)).fetchone()
                if ex:
                    cur.execute(
                        "UPDATE alumnos SET nombres=?,apellido_paterno=?,apellido_materno=?,"
                        "seccion_id=?,apoderado_id=?,nombre_apoderado=?,telefono_apoderado=?,"
                        "periodo_id=?,activo=1,retirado_en=NULL WHERE id=?",
                        (d["nombres"], d["apellido_paterno"], d["apellido_materno"], idsec, ida,
                         d["apoderado_nombre"] or None, d["apoderado_telefono"] or None, pid, ex["id"])
                    )
                    reac += 1
                else:
                    cur.execute(
                        "INSERT INTO alumnos(dni,nombres,apellido_paterno,apellido_materno,seccion_id,"
                        "apoderado_id,nombre_apoderado,telefono_apoderado,periodo_id,activo) "
                        "VALUES(?,?,?,?,?,?,?,?,?,1)",
                        (d["dni"], d["nombres"], d["apellido_paterno"], d["apellido_materno"], idsec, ida,
                         d["apoderado_nombre"] or None, d["apoderado_telefono"] or None, pid)
                    )
                    ins += 1
            except sqlite3.Error as e:
                errs.append("Fila " + str(i + 1) + ": " + str(e))
        con.commit()
    return ins, reac, errs


# ============================================================
# BLOQUEOS
# ============================================================
def alumno_bloqueado(idal):
    con = obtener_conexion()
    f = con.execute(
        "SELECT * FROM bloqueos WHERE alumno_id=? AND activo=1 ORDER BY id DESC LIMIT 1",
        (idal,)
    ).fetchone()
    return dict(f) if f else None


def crear_bloqueo(idal, motivo, usuario, origen="automatico"):
    escribir(
        "INSERT INTO bloqueos(alumno_id,motivo,activo,fecha_inicio,creado_por,origen) "
        "VALUES(?,?,1,?,?,?)",
        (idal, motivo, hoy_str(), usuario["usuario"], origen)
    )
    auditar(usuario["usuario"], "Bloqueo " + origen + " alumno_id=" + str(idal), tb="bloqueos", rid=idal)


def liberar_bloqueo(idal, usuario, obs=""):
    escribir(
        "UPDATE bloqueos SET activo=0,fecha_fin=?,liberado_por=? WHERE alumno_id=? AND activo=1",
        (timestamp_str(), usuario["usuario"], idal)
    )
    auditar(usuario["usuario"], "Libero bloqueo alumno_id=" + str(idal) + ". Obs: " + str(obs), tb="bloqueos", rid=idal)


def historial_bloqueos_alumno(alumno_id):
    con = obtener_conexion()
    return pd.read_sql(
        "SELECT id, motivo, activo, fecha_inicio, fecha_fin, creado_por, "
        "origen, liberado_por, desbloqueado_en, desbloqueado_por, motivo_desbloqueo "
        "FROM bloqueos WHERE alumno_id=? ORDER BY id DESC",
        con, params=[alumno_id]
    )


def desbloquear_alumno_completo(alumno_id, motivo, usuario, acta_id=None):
    con = obtener_conexion()
    with _lock_escritura:
        con.execute(
            "UPDATE bloqueos SET activo=0, fecha_fin=?, liberado_por=?, "
            "desbloqueado_en=?, desbloqueado_por=?, motivo_desbloqueo=?, acta_id=? "
            "WHERE alumno_id=? AND activo=1",
            (timestamp_str(), usuario["usuario"], timestamp_str(),
             usuario["usuario"], motivo or "", acta_id, alumno_id)
        )
        con.execute(
            "INSERT OR REPLACE INTO config(clave,valor) VALUES(?,?)",
            ("reset_tardanzas_" + str(alumno_id), timestamp_str())
        )
        con.commit()
    auditar(usuario["usuario"], "Desbloqueo alumno_id=" + str(alumno_id) + " motivo: " + motivo,
            tb="bloqueos", rid=alumno_id)
    return True, "Alumno desbloqueado. Debe llegar temprano manana."


# ============================================================
# JUSTIFICACIONES Y PERMISOS
# ============================================================
def contar_tardanzas_injustificadas(idal, pid=None):
    con = obtener_conexion()
    q = "SELECT COUNT(*) FROM tardanzas WHERE alumno_id=? AND justificada=0"
    p = [idal]
    if pid is not None:
        q += " AND periodo_id=?"
        p.append(pid)
    r = con.execute(q, p).fetchone()
    return r[0] or 0


def contar_tardanzas_desde_reset(alumno_id, pid=None):
    con = obtener_conexion()
    f = con.execute("SELECT valor FROM config WHERE clave=?",
                    ("reset_tardanzas_" + str(alumno_id),)).fetchone()
    desde = f["valor"] if f else None
    q = "SELECT COUNT(*) FROM tardanzas WHERE alumno_id=? AND justificada=0"
    p = [alumno_id]
    if desde:
        q += " AND timestamp > ?"
        p.append(desde)
    if pid is not None:
        q += " AND periodo_id=?"
        p.append(pid)
    r = con.execute(q, p).fetchone()
    return r[0] or 0


def _aplicar_just_prev(con, idal, fecha, tipo):
    f = con.execute(
        "SELECT id FROM justificaciones_previas WHERE alumno_id=? AND fecha_objetivo=? AND tipo=? AND aplicada=0",
        (idal, fecha, tipo)
    ).fetchone()
    if f:
        con.execute("UPDATE justificaciones_previas SET aplicada=1 WHERE id=?", (f["id"],))
        return True
    return False


def hay_permiso_activo(idal, fecha):
    con = obtener_conexion()
    f = con.execute(
        "SELECT id FROM permisos WHERE alumno_id=? AND activo=1 AND fecha_inicio<=? AND fecha_fin>=? LIMIT 1",
        (idal, fecha, fecha)
    ).fetchone()
    return bool(f)


def _puede_justificar(fecha_objetivo_str):
    try:
        f_obj = datetime.strptime(fecha_objetivo_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False, "Fecha invalida."
    hoy = ahora().date()
    diff = (hoy - f_obj).days
    if diff < -1:
        return False, "Solo se puede justificar hoy o manana."
    if diff > 1:
        return False, "Pasaron mas de 24h. Ya no se puede justificar."
    return True, ""


def crear_justificacion_previa(idal, fecha_obj, tipo, motivo, usuario):
    ok, msg = _puede_justificar(fecha_obj)
    if not ok:
        return False, msg
    if not motivo or not motivo.strip():
        return False, "El motivo es obligatorio."
    con = obtener_conexion()
    ex = con.execute(
        "SELECT id FROM justificaciones_previas WHERE alumno_id=? AND fecha_objetivo=?",
        (idal, fecha_obj)
    ).fetchone()
    if ex:
        return False, "Ya existe una justificacion para ese alumno en esa fecha."
    escribir(
        "INSERT INTO justificaciones_previas(alumno_id,fecha_objetivo,tipo,motivo,creado_por,timestamp,aplicada) "
        "VALUES(?,?,?,?,?,?,0)",
        (idal, fecha_obj, tipo, motivo.strip(), usuario["usuario"], timestamp_str())
    )
    auditar(usuario["usuario"], "Creo justificacion previa " + tipo + " " + fecha_obj + " id=" + str(idal),
            tb="justificaciones_previas", rid=idal)
    return True, "Justificacion registrada."


def crear_permiso(idal, fi, ff, motivo, usuario):
    if ff < fi:
        return False, "La fecha fin no puede ser anterior a la fecha inicio."
    if not motivo or not motivo.strip():
        return False, "El motivo es obligatorio."
    dias = (ff - fi).days + 1
    if dias > MAX_DIAS_PERMISO:
        return False, "El permiso no puede exceder " + str(MAX_DIAS_PERMISO) + " dias."
    per = obtener_periodo_activo()
    pid = per["id"] if per else None
    escribir(
        "INSERT INTO permisos(alumno_id,fecha_inicio,fecha_fin,motivo,creado_por,timestamp,activo,periodo_id) "
        "VALUES(?,?,?,?,?,?,1,?)",
        (idal, fi, ff, motivo.strip(), usuario["usuario"], timestamp_str(), pid)
    )
    auditar(usuario["usuario"], "Creo permiso " + fi + " a " + ff + " id=" + str(idal),
            tb="permisos", rid=idal)
    return True, "Permiso registrado."


def listar_permisos(solo_activos=True):
    con = obtener_conexion()
    q = ("SELECT p.id,p.fecha_inicio,p.fecha_fin,COALESCE(p.motivo,'') AS motivo,p.activo,"
         "p.creado_por,p.timestamp,a.dni,"
         "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,"
         "a.nombres,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno "
         "FROM permisos p JOIN alumnos a ON p.alumno_id=a.id "
         "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
         "JOIN turnos t ON s.turno_id=t.id")
    if solo_activos:
        q += " WHERE p.activo=1"
    q += " ORDER BY p.fecha_inicio DESC"
    return pd.read_sql(q, con)


# ============================================================
# ASISTENCIA
# ============================================================
def registrar_entrada(dni, usuario, origen="qr", modo_codigo="clases",
                       tipo_incidencia_id=None, descripcion_breve=None):
    dni = (dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni):
        return False, "ERROR", "DNI invalido", {}
    con = obtener_conexion()
    al = _buscar_alumno_por_dni(con, dni)
    if not al:
        return False, "ERROR", "DNI no encontrado", {}
    bloq = alumno_bloqueado(al["id"])
    if bloq:
        auditar(usuario["usuario"], "Intento escaneo bloqueado DNI " + dni, tb="bloqueos", rid=al["id"])
        return False, "BLOQUEADO", _nombre_completo(al) + " | BLOQUEADO - retener y llevar a TOECE", {"alumno": al, "motivo": bloq["motivo"]}

    if modo_codigo == "incidencia":
        per = obtener_periodo_activo()
        pid = per["id"] if per else None
        ok, msg, inc_id = crear_incidencia_reportada(
            al["id"], tipo_incidencia_id, descripcion_breve, usuario, pid
        )
        if ok:
            return True, "INCIDENCIA", _nombre_completo(al) + " | Incidencia reportada a TOECE", {"alumno": al, "incidencia_id": inc_id}
        else:
            return False, "ERROR", msg, {"alumno": al}

    fecha = hoy_str()
    ha = hora_corta()
    hc = hora_str()
    per = obtener_periodo_activo()
    pid = per["id"] if per else None
    dia = dia_especial_hoy(al["turno_id"], fecha, al["seccion_id"])
    if dia and dia["tipo"] == "feriado":
        return False, "ERROR", "Hoy es feriado, no se registra", {}
    v = ventana_activa_para_alumno(al["turno_id"], fecha, al["seccion_id"])
    if not v:
        return False, "ERROR", "Sin ventana activa (" + ha + ")", {}

    permiso = hay_permiso_activo(al["id"], fecha)
    tipo_real = v["tipo"]
    if v.get("es_evento") and v["tipo"] == VENT_CLASES:
        tipo_real = TIPO_ASIST_EVENTO

    with _lock_escritura:
        ex = con.execute(
            "SELECT id,estado FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo=?",
            (al["id"], fecha, tipo_real)
        ).fetchone()
        if ex:
            return False, "ERROR", _nombre_completo(al) + " ya registro " + tipo_real + " hoy (" + ex["estado"] + ")", {}

        if v["tipo"] == VENT_REF:
            try:
                con.execute(
                    "INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,origen,periodo_id) "
                    "VALUES(?,?,?,'reforzamiento',?,?,?,?,?)",
                    (al["id"], fecha, v["id"], hc, REF_ASISTIO, 1 if permiso else 0, origen, pid)
                )
                if al["turno"] == "Tarde":
                    ya = con.execute(
                        "SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo='clases'",
                        (al["id"], fecha)
                    ).fetchone()
                    if not ya:
                        con.execute(
                            "INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,origen,periodo_id) "
                            "VALUES(?,?,NULL,'clases',?,'Puntual',?,?,?)",
                            (al["id"], fecha, hc, 1 if permiso else 0, origen, pid)
                        )
                con.commit()
            except sqlite3.IntegrityError:
                con.rollback()
                return False, "ERROR", _nombre_completo(al) + " ya registro reforzamiento hoy", {}
            auditar(usuario["usuario"], "Reforzamiento " + origen + " DNI " + dni, tb="asistencias")
            msg = (_nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] + " | Reforzamiento + Clases Puntual " + ha
                   if al["turno"] == "Tarde" else
                   _nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] + " | Asistio a reforzamiento " + ha)
            return True, "REFORZAMIENTO", msg, {"alumno": al}

        if al["turno"] == "Tarde":
            ya = con.execute(
                "SELECT id,estado,hora FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo='clases'",
                (al["id"], fecha)
            ).fetchone()
            if ya:
                return False, "ERROR", _nombre_completo(al) + " ya tiene clases hoy (" + ya["estado"] + " " + (ya["hora"] or "") + ").", {}

        lim = v["hora_limite_efectiva"]
        est = PUNTUAL if ha <= lim else TARDANZA

        if est == TARDANZA:
            jp = _aplicar_just_prev(con, al["id"], fecha, "Tardanza")
        else:
            jp = False
        just_final = 1 if (jp or permiso) else 0

        try:
            con.execute(
                "INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,origen,periodo_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (al["id"], fecha, v["id"], tipo_real, hc, est, just_final, origen, pid)
            )
        except sqlite3.IntegrityError:
            con.rollback()
            return False, "ERROR", _nombre_completo(al) + " ya registro hoy (carrera)", {}

        n = 0
        acc = None
        if est == TARDANZA:
            n = contar_tardanzas_desde_reset(al["id"], pid) + 1
            acc = ACC_PERDONADO if n <= 2 else (ACC_DERIVADO if n == 3 else ACC_RETENIDO)
            try:
                con.execute(
                    "INSERT INTO tardanzas(alumno_id,fecha,hora,numero,accion,justificada,origen,"
                    "registrado_por,timestamp,periodo_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (al["id"], fecha, hc, n, acc, just_final, origen,
                     usuario["usuario"], timestamp_str(), pid)
                )
            except sqlite3.IntegrityError:
                log.warning("tardanza duplicada alumno_id=%s fecha=%s", al["id"], fecha)
            except sqlite3.Error as e:
                log.error("error al insertar tardanza: %s", e)
        con.commit()

    if est == TARDANZA:
        if n >= 4 and not alumno_bloqueado(al["id"]):
            crear_bloqueo(al["id"], str(n) + "ta tardanza injustificada (" + fecha + ")", usuario, "automatico")
        auditar(usuario["usuario"], "Tardanza " + str(n) + "a DNI " + dni + " -> " + acc, tb="tardanzas")
        sufijo = " [JUSTIFICADA]" if just_final else ""
        return True, "TARDANZA", _nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] + " | Tardanza " + str(n) + "a (" + acc + ")" + sufijo + " " + ha, {"alumno": al, "numero": n, "accion": acc}

    auditar(usuario["usuario"], "Entrada Puntual " + origen + " DNI " + dni, tb="asistencias")
    return True, "PUNTUAL", _nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] + " | " + tipo_real.upper() + " Puntual " + ha, {"alumno": al}


def marcar_faltas_al_cierre():
    fecha = hoy_str()
    ha = hora_corta()
    con = obtener_conexion()
    per = obtener_periodo_activo()
    pid = per["id"] if per else None
    for t in listar_turnos():
        if es_fin_de_semana():
            if not con.execute(
                "SELECT tipo FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento'",
                (fecha,)
            ).fetchone():
                continue
        for v in listar_ventanas(t["id"]):
            if ha < v["hora_cierre"]:
                continue
            tipo = "clases" if v["tipo"] == VENT_CLASES else "reforzamiento"
            est = "Falta" if v["tipo"] == VENT_CLASES else "No asistio"
            for al in con.execute(
                "SELECT a.id FROM alumnos a JOIN secciones s ON a.seccion_id=s.id "
                "WHERE s.turno_id=? AND a.activo=1", (t["id"],)
            ).fetchall():
                if con.execute(
                    "SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo=?",
                    (al["id"], fecha, tipo)
                ).fetchone():
                    continue
                if hay_permiso_activo(al["id"], fecha):
                    escribir(
                        "INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,"
                        "observacion,origen,periodo_id) VALUES(?,?,?,?,?,?,1,?,?,?)",
                        (al["id"], fecha, v["id"], tipo, hora_str(), PERMISO,
                         "Permiso otorgado", "manual", pid)
                    )
                    continue
                jp = False
                if v["tipo"] == VENT_CLASES:
                    jp = _aplicar_just_prev(con, al["id"], fecha, "Falta")
                escribir(
                    "INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,origen,periodo_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (al["id"], fecha, v["id"], tipo, hora_str(), est, 1 if jp else 0, "auto", pid)
                )


def justificar_asistencia(ida, obs, usuario):
    con = obtener_conexion()
    reg = con.execute("SELECT * FROM asistencias WHERE id=?", (ida,)).fetchone()
    if not reg:
        return False, "Registro no encontrado."
    if reg["estado"] in (PUNTUAL, PERMISO, REF_ASISTIO, REF_NO_ASISTIO):
        return False, "Solo se pueden justificar Faltas o Tardanzas."
    if reg["justificada"]:
        return False, "Esta asistencia ya esta justificada."
    ok, msg = _puede_justificar(reg["fecha"])
    if not ok:
        return False, msg
    if not obs or not obs.strip():
        return False, "La observacion es obligatoria."
    escribir(
        "UPDATE asistencias SET justificada=1,observacion=?,justificado_por=?,justificado_en=? WHERE id=?",
        (obs.strip(), usuario["usuario"], timestamp_str(), ida)
    )
    auditar(usuario["usuario"], "Justifico asistencia id=" + str(ida) + " motivo: " + obs,
            tb="asistencias", rid=ida)
    return True, "Asistencia justificada."


def quitar_justificacion(ida, usuario):
    con = obtener_conexion()
    reg = con.execute("SELECT * FROM asistencias WHERE id=?", (ida,)).fetchone()
    if not reg:
        return False, "Registro no encontrado."
    escribir(
        "UPDATE asistencias SET justificada=0,observacion=NULL,justificado_por=NULL,justificado_en=NULL WHERE id=?",
        (ida,)
    )
    auditar(usuario["usuario"], "Quito justificacion id=" + str(ida), tb="asistencias", rid=ida)
    return True, "Justificacion eliminada."


# ============================================================
# INCIDENCIAS
# ============================================================
def crear_incidencia_reportada(alumno_id, tipo_id, descripcion_breve, usuario, periodo_id=None):
    con = obtener_conexion()
    fecha = hoy_str()
    hora = hora_str()
    with _lock_escritura:
        ex = con.execute(
            "SELECT id FROM incidencias WHERE alumno_id=? AND fecha=? "
            "AND COALESCE(tipo_id,0)=COALESCE(?,0) AND estado='reportada'",
            (alumno_id, fecha, tipo_id)
        ).fetchone()
        if ex:
            return False, "Ya existe un reporte de incidencia de este alumno hoy con ese tipo.", ex["id"]
        cur = con.execute(
            "INSERT INTO incidencias(alumno_id,fecha,hora,tipo_id,descripcion_breve,estado,"
            "reportado_por,creado_en,periodo_id) VALUES(?,?,?,?,?,'reportada',?,?,?)",
            (alumno_id, fecha, hora, tipo_id, descripcion_breve or None,
             usuario["usuario"], timestamp_str(), periodo_id)
        )
        inc_id = cur.lastrowid
        con.commit()
    auditar(usuario["usuario"], "Reporto incidencia id=" + str(inc_id) + " alumno_id=" + str(alumno_id),
            tb="incidencias", rid=inc_id)
    return True, "Incidencia reportada a TOECE.", inc_id


def obtener_incidencia(id_inc):
    con = obtener_conexion()
    f = con.execute(
        "SELECT i.*, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno, "
        "a.seccion_id, a.nombre_apoderado, a.telefono_apoderado, "
        "g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
        "ti.nombre AS tipo_nombre, li.nombre AS lugar_nombre "
        "FROM incidencias i "
        "JOIN alumnos a ON i.alumno_id=a.id "
        "JOIN secciones s ON a.seccion_id=s.id "
        "JOIN grados g ON s.grado_id=g.id "
        "JOIN turnos t ON s.turno_id=t.id "
        "LEFT JOIN tipos_incidencia ti ON i.tipo_id=ti.id "
        "LEFT JOIN lugares_incidencia li ON i.lugar_id=li.id "
        "WHERE i.id=?", (id_inc,)
    ).fetchone()
    return dict(f) if f else None


def listar_incidencias_por_estado(estados, limite=200):
    con = obtener_conexion()
    ph = ",".join(["?"] * len(estados))
    q = ("SELECT i.id, i.fecha, i.hora, i.estado, i.descripcion_breve, "
         "i.reportado_por, i.creado_por, i.creado_en, "
         "a.dni, a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos, "
         "a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
         "ti.nombre AS tipo_nombre, li.nombre AS lugar_nombre "
         "FROM incidencias i "
         "JOIN alumnos a ON i.alumno_id=a.id "
         "JOIN secciones s ON a.seccion_id=s.id "
         "JOIN grados g ON s.grado_id=g.id "
         "JOIN turnos t ON s.turno_id=t.id "
         "LEFT JOIN tipos_incidencia ti ON i.tipo_id=ti.id "
         "LEFT JOIN lugares_incidencia li ON i.lugar_id=li.id "
         "WHERE i.estado IN (" + ph + ") "
         "ORDER BY i.fecha DESC, i.hora DESC LIMIT ?")
    params = list(estados) + [limite]
    return pd.read_sql(q, con, params=params)


def contar_incidencias_nuevas(usuario):
    if usuario["rol"] not in ("TOECE", "Admin"):
        return 0
    con = obtener_conexion()
    f = con.execute("SELECT ultima_lectura_notif FROM usuarios WHERE id=?", (usuario["id"],)).fetchone()
    ult = f["ultima_lectura_notif"] if f and f["ultima_lectura_notif"] else "1970-01-01 00:00:00"
    r = con.execute(
        "SELECT COUNT(*) FROM incidencias WHERE estado='reportada' AND creado_en > ?", (ult,)
    ).fetchone()
    return r[0] or 0


def marcar_notif_leidas(usuario):
    escribir("UPDATE usuarios SET ultima_lectura_notif=? WHERE id=?",
             (timestamp_str(), usuario["id"]))


def guardar_seguimiento_incidencia(inc_id, tipo, detalle, usuario):
    if tipo not in ("acuerdo", "intervencion_docente", "observacion", "cierre"):
        return False, "Tipo invalido."
    if not detalle or not detalle.strip():
        return False, "El detalle es obligatorio."
    escribir(
        "INSERT INTO incidencias_seguimiento(incidencia_id,tipo,detalle,registrado_por,timestamp) "
        "VALUES(?,?,?,?,?)",
        (inc_id, tipo, detalle.strip(), usuario["usuario"], timestamp_str())
    )
    auditar(usuario["usuario"], "Seguimiento incidencia id=" + str(inc_id) + " tipo=" + tipo,
            tb="incidencias_seguimiento", rid=inc_id)
    return True, "Seguimiento guardado."


def listar_seguimiento_incidencia(inc_id):
    con = obtener_conexion()
    return pd.read_sql(
        "SELECT tipo, detalle, registrado_por, timestamp "
        "FROM incidencias_seguimiento WHERE incidencia_id=? ORDER BY id",
        con, params=[inc_id]
    )


def actualizar_campos_incidencia(inc_id, tipo_id, lugar_id, antecedentes, descripcion_hechos, usuario):
    escribir(
        "UPDATE incidencias SET tipo_id=?, lugar_id=?, antecedentes=?, "
        "descripcion_hechos=?, estado='en_revision', creado_por=? "
        "WHERE id=? AND estado IN ('reportada','en_revision')",
        (tipo_id, lugar_id, 1 if antecedentes else 0,
         descripcion_hechos or None, usuario["usuario"], inc_id)
    )
    auditar(usuario["usuario"], "Actualizo incidencia id=" + str(inc_id), tb="incidencias", rid=inc_id)
    return True, "Incidencia actualizada."


def eliminar_incidencia(inc_id, motivo, usuario):
    if usuario["rol"] not in ("TOECE", "Admin"):
        return False, "Solo TOECE o Admin pueden eliminar."
    if not motivo or not motivo.strip():
        return False, "El motivo de eliminacion es obligatorio."
    escribir(
        "UPDATE incidencias SET estado='eliminada', eliminada_por=?, eliminada_en=?, "
        "motivo_eliminacion=? WHERE id=?",
        (usuario["usuario"], timestamp_str(), motivo.strip(), inc_id)
    )
    auditar(usuario["usuario"], "Elimino incidencia id=" + str(inc_id) + " motivo: " + motivo,
            tb="incidencias", rid=inc_id)
    return True, "Incidencia eliminada."


def marcar_incidencia_perdonada(inc_id, usuario):
    escribir(
        "UPDATE incidencias SET estado='cerrada', cerrado_por=?, cerrado_en=? WHERE id=?",
        (usuario["usuario"], timestamp_str(), inc_id)
    )
    auditar(usuario["usuario"], "Perdono incidencia id=" + str(inc_id), tb="incidencias", rid=inc_id)
    return True, "Incidencia perdonada y cerrada."


def contar_incidencias_por_tipo_alumno(alumno_id, tipo_id):
    con = obtener_conexion()
    r = con.execute(
        "SELECT COUNT(*) FROM incidencias WHERE alumno_id=? AND tipo_id=? AND estado NOT IN ('eliminada')",
        (alumno_id, tipo_id)
    ).fetchone()
    return r[0] or 0


def tiene_acta_firmada_por_tipo(alumno_id, tipo_id):
    con = obtener_conexion()
    r = con.execute(
        "SELECT COUNT(*) FROM actas a JOIN incidencias i ON a.incidencia_id=i.id "
        "WHERE a.alumno_id=? AND i.tipo_id=? AND a.estado IN ('firmada','cerrada')",
        (alumno_id, tipo_id)
    ).fetchone()
    return (r[0] or 0) > 0


def historial_incidencias_alumno(alumno_id):
    con = obtener_conexion()
    return pd.read_sql(
        "SELECT i.id, i.fecha, i.hora, i.estado, ti.nombre AS tipo, "
        "li.nombre AS lugar, i.antecedentes, i.descripcion_breve, i.descripcion_hechos, "
        "i.reportado_por, i.creado_por, i.cerrado_por, i.creado_en "
        "FROM incidencias i "
        "LEFT JOIN tipos_incidencia ti ON i.tipo_id=ti.id "
        "LEFT JOIN lugares_incidencia li ON i.lugar_id=li.id "
        "WHERE i.alumno_id=? ORDER BY i.fecha DESC, i.hora DESC",
        con, params=[alumno_id]
    )


# ============================================================
# FIRMAS
# ============================================================
def guardar_firma_incidencia(inc_id, rol_firmante, nombre_firmante, imagen_base64, usuario):
    if not imagen_base64:
        return False, "La firma esta vacia."
    escribir(
        "INSERT INTO incidencias_firmas(incidencia_id,rol_firmante,nombre_firmante,imagen_base64,timestamp) "
        "VALUES(?,?,?,?,?)",
        (inc_id, rol_firmante, nombre_firmante or usuario["usuario"], imagen_base64, timestamp_str())
    )
    auditar(usuario["usuario"], "Firmo incidencia id=" + str(inc_id) + " como " + rol_firmante,
            tb="incidencias_firmas", rid=inc_id)
    return True, "Firma guardada."


def listar_firmas_incidencia(inc_id):
    con = obtener_conexion()
    return con.execute(
        "SELECT rol_firmante, nombre_firmante, imagen_base64, timestamp "
        "FROM incidencias_firmas WHERE incidencia_id=? ORDER BY id",
        (inc_id,)
    ).fetchall()


def canvas_a_base64(canvas_result):
    if canvas_result is None:
        return None
    if canvas_result.image_data is None:
        return None
    from PIL import Image
    img = Image.fromarray(canvas_result.image_data.astype("uint8"), "RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ============================================================
# ACTAS
# ============================================================
def crear_acta(incidencia_id, alumno_id, tipo_acta_id, asunto, detalle, usuario, periodo_id=None):
    con = obtener_conexion()
    ta = con.execute("SELECT * FROM tipos_acta WHERE id=?", (tipo_acta_id,)).fetchone()
    if not ta:
        return False, "Tipo de acta no encontrado.", None
    with _lock_escritura:
        cur = con.execute(
            "INSERT INTO actas(incidencia_id,alumno_id,tipo_acta_id,fecha,asunto,detalle,"
            "estado,creado_por,creado_en,periodo_id) VALUES(?,?,?,?,?,?,'pendiente_firmas',?,?,?)",
            (incidencia_id, alumno_id, tipo_acta_id, hoy_str(),
             asunto or None, detalle or None,
             usuario["usuario"], timestamp_str(), periodo_id)
        )
        acta_id = cur.lastrowid
        if incidencia_id:
            con.execute("UPDATE incidencias SET estado='acta_pendiente' WHERE id=?", (incidencia_id,))
        con.commit()
    auditar(usuario["usuario"], "Creo acta id=" + str(acta_id) + " alumno_id=" + str(alumno_id),
            tb="actas", rid=acta_id)
    return True, "Acta creada. Pendiente de firmas.", acta_id


def obtener_acta(acta_id):
    con = obtener_conexion()
    f = con.execute(
        "SELECT a.*, ta.nombre AS tipo_nombre, ta.requiere_firma_estudiante, ta.requiere_firma_apoderado, "
        "al.dni, al.nombres, al.apellido_paterno, al.apellido_materno, "
        "al.nombre_apoderado, al.telefono_apoderado, "
        "g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno "
        "FROM actas a JOIN tipos_acta ta ON a.tipo_acta_id=ta.id "
        "JOIN alumnos al ON a.alumno_id=al.id "
        "JOIN secciones s ON al.seccion_id=s.id "
        "JOIN grados g ON s.grado_id=g.id "
        "JOIN turnos t ON s.turno_id=t.id "
        "WHERE a.id=?", (acta_id,)
    ).fetchone()
    return dict(f) if f else None


def firmas_acta(acta_id):
    con = obtener_conexion()
    return con.execute(
        "SELECT rol_firmante, nombre_firmante, imagen_base64, timestamp "
        "FROM actas_firmas WHERE acta_id=? ORDER BY id",
        (acta_id,)
    ).fetchall()


def acta_tiene_firma(acta_id, rol):
    con = obtener_conexion()
    r = con.execute(
        "SELECT COUNT(*) FROM actas_firmas WHERE acta_id=? AND rol_firmante=?",
        (acta_id, rol)
    ).fetchone()
    return (r[0] or 0) > 0


def acta_firmas_completas(acta_id):
    a = obtener_acta(acta_id)
    if not a:
        return False, ["Acta no encontrada."]
    faltan = []
    if a["requiere_firma_estudiante"] and not acta_tiene_firma(acta_id, "estudiante"):
        faltan.append("estudiante")
    if a["requiere_firma_apoderado"] and not acta_tiene_firma(acta_id, "apoderado"):
        faltan.append("apoderado")
    if not acta_tiene_firma(acta_id, "toece"):
        faltan.append("TOECE")
    return len(faltan) == 0, faltan


def guardar_firma_acta(acta_id, rol_firmante, nombre_firmante, imagen_base64, usuario):
    if not imagen_base64:
        return False, "La firma esta vacia."
    if rol_firmante not in ("estudiante", "apoderado", "toece"):
        return False, "Rol de firmante invalido."
    if acta_tiene_firma(acta_id, rol_firmante):
        return False, "Esa firma ya existe en el acta."
    escribir(
        "INSERT INTO actas_firmas(acta_id,rol_firmante,nombre_firmante,imagen_base64,timestamp) "
        "VALUES(?,?,?,?,?)",
        (acta_id, rol_firmante, nombre_firmante or "", imagen_base64, timestamp_str())
    )
    auditar(usuario["usuario"], "Firmo acta id=" + str(acta_id) + " como " + rol_firmante,
            tb="actas_firmas", rid=acta_id)
    completo, faltan = acta_firmas_completas(acta_id)
    if completo:
        con = obtener_conexion()
        with _lock_escritura:
            con.execute("UPDATE actas SET estado='cerrada', cerrado_por=?, cerrado_en=? WHERE id=?",
                        (usuario["usuario"], timestamp_str(), acta_id))
            a = obtener_acta(acta_id)
            if a and a["incidencia_id"]:
                con.execute(
                    "UPDATE incidencias SET estado='cerrada', cerrado_por=?, cerrado_en=? WHERE id=?",
                    (usuario["usuario"], timestamp_str(), a["incidencia_id"])
                )
            con.commit()
        return True, "Firma guardada. Acta cerrada."
    return True, "Firma guardada. Faltan: " + ", ".join(faltan)


def listar_actas_por_estado(estados, limite=200):
    con = obtener_conexion()
    ph = ",".join(["?"] * len(estados))
    q = ("SELECT a.id, a.fecha, a.estado, ta.nombre AS tipo_acta, "
         "ta.requiere_firma_estudiante, ta.requiere_firma_apoderado, "
         "al.dni, al.apellido_paterno||' '||COALESCE(al.apellido_materno,'') AS apellidos, "
         "al.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno "
         "FROM actas a JOIN tipos_acta ta ON a.tipo_acta_id=ta.id "
         "JOIN alumnos al ON a.alumno_id=al.id "
         "JOIN secciones s ON al.seccion_id=s.id "
         "JOIN grados g ON s.grado_id=g.id "
         "JOIN turnos t ON s.turno_id=t.id "
         "WHERE a.estado IN (" + ph + ") "
         "ORDER BY a.fecha DESC, a.id DESC LIMIT ?")
    params = list(estados) + [limite]
    return pd.read_sql(q, con, params=params)


def actas_pendientes_detalle():
    con = obtener_conexion()
    df = pd.read_sql(
        "SELECT a.id, a.fecha, a.estado, ta.nombre AS tipo_acta, "
        "ta.requiere_firma_estudiante, ta.requiere_firma_apoderado, "
        "al.dni, al.apellido_paterno||' '||COALESCE(al.apellido_materno,'') AS apellidos, "
        "al.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno "
        "FROM actas a JOIN tipos_acta ta ON a.tipo_acta_id=ta.id "
        "JOIN alumnos al ON a.alumno_id=al.id "
        "JOIN secciones s ON al.seccion_id=s.id "
        "JOIN grados g ON s.grado_id=g.id "
        "JOIN turnos t ON s.turno_id=t.id "
        "WHERE a.estado='pendiente_firmas' ORDER BY a.fecha",
        con
    )
    if df.empty:
        return df
    faltantes = []
    for _, r in df.iterrows():
        f = []
        if r["requiere_firma_estudiante"] and not acta_tiene_firma(r["id"], "estudiante"):
            f.append("Estudiante")
        if r["requiere_firma_apoderado"] and not acta_tiene_firma(r["id"], "apoderado"):
            f.append("Apoderado")
        if not acta_tiene_firma(r["id"], "toece"):
            f.append("TOECE")
        faltantes.append(", ".join(f) if f else "Lista para cerrar")
    df["faltan"] = faltantes
    return df


def eliminar_acta(acta_id, motivo, usuario):
    if usuario["rol"] not in ("TOECE", "Admin"):
        return False, "Solo TOECE o Admin."
    if not motivo or not motivo.strip():
        return False, "Motivo obligatorio."
    escribir("UPDATE actas SET estado='eliminada' WHERE id=?", (acta_id,))
    auditar(usuario["usuario"], "Elimino acta id=" + str(acta_id) + " motivo: " + motivo,
            tb="actas", rid=acta_id)
    return True, "Acta eliminada."


# ============================================================
# MODOS DE CAMARA
# ============================================================
@st.cache_data(ttl=30)
def listar_modos_camara(solo_activos=True):
    con = obtener_conexion()
    q = "SELECT * FROM modos_camara"
    if solo_activos:
        q += " WHERE activo=1"
    q += " ORDER BY orden,id"
    return [dict(f) for f in con.execute(q).fetchall()]


def crear_modo_camara(codigo, nombre, tabla_destino, tipo_asistencia,
                       obedece_ventana, descripcion, usuario):
    codigo = (codigo or "").strip().lower().replace(" ", "_")
    if not codigo or not nombre.strip():
        return False, "Codigo y nombre son obligatorios."
    if tabla_destino not in ("asistencias", "incidencias", "otro"):
        return False, "Tabla destino invalida."
    con = obtener_conexion()
    try:
        orden = con.execute("SELECT COALESCE(MAX(orden),0)+1 FROM modos_camara").fetchone()[0]
        escribir(
            "INSERT INTO modos_camara(codigo,nombre,tabla_destino,tipo_asistencia,obedece_ventana,"
            "activo,orden,es_sistema,descripcion) VALUES(?,?,?,?,?,1,?,0,?)",
            (codigo, nombre.strip(), tabla_destino, tipo_asistencia,
             1 if obedece_ventana else 0, orden, descripcion or None)
        )
        auditar(usuario["usuario"], "Creo modo camara " + codigo)
        listar_modos_camara.clear()
        return True, "Modo creado."
    except sqlite3.IntegrityError:
        return False, "Ya existe un modo con ese codigo."
    except sqlite3.Error as e:
        return False, "Error: " + str(e)


def editar_modo_camara(idm, nombre, tabla_destino, tipo_asistencia,
                        obedece_ventana, descripcion, usuario):
    if not nombre.strip():
        return False, "Nombre obligatorio."
    escribir(
        "UPDATE modos_camara SET nombre=?,tabla_destino=?,tipo_asistencia=?,"
        "obedece_ventana=?,descripcion=? WHERE id=?",
        (nombre.strip(), tabla_destino, tipo_asistencia,
         1 if obedece_ventana else 0, descripcion or None, idm)
    )
    auditar(usuario["usuario"], "Edito modo camara id=" + str(idm))
    listar_modos_camara.clear()
    return True, "Modo actualizado."


def activar_desactivar_modo_camara(idm, activo, usuario):
    con = obtener_conexion()
    m = con.execute("SELECT codigo,es_sistema FROM modos_camara WHERE id=?", (idm,)).fetchone()
    if not m:
        return False, "Modo no encontrado."
    if m["es_sistema"] and not activo:
        return False, "Los modos del sistema no se pueden desactivar."
    escribir("UPDATE modos_camara SET activo=? WHERE id=?", (1 if activo else 0, idm))
    auditar(usuario["usuario"], ("Activo" if activo else "Desactivo") + " modo camara " + m["codigo"])
    listar_modos_camara.clear()
    return True, "Modo " + ("activado" if activo else "desactivado") + "."


@st.cache_data(ttl=30)
def listar_tipos_incidencia(solo_activos=True):
    con = obtener_conexion()
    q = "SELECT * FROM tipos_incidencia"
    if solo_activos:
        q += " WHERE activo=1"
    q += " ORDER BY orden,id"
    return [dict(f) for f in con.execute(q).fetchall()]


@st.cache_data(ttl=30)
def listar_lugares_incidencia(solo_activos=True):
    con = obtener_conexion()
    q = "SELECT * FROM lugares_incidencia"
    if solo_activos:
        q += " WHERE activo=1"
    q += " ORDER BY orden,id"
    return [dict(f) for f in con.execute(q).fetchall()]


@st.cache_data(ttl=30)
def listar_tipos_acta(solo_activos=True):
    con = obtener_conexion()
    q = "SELECT * FROM tipos_acta"
    if solo_activos:
        q += " WHERE activo=1"
    q += " ORDER BY orden,id"
    return [dict(f) for f in con.execute(q).fetchall()]


@st.cache_data(ttl=30)
def listar_cursos(solo_activos=True):
    con = obtener_conexion()
    q = "SELECT * FROM cursos"
    if solo_activos:
        q += " WHERE activo=1"
    q += " ORDER BY orden,id"
    return [dict(f) for f in con.execute(q).fetchall()]


def crear_tipo_incidencia(nombre, reincidente_grave, usuario):
    if not nombre.strip():
        return False, "Nombre obligatorio."
    con = obtener_conexion()
    try:
        orden = con.execute("SELECT COALESCE(MAX(orden),0)+1 FROM tipos_incidencia").fetchone()[0]
        escribir("INSERT INTO tipos_incidencia(nombre,es_reincidente_grave,activo,orden) VALUES(?,?,1,?)",
                 (nombre.strip(), 1 if reincidente_grave else 0, orden))
        auditar(usuario["usuario"], "Creo tipo incidencia " + nombre.strip())
        listar_tipos_incidencia.clear()
        return True, "Tipo creado."
    except sqlite3.IntegrityError:
        return False, "Ya existe ese tipo."
    except sqlite3.Error as e:
        return False, "Error: " + str(e)


def activar_desactivar_tipo_incidencia(idt, activo, usuario):
    escribir("UPDATE tipos_incidencia SET activo=? WHERE id=?", (1 if activo else 0, idt))
    auditar(usuario["usuario"], ("Activo" if activo else "Desactivo") + " tipo incidencia id=" + str(idt))
    listar_tipos_incidencia.clear()
    return True, "Actualizado."


def crear_lugar_incidencia(nombre, usuario):
    if not nombre.strip():
        return False, "Nombre obligatorio."
    con = obtener_conexion()
    try:
        orden = con.execute("SELECT COALESCE(MAX(orden),0)+1 FROM lugares_incidencia").fetchone()[0]
        escribir("INSERT INTO lugares_incidencia(nombre,activo,orden) VALUES(?,1,?)",
                 (nombre.strip(), orden))
        auditar(usuario["usuario"], "Creo lugar incidencia " + nombre.strip())
        listar_lugares_incidencia.clear()
        return True, "Lugar creado."
    except sqlite3.IntegrityError:
        return False, "Ya existe ese lugar."
    except sqlite3.Error as e:
        return False, "Error: " + str(e)


def activar_desactivar_lugar_incidencia(idl, activo, usuario):
    escribir("UPDATE lugares_incidencia SET activo=? WHERE id=?", (1 if activo else 0, idl))
    auditar(usuario["usuario"], ("Activo" if activo else "Desactivo") + " lugar id=" + str(idl))
    listar_lugares_incidencia.clear()
    return True, "Actualizado."


def crear_tipo_acta(nombre, req_est, req_apo, usuario):
    if not nombre.strip():
        return False, "Nombre obligatorio."
    con = obtener_conexion()
    try:
        orden = con.execute("SELECT COALESCE(MAX(orden),0)+1 FROM tipos_acta").fetchone()[0]
        escribir(
            "INSERT INTO tipos_acta(nombre,requiere_firma_estudiante,requiere_firma_apoderado,"
            "activo,orden) VALUES(?,?,?,1,?)",
            (nombre.strip(), 1 if req_est else 0, 1 if req_apo else 0, orden)
        )
        auditar(usuario["usuario"], "Creo tipo acta " + nombre.strip())
        listar_tipos_acta.clear()
        return True, "Tipo de acta creado."
    except sqlite3.IntegrityError:
        return False, "Ya existe ese tipo de acta."
    except sqlite3.Error as e:
        return False, "Error: " + str(e)


def editar_tipo_acta(idt, nombre, req_est, req_apo, usuario):
    if not nombre.strip():
        return False, "Nombre obligatorio."
    escribir(
        "UPDATE tipos_acta SET nombre=?,requiere_firma_estudiante=?,requiere_firma_apoderado=? WHERE id=?",
        (nombre.strip(), 1 if req_est else 0, 1 if req_apo else 0, idt)
    )
    auditar(usuario["usuario"], "Edito tipo acta id=" + str(idt))
    listar_tipos_acta.clear()
    return True, "Tipo de acta actualizado."


def activar_desactivar_tipo_acta(idt, activo, usuario):
    escribir("UPDATE tipos_acta SET activo=? WHERE id=?", (1 if activo else 0, idt))
    auditar(usuario["usuario"], ("Activo" if activo else "Desactivo") + " tipo acta id=" + str(idt))
    listar_tipos_acta.clear()
    return True, "Actualizado."


# ============================================================
# REPORTES BASE
# ============================================================
def metricas_dia(fecha, pid=None):
    con = obtener_conexion()
    total = con.execute("SELECT COUNT(*) FROM alumnos WHERE activo=1").fetchone()[0]
    puntuales = con.execute(
        "SELECT COUNT(*) FROM asistencias WHERE fecha=? AND tipo='clases' AND estado='Puntual'",
        (fecha,)
    ).fetchone()[0]
    tardanzas = con.execute(
        "SELECT COUNT(*) FROM asistencias WHERE fecha=? AND tipo='clases' AND estado='Tardanza'",
        (fecha,)
    ).fetchone()[0]
    faltas = con.execute(
        "SELECT COUNT(*) FROM asistencias WHERE fecha=? AND tipo='clases' AND estado='Falta'",
        (fecha,)
    ).fetchone()[0]
    permisos_hoy = con.execute(
        "SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado='Permiso'", (fecha,)
    ).fetchone()[0]
    ref_asistio = con.execute(
        "SELECT COUNT(*) FROM asistencias WHERE fecha=? AND tipo='reforzamiento' AND estado='Asistio'",
        (fecha,)
    ).fetchone()[0]
    bloqueados = con.execute("SELECT COUNT(*) FROM bloqueos WHERE activo=1").fetchone()[0]
    just_hoy = con.execute(
        "SELECT COUNT(*) FROM asistencias WHERE fecha=? AND justificada=1", (fecha,)
    ).fetchone()[0]
    return {"total": total, "puntuales": puntuales, "tardanzas": tardanzas,
            "faltas": faltas, "ref_asistio": ref_asistio, "bloqueados": bloqueados,
            "justificadas": just_hoy, "permisos": permisos_hoy}


def ultimos_registros(fecha, limite=20):
    return pd.read_sql(
        "SELECT a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,"
        "a.nombres,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,"
        "ast.tipo,ast.hora,ast.estado "
        "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id "
        "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
        "JOIN turnos t ON s.turno_id=t.id "
        "WHERE ast.fecha=? AND ast.hora IS NOT NULL ORDER BY ast.hora DESC LIMIT ?",
        obtener_conexion(), params=[fecha, limite]
    )


def reporte_detalle_por_tipo(inicio, fin, ids_sec, tipo_reporte, pid=None):
    con = obtener_conexion()
    q = ("SELECT a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,"
         "a.nombres,ast.fecha,ast.tipo,ast.estado,ast.justificada,ast.origen,"
         "COALESCE(ast.justificado_por,'') AS justificado_por,"
         "COALESCE(ast.justificado_en,'') AS justificado_en,"
         "COALESCE(ast.observacion,'') AS observacion "
         "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id "
         "JOIN secciones s ON a.seccion_id=s.id "
         "WHERE ast.fecha BETWEEN ? AND ? AND ast.hora IS NOT NULL AND ast.tipo=?")
    p = [inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d"), tipo_reporte]
    if ids_sec:
        q += " AND s.id IN (" + ",".join(["?"] * len(ids_sec)) + ")"
        p += ids_sec
    if pid is not None:
        q += " AND ast.periodo_id=?"
        p.append(pid)
    q += " ORDER BY ast.fecha DESC, a.apellido_paterno"
    return pd.read_sql(q, con, params=p)


def reporte_conteo_faltas(inicio, fin, ids_sec, pid=None):
    con = obtener_conexion()
    q = ("SELECT a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,"
         "a.nombres,"
         "SUM(CASE WHEN ast.justificada=1 THEN 1 ELSE 0 END) AS faltas_just,"
         "SUM(CASE WHEN ast.justificada=0 THEN 1 ELSE 0 END) AS faltas_injust,"
         "COUNT(*) AS total_faltas "
         "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id "
         "JOIN secciones s ON a.seccion_id=s.id "
         "WHERE ast.estado='Falta' AND ast.tipo='clases' AND ast.fecha BETWEEN ? AND ?")
    p = [inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")]
    if ids_sec:
        q += " AND s.id IN (" + ",".join(["?"] * len(ids_sec)) + ")"
        p += ids_sec
    if pid is not None:
        q += " AND ast.periodo_id=?"
        p.append(pid)
    q += " GROUP BY a.id ORDER BY faltas_injust DESC, total_faltas DESC"
    return pd.read_sql(q, con, params=p)


def cierre_mensual_calendario(mes, año, ids_sec, pid=None):
    ult = monthrange(año, mes)[1]
    ini = date(año, mes, 1)
    fin = date(año, mes, ult)
    con = obtener_conexion()
    dias = []
    for d in range(1, ult + 1):
        f = date(año, mes, d)
        if f.weekday() >= 5:
            esp = con.execute(
                "SELECT id FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento'",
                (f.strftime("%Y-%m-%d"),)
            ).fetchone()
            if not esp:
                continue
        dias.append(f)
    ph = ",".join(["?"] * len(ids_sec))
    df_al = pd.read_sql(
        "SELECT a.id AS alumno_id, "
        "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos, "
        "a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno "
        "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id "
        "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
        "WHERE a.seccion_id IN (" + ph + ") AND a.activo=1 "
        "ORDER BY t.nombre, g.nombre, s.nombre, a.apellido_paterno",
        con, params=ids_sec
    )
    if df_al.empty:
        return pd.DataFrame()

    asis = {}
    q_asis = ("SELECT alumno_id, fecha, estado, justificada FROM asistencias "
              "WHERE fecha BETWEEN ? AND ? AND tipo='clases'")
    p_asis = [ini.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")]
    if pid is not None:
        q_asis += " AND periodo_id=?"
        p_asis.append(pid)
    for r in con.execute(q_asis, p_asis).fetchall():
        key = (r["alumno_id"], r["fecha"])
        est = r["estado"]
        if est == "Falta":
            asis[key] = "F"
        else:
            asis[key] = "P"

    filas = []
    for _, al in df_al.iterrows():
        fila = {"Apellidos": al["apellidos"], "Nombres": al["nombres"]}
        tp = tf = 0
        for d in dias:
            f_str = d.strftime("%Y-%m-%d")
            etiqueta = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"][d.weekday()] + " " + str(d.day).zfill(2)
            est = asis.get((al["alumno_id"], f_str), "-")
            fila[etiqueta] = est
            if est == "P":
                tp += 1
            elif est == "F":
                tf += 1
        fila["Total P"] = tp
        fila["Total F"] = tf
        filas.append(fila)

    df = pd.DataFrame(filas)
    cols_base = ["Apellidos", "Nombres"]
    cols_dias = [c for c in df.columns if c not in cols_base and not c.startswith("Total")]
    cols_tot = ["Total P", "Total F"]
    return df[cols_base + cols_dias + cols_tot]


def casos_toece(pid=None):
    con = obtener_conexion()
    filt = ""
    p = []
    if pid is not None:
        filt = " AND t2.periodo_id=?"
        p.append(pid)
    return pd.read_sql("""
        SELECT a.id,a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,
        a.nombres,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,
        a.nombre_apoderado,a.telefono_apoderado,
        COUNT(t2.id) AS tard_injust,
        (SELECT COUNT(*) FROM actas_compromiso WHERE alumno_id=a.id) AS total_actas,
        CASE WHEN EXISTS(SELECT 1 FROM bloqueos WHERE alumno_id=a.id AND activo=1) THEN 'SI' ELSE 'NO' END AS bloqueado
        FROM tardanzas t2 JOIN alumnos a ON t2.alumno_id=a.id
        JOIN secciones s ON a.seccion_id=s.id
        JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
        WHERE t2.justificada=0 """ + filt + """
        GROUP BY a.id HAVING COUNT(t2.id)>=3 ORDER BY tard_injust DESC
        """, con, params=p)


def listar_observados(solo_activos=True):
    con = obtener_conexion()
    q = ("SELECT o.id,a.id AS alumno_id,a.dni,"
         "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,"
         "a.nombres,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,"
         "o.fecha_ingreso,COALESCE(o.motivo,'') AS motivo,o.activo,"
         "COALESCE(o.fecha_salida,'') AS fecha_salida,"
         "COALESCE(o.observacion_cierre,'') AS observacion_cierre "
         "FROM observados o JOIN alumnos a ON o.alumno_id=a.id "
         "JOIN secciones s ON a.seccion_id=s.id "
         "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id")
    if solo_activos:
        q += " WHERE o.activo=1"
    q += " ORDER BY o.fecha_ingreso DESC"
    return pd.read_sql(q, con)


def listar_bloqueados():
    return pd.read_sql(
        "SELECT b.id,a.id AS alumno_id,a.dni,"
        "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS alumno,"
        "g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,b.motivo,b.fecha_inicio,"
        "COALESCE(b.origen,'automatico') AS origen "
        "FROM bloqueos b JOIN alumnos a ON b.alumno_id=a.id "
        "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
        "JOIN turnos t ON s.turno_id=t.id WHERE b.activo=1 ORDER BY b.fecha_inicio DESC",
        obtener_conexion()
    )


def obtener_auditoria(limite=500):
    return pd.read_sql(
        "SELECT id,usuario,accion,fecha,ip FROM auditoria ORDER BY id DESC LIMIT " + str(int(limite)),
        obtener_conexion()
    )


def reporte_general_por_turno(desde, hasta, id_turno, pid=None):
    con = obtener_conexion()
    q = """
    SELECT s.id AS seccion_id, g.nombre AS grado, s.nombre AS seccion,
    COALESCE(u.nombres, '(Sin auxiliar)') AS auxiliar,
    SUM(CASE WHEN ast.estado='Puntual' AND ast.tipo='clases' THEN 1 ELSE 0 END) AS puntuales,
    SUM(CASE WHEN ast.estado='Falta' AND ast.tipo='clases' THEN 1 ELSE 0 END) AS faltas,
    SUM(CASE WHEN ast.estado='Permiso' AND ast.tipo='clases' THEN 1 ELSE 0 END) AS permisos
    FROM secciones s
    JOIN grados g ON s.grado_id=g.id
    LEFT JOIN auxiliar_secciones ause ON ause.seccion_id = s.id
    LEFT JOIN usuarios u ON u.id = ause.usuario_id AND u.rol='Auxiliar' AND u.activo=1
    LEFT JOIN asistencias ast ON ast.alumno_id IN (SELECT id FROM alumnos WHERE seccion_id=s.id)
        AND ast.fecha BETWEEN ? AND ?
    WHERE s.turno_id=?
    """
    p = [desde.strftime("%Y-%m-%d"), hasta.strftime("%Y-%m-%d"), id_turno]
    if pid is not None:
        q += " AND ast.periodo_id=?"
        p.append(pid)
    q += """
    GROUP BY s.id, g.nombre, s.nombre, auxiliar
    ORDER BY auxiliar, g.nombre, s.nombre
    """
    df = pd.read_sql(q, con, params=p)
    if df.empty:
        return df
    df["total"] = df["puntuales"] + df["faltas"] + df["permisos"]
    return df[["auxiliar", "grado", "seccion", "puntuales", "faltas", "permisos", "total"]]


# ============================================================
# PERFIL
# ============================================================
def perfil_alumno_datos(idal):
    con = obtener_conexion()
    a = con.execute(
        "SELECT a.*,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,"
        "s.id AS seccion_id,t.id AS turno_id "
        "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id "
        "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
        "WHERE a.id=?", (idal,)
    ).fetchone()
    if not a:
        return {}
    a = dict(a)
    da = pd.read_sql(
        "SELECT id,fecha,hora,tipo,estado,justificada,COALESCE(observacion,'') AS observacion,"
        "COALESCE(origen,'qr') AS origen,COALESCE(justificado_por,'') AS justificado_por,"
        "COALESCE(justificado_en,'') AS justificado_en "
        "FROM asistencias WHERE alumno_id=? ORDER BY fecha DESC,hora DESC",
        con, params=[idal]
    )
    dt = pd.read_sql(
        "SELECT fecha,hora,numero AS 'N',accion,justificada,"
        "COALESCE(origen,'qr') AS origen,COALESCE(observacion,'') AS observacion "
        "FROM tardanzas WHERE alumno_id=? ORDER BY fecha DESC,hora DESC",
        con, params=[idal]
    )
    do = pd.read_sql(
        "SELECT fecha_ingreso,COALESCE(fecha_salida,'-') AS fecha_salida,"
        "COALESCE(motivo,'') AS motivo,activo FROM observados "
        "WHERE alumno_id=? ORDER BY fecha_ingreso DESC",
        con, params=[idal]
    )
    dac = pd.read_sql(
        "SELECT fecha,COALESCE(motivo,'') AS motivo,COALESCE(observacion,'') AS observacion,"
        "COALESCE(registrado_por,'') AS registrado_por FROM actas_compromiso "
        "WHERE alumno_id=? ORDER BY fecha DESC",
        con, params=[idal]
    )
    db = pd.read_sql(
        "SELECT fecha_inicio,COALESCE(fecha_fin,'-') AS fecha_fin,"
        "COALESCE(motivo,'') AS motivo,activo FROM bloqueos "
        "WHERE alumno_id=? ORDER BY fecha_inicio DESC",
        con, params=[idal]
    )
    dp = pd.read_sql(
        "SELECT fecha_inicio,fecha_fin,COALESCE(motivo,'') AS motivo,activo,creado_por "
        "FROM permisos WHERE alumno_id=? ORDER BY fecha_inicio DESC",
        con, params=[idal]
    )
    tp = int((da["estado"] == PUNTUAL).sum()) if not da.empty else 0
    tf = int((da["estado"] == FALTA).sum()) if not da.empty else 0
    tt = int((da["estado"] == TARDANZA).sum()) if not da.empty else 0
    tr = int(((da["tipo"] == "reforzamiento") & (da["estado"] == REF_ASISTIO)).sum()) if not da.empty else 0
    return {"alumno": a, "asistencias": da, "tardanzas": dt, "observados": do,
            "actas": dac, "bloqueos": db, "permisos": dp,
            "total_puntuales": tp, "total_faltas": tf,
            "total_tardanzas": tt, "total_ref_asistio": tr,
            "tard_injust": contar_tardanzas_injustificadas(idal),
            "bloqueado": alumno_bloqueado(idal) is not None}


# ============================================================
# CIERRE ANUAL
# ============================================================
def reporte_cierre_anual(pid):
    return pd.read_sql("""
        SELECT g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,
        COUNT(DISTINCT a.id) AS total_alumnos,
        (SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id=? AND ast.alumno_id IN (
            SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?) AND ast.estado='Puntual') AS puntuales,
        (SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id=? AND ast.alumno_id IN (
            SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?) AND ast.estado='Tardanza') AS tardanzas,
        (SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id=? AND ast.alumno_id IN (
            SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?) AND ast.estado='Falta' AND ast.justificada=0) AS faltas_injust,
        (SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id=? AND ast.alumno_id IN (
            SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?) AND ast.estado='Falta' AND ast.justificada=1) AS faltas_just
        FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
        LEFT JOIN alumnos a ON a.seccion_id=s.id AND a.periodo_id=?
        GROUP BY s.id ORDER BY t.nombre,g.nombre,s.nombre
        """, obtener_conexion(), params=[pid] * 9)


def cerrar_año_escolar(usuario, pid, nuevo_nombre, fi, ff):
    con = obtener_conexion()
    p = con.execute("SELECT * FROM periodos WHERE id=?", (pid,)).fetchone()
    if not p:
        return False, "Periodo no encontrado."
    if p["cerrado"]:
        return False, "Ese periodo ya esta cerrado."
    rep_json = None
    try:
        rep = reporte_cierre_anual(pid)
        if not rep.empty:
            rep_json = rep.to_json(orient="records", force_ascii=False)
    except Exception:
        rep_json = None
    fecha = timestamp_str()
    try:
        with _lock_escritura:
            con.execute(
                "INSERT INTO cierres_anuales(periodo_id,fecha_cierre,generado_por,reporte_json) "
                "VALUES(?,?,?,?)", (pid, fecha, usuario["usuario"], rep_json)
            )
            con.execute("UPDATE periodos SET activo=0,fecha_cierre=?,cerrado=1 WHERE id=?", (fecha, pid))
            con.execute("UPDATE alumnos SET activo=0,retirado_en=? WHERE periodo_id=?", (fecha, pid))
            con.execute(
                "INSERT INTO periodos(nombre,fecha_inicio,fecha_fin,activo,cerrado) VALUES(?,?,?,1,0)",
                (nuevo_nombre, fi, ff)
            )
            con.commit()
        listar_periodos.clear()
    except sqlite3.Error as e:
        con.rollback()
        log.error("cerrar año: %s", e)
        return False, "Error al cerrar el año."
    auditar(usuario["usuario"], "Cerro periodo " + p["nombre"], tb="periodos", rid=pid)
    return True, "Periodo '" + p["nombre"] + "' cerrado. Nuevo: '" + nuevo_nombre + "'."


def listar_cierres_anuales():
    return pd.read_sql(
        "SELECT c.id,p.nombre AS periodo,c.fecha_cierre,c.generado_por "
        "FROM cierres_anuales c JOIN periodos p ON c.periodo_id=p.id ORDER BY c.id DESC",
        obtener_conexion()
    )


# ============================================================
# PDF / EXCEL / QR / CARNETS
# ============================================================
def _escudo_path():
    p = Path("escudo.png")
    return p if p.exists() else None


def _marca_agua(canvas_obj, doc):
    """Dibuja el escudo como marca de agua centrada, opacidad baja."""
    p = _escudo_path()
    if not p:
        return
    try:
        from reportlab.lib.utils import ImageReader
        img = ImageReader(str(p))
        ancho_pag, alto_pag = doc.pagesize
        w = 320
        h = 320
        x = (ancho_pag - w) / 2
        y = (alto_pag - h) / 2
        canvas_obj.saveState()
        try:
            canvas_obj.setFillAlpha(0.12)
        except Exception:
            pass
        canvas_obj.drawImage(img, x, y, width=w, height=h, mask='auto')
        canvas_obj.restoreState()
    except Exception as e:
        log.warning("marca de agua: %s", e)


def _pdf_base(titulo, subtitulo=None, paisaje=False):
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=(A4[1], A4[0]) if paisaje else A4,
        rightMargin=25, leftMargin=25, topMargin=25, bottomMargin=25,
        onFirstPage=_marca_agua, onLaterPages=_marca_agua
    )
    est = getSampleStyleSheet()
    el = [Paragraph("<b>" + titulo + "</b>", est["Heading1"])]
    if subtitulo:
        el.append(Paragraph(subtitulo, est["Normal"]))
    el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"), est["Normal"]))
    el.append(Spacer(1, 15))
    return buf, doc, el, est


def generar_pdf_tabla(df, titulo, subtitulo=None):
    buf, doc, el, _ = _pdf_base(titulo, subtitulo, paisaje=len(df.columns) > 6)
    if not df.empty:
        datos = [df.columns.tolist()] + df.astype(str).values.tolist()
        t = Table(datos, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.white),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.black),
            ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        el.append(t)
    doc.build(el)
    buf.seek(0)
    return buf.getvalue()


def generar_pdf_tabla_ancha(df, titulo, subtitulo=None, fuente_chica=False):
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=(A4[1], A4[0]),
        rightMargin=15, leftMargin=15,
        topMargin=20, bottomMargin=20,
        onFirstPage=_marca_agua, onLaterPages=_marca_agua
    )
    est = getSampleStyleSheet()
    el = [Paragraph("<b>" + titulo + "</b>", est["Heading1"])]
    if subtitulo:
        el.append(Paragraph(subtitulo, est["Normal"]))
    el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"), est["Normal"]))
    el.append(Spacer(1, 12))

    if not df.empty:
        datos = [df.columns.tolist()] + df.astype(str).values.tolist()
        anchos = []
        for col in df.columns:
            col_low = str(col).lower()
            if any(k in col_low for k in ["auxiliar", "apellido", "nombres", "alumno", "observacion"]):
                anchos.append(3.5)
            elif any(k in col_low for k in ["grado", "seccion", "turno", "tipo", "estado"]):
                anchos.append(1.5)
            elif any(k in col_low for k in ["fecha", "hora"]):
                anchos.append(1.8)
            else:
                anchos.append(1.0)
        ancho_total = A4[1] - 30
        suma = sum(anchos)
        anchos = [a * ancho_total / suma for a in anchos]

        GRIS_FILA_ALT = colors.HexColor("#FAFAFA")
        if fuente_chica:
            fuente_cab = 6
            fuente_fila = 6
            padding = 3
        else:
            fuente_cab = 11
            fuente_fila = 10
            padding = 5

        t = Table(datos, colWidths=anchos, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.white),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), fuente_cab),
            ("FONTSIZE", (0, 1), (-1, -1), fuente_fila),
            ("TEXTCOLOR", (0, 1), (-1, -1), colors.black),
            ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.black),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("LEFTPADDING", (0, 0), (-1, -1), padding),
            ("RIGHTPADDING", (0, 0), (-1, -1), padding),
            ("TOPPADDING", (0, 0), (-1, -1), padding),
            ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GRIS_FILA_ALT]),
        ]))
        el.append(t)

    doc.build(el)
    buf.seek(0)
    return buf.getvalue()


def generar_qr(dni):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(str(dni).strip())
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def _generar_fotocheck_pil(alumno, escudo_path=None):
    from PIL import Image, ImageDraw, ImageFont
    import qrcode
    from datetime import datetime

    ANCHO_PX = 817
    ALTO_PX = 550

    NARANJA_OSCURO = (225, 150, 90)
    NARANJA_CLARO = (240, 175, 115)
    NARANJA_FRANJA = (220, 140, 80)
    FONDO_ANARANJADO = (255, 230, 200)
    BLANCO = (255, 255, 255)
    NEGRO = (17, 17, 17)
    GRIS_LABEL = (90, 60, 30)
    GRIS_TXT = (150, 150, 150)
    img = Image.new("RGB", (ANCHO_PX, ALTO_PX), FONDO_ANARANJADO)
    draw = ImageDraw.Draw(img)

    color_punto = (235, 210, 180)
    paso_puntos = 22
    radio_punto = 1
    for py in range(0, ALTO_PX, paso_puntos):
        for px in range(0, ANCHO_PX, paso_puntos):
            draw.ellipse(
                [px - radio_punto, py - radio_punto, px + radio_punto, py + radio_punto],
                fill=color_punto
            )

    if escudo_path and Path(escudo_path).exists():
        try:
            escudo_wm = Image.open(str(escudo_path)).convert("RGBA")
            wm_size = 280
            escudo_wm = escudo_wm.resize((wm_size, wm_size), Image.LANCZOS)
            alpha = escudo_wm.split()[3]
            alpha = alpha.point(lambda p: int(p * 0.15))
            escudo_wm.putalpha(alpha)
            wm_x = (ANCHO_PX - wm_size) // 2 + 100
            wm_y = (ALTO_PX - wm_size) // 2
            img.paste(escudo_wm, (wm_x, wm_y), escudo_wm)
        except Exception:
            pass

    FOTO_W = 216
    FOTO_H = 280
    FRANJA_W = FOTO_W

    for y in range(ALTO_PX):
        t = y / ALTO_PX
        r = int(NARANJA_OSCURO[0] + (NARANJA_CLARO[0] - NARANJA_OSCURO[0]) * t)
        g = int(NARANJA_OSCURO[1] + (NARANJA_CLARO[1] - NARANJA_OSCURO[1]) * t)
        b = int(NARANJA_OSCURO[2] + (NARANJA_CLARO[2] - NARANJA_OSCURO[2]) * t)
        draw.line([(0, y), (FRANJA_W, y)], fill=(r, g, b))

    def _font(size, bold=False, italic=False):
        nombres = []
        if bold and italic:
            nombres = ["arialbi.ttf", "Arial_Bold_Italic.ttf", "DejaVuSans-BoldOblique.ttf"]
        elif bold:
            nombres = ["arialbd.ttf", "Arial_Bold.ttf", "DejaVuSans-Bold.ttf", "Helvetica-Bold"]
        elif italic:
            nombres = ["ariali.ttf", "Arial_Italic.ttf", "DejaVuSans-Oblique.ttf"]
        else:
            nombres = ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "Helvetica"]
        for n in nombres:
            try:
                return ImageFont.truetype(n, size)
            except Exception:
                continue
        return ImageFont.load_default()

    f_colegio = _font(13, bold=True)
    f_foto = _font(14, bold=True)
    f_titulo = _font(20, bold=True)
    f_frase = _font(19, bold=True, italic=True)
    f_label = _font(20, bold=True)

    escudo_size = 80
    escudo_x = (FRANJA_W - escudo_size) // 2
    espacio_disponible = ALTO_PX - FOTO_H
    alto_bloque_escudo = escudo_size + 70
    escudo_y = (espacio_disponible - alto_bloque_escudo) // 2 + 10
    if escudo_y < 8:
        escudo_y = 8

    if escudo_path and Path(escudo_path).exists():
        try:
            escudo = Image.open(str(escudo_path)).convert("RGBA")
            escudo = escudo.resize((escudo_size, escudo_size), Image.LANCZOS)
            img.paste(escudo, (escudo_x, escudo_y), escudo)
        except Exception:
            pass

    def _texto_centrado_franja(texto, y, font, color):
        try:
            bbox = draw.textbbox((0, 0), texto, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(texto) * 7
        x = (FRANJA_W - tw) // 2
        draw.text((x, y), texto, fill=color, font=font)

    y_txt = escudo_y + escudo_size + 6
    _texto_centrado_franja("INSTITUCION", y_txt, f_colegio, BLANCO)
    y_txt += 14
    _texto_centrado_franja("EDUCATIVA", y_txt, f_colegio, BLANCO)
    y_txt += 14
    _texto_centrado_franja("YARINACOCHA", y_txt, f_colegio, BLANCO)

    foto_x = 0
    foto_y = ALTO_PX - FOTO_H
    draw.rectangle([foto_x, foto_y, foto_x + FOTO_W, foto_y + FOTO_H], fill=BLANCO)
    draw.rectangle(
        [foto_x, foto_y, foto_x + FOTO_W - 1, foto_y + FOTO_H - 1],
        outline=NARANJA_FRANJA, width=1
    )

    texto_foto = "FOTO"
    try:
        bbox = draw.textbbox((0, 0), texto_foto, font=f_foto)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
    except Exception:
        tw = 40
        th = 12
    draw.text(
        (foto_x + (FOTO_W - tw) // 2, foto_y + (FOTO_H - th) // 2),
        texto_foto, fill=GRIS_TXT, font=f_foto
    )

    DER_X = FRANJA_W + 14
    titulo_txt = "FOTOCHECK DEL ESTUDIANTE"
    try:
        bbox = draw.textbbox((0, 0), titulo_txt, font=f_titulo)
        tw = bbox[2] - bbox[0]
    except Exception:
        tw = 280
    espacio_derecho = ANCHO_PX - DER_X
    titulo_x = DER_X + (espacio_derecho - tw) // 2
    draw.text((titulo_x, 12), titulo_txt, fill=NEGRO, font=f_titulo)

    apellidos_p = alumno['apellido_paterno'].upper()
    apellidos_m = (alumno['apellido_materno'] or "").upper()
    nombres = alumno['nombres'].upper()
    dni = alumno["dni"]
    grado = alumno['grado'].upper()
    seccion = alumno['seccion'].upper()
    turno = alumno['turno'].upper()
    anio = str(datetime.now().year)

    info_x = DER_X
    QR_SIZE = 215
    qr_x = ANCHO_PX - QR_SIZE - 12
    ancho_info = qr_x - info_x - 12

    def _ajustar(texto, size_inicial, max_ancho, bold):
        size = size_inicial
        while size > 10:
            f = _font(size, bold=bold)
            try:
                ancho = draw.textlength(texto, font=f)
            except Exception:
                ancho = len(texto) * size * 0.55
            if ancho <= max_ancho:
                return f
            size -= 1
        return _font(10, bold=bold)

    INFO_Y = 130
    alto_linea = 52

    apellidos_full = apellidos_p + " " + apellidos_m
    f_ap = _ajustar(apellidos_full, 32, ancho_info, True)
    draw.text((info_x, INFO_Y), apellidos_full, fill=NEGRO, font=f_ap)

    y2 = INFO_Y + alto_linea
    f_no = _ajustar(nombres, 32, ancho_info, True)
    draw.text((info_x, y2), nombres, fill=NEGRO, font=f_no)

    y3 = y2 + alto_linea
    draw.text((info_x, y3), "DNI:", fill=GRIS_LABEL, font=f_label)
    etiqueta_ancho = draw.textlength("DNI:", font=f_label)
    valor_x = info_x + int(etiqueta_ancho) + 10
    f_v = _ajustar(dni, 28, ancho_info - int(etiqueta_ancho) - 10, True)
    draw.text((valor_x, y3), dni, fill=NEGRO, font=f_v)

    y4 = y3 + alto_linea
    draw.text((info_x, y4), "GRADO:", fill=GRIS_LABEL, font=f_label)
    etiqueta_ancho = draw.textlength("GRADO:", font=f_label)
    valor_x = info_x + int(etiqueta_ancho) + 10
    valor_grado = grado + " \"" + seccion + "\""
    f_v = _ajustar(valor_grado, 28, ancho_info - int(etiqueta_ancho) - 10, True)
    draw.text((valor_x, y4), valor_grado, fill=NEGRO, font=f_v)

    y5 = y4 + alto_linea
    draw.text((info_x, y5), "TURNO:", fill=GRIS_LABEL, font=f_label)
    etiqueta_ancho = draw.textlength("TURNO:", font=f_label)
    valor_x = info_x + int(etiqueta_ancho) + 10
    f_v = _ajustar(turno, 28, ancho_info - int(etiqueta_ancho) - 10, True)
    draw.text((valor_x, y5), turno, fill=NEGRO, font=f_v)

    y6 = y5 + alto_linea
    draw.text((info_x, y6), "ANIO:", fill=GRIS_LABEL, font=f_label)
    etiqueta_ancho = draw.textlength("ANIO:", font=f_label)
    valor_x = info_x + int(etiqueta_ancho) + 10
    f_v = _ajustar(anio, 28, ancho_info - int(etiqueta_ancho) - 10, True)
    draw.text((valor_x, y6), anio, fill=NEGRO, font=f_v)

    qr_y = 60
    qr = qrcode.QRCode(version=1, box_size=10, border=1)
    qr.add_data(str(alumno["dni"]).strip())
    qr.make(fit=True)
    qr_img_pil = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img_pil = qr_img_pil.resize((QR_SIZE, QR_SIZE), Image.LANCZOS)
    img.paste(qr_img_pil, (qr_x, qr_y))

    frase = "\"Ser del CNY, es ser mejor\""
    try:
        bbox = draw.textbbox((0, 0), frase, font=f_frase)
        fw = bbox[2] - bbox[0]
    except Exception:
        fw = 250
    frase_x = DER_X + (espacio_derecho - fw) // 2
    frase_y = ALTO_PX - 42
    draw.text((frase_x, frase_y), frase, fill=NARANJA_FRANJA, font=f_frase)

    return img


def _render_carnets(filas, titulo=None):
    buf = BytesIO()
    m = 5
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=m, leftMargin=m, topMargin=m, bottomMargin=m)
    est = getSampleStyleSheet()

    MM = 2.8346
    ANCHO = 85.0 * MM
    ALTO = 50.0 * MM
    escudo_path = _escudo_path()

    def _fotocheck(alumno):
        img = _generar_fotocheck_pil(alumno, escudo_path)
        ib = BytesIO()
        img.save(ib, format="PNG")
        ib.seek(0)
        return RLImage(ib, width=ANCHO, height=ALTO)

    el = []
    if titulo:
        el.append(Paragraph("<b>" + titulo + "</b>", est["Heading2"]))
        el.append(Spacer(1, 6))

    sep_x = 6
    sep_y = 10
    caben_x = 2
    alto_hoja = A4[1] - 2 * m
    caben_y = max(1, int((alto_hoja + sep_y) // (ALTO + sep_y)))

    fotochecks = [_fotocheck(a) for a in filas]

    for i in range(0, len(fotochecks), caben_x * caben_y):
        lote = fotochecks[i:i + caben_x * caben_y]
        tabla_filas = []
        for j in range(0, len(lote), caben_x):
            fila = lote[j:j + caben_x]
            while len(fila) < caben_x:
                fila.append("")
            tabla_filas.append(fila)
        while len(tabla_filas) < caben_y:
            tabla_filas.append([""] * caben_x)

        t = Table(
            tabla_filas,
            colWidths=[ANCHO, ANCHO],
            rowHeights=[ALTO] * len(tabla_filas),
            hAlign="CENTER",
        )
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), sep_x / 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), sep_x / 2),
            ("TOPPADDING", (0, 0), (-1, -1), sep_y / 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), sep_y / 2),
        ]))
        el.append(t)
        if i + caben_x * caben_y < len(fotochecks):
            el.append(PageBreak())

    doc.build(el)
    buf.seek(0)
    return buf.getvalue()


def _filas_alumnos_por_seccion(idsec):
    con = obtener_conexion()
    return con.execute(
        "SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno "
        "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id "
        "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
        "WHERE a.seccion_id=? AND a.activo=1 ORDER BY a.apellido_paterno,a.apellido_materno",
        (idsec,)
    ).fetchall()


def pdf_carnets_por_seccion(idsec):
    f = _filas_alumnos_por_seccion(idsec)
    return _render_carnets(f) if f else None


def pdf_carnets_seleccionados(ids, titulo=None):
    if not ids:
        return None
    con = obtener_conexion()
    ph = ",".join("?" * len(ids))
    f = con.execute(
        "SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno "
        "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id "
        "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
        "WHERE a.id IN (" + ph + ") AND a.activo=1 "
        "ORDER BY a.apellido_paterno,a.apellido_materno",
        ids
    ).fetchall()
    return _render_carnets(f, titulo) if f else None


def pdf_carnet_alumno(dni):
    con = obtener_conexion()
    f = con.execute(
        "SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno "
        "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id "
        "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
        "WHERE a.dni=? AND a.activo=1",
        (dni,)
    ).fetchall()
    return _render_carnets(f) if f else None


def pdf_resumen_alumno(idal):
    d = perfil_alumno_datos(idal)
    if not d:
        return None
    al = d["alumno"]
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30,
        onFirstPage=_marca_agua, onLaterPages=_marca_agua
    )
    est = getSampleStyleSheet()
    el = []
    el.append(Paragraph("<b>Reporte de Asistencia</b>", est["Heading1"]))
    el.append(Paragraph("I.E. Yarinacocha", est["Normal"]))
    el.append(Spacer(1, 20))
    nombre = al['apellido_paterno'] + " " + (al['apellido_materno'] or "") + ", " + al['nombres']
    el.append(Paragraph("<b>Alumno:</b> " + nombre, est["Normal"]))
    el.append(Paragraph("<b>DNI:</b> " + al['dni'], est["Normal"]))
    el.append(Paragraph("<b>Grado:</b> " + al['grado'] + " " + al['seccion'] + " - " + al['turno'], est["Normal"]))
    el.append(Paragraph("<b>Apoderado:</b> " + (al['nombre_apoderado'] or "-"), est["Normal"]))
    el.append(Paragraph("<b>Telefono:</b> " + (al['telefono_apoderado'] or "-"), est["Normal"]))
    el.append(Spacer(1, 15))
    el.append(Paragraph("<b>Resumen</b>", est["Heading2"]))
    resumen = [["Puntuales", "Tardanzas", "Faltas", "Reforzamiento", "Tard. injust."],
               [d["total_puntuales"], d["total_tardanzas"], d["total_faltas"],
                d["total_ref_asistio"], d["tard_injust"]]]
    t = Table(resumen)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(C_NARANJA)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey)
    ]))
    el.append(t)
    el.append(Spacer(1, 20))
    el.append(Paragraph("<b>Ultimas asistencias</b>", est["Heading2"]))
    if not d["asistencias"].empty:
        datos = [["Fecha", "Tipo", "Estado", "Justificada"]]
        for _, r in d["asistencias"].head(30).iterrows():
            datos.append([r["fecha"], r["tipo"], r["estado"], "Si" if r["justificada"] else "No"])
        t2 = Table(datos, repeatRows=1)
        t2.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(C_NARANJA)),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 8)
        ]))
        el.append(t2)
    el.append(Spacer(1, 30))
    el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"), est["Normal"]))
    doc.build(el)
    buf.seek(0)
    return buf.getvalue()


def df_a_xlsx(df, hoja="Datos"):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=hoja)
    buf.seek(0)
    return buf.getvalue()


def df_a_xlsx_multilhoja(hojas):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for n, df in hojas.items():
            df.to_excel(w, index=False, sheet_name=n[:31])
    buf.seek(0)
    return buf.getvalue()


# ============================================================
# ESTILOS
# ============================================================
def aplicar_estilos():
    st.markdown("""
    <style>
    /* ============================================================
       DISENO MODERNO 2026
       Respeta el tema de Streamlit (claro / oscuro).
       Usa variables nativas: --background-color, --text-color,
       --primary-color, --secondary-background-color.
       ============================================================ */

    :root {
        --accent: #E65100;
        --accent-hover: #BF360C;
        --accent-soft: color-mix(in srgb, var(--accent) 12%, transparent);
        --accent-soft-2: color-mix(in srgb, var(--accent) 20%, transparent);

        --ok: #22C55E;
        --ok-soft: color-mix(in srgb, var(--ok) 14%, transparent);
        --warn: #F59E0B;
        --warn-soft: color-mix(in srgb, var(--warn) 14%, transparent);
        --danger: #EF4444;
        --danger-soft: color-mix(in srgb, var(--danger) 14%, transparent);
        --info: #3B82F6;
        --info-soft: color-mix(in srgb, var(--info) 14%, transparent);

        --r-xs: 6px;
        --r-sm: 8px;
        --r-md: 10px;
        --r-lg: 14px;
        --r-xl: 18px;

        --ease: cubic-bezier(0.22, 1, 0.36, 1);
        --t: 140ms;
    }

    /* ============================================================
       TIPOGRAFIA
       ============================================================ */
    html, body, [class*="css"], .stApp {
        font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI",
                     Roboto, "Helvetica Neue", sans-serif !important;
        font-feature-settings: "cv02", "cv03", "cv04", "cv11", "ss01";
        -webkit-font-smoothing: antialiased;
        -moz-osx-font-smoothing: grayscale;
        letter-spacing: -0.012em;
    }

    h1, h2, h3, h4, h5 {
        font-weight: 700 !important;
        letter-spacing: -0.025em !important;
        line-height: 1.2 !important;
        color: var(--text-color);
    }
    h1 { font-size: 1.7rem !important; margin: 0 0 0.35rem 0 !important; }
    h2 { font-size: 1.3rem !important; margin: 1.5rem 0 0.7rem 0 !important; }
    h3 { font-size: 1.05rem !important; margin: 1.2rem 0 0.5rem 0 !important; }
    h4 { font-size: 0.95rem !important; margin: 1rem 0 0.4rem 0 !important; }

    p, span, label, li, td, th {
        color: var(--text-color);
    }

    code, pre {
        font-family: "JetBrains Mono", "SF Mono", "Menlo", monospace !important;
        font-size: 0.86em !important;
        border-radius: var(--r-xs);
    }

    /* ============================================================
       SCROLLBAR
       ============================================================ */
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb {
        background: color-mix(in srgb, var(--text-color) 18%, transparent);
        border-radius: 10px;
        border: 3px solid transparent;
        background-clip: padding-box;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: color-mix(in srgb, var(--text-color) 32%, transparent);
        background-clip: padding-box;
    }

    /* ============================================================
       SIDEBAR
       ============================================================ */
    section[data-testid="stSidebar"] {
        border-right: 1px solid color-mix(in srgb, var(--text-color) 8%, transparent);
    }
    section[data-testid="stSidebar"] > div:first-child {
        padding-top: 14px;
    }
    section[data-testid="stSidebar"] .stRadio > label { display: none; }
    section[data-testid="stSidebar"] .stRadio label {
        border-radius: var(--r-sm) !important;
        padding: 8px 12px !important;
        font-weight: 500 !important;
        font-size: 13.5px !important;
        letter-spacing: -0.008em !important;
        color: color-mix(in srgb, var(--text-color) 72%, transparent) !important;
        transition: background var(--t) var(--ease),
                    color var(--t) var(--ease);
        margin: 1px 0 !important;
    }
    section[data-testid="stSidebar"] .stRadio label:hover {
        background: color-mix(in srgb, var(--text-color) 6%, transparent) !important;
        color: var(--text-color) !important;
    }
    section[data-testid="stSidebar"] .stRadio label:has(input:checked) {
        background: var(--accent-soft) !important;
        color: var(--accent) !important;
        font-weight: 600 !important;
    }
    section[data-testid="stSidebar"] .stRadio input { display: none; }

    /* Encabezado de usuario */
    .encabezado-sidebar {
        background: color-mix(in srgb, var(--text-color) 4%, transparent);
        border: 1px solid color-mix(in srgb, var(--text-color) 8%, transparent);
        padding: 18px 14px;
        margin: 6px 10px 14px 10px;
        border-radius: var(--r-lg);
        text-align: center;
    }
    .encabezado-sidebar .avatar {
        width: 54px;
        height: 54px;
        border-radius: 50%;
        background: linear-gradient(135deg,
                    color-mix(in srgb, var(--accent) 90%, white),
                    var(--accent-hover));
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 20px;
        font-weight: 700;
        color: #FFFFFF;
        margin: 0 auto 12px auto;
        box-shadow: 0 4px 14px var(--accent-soft-2),
                    inset 0 0 0 1px color-mix(in srgb, white 20%, transparent);
        letter-spacing: -0.02em;
    }
    .encabezado-sidebar .nombre {
        font-size: 13.5px;
        font-weight: 700;
        letter-spacing: -0.015em;
        color: var(--text-color);
    }
    .encabezado-sidebar .rol {
        display: inline-block;
        margin-top: 8px;
        padding: 3px 10px;
        background: color-mix(in srgb, var(--text-color) 8%, transparent);
        border: 1px solid color-mix(in srgb, var(--text-color) 6%, transparent);
        border-radius: 999px;
        font-size: 9.5px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.09em;
        color: color-mix(in srgb, var(--text-color) 70%, transparent) !important;
    }

    /* ============================================================
       INPUTS
       ============================================================ */
    .stTextInput input,
    .stNumberInput input,
    .stDateInput input,
    .stTimeInput input,
    .stTextArea textarea,
    .stSelectbox > div > div {
        border-radius: var(--r-md) !important;
        border: 1px solid color-mix(in srgb, var(--text-color) 10%, transparent) !important;
        background: color-mix(in srgb, var(--text-color) 3%, transparent) !important;
        color: var(--text-color) !important;
        min-height: 42px;
        font-size: 14px !important;
        letter-spacing: -0.008em;
        transition: border-color var(--t) var(--ease),
                    box-shadow var(--t) var(--ease),
                    background var(--t) var(--ease);
    }
    .stTextInput input:hover,
    .stNumberInput input:hover,
    .stDateInput input:hover,
    .stTimeInput input:hover,
    .stTextArea textarea:hover,
    .stSelectbox > div > div:hover {
        border-color: color-mix(in srgb, var(--text-color) 18%, transparent) !important;
    }
    .stTextInput input:focus,
    .stNumberInput input:focus,
    .stDateInput input:focus,
    .stTimeInput input:focus,
    .stTextArea textarea:focus,
    .stSelectbox > div > div:focus-within {
        border-color: var(--accent) !important;
        background: color-mix(in srgb, var(--text-color) 1%, transparent) !important;
        box-shadow: 0 0 0 3px var(--accent-soft) !important;
        outline: none !important;
    }
    .stTextInput label,
    .stNumberInput label,
    .stDateInput label,
    .stTimeInput label,
    .stTextArea label,
    .stSelectbox label {
        font-size: 12px !important;
        font-weight: 600 !important;
        letter-spacing: -0.005em !important;
        color: color-mix(in srgb, var(--text-color) 75%, transparent) !important;
        text-transform: none !important;
    }

    /* ============================================================
       BOTONES
       ============================================================ */
    .stButton > button,
    .stFormSubmitButton > button,
    .stDownloadButton > button {
        border-radius: var(--r-md) !important;
        font-weight: 600 !important;
        font-size: 13.5px !important;
        letter-spacing: -0.008em !important;
        border: 1px solid color-mix(in srgb, var(--text-color) 10%, transparent) !important;
        background: color-mix(in srgb, var(--text-color) 4%, transparent) !important;
        color: var(--text-color) !important;
        padding: 9px 16px !important;
        min-height: 40px;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
        transition: background var(--t) var(--ease),
                    border-color var(--t) var(--ease),
                    transform 80ms var(--ease),
                    box-shadow var(--t) var(--ease);
    }
    .stButton > button:hover,
    .stFormSubmitButton > button:hover,
    .stDownloadButton > button:hover {
        background: color-mix(in srgb, var(--text-color) 8%, transparent) !important;
        border-color: color-mix(in srgb, var(--text-color) 18%, transparent) !important;
        box-shadow: 0 2px 6px rgba(0, 0, 0, 0.06);
    }
    .stButton > button:active,
    .stFormSubmitButton > button:active,
    .stDownloadButton > button:active {
        transform: scale(0.985);
    }

    .stButton > button[kind="primary"],
    .stFormSubmitButton > button[kind="primary"] {
        background: linear-gradient(180deg, var(--accent), var(--accent-hover)) !important;
        color: #FFFFFF !important;
        border-color: color-mix(in srgb, var(--accent) 60%, black) !important;
        box-shadow: 0 1px 2px var(--accent-soft-2),
                    inset 0 1px 0 color-mix(in srgb, white 18%, transparent);
    }
    .stButton > button[kind="primary"]:hover,
    .stFormSubmitButton > button[kind="primary"]:hover {
        background: linear-gradient(180deg, var(--accent-hover), var(--accent-hover)) !important;
        box-shadow: 0 4px 14px var(--accent-soft-2),
                    inset 0 1px 0 color-mix(in srgb, white 18%, transparent);
    }

    /* ============================================================
       METRICAS
       ============================================================ */
    div[data-testid="stMetric"] {
        background: color-mix(in srgb, var(--text-color) 3.5%, transparent);
        border: 1px solid color-mix(in srgb, var(--text-color) 8%, transparent);
        border-radius: var(--r-lg);
        padding: 18px 20px !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
        transition: border-color var(--t) var(--ease),
                    background var(--t) var(--ease),
                    box-shadow var(--t) var(--ease);
    }
    div[data-testid="stMetric"]:hover {
        border-color: color-mix(in srgb, var(--text-color) 14%, transparent);
        background: color-mix(in srgb, var(--text-color) 5%, transparent);
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
    }
    div[data-testid="stMetric"] label {
        font-size: 10.5px !important;
        font-weight: 700 !important;
        text-transform: uppercase;
        letter-spacing: 0.10em !important;
        color: color-mix(in srgb, var(--text-color) 55%, transparent) !important;
    }
    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
        font-size: 26px !important;
        font-weight: 700 !important;
        letter-spacing: -0.03em !important;
        line-height: 1.1 !important;
        color: var(--text-color) !important;
    }

    /* ============================================================
       TABS
       ============================================================ */
    .stTabs [data-baseweb="tab-list"] {
        gap: 4px;
        background: transparent;
        border-bottom: 1px solid color-mix(in srgb, var(--text-color) 8%, transparent);
        padding: 0;
        overflow-x: auto;
        flex-wrap: nowrap;
        scrollbar-width: none;
    }
    .stTabs [data-baseweb="tab-list"]::-webkit-scrollbar { display: none; }
    .stTabs [data-baseweb="tab"] {
        font-weight: 500 !important;
        font-size: 13.5px !important;
        letter-spacing: -0.008em !important;
        padding: 11px 14px !important;
        color: color-mix(in srgb, var(--text-color) 60%, transparent) !important;
        background: transparent !important;
        border-radius: 0 !important;
        white-space: nowrap;
        border-bottom: 2px solid transparent !important;
        transition: color var(--t) var(--ease);
    }
    .stTabs [data-baseweb="tab"]:hover {
        color: var(--text-color) !important;
    }
    .stTabs [aria-selected="true"] {
        color: var(--accent) !important;
        font-weight: 600 !important;
        border-bottom: 2px solid var(--accent) !important;
    }
    .stTabs [data-baseweb="tab-highlight"] { display: none !important; }
    .stTabs [data-baseweb="tab-border"] { display: none !important; }

    /* ============================================================
       EXPANDER
       ============================================================ */
    .streamlit-expanderHeader,
    details summary {
        border: 1px solid color-mix(in srgb, var(--text-color) 8%, transparent) !important;
        border-radius: var(--r-md) !important;
        font-weight: 600 !important;
        font-size: 13.5px !important;
        letter-spacing: -0.008em !important;
        padding: 12px 16px !important;
        background: color-mix(in srgb, var(--text-color) 3%, transparent) !important;
        color: var(--text-color) !important;
        transition: background var(--t) var(--ease),
                    border-color var(--t) var(--ease);
    }
    .streamlit-expanderHeader:hover,
    details summary:hover {
        background: color-mix(in srgb, var(--text-color) 5.5%, transparent) !important;
        border-color: color-mix(in srgb, var(--text-color) 14%, transparent) !important;
    }
    details[open] > summary {
        border-bottom-left-radius: 0 !important;
        border-bottom-right-radius: 0 !important;
    }

    /* ============================================================
       DATAFRAMES
       ============================================================ */
    div[data-testid="stDataFrame"],
    div[data-testid="stTable"] {
        border-radius: var(--r-lg);
        overflow: hidden;
        border: 1px solid color-mix(in srgb, var(--text-color) 8%, transparent);
    }
    div[data-testid="stDataFrame"] thead tr th {
        background: color-mix(in srgb, var(--text-color) 4.5%, transparent) !important;
        font-weight: 700 !important;
        font-size: 11px !important;
        text-transform: uppercase;
        letter-spacing: 0.06em !important;
        color: color-mix(in srgb, var(--text-color) 60%, transparent) !important;
    }

    /* ============================================================
       SCAN HEADER
       ============================================================ */
    .scan-header {
        background: color-mix(in srgb, var(--text-color) 3.5%, transparent);
        border: 1px solid color-mix(in srgb, var(--text-color) 8%, transparent);
        padding: 22px 24px;
        border-radius: var(--r-xl);
        margin-bottom: 16px;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
    }
    .scan-header .scan-titulo {
        font-size: 20px;
        font-weight: 700;
        letter-spacing: -0.025em;
        color: var(--text-color);
    }
    .scan-header .scan-sub {
        font-size: 13px;
        color: color-mix(in srgb, var(--text-color) 58%, transparent);
        margin-top: 4px;
        letter-spacing: -0.008em;
    }
    .scan-ultimos {
        font-size: 10.5px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        color: color-mix(in srgb, var(--text-color) 55%, transparent);
        margin: 22px 0 12px 0;
        padding-bottom: 8px;
        border-bottom: 1px solid color-mix(in srgb, var(--text-color) 8%, transparent);
    }

    /* ============================================================
       MENSAJES QR
       ============================================================ */
    .qr-msg {
        padding: 14px 16px;
        border-radius: var(--r-md);
        margin: 8px 0;
        background: color-mix(in srgb, var(--text-color) 3%, transparent);
        border: 1px solid color-mix(in srgb, var(--text-color) 8%, transparent);
        border-left: 3px solid color-mix(in srgb, var(--text-color) 30%, transparent);
        transition: background var(--t) var(--ease);
    }
    .qr-msg:hover {
        background: color-mix(in srgb, var(--text-color) 5%, transparent);
    }
    .qr-texto {
        font-size: 13.5px;
        font-weight: 500;
        line-height: 1.5;
        letter-spacing: -0.008em;
        color: var(--text-color);
    }
    .qr-puntual   { border-left-color: var(--ok); }
    .qr-tardanza  { border-left-color: var(--warn); }
    .qr-derivado  { border-left-color: var(--warn); }
    .qr-retenido  { border-left-color: var(--danger); font-weight: 700; }
    .qr-refuerzo  { border-left-color: var(--info); }
    .qr-bloqueado {
        border-left-color: var(--danger);
        background: var(--danger-soft);
        font-weight: 800;
    }
    .qr-error { border-left-color: color-mix(in srgb, var(--text-color) 30%, transparent); }

    /* ============================================================
       PERFIL
       ============================================================ */
    .perfil-card {
        background: color-mix(in srgb, var(--text-color) 3.5%, transparent);
        border: 1px solid color-mix(in srgb, var(--text-color) 8%, transparent);
        border-radius: var(--r-xl);
        padding: 26px 28px;
        margin-bottom: 18px;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
    }
    .perfil-nombre {
        font-size: 22px;
        font-weight: 700;
        letter-spacing: -0.028em;
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 10px;
        color: var(--text-color);
    }
    .perfil-meta {
        font-size: 13px;
        margin-top: 8px;
        line-height: 1.7;
        color: color-mix(in srgb, var(--text-color) 68%, transparent);
    }
    .perfil-meta b {
        color: var(--text-color);
        font-weight: 600;
    }
    .perfil-badge {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 999px;
        font-size: 9.5px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.09em;
        background: color-mix(in srgb, var(--text-color) 10%, transparent);
        color: color-mix(in srgb, var(--text-color) 70%, transparent);
    }
    .badge-bloqueado { background: var(--danger-soft); color: var(--danger); }
    .badge-observado { background: var(--warn-soft); color: var(--warn); }
    .badge-ok        { background: var(--ok-soft); color: var(--ok); }

    .perfil-resumen {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(115px, 1fr));
        gap: 10px;
        margin-top: 20px;
    }
    .perfil-resumen-item {
        background: color-mix(in srgb, var(--text-color) 5%, transparent);
        border: 1px solid color-mix(in srgb, var(--text-color) 7%, transparent);
        border-radius: var(--r-md);
        padding: 14px 10px;
        text-align: center;
    }
    .perfil-resumen-item .num {
        font-size: 22px;
        font-weight: 700;
        letter-spacing: -0.03em;
        color: var(--text-color);
    }
    .perfil-resumen-item .lbl {
        font-size: 9.5px;
        text-transform: uppercase;
        letter-spacing: 0.10em;
        font-weight: 700;
        margin-top: 4px;
        color: color-mix(in srgb, var(--text-color) 55%, transparent);
    }

    /* ============================================================
       BOTON SELECCIONADO
       ============================================================ */
    .btn-sel .stButton > button {
        background: linear-gradient(180deg,
                    color-mix(in srgb, var(--accent) 90%, white),
                    var(--accent)) !important;
        color: #FFFFFF !important;
        border-color: var(--accent) !important;
        box-shadow: 0 4px 14px var(--accent-soft-2);
    }
    .btn-sel .stButton > button:hover {
        background: linear-gradient(180deg,
                    var(--accent),
                    var(--accent-hover)) !important;
        border-color: var(--accent-hover) !important;
    }

    /* ============================================================
       LOGIN
       ============================================================ */
    .login-form .stTextInput input {
        border: none !important;
        border-bottom: 1px solid color-mix(in srgb, var(--text-color) 18%, transparent) !important;
        border-radius: 0 !important;
        background: transparent !important;
        box-shadow: none !important;
        padding-left: 0 !important;
    }
    .login-form .stTextInput input:focus {
        border-bottom-color: var(--accent) !important;
        box-shadow: none !important;
    }

    /* ============================================================
       SEPARADOR
       ============================================================ */
    hr {
        border: none;
        height: 1px;
        background: color-mix(in srgb, var(--text-color) 8%, transparent);
        margin: 24px 0;
    }

    /* ============================================================
       ALERTAS
       ============================================================ */
    div[data-testid="stAlert"] {
        border-radius: var(--r-md);
        border-width: 1px;
        font-size: 13.5px;
        letter-spacing: -0.008em;
    }

    /* ============================================================
       MOBILE
       ============================================================ */
    @media (max-width: 768px) {
        h1 { font-size: 1.4rem !important; }
        h2 { font-size: 1.15rem !important; }
        .stButton > button,
        .stFormSubmitButton > button,
        .stDownloadButton > button {
            width: 100% !important;
            padding: 13px 18px !important;
            font-size: 14.5px !important;
            min-height: 46px;
        }
        .perfil-nombre { font-size: 17px; }
        .perfil-resumen { grid-template-columns: repeat(2, 1fr); gap: 10px; }
        .encabezado-sidebar .avatar { width: 48px; height: 48px; font-size: 18px; }
    }
    </style>
    """, unsafe_allow_html=True)

def filtros_grado_seccion_nombre(clave, placeholder="Buscar"):
    grados = listar_grados()
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        ops = [{"id": None, "nombre": "Todos"}] + grados
        g = st.selectbox("Grado", ops, format_func=lambda x: x["nombre"], key=clave + "_g")
    with c2:
        secs = ([{"id": None, "nombre": "Todas"}] + secciones_por_grado(g["id"])) if (g and g["id"]) else [{"id": None, "nombre": "Todas"}]
        s = st.selectbox("Seccion", secs, format_func=lambda x: x["nombre"], key=clave + "_s")
    with c3:
        t = st.text_input("Buscar", placeholder=placeholder, key=clave + "_t")
    return (g["id"] if g else None, s["id"] if (g and g["id"] and s) else None, t.strip())


# ============================================================
# LOGIN
# ============================================================
def vista_login():
    st.markdown("""
    <div style="text-align:center; margin-top:100px; margin-bottom:40px;">
        <h1 style="font-size:42px; margin-bottom:0; border:none; letter-spacing:-1px;">asisyarina</h1>
    </div>
    """, unsafe_allow_html=True)
    _, centro, _ = st.columns([1, 1, 1])
    with centro:
        st.markdown('<div class="login-form">', unsafe_allow_html=True)
        with st.form("login"):
            usuario = st.text_input("Usuario", placeholder="tu usuario")
            password = st.text_input("Contrasena", type="password", placeholder="tu contrasena")
            enviado = st.form_submit_button("Ingresar", type="primary", width='stretch')
            if enviado:
                datos, err = autenticar(usuario, password)
                if datos:
                    st.session_state["user"] = datos
                    auditar(datos["usuario"], "Login")
                    st.rerun()
                else:
                    st.error(err or "Credenciales incorrectas")
        st.markdown('</div>', unsafe_allow_html=True)


def vista_cambio_password_obligatorio():
    usuario = st.session_state["user"]
    st.title("Cambio obligatorio de contrasena")
    st.warning("Tu cuenta tiene una contrasena temporal.")
    _, centro, _ = st.columns([1, 1.2, 1])
    with centro:
        with st.form("cambio_pwd"):
            nueva = st.text_input("Nueva contrasena", type="password")
            confirmar = st.text_input("Confirmar", type="password")
            ok = st.form_submit_button("Cambiar", type="primary", width='stretch')
        if ok:
            if len(nueva) < 6:
                st.error("Minimo 6 caracteres.")
            elif nueva != confirmar:
                st.error("No coinciden.")
            else:
                escribir("UPDATE usuarios SET password=?,debe_cambiar_password=0 WHERE id=?",
                         (hashear_password(nueva), usuario["id"]))
                st.session_state["user"]["debe_cambiar_password"] = 0
                auditar(usuario["usuario"], "Cambio pwd obligatorio")
                st.rerun()


# ============================================================
# MI CUENTA
# ============================================================
def vista_mi_cuenta():
    st.title("Mi cuenta")
    usuario = st.session_state["user"]
    inicial = (usuario["nombres"] or "?")[0].upper()
    st.markdown("""
    <div class="perfil-card">
        <div class="perfil-nombre">
            <div style="width:60px;height:60px;border-radius:50%;background:#808080;display:inline-flex;align-items:center;justify-content:center;font-size:24px;font-weight:700;color:white;margin-right:12px;">""" + inicial + """</div>
            """ + usuario['nombres'] + """
        </div>
        <div class="perfil-meta"><b>Usuario:</b> """ + usuario['usuario'] + """ &nbsp;|&nbsp; <b>Rol:</b> """ + usuario['rol'] + """</div>
        <div class="perfil-meta"><b>Ultimo login:</b> """ + (usuario.get('ultimo_login') or 'Nunca') + """ &nbsp;|&nbsp; <b>IP:</b> """ + (usuario.get('ultimo_ip') or '-') + """</div>
    </div>
    """, unsafe_allow_html=True)

    if usuario["rol"] == "Auxiliar":
        con = obtener_conexion()
        df_secs = pd.read_sql("""
            SELECT g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
            FROM auxiliar_secciones a
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE a.usuario_id=?
            ORDER BY g.nombre, s.nombre
        """, con, params=[usuario["id"]])
        st.markdown("---")
        st.subheader("Mis secciones asignadas")
        if df_secs.empty:
            st.warning("No tienes secciones asignadas.")
        else:
            st.dataframe(df_secs, width='stretch')

    st.markdown("---")
    st.subheader("Mi actividad reciente")
    con = obtener_conexion()
    df_act = pd.read_sql(
        "SELECT accion,fecha FROM auditoria WHERE usuario=? ORDER BY id DESC LIMIT 10",
        con, params=[usuario["usuario"]]
    )
    if df_act.empty:
        st.info("Sin actividad.")
    else:
        st.dataframe(df_act, width='stretch')

    st.markdown("---")
    st.subheader("Cambiar contrasena")
    with st.form("cambiar_mi_pwd"):
        actual = st.text_input("Contrasena actual", type="password")
        nueva = st.text_input("Nueva contrasena", type="password")
        confirmar = st.text_input("Confirmar nueva contrasena", type="password")
        if st.form_submit_button("Cambiar contrasena", type="primary"):
            if not verificar_password(actual, usuario["password"]):
                st.error("Contrasena actual incorrecta.")
            elif len(nueva) < 6:
                st.error("Minimo 6 caracteres.")
            elif nueva != confirmar:
                st.error("No coinciden.")
            else:
                nueva_hash = hashear_password(nueva)
                escribir("UPDATE usuarios SET password=? WHERE id=?", (nueva_hash, usuario["id"]))
                st.session_state["user"]["password"] = nueva_hash
                auditar(usuario["usuario"], "Cambio su contrasena")
                st.success("Contrasena actualizada.")

    st.markdown("---")
    if st.button("Cerrar sesion"):
        cerrar_sesion()
        st.rerun()


# ============================================================
# PUERTA
# ============================================================
def _puerta_escanear(usuario, fecha):
    con = obtener_conexion()
    esp = con.execute(
        "SELECT descripcion, hora_entrada, tipo FROM dias_especiales "
        "WHERE fecha=? AND activo=1 LIMIT 1", (fecha,)
    ).fetchone()

    # Feriado: bloquea todo
    if esp and esp["tipo"] == "feriado":
        st.info("Feriado / sin clases: " + esp['descripcion'] + ".")
        return

    # Fin de semana: solo permitir si hay dia especial tipo evento
    if es_fin_de_semana() and not (esp and esp["tipo"] == "evento"):
        st.warning("Hoy no es dia laboral.")
        return

    # Si hay dia especial tipo evento, mostrar info
    if esp and esp["tipo"] == "evento":
        st.info("Dia especial: " + (esp["descripcion"] or "") +
                " (entrada " + (esp["hora_entrada"] or "-") + ")")

    escaner_qr_continuo(key="puerta_qr")

def _render_mensaje_qr(msg):
    tipo = msg["tipo"]
    mensaje = msg["mensaje"]
    clase = {"PUNTUAL": "qr-puntual", "TARDANZA": "qr-tardanza",
             "REFORZAMIENTO": "qr-refuerzo", "BLOQUEADO": "qr-bloqueado",
             "INCIDENCIA": "qr-refuerzo", "ERROR": "qr-error"}.get(tipo, "qr-error")

    if tipo == "TARDANZA":
        acc = (msg.get("extra") or {}).get("accion")
        if acc == ACC_DERIVADO:
            mensaje += " -> Derivar a TOECE"
            clase = "qr-derivado"
        elif acc == ACC_RETENIDO:
            mensaje += " -> Retener hasta apoderado"
            clase = "qr-retenido"

    st.markdown(
        '<div class="qr-msg ' + clase + '"><div class="qr-texto">' +
        mensaje + '</div></div>',
        unsafe_allow_html=True
    )


def _procesar_escaneo(dni):
    u = st.session_state.get("user")
    if not u:
        return
    dni = str(dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni):
        return

    h = ahora().hour
    if h < 5 or h >= 20:
        return

    modo_codigo = st.session_state.get("_modo_camara_actual", "clases")
    tipo_inc_id = st.session_state.get("_modo_incidencia_tipo_id")
    desc_breve = st.session_state.get("_modo_incidencia_desc", "")

    ok, tipo, msg, extra = registrar_entrada(
        dni, u, origen="qr",
        modo_codigo=modo_codigo,
        tipo_incidencia_id=tipo_inc_id,
        descripcion_breve=desc_breve
    )

    if not ok and tipo == "ERROR":
        if "ya registro" in msg or "ya tiene" in msg:
            sonido = "duplicado"
        else:
            sonido = "error"
    elif tipo == "BLOQUEADO":
        sonido = "bloqueado"
    elif ok and tipo == "TARDANZA":
        sonido = "tardanza"
    elif ok and tipo == "INCIDENCIA":
        sonido = "error"
    elif ok:
        sonido = "puntual"
    else:
        sonido = "error"

    st.session_state.setdefault("_qr_mensajes", [])
    st.session_state["_qr_mensajes"].insert(0, {
        "dni": dni, "tipo": tipo, "mensaje": msg,
        "extra": extra, "ts": time.time()
    })
    st.session_state["_qr_mensajes"] = st.session_state["_qr_mensajes"][:10]

    contador = st.session_state.get("_qr_sonido_contador", 0) + 1
    st.session_state["_qr_sonido_contador"] = contador
    st.session_state["_qr_sonido_pendiente"] = {
        "kind": sonido, "nonce": contador, "ts": time.time(),
    }


def escaner_qr_continuo(key="qr_scanner"):
    st.markdown(
        '<div class="scan-header"><div class="scan-titulo">Escaneo QR</div>'
        '<div class="scan-sub">Apunta al codigo del alumno</div></div>',
        unsafe_allow_html=True
    )

    # Callback: aqui llega el DNI escaneado desde el componente
    def _on_scan(dni_nuevo=None, *args, **kwargs):
        if not dni_nuevo:
            return
        dni_str = str(dni_nuevo).strip()
        if not re.fullmatch(r"\d{8}", dni_str):
            return
        ult = st.session_state.get("_ultimo_qr_scan", {})
        if (ult.get("dni") == dni_str and
                (time.time() - ult.get("ts", 0)) < 1):
            return
        st.session_state["_ultimo_qr_scan"] = {"dni": dni_str, "ts": time.time()}
        _procesar_escaneo(dni_str)

    key_full = "qr_scanner_persistente"

    # Armar el ultimo mensaje para el toast flotante de arriba
    ultimo_mensaje = None
    mensajes = st.session_state.get("_qr_mensajes", [])
    if mensajes:
        m = mensajes[0]
        if (time.time() - m.get("ts", 0)) < 6:
            tipo = m.get("tipo", "ERROR")
            msg_txt = m.get("mensaje", "")
            extra = m.get("extra") or {}
            alumno = extra.get("alumno") or {}

            if alumno:
                nombre = (alumno.get("apellido_paterno", "") + " " +
                          (alumno.get("apellido_materno") or "") + ", " +
                          alumno.get("nombres", "")).strip(", ")
                detalle = (alumno.get("grado", "") + " " +
                           alumno.get("seccion", "") + " - " +
                           alumno.get("turno", ""))
            else:
                nombre = "(no identificado)"
                detalle = msg_txt

            if tipo == "PUNTUAL":
                kind = "puntual"; titulo = "PUNTUAL"
            elif tipo == "TARDANZA":
                kind = "tardanza"; titulo = "TARDANZA"
                accion = extra.get("accion", "")
                if accion:
                    detalle = detalle + " - " + accion
            elif tipo == "BLOQUEADO":
                kind = "bloqueado"; titulo = "BLOQUEADO"
                detalle = "Retener y llevar a TOECE"
            elif tipo == "INCIDENCIA":
                kind = "incidencia"; titulo = "INCIDENCIA REPORTADA"
            elif tipo == "REFORZAMIENTO":
                kind = "puntual"; titulo = "REFORZAMIENTO"
            else:
                if "ya registro" in msg_txt or "ya tiene" in msg_txt:
                    kind = "duplicado"; titulo = "YA REGISTRADO"
                elif "DNI no encontrado" in msg_txt:
                    kind = "error"; titulo = "NO ENCONTRADO"
                elif "Sin ventana" in msg_txt:
                    kind = "error"; titulo = "FUERA DE VENTANA"
                elif "Feriado" in msg_txt:
                    kind = "error"; titulo = "FERIADO"
                else:
                    kind = "error"; titulo = "AVISO"

            ultimo_mensaje = {
                "kind": kind,
                "titulo": titulo,
                "nombre": nombre,
                "detalle": detalle,
                "mensaje": msg_txt,
                "tipo": tipo,
                "extra": extra,
            }

    # Llamar al componente. El DNI llega via _on_scan.
    qr_scanner(key=key_full, on_scan=_on_scan, ultimo_mensaje=ultimo_mensaje)

    # Sonido: disparar el sonido correspondiente
    sp = st.session_state.get("_qr_sonido_pendiente")
    if sp and (time.time() - sp.get("ts", 0)) < 30:
        kind_js = sp["kind"]
        nonce = sp["nonce"]
        st.components.v1.html(f"""
            <script>
            (function() {{
                let tries = 0;
                const disparar = () => {{
                    tries++;
                    try {{
                        if (window.parent && typeof window.parent.__qrFeedback === 'function') {{
                            window.parent.__qrFeedback('{kind_js}');
                            return;
                        }}
                    }} catch(e) {{}}
                    try {{
                        const frames = document.querySelectorAll('iframe');
                        for (const f of frames) {{
                            try {{
                                const w = f.contentWindow;
                                if (w && typeof w.__qrFeedback === 'function') {{
                                    w.__qrFeedback('{kind_js}');
                                    return;
                                }}
                            }} catch(e) {{}}
                        }}
                    }} catch(e) {{}}
                    if (tries < 60) setTimeout(disparar, 100);
                }};
                disparar();
            }})();
            </script>
        """, height=0)
        st.session_state.pop("_qr_sonido_pendiente", None)

    # Lista de ultimos escaneos debajo del escaner
    if st.session_state.get("_qr_mensajes"):
        st.markdown('<div class="scan-ultimos">Ultimos escaneos</div>', unsafe_allow_html=True)
        for msg in st.session_state["_qr_mensajes"][:5]:
            _render_mensaje_qr(msg)

def _puerta_manual(usuario, fecha):
    if es_fin_de_semana():
        st.warning("Hoy no es dia laboral.")
        return
    ha = hora_corta()
    turnos_activos = []
    for t in listar_turnos():
        for v in listar_ventanas(t["id"]):
            if v["hora_apertura"] <= ha <= v["hora_cierre"]:
                turnos_activos.append({"turno_id": t["id"], "turno": t["nombre"], "ventana": v["nombre"]})
                break
    if not turnos_activos:
        st.info("No hay ventanas activas en este momento.")
        return
    st.caption("Ventanas activas: " + " | ".join([t["turno"] + " (" + t["ventana"] + ")" for t in turnos_activos]))
    st.warning("El modo manual es solo para emergencias. Usa el escaneo QR si puedes.")
    idsec = st.session_state.get("puerta_manual_seccion")
    if idsec:
        _puerta_manual_alumnos(idsec, usuario)
        return
    idg = st.session_state.get("puerta_manual_grado")
    if idg:
        _puerta_manual_secciones(idg, turnos_activos)
        return
    turnos_ids = [t["turno_id"] for t in turnos_activos]
    grados = listar_grados()
    grados_mostrar = []
    for g in grados:
        secs = secciones_por_grado(g["id"])
        secs_validas = [s for s in secs if s["turno_id"] in turnos_ids]
        if secs_validas:
            grados_mostrar.append({"grado": g, "n_secciones": len(secs_validas)})
    if not grados_mostrar:
        st.info("No hay grados con ventana activa.")
        return
    st.markdown("### Selecciona el grado")
    cols = st.columns(3)
    for i, item in enumerate(grados_mostrar):
        with cols[i % 3]:
            if st.button(item["grado"]["nombre"] + "  (" + str(item["n_secciones"]) + " secciones)",
                         width='stretch', key="pm_g_" + str(item["grado"]["id"])):
                st.session_state["puerta_manual_grado"] = item["grado"]["id"]
                st.rerun()


def _puerta_manual_secciones(idg, turnos_activos):
    if st.button("Regresar a grados", key="pm_volver_g"):
        st.session_state.pop("puerta_manual_grado", None)
        st.rerun()
    grados = listar_grados()
    g = next((x for x in grados if x["id"] == idg), None)
    if not g:
        st.session_state.pop("puerta_manual_grado", None)
        st.rerun()
        return
    st.markdown("### Secciones de " + g["nombre"])
    turnos_ids = [t["turno_id"] for t in turnos_activos]
    secs = [s for s in secciones_por_grado(idg) if s["turno_id"] in turnos_ids]
    if not secs:
        st.warning("Este grado no tiene secciones con ventana activa.")
        return
    cols = st.columns(3)
    for i, s in enumerate(secs):
        with cols[i % 3]:
            n = len(alumnos_de_seccion(s["id"]))
            if st.button(s["nombre"] + "  (" + str(n) + " alumnos)", width='stretch', key="pm_s_" + str(s["id"])):
                st.session_state["puerta_manual_seccion"] = s["id"]
                st.rerun()


def _puerta_manual_alumnos(idsec, usuario):
    if st.button("Regresar a secciones", key="pm_volver_s"):
        st.session_state.pop("puerta_manual_seccion", None)
        st.rerun()
    con = obtener_conexion()
    sec = con.execute("""
        SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
        WHERE s.id=?
    """, (idsec,)).fetchone()
    if not sec:
        st.session_state.pop("puerta_manual_seccion", None)
        st.rerun()
        return
    st.markdown("### " + sec['grado'] + " " + sec['seccion'] + " - Turno " + sec['turno'])
    df = alumnos_de_seccion(idsec)
    if df.empty:
        st.info("Sin alumnos.")
        return
    st.caption(str(len(df)) + " alumnos. Aprieta Marcar en cada uno que llegue.")
    for _, al in df.iterrows():
        c1, c2 = st.columns([5, 1])
        c1.write(al["nombre_completo"])
        if c2.button("Marcar", key="pm_m_" + str(al['id'])):
            _, _, msg, _ = registrar_entrada(al["dni"], usuario, origen="manual")
            st.toast(msg)


def vista_puerta():
    usuario = st.session_state["user"]
    fecha = hoy_str()
    st.markdown("""
    <div style="background:#E65100; padding:22px 28px; border-radius:10px; color:white; margin-bottom:20px;">
        <div style="font-size:26px; font-weight:700;">Control de Puerta</div>
        <div style="font-size:14px; opacity:0.9; margin-top:4px;">""" + fecha + """</div>
    </div>
    """, unsafe_allow_html=True)

    if "puerta_modo" not in st.session_state:
        st.session_state["puerta_modo"] = "Escanear QR"
    modo = st.radio("Modo", ["Escanear QR", "Manual"], key="puerta_modo",
                    horizontal=True, label_visibility="collapsed")

    if modo == "Escanear QR":
        modos = listar_modos_camara(solo_activos=True)
        if not modos:
            st.warning("No hay modos de camara configurados.")
            return
        etiquetas = [m["nombre"] for m in modos]
        idx_actual = 0
        actual = st.session_state.get("_modo_camara_actual", "clases")
        for i, m in enumerate(modos):
            if m["codigo"] == actual:
                idx_actual = i
                break
        sel = st.radio("Modo de camara", etiquetas, index=idx_actual,
                       horizontal=True, key="sel_modo_camara")
        m_sel = next(m for m in modos if m["nombre"] == sel)
        st.session_state["_modo_camara_actual"] = m_sel["codigo"]

        if m_sel["codigo"] == "incidencia":
            tipos = listar_tipos_incidencia(solo_activos=True)
            if tipos:
                ops = {t["nombre"]: t["id"] for t in tipos}
                t_sel = st.selectbox("Tipo de incidencia (opcional)",
                                     ["(Sin tipo)"] + list(ops.keys()),
                                     key="sel_tipo_inc")
                st.session_state["_modo_incidencia_tipo_id"] = ops.get(t_sel)
            desc = st.text_input("Descripcion breve (opcional)",
                                 key="desc_inc_breve")
            st.session_state["_modo_incidencia_desc"] = desc
            st.info("TOECE recibira el aviso y completara el resto.")

        _puerta_escanear(usuario, fecha)
    else:
        _puerta_manual(usuario, fecha)


# ============================================================
# TOECE
# ============================================================
def _toece_justificar_permiso(usuario):
    st.caption("Justificar: solo Faltas o Tardanzas. Hasta 24h antes o 24h despues. Permisos: hasta 7 dias.")
    sub_tabs = st.tabs(["Justificar asistencia", "Crear permiso", "Listar permisos"])
    with sub_tabs[0]:
        idg, ids, texto = filtros_grado_seccion_nombre("jp_just")
        df = buscar_alumnos(texto, idg, ids, limite=100)
        if df.empty:
            st.info("Busca un alumno para justificar.")
        else:
            ops = {r['nombre_completo'] + " (" + r['dni'] + ")": r["id"] for _, r in df.iterrows()}
            sel = st.selectbox("Alumno", list(ops.keys()))
            con = obtener_conexion()
            df_as = pd.read_sql(
                "SELECT id, fecha, tipo, estado FROM asistencias "
                "WHERE alumno_id=? AND justificada=0 AND estado IN ('Falta','Tardanza') "
                "AND fecha >= date('now','-1 day') AND fecha <= date('now','+1 day') ORDER BY fecha DESC",
                con, params=[ops[sel]]
            )
            if df_as.empty:
                st.info("Este alumno no tiene faltas ni tardanzas justificables.")
            else:
                ops_as = {r['fecha'] + " - " + r['tipo'] + " (" + r['estado'] + ")": r["id"] for _, r in df_as.iterrows()}
                with st.form("just_form"):
                    sel_as = st.selectbox("Asistencia a justificar", list(ops_as.keys()))
                    obs = st.text_input("Observacion (obligatoria)")
                    pwd = st.text_input("Contrasena de Admin o TOECE", type="password")
                    sub = st.form_submit_button("Justificar", type="primary")
                if sub:
                    if not obs.strip():
                        st.error("La observacion es obligatoria.")
                    elif not pwd:
                        st.error("Ingresa la contrasena.")
                    elif not verificar_password_critica(pwd):
                        st.error("Contrasena incorrecta.")
                    else:
                        ok, msg = justificar_asistencia(ops_as[sel_as], obs, usuario)
                        if ok:
                            st.toast(msg)
                            st.rerun()
                        else:
                            st.error(msg)
    with sub_tabs[1]:
        idg, ids, texto = filtros_grado_seccion_nombre("jp_perm")
        df = buscar_alumnos(texto, idg, ids, limite=100)
        if df.empty:
            st.info("Busca un alumno para crear permiso.")
        else:
            ops = {r['nombre_completo'] + " (" + r['dni'] + ")": r["id"] for _, r in df.iterrows()}
            with st.form("permiso_form"):
                sel = st.selectbox("Alumno", list(ops.keys()))
                fi = st.date_input("Fecha inicio", value=ahora().date())
                ff = st.date_input("Fecha fin", value=ahora().date())
                st.caption("Maximo " + str(MAX_DIAS_PERMISO) + " dias.")
                mot = st.text_input("Motivo del permiso (obligatorio)")
                pwd = st.text_input("Contrasena de Admin o TOECE", type="password")
                sub = st.form_submit_button("Registrar permiso", type="primary")
            if sub:
                if not mot.strip():
                    st.error("El motivo es obligatorio.")
                elif not pwd:
                    st.error("Ingresa la contrasena.")
                elif not verificar_password_critica(pwd):
                    st.error("Contrasena incorrecta.")
                else:
                    ok, msg = crear_permiso(ops[sel], fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"), mot.strip(), usuario)
                    if ok:
                        st.toast(msg)
                        st.rerun()
                    else:
                        st.error(msg)
    with sub_tabs[2]:
        df = listar_permisos(solo_activos=True)
        if df.empty:
            st.info("Sin permisos activos.")
        else:
            st.dataframe(df, width='stretch')
            st.download_button("Excel", df_a_xlsx(df), "permisos.xlsx")


def _ficha_incidencia(inc_id):
    inc = obtener_incidencia(inc_id)
    if not inc:
        st.warning("Incidencia no encontrada.")
        st.session_state.pop("_incidencia_abierta", None)
        return

    st.markdown("---")
    st.markdown("## Ficha de incidencia #" + str(inc_id))

    nombre = (inc['apellido_paterno'] + " " + (inc['apellido_materno'] or "") + ", " + inc['nombres']).strip(", ")
    st.markdown("**Alumno:** " + nombre + " | DNI: " + inc['dni'])
    st.markdown("**Grado:** " + inc['grado'] + " " + inc['seccion'] + " | Turno: " + inc['turno'])
    st.markdown("**Apoderado:** " + (inc['nombre_apoderado'] or '-') + " | Tel: " + (inc['telefono_apoderado'] or '-'))
    st.markdown("**Reportado por:** " + (inc['reportado_por'] or '-') + " el " + inc['fecha'] + " " + inc['hora'])
    st.markdown("**Estado actual:** " + inc['estado'])

    if inc['tipo_id']:
        n_prev = contar_incidencias_por_tipo_alumno(inc['alumno_id'], inc['tipo_id'])
        tiene_acta = tiene_acta_firmada_por_tipo(inc['alumno_id'], inc['tipo_id'])
        if n_prev > 1 or tiene_acta:
            st.error("REINCIDENTE: este alumno ya tiene " + str(n_prev) + " incidencia(s) de este tipo. " +
                     ("Ya tiene acta firmada por este tipo. No se recomienda perdonar." if tiene_acta else ""))

    st.markdown("---")
    st.markdown("### Campos que completa TOECE")
    tipos = listar_tipos_incidencia(solo_activos=True)
    lugares = listar_lugares_incidencia(solo_activos=True)
    ops_t = {t["nombre"]: t["id"] for t in tipos}
    ops_l = {l["nombre"]: l["id"] for l in lugares}
    idx_t = list(ops_t.values()).index(inc['tipo_id']) if inc['tipo_id'] in ops_t.values() else 0
    idx_l = list(ops_l.values()).index(inc['lugar_id']) if inc['lugar_id'] in ops_l.values() else 0

    tipo_sel = st.selectbox("Tipo de incidencia", list(ops_t.keys()), index=idx_t, key="fich_tipo")
    lugar_sel = st.selectbox("Lugar", list(ops_l.keys()), index=idx_l, key="fich_lugar")
    antecedentes = st.checkbox("Tiene antecedentes", value=bool(inc['antecedentes']), key="fich_ante")
    desc_hechos = st.text_area("Descripcion de los hechos", value=inc['descripcion_hechos'] or "", key="fich_desc")

    if st.button("Guardar campos", type="primary", key="fich_guardar"):
        ok, msg = actualizar_campos_incidencia(
            inc_id, ops_t[tipo_sel], ops_l[lugar_sel],
            antecedentes, desc_hechos, st.session_state["user"]
        )
        if ok:
            st.toast(msg)
            st.rerun()
        else:
            st.error(msg)

    st.markdown("---")
    st.markdown("### Acuerdos y compromisos")
    with st.form("form_acuerdo"):
        det = st.text_area("Acuerdos y compromisos", key="ac_det")
        if st.form_submit_button("Guardar acuerdo"):
            ok, msg = guardar_seguimiento_incidencia(inc_id, "acuerdo", det, st.session_state["user"])
            if ok:
                st.toast(msg)
                st.rerun()
            else:
                st.error(msg)

    st.markdown("### Intervencion del docente")
    with st.form("form_interv"):
        det = st.text_area("Que hizo el docente", key="int_det")
        if st.form_submit_button("Guardar intervencion"):
            ok, msg = guardar_seguimiento_incidencia(inc_id, "intervencion_docente", det, st.session_state["user"])
            if ok:
                st.toast(msg)
                st.rerun()
            else:
                st.error(msg)

    st.markdown("### Observaciones")
    with st.form("form_obs_inc"):
        det = st.text_area("Observacion", key="obs_det")
        if st.form_submit_button("Guardar observacion"):
            ok, msg = guardar_seguimiento_incidencia(inc_id, "observacion", det, st.session_state["user"])
            if ok:
                st.toast(msg)
                st.rerun()
            else:
                st.error(msg)

    seg = listar_seguimiento_incidencia(inc_id)
    if not seg.empty:
        st.markdown("### Historial de seguimiento")
        st.dataframe(seg, width='stretch', hide_index=True)

    st.markdown("---")
    st.markdown("### Firma digital de TOECE")
    st.caption("Dibuja tu firma en el recuadro.")
    if st_canvas is not None:
        canvas = st_canvas(
            fill_color="rgba(0,0,0,0)",
            stroke_width=3,
            stroke_color="#000000",
            background_color="#FFFFFF",
            height=180,
            width=500,
            drawing_mode="freedraw",
            key="canvas_inc_" + str(inc_id),
        )
        if st.button("Guardar firma TOECE", key="btn_firma_inc"):
            img = canvas_a_base64(canvas)
            if not img:
                st.error("Dibuja la firma primero.")
            else:
                ok, msg = guardar_firma_incidencia(
                    inc_id, "toece", st.session_state["user"]["nombres"], img,
                    st.session_state["user"]
                )
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)
    else:
        st.warning("streamlit-drawable-canvas no esta instalado.")

    firmas = listar_firmas_incidencia(inc_id)
    if firmas:
        st.markdown("**Firmas registradas:**")
        for f in firmas:
            st.markdown("- " + f["rol_firmante"] + ": " + (f["nombre_firmante"] or "-"))

    st.markdown("---")
    st.markdown("### Acciones finales")
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Perdonar y cerrar", key="btn_perdonar"):
            ok, msg = marcar_incidencia_perdonada(inc_id, st.session_state["user"])
            st.toast(msg)
            st.session_state.pop("_incidencia_abierta", None)
            st.rerun()
    with c2:
        if st.button("Crear acta", type="primary", key="btn_crear_acta"):
            st.session_state["_crear_acta_para_inc"] = inc_id
            st.rerun()
    with c3:
        if st.button("Cerrar ficha", key="btn_cerrar_ficha"):
            st.session_state.pop("_incidencia_abierta", None)
            st.rerun()

    if st.session_state.get("_crear_acta_para_inc") == inc_id:
        st.markdown("---")
        st.markdown("### Crear acta")
        tipos_a = listar_tipos_acta(solo_activos=True)
        if not tipos_a:
            st.warning("No hay tipos de acta configurados.")
        else:
            ops_ta = {t["nombre"] + (" (requiere apoderado)" if t["requiere_firma_apoderado"] else ""): t["id"] for t in tipos_a}
            ta_sel = st.selectbox("Tipo de acta", list(ops_ta.keys()), key="acta_tipo_sel")
            asunto = st.text_input("Asunto", key="acta_asunto")
            detalle = st.text_area("Detalle", key="acta_detalle")
            if st.button("Crear acta ahora", type="primary", key="btn_crear_acta_ok"):
                per = obtener_periodo_activo()
                pid = per["id"] if per else None
                ok, msg, acta_id = crear_acta(
                    inc_id, inc["alumno_id"], ops_ta[ta_sel],
                    asunto, detalle, st.session_state["user"], pid
                )
                if ok:
                    st.toast(msg)
                    st.session_state.pop("_crear_acta_para_inc", None)
                    st.session_state["_acta_abierta"] = acta_id
                    st.session_state.pop("_incidencia_abierta", None)
                    st.rerun()
                else:
                    st.error(msg)

    if st.session_state["user"]["rol"] in ("TOECE", "Admin"):
        st.markdown("---")
        with st.expander("Eliminar incidencia (reporte falso)"):
            motivo = st.text_input("Motivo de eliminacion", key="elim_motivo")
            if _pedir_password_critica("elim_inc_" + str(inc_id), "Eliminar incidencia"):
                ok, msg = eliminar_incidencia(inc_id, motivo, st.session_state["user"])
                if ok:
                    st.toast(msg)
                    st.session_state.pop("_incidencia_abierta", None)
                    st.rerun()
                else:
                    st.error(msg)


def _ficha_acta(acta_id):
    a = obtener_acta(acta_id)
    if not a:
        st.warning("Acta no encontrada.")
        st.session_state.pop("_acta_abierta", None)
        return

    st.markdown("---")
    st.markdown("## Acta #" + str(acta_id) + " - " + a['tipo_nombre'])
    nombre = (a['apellido_paterno'] + " " + (a['apellido_materno'] or "") + ", " + a['nombres']).strip(", ")
    st.markdown("**Alumno:** " + nombre + " | DNI: " + a['dni'])
    st.markdown("**Grado:** " + a['grado'] + " " + a['seccion'] + " | Turno: " + a['turno'])
    st.markdown("**Apoderado:** " + (a['nombre_apoderado'] or '-'))
    st.markdown("**Estado:** " + a['estado'])
    if a['asunto']:
        st.markdown("**Asunto:** " + a['asunto'])
    if a['detalle']:
        st.markdown("**Detalle:** " + a['detalle'])

    completo, faltan = acta_firmas_completas(acta_id)
    if not completo:
        st.warning("Faltan firmas: " + ", ".join(faltan))
    else:
        st.success("Todas las firmas completas.")

    st.markdown("---")
    firmas = firmas_acta(acta_id)
    st.markdown("### Firmas registradas")
    if firmas:
        for f in firmas:
            st.markdown("- " + f["rol_firmante"] + ": " + (f["nombre_firmante"] or "-"))
    else:
        st.info("Sin firmas aun.")

    if st_canvas is None:
        st.warning("streamlit-drawable-canvas no esta instalado. No se pueden firmar actas.")
        return

    if not acta_tiene_firma(acta_id, "toece"):
        st.markdown("### Firma TOECE")
        canvas = st_canvas(
            fill_color="rgba(0,0,0,0)",
            stroke_width=3,
            stroke_color="#000000",
            background_color="#FFFFFF",
            height=180,
            width=500,
            drawing_mode="freedraw",
            key="canvas_acta_toece_" + str(acta_id),
        )
        if st.button("Guardar firma TOECE", key="btn_firma_acta_toece"):
            img = canvas_a_base64(canvas)
            if not img:
                st.error("Dibuja la firma.")
            else:
                ok, msg = guardar_firma_acta(acta_id, "toece",
                                              st.session_state["user"]["nombres"],
                                              img, st.session_state["user"])
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)

    if a["requiere_firma_estudiante"] and not acta_tiene_firma(acta_id, "estudiante"):
        st.markdown("### Firma del estudiante")
        st.caption("El estudiante firma aqui en pantalla, presente en la oficina TOECE.")
        canvas = st_canvas(
            fill_color="rgba(0,0,0,0)",
            stroke_width=3,
            stroke_color="#000000",
            background_color="#FFFFFF",
            height=180,
            width=500,
            drawing_mode="freedraw",
            key="canvas_acta_est_" + str(acta_id),
        )
        if st.button("Guardar firma estudiante", key="btn_firma_acta_est"):
            img = canvas_a_base64(canvas)
            if not img:
                st.error("Dibuja la firma.")
            else:
                ok, msg = guardar_firma_acta(acta_id, "estudiante", nombre,
                                              img, st.session_state["user"])
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)

    if a["requiere_firma_apoderado"] and not acta_tiene_firma(acta_id, "apoderado"):
        st.markdown("### Firma del apoderado")
        st.caption("El apoderado debe venir al colegio. No se puede cerrar el acta sin esta firma.")
        canvas = st_canvas(
            fill_color="rgba(0,0,0,0)",
            stroke_width=3,
            stroke_color="#000000",
            background_color="#FFFFFF",
            height=180,
            width=500,
            drawing_mode="freedraw",
            key="canvas_acta_apo_" + str(acta_id),
        )
        if st.button("Guardar firma apoderado", key="btn_firma_acta_apo"):
            img = canvas_a_base64(canvas)
            if not img:
                st.error("Dibuja la firma.")
            else:
                ok, msg = guardar_firma_acta(acta_id, "apoderado",
                                              a['nombre_apoderado'] or "",
                                              img, st.session_state["user"])
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)

    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Cerrar ficha", key="btn_cerrar_acta_ficha"):
            st.session_state.pop("_acta_abierta", None)
            st.rerun()
    with c2:
        if st.session_state["user"]["rol"] in ("TOECE", "Admin"):
            with st.expander("Eliminar acta"):
                motivo = st.text_input("Motivo", key="elim_acta_motivo")
                if _pedir_password_critica("elim_acta_" + str(acta_id), "Eliminar acta"):
                    ok, msg = eliminar_acta(acta_id, motivo, st.session_state["user"])
                    if ok:
                        st.toast(msg)
                        st.session_state.pop("_acta_abierta", None)
                        st.rerun()
                    else:
                        st.error(msg)


def vista_toece():
    st_autorefresh(interval=10000, key="toece_refresh")
    st.title("TOECE")
    usuario = st.session_state["user"]

    if usuario["rol"] == "Auxiliar":
        _toece_justificar_permiso(usuario)
        return

    marcar_notif_leidas(usuario)

    tabs = st.tabs([
        "Incidencias reportadas",
        "En revision",
        "Actas pendientes",
        "Actas firmadas",
        "Bloqueados",
        "Justificar / Permiso"
    ])

    with tabs[0]:
        st.subheader("Incidencias reportadas por auxiliares")
        df = listar_incidencias_por_estado(["reportada"])
        if df.empty:
            st.info("Sin incidencias reportadas.")
        else:
            st.write(str(len(df)) + " incidencias pendientes de revision.")
            st.dataframe(df, width='stretch', hide_index=True)
            ops = {}
            for _, r in df.iterrows():
                et = r['fecha'] + " " + r['hora'] + " | " + r['apellidos'] + ", " + r['nombres'] + " (" + r['grado'] + " " + r['seccion'] + ") | " + (r['tipo_nombre'] or "Sin tipo")
                ops[et] = r["id"]
            sel = st.selectbox("Abrir incidencia", list(ops.keys()), key="inc_sel_rev")
            if st.button("Abrir ficha", type="primary", key="btn_abrir_inc"):
                st.session_state["_incidencia_abierta"] = ops[sel]
                st.rerun()

    with tabs[1]:
        st.subheader("Incidencias en revision")
        df = listar_incidencias_por_estado(["en_revision", "acta_pendiente"])
        if df.empty:
            st.info("Sin incidencias en revision.")
        else:
            st.dataframe(df, width='stretch', hide_index=True)
            ops = {}
            for _, r in df.iterrows():
                et = r['fecha'] + " | " + r['apellidos'] + " (" + r['estado'] + ")"
                ops[et] = r["id"]
            sel = st.selectbox("Abrir incidencia", list(ops.keys()), key="inc_sel_rev2")
            if st.button("Abrir ficha", type="primary", key="btn_abrir_inc2"):
                st.session_state["_incidencia_abierta"] = ops[sel]
                st.rerun()

    with tabs[2]:
        st.subheader("Actas pendientes de firma")
        st.caption("Aqui figuran los estudiantes que aun no firman su acta. Si falta apoderado, debe venir al colegio.")
        df = actas_pendientes_detalle()
        if df.empty:
            st.info("Sin actas pendientes.")
        else:
            st.dataframe(df, width='stretch', hide_index=True)
            ops = {r['fecha'] + " | " + r['apellidos'] + ", " + r['nombres'] + " | " + r['tipo_acta']: r["id"] for _, r in df.iterrows()}
            sel = st.selectbox("Abrir acta", list(ops.keys()), key="acta_sel_pend")
            if st.button("Abrir acta", type="primary", key="btn_abrir_acta"):
                st.session_state["_acta_abierta"] = ops[sel]
                st.rerun()

    with tabs[3]:
        st.subheader("Actas firmadas / cerradas")
        df = listar_actas_por_estado(["firmada", "cerrada"])
        if df.empty:
            st.info("Sin actas firmadas.")
        else:
            st.dataframe(df, width='stretch', hide_index=True)

    with tabs[4]:
        st.subheader("Alumnos bloqueados")
        df = listar_bloqueados()
        if df.empty:
            st.info("Sin bloqueados.")
        else:
            st.dataframe(df, width='stretch', hide_index=True)
            ops = {r['alumno'] + " (" + r['dni'] + ") - " + r['motivo']: r["alumno_id"] for _, r in df.iterrows()}
            sel = st.selectbox("Alumno a desbloquear", list(ops.keys()), key="desbloq_sel")
            idal = ops[sel]
            hist = historial_bloqueos_alumno(idal)
            with st.expander("Historial de bloqueos de este alumno"):
                st.dataframe(hist, width='stretch', hide_index=True)
            motivo = st.text_input("Motivo de desbloqueo", key="desbloq_motivo")
            if _pedir_password_critica("desbloq_" + str(idal), "Desbloquear"):
                ok, msg = desbloquear_alumno_completo(idal, motivo, usuario)
                st.toast(msg)
                st.rerun()

    with tabs[5]:
        _toece_justificar_permiso(usuario)

    if st.session_state.get("_incidencia_abierta"):
        _ficha_incidencia(st.session_state["_incidencia_abierta"])

    if st.session_state.get("_acta_abierta"):
        _ficha_acta(st.session_state["_acta_abierta"])


# ============================================================
# PANEL DIRECCION
# ============================================================
def _panel_direccion_kpis_modos(id_turno, fecha):
    con = obtener_conexion()
    try:
        modos = con.execute(
            "SELECT id,nombre,tabla_destino,tipo_asistencia FROM modos_camara "
            "WHERE activo=1 ORDER BY orden,id"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    if not modos:
        return
    st.markdown("### Modos de camara")
    cols = st.columns(3)
    for i, m in enumerate(modos):
        with cols[i % 3]:
            if m["tabla_destino"] == "incidencias":
                n = con.execute("SELECT COUNT(*) FROM incidencias WHERE fecha=?", (fecha,)).fetchone()[0]
                hora_ini = con.execute("SELECT MIN(hora) FROM incidencias WHERE fecha=?", (fecha,)).fetchone()[0]
                hora_fin = con.execute("SELECT MAX(hora) FROM incidencias WHERE fecha=?", (fecha,)).fetchone()[0]
                ultimo = con.execute(
                    "SELECT creado_por FROM incidencias WHERE fecha=? ORDER BY id DESC LIMIT 1",
                    (fecha,)
                ).fetchone()
            else:
                q = ("SELECT COUNT(*) FROM asistencias a "
                     "JOIN alumnos al ON a.alumno_id=al.id "
                     "JOIN secciones s ON al.seccion_id=s.id "
                     "WHERE a.fecha=? AND s.turno_id=?")
                n = con.execute(q, (fecha, id_turno)).fetchone()[0]
                q2 = ("SELECT MIN(a.hora) FROM asistencias a "
                      "JOIN alumnos al ON a.alumno_id=al.id "
                      "JOIN secciones s ON al.seccion_id=s.id "
                      "WHERE a.fecha=? AND s.turno_id=?")
                hora_ini = con.execute(q2, (fecha, id_turno)).fetchone()[0]
                q3 = ("SELECT MAX(a.hora) FROM asistencias a "
                      "JOIN alumnos al ON a.alumno_id=al.id "
                      "JOIN secciones s ON al.seccion_id=s.id "
                      "WHERE a.fecha=? AND s.turno_id=?")
                hora_fin = con.execute(q3, (fecha, id_turno)).fetchone()[0]
                ultimo = con.execute(
                    "SELECT registrado_por FROM tardanzas WHERE fecha=? ORDER BY id DESC LIMIT 1",
                    (fecha,)
                ).fetchone()
            st.markdown(
                '<div style="border:1px solid rgba(128,128,128,0.3);border-radius:8px;'
                'padding:14px 16px;margin-bottom:10px;">'
                '<div style="font-size:12px;font-weight:700;text-transform:uppercase;'
                'letter-spacing:0.08em;opacity:0.7;">' + m["nombre"] + '</div>'
                '<div style="font-size:26px;font-weight:700;margin:6px 0;">' + str(n) + '</div>'
                '<div style="font-size:12px;opacity:0.8;">'
                'Primera: ' + str(hora_ini or '-') + ' &nbsp;|&nbsp; '
                'Ultima: ' + str(hora_fin or '-') + ' &nbsp;|&nbsp; '
                'Ultimo: ' + str((ultimo[0] if ultimo else '-') or '-') +
                '</div></div>',
                unsafe_allow_html=True
            )


def vista_panel_direccion():
    st_autorefresh(interval=10000, key="panel_dir_refresh")
    st.title("Panel Direccion")
    fecha = hoy_str()

    turnos = listar_turnos()
    if not turnos:
        st.info("Sin turnos configurados.")
        return
    turnos_opts = {t["nombre"]: t["id"] for t in turnos}
    sel_turno = st.radio("Turno", list(turnos_opts.keys()), horizontal=True, key="panel_dir_turno")
    id_turno = turnos_opts[sel_turno]

    if st.button("Actualizar ahora", key="refresh_panel"):
        _control_faltas()
        st.rerun()

    con = obtener_conexion()
    total_turno = con.execute(
        "SELECT COUNT(*) FROM alumnos a JOIN secciones s ON a.seccion_id=s.id "
        "WHERE a.activo=1 AND s.turno_id=?", (id_turno,)
    ).fetchone()[0]
    puntuales_turno = con.execute(
        "SELECT COUNT(*) FROM asistencias a JOIN alumnos al ON a.alumno_id=al.id "
        "JOIN secciones s ON al.seccion_id=s.id "
        "WHERE a.fecha=? AND a.tipo='clases' AND a.estado='Puntual' AND s.turno_id=?",
        (fecha, id_turno)
    ).fetchone()[0]
    tardanzas_turno = con.execute(
        "SELECT COUNT(*) FROM asistencias a JOIN alumnos al ON a.alumno_id=al.id "
        "JOIN secciones s ON al.seccion_id=s.id "
        "WHERE a.fecha=? AND a.tipo='clases' AND a.estado='Tardanza' AND s.turno_id=?",
        (fecha, id_turno)
    ).fetchone()[0]
    faltas_turno = con.execute(
        "SELECT COUNT(*) FROM asistencias a JOIN alumnos al ON a.alumno_id=al.id "
        "JOIN secciones s ON al.seccion_id=s.id "
        "WHERE a.fecha=? AND a.tipo='clases' AND a.estado='Falta' AND s.turno_id=?",
        (fecha, id_turno)
    ).fetchone()[0]
    permisos_turno = con.execute(
        "SELECT COUNT(*) FROM asistencias a JOIN alumnos al ON a.alumno_id=al.id "
        "JOIN secciones s ON al.seccion_id=s.id "
        "WHERE a.fecha=? AND a.estado='Permiso' AND s.turno_id=?",
        (fecha, id_turno)
    ).fetchone()[0]
    bloqueados_turno = con.execute(
        "SELECT COUNT(*) FROM bloqueos b JOIN alumnos al ON b.alumno_id=al.id "
        "JOIN secciones s ON al.seccion_id=s.id "
        "WHERE b.activo=1 AND s.turno_id=?", (id_turno,)
    ).fetchone()[0]
    just_hoy_turno = con.execute(
        "SELECT COUNT(*) FROM asistencias a JOIN alumnos al ON a.alumno_id=al.id "
        "JOIN secciones s ON al.seccion_id=s.id "
        "WHERE a.fecha=? AND a.justificada=1 AND s.turno_id=?",
        (fecha, id_turno)
    ).fetchone()[0]
    ref_turno = con.execute(
        "SELECT COUNT(*) FROM asistencias a JOIN alumnos al ON a.alumno_id=al.id "
        "JOIN secciones s ON al.seccion_id=s.id "
        "WHERE a.fecha=? AND a.tipo='reforzamiento' AND a.estado='Asistio' AND s.turno_id=?",
        (fecha, id_turno)
    ).fetchone()[0]

    st.subheader("Resumen del dia - Turno " + sel_turno)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total alumnos", total_turno)
    c2.metric("Puntuales", puntuales_turno)
    c3.metric("Tardanzas", tardanzas_turno)
    c4.metric("Faltas", faltas_turno)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Reforzamiento asistio", ref_turno)
    c2.metric("Bloqueados", bloqueados_turno)
    c3.metric("Justificadas hoy", just_hoy_turno)
    c4.metric("Permisos hoy", permisos_turno)

    st.markdown("---")
    _panel_direccion_kpis_modos(id_turno, fecha)
    st.markdown("---")

    st.subheader("Ultimos escaneos del turno")
    df = pd.read_sql(
        "SELECT a.dni, a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos, "
        "a.nombres, g.nombre AS grado, s.nombre AS seccion, ast.tipo, ast.hora, ast.estado "
        "FROM asistencias ast "
        "JOIN alumnos a ON ast.alumno_id=a.id "
        "JOIN secciones s ON a.seccion_id=s.id "
        "JOIN grados g ON s.grado_id=g.id "
        "WHERE ast.fecha=? AND s.turno_id=? AND ast.hora IS NOT NULL "
        "ORDER BY ast.hora DESC LIMIT 30",
        con, params=[fecha, id_turno]
    )
    if df.empty:
        st.info("Sin escaneos hoy.")
    else:
        st.dataframe(df, width='stretch')

    st.markdown("---")
    st.subheader("Incidencias del turno hoy")
    dfi = pd.read_sql(
        "SELECT i.id, i.fecha, i.hora, i.estado, a.dni, "
        "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos, "
        "a.nombres, g.nombre AS grado, s.nombre AS seccion, ti.nombre AS tipo "
        "FROM incidencias i "
        "JOIN alumnos a ON i.alumno_id=a.id "
        "JOIN secciones s ON a.seccion_id=s.id "
        "JOIN grados g ON s.grado_id=g.id "
        "LEFT JOIN tipos_incidencia ti ON i.tipo_id=ti.id "
        "WHERE i.fecha=? AND s.turno_id=? AND i.estado!='eliminada' "
        "ORDER BY i.hora DESC",
        con, params=[fecha, id_turno]
    )
    if dfi.empty:
        st.info("Sin incidencias hoy.")
    else:
        st.dataframe(dfi, width='stretch')

    st.markdown("---")
    st.subheader("Justificaciones y permisos del turno")
    dfj = pd.read_sql(
        "SELECT a.dni, a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos, "
        "a.nombres, g.nombre AS grado, s.nombre AS seccion, ast.tipo, ast.estado, ast.justificada, ast.hora "
        "FROM asistencias ast "
        "JOIN alumnos a ON ast.alumno_id=a.id "
        "JOIN secciones s ON a.seccion_id=s.id "
        "JOIN grados g ON s.grado_id=g.id "
        "WHERE ast.fecha=? AND ast.justificada=1 AND s.turno_id=? "
        "ORDER BY a.apellido_paterno",
        con, params=[fecha, id_turno]
    )
    if dfj.empty:
        st.info("Sin justificaciones hoy.")
    else:
        st.dataframe(dfj, width='stretch')

    dfp = pd.read_sql(
        "SELECT a.dni, a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos, "
        "a.nombres, g.nombre AS grado, s.nombre AS seccion, p.fecha_inicio, p.fecha_fin, "
        "COALESCE(p.motivo,'') AS motivo "
        "FROM permisos p JOIN alumnos a ON p.alumno_id=a.id "
        "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
        "WHERE p.activo=1 AND p.fecha_inicio<=? AND p.fecha_fin>=? AND s.turno_id=? "
        "ORDER BY a.apellido_paterno",
        con, params=[fecha, fecha, id_turno]
    )
    if dfp.empty:
        st.info("Sin permisos vigentes hoy.")
    else:
        st.dataframe(dfp, width='stretch')


# ============================================================
# REPORTES
# ============================================================
def _rep_pantalla_tipos(idsec):
    con = obtener_conexion()
    sec = con.execute("""
        SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
        WHERE s.id=?
    """, (idsec,)).fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        st.session_state.pop("rep_idsec", None)
        st.rerun()
        return
    if st.button("Regresar a secciones", key="rep_volver_secciones"):
        st.session_state.pop("rep_idsec", None)
        st.rerun()
    st.subheader(sec['grado'] + " " + sec['seccion'] + " - Turno " + sec['turno'])
    c1, c2 = st.columns(2)
    with c1:
        desde = st.date_input("Desde", ahora().date() - timedelta(days=30), key="rep_desde")
    with c2:
        hasta = st.date_input("Hasta", ahora().date(), key="rep_hasta")
    if desde > hasta:
        st.error("La fecha Desde no puede ser mayor que Hasta.")
        return
    st.markdown("### Elige el tipo de reporte")
    tipos = [
        ("detalle", "Detalle"),
        ("faltas", "Conteo faltas"),
        ("mensual", "Cierre mensual"),
        ("eventos", "Eventos"),
        ("reforzamiento", "Reforzamiento"),
    ]
    cols = st.columns(5)
    for i, (k, titulo) in enumerate(tipos):
        with cols[i % 5]:
            if st.button(titulo, width='stretch', key="rep_tipo_btn_" + k):
                st.session_state["rep_tipo"] = k
                st.session_state["rep_desde_val"] = desde
                st.session_state["rep_hasta_val"] = hasta
                st.session_state["rep_idsec_val"] = idsec
                st.rerun()


def _rep_mostrar_reporte(idsec, tipo, desde, hasta):
    con = obtener_conexion()
    sec = con.execute("""
        SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
        WHERE s.id=?
    """, (idsec,)).fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        st.session_state.pop("rep_tipo", None)
        st.rerun()
        return
    if st.button("Regresar a reportes", key="rep_volver_tipos"):
        st.session_state.pop("rep_tipo", None)
        st.rerun()
    titulos = {"detalle": "Detalle", "faltas": "Conteo faltas", "mensual": "Cierre mensual",
               "eventos": "Eventos", "reforzamiento": "Reforzamiento"}
    st.subheader(titulos[tipo] + " - " + sec['grado'] + " " + sec['seccion'])
    st.caption("Desde " + str(desde) + " a " + str(hasta))

    if tipo == "detalle":
        df = reporte_detalle_por_tipo(desde, hasta, [idsec], "clases")
        _mostrar_reporte_agrupado(df, sec, "Detalle")
    elif tipo == "eventos":
        df = reporte_detalle_por_tipo(desde, hasta, [idsec], "evento")
        _mostrar_reporte_agrupado(df, sec, "Eventos")
    elif tipo == "reforzamiento":
        df = reporte_detalle_por_tipo(desde, hasta, [idsec], "reforzamiento")
        _mostrar_reporte_agrupado(df, sec, "Reforzamiento")
    elif tipo == "faltas":
        df = reporte_conteo_faltas(desde, hasta, [idsec])
        if df.empty:
            st.info("Sin faltas en este rango.")
        else:
            st.dataframe(df, width='stretch', hide_index=True)
            c1, c2 = st.columns(2)
            with c1:
                st.download_button("Excel", df_a_xlsx(df),
                                   "Faltas_" + sec['grado'] + sec['seccion'] + ".xlsx",
                                   key="rep_dl_fal_x")
            with c2:
                st.download_button("PDF",
                                   generar_pdf_tabla_ancha(
                                       df, "Conteo de faltas - " + sec['grado'] + " " + sec['seccion'] + " - Turno " + sec['turno']),
                                   "Faltas_" + sec['grado'] + sec['seccion'] + ".pdf",
                                   "application/pdf", key="rep_dl_fal_p")
    elif tipo == "mensual":
        df = cierre_mensual_calendario(hasta.month, hasta.year, [idsec])
        if df.empty:
            st.info("Sin datos para el mes.")
        else:
            st.dataframe(df, width='stretch', hide_index=True)
            c1, c2 = st.columns(2)
            with c1:
                st.download_button("Excel", df_a_xlsx(df),
                                   "Mensual_" + sec['grado'] + sec['seccion'] + ".xlsx",
                                   key="rep_dl_men_x")
            with c2:
                st.download_button("PDF",
                                   generar_pdf_tabla_ancha(
                                       df, "Cierre mensual - " + sec['grado'] + " " + sec['seccion'] + " - Turno " + sec['turno'],
                                       fuente_chica=True),
                                   "Mensual_" + sec['grado'] + sec['seccion'] + ".pdf",
                                   "application/pdf", key="rep_dl_men_p")


def _mostrar_reporte_agrupado(df, sec, titulo):
    if df.empty:
        st.info("Sin registros en este rango.")
        return
    st.write(str(len(df)) + " registros")
    df["fecha_dt"] = pd.to_datetime(df["fecha"])
    fechas = sorted(df["fecha_dt"].unique(), reverse=True)
    dias_es = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]
    for f in fechas:
        fecha_dt = pd.to_datetime(f)
        st.markdown("**" + fecha_dt.strftime("%d/%m/%Y") + " - " + dias_es[fecha_dt.weekday()] + "**")
        df_dia = df[df["fecha_dt"] == f].sort_values("apellidos")
        for _, r in df_dia.iterrows():
            just = ""
            if r["justificada"]:
                just = " (justificada"
                if r.get("justificado_por"):
                    just += " por " + str(r["justificado_por"])
                just += ")"
            origen = " [QR]" if r.get("origen") == "qr" else " [MANUAL]"
            st.markdown("- **" + r['apellidos'] + ", " + r['nombres'] + "** - " + r['estado'] + just + origen)
    c1, c2 = st.columns(2)
    with c1:
        st.download_button("Excel", df_a_xlsx(df),
                           titulo + "_" + sec['grado'] + sec['seccion'] + ".xlsx",
                           key="rep_dl_" + titulo)
    with c2:
        st.download_button("PDF",
                           generar_pdf_tabla(df, titulo + " - " + sec['grado'] + " " + sec['seccion']),
                           titulo + "_" + sec['grado'] + sec['seccion'] + ".pdf",
                           "application/pdf", key="rep_dl_pdf_" + titulo)


def _rep_seleccionar_seccion(usuario):
    con = obtener_conexion()
    rol = usuario["rol"]
    permitidas = None
    if rol == "Auxiliar":
        permitidas = [f["seccion_id"] for f in con.execute(
            "SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id=?",
            (usuario["id"],)
        ).fetchall()]
        if not permitidas:
            st.info("No tienes secciones asignadas. Contacta al Admin.")
            return
    grados = listar_grados()
    grados_mostrar = []
    for g in grados:
        secs = secciones_por_grado(g["id"])
        if permitidas is not None:
            secs = [s for s in secs if s["id"] in permitidas]
        if secs:
            grados_mostrar.append({"grado": g, "n": len(secs)})
    if not grados_mostrar:
        st.info("No hay secciones disponibles.")
        return
    idg = st.session_state.get("rep_grado_sel")
    if idg:
        g = next((x for x in grados if x["id"] == idg), None)
        if not g:
            st.session_state.pop("rep_grado_sel", None)
            st.rerun()
            return
        if st.button("Regresar a grados", key="rep_volver_g"):
            st.session_state.pop("rep_grado_sel", None)
            st.rerun()
        st.subheader("Secciones de " + g["nombre"])
        secs = secciones_por_grado(g["id"])
        if permitidas is not None:
            secs = [s for s in secs if s["id"] in permitidas]
        if not secs:
            st.info("No hay secciones visibles para este grado.")
            return
        cols = st.columns(3)
        for i, s in enumerate(secs):
            with cols[i % 3]:
                n = len(alumnos_de_seccion(s["id"]))
                if st.button(s["nombre"] + "  (" + str(n) + " alumnos)", width='stretch', key="rep_s_" + str(s["id"])):
                    st.session_state["rep_idsec"] = s["id"]
                    st.rerun()
        return
    st.markdown("### Elige el grado")
    cols = st.columns(3)
    for i, item in enumerate(grados_mostrar):
        with cols[i % 3]:
            if st.button(item["grado"]["nombre"] + "  (" + str(item["n"]) + " secciones)",
                         width='stretch', key="rep_g_" + str(item["grado"]["id"])):
                st.session_state["rep_grado_sel"] = item["grado"]["id"]
                st.rerun()


def _rep_general_por_turno_admin():
    st.subheader("Reporte general por auxiliar")
    st.caption("Solo se puede generar el reporte cuando la ventana de clases del turno ya cerro.")
    turnos = listar_turnos()
    if not turnos:
        st.info("Sin turnos configurados.")
        return
    ops = {t["nombre"]: t["id"] for t in turnos}
    sel_turno = st.selectbox("Turno", list(ops.keys()), key="repgen_turno")
    id_turno = ops[sel_turno]

    ha = hora_corta()
    ventana_clases = None
    for v in listar_ventanas(id_turno):
        if v["tipo"] == "clases":
            ventana_clases = v
            break
    if not ventana_clases:
        st.warning("Este turno no tiene ventana de clases configurada.")
        return
    if ha < ventana_clases["hora_cierre"]:
        st.warning("**No se puede generar el reporte todavia.** La ventana de clases de " +
                   sel_turno + " aun esta abierta (cierra a las " + ventana_clases["hora_cierre"] + ").")
        return

    c1, c2 = st.columns(2)
    with c1:
        desde = st.date_input("Desde", ahora().date() - timedelta(days=30), key="repgen_desde")
    with c2:
        hasta = st.date_input("Hasta", ahora().date(), key="repgen_hasta")
    if desde > hasta:
        st.error("La fecha Desde no puede ser mayor que Hasta.")
        return
    pid = None
    dfp = listar_periodos()
    if not dfp.empty:
        ops_p = {}
        for _, r in dfp.iterrows():
            et = r['nombre'] + " (" + r['fecha_inicio'] + " - " + r['fecha_fin'] + ")"
            if r["cerrado"]:
                et += " [CERRADO]"
            elif r["activo"]:
                et += " [ACTIVO]"
            ops_p[et] = r["id"]
        sel_lbl = st.selectbox("Periodo", list(ops_p.keys()), key="repgen_pid")
        pid = ops_p[sel_lbl]
    df = reporte_general_por_turno(desde, hasta, id_turno, pid)
    if df.empty:
        st.info("Sin datos en ese rango.")
        return
    st.markdown("---")

    df_export = df.rename(columns={
        "auxiliar": "Auxiliar", "grado": "Grado", "seccion": "Seccion",
        "puntuales": "Puntuales", "faltas": "Faltas",
        "permisos": "Permisos", "total": "Total"
    })
    st.dataframe(df_export, width='stretch', hide_index=True)
    st.markdown("### Descargar")
    c1, c2 = st.columns(2)
    with c1:
        st.download_button("Excel", df_a_xlsx(df_export, "General por auxiliar"),
                           "Reporte_" + sel_turno + "_" + str(desde) + "_" + str(hasta) + ".xlsx",
                           width='stretch')
    with c2:
        st.download_button("PDF",
                           generar_pdf_tabla_ancha(df_export, "Reporte general " + sel_turno,
                                                    str(desde) + " a " + str(hasta)),
                           "Reporte_" + sel_turno + "_" + str(desde) + "_" + str(hasta) + ".pdf",
                           "application/pdf", width='stretch')


def vista_reportes():
    st.title("Reportes y Consultas")
    usuario = st.session_state["user"]
    rol = usuario["rol"]

    if st.session_state.get("rep_tipo") and st.session_state.get("rep_idsec_val"):
        _rep_mostrar_reporte(
            st.session_state["rep_idsec_val"],
            st.session_state["rep_tipo"],
            st.session_state["rep_desde_val"],
            st.session_state["rep_hasta_val"]
        )
        return

    if st.session_state.get("rep_idsec"):
        _rep_pantalla_tipos(st.session_state["rep_idsec"])
        return

    if rol == "Admin":
        modo = st.radio(
            "Modo",
            ["Por seccion (normal)", "General por auxiliar (Admin)"],
            key="rep_modo_admin", horizontal=True, label_visibility="collapsed"
        )
        if modo == "General por auxiliar (Admin)":
            _rep_general_por_turno_admin()
            return

    _rep_seleccionar_seccion(usuario)


# ============================================================
# ALUMNOS UI
# ============================================================
def _frag_crear_alumno():
    st.subheader("Crear alumno manualmente")
    grados = listar_grados()
    if not grados:
        st.warning("No hay grados.")
        return
    with st.form("crear_al"):
        c1, c2 = st.columns(2)
        with c1:
            dni = st.text_input("DNI * (8 digitos)", max_chars=8)
            nom = st.text_input("Nombres *")
            pat = st.text_input("Apellido Paterno *")
        with c2:
            mat = st.text_input("Apellido Materno")
            g = st.selectbox("Grado *", grados, format_func=lambda x: x["nombre"])
            secs = secciones_por_grado(g["id"]) if g else []
            s = st.selectbox("Seccion *", secs, format_func=lambda x: x["nombre"]) if secs else None
        c3, c4 = st.columns(2)
        with c3:
            apo = st.text_input("Apoderado (opcional)")
        with c4:
            tel = st.text_input("Telefono (opcional)")
        pwd = st.text_input("Contrasena de Admin o TOECE", type="password")
        if st.form_submit_button("Crear", type="primary"):
            if not dni or not nom or not pat or not s:
                st.error("Completa obligatorios.")
            elif not re.fullmatch(r"\d{8}", dni.strip()):
                st.error("DNI invalido.")
            elif not pwd:
                st.error("Ingresa la contrasena.")
            elif not verificar_password_critica(pwd):
                st.error("Contrasena incorrecta.")
            else:
                ok, msg = crear_alumno(dni.strip(), nom.strip(), pat.strip(), mat.strip(),
                                        s["id"], apo.strip(), tel.strip(), st.session_state["user"])
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)


def _frag_editar_alumno():
    st.subheader("Editar alumno")
    idg, ids, texto = filtros_grado_seccion_nombre("ed_al")
    if not (texto or idg):
        return
    df = buscar_alumnos(texto, idg, ids, limite=50)
    if df.empty:
        st.info("Sin coincidencias.")
        return
    ops = {r['nombre_completo'] + " - " + r['grado'] + " " + r['seccion']: r["id"] for _, r in df.iterrows()}
    sel = st.selectbox("Alumno", list(ops.keys()), key="ed_sel")
    idal = ops[sel]
    con = obtener_conexion()
    datos = con.execute(
        "SELECT a.*,g.nombre AS grado,s.nombre AS seccion FROM alumnos a "
        "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id WHERE a.id=?",
        (idal,)
    ).fetchone()
    if not datos:
        return
    grados = listar_grados()
    with st.form("ed_form"):
        st.info("DNI: " + datos['dni'] + " (no editable)")
        apo = st.text_input("Apoderado", value=datos["nombre_apoderado"] or "")
        tel = st.text_input("Telefono", value=datos["telefono_apoderado"] or "")
        idxg = next((i for i, g in enumerate(grados) if g["nombre"] == datos["grado"]), 0)
        g = st.selectbox("Grado", grados, index=idxg, format_func=lambda x: x["nombre"])
        secs = secciones_por_grado(g["id"]) if g else []
        idxs = next((i for i, s in enumerate(secs) if s["id"] == datos["seccion_id"]), 0)
        s = st.selectbox("Seccion", secs, index=idxs, format_func=lambda x: x["nombre"])
        pwd = st.text_input("Contrasena de Admin o TOECE", type="password")
        if st.form_submit_button("Guardar", type="primary"):
            if not pwd:
                st.error("Ingresa la contrasena.")
            elif not verificar_password_critica(pwd):
                st.error("Contrasena incorrecta.")
            else:
                ok, msg = editar_alumno(idal, apo, tel, s["id"], datos["dni"], st.session_state["user"])
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)


def _frag_listar_alumnos():
    idg, ids, texto = filtros_grado_seccion_nombre("list_al")
    df = buscar_alumnos(texto, idg, ids, limite=5000)
    st.write(str(len(df)) + " alumnos")
    if df.empty:
        st.info("Sin resultados.")
        return
    mostrar = st.checkbox("Mostrar todos", value=False)
    lim = len(df) if mostrar else 50
    for _, al in df.head(lim).iterrows():
        c1, c2 = st.columns([5, 1])
        c1.markdown(
            "**" + al['nombre_completo'] + "** &nbsp; "
            "<span style='color:#E65100; font-weight:700;'>" + al['grado'] + " " + al['seccion'] + "</span> "
            "<span style='color:#757575;'>(" + al['turno'] + ")</span>",
            unsafe_allow_html=True
        )
        if c2.button("Ver perfil", key="perfil_" + str(al['id'])):
            st.session_state["perfil_alumno_id"] = al["id"]
            st.rerun()


def _perfil_alumno(idal):
    d = perfil_alumno_datos(idal)
    if not d:
        st.warning("Alumno no encontrado.")
        st.session_state.pop("perfil_alumno_id", None)
        return
    al = d["alumno"]
    usuario = st.session_state["user"]
    if st.button("Volver a la lista", key="volver_perfil"):
        st.session_state.pop("perfil_alumno_id", None)
        st.rerun()
    nombre = (al['apellido_paterno'] + " " + (al['apellido_materno'] or "") + ", " + al['nombres']).strip(", ")
    if d["bloqueado"]:
        badge = '<span class="perfil-badge badge-bloqueado">BLOQUEADO</span>'
    elif not d["observados"].empty and any(d["observados"]["activo"] == 1):
        badge = '<span class="perfil-badge badge-observado">OBSERVADO</span>'
    else:
        badge = '<span class="perfil-badge badge-ok">ACTIVO</span>'
    c_info, c_qr = st.columns([3, 1])
    with c_info:
        st.markdown("""
        <div class="perfil-card">
            <div class="perfil-nombre">""" + nombre + """ """ + badge + """</div>
            <div class="perfil-meta"><b>DNI:</b> """ + al['dni'] + """ &nbsp;|&nbsp; <b>Grado:</b> """ + al['grado'] + """ &nbsp;|&nbsp; <b>Seccion:</b> """ + al['seccion'] + """ &nbsp;|&nbsp; <b>Turno:</b> """ + al['turno'] + """</div>
            <div class="perfil-meta"><b>Apoderado:</b> """ + (al['nombre_apoderado'] or '-') + """ &nbsp;|&nbsp; <b>Telefono:</b> """ + (al['telefono_apoderado'] or '-') + """</div>
            <div class="perfil-resumen">
                <div class="perfil-resumen-item"><div class="num">""" + str(d['total_puntuales']) + """</div><div class="lbl">Puntuales</div></div>
                <div class="perfil-resumen-item"><div class="num">""" + str(d['total_tardanzas']) + """</div><div class="lbl">Tardanzas</div></div>
                <div class="perfil-resumen-item"><div class="num">""" + str(d['total_faltas']) + """</div><div class="lbl">Faltas</div></div>
                <div class="perfil-resumen-item"><div class="num">""" + str(d['total_ref_asistio']) + """</div><div class="lbl">Reforzamiento</div></div>
                <div class="perfil-resumen-item"><div class="num">""" + str(d['tard_injust']) + """</div><div class="lbl">Tard. injust.</div></div>
                <div class="perfil-resumen-item"><div class="num">""" + str(len(d['actas'])) + """</div><div class="lbl">Actas</div></div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    with c_qr:
        st.markdown("Codigo QR")
        st.image(generar_qr(al["dni"]), width=180)
    c1, c2, c3 = st.columns(3)
    with c1:
        pdf = pdf_carnet_alumno(al["dni"])
        if pdf:
            st.download_button("Descargar carnet QR", pdf,
                               "carnet_" + al['dni'] + ".pdf", "application/pdf",
                               width='stretch')
    with c2:
        st.download_button("Historial (Excel)", df_a_xlsx(d["asistencias"], "Historial"),
                           "historial_" + al['dni'] + ".xlsx", width='stretch')
    with c3:
        pdf_res = pdf_resumen_alumno(al["id"])
        if pdf_res:
            st.download_button("Resumen (PDF)", pdf_res,
                               "resumen_" + al['dni'] + ".pdf", "application/pdf",
                               width='stretch')
    if usuario["rol"] == "Admin":
        st.markdown("---")
        if al.get("activo", 1) == 1:
            with st.expander("Desactivar alumno"):
                st.warning("Estas seguro?")
                if _pedir_password_critica("desac_al", "Confirmar desactivacion"):
                    ok, msg = retirar_alumno(al["id"], al["dni"], usuario)
                    st.toast(msg)
                    st.rerun()
        else:
            with st.expander("Reactivar alumno"):
                if _pedir_password_critica("reac_al", "Confirmar reactivacion"):
                    ok, msg = reactivar_alumno(al["id"], al["dni"], usuario)
                    st.toast(msg)
                    st.rerun()
    st.markdown("---")
    tabs = st.tabs(["Asistencias", "Tardanzas", "Incidencias", "Actas",
                    "Observados", "Bloqueos", "Permisos"])
    with tabs[0]:
        if d["asistencias"].empty:
            st.info("Sin asistencias registradas.")
        else:
            st.write("Asistencias registradas")
            for _, ast in d["asistencias"].head(50).iterrows():
                c1, c2, c3, c4 = st.columns([2, 3, 2, 1])
                c1.write("**" + ast['fecha'] + "**")
                c2.write(ast['tipo'] + " - " + ast['estado'])
                just_txt = "Justificada"
                if ast["justificada"] and ast.get("justificado_por"):
                    just_txt += " por " + str(ast["justificado_por"])
                c3.write(just_txt if ast["justificada"] else "Sin justificar")
                if ast["justificada"]:
                    if c4.button("Quitar", key="quitar_" + str(ast['id'])):
                        ok, msg = quitar_justificacion(ast["id"], usuario)
                        if ok:
                            st.toast(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                else:
                    if ast["estado"] in ("Falta", "Tardanza"):
                        if c4.button("Justificar", key="just_" + str(ast['id'])):
                            st.session_state["justif_id"] = ast["id"]
                            st.rerun()
            if st.session_state.get("justif_id"):
                jid = st.session_state["justif_id"]
                st.markdown("---")
                st.markdown("Justificar asistencia")
                obs = st.text_input("Observacion (obligatoria)", key="justif_obs")
                pwd = st.text_input("Contrasena de Admin o TOECE", type="password", key="justif_pwd")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Confirmar justificacion", type="primary"):
                        if not obs.strip():
                            st.error("La observacion es obligatoria.")
                        elif not pwd:
                            st.error("Ingresa la contrasena.")
                        elif not verificar_password_critica(pwd):
                            st.error("Contrasena incorrecta.")
                        else:
                            ok, msg = justificar_asistencia(jid, obs, usuario)
                            if ok:
                                st.session_state.pop("justif_id", None)
                                st.toast(msg)
                                st.rerun()
                            else:
                                st.error(msg)
                with c2:
                    if st.button("Cancelar"):
                        st.session_state.pop("justif_id", None)
                        st.rerun()
    with tabs[1]:
        if d["tardanzas"].empty:
            st.info("Sin tardanzas registradas.")
        else:
            st.dataframe(d["tardanzas"], width='stretch')
    with tabs[2]:
        hist_inc = historial_incidencias_alumno(al["id"])
        if hist_inc.empty:
            st.info("Sin incidencias registradas.")
        else:
            st.dataframe(hist_inc, width='stretch', hide_index=True)
            for _, inc in hist_inc.head(10).iterrows():
                with st.expander("Incidencia #" + str(inc['id']) + " - " + inc['fecha'] + " - " + (inc['tipo'] or "Sin tipo")):
                    st.markdown("**Estado:** " + inc['estado'])
                    st.markdown("**Lugar:** " + (inc['lugar'] or '-'))
                    st.markdown("**Descripcion:** " + (inc['descripcion_hechos'] or inc['descripcion_breve'] or '-'))
                    st.markdown("**Reportado por:** " + (inc['reportado_por'] or '-'))
                    if st.session_state["user"]["rol"] in ("TOECE", "Admin"):
                        if st.button("Abrir ficha", key="abrir_inc_" + str(inc['id'])):
                            st.session_state["_incidencia_abierta"] = inc['id']
                            st.rerun()
    with tabs[3]:
        actas_al = pd.read_sql(
            "SELECT a.id, a.fecha, a.estado, ta.nombre AS tipo_acta "
            "FROM actas a JOIN tipos_acta ta ON a.tipo_acta_id=ta.id "
            "WHERE a.alumno_id=? ORDER BY a.fecha DESC",
            obtener_conexion(), params=[al["id"]]
        )
        if actas_al.empty:
            st.info("Sin actas registradas.")
        else:
            st.dataframe(actas_al, width='stretch', hide_index=True)
            for _, ac in actas_al.iterrows():
                if st.button("Abrir acta #" + str(ac['id']), key="abrir_acta_" + str(ac['id'])):
                    st.session_state["_acta_abierta"] = ac['id']
                    st.rerun()
    with tabs[4]:
        if d["observados"].empty:
            st.info("Sin registros de observados.")
        else:
            st.dataframe(d["observados"], width='stretch')
    with tabs[5]:
        hist_bloq = historial_bloqueos_alumno(al["id"])
        if hist_bloq.empty:
            st.info("Sin bloqueos registrados.")
        else:
            st.dataframe(hist_bloq, width='stretch', hide_index=True)
    with tabs[6]:
        if d["permisos"].empty:
            st.info("Sin permisos registrados.")
        else:
            st.dataframe(d["permisos"], width='stretch')


def vista_alumnos():
    st.title("Alumnos")
    pid = st.session_state.get("perfil_alumno_id")
    if pid:
        _perfil_alumno(pid)
        return
    tabs = st.tabs(["Listar", "Crear", "Editar"])
    with tabs[0]:
        _frag_listar_alumnos()
    with tabs[1]:
        _frag_crear_alumno()
    with tabs[2]:
        _frag_editar_alumno()


# ============================================================
# GRADOS Y SECCIONES
# ============================================================
def vista_grados_secciones():
    st.title("Grados y Secciones")
    st.caption("Las secciones se crean automaticamente al importar el Excel de alumnos.")
    con = obtener_conexion()
    grados = listar_grados()
    st.subheader("Grados")
    if grados:
        st.dataframe(pd.DataFrame(grados), width='stretch')
    else:
        st.info("Sin grados.")
    st.subheader("Secciones")
    df = pd.read_sql(
        "SELECT s.id,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno,"
        "(SELECT COUNT(*) FROM alumnos a WHERE a.seccion_id=s.id AND a.activo=1) AS alumnos_activos "
        "FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
        "ORDER BY t.nombre,g.nombre,s.nombre", con
    )
    if df.empty:
        st.info("Sin secciones.")
    else:
        st.dataframe(df, width='stretch')


# ============================================================
# CARNETS
# ============================================================
def vista_carnets():
    st.title("Carnets QR")
    if st.session_state.get("carn_ver_seccion"):
        _carnets_ver_seccion(st.session_state["carn_ver_seccion"])
        return
    grados = listar_grados()
    if not grados:
        st.warning("No hay grados.")
        return
    st.caption("Aprieta un grado para ver sus secciones.")
    cols = st.columns(3)
    for i, g in enumerate(grados):
        secs = secciones_por_grado(g["id"])
        with cols[i % 3]:
            if st.button(g['nombre'] + "  (" + str(len(secs)) + " secciones)",
                         width='stretch', key="carn_g_" + str(g['id'])):
                st.session_state["carn_grado_sel"] = g["id"]
                st.rerun()
    gid = st.session_state.get("carn_grado_sel")
    if not gid:
        return
    g = next((x for x in grados if x["id"] == gid), None)
    if not g:
        st.session_state.pop("carn_grado_sel", None)
        return
    st.markdown("---")
    st.subheader("Secciones de " + g['nombre'])
    secs = secciones_por_grado(g["id"])
    if not secs:
        st.info("Este grado no tiene secciones.")
        return
    cols = st.columns(3)
    for i, s in enumerate(secs):
        n = len(alumnos_de_seccion(s["id"]))
        with cols[i % 3]:
            if st.button(s['nombre'] + "  (" + str(n) + " alumnos)",
                         width='stretch', key="carn_s_" + str(s['id'])):
                st.session_state["carn_ver_seccion"] = s["id"]
                st.rerun()


def _carnets_ver_seccion(idsec):
    con = obtener_conexion()
    sec = con.execute("""
        SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
        WHERE s.id=?
    """, (idsec,)).fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        st.session_state.pop("carn_ver_seccion", None)
        return
    if st.button("Regresar a grados", key="carn_volver"):
        st.session_state.pop("carn_ver_seccion", None)
        st.session_state.pop("carn_sel_alumnos", None)
        st.rerun()
    st.subheader(sec['grado'] + " " + sec['seccion'] + " - Turno " + sec['turno'])
    df = alumnos_de_seccion(idsec)
    if df.empty:
        st.info("Sin alumnos activos.")
        return
    st.caption(str(len(df)) + " alumnos. Aprieta un nombre para seleccionarlo.")
    if "carn_sel_alumnos" not in st.session_state:
        st.session_state["carn_sel_alumnos"] = set()
    sel = st.session_state["carn_sel_alumnos"]
    cols = st.columns(4)
    for i, (_, al) in enumerate(df.iterrows()):
        with cols[i % 4]:
            if al["id"] in sel:
                st.markdown('<div class="btn-sel">', unsafe_allow_html=True)
                if st.button(al['nombre_completo'], key="carn_al_" + str(al['id']), width='stretch'):
                    sel.discard(al["id"])
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
            else:
                if st.button(al['nombre_completo'], key="carn_al_" + str(al['id']), width='stretch'):
                    sel.add(al["id"])
                    st.rerun()
    st.markdown("---")
    st.write("Seleccionados: " + str(len(sel)))
    c1, c2, c3 = st.columns(3)
    with c1:
        if sel:
            if st.button("Descargar seleccionados", type="primary", width='stretch', key="carn_dl_sel"):
                pdf = pdf_carnets_seleccionados(list(sel), titulo="Carnets seleccionados - " + sec['grado'] + " " + sec['seccion'])
                if pdf:
                    st.download_button("Guardar PDF", pdf,
                                       "carnets_sel_" + sec['grado'] + sec['seccion'] + ".pdf",
                                       "application/pdf", width='stretch')
        else:
            st.info("Marca al menos un alumno.")
    with c2:
        if st.button("Descargar todo el salon", width='stretch', key="carn_dl_todo"):
            pdf = pdf_carnets_por_seccion(idsec)
            if pdf:
                st.download_button("Guardar PDF", pdf,
                                   "carnets_" + sec['grado'] + sec['seccion'] + ".pdf",
                                   "application/pdf", width='stretch')
    with c3:
        if st.button("Limpiar seleccion", width='stretch', key="carn_limpiar"):
            st.session_state["carn_sel_alumnos"] = set()
            st.rerun()


# ============================================================
# VENTANAS
# ============================================================
def vista_ventanas():
    st.title("Ventanas de asistencia")
    st.caption("Configura apertura, limite puntual y cierre por turno y tipo.")
    for turno in listar_turnos():
        st.subheader("Turno " + turno['nombre'])
        for v in listar_ventanas(turno["id"]):
            with st.expander(v['nombre'] + " (" + v['tipo'] + ")"):
                with st.form("v_" + str(v['id'])):
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        ap_t = st.time_input("Apertura",
                                             value=datetime.strptime(v["hora_apertura"], "%H:%M").time(),
                                             key="ap_" + str(v['id']))
                    with c2:
                        lim_val = v["hora_limite_puntual"] or v["hora_apertura"]
                        lim_t = st.time_input("Limite puntual",
                                              value=datetime.strptime(lim_val, "%H:%M").time(),
                                              key="lim_" + str(v['id']))
                    with c3:
                        ci_t = st.time_input("Cierre",
                                             value=datetime.strptime(v["hora_cierre"], "%H:%M").time(),
                                             key="ci_" + str(v['id']))
                    pwd = st.text_input("Contrasena de Admin o TOECE", type="password",
                                         key="pwd_vent_" + str(v['id']))
                    if st.form_submit_button("Guardar", type="primary"):
                        if not pwd:
                            st.error("Ingresa la contrasena.")
                        elif not verificar_password_critica(pwd):
                            st.error("Contrasena incorrecta.")
                        else:
                            ap = ap_t.strftime("%H:%M")
                            lim = lim_t.strftime("%H:%M")
                            ci = ci_t.strftime("%H:%M")
                            escribir("UPDATE ventanas SET hora_apertura=?,hora_limite_puntual=?,hora_cierre=? WHERE id=?",
                                     (ap, lim, ci, v["id"]))
                            auditar(st.session_state["user"]["usuario"], "Edito ventana id=" + str(v['id']))
                            listar_ventanas.clear()
                            st.toast("Ventana actualizada")
                            st.rerun()


# ============================================================
# USUARIOS
# ============================================================
def puede_gestionar_usuario(usuario_actual, id_objetivo):
    con = obtener_conexion()
    obj = con.execute("SELECT id,rol,es_principal,usuario FROM usuarios WHERE id=?",
                      (id_objetivo,)).fetchone()
    if not obj:
        return False, "Usuario no encontrado."
    if id_objetivo == usuario_actual["id"]:
        return False, "Para cambiar tus datos usa Mi cuenta."
    soy_principal = (usuario_actual.get("es_principal") or 0) == 1
    if soy_principal:
        return True, ""
    if obj["rol"] == "Admin":
        return False, "No tienes permisos para gestionar a un Admin."
    return True, ""


def _usuario_ver_perfil(idu):
    usuario_actual = st.session_state["user"]
    soy_principal = (usuario_actual.get("es_principal") or 0) == 1
    con = obtener_conexion()
    u = con.execute("SELECT * FROM usuarios WHERE id=?", (idu,)).fetchone()
    if not u:
        st.warning("Usuario no encontrado.")
        st.session_state.pop("usr_ver_perfil", None)
        return
    if not soy_principal and u["rol"] == "Admin" and u["id"] != usuario_actual["id"]:
        st.error("No tienes permisos para ver este usuario.")
        st.session_state.pop("usr_ver_perfil", None)
        return
    if st.button("Regresar", key="usr_volver"):
        st.session_state.pop("usr_ver_perfil", None)
        st.rerun()
    inicial = (u["nombres"] or "?")[0].upper()
    estado = "Activo" if u["activo"] else "Inactivo"
    etiqueta = u['rol']
    if u['rol'] == 'Admin':
        etiqueta = "Admin principal" if u['es_principal'] else "Sub Admin"
    st.markdown("""
    <div class="perfil-card">
        <div class="perfil-nombre">
            <div style="width:60px;height:60px;border-radius:50%;background:#808080;display:inline-flex;align-items:center;justify-content:center;font-size:24px;font-weight:700;color:white;margin-right:12px;">""" + inicial + """</div>
            """ + u['nombres'] + """
        </div>
        <div class="perfil-meta"><b>Usuario:</b> """ + u['usuario'] + """ &nbsp;|&nbsp; <b>Rol:</b> """ + etiqueta + """ &nbsp;|&nbsp; <b>Estado:</b> """ + estado + """</div>
        <div class="perfil-meta"><b>Ultimo login:</b> """ + (u['ultimo_login'] or 'Nunca') + """ &nbsp;|&nbsp; <b>IP:</b> """ + (u['ultimo_ip'] or '-') + """</div>
        <div class="perfil-meta"><b>Intentos fallidos:</b> """ + str(u['intentos_fallidos'] or 0) + """ &nbsp;|&nbsp; <b>Bloqueado hasta:</b> """ + (u['bloqueado_hasta'] or '-') + """</div>
    </div>
    """, unsafe_allow_html=True)


def vista_usuarios():
    st.title("Usuarios")
    usuario = st.session_state["user"]
    soy_principal = (usuario.get("es_principal") or 0) == 1
    con = obtener_conexion()
    if st.session_state.get("usr_ver_perfil"):
        _usuario_ver_perfil(st.session_state["usr_ver_perfil"])
        return
    tabs = st.tabs(["Listar", "Crear", "Editar", "Asignar secciones", "Mantenimiento"])

    with tabs[0]:
        if soy_principal:
            df = pd.read_sql(
                "SELECT id,usuario,rol,nombres,activo,ultimo_login,es_principal "
                "FROM usuarios ORDER BY es_principal DESC, usuario", con
            )
        else:
            df = pd.read_sql(
                "SELECT id,usuario,rol,nombres,activo,ultimo_login,es_principal "
                "FROM usuarios WHERE id=? OR rol!='Admin' ORDER BY usuario",
                con, params=[usuario["id"]]
            )
        if df.empty:
            st.info("Sin usuarios.")
        else:
            st.caption("Aprieta un usuario para ver su perfil.")
            cols = st.columns(3)
            for i, (_, u) in enumerate(df.iterrows()):
                inicial = (u["nombres"] or "?")[0].upper()
                estado = "Activo" if u["activo"] else "Inactivo"
                etiqueta_rol = u["rol"]
                if u["rol"] == "Admin":
                    etiqueta_rol = "Admin (principal)" if u["es_principal"] else "Sub Admin"
                with cols[i % 3]:
                    if st.button(inicial + "  |  " + u['nombres'] + "\n" + etiqueta_rol + "  (" + estado + ")",
                                 key="usr_btn_" + str(u['id']), width='stretch'):
                        st.session_state["usr_ver_perfil"] = u["id"]
                        st.rerun()

    with tabs[1]:
        st.subheader("Crear usuario")
        c1, c2 = st.columns(2)
        with c1:
            u = st.text_input("Usuario", key="crear_u_usuario")
            p = st.text_input("Contrasena", type="password", key="crear_u_pass")
        with c2:
            n = st.text_input("Nombres", key="crear_u_nombres")
            if soy_principal:
                roles_disp = ["Admin", "TOECE", "Auxiliar", "Direccion"]
            else:
                roles_disp = ["TOECE", "Auxiliar", "Direccion"]
            r = st.selectbox("Rol", roles_disp, key="crear_u_rol")
        idt = None
        if r == "Auxiliar":
            t_lbl = st.selectbox("Turno", [x["nombre"] for x in listar_turnos()], key="crear_u_turno")
            idt = next((x["id"] for x in listar_turnos() if x["nombre"] == t_lbl), None)
        pwd_crit = st.text_input("Contrasena de Admin o TOECE", type="password", key="crear_u_pwd_crit")
        if st.button("Crear usuario", type="primary", key="crear_u_btn"):
            if not u or not p or not n:
                st.error("Completa usuario, contrasena y nombres.")
            elif len(p) < 6:
                st.error("La contrasena debe tener al menos 6 caracteres.")
            elif r == "Auxiliar" and not idt:
                st.error("Selecciona un turno para el Auxiliar.")
            elif not pwd_crit:
                st.error("Ingresa la contrasena de Admin o TOECE.")
            elif not verificar_password_critica(pwd_crit):
                st.error("Contrasena incorrecta.")
            else:
                u_norm = u.strip().lower()
                if con.execute("SELECT 1 FROM usuarios WHERE LOWER(usuario)=?", (u_norm,)).fetchone():
                    st.error("El usuario '" + u_norm + "' ya existe (sin importar mayusculas).")
                else:
                    try:
                        escribir(
                            "INSERT INTO usuarios(usuario,password,rol,nombres,turno_asignado,es_principal) "
                            "VALUES(?,?,?,?,?,0)",
                            (u_norm, hashear_password(p), r, n, idt)
                        )
                        auditar(usuario["usuario"], "Creo usuario " + u_norm + " con rol " + r)
                        st.toast("Usuario " + u_norm + " creado")
                        for k in ["crear_u_usuario", "crear_u_pass", "crear_u_nombres",
                                  "crear_u_rol", "crear_u_turno", "crear_u_pwd_crit"]:
                            st.session_state.pop(k, None)
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Ese usuario ya existe.")

    with tabs[2]:
        if soy_principal:
            df = pd.read_sql("SELECT id,usuario,rol,nombres,es_principal FROM usuarios WHERE usuario!='admin'", con)
        else:
            df = pd.read_sql("SELECT id,usuario,rol,nombres,es_principal FROM usuarios WHERE rol!='Admin'", con)
        if df.empty:
            st.info("Sin usuarios editables.")
        else:
            ops = {}
            for _, r in df.iterrows():
                et = r['usuario'] + " (" + r['rol'] + ")"
                if r["rol"] == "Admin" and r.get("es_principal"):
                    et += " principal"
                ops[et] = r["id"]
            sel = st.selectbox("Usuario", list(ops.keys()), key="edit_u_sel")
            idu = ops[sel]
            ok, msg = puede_gestionar_usuario(usuario, idu)
            if not ok:
                st.warning(msg)
            else:
                datos = con.execute("SELECT * FROM usuarios WHERE id=?", (idu,)).fetchone()
                with st.form("edit_u"):
                    u = st.text_input("Usuario", value=datos["usuario"])
                    n = st.text_input("Nombres", value=datos["nombres"])
                    p = st.text_input("Nueva contrasena (opcional)", type="password")
                    roles_edit = ["Admin", "TOECE", "Auxiliar", "Direccion"] if soy_principal else ["TOECE", "Auxiliar", "Direccion"]
                    idx_rol = roles_edit.index(datos["rol"]) if datos["rol"] in roles_edit else 0
                    r = st.selectbox("Rol", roles_edit, index=idx_rol)
                    act = st.checkbox("Activo", value=bool(datos["activo"]))
                    pwd_crit = st.text_input("Contrasena de Admin o TOECE", type="password")
                    if st.form_submit_button("Guardar", type="primary"):
                        if not pwd_crit:
                            st.error("Ingresa la contrasena.")
                        elif not verificar_password_critica(pwd_crit):
                            st.error("Contrasena incorrecta.")
                        else:
                            u_norm = u.strip().lower()
                            dup = con.execute(
                                "SELECT id FROM usuarios WHERE LOWER(usuario)=? AND id!=?",
                                (u_norm, idu)
                            ).fetchone()
                            if dup:
                                st.error("Ya existe otro usuario con ese nombre (sin importar mayusculas).")
                            else:
                                if p:
                                    escribir(
                                        "UPDATE usuarios SET usuario=?,nombres=?,rol=?,password=?,activo=? WHERE id=?",
                                        (u_norm, n, r, hashear_password(p), 1 if act else 0, idu)
                                    )
                                else:
                                    escribir(
                                        "UPDATE usuarios SET usuario=?,nombres=?,rol=?,activo=? WHERE id=?",
                                        (u_norm, n, r, 1 if act else 0, idu)
                                    )
                                auditar(usuario["usuario"], "Edito usuario " + u_norm)
                                st.toast("Usuario editado")
                                st.rerun()

    with tabs[3]:
        st.subheader("Asignar secciones a Auxiliares")
        st.caption("Cada seccion solo puede estar asignada a un auxiliar.")
        dfa = pd.read_sql(
            "SELECT id,usuario,nombres,turno_asignado FROM usuarios "
            "WHERE rol='Auxiliar' AND activo=1", con
        )
        if dfa.empty:
            st.info("Sin auxiliares.")
        else:
            ops = {r['nombres'] + " (" + r['usuario'] + ")": r["id"] for _, r in dfa.iterrows()}
            sel = st.selectbox("Auxiliar", list(ops.keys()))
            ida = ops[sel]
            ta = con.execute("SELECT turno_asignado FROM usuarios WHERE id=?", (ida,)).fetchone()
            if ta and ta["turno_asignado"]:
                secs = secciones_por_turno(ta["turno_asignado"])
                asignadas_a_otros = {
                    r["seccion_id"] for r in con.execute(
                        "SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id!=?",
                        (ida,)
                    ).fetchall()
                }
                asig = {r["seccion_id"] for r in con.execute(
                    "SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id=?", (ida,)
                ).fetchall()}
                st.write("Secciones disponibles:")
                st.caption("Las secciones ya asignadas a otro auxiliar no aparecen.")
                sel_s = []
                for s in secs:
                    if s["id"] in asignadas_a_otros:
                        continue
                    if st.checkbox(s['grado'] + " " + s['nombre'], value=s["id"] in asig,
                                   key="asig_" + str(ida) + "_" + str(s['id'])):
                        sel_s.append(s["id"])
                if _pedir_password_critica("asig_" + str(ida), "Guardar asignaciones"):
                    with _lock_escritura:
                        con.execute("DELETE FROM auxiliar_secciones WHERE usuario_id=?", (ida,))
                        for sid in sel_s:
                            try:
                                con.execute("INSERT INTO auxiliar_secciones(usuario_id,seccion_id) VALUES(?,?)",
                                            (ida, sid))
                            except sqlite3.IntegrityError:
                                st.warning("La seccion ya estaba asignada a otro auxiliar.")
                        con.commit()
                    auditar(usuario["usuario"], "Asigno " + str(len(sel_s)) + " secciones a usuario_id=" + str(ida))
                    st.toast("Asignaciones guardadas")
                    st.rerun()
            else:
                st.warning("Este auxiliar no tiene turno asignado.")

    with tabs[4]:
        st.subheader("Modo mantenimiento")
        if modo_mantenimiento():
            st.error("El sistema esta en MANTENIMIENTO.")
            if _pedir_password_critica("mant_off", "Desactivar mantenimiento"):
                desactivar_mantenimiento(usuario)
                st.toast("Mantenimiento desactivado")
                st.rerun()
        else:
            st.success("El sistema esta operativo.")
            msg = st.text_input("Mensaje para mostrar (opcional)", key="mant_msg")
            if _pedir_password_critica("mant_on", "Activar mantenimiento"):
                activar_mantenimiento(usuario, msg)
                st.toast("Mantenimiento activado")
                st.rerun()


# ============================================================
# CONFIG TOECE
# ============================================================
def vista_config_toece():
    st.title("Configuracion TOECE")
    usuario = st.session_state["user"]
    if usuario["rol"] not in ("Admin",):
        st.error("Solo Admin puede entrar aqui.")
        return
    tabs = st.tabs(["Modos de camara", "Tipos de incidencia", "Lugares",
                    "Tipos de acta", "Cursos"])

    with tabs[0]:
        st.subheader("Modos de camara")
        st.caption("Cada modo define que hace el escaner. Los modos del sistema no se pueden desactivar.")
        modos = listar_modos_camara(solo_activos=False)
        if modos:
            df = pd.DataFrame([{
                "id": m["id"], "codigo": m["codigo"], "nombre": m["nombre"],
                "tabla_destino": m["tabla_destino"], "tipo_asistencia": m["tipo_asistencia"],
                "obedece_ventana": "Si" if m["obedece_ventana"] else "No",
                "activo": "Si" if m["activo"] else "No",
                "es_sistema": "Si" if m["es_sistema"] else "No",
                "orden": m["orden"],
            } for m in modos])
            st.dataframe(df, width='stretch', hide_index=True)
        st.markdown("---")
        st.markdown("### Crear modo")
        with st.form("crear_modo"):
            c1, c2 = st.columns(2)
            with c1:
                cod = st.text_input("Codigo (sin espacios, ej: psicologia)")
                nom = st.text_input("Nombre visible")
            with c2:
                td = st.selectbox("Tabla destino", ["asistencias", "incidencias", "otro"])
                ta = st.selectbox("Tipo de asistencia", ["ninguno", "clases", "reforzamiento", "evento"])
            ov = st.checkbox("Obedece ventana horaria (solo clases)")
            desc = st.text_input("Descripcion (opcional)")
            pwd = st.text_input("Contrasena de Admin o TOECE", type="password", key="pwd_modo_crear")
            if st.form_submit_button("Crear modo", type="primary"):
                if not pwd:
                    st.error("Ingresa la contrasena.")
                elif not verificar_password_critica(pwd):
                    st.error("Contrasena incorrecta.")
                else:
                    ok, msg = crear_modo_camara(cod, nom, td, ta, ov, desc, usuario)
                    if ok:
                        st.toast(msg)
                        st.rerun()
                    else:
                        st.error(msg)
        st.markdown("---")
        st.markdown("### Editar / activar / desactivar")
        if modos:
            ops = {m["codigo"] + " | " + m["nombre"]: m["id"] for m in modos}
            sel = st.selectbox("Modo", list(ops.keys()), key="edit_modo_sel")
            idm = ops[sel]
            m = next(x for x in modos if x["id"] == idm)
            with st.form("editar_modo"):
                c1, c2 = st.columns(2)
                with c1:
                    nom_e = st.text_input("Nombre", value=m["nombre"])
                with c2:
                    td_e = st.selectbox("Tabla destino", ["asistencias", "incidencias", "otro"],
                                         index=["asistencias", "incidencias", "otro"].index(m["tabla_destino"]))
                    ta_e = st.selectbox("Tipo de asistencia",
                                         ["ninguno", "clases", "reforzamiento", "evento"],
                                         index=["ninguno", "clases", "reforzamiento", "evento"].index(m["tipo_asistencia"]))
                ov_e = st.checkbox("Obedece ventana horaria", value=bool(m["obedece_ventana"]))
                desc_e = st.text_input("Descripcion", value=m["descripcion"] or "")
                pwd_e = st.text_input("Contrasena de Admin o TOECE", type="password", key="pwd_modo_edit")
                if st.form_submit_button("Guardar cambios", type="primary"):
                    if not pwd_e:
                        st.error("Ingresa la contrasena.")
                    elif not verificar_password_critica(pwd_e):
                        st.error("Contrasena incorrecta.")
                    else:
                        ok, msg = editar_modo_camara(idm, nom_e, td_e, ta_e, ov_e, desc_e, usuario)
                        if ok:
                            st.toast(msg)
                            st.rerun()
                        else:
                            st.error(msg)
            st.markdown("---")
            nuevo_estado = not bool(m["activo"])
            etiqueta = "Desactivar" if m["activo"] else "Activar"
            if _pedir_password_critica("toggle_modo_" + str(idm), etiqueta):
                ok, msg = activar_desactivar_modo_camara(idm, nuevo_estado, usuario)
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)

    with tabs[1]:
        st.subheader("Tipos de incidencia")
        df = pd.DataFrame(listar_tipos_incidencia(solo_activos=False))
        if not df.empty:
            df["es_reincidente_grave"] = df["es_reincidente_grave"].apply(lambda x: "Si" if x else "No")
            df["activo"] = df["activo"].apply(lambda x: "Si" if x else "No")
            st.dataframe(df[["id", "nombre", "es_reincidente_grave", "activo", "orden"]],
                         width='stretch', hide_index=True)
        st.markdown("---")
        with st.form("crear_tipo_inc"):
            nom = st.text_input("Nombre del tipo")
            grave = st.checkbox("Es reincidente grave (si reincide, va directo a acta con apoderado)")
            pwd = st.text_input("Contrasena de Admin o TOECE", type="password", key="pwd_tipo_inc")
            if st.form_submit_button("Crear tipo", type="primary"):
                if not pwd:
                    st.error("Ingresa la contrasena.")
                elif not verificar_password_critica(pwd):
                    st.error("Contrasena incorrecta.")
                else:
                    ok, msg = crear_tipo_incidencia(nom, grave, usuario)
                    if ok:
                        st.toast(msg)
                        st.rerun()
                    else:
                        st.error(msg)
        st.markdown("---")
        tipos = listar_tipos_incidencia(solo_activos=False)
        if tipos:
            ops = {t["nombre"] + (" (activo)" if t["activo"] else " (inactivo)"): t["id"] for t in tipos}
            sel = st.selectbox("Tipo a activar/desactivar", list(ops.keys()), key="toggle_tipo_inc")
            idt = ops[sel]
            t = next(x for x in tipos if x["id"] == idt)
            etiqueta = "Desactivar" if t["activo"] else "Activar"
            if _pedir_password_critica("toggle_tipo_" + str(idt), etiqueta):
                ok, msg = activar_desactivar_tipo_incidencia(idt, not bool(t["activo"]), usuario)
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)

    with tabs[2]:
        st.subheader("Lugares de incidencia")
        df = pd.DataFrame(listar_lugares_incidencia(solo_activos=False))
        if not df.empty:
            df["activo"] = df["activo"].apply(lambda x: "Si" if x else "No")
            st.dataframe(df[["id", "nombre", "activo", "orden"]], width='stretch', hide_index=True)
        st.markdown("---")
        with st.form("crear_lugar"):
            nom = st.text_input("Nombre del lugar")
            pwd = st.text_input("Contrasena de Admin o TOECE", type="password", key="pwd_lugar")
            if st.form_submit_button("Crear lugar", type="primary"):
                if not pwd:
                    st.error("Ingresa la contrasena.")
                elif not verificar_password_critica(pwd):
                    st.error("Contrasena incorrecta.")
                else:
                    ok, msg = crear_lugar_incidencia(nom, usuario)
                    if ok:
                        st.toast(msg)
                        st.rerun()
                    else:
                        st.error(msg)
        st.markdown("---")
        lugares = listar_lugares_incidencia(solo_activos=False)
        if lugares:
            ops = {l["nombre"] + (" (activo)" if l["activo"] else " (inactivo)"): l["id"] for l in lugares}
            sel = st.selectbox("Lugar a activar/desactivar", list(ops.keys()), key="toggle_lugar")
            idl = ops[sel]
            l = next(x for x in lugares if x["id"] == idl)
            etiqueta = "Desactivar" if l["activo"] else "Activar"
            if _pedir_password_critica("toggle_lugar_" + str(idl), etiqueta):
                ok, msg = activar_desactivar_lugar_incidencia(idl, not bool(l["activo"]), usuario)
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)

    with tabs[3]:
        st.subheader("Tipos de acta")
        st.caption("Si un tipo requiere firma del apoderado, el acta no se puede cerrar sin esa firma.")
        df = pd.DataFrame(listar_tipos_acta(solo_activos=False))
        if not df.empty:
            df["requiere_firma_estudiante"] = df["requiere_firma_estudiante"].apply(lambda x: "Si" if x else "No")
            df["requiere_firma_apoderado"] = df["requiere_firma_apoderado"].apply(lambda x: "Si" if x else "No")
            df["activo"] = df["activo"].apply(lambda x: "Si" if x else "No")
            st.dataframe(df[["id", "nombre", "requiere_firma_estudiante",
                             "requiere_firma_apoderado", "activo", "orden"]],
                         width='stretch', hide_index=True)
        st.markdown("---")
        with st.form("crear_tipo_acta"):
            nom = st.text_input("Nombre del tipo de acta")
            c1, c2 = st.columns(2)
            with c1:
                req_e = st.checkbox("Requiere firma del estudiante", value=True)
            with c2:
                req_a = st.checkbox("Requiere firma del apoderado", value=False)
            pwd = st.text_input("Contrasena de Admin o TOECE", type="password", key="pwd_tipo_acta")
            if st.form_submit_button("Crear tipo de acta", type="primary"):
                if not pwd:
                    st.error("Ingresa la contrasena.")
                elif not verificar_password_critica(pwd):
                    st.error("Contrasena incorrecta.")
                else:
                    ok, msg = crear_tipo_acta(nom, req_e, req_a, usuario)
                    if ok:
                        st.toast(msg)
                        st.rerun()
                    else:
                        st.error(msg)
        st.markdown("---")
        tipos_a = listar_tipos_acta(solo_activos=False)
        if tipos_a:
            ops = {t["nombre"] + (" (activo)" if t["activo"] else " (inactivo)"): t["id"] for t in tipos_a}
            sel = st.selectbox("Tipo de acta a editar", list(ops.keys()), key="edit_tipo_acta")
            idt = ops[sel]
            t = next(x for x in tipos_a if x["id"] == idt)
            with st.form("editar_tipo_acta"):
                nom_e = st.text_input("Nombre", value=t["nombre"])
                c1, c2 = st.columns(2)
                with c1:
                    req_e = st.checkbox("Requiere firma del estudiante", value=bool(t["requiere_firma_estudiante"]))
                with c2:
                    req_a = st.checkbox("Requiere firma del apoderado", value=bool(t["requiere_firma_apoderado"]))
                pwd_e = st.text_input("Contrasena de Admin o TOECE", type="password", key="pwd_edit_tipo_acta")
                if st.form_submit_button("Guardar cambios", type="primary"):
                    if not pwd_e:
                        st.error("Ingresa la contrasena.")
                    elif not verificar_password_critica(pwd_e):
                        st.error("Contrasena incorrecta.")
                    else:
                        ok, msg = editar_tipo_acta(idt, nom_e, req_e, req_a, usuario)
                        if ok:
                            st.toast(msg)
                            st.rerun()
                        else:
                            st.error(msg)
            etiqueta = "Desactivar" if t["activo"] else "Activar"
            if _pedir_password_critica("toggle_tipo_acta_" + str(idt), etiqueta):
                ok, msg = activar_desactivar_tipo_acta(idt, not bool(t["activo"]), usuario)
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)

    with tabs[4]:
        st.subheader("Cursos (fijos)")
        st.caption("Los cursos son fijos para el sistema. Solo se pueden activar o desactivar.")
        df = pd.DataFrame(listar_cursos(solo_activos=False))
        if not df.empty:
            df["activo"] = df["activo"].apply(lambda x: "Si" if x else "No")
            st.dataframe(df[["id", "nombre", "activo", "orden"]], width='stretch', hide_index=True)
        cursos = listar_cursos(solo_activos=False)
        if cursos:
            ops = {c["nombre"] + (" (activo)" if c["activo"] else " (inactivo)"): c["id"] for c in cursos}
            sel = st.selectbox("Curso a activar/desactivar", list(ops.keys()), key="toggle_curso")
            idc = ops[sel]
            c = next(x for x in cursos if x["id"] == idc)
            etiqueta = "Desactivar" if c["activo"] else "Activar"
            if _pedir_password_critica("toggle_curso_" + str(idc), etiqueta):
                escribir("UPDATE cursos SET activo=? WHERE id=?", (0 if c["activo"] else 1, idc))
                auditar(usuario["usuario"], ("Activo" if not c["activo"] else "Desactivo") + " curso " + c["nombre"])
                listar_cursos.clear()
                st.toast("Curso actualizado")
                st.rerun()


# ============================================================
# IMPORTAR EXCEL
# ============================================================
def _frag_importar_excel():
    st.subheader("Cargar alumnos al periodo")
    st.info("Columnas: DNI, Nombres, Apellido Paterno, Apellido Materno, Grado, Seccion, Turno, Apoderado, Telefono.")
    st.caption("Si un DNI ya existe, se reactiva y actualiza.")
    arch = st.file_uploader("Sube el Excel", type=["xlsx", "xls"], key="import_excel_periodo")
    if not arch:
        return
    df = pd.read_excel(arch)
    st.write(str(len(df)) + " filas detectadas.")
    cols = list(df.columns)
    with st.form("mapeo_periodo"):
        c1, c2 = st.columns(2)
        with c1:
            m_dni = st.selectbox("DNI *", cols)
            m_nom = st.selectbox("Nombres *", cols)
            m_pat = st.selectbox("Apellido Paterno *", cols)
            m_mat = st.selectbox("Apellido Materno", [""] + cols)
        with c2:
            m_gra = st.selectbox("Grado *", cols)
            m_sec = st.selectbox("Seccion *", cols)
            m_tur = st.selectbox("Turno *", cols)
            m_apo_n = st.selectbox("Nombre Apoderado", [""] + cols)
            m_apo_t = st.selectbox("Telefono Apoderado", [""] + cols)
        validar = st.form_submit_button("Validar", type="primary")
    if validar:
        mapeo = {"dni": m_dni, "nombres": m_nom, "apellido_paterno": m_pat,
                 "apellido_materno": m_mat, "grado": m_gra, "seccion": m_sec,
                 "turno": m_tur, "apoderado_nombre": m_apo_n,
                 "apoderado_telefono": m_apo_t}
        val, errs, res = validar_importacion(df, mapeo)
        st.session_state["_iv"] = val
        st.session_state["_ie"] = errs
        st.session_state["_ir"] = res
    if "_ir" in st.session_state:
        r = st.session_state["_ir"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Total", r["total"])
        c2.metric("Validas", r["validas"])
        c3.metric("Errores", r["errores"])
        if st.session_state["_ie"]:
            with st.expander("Errores"):
                st.dataframe(pd.DataFrame(st.session_state["_ie"]), width='stretch')
        if st.session_state["_iv"]:
            if _pedir_password_critica("importar_excel", "Importar validas"):
                ins, reac, errs = insertar_alumnos_validos(st.session_state["_iv"])
                st.toast(str(ins) + " alumnos importados, " + str(reac) + " reactivados.")
                if errs:
                    st.warning(str(len(errs)) + " errores al insertar")
                for k in ["_iv", "_ie", "_ir"]:
                    st.session_state.pop(k, None)
                st.rerun()


# ============================================================
# AUDITORIA
# ============================================================
def vista_auditoria():
    st.title("Auditoria y Periodos")
    usuario = st.session_state["user"]
    tabs = st.tabs(["Registros", "Periodos", "Cierre de año"])
    with tabs[0]:
        df = obtener_auditoria(500)
        st.write(str(len(df)) + " registros")
        if not df.empty:
            st.dataframe(df, width='stretch')
    with tabs[1]:
        st.subheader("Periodos")
        st.dataframe(listar_periodos(), width='stretch')
        st.markdown("### Crear nuevo periodo")
        with st.form("nuevo_periodo"):
            c1, c2, c3 = st.columns(3)
            with c1:
                nom = st.text_input("Nombre (ej: 2026)")
            with c2:
                fi = st.date_input("Inicio", ahora().date())
            with c3:
                ff = st.date_input("Fin", ahora().date() + timedelta(days=270))
            pwd_crit = st.text_input("Contrasena de Admin o TOECE", type="password")
            if st.form_submit_button("Crear y activar", type="primary"):
                if not nom.strip():
                    st.error("Ingresa un nombre.")
                elif (ff - fi).days < 30:
                    st.error("El periodo debe durar minimo 1 mes.")
                elif not pwd_crit:
                    st.error("Ingresa la contrasena.")
                elif not verificar_password_critica(pwd_crit):
                    st.error("Contrasena incorrecta.")
                else:
                    ok, msg = crear_periodo(nom.strip(), fi.strftime("%Y-%m-%d"),
                                             ff.strftime("%Y-%m-%d"), usuario)
                    if ok:
                        st.toast(msg)
                        st.rerun()
                    else:
                        st.error(msg)
        st.markdown("---")
        st.markdown("### Cargar alumnos al periodo activo")
        p = obtener_periodo_activo()
        if p:
            if periodo_tiene_alumnos(p["id"]):
                st.success("El periodo ya tiene alumnos cargados.")
            else:
                st.warning("El periodo NO tiene alumnos.")
            _frag_importar_excel()
        else:
            st.info("No hay periodo activo.")
        st.markdown("---")
        st.markdown("### Activar periodo (solo no cerrados)")
        df2 = listar_periodos()
        df2 = df2[df2["cerrado"] == 0]
        if not df2.empty:
            ops = {}
            for _, r in df2.iterrows():
                et = r['nombre'] + " (" + r['fecha_inicio'] + " - " + r['fecha_fin'] + ")"
                if r["activo"]:
                    et += " ACTIVO"
                ops[et] = r["id"]
            sel = st.selectbox("Periodo a activar", list(ops.keys()))
            if _pedir_password_critica("activar_periodo", "Activar"):
                ok, msg = activar_periodo(ops[sel], usuario)
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)
    with tabs[2]:
        st.subheader("Cierre de año escolar")
        st.warning("Al cerrar el periodo se desactivan TODOS los alumnos.")
        p = obtener_periodo_activo()
        if not p:
            st.info("No hay periodo activo.")
            return
        st.info("Periodo activo: " + p['nombre'] + " (" + p['fecha_inicio'] + " - " + p['fecha_fin'] + ")")
        st.markdown("Reporte resumen del periodo:")
        df_rep = reporte_cierre_anual(p["id"])
        if not df_rep.empty:
            st.dataframe(df_rep, width='stretch')
        st.markdown("---")
        st.markdown("Paso 1: Descargar resumen por salon (OBLIGATORIO)")
        if st.button("Generar resumen por salon", key="btn_gen_resumen"):
            hojas = {}
            con = obtener_conexion()
            secs = con.execute(
                "SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno "
                "FROM secciones s JOIN grados g ON s.grado_id=g.id "
                "JOIN turnos t ON s.turno_id=t.id ORDER BY t.nombre, g.nombre, s.nombre"
            ).fetchall()
            for sec in secs:
                df_sec = pd.read_sql(
                    "SELECT a.dni, "
                    "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos, "
                    "a.nombres, "
                    "(SELECT COUNT(*) FROM asistencias ast WHERE ast.alumno_id=a.id AND ast.periodo_id=? "
                    "AND ast.estado='Puntual') AS puntuales, "
                    "(SELECT COUNT(*) FROM asistencias ast WHERE ast.alumno_id=a.id AND ast.periodo_id=? "
                    "AND ast.estado='Tardanza') AS tardanzas, "
                    "(SELECT COUNT(*) FROM asistencias ast WHERE ast.alumno_id=a.id AND ast.periodo_id=? "
                    "AND ast.estado='Falta' AND ast.justificada=0) AS faltas_injust, "
                    "(SELECT COUNT(*) FROM asistencias ast WHERE ast.alumno_id=a.id AND ast.periodo_id=? "
                    "AND ast.estado='Falta' AND ast.justificada=1) AS faltas_just, "
                    "(SELECT COUNT(*) FROM incidencias i WHERE i.alumno_id=a.id AND i.periodo_id=? "
                    "AND i.estado!='eliminada') AS incidencias "
                    "FROM alumnos a WHERE a.seccion_id=? AND a.periodo_id=? "
                    "ORDER BY a.apellido_paterno",
                    con, params=[p["id"]] * 5 + [sec["id"], p["id"]]
                )
                if not df_sec.empty:
                    nombre_hoja = (sec["grado"] + "_" + sec["seccion"] + "_" + sec["turno"])[:31]
                    hojas[nombre_hoja] = df_sec
            if not hojas:
                st.warning("Sin datos para exportar.")
            else:
                xlsx = df_a_xlsx_multilhoja(hojas)
                st.download_button("Descargar resumen anual (Excel)",
                                   xlsx, "resumen_anual_" + p['nombre'] + ".xlsx",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                st.session_state["_reporte_descargado"] = True
        st.markdown("---")
        st.markdown("Paso 2: Cerrar periodo (requiere contrasena)")
        desc = st.session_state.get("_reporte_descargado", False)
        if not desc:
            st.info("Debes descargar el resumen antes.")
        with st.form("cerrar_año"):
            c1, c2, c3 = st.columns(3)
            with c1:
                nn = st.text_input("Nombre nuevo periodo", value=str(ahora().year + 1))
            with c2:
                fi = st.date_input("Inicio nuevo", date(ahora().year + 1, 3, 1))
            with c3:
                ff = st.date_input("Fin nuevo", date(ahora().year + 1, 12, 31))
            pwd = st.text_input("Contrasena de Admin o TOECE", type="password")
            conf = st.text_input("Escribe CERRAR para confirmar")
            sub = st.form_submit_button("Cerrar año escolar", type="primary")
        if sub:
            if not desc:
                st.error("Primero debes descargar el resumen.")
            elif conf.strip() != "CERRAR":
                st.error("Debes escribir exactamente CERRAR.")
            elif not pwd:
                st.error("Ingresa la contrasena.")
            elif not verificar_password_critica(pwd):
                st.error("Contrasena incorrecta.")
            else:
                ok, msg = cerrar_año_escolar(usuario, p["id"], nn,
                                              fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"))
                if ok:
                    st.session_state.pop("_reporte_descargado", None)
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
        st.markdown("---")
        st.markdown("Cierres anteriores:")
        dfc = listar_cierres_anuales()
        if not dfc.empty:
            st.dataframe(dfc, width='stretch')
        st.markdown("Periodos cerrados (solo consulta):")
        dfp = listar_periodos_cerrados()
        if not dfp.empty:
            st.dataframe(dfp, width='stretch')


# ============================================================
# DIAS ESPECIALES
# ============================================================
def vista_dias_especiales():
    st.title("Dias especiales")
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
                    turnos_opts.update({t["nombre"]: t["id"] for t in listar_turnos()})
                    t_lbl = st.selectbox("Turno", list(turnos_opts.keys()))
                    hora_t = st.time_input("Hora entrada",
                                           value=datetime.strptime("08:00", "%H:%M").time())
                    hora = hora_t.strftime("%H:%M")
                else:
                    turnos_opts = {"Ambos": None}
                    t_lbl = "Ambos"
                    hora = "00:00"
                    st.info("Los feriados no tienen horario ni turno.")
            st.markdown("Alcance del dia especial:")
            st.caption("Si no marcas nada, aplica a TODO el colegio.")
            with st.expander("Filtrar por grados y secciones", expanded=False):
                grados = listar_grados()
                selecciones_secciones = []
                for g in grados:
                    st.checkbox("Todo " + g['nombre'], key="dia_grado_" + str(g['id']))
                    secs = secciones_por_grado(g["id"])
                    if secs:
                        cols = st.columns(3)
                        for i, s in enumerate(secs):
                            with cols[i % 3]:
                                if st.checkbox(g['nombre'] + " " + s['nombre'],
                                               key="dia_sec_" + str(g['id']) + "_" + str(s['id'])):
                                    selecciones_secciones.append(s["id"])
            pwd_crear = st.text_input("Contrasena de Admin o TOECE", type="password")
            sub = st.form_submit_button("Crear", type="primary")
        if sub:
            if not desc.strip():
                st.error("Descripcion requerida.")
            elif not pwd_crear:
                st.error("Ingresa la contrasena.")
            elif not verificar_password_critica(pwd_crear):
                st.error("Contrasena incorrecta.")
            else:
                idt = turnos_opts.get(t_lbl) if tipo == "Evento" else None
                per = obtener_periodo_activo()
                pid = per["id"] if per else None
                with _lock_escritura:
                    cur = con.execute(
                        "INSERT INTO dias_especiales(fecha,descripcion,turno_id,hora_entrada,"
                        "tipo,periodo_id) VALUES(?,?,?,?,?,?)",
                        (fecha.strftime("%Y-%m-%d"), desc.strip(), idt,
                         hora if tipo == "Evento" else "00:00",
                         "evento" if tipo == "Evento" else "feriado", pid)
                    )
                    idd = cur.lastrowid
                    for sid in selecciones_secciones:
                        con.execute(
                            "INSERT INTO dias_especiales_secciones(dia_especial_id,seccion_id) VALUES(?,?)",
                            (idd, sid)
                        )
                    con.commit()
                auditar(usuario["usuario"], "Creo dia especial " + desc)
                st.toast("Dia especial creado")
                st.rerun()
    with tabs[1]:
        fh = hoy_str()
        df = pd.read_sql(
            "SELECT d.id,d.fecha,d.descripcion,COALESCE(t.nombre,'Ambos') AS turno,"
            "d.hora_entrada,d.tipo,"
            "(SELECT COUNT(*) FROM dias_especiales_secciones WHERE dia_especial_id=d.id) AS num_secciones "
            "FROM dias_especiales d LEFT JOIN turnos t ON d.turno_id=t.id "
            "WHERE d.fecha>=? AND d.activo=1 ORDER BY d.fecha",
            con, params=[fh]
        )
        if df.empty:
            st.info("Sin dias especiales.")
        else:
            st.dataframe(df, width='stretch')
            ops = {r['fecha'] + " - " + r['descripcion'] + " (" + r['tipo'] + ")": r["id"] for _, r in df.iterrows()}
            sel = st.selectbox("Eliminar", list(ops.keys()))
            st.warning("Esta accion es irreversible.")
            if _pedir_password_critica("del_dia", "Eliminar dia especial"):
                escribir("DELETE FROM dias_especiales WHERE id=?", (ops[sel],))
                auditar(usuario["usuario"], "Elimino dia especial id=" + str(ops[sel]))
                st.toast("Dia especial eliminado")
                st.rerun()
                
def _opciones_auxiliar(usuario):
    return ["Puerta", "TOECE", "Reportes", "Mi cuenta"]


def obtener_opciones_por_rol(usuario):
    rol = usuario["rol"]
    if rol == "Admin":
        return ["Puerta", "TOECE", "Panel Direccion", "Reportes", "Alumnos",
                "Grados y Secciones", "Carnets", "Dias especiales", "Ventanas",
                "Config TOECE", "Usuarios", "Auditoria", "Mi cuenta"]
    if rol == "TOECE":
        return ["Puerta", "TOECE", "Reportes", "Mi cuenta"]
    if rol == "Direccion":
        return ["Panel Direccion", "TOECE", "Alumnos", "Carnets",
                "Dias especiales", "Mi cuenta"]
    if rol == "Auxiliar":
        return _opciones_auxiliar(usuario)
    return []


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
    "Config TOECE": vista_config_toece,
    "Usuarios": vista_usuarios,
    "Auditoria": vista_auditoria,
    "Mi cuenta": vista_mi_cuenta,
}


def menu_lateral():
    usuario = st.session_state["user"]
    rol = usuario["rol"]
    opciones = obtener_opciones_por_rol(usuario)
    with st.sidebar:
        inicial = (usuario["nombres"] or "?")[0].upper()
        st.markdown(
            '<div class="encabezado-sidebar">'
            '<div class="avatar">' + inicial + '</div>'
            '<div class="nombre">' + usuario["nombres"] + '</div>'
            '<div class="rol">' + rol + '</div>'
            '</div>',
            unsafe_allow_html=True
        )
        if rol in ("TOECE", "Admin"):
            st_autorefresh(interval=10000, key="badge_refresh")
            try:
                n_notif = contar_incidencias_nuevas(usuario)
                if n_notif:
                    st.markdown(
                        '<div style="background:#C62828;color:white;padding:8px 12px;'
                        'border-radius:6px;font-weight:700;text-align:center;margin:6px 8px;">'
                        'Incidencias nuevas: ' + str(n_notif) + '</div>',
                        unsafe_allow_html=True
                    )
            except Exception:
                pass

        if "menu" not in st.session_state or st.session_state["menu"] not in opciones:
            st.session_state["menu"] = opciones[0]
        op = st.radio("Menu", opciones, key="menu", label_visibility="collapsed")
        st.markdown("---")
        if st.button("Cerrar sesion", width='stretch'):
            cerrar_sesion()
            st.rerun()
    return op


def _enrutar(op, usuario):
    v = RUTAS.get(op)
    if not v:
        st.warning("Vista no disponible.")
        return
    if op not in obtener_opciones_por_rol(usuario):
        st.error("Sin permisos.")
        auditar(usuario["usuario"], "Intento acceso no autorizado a " + op)
        return
    ultimo = st.session_state.get("_ultima_vista")
    if ultimo != op:
        st.session_state["_qr_mount_id"] = st.session_state.get("_qr_mount_id", 0) + 1
        st.session_state["_ultima_vista"] = op
    v()


def _control_faltas():
    ult = st.session_state.get("_ultimo_control_faltas")
    t = time.time()
    if ult and (t - ult) < 300:
        return
    st.session_state["_ultimo_control_faltas"] = t
    marcar_faltas_al_cierre()


def main():
    st.set_page_config(
        page_title="Asistencia I.E. Yarinacocha",
        page_icon="escudo.png",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    try:
        inicializar_bd()
        aplicar_estilos()
        if not st.session_state.get("user"):
            vista_login()
            return
        if not _verificar_admin_activo():
            st.error("No hay Admin principal activo en el sistema.")
            st.info("Contacta al desarrollador para restaurar el acceso.")
            st.stop()
        if st.session_state["user"].get("debe_cambiar_password"):
            vista_cambio_password_obligatorio()
            return
        if modo_mantenimiento() and st.session_state["user"]["rol"] != "Admin":
            vista_mantenimiento()
            return
        if sistema_bloqueado():
            st.warning("El sistema no esta configurado. No hay periodo activo con alumnos cargados.")
            if st.session_state["user"]["rol"] == "Admin":
                st.info("Ve a Auditoria -> Periodos para crear un periodo y subir el Excel de alumnos.")
                st.session_state["menu"] = "Auditoria"
                _enrutar("Auditoria", st.session_state["user"])
            else:
                st.info("Contacta al Administrador.")
            return
        _control_faltas()
        op = menu_lateral()
        if op:
            _enrutar(op, st.session_state["user"])
    except sqlite3.OperationalError as e:
        st.error("Error de base de datos: " + str(e))
        st.info("Verifica que el archivo asistencia.db no este bloqueado.")
        log.exception("Error de BD en main")
    except Exception as e:
        st.error("Error inesperado: " + str(e))
        st.info("El error quedo registrado en logs/app.log.")
        log.exception("Error en main")


if __name__ == "__main__":
    main()
