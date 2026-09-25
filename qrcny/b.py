# asistencia I.E. Yarinacocha
import hashlib, logging, os, re, secrets, sqlite3, threading, time
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import qrcode
import streamlit as st
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (Image as RLImage, PageBreak, Paragraph,
                                 SimpleDocTemplate, Spacer, Table, TableStyle)

from qr_scanner_component import qr_scanner

# logs
LOG_DIR = Path("logs"); LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("asistencia")

# config
DB_PATH = "asistencia.db"
MESES_ES = ["","Enero","Febrero","Marzo","Abril","Mayo","Junio",
            "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
C_NARANJA = "#E65100"; C_NARANJA_H = "#BF360C"
C_VERDE = "#2E7D32"; C_VERDE_H = "#1B5E20"
C_VERDE_BG = "#d4edda"; C_VERDE_TX = "#155724"
C_AMAR_BG = "#fff3cd"; C_AMAR_TX = "#856404"
C_ROJO_BG = "#f8d7da"; C_ROJO_TX = "#721c24"; C_ROJO_BTN = "#C62828"

PBK_ITER = 260_000; PBK_ALG = "sha256"
MAX_INTENTOS = 3; MIN_BLOQUEO = 10

PUNTUAL="Puntual"; TARDANZA="Tardanza"; FALTA="Falta"; PERMISO="Permiso"
REF_ASISTIO="Asistio"; REF_NO_ASISTIO="No asistio"
ACC_PERDONADO="PERDONADO"; ACC_DERIVADO="DERIVADO_TOECE"; ACC_RETENIDO="RETENIDO_APODERADO"
ROLES_VALIDOS=("Admin","TOECE","Auxiliar","Direccion")
VENT_CLASES="clases"; VENT_REF="reforzamiento"
TIPO_ASIST_CLASES="clases"; TIPO_ASIST_REF="reforzamiento"; TIPO_ASIST_EVENTO="evento"

JUSTIF_HORAS_DESPUES = 24

# helpers tiempo
def ahora(): return datetime.now(timezone.utc) - timedelta(hours=5)
def hoy_str(): return ahora().strftime("%Y-%m-%d")
def hora_str(): return ahora().strftime("%H:%M:%S")
def hora_corta(): return ahora().strftime("%H:%M")
def timestamp_str(): return ahora().strftime("%Y-%m-%d %H:%M:%S")
def sumar_minutos(hhmm, mins):
    return (datetime.strptime(hhmm,"%H:%M")+timedelta(minutes=mins)).strftime("%H:%M")
def es_fin_de_semana(fecha=None):
    return (fecha or ahora().date()).weekday() >= 5
def nombre_mes(m):
    return MESES_ES[m] if 1 <= m <= 12 else ""

def hora_a_minutos(hhmm):
    if not hhmm: return None
    try:
        h, m = hhmm.strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None

def fecha_a_dt(f):
    return datetime.strptime(f, "%Y-%m-%d").date()

# seguridad
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
        except (ValueError, TypeError): return False
    return secrets.compare_digest(
        hashlib.sha256(password.encode()).hexdigest(), hash_guardado)

def verificar_password_admin(password):
    con = obtener_conexion()
    for fila in con.execute("SELECT password FROM usuarios WHERE rol='Admin' AND activo=1").fetchall():
        if verificar_password(password, fila["password"]): return True
    return False

def verificar_password_critica(password):
    con = obtener_conexion()
    for fila in con.execute(
        "SELECT password FROM usuarios WHERE rol IN ('Admin','TOECE') AND activo=1"
    ).fetchall():
        if verificar_password(password, fila["password"]):
            return True
    return False

# ============================================================
# BD - ARQUITECTURA MULTIHILO
# ============================================================

_hilos = threading.local()
_lock_escritura = threading.Lock()

def obtener_conexion():
    if not hasattr(_hilos, "con") or _hilos.con is None:
        con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        con.row_factory = sqlite3.Row
        for p in ("journal_mode=WAL","synchronous=NORMAL","foreign_keys=ON","busy_timeout=30000"):
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

def escribir_muchos(sql, lista_params):
    with _lock_escritura:
        con = obtener_conexion()
        cur = con.executemany(sql, lista_params)
        con.commit()
        return cur

def leer(sql, params=()):
    con = obtener_conexion()
    return con.execute(sql, params)

def existe_columna(cur, tabla, col):
    return any(f["name"] == col for f in cur.execute("PRAGMA table_info(" + tabla + ")").fetchall())

def _tabla_existe(cur, tabla):
    return cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tabla,)).fetchone() is not None

def inicializar_bd():
    con = obtener_conexion(); cur = con.cursor()
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
    CREATE TABLE IF NOT EXISTS justificaciones_previas(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha_objetivo TEXT NOT NULL,tipo TEXT NOT NULL CHECK(tipo IN ('Falta','Tardanza')),motivo TEXT,creado_por TEXT,timestamp TEXT NOT NULL,aplicada INTEGER DEFAULT 0,UNIQUE(alumno_id,fecha_objetivo,tipo));
    CREATE TABLE IF NOT EXISTS permisos(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha_inicio TEXT NOT NULL,fecha_fin TEXT NOT NULL,motivo TEXT,tipo TEXT NOT NULL DEFAULT 'permiso',creado_por TEXT,timestamp TEXT NOT NULL,activo INTEGER DEFAULT 1,periodo_id INTEGER);
    CREATE TABLE IF NOT EXISTS dias_especiales(id INTEGER PRIMARY KEY,fecha TEXT NOT NULL,descripcion TEXT,turno_id INTEGER,hora_entrada TEXT,activo INTEGER DEFAULT 1,tipo TEXT DEFAULT 'evento' CHECK(tipo IN ('evento','feriado')),periodo_id INTEGER);
    CREATE TABLE IF NOT EXISTS dias_especiales_secciones(id INTEGER PRIMARY KEY,dia_especial_id INTEGER NOT NULL,seccion_id INTEGER NOT NULL,UNIQUE(dia_especial_id,seccion_id));
    CREATE TABLE IF NOT EXISTS usuarios(id INTEGER PRIMARY KEY,usuario TEXT UNIQUE NOT NULL,password TEXT NOT NULL,rol TEXT NOT NULL CHECK(rol IN ('Admin','TOECE','Auxiliar','Direccion')),nombres TEXT NOT NULL,turno_asignado INTEGER,activo INTEGER DEFAULT 1,intentos_fallidos INTEGER DEFAULT 0,bloqueado_hasta TEXT,debe_cambiar_password INTEGER DEFAULT 0,ultimo_login TEXT,ultimo_ip TEXT);
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
    _migrar(cur); _seed(cur); con.commit()
    log.info("bd lista")

def _migrar(cur):
    migs = [("alumnos","activo","ALTER TABLE alumnos ADD COLUMN activo INTEGER DEFAULT 1"),
            ("alumnos","retirado_en","ALTER TABLE alumnos ADD COLUMN retirado_en TEXT"),
            ("usuarios","ultimo_login","ALTER TABLE usuarios ADD COLUMN ultimo_login TEXT"),
            ("usuarios","ultimo_ip","ALTER TABLE usuarios ADD COLUMN ultimo_ip TEXT"),
            ("usuarios","es_principal","ALTER TABLE usuarios ADD COLUMN es_principal INTEGER DEFAULT 0"),
            ("auditoria","ip","ALTER TABLE auditoria ADD COLUMN ip TEXT"),
            ("asistencias","tipo","ALTER TABLE asistencias ADD COLUMN tipo TEXT DEFAULT 'clases'"),
            ("asistencias","ventana_id","ALTER TABLE asistencias ADD COLUMN ventana_id INTEGER"),
            ("asistencias","origen","ALTER TABLE asistencias ADD COLUMN origen TEXT DEFAULT 'qr'"),
            ("asistencias","justificado_por","ALTER TABLE asistencias ADD COLUMN justificado_por TEXT"),
            ("asistencias","justificado_en","ALTER TABLE asistencias ADD COLUMN justificado_en TEXT"),
            ("tardanzas","origen","ALTER TABLE tardanzas ADD COLUMN origen TEXT DEFAULT 'qr'"),
            ("periodos","cerrado","ALTER TABLE periodos ADD COLUMN cerrado INTEGER DEFAULT 0"),
            ("observados","observacion_cierre","ALTER TABLE observados ADD COLUMN observacion_cierre TEXT"),
            ("bloqueos","origen","ALTER TABLE bloqueos ADD COLUMN origen TEXT DEFAULT 'automatico'")]
    for t, c, sql in migs:
        if _tabla_existe(cur, t) and not existe_columna(cur, t, c):
            try: cur.execute(sql)
            except sqlite3.Error as e: log.warning("mig %s.%s: %s", t, c, e)
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
        cur.executemany("INSERT INTO turnos(nombre,hora_entrada,hora_salida,tolerancia_min) VALUES(?,?,?,?)",
                        [("Mañana","06:00","12:25",10),("Tarde","12:00","18:10",10)])
    if cur.execute("SELECT COUNT(*) FROM ventanas").fetchone()[0] == 0:
        tm = cur.execute("SELECT id FROM turnos WHERE nombre='Mañana'").fetchone()
        tt = cur.execute("SELECT id FROM turnos WHERE nombre='Tarde'").fetchone()
        if tm:
            cur.execute("INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,hora_limite_puntual,hora_cierre,tolerancia_min,orden) VALUES(?,'clases','Clases mañana','06:00','06:55','07:10',10,1)",(tm["id"],))
            cur.execute("INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,hora_limite_puntual,hora_cierre,tolerancia_min,orden) VALUES(?,'reforzamiento','Reforzamiento mañana','08:00','08:00','14:00',0,2)",(tm["id"],))
        if tt:
            cur.execute("INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,hora_limite_puntual,hora_cierre,tolerancia_min,orden) VALUES(?,'reforzamiento','Reforzamiento tarde','10:00','10:00','10:20',0,1)",(tt["id"],))
            cur.execute("INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,hora_limite_puntual,hora_cierre,tolerancia_min,orden) VALUES(?,'clases','Clases tarde','12:00','12:49','18:10',10,2)",(tt["id"],))
    if cur.execute("SELECT COUNT(*) FROM grados").fetchone()[0] == 0:
        for g in ["1ro","2do","3ro","4to","5to"]:
            cur.execute("INSERT INTO grados(nombre) VALUES(?)",(g,))
    if cur.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        pwd = secrets.token_urlsafe(9)
        cur.execute("INSERT INTO usuarios(usuario,password,rol,nombres,debe_cambiar_password,es_principal) VALUES(?,?,'Admin','Administrador',1,1)",
                    ("admin", hashear_password(pwd)))
        log.warning("admin creado, pass temporal: %s", pwd)

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

# sesion
def inicializar_sesion():
    if st.session_state.get("user"): return

def refrescar_sesion_si_necesario():
    return

def cerrar_sesion():
    usuario = st.session_state.get("user")
    if usuario:
        auditar(usuario["usuario"], "Logout")
    for k in list(st.session_state.keys()):
        del st.session_state[k]

