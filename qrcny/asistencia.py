import base64, hashlib, logging, re, secrets, sqlite3, threading, time
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
from reportlab.platypus import (Image as RLImage, PageBreak, Paragraph,
                                 SimpleDocTemplate, Spacer, Table, TableStyle)

from qr_scanner_component import qr_scanner

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    def st_autorefresh(**kwargs): pass

LOG_DIR = Path("logs"); LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("asistencia")

DB_PATH = "asistencia.db"
MESES_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
C_NARANJA = "#E65100"; C_NARANJA_H = "#BF360C"
PBK_ITER = 260_000; PBK_ALG = "sha256"
MAX_INTENTOS = 3; MIN_BLOQUEO = 10
PUNTUAL = "Puntual"; TARDANZA = "Tardanza"; FALTA = "Falta"; PERMISO = "Permiso"
REF_ASISTIO = "Asistio"; REF_NO_ASISTIO = "No asistio"
ACC_PERDONADO = "PERDONADO"; ACC_DERIVADO = "DERIVADO_DIRECCION"; ACC_RETENIDO = "RETENIDO_APODERADO"
VENT_CLASES = "clases"; VENT_REF = "reforzamiento"; TIPO_ASIST_EVENTO = "evento"
MAX_DIAS_PERMISO = 7


#HELPERS TIEMPO 
def ahora(): return datetime.now(timezone.utc) - timedelta(hours=5)
def hoy_str(): return ahora().strftime("%Y-%m-%d")
def hora_str(): return ahora().strftime("%H:%M:%S")
def hora_corta(): return ahora().strftime("%H:%M")
def timestamp_str(): return ahora().strftime("%Y-%m-%d %H:%M:%S")
def es_fin_de_semana(fecha=None): return (fecha or ahora().date()).weekday() >= 5


# SEGURIDAD 
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
        "SELECT password FROM usuarios WHERE rol IN ('Admin','Direccion') AND activo=1"
    ).fetchall():
        if verificar_password(password, fila["password"]):
            return True
    return False

# CONEXION SQLITE 
_hilos = threading.local()
_lock_escritura = threading.Lock()

def obtener_conexion():
    if not hasattr(_hilos, "con") or _hilos.con is None:
        con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        con.row_factory = sqlite3.Row
        for p in ("journal_mode=WAL", "synchronous=NORMAL",
                  "foreign_keys=ON", "busy_timeout=30000"):
            try: con.execute("PRAGMA " + p)
            except sqlite3.Error as e: log.warning("pragma %s: %s", p, e)
        _hilos.con = con
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
        (tabla,)).fetchone() is not None


