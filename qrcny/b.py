import hashlib, logging, os, re, secrets, sqlite3, threading, time
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from streamlit_qrcode_scanner import qrcode_scanner
import cv2
import extra_streamlit_components as stx
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

LOG_DIR = Path("logs"); LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("asistencia")

DB_PATH = "asistencia.db"
MESES_ES = ["","Enero","Febrero","Marzo","Abril","Mayo","Junio",
            "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
C_NARANJA = "#E65100"
C_VERDE_BG = "#d4edda"; C_VERDE_TX = "#155724"
C_AMAR_BG = "#fff3cd"; C_AMAR_TX = "#856404"
C_ROJO_BG = "#f8d7da"; C_ROJO_TX = "#721c24"

PBK_ITER = 260_000; PBK_ALG = "sha256"
DIAS_TOKEN = 30
COOKIE_KEY = "asistencia_ie_yarinacocha_token"
COOKIE_NOM = "asistencia_token"
MAX_INTENTOS = 3; MIN_BLOQUEO = 10

PUNTUAL="Puntual"; TARDANZA="Tardanza"; FALTA="Falta"
REF_ASISTIO="Asistio"; REF_NO_ASISTIO="No asistio"
ACC_PERDONADO="PERDONADO"; ACC_DERIVADO="DERIVADO_TOECE"; ACC_RETENIDO="RETENIDO_APODERADO"
ROLES_VALIDOS=("Admin","TOECE","Auxiliar","Direccion")
VENT_CLASES="clases"; VENT_REF="reforzamiento"

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

def sumar_minutos(hhmm, mins):
    return (datetime.strptime(hhmm, "%H:%M") + timedelta(minutes=mins)).strftime("%H:%M")

def es_fin_de_semana(fecha=None):
    return (fecha or ahora().date()).weekday() >= 5

def validar_usuario(usuario):
    if not usuario: return False, "El usuario no puede estar vacio."
    if len(usuario) > 15: return False, "El usuario no puede tener mas de 15 caracteres."
    if " " in usuario: return False, "El usuario no puede tener espacios."
    return True, ""

def validar_password(password):
    if not password: return False, "La contrasena no puede estar vacia."
    if len(password) < 6: return False, "La contrasena debe tener al menos 6 caracteres."
    if not re.search(r"[a-z]", password):
        return False, "La contrasena debe tener al menos una minuscula."
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>_\-+=\[\]\\/;'`~]", password):
        return False, "La contrasena debe tener al menos un caracter especial."
    if len(re.findall(r"\d", password)) < 4:
        return False, "La contrasena debe tener al menos 4 numeros."
    return True, ""

def validar_nombre(nombre):
    if not nombre: return False, "El nombre no puede estar vacio."
    if len(nombre) > 15: return False, "El nombre no puede tener mas de 15 caracteres."
    return True, ""

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

def hash_token(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()

def verificar_password_admin(password):
    con = obtener_conexion()
    for fila in con.execute("SELECT password FROM usuarios WHERE rol='Admin' AND activo=1").fetchall():
        if verificar_password(password, fila["password"]):
            return True
    return False

_conexion = None
_lock = threading.Lock()

def obtener_conexion():
    global _conexion
    if _conexion is None:
        _conexion = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        _conexion.row_factory = sqlite3.Row
        for p in ("journal_mode=WAL","synchronous=NORMAL","foreign_keys=ON","busy_timeout=30000"):
            _conexion.execute(f"PRAGMA {p}")
    return _conexion

def existe_columna(cur, tabla, col):
    return any(f["name"] == col for f in cur.execute(f"PRAGMA table_info({tabla})").fetchall())

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
    CREATE TABLE IF NOT EXISTS asistencias(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha TEXT NOT NULL,ventana_id INTEGER,tipo TEXT NOT NULL DEFAULT 'clases',hora TEXT,estado TEXT NOT NULL,justificada INTEGER DEFAULT 0,observacion TEXT,periodo_id INTEGER,UNIQUE(alumno_id,fecha,tipo));
    CREATE TABLE IF NOT EXISTS tardanzas(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha TEXT NOT NULL,hora TEXT NOT NULL,numero INTEGER NOT NULL,accion TEXT NOT NULL,observacion TEXT,justificada INTEGER DEFAULT 0,registrado_por TEXT,timestamp TEXT NOT NULL,periodo_id INTEGER,UNIQUE(alumno_id,fecha));
    CREATE TABLE IF NOT EXISTS actas_compromiso(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha TEXT NOT NULL,motivo TEXT,observacion TEXT,registrado_por TEXT,timestamp TEXT NOT NULL,periodo_id INTEGER);
    CREATE TABLE IF NOT EXISTS observados(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha_ingreso TEXT NOT NULL,motivo TEXT,activo INTEGER DEFAULT 1,fecha_salida TEXT,observacion_cierre TEXT,periodo_id INTEGER);
    CREATE TABLE IF NOT EXISTS bloqueos(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,motivo TEXT,activo INTEGER DEFAULT 1,fecha_inicio TEXT NOT NULL,fecha_fin TEXT,liberado_por TEXT,creado_por TEXT,origen TEXT DEFAULT 'automatico');
    CREATE TABLE IF NOT EXISTS justificaciones_previas(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha_objetivo TEXT NOT NULL,tipo TEXT NOT NULL CHECK(tipo IN ('Falta','Tardanza')),motivo TEXT,creado_por TEXT,timestamp TEXT NOT NULL,aplicada INTEGER DEFAULT 0,UNIQUE(alumno_id,fecha_objetivo,tipo));
    CREATE TABLE IF NOT EXISTS permisos(id INTEGER PRIMARY KEY,alumno_id INTEGER NOT NULL,fecha_objetivo TEXT NOT NULL,motivo TEXT,creado_por TEXT,timestamp TEXT NOT NULL,aplicada INTEGER DEFAULT 0,periodo_id INTEGER,UNIQUE(alumno_id,fecha_objetivo));
    CREATE TABLE IF NOT EXISTS dias_especiales(id INTEGER PRIMARY KEY,fecha TEXT NOT NULL,descripcion TEXT,turno_id INTEGER,hora_entrada TEXT,activo INTEGER DEFAULT 1,tipo TEXT DEFAULT 'evento' CHECK(tipo IN ('evento','feriado')),periodo_id INTEGER);
    CREATE TABLE IF NOT EXISTS dias_especiales_secciones(id INTEGER PRIMARY KEY,dia_especial_id INTEGER NOT NULL,seccion_id INTEGER NOT NULL,UNIQUE(dia_especial_id,seccion_id));
    CREATE TABLE IF NOT EXISTS usuarios(id INTEGER PRIMARY KEY,usuario TEXT UNIQUE NOT NULL,password TEXT NOT NULL,rol TEXT NOT NULL CHECK(rol IN ('Admin','TOECE','Auxiliar','Direccion')),nombres TEXT NOT NULL,turno_asignado INTEGER,activo INTEGER DEFAULT 1,intentos_fallidos INTEGER DEFAULT 0,bloqueado_hasta TEXT,debe_cambiar_password INTEGER DEFAULT 0,ultimo_login TEXT,ultimo_ip TEXT);
    CREATE TABLE IF NOT EXISTS auxiliar_secciones(id INTEGER PRIMARY KEY,usuario_id INTEGER NOT NULL,seccion_id INTEGER NOT NULL,UNIQUE(usuario_id,seccion_id));
    CREATE TABLE IF NOT EXISTS auditoria(id INTEGER PRIMARY KEY,usuario TEXT,accion TEXT,fecha TEXT,valor_anterior TEXT,valor_nuevo TEXT,tabla_afectada TEXT,registro_id INTEGER,ip TEXT);
    CREATE TABLE IF NOT EXISTS sesiones_tokens(id INTEGER PRIMARY KEY,token TEXT UNIQUE NOT NULL,usuario_id INTEGER NOT NULL,expira TEXT NOT NULL,creado TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS cierres_anuales(id INTEGER PRIMARY KEY,periodo_id INTEGER NOT NULL,fecha_cierre TEXT NOT NULL,generado_por TEXT,reporte_json TEXT);
    CREATE INDEX IF NOT EXISTS idx_ast_f ON asistencias(fecha);
    CREATE INDEX IF NOT EXISTS idx_ast_a ON asistencias(alumno_id);
    CREATE INDEX IF NOT EXISTS idx_tard_a ON tardanzas(alumno_id);
    CREATE INDEX IF NOT EXISTS idx_al_dni ON alumnos(dni);
    CREATE INDEX IF NOT EXISTS idx_al_sec ON alumnos(seccion_id);
    CREATE INDEX IF NOT EXISTS idx_jp_a ON justificaciones_previas(alumno_id,fecha_objetivo);
    CREATE INDEX IF NOT EXISTS idx_perm_a ON permisos(alumno_id,fecha_objetivo);
    CREATE INDEX IF NOT EXISTS idx_tok ON sesiones_tokens(token);
    """)
    _migrar(cur); _seed(cur); con.commit()

def _migrar(cur):
    migs = [("alumnos","activo","ALTER TABLE alumnos ADD COLUMN activo INTEGER DEFAULT 1"),
            ("alumnos","retirado_en","ALTER TABLE alumnos ADD COLUMN retirado_en TEXT"),
            ("usuarios","ultimo_login","ALTER TABLE usuarios ADD COLUMN ultimo_login TEXT"),
            ("usuarios","ultimo_ip","ALTER TABLE usuarios ADD COLUMN ultimo_ip TEXT"),
            ("auditoria","ip","ALTER TABLE auditoria ADD COLUMN ip TEXT"),
            ("asistencias","tipo","ALTER TABLE asistencias ADD COLUMN tipo TEXT DEFAULT 'clases'"),
            ("asistencias","ventana_id","ALTER TABLE asistencias ADD COLUMN ventana_id INTEGER"),
            ("periodos","cerrado","ALTER TABLE periodos ADD COLUMN cerrado INTEGER DEFAULT 0"),
            ("observados","observacion_cierre","ALTER TABLE observados ADD COLUMN observacion_cierre TEXT"),
            ("bloqueos","origen","ALTER TABLE bloqueos ADD COLUMN origen TEXT DEFAULT 'automatico'")]
    for t, c, sql in migs:
        if _tabla_existe(cur, t) and not existe_columna(cur, t, c):
            try: cur.execute(sql)
            except sqlite3.Error as e: log.warning("mig %s.%s: %s", t, c, e)

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
        cur.execute("INSERT INTO usuarios(usuario,password,rol,nombres,debe_cambiar_password) VALUES(?,?,'Admin','Administrador',1)",
                    ("admin", hashear_password(pwd)))
        log.warning("admin pass: %s", pwd)

def _cookie_mgr():
    return stx.CookieManager(key=COOKIE_KEY)

def crear_token_sesion(usuario):
    token = secrets.token_urlsafe(32); th = hash_token(token)
    con = obtener_conexion()
    exp = (ahora()+timedelta(days=DIAS_TOKEN)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("INSERT INTO sesiones_tokens(token,usuario_id,expira) VALUES(?,?,?)",
                (th, usuario["id"], exp)); con.commit()
    return token

def restaurar_sesion(token):
    if not token: return None
    con = obtener_conexion()
    f = con.execute("SELECT u.* FROM usuarios u JOIN sesiones_tokens st ON u.id=st.usuario_id "
                    "WHERE st.token=? AND st.expira>? AND u.activo=1",
                    (hash_token(token), timestamp_str())).fetchone()
    return dict(f) if f else None

def eliminar_token(token):
    if not token: return
    con = obtener_conexion()
    con.execute("DELETE FROM sesiones_tokens WHERE token=?", (hash_token(token),)); con.commit()

def _leer_cookie():
    try: return st.context.cookies.get(COOKIE_NOM)
    except Exception: return None

def _leer_query():
    try: return st.query_params.get("t")
    except Exception: return None

def _guardar_cookie(token):
    try:
        c = _cookie_mgr()
        c.set(COOKIE_NOM, token, expires_at=datetime.now()+timedelta(days=DIAS_TOKEN))
    except Exception as e: log.warning("cookie: %s", e)

def _borrar_cookie():
    try: _cookie_mgr().delete(COOKIE_NOM)
    except Exception as e: log.warning("cookie: %s", e)

def inicializar_sesion():
    if st.session_state.get("user"): return
    token = _leer_cookie()
    if not token:
        try: token = _cookie_mgr().get(COOKIE_NOM)
        except Exception: token = None
    if not token: token = _leer_query()
    if token:
        u = restaurar_sesion(token)
        if u:
            st.session_state["user"] = u
            st.session_state["_token"] = token
            st.session_state["_token_expira"] = time.time()+300

def refrescar_sesion_si_necesario():
    if not st.session_state.get("user"): return
    if time.time() < st.session_state.get("_token_expira", 0) - 60: return
    try:
        u = st.session_state["user"]; viejo = st.session_state.get("_token")
        if viejo: eliminar_token(viejo)
        nuevo = crear_token_sesion(u)
        st.session_state["_token"] = nuevo
        st.session_state["_token_expira"] = time.time()+300
        _guardar_cookie(nuevo)
    except Exception as e: log.warning("refresh sesion: %s", e)

def _bloqueado(u):
    if not u.get("bloqueado_hasta"): return False
    try: return ahora() < datetime.strptime(u["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError): return False

def _ip():
    try: return st.context.headers.get("X-Forwarded-For", "local")
    except Exception: return "local"

def autenticar(nombre_usuario, password):
    con = obtener_conexion()
    f = con.execute("SELECT * FROM usuarios WHERE usuario=? AND activo=1", (nombre_usuario,)).fetchone()
    if not f: return None, "Usuario no encontrado o inactivo"
    u = dict(f)
    if _bloqueado(u):
        lim = datetime.strptime(u["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S")
        return None, f"Cuenta bloqueada. Intenta en {int((lim-ahora()).total_seconds()/60)+1} minuto(s)"
    if not verificar_password(password, u["password"]):
        it = (u.get("intentos_fallidos") or 0) + 1
        if it >= MAX_INTENTOS:
            bh = (ahora()+timedelta(minutes=MIN_BLOQUEO)).strftime("%Y-%m-%d %H:%M:%S")
            con.execute("UPDATE usuarios SET intentos_fallidos=0,bloqueado_hasta=? WHERE id=?", (bh, u["id"]))
            con.commit()
            auditar(nombre_usuario, "Cuenta bloqueada por intentos fallidos")
            return None, f"Cuenta bloqueada por {MIN_BLOQUEO} minutos"
        con.execute("UPDATE usuarios SET intentos_fallidos=? WHERE id=?", (it, u["id"])); con.commit()
        return None, f"Credenciales incorrectas. Te quedan {MAX_INTENTOS-it} intento(s)"
    con.execute("UPDATE usuarios SET intentos_fallidos=0,bloqueado_hasta=NULL,ultimo_login=?,ultimo_ip=? WHERE id=?",
                (timestamp_str(), _ip(), u["id"])); con.commit()
    return u, ""

def auditar(usuario, accion, va=None, vn=None, tb=None, rid=None):
    try:
        con = obtener_conexion()
        con.execute("INSERT INTO auditoria(usuario,accion,fecha,valor_anterior,valor_nuevo,tabla_afectada,registro_id,ip) VALUES(?,?,?,?,?,?,?,?)",
                    (usuario, accion, timestamp_str(), va, vn, tb, rid, _ip()))
        con.commit()
    except Exception as e: log.warning("audit: %s", e)

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

def listar_periodos():
    return pd.read_sql("SELECT id,nombre,fecha_inicio,fecha_fin,activo,cerrado FROM periodos ORDER BY id DESC", obtener_conexion())

def listar_periodos_cerrados():
    return pd.read_sql("SELECT id,nombre,fecha_inicio,fecha_fin,fecha_cierre FROM periodos WHERE cerrado=1 ORDER BY id DESC", obtener_conexion())

def crear_periodo(nombre, fi, ff, usuario):
    con = obtener_conexion()
    try:
        cur = con.execute("INSERT INTO periodos(nombre,fecha_inicio,fecha_fin,activo,cerrado) VALUES(?,?,?,1,0)", (nombre, fi, ff))
        idn = cur.lastrowid
        con.execute("UPDATE periodos SET activo=0 WHERE id!=?", (idn,)); con.commit()
        auditar(usuario["usuario"], f"Creo periodo {nombre}", tb="periodos", rid=idn)
        return True, f"Periodo {nombre} creado y activado correctamente."
    except sqlite3.Error as e: return False, f"Error: {e}"

def activar_periodo(idp, usuario):
    con = obtener_conexion()
    f = con.execute("SELECT cerrado FROM periodos WHERE id=?", (idp,)).fetchone()
    if not f: return False, "Periodo no encontrado."
    if f["cerrado"]: return False, "Ese periodo esta cerrado."
    con.execute("UPDATE periodos SET activo=0")
    con.execute("UPDATE periodos SET activo=1 WHERE id=?", (idp,)); con.commit()
    auditar(usuario["usuario"], f"Activo periodo id={idp}", tb="periodos", rid=idp)
    return True, "Periodo activado correctamente."


def listar_turnos():
    con = obtener_conexion()
    return [dict(f) for f in con.execute("SELECT * FROM turnos ORDER BY id").fetchall()]

def listar_ventanas(id_turno=None):
    con = obtener_conexion()
    if id_turno:
        return [dict(f) for f in con.execute("SELECT * FROM ventanas WHERE turno_id=? AND activo=1 ORDER BY orden,id", (id_turno,)).fetchall()]
    return [dict(f) for f in con.execute("SELECT * FROM ventanas WHERE activo=1 ORDER BY turno_id,orden").fetchall()]

def dia_especial_hoy(id_turno, fecha, id_seccion=None):
    con = obtener_conexion()
    filas = con.execute("SELECT * FROM dias_especiales WHERE fecha=? AND activo=1 AND (turno_id=? OR turno_id IS NULL) ORDER BY turno_id DESC",
                        (fecha, id_turno)).fetchall()
    for f in filas:
        dia = dict(f)
        if not id_seccion: return dia
        secs = con.execute("SELECT seccion_id FROM dias_especiales_secciones WHERE dia_especial_id=?", (dia["id"],)).fetchall()
        if not secs: return dia
        if id_seccion in [s["seccion_id"] for s in secs]: return dia
    return None

def ventana_activa_para_alumno(id_turno, fecha, id_seccion=None):
    dia = dia_especial_hoy(id_turno, fecha, id_seccion)
    if dia and dia["tipo"] == "feriado": return None
    h = hora_corta()
    for v in listar_ventanas(id_turno):
        ap = v["hora_apertura"]
        if v["tipo"] == VENT_CLASES and dia and dia["tipo"] == "evento":
            try:
                datetime.strptime(dia["hora_entrada"], "%H:%M")
                ap = dia["hora_entrada"]
            except (ValueError, TypeError):
                pass
        lim = v["hora_limite_puntual"] or ap
        if ap <= h <= v["hora_cierre"]:
            return {**v, "hora_apertura_efectiva": ap, "hora_limite_efectiva": lim}
    return None

def listar_grados():
    con = obtener_conexion()
    return [dict(f) for f in con.execute("SELECT * FROM grados ORDER BY nombre").fetchall()]

def secciones_por_grado(idg):
    con = obtener_conexion()
    return [dict(f) for f in con.execute("SELECT * FROM secciones WHERE grado_id=? ORDER BY nombre", (idg,)).fetchall()]

def secciones_por_turno(idt):
    con = obtener_conexion()
    return [dict(f) for f in con.execute("SELECT s.*,g.nombre AS grado FROM secciones s JOIN grados g ON s.grado_id=g.id WHERE s.turno_id=? ORDER BY g.nombre,s.nombre", (idt,)).fetchall()]

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
            pat = f"%{w}%"; p += [pat, pat, pat]
    if idg: q += " AND g.id=?"; p.append(idg)
    if idsec: q += " AND s.id=?"; p.append(idsec)
    q += " ORDER BY a.apellido_paterno LIMIT ?"; p.append(limite)
    return pd.read_sql(q, con, params=p)

def _buscar_alumno_por_dni(con, dni):
    f = con.execute("SELECT a.id,a.dni,a.nombres,a.apellido_paterno,a.apellido_materno,s.id AS seccion_id,s.nombre AS seccion,g.nombre AS grado,t.id AS turno_id,t.nombre AS turno FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE a.dni=? AND a.activo=1",
                    (dni,)).fetchone()
    return dict(f) if f else None

def _nombre_completo(a):
    return f"{a['apellido_paterno']} {a['apellido_materno'] or ''}, {a['nombres']}".strip(", ")

def crear_alumno(dni, nombres, ap, am, idsec, apo_n, apo_t, usuario):
    con = obtener_conexion(); per = obtener_periodo_activo()
    if not per: return False, "No hay periodo activo."
    try:
        cur = con.cursor(); ida = None
        if apo_n:
            f = cur.execute("SELECT id FROM apoderados WHERE nombre=? AND COALESCE(telefono,'')=?", (apo_n, apo_t or "")).fetchone()
            ida = f["id"] if f else cur.execute("INSERT INTO apoderados(nombre,telefono) VALUES(?,?)", (apo_n, apo_t or None)).lastrowid
        cur.execute("INSERT INTO alumnos(dni,nombres,apellido_paterno,apellido_materno,seccion_id,apoderado_id,nombre_apoderado,telefono_apoderado,periodo_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (dni, nombres, ap, am or None, idsec, ida, apo_n or None, apo_t or None, per["id"]))
        con.commit()
        auditar(usuario["usuario"], f"Creo alumno DNI {dni}", tb="alumnos")
        return True, f"Alumno {nombres} creado correctamente."
    except sqlite3.IntegrityError: return False, f"Ya existe un alumno con DNI {dni}."
    except sqlite3.Error as e:
        log.error("crear alumno: %s", e); return False, "Error al crear el alumno."

def editar_alumno(idal, apo_n, apo_t, idsec, dni, usuario):
    con = obtener_conexion(); cur = con.cursor(); ida = None
    if apo_n:
        f = cur.execute("SELECT id FROM apoderados WHERE nombre=? AND COALESCE(telefono,'')=?", (apo_n, apo_t or "")).fetchone()
        ida = f["id"] if f else cur.execute("INSERT INTO apoderados(nombre,telefono) VALUES(?,?)", (apo_n, apo_t or None)).lastrowid
    con.execute("UPDATE alumnos SET apoderado_id=?,nombre_apoderado=?,telefono_apoderado=?,seccion_id=? WHERE id=?",
                (ida, apo_n or None, apo_t or None, idsec, idal)); con.commit()
    auditar(usuario["usuario"], f"Edito alumno {dni}", tb="alumnos", rid=idal)
    return True, "Alumno editado correctamente."

def retirar_alumno(idal, dni, usuario):
    con = obtener_conexion()
    con.execute("UPDATE alumnos SET activo=0,retirado_en=? WHERE id=?", (timestamp_str(), idal)); con.commit()
    auditar(usuario["usuario"], f"Desactivo alumno DNI {dni}", tb="alumnos", rid=idal)
    return True, "Alumno desactivado correctamente."

def reactivar_alumno(idal, dni, usuario):
    con = obtener_conexion()
    con.execute("UPDATE alumnos SET activo=1,retirado_en=NULL WHERE id=?", (idal,)); con.commit()
    auditar(usuario["usuario"], f"Reactivo alumno DNI {dni}", tb="alumnos", rid=idal)
    return True, "Alumno reactivado correctamente."


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
            if not re.fullmatch(r"\d{8}", dni): errs.append({"fila": nf, "motivo": f"DNI invalido '{dni}'"}); continue
            if dni in vistos: errs.append({"fila": nf, "motivo": f"DNI {dni} duplicado"}); continue
            if not nom or not ap or not gr or not sec: errs.append({"fila": nf, "motivo": "Faltan campos"}); continue
            if not con.execute("SELECT id FROM grados WHERE nombre=?", (gr,)).fetchone():
                errs.append({"fila": nf, "motivo": f"Grado '{gr}' no existe"}); continue
            if tur in ("mañana","manana","m","am","mñ"): tn = "Mañana"
            elif tur in ("tarde","t","tm","pm"): tn = "Tarde"
            else: errs.append({"fila": nf, "motivo": f"Turno '{tur}'"}); continue
            vistos[dni] = nf
            val.append({"dni": dni, "nombres": nom, "apellido_paterno": ap, "apellido_materno": am,
                        "grado": gr, "seccion": sec, "turno": tn, "apoderado_nombre": an, "apoderado_telefono": at})
        except (KeyError, ValueError, TypeError) as e:
            errs.append({"fila": nf, "motivo": f"Error: {e}"})
    return val, errs, {"total": len(df), "validas": len(val), "errores": len(errs)}

def insertar_alumnos_validos(val):
    con = obtener_conexion(); cur = con.cursor()
    mapa_t = {f["nombre"]: f["id"] for f in cur.execute("SELECT id,nombre FROM turnos").fetchall()}
    per = obtener_periodo_activo()
    if not per: return 0, 0, ["No hay periodo activo."]
    pid = per["id"]; ins = 0; reac = 0; errs = []
    for i, d in enumerate(val):
        try:
            fg = cur.execute("SELECT id FROM grados WHERE nombre=?", (d["grado"],)).fetchone()
            if not fg: errs.append(f"Fila {i+1}: grado no reconocido"); continue
            it = mapa_t.get(d["turno"])
            if not it: errs.append(f"Fila {i+1}: turno no encontrado"); continue
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
        except sqlite3.Error as e: errs.append(f"Fila {i+1}: {e}")
    con.commit()
    return ins, reac, errs


def alumno_bloqueado(idal):
    con = obtener_conexion()
    f = con.execute("SELECT * FROM bloqueos WHERE alumno_id=? AND activo=1 ORDER BY id DESC LIMIT 1", (idal,)).fetchone()
    return dict(f) if f else None

def crear_bloqueo(idal, motivo, usuario, origen="automatico"):
    con = obtener_conexion()
    con.execute("INSERT INTO bloqueos(alumno_id,motivo,activo,fecha_inicio,creado_por,origen) VALUES(?,?,1,?,?,?)",
                (idal, motivo, hoy_str(), usuario["usuario"], origen)); con.commit()
    auditar(usuario["usuario"], f"Bloqueo {origen} alumno_id={idal}", tb="bloqueos", rid=idal)

def liberar_bloqueo(idal, usuario, obs=""):
    con = obtener_conexion()
    con.execute("UPDATE bloqueos SET activo=0,fecha_fin=?,liberado_por=? WHERE alumno_id=? AND activo=1",
                (timestamp_str(), usuario["usuario"], idal)); con.commit()
    auditar(usuario["usuario"], f"Libero bloqueo alumno_id={idal}. Obs: {obs}", tb="bloqueos", rid=idal)


def contar_tardanzas_injustificadas(idal, pid=None):
    con = obtener_conexion()
    q = "SELECT COUNT(*) FROM tardanzas WHERE alumno_id=? AND justificada=0"; p = [idal]
    if pid is not None: q += " AND periodo_id=?"; p.append(pid)
    r = con.execute(q, p).fetchone()
    return r[0] or 0

def _aplicar_just_prev(con, idal, fecha):
    f = con.execute("SELECT id FROM justificaciones_previas WHERE alumno_id=? AND fecha_objetivo=? AND aplicada=0 LIMIT 1",
                    (idal, fecha)).fetchone()
    if f:
        con.execute("UPDATE justificaciones_previas SET aplicada=1 WHERE id=?", (f["id"],))
        return True
    return False

def _aplicar_permiso(con, idal, fecha):
    f = con.execute("SELECT id FROM permisos WHERE alumno_id=? AND fecha_objetivo=? AND aplicada=0 LIMIT 1",
                    (idal, fecha)).fetchone()
    if f:
        con.execute("UPDATE permisos SET aplicada=1 WHERE id=?", (f["id"],))
        return True
    return False

def registrar_entrada(dni, usuario):
    dni = (dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni): return False, "ERROR", "DNI invalido", {}
    con = obtener_conexion()
    al = _buscar_alumno_por_dni(con, dni)
    if not al: return False, "ERROR", "DNI no encontrado", {}
    bloq = alumno_bloqueado(al["id"])
    if bloq:
        auditar(usuario["usuario"], f"Intento escaneo bloqueado DNI {dni}", tb="bloqueos", rid=al["id"])
        return False, "BLOQUEADO", f"{_nombre_completo(al)} | BLOQUEADO - retener y llevar a TOECE", {"alumno": al, "motivo": bloq["motivo"]}
    fecha = hoy_str(); ha = hora_corta(); hc = hora_str()
    per = obtener_periodo_activo(); pid = per["id"] if per else None
    dia = dia_especial_hoy(al["turno_id"], fecha, al["seccion_id"])
    if dia and dia["tipo"] == "feriado": return False, "ERROR", "Hoy es feriado, no se registra asistencia", {}
    v = ventana_activa_para_alumno(al["turno_id"], fecha, al["seccion_id"])
    if not v: return False, "ERROR", f"Sin ventana activa ({ha})", {}
    ex = con.execute("SELECT id,estado FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo=?", (al["id"], fecha, v["tipo"])).fetchone()
    if ex: return False, "ERROR", f"{_nombre_completo(al)} ya registro {v['tipo']} hoy ({ex['estado']})", {}
    if v["tipo"] == VENT_REF:
        con.execute("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,periodo_id) VALUES(?,?,?,'reforzamiento',?,?,?)",
                    (al["id"], fecha, v["id"], hc, REF_ASISTIO, pid))
        if al["turno"] == "Tarde":
            ya = con.execute("SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo='clases'", (al["id"], fecha)).fetchone()
            if not ya:
                con.execute("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,periodo_id) VALUES(?,?,NULL,'clases',?,'Puntual',?)",
                            (al["id"], fecha, hc, pid))
        con.commit()
        auditar(usuario["usuario"], f"Reforzamiento DNI {dni}", tb="asistencias")
        msg = (f"{_nombre_completo(al)} | {al['grado']} {al['seccion']} | Reforzamiento + Clases Puntual {ha}"
               if al["turno"] == "Tarde" else
               f"{_nombre_completo(al)} | {al['grado']} {al['seccion']} | Asistio a reforzamiento {ha}")
        return True, "REFORZAMIENTO", msg, {"alumno": al}
    if al["turno"] == "Tarde":
        ya = con.execute("SELECT id,estado,hora FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo='clases'", (al["id"], fecha)).fetchone()
        if ya: return False, "ERROR", f"{_nombre_completo(al)} ya tiene clases registradas hoy ({ya['estado']} {ya['hora']}).", {}
    lim = v["hora_limite_efectiva"]
    est = PUNTUAL if ha <= lim else TARDANZA
    jp = _aplicar_just_prev(con, al["id"], fecha)
    pm = _aplicar_permiso(con, al["id"], fecha)
    just = 1 if (jp or pm) else 0
    con.execute("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,periodo_id) VALUES(?,?,?,'clases',?,?,?,?)",
                (al["id"], fecha, v["id"], hc, est, just, pid))
    if est == TARDANZA:
        n = contar_tardanzas_injustificadas(al["id"], pid) + 1
        acc = ACC_PERDONADO if n <= 2 else (ACC_DERIVADO if n == 3 else ACC_RETENIDO)
        con.execute("INSERT INTO tardanzas(alumno_id,fecha,hora,numero,accion,justificada,registrado_por,timestamp,periodo_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (al["id"], fecha, hc, n, acc, 0, usuario["usuario"], timestamp_str(), pid))
        if n >= 4 and not alumno_bloqueado(al["id"]):
            crear_bloqueo(al["id"], f"{n}ta tardanza injustificada ({fecha})", usuario, "automatico")
        con.commit()
        auditar(usuario["usuario"], f"Tardanza {n}a DNI {dni} -> {acc}", tb="tardanzas")
        return True, "TARDANZA", f"{_nombre_completo(al)} | {al['grado']} {al['seccion']} | Tardanza {n}a ({acc}) {ha}", {"alumno": al, "numero": n, "accion": acc}
    con.commit()
    auditar(usuario["usuario"], f"Entrada Puntual DNI {dni}", tb="asistencias")
    return True, "PUNTUAL", f"{_nombre_completo(al)} | {al['grado']} {al['seccion']} | Puntual {ha}", {"alumno": al}

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
                if not con.execute("SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo=?", (al["id"], fecha, tipo)).fetchone():
                    jp = _aplicar_just_prev(con, al["id"], fecha)
                    pm = _aplicar_permiso(con, al["id"], fecha)
                    just = 1 if (jp or pm) else 0
                    con.execute("INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,estado,justificada,periodo_id) VALUES(?,?,?,?,?,?,?,?)",
                                (al["id"], fecha, v["id"], tipo, hora_str(), est, just, pid))
    con.commit()

def justificar_asistencia(ida, just, obs, usuario):
    con = obtener_conexion()
    if not con.execute("SELECT * FROM asistencias WHERE id=?", (ida,)).fetchone(): return False, "Registro no encontrado."
    con.execute("UPDATE asistencias SET justificada=?,observacion=? WHERE id=?", (1 if just else 0, obs, ida)); con.commit()
    auditar(usuario["usuario"], f"{'Justifico' if just else 'Quito justif.'} id={ida}", tb="asistencias", rid=ida)
    return True, "Asistencia actualizada correctamente."

def crear_justificacion_previa(idal, fecha_obj, tipo, motivo, usuario):
    con = obtener_conexion()
    hoy_dt = ahora().date()
    fecha_obj_dt = datetime.strptime(fecha_obj, "%Y-%m-%d").date()
    dif = (fecha_obj_dt - hoy_dt).days
    if dif > 2: return False, "Solo se puede justificar con un maximo de 48 horas de anticipacion."
    if dif < -1: return False, "Solo se puede justificar hasta 24 horas despues del dia."
    if dif == 0:
        al = con.execute("SELECT a.id, s.turno_id, a.seccion_id FROM alumnos a JOIN secciones s ON a.seccion_id=s.id WHERE a.id=?", (idal,)).fetchone()
        if not al: return False, "Alumno no encontrado."
        ventana = ventana_activa_para_alumno(al["turno_id"], fecha_obj, al["seccion_id"])
        if not ventana:
            return False, "Si justificas para HOY, debe ser dentro de la ventana de clases."
    try:
        con.execute("INSERT INTO justificaciones_previas(alumno_id,fecha_objetivo,tipo,motivo,creado_por,timestamp) VALUES(?,?,?,?,?,?)",
                    (idal, fecha_obj, tipo, motivo, usuario["usuario"], timestamp_str())); con.commit()
        auditar(usuario["usuario"], f"Justif. {tipo} {fecha_obj} id={idal}", tb="justificaciones_previas", rid=idal)
        return True, "Justificacion registrada correctamente."
    except sqlite3.IntegrityError:
        return False, "Ya existe una justificacion para ese dia y tipo."

def crear_permiso(idal, fecha_obj, motivo, usuario):
    con = obtener_conexion()
    hoy_dt = ahora().date()
    fecha_obj_dt = datetime.strptime(fecha_obj, "%Y-%m-%d").date()
    dif = (fecha_obj_dt - hoy_dt).days
    if dif > 2: return False, "Solo se puede registrar un permiso con un maximo de 48 horas de anticipacion."
    if dif < -1: return False, "Solo se puede registrar un permiso hasta 24 horas despues del dia."
    if dif == 0:
        al = con.execute("SELECT a.id, s.turno_id, a.seccion_id FROM alumnos a JOIN secciones s ON a.seccion_id=s.id WHERE a.id=?", (idal,)).fetchone()
        if not al: return False, "Alumno no encontrado."
        ventana = ventana_activa_para_alumno(al["turno_id"], fecha_obj, al["seccion_id"])
        if not ventana:
            return False, "Si registras un permiso para HOY, debe ser dentro de la ventana de clases."
    per = obtener_periodo_activo(); pid = per["id"] if per else None
    try:
        con.execute("INSERT INTO permisos(alumno_id,fecha_objetivo,motivo,creado_por,timestamp,periodo_id) VALUES(?,?,?,?,?,?)",
                    (idal, fecha_obj, motivo, usuario["usuario"], timestamp_str(), pid)); con.commit()
        auditar(usuario["usuario"], f"Permiso {fecha_obj} id={idal}", tb="permisos", rid=idal)
        return True, "Permiso registrado correctamente."
    except sqlite3.IntegrityError:
        return False, "Ya existe un permiso para ese dia."

def _procesar_escaneo(dni):
    u = st.session_state.get("user")
    if not u: return
    _, tipo, msg, extra = registrar_entrada(dni, u)
    st.session_state.setdefault("_qr_mensajes", [])
    st.session_state["_qr_mensajes"].insert(0, {"dni": dni, "tipo": tipo, "mensaje": msg, "extra": extra, "ts": time.time()})
    st.session_state["_qr_mensajes"] = st.session_state["_qr_mensajes"][:10]

def _render_mensaje_qr(msg):
    tipo = msg["tipo"]; mensaje = msg["mensaje"]
    iconos = {"PUNTUAL":"&#10004;","TARDANZA":"&#9200;","REFORZAMIENTO":"&#128218;","BLOQUEADO":"&#9888;","ERROR":"&#10006;"}
    ic = iconos.get(tipo, "&#10006;")
    clase = {"PUNTUAL":"qr-puntual","TARDANZA":"qr-tardanza","REFORZAMIENTO":"qr-refuerzo","BLOQUEADO":"qr-bloqueado","ERROR":"qr-error"}.get(tipo, "qr-error")
    if tipo == "TARDANZA":
        acc = (msg.get("extra") or {}).get("accion")
        if acc == ACC_DERIVADO: mensaje += " &rarr; Derivar a TOECE"; clase = "qr-derivado"
        elif acc == ACC_RETENIDO: mensaje += " &rarr; Retener hasta apoderado"; clase = "qr-retenido"
    st.markdown(f'<div class="qr-msg {clase}"><div class="qr-icono">{ic}</div><div class="qr-texto">{mensaje}</div></div>', unsafe_allow_html=True)

def escaner_qr_continuo(key="qr_scanner"):
    st.markdown('<div class="scan-header"><div class="scan-titulo">Escaneo QR</div><div class="scan-sub">Apunta al codigo del alumno</div></div>', unsafe_allow_html=True)
    qr_code = qrcode_scanner(key=f"qr_{key}")
    if qr_code:
        m = re.search(r"\b(\d{8})\b", str(qr_code))
        if m:
            dni = m.group(1)
            ult = st.session_state.get("_ultimo_qr_scan", {})
            if not (ult.get("dni") == dni and (time.time() - ult.get("ts", 0)) < 3):
                st.session_state["_ultimo_qr_scan"] = {"dni": dni, "ts": time.time()}
                _procesar_escaneo(dni)
    if st.session_state.get("_qr_mensajes"):
        st.markdown('<div class="scan-ultimos">Ultimos escaneos</div>', unsafe_allow_html=True)
        for msg in st.session_state["_qr_mensajes"][:5]:
            _render_mensaje_qr(msg)

  # ---------- REPORTES ----------
def metricas_dia(fecha, pid=None):
    if pid is None:
        p = obtener_periodo_activo(); pid = p["id"] if p else None
    con = obtener_conexion()
    filt = " AND periodo_id=?" if pid is not None else ""
    base = [pid] if pid is not None else []
    f = con.execute(f"""
        SELECT (SELECT COUNT(*) FROM alumnos WHERE activo=1) AS total,
        (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND tipo='clases' AND estado='Puntual'{filt}) AS puntuales,
        (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND tipo='clases' AND estado='Falta'{filt}) AS faltas,
        (SELECT COUNT(*) FROM tardanzas WHERE fecha=?{filt}) AS tardanzas,
        (SELECT COUNT(*) FROM asistencias WHERE fecha=? AND tipo='reforzamiento' AND estado='Asistio'{filt}) AS ref_asistio,
        (SELECT COUNT(*) FROM bloqueos WHERE activo=1) AS bloqueados
        """, base+[fecha]+base+[fecha]+base+[fecha]+base+[fecha]).fetchone()
    return dict(f)

def ultimos_registros(fecha, limite=20):
    return pd.read_sql("SELECT a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,ast.tipo,ast.hora,ast.estado FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE ast.fecha=? AND ast.hora IS NOT NULL ORDER BY ast.hora DESC LIMIT ?",
                       obtener_conexion(), params=[fecha, limite])

def reporte_detalle(inicio, fin, turno, idg=None, idsec=None, texto="", tipo="clases", ids_sec=None, pid=None):
    con = obtener_conexion()
    q = ("SELECT g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
         "ast.fecha, ast.hora, ast.tipo, ast.estado, COUNT(*) AS cantidad "
         "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id "
         "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
         "WHERE ast.fecha BETWEEN ? AND ? AND ast.hora IS NOT NULL")
    p = [inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")]
    if tipo != "todas": q += " AND ast.tipo=?"; p.append(tipo)
    if turno != "Todos": q += " AND t.nombre=?"; p.append(turno)
    if idg: q += " AND g.id=?"; p.append(idg)
    if idsec: q += " AND s.id=?"; p.append(idsec)
    if ids_sec:
        q += " AND s.id IN (" + ",".join(["?"]*len(ids_sec)) + ")"; p += ids_sec
    if pid is not None: q += " AND ast.periodo_id=?"; p.append(pid)
    if texto:
        for w in [x.strip() for x in texto.split() if x.strip()]:
            q += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
            pat = f"%{w}%"; p += [pat, pat, pat]
    q += " GROUP BY g.nombre, s.nombre, t.nombre, ast.fecha, ast.hora, ast.tipo, ast.estado ORDER BY ast.fecha DESC, ast.hora DESC"
    return pd.read_sql(q, con, params=p)

def reporte_conteo_faltas(inicio, fin, turno, idg=None, idsec=None, texto="", ids_sec=None, pid=None):
    con = obtener_conexion()
    q = ("SELECT g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
         "COUNT(DISTINCT a.id) AS total_alumnos, "
         "SUM(CASE WHEN ast.justificada=1 THEN 1 ELSE 0 END) AS faltas_just, "
         "SUM(CASE WHEN ast.justificada=0 THEN 1 ELSE 0 END) AS faltas_injust, COUNT(*) AS total_faltas "
         "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id "
         "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
         "WHERE ast.estado='Falta' AND ast.tipo='clases' AND ast.fecha BETWEEN ? AND ?")
    p = [inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")]
    if turno != "Todos": q += " AND t.nombre=?"; p.append(turno)
    if idg: q += " AND g.id=?"; p.append(idg)
    if idsec: q += " AND s.id=?"; p.append(idsec)
    if ids_sec:
        q += " AND s.id IN (" + ",".join(["?"]*len(ids_sec)) + ")"; p += ids_sec
    if pid is not None: q += " AND ast.periodo_id=?"; p.append(pid)
    if texto:
        for w in [x.strip() for x in texto.split() if x.strip()]:
            q += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
            pat = f"%{w}%"; p += [pat, pat, pat]
    q += " GROUP BY g.nombre, s.nombre, t.nombre ORDER BY t.nombre, g.nombre, s.nombre"
    return pd.read_sql(q, con, params=p)

def _estado_a_letra(estado):
    if estado == "Puntual": return "P"
    if estado == "Falta": return "F"
    if estado == "Tardanza": return "T"
    if estado == "Asistio": return "P"
    if estado == "No asistio": return "F"
    return "?"

def _matriz_asistencia(inicio, fin, turno, ids_sec, pid, tipo_asist):
    con = obtener_conexion()
    q = ("SELECT a.id, a.apellido_paterno, a.apellido_materno, a.nombres, "
         "g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
         "ast.fecha, ast.estado "
         "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id "
         "JOIN secciones s ON a.seccion_id=s.id "
         "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
         "WHERE ast.fecha BETWEEN ? AND ?")
    p = [inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")]
    if tipo_asist != "todas": q += " AND ast.tipo=?"; p.append(tipo_asist)
    if turno != "Todos": q += " AND t.nombre=?"; p.append(turno)
    if ids_sec:
        q += " AND s.id IN (" + ",".join(["?"]*len(ids_sec)) + ")"; p += ids_sec
    if pid is not None: q += " AND ast.periodo_id=?"; p.append(pid)
    q += " ORDER BY t.nombre, g.nombre, s.nombre, a.apellido_paterno"
    return pd.read_sql(q, con, params=p)

def reporte_diario(desde, hasta, turno, ids_sec, pid, tipo_asist):
    df = _matriz_asistencia(desde, hasta, turno, ids_sec, pid, tipo_asist)
    if df.empty: return pd.DataFrame()
    df["letra"] = df["estado"].apply(_estado_a_letra)
    pivot = df.pivot_table(
        index=["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno"],
        columns="fecha", values="letra", aggfunc="first"
    ).reset_index()
    pivot.columns.name = None
    return pivot

def reporte_mensual(desde, hasta, turno, ids_sec, pid, tipo_asist):
    df = _matriz_asistencia(desde, hasta, turno, ids_sec, pid, tipo_asist)
    if df.empty: return pd.DataFrame()
    df["letra"] = df["estado"].apply(_estado_a_letra)
    resumen = df.groupby(["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno"])["letra"].apply(
        lambda x: " ".join(sorted(x))
    ).reset_index()
    resumen.columns = ["Apellido Paterno", "Apellido Materno", "Nombres", "Grado", "Seccion", "Turno", "Asistencia"]
    return resumen

def reporte_bimestral(desde, hasta, turno, ids_sec, pid, tipo_asist):
    df = _matriz_asistencia(desde, hasta, turno, ids_sec, pid, tipo_asist)
    if df.empty: return pd.DataFrame()
    df["letra"] = df["estado"].apply(_estado_a_letra)
    resumen = df.groupby(["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno", "letra"]).size().unstack(fill_value=0).reset_index()
    resumen.columns.name = None
    for col in ["P", "F", "T"]:
        if col not in resumen.columns:
            resumen[col] = 0
    resumen = resumen.rename(columns={"apellido_paterno": "Apellido Paterno", "apellido_materno": "Apellido Materno",
                                        "nombres": "Nombres", "grado": "Grado", "seccion": "Seccion",
                                        "turno": "Turno", "P": "Puntuales", "F": "Faltas", "T": "Tardanzas"})
    return resumen[["Apellido Paterno", "Apellido Materno", "Nombres", "Grado", "Seccion", "Turno", "Puntuales", "Faltas", "Tardanzas"]]


def casos_toece(pid=None):
    con = obtener_conexion()
    filt = ""; p = []
    if pid is not None: filt = " AND t2.periodo_id=?"; p.append(pid)
    return pd.read_sql(f"""
        SELECT a.id,a.dni,a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,a.nombres,
        g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,a.nombre_apoderado,a.telefono_apoderado,
        COUNT(t2.id) AS tard_injust,
        (SELECT COUNT(*) FROM actas_compromiso WHERE alumno_id=a.id) AS total_actas,
        CASE WHEN EXISTS(SELECT 1 FROM bloqueos WHERE alumno_id=a.id AND activo=1) THEN 'SI' ELSE 'NO' END AS bloqueado
        FROM tardanzas t2 JOIN alumnos a ON t2.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id
        JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
        WHERE t2.justificada=0 {filt}
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

def listar_permisos(solo_activos=True):
    con = obtener_conexion()
    q = ("SELECT p.id, a.id AS alumno_id, a.dni, "
         "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS alumno, "
         "g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
         "p.fecha_objetivo, COALESCE(p.motivo,'') AS motivo, p.aplicada, p.timestamp "
         "FROM permisos p JOIN alumnos a ON p.alumno_id=a.id "
         "JOIN secciones s ON a.seccion_id=s.id "
         "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id")
    if solo_activos: q += " WHERE p.aplicada=0"
    q += " ORDER BY p.fecha_objetivo DESC"
    return pd.read_sql(q, con)

def obtener_auditoria(limite=500):
    return pd.read_sql(f"SELECT id,usuario,accion,fecha,ip FROM auditoria ORDER BY id DESC LIMIT {int(limite)}",
                       obtener_conexion())


def perfil_alumno_datos(idal):
    con = obtener_conexion()
    a = con.execute("SELECT a.*,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,s.id AS seccion_id,t.id AS turno_id FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE a.id=?",
                    (idal,)).fetchone()
    if not a: return {}
    a = dict(a)
    da = pd.read_sql("SELECT fecha,hora,tipo,estado,justificada,COALESCE(observacion,'') AS observacion FROM asistencias WHERE alumno_id=? ORDER BY fecha DESC,hora DESC", con, params=[idal])
    dt = pd.read_sql("SELECT fecha,hora,numero AS 'N',accion,justificada,COALESCE(observacion,'') AS observacion FROM tardanzas WHERE alumno_id=? ORDER BY fecha DESC,hora DESC", con, params=[idal])
    do = pd.read_sql("SELECT fecha_ingreso,COALESCE(fecha_salida,'-') AS fecha_salida,COALESCE(motivo,'') AS motivo,activo FROM observados WHERE alumno_id=? ORDER BY fecha_ingreso DESC", con, params=[idal])
    dac = pd.read_sql("SELECT fecha,COALESCE(motivo,'') AS motivo,COALESCE(observacion,'') AS observacion,COALESCE(registrado_por,'') AS registrado_por FROM actas_compromiso WHERE alumno_id=? ORDER BY fecha DESC", con, params=[idal])
    db = pd.read_sql("SELECT fecha_inicio,COALESCE(fecha_fin,'-') AS fecha_fin,COALESCE(motivo,'') AS motivo,activo FROM bloqueos WHERE alumno_id=? ORDER BY fecha_inicio DESC", con, params=[idal])
    dj = pd.read_sql("SELECT fecha_objetivo,tipo,COALESCE(motivo,'') AS motivo,aplicada,timestamp FROM justificaciones_previas WHERE alumno_id=? ORDER BY fecha_objetivo DESC", con, params=[idal])
    dp = pd.read_sql("SELECT fecha_objetivo,COALESCE(motivo,'') AS motivo,aplicada,timestamp FROM permisos WHERE alumno_id=? ORDER BY fecha_objetivo DESC", con, params=[idal])
    tp = int((da["estado"] == PUNTUAL).sum()) if not da.empty else 0
    tf = int((da["estado"] == FALTA).sum()) if not da.empty else 0
    tt = int((da["estado"] == TARDANZA).sum()) if not da.empty else 0
    tr = int(((da["tipo"] == "reforzamiento") & (da["estado"] == REF_ASISTIO)).sum()) if not da.empty else 0
    return {"alumno": a, "asistencias": da, "tardanzas": dt, "observados": do, "actas": dac,
            "bloqueos": db, "just_previas": dj, "permisos": dp, "total_puntuales": tp, "total_faltas": tf,
            "total_tardanzas": tt, "total_ref_asistio": tr,
            "tard_injust": contar_tardanzas_injustificadas(idal),
            "bloqueado": alumno_bloqueado(idal) is not None}


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
        res[f"{cur.year}-{cur.month:02d}"] = df
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
        con.execute("INSERT INTO cierres_anuales(periodo_id,fecha_cierre,generado_por,reporte_json) VALUES(?,?,?,?)",
                    (pid, fecha, usuario["usuario"], rep_json))
        con.execute("UPDATE periodos SET activo=0,fecha_cierre=?,cerrado=1 WHERE id=?", (fecha, pid))
        con.execute("UPDATE alumnos SET activo=0,retirado_en=? WHERE periodo_id=?", (fecha, pid))
        con.execute("INSERT INTO periodos(nombre,fecha_inicio,fecha_fin,activo,cerrado) VALUES(?,?,?,1,0)", (nuevo_nombre, fi, ff))
        con.commit()
    except sqlite3.Error as e:
        con.rollback(); log.error("cerrar anio: %s", e); return False, "Error al cerrar el anio."
    auditar(usuario["usuario"], f"Cerro periodo {p['nombre']}", tb="periodos", rid=pid)
    return True, f"Periodo '{p['nombre']}' cerrado. Nuevo: '{nuevo_nombre}'."

def listar_cierres_anuales():
    return pd.read_sql("SELECT c.id,p.nombre AS periodo,c.fecha_cierre,c.generado_por FROM cierres_anuales c JOIN periodos p ON c.periodo_id=p.id ORDER BY c.id DESC",
                       obtener_conexion())


def _pdf_base(titulo, subtitulo=None, paisaje=False):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(A4[1],A4[0]) if paisaje else A4, rightMargin=25, leftMargin=25, topMargin=25, bottomMargin=25)
    est = getSampleStyleSheet()
    el = [Paragraph(f"<b>{titulo}</b>", est["Heading1"])]
    if subtitulo: el.append(Paragraph(subtitulo, est["Normal"]))
    el.append(Paragraph(f"Generado: {ahora().strftime('%Y-%m-%d %H:%M')}", est["Normal"]))
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
        titulo = f"Carnets - {filas[0]['grado']} {filas[0]['seccion']}" if len(filas) > 1 else f"Carnet - {filas[0]['apellido_paterno']} {filas[0]['apellido_materno'] or ''}, {filas[0]['nombres']}"
    el = [Paragraph(titulo, est["Heading1"]), Spacer(1, 6)]
    cols, rows = 3, 3; pp = cols*rows
    aw = (A4[0]-2*m)/cols; ah = (A4[1]-2*m-50)/rows
    for i in range(0, len(filas), pp):
        lote = filas[i:i+pp]; tabla = []
        for j in range(0, len(lote), cols):
            fila = []
            for a in lote[j:j+cols]:
                qb = BytesIO(); generar_qr(a["dni"]).save(qb, format="PNG"); qb.seek(0)
                fila.append([Paragraph(f"<b><font size=11>{a['apellido_paterno']} {a['apellido_materno'] or ''}</font></b>", est["Normal"]),
                             Paragraph(f"<font size=10>{a['nombres']}</font>", est["Normal"]),
                             Paragraph(f"<font size=9>{a['grado']} {a['seccion']} - {a['turno']}</font>", est["Normal"]),
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
    f = con.execute(f"SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE a.id IN ({ph}) AND a.activo=1 ORDER BY a.apellido_paterno,a.apellido_materno",
                    ids).fetchall()
    return _render_carnets(f, titulo) if f else None

def pdf_carnet_alumno(dni):
    con = obtener_conexion()
    f = con.execute("SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id WHERE a.dni=? AND a.activo=1",
                    (dni,)).fetchall()
    return _render_carnets(f) if f else None

def df_a_xlsx(df, hoja="Datos"):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w: df.to_excel(w, index=False, sheet_name=hoja)
    buf.seek(0); return buf.getvalue()

def df_a_xlsx_multilhoja(hojas):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for n, df in hojas.items(): df.to_excel(w, index=False, sheet_name=n[:31])
    buf.seek(0); return buf.getvalue()


def aplicar_estilos():
    st.markdown("""
    <style>
    html, body, [class*="css"] {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif !important;
    }
    h1, h2, h3, h4 { font-weight: 700 !important; }
    h1 { font-size: 1.9rem !important; }
    h2 { font-size: 1.4rem !important; }
    h3 { font-size: 1.15rem !important; }

    section[data-testid="stSidebar"] .stRadio label {
        border-radius: 6px !important;
        padding: 10px 12px !important;
        font-weight: 500 !important;
        font-size: 14px !important;
    }
    section[data-testid="stSidebar"] .stRadio label:hover {
        background: rgba(128,128,128,0.15) !important;
    }
    section[data-testid="stSidebar"] .stRadio label:has(input:checked) {
        background: rgba(128,128,128,0.25) !important;
        font-weight: 600 !important;
    }

    .encabezado-sidebar {
        border: 1px solid rgba(128,128,128,0.3);
        padding: 16px 14px;
        margin: 10px 8px;
        border-radius: 8px;
        text-align: center;
    }
    .encabezado-sidebar .avatar {
        width: 52px; height: 52px; border-radius: 50%;
        background: #808080; color: #FFFFFF;
        display: flex; align-items: center; justify-content: center;
        font-size: 20px; font-weight: 700;
        margin: 0 auto 10px auto;
    }
    .encabezado-sidebar .nombre { font-size: 14px; font-weight: 700; }
    .encabezado-sidebar .rol {
        display: inline-block; margin-top: 6px; padding: 3px 10px;
        background: #808080; color: #FFFFFF; border-radius: 10px;
        font-size: 10px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 0.08em;
    }

    .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
        background: #808080 !important;
        color: #FFFFFF !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        border: 1px solid #808080 !important;
        padding: 11px 20px !important;
        font-size: 14px !important;
        min-height: 44px;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover, .stDownloadButton > button:hover {
        background: #666666 !important; border-color: #666666 !important;
    }
    .stButton > button[kind="secondary"], .stDownloadButton > button {
        background: transparent !important;
        color: inherit !important;
        border: 1px solid #808080 !important;
    }
    .stButton > button[kind="secondary"]:hover, .stDownloadButton > button:hover {
        background: #808080 !important; color: #FFFFFF !important;
    }

    div[data-testid="stMetric"] {
        border: 1px solid rgba(128,128,128,0.3);
        border-radius: 8px;
        padding: 18px 20px !important;
    }
    div[data-testid="stMetric"] label { font-size: 11px !important; opacity: 0.7; text-transform: uppercase; }

    .stTextInput input, .stNumberInput input, .stDateInput input,
    .stTimeInput input, .stTextArea textarea, .stSelectbox > div > div {
        border-radius: 6px !important; min-height: 44px;
    }

    .scan-header {
        border: 1px solid rgba(128,128,128,0.3);
        padding: 20px 22px; border-radius: 10px; margin-bottom: 16px;
    }
    .scan-header .scan-titulo { font-size: 22px; font-weight: 700; }
    .scan-header .scan-sub { font-size: 13px; opacity: 0.75; margin-top: 4px; }
    .scan-ultimos {
        font-size: 11px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 0.12em; margin: 20px 0 12px 0;
        padding-bottom: 6px; border-bottom: 1px solid rgba(128,128,128,0.3);
    }
    .qr-msg {
        display: flex; align-items: center; gap: 14px;
        padding: 14px 16px; border-radius: 6px; margin: 8px 0;
        border: 1px solid rgba(128,128,128,0.3);
        border-left: 3px solid #808080;
    }
    .qr-icono {
        font-size: 18px; width: 34px; height: 34px;
        display: flex; align-items: center; justify-content: center;
        border-radius: 50%; border: 1px solid rgba(128,128,128,0.4);
        flex-shrink: 0; font-weight: 700;
    }
    .qr-texto { flex: 1; font-size: 14px; }
    .qr-puntual { border-left-color: #66BB6A; } .qr-puntual .qr-icono { color: #66BB6A; }
    .qr-tardanza { border-left-color: #FFB74D; } .qr-tardanza .qr-icono { color: #FFB74D; }
    .qr-derivado { border-left-color: #FF9800; } .qr-derivado .qr-icono { color: #FF9800; }
    .qr-retenido { border-left-color: #FF7043; font-weight: 700; } .qr-retenido .qr-icono { color: #FF7043; }
    .qr-refuerzo { border-left-color: #42A5F5; } .qr-refuerzo .qr-icono { color: #42A5F5; }
    .qr-bloqueado { border-left-color: #EF5350; border: 2px solid #EF5350; }
    .qr-bloqueado .qr-icono { color: #EF5350; border-color: #EF5350; }
    .qr-error { border-left-color: #888888; } .qr-error .qr-icono { color: #888888; }

    .perfil-card {
        border: 1px solid rgba(128,128,128,0.3);
        border-radius: 10px; padding: 24px 26px;
        margin-bottom: 18px; border-top: 4px solid #808080;
    }
    .perfil-nombre { font-size: 22px; font-weight: 700; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .perfil-meta { font-size: 13px; margin-top: 8px; opacity: 0.8; }
    .perfil-badge {
        display: inline-block; padding: 4px 12px; border-radius: 10px;
        font-size: 10px; font-weight: 700; text-transform: uppercase;
        background: #808080; color: #FFFFFF;
    }
    .badge-bloqueado { background: #EF5350; color: #FFFFFF; }
    .badge-observado { background: #FFB74D; color: #0A0A0A; }
    .badge-ok { background: #808080; color: #FFFFFF; }
    .perfil-resumen {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
        gap: 12px; margin-top: 18px;
    }
    .perfil-resumen-item {
        border: 1px solid rgba(128,128,128,0.3);
        border-radius: 8px; padding: 14px 10px; text-align: center;
    }
    .perfil-resumen-item .num { font-size: 22px; font-weight: 700; }
    .perfil-resumen-item .lbl { font-size: 10px; text-transform: uppercase; opacity: 0.7; }

    .login-card {
        border: 1px solid rgba(128,128,128,0.3);
        border-radius: 10px; padding: 36px 30px;
        border-top: 4px solid #808080;
    }

    hr { border: none; height: 1px; background: rgba(128,128,128,0.3); margin: 20px 0; }

    @media (max-width: 768px) {
        h1 { font-size: 1.4rem !important; }
        h2 { font-size: 1.15rem !important; }
        .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
            width: 100% !important;
        }
        .perfil-resumen { grid-template-columns: repeat(2, 1fr); }
    }
    </style>
    """, unsafe_allow_html=True)

def color_estado(valor):
    if valor in (PUNTUAL, REF_ASISTIO): return f"background-color:{C_VERDE_BG};color:{C_VERDE_TX};font-weight:bold"
    if valor == TARDANZA: return f"background-color:{C_AMAR_BG};color:{C_AMAR_TX};font-weight:bold"
    if valor in (FALTA, REF_NO_ASISTIO): return f"background-color:{C_ROJO_BG};color:{C_ROJO_TX};font-weight:bold"
    return ""

def filtros_grado_seccion_nombre(clave, placeholder="Buscar"):
    grados = listar_grados()
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        ops = [{"id": None, "nombre": "Todos"}] + grados
        g = st.selectbox("Grado", ops, format_func=lambda x: x["nombre"], key=f"{clave}_g")
    with c2:
        secs = ([{"id": None, "nombre": "Todas"}] + secciones_por_grado(g["id"])) if (g and g["id"]) else [{"id": None, "nombre": "Todas"}]
        s = st.selectbox("Seccion", secs, format_func=lambda x: x["nombre"], key=f"{clave}_s")
    with c3:
        t = st.text_input("Buscar", placeholder=placeholder, key=f"{clave}_t")
    return (g["id"] if g else None, s["id"] if (g and g["id"] and s) else None, t.strip())


def vista_login():
    st.markdown("""
    <div style="text-align:center; margin-top:40px; margin-bottom:20px;">
        <h1 style="font-size:42px; margin-bottom:6px; letter-spacing:-1px;">Sistema de Asistencia</h1>
        <p style="font-size:18px; font-weight:600; margin-top:0; color:#E65100;">I.E. Yarinacocha</p>
    </div>
    """, unsafe_allow_html=True)
    _, centro, _ = st.columns([1, 1.2, 1])
    with centro:
        st.markdown('<div class="login-card">', unsafe_allow_html=True)
        with st.form("login"):
            st.markdown("<h3 style='text-align:center;'>Iniciar Sesion</h3>", unsafe_allow_html=True)
            usuario = st.text_input("Usuario", placeholder="tu.usuario")
            password = st.text_input("Contrasena", type="password", placeholder="********")
            recordarme = st.checkbox("Recordarme en este dispositivo", value=True)
            enviado = st.form_submit_button("Ingresar", type="primary", use_container_width=True)
            if enviado:
                datos, err = autenticar(usuario, password)
                if datos:
                    st.session_state["user"] = datos
                    if recordarme:
                        token = crear_token_sesion(datos)
                        st.session_state["_token"] = token
                        st.session_state["_token_expira"] = time.time() + 300
                        _guardar_cookie(token)
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
            ok = st.form_submit_button("Cambiar", type="primary", use_container_width=True)
        if ok:
            ok_v, msg_v = validar_password(nueva)
            if not ok_v: st.error(msg_v)
            elif nueva != confirmar: st.error("No coinciden.")
            else:
                con = obtener_conexion()
                con.execute("UPDATE usuarios SET password=?,debe_cambiar_password=0 WHERE id=?", (hashear_password(nueva), usuario["id"]))
                con.commit()
                st.session_state["user"]["debe_cambiar_password"] = 0
                auditar(usuario["usuario"], "Cambio pwd obligatorio")
                st.rerun()


def vista_puerta():
    usuario = st.session_state["user"]
    fecha = hoy_str()
    st.markdown(f"""
    <div style="background:#E65100; padding:22px 28px; border-radius:10px; color:white; margin-bottom:20px;">
        <div style="font-size:26px; font-weight:700;">Control de Puerta</div>
        <div style="font-size:14px; opacity:0.9; margin-top:4px;">{fecha}</div>
    </div>
    """, unsafe_allow_html=True)
    con = obtener_conexion()
    esp = con.execute("SELECT descripcion, hora_entrada, tipo FROM dias_especiales WHERE fecha=? AND activo=1 LIMIT 1", (fecha,)).fetchone()
    if esp and esp["tipo"] == "evento":
        st.success(f"Evento escolar: **{esp['descripcion']}** (entrada {esp['hora_entrada']})")
    elif esp and esp["tipo"] == "feriado":
        st.info(f"Feriado / sin clases: **{esp['descripcion']}**.")
        return
    elif es_fin_de_semana():
        st.warning("Hoy no es dia laboral. No se toma asistencia.")
        return
    marcar_faltas_al_cierre()
    ha = hora_corta()
    va = []
    for t in listar_turnos():
        for v in listar_ventanas(t["id"]):
            if v["hora_apertura"] <= ha <= v["hora_cierre"]:
                va.append(f"{t['nombre']}: {v['nombre']}")
    if va:
        st.markdown(f"<div style='background:#FFF3E0; padding:10px 16px; border-radius:8px; border-left:3px solid #E65100; font-weight:600; color:#E65100; margin-bottom:16px;'><b>Ventanas activas:</b> {' | '.join(va)}</div>", unsafe_allow_html=True)
    escaner_qr_continuo(key="puerta_qr")
    if usuario["rol"] == "Admin":
        st.markdown("---")
        with st.expander("Lista manual (solo Admin)"):
            grados = listar_grados()
            if not grados: return
            c1, c2 = st.columns(2)
            with c1:
                g = st.selectbox("Grado", grados, format_func=lambda x: x["nombre"], key="pt_g")
            with c2:
                secs = secciones_por_grado(g["id"]) if g else []
                if not secs:
                    st.warning("Sin secciones"); return
                s = st.selectbox("Seccion", secs, format_func=lambda x: x["nombre"], key="pt_s")
            df = alumnos_de_seccion(s["id"])
            for _, al in df.iterrows():
                c1, c2 = st.columns([5, 1])
                c1.write(al["nombre_completo"])
                if c2.button("Marcar", key=f"m_{al['id']}"):
                    _, _, msg, _ = registrar_entrada(al["dni"], usuario)
                    st.toast(msg)


def vista_toece():
    st.title("TOECE")
    usuario = st.session_state["user"]
    tabs = st.tabs(["Casos activos", "Bloqueados", "Observados", "Firmar acta", "Justificar previo", "Permisos"])
    with tabs[0]:
        df = casos_toece()
        if df.empty: st.info("Sin casos activos.")
        else:
            st.dataframe(df, use_container_width=True)
            st.download_button("Excel", df_a_xlsx(df), "casos_toece.xlsx")
    with tabs[1]:
        st.subheader("Alumnos bloqueados")
        st.caption("Bloqueo automatico a la 4ta tardanza.")
        df = listar_bloqueados()
        if df.empty: st.info("Sin bloqueados.")
        else:
            st.dataframe(df, use_container_width=True)
            ops = {f"{r['alumno']} ({r['dni']}) - {r['motivo']}": r["alumno_id"] for _, r in df.iterrows()}
            sel = st.selectbox("Liberar bloqueo", list(ops.keys()))
            obs = st.text_input("Observacion de liberacion", key="lib_obs")
            if st.button("Liberar bloqueo", type="primary"):
                liberar_bloqueo(ops[sel], usuario, obs)
                st.toast("Bloqueo liberado correctamente"); st.rerun()
        st.markdown("---")
        st.subheader("Bloquear manualmente")
        idg, ids, texto = filtros_grado_seccion_nombre("bloq_man")
        if texto or idg:
            df_b = buscar_alumnos(texto, idg, ids, limite=50)
            if not df_b.empty:
                ops_b = {f"{r['nombre_completo']} ({r['dni']})": r["id"] for _, r in df_b.iterrows()}
                sel_b = st.selectbox("Alumno a bloquear", list(ops_b.keys()), key="bloq_sel")
                mot = st.text_input("Motivo del bloqueo", key="bloq_mot")
                if st.button("Bloquear", type="primary", key="bloq_btn"):
                    if not mot.strip(): st.error("Ingresa un motivo.")
                    else:
                        crear_bloqueo(ops_b[sel_b], mot.strip(), usuario, "manual")
                        st.toast("Alumno bloqueado correctamente"); st.rerun()
    with tabs[2]:
        st.subheader("Alumnos observados")
        solo = st.checkbox("Solo activos", value=True, key="obs_act")
        df = listar_observados(solo_activos=solo)
        if df.empty: st.info("Sin observados.")
        else:
            st.dataframe(df, use_container_width=True)
            st.download_button("Excel", df_a_xlsx(df), "observados.xlsx")
        st.markdown("---")
        st.subheader("Crear observado")
        idg, ids, texto = filtros_grado_seccion_nombre("obs_crear")
        if texto or idg:
            df_a = buscar_alumnos(texto, idg, ids, limite=50)
            if not df_a.empty:
                ops_a = {f"{r['nombre_completo']} ({r['dni']})": r["id"] for _, r in df_a.iterrows()}
                with st.form("form_obs"):
                    sel_a = st.selectbox("Alumno", list(ops_a.keys()))
                    mot = st.text_input("Motivo")
                    sub = st.form_submit_button("Crear observado", type="primary")
                if sub:
                    if not mot.strip(): st.error("Ingresa un motivo.")
                    else:
                        con = obtener_conexion(); per = obtener_periodo_activo(); pid = per["id"] if per else None
                        con.execute("INSERT INTO observados(alumno_id,fecha_ingreso,motivo,activo,periodo_id) VALUES(?,?,?,1,?)",
                                    (ops_a[sel_a], hoy_str(), mot.strip(), pid)); con.commit()
                        auditar(usuario["usuario"], f"Creo observado alumno_id={ops_a[sel_a]}")
                        st.toast("Observado creado correctamente"); st.rerun()
        st.markdown("---")
        st.subheader("Cerrar observado")
        df_act = listar_observados(solo_activos=True)
        if not df_act.empty:
            ops_c = {f"{r['apellidos']}, {r['nombres']} ({r['dni']})": r["id"] for _, r in df_act.iterrows()}
            with st.form("cerrar_obs"):
                sel_c = st.selectbox("Observado a cerrar", list(ops_c.keys()))
                obs_c = st.text_input("Observacion de cierre")
                sub_c = st.form_submit_button("Cerrar observado", type="primary")
            if sub_c:
                con = obtener_conexion()
                con.execute("UPDATE observados SET activo=0,fecha_salida=?,observacion_cierre=? WHERE id=?",
                            (timestamp_str(), obs_c, ops_c[sel_c])); con.commit()
                auditar(usuario["usuario"], f"Cerro observado id={ops_c[sel_c]}")
                st.toast("Observado cerrado correctamente"); st.rerun()
    with tabs[3]:
        st.subheader("Firmar acta de compromiso")
        con = obtener_conexion(); df = casos_toece()
        if df.empty: st.info("Sin casos.")
        else:
            ops = {f"{r['apellidos']}, {r['nombres']} ({r['dni']})": r["id"] for _, r in df.iterrows()}
            with st.form("firmar_acta_form"):
                sel = st.selectbox("Alumno", list(ops.keys()))
                mot = st.text_input("Motivo", value="Reincidencia en tardanzas")
                obs = st.text_area("Observacion")
                sub = st.form_submit_button("Firmar acta", type="primary")
            if sub:
                con.execute("INSERT INTO actas_compromiso(alumno_id,fecha,motivo,observacion,registrado_por,timestamp) VALUES(?,?,?,?,?,?)",
                            (ops[sel], hoy_str(), mot, obs, usuario["usuario"], timestamp_str())); con.commit()
                auditar(usuario["usuario"], f"Firmo acta alumno_id={ops[sel]}")
                st.toast("Acta registrada correctamente"); st.rerun()
    with tabs[4]:
        st.subheader("Justificacion previa")
        st.caption("48 horas antes hasta 24 horas despues.")
        idg, ids, texto = filtros_grado_seccion_nombre("jp")
        df = buscar_alumnos(texto, idg, ids, limite=100)
        if df.empty: st.info("Sin alumnos.")
        else:
            ops = {f"{r['nombre_completo']} ({r['dni']})": r["dni"] for _, r in df.iterrows()}
            with st.form("just_prev_form"):
                sel = st.selectbox("Alumno", list(ops.keys()))
                fo = st.date_input("Fecha a justificar",
                                   value=ahora().date(),
                                   min_value=ahora().date() - timedelta(days=1),
                                   max_value=ahora().date() + timedelta(days=2))
                tipo = st.selectbox("Tipo", ["Falta", "Tardanza"])
                mot = st.text_input("Motivo")
                sub = st.form_submit_button("Registrar", type="primary")
            if sub:
                con = obtener_conexion()
                f = con.execute("SELECT id FROM alumnos WHERE dni=?", (ops[sel],)).fetchone()
                if f:
                    ok, msg = crear_justificacion_previa(f["id"], fo.strftime("%Y-%m-%d"), tipo, mot, usuario)
                    if ok: st.toast(msg)
                    else: st.error(msg)
    with tabs[5]:
        st.subheader("Permisos")
        st.caption("Se puede registrar 48 horas antes, durante la ventana, y hasta 24 horas despues.")
        idg, ids, texto = filtros_grado_seccion_nombre("perm")
        df = buscar_alumnos(texto, idg, ids, limite=100)
        if df.empty: st.info("Sin alumnos.")
        else:
            ops = {f"{r['nombre_completo']} ({r['dni']})": r["dni"] for _, r in df.iterrows()}
            with st.form("permiso_form"):
                sel = st.selectbox("Alumno", list(ops.keys()))
                fo = st.date_input("Fecha del permiso",
                                   value=ahora().date(),
                                   min_value=ahora().date() - timedelta(days=1),
                                   max_value=ahora().date() + timedelta(days=2))
                mot = st.text_input("Motivo del permiso")
                sub = st.form_submit_button("Registrar permiso", type="primary")
            if sub:
                con = obtener_conexion()
                f = con.execute("SELECT id FROM alumnos WHERE dni=?", (ops[sel],)).fetchone()
                if f:
                    ok, msg = crear_permiso(f["id"], fo.strftime("%Y-%m-%d"), mot, usuario)
                    if ok: st.toast(msg); st.rerun()
                    else: st.error(msg)
        st.markdown("---")
        st.subheader("Permisos registrados")
        dp = listar_permisos(solo_activos=False)
        if dp.empty: st.info("Sin permisos registrados.")
        else: st.dataframe(dp, use_container_width=True)


def vista_panel_direccion():
    st.title("Panel Direccion")
    fecha = hoy_str()
    marcar_faltas_al_cierre()
    if st.button("Actualizar", key="refresh_panel"):
        st.cache_data.clear()
        st.rerun()
    m = metricas_dia(fecha)
    st.subheader("Resumen del dia")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total alumnos", m["total"]); c2.metric("Puntuales", m["puntuales"])
    c3.metric("Tardanzas", m["tardanzas"]); c4.metric("Faltas", m["faltas"])
    c1, c2, c3 = st.columns(3)
    c1.metric("Reforzamiento asistio", m["ref_asistio"]); c2.metric("Bloqueados", m["bloqueados"])
    st.markdown("---")
    st.subheader("Ultimos escaneos")
    df = ultimos_registros(fecha, 30)
    if df.empty: st.info("Sin escaneos hoy.")
    else: st.dataframe(df, use_container_width=True)


def _selector_secciones(clave, permitidas=None):
    secs = listar_todas_secciones()
    if permitidas is not None: secs = [s for s in secs if s["id"] in permitidas]
    if not secs:
        st.warning("No hay secciones disponibles."); return [], {}
    etiquetas = {s["id"]: f"{s['grado']} {s['seccion']} ({s['turno']})" for s in secs}
    st.write("**Selecciona las secciones:**")
    sel = []; cols = st.columns(4)
    for i, s in enumerate(secs):
        with cols[i % 4]:
            if st.checkbox(etiquetas[s["id"]], key=f"{clave}_{s['id']}"): sel.append(s["id"])
    return sel, etiquetas

def cierre_mensual(mes, anio, turno, ids_sec=None, pid=None):
    ult = monthrange(anio, mes)[1]
    ini = f"{anio:04d}-{mes:02d}-01"; fin = f"{anio:04d}-{mes:02d}-{ult:02d}"
    con = obtener_conexion()
    q = ("SELECT g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno, "
         "COUNT(DISTINCT a.id) AS total_alumnos, "
         "SUM(CASE WHEN ast.estado='Puntual' THEN 1 ELSE 0 END) AS puntuales, "
         "SUM(CASE WHEN ast.estado='Falta' AND ast.justificada=1 THEN 1 ELSE 0 END) AS faltas_just, "
         "SUM(CASE WHEN ast.estado='Falta' AND ast.justificada=0 THEN 1 ELSE 0 END) AS faltas_injust, "
         "SUM(CASE WHEN ast.estado='Tardanza' THEN 1 ELSE 0 END) AS tardanzas, "
         "COUNT(*) AS total_dias "
         "FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id JOIN secciones s ON a.seccion_id=s.id "
         "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
         "WHERE ast.fecha BETWEEN ? AND ? AND ast.tipo='clases'")
    p = [ini, fin]
    if turno != "Todos": q += " AND t.nombre=?"; p.append(turno)
    if ids_sec:
        q += " AND s.id IN (" + ",".join(["?"]*len(ids_sec)) + ")"; p += ids_sec
    if pid is not None: q += " AND ast.periodo_id=?"; p.append(pid)
    q += " GROUP BY g.nombre, s.nombre, t.nombre ORDER BY t.nombre, g.nombre, s.nombre"
    return pd.read_sql(q, con, params=p)

def vista_reportes():
    st.title("Reportes y Consultas")
    usuario = st.session_state["user"]; rol = usuario["rol"]
    permitidas = None
    if rol == "Auxiliar":
        con = obtener_conexion()
        permitidas = [f["seccion_id"] for f in con.execute("SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id=?", (usuario["id"],)).fetchall()]
        if not permitidas:
            st.info("No tienes secciones asignadas. Contacta al Admin."); return
    pid = None
    if rol == "Admin":
        dfp = listar_periodos()
        if not dfp.empty:
            ops_p = {f"{r['nombre']} ({r['fecha_inicio']} - {r['fecha_fin']})" + (" [CERRADO]" if r["cerrado"] else (" [ACTIVO]" if r["activo"] else "")): r["id"] for _, r in dfp.iterrows()}
            sel = st.selectbox("Periodo", list(ops_p.keys())); pid = ops_p[sel]
    st.markdown("---")
    st.subheader("1. Selecciona las secciones")
    ids_sel, _ = _selector_secciones("rep_sec", permitidas)
    if not ids_sel:
        st.info("Selecciona al menos una seccion para continuar."); return
    st.markdown("---")
    st.subheader("2. Configura el reporte")
    if rol == "Auxiliar":
        opciones_tipo = ["Diario", "Mensual", "Bimestral"]
    else:
        opciones_tipo = ["Diario", "Mensual", "Bimestral", "Conteo faltas", "Cierre mensual"]
    c1, c2, c3 = st.columns(3)
    with c1: tipo = st.selectbox("Tipo de reporte", opciones_tipo)
    with c2: desde = st.date_input("Desde", ahora().date() - timedelta(days=7))
    with c3: hasta = st.date_input("Hasta", ahora().date())
    tipo_asist = st.selectbox("Asistencia", ["clases", "reforzamiento", "todas"])
    turno = st.selectbox("Turno", ["Todos"] + [t["nombre"] for t in listar_turnos()])
    if st.button("Generar reporte", type="primary"):
        if tipo == "Cierre mensual":
            df = cierre_mensual(ahora().month, ahora().year, turno, ids_sel, pid)
            if df.empty: st.warning("Sin datos.")
            else:
                st.dataframe(df, use_container_width=True)
                st.download_button("Excel", df_a_xlsx(df), f"cierre_{ahora().year}_{ahora().month:02d}.xlsx")
            return
        if tipo == "Conteo faltas":
            df = reporte_conteo_faltas(desde, hasta, turno, None, None, "", ids_sel, pid)
            st.write(f"**{len(df)} agrupaciones con faltas**")
        elif tipo == "Diario":
            df = reporte_diario(desde, hasta, turno, ids_sel, pid, tipo_asist)
            st.write(f"**{len(df)} alumnos**")
        elif tipo == "Mensual":
            df = reporte_mensual(desde, hasta, turno, ids_sel, pid, tipo_asist)
            st.write(f"**{len(df)} alumnos**")
        elif tipo == "Bimestral":
            df = reporte_bimestral(desde, hasta, turno, ids_sel, pid, tipo_asist)
            st.write(f"**{len(df)} alumnos**")
        else:
            df = reporte_detalle(desde, hasta, turno, None, None, "", tipo_asist, ids_sel, pid)
            st.write(f"**{len(df)} registros agrupados**")
        if df.empty: st.info("Sin datos."); return
        st.dataframe(df, use_container_width=True)
        c1, c2 = st.columns(2)
        with c1: st.download_button("Excel", df_a_xlsx(df), "reporte.xlsx", use_container_width=True)
        with c2: st.download_button("PDF", generar_pdf_tabla(df, "Reporte"), "reporte.pdf", "application/pdf", use_container_width=True)


def _frag_crear_alumno():
    st.subheader("Crear alumno manualmente")
    grados = listar_grados()
    if not grados: st.warning("No hay grados."); return
    with st.form("crear_al", clear_on_submit=True):
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
        pwd_admin = st.text_input("Contrasena de Admin para confirmar *", type="password")
        if st.form_submit_button("Crear", type="primary"):
            if not dni or not nom or not pat or not s:
                st.error("Completa obligatorios."); return
            if not re.fullmatch(r"\d{8}", dni.strip()):
                st.error("DNI invalido."); return
            if not pwd_admin:
                st.error("Ingresa tu contrasena de Admin."); return
            if not verificar_password_admin(pwd_admin):
                st.error("Contrasena de Admin incorrecta."); return
            ok, msg = crear_alumno(dni.strip(), nom.strip(), pat.strip(), mat.strip(), s["id"], apo.strip(), tel.strip(), st.session_state["user"])
            if ok: st.toast(msg); st.rerun()
            else: st.error(msg)

def _frag_editar_alumno():
    st.subheader("Editar alumno")
    idg, ids, texto = filtros_grado_seccion_nombre("ed_al")
    if not (texto or idg): return
    df = buscar_alumnos(texto, idg, ids, limite=50)
    if df.empty: st.info("Sin coincidencias."); return
    ops = {f"{r['nombre_completo']} - {r['grado']} {r['seccion']}": r["id"] for _, r in df.iterrows()}
    sel = st.selectbox("Alumno", list(ops.keys()), key="ed_sel"); idal = ops[sel]
    con = obtener_conexion()
    datos = con.execute("SELECT a.*,g.nombre AS grado,s.nombre AS seccion FROM alumnos a JOIN secciones s ON a.seccion_id=s.id JOIN grados g ON s.grado_id=g.id WHERE a.id=?", (idal,)).fetchone()
    if not datos: return
    grados = listar_grados()
    with st.form("ed_form"):
        st.info(f"DNI: {datos['dni']} (no editable)")
        apo = st.text_input("Apoderado", value=datos["nombre_apoderado"] or "")
        tel = st.text_input("Telefono", value=datos["telefono_apoderado"] or "")
        idxg = next((i for i, g in enumerate(grados) if g["nombre"] == datos["grado"]), 0)
        g = st.selectbox("Grado", grados, index=idxg, format_func=lambda x: x["nombre"])
        secs = secciones_por_grado(g["id"]) if g else []
        idxs = next((i for i, s in enumerate(secs) if s["id"] == datos["seccion_id"]), 0)
        s = st.selectbox("Seccion", secs, index=idxs, format_func=lambda x: x["nombre"])
        pwd_admin = st.text_input("Contrasena de Admin para confirmar *", type="password")
        if st.form_submit_button("Guardar", type="primary"):
            if not pwd_admin:
                st.error("Ingresa tu contrasena de Admin."); return
            if not verificar_password_admin(pwd_admin):
                st.error("Contrasena de Admin incorrecta."); return
            ok, msg = editar_alumno(idal, apo, tel, s["id"], datos["dni"], st.session_state["user"])
            if ok: st.toast(msg); st.rerun()
            else: st.error(msg)

def _frag_listar_alumnos():
    idg, ids, texto = filtros_grado_seccion_nombre("list_al")
    df = buscar_alumnos(texto, idg, ids, limite=5000)
    st.write(f"**{len(df)} alumnos**")
    if df.empty: st.info("Sin resultados."); return
    mostrar = st.checkbox("Mostrar todos", value=False)
    lim = len(df) if mostrar else 50
    for _, al in df.head(lim).iterrows():
        c1, c2 = st.columns([5, 1])
        c1.markdown(f"**{al['nombre_completo']}** &nbsp; <span style='color:#E65100; font-weight:700;'>{al['grado']} {al['seccion']}</span> <span style='color:#757575;'>({al['turno']})</span>", unsafe_allow_html=True)
        if c2.button("Ver perfil", key=f"perfil_{al['id']}"):
            st.session_state["perfil_alumno_id"] = al["id"]; st.rerun()

def _perfil_alumno(idal):
    d = perfil_alumno_datos(idal)
    if not d:
        st.warning("Alumno no encontrado."); st.session_state.pop("perfil_alumno_id", None); return
    al = d["alumno"]; usuario = st.session_state["user"]
    if st.button("Volver a la lista", key="volver_perfil"):
        st.session_state.pop("perfil_alumno_id", None); st.rerun()
    nombre = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}".strip(", ")
    if d["bloqueado"]: badge = '<span class="perfil-badge badge-bloqueado">BLOQUEADO</span>'
    elif not d["observados"].empty and any(d["observados"]["activo"] == 1): badge = '<span class="perfil-badge badge-observado">OBSERVADO</span>'
    else: badge = '<span class="perfil-badge badge-ok">ACTIVO</span>'
    c_info, c_qr = st.columns([3, 1])
    with c_info:
        st.markdown(f"""
        <div class="perfil-card">
            <div class="perfil-nombre">{nombre} {badge}</div>
            <div class="perfil-meta"><b>DNI:</b> {al['dni']} | <b>Grado:</b> {al['grado']} | <b>Seccion:</b> {al['seccion']} | <b>Turno:</b> {al['turno']}</div>
            <div class="perfil-meta"><b>Apoderado:</b> {al['nombre_apoderado'] or '-'} | <b>Telefono:</b> {al['telefono_apoderado'] or '-'}</div>
            <div class="perfil-resumen">
                <div class="perfil-resumen-item"><div class="num">{d['total_puntuales']}</div><div class="lbl">Puntuales</div></div>
                <div class="perfil-resumen-item"><div class="num">{d['total_tardanzas']}</div><div class="lbl">Tardanzas</div></div>
                <div class="perfil-resumen-item"><div class="num">{d['total_faltas']}</div><div class="lbl">Faltas</div></div>
                <div class="perfil-resumen-item"><div class="num">{d['total_ref_asistio']}</div><div class="lbl">Reforzamiento</div></div>
                <div class="perfil-resumen-item"><div class="num">{d['tard_injust']}</div><div class="lbl">Tard. injust.</div></div>
                <div class="perfil-resumen-item"><div class="num">{len(d['actas'])}</div><div class="lbl">Actas</div></div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    with c_qr:
        st.markdown("**Codigo QR**"); st.image(generar_qr(al["dni"]), width=180)
    c1, c2 = st.columns(2)
    with c1:
        pdf = pdf_carnet_alumno(al["dni"])
        if pdf: st.download_button("Descargar carnet QR", pdf, f"carnet_{al['dni']}.pdf", "application/pdf", use_container_width=True)
    with c2:
        st.download_button("Historial (Excel)", df_a_xlsx(d["asistencias"], "Historial"), f"historial_{al['dni']}.xlsx", use_container_width=True)
    if usuario["rol"] == "Admin":
        st.markdown("---")
        if al.get("activo", 1) == 1:
            with st.expander("Desactivar alumno"):
                st.warning("Estas seguro que desea desactivar al alumno?")
                pwd = st.text_input("Ingrese su contrasena de Admin", type="password", key="pwd_desac")
                if st.button("Confirmar desactivacion", type="primary", key="btn_desac"):
                    if not pwd: st.error("Ingrese su contrasena.")
                    elif not verificar_password_admin(pwd): st.error("Contrasena incorrecta.")
                    else:
                        ok, msg = retirar_alumno(al["id"], al["dni"], usuario); st.toast(msg); st.rerun()
        else:
            with st.expander("Reactivar alumno"):
                st.info("Estas seguro que desea reactivar al alumno?")
                pwd = st.text_input("Ingrese su contrasena de Admin", type="password", key="pwd_react")
                if st.button("Confirmar reactivacion", type="primary", key="btn_react"):
                    if not pwd: st.error("Ingrese su contrasena.")
                    elif not verificar_password_admin(pwd): st.error("Contrasena incorrecta.")
                    else:
                        ok, msg = reactivar_alumno(al["id"], al["dni"], usuario); st.toast(msg); st.rerun()
    st.markdown("---")
    tabs = st.tabs(["Asistencias", "Tardanzas", "Actas", "Observados", "Bloqueos", "Just. previas", "Permisos"])
    with tabs[0]:
        if d["asistencias"].empty: st.info("Sin asistencias registradas.")
        else: st.dataframe(d["asistencias"], use_container_width=True)
    with tabs[1]:
        if d["tardanzas"].empty: st.info("Sin tardanzas registradas.")
        else: st.dataframe(d["tardanzas"], use_container_width=True)
    with tabs[2]:
        if d["actas"].empty: st.info("Sin actas firmadas.")
        else: st.dataframe(d["actas"], use_container_width=True)
    with tabs[3]:
        if d["observados"].empty: st.info("Sin registros de observados.")
        else: st.dataframe(d["observados"], use_container_width=True)
    with tabs[4]:
        if d["bloqueos"].empty: st.info("Sin bloqueos registrados.")
        else: st.dataframe(d["bloqueos"], use_container_width=True)
    with tabs[5]:
        if d["just_previas"].empty: st.info("Sin justificaciones previas.")
        else: st.dataframe(d["just_previas"], use_container_width=True)
    with tabs[6]:
        if d["permisos"].empty: st.info("Sin permisos registrados.")
        else: st.dataframe(d["permisos"], use_container_width=True)

def vista_alumnos():
    st.title("Alumnos")
    pid = st.session_state.get("perfil_alumno_id")
    if pid: _perfil_alumno(pid); return
    tabs = st.tabs(["Listar", "Crear", "Editar"])
    with tabs[0]: _frag_listar_alumnos()
    with tabs[1]: _frag_crear_alumno()
    with tabs[2]: _frag_editar_alumno()


def vista_grados_secciones():
    st.title("Grados y Secciones")
    st.caption("Las secciones se crean automaticamente al importar el Excel de alumnos.")
    con = obtener_conexion()
    grados = listar_grados()
    st.subheader("Grados")
    if grados: st.dataframe(pd.DataFrame(grados), use_container_width=True)
    else: st.info("Sin grados.")
    st.subheader("Secciones")
    df = pd.read_sql("SELECT s.id,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno,(SELECT COUNT(*) FROM alumnos a WHERE a.seccion_id=s.id AND a.activo=1) AS alumnos_activos FROM secciones s JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id ORDER BY t.nombre,g.nombre,s.nombre", con)
    if df.empty: st.info("Sin secciones. Importa un Excel de alumnos para crearlas.")
    else: st.dataframe(df, use_container_width=True)


def vista_carnets():
    st.title("Carnets QR")
    st.caption("Selecciona los alumnos y descarga sus carnets, o descarga el salon completo.")
    grados = listar_grados()
    if not grados: st.warning("No hay grados."); return
    c1, c2 = st.columns(2)
    with c1: g = st.selectbox("Grado", grados, format_func=lambda x: x["nombre"], key="carn_g")
    with c2:
        secs = secciones_por_grado(g["id"]) if g else []
        if not secs: st.warning("Sin secciones."); return
        s = st.selectbox("Seccion", secs, format_func=lambda x: x["nombre"], key="carn_s")
    df = alumnos_de_seccion(s["id"])
    if df.empty: st.info("Sin alumnos en esa seccion."); return
    st.markdown("---")
    st.write(f"**{len(df)} alumnos en {g['nombre']} {s['nombre']}**")
    sel = []; cols = st.columns(3)
    for i, (_, al) in enumerate(df.iterrows()):
        with cols[i % 3]:
            if st.checkbox(al["nombre_completo"], key=f"carn_{al['id']}"): sel.append(al["id"])
    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        if sel:
            if st.button(f"Descargar {len(sel)} carnet(s)", type="primary", key="carn_sel"):
                pdf = pdf_carnets_seleccionados(sel, titulo=f"Carnets seleccionados - {g['nombre']} {s['nombre']}")
                if pdf: st.download_button("Descargar PDF", pdf, f"carnets_sel_{g['nombre']}{s['nombre']}.pdf", "application/pdf")
        else: st.info("Marca al menos un alumno.")
    with c2:
        if st.button("Descargar todo el salon", key="carn_todo"):
            pdf = pdf_carnets_por_seccion(s["id"])
            if pdf: st.download_button("Descargar PDF", pdf, f"carnets_{g['nombre']}{s['nombre']}.pdf", "application/pdf")


def vista_ventanas():
    st.title("Ventanas de asistencia")
    st.caption("Configura apertura, limite puntual y cierre por turno y tipo.")
    for turno in listar_turnos():
        st.subheader(f"Turno {turno['nombre']}")
        for v in listar_ventanas(turno["id"]):
            with st.expander(f"{v['nombre']} ({v['tipo']})"):
                with st.form(f"v_{v['id']}"):
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        try:
                            ap_dt = datetime.strptime(v["hora_apertura"], "%H:%M").time()
                        except (ValueError, TypeError):
                            ap_dt = datetime.strptime("08:00", "%H:%M").time()
                        ap = st.time_input("Apertura", value=ap_dt)
                    with c2:
                        try:
                            lim_dt = datetime.strptime(v["hora_limite_puntual"], "%H:%M").time() if v["hora_limite_puntual"] else ap_dt
                        except (ValueError, TypeError):
                            lim_dt = ap_dt
                        lim = st.time_input("Limite puntual", value=lim_dt)
                    with c3:
                        try:
                            ci_dt = datetime.strptime(v["hora_cierre"], "%H:%M").time()
                        except (ValueError, TypeError):
                            ci_dt = datetime.strptime("18:00", "%H:%M").time()
                        ci = st.time_input("Cierre", value=ci_dt)
                    if st.form_submit_button("Guardar", type="primary"):
                        con = obtener_conexion()
                        con.execute("UPDATE ventanas SET hora_apertura=?,hora_limite_puntual=?,hora_cierre=? WHERE id=?",
                                    (ap.strftime("%H:%M"), lim.strftime("%H:%M"), ci.strftime("%H:%M"), v["id"]))
                        con.commit()
                        auditar(st.session_state["user"]["usuario"], f"Edito ventana id={v['id']}")
                        st.toast("Ventana actualizada correctamente")
                        st.rerun()


def vista_usuarios():
    st.title("Usuarios")
    usuario = st.session_state["user"]; con = obtener_conexion()
    tabs = st.tabs(["Listar", "Crear", "Editar", "Asignar secciones"])
    with tabs[0]:
        st.dataframe(pd.read_sql("SELECT id,usuario,rol,nombres,activo,ultimo_login FROM usuarios ORDER BY usuario", con), use_container_width=True)
    with tabs[1]:
        with st.form("crear_u"):
            c1, c2 = st.columns(2)
            with c1:
                u = st.text_input("Usuario (max 15)"); p = st.text_input("Contrasena", type="password")
            with c2:
                n = st.text_input("Nombres (max 15)")
                r = st.selectbox("Rol", ["TOECE", "Auxiliar", "Direccion"])
            st.caption("Contrasena: minimo 6, 1 minuscula, 1 especial, 4 numeros.")
            t_lbl = st.selectbox("Turno (solo Auxiliar)", ["Ninguno"] + [x["nombre"] for x in listar_turnos()])
            pwd_admin = st.text_input("Contrasena de Admin para confirmar *", type="password")
            if st.form_submit_button("Crear", type="primary"):
                ok_u, msg_u = validar_usuario(u)
                ok_p, msg_p = validar_password(p)
                ok_n, msg_n = validar_nombre(n)
                if not ok_u: st.error(msg_u)
                elif not ok_n: st.error(msg_n)
                elif not ok_p: st.error(msg_p)
                elif not pwd_admin: st.error("Ingresa tu contrasena de Admin.")
                elif not verificar_password_admin(pwd_admin): st.error("Contrasena de Admin incorrecta.")
                else:
                    idt = None
                    if t_lbl != "Ninguno" and r == "Auxiliar":
                        idt = next(x["id"] for x in listar_turnos() if x["nombre"] == t_lbl)
                    try:
                        con.execute("INSERT INTO usuarios(usuario,password,rol,nombres,turno_asignado) VALUES(?,?,?,?,?)",
                                    (u, hashear_password(p), r, n, idt)); con.commit()
                        auditar(usuario["usuario"], f"Creo usuario {u}")
                        st.toast(f"Usuario {u} creado correctamente"); st.rerun()
                    except sqlite3.IntegrityError: st.error("Usuario ya existe.")
    with tabs[2]:
        df = pd.read_sql("SELECT id,usuario,rol,nombres FROM usuarios WHERE usuario!='admin'", con)
        if df.empty: st.info("Sin usuarios.")
        else:
            ops = {f"{r['usuario']} ({r['rol']})": r["id"] for _, r in df.iterrows()}
            sel = st.selectbox("Usuario", list(ops.keys()), key="edit_u_sel"); idu = ops[sel]
            datos = con.execute("SELECT * FROM usuarios WHERE id=?", (idu,)).fetchone()
            with st.form("edit_u"):
                u = st.text_input("Usuario", value=datos["usuario"])
                n = st.text_input("Nombres", value=datos["nombres"])
                p = st.text_input("Nueva contrasena (opcional)", type="password")
                r = st.selectbox("Rol", list(ROLES_VALIDOS), index=list(ROLES_VALIDOS).index(datos["rol"]))
                act = st.checkbox("Activo", value=bool(datos["activo"]))
                pwd_admin = st.text_input("Contrasena de Admin para confirmar *", type="password")
                if st.form_submit_button("Guardar", type="primary"):
                    ok_u, msg_u = validar_usuario(u)
                    ok_n, msg_n = validar_nombre(n)
                    if not ok_u: st.error(msg_u)
                    elif not ok_n: st.error(msg_n)
                    elif p:
                        ok_p, msg_p = validar_password(p)
                        if not ok_p: st.error(msg_p); return
                    if not pwd_admin: st.error("Ingresa tu contrasena de Admin."); return
                    if not verificar_password_admin(pwd_admin): st.error("Contrasena de Admin incorrecta."); return
                    if p: con.execute("UPDATE usuarios SET usuario=?,nombres=?,rol=?,password=?,activo=? WHERE id=?", (u, n, r, hashear_password(p), 1 if act else 0, idu))
                    else: con.execute("UPDATE usuarios SET usuario=?,nombres=?,rol=?,activo=? WHERE id=?", (u, n, r, 1 if act else 0, idu))
                    con.commit(); auditar(usuario["usuario"], f"Edito usuario {u}")
                    st.toast("Usuario editado correctamente"); st.rerun()
    with tabs[3]:
        st.subheader("Asignar secciones a Auxiliares")
        dfa = pd.read_sql("SELECT id,usuario,nombres,turno_asignado FROM usuarios WHERE rol='Auxiliar' AND activo=1", con)
        if dfa.empty: st.info("Sin auxiliares."); return
        ops = {f"{r['nombres']} ({r['usuario']})": r["id"] for _, r in dfa.iterrows()}
        sel = st.selectbox("Auxiliar", list(ops.keys())); ida = ops[sel]
        ta = con.execute("SELECT turno_asignado FROM usuarios WHERE id=?", (ida,)).fetchone()
        if ta and ta["turno_asignado"]: secs = secciones_por_turno(ta["turno_asignado"])
        else:
            st.warning("Este auxiliar no tiene turno asignado."); return
        asig = {r["seccion_id"] for r in con.execute("SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id=?", (ida,)).fetchall()}
        st.write("**Secciones disponibles:**")
        sel_s = []
        for s in secs:
            if st.checkbox(f"{s['grado']} {s['nombre']}", value=s["id"] in asig, key=f"asig_{ida}_{s['id']}"): sel_s.append(s["id"])
        pwd_admin = st.text_input("Contrasena de Admin para confirmar", type="password", key="pwd_asig")
        if st.button("Guardar asignaciones", type="primary"):
            if not pwd_admin: st.error("Ingresa tu contrasena de Admin.")
            elif not verificar_password_admin(pwd_admin): st.error("Contrasena de Admin incorrecta.")
            else:
                con.execute("DELETE FROM auxiliar_secciones WHERE usuario_id=?", (ida,))
                for sid in sel_s: con.execute("INSERT INTO auxiliar_secciones(usuario_id,seccion_id) VALUES(?,?)", (ida, sid))
                con.commit(); auditar(usuario["usuario"], f"Asigno {len(sel_s)} secciones a usuario_id={ida}")
                st.toast("Asignaciones guardadas correctamente"); st.rerun()


def _frag_importar_excel():
    st.subheader("Cargar alumnos al periodo")
    st.info("Columnas: DNI, Nombres, Apellido Paterno, Apellido Materno, Grado, Seccion, Turno, Apoderado, Telefono.")
    arch = st.file_uploader("Sube el Excel", type=["xlsx", "xls"], key="import_excel_periodo")
    if not arch: return
    df = pd.read_excel(arch)
    st.write(f"**{len(df)} filas detectadas.**")
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
        pwd_admin = st.text_input("Contrasena de Admin para confirmar *", type="password")
        validar = st.form_submit_button("Validar", type="primary")
    if validar:
        if not pwd_admin:
            st.error("Ingresa tu contrasena de Admin."); return
        if not verificar_password_admin(pwd_admin):
            st.error("Contrasena de Admin incorrecta."); return
        mapeo = {"dni": m_dni, "nombres": m_nom, "apellido_paterno": m_pat, "apellido_materno": m_mat, "grado": m_gra, "seccion": m_sec, "turno": m_tur, "apoderado_nombre": m_apo_n, "apoderado_telefono": m_apo_t}
        val, errs, res = validar_importacion(df, mapeo)
        st.session_state["_iv"] = val; st.session_state["_ie"] = errs; st.session_state["_ir"] = res
    if "_ir" in st.session_state:
        r = st.session_state["_ir"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Total", r["total"]); c2.metric("Validas", r["validas"]); c3.metric("Errores", r["errores"])
        if st.session_state["_ie"]:
            with st.expander("Errores"): st.dataframe(pd.DataFrame(st.session_state["_ie"]))
        if st.session_state["_iv"]:
            if st.button("Importar validas", type="primary"):
                ins, reac, errs = insertar_alumnos_validos(st.session_state["_iv"])
                st.toast(f"{ins} alumnos importados, {reac} reactivados.")
                if errs: st.warning(f"{len(errs)} errores al insertar")
                for k in ["_iv", "_ie", "_ir"]: st.session_state.pop(k, None)
                st.rerun()

def vista_auditoria():
    st.title("Auditoria y Periodos")
    usuario = st.session_state["user"]
    tabs = st.tabs(["Registros", "Periodos", "Cierre de año"])
    with tabs[0]:
        df = obtener_auditoria(500)
        st.write(f"**{len(df)} registros**")
        if not df.empty: st.dataframe(df, use_container_width=True)
    with tabs[1]:
        st.subheader("Periodos")
        st.dataframe(listar_periodos(), use_container_width=True)
        st.markdown("### Crear nuevo periodo")
        with st.form("nuevo_periodo"):
            c1, c2, c3 = st.columns(3)
            with c1: nom = st.text_input("Nombre (ej: 2026)")
            with c2: fi = st.date_input("Inicio", ahora().date())
            with c3: ff = st.date_input("Fin", ahora().date() + timedelta(days=270))
            pwd_admin = st.text_input("Contrasena de Admin para confirmar *", type="password")
            if st.form_submit_button("Crear y activar", type="primary"):
                if not nom.strip(): st.error("Ingresa un nombre.")
                elif (ff - fi).days < 30: st.error("El periodo debe durar minimo 1 mes.")
                elif not pwd_admin: st.error("Ingresa tu contrasena de Admin.")
                elif not verificar_password_admin(pwd_admin): st.error("Contrasena de Admin incorrecta.")
                else:
                    ok, msg = crear_periodo(nom.strip(), fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"), usuario)
                    if ok: st.toast(msg); st.rerun()
                    else: st.error(msg)
        st.markdown("---")
        st.markdown("### Cargar alumnos al periodo activo")
        p = obtener_periodo_activo()
        if p:
            if periodo_tiene_alumnos(p["id"]): st.success(f"El periodo '{p['nombre']}' ya tiene alumnos cargados.")
            else: st.warning(f"El periodo '{p['nombre']}' NO tiene alumnos. Sube el Excel.")
            _frag_importar_excel()
        else: st.info("No hay periodo activo. Crea uno primero.")
        st.markdown("---")
        st.markdown("### Activar periodo (solo no cerrados)")
        df2 = listar_periodos(); df2 = df2[df2["cerrado"] == 0]
        if not df2.empty:
            ops = {f"{r['nombre']} ({r['fecha_inicio']} - {r['fecha_fin']})" + (" ACTIVO" if r["activo"] else ""): r["id"] for _, r in df2.iterrows()}
            sel = st.selectbox("Periodo a activar", list(ops.keys()))
            pwd_act = st.text_input("Contrasena de Admin", type="password", key="pwd_act_per")
            if st.button("Activar", type="primary"):
                if not pwd_act: st.error("Ingresa tu contrasena de Admin.")
                elif not verificar_password_admin(pwd_act): st.error("Contrasena incorrecta.")
                else:
                    ok, msg = activar_periodo(ops[sel], usuario)
                    if ok: st.toast(msg); st.rerun()
                    else: st.error(msg)
    with tabs[2]:
        st.subheader("Cierre de año escolar")
        st.warning("Al cerrar el periodo se desactivan TODOS los alumnos de ese periodo.")
        p = obtener_periodo_activo()
        if not p: st.info("No hay periodo activo."); return
        st.info(f"Periodo activo: {p['nombre']} ({p['fecha_inicio']} - {p['fecha_fin']})")
        st.markdown("**Reporte resumen del periodo:**")
        df_rep = reporte_cierre_anual(p["id"])
        if not df_rep.empty: st.dataframe(df_rep, use_container_width=True)
        st.markdown("---")
        st.markdown("**Paso 1: Descargar reporte anual detallado (OBLIGATORIO)**")
        if st.button("Generar y descargar reporte anual", key="btn_desc_anual"):
            hojas = reporte_detallado_por_mes(p["id"])
            if not hojas: st.warning("Sin datos para el reporte.")
            else:
                st.download_button("Descargar Excel anual", df_a_xlsx_multilhoja(hojas), f"reporte_anual_{p['nombre']}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                st.session_state["_reporte_descargado"] = True
                st.toast("Reporte generado. Procede a cerrar el periodo.")
        st.markdown("---")
        st.markdown("**Paso 2: Cerrar periodo (requiere contrasena de Admin)**")
        desc = st.session_state.get("_reporte_descargado", False)
        if not desc: st.info("Debes descargar el reporte anual antes de cerrar el periodo.")
        with st.form("cerrar_anio"):
            c1, c2, c3 = st.columns(3)
            with c1: nn = st.text_input("Nombre nuevo periodo", value=str(ahora().year + 1))
            with c2: fi = st.date_input("Inicio nuevo", date(ahora().year + 1, 3, 1))
            with c3: ff = st.date_input("Fin nuevo", date(ahora().year + 1, 12, 31))
            pwd = st.text_input("Contrasena de Admin", type="password")
            conf = st.text_input("Escribe 'CERRAR' para confirmar")
            sub = st.form_submit_button("Cerrar año escolar", type="primary")
        if sub:
            if not desc: st.error("Primero debes descargar el reporte anual.")
            elif conf.strip() != "CERRAR": st.error("Debes escribir exactamente 'CERRAR'.")
            elif not pwd: st.error("Ingresa la contrasena de Admin.")
            elif not verificar_password_admin(pwd): st.error("Contrasena incorrecta.")
            else:
                ok, msg = cerrar_anio_escolar(usuario, p["id"], nn, fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"))
                if ok:
                    st.session_state.pop("_reporte_descargado", None); st.success(msg); st.rerun()
                else: st.error(msg)
        st.markdown("---")
        st.markdown("**Cierres anteriores:**")
        dfc = listar_cierres_anuales()
        if not dfc.empty: st.dataframe(dfc, use_container_width=True)
        st.markdown("**Periodos cerrados (solo consulta):**")
        dfp = listar_periodos_cerrados()
        if not dfp.empty: st.dataframe(dfp, use_container_width=True)


def vista_dias_especiales():
    st.title("Dias especiales")
    usuario = st.session_state["user"]; con = obtener_conexion()
    tabs = st.tabs(["Crear", "Listar / eliminar"])
    with tabs[0]:
        if "dia_tipo" not in st.session_state: st.session_state["dia_tipo"] = "Evento"
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
                    hora_dt = st.time_input("Hora entrada", value=datetime.strptime("08:00", "%H:%M").time())
                else:
                    turnos_opts = {"Ambos": None}; t_lbl = "Ambos"
                    hora_dt = datetime.strptime("00:00", "%H:%M").time()
                    st.info("Los feriados no tienen horario ni turno.")
            st.markdown("**Alcance del dia especial:**")
            st.caption("Si no marcas nada, aplica a TODO el colegio.")
            with st.expander("Filtrar por grados y secciones", expanded=False):
                grados = listar_grados(); selecciones_secciones = []
                for g in grados:
                    st.checkbox(f"Todo {g['nombre']}", key=f"dia_grado_{g['id']}")
                    secs = secciones_por_grado(g["id"])
                    if secs:
                        cols = st.columns(3)
                        for i, s in enumerate(secs):
                            with cols[i % 3]:
                                if st.checkbox(f"{g['nombre']} {s['nombre']}", key=f"dia_sec_{g['id']}_{s['id']}"):
                                    selecciones_secciones.append(s["id"])
            pwd_admin = st.text_input("Contrasena de Admin para confirmar *", type="password")
            sub = st.form_submit_button("Crear", type="primary")
        if sub:
            if not desc.strip():
                st.error("Descripcion requerida.")
            elif not pwd_admin:
                st.error("Ingresa tu contrasena de Admin.")
            elif not verificar_password_admin(pwd_admin):
                st.error("Contrasena de Admin incorrecta.")
            else:
                hora_str_guardar = hora_dt.strftime("%H:%M")
                idt = turnos_opts.get(t_lbl) if tipo == "Evento" else None
                per = obtener_periodo_activo(); pid = per["id"] if per else None
                cur = con.execute("INSERT INTO dias_especiales(fecha,descripcion,turno_id,hora_entrada,tipo,periodo_id) VALUES(?,?,?,?,?,?)",
                                  (fecha.strftime("%Y-%m-%d"), desc.strip(), idt,
                                   hora_str_guardar, "evento" if tipo == "Evento" else "feriado", pid))
                idd = cur.lastrowid
                for sid in selecciones_secciones:
                    con.execute("INSERT INTO dias_especiales_secciones(dia_especial_id,seccion_id) VALUES(?,?)", (idd, sid))
                con.commit()
                auditar(usuario["usuario"], f"Creo dia especial {desc}")
                st.toast("Dia especial creado correctamente")
                st.rerun()
    with tabs[1]:
        fh = hoy_str()
        df = pd.read_sql("SELECT d.id,d.fecha,d.descripcion,COALESCE(t.nombre,'Ambos') AS turno,d.hora_entrada,d.tipo,(SELECT COUNT(*) FROM dias_especiales_secciones WHERE dia_especial_id=d.id) AS num_secciones FROM dias_especiales d LEFT JOIN turnos t ON d.turno_id=t.id WHERE d.fecha>=? AND d.activo=1 ORDER BY d.fecha",
                          con, params=[fh])
        if df.empty: st.info("Sin dias especiales.")
        else:
            st.dataframe(df, use_container_width=True)
            ops = {f"{r['fecha']} - {r['descripcion']} ({r['tipo']})": r["id"] for _, r in df.iterrows()}
            sel = st.selectbox("Eliminar", list(ops.keys()))
            pwd_del = st.text_input("Contrasena de Admin", type="password", key="pwd_del_dia")
            if st.button("Eliminar", type="primary"):
                if not pwd_del: st.error("Ingresa tu contrasena de Admin.")
                elif not verificar_password_admin(pwd_del): st.error("Contrasena incorrecta.")
                else:
                    con.execute("DELETE FROM dias_especiales WHERE id=?", (ops[sel],)); con.commit()
                    auditar(usuario["usuario"], f"Elimino dia especial id={ops[sel]}")
                    st.toast("Dia especial eliminado correctamente"); st.rerun()


OPCIONES_POR_ROL = {
    "Admin": ["Puerta","TOECE","Panel Direccion","Reportes","Alumnos","Grados y Secciones","Carnets","Dias especiales","Ventanas","Usuarios","Auditoria"],
    "TOECE": ["Puerta","TOECE","Reportes","Dias especiales"],
    "Direccion": ["Puerta","TOECE","Panel Direccion","Reportes","Carnets","Auditoria"],
    "Auxiliar": ["Puerta","Reportes"],
}
RUTAS = {
    "Puerta": vista_puerta, "TOECE": vista_toece, "Panel Direccion": vista_panel_direccion,
    "Reportes": vista_reportes, "Alumnos": vista_alumnos, "Grados y Secciones": vista_grados_secciones,
    "Carnets": vista_carnets, "Dias especiales": vista_dias_especiales, "Ventanas": vista_ventanas,
    "Usuarios": vista_usuarios, "Auditoria": vista_auditoria,
}

def menu_lateral():
    usuario = st.session_state["user"]; rol = usuario["rol"]
    opciones = OPCIONES_POR_ROL.get(rol, [])
    with st.sidebar:
        inicial = (usuario["nombres"] or "?")[0].upper()
        st.markdown(f'<div class="encabezado-sidebar"><div class="avatar">{inicial}</div><div class="nombre">{usuario["nombres"]}</div><div class="rol">{rol}</div></div>', unsafe_allow_html=True)
        if "menu" not in st.session_state or st.session_state["menu"] not in opciones:
            st.session_state["menu"] = opciones[0]
        op = st.radio("Menu", opciones, key="menu", label_visibility="collapsed")
        st.markdown("---")
        if st.button("Cerrar sesion", use_container_width=True):
            auditar(usuario["usuario"], "Logout")
            tok = st.session_state.get("_token")
            if tok: eliminar_token(tok)
            _borrar_cookie()
            for k in list(st.session_state.keys()): del st.session_state[k]
            st.rerun()
    return op

def _enrutar(op, usuario):
    v = RUTAS.get(op)
    if not v: st.warning("Vista no disponible."); return
    if op not in OPCIONES_POR_ROL.get(usuario["rol"], []):
        st.error("Sin permisos.")
        auditar(usuario["usuario"], f"Intento acceso no autorizado a {op}")
        return
    v()

def _control_faltas():
    ult = st.session_state.get("_ultimo_control_faltas"); t = time.time()
    if ult and (t - ult) < 300: return
    st.session_state["_ultimo_control_faltas"] = t
    marcar_faltas_al_cierre()

def main():
    st.set_page_config(page_title="Asistencia I.E. Yarinacocha", page_icon="escudo.png", layout="wide", initial_sidebar_state="expanded")
    inicializar_bd()
    aplicar_estilos()
    inicializar_sesion()
    if not st.session_state.get("user"):
        vista_login(); return
    refrescar_sesion_si_necesario()
    if st.session_state["user"].get("debe_cambiar_password"):
        vista_cambio_password_obligatorio(); return
    if sistema_bloqueado():
        st.warning("El sistema no esta configurado. No hay periodo activo con alumnos cargados.")
        if st.session_state["user"]["rol"] == "Admin":
            st.info("Ve a **Auditoria → Periodos** para crear un periodo y subir el Excel de alumnos.")
            st.session_state["menu"] = "Auditoria"
            _enrutar("Auditoria", st.session_state["user"])
        else:
            st.info("Contacta al Administrador para que configure el periodo.")
        return
    _control_faltas()
    op = menu_lateral()
    if op: _enrutar(op, st.session_state["user"])
if __name__ == "__main__":
    main()