# auth
def _bloqueado(u):
    if not u.get("bloqueado_hasta"): return False
    try: return ahora() < datetime.strptime(u["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError): return False

def _ip():
    try: return st.context.headers.get("X-Forwarded-For", "local")
    except Exception: return "local"

def autenticar(nombre_usuario, password):
    con = obtener_conexion()
    nombre_usuario = (nombre_usuario or "").strip().lower()
    f = con.execute("SELECT * FROM usuarios WHERE LOWER(usuario)=? AND activo=1", (nombre_usuario,)).fetchone()
    if not f: return None, "Usuario no encontrado o inactivo"
    u = dict(f)
    if _bloqueado(u):
        lim = datetime.strptime(u["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S")
        return None, "Cuenta bloqueada. Intenta en " + str(int((lim-ahora()).total_seconds()/60)+1) + " min"
    if not verificar_password(password, u["password"]):
        if u["rol"] == "Admin":
            auditar(nombre_usuario, "Intento fallido de login (Admin no se bloquea)")
            return None, "Credenciales incorrectas."
        it = (u.get("intentos_fallidos") or 0) + 1
        if it >= MAX_INTENTOS:
            bh = (ahora()+timedelta(minutes=MIN_BLOQUEO)).strftime("%Y-%m-%d %H:%M:%S")
            escribir("UPDATE usuarios SET intentos_fallidos=0,bloqueado_hasta=? WHERE id=?", (bh, u["id"]))
            auditar(nombre_usuario, "Cuenta bloqueada por intentos fallidos")
            return None, "Cuenta bloqueada por %d min" % MIN_BLOQUEO
        escribir("UPDATE usuarios SET intentos_fallidos=? WHERE id=?", (it, u["id"]))
        return None, "Credenciales incorrectas. Quedan " + str(MAX_INTENTOS-it) + " intento(s)"
    escribir("UPDATE usuarios SET intentos_fallidos=0,bloqueado_hasta=NULL,ultimo_login=?,ultimo_ip=? WHERE id=?",
                (timestamp_str(), _ip(), u["id"]))
    return u, ""

def auditar(usuario, accion, va=None, vn=None, tb=None, rid=None):
    try:
        escribir("INSERT INTO auditoria(usuario,accion,fecha,valor_anterior,valor_nuevo,tabla_afectada,registro_id,ip) VALUES(?,?,?,?,?,?,?,?)",
                    (usuario, accion, timestamp_str(), va, vn, tb, rid, _ip()))
    except Exception as e: log.warning("audit: %s", e)

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
    a = con.execute("SELECT id FROM usuarios WHERE rol='Admin' AND es_principal=1 AND activo=1").fetchone()
    return a is not None

# periodos
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
    return pd.read_sql("SELECT id,nombre,fecha_inicio,fecha_fin,activo,cerrado FROM periodos ORDER BY id DESC", obtener_conexion())

@st.cache_data(ttl=60)
def listar_periodos_cerrados():
    return pd.read_sql("SELECT id,nombre,fecha_inicio,fecha_fin,fecha_cierre FROM periodos WHERE cerrado=1 ORDER BY id DESC", obtener_conexion())

def crear_periodo(nombre, fi, ff, usuario):
    con = obtener_conexion()
    try:
        with _lock_escritura:
            cur = con.execute("INSERT INTO periodos(nombre,fecha_inicio,fecha_fin,activo,cerrado) VALUES(?,?,?,1,0)", (nombre, fi, ff))
            idn = cur.lastrowid
            con.execute("UPDATE periodos SET activo=0 WHERE id!=?", (idn,)); con.commit()
        auditar(usuario["usuario"], "Creo periodo " + nombre, tb="periodos", rid=idn)
        listar_periodos.clear()
        return True, "Periodo " + nombre + " creado y activado."
    except sqlite3.Error as e: return False, "Error: " + str(e)

def activar_periodo(idp, usuario):
    con = obtener_conexion()
    f = con.execute("SELECT cerrado FROM periodos WHERE id=?", (idp,)).fetchone()
    if not f: return False, "Periodo no encontrado."
    if f["cerrado"]: return False, "Ese periodo esta cerrado."
    with _lock_escritura:
        con.execute("UPDATE periodos SET activo=0")
        con.execute("UPDATE periodos SET activo=1 WHERE id=?", (idp,)); con.commit()
    auditar(usuario["usuario"], "Activo periodo id=" + str(idp), tb="periodos", rid=idp)
    listar_periodos.clear()
    return True, "Periodo activado."

# ventanas
@st.cache_data(ttl=60)
def listar_turnos():
    con = obtener_conexion()
    return [dict(f) for f in con.execute("SELECT * FROM turnos ORDER BY id").fetchall()]

@st.cache_data(ttl=60)
def listar_ventanas(id_turno=None):
    con = obtener_conexion()
    if id_turno:
        return [dict(f) for f in con.execute("SELECT * FROM ventanas WHERE turno_id=? AND activo=1 ORDER BY orden,id", (id_turno,)).fetchall()]
    return [dict(f) for f in con.execute("SELECT * FROM ventanas WHERE activo=1 ORDER BY turno_id,orden").fetchall()]

def dia_especial_hoy(id_turno, fecha, id_seccion=None):
    con = obtener_conexion()
    f = con.execute("SELECT * FROM dias_especiales WHERE fecha=? AND activo=1 AND (turno_id=? OR turno_id IS NULL) ORDER BY turno_id DESC LIMIT 1",
                    (fecha, id_turno)).fetchone()
    if not f: return None
    dia = dict(f)
    if id_seccion:
        secs = con.execute("SELECT seccion_id FROM dias_especiales_secciones WHERE dia_especial_id=?", (dia["id"],)).fetchall()
        if secs and id_seccion not in [s["seccion_id"] for s in secs]: return None
    return dia

def ventana_activa_para_alumno(id_turno, fecha, id_seccion=None):
    dia = dia_especial_hoy(id_turno, fecha, id_seccion)
    if dia and dia["tipo"] == "feriado": return None
    h = hora_corta()
    for v in listar_ventanas(id_turno):
        ap = v["hora_apertura"]
        if v["tipo"] == VENT_CLASES and dia and dia["tipo"] == "evento":
            ap = dia["hora_entrada"]
        lim = v["hora_limite_puntual"] or ap
        if ap <= h <= v["hora_cierre"]:
            return {**v, "hora_apertura_efectiva": ap, "hora_limite_efectiva": lim,
                    "es_evento": bool(dia and dia["tipo"] == "evento" and v["tipo"] == VENT_CLASES)}
    return None

# alumnos
@st.cache_data(ttl=60)
def listar_grados():
    con = obtener_conexion()
    return [dict(f) for f in con.execute("SELECT * FROM grados ORDER BY nombre").fetchall()]

@st.cache_data(ttl=60)
def secciones_por_grado(idg):
    con = obtener_conexion()
    return [dict(f) for f in con.execute("SELECT * FROM secciones WHERE grado_id=? ORDER BY nombre", (idg,)).fetchall()]

@st.cache_data(ttl=60)
def secciones_por_turno(idt):
    con = obtener_conexion()
    return [dict(f) for f in con.execute("SELECT s.*,g.nombre AS grado FROM secciones s JOIN grados g ON s.grado_id=g.id WHERE s.turno_id=? ORDER BY g.nombre,s.nombre", (idt,)).fetchall()]

@st.cache_data(ttl=60)
def listar_todas_secciones():
    con = obtener_conexion()
    return [dict(f) for f in con.execute("SELECT s.id,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno,g.id AS grado_id,t.id AS turno_id FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id ORDER BY t.nombre,g.nombre,s.nombre").fetchall()]

def alumnos_de_seccion(idsec):
    con = obtener_conexion()
    return pd.read_sql("SELECT a.id,a.dni,a.nombres,a.apellido_paterno,a.apellido_materno,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS nombre_completo FROM alumnos a WHERE a.seccion_id=? AND a.activo=1 ORDER BY a.apellido_paterno,a.apellido_materno,a.nombres",
                       con, params=[idsec])

def buscar_alumnos(texto, idg=None, idsec=None, limite=200):
    con = obtener_conexion()
    q = ("SELECT a.id,a.dni,a.nombres,a.apellido_paterno,a.apellido_materno,g.id AS grado_id,g.nombre AS grado,"
         "s.id AS seccion_id,s.nombre AS seccion,t.nombre AS turno,"
         "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS nombre_completo "
         "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
         "JOIN turnos t ON s.turno_id=t.id WHERE a.activo=1")
    p = []
    if texto:
        for w in [x.strip() for x in texto.split() if x.strip()]:
            q += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
            pat = "%" + w + "%"; p += [pat, pat, pat]
    if idg: q += " AND g.id=?"; p.append(idg)
    if idsec: q += " AND s.id=?"; p.append(idsec)
    q += " ORDER BY a.apellido_paterno LIMIT ?"; p.append(limite)
    return pd.read_sql(q, con, params=p)

def buscar_alumnos_con_estado(texto, limite=15):
    con = obtener_conexion()
    hoy = hoy_str()
    q = """
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
        g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
        a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS nombre_completo,
        (SELECT estado FROM asistencias WHERE alumno_id=a.id AND fecha=? AND tipo='clases' LIMIT 1) AS estado_hoy,
        (SELECT hora FROM asistencias WHERE alumno_id=a.id AND fecha=? AND tipo='clases' LIMIT 1) AS hora_hoy
        FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id
        JOIN turnos t ON s.turno_id=t.id WHERE a.activo=1
    """
    p = [hoy, hoy]
    if texto:
        for w in [x.strip() for x in texto.split() if x.strip()]:
            q += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ? OR a.dni LIKE ?)"
            pat = "%" + w + "%"
            p += [pat, pat, pat, pat]
    q += " ORDER BY a.apellido_paterno LIMIT ?"
    p.append(limite)
    return pd.read_sql(q, con, params=p)

def _buscar_alumno_por_dni(con, dni):
    f = con.execute("SELECT a.id,a.dni,a.nombres,a.apellido_paterno,a.apellido_materno,s.id AS seccion_id,s.nombre AS seccion,g.nombre AS grado,t.id AS turno_id,t.nombre AS turno FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE a.dni=? AND a.activo=1",
                    (dni,)).fetchone()
    return dict(f) if f else None

def _nombre_completo(a):
    return (a['apellido_paterno'] + " " + (a['apellido_materno'] or "") + ", " + a['nombres']).strip(", ")

def crear_alumno(dni, nombres, ap, am, idsec, apo_n, apo_t, usuario):
    con = obtener_conexion(); per = obtener_periodo_activo()
    if not per: return False, "No hay periodo activo."
    try:
        with _lock_escritura:
            cur = con.cursor(); ida = None
            if apo_n:
                f = cur.execute("SELECT id FROM apoderados WHERE nombre=? AND COALESCE(telefono,'')=?", (apo_n, apo_t or "")).fetchone()
                ida = f["id"] if f else cur.execute("INSERT INTO apoderados(nombre,telefono) VALUES(?,?)", (apo_n, apo_t or None)).lastrowid
            cur.execute("INSERT INTO alumnos(dni,nombres,apellido_paterno,apellido_materno,seccion_id,apoderado_id,nombre_apoderado,telefono_apoderado,periodo_id) VALUES(?,?,?,?,?,?,?,?,?)",
                        (dni, nombres, ap, am or None, idsec, ida, apo_n or None, apo_t or None, per["id"]))
            con.commit()
        auditar(usuario["usuario"], "Creo alumno DNI " + dni, tb="alumnos")
        listar_todas_secciones.clear()
        return True, "Alumno " + nombres + " creado."
    except sqlite3.IntegrityError: return False, "Ya existe un alumno con DNI " + dni
    except sqlite3.Error as e:
        log.error("crear alumno: %s", e); return False, "Error al crear el alumno."

def editar_alumno(idal, apo_n, apo_t, idsec, dni, usuario):
    con = obtener_conexion(); ida = None
    with _lock_escritura:
        cur = con.cursor()
        if apo_n:
            f = cur.execute("SELECT id FROM apoderados WHERE nombre=? AND COALESCE(telefono,'')=?", (apo_n, apo_t or "")).fetchone()
            ida = f["id"] if f else cur.execute("INSERT INTO apoderados(nombre,telefono) VALUES(?,?)", (apo_n, apo_t or None)).lastrowid
        con.execute("UPDATE alumnos SET apoderado_id=?,nombre_apoderado=?,telefono_apoderado=?,seccion_id=? WHERE id=?",
                    (ida, apo_n or None, apo_t or None, idsec, idal)); con.commit()
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

# import excel
def _normalizar_grado(n):
    n = (n or "").strip().title()
    r = {"1°":"1ro","2°":"2do","3°":"3ro","4°":"4to","5°":"5to",
         "1o":"1ro","2o":"2do","3o":"3ro","4o":"4to","5o":"5to","1ero":"1ro","3ero":"3ro"}
    return r.get(n, n)

def validar_importacion(df, mapeo):
    errs = []; val = []; con = obtener_conexion(); vistos = {}
    for idx, fila in df.iterrows():
        nf = idx + 2
        try:
            dni = str(fila[mapeo["dni"]]).strip()
            nom = str(fila[mapeo["nombres"]]).strip()
            ap = str(fila[mapeo["apellido_paterno"]]).strip()
            am = str(fila[mapeo["apellido_materno"]]).strip() if mapeo.get("apellido_materno") else ""
            gr = _normalizar_grado(str(fila[mapeo["grado"]]))
            sec = str(fila[mapeo["seccion"]]).strip().upper()
            tur = str(fila[mapeo["turno"]]).strip().lower()
            an = str(fila[mapeo["apoderado_nombre"]]).strip() if mapeo.get("apoderado_nombre") else ""
            at = str(fila[mapeo["apoderado_telefono"]]).strip() if mapeo.get("apoderado_telefono") else ""
            if not dni: errs.append({"fila": nf, "motivo": "DNI vacio"}); continue
            if not re.fullmatch(r"\d{8}", dni): errs.append({"fila": nf, "motivo": "DNI invalido '" + dni + "'"}); continue
            if dni in vistos: errs.append({"fila": nf, "motivo": "DNI " + dni + " duplicado"}); continue
            if not nom or not ap or not gr or not sec: errs.append({"fila": nf, "motivo": "Faltan campos"}); continue
            if not con.execute("SELECT id FROM grados WHERE nombre=?", (gr,)).fetchone():
                errs.append({"fila": nf, "motivo": "Grado '" + gr + "' no existe"}); continue
            if tur in ("mañana","manana","m","am","mñ"): tn = "Mañana"
            elif tur in ("tarde","t","tm","pm"): tn = "Tarde"
            else: errs.append({"fila": nf, "motivo": "Turno '" + tur + "'"}); continue
            vistos[dni] = nf
            val.append({"dni": dni, "nombres": nom, "apellido_paterno": ap, "apellido_materno": am,
                        "grado": gr, "seccion": sec, "turno": tn, "apoderado_nombre": an, "apoderado_telefono": at})
        except (KeyError, ValueError, TypeError) as e:
            errs.append({"fila": nf, "motivo": "Error: " + str(e)})
    return val, errs, {"total": len(df), "validas": len(val), "errores": len(errs)}

def insertar_alumnos_validos(val):
    con = obtener_conexion(); cur = con.cursor()
    mapa_t = {f["nombre"]: f["id"] for f in cur.execute("SELECT id,nombre FROM turnos").fetchall()}
    per = obtener_periodo_activo()
    if not per: return 0, 0, ["No hay periodo activo."]
    pid = per["id"]; ins = 0; reac = 0; errs = []
    with _lock_escritura:
        for i, d in enumerate(val):
            try:
                fg = cur.execute("SELECT id FROM grados WHERE nombre=?", (d["grado"],)).fetchone()
                if not fg: errs.append("Fila " + str(i+1) + ": grado no reconocido"); continue
                it = mapa_t.get(d["turno"])
                if not it: errs.append("Fila " + str(i+1) + ": turno no encontrado"); continue
                fs = cur.execute("SELECT id FROM secciones WHERE nombre=? AND grado_id=? AND turno_id=?", (d["seccion"], fg["id"], it)).fetchone()
                idsec = fs["id"] if fs else cur.execute("INSERT INTO secciones(nombre,grado_id,turno_id) VALUES(?,?,?)", (d["seccion"], fg["id"], it)).lastrowid
                ida = None
                if d["apoderado_nombre"]:
                    fa = cur.execute("SELECT id FROM apoderados WHERE nombre=? AND COALESCE(telefono,'')=?", (d["apoderado_nombre"], d["apoderado_telefono"] or "")).fetchone()
                    ida = fa["id"] if fa else cur.execute("INSERT INTO apoderados(nombre,telefono) VALUES(?,?)", (d["apoderado_nombre"], d["apoderado_telefono"] or None)).lastrowid
                ex = cur.execute("SELECT id FROM alumnos WHERE dni=?", (d["dni"],)).fetchone()
                if ex:
                    cur.execute("UPDATE alumnos SET nombres=?,apellido_paterno=?,apellido_materno=?,seccion_id=?,apoderado_id=?,nombre_apoderado=?,telefono_apoderado=?,periodo_id=?,activo=1,retirado_en=NULL WHERE id=?",
                                (d["nombres"], d["apellido_paterno"], d["apellido_materno"], idsec, ida,
                                 d["apoderado_nombre"] or None, d["apoderado_telefono"] or None, pid, ex["id"]))
                    reac += 1
                else:
                    cur.execute("INSERT INTO alumnos(dni,nombres,apellido_paterno,apellido_materno,seccion_id,apoderado_id,nombre_apoderado,telefono_apoderado,periodo_id,activo) VALUES(?,?,?,?,?,?,?,?,?,1)",
                                (d["dni"], d["nombres"], d["apellido_paterno"], d["apellido_materno"], idsec, ida,
                                 d["apoderado_nombre"] or None, d["apoderado_telefono"] or None, pid))
                    ins += 1
            except sqlite3.Error as e: errs.append("Fila " + str(i+1) + ": " + str(e))
        con.commit()
    listar_todas_secciones.clear()
    return ins, reac, errs

# bloqueos
def alumno_bloqueado(idal):
    con = obtener_conexion()
    f = con.execute("SELECT * FROM bloqueos WHERE alumno_id=? AND activo=1 ORDER BY id DESC LIMIT 1", (idal,)).fetchone()
    return dict(f) if f else None

def crear_bloqueo(idal, motivo, usuario, origen="automatico"):
    escribir("INSERT INTO bloqueos(alumno_id,motivo,activo,fecha_inicio,creado_por,origen) VALUES(?,?,1,?,?,?)",
                (idal, motivo, hoy_str(), usuario["usuario"], origen))
    auditar(usuario["usuario"], "Bloqueo " + origen + " alumno_id=" + str(idal), tb="bloqueos", rid=idal)

def liberar_bloqueo(idal, usuario, obs=""):
    escribir("UPDATE bloqueos SET activo=0,fecha_fin=?,liberado_por=? WHERE alumno_id=? AND activo=1",
                (timestamp_str(), usuario["usuario"], idal))
    auditar(usuario["usuario"], "Libero bloqueo alumno_id=" + str(idal) + ". Obs: " + str(obs), tb="bloqueos", rid=idal)

# justificaciones y permisos
def contar_tardanzas_injustificadas(idal, pid=None):
    con = obtener_conexion()
    q = "SELECT COUNT(*) FROM tardanzas WHERE alumno_id=? AND justificada=0"; p = [idal]
    if pid is not None: q += " AND periodo_id=?"; p.append(pid)
    r = con.execute(q, p).fetchone()
    return r[0] or 0

def _aplicar_just_prev(con, idal, fecha, tipo):
    f = con.execute("SELECT id FROM justificaciones_previas WHERE alumno_id=? AND fecha_objetivo=? AND tipo=? AND aplicada=0",
                    (idal, fecha, tipo)).fetchone()
    if f:
        con.execute("UPDATE justificaciones_previas SET aplicada=1 WHERE id=?", (f["id"],)); return True
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
    if diff < 0:
        return False, "No se puede justificar una asistencia futura."
    if diff > 1:
        return False, "Pasaron mas de 24h. Ya no se puede justificar."
    return True, ""

def crear_justificacion_previa(idal, fecha_obj, tipo, motivo, usuario):
    return False, "Las justificaciones previas ya no estan disponibles."

def crear_permiso(idal, fi, ff, motivo, usuario):
    if ff < fi:
        return False, "La fecha fin no puede ser anterior a la fecha inicio."
    if not motivo or not motivo.strip():
        return False, "El motivo es obligatorio."
    per = obtener_periodo_activo(); pid = per["id"] if per else None
    escribir(
        "INSERT INTO permisos(alumno_id,fecha_inicio,fecha_fin,motivo,creado_por,timestamp,activo,periodo_id) "
        "VALUES(?,?,?,?,?,?,1,?)",
        (idal, fi, ff, motivo.strip(), usuario["usuario"], timestamp_str(), pid)
    )
    auditar(usuario["usuario"], "Creo permiso " + fi + " a " + ff + " id=" + str(idal), tb="permisos", rid=idal)
    return True, "Permiso registrado."

def listar_permisos(solo_activos=True):
    con = obtener_conexion()
    q = ("SELECT p.id,p.fecha_inicio,p.fecha_fin,COALESCE(p.motivo,'') AS motivo,p.activo,"
         "p.creado_por,p.timestamp,a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,"
         "a.nombres,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno "
         "FROM permisos p JOIN alumnos a ON p.alumno_id=a.id "
         "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
         "JOIN turnos t ON s.turno_id=t.id")
    if solo_activos: q += " WHERE p.activo=1"
    q += " ORDER BY p.fecha_inicio DESC"
    return pd.read_sql(q, con)

# asistencia
def registrar_entrada(dni, usuario, origen="qr"):
    dni = (dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni): return False, "ERROR", "DNI invalido", {}
    con = obtener_conexion()
    al = _buscar_alumno_por_dni(con, dni)
    if not al: return False, "ERROR", "DNI no encontrado", {}
    bloq = alumno_bloqueado(al["id"])
    if bloq:
        auditar(usuario["usuario"], "Intento escaneo bloqueado DNI " + dni, tb="bloqueos", rid=al["id"])
        return False, "BLOQUEADO", _nombre_completo(al) + " | BLOQUEADO - retener y llevar a TOECE", {"alumno": al, "motivo": bloq["motivo"]}
    fecha = hoy_str(); ha = hora_corta(); hc = hora_str()
    per = obtener_periodo_activo(); pid = per["id"] if per else None
    dia = dia_especial_hoy(al["turno_id"], fecha, al["seccion_id"])
    if dia and dia["tipo"] == "feriado": return False, "ERROR", "Hoy es feriado, no se registra", {}
    v = ventana_activa_para_alumno(al["turno_id"], fecha, al["seccion_id"])
    if not v: return False, "ERROR", "Sin ventana activa (" + ha + ")", {}

    permiso = hay_permiso_activo(al["id"], fecha)

    tipo_real = v["tipo"]
    if v.get("es_evento") and v["tipo"] == VENT_CLASES:
        tipo_real = TIPO_ASIST_EVENTO

    ex = con.execute("SELECT id,estado FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo=?", (al["id"], fecha, tipo_real)).fetchone()
    if ex: return False, "ERROR", _nombre_completo(al) + " ya registro " + tipo_real + " hoy (" + ex["estado"] + ")", {}

    if v["tipo"] == VENT_REF:
        with _lock_escritura:
            con.execute("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,origen,periodo_id) VALUES(?,?,?,'reforzamiento',?,?,?,?,?)",
                        (al["id"], fecha, v["id"], hc, REF_ASISTIO, 1 if permiso else 0, origen, pid))
            if al["turno"] == "Tarde":
                ya = con.execute("SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo='clases'", (al["id"], fecha)).fetchone()
                if not ya:
                    con.execute("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,origen,periodo_id) VALUES(?,?,NULL,'clases',?,'Puntual',?,?,?)",
                                (al["id"], fecha, hc, 1 if permiso else 0, origen, pid))
            con.commit()
        auditar(usuario["usuario"], "Reforzamiento " + origen + " DNI " + dni, tb="asistencias")
        msg = (_nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] + " | Reforzamiento + Clases Puntual " + ha
               if al["turno"] == "Tarde" else
               _nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] + " | Asistio a reforzamiento " + ha)
        return True, "REFORZAMIENTO", msg, {"alumno": al}

    if al["turno"] == "Tarde":
        ya = con.execute("SELECT id,estado,hora FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo='clases'", (al["id"], fecha)).fetchone()
        if ya: return False, "ERROR", _nombre_completo(al) + " ya tiene clases hoy (" + ya["estado"] + " " + (ya["hora"] or "") + ").", {}

    lim = v["hora_limite_efectiva"]
    est = PUNTUAL if ha <= lim else TARDANZA

    if est == TARDANZA:
        jp = _aplicar_just_prev(con, al["id"], fecha, "Tardanza")
    else:
        jp = False

    just_final = 1 if (jp or permiso) else 0

    with _lock_escritura:
        con.execute("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,origen,periodo_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (al["id"], fecha, v["id"], tipo_real, hc, est, just_final, origen, pid))

        if est == TARDANZA:
            n = contar_tardanzas_injustificadas(al["id"], pid) + 1
            acc = ACC_PERDONADO if n <= 2 else (ACC_DERIVADO if n == 3 else ACC_RETENIDO)
            try:
                con.execute("INSERT INTO tardanzas(alumno_id,fecha,hora,numero,accion,justificada,origen,registrado_por,timestamp,periodo_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (al["id"], fecha, hc, n, acc, just_final, origen, usuario["usuario"], timestamp_str(), pid))
            except sqlite3.IntegrityError:
                log.warning("tardanza duplicada alumno_id=%s fecha=%s", al["id"], fecha)
            except sqlite3.Error as e:
                log.error("error al insertar tardanza: %s", e)
            con.commit()
        else:
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
    fecha = hoy_str(); ha = hora_corta()
    con = obtener_conexion(); per = obtener_periodo_activo(); pid = per["id"] if per else None
    for t in listar_turnos():
        if es_fin_de_semana():
            if not con.execute("SELECT tipo FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento'", (fecha,)).fetchone():
                continue
        for v in listar_ventanas(t["id"]):
            if ha < v["hora_cierre"]: continue
            tipo = "clases" if v["tipo"] == VENT_CLASES else "reforzamiento"
            est = "Falta" if v["tipo"] == VENT_CLASES else "No asistio"
            for al in con.execute("SELECT a.id FROM alumnos a JOIN secciones s ON a.seccion_id=s.id WHERE s.turno_id=? AND a.activo=1", (t["id"],)).fetchall():
                if con.execute("SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo=?", (al["id"], fecha, tipo)).fetchone():
                    continue
                if hay_permiso_activo(al["id"], fecha):
                    escribir(
                        "INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,observacion,origen,periodo_id) "
                        "VALUES(?,?,?,?,?,?,1,?,?,?)",
                        (al["id"], fecha, v["id"], tipo, hora_str(), PERMISO, "Permiso otorgado", "manual", pid)
                    )
                    continue
                escribir(
                    "INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,origen,periodo_id) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (al["id"], fecha, v["id"], tipo, hora_str(), est, "auto", pid)
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
    escribir("UPDATE asistencias SET justificada=1,observacion=?,justificado_por=?,justificado_en=? WHERE id=?",
                (obs.strip(), usuario["usuario"], timestamp_str(), ida))
    auditar(usuario["usuario"], "Justifico asistencia id=" + str(ida) + " motivo: " + obs, tb="asistencias", rid=ida)
    return True, "Asistencia justificada."

def quitar_justificacion(ida, usuario):
    con = obtener_conexion()
    reg = con.execute("SELECT * FROM asistencias WHERE id=?", (ida,)).fetchone()
    if not reg: return False, "Registro no encontrado."
    escribir("UPDATE asistencias SET justificada=0,observacion=NULL,justificado_por=NULL,justificado_en=NULL WHERE id=?", (ida,))
    auditar(usuario["usuario"], "Quito justificacion id=" + str(ida), tb="asistencias", rid=ida)
    return True, "Justificacion eliminada."

# ============================================================
# ESCANEO QR - COMPONENTE PROPIO
# ============================================================

def _procesar_escaneo(dni):
    u = st.session_state.get("user")
    if not u: return
    ok, tipo, msg, extra = registrar_entrada(dni, u, origen="qr")

    # Clasificar feedback para el componente JS
    if not ok and tipo == "ERROR":
        if "ya registro" in msg or "ya tiene" in msg:
            feedback = "duplicado"
        else:
            feedback = "error"
    elif tipo == "BLOQUEADO":
        feedback = "bloqueado"
    elif ok:
        feedback = "nuevo"
    else:
        feedback = "error"

    st.session_state.setdefault("_qr_mensajes", [])
    st.session_state["_qr_mensajes"].insert(0, {
        "dni": dni, "tipo": tipo, "mensaje": msg,
        "extra": extra, "ts": time.time(), "feedback": feedback
    })
    st.session_state["_qr_mensajes"] = st.session_state["_qr_mensajes"][:10]

    # Guardar feedback pendiente para disparar JS en el proximo render
    st.session_state["_qr_feedback_pendiente"] = {
        "kind": feedback,
        "texto": msg[:60],
        "ts": time.time(),
    }

def _render_mensaje_qr(msg):
    tipo = msg["tipo"]; mensaje = msg["mensaje"]
    clase = {"PUNTUAL":"qr-puntual","TARDANZA":"qr-tardanza","REFORZAMIENTO":"qr-refuerzo",
             "BLOQUEADO":"qr-bloqueado","ERROR":"qr-error"}.get(tipo, "qr-error")

    if tipo == "TARDANZA":
        acc = (msg.get("extra") or {}).get("accion")
        if acc == ACC_DERIVADO:
            mensaje += " -> Derivar a TOECE"; clase = "qr-derivado"
        elif acc == ACC_RETENIDO:
            mensaje += " -> Retener hasta apoderado"; clase = "qr-retenido"

    st.markdown(
        '<div class="qr-msg ' + clase + '"><div class="qr-texto">' +
        mensaje + '</div></div>',
        unsafe_allow_html=True
    )
def escaner_qr_continuo(key="qr_scanner"):
    st.markdown(
        '<div class="scan-header"><div class="scan-titulo">Escaneo QR</div>'
        '<div class="scan-sub">Apunta al codigo del alumno</div></div>',
        unsafe_allow_html=True
    )

    def _on_scan():
        pass

    # Key unica por montaje para forzar iframe nuevo al volver a la vista
    mount_id = st.session_state.get("_qr_mount_id", 0)
    result = qr_scanner(key="qr_" + key + "_" + str(mount_id), on_scan=_on_scan)

    if result is not None and getattr(result, "qr_dni", None):
        dni = result.qr_dni
        ult = st.session_state.get("_ultimo_qr_scan", {})
        if not (ult.get("dni") == dni and (time.time() - ult.get("ts", 0)) < 3):
            st.session_state["_ultimo_qr_scan"] = {"dni": dni, "ts": time.time()}
            _procesar_escaneo(dni)

    # Puente Python -> JS: pitido diferenciado + cartel visual
    fb = st.session_state.get("_qr_feedback_pendiente")
    if fb and (time.time() - fb.get("ts", 0)) < 5:
        texto_js = fb["texto"].replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")[:60]
        st.components.v1.html(f"""
            <script>
            (function() {{
                let tries = 0;
                const buscar = () => {{
                    tries++;
                    try {{
                        const frames = document.querySelectorAll('iframe');
                        for (const f of frames) {{
                            try {{
                                const w = f.contentWindow;
                                if (w && w.__qrFeedback) {{
                                    w.__qrFeedback('{fb["kind"]}', '{texto_js}');
                                    return;
                                }}
                            }} catch(e) {{}}
                        }}
                    }} catch(e) {{}}
                    if (tries < 40) setTimeout(buscar, 100);
                }};
                buscar();
            }})();
            </script>
        """, height=0)
        st.session_state.pop("_qr_feedback_pendiente", None)

    if st.session_state.get("_qr_mensajes"):
        st.markdown('<div class="scan-ultimos">Ultimos escaneos</div>', unsafe_allow_html=True)
        for msg in st.session_state["_qr_mensajes"][:5]:
            _render_mensaje_qr(msg)

# reportes base
def metricas_dia(fecha, pid=None):
    con = obtener_conexion()
    total = con.execute("SELECT COUNT(*) FROM alumnos WHERE activo=1").fetchone()[0]
    puntuales = con.execute("SELECT COUNT(*) FROM asistencias WHERE fecha=? AND tipo='clases' AND estado='Puntual'", (fecha,)).fetchone()[0]
    tardanzas = con.execute("SELECT COUNT(*) FROM asistencias WHERE fecha=? AND tipo='clases' AND estado='Tardanza'", (fecha,)).fetchone()[0]
    faltas = con.execute("SELECT COUNT(*) FROM asistencias WHERE fecha=? AND tipo='clases' AND estado='Falta'", (fecha,)).fetchone()[0]
    permisos_hoy = con.execute("SELECT COUNT(*) FROM asistencias WHERE fecha=? AND estado='Permiso'", (fecha,)).fetchone()[0]
    ref_asistio = con.execute("SELECT COUNT(*) FROM asistencias WHERE fecha=? AND tipo='reforzamiento' AND estado='Asistio'", (fecha,)).fetchone()[0]
    bloqueados = con.execute("SELECT COUNT(*) FROM bloqueos WHERE activo=1").fetchone()[0]
    just_hoy = con.execute("SELECT COUNT(*) FROM asistencias WHERE fecha=? AND justificada=1", (fecha,)).fetchone()[0]
    return {"total": total, "puntuales": puntuales, "tardanzas": tardanzas,
            "faltas": faltas, "ref_asistio": ref_asistio, "bloqueados": bloqueados,
            "justificadas": just_hoy, "permisos": permisos_hoy}

def ultimos_registros(fecha, limite=20):
    return pd.read_sql("SELECT a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,ast.tipo,ast.hora,ast.estado FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE ast.fecha=? AND ast.hora IS NOT NULL ORDER BY ast.hora DESC LIMIT ?",
                       obtener_conexion(), params=[fecha, limite])

def reporte_detalle_por_tipo(inicio, fin, ids_sec, tipo_reporte, pid=None):
    con = obtener_conexion()
    q = ("SELECT a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres,"
         "ast.fecha,ast.tipo,ast.estado,ast.justificada,ast.origen,"
         "COALESCE(ast.justificado_por,'') AS justificado_por,COALESCE(ast.justificado_en,'') AS justificado_en,"
         "COALESCE(ast.observacion,'') AS observacion "
         "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id "
         "WHERE ast.fecha BETWEEN ? AND ? AND ast.hora IS NOT NULL AND ast.tipo=?")
    p = [inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d"), tipo_reporte]
    if ids_sec:
        q += " AND s.id IN (" + ",".join(["?"]*len(ids_sec)) + ")"; p += ids_sec
    if pid is not None: q += " AND ast.periodo_id=?"; p.append(pid)
    q += " ORDER BY ast.fecha DESC, a.apellido_paterno"
    return pd.read_sql(q, con, params=p)

def reporte_conteo_faltas(inicio, fin, ids_sec, pid=None):
    con = obtener_conexion()
    q = ("SELECT a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres,"
         "SUM(CASE WHEN ast.justificada=1 THEN 1 ELSE 0 END) AS faltas_just,"
         "SUM(CASE WHEN ast.justificada=0 THEN 1 ELSE 0 END) AS faltas_injust,COUNT(*) AS total_faltas "
         "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id "
         "WHERE ast.estado='Falta' AND ast.tipo='clases' AND ast.fecha BETWEEN ? AND ?")
    p = [inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")]
    if ids_sec:
        q += " AND s.id IN (" + ",".join(["?"]*len(ids_sec)) + ")"; p += ids_sec
    if pid is not None: q += " AND ast.periodo_id=?"; p.append(pid)
    q += " GROUP BY a.id ORDER BY faltas_injust DESC, total_faltas DESC"
    return pd.read_sql(q, con, params=p)

def cierre_mensual_calendario(mes, anio, ids_sec, pid=None):
    ult = monthrange(anio, mes)[1]
    ini = date(anio, mes, 1)
    fin = date(anio, mes, ult)
    con = obtener_conexion()
    dias = []
    for d in range(1, ult + 1):
        f = date(anio, mes, d)
        if f.weekday() >= 5:
            esp = con.execute("SELECT id FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento'", (f.strftime("%Y-%m-%d"),)).fetchone()
            if not esp:
                continue
        dias.append(f)
    ph = ",".join(["?"] * len(ids_sec))
    df_al = pd.read_sql(
        "SELECT a.id AS alumno_id, a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos, a.nombres, "
        "g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno "
        "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
        "JOIN turnos t ON s.turno_id=t.id "
        "WHERE a.seccion_id IN (" + ph + ") AND a.activo=1 "
        "ORDER BY t.nombre, g.nombre, s.nombre, a.apellido_paterno",
        con, params=ids_sec
    )
    if df_al.empty:
        return pd.DataFrame()
    abre = {"Puntual": "PUN", "Falta": "FAL", "Tardanza": "TAR", "Permiso": "PER"}
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
        if est == "Falta" and r["justificada"]:
            est = "JUS"
        else:
            est = abre.get(est, est[:3].upper())
        asis[key] = est
    filas = []
    for _, al in df_al.iterrows():
        fila = {"Apellidos": al["apellidos"], "Nombres": al["nombres"],
                "Grado": al["grado"], "Seccion": al["seccion"], "Turno": al["turno"]}
        tp = tf = tt = tj = 0
        for d in dias:
            f_str = d.strftime("%Y-%m-%d")
            etiqueta = ["Lun","Mar","Mie","Jue","Vie","Sab","Dom"][d.weekday()] + " " + str(d.day).zfill(2)
            est = asis.get((al["alumno_id"], f_str), "-")
            fila[etiqueta] = est
            if est == "PUN": tp += 1
            elif est == "FAL": tf += 1
            elif est == "TAR": tt += 1
            elif est == "JUS": tj += 1
        fila["Total PUN"] = tp
        fila["Total FAL"] = tf
        fila["Total TAR"] = tt
        fila["Total JUS"] = tj
        filas.append(fila)
    df = pd.DataFrame(filas)
    cols_base = ["Apellidos", "Nombres", "Grado", "Seccion", "Turno"]
    cols_dias = [c for c in df.columns if c not in cols_base and not c.startswith("Total")]
    cols_tot = ["Total PUN", "Total FAL", "Total TAR", "Total JUS"]
    return df[cols_base + cols_dias + cols_tot]

def casos_toece(pid=None):
    con = obtener_conexion()
    filt = ""; p = []
    if pid is not None: filt = " AND t2.periodo_id=?"; p.append(pid)
    return pd.read_sql("""
        SELECT a.id,a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres,
        g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,a.nombre_apoderado,a.telefono_apoderado,
        COUNT(t2.id) AS tard_injust,
        (SELECT COUNT(*) FROM actas_compromiso WHERE alumno_id=a.id) AS total_actas,
        CASE WHEN EXISTS(SELECT 1 FROM bloqueos WHERE alumno_id=a.id AND activo=1) THEN 'SI' ELSE 'NO' END AS bloqueado
        FROM tardanzas t2 JOIN alumnos a ON t2.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id
        JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
        WHERE t2.justificada=0 """ + filt + """
        GROUP BY a.id HAVING COUNT(t2.id)>=3 ORDER BY tard_injust DESC
        """, con, params=p)

def listar_observados(solo_activos=True):
    con = obtener_conexion()
    q = ("SELECT o.id,a.id AS alumno_id,a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,"
         "a.nombres,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,o.fecha_ingreso,COALESCE(o.motivo,'') AS motivo,"
         "o.activo,COALESCE(o.fecha_salida,'') AS fecha_salida,COALESCE(o.observacion_cierre,'') AS observacion_cierre "
         "FROM observados o JOIN alumnos a ON o.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id "
         "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id")
    if solo_activos: q += " WHERE o.activo=1"
    q += " ORDER BY o.fecha_ingreso DESC"
    return pd.read_sql(q, con)

def listar_bloqueados():
    return pd.read_sql("SELECT b.id,a.id AS alumno_id,a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS alumno,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,b.motivo,b.fecha_inicio,COALESCE(b.origen,'automatico') AS origen FROM bloqueos b JOIN alumnos a ON b.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE b.activo=1 ORDER BY b.fecha_inicio DESC",
                       obtener_conexion())

def obtener_auditoria(limite=500):
    return pd.read_sql("SELECT id,usuario,accion,fecha,ip FROM auditoria ORDER BY id DESC LIMIT " + str(int(limite)),
                       obtener_conexion())

def reporte_general_por_seccion(desde, hasta, pid=None):
    con = obtener_conexion()
    q = """
    SELECT g.nombre AS grado, s.nombre AS seccion, s.id AS seccion_id, t.nombre AS turno,
    SUM(CASE WHEN ast.estado='Puntual' AND ast.tipo='clases' THEN 1 ELSE 0 END) AS puntuales,
    SUM(CASE WHEN ast.estado='Falta' AND ast.tipo='clases' THEN 1 ELSE 0 END) AS faltas
    FROM secciones s
    JOIN grados g ON s.grado_id=g.id
    JOIN turnos t ON s.turno_id=t.id
    LEFT JOIN asistencias ast ON ast.alumno_id IN (SELECT id FROM alumnos WHERE seccion_id=s.id)
        AND ast.fecha BETWEEN ? AND ?
    """
    p = [desde.strftime("%Y-%m-%d"), hasta.strftime("%Y-%m-%d")]
    if pid is not None:
        q += " AND ast.periodo_id=?"
        p.append(pid)
    q += " GROUP BY s.id ORDER BY t.nombre, g.nombre, s.nombre"
    df = pd.read_sql(q, con, params=p)
    aux_por_seccion = {}
    for _, r in df.iterrows():
        sid = r["seccion_id"]
        aux = con.execute("""
            SELECT u.nombres
            FROM usuarios u
            JOIN auxiliar_secciones au ON au.usuario_id=u.id
            WHERE au.seccion_id=? AND u.rol='Auxiliar' AND u.activo=1
            LIMIT 1
        """, (sid,)).fetchone()
        if aux:
            aux_por_seccion[sid] = aux["nombres"]
        else:
            aux_por_seccion[sid] = "(sin auxiliar asignado)"
    df["auxiliar"] = df["seccion_id"].map(aux_por_seccion)
    return df[["turno", "grado", "seccion", "puntuales", "faltas", "auxiliar"]]

# perfil
def perfil_alumno_datos(idal):
    con = obtener_conexion()
    a = con.execute("SELECT a.*,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,s.id AS seccion_id,t.id AS turno_id FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE a.id=?",
                    (idal,)).fetchone()
    if not a: return {}
    a = dict(a)
    da = pd.read_sql("SELECT id,fecha,hora,tipo,estado,justificada,COALESCE(observacion,'') AS observacion,COALESCE(origen,'qr') AS origen,COALESCE(justificado_por,'') AS justificado_por,COALESCE(justificado_en,'') AS justificado_en FROM asistencias WHERE alumno_id=? ORDER BY fecha DESC,hora DESC", con, params=[idal])
    dt = pd.read_sql("SELECT fecha,hora,numero AS 'N',accion,justificada,COALESCE(origen,'qr') AS origen,COALESCE(observacion,'') AS observacion FROM tardanzas WHERE alumno_id=? ORDER BY fecha DESC,hora DESC", con, params=[idal])
    do = pd.read_sql("SELECT fecha_ingreso,COALESCE(fecha_salida,'-') AS fecha_salida,COALESCE(motivo,'') AS motivo,activo FROM observados WHERE alumno_id=? ORDER BY fecha_ingreso DESC", con, params=[idal])
    dac = pd.read_sql("SELECT fecha,COALESCE(motivo,'') AS motivo,COALESCE(observacion,'') AS observacion,COALESCE(registrado_por,'') AS registrado_por FROM actas_compromiso WHERE alumno_id=? ORDER BY fecha DESC", con, params=[idal])
    db = pd.read_sql("SELECT fecha_inicio,COALESCE(fecha_fin,'-') AS fecha_fin,COALESCE(motivo,'') AS motivo,activo FROM bloqueos WHERE alumno_id=? ORDER BY fecha_inicio DESC", con, params=[idal])
    dp = pd.read_sql("SELECT fecha_inicio,fecha_fin,COALESCE(motivo,'') AS motivo,activo,creado_por FROM permisos WHERE alumno_id=? ORDER BY fecha_inicio DESC", con, params=[idal])
    tp = int((da["estado"] == PUNTUAL).sum()) if not da.empty else 0
    tf = int((da["estado"] == FALTA).sum()) if not da.empty else 0
    tt = int((da["estado"] == TARDANZA).sum()) if not da.empty else 0
    tr = int(((da["tipo"] == "reforzamiento") & (da["estado"] == REF_ASISTIO)).sum()) if not da.empty else 0
    return {"alumno": a, "asistencias": da, "tardanzas": dt, "observados": do, "actas": dac,
            "bloqueos": db, "permisos": dp,
            "total_puntuales": tp, "total_faltas": tf,
            "total_tardanzas": tt, "total_ref_asistio": tr,
            "tard_injust": contar_tardanzas_injustificadas(idal),
            "bloqueado": alumno_bloqueado(idal) is not None}

# cierre anual
def reporte_cierre_anual(pid):
    return pd.read_sql("""
        SELECT g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,COUNT(DISTINCT a.id) AS total_alumnos,
        (SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id=? AND ast.alumno_id IN (SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?) AND ast.estado='Puntual') AS puntuales,
        (SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id=? AND ast.alumno_id IN (SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?) AND ast.estado='Tardanza') AS tardanzas,
        (SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id=? AND ast.alumno_id IN (SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?) AND ast.estado='Falta' AND ast.justificada=0) AS faltas_injust,
        (SELECT COUNT(*) FROM asistencias ast WHERE ast.periodo_id=? AND ast.alumno_id IN (SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?) AND ast.estado='Falta' AND ast.justificada=1) AS faltas_just
        FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
        LEFT JOIN alumnos a ON a.seccion_id=s.id AND a.periodo_id=?
        GROUP BY s.id ORDER BY t.nombre,g.nombre,s.nombre
        """, obtener_conexion(), params=[pid]*9)

def reporte_detallado_por_mes(pid):
    con = obtener_conexion()
    p = con.execute("SELECT * FROM periodos WHERE id=?", (pid,)).fetchone()
    if not p: return {}
    fi = datetime.strptime(p["fecha_inicio"], "%Y-%m-%d").date()
    ff = datetime.strptime(p["fecha_fin"], "%Y-%m-%d").date()
    res = {}; cur = date(fi.year, fi.month, 1)
    while cur <= ff:
        um = monthrange(cur.year, cur.month)[1]
        im = max(cur.replace(day=1), fi); fm = min(cur.replace(day=um), ff)
        df = pd.read_sql("SELECT a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,ast.fecha,ast.hora,ast.tipo,ast.estado,ast.justificada,COALESCE(ast.observacion,'') AS observacion FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE ast.periodo_id=? AND ast.fecha BETWEEN ? AND ? ORDER BY ast.fecha,t.nombre,g.nombre,s.nombre,a.apellido_paterno",
                          con, params=[pid, im.strftime("%Y-%m-%d"), fm.strftime("%Y-%m-%d")])
        res[str(cur.year) + "-" + str(cur.month).zfill(2)] = df
        cur = date(cur.year+1, 1, 1) if cur.month == 12 else date(cur.year, cur.month+1, 1)
    return res

def cerrar_anio_escolar(usuario, pid, nuevo_nombre, fi, ff):
    con = obtener_conexion()
    p = con.execute("SELECT * FROM periodos WHERE id=?", (pid,)).fetchone()
    if not p: return False, "Periodo no encontrado."
    if p["cerrado"]: return False, "Ese periodo ya esta cerrado."
    rep_json = None
    try:
        rep = reporte_cierre_anual(pid)
        if not rep.empty: rep_json = rep.to_json(orient="records", force_ascii=False)
    except Exception: rep_json = None
    fecha = timestamp_str()
    try:
        with _lock_escritura:
            con.execute("INSERT INTO cierres_anuales(periodo_id,fecha_cierre,generado_por,reporte_json) VALUES(?,?,?,?)",
                        (pid, fecha, usuario["usuario"], rep_json))
            con.execute("UPDATE periodos SET activo=0,fecha_cierre=?,cerrado=1 WHERE id=?", (fecha, pid))
            con.execute("UPDATE alumnos SET activo=0,retirado_en=? WHERE periodo_id=?", (fecha, pid))
            con.execute("INSERT INTO periodos(nombre,fecha_inicio,fecha_fin,activo,cerrado) VALUES(?,?,?,1,0)", (nuevo_nombre, fi, ff))
            con.commit()
        listar_periodos.clear()
    except sqlite3.Error as e:
        con.rollback(); log.error("cerrar anio: %s", e); return False, "Error al cerrar el anio."
    auditar(usuario["usuario"], "Cerro periodo " + p["nombre"], tb="periodos", rid=pid)
    return True, "Periodo '" + p["nombre"] + "' cerrado. Nuevo: '" + nuevo_nombre + "'."

def listar_cierres_anuales():
    return pd.read_sql("SELECT c.id,p.nombre AS periodo,c.fecha_cierre,c.generado_por FROM cierres_anuales c JOIN periodos p ON c.periodo_id=p.id ORDER BY c.id DESC",
                       obtener_conexion())

# pdf / excel / qr
def _pdf_base(titulo, subtitulo=None, paisaje=False):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(A4[1],A4[0]) if paisaje else A4, rightMargin=25, leftMargin=25, topMargin=25, bottomMargin=25)
    est = getSampleStyleSheet()
    el = [Paragraph("<b>" + titulo + "</b>", est["Heading1"])]
    if subtitulo: el.append(Paragraph(subtitulo, est["Normal"]))
    el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"), est["Normal"]))
    el.append(Spacer(1, 15))
    return buf, doc, el, est

def generar_pdf_tabla(df, titulo, subtitulo=None):
    buf, doc, el, _ = _pdf_base(titulo, subtitulo, paisaje=len(df.columns) > 6)
    if not df.empty:
        datos = [df.columns.tolist()] + df.astype(str).values.tolist()
        t = Table(datos, repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor(C_NARANJA)),
                                ("TEXTCOLOR",(0,0),(-1,0),colors.whitesmoke),
                                ("GRID",(0,0),(-1,-1),0.4,colors.grey),
                                ("FONTSIZE",(0,0),(-1,-1),7)]))
        el.append(t)
    doc.build(el); buf.seek(0); return buf.getvalue()

def generar_qr(dni):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(str(dni).strip()); qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")

def _render_carnets(filas, titulo=None):
    buf = BytesIO(); m = 8
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=m, leftMargin=m, topMargin=m, bottomMargin=m)
    est = getSampleStyleSheet()
    if titulo is None:
        titulo = ("Carnets - " + filas[0]['grado'] + " " + filas[0]['seccion']) if len(filas) > 1 else ("Carnet - " + filas[0]['apellido_paterno'] + " " + (filas[0]['apellido_materno'] or "") + ", " + filas[0]['nombres'])
    el = [Paragraph(titulo, est["Heading1"]), Spacer(1, 6)]
    cols, rows = 3, 3; pp = cols*rows
    aw = (A4[0]-2*m)/cols; ah = (A4[1]-2*m-50)/rows
    for i in range(0, len(filas), pp):
        lote = filas[i:i+pp]; tabla = []
        for j in range(0, len(lote), cols):
            fila = []
            for a in lote[j:j+cols]:
                qb = BytesIO(); generar_qr(a["dni"]).save(qb, format="PNG"); qb.seek(0)
                fila.append([Paragraph("<b><font size=11>" + a['apellido_paterno'] + " " + (a['apellido_materno'] or "") + "</font></b>", est["Normal"]),
                             Paragraph("<font size=10>" + a['nombres'] + "</font>", est["Normal"]),
                             Paragraph("<font size=10>DNI: " + a['dni'] + "</font>", est["Normal"]),
                             Paragraph("<font size=9>" + a['grado'] + " " + a['seccion'] + " - " + a['turno'] + "</font>", est["Normal"]),
                             RLImage(qb, width=150, height=150)])
            while len(fila) < cols: fila.append([])
            tabla.append(fila)
        while len(tabla) < rows: tabla.append([[] for _ in range(cols)])
        t = Table(tabla, colWidths=[aw]*cols, rowHeights=[ah]*rows)
        t.setStyle(TableStyle([("ALIGN",(0,0),(-1,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                                ("BOX",(0,0),(-1,-1),1.5,colors.HexColor(C_NARANJA)),
                                ("INNERGRID",(0,0),(-1,-1),1,colors.grey)]))
        el.append(t)
        if i+pp < len(filas): el.append(PageBreak())
    doc.build(el); buf.seek(0); return buf.getvalue()

def _filas_alumnos_por_seccion(idsec):
    con = obtener_conexion()
    return con.execute("SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE a.seccion_id=? AND a.activo=1 ORDER BY a.apellido_paterno,a.apellido_materno",
                       (idsec,)).fetchall()

def pdf_carnets_por_seccion(idsec):
    f = _filas_alumnos_por_seccion(idsec)
    return _render_carnets(f) if f else None

def pdf_carnets_seleccionados(ids, titulo=None):
    if not ids: return None
    con = obtener_conexion()
    ph = ",".join("?"*len(ids))
    f = con.execute("SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE a.id IN (" + ph + ") AND a.activo=1 ORDER BY a.apellido_paterno,a.apellido_materno",
                    ids).fetchall()
    return _render_carnets(f, titulo) if f else None

def pdf_carnet_alumno(dni):
    con = obtener_conexion()
    f = con.execute("SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE a.dni=? AND a.activo=1",
                    (dni,)).fetchall()
    return _render_carnets(f) if f else None

def pdf_resumen_alumno(idal):
    d = perfil_alumno_datos(idal)
    if not d: return None
    al = d["alumno"]
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
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
               [d["total_puntuales"], d["total_tardanzas"], d["total_faltas"], d["total_ref_asistio"], d["tard_injust"]]]
    t = Table(resumen)
    t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor(C_NARANJA)),
                            ("TEXTCOLOR",(0,0),(-1,0),colors.whitesmoke),
                            ("ALIGN",(0,0),(-1,-1),"CENTER"),
                            ("GRID",(0,0),(-1,-1),0.5,colors.grey)]))
    el.append(t); el.append(Spacer(1, 20))
    el.append(Paragraph("<b>Ultimas asistencias</b>", est["Heading2"]))
    if not d["asistencias"].empty:
        datos = [["Fecha", "Tipo", "Estado", "Justificada"]]
        for _, r in d["asistencias"].head(30).iterrows():
            datos.append([r["fecha"], r["tipo"], r["estado"], "Si" if r["justificada"] else "No"])
        t2 = Table(datos, repeatRows=1)
        t2.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor(C_NARANJA)),
                                ("TEXTCOLOR",(0,0),(-1,0),colors.whitesmoke),
                                ("GRID",(0,0),(-1,-1),0.3,colors.grey),
                                ("FONTSIZE",(0,0),(-1,-1),8)]))
        el.append(t2)
    el.append(Spacer(1, 30))
    el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"), est["Normal"]))
    doc.build(el); buf.seek(0); return buf.getvalue()