# ─── INICIALIZAR BD 
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
    CREATE TABLE IF NOT EXISTS asistencias(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha TEXT NOT NULL,ventana_id INTEGER,tipo TEXT NOT NULL DEFAULT 'clases',hora TEXT,estado TEXT NOT NULL,justificada INTEGER DEFAULT 0,observacion TEXT,origen TEXT DEFAULT 'qr',justificado_por TEXT,justificado_en TEXT,periodo_id INTEGER,dia_especial_id INTEGER,UNIQUE(alumno_id,fecha,tipo));
    CREATE TABLE IF NOT EXISTS tardanzas(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha TEXT NOT NULL,hora TEXT NOT NULL,numero INTEGER NOT NULL,accion TEXT NOT NULL,observacion TEXT,justificada INTEGER DEFAULT 0,origen TEXT DEFAULT 'qr',registrado_por TEXT,timestamp TEXT NOT NULL,periodo_id INTEGER,UNIQUE(alumno_id,fecha));
    CREATE TABLE IF NOT EXISTS bloqueos(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,motivo TEXT,activo INTEGER DEFAULT 1,fecha_inicio TEXT NOT NULL,fecha_fin TEXT,liberado_por TEXT,creado_por TEXT,origen TEXT DEFAULT 'automatico');
    CREATE TABLE IF NOT EXISTS justificaciones_previas(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha_objetivo TEXT NOT NULL,tipo TEXT NOT NULL CHECK(tipo IN ('Falta')),motivo TEXT,creado_por TEXT,timestamp TEXT NOT NULL,aplicada INTEGER DEFAULT 0,UNIQUE(alumno_id,fecha_objetivo));
    CREATE TABLE IF NOT EXISTS permisos(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha_inicio TEXT NOT NULL,fecha_fin TEXT NOT NULL,motivo TEXT,tipo TEXT NOT NULL DEFAULT 'permiso',creado_por TEXT,timestamp TEXT NOT NULL,activo INTEGER DEFAULT 1,periodo_id INTEGER);
    CREATE TABLE IF NOT EXISTS dias_especiales(id INTEGER PRIMARY KEY,fecha TEXT NOT NULL,descripcion TEXT,turno_id INTEGER,hora_entrada TEXT,activo INTEGER DEFAULT 1,tipo TEXT DEFAULT 'evento' CHECK(tipo IN ('evento','feriado')),periodo_id INTEGER,contar_como_clases INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS dias_especiales_secciones(id INTEGER PRIMARY KEY,dia_especial_id INTEGER NOT NULL,seccion_id INTEGER NOT NULL,UNIQUE(dia_especial_id,seccion_id));
    CREATE TABLE IF NOT EXISTS usuarios(id INTEGER PRIMARY KEY,usuario TEXT UNIQUE NOT NULL,password TEXT NOT NULL,rol TEXT NOT NULL CHECK(rol IN ('Admin','Direccion','Auxiliar')),nombres TEXT NOT NULL,turno_asignado INTEGER,activo INTEGER DEFAULT 1,intentos_fallidos INTEGER DEFAULT 0,bloqueado_hasta TEXT,debe_cambiar_password INTEGER DEFAULT 0,ultimo_login TEXT,ultimo_ip TEXT,es_principal INTEGER DEFAULT 0);
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
    CREATE INDEX IF NOT EXISTS idx_aud_f ON auditoria(fecha);
    CREATE INDEX IF NOT EXISTS idx_aux_sec_unica ON auxiliar_secciones(seccion_id);
    """)
    _migrar(cur); _seed(cur); con.commit()
    log.info("bd lista")


def _migrar(cur):
    migs = [
        ("alumnos","activo","ALTER TABLE alumnos ADD COLUMN activo INTEGER DEFAULT 1"),
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
        ("asistencias","dia_especial_id","ALTER TABLE asistencias ADD COLUMN dia_especial_id INTEGER"),
        ("tardanzas","origen","ALTER TABLE tardanzas ADD COLUMN origen TEXT DEFAULT 'qr'"),
        ("periodos","cerrado","ALTER TABLE periodos ADD COLUMN cerrado INTEGER DEFAULT 0"),
        ("bloqueos","origen","ALTER TABLE bloqueos ADD COLUMN origen TEXT DEFAULT 'automatico'"),
        ("dias_especiales","contar_como_clases","ALTER TABLE dias_especiales ADD COLUMN contar_como_clases INTEGER DEFAULT 0"),
    ]
    for t, c, sql in migs:
        if _tabla_existe(cur, t) and not existe_columna(cur, t, c):
            try: cur.execute(sql)
            except sqlite3.Error as e: log.warning("mig %s.%s: %s", t, c, e)
    try:
        with _lock_escritura:
            cur.execute("DELETE FROM auxiliar_secciones WHERE id NOT IN (SELECT MIN(id) FROM auxiliar_secciones GROUP BY seccion_id)")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_aux_sec_unica ON auxiliar_secciones(seccion_id)")
    except sqlite3.Error as e: log.warning("mig aux_sec: %s", e)
    try:
        with _lock_escritura:
            cur.execute("UPDATE usuarios SET usuario=LOWER(TRIM(usuario)) WHERE usuario!=LOWER(TRIM(usuario))")
    except sqlite3.Error as e: log.warning("mig usuarios: %s", e)


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


# ─── MANTENIMIENTO
def modo_mantenimiento():
    con = obtener_conexion()
    f = con.execute("SELECT valor FROM config WHERE clave='mantenimiento'").fetchone()
    return bool(f and f["valor"] == "1")

def activar_mantenimiento(usuario, mensaje=""):
    escribir("INSERT OR REPLACE INTO config(clave,valor) VALUES('mantenimiento','1')")
    escribir("INSERT OR REPLACE INTO config(clave,valor) VALUES('mantenimiento_msg',?)", (mensaje or "",))
    auditar(usuario["usuario"], "Activo modo mantenimiento")

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
    if msg: st.info("Mensaje del Administrador: " + msg)
    if st.button("Cerrar sesion", width='stretch'):
        cerrar_sesion(); st.rerun()


# ─── SESION 
def cerrar_sesion():
    usuario = st.session_state.get("user")
    if usuario: auditar(usuario["usuario"], "Logout")
    for k in list(st.session_state.keys()): del st.session_state[k]


# ─── AUTH 
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
            auditar(nombre_usuario, "Intento fallido de login (Admin)")
            return None, "Credenciales incorrectas."
        it = (u.get("intentos_fallidos") or 0) + 1
        if it >= MAX_INTENTOS:
            bh = (ahora()+timedelta(minutes=MIN_BLOQUEO)).strftime("%Y-%m-%d %H:%M:%S")
            escribir("UPDATE usuarios SET intentos_fallidos=0,bloqueado_hasta=? WHERE id=?", (bh, u["id"]))
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

def _pedir_password_critica(clave, texto_boton="Confirmar", texto_input="Contrasena de Admin o Direccion"):
    pwd = st.text_input(texto_input, type="password", key="pwd_crit_" + clave)
    conf = st.checkbox("Confirmo esta accion", key="conf_crit_" + clave)
    if st.button(texto_boton, type="primary", key="btn_crit_" + clave, disabled=not conf):
        if not pwd: st.error("Ingresa la contrasena."); return False
        if not verificar_password_critica(pwd): st.error("Contrasena incorrecta."); return False
        return True
    return False

def _verificar_admin_activo():
    con = obtener_conexion()
    a = con.execute("SELECT id FROM usuarios WHERE rol='Admin' AND es_principal=1 AND activo=1").fetchone()
    return a is not None


# ─── PERIODOS ─────────────────────────────────────────────────────────────
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


# ─── VENTANAS ─────────────────────────────────────────────────────────────
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
    feriado = con.execute("SELECT * FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='feriado' LIMIT 1", (fecha,)).fetchone()
    if feriado: return dict(feriado)
    if id_seccion:
        evento_sec = con.execute("""
            SELECT d.* FROM dias_especiales d
            JOIN dias_especiales_secciones ds ON ds.dia_especial_id = d.id
            WHERE d.fecha=? AND d.activo=1 AND d.tipo='evento'
              AND ds.seccion_id=? AND (d.turno_id=? OR d.turno_id IS NULL)
            ORDER BY d.turno_id DESC, d.id DESC LIMIT 1
        """, (fecha, id_seccion, id_turno)).fetchone()
        if evento_sec: return dict(evento_sec)
    evento_turno = con.execute("""
        SELECT * FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento' AND turno_id=?
          AND id NOT IN (SELECT dia_especial_id FROM dias_especiales_secciones)
        ORDER BY id DESC LIMIT 1
    """, (fecha, id_turno)).fetchone()
    if evento_turno: return dict(evento_turno)
    evento_global = con.execute("""
        SELECT * FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento' AND turno_id IS NULL
          AND id NOT IN (SELECT dia_especial_id FROM dias_especiales_secciones)
        ORDER BY id DESC LIMIT 1
    """, (fecha,)).fetchone()
    if evento_global: return dict(evento_global)
    return None

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
            es_ev = bool(dia and dia["tipo"] == "evento" and v["tipo"] == VENT_CLASES)
            return {
                **v,
                "hora_apertura_efectiva": ap,
                "hora_limite_efectiva": lim,
                "es_evento": es_ev,
                "evento_nombre": (dia.get("descripcion") if es_ev else None),
                "dia_especial_id": (dia.get("id") if es_ev else None),
                "contar_como_clases": (bool(dia.get("contar_como_clases")) if es_ev else False),
            }
    return None


# ─── ALUMNOS ──────────────────────────────────────────────────────────────
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
        return True, "Alumno " + nombres + " creado."
    except sqlite3.IntegrityError: return False, "Ya existe un alumno con DNI " + dni
    except sqlite3.Error as e: log.error("crear alumno: %s", e); return False, "Error al crear el alumno."

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


# ─── IMPORT EXCEL ─────────────────────────────────────────────────────────
def _normalizar_grado(n):
    n = (n or "").strip().title()
    r = {"1°":"1ro","2°":"2do","3°":"3ro","4°":"4to","5°":"5to",
         "1o":"1ro","2o":"2do","3o":"3ro","4o":"4to","5o":"5to","1ero":"1ro","3ero":"3ro"}
    return r.get(n, n)

def validar_importacion(df, mapeo):
    errs = []; val = []; con = obtener_conexion(); vistos = {}
    def _limpiar(v):
        if v is None: return ""
        if isinstance(v, float):
            if pd.isna(v): return ""
            if v == int(v): return str(int(v))
            return str(v)
        if isinstance(v, int): return str(v)
        s = str(v).strip()
        if s.endswith(".0") and s[:-2].isdigit(): s = s[:-2]
        if s.lower() == "nan": return ""
        return s
    for idx, fila in df.iterrows():
        nf = idx + 2
        try:
            dni = _limpiar(fila[mapeo["dni"]]); nom = _limpiar(fila[mapeo["nombres"]])
            ap = _limpiar(fila[mapeo["apellido_paterno"]])
            am = _limpiar(fila[mapeo["apellido_materno"]]) if mapeo.get("apellido_materno") else ""
            gr = _normalizar_grado(_limpiar(fila[mapeo["grado"]]))
            sec = _limpiar(fila[mapeo["seccion"]]).upper()
            tur = _limpiar(fila[mapeo["turno"]]).lower()
            an = _limpiar(fila[mapeo["apoderado_nombre"]]) if mapeo.get("apoderado_nombre") else ""
            at = _limpiar(fila[mapeo["apoderado_telefono"]]) if mapeo.get("apoderado_telefono") else ""
            if not dni: errs.append({"fila": nf, "motivo": "DNI vacio"}); continue
            if not re.fullmatch(r"\d{8}", dni): errs.append({"fila": nf, "motivo": "DNI invalido '" + dni + "'"}); continue
            if dni in vistos: errs.append({"fila": nf, "motivo": "DNI " + dni + " duplicado"}); continue
            if not nom or not ap or not gr or not sec: errs.append({"fila": nf, "motivo": "Faltan campos"}); continue
            if not con.execute("SELECT id FROM grados WHERE nombre=?", (gr,)).fetchone():
                errs.append({"fila": nf, "motivo": "Grado '" + gr + "' no existe"}); continue
            if tur in ("mañana", "manana", "m", "am", "mñ"): tn = "Mañana"
            elif tur in ("tarde", "t", "tm", "pm"): tn = "Tarde"
            else: errs.append({"fila": nf, "motivo": "Turno '" + tur + "'"}); continue
            vistos[dni] = nf
            val.append({"dni": dni, "nombres": nom, "apellido_paterno": ap, "apellido_materno": am,
                        "grado": gr, "seccion": sec, "turno": tn,
                        "apoderado_nombre": an, "apoderado_telefono": at})
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
    return ins, reac, errs


# ─── BLOQUEOS ─────────────────────────────────────────────────────────────
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

def historial_bloqueos_alumno(alumno_id):
    con = obtener_conexion()
    return pd.read_sql("SELECT id,motivo,activo,fecha_inicio,fecha_fin,creado_por,origen,liberado_por FROM bloqueos WHERE alumno_id=? ORDER BY id DESC",
                       con, params=[alumno_id])

def listar_bloqueados():
    return pd.read_sql("SELECT b.id,a.id AS alumno_id,a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS alumno,"
                       "g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,b.motivo,b.fecha_inicio,COALESCE(b.origen,'automatico') AS origen "
                       "FROM bloqueos b JOIN alumnos a ON b.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id "
                       "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE b.activo=1 ORDER BY b.fecha_inicio DESC",
                       obtener_conexion())

def obtener_auditoria(limite=500):
    return pd.read_sql("SELECT id,usuario,accion,fecha,ip FROM auditoria ORDER BY id DESC LIMIT " + str(int(limite)),
                       obtener_conexion())


# ─── JUSTIFICACIONES Y PERMISOS ───────────────────────────────────────────
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
    f = con.execute("SELECT id FROM permisos WHERE alumno_id=? AND activo=1 AND fecha_inicio<=? AND fecha_fin>=? LIMIT 1",
                    (idal, fecha, fecha)).fetchone()
    return bool(f)

def _puede_justificar(fecha_objetivo_str, tipo_asistencia=None, ventana_id=None):
    try:
        f_obj = datetime.strptime(fecha_objetivo_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False, "Fecha invalida."
    if tipo_asistencia and tipo_asistencia != FALTA:
        return False, "Solo se pueden justificar FALTAS."
    hoy = ahora().date()
    diff = (hoy - f_obj).days
    if diff > 0:
        return False, "Ya paso la ventana. Solo se puede justificar hoy, manana o pasado manana."
    if diff < -2:
        return False, "Solo se puede justificar hasta pasado manana."
    if diff == 0 and ventana_id is not None:
        con = obtener_conexion()
        v = con.execute("SELECT hora_apertura,hora_cierre FROM ventanas WHERE id=?", (ventana_id,)).fetchone()
        if v:
            h = hora_corta()
            if not (v["hora_apertura"] <= h <= v["hora_cierre"]):
                return False, "La ventana ya esta cerrada."
    return True, ""

def crear_justificacion_previa(idal, fecha_obj, tipo, motivo, usuario):
    if tipo != FALTA:
        return False, "Solo se pueden registrar justificaciones para FALTAS."
    ok, msg = _puede_justificar(fecha_obj, tipo_asistencia=FALTA)
    if not ok: return False, msg
    if not motivo or not motivo.strip(): return False, "El motivo es obligatorio."
    con = obtener_conexion()
    ex = con.execute("SELECT id FROM justificaciones_previas WHERE alumno_id=? AND fecha_objetivo=?", (idal, fecha_obj)).fetchone()
    if ex: return False, "Ya existe una justificacion para ese alumno en esa fecha."
    escribir("INSERT INTO justificaciones_previas(alumno_id,fecha_objetivo,tipo,motivo,creado_por,timestamp,aplicada) VALUES(?,?,?,?,?,?,0)",
             (idal, fecha_obj, FALTA, motivo.strip(), usuario["usuario"], timestamp_str()))
    auditar(usuario["usuario"], "Creo justificacion previa " + fecha_obj + " id=" + str(idal), tb="justificaciones_previas", rid=idal)
    return True, "Justificacion registrada. Se aplicara automaticamente cuando llegue el dia."

def crear_permiso(idal, fi, ff, motivo, usuario):
    try:
        fi_d = datetime.strptime(fi, "%Y-%m-%d").date()
        ff_d = datetime.strptime(ff, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False, "Fechas invalidas."
    hoy = ahora().date()
    manana = hoy + timedelta(days=1)
    if fi_d < manana:
        return False, "El permiso solo puede empezar desde manana en adelante."
    if ff_d < fi_d:
        return False, "La fecha fin no puede ser anterior a la fecha inicio."
    dias = (ff_d - fi_d).days + 1
    if dias > MAX_DIAS_PERMISO:
        return False, "El permiso no puede exceder " + str(MAX_DIAS_PERMISO) + " dias."
    if not motivo or not motivo.strip():
        return False, "El motivo es obligatorio."
    con = obtener_conexion()
    solape = con.execute(
        "SELECT id FROM permisos WHERE alumno_id=? AND activo=1 AND NOT (fecha_fin < ? OR fecha_inicio > ?)",
        (idal, fi, ff)).fetchone()
    if solape:
        return False, "El alumno ya tiene un permiso que se solapa con esas fechas."
    per = obtener_periodo_activo(); pid = per["id"] if per else None
    escribir("INSERT INTO permisos(alumno_id,fecha_inicio,fecha_fin,motivo,creado_por,timestamp,activo,periodo_id) VALUES(?,?,?,?,?,?,1,?)",
             (idal, fi, ff, motivo.strip(), usuario["usuario"], timestamp_str(), pid))
    auditar(usuario["usuario"], "Creo permiso " + fi + " a " + ff + " id=" + str(idal), tb="permisos", rid=idal)
    return True, "Permiso registrado del " + fi + " al " + ff + "."

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


# ─── ASISTENCIA (REGISTRO) ────────────────────────────────────────────────
def registrar_entrada(dni, usuario, origen="qr"):
    dni = (dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni): return False, "ERROR", "DNI invalido", {}
    con = obtener_conexion()
    al = _buscar_alumno_por_dni(con, dni)
    if not al: return False, "ERROR", "DNI no encontrado", {}
    bloq = alumno_bloqueado(al["id"])
    if bloq:
        auditar(usuario["usuario"], "Intento escaneo bloqueado DNI " + dni, tb="bloqueos", rid=al["id"])
        return False, "BLOQUEADO", _nombre_completo(al) + " | BLOQUEADO - retener y llevar a Direccion", {"alumno": al, "motivo": bloq["motivo"]}

    fecha = hoy_str(); ha = hora_corta(); hc = hora_str()
    per = obtener_periodo_activo(); pid = per["id"] if per else None
    dia = dia_especial_hoy(al["turno_id"], fecha, al["seccion_id"])
    if dia and dia["tipo"] == "feriado": return False, "ERROR", "Hoy es feriado, no se registra", {}
    v = ventana_activa_para_alumno(al["turno_id"], fecha, al["seccion_id"])
    if not v: return False, "ERROR", "Sin ventana activa (" + ha + ")", {}

    permiso = hay_permiso_activo(al["id"], fecha)
    tipo_real = v["tipo"]
    if v.get("es_evento") and v["tipo"] == VENT_CLASES:
        if v.get("contar_como_clases"): tipo_real = VENT_CLASES
        else: tipo_real = TIPO_ASIST_EVENTO
    dia_id = v.get("dia_especial_id")
    ev_nombre = v.get("evento_nombre")

    ex = con.execute("SELECT id,estado FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo=?", (al["id"], fecha, tipo_real)).fetchone()
    if ex: return False, "ERROR", _nombre_completo(al) + " ya registro " + tipo_real + " hoy (" + ex["estado"] + ")", {}

    if v["tipo"] == VENT_REF:
        with _lock_escritura:
            try:
                con.execute("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,origen,periodo_id,dia_especial_id) VALUES(?,?,?,'reforzamiento',?,?,?,?,?,?)",
                            (al["id"], fecha, v["id"], hc, REF_ASISTIO, 1 if permiso else 0, origen, pid, dia_id))
                if al["turno"] == "Tarde":
                    ya = con.execute("SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo='clases'", (al["id"], fecha)).fetchone()
                    if not ya:
                        con.execute("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,origen,periodo_id,dia_especial_id) VALUES(?,?,NULL,'clases',?,'Puntual',?,?,?,?)",
                                    (al["id"], fecha, hc, 1 if permiso else 0, origen, pid, dia_id))
                con.commit()
            except sqlite3.IntegrityError:
                con.rollback(); return False, "ERROR", _nombre_completo(al) + " ya registro reforzamiento hoy", {}
        auditar(usuario["usuario"], "Reforzamiento " + origen + " DNI " + dni, tb="asistencias")
        msg = (_nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] + " | Reforzamiento + Clases Puntual " + ha
               if al["turno"] == "Tarde" else
               _nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] + " | Asistio a reforzamiento " + ha)
        return True, "REFORZAMIENTO", msg, {"alumno": al, "evento_nombre": None}

    if al["turno"] == "Tarde":
        ya = con.execute("SELECT id,estado,hora FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo IN ('clases','evento')", (al["id"], fecha)).fetchone()
        if ya: return False, "ERROR", _nombre_completo(al) + " ya tiene registro hoy.", {}

    lim = v["hora_limite_efectiva"]
    est = PUNTUAL if ha <= lim else TARDANZA
    just_final = 1 if permiso else 0

    with _lock_escritura:
        try:
            con.execute("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,origen,periodo_id,dia_especial_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (al["id"], fecha, v["id"], tipo_real, hc, est, just_final, origen, pid, dia_id))
        except sqlite3.IntegrityError:
            con.rollback(); return False, "ERROR", _nombre_completo(al) + " ya registro hoy (carrera)", {}
        n = 0; acc = None
        if est == TARDANZA:
            n = contar_tardanzas_injustificadas(al["id"], pid) + 1
            acc = ACC_PERDONADO if n <= 2 else (ACC_DERIVADO if n == 3 else ACC_RETENIDO)
            try:
                con.execute("INSERT INTO tardanzas(alumno_id,fecha,hora,numero,accion,justificada,origen,registrado_por,timestamp,periodo_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (al["id"], fecha, hc, n, acc, just_final, origen, usuario["usuario"], timestamp_str(), pid))
            except sqlite3.IntegrityError:
                log.warning("tardanza duplicada alumno_id=%s fecha=%s", al["id"], fecha)
        con.commit()

    if est == TARDANZA:
        if n >= 4 and not alumno_bloqueado(al["id"]):
            crear_bloqueo(al["id"], str(n) + "ta tardanza injustificada (" + fecha + ")", usuario, "automatico")
        auditar(usuario["usuario"], "Tardanza " + str(n) + "a DNI " + dni + " -> " + acc, tb="tardanzas")
        sufijo = " [JUSTIFICADA]" if just_final else ""
        return True, "TARDANZA", _nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] + " | Tardanza " + str(n) + "a (" + acc + ")" + sufijo + " " + ha, {"alumno": al, "numero": n, "accion": acc, "evento_nombre": ev_nombre}

    auditar(usuario["usuario"], "Entrada Puntual " + origen + " DNI " + dni, tb="asistencias")
    return True, "PUNTUAL", _nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] + " | " + tipo_real.upper() + " Puntual " + ha, {"alumno": al, "evento_nombre": ev_nombre}


def marcar_faltas_al_cierre():
    fecha = hoy_str(); ha = hora_corta()
    con = obtener_conexion(); per = obtener_periodo_activo(); pid = per["id"] if per else None

    feriado = con.execute("SELECT id FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='feriado' LIMIT 1", (fecha,)).fetchone()
    if feriado: return

    evento = con.execute("SELECT id,turno_id,hora_entrada FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento' LIMIT 1", (fecha,)).fetchone()
    if evento:
        for t in listar_turnos():
            if evento["turno_id"] and t["id"] != evento["turno_id"]: continue
            for v in listar_ventanas(t["id"]):
                if v["tipo"] != VENT_CLASES: continue
                if ha < v["hora_cierre"]: continue
                for al in con.execute("SELECT a.id FROM alumnos a JOIN secciones s ON a.seccion_id=s.id WHERE s.turno_id=? AND a.activo=1", (t["id"],)).fetchall():
                    if con.execute("SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo='evento'", (al["id"], fecha)).fetchone(): continue
                    if hay_permiso_activo(al["id"], fecha):
                        escribir("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,observacion,origen,periodo_id,dia_especial_id) VALUES(?,?,?,'evento',?,?,1,?,?,?,?)",
                                 (al["id"], fecha, v["id"], hora_str(), PERMISO, "Permiso", "manual", pid, evento["id"])); continue
                    escribir("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,origen,periodo_id,dia_especial_id) VALUES(?,?,?,'evento',?,?,?,?,?)",
                             (al["id"], fecha, v["id"], hora_str(), "Falta", "auto", pid, evento["id"]))
        return

    if es_fin_de_semana(): return
    for t in listar_turnos():
        for v in listar_ventanas(t["id"]):
            if ha < v["hora_cierre"]: continue
            tipo = "clases" if v["tipo"] == VENT_CLASES else "reforzamiento"
            est = "Falta" if v["tipo"] == VENT_CLASES else "No asistio"
            for al in con.execute("SELECT a.id FROM alumnos a JOIN secciones s ON a.seccion_id=s.id WHERE s.turno_id=? AND a.activo=1", (t["id"],)).fetchall():
                if con.execute("SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo=?", (al["id"], fecha, tipo)).fetchone(): continue
                if hay_permiso_activo(al["id"], fecha):
                    escribir("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,observacion,origen,periodo_id) VALUES(?,?,?,?,?,?,1,?,?,?)",
                             (al["id"], fecha, v["id"], tipo, hora_str(), PERMISO, "Permiso otorgado", "manual", pid)); continue
                jp = _aplicar_just_prev(con, al["id"], fecha, "Falta") if v["tipo"] == VENT_CLASES else False
                escribir("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,origen,periodo_id) VALUES(?,?,?,?,?,?,?,?,?)",
                         (al["id"], fecha, v["id"], tipo, hora_str(), est, 1 if jp else 0, "auto", pid))


def justificar_asistencia(ida, obs, usuario):
    con = obtener_conexion()
    reg = con.execute("SELECT * FROM asistencias WHERE id=?", (ida,)).fetchone()
    if not reg: return False, "Registro no encontrado."
    if reg["estado"] != FALTA:
        return False, "Solo se pueden justificar FALTAS."
    if reg["justificada"]: return False, "Esta asistencia ya esta justificada."
    ok, msg = _puede_justificar(reg["fecha"], tipo_asistencia=FALTA, ventana_id=reg["ventana_id"])
    if not ok: return False, msg
    if not obs or not obs.strip(): return False, "La observacion es obligatoria."
    escribir("UPDATE asistencias SET justificada=1,observacion=?,justificado_por=?,justificado_en=? WHERE id=?",
             (obs.strip(), usuario["usuario"], timestamp_str(), ida))
    auditar(usuario["usuario"], "Justifico falta id=" + str(ida) + " motivo: " + obs, tb="asistencias", rid=ida)
    return True, "Falta justificada."

def quitar_justificacion(ida, usuario):
    con = obtener_conexion()
    reg = con.execute("SELECT * FROM asistencias WHERE id=?", (ida,)).fetchone()
    if not reg: return False, "Registro no encontrado."
    escribir("UPDATE asistencias SET justificada=0,observacion=NULL,justificado_por=NULL,justificado_en=NULL WHERE id=?", (ida,))
    auditar(usuario["usuario"], "Quito justificacion id=" + str(ida), tb="asistencias", rid=ida)
    return True, "Justificacion eliminada."


# ─── ESCANER QR ───────────────────────────────────────────────────────────
def _procesar_escaneo(dni):
    u = st.session_state.get("user")
    if not u: return
    ok, tipo, msg, extra = registrar_entrada(dni, u, origen="qr")
    if not ok and tipo == "ERROR":
        sonido = "duplicado" if ("ya registro" in msg or "ya tiene" in msg) else "error"
    elif tipo == "BLOQUEADO": sonido = "bloqueado"
    elif ok and tipo == "TARDANZA": sonido = "tardanza"
    elif ok: sonido = "puntual"
    else: sonido = "error"
    st.session_state.setdefault("_qr_mensajes", [])
    st.session_state["_qr_mensajes"].insert(0, {"dni": dni, "tipo": tipo, "mensaje": msg, "extra": extra, "ts": time.time()})
    st.session_state["_qr_mensajes"] = st.session_state["_qr_mensajes"][:10]
    contador = st.session_state.get("_qr_sonido_contador", 0) + 1
    st.session_state["_qr_sonido_contador"] = contador
    st.session_state["_qr_sonido_pendiente"] = {"kind": sonido, "nonce": contador, "ts": time.time()}

def _render_mensaje_qr(msg):
    tipo = msg["tipo"]; mensaje = msg["mensaje"]
    extra = msg.get("extra") or {}
    alumno = extra.get("alumno") or {}
    ev = extra.get("evento_nombre")
    clase = {"PUNTUAL":"qr-puntual","TARDANZA":"qr-tardanza","REFORZAMIENTO":"qr-refuerzo",
             "BLOQUEADO":"qr-bloqueado","ERROR":"qr-error"}.get(tipo, "qr-error")
    if tipo == "TARDANZA":
        acc = extra.get("accion")
        if acc == ACC_DERIVADO: mensaje += " -> Derivar a Direccion"; clase = "qr-derivado"
        elif acc == ACC_RETENIDO: mensaje += " -> Retener hasta apoderado"; clase = "qr-retenido"
    html = '<div class="qr-msg ' + clase + '">'
    if ev: html += '<div class="qr-evento">Evento: ' + str(ev) + '</div>'
    if alumno:
        nombre = (alumno.get("apellido_paterno","") + " " + (alumno.get("apellido_materno") or "") + ", " + alumno.get("nombres","")).strip(", ")
        html += '<div class="qr-nombre">' + nombre + '</div>'
    html += '<div class="qr-texto">' + mensaje + '</div></div>'
    st.markdown(html, unsafe_allow_html=True)

def escaner_qr_continuo(key="qr_scanner"):
    st.markdown('<div class="scan-header"><div class="scan-titulo">Escaneo QR</div><div class="scan-sub">Apunta al codigo del alumno</div></div>', unsafe_allow_html=True)
    def _on_scan(): pass
    sp = st.session_state.get("_qr_sonido_pendiente") or {}
    sonido_kind = sp.get("kind", ""); sonido_nonce = sp.get("nonce", 0)
    mount_id = st.session_state.get("_qr_mount_id", 0)
    result = qr_scanner(key="qr_" + key + "_" + str(mount_id), on_scan=_on_scan,
                        sonido_kind=sonido_kind, sonido_nonce=sonido_nonce)
    if result is not None and getattr(result, "qr_dni", None):
        dni = str(result.qr_dni).strip()
        ult = st.session_state.get("_ultimo_qr_scan", {})
        if not (ult.get("dni") == dni and (time.time() - ult.get("ts", 0)) < 1.5):
            st.session_state["_ultimo_qr_scan"] = {"dni": dni, "ts": time.time()}
            _procesar_escaneo(dni)
            st.rerun()
    if sp and sp.get("nonce"): pass
    mensajes = st.session_state.get("_qr_mensajes", [])
    if mensajes:
        m = mensajes[0]
        if (time.time() - m.get("ts", 0)) < 8: _render_mensaje_qr(m)
        if len(mensajes) > 1:
            st.markdown('<div class="scan-ultimos">Ultimos escaneos</div>', unsafe_allow_html=True)
            for msg in mensajes[1:6]: _render_mensaje_qr(msg)

# ─── REPORTES BASE ────────────────────────────────────────────────────────
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
    return pd.read_sql("SELECT a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres,"
                       "g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,ast.tipo,ast.hora,ast.estado "
                       "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id "
                       "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
                       "WHERE ast.fecha=? AND ast.hora IS NOT NULL ORDER BY ast.hora DESC LIMIT ?",
                       obtener_conexion(), params=[fecha, limite])


# ─── PERFIL ───────────────────────────────────────────────────────────────
def perfil_alumno_datos(idal):
    con = obtener_conexion()
    a = con.execute("SELECT a.*,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,s.id AS seccion_id,t.id AS turno_id "
                    "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
                    "JOIN turnos t ON s.turno_id=t.id WHERE a.id=?", (idal,)).fetchone()
    if not a: return {}
    a = dict(a)
    da = pd.read_sql("SELECT a.id,a.fecha,a.hora,a.tipo,a.estado,a.justificada,COALESCE(a.observacion,'') AS observacion,"
                     "COALESCE(a.origen,'qr') AS origen,COALESCE(a.justificado_por,'') AS justificado_por,"
                     "COALESCE(a.justificado_en,'') AS justificado_en,COALESCE(d.descripcion,'') AS evento_nombre,"
                     "a.ventana_id "
                     "FROM asistencias a LEFT JOIN dias_especiales d ON a.dia_especial_id=d.id "
                     "WHERE a.alumno_id=? ORDER BY a.fecha DESC,a.hora DESC", con, params=[idal])
    dt = pd.read_sql("SELECT fecha,hora,numero AS 'N',accion,justificada,COALESCE(origen,'qr') AS origen FROM tardanzas WHERE alumno_id=? ORDER BY fecha DESC,hora DESC", con, params=[idal])
    db = pd.read_sql("SELECT fecha_inicio,COALESCE(fecha_fin,'-') AS fecha_fin,COALESCE(motivo,'') AS motivo,activo FROM bloqueos WHERE alumno_id=? ORDER BY fecha_inicio DESC", con, params=[idal])
    dp = pd.read_sql("SELECT fecha_inicio,fecha_fin,COALESCE(motivo,'') AS motivo,activo,creado_por FROM permisos WHERE alumno_id=? ORDER BY fecha_inicio DESC", con, params=[idal])
    dj = pd.read_sql("SELECT fecha_objetivo, tipo, motivo, aplicada, creado_por, timestamp "
                     "FROM justificaciones_previas WHERE alumno_id=? ORDER BY fecha_objetivo DESC", con, params=[idal])
    tp = int((da["estado"] == PUNTUAL).sum()) if not da.empty else 0
    tf = int((da["estado"] == FALTA).sum()) if not da.empty else 0
    tt = int((da["estado"] == TARDANZA).sum()) if not da.empty else 0
    tr = int(((da["tipo"] == "reforzamiento") & (da["estado"] == REF_ASISTIO)).sum()) if not da.empty else 0
    return {"alumno": a, "asistencias": da, "tardanzas": dt, "bloqueos": db, "permisos": dp,
            "justificaciones_previas": dj,
            "total_puntuales": tp, "total_faltas": tf, "total_tardanzas": tt, "total_ref_asistio": tr,
            "tard_injust": contar_tardanzas_injustificadas(idal),
            "bloqueado": alumno_bloqueado(idal) is not None}


# ─── CIERRE ANUAL ─────────────────────────────────────────────────────────
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
        df = pd.read_sql("SELECT a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres,"
                         "g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,ast.fecha,ast.hora,ast.tipo,ast.estado,"
                         "ast.justificada,COALESCE(ast.observacion,'') AS observacion "
                         "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id "
                         "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
                         "JOIN turnos t ON s.turno_id=t.id "
                         "WHERE ast.periodo_id=? AND ast.fecha BETWEEN ? AND ? "
                         "ORDER BY ast.fecha,t.nombre,g.nombre,s.nombre,a.apellido_paterno",
                         con, params=[pid, im.strftime("%Y-%m-%d"), fm.strftime("%Y-%m-%d")])
        res[str(cur.year) + "-" + str(cur.month).zfill(2)] = df
        cur = date(cur.year+1, 1, 1) if cur.month == 12 else date(cur.year, cur.month+1, 1)
    return res

def cerrar_año_escolar(usuario, pid, nuevo_nombre, fi, ff):
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
        con.rollback(); log.error("cerrar año: %s", e); return False, "Error al cerrar el año."
    auditar(usuario["usuario"], "Cerro periodo " + p["nombre"], tb="periodos", rid=pid)
    return True, "Periodo '" + p["nombre"] + "' cerrado. Nuevo: '" + nuevo_nombre + "'."

def listar_cierres_anuales():
    return pd.read_sql("SELECT c.id,p.nombre AS periodo,c.fecha_cierre,c.generado_por FROM cierres_anuales c JOIN periodos p ON c.periodo_id=p.id ORDER BY c.id DESC",
                       obtener_conexion())


# ─── PDF / EXCEL / QR ─────────────────────────────────────────────────────
def _pdf_base(titulo, subtitulo=None, paisaje=False):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(A4[1],A4[0]) if paisaje else A4,
                            rightMargin=25, leftMargin=25, topMargin=25, bottomMargin=25)
    est = getSampleStyleSheet()
    el = [Paragraph("<b>" + titulo + "</b>", est["Heading1"])]
    if subtitulo: el.append(Paragraph(subtitulo, est["Normal"]))
    el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"), est["Normal"]))
    el.append(Spacer(1, 15))
    return buf, doc, el, est

def generar_pdf_tabla_ancha(df, titulo, subtitulo=None, fuente_chica=False):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(A4[1], A4[0]), rightMargin=15, leftMargin=15,
                            topMargin=20, bottomMargin=20)
    est = getSampleStyleSheet()
    el = [Paragraph("<b>" + titulo + "</b>", est["Heading1"])]
    if subtitulo: el.append(Paragraph(subtitulo, est["Normal"]))
    el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"), est["Normal"]))
    el.append(Spacer(1, 12))
    if not df.empty:
        datos = [df.columns.tolist()] + df.astype(str).values.tolist()
        anchos = []
        for col in df.columns:
            cl = str(col).lower()
            if any(k in cl for k in ["auxiliar", "apellido", "nombres", "alumno", "observacion"]): anchos.append(3.5)
            elif any(k in cl for k in ["grado", "seccion", "turno", "tipo", "estado"]): anchos.append(1.5)
            elif any(k in cl for k in ["fecha", "hora"]): anchos.append(1.8)
            else: anchos.append(1.0)
        at = A4[1] - 30; sm = sum(anchos)
        anchos = [a * at / sm for a in anchos]
        GRIS_FILA_ALT = colors.HexColor("#FAFAFA")
        if fuente_chica: fc = ff = 6; pad = 3
        else: fc = 11; ff = 10; pad = 5
        t = Table(datos, colWidths=anchos, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), fc),
            ("FONTSIZE", (0, 1), (-1, -1), ff),
            ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.black),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), pad),
            ("RIGHTPADDING", (0, 0), (-1, -1), pad),
            ("TOPPADDING", (0, 0), (-1, -1), pad),
            ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GRIS_FILA_ALT]),
        ]))
        el.append(t)
    doc.build(el); buf.seek(0); return buf.getvalue()

def generar_pdf_multilhoja(hojas, titulo_base="Reporte"):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(A4[1], A4[0]), rightMargin=15, leftMargin=15,
                            topMargin=20, bottomMargin=20)
    est = getSampleStyleSheet()
    el = []; primera = True
    for nombre_hoja, bloques in hojas.items():
        if not primera: el.append(PageBreak())
        primera = False
        el.append(Paragraph("<b>" + titulo_base + "</b>", est["Heading2"]))
        el.append(Paragraph("<b>" + nombre_hoja + "</b>", est["Heading3"]))
        el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"), est["Normal"]))
        el.append(Spacer(1, 10))
        for titulo_bloque, df in bloques:
            el.append(Paragraph("<b>" + titulo_bloque + "</b>", est["Heading4"]))
            el.append(Spacer(1, 4))
            if df is None or df.empty:
                el.append(Paragraph("(Sin datos)", est["Normal"]))
                el.append(Spacer(1, 10))
                continue
            datos = [df.columns.tolist()] + df.astype(str).values.tolist()
            n_cols = len(df.columns); at = A4[1] - 30; ac = at / n_cols
            t = Table(datos, colWidths=[ac]*n_cols, repeatRows=1)
            t.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("FONTSIZE", (0, 1), (-1, -1), 8),
                ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            el.append(t)
            el.append(Spacer(1, 12))
    doc.build(el); buf.seek(0); return buf.getvalue()

def generar_qr(dni):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(str(dni).strip()); qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")

def _generar_fotocheck_pil(alumno, escudo_path=None):
    from PIL import Image, ImageDraw, ImageFont
    from datetime import datetime
    ANCHO_PX = 817; ALTO_PX = 550
    NARANJA_OSCURO = (225, 150, 90); NARANJA_CLARO = (240, 175, 115)
    NARANJA_FRANJA = (220, 140, 80); FONDO_ANARANJADO = (255, 230, 200)
    BLANCO = (255, 255, 255); NEGRO = (17, 17, 17)
    GRIS_LABEL = (90, 60, 30); GRIS_TXT = (150, 150, 150)
    img = Image.new("RGB", (ANCHO_PX, ALTO_PX), FONDO_ANARANJADO)
    draw = ImageDraw.Draw(img)
    color_punto = (235, 210, 180); paso = 22; radio = 1
    for py in range(0, ALTO_PX, paso):
        for px in range(0, ANCHO_PX, paso):
            draw.ellipse([px - radio, py - radio, px + radio, py + radio], fill=color_punto)
    if escudo_path and Path(escudo_path).exists():
        try:
            esc = Image.open(str(escudo_path)).convert("RGBA").resize((280, 280), Image.LANCZOS)
            a = esc.split()[3]; a = a.point(lambda p: int(p * 0.15)); esc.putalpha(a)
            img.paste(esc, ((ANCHO_PX - 280) // 2 + 100, (ALTO_PX - 280) // 2), esc)
        except Exception: pass
    FOTO_W = 216; FOTO_H = 280; FRANJA_W = FOTO_W
    for y in range(ALTO_PX):
        t = y / ALTO_PX
        r = int(NARANJA_OSCURO[0] + (NARANJA_CLARO[0] - NARANJA_OSCURO[0]) * t)
        g = int(NARANJA_OSCURO[1] + (NARANJA_CLARO[1] - NARANJA_OSCURO[1]) * t)
        b = int(NARANJA_OSCURO[2] + (NARANJA_CLARO[2] - NARANJA_OSCURO[2]) * t)
        draw.line([(0, y), (FRANJA_W, y)], fill=(r, g, b))
    def _font(size, bold=False, italic=False):
        nombres = []
        if bold and italic: nombres = ["arialbi.ttf", "DejaVuSans-BoldOblique.ttf"]
        elif bold: nombres = ["arialbd.ttf", "DejaVuSans-Bold.ttf", "Helvetica-Bold"]
        elif italic: nombres = ["ariali.ttf", "DejaVuSans-Oblique.ttf"]
        else: nombres = ["arial.ttf", "DejaVuSans.ttf", "Helvetica"]
        for n in nombres:
            try: return ImageFont.truetype(n, size)
            except Exception: continue
        return ImageFont.load_default()
    f_colegio = _font(13, bold=True); f_foto = _font(14, bold=True)
    f_titulo = _font(20, bold=True); f_frase = _font(19, bold=True, italic=True); f_label = _font(20, bold=True)
    esc_size = 80; esc_x = (FRANJA_W - esc_size) // 2
    espacio = ALTO_PX - FOTO_H; alto_bloque = esc_size + 70
    esc_y = max(8, (espacio - alto_bloque) // 2 + 10)
    if escudo_path and Path(escudo_path).exists():
        try:
            esc = Image.open(str(escudo_path)).convert("RGBA").resize((esc_size, esc_size), Image.LANCZOS)
            img.paste(esc, (esc_x, esc_y), esc)
        except Exception: pass
    def _txt_centrado(texto, y, font, color):
        try:
            bbox = draw.textbbox((0, 0), texto, font=font); tw = bbox[2] - bbox[0]
        except Exception: tw = len(texto) * 7
        draw.text(((FRANJA_W - tw) // 2, y), texto, fill=color, font=font)
    y_txt = esc_y + esc_size + 6
    for lbl in ["INSTITUCION", "EDUCATIVA", "YARINACOCHA"]:
        _txt_centrado(lbl, y_txt, f_colegio, BLANCO); y_txt += 14
    foto_x = 0; foto_y = ALTO_PX - FOTO_H
    draw.rectangle([foto_x, foto_y, foto_x + FOTO_W, foto_y + FOTO_H], fill=BLANCO)
    draw.rectangle([foto_x, foto_y, foto_x + FOTO_W - 1, foto_y + FOTO_H - 1], outline=NARANJA_FRANJA, width=1)
    try:
        bbox = draw.textbbox((0, 0), "FOTO", font=f_foto); tw = bbox[2] - bbox[0]; th = bbox[3] - bbox[1]
    except Exception: tw = 40; th = 12
    draw.text((foto_x + (FOTO_W - tw) // 2, foto_y + (FOTO_H - th) // 2), "FOTO", fill=GRIS_TXT, font=f_foto)
    DER_X = FRANJA_W + 14; titulo_txt = "FOTOCHECK DEL ESTUDIANTE"
    try:
        bbox = draw.textbbox((0, 0), titulo_txt, font=f_titulo); tw = bbox[2] - bbox[0]
    except Exception: tw = 280
    espacio_d = ANCHO_PX - DER_X
    draw.text((DER_X + (espacio_d - tw) // 2, 12), titulo_txt, fill=NEGRO, font=f_titulo)
    ap_p = alumno['apellido_paterno'].upper(); ap_m = (alumno['apellido_materno'] or "").upper()
    nombres = alumno['nombres'].upper(); dni = alumno["dni"]
    grado = alumno['grado'].upper(); seccion = alumno['seccion'].upper()
    turno = alumno['turno'].upper(); anio = str(datetime.now().year)
    info_x = DER_X; QR_SIZE = 215; qr_x = ANCHO_PX - QR_SIZE - 12
    ancho_info = qr_x - info_x - 12
    def _ajustar(texto, size_ini, max_ancho, bold):
        size = size_ini
        while size > 10:
            f = _font(size, bold=bold)
            try: ancho = draw.textlength(texto, font=f)
            except Exception: ancho = len(texto) * size * 0.55
            if ancho <= max_ancho: return f
            size -= 1
        return _font(10, bold=bold)
    INFO_Y = 130; alto_linea = 52
    ap_full = ap_p + " " + ap_m
    draw.text((info_x, INFO_Y), ap_full, fill=NEGRO, font=_ajustar(ap_full, 32, ancho_info, True))
    y2 = INFO_Y + alto_linea
    draw.text((info_x, y2), nombres, fill=NEGRO, font=_ajustar(nombres, 32, ancho_info, True))
    def _linea(y, label, valor, size=28):
        draw.text((info_x, y), label, fill=GRIS_LABEL, font=f_label)
        an = draw.textlength(label, font=f_label); vx = info_x + int(an) + 10
        draw.text((vx, y), valor, fill=NEGRO, font=_ajustar(valor, size, ancho_info - int(an) - 10, True))
    _linea(y2 + alto_linea, "DNI:", dni)
    _linea(y2 + 2 * alto_linea, "GRADO:", grado + " \"" + seccion + "\"")
    _linea(y2 + 3 * alto_linea, "TURNO:", turno)
    _linea(y2 + 4 * alto_linea, "ANIO:", anio)
    qr = qrcode.QRCode(version=1, box_size=10, border=1); qr.add_data(str(alumno["dni"]).strip()); qr.make(fit=True)
    qr_img_pil = qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((QR_SIZE, QR_SIZE), Image.LANCZOS)
    img.paste(qr_img_pil, (qr_x, 60))
    frase = "\"Ser del CNY, es ser mejor\""
    try:
        bbox = draw.textbbox((0, 0), frase, font=f_frase); fw = bbox[2] - bbox[0]
    except Exception: fw = 250
    draw.text((DER_X + (espacio_d - fw) // 2, ALTO_PX - 42), frase, fill=NARANJA_FRANJA, font=f_frase)
    return img

def _render_carnets(filas, titulo=None):
    buf = BytesIO(); m = 5
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=m, leftMargin=m, topMargin=m, bottomMargin=m)
    est = getSampleStyleSheet()
    MM = 2.8346; ANCHO = 85.0 * MM; ALTO = 50.0 * MM
    escudo_path = Path("escudo.png")
    def _fotocheck(alumno):
        img = _generar_fotocheck_pil(alumno, escudo_path if escudo_path.exists() else None)
        ib = BytesIO(); img.save(ib, format="PNG"); ib.seek(0)
        return RLImage(ib, width=ANCHO, height=ALTO)
    el = []
    if titulo:
        el.append(Paragraph("<b>" + titulo + "</b>", est["Heading2"])); el.append(Spacer(1, 6))
    sep_x = 6; sep_y = 10; caben_x = 2
    alto_hoja = A4[1] - 2 * m
    caben_y = max(1, int((alto_hoja + sep_y) // (ALTO + sep_y)))
    fotochecks = [_fotocheck(a) for a in filas]
    for i in range(0, len(fotochecks), caben_x * caben_y):
        lote = fotochecks[i:i + caben_x * caben_y]; tabla_filas = []
        for j in range(0, len(lote), caben_x):
            fila = lote[j:j + caben_x]
            while len(fila) < caben_x: fila.append("")
            tabla_filas.append(fila)
        while len(tabla_filas) < caben_y: tabla_filas.append([""] * caben_x)
        t = Table(tabla_filas, colWidths=[ANCHO, ANCHO], rowHeights=[ALTO] * len(tabla_filas), hAlign="CENTER")
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                ("LEFTPADDING", (0, 0), (-1, -1), sep_x / 2), ("RIGHTPADDING", (0, 0), (-1, -1), sep_x / 2),
                                ("TOPPADDING", (0, 0), (-1, -1), sep_y / 2), ("BOTTOMPADDING", (0, 0), (-1, -1), sep_y / 2)]))
        el.append(t)
        if i + caben_x * caben_y < len(fotochecks): el.append(PageBreak())
    doc.build(el); buf.seek(0); return buf.getvalue()

def _filas_alumnos_por_seccion(idsec):
    con = obtener_conexion()
    return con.execute("SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno "
                       "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
                       "JOIN turnos t ON s.turno_id=t.id WHERE a.seccion_id=? AND a.activo=1 "
                       "ORDER BY a.apellido_paterno,a.apellido_materno", (idsec,)).fetchall()

def pdf_carnets_por_seccion(idsec):
    f = _filas_alumnos_por_seccion(idsec)
    return _render_carnets(f) if f else None

def pdf_carnets_seleccionados(ids, titulo=None):
    if not ids: return None
    con = obtener_conexion()
    ph = ",".join("?"*len(ids))
    f = con.execute("SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno "
                    "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
                    "JOIN turnos t ON s.turno_id=t.id WHERE a.id IN (" + ph + ") AND a.activo=1 "
                    "ORDER BY a.apellido_paterno,a.apellido_materno", ids).fetchall()
    return _render_carnets(f, titulo) if f else None

def pdf_carnet_alumno(dni):
    con = obtener_conexion()
    f = con.execute("SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno "
                    "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
                    "JOIN turnos t ON s.turno_id=t.id WHERE a.dni=? AND a.activo=1", (dni,)).fetchall()
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
    el.append(Spacer(1, 15))
    el.append(Paragraph("<b>Resumen</b>", est["Heading2"]))
    resumen = [["Puntuales","Tardanzas","Faltas","Reforzamiento","Tard. injust."],
               [d["total_puntuales"], d["total_tardanzas"], d["total_faltas"], d["total_ref_asistio"], d["tard_injust"]]]
    t = Table(resumen)
    t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor(C_NARANJA)),
                            ("TEXTCOLOR",(0,0),(-1,0),colors.whitesmoke),
                            ("ALIGN",(0,0),(-1,-1),"CENTER"),
                            ("GRID",(0,0),(-1,-1),0.5,colors.grey)]))
    el.append(t)
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


# ─── ESTILOS CSS ──────────────────────────────────────────────────────────
def aplicar_estilos():
    st.markdown("""
    <style>
    :root {
        --naranja: #E65100;
        --naranja-osc: #BF360C;
        --naranja-cla: #FF9800;
        --naranja-suave: rgba(230, 81, 0, 0.08);
        --naranja-borde: rgba(230, 81, 0, 0.35);
        --sombra: 0 2px 8px rgba(0,0,0,0.08);
        --sombra-hover: 0 4px 16px rgba(230, 81, 0, 0.20);
        --radius: 12px;
        --radius-sm: 8px;
    }
    html, body, [class*="css"], .stApp {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
    }
    h1, h2, h3 { font-weight: 800 !important; }
    h1 {
        font-size: 2rem !important;
        background: linear-gradient(135deg, #E65100, #FF9800);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    h2 { font-size: 1.5rem !important; color: #E65100 !important; }

    /* ─── SIDEBAR: solo ajustamos el borde y los radios, sin tocar colores ─── */
    section[data-testid="stSidebar"] {
        border-right: 1px solid var(--naranja-borde);
    }
    section[data-testid="stSidebar"] .stRadio label {
        border-radius: var(--radius-sm) !important;
        padding: 11px 14px !important;
        font-weight: 500 !important;
        font-size: 14px !important;
        margin: 2px 0 !important;
        cursor: pointer;
        border-left: 3px solid transparent;
    }
    section[data-testid="stSidebar"] .stRadio label:hover {
        background: var(--naranja-suave) !important;
        border-left-color: #FF9800;
    }
    section[data-testid="stSidebar"] .stRadio label:has(input:checked) {
        background: linear-gradient(90deg, rgba(230,81,0,0.18), transparent) !important;
        border-left-color: #E65100;
        color: #E65100 !important;
        font-weight: 700 !important;
    }
    section[data-testid="stSidebar"] .stRadio input { display: none; }

    /* ─── SIDEBAR MOVIL: dejamos que Streamlit maneje el color, solo quitamos la transparencia ─── */
    @media (max-width: 768px) {
        section[data-testid="stSidebar"] {
            background-image: none !important;
            opacity: 1 !important;
            z-index: 999999 !important;
        }
        section[data-testid="stSidebar"] > div:first-child {
            opacity: 1 !important;
        }
        div[data-testid="stSidebarOverlay"] {
            background-color: rgba(0, 0, 0, 0.4) !important;
            opacity: 1 !important;
        }
        section[data-testid="stSidebar"]::before,
        section[data-testid="stSidebar"]::after {
            display: none !important;
            background: transparent !important;
        }
    }

    /* ─── ENCABEZADO SIDEBAR ─── */
    .encabezado-sidebar {
        background: linear-gradient(135deg, #E65100, #FF9800);
        padding: 20px 16px;
        margin: 8px 10px 18px 10px;
        border-radius: var(--radius);
        text-align: center;
        color: #FFF;
        box-shadow: var(--sombra-hover);
    }
    .encabezado-sidebar .avatar {
        width: 58px; height: 58px;
        border-radius: 50%;
        background: rgba(255,255,255,0.25);
        display: flex; align-items: center; justify-content: center;
        font-size: 24px; font-weight: 800;
        color: #FFF;
        margin: 0 auto 10px auto;
        border: 2px solid rgba(255,255,255,0.5);
    }
    .encabezado-sidebar .nombre { font-size: 15px; font-weight: 700; color: #FFF !important; }
    .encabezado-sidebar .rol {
        display: inline-block; margin-top: 8px;
        padding: 4px 12px;
        background: rgba(255,255,255,0.25);
        border-radius: 20px;
        font-size: 10px; font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.10em;
        color: #FFF !important;
    }

    /* ─── INPUTS ─── */
    .stTextInput input, .stNumberInput input, .stDateInput input,
    .stTimeInput input, .stTextArea textarea, .stSelectbox > div > div {
        border-radius: var(--radius-sm) !important;
        min-height: 44px;
        border: 2px solid rgba(128,128,128,0.15) !important;
    }

    /* ─── BOTONES ─── */
    .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
        background: linear-gradient(135deg, #E65100, #FF9800) !important;
        color: #FFFFFF !important;
        border-radius: var(--radius-sm) !important;
        font-weight: 700 !important;
        border: none !important;
        padding: 12px 22px !important;
        box-shadow: 0 2px 8px rgba(230,81,0,0.25);
        font-size: 14px !important;
        min-height: 46px;
    }
    .stButton > button:hover {
        background: linear-gradient(135deg, #BF360C, #E65100) !important;
        box-shadow: 0 6px 20px rgba(230,81,0,0.35);
        transform: translateY(-2px);
    }

    /* ─── METRICAS ─── */
    div[data-testid="stMetric"] {
        background: linear-gradient(180deg, rgba(230,81,0,0.05), rgba(255,152,0,0.02));
        border: 1px solid var(--naranja-borde);
        border-radius: var(--radius);
        padding: 20px 22px !important;
    }
    div[data-testid="stMetric"] label {
        font-size: 11px !important; font-weight: 700 !important;
        text-transform: uppercase; letter-spacing: 0.08em !important;
    }
    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
        font-size: 30px !important; font-weight: 800 !important;
        color: #E65100 !important;
    }

    /* ─── TABS ─── */
    .stTabs [data-baseweb="tab-list"] {
        gap: 4px; border-bottom: 2px solid var(--naranja-borde);
    }
    .stTabs [data-baseweb="tab"] {
        font-weight: 600 !important; padding: 12px 18px !important;
        font-size: 13px !important;
    }
    .stTabs [aria-selected="true"] {
        font-weight: 800 !important; color: #E65100 !important;
        border-bottom: 3px solid #E65100 !important;
    }

    /* ─── EXPANDER ─── */
    .streamlit-expanderHeader, details summary {
        border: 1px solid var(--naranja-borde) !important;
        border-radius: var(--radius-sm) !important;
        font-weight: 600 !important;
        padding: 14px 16px !important;
    }

    /* ─── SCAN HEADER ─── */
    .scan-header {
        background: linear-gradient(135deg, #E65100, #FF9800);
        padding: 24px 28px; border-radius: var(--radius);
        margin-bottom: 20px; color: #FFF;
        box-shadow: var(--sombra-hover);
    }
    .scan-header .scan-titulo { font-size: 24px; font-weight: 800; color: #FFF; }
    .scan-header .scan-sub { font-size: 14px; opacity: 0.9; margin-top: 4px; color: #FFF; }
    .scan-ultimos {
        font-size: 11px; font-weight: 800;
        text-transform: uppercase; letter-spacing: 0.12em;
        margin: 24px 0 12px 0; padding-bottom: 8px;
        border-bottom: 2px solid var(--naranja-borde);
        color: #E65100;
    }

    /* ─── MENSAJES QR ─── */
    .qr-msg {
        padding: 14px 18px; border-radius: var(--radius-sm);
        margin: 8px 0; border: 1px solid rgba(128,128,128,0.15);
        border-left: 5px solid #808080;
        background: rgba(128,128,128,0.03);
    }
    .qr-nombre { font-size: 15px; font-weight: 800; margin-bottom: 4px; }
    .qr-evento {
        font-size: 11px; font-weight: 800;
        text-transform: uppercase; letter-spacing: 0.10em;
        margin-bottom: 5px; color: #E65100;
        display: inline-block; padding: 2px 8px;
        background: rgba(230,81,0,0.12); border-radius: 6px;
    }
    .qr-texto { font-size: 14px; font-weight: 500; line-height: 1.4; }
    .qr-puntual { border-left-color: #22C55E; background: linear-gradient(90deg, rgba(34,197,94,0.06), transparent); }
    .qr-tardanza { border-left-color: #F59E0B; background: linear-gradient(90deg, rgba(245,158,11,0.06), transparent); }
    .qr-derivado { border-left-color: #FF9800; background: linear-gradient(90deg, rgba(255,152,0,0.08), transparent); }
    .qr-retenido { border-left-color: #FF7043; font-weight: 700; background: linear-gradient(90deg, rgba(255,112,67,0.08), transparent); }
    .qr-refuerzo { border-left-color: #42A5F5; background: linear-gradient(90deg, rgba(66,165,245,0.06), transparent); }
    .qr-bloqueado { border-left-color: #EF4444; border: 2px solid #EF4444; font-weight: 800; background: linear-gradient(90deg, rgba(239,68,68,0.10), transparent); }
    .qr-error { border-left-color: #888888; }

    /* ─── PERFIL ─── */
    .perfil-hero {
        background: linear-gradient(135deg, #E65100, #FF9800);
        border-radius: 14px; padding: 26px 28px;
        margin-bottom: 20px; color: #FFF;
        box-shadow: 0 6px 20px rgba(230, 81, 0, 0.30);
        display: flex; align-items: center; gap: 22px; flex-wrap: wrap;
    }
    .perfil-hero .hero-avatar {
        width: 84px; height: 84px; border-radius: 50%;
        background: rgba(255, 255, 255, 0.22);
        border: 3px solid rgba(255, 255, 255, 0.55);
        display: flex; align-items: center; justify-content: center;
        font-size: 34px; font-weight: 800; color: #FFF; flex-shrink: 0;
    }
    .perfil-hero .hero-info { flex: 1; min-width: 220px; }
    .perfil-hero .hero-nombre {
        font-size: 26px; font-weight: 800; margin-bottom: 6px; color: #FFF;
    }
    .perfil-hero .hero-meta { font-size: 14px; opacity: 0.95; line-height: 1.6; color: #FFF; }
    .perfil-hero .hero-badge {
        display: inline-block; padding: 5px 14px; border-radius: 20px;
        font-size: 10.5px; font-weight: 800; text-transform: uppercase;
        letter-spacing: 0.12em; background: rgba(255, 255, 255, 0.25);
        color: #FFF; margin-top: 10px;
        border: 1px solid rgba(255, 255, 255, 0.4);
    }
    .perfil-bloque {
        background: rgba(128, 128, 128, 0.04);
        border: 1px solid var(--naranja-borde);
        border-radius: 12px; padding: 20px 22px; margin-bottom: 20px;
    }
    .perfil-bloque .titulo-seccion {
        font-size: 11px; font-weight: 800;
        text-transform: uppercase; letter-spacing: 0.12em;
        color: #E65100; margin-bottom: 14px;
        padding-bottom: 8px; border-bottom: 1px solid var(--naranja-borde);
    }
    .kpi-grid {
        display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 14px;
    }
    .kpi-card {
        background: linear-gradient(180deg, rgba(230,81,0,0.06), rgba(255,152,0,0.02));
        border: 1px solid var(--naranja-borde);
        border-radius: 10px; padding: 16px 12px; text-align: center;
    }
    .kpi-card .kpi-num {
        font-size: 28px; font-weight: 800; color: #E65100;
        line-height: 1; margin-bottom: 6px;
    }
    .kpi-card .kpi-lbl {
        font-size: 10px; font-weight: 700;
        text-transform: uppercase; letter-spacing: 0.08em;
        color: rgba(128,128,128,0.9); line-height: 1.3;
    }
    .kpi-card .kpi-lbl-alerta { color: #C62828; }
    .info-linea {
        display: flex; align-items: center; gap: 10px;
        font-size: 14px; padding: 6px 0;
    }
    .info-linea .info-label {
        font-size: 11px; font-weight: 800;
        text-transform: uppercase; letter-spacing: 0.08em;
        color: rgba(128,128,128,0.85); min-width: 90px;
    }
    .info-linea .info-valor { font-weight: 600; }
    .btn-sel .stButton > button {
        background: linear-gradient(135deg, #43A047, #2E7D32) !important;
        color: #FFFFFF !important;
    }
    .puerta-header {
        background: linear-gradient(135deg, #E65100 0%, #FF9800 100%);
        padding: 26px 32px; border-radius: var(--radius);
        color: white; margin-bottom: 24px;
        box-shadow: 0 8px 24px rgba(230,81,0,0.30);
    }
    .puerta-header .titulo { font-size: 26px; font-weight: 800; }
    .puerta-header .fecha { font-size: 14px; opacity: 0.9; margin-top: 4px; }
    hr { border: none; height: 1px; background: var(--naranja-borde); margin: 20px 0; }

    /* ─── MOBILE AJUSTES GENERALES ─── */
    @media (max-width: 768px) {
        h1 { font-size: 1.4rem !important; }
        h2 { font-size: 1.15rem !important; }
        .stButton > button { width: 100% !important; }
        .perfil-hero { padding: 20px; gap: 16px; }
        .perfil-hero .hero-avatar { width: 64px; height: 64px; font-size: 26px; }
        .perfil-hero .hero-nombre { font-size: 20px; }
        .perfil-bloque { padding: 16px; }
        .kpi-grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
        .kpi-card .kpi-num { font-size: 22px; }
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


# ─── LOGIN ────────────────────────────────────────────────────────────────
def vista_login():
    st.markdown("""
    <div style="text-align:center; margin-top:100px; margin-bottom:40px;">
        <h1 style="font-size:42px; margin-bottom:0; border:none; letter-spacing:-1px;">asisyarina</h1>
        <p style="opacity:0.6; margin-top:6px;">Sistema de Asistencia</p>
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
            if len(nueva) < 6: st.error("Minimo 6 caracteres.")
            elif nueva != confirmar: st.error("No coinciden.")
            else:
                escribir("UPDATE usuarios SET password=?,debe_cambiar_password=0 WHERE id=?",
                          (hashear_password(nueva), usuario["id"]))
                st.session_state["user"]["debe_cambiar_password"] = 0
                auditar(usuario["usuario"], "Cambio pwd obligatorio")
                st.rerun()


# ─── MI CUENTA ────────────────────────────────────────────────────────────
def vista_mi_cuenta():
    st.title("Mi cuenta")
    usuario = st.session_state["user"]
    inicial = (usuario["nombres"] or "?")[0].upper()
    st.markdown("""
    <div class="perfil-bloque">
        <div class="perfil-hero" style="margin-bottom:0;">
            <div class="hero-avatar">""" + inicial + """</div>
            <div class="hero-info">
                <div class="hero-nombre">""" + usuario['nombres'] + """</div>
                <div class="hero-meta">Usuario: """ + usuario['usuario'] + """ &nbsp;|&nbsp; Rol: """ + usuario['rol'] + """</div>
                <div class="hero-meta">Ultimo login: """ + (usuario.get('ultimo_login') or 'Nunca') + """</div>
            </div>
        </div>
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
        if df_secs.empty: st.warning("No tienes secciones asignadas.")
        else: st.dataframe(df_secs, width='stretch')

    st.markdown("---")
    st.subheader("Mi actividad reciente")
    con = obtener_conexion()
    df_act = pd.read_sql("SELECT accion,fecha FROM auditoria WHERE usuario=? ORDER BY id DESC LIMIT 10", con, params=[usuario["usuario"]])
    if df_act.empty: st.info("Sin actividad.")
    else: st.dataframe(df_act, width='stretch')

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
        cerrar_sesion(); st.rerun()


# ─── PUERTA ───────────────────────────────────────────────────────────────
def _puerta_puede_operar(fecha):
    con = obtener_conexion()
    esp = con.execute("SELECT descripcion,hora_entrada,tipo FROM dias_especiales WHERE fecha=? AND activo=1 LIMIT 1", (fecha,)).fetchone()
    if esp and esp["tipo"] == "feriado":
        return False, "Feriado / sin clases: " + (esp["descripcion"] or "") + ".", esp
    if es_fin_de_semana() and not (esp and esp["tipo"] == "evento"):
        return False, "Hoy no es dia laboral.", esp
    return True, "", esp

def _puerta_escanear(usuario, fecha):
    ok, msg, esp = _puerta_puede_operar(fecha)
    if not ok:
        if "Feriado" in msg: st.info(msg)
        else: st.warning(msg)
        return
    if esp and esp["tipo"] == "evento":
        st.success("Evento escolar: " + (esp["descripcion"] or "") + " (entrada " + (esp["hora_entrada"] or "-") + ")")
    escaner_qr_continuo(key="puerta_qr")

def _puerta_manual(usuario, fecha):
    ok, msg, esp = _puerta_puede_operar(fecha)
    if not ok:
        if "Feriado" in msg: st.info(msg)
        else: st.warning(msg)
        return
    if esp and esp["tipo"] == "evento":
        st.success("Evento escolar: " + (esp["descripcion"] or "") + " (entrada " + (esp["hora_entrada"] or "-") + ")")
    ha = hora_corta()
    turnos_activos = []
    for t in listar_turnos():
        for v in listar_ventanas(t["id"]):
            ap = v["hora_apertura"]
            if esp and v["tipo"] == VENT_CLASES and esp["tipo"] == "evento":
                ap = esp["hora_entrada"] or ap
            if ap <= ha <= v["hora_cierre"]:
                turnos_activos.append({"turno_id": t["id"], "turno": t["nombre"], "ventana": v["nombre"]})
                break
    if not turnos_activos:
        st.info("No hay ventanas activas en este momento.")
        return
    st.caption("Ventanas activas: " + " | ".join([t["turno"] + " (" + t["ventana"] + ")" for t in turnos_activos]))
    st.warning("El modo manual es solo para emergencias.")
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
    gm = []
    for g in grados:
        secs = secciones_por_grado(g["id"])
        secs_validas = [s for s in secs if s["turno_id"] in turnos_ids]
        if secs_validas:
            gm.append({"grado": g, "n_secciones": len(secs_validas)})
    if not gm:
        st.info("No hay grados con ventana activa.")
        return
    st.markdown("### Selecciona el grado")
    cols = st.columns(3)
    for i, item in enumerate(gm):
        with cols[i % 3]:
            if st.button(item["grado"]["nombre"] + "  (" + str(item["n_secciones"]) + " secciones)",
                         width='stretch', key="pm_g_" + str(item["grado"]["id"])):
                st.session_state["puerta_manual_grado"] = item["grado"]["id"]
                st.rerun()

def _puerta_manual_secciones(idg, turnos_activos):
    if st.button("Regresar a grados", key="pm_volver_g"):
        st.session_state.pop("puerta_manual_grado", None); st.rerun()
    grados = listar_grados()
    g = next((x for x in grados if x["id"] == idg), None)
    if not g:
        st.session_state.pop("puerta_manual_grado", None); st.rerun(); return
    st.markdown("### Secciones de " + g["nombre"])
    turnos_ids = [t["turno_id"] for t in turnos_activos]
    secs = [s for s in secciones_por_grado(idg) if s["turno_id"] in turnos_ids]
    if not secs:
        st.warning("Este grado no tiene secciones con ventana activa."); return
    cols = st.columns(3)
    for i, s in enumerate(secs):
        with cols[i % 3]:
            n = len(alumnos_de_seccion(s["id"]))
            if st.button(s["nombre"] + "  (" + str(n) + " alumnos)", width='stretch', key="pm_s_" + str(s["id"])):
                st.session_state["puerta_manual_seccion"] = s["id"]; st.rerun()

def _puerta_manual_alumnos(idsec, usuario):
    if st.button("Regresar a secciones", key="pm_volver_s"):
        st.session_state.pop("puerta_manual_seccion", None); st.rerun()
    con = obtener_conexion()
    sec = con.execute("""SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE s.id=?""", (idsec,)).fetchone()
    if not sec:
        st.session_state.pop("puerta_manual_seccion", None); st.rerun(); return
    st.markdown("### " + sec['grado'] + " " + sec['seccion'] + " - Turno " + sec['turno'])
    df = alumnos_de_seccion(idsec)
    if df.empty: st.info("Sin alumnos."); return
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
    <div class="puerta-header">
        <div class="titulo">Control de Puerta</div>
        <div class="fecha">""" + fecha + """</div>
    </div>
    """, unsafe_allow_html=True)
    if "puerta_modo" not in st.session_state:
        st.session_state["puerta_modo"] = "Escanear QR"
    modo = st.radio("Modo", ["Escanear QR", "Manual"], key="puerta_modo",
                    horizontal=True, label_visibility="collapsed")
    if modo == "Escanear QR":
        _puerta_escanear(usuario, fecha)
    else:
        _puerta_manual(usuario, fecha)


# ─── BLOQUEADOS ───────────────────────────────────────────────────────────
def vista_bloqueados():
    st.title("Alumnos bloqueados")
    st.caption("Cuando un alumno llega a la 4ta tardanza, se bloquea automaticamente.")
    usuario = st.session_state["user"]
    df = listar_bloqueados()
    if df.empty:
        st.success("No hay alumnos bloqueados.")
        return
    st.dataframe(df, width='stretch')
    ops = {r['alumno'] + " (" + r['dni'] + ") - " + r['motivo']: r["alumno_id"] for _, r in df.iterrows()}
    sel = st.selectbox("Alumno a desbloquear", list(ops.keys()), key="desbloq_sel")
    idal = ops[sel]
    with st.expander("Historial de bloqueos de este alumno"):
        st.dataframe(historial_bloqueos_alumno(idal), width='stretch', hide_index=True)
    motivo = st.text_input("Motivo de desbloqueo", key="desbloq_motivo")
    st.warning("Al desbloquear, el contador de tardanzas se reinicia.")
    if _pedir_password_critica("desbloq_" + str(idal), "Desbloquear"):
        liberar_bloqueo(idal, usuario, motivo)
        st.toast("Alumno desbloqueado. Contador reiniciado.")
        st.rerun()


# ─── PANEL DIRECCION ──────────────────────────────────────────────────────
def vista_panel_direccion():
    try: st_autorefresh(interval=10000, key="panel_dir_refresh")
    except Exception: pass
    st.title("Panel Direccion")
    fecha = hoy_str()
    if st.button("Actualizar", key="refresh_panel"):
        marcar_faltas_al_cierre(); st.rerun()

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
    if df.empty:
        st.info("Sin escaneos hoy.")
    else:
        st.dataframe(df, width='stretch')

# ─── REPORTES ─────────────────────────────────────────────────────────────
def _generar_hojas_diario(fecha, ids_secs):
    con = obtener_conexion(); hojas = {}
    for idsec in ids_secs:
        sec = con.execute("SELECT s.id,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno FROM secciones s "
                          "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE s.id=?", (idsec,)).fetchone()
        if not sec: continue
        df_clases = pd.read_sql(
            "SELECT a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS Apellidos, "
            "a.nombres AS Nombres, "
            "CASE "
            "  WHEN ast.estado IS NULL THEN '-' "
            "  WHEN ast.estado='Puntual' THEN 'P' "
            "  WHEN ast.estado='Tardanza' THEN 'T' "
            "  WHEN ast.estado='Falta' AND ast.justificada=1 THEN 'J' "
            "  WHEN ast.estado='Falta' THEN 'F' "
            "  WHEN ast.estado='Permiso' THEN 'PERMISO' "
            "  ELSE ast.estado END AS Estado, "
            "COALESCE(d.descripcion, '') AS Evento "
            "FROM alumnos a "
            "LEFT JOIN asistencias ast ON ast.alumno_id=a.id AND ast.fecha=? AND ast.tipo IN ('clases','evento') "
            "LEFT JOIN dias_especiales d ON ast.dia_especial_id = d.id "
            "WHERE a.seccion_id=? AND a.activo=1 "
            "ORDER BY a.apellido_paterno,a.apellido_materno",
            con, params=[fecha, idsec])
        df_faltaron = pd.read_sql(
            "SELECT a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS Apellidos, "
            "a.nombres AS Nombres, 'Falto' AS Estado "
            "FROM alumnos a JOIN asistencias ast ON ast.alumno_id=a.id "
            "WHERE a.seccion_id=? AND a.activo=1 AND ast.fecha=? AND ast.tipo='clases' AND ast.estado='Falta' "
            "ORDER BY a.apellido_paterno",
            con, params=[idsec, fecha])
        df_ref = pd.read_sql(
            "SELECT a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS Apellidos, "
            "a.nombres AS Nombres, 'Vino' AS Estado, COALESCE(ast.hora,'') AS Hora "
            "FROM alumnos a JOIN asistencias ast ON ast.alumno_id=a.id "
            "WHERE a.seccion_id=? AND a.activo=1 AND ast.fecha=? AND ast.tipo='reforzamiento' AND ast.estado='Asistio' "
            "ORDER BY a.apellido_paterno",
            con, params=[idsec, fecha])
        nombre_hoja = (sec["grado"] + " " + sec["seccion"] + " - " + sec["turno"])
        bloques = [
            ("ASISTENCIA DE CLASES   (P=Puntual | T=Tardanza | J=Falta justificada | F=Falta | PERMISO=Permiso | -=Sin registro)", df_clases),
            ("ALUMNOS QUE FALTARON A CLASES", df_faltaron),
            ("ALUMNOS QUE VINIERON A REFORZAMIENTO", df_ref),
        ]
        hojas[nombre_hoja] = bloques
    return hojas


def _generar_hojas_mensual(mes, anio, ids_secs):
    con = obtener_conexion(); hojas = {}
    ult = monthrange(anio, mes)[1]
    ini = date(anio, mes, 1); fin = date(anio, mes, ult)
    for idsec in ids_secs:
        sec = con.execute("SELECT s.id,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno FROM secciones s "
                          "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE s.id=?", (idsec,)).fetchone()
        if not sec: continue
        dias = []
        for d in range(1, ult+1):
            f = date(anio, mes, d)
            if f.weekday() >= 5:
                esp = con.execute("SELECT id FROM dias_especiales WHERE fecha=? AND activo=1 AND tipo='evento'", (f.strftime("%Y-%m-%d"),)).fetchone()
                if not esp: continue
            dias.append(f)
        df_al = pd.read_sql("SELECT a.id AS alumno_id,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres "
                            "FROM alumnos a WHERE a.seccion_id=? AND a.activo=1 ORDER BY a.apellido_paterno", con, params=[idsec])
        if df_al.empty: continue
        asis = {}
        for r in con.execute("SELECT alumno_id,fecha,estado,justificada FROM asistencias WHERE fecha BETWEEN ? AND ? AND tipo='clases' AND alumno_id IN (SELECT id FROM alumnos WHERE seccion_id=?)",
                              (ini.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d"), idsec)).fetchall():
            asis[(r["alumno_id"], r["fecha"])] = "F" if r["estado"] == "Falta" else "P"
        ref_map = {}
        for r in con.execute("SELECT alumno_id,fecha FROM asistencias WHERE fecha BETWEEN ? AND ? AND tipo='reforzamiento' AND estado='Asistio' AND alumno_id IN (SELECT id FROM alumnos WHERE seccion_id=?)",
                              (ini.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d"), idsec)).fetchall():
            ref_map.setdefault(r["alumno_id"], []).append(r["fecha"])
        filas = []
        for _, al in df_al.iterrows():
            fila = {"Apellidos": al["apellidos"], "Nombres": al["nombres"]}
            tp = tf = 0
            for d in dias:
                f_str = d.strftime("%Y-%m-%d")
                etq = ["Lun","Mar","Mie","Jue","Vie","Sab","Dom"][d.weekday()] + " " + str(d.day).zfill(2)
                est = asis.get((al["alumno_id"], f_str), "-")
                fila[etq] = est
                if est == "P": tp += 1
                elif est == "F": tf += 1
            fila["Total P"] = tp
            fila["Total F"] = tf
            filas.append(fila)
        df_cal = pd.DataFrame(filas)
        cols_base = ["Apellidos", "Nombres"]
        cols_dias = [c for c in df_cal.columns if c not in cols_base and not c.startswith("Total")]
        df_cal = df_cal[cols_base + cols_dias + ["Total P", "Total F"]]
        filas_f = []
        for _, al in df_al.iterrows():
            faltas = sum(1 for d in dias if asis.get((al["alumno_id"], d.strftime("%Y-%m-%d"))) == "F")
            if faltas > 0:
                filas_f.append({"Apellidos": al["apellidos"], "Nombres": al["nombres"], "Faltas en el mes": faltas})
        df_faltas = pd.DataFrame(filas_f) if filas_f else pd.DataFrame(columns=["Apellidos","Nombres","Faltas en el mes"])
        filas_r = []
        for _, al in df_al.iterrows():
            refs = ref_map.get(al["alumno_id"], [])
            if refs:
                filas_r.append({"Apellidos": al["apellidos"], "Nombres": al["nombres"], "Veces que vino": len(refs)})
        df_ref = pd.DataFrame(filas_r) if filas_r else pd.DataFrame(columns=["Apellidos","Nombres","Veces que vino"])
        nombre_hoja = (sec["grado"] + " " + sec["seccion"] + " - " + sec["turno"])
        bloques = [
            ("CALENDARIO DEL MES", df_cal),
            ("CONTEO DE FALTAS (solo los que faltaron)", df_faltas),
            ("REFORZAMIENTO (solo los que vinieron)", df_ref),
        ]
        hojas[nombre_hoja] = bloques
    return hojas


def _generar_hojas_eventos(desde, hasta, ids_secs):
    con = obtener_conexion(); hojas = {}
    for idsec in ids_secs:
        sec = con.execute("SELECT s.id,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno FROM secciones s "
                          "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE s.id=?", (idsec,)).fetchone()
        if not sec: continue
        df = pd.read_sql(
            "SELECT d.descripcion AS Evento, ast.fecha AS Fecha, ast.hora AS Hora, "
            "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS Apellidos, a.nombres AS Nombres, "
            "'Asistio' AS Estado "
            "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id "
            "LEFT JOIN dias_especiales d ON ast.dia_especial_id=d.id "
            "WHERE ast.tipo='evento' AND ast.fecha BETWEEN ? AND ? AND a.seccion_id=? AND ast.estado IN ('Puntual','Tardanza') "
            "ORDER BY ast.fecha DESC, a.apellido_paterno",
            con, params=[desde.strftime("%Y-%m-%d"), hasta.strftime("%Y-%m-%d"), idsec])
        nombre_hoja = (sec["grado"] + " " + sec["seccion"] + " - " + sec["turno"])
        bloques = [("ALUMNOS QUE ASISTIERON A EVENTOS", df)]
        hojas[nombre_hoja] = bloques
    return hojas


def _rep_seleccionar_seccion(usuario):
    con = obtener_conexion()
    rol = usuario["rol"]
    permitidas = None
    if rol == "Auxiliar":
        permitidas = [f["seccion_id"] for f in con.execute(
            "SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id=?",
            (usuario["id"],)).fetchall()]
        if not permitidas:
            st.info("No tienes secciones asignadas. Contacta al Admin.")
            return

    # ─── Si hay una seccion seleccionada, mostrar tipos ───
    if st.session_state.get("rep_idsec"):
        _rep_pantalla_tipos(st.session_state["rep_idsec"])
        return

    # ─── Si hay un grado seleccionado, mostrar secciones ───
    idg = st.session_state.get("rep_grado_sel")
    if idg:
        g = next((x for x in listar_grados() if x["id"] == idg), None)
        if not g:
            st.session_state.pop("rep_grado_sel", None); st.rerun(); return
        if st.button("← Regresar a grados", key="rep_volver_g"):
            st.session_state.pop("rep_grado_sel", None); st.rerun()
        st.subheader("Secciones de " + g["nombre"])
        secs = secciones_por_grado(g["id"])
        if permitidas is not None:
            secs = [s for s in secs if s["id"] in permitidas]
        if not secs:
            st.info("No hay secciones visibles para este grado."); return
        cols = st.columns(3)
        for i, s in enumerate(secs):
            with cols[i % 3]:
                n = len(alumnos_de_seccion(s["id"]))
                if st.button(s["nombre"] + "  (" + str(n) + " alumnos)", width='stretch',
                             key="rep_s_" + str(s["id"])):
                    st.session_state["rep_idsec"] = s["id"]; st.rerun()
        return

    # ─── Mostrar lista de grados ───
    st.markdown("### Elige el grado")

    # ─── BOTON PARA DESCARGAR TODOS LOS REPORTES DEL AUXILIAR ───
    if rol == "Auxiliar" and permitidas:
        st.markdown("---")
        st.markdown("**Descargar Reporte Diario de TODAS mis secciones**")
        st.caption("Genera un solo PDF con todas las secciones que tienes asignadas.")
        if st.button("Descargar Reporte Diario (todas mis secciones)", type="primary",
                     width='stretch', key="rep_dl_todas_diario"):
            hojas = _generar_hojas_diario(hoy_str(), permitidas)
            if not hojas:
                st.warning("Sin datos para hoy.")
            else:
                pdf = generar_pdf_multilhoja(hojas, "Reporte Diario - " + hoy_str())
                st.download_button("Guardar PDF", pdf,
                                   "Reporte_diario_todas_" + hoy_str() + ".pdf",
                                   "application/pdf", width='stretch', key="rep_dl_todas_diario_pdf")
        st.markdown("---")

    grados_mostrar = []
    for g in listar_grados():
        secs = secciones_por_grado(g["id"])
        if permitidas is not None:
            secs = [s for s in secs if s["id"] in permitidas]
        if secs:
            grados_mostrar.append({"grado": g, "n": len(secs)})
    if not grados_mostrar:
        st.info("No hay secciones disponibles."); return
    cols = st.columns(3)
    for i, item in enumerate(grados_mostrar):
        with cols[i % 3]:
            if st.button(item["grado"]["nombre"] + "  (" + str(item["n"]) + " secciones)",
                         width='stretch', key="rep_g_" + str(item["grado"]["id"])):
                st.session_state["rep_grado_sel"] = item["grado"]["id"]; st.rerun()
def _rep_pantalla_tipos(idsec):
    con = obtener_conexion()
    sec = con.execute(
        "SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno "
        "FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
        "WHERE s.id=?", (idsec,)).fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        st.session_state.pop("rep_idsec", None); st.rerun(); return

    # ─── BOTON REGRESAR A SECCIONES ───
    if st.button("← Regresar a secciones", key="rep_volver_secciones"):
        st.session_state.pop("rep_idsec", None)
        st.session_state.pop("rep_tipo", None)
        st.rerun()

    st.subheader(sec['grado'] + " " + sec['seccion'] + " - Turno " + sec['turno'])

    # ─── Verificar si HOY ya es fin de mes o despues ───
    hoy = ahora().date()
    ult_dia_mes = date(hoy.year, hoy.month, monthrange(hoy.year, hoy.month)[1])
    ult_lab = ult_dia_mes
    while ult_lab.weekday() >= 5:
        ult_lab = ult_lab - timedelta(days=1)
    cierre_activo = hoy >= ult_lab

    st.markdown("### Elige el tipo de reporte")
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Reporte Diario", width='stretch', key="rep_tipo_btn_diario"):
            st.session_state["rep_tipo"] = "diario"
            st.session_state["rep_idsec_val"] = idsec
            st.rerun()
    with c2:
        if cierre_activo:
            if st.button("Cierre Mensual", width='stretch', key="rep_tipo_btn_mensual"):
                st.session_state["rep_tipo"] = "mensual"
                st.session_state["rep_idsec_val"] = idsec
                st.rerun()
        else:
            st.button("Cierre Mensual", width='stretch', disabled=True,
                      key="rep_tipo_btn_mensual_disabled",
                      help="Se activa a partir del " + str(ult_lab) + ".")
    with c3:
        if st.button("Reporte General", width='stretch', key="rep_tipo_btn_general"):
            st.session_state["rep_tipo"] = "general"
            st.session_state["rep_idsec_val"] = idsec
            st.rerun()

    if not cierre_activo:
        st.caption("El Cierre Mensual se activara el **" + str(ult_lab) + "** (ultimo dia laborable del mes).")

    if st.session_state.get("rep_tipo") and st.session_state.get("rep_idsec_val") == idsec:
        if st.button("← Regresar a tipos", key="rep_volver_tipos"):
            st.session_state.pop("rep_tipo", None)
            st.rerun()
        st.markdown("---")
        tipo = st.session_state["rep_tipo"]
        if tipo == "diario":
            _rep_mostrar_reporte(idsec, "diario", ahora().date() - timedelta(days=30), ahora().date())
        elif tipo == "mensual":
            _rep_mostrar_reporte(idsec, "mensual", ahora().date() - timedelta(days=30), ahora().date())
        elif tipo == "general":
            _rep_general_por_turno_admin()

def _rep_mostrar_reporte(idsec, tipo, desde, hasta):
    con = obtener_conexion()
    sec = con.execute(
        "SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno "
        "FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
        "WHERE s.id=?", (idsec,)).fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        st.session_state.pop("rep_idsec_val", None)
        st.session_state.pop("rep_tipo", None)
        st.rerun()
        return

    # ─── BOTON REGRESAR A TIPOS ───
    if st.button("← Regresar a tipos de reporte", key="rep_volver_tipos_desde_mostrar"):
        st.session_state.pop("rep_tipo", None)
        st.session_state.pop("rep_idsec_val", None)
        st.rerun()

    # ─── REPORTE DIARIO ───
    if tipo == "diario":
        st.subheader("Reporte Diario - " + sec['grado'] + " " + sec['seccion'])
        st.caption("Fecha: " + str(ahora().date()))
        hojas = _generar_hojas_diario(hoy_str(), [idsec])
        if not hojas:
            st.info("Sin datos para hoy.")
            return
        pdf = generar_pdf_multilhoja(hojas, "Reporte Diario - " + str(ahora().date()))
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("Descargar PDF", pdf,
                               "Reporte_diario_" + sec['grado'] + "_" + sec['seccion'] + "_" + hoy_str() + ".pdf",
                               "application/pdf", key="rep_dl_diario_pdf")
        with c2:
            hojas_xlsx = {}
            for nombre, bloques in hojas.items():
                for titulo_bloque, df in bloques:
                    hojas_xlsx[(nombre + "_" + titulo_bloque)[:31]] = df
            if hojas_xlsx:
                st.download_button("Descargar Excel", df_a_xlsx_multilhoja(hojas_xlsx),
                                   "Reporte_diario_" + sec['grado'] + "_" + sec['seccion'] + "_" + hoy_str() + ".xlsx",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                   key="rep_dl_diario_xlsx")

    # ─── CIERRE MENSUAL ───
    elif tipo == "mensual":
        hoy = ahora().date()
        st.subheader("Cierre Mensual " + MESES_ES[hoy.month] + " " + str(hoy.year) +
                     " - " + sec['grado'] + " " + sec['seccion'])
        hojas = _generar_hojas_mensual(hoy.month, hoy.year, [idsec])
        if not hojas:
            st.info("Sin datos para este mes.")
            return
        pdf = generar_pdf_multilhoja(hojas, "Cierre Mensual " + MESES_ES[hoy.month] + " " + str(hoy.year))
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("Descargar PDF", pdf,
                               "Cierre_mensual_" + sec['grado'] + "_" + sec['seccion'] + "_" + MESES_ES[hoy.month] + ".pdf",
                               "application/pdf", key="rep_dl_mensual_pdf")
        with c2:
            hojas_xlsx = {}
            for nombre, bloques in hojas.items():
                for titulo_bloque, df in bloques:
                    hojas_xlsx[(nombre + "_" + titulo_bloque)[:31]] = df
            if hojas_xlsx:
                st.download_button("Descargar Excel", df_a_xlsx_multilhoja(hojas_xlsx),
                                   "Cierre_mensual_" + sec['grado'] + "_" + sec['seccion'] + ".xlsx",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                   key="rep_dl_mensual_xlsx")

    # ─── REPORTE GENERAL ───
    elif tipo == "general":
        _rep_general_por_turno_admin()


def _rep_general_por_turno_admin():
    st.subheader("Reporte general por auxiliar")
    st.caption("Agrupado por auxiliar. Solo se puede generar cuando la ventana de clases del turno ya cerro.")
    turnos = listar_turnos()
    if not turnos:
        st.info("Sin turnos configurados."); return
    ops = {t["nombre"]: t["id"] for t in turnos}
    sel_turno = st.selectbox("Turno", list(ops.keys()), key="repgen_turno")
    id_turno = ops[sel_turno]

    ha = hora_corta()
    ventana_clases = None
    for v in listar_ventanas(id_turno):
        if v["tipo"] == "clases":
            ventana_clases = v; break
    if not ventana_clases:
        st.warning("Este turno no tiene ventana de clases configurada."); return
    if ha < ventana_clases["hora_cierre"]:
        st.warning("**No se puede generar el reporte todavia.** La ventana de clases de " +
                   sel_turno + " aun esta abierta (cierra a las " + ventana_clases["hora_cierre"] + ").")
        return

    c1, c2 = st.columns(2)
    with c1: desde = st.date_input("Desde", ahora().date() - timedelta(days=30), key="repgen_desde")
    with c2: hasta = st.date_input("Hasta", ahora().date(), key="repgen_hasta")
    if desde > hasta:
        st.error("La fecha Desde no puede ser mayor que Hasta."); return

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

    con = obtener_conexion()
    q = """
    SELECT s.id AS seccion_id, g.nombre AS grado, s.nombre AS seccion,
    COALESCE(u.nombres, '(Sin auxiliar)') AS auxiliar,
    SUM(CASE WHEN ast.estado='Puntual' AND ast.tipo='clases' THEN 1 ELSE 0 END) AS puntuales,
    SUM(CASE WHEN ast.estado='Falta' AND ast.tipo='clases' THEN 1 ELSE 0 END) AS faltas
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
    q += " GROUP BY s.id, g.nombre, s.nombre, auxiliar ORDER BY auxiliar, g.nombre, s.nombre"
    df = pd.read_sql(q, con, params=p)

    if df.empty:
        st.info("Sin datos en ese rango."); return
    df["total"] = df["puntuales"] + df["faltas"]

    st.markdown("---")
    totales = {"puntuales": 0, "faltas": 0, "total": 0}
    html = ['<table style="width:100%; border-collapse: collapse; font-size: 13px;">']
    html.append('<tr><th colspan="5" style="background:#FFFFFF; color:black; padding:10px; '
                'text-align:left; font-size:14px; border-bottom:2px solid black;">TURNO ' +
                sel_turno.upper() + '</th></tr>')
    html.append('<tr style="background:#FFFFFF;">'
                '<th style="padding:6px; border:1px solid #666; text-align:left;">Auxiliar</th>'
                '<th style="padding:6px; border:1px solid #666; text-align:left;">Seccion</th>'
                '<th style="padding:6px; border:1px solid #666; text-align:center;">Puntuales</th>'
                '<th style="padding:6px; border:1px solid #666; text-align:center;">Faltas</th>'
                '<th style="padding:6px; border:1px solid #666; text-align:center;">Total</th>'
                '</tr>')

    for aux in df["auxiliar"].unique().tolist():
        df_aux = df[df["auxiliar"] == aux].reset_index(drop=True)
        n = len(df_aux)
        for i, row in df_aux.iterrows():
            html.append('<tr>')
            if i == 0:
                html.append('<td rowspan="' + str(n) + '" style="padding:8px; border:1px solid #666; '
                            'font-weight:700; background:#FFFFFF; vertical-align:top;">' + str(aux) + '</td>')
            html.append('<td style="padding:6px; border:1px solid #666;">' +
                        str(row['grado']) + " " + str(row['seccion']) + '</td>'
                        '<td style="padding:6px; border:1px solid #666; text-align:center;">' + str(int(row['puntuales'])) + '</td>'
                        '<td style="padding:6px; border:1px solid #666; text-align:center;">' + str(int(row['faltas'])) + '</td>'
                        '<td style="padding:6px; border:1px solid #666; text-align:center; font-weight:600;">' + str(int(row['total'])) + '</td>'
                        '</tr>')
        sub_p = int(df_aux["puntuales"].sum()); sub_f = int(df_aux["faltas"].sum()); sub_t = int(df_aux["total"].sum())
        totales["puntuales"] += sub_p; totales["faltas"] += sub_f; totales["total"] += sub_t
        html.append('<tr style="background:#F5F5F5; font-style:italic;">'
                    '<td colspan="2" style="padding:6px; border:1px solid #666; text-align:right;">Subtotal ' + str(aux) + '</td>'
                    '<td style="padding:6px; border:1px solid #666; text-align:center;">' + str(sub_p) + '</td>'
                    '<td style="padding:6px; border:1px solid #666; text-align:center;">' + str(sub_f) + '</td>'
                    '<td style="padding:6px; border:1px solid #666; text-align:center;">' + str(sub_t) + '</td>'
                    '</tr>')

    html.append('<tr style="background:#E8E8E8; font-weight:700;">'
                '<td colspan="2" style="padding:8px; border:1px solid #666; text-align:right;">TOTAL ' + sel_turno.upper() + '</td>'
                '<td style="padding:8px; border:1px solid #666; text-align:center;">' + str(totales["puntuales"]) + '</td>'
                '<td style="padding:8px; border:1px solid #666; text-align:center;">' + str(totales["faltas"]) + '</td>'
                '<td style="padding:8px; border:1px solid #666; text-align:center;">' + str(totales["total"]) + '</td>'
                '</tr>')
    html.append('</table>')
    st.markdown("".join(html), unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### Descargar")
    df_export = df.rename(columns={"auxiliar": "Auxiliar", "grado": "Grado", "seccion": "Seccion",
                                    "puntuales": "Puntuales", "faltas": "Faltas", "total": "Total"})
    df_export = df_export[["Auxiliar", "Grado", "Seccion", "Puntuales", "Faltas", "Total"]]
    c1, c2 = st.columns(2)
    with c1:
        st.download_button("Excel", df_a_xlsx(df_export, "General por auxiliar"),
                           "Reporte_general_" + sel_turno + "_" + str(desde) + "_" + str(hasta) + ".xlsx",
                           width='stretch', key="repgen_dl_x")
    with c2:
        st.download_button("PDF",
                           generar_pdf_tabla_ancha(df_export, "Reporte general " + sel_turno,
                                                    str(desde) + " a " + str(hasta)),
                           "Reporte_general_" + sel_turno + "_" + str(desde) + "_" + str(hasta) + ".pdf",
                           "application/pdf", width='stretch', key="repgen_dl_p")


def vista_reportes():
    st.title("Reportes y Consultas")
    usuario = st.session_state["user"]; rol = usuario["rol"]

    # ─── LIMPIAR estado viejo al entrar a Reportes ───
    if not st.session_state.get("_rep_iniciado"):
        for k in ["rep_grado_sel", "rep_idsec", "rep_tipo", "rep_idsec_val",
                  "rep_desde_val", "rep_hasta_val", "rep_modo_admin"]:
            st.session_state.pop(k, None)
        st.session_state["_rep_iniciado"] = True

    # ─── MODO ADMIN: elegir entre reportes por seccion o general ───
    if rol == "Admin":
        modo = st.radio(
            "Modo",
            ["Por seccion", "Reporte general por auxiliar"],
            key="rep_modo_admin",
            horizontal=True,
            label_visibility="collapsed"
        )
        if modo == "Reporte general por auxiliar":
            if st.button("← Volver a reportes por seccion", key="rep_volver_modo"):
                st.session_state.pop("rep_modo_admin", None)
                st.session_state["_rep_iniciado"] = False
                st.rerun()
            _rep_general_por_turno_admin()
            return

    # ─── MODO POR SECCION ───
    if st.session_state.get("rep_tipo") and st.session_state.get("rep_idsec_val"):
        _rep_mostrar_reporte(
            st.session_state.get("rep_idsec_val"),
            st.session_state.get("rep_tipo"),
            ahora().date() - timedelta(days=30),
            ahora().date()
        )
        return

    if st.session_state.get("rep_idsec"):
        _rep_pantalla_tipos(st.session_state["rep_idsec"])
        return

    _rep_seleccionar_seccion(usuario)
# ─── ALUMNOS UI ───────────────────────────────────────────────────────────
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
        pwd = st.text_input("Contrasena de Admin o Direccion", type="password")
        if st.form_submit_button("Crear", type="primary"):
            if not dni or not nom or not pat or not s: st.error("Completa obligatorios.")
            elif not re.fullmatch(r"\d{8}", dni.strip()): st.error("DNI invalido.")
            elif not pwd: st.error("Ingresa la contrasena.")
            elif not verificar_password_critica(pwd): st.error("Contrasena incorrecta.")
            else:
                ok, msg = crear_alumno(dni.strip(), nom.strip(), pat.strip(), mat.strip(),
                                         s["id"], apo.strip(), tel.strip(), st.session_state["user"])
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
    datos = con.execute("SELECT a.*,g.nombre AS grado,s.nombre AS seccion FROM alumnos a "
                        "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id WHERE a.id=?", (idal,)).fetchone()
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
        pwd = st.text_input("Contrasena de Admin o Direccion", type="password")
        if st.form_submit_button("Guardar", type="primary"):
            if not pwd: st.error("Ingresa la contrasena.")
            elif not verificar_password_critica(pwd): st.error("Contrasena incorrecta.")
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
        c1.markdown("**" + al['nombre_completo'] + "** &nbsp; <span style='color:#E65100; font-weight:700;'>" +
                    al['grado'] + " " + al['seccion'] + "</span> <span style='color:#757575;'>(" + al['turno'] + ")</span>",
                    unsafe_allow_html=True)
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
    inicial = (al['apellido_paterno'] or al['nombres'] or "?")[0].upper()
    badge_txt = "BLOQUEADO" if d["bloqueado"] else "ACTIVO"
    st.markdown("""
    <div class="perfil-hero">
        <div class="hero-avatar">""" + inicial + """</div>
        <div class="hero-info">
            <div class="hero-nombre">""" + nombre + """</div>
            <div class="hero-meta">""" + al['grado'] + " " + al['seccion'] + """ | Turno """ + al['turno'] + """ | DNI """ + al['dni'] + """</div>
            <span class="hero-badge">""" + badge_txt + """</span>
        </div>
    </div>
    """, unsafe_allow_html=True)
    apo = al['nombre_apoderado'] or '-'
    tel = al['telefono_apoderado'] or '-'
    st.markdown("""
    <div class="perfil-bloque">
        <div class="titulo-seccion">Datos del Apoderado</div>
        <div class="info-linea"><span class="info-label">Apoderado:</span><span class="info-valor">""" + apo + """</span></div>
        <div class="info-linea"><span class="info-label">Telefono:</span><span class="info-valor">""" + tel + """</span></div>
    </div>
    """, unsafe_allow_html=True)
    tard_injust = d['tard_injust']
    tard_injust_class = "kpi-lbl-alerta" if tard_injust > 0 else "kpi-lbl"
    bloqueado_txt = "SI" if d['bloqueado'] else "NO"
    bloqueado_class = "kpi-lbl-alerta" if d['bloqueado'] else "kpi-lbl"
    st.markdown("""
    <div class="perfil-bloque">
        <div class="titulo-seccion">Estadisticas del Periodo</div>
        <div class="kpi-grid">
            <div class="kpi-card"><div class="kpi-num">""" + str(d['total_puntuales']) + """</div><div class="kpi-lbl">Puntuales</div></div>
            <div class="kpi-card"><div class="kpi-num">""" + str(d['total_tardanzas']) + """</div><div class="kpi-lbl">Tardanzas</div></div>
            <div class="kpi-card"><div class="kpi-num">""" + str(d['total_faltas']) + """</div><div class="kpi-lbl">Faltas</div></div>
            <div class="kpi-card"><div class="kpi-num">""" + str(d['total_ref_asistio']) + """</div><div class="kpi-lbl">Reforzamiento</div></div>
            <div class="kpi-card"><div class="kpi-num">""" + str(tard_injust) + """</div><div class=\"""" + tard_injust_class + """\">Tard. Injust.</div></div>
            <div class="kpi-card"><div class="kpi-num">""" + bloqueado_txt + """</div><div class=\"""" + bloqueado_class + """\">Bloqueado</div></div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.markdown('<div class="perfil-bloque"><div class="titulo-seccion">Acciones</div>', unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        pdf = pdf_carnet_alumno(al["dni"])
        if pdf:
            st.download_button("Carnet QR", pdf, "carnet_" + al['dni'] + ".pdf", "application/pdf", width='stretch', key="acc_carnet")
    with c2:
        st.download_button("Historial (Excel)", df_a_xlsx(d["asistencias"], "Historial"),
                           "historial_" + al['dni'] + ".xlsx", width='stretch', key="acc_hist")
    with c3:
        pdf_res = pdf_resumen_alumno(al["id"])
        if pdf_res:
            st.download_button("Resumen (PDF)", pdf_res, "resumen_" + al['dni'] + ".pdf",
                               "application/pdf", width='stretch', key="acc_res")
    with c4:
        if usuario["rol"] == "Admin":
            if al.get("activo", 1) == 1:
                if st.button("Desactivar", width='stretch', key="acc_desac"): st.session_state["_desac_al"] = True
            else:
                if st.button("Reactivar", width='stretch', key="acc_reac"): st.session_state["_reac_al"] = True
    st.markdown('</div>', unsafe_allow_html=True)
    if st.session_state.get("_desac_al"):
        with st.expander("Confirmar desactivacion", expanded=True):
            if _pedir_password_critica("desac_al", "Confirmar"):
                ok, msg = retirar_alumno(al["id"], al["dni"], usuario)
                st.session_state.pop("_desac_al", None); st.toast(msg); st.rerun()
    if st.session_state.get("_reac_al"):
        with st.expander("Confirmar reactivacion", expanded=True):
            if _pedir_password_critica("reac_al", "Confirmar"):
                ok, msg = reactivar_alumno(al["id"], al["dni"], usuario)
                st.session_state.pop("_reac_al", None); st.toast(msg); st.rerun()
    st.markdown('<div class="perfil-bloque"><div class="titulo-seccion">Codigo QR</div>', unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1,1,1])
    with col2: st.image(generar_qr(al["dni"]), width=220)
    st.markdown('</div>', unsafe_allow_html=True)
    tabs = st.tabs(["Asistencias", "Tardanzas", "Bloqueos", "Permisos", "Justificaciones previas"])
    with tabs[0]:
        if d["asistencias"].empty: st.info("Sin asistencias.")
        else:
            for _, ast in d["asistencias"].head(50).iterrows():
                c1, c2, c3, c4 = st.columns([2, 3, 2, 1])
                c1.write("**" + ast['fecha'] + "**")
                desc = ast['tipo'] + " - " + ast['estado']
                if ast['tipo'] == "evento" and ast.get("evento_nombre"):
                    desc += " - " + ast['evento_nombre']
                c2.write(desc)
                just_txt = "Justificada"
                if ast["justificada"] and ast.get("justificado_por"): just_txt += " por " + str(ast["justificado_por"])
                c3.write(just_txt if ast["justificada"] else "Sin justificar")
                if ast["justificada"]:
                    if c4.button("Quitar", key="quitar_" + str(ast['id'])):
                        ok, msg = quitar_justificacion(ast["id"], usuario)
                        if ok: st.toast(msg); st.rerun()
                        else: st.error(msg)
                else:
                    if ast["estado"] == "Falta":
                        puede, _m = _puede_justificar(ast["fecha"], tipo_asistencia=FALTA, ventana_id=ast.get("ventana_id"))
                        if puede:
                            if c4.button("Justificar", key="just_" + str(ast['id'])):
                                st.session_state["justif_id"] = ast["id"]; st.rerun()
                        else: c4.caption("Cerrada")
            if st.session_state.get("justif_id"):
                jid = st.session_state["justif_id"]
                st.markdown("---"); st.markdown("Justificar falta")
                obs = st.text_input("Observacion (obligatoria)", key="justif_obs")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Confirmar", type="primary"):
                        if not obs.strip(): st.error("La observacion es obligatoria.")
                        else:
                            ok, msg = justificar_asistencia(jid, obs, usuario)
                            if ok: st.session_state.pop("justif_id", None); st.toast(msg); st.rerun()
                            else: st.error(msg)
                with c2:
                    if st.button("Cancelar"): st.session_state.pop("justif_id", None); st.rerun()
    with tabs[1]:
        if d["tardanzas"].empty: st.info("Sin tardanzas.")
        else: st.dataframe(d["tardanzas"], width='stretch')
    with tabs[2]:
        if d["bloqueos"].empty: st.info("Sin bloqueos.")
        else: st.dataframe(d["bloqueos"], width='stretch')
    with tabs[3]:
        if d["permisos"].empty: st.info("Sin permisos.")
        else: st.dataframe(d["permisos"], width='stretch')
    with tabs[4]:
        if d["justificaciones_previas"].empty: st.info("Sin justificaciones previas.")
        else:
            dj = d["justificaciones_previas"].copy()
            dj["aplicada"] = dj["aplicada"].apply(lambda x: "Si" if x else "Pendiente")
            dj = dj.rename(columns={"fecha_objetivo": "Fecha objetivo", "tipo": "Tipo", "motivo": "Motivo",
                                     "aplicada": "Aplicada", "creado_por": "Creado por", "timestamp": "Registrado"})
            st.dataframe(dj, width='stretch', hide_index=True)


def vista_alumnos():
    st.title("Alumnos")
    pid = st.session_state.get("perfil_alumno_id")
    if pid: _perfil_alumno(pid); return
    tabs = st.tabs(["Listar", "Crear", "Editar"])
    with tabs[0]: _frag_listar_alumnos()
    with tabs[1]: _frag_crear_alumno()
    with tabs[2]: _frag_editar_alumno()


# ─── GRADOS Y SECCIONES ───────────────────────────────────────────────────
def vista_grados_secciones():
    st.title("Grados y Secciones")
    st.caption("Las secciones se crean automaticamente al importar el Excel de alumnos.")
    con = obtener_conexion()
    grados = listar_grados()
    st.subheader("Grados")
    if grados: st.dataframe(pd.DataFrame(grados), width='stretch')
    else: st.info("Sin grados.")
    st.subheader("Secciones")
    df = pd.read_sql("SELECT s.id,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno,"
                     "(SELECT COUNT(*) FROM alumnos a WHERE a.seccion_id=s.id AND a.activo=1) AS alumnos_activos "
                     "FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
                     "ORDER BY t.nombre,g.nombre,s.nombre", con)
    if df.empty: st.info("Sin secciones.")
    else: st.dataframe(df, width='stretch')


# ─── CARNETS ──────────────────────────────────────────────────────────────
def vista_carnets():
    st.title("Carnets QR")
    if st.session_state.get("carn_ver_seccion"):
        _carnets_ver_seccion(st.session_state["carn_ver_seccion"]); return
    grados = listar_grados()
    if not grados: st.warning("No hay grados."); return
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
    if not g: st.session_state.pop("carn_grado_sel", None); return
    st.markdown("---"); st.subheader("Secciones de " + g['nombre'])
    secs = secciones_por_grado(g["id"])
    if not secs: st.info("Sin secciones."); return
    cols = st.columns(3)
    for i, s in enumerate(secs):
        n = len(alumnos_de_seccion(s["id"]))
        with cols[i % 3]:
            if st.button(s['nombre'] + "  (" + str(n) + " alumnos)", width='stretch', key="carn_s_" + str(s['id'])):
                st.session_state["carn_ver_seccion"] = s["id"]; st.rerun()


def _carnets_ver_seccion(idsec):
    con = obtener_conexion()
    sec = con.execute("SELECT s.id,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno FROM secciones s "
                      "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE s.id=?", (idsec,)).fetchone()
    if not sec:
        st.warning("Seccion no encontrada."); st.session_state.pop("carn_ver_seccion", None); return
    if st.button("Regresar a grados", key="carn_volver"):
        st.session_state.pop("carn_ver_seccion", None); st.session_state.pop("carn_sel_alumnos", None); st.rerun()
    st.subheader(sec['grado'] + " " + sec['seccion'] + " - Turno " + sec['turno'])
    df = alumnos_de_seccion(idsec)
    if df.empty: st.info("Sin alumnos activos."); return
    st.caption(str(len(df)) + " alumnos. Aprieta un nombre para seleccionarlo.")
    if "carn_sel_alumnos" not in st.session_state: st.session_state["carn_sel_alumnos"] = set()
    sel = st.session_state["carn_sel_alumnos"]
    cols = st.columns(4)
    for i, (_, al) in enumerate(df.iterrows()):
        with cols[i % 4]:
            if al["id"] in sel:
                st.markdown('<div class="btn-sel">', unsafe_allow_html=True)
                if st.button(al['nombre_completo'], key="carn_al_" + str(al['id']), width='stretch'):
                    sel.discard(al["id"]); st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
            else:
                if st.button(al['nombre_completo'], key="carn_al_" + str(al['id']), width='stretch'):
                    sel.add(al["id"]); st.rerun()
    st.markdown("---")
    st.write("Seleccionados: " + str(len(sel)))
    c1, c2, c3 = st.columns(3)
    with c1:
        if sel:
            if st.button("Descargar seleccionados", type="primary", width='stretch', key="carn_dl_sel"):
                pdf = pdf_carnets_seleccionados(list(sel), titulo="Carnets - " + sec['grado'] + " " + sec['seccion'])
                if pdf:
                    st.download_button("Guardar PDF", pdf, "carnets_sel_" + sec['grado'] + sec['seccion'] + ".pdf",
                                       "application/pdf", width='stretch')
        else: st.info("Marca al menos un alumno.")
    with c2:
        if st.button("Descargar todo el salon", width='stretch', key="carn_dl_todo"):
            pdf = pdf_carnets_por_seccion(idsec)
            if pdf:
                st.download_button("Guardar PDF", pdf, "carnets_" + sec['grado'] + sec['seccion'] + ".pdf",
                                   "application/pdf", width='stretch')
    with c3:
        if st.button("Limpiar seleccion", width='stretch', key="carn_limpiar"):
            st.session_state["carn_sel_alumnos"] = set(); st.rerun()


# ─── VENTANAS ─────────────────────────────────────────────────────────────
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
                    pwd = st.text_input("Contrasena de Admin o Direccion", type="password", key="pwd_vent_" + str(v['id']))
                    if st.form_submit_button("Guardar", type="primary"):
                        if not pwd: st.error("Ingresa la contrasena.")
                        elif not verificar_password_critica(pwd): st.error("Contrasena incorrecta.")
                        else:
                            ap = ap_t.strftime("%H:%M"); lim = lim_t.strftime("%H:%M"); ci = ci_t.strftime("%H:%M")
                            escribir("UPDATE ventanas SET hora_apertura=?,hora_limite_puntual=?,hora_cierre=? WHERE id=?",
                                     (ap, lim, ci, v["id"]))
                            auditar(st.session_state["user"]["usuario"], "Edito ventana id=" + str(v['id']))
                            listar_ventanas.clear()
                            st.toast("Ventana actualizada"); st.rerun()


# ─── USUARIOS ─────────────────────────────────────────────────────────────
def puede_gestionar_usuario(usuario_actual, id_objetivo):
    con = obtener_conexion()
    obj = con.execute("SELECT id,rol,es_principal,usuario FROM usuarios WHERE id=?", (id_objetivo,)).fetchone()
    if not obj: return False, "Usuario no encontrado."
    if id_objetivo == usuario_actual["id"]: return False, "Para cambiar tus datos usa Mi cuenta."
    soy_principal = (usuario_actual.get("es_principal") or 0) == 1
    if soy_principal: return True, ""
    if obj["rol"] == "Admin": return False, "No tienes permisos para gestionar a un Admin."
    return True, ""


def vista_usuarios():
    st.title("Usuarios")
    usuario = st.session_state["user"]
    soy_principal = (usuario.get("es_principal") or 0) == 1
    con = obtener_conexion()
    tabs = st.tabs(["Listar", "Crear", "Editar", "Asignar secciones", "Mantenimiento"])

    with tabs[0]:
        if soy_principal:
            df = pd.read_sql("SELECT id,usuario,rol,nombres,activo,ultimo_login,es_principal FROM usuarios ORDER BY es_principal DESC, usuario", con)
        else:
            df = pd.read_sql("SELECT id,usuario,rol,nombres,activo,ultimo_login,es_principal FROM usuarios WHERE id=? OR rol!='Admin' ORDER BY usuario", con, params=[usuario["id"]])
        if df.empty: st.info("Sin usuarios.")
        else:
            for _, u in df.iterrows():
                etiqueta_rol = "Admin (principal)" if (u["rol"]=="Admin" and u["es_principal"]) else ("Sub Admin" if u["rol"]=="Admin" else u["rol"])
                st.write("**" + u['nombres'] + "** (" + u['usuario'] + ") - " + etiqueta_rol + " - " + ("Activo" if u["activo"] else "Inactivo"))

    with tabs[1]:
        st.subheader("Crear usuario")
        aviso = st.session_state.pop("_aviso_crear_usuario", None)
        if aviso:
            if aviso.get("tipo") == "ok": st.success(aviso.get("msg", ""))
            elif aviso.get("tipo") == "error": st.error(aviso.get("msg", ""))
        c1, c2 = st.columns(2)
        with c1:
            u = st.text_input("Usuario", key="crear_u_usuario")
            p = st.text_input("Contrasena", type="password", key="crear_u_pass")
        with c2:
            n = st.text_input("Nombres", key="crear_u_nombres")
            roles_disp = ["Admin", "Direccion", "Auxiliar"] if soy_principal else ["Direccion", "Auxiliar"]
            r = st.selectbox("Rol", roles_disp, key="crear_u_rol")
        idt = None
        if r == "Auxiliar":
            t_lbl = st.selectbox("Turno", [x["nombre"] for x in listar_turnos()], key="crear_u_turno")
            idt = next((x["id"] for x in listar_turnos() if x["nombre"] == t_lbl), None)
        pwd_crit = st.text_input("Contrasena de Admin o Direccion", type="password", key="crear_u_pwd_crit")
        if st.button("Crear usuario", type="primary", key="crear_u_btn"):
            if not u or not p or not n: st.error("Completa usuario, contrasena y nombres.")
            elif len(p) < 6: st.error("Minimo 6 caracteres.")
            elif r == "Auxiliar" and not idt: st.error("Selecciona un turno.")
            elif not pwd_crit: st.error("Ingresa la contrasena critica.")
            elif not verificar_password_critica(pwd_crit): st.error("Contrasena incorrecta.")
            else:
                u_norm = u.strip().lower()
                if con.execute("SELECT 1 FROM usuarios WHERE LOWER(usuario)=?", (u_norm,)).fetchone():
                    st.error("El usuario ya existe.")
                else:
                    try:
                        escribir("INSERT INTO usuarios(usuario,password,rol,nombres,turno_asignado,es_principal) VALUES(?,?,?,?,?,0)",
                                 (u_norm, hashear_password(p), r, n, idt))
                        auditar(usuario["usuario"], "Creo usuario " + u_norm)
                        st.session_state["_aviso_crear_usuario"] = {"tipo": "ok", "msg": "Usuario creado."}
                        for k in ["crear_u_usuario","crear_u_pass","crear_u_nombres","crear_u_rol","crear_u_turno","crear_u_pwd_crit"]:
                            st.session_state.pop(k, None)
                        st.rerun()
                    except sqlite3.IntegrityError: st.error("Ese usuario ya existe.")

    with tabs[2]:
        if soy_principal:
            df = pd.read_sql("SELECT id,usuario,rol,nombres,es_principal FROM usuarios WHERE usuario!='admin'", con)
        else:
            df = pd.read_sql("SELECT id,usuario,rol,nombres,es_principal FROM usuarios WHERE rol!='Admin'", con)
        if df.empty: st.info("Sin usuarios editables.")
        else:
            ops = {}
            for _, r in df.iterrows():
                et = r['usuario'] + " (" + r['rol'] + ")"
                if r["rol"] == "Admin" and r.get("es_principal"): et += " principal"
                ops[et] = r["id"]
            sel = st.selectbox("Usuario", list(ops.keys()), key="edit_u_sel"); idu = ops[sel]
            ok, msg = puede_gestionar_usuario(usuario, idu)
            if not ok: st.warning(msg)
            else:
                datos = con.execute("SELECT * FROM usuarios WHERE id=?", (idu,)).fetchone()
                with st.form("edit_u"):
                    u = st.text_input("Usuario", value=datos["usuario"])
                    n = st.text_input("Nombres", value=datos["nombres"])
                    p = st.text_input("Nueva contrasena (opcional)", type="password")
                    roles_edit = ["Admin","Direccion","Auxiliar"] if soy_principal else ["Direccion","Auxiliar"]
                    idx_rol = roles_edit.index(datos["rol"]) if datos["rol"] in roles_edit else 0
                    r = st.selectbox("Rol", roles_edit, index=idx_rol)
                    act = st.checkbox("Activo", value=bool(datos["activo"]))
                    pwd_crit = st.text_input("Contrasena de Admin o Direccion", type="password")
                    if st.form_submit_button("Guardar", type="primary"):
                        if not pwd_crit: st.error("Ingresa la contrasena.")
                        elif not verificar_password_critica(pwd_crit): st.error("Contrasena incorrecta.")
                        else:
                            u_norm = u.strip().lower()
                            dup = con.execute("SELECT id FROM usuarios WHERE LOWER(usuario)=? AND id!=?", (u_norm, idu)).fetchone()
                            if dup: st.error("Ya existe otro usuario con ese nombre.")
                            else:
                                if p:
                                    escribir("UPDATE usuarios SET usuario=?,nombres=?,rol=?,password=?,activo=? WHERE id=?",
                                             (u_norm, n, r, hashear_password(p), 1 if act else 0, idu))
                                else:
                                    escribir("UPDATE usuarios SET usuario=?,nombres=?,rol=?,activo=? WHERE id=?",
                                             (u_norm, n, r, 1 if act else 0, idu))
                                auditar(usuario["usuario"], "Edito usuario " + u_norm)
                                st.session_state["_aviso_crear_usuario"] = {"tipo": "ok", "msg": "Usuario editado."}
                                st.rerun()

    with tabs[3]:
        st.subheader("Asignar secciones a Auxiliares")
        st.caption("Cada seccion solo puede estar asignada a un auxiliar.")
        dfa = pd.read_sql("SELECT id,usuario,nombres,turno_asignado FROM usuarios WHERE rol='Auxiliar' AND activo=1", con)
        if dfa.empty: st.info("Sin auxiliares.")
        else:
            ops = {r['nombres'] + " (" + r['usuario'] + ")": r["id"] for _, r in dfa.iterrows()}
            sel = st.selectbox("Auxiliar", list(ops.keys())); ida = ops[sel]
            ta = con.execute("SELECT turno_asignado FROM usuarios WHERE id=?", (ida,)).fetchone()
            if ta and ta["turno_asignado"]:
                secs = secciones_por_turno(ta["turno_asignado"])
                asignadas_a_otros = {r["seccion_id"] for r in con.execute("SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id!=?", (ida,)).fetchall()}
                asig = {r["seccion_id"] for r in con.execute("SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id=?", (ida,)).fetchall()}
                st.write("Secciones disponibles:")
                sel_s = []
                for s in secs:
                    if s["id"] in asignadas_a_otros: continue
                    if st.checkbox(s['grado'] + " " + s['nombre'], value=s["id"] in asig, key="asig_" + str(ida) + "_" + str(s['id'])):
                        sel_s.append(s["id"])
                if _pedir_password_critica("asig_" + str(ida), "Guardar asignaciones"):
                    with _lock_escritura:
                        con.execute("DELETE FROM auxiliar_secciones WHERE usuario_id=?", (ida,))
                        for sid in sel_s:
                            try: con.execute("INSERT INTO auxiliar_secciones(usuario_id,seccion_id) VALUES(?,?)", (ida, sid))
                            except sqlite3.IntegrityError: st.warning("La seccion ya estaba asignada.")
                        con.commit()
                    auditar(usuario["usuario"], "Asigno " + str(len(sel_s)) + " secciones a usuario_id=" + str(ida))
                    st.session_state["_aviso_crear_usuario"] = {"tipo": "ok", "msg": "Asignaciones guardadas."}
                    st.rerun()
            else: st.warning("Este auxiliar no tiene turno asignado.")

    with tabs[4]:
        st.subheader("Modo mantenimiento")
        if modo_mantenimiento():
            st.error("El sistema esta en MANTENIMIENTO.")
            if _pedir_password_critica("mant_off", "Desactivar mantenimiento"):
                desactivar_mantenimiento(usuario)
                st.session_state["_aviso_crear_usuario"] = {"tipo": "ok", "msg": "Mantenimiento desactivado."}
                st.rerun()
        else:
            st.success("El sistema esta operativo.")
            msg = st.text_input("Mensaje para mostrar (opcional)", key="mant_msg")
            if _pedir_password_critica("mant_on", "Activar mantenimiento"):
                activar_mantenimiento(usuario, msg)
                st.session_state["_aviso_crear_usuario"] = {"tipo": "ok", "msg": "Mantenimiento activado."}
                st.rerun()


# ─── AUDITORIA ────────────────────────────────────────────────────────────
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
        mapeo = {"dni": m_dni, "nombres": m_nom, "apellido_paterno": m_pat, "apellido_materno": m_mat,
                 "grado": m_gra, "seccion": m_sec, "turno": m_tur,
                 "apoderado_nombre": m_apo_n, "apoderado_telefono": m_apo_t}
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
                st.toast(str(ins) + " importados, " + str(reac) + " reactivados.")
                if errs: st.warning(str(len(errs)) + " errores al insertar")
                for k in ["_iv", "_ie", "_ir"]: st.session_state.pop(k, None)
                st.rerun()


def vista_auditoria():
    st.title("Auditoria y Periodos")
    usuario = st.session_state["user"]
    tabs = st.tabs(["Registros", "Periodos", "Cierre de año"])
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
            pwd_crit = st.text_input("Contrasena de Admin o Direccion", type="password")
            if st.form_submit_button("Crear y activar", type="primary"):
                if not nom.strip(): st.error("Ingresa un nombre.")
                elif (ff - fi).days < 30: st.error("El periodo debe durar minimo 1 mes.")
                elif not pwd_crit: st.error("Ingresa la contrasena.")
                elif not verificar_password_critica(pwd_crit): st.error("Contrasena incorrecta.")
                else:
                    ok, msg = crear_periodo(nom.strip(), fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"), usuario)
                    if ok: st.toast(msg); st.rerun()
                    else: st.error(msg)
        st.markdown("---"); st.markdown("### Cargar alumnos al periodo activo")
        p = obtener_periodo_activo()
        if p:
            if periodo_tiene_alumnos(p["id"]): st.success("El periodo ya tiene alumnos cargados.")
            else: st.warning("El periodo NO tiene alumnos.")
            _frag_importar_excel()
        else: st.info("No hay periodo activo.")
        st.markdown("---"); st.markdown("### Activar periodo (solo no cerrados)")
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
        st.subheader("Cierre de año escolar")
        st.warning("Al cerrar el periodo se desactivan TODOS los alumnos.")
        p = obtener_periodo_activo()
        if not p: st.info("No hay periodo activo."); return
        st.info("Periodo activo: " + p['nombre'] + " (" + p['fecha_inicio'] + " - " + p['fecha_fin'] + ")")
        st.markdown("Reporte resumen del periodo:")
        df_rep = reporte_cierre_anual(p["id"])
        if not df_rep.empty: st.dataframe(df_rep, width='stretch')
        st.markdown("---")
        st.markdown("Paso 1: Descargar reporte anual detallado (OBLIGATORIO)")
        if st.button("Generar y descargar reporte anual", key="btn_desc_anual"):
            hojas = reporte_detallado_por_mes(p["id"])
            if not hojas: st.warning("Sin datos.")
            else:
                st.download_button("Descargar Excel anual", df_a_xlsx_multilhoja(hojas),
                                   "reporte_anual_" + p['nombre'] + ".xlsx",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                st.session_state["_reporte_descargado"] = True
                st.toast("Reporte generado.")
        st.markdown("---")
        st.markdown("Paso 2: Cerrar periodo (requiere contrasena)")
        desc = st.session_state.get("_reporte_descargado", False)
        if not desc: st.info("Debes descargar el reporte anual antes.")
        with st.form("cerrar_año"):
            c1, c2, c3 = st.columns(3)
            with c1: nn = st.text_input("Nombre nuevo periodo", value=str(ahora().year + 1))
            with c2: fi = st.date_input("Inicio nuevo", date(ahora().year + 1, 3, 1))
            with c3: ff = st.date_input("Fin nuevo", date(ahora().year + 1, 12, 31))
            pwd = st.text_input("Contrasena de Admin o Direccion", type="password")
            conf = st.text_input("Escribe CERRAR para confirmar")
            sub = st.form_submit_button("Cerrar año escolar", type="primary")
        if sub:
            if not desc: st.error("Primero debes descargar el reporte anual.")
            elif conf.strip() != "CERRAR": st.error("Debes escribir exactamente CERRAR.")
            elif not pwd: st.error("Ingresa la contrasena.")
            elif not verificar_password_critica(pwd): st.error("Contrasena incorrecta.")
            else:
                ok, msg = cerrar_año_escolar(usuario, p["id"], nn, fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"))
                if ok:
                    st.session_state.pop("_reporte_descargado", None); st.success(msg); st.rerun()
                else: st.error(msg)
        st.markdown("---"); st.markdown("Cierres anteriores:")
        dfc = listar_cierres_anuales()
        if not dfc.empty: st.dataframe(dfc, width='stretch')
        st.markdown("Periodos cerrados (solo consulta):")
        dfp = listar_periodos_cerrados()
        if not dfp.empty: st.dataframe(dfp, width='stretch')


# ─── DIAS ESPECIALES ──────────────────────────────────────────────────────
def vista_dias_especiales():
    st.title("Dias especiales")
    usuario = st.session_state["user"]
    con = obtener_conexion()
    tabs = st.tabs(["Crear", "Listar / eliminar"])
    with tabs[0]:
        st.caption("Puedes crear varios eventos para el mismo dia.")
        for k in list(st.session_state.keys()):
            if (k.startswith("dia_sec_") or k.startswith("dia_grado_")):
                if k not in st.session_state.get("_dia_keys_actuales", set()): del st.session_state[k]
        if "dia_tipo" not in st.session_state: st.session_state["dia_tipo"] = "Evento"
        tipo = st.radio("Tipo", ["Evento", "Feriado"], key="dia_tipo", horizontal=True)
        with st.form("crear_dia"):
            c1, c2 = st.columns(2)
            with c1:
                fecha = st.date_input("Fecha", min_value=ahora().date())
                desc = st.text_input("Descripcion / Nombre del evento")
            with c2:
                if tipo == "Evento":
                    turnos_opts = {"Ambos": None}
                    turnos_opts.update({t["nombre"]: t["id"] for t in listar_turnos()})
                    t_lbl = st.selectbox("Turno", list(turnos_opts.keys()))
                    hora_t = st.time_input("Hora entrada", value=datetime.strptime("08:00", "%H:%M").time())
                    hora = hora_t.strftime("%H:%M")
                else:
                    turnos_opts = {"Ambos": None}; t_lbl = "Ambos"; hora = "00:00"
                    st.info("Los feriados no tienen horario ni turno.")
            contar_como_clases = st.checkbox("Contar como CLASES NORMALES (no como evento)", value=False) if tipo == "Evento" else False
            st.markdown("Alcance del dia especial:")
            keys_actuales = set()
            with st.expander("Seleccionar grados y secciones", expanded=True):
                grados = listar_grados()
                selecciones_secciones = []
                for g in grados:
                    k_grado = "dia_grado_" + str(g["id"]); keys_actuales.add(k_grado)
                    st.checkbox("Todo " + g["nombre"], key=k_grado)
                    secs = secciones_por_grado(g["id"])
                    if secs:
                        cols = st.columns(3)
                        for i, s in enumerate(secs):
                            k_sec = "dia_sec_" + str(g["id"]) + "_" + str(s["id"]); keys_actuales.add(k_sec)
                            with cols[i % 3]:
                                if st.checkbox(g["nombre"] + " " + s["nombre"], key=k_sec):
                                    selecciones_secciones.append(s["id"])
            st.session_state["_dia_keys_actuales"] = keys_actuales
            pwd_crear = st.text_input("Contrasena de Admin o Direccion", type="password")
            sub = st.form_submit_button("Crear evento", type="primary")
        if sub:
            if not desc.strip(): st.error("Descripcion requerida.")
            elif tipo == "Evento" and not selecciones_secciones: st.error("Selecciona al menos una seccion.")
            elif not pwd_crear: st.error("Ingresa la contrasena.")
            elif not verificar_password_critica(pwd_crear): st.error("Contrasena incorrecta.")
            else:
                selecciones_secciones = list(set(selecciones_secciones))
                idt = turnos_opts.get(t_lbl) if tipo == "Evento" else None
                per = obtener_periodo_activo(); pid = per["id"] if per else None
                with _lock_escritura:
                    cur = con.execute(
                        "INSERT INTO dias_especiales(fecha,descripcion,turno_id,hora_entrada,tipo,periodo_id,contar_como_clases) VALUES(?,?,?,?,?,?,?)",
                        (fecha.strftime("%Y-%m-%d"), desc.strip(), idt,
                         hora if tipo == "Evento" else "00:00",
                         "evento" if tipo == "Evento" else "feriado",
                         pid, 1 if contar_como_clases else 0))
                    idd = cur.lastrowid; con.commit()
                for sid in selecciones_secciones:
                    try: escribir("INSERT INTO dias_especiales_secciones(dia_especial_id,seccion_id) VALUES(?,?)", (idd, sid))
                    except sqlite3.IntegrityError: pass
                auditar(usuario["usuario"], "Creo dia especial " + desc)
                for k in list(st.session_state.keys()):
                    if k.startswith("dia_sec_") or k.startswith("dia_grado_"): del st.session_state[k]
                st.session_state.pop("_dia_keys_actuales", None)
                st.session_state["_aviso_crear_usuario"] = {"tipo": "ok", "msg": "Evento creado."}
                st.rerun()
    with tabs[1]:
        fh = hoy_str()
        df = pd.read_sql(
            "SELECT d.id,d.fecha,d.descripcion,COALESCE(t.nombre,'Ambos') AS turno,d.hora_entrada,d.tipo,d.contar_como_clases,"
            "(SELECT GROUP_CONCAT(g.nombre||' '||s.nombre, ', ') FROM dias_especiales_secciones ds "
            " JOIN secciones s ON ds.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
            " WHERE ds.dia_especial_id=d.id) AS secciones_aplicadas "
            "FROM dias_especiales d LEFT JOIN turnos t ON d.turno_id=t.id "
            "WHERE d.fecha>=? AND d.activo=1 ORDER BY d.fecha, d.hora_entrada",
            con, params=[fh])
        if df.empty: st.info("Sin dias especiales.")
        else:
            df_display = df.copy()
            df_display["contar_como_clases"] = df_display["contar_como_clases"].apply(lambda x: "Clases normales" if x else "Evento")
            df_display["secciones_aplicadas"] = df_display["secciones_aplicadas"].fillna("Todo el colegio")
            st.dataframe(df_display, width='stretch')
            ops = {r['fecha'] + " " + r['hora_entrada'] + " - " + r['descripcion']: r["id"] for _, r in df.iterrows()}
            sel = st.selectbox("Eliminar", list(ops.keys()))
            st.warning("Esta accion es irreversible.")
            if _pedir_password_critica("del_dia", "Eliminar dia especial"):
                escribir("DELETE FROM dias_especiales WHERE id=?", (ops[sel],))
                auditar(usuario["usuario"], "Elimino dia especial id=" + str(ops[sel]))
                st.session_state["_aviso_crear_usuario"] = {"tipo": "ok", "msg": "Dia especial eliminado."}
                st.rerun()


# ─── JUSTIFICACIONES Y PERMISOS ───────────────────────────────────────────
def _buscar_alumno_widget(clave):
    idg, ids, texto = filtros_grado_seccion_nombre(clave)
    if texto or idg:
        df = buscar_alumnos(texto, idg, ids, limite=200)
        st.caption(str(len(df)) + " coincidencia(s).")
    else:
        con = obtener_conexion()
        df = pd.read_sql(
            "SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno, "
            "g.id AS grado_id, g.nombre AS grado, s.id AS seccion_id, s.nombre AS seccion, t.nombre AS turno, "
            "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS nombre_completo "
            "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
            "JOIN turnos t ON s.turno_id=t.id WHERE a.activo=1 "
            "ORDER BY a.apellido_paterno, a.apellido_materno LIMIT 200", con)
        st.caption("Mostrando " + str(len(df)) + " alumnos.")
    if df.empty: st.warning("Sin coincidencias."); return None
    ops = {r['nombre_completo'] + " - " + r['grado'] + " " + r['seccion'] + " (" + r['turno'] + ")": r["id"] for _, r in df.iterrows()}
    sel = st.selectbox("Alumno", list(ops.keys()), key=clave + "_sel_al"); idal = ops[sel]
    con = obtener_conexion()
    al = con.execute(
        "SELECT a.id,a.dni,a.nombres,a.apellido_paterno,a.apellido_materno,a.seccion_id,"
        "a.nombre_apoderado,a.telefono_apoderado,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno "
        "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
        "JOIN turnos t ON s.turno_id=t.id WHERE a.id=?", (idal,)).fetchone()
    return dict(al) if al else None


def _frag_justificacion_previa(usuario):
    st.subheader("Justificacion previa")
    st.caption("Solo aplica a FALTAS. Solo hoy, manana o pasado manana.")
    tabs = st.tabs(["Historial global", "Registrar nueva", "Por alumno"])
    with tabs[0]:
        hoy_s = hoy_str()
        manana_s = (ahora().date() + timedelta(days=1)).strftime("%Y-%m-%d")
        pasado_s = (ahora().date() + timedelta(days=2)).strftime("%Y-%m-%d")
        st.markdown("**Justificaciones previas de HOY / MANANA / PASADO MANANA**")
        df_vig = pd.read_sql(
            "SELECT jp.fecha_objetivo AS Fecha, a.dni AS DNI, "
            "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS Alumno, "
            "g.nombre AS Grado, s.nombre AS Seccion, t.nombre AS Turno, jp.motivo AS Motivo, "
            "CASE WHEN jp.aplicada=1 THEN 'Aplicada' ELSE 'Pendiente' END AS Estado, "
            "jp.creado_por AS 'Creado por', jp.timestamp AS 'Registrado' "
            "FROM justificaciones_previas jp JOIN alumnos a ON jp.alumno_id=a.id "
            "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
            "JOIN turnos t ON s.turno_id=t.id "
            "WHERE jp.fecha_objetivo BETWEEN ? AND ? ORDER BY jp.fecha_objetivo, a.apellido_paterno",
            obtener_conexion(), params=[hoy_s, pasado_s])
        if df_vig.empty: st.info("Sin justificaciones previas para hoy, manana o pasado manana.")
        else: st.dataframe(df_vig, width='stretch', hide_index=True)
        st.markdown("---")
        st.markdown("**Todas las justificaciones previas registradas**")
        c1, c2 = st.columns(2)
        with c1: filtro_estado = st.selectbox("Filtrar por estado", ["Todas","Pendientes","Aplicadas"], key="jp_filtro_estado")
        with c2: filtro_texto = st.text_input("Buscar por DNI o apellido", key="jp_filtro_texto", placeholder="Ej: 12345678 o Quispe")
        q = ("SELECT jp.fecha_objetivo AS Fecha, a.dni AS DNI, "
             "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS Alumno, "
             "g.nombre AS Grado, s.nombre AS Seccion, t.nombre AS Turno, jp.motivo AS Motivo, "
             "CASE WHEN jp.aplicada=1 THEN 'Aplicada' ELSE 'Pendiente' END AS Estado, "
             "jp.creado_por AS 'Creado por', jp.timestamp AS 'Registrado' "
             "FROM justificaciones_previas jp JOIN alumnos a ON jp.alumno_id=a.id "
             "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
             "JOIN turnos t ON s.turno_id=t.id WHERE 1=1")
        params = []
        if filtro_estado == "Pendientes": q += " AND jp.aplicada=0"
        elif filtro_estado == "Aplicadas": q += " AND jp.aplicada=1"
        if filtro_texto.strip():
            q += " AND (a.dni LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
            pat = "%" + filtro_texto.strip() + "%"; params += [pat, pat, pat]
        q += " ORDER BY jp.fecha_objetivo DESC, a.apellido_paterno LIMIT 500"
        df_all = pd.read_sql(q, obtener_conexion(), params=params)
        if df_all.empty: st.info("Sin justificaciones previas.")
        else:
            st.write(str(len(df_all)) + " justificaciones")
            st.dataframe(df_all, width='stretch', hide_index=True)
    with tabs[1]:
        al = _buscar_alumno_widget("jp")
        if not al: return
        bloq = alumno_bloqueado(al["id"])
        if bloq: st.error("Este alumno esta BLOQUEADO: " + (bloq.get("motivo") or ""))
        nombre = (al['apellido_paterno'] + " " + (al['apellido_materno'] or "") + ", " + al['nombres']).strip(", ")
        st.markdown("**" + nombre + "** | DNI " + al['dni'] + " | " + al['grado'] + " " + al['seccion'] + " | Turno " + al['turno'], unsafe_allow_html=True)
        if al.get("nombre_apoderado"):
            st.caption("Apoderado: " + al["nombre_apoderado"] + (" - Tel: " + al["telefono_apoderado"] if al.get("telefono_apoderado") else ""))
        st.markdown("---")
        st.markdown("**Registrar nueva justificacion previa (FALTA)**")
        hoy = ahora().date()
        with st.form("form_just_prev"):
            c1, c2 = st.columns(2)
            with c1:
                fecha_obj = st.date_input("Fecha objetivo", value=hoy, min_value=hoy, max_value=hoy + timedelta(days=2), key="jp_fecha")
            with c2:
                motivo = st.text_area("Motivo (obligatorio)", key="jp_motivo", placeholder="Ej: Cita medica")
            sub = st.form_submit_button("Registrar justificacion", type="primary")
        if sub:
            if not motivo.strip(): st.error("El motivo es obligatorio.")
            else:
                ok, msg = crear_justificacion_previa(al["id"], fecha_obj.strftime("%Y-%m-%d"), FALTA, motivo.strip(), usuario)
                if ok: st.toast(msg); st.rerun()
                else: st.error(msg)
    with tabs[2]:
        st.markdown("**Busca un alumno para ver TODAS sus justificaciones previas**")
        al = _buscar_alumno_widget("jp_ver")
        if not al: return
        nombre = (al['apellido_paterno'] + " " + (al['apellido_materno'] or "") + ", " + al['nombres']).strip(", ")
        st.markdown("**" + nombre + "** | DNI " + al['dni'], unsafe_allow_html=True)
        con = obtener_conexion()
        df_prev = pd.read_sql(
            "SELECT fecha_objetivo AS 'Fecha objetivo', motivo AS Motivo, "
            "CASE WHEN aplicada=1 THEN 'Aplicada' ELSE 'Pendiente' END AS Estado, "
            "creado_por AS 'Creado por', timestamp AS 'Registrado' "
            "FROM justificaciones_previas WHERE alumno_id=? ORDER BY fecha_objetivo DESC",
            con, params=[al["id"]])
        if not df_prev.empty:
            st.write(str(len(df_prev)) + " justificaciones")
            st.dataframe(df_prev, width='stretch', hide_index=True)
        else: st.info("Sin justificaciones previas.")


def _frag_permisos(usuario):
    st.subheader("Permisos de inasistencia")
    st.caption("Permisos para dias FUTUROS (desde manana, maximo " + str(MAX_DIAS_PERMISO) + " dias).")
    tabs = st.tabs(["Historial global", "Registrar nueva", "Por alumno"])
    with tabs[0]:
        hoy_s = hoy_str()
        st.markdown("**Permisos vigentes HOY**")
        df_hoy = pd.read_sql(
            "SELECT p.fecha_inicio AS Inicio, p.fecha_fin AS Fin, a.dni AS DNI, "
            "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS Alumno, "
            "g.nombre AS Grado, s.nombre AS Seccion, t.nombre AS Turno, COALESCE(p.motivo,'') AS Motivo "
            "FROM permisos p JOIN alumnos a ON p.alumno_id=a.id "
            "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
            "JOIN turnos t ON s.turno_id=t.id "
            "WHERE p.activo=1 AND p.fecha_inicio<=? AND p.fecha_fin>=? ORDER BY a.apellido_paterno",
            obtener_conexion(), params=[hoy_s, hoy_s])
        if df_hoy.empty: st.info("Sin permisos vigentes hoy.")
        else: st.dataframe(df_hoy, width='stretch', hide_index=True)
        st.markdown("---")
        st.markdown("**Permisos de los proximos 7 dias**")
        limite_s = (ahora().date() + timedelta(days=MAX_DIAS_PERMISO)).strftime("%Y-%m-%d")
        df_prox = pd.read_sql(
            "SELECT p.fecha_inicio AS Inicio, p.fecha_fin AS Fin, a.dni AS DNI, "
            "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS Alumno, "
            "g.nombre AS Grado, s.nombre AS Seccion, t.nombre AS Turno, COALESCE(p.motivo,'') AS Motivo "
            "FROM permisos p JOIN alumnos a ON p.alumno_id=a.id "
            "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
            "JOIN turnos t ON s.turno_id=t.id "
            "WHERE p.activo=1 AND p.fecha_inicio>? AND p.fecha_inicio<=? ORDER BY p.fecha_inicio, a.apellido_paterno",
            obtener_conexion(), params=[hoy_s, limite_s])
        if df_prox.empty: st.info("Sin permisos programados.")
        else: st.dataframe(df_prox, width='stretch', hide_index=True)
        st.markdown("---")
        st.markdown("**Todos los permisos registrados**")
        c1, c2 = st.columns(2)
        with c1: filtro_estado = st.selectbox("Filtrar por estado", ["Todos","Activos","Inactivos"], key="perm_filtro_estado")
        with c2: filtro_texto = st.text_input("Buscar por DNI o apellido", key="perm_filtro_texto", placeholder="Ej: 12345678")
        q = ("SELECT p.fecha_inicio AS Inicio, p.fecha_fin AS Fin, a.dni AS DNI, "
             "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS Alumno, "
             "g.nombre AS Grado, s.nombre AS Seccion, t.nombre AS Turno, COALESCE(p.motivo,'') AS Motivo, "
             "CASE WHEN p.activo=1 THEN 'Activo' ELSE 'Inactivo' END AS Estado "
             "FROM permisos p JOIN alumnos a ON p.alumno_id=a.id "
             "JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id "
             "JOIN turnos t ON s.turno_id=t.id WHERE 1=1")
        params = []
        if filtro_estado == "Activos": q += " AND p.activo=1"
        elif filtro_estado == "Inactivos": q += " AND p.activo=0"
        if filtro_texto.strip():
            q += " AND (a.dni LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
            pat = "%" + filtro_texto.strip() + "%"; params += [pat, pat, pat]
        q += " ORDER BY p.fecha_inicio DESC, a.apellido_paterno LIMIT 500"
        df_all = pd.read_sql(q, obtener_conexion(), params=params)
        if df_all.empty: st.info("Sin permisos.")
        else:
            st.write(str(len(df_all)) + " permisos")
            st.dataframe(df_all, width='stretch', hide_index=True)
    with tabs[1]:
        al = _buscar_alumno_widget("perm")
        if not al: return
        bloq = alumno_bloqueado(al["id"])
        if bloq: st.error("Este alumno esta BLOQUEADO: " + (bloq.get("motivo") or ""))
        nombre = (al['apellido_paterno'] + " " + (al['apellido_materno'] or "") + ", " + al['nombres']).strip(", ")
        st.markdown("**" + nombre + "** | DNI " + al['dni'] + " | " + al['grado'] + " " + al['seccion'], unsafe_allow_html=True)
        st.markdown("---")
        st.markdown("**Registrar nuevo permiso**")
        manana = ahora().date() + timedelta(days=1)
        with st.form("form_permiso"):
            c1, c2 = st.columns(2)
            with c1: fi = st.date_input("Fecha inicio", value=manana, min_value=manana, key="perm_fi")
            with c2: ff = st.date_input("Fecha fin", value=manana, min_value=manana, key="perm_ff")
            motivo = st.text_area("Motivo (obligatorio)", key="perm_motivo", placeholder="Ej: Viaje familiar")
            sub = st.form_submit_button("Registrar permiso", type="primary")
        if sub:
            if not motivo.strip(): st.error("El motivo es obligatorio.")
            elif ff < fi: st.error("La fecha fin no puede ser anterior a la fecha inicio.")
            elif (ff - fi).days + 1 > MAX_DIAS_PERMISO: st.error("Maximo " + str(MAX_DIAS_PERMISO) + " dias.")
            else:
                ok, msg = crear_permiso(al["id"], fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"), motivo.strip(), usuario)
                if ok: st.toast(msg); st.rerun()
                else: st.error(msg)
    with tabs[2]:
        st.markdown("**Busca un alumno para ver TODOS sus permisos**")
        al = _buscar_alumno_widget("perm_ver")
        if not al: return
        nombre = (al['apellido_paterno'] + " " + (al['apellido_materno'] or "") + ", " + al['nombres']).strip(", ")
        st.markdown("**" + nombre + "** | DNI " + al['dni'], unsafe_allow_html=True)
        con = obtener_conexion()
        df_perm = pd.read_sql(
            "SELECT fecha_inicio AS Inicio, fecha_fin AS Fin, COALESCE(motivo,'') AS Motivo, "
            "CASE WHEN activo=1 THEN 'Activo' ELSE 'Inactivo' END AS Estado, "
            "creado_por AS 'Creado por', timestamp AS 'Registrado' "
            "FROM permisos WHERE alumno_id=? ORDER BY fecha_inicio DESC",
            con, params=[al["id"]])
        if not df_perm.empty:
            st.write(str(len(df_perm)) + " permisos")
            st.dataframe(df_perm, width='stretch', hide_index=True)
        else: st.info("Sin permisos registrados.")


def vista_justificaciones_permisos():
    st.title("Justificaciones y Permisos")
    usuario = st.session_state["user"]
    tabs = st.tabs(["Justificacion previa", "Permisos"])
    with tabs[0]: _frag_justificacion_previa(usuario)
    with tabs[1]: _frag_permisos(usuario)


# ─── MENU Y RUTAS ─────────────────────────────────────────────────────────
def obtener_opciones_por_rol(usuario):
    rol = usuario["rol"]
    if rol == "Admin":
        return ["Puerta","Bloqueados","Panel Direccion","Reportes","Alumnos","Grados y Secciones",
                "Carnets","Dias especiales","Ventanas","Justificaciones y Permisos",
                "Usuarios","Auditoria","Mi cuenta"]
    if rol == "Direccion":
        return ["Panel Direccion","Bloqueados","Reportes","Alumnos","Carnets",
                "Dias especiales","Justificaciones y Permisos","Mi cuenta"]
    if rol == "Auxiliar":
        return ["Puerta","Bloqueados","Justificaciones y Permisos","Reportes","Mi cuenta"]
    return []


RUTAS = {
    "Puerta": vista_puerta,
    "Bloqueados": vista_bloqueados,
    "Panel Direccion": vista_panel_direccion,
    "Reportes": vista_reportes,
    "Alumnos": vista_alumnos,
    "Grados y Secciones": vista_grados_secciones,
    "Carnets": vista_carnets,
    "Dias especiales": vista_dias_especiales,
    "Ventanas": vista_ventanas,
    "Usuarios": vista_usuarios,
    "Auditoria": vista_auditoria,
    "Mi cuenta": vista_mi_cuenta,
    "Justificaciones y Permisos": vista_justificaciones_permisos,
}


def menu_lateral():
    usuario = st.session_state["user"]; rol = usuario["rol"]
    opciones = obtener_opciones_por_rol(usuario)
    with st.sidebar:
        inicial = (usuario["nombres"] or "?")[0].upper()
        st.markdown(
            '<div class="encabezado-sidebar">'
            '<div class="avatar">' + inicial + '</div>'
            '<div class="nombre">' + usuario["nombres"] + '</div>'
            '<div class="rol">' + rol + '</div>'
            '</div>', unsafe_allow_html=True)
        if "menu" not in st.session_state or st.session_state["menu"] not in opciones:
            st.session_state["menu"] = opciones[0]
        op = st.radio("Menu", opciones, key="menu", label_visibility="collapsed")
        st.markdown("---")
        if st.button("Cerrar sesion", width='stretch'):
            cerrar_sesion(); st.rerun()
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

    # ─── Si NO estamos en Reportes, limpiamos su estado ───
    if op != "Reportes":
        st.session_state.pop("_rep_iniciado", None)

    v()


def _control_faltas():
    ult = st.session_state.get("_ultimo_control_faltas"); t = time.time()
    if ult and (t - ult) < 300: return
    st.session_state["_ultimo_control_faltas"] = t
    marcar_faltas_al_cierre()


def main():
    st.set_page_config(page_title="Asistencia I.E. Yarinacocha", page_icon="escudo.png",
                       layout="wide", initial_sidebar_state="expanded")
    try:
        inicializar_bd()
        aplicar_estilos()
        if not st.session_state.get("user"):
            vista_login(); return
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