def df_a_xlsx(df, hoja="Datos"):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w: df.to_excel(w, index=False, sheet_name=hoja)
    buf.seek(0); return buf.getvalue()

def df_a_xlsx_multilhoja(hojas):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for n, df in hojas.items(): df.to_excel(w, index=False, sheet_name=n[:31])
    buf.seek(0); return buf.getvalue()

# estilos
def aplicar_estilos():
    st.markdown("""
    <style>
    html, body, [class*="css"] {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
        letter-spacing: -0.011em;
    }
    h1, h2, h3, h4 { font-weight: 700 !important; }
    h1 { font-size: 1.9rem !important; }
    h2 { font-size: 1.4rem !important; }
    h3 { font-size: 1.15rem !important; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-thumb { background: #888888; border-radius: 10px; }
    section[data-testid="stSidebar"] .stRadio label {
        border-radius: 6px !important; padding: 10px 12px !important;
        font-weight: 500 !important; font-size: 14px !important;
    }
    section[data-testid="stSidebar"] .stRadio label:hover { background: rgba(128, 128, 128, 0.15) !important; }
    section[data-testid="stSidebar"] .stRadio label:has(input:checked) { background: rgba(128, 128, 128, 0.25) !important; font-weight: 600 !important; }
    section[data-testid="stSidebar"] .stRadio input { display: none; }
    .encabezado-sidebar {
        border: 1px solid rgba(128, 128, 128, 0.3); padding: 16px 14px;
        margin: 10px 8px; border-radius: 8px; text-align: center;
    }
    .encabezado-sidebar .avatar {
        width: 52px; height: 52px; border-radius: 50%; background: #808080;
        display: flex; align-items: center; justify-content: center;
        font-size: 20px; font-weight: 700; color: #FFFFFF; margin: 0 auto 10px auto;
    }
    .encabezado-sidebar .nombre { font-size: 14px; font-weight: 700; }
    .encabezado-sidebar .rol {
        display: inline-block; margin-top: 6px; padding: 3px 10px;
        background: #808080; border-radius: 10px; font-size: 10px; font-weight: 700;
        text-transform: uppercase; letter-spacing: 0.08em; color: #FFFFFF !important;
    }
    .stTextInput input, .stNumberInput input, .stDateInput input,
    .stTimeInput input, .stTextArea textarea, .stSelectbox > div > div {
        border-radius: 6px !important; min-height: 44px;
    }
    .login-form .stTextInput input {
        border: none !important; border-bottom: 1px solid rgba(128,128,128,0.4) !important;
        border-radius: 0 !important; background: transparent !important;
        box-shadow: none !important; padding-left: 0 !important;
    }
    .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
        background: #808080 !important; color: #FFFFFF !important;
        border-radius: 8px !important; font-weight: 600 !important;
        border: 1px solid #808080 !important; padding: 11px 20px !important;
        box-shadow: none !important; font-size: 14px !important; min-height: 44px;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover, .stDownloadButton > button:hover {
        background: #666666 !important; border-color: #666666 !important;
    }
    div[data-testid="stMetric"] {
        border: 1px solid rgba(128, 128, 128, 0.3); border-radius: 8px;
        padding: 18px 20px !important;
    }
    div[data-testid="stMetric"] label {
        font-size: 11px !important; font-weight: 600 !important;
        text-transform: uppercase; letter-spacing: 0.08em !important; opacity: 0.7;
    }
    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
        font-size: 28px !important; font-weight: 700 !important;
    }
    .stTabs [data-baseweb="tab-list"] { gap: 4px; overflow-x: auto; flex-wrap: nowrap; }
    .stTabs [data-baseweb="tab"] {
        font-weight: 500 !important; padding: 12px 16px !important;
        white-space: nowrap; font-size: 13px !important;
    }
    .stTabs [aria-selected="true"] { font-weight: 700 !important; border-bottom: 2px solid #808080 !important; }
    .streamlit-expanderHeader, details summary {
        border: 1px solid rgba(128, 128, 128, 0.3) !important; border-radius: 6px !important;
        font-weight: 600 !important; padding: 12px 14px !important; min-height: 44px;
    }
    .scan-header {
        border: 1px solid rgba(128, 128, 128, 0.3); padding: 20px 22px;
        border-radius: 10px; margin-bottom: 16px;
    }
    .scan-header .scan-titulo { font-size: 22px; font-weight: 700; }
    .scan-header .scan-sub { font-size: 13px; opacity: 0.75; margin-top: 4px; }
    .scan-ultimos {
        font-size: 11px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 0.12em; margin: 20px 0 12px 0; padding-bottom: 6px;
        border-bottom: 1px solid rgba(128, 128, 128, 0.3);
    }
    .qr-msg {
        padding: 14px 16px; border-radius: 6px; margin: 8px 0;
        border: 1px solid rgba(128, 128, 128, 0.3); border-left: 3px solid #808080;
        font-weight: 500;
    }
    .qr-texto { font-size: 14px; font-weight: 500; line-height: 1.4; }
    .qr-puntual { border-left-color: #66BB6A; }
    .qr-tardanza { border-left-color: #FFB74D; }
    .qr-derivado { border-left-color: #FF9800; }
    .qr-retenido { border-left-color: #FF7043; font-weight: 700; }
    .qr-refuerzo { border-left-color: #42A5F5; }
    .qr-bloqueado { border-left-color: #EF5350; font-weight: 800; border: 2px solid #EF5350; }
    .qr-error { border-left-color: #888888; }
    .perfil-card {
        border: 1px solid rgba(128, 128, 128, 0.3); border-radius: 10px;
        padding: 24px 26px; margin-bottom: 18px; border-top: 4px solid #808080;
    }
    .perfil-nombre { font-size: 22px; font-weight: 700; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
    .perfil-meta { font-size: 13px; margin-top: 8px; opacity: 0.8; line-height: 1.6; }
    .perfil-badge {
        display: inline-block; padding: 4px 12px; border-radius: 10px;
        font-size: 10px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 0.10em; background: #808080; color: #FFFFFF;
    }
    .badge-bloqueado { background: #EF5350; color: #FFFFFF; }
    .badge-observado { background: #FFB74D; color: #0A0A0A; }
    .badge-ok { background: #808080; color: #FFFFFF; }
    .perfil-resumen {
        display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
        gap: 12px; margin-top: 18px;
    }
    .perfil-resumen-item {
        border: 1px solid rgba(128, 128, 128, 0.3); border-radius: 8px;
        padding: 14px 10px; text-align: center;
    }
    .perfil-resumen-item .num { font-size: 22px; font-weight: 700; }
    .perfil-resumen-item .lbl {
        font-size: 10px; text-transform: uppercase; letter-spacing: 0.10em;
        font-weight: 600; margin-top: 4px; opacity: 0.7;
    }
    div[data-testid="stDataFrame"] {
        border-radius: 8px; overflow: hidden;
        border: 1px solid rgba(128, 128, 128, 0.3);
    }
    hr { border: none; height: 1px; background: rgba(128, 128, 128, 0.3); margin: 20px 0; }
    @media (max-width: 768px) {
        h1 { font-size: 1.4rem !important; }
        h2 { font-size: 1.15rem !important; }
        .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
            width: 100% !important; padding: 14px 18px !important;
            font-size: 15px !important; min-height: 48px;
        }
        .perfil-nombre { font-size: 17px; }
        .perfil-resumen { grid-template-columns: repeat(2, 1fr); gap: 10px; }
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

# login
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
                escribir("UPDATE usuarios SET password=?,debe_cambiar_password=0 WHERE id=?", (hashear_password(nueva), usuario["id"]))
                st.session_state["user"]["debe_cambiar_password"] = 0
                auditar(usuario["usuario"], "Cambio pwd obligatorio")
                st.rerun()

# cuenta
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
    df_act = pd.read_sql("SELECT accion,fecha FROM auditoria WHERE usuario=? ORDER BY id DESC LIMIT 10", con, params=[usuario["usuario"]])
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

# puerta
def _puerta_bienvenida(usuario, fecha):
    con = obtener_conexion()
    esp = con.execute("SELECT descripcion, hora_entrada, tipo FROM dias_especiales WHERE fecha=? AND activo=1 LIMIT 1", (fecha,)).fetchone()
    st.markdown("""
    <div style="text-align:center; margin-top:50px;">
        <h2 style="font-size:26px; margin-top:10px;">Bienvenido</h2>
        <p style="font-size:15px; opacity:0.7; margin-top:16px;">
            Cuando llegue un alumno, cambia a <b>Escanear QR</b> para activar la camara.
        </p>
    </div>
    """, unsafe_allow_html=True)
    st.markdown("---")
    if esp and esp["tipo"] == "evento":
        st.success("Evento escolar: " + esp['descripcion'] + " (entrada " + esp['hora_entrada'] + ")")
    elif esp and esp["tipo"] == "feriado":
        st.info("Feriado / sin clases: " + esp['descripcion'] + ".")
    elif es_fin_de_semana():
        st.warning("Hoy no es dia laboral. No se toma asistencia.")
    else:
        ha = hora_corta()
        va = []
        for t in listar_turnos():
            for v in listar_ventanas(t["id"]):
                if v["hora_apertura"] <= ha <= v["hora_cierre"]:
                    va.append(t['nombre'] + ": " + v['nombre'])
        if va:
            st.markdown("<div style='background:#FFF3E0; padding:10px 16px; border-radius:8px; border-left:3px solid #E65100; font-weight:600; color:#E65100;'><b>Ventanas activas:</b> " + " | ".join(va) + "</div>", unsafe_allow_html=True)
        else:
            st.info("No hay ventanas activas en este momento.")

def _puerta_escanear(usuario, fecha):
    con = obtener_conexion()
    esp = con.execute("SELECT descripcion, hora_entrada, tipo FROM dias_especiales WHERE fecha=? AND activo=1 LIMIT 1", (fecha,)).fetchone()
    if esp and esp["tipo"] == "feriado":
        st.info("Feriado / sin clases: " + esp['descripcion'] + ".")
        return
    if es_fin_de_semana():
        st.warning("Hoy no es dia laboral.")
        return
    escaner_qr_continuo(key="puerta_qr")

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
        st.session_state["puerta_modo"] = "Bienvenida"
    modo = st.radio("Modo", ["Bienvenida", "Escanear QR", "Manual"], key="puerta_modo", horizontal=True, label_visibility="collapsed")
    if modo == "Bienvenida":
        _puerta_bienvenida(usuario, fecha)
    elif modo == "Escanear QR":
        _puerta_escanear(usuario, fecha)
    else:
        _puerta_manual(usuario, fecha)

# toece
def _toece_justificar_permiso(usuario):
    st.caption("Justificar: solo Faltas o Tardanzas, hasta 24h despues. Permisos: cualquier dia.")
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
                "AND fecha >= date('now','-1 day') ORDER BY fecha DESC",
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
                        if ok: st.toast(msg); st.rerun()
                        else: st.error(msg)
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
                    if ok: st.toast(msg); st.rerun()
                    else: st.error(msg)
    with sub_tabs[2]:
        df = listar_permisos(solo_activos=True)
        if df.empty: st.info("Sin permisos activos.")
        else:
            st.dataframe(df, width='stretch')
            st.download_button("Excel", df_a_xlsx(df), "permisos.xlsx")

def vista_toece():
    st.title("TOECE")
    usuario = st.session_state["user"]
    if usuario["rol"] == "Auxiliar":
        _toece_justificar_permiso(usuario)
        return
    tabs = st.tabs(["Casos activos", "Bloqueados", "Observados", "Firmar acta", "Justificar / Permiso"])
    with tabs[0]:
        df = casos_toece()
        if df.empty: st.info("Sin casos activos.")
        else:
            st.dataframe(df, width='stretch')
            st.download_button("Excel", df_a_xlsx(df), "casos_toece.xlsx")
    with tabs[1]:
        st.subheader("Alumnos bloqueados")
        df = listar_bloqueados()
        if df.empty:
            st.info("Sin bloqueados.")
        else:
            st.dataframe(df, width='stretch')
            ops = {r['alumno'] + " (" + r['dni'] + ") - " + r['motivo']: r["alumno_id"] for _, r in df.iterrows()}
            sel = st.selectbox("Liberar bloqueo", list(ops.keys()))
            obs = st.text_input("Observacion de liberacion", key="lib_obs")
            if _pedir_password_critica("lib_bloq", "Liberar bloqueo"):
                liberar_bloqueo(ops[sel], usuario, obs)
                st.toast("Bloqueo liberado"); st.rerun()
        st.markdown("---")
        st.subheader("Bloquear manualmente")
        idg, ids, texto = filtros_grado_seccion_nombre("bloq_man")
        if texto or idg:
            df_b = buscar_alumnos(texto, idg, ids, limite=50)
            if not df_b.empty:
                ops_b = {r['nombre_completo'] + " (" + r['dni'] + ")": r["id"] for _, r in df_b.iterrows()}
                sel_b = st.selectbox("Alumno a bloquear", list(ops_b.keys()), key="bloq_sel")
                mot = st.text_input("Motivo del bloqueo", key="bloq_mot")
                if not mot.strip():
                    st.info("Ingresa un motivo.")
                else:
                    if _pedir_password_critica("bloq_man", "Bloquear"):
                        crear_bloqueo(ops_b[sel_b], mot.strip(), usuario, "manual")
                        st.toast("Alumno bloqueado"); st.rerun()
    with tabs[2]:
        st.subheader("Alumnos observados")
        solo = st.checkbox("Solo activos", value=True, key="obs_act")
        df = listar_observados(solo_activos=solo)
        if df.empty: st.info("Sin observados.")
        else:
            st.dataframe(df, width='stretch')
            st.download_button("Excel", df_a_xlsx(df), "observados.xlsx")
        st.markdown("---")
        st.subheader("Crear observado")
        idg, ids, texto = filtros_grado_seccion_nombre("obs_crear")
        if texto or idg:
            df_a = buscar_alumnos(texto, idg, ids, limite=50)
            if not df_a.empty:
                ops_a = {r['nombre_completo'] + " (" + r['dni'] + ")": r["id"] for _, r in df_a.iterrows()}
                with st.form("form_obs"):
                    sel_a = st.selectbox("Alumno", list(ops_a.keys()))
                    mot = st.text_input("Motivo")
                    pwd = st.text_input("Contrasena de Admin o TOECE", type="password")
                    sub = st.form_submit_button("Crear observado", type="primary")
                if sub:
                    if not mot.strip():
                        st.error("Ingresa un motivo.")
                    elif not pwd:
                        st.error("Ingresa la contrasena.")
                    elif not verificar_password_critica(pwd):
                        st.error("Contrasena incorrecta.")
                    else:
                        per = obtener_periodo_activo(); pid = per["id"] if per else None
                        escribir("INSERT INTO observados(alumno_id,fecha_ingreso,motivo,activo,periodo_id) VALUES(?,?,?,1,?)",
                                    (ops_a[sel_a], hoy_str(), mot.strip(), pid))
                        auditar(usuario["usuario"], "Creo observado alumno_id=" + str(ops_a[sel_a]))
                        st.toast("Observado creado"); st.rerun()
        st.markdown("---")
        st.subheader("Cerrar observado")
        df_act = listar_observados(solo_activos=True)
        if not df_act.empty:
            ops_c = {r['apellidos'] + ", " + r['nombres'] + " (" + r['dni'] + ")": r["id"] for _, r in df_act.iterrows()}
            with st.form("cerrar_obs"):
                sel_c = st.selectbox("Observado a cerrar", list(ops_c.keys()))
                obs_c = st.text_input("Observacion de cierre")
                pwd = st.text_input("Contrasena de Admin o TOECE", type="password")
                sub_c = st.form_submit_button("Cerrar observado", type="primary")
            if sub_c:
                if not pwd:
                    st.error("Ingresa la contrasena.")
                elif not verificar_password_critica(pwd):
                    st.error("Contrasena incorrecta.")
                else:
                    escribir("UPDATE observados SET activo=0,fecha_salida=?,observacion_cierre=? WHERE id=?",
                                (timestamp_str(), obs_c, ops_c[sel_c]))
                    auditar(usuario["usuario"], "Cerro observado id=" + str(ops_c[sel_c]))
                    st.toast("Observado cerrado"); st.rerun()
    with tabs[3]:
        st.subheader("Firmar acta de compromiso")
        df = casos_toece()
        if df.empty:
            st.info("Sin casos.")
        else:
            ops = {r['apellidos'] + ", " + r['nombres'] + " (" + r['dni'] + ")": r["id"] for _, r in df.iterrows()}
            with st.form("firmar_acta_form"):
                sel = st.selectbox("Alumno", list(ops.keys()))
                mot = st.text_input("Motivo", value="Reincidencia en tardanzas")
                obs = st.text_area("Observacion")
                pwd = st.text_input("Contrasena de Admin o TOECE", type="password")
                sub = st.form_submit_button("Firmar acta", type="primary")
            if sub:
                if not pwd:
                    st.error("Ingresa la contrasena.")
                elif not verificar_password_critica(pwd):
                    st.error("Contrasena incorrecta.")
                else:
                    escribir("INSERT INTO actas_compromiso(alumno_id,fecha,motivo,observacion,registrado_por,timestamp) VALUES(?,?,?,?,?,?)",
                                (ops[sel], hoy_str(), mot, obs, usuario["usuario"], timestamp_str()))
                    auditar(usuario["usuario"], "Firmo acta alumno_id=" + str(ops[sel]))
                    st.toast("Acta registrada"); st.rerun()
    with tabs[4]:
        _toece_justificar_permiso(usuario)

# panel direccion
def vista_panel_direccion():
    st.title("Panel Direccion")
    fecha = hoy_str()
    if st.button("Actualizar", key="refresh_panel"):
        _control_faltas()
        st.rerun()
    m = metricas_dia(fecha)
    st.subheader("Resumen del dia")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total alumnos", m["total"]); c2.metric("Puntuales", m["puntuales"])
    c3.metric("Tardanzas", m["tardanzas"]); c4.metric("Faltas", m["faltas"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Reforzamiento asistio", m["ref_asistio"]); c2.metric("Bloqueados", m["bloqueados"])
    c3.metric("Justificadas hoy", m["justificadas"]); c4.metric("Permisos hoy", m["permisos"])
    st.markdown("---")
    st.subheader("Ultimos escaneos")
    df = ultimos_registros(fecha, 30)
    if df.empty: st.info("Sin escaneos hoy.")
    else: st.dataframe(df, width='stretch')
    st.markdown("---")
    st.subheader("Justificaciones y permisos de hoy")
    con = obtener_conexion()
    dfj = pd.read_sql(
        "SELECT a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres,"
        "g.nombre AS grado,s.nombre AS seccion,ast.tipo,ast.estado,ast.justificada,ast.hora "
        "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id "
        "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
        "WHERE ast.fecha=? AND ast.justificada=1 ORDER BY a.apellido_paterno",
        con, params=[fecha]
    )
    if dfj.empty: st.info("Sin justificaciones hoy.")
    else: st.dataframe(dfj, width='stretch')
    st.markdown("Permisos vigentes hoy:")
    dfp = pd.read_sql(
        "SELECT a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres,"
        "g.nombre AS grado,s.nombre AS seccion,p.fecha_inicio,p.fecha_fin,COALESCE(p.motivo,'') AS motivo "
        "FROM permisos p JOIN alumnos a ON p.alumno_id=a.id "
        "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
        "WHERE p.activo=1 AND p.fecha_inicio<=? AND p.fecha_fin>=? ORDER BY a.apellido_paterno",
        con, params=[fecha, fecha]
    )
    if dfp.empty: st.info("Sin permisos vigentes hoy.")
    else: st.dataframe(dfp, width='stretch')

# REPORTES CON BOTONES
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
    with c1: desde = st.date_input("Desde", ahora().date() - timedelta(days=30), key="rep_desde")
    with c2: hasta = st.date_input("Hasta", ahora().date(), key="rep_hasta")
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
            st.dataframe(df, width='stretch')
            c1, c2 = st.columns(2)
            with c1:
                st.download_button("Excel", df_a_xlsx(df), "Faltas_" + sec['grado'] + sec['seccion'] + ".xlsx", key="rep_dl_fal_x")
            with c2:
                st.download_button("PDF", generar_pdf_tabla(df, "Conteo de faltas - " + sec['grado'] + " " + sec['seccion']), "Faltas_" + sec['grado'] + sec['seccion'] + ".pdf", "application/pdf", key="rep_dl_fal_p")
    elif tipo == "mensual":
        df = cierre_mensual_calendario(hasta.month, hasta.year, [idsec])
        if df.empty:
            st.info("Sin datos para el mes.")
        else:
            st.dataframe(df, width='stretch')
            c1, c2 = st.columns(2)
            with c1:
                st.download_button("Excel", df_a_xlsx(df), "Mensual_" + sec['grado'] + sec['seccion'] + ".xlsx", key="rep_dl_men_x")
            with c2:
                st.download_button("PDF", generar_pdf_tabla(df, "Cierre mensual - " + sec['grado'] + " " + sec['seccion']), "Mensual_" + sec['grado'] + sec['seccion'] + ".pdf", "application/pdf", key="rep_dl_men_p")

def _mostrar_reporte_agrupado(df, sec, titulo):
    if df.empty:
        st.info("Sin registros en este rango.")
        return
    st.write(str(len(df)) + " registros")
    df["fecha_dt"] = pd.to_datetime(df["fecha"])
    fechas = sorted(df["fecha_dt"].unique(), reverse=True)
    dias_es = ["Lunes","Martes","Miercoles","Jueves","Viernes","Sabado","Domingo"]
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
        st.download_button("Excel", df_a_xlsx(df), titulo + "_" + sec['grado'] + sec['seccion'] + ".xlsx", key="rep_dl_" + titulo)
    with c2:
        st.download_button("PDF", generar_pdf_tabla(df, titulo + " - " + sec['grado'] + " " + sec['seccion']), titulo + "_" + sec['grado'] + sec['seccion'] + ".pdf", "application/pdf", key="rep_dl_pdf_" + titulo)

def _rep_descarga_directa(idsec):
    st.session_state["rep_idsec"] = idsec
    st.rerun()

def _rep_general_por_grado_admin():
    st.subheader("Reporte general por seccion")
    st.caption("Puntuales, faltas y auxiliar asignado por cada seccion.")
    c1, c2 = st.columns(2)
    with c1: desde = st.date_input("Desde", ahora().date() - timedelta(days=30), key="repgen_desde")
    with c2: hasta = st.date_input("Hasta", ahora().date(), key="repgen_hasta")
    if desde > hasta:
        st.error("La fecha Desde no puede ser mayor que Hasta.")
        return
    pid = None
    dfp = listar_periodos()
    if not dfp.empty:
        ops_p = {}
        for _, r in dfp.iterrows():
            et = r['nombre'] + " (" + r['fecha_inicio'] + " - " + r['fecha_fin'] + ")"
            if r["cerrado"]: et += " [CERRADO]"
            elif r["activo"]: et += " [ACTIVO]"
            ops_p[et] = r["id"]
        sel_lbl = st.selectbox("Periodo", list(ops_p.keys()), key="repgen_pid")
        pid = ops_p[sel_lbl]
    df = reporte_general_por_seccion(desde, hasta, pid)
    if df.empty:
        st.info("Sin datos en ese rango.")
        return
    st.markdown("---")
    st.dataframe(df, width='stretch')
    st.markdown("### Descargar")
    c1, c2 = st.columns(2)
    with c1:
        st.download_button("Excel", df_a_xlsx(df, "General por seccion"), "Reporte_general_" + str(desde) + "_" + str(hasta) + ".xlsx", width='stretch')
    with c2:
        st.download_button("PDF", generar_pdf_tabla(df, "Reporte general por seccion", str(desde) + " a " + str(hasta)), "Reporte_general_" + str(desde) + "_" + str(hasta) + ".pdf", "application/pdf", width='stretch')

def vista_reportes():
    st.title("Reportes y Consultas")
    usuario = st.session_state["user"]; rol = usuario["rol"]

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
        modo = st.radio("Modo", ["Por seccion (normal)", "General por seccion (Admin)"], key="rep_modo_admin", horizontal=True, label_visibility="collapsed")
        if modo == "General por seccion (Admin)":
            _rep_general_por_grado_admin()
            return

    permitidas = None
    if rol == "Auxiliar":
        con = obtener_conexion()
        permitidas = [f["seccion_id"] for f in con.execute("SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id=?", (usuario["id"],)).fetchall()]
        if not permitidas:
            st.info("No tienes secciones asignadas. Contacta al Admin.")
            return

    secs = listar_todas_secciones()
    if permitidas is not None:
        secs = [s for s in secs if s["id"] in permitidas]
    if not secs:
        st.warning("No hay secciones disponibles.")
        return

    if rol == "Auxiliar" and len(secs) == 1:
        _rep_descarga_directa(secs[0]["id"])
        return

    st.markdown("### Elige la seccion")
    cols = st.columns(3)
    for i, s in enumerate(secs):
        with cols[i % 3]:
            if st.button(s['grado'] + " " + s['seccion'] + " (" + s['turno'] + ")", width='stretch', key="rep_sec_" + str(s['id'])):
                _rep_descarga_directa(s["id"])

# alumnos UI
def _frag_crear_alumno():
    st.subheader("Crear alumno manualmente")
    grados = listar_grados()
    if not grados: st.warning("No hay grados."); return
    with st.form("crear_al"):
        c1, c2 = st.columns(2)
        with c1:
            dni = st.text_input("DNI * (8 digitos)", max_chars=8)
            nom = st.text_input("Nombres *"); pat = st.text_input("Apellido Paterno *")
        with c2:
            mat = st.text_input("Apellido Materno")
            g = st.selectbox("Grado *", grados, format_func=lambda x: x["nombre"])
            secs = secciones_por_grado(g["id"]) if g else []
            s = st.selectbox("Seccion *", secs, format_func=lambda x: x["nombre"]) if secs else None
        c3, c4 = st.columns(2)
        with c3: apo = st.text_input("Apoderado (opcional)")
        with c4: tel = st.text_input("Telefono (opcional)")
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
                ok, msg = crear_alumno(dni.strip(), nom.strip(), pat.strip(), mat.strip(), s["id"], apo.strip(), tel.strip(), st.session_state["user"])
                if ok: st.toast(msg); st.rerun()
                else: st.error(msg)

def _frag_editar_alumno():
    st.subheader("Editar alumno")
    idg, ids, texto = filtros_grado_seccion_nombre("ed_al")
    if not (texto or idg): return
    df = buscar_alumnos(texto, idg, ids, limite=50)
    if df.empty: st.info("Sin coincidencias."); return
    ops = {r['nombre_completo'] + " - " + r['grado'] + " " + r['seccion']: r["id"] for _, r in df.iterrows()}
    sel = st.selectbox("Alumno", list(ops.keys()), key="ed_sel"); idal = ops[sel]
    con = obtener_conexion()
    datos = con.execute("SELECT a.*,g.nombre AS grado,s.nombre AS seccion FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id WHERE a.id=?", (idal,)).fetchone()
    if not datos: return
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
                if ok: st.toast(msg); st.rerun()
                else: st.error(msg)

def _frag_listar_alumnos():
    idg, ids, texto = filtros_grado_seccion_nombre("list_al")
    df = buscar_alumnos(texto, idg, ids, limite=5000)
    st.write(str(len(df)) + " alumnos")
    if df.empty: st.info("Sin resultados."); return
    mostrar = st.checkbox("Mostrar todos", value=False)
    lim = len(df) if mostrar else 50
    for _, al in df.head(lim).iterrows():
        c1, c2 = st.columns([5, 1])
        c1.markdown("**" + al['nombre_completo'] + "** &nbsp; <span style='color:#E65100; font-weight:700;'>" + al['grado'] + " " + al['seccion'] + "</span> <span style='color:#757575;'>(" + al['turno'] + ")</span>", unsafe_allow_html=True)
        if c2.button("Ver perfil", key="perfil_" + str(al['id'])):
            st.session_state["perfil_alumno_id"] = al["id"]; st.rerun()

def _perfil_alumno(idal):
    d = perfil_alumno_datos(idal)
    if not d:
        st.warning("Alumno no encontrado."); st.session_state.pop("perfil_alumno_id", None); return
    al = d["alumno"]; usuario = st.session_state["user"]
    if st.button("Volver a la lista", key="volver_perfil"):
        st.session_state.pop("perfil_alumno_id", None); st.rerun()
    nombre = (al['apellido_paterno'] + " " + (al['apellido_materno'] or "") + ", " + al['nombres']).strip(", ")
    if d["bloqueado"]: badge = '<span class="perfil-badge badge-bloqueado">BLOQUEADO</span>'
    elif not d["observados"].empty and any(d["observados"]["activo"] == 1): badge = '<span class="perfil-badge badge-observado">OBSERVADO</span>'
    else: badge = '<span class="perfil-badge badge-ok">ACTIVO</span>'
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
        st.markdown("Codigo QR"); st.image(generar_qr(al["dni"]), width=180)
    c1, c2, c3 = st.columns(3)
    with c1:
        pdf = pdf_carnet_alumno(al["dni"])
        if pdf: st.download_button("Descargar carnet QR", pdf, "carnet_" + al['dni'] + ".pdf", "application/pdf", width='stretch')
    with c2:
        st.download_button("Historial (Excel)", df_a_xlsx(d["asistencias"], "Historial"), "historial_" + al['dni'] + ".xlsx", width='stretch')
    with c3:
        pdf_res = pdf_resumen_alumno(al["id"])
        if pdf_res: st.download_button("Resumen (PDF)", pdf_res, "resumen_" + al['dni'] + ".pdf", "application/pdf", width='stretch')
    if usuario["rol"] == "Admin":
        st.markdown("---")
        if al.get("activo", 1) == 1:
            with st.expander("Desactivar alumno"):
                st.warning("Estas seguro?")
                if _pedir_password_critica("desac_al", "Confirmar desactivacion"):
                    ok, msg = retirar_alumno(al["id"], al["dni"], usuario); st.toast(msg); st.rerun()
        else:
            with st.expander("Reactivar alumno"):
                if _pedir_password_critica("reac_al", "Confirmar reactivacion"):
                    ok, msg = reactivar_alumno(al["id"], al["dni"], usuario); st.toast(msg); st.rerun()
    st.markdown("---")
    tabs = st.tabs(["Asistencias", "Tardanzas", "Actas", "Observados", "Bloqueos", "Permisos"])
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
                        if ok: st.toast(msg); st.rerun()
                        else: st.error(msg)
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
                                st.toast(msg); st.rerun()
                            else: st.error(msg)
                with c2:
                    if st.button("Cancelar"):
                        st.session_state.pop("justif_id", None)
                        st.rerun()
    with tabs[1]:
        if d["tardanzas"].empty: st.info("Sin tardanzas registradas.")
        else: st.dataframe(d["tardanzas"], width='stretch')
    with tabs[2]:
        if d["actas"].empty: st.info("Sin actas firmadas.")
        else: st.dataframe(d["actas"], width='stretch')
    with tabs[3]:
        if d["observados"].empty: st.info("Sin registros de observados.")
        else: st.dataframe(d["observados"], width='stretch')
    with tabs[4]:
        if d["bloqueos"].empty: st.info("Sin bloqueos registrados.")
        else: st.dataframe(d["bloqueos"], width='stretch')
    with tabs[5]:
        if d["permisos"].empty: st.info("Sin permisos registrados.")
        else: st.dataframe(d["permisos"], width='stretch')

def vista_alumnos():
    st.title("Alumnos")
    pid = st.session_state.get("perfil_alumno_id")
    if pid: _perfil_alumno(pid); return
    tabs = st.tabs(["Listar", "Crear", "Editar"])
    with tabs[0]: _frag_listar_alumnos()
    with tabs[1]: _frag_crear_alumno()
    with tabs[2]: _frag_editar_alumno()

# grados y secciones
def vista_grados_secciones():
    st.title("Grados y Secciones")
    st.caption("Las secciones se crean automaticamente al importar el Excel de alumnos.")
    con = obtener_conexion()
    grados = listar_grados()
    st.subheader("Grados")
    if grados: st.dataframe(pd.DataFrame(grados), width='stretch')
    else: st.info("Sin grados.")
    st.subheader("Secciones")
    df = pd.read_sql("SELECT s.id,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno,(SELECT COUNT(*) FROM alumnos a WHERE a.seccion_id=s.id AND a.activo=1) AS alumnos_activos FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id ORDER BY t.nombre,g.nombre,s.nombre", con)
    if df.empty: st.info("Sin secciones.")
    else: st.dataframe(df, width='stretch')

# carnets
def vista_carnets():
    st.title("Carnets QR")
    if st.session_state.get("carn_ver_seccion"):
        _carnets_ver_seccion(st.session_state["carn_ver_seccion"])
        return
    grados = listar_grados()
    if not grados:
        st.warning("No hay grados."); return
    st.caption("Aprieta un grado para ver sus secciones.")
    cols = st.columns(3)
    for i, g in enumerate(grados):
        secs = secciones_por_grado(g["id"])
        with cols[i % 3]:
            if st.button(g['nombre'] + "  (" + str(len(secs)) + " secciones)", width='stretch', key="carn_g_" + str(g['id'])):
                st.session_state["carn_grado_sel"] = g["id"]; st.rerun()
    gid = st.session_state.get("carn_grado_sel")
    if not gid: return
    g = next((x for x in grados if x["id"] == gid), None)
    if not g:
        st.session_state.pop("carn_grado_sel", None); return
    st.markdown("---")
    st.subheader("Secciones de " + g['nombre'])
    secs = secciones_por_grado(g["id"])
    if not secs:
        st.info("Este grado no tiene secciones."); return
    cols = st.columns(3)
    for i, s in enumerate(secs):
        n = len(alumnos_de_seccion(s["id"]))
        with cols[i % 3]:
            if st.button(s['nombre'] + "  (" + str(n) + " alumnos)", width='stretch', key="carn_s_" + str(s['id'])):
                st.session_state["carn_ver_seccion"] = s["id"]; st.rerun()

def _carnets_ver_seccion(idsec):
    con = obtener_conexion()
    sec = con.execute("""
        SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
        WHERE s.id=?
    """, (idsec,)).fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        st.session_state.pop("carn_ver_seccion", None); return
    if st.button("Regresar a grados", key="carn_volver"):
        st.session_state.pop("carn_ver_seccion", None)
        st.session_state.pop("carn_sel_alumnos", None)
        st.rerun()
    st.subheader(sec['grado'] + " " + sec['seccion'] + " - Turno " + sec['turno'])
    df = alumnos_de_seccion(idsec)
    if df.empty:
        st.info("Sin alumnos activos."); return
    st.caption(str(len(df)) + " alumnos. Aprieta un QR para seleccionarlo.")
    if "carn_sel_alumnos" not in st.session_state:
        st.session_state["carn_sel_alumnos"] = set()
    sel = st.session_state["carn_sel_alumnos"]
    cols = st.columns(4)
    for i, (_, al) in enumerate(df.iterrows()):
        with cols[i % 4]:
            marcado = "[X] " if al["id"] in sel else ""
            if st.button(marcado + al['nombre_completo'], key="carn_al_" + str(al['id']), width='stretch'):
                if al["id"] in sel: sel.discard(al["id"])
                else: sel.add(al["id"])
                st.rerun()
            st.image(generar_qr(al["dni"]), width=120)
    st.markdown("---")
    st.write("Seleccionados: " + str(len(sel)))
    c1, c2, c3 = st.columns(3)
    with c1:
        if sel:
            if st.button("Descargar seleccionados", type="primary", width='stretch', key="carn_dl_sel"):
                pdf = pdf_carnets_seleccionados(list(sel), titulo="Carnets seleccionados - " + sec['grado'] + " " + sec['seccion'])
                if pdf:
                    st.download_button("Guardar PDF", pdf, "carnets_sel_" + sec['grado'] + sec['seccion'] + ".pdf", "application/pdf", width='stretch')
        else:
            st.info("Marca al menos un alumno.")
    with c2:
        if st.button("Descargar todo el salon", width='stretch', key="carn_dl_todo"):
            pdf = pdf_carnets_por_seccion(idsec)
            if pdf:
                st.download_button("Guardar PDF", pdf, "carnets_" + sec['grado'] + sec['seccion'] + ".pdf", "application/pdf", width='stretch')
    with c3:
        if st.button("Limpiar seleccion", width='stretch', key="carn_limpiar"):
            st.session_state["carn_sel_alumnos"] = set(); st.rerun()

# ventanas
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
                        ap_t = st.time_input("Apertura", value=datetime.strptime(v["hora_apertura"], "%H:%M").time(), key="ap_" + str(v['id']))
                    with c2:
                        lim_val = v["hora_limite_puntual"] or v["hora_apertura"]
                        lim_t = st.time_input("Limite puntual", value=datetime.strptime(lim_val, "%H:%M").time(), key="lim_" + str(v['id']))
                    with c3:
                        ci_t = st.time_input("Cierre", value=datetime.strptime(v["hora_cierre"], "%H:%M").time(), key="ci_" + str(v['id']))
                    pwd = st.text_input("Contrasena de Admin o TOECE", type="password", key="pwd_vent_" + str(v['id']))
                    if st.form_submit_button("Guardar", type="primary"):
                        if not pwd:
                            st.error("Ingresa la contrasena.")
                        elif not verificar_password_critica(pwd):
                            st.error("Contrasena incorrecta.")
                        else:
                            ap = ap_t.strftime("%H:%M"); lim = lim_t.strftime("%H:%M"); ci = ci_t.strftime("%H:%M")
                            escribir("UPDATE ventanas SET hora_apertura=?,hora_limite_puntual=?,hora_cierre=? WHERE id=?", (ap, lim, ci, v["id"]))
                            auditar(st.session_state["user"]["usuario"], "Edito ventana id=" + str(v['id']))
                            listar_ventanas.clear()
                            st.toast("Ventana actualizada"); st.rerun()

# usuarios
def puede_gestionar_usuario(usuario_actual, id_objetivo):
    con = obtener_conexion()
    obj = con.execute("SELECT id,rol,es_principal,usuario FROM usuarios WHERE id=?", (id_objetivo,)).fetchone()
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
            df = pd.read_sql("SELECT id,usuario,rol,nombres,activo,ultimo_login,es_principal FROM usuarios ORDER BY es_principal DESC, usuario", con)
        else:
            df = pd.read_sql(
                "SELECT id,usuario,rol,nombres,activo,ultimo_login,es_principal FROM usuarios "
                "WHERE id=? OR rol!='Admin' ORDER BY usuario",
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
                    if st.button(inicial + "  |  " + u['nombres'] + "\n" + etiqueta_rol + "  (" + estado + ")", key="usr_btn_" + str(u['id']), width='stretch'):
                        st.session_state["usr_ver_perfil"] = u["id"]; st.rerun()

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
                        escribir("INSERT INTO usuarios(usuario,password,rol,nombres,turno_asignado,es_principal) VALUES(?,?,?,?,?,0)",
                                    (u_norm, hashear_password(p), r, n, idt))
                        auditar(usuario["usuario"], "Creo usuario " + u_norm + " con rol " + r)
                        st.toast("Usuario " + u_norm + " creado")
                        for k in ["crear_u_usuario", "crear_u_pass", "crear_u_nombres", "crear_u_rol", "crear_u_turno", "crear_u_pwd_crit"]:
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
                    roles_edit = ["Admin","TOECE","Auxiliar","Direccion"] if soy_principal else ["TOECE","Auxiliar","Direccion"]
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
                            dup = con.execute("SELECT id FROM usuarios WHERE LOWER(usuario)=? AND id!=?", (u_norm, idu)).fetchone()
                            if dup:
                                st.error("Ya existe otro usuario con ese nombre (sin importar mayusculas).")
                            else:
                                if p:
                                    escribir("UPDATE usuarios SET usuario=?,nombres=?,rol=?,password=?,activo=? WHERE id=?",
                                                (u_norm, n, r, hashear_password(p), 1 if act else 0, idu))
                                else:
                                    escribir("UPDATE usuarios SET usuario=?,nombres=?,rol=?,activo=? WHERE id=?",
                                                (u_norm, n, r, 1 if act else 0, idu))
                                auditar(usuario["usuario"], "Edito usuario " + u_norm)
                                st.toast("Usuario editado"); st.rerun()

    with tabs[3]:
        st.subheader("Asignar secciones a Auxiliares")
        st.caption("Cada seccion solo puede estar asignada a un auxiliar.")
        dfa = pd.read_sql("SELECT id,usuario,nombres,turno_asignado FROM usuarios WHERE rol='Auxiliar' AND activo=1", con)
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
                asig = {r["seccion_id"] for r in con.execute("SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id=?", (ida,)).fetchall()}
                st.write("Secciones disponibles:")
                st.caption("Las secciones ya asignadas a otro auxiliar no aparecen.")
                sel_s = []
                for s in secs:
                    if s["id"] in asignadas_a_otros:
                        continue
                    if st.checkbox(s['grado'] + " " + s['nombre'], value=s["id"] in asig, key="asig_" + str(ida) + "_" + str(s['id'])):
                        sel_s.append(s["id"])
                if _pedir_password_critica("asig_" + str(ida), "Guardar asignaciones"):
                    with _lock_escritura:
                        con.execute("DELETE FROM auxiliar_secciones WHERE usuario_id=?", (ida,))
                        for sid in sel_s:
                            try:
                                con.execute("INSERT INTO auxiliar_secciones(usuario_id,seccion_id) VALUES(?,?)", (ida, sid))
                            except sqlite3.IntegrityError:
                                st.warning("La seccion ya estaba asignada a otro auxiliar.")
                        con.commit()
                    auditar(usuario["usuario"], "Asigno " + str(len(sel_s)) + " secciones a usuario_id=" + str(ida))
                    st.toast("Asignaciones guardadas"); st.rerun()
            else:
                st.warning("Este auxiliar no tiene turno asignado.")

    with tabs[4]:
        st.subheader("Modo mantenimiento")
        if modo_mantenimiento():
            st.error("El sistema esta en MANTENIMIENTO.")
            if _pedir_password_critica("mant_off", "Desactivar mantenimiento"):
                desactivar_mantenimiento(usuario)
                st.toast("Mantenimiento desactivado"); st.rerun()
        else:
            st.success("El sistema esta operativo.")
            msg = st.text_input("Mensaje para mostrar (opcional)", key="mant_msg")
            if _pedir_password_critica("mant_on", "Activar mantenimiento"):
                activar_mantenimiento(usuario, msg)
                st.toast("Mantenimiento activado"); st.rerun()

def _usuario_ver_perfil(idu):
    usuario_actual = st.session_state["user"]
    soy_principal = (usuario_actual.get("es_principal") or 0) == 1
    con = obtener_conexion()
    u = con.execute("SELECT * FROM usuarios WHERE id=?", (idu,)).fetchone()
    if not u:
        st.warning("Usuario no encontrado.")
        st.session_state.pop("usr_ver_perfil", None); return
    if not soy_principal and u["rol"] == "Admin" and u["id"] != usuario_actual["id"]:
        st.error("No tienes permisos para ver este usuario.")
        st.session_state.pop("usr_ver_perfil", None); return
    if st.button("Regresar", key="usr_volver"):
        st.session_state.pop("usr_ver_perfil", None); st.rerun()
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

# auditoria
def _frag_importar_excel():
    st.subheader("Cargar alumnos al periodo")
    st.info("Columnas: DNI, Nombres, Apellido Paterno, Apellido Materno, Grado, Seccion, Turno, Apoderado, Telefono.")
    st.caption("Si un DNI ya existe, se reactiva y actualiza.")
    arch = st.file_uploader("Sube el Excel", type=["xlsx", "xls"], key="import_excel_periodo")
    if not arch: return
    df = pd.read_excel(arch)
    st.write(str(len(df)) + " filas detectadas.")
    cols = list(df.columns)
    with st.form("mapeo_periodo"):
        c1, c2 = st.columns(2)
        with c1:
            m_dni = st.selectbox("DNI *", cols); m_nom = st.selectbox("Nombres *", cols)
            m_pat = st.selectbox("Apellido Paterno *", cols); m_mat = st.selectbox("Apellido Materno", [""] + cols)
        with c2:
            m_gra = st.selectbox("Grado *", cols); m_sec = st.selectbox("Seccion *", cols)
            m_tur = st.selectbox("Turno *", cols); m_apo_n = st.selectbox("Nombre Apoderado", [""] + cols)
            m_apo_t = st.selectbox("Telefono Apoderado", [""] + cols)
        validar = st.form_submit_button("Validar", type="primary")
    if validar:
        mapeo = {"dni": m_dni, "nombres": m_nom, "apellido_paterno": m_pat, "apellido_materno": m_mat, "grado": m_gra, "seccion": m_sec, "turno": m_tur, "apoderado_nombre": m_apo_n, "apoderado_telefono": m_apo_t}
        val, errs, res = validar_importacion(df, mapeo)
        st.session_state["_iv"] = val; st.session_state["_ie"] = errs; st.session_state["_ir"] = res
    if "_ir" in st.session_state:
        r = st.session_state["_ir"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Total", r["total"]); c2.metric("Validas", r["validas"]); c3.metric("Errores", r["errores"])
        if st.session_state["_ie"]:
            with st.expander("Errores"): st.dataframe(pd.DataFrame(st.session_state["_ie"]), width='stretch')
        if st.session_state["_iv"]:
            if _pedir_password_critica("importar_excel", "Importar validas"):
                ins, reac, errs = insertar_alumnos_validos(st.session_state["_iv"])
                st.toast(str(ins) + " alumnos importados, " + str(reac) + " reactivados.")
                if errs: st.warning(str(len(errs)) + " errores al insertar")
                for k in ["_iv", "_ie", "_ir"]: st.session_state.pop(k, None)
                st.rerun()

def vista_auditoria():
    st.title("Auditoria y Periodos")
    usuario = st.session_state["user"]
    tabs = st.tabs(["Registros", "Periodos", "Cierre de anio"])
    with tabs[0]:
        df = obtener_auditoria(500)
        st.write(str(len(df)) + " registros")
        if not df.empty: st.dataframe(df, width='stretch')
    with tabs[1]:
        st.subheader("Periodos")
        st.dataframe(listar_periodos(), width='stretch')
        st.markdown("### Crear nuevo periodo")
        with st.form("nuevo_periodo"):
            c1, c2, c3 = st.columns(3)
            with c1: nom = st.text_input("Nombre (ej: 2026)")
            with c2: fi = st.date_input("Inicio", ahora().date())
            with c3: ff = st.date_input("Fin", ahora().date() + timedelta(days=270))
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
                    ok, msg = crear_periodo(nom.strip(), fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"), usuario)
                    if ok: st.toast(msg); st.rerun()
                    else: st.error(msg)
        st.markdown("---")
        st.markdown("### Cargar alumnos al periodo activo")
        p = obtener_periodo_activo()
        if p:
            if periodo_tiene_alumnos(p["id"]): st.success("El periodo ya tiene alumnos cargados.")
            else: st.warning("El periodo NO tiene alumnos.")
            _frag_importar_excel()
        else:
            st.info("No hay periodo activo.")
        st.markdown("---")
        st.markdown("### Activar periodo (solo no cerrados)")
        df2 = listar_periodos(); df2 = df2[df2["cerrado"] == 0]
        if not df2.empty:
            ops = {}
            for _, r in df2.iterrows():
                et = r['nombre'] + " (" + r['fecha_inicio'] + " - " + r['fecha_fin'] + ")"
                if r["activo"]: et += " ACTIVO"
                ops[et] = r["id"]
            sel = st.selectbox("Periodo a activar", list(ops.keys()))
            if _pedir_password_critica("activar_periodo", "Activar"):
                ok, msg = activar_periodo(ops[sel], usuario)
                if ok: st.toast(msg); st.rerun()
                else: st.error(msg)
    with tabs[2]:
        st.subheader("Cierre de anio escolar")
        st.warning("Al cerrar el periodo se desactivan TODOS los alumnos.")
        p = obtener_periodo_activo()
        if not p:
            st.info("No hay periodo activo.")
            return
        st.info("Periodo activo: " + p['nombre'] + " (" + p['fecha_inicio'] + " - " + p['fecha_fin'] + ")")
        st.markdown("Reporte resumen del periodo:")
        df_rep = reporte_cierre_anual(p["id"])
        if not df_rep.empty: st.dataframe(df_rep, width='stretch')
        st.markdown("---")
        st.markdown("Paso 1: Descargar reporte anual detallado (OBLIGATORIO)")
        if st.button("Generar y descargar reporte anual", key="btn_desc_anual"):
            hojas = reporte_detallado_por_mes(p["id"])
            if not hojas:
                st.warning("Sin datos.")
            else:
                st.download_button("Descargar Excel anual", df_a_xlsx_multilhoja(hojas), "reporte_anual_" + p['nombre'] + ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                st.session_state["_reporte_descargado"] = True
                st.toast("Reporte generado.")
        st.markdown("---")
        st.markdown("Paso 2: Cerrar periodo (requiere contrasena)")
        desc = st.session_state.get("_reporte_descargado", False)
        if not desc: st.info("Debes descargar el reporte anual antes.")
        with st.form("cerrar_anio"):
            c1, c2, c3 = st.columns(3)
            with c1: nn = st.text_input("Nombre nuevo periodo", value=str(ahora().year + 1))
            with c2: fi = st.date_input("Inicio nuevo", date(ahora().year + 1, 3, 1))
            with c3: ff = st.date_input("Fin nuevo", date(ahora().year + 1, 12, 31))
            pwd = st.text_input("Contrasena de Admin o TOECE", type="password")
            conf = st.text_input("Escribe CERRAR para confirmar")
            sub = st.form_submit_button("Cerrar anio escolar", type="primary")
        if sub:
            if not desc: st.error("Primero debes descargar el reporte anual.")
            elif conf.strip() != "CERRAR": st.error("Debes escribir exactamente CERRAR.")
            elif not pwd: st.error("Ingresa la contrasena.")
            elif not verificar_password_critica(pwd): st.error("Contrasena incorrecta.")
            else:
                ok, msg = cerrar_anio_escolar(usuario, p["id"], nn, fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"))
                if ok:
                    st.session_state.pop("_reporte_descargado", None); st.success(msg); st.rerun()
                else: st.error(msg)
        st.markdown("---")
        st.markdown("Cierres anteriores:")
        dfc = listar_cierres_anuales()
        if not dfc.empty: st.dataframe(dfc, width='stretch')
        st.markdown("Periodos cerrados (solo consulta):")
        dfp = listar_periodos_cerrados()
        if not dfp.empty: st.dataframe(dfp, width='stretch')

# dias especiales
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
                    hora_t = st.time_input("Hora entrada", value=datetime.strptime("08:00", "%H:%M").time())
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
                                if st.checkbox(g['nombre'] + " " + s['nombre'], key="dia_sec_" + str(g['id']) + "_" + str(s['id'])):
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
                per = obtener_periodo_activo(); pid = per["id"] if per else None
                with _lock_escritura:
                    cur = con.execute("INSERT INTO dias_especiales(fecha,descripcion,turno_id,hora_entrada,tipo,periodo_id) VALUES(?,?,?,?,?,?)",
                                      (fecha.strftime("%Y-%m-%d"), desc.strip(), idt, hora if tipo == "Evento" else "00:00",
                                       "evento" if tipo == "Evento" else "feriado", pid))
                    idd = cur.lastrowid
                    for sid in selecciones_secciones:
                        con.execute("INSERT INTO dias_especiales_secciones(dia_especial_id,seccion_id) VALUES(?,?)", (idd, sid))
                    con.commit()
                auditar(usuario["usuario"], "Creo dia especial " + desc)
                st.toast("Dia especial creado"); st.rerun()
    with tabs[1]:
        fh = hoy_str()
        df = pd.read_sql("SELECT d.id,d.fecha,d.descripcion,COALESCE(t.nombre,'Ambos') AS turno,d.hora_entrada,d.tipo,(SELECT COUNT(*) FROM dias_especiales_secciones WHERE dia_especial_id=d.id) AS num_secciones FROM dias_especiales d LEFT JOIN turnos t ON d.turno_id=t.id WHERE d.fecha>=? AND d.activo=1 ORDER BY d.fecha",
                          con, params=[fh])
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
                st.toast("Dia especial eliminado"); st.rerun()

# menu
def _opciones_auxiliar(usuario):
    con = obtener_conexion()
    tiene = con.execute(
        "SELECT COUNT(*) FROM auxiliar_secciones WHERE usuario_id=?",
        (usuario["id"],)
    ).fetchone()[0] > 0
    if not tiene:
        return ["Puerta"]
    return ["Puerta", "TOECE", "Reportes", "Mi cuenta"]

def obtener_opciones_por_rol(usuario):
    rol = usuario["rol"]
    if rol == "Admin":
        return ["Puerta","TOECE","Panel Direccion","Reportes","Alumnos","Grados y Secciones",
                "Carnets","Dias especiales","Ventanas","Usuarios","Auditoria","Mi cuenta"]
    if rol == "TOECE":
        return ["Puerta","TOECE","Reportes","Mi cuenta"]
    if rol == "Direccion":
        return ["Panel Direccion","TOECE","Alumnos","Carnets","Dias especiales","Mi cuenta"]
    if rol == "Auxiliar":
        return _opciones_auxiliar(usuario)
    return []

RUTAS = {
    "Puerta": vista_puerta, "TOECE": vista_toece, "Panel Direccion": vista_panel_direccion,
    "Reportes": vista_reportes, "Alumnos": vista_alumnos, "Grados y Secciones": vista_grados_secciones,
    "Carnets": vista_carnets, "Dias especiales": vista_dias_especiales, "Ventanas": vista_ventanas,
    "Usuarios": vista_usuarios, "Auditoria": vista_auditoria, "Mi cuenta": vista_mi_cuenta,
}

def menu_lateral():
    usuario = st.session_state["user"]; rol = usuario["rol"]
    opciones = obtener_opciones_por_rol(usuario)
    with st.sidebar:
        inicial = (usuario["nombres"] or "?")[0].upper()
        st.markdown('<div class="encabezado-sidebar"><div class="avatar">' + inicial + '</div><div class="nombre">' + usuario["nombres"] + '</div><div class="rol">' + rol + '</div></div>', unsafe_allow_html=True)
        with st.expander("Buscar alumno", expanded=False):
            q = st.text_input("Nombre o DNI", key="global_search")
            if q and len(q) >= 3:
                df_gs = buscar_alumnos_con_estado(q, limite=15)
                if df_gs.empty:
                    st.caption("Sin resultados")
                else:
                    for _, al in df_gs.iterrows():
                        estado = al["estado_hoy"]
                        hora = al["hora_hoy"]
                        if estado == "Puntual":
                            etiqueta = "Puntual"
                        elif estado == "Tardanza":
                            etiqueta = "Tardanza"
                        elif estado == "Falta":
                            etiqueta = "Falta"
                        elif estado == "Permiso":
                            etiqueta = "Permiso"
                        else:
                            etiqueta = "Sin registro hoy"
                        if hora:
                            etiqueta += " - " + hora
                        st.markdown(
                            "**" + al['nombre_completo'] + "** — " + al['grado'] + " " + al['seccion'] + "  \n"
                            + etiqueta
                        )
                        st.markdown("---")
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
    if not v: st.warning("Vista no disponible."); return
    if op not in obtener_opciones_por_rol(usuario):
        st.error("Sin permisos.")
        auditar(usuario["usuario"], "Intento acceso no autorizado a " + op)
        return
    v()

def _control_faltas():
    ult = st.session_state.get("_ultimo_control_faltas"); t = time.time()
    if ult and (t - ult) < 300: return
    st.session_state["_ultimo_control_faltas"] = t
    marcar_faltas_al_cierre()

def main():
    st.set_page_config(page_title="Asistencia I.E. Yarinacocha", page_icon="escudo.png", layout="wide", initial_sidebar_state="expanded")
    try:
        inicializar_bd()
        aplicar_estilos()
        inicializar_sesion()
        if not st.session_state.get("user"):
            vista_login(); return
        refrescar_sesion_si_necesario()
        if not _verificar_admin_activo():
            st.error("No hay Admin principal activo en el sistema.")
            st.info("Contacta al desarrollador para restaurar el acceso.")
            st.stop()
        if st.session_state["user"].get("debe_cambiar_password"):
            vista_cambio_password_obligatorio(); return
        if modo_mantenimiento() and st.session_state["user"]["rol"] != "Admin":
            vista_mantenimiento(); return
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
        if op: _enrutar(op, st.session_state["user"])
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
