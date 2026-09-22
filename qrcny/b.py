# ============================================================
# SISTEMA DE ASISTENCIA - I.E. YARINACOCHA
# Ejecutar: streamlit run app.py
# ============================================================

import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pandas as pd
import qrcode
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# ============================================================
# CONSTANTES
# ============================================================

RUTA_BD = "asistencia.db"
ZONA_PERU = timezone(timedelta(hours=-5))

PUNTUAL = "Puntual"
TARDANZA = "Tardanza"
FALTA = "Falta"
ASISTIO = "Asistio"
NO_ASISTIO = "No asistio"

CLASES = "clases"
REFORZAMIENTO = "reforzamiento"

ADMIN = "Admin"
TOECE = "TOECE"
AUXILIAR = "Auxiliar"
DIRECCION = "Direccion"

PERDONADO = "PERDONADO"
DERIVADO = "DERIVADO_TOECE"
RETENIDO = "RETENIDO_APODERADO"


# ============================================================
# FECHAS
# ============================================================

def ahora():
    return datetime.now(ZONA_PERU)

def hoy():
    return ahora().strftime("%Y-%m-%d")

def hora():
    return ahora().strftime("%H:%M:%S")

def hora_corta():
    return ahora().strftime("%H:%M")

def fecha_hora():
    return ahora().strftime("%Y-%m-%d %H:%M:%S")

def es_fin_de_semana():
    return ahora().weekday() >= 5


# ============================================================
# BASE DE DATOS
# ============================================================

_conexion = None

def conexion():
    global _conexion
    if _conexion is None:
        _conexion = sqlite3.connect(RUTA_BD, check_same_thread=False)
        _conexion.row_factory = sqlite3.Row
        _conexion.execute("PRAGMA foreign_keys = ON")
    return _conexion


def crear_tablas():
    con = conexion()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS turnos (
        id INTEGER PRIMARY KEY,
        nombre TEXT UNIQUE,
        hora_entrada TEXT,
        hora_salida TEXT
    );

    CREATE TABLE IF NOT EXISTS grados (
        id INTEGER PRIMARY KEY,
        nombre TEXT UNIQUE
    );

    CREATE TABLE IF NOT EXISTS secciones (
        id INTEGER PRIMARY KEY,
        nombre TEXT,
        grado_id INTEGER,
        turno_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS alumnos (
        id INTEGER PRIMARY KEY,
        dni TEXT UNIQUE NOT NULL,
        nombres TEXT NOT NULL,
        apellido_paterno TEXT NOT NULL,
        apellido_materno TEXT,
        seccion_id INTEGER,
        nombre_apoderado TEXT,
        telefono_apoderado TEXT,
        activo INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS ventanas (
        id INTEGER PRIMARY KEY,
        turno_id INTEGER,
        tipo TEXT,
        nombre TEXT,
        hora_apertura TEXT,
        hora_limite_puntual TEXT,
        hora_cierre TEXT
    );

    CREATE TABLE IF NOT EXISTS asistencias (
        id INTEGER PRIMARY KEY,
        alumno_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        tipo TEXT DEFAULT 'clases',
        hora TEXT,
        estado TEXT NOT NULL,
        justificada INTEGER DEFAULT 0,
        observacion TEXT,
        UNIQUE(alumno_id, fecha, tipo)
    );

    CREATE TABLE IF NOT EXISTS tardanzas (
        id INTEGER PRIMARY KEY,
        alumno_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        hora TEXT,
        numero INTEGER,
        accion TEXT,
        justificada INTEGER DEFAULT 0,
        registrado_por TEXT,
        UNIQUE(alumno_id, fecha)
    );

    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY,
        usuario TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        rol TEXT NOT NULL,
        nombres TEXT NOT NULL,
        activo INTEGER DEFAULT 1,
        debe_cambiar_password INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS bloqueos (
        id INTEGER PRIMARY KEY,
        alumno_id INTEGER NOT NULL,
        motivo TEXT,
        activo INTEGER DEFAULT 1,
        fecha_inicio TEXT,
        liberado_por TEXT
    );

    CREATE TABLE IF NOT EXISTS auditoria (
        id INTEGER PRIMARY KEY,
        usuario TEXT,
        accion TEXT,
        fecha TEXT,
        tabla_afectada TEXT,
        registro_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS dias_especiales (
        id INTEGER PRIMARY KEY,
        fecha TEXT NOT NULL,
        descripcion TEXT,
        tipo TEXT DEFAULT 'evento',
        turno_id INTEGER,
        hora_entrada TEXT,
        activo INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS observados (
        id INTEGER PRIMARY KEY,
        alumno_id INTEGER NOT NULL,
        fecha_ingreso TEXT,
        motivo TEXT,
        activo INTEGER DEFAULT 1,
        fecha_salida TEXT,
        observacion_cierre TEXT
    );
    """)
    con.commit()
    _datos_iniciales()


def _datos_iniciales():
    con = conexion()

    if con.execute("SELECT COUNT(*) FROM turnos").fetchone()[0] == 0:
        con.executemany(
            "INSERT INTO turnos(nombre, hora_entrada, hora_salida) VALUES(?,?,?)",
            [("Mañana", "06:00", "12:25"), ("Tarde", "12:00", "18:10")]
        )

    if con.execute("SELECT COUNT(*) FROM grados").fetchone()[0] == 0:
        for g in ["1ro", "2do", "3ro", "4to", "5to"]:
            con.execute("INSERT INTO grados(nombre) VALUES(?)", (g,))

    if con.execute("SELECT COUNT(*) FROM ventanas").fetchone()[0] == 0:
        mañana = con.execute("SELECT id FROM turnos WHERE nombre='Mañana'").fetchone()
        tarde = con.execute("SELECT id FROM turnos WHERE nombre='Tarde'").fetchone()

        if mañana:
            con.execute("""INSERT INTO ventanas(turno_id, tipo, nombre, hora_apertura, hora_limite_puntual, hora_cierre)
                           VALUES(?, 'clases', 'Clases mañana', '06:00', '06:55', '12:25')""", (mañana["id"],))
            con.execute("""INSERT INTO ventanas(turno_id, tipo, nombre, hora_apertura, hora_limite_puntual, hora_cierre)
                           VALUES(?, 'reforzamiento', 'Reforzamiento mañana', '14:00', '14:00', '16:00')""", (mañana["id"],))

        if tarde:
            con.execute("""INSERT INTO ventanas(turno_id, tipo, nombre, hora_apertura, hora_limite_puntual, hora_cierre)
                           VALUES(?, 'reforzamiento', 'Reforzamiento tarde', '10:00', '10:00', '10:20')""", (tarde["id"],))
            con.execute("""INSERT INTO ventanas(turno_id, tipo, nombre, hora_apertura, hora_limite_puntual, hora_cierre)
                           VALUES(?, 'clases', 'Clases tarde', '12:00', '12:49', '18:10')""", (tarde["id"],))

    if con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        con.execute(
            "INSERT INTO usuarios(usuario, password, rol, nombres, debe_cambiar_password) VALUES(?,?,?,?,1)",
            ("admin", hashear_password("Admin1234!"), ADMIN, "Administrador")
        )

    con.commit()


# ============================================================
# AUTENTICACIÓN
# ============================================================

def hashear_password(password):
    sal = secrets.token_bytes(16)
    hash_bytes = hashlib.pbkdf2_hmac("sha256", password.encode(), sal, 260000)
    return f"{sal.hex()}${hash_bytes.hex()}"


def verificar_password(password, hash_guardado):
    try:
        sal_hex, hash_hex = hash_guardado.split("$")
        sal = bytes.fromhex(sal_hex)
        nuevo = hashlib.pbkdf2_hmac("sha256", password.encode(), sal, 260000)
        return secrets.compare_digest(nuevo.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def validar_password(password):
    if not password:
        return False, "La contraseña no puede estar vacía."
    if len(password) < 6:
        return False, "Debe tener al menos 6 caracteres."
    if not any(c.islower() for c in password):
        return False, "Debe tener al menos una minúscula."
    if sum(c.isdigit() for c in password) < 4:
        return False, "Debe tener al menos 4 números."
    if not any(c in "!@#$%^&*(),.?\":{}|<>_-+=[]\\/;'`~" for c in password):
        return False, "Debe tener al menos un carácter especial."
    return True, ""


def iniciar_sesion(usuario, password):
    con = conexion()
    fila = con.execute(
        "SELECT * FROM usuarios WHERE usuario=? AND activo=1", (usuario,)
    ).fetchone()
    if not fila:
        return False, "Usuario no encontrado."
    if not verificar_password(password, fila["password"]):
        return False, "Contraseña incorrecta."
    st.session_state["usuario"] = dict(fila)
    auditar("Inicio de sesión")
    return True, ""


def cerrar_sesion():
    if "usuario" in st.session_state:
        auditar("Cierre de sesión")
    st.session_state.clear()


def usuario_actual():
    return st.session_state.get("usuario")


def verificar_admin(password):
    con = conexion()
    admins = con.execute(
        "SELECT password FROM usuarios WHERE rol='Admin' AND activo=1"
    ).fetchall()
    return any(verificar_password(password, a["password"]) for a in admins)


def auditar(accion, tabla=None, registro_id=None):
    u = usuario_actual()
    nombre = u["usuario"] if u else "sistema"
    con = conexion()
    con.execute(
        "INSERT INTO auditoria(usuario, accion, fecha, tabla_afectada, registro_id) VALUES(?,?,?,?,?)",
        (nombre, accion, fecha_hora(), tabla, registro_id)
    )
    con.commit()


# ============================================================
# ALUMNOS
# ============================================================

def listar_grados():
    con = conexion()
    return [dict(f) for f in con.execute("SELECT * FROM grados ORDER BY nombre").fetchall()]


def listar_secciones(grado_id=None):
    con = conexion()
    if grado_id:
        filas = con.execute(
            "SELECT * FROM secciones WHERE grado_id=? ORDER BY nombre", (grado_id,)
        ).fetchall()
    else:
        filas = con.execute("SELECT * FROM secciones ORDER BY nombre").fetchall()
    return [dict(f) for f in filas]


def listar_turnos():
    con = conexion()
    return [dict(f) for f in con.execute("SELECT * FROM turnos ORDER BY id").fetchall()]


def listar_alumnos_de_seccion(seccion_id):
    con = conexion()
    return pd.read_sql("""
        SELECT id, dni, nombres, apellido_paterno, apellido_materno,
               apellido_paterno || ' ' || COALESCE(apellido_materno, '') || ', ' || nombres AS nombre_completo
        FROM alumnos
        WHERE seccion_id=? AND activo=1
        ORDER BY apellido_paterno, apellido_materno, nombres
    """, con, params=[seccion_id])


def buscar_alumnos(texto="", grado_id=None, seccion_id=None, limite=200):
    con = conexion()
    sql = """
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') || ', ' || a.nombres AS nombre_completo
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.activo = 1
    """
    parametros = []
    if texto:
        for palabra in texto.split():
            sql += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? OR a.apellido_materno LIKE ?)"
            patron = f"%{palabra}%"
            parametros.extend([patron, patron, patron])
    if grado_id:
        sql += " AND g.id = ?"
        parametros.append(grado_id)
    if seccion_id:
        sql += " AND s.id = ?"
        parametros.append(seccion_id)
    sql += " ORDER BY a.apellido_paterno LIMIT ?"
    parametros.append(limite)
    return pd.read_sql(sql, con, params=parametros)


def buscar_por_dni(dni):
    con = conexion()
    fila = con.execute("""
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               s.id AS seccion_id, s.nombre AS seccion, s.turno_id,
               g.nombre AS grado, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.dni = ? AND a.activo = 1
    """, (dni,)).fetchone()
    return dict(fila) if fila else None


def nombre_completo(alumno):
    return f"{alumno['apellido_paterno']} {alumno['apellido_materno'] or ''}, {alumno['nombres']}".strip(", ")


def crear_alumno(dni, nombres, apellido_paterno, apellido_materno, seccion_id, apoderado, telefono):
    con = conexion()
    try:
        con.execute("""
            INSERT INTO alumnos(dni, nombres, apellido_paterno, apellido_materno,
                                seccion_id, nombre_apoderado, telefono_apoderado)
            VALUES(?,?,?,?,?,?,?)
        """, (dni, nombres, apellido_paterno, apellido_materno,
              seccion_id, apoderado or None, telefono or None))
        con.commit()
        auditar(f"Creó alumno DNI {dni}", "alumnos")
        return True, f"Alumno {nombres} creado."
    except sqlite3.IntegrityError:
        return False, f"Ya existe un alumno con DNI {dni}."
    except sqlite3.Error as e:
        return False, f"Error: {e}"


# ============================================================
# DÍAS ESPECIALES
# ============================================================

def dia_especial_de_hoy(turno_id, seccion_id=None):
    con = conexion()
    filas = con.execute("""
        SELECT * FROM dias_especiales
        WHERE fecha=? AND activo=1 AND (turno_id=? OR turno_id IS NULL)
        ORDER BY turno_id DESC
    """, (hoy(), turno_id)).fetchall()
    for f in filas:
        return dict(f)
    return None


def es_feriado_hoy():
    con = conexion()
    fila = con.execute(
        "SELECT * FROM dias_especiales WHERE fecha=? AND tipo='feriado' AND activo=1",
        (hoy(),)
    ).fetchone()
    return dict(fila) if fila else None


def crear_dia_especial(fecha, descripcion, tipo, turno_id=None, hora_entrada=None):
    con = conexion()
    con.execute("""
        INSERT INTO dias_especiales(fecha, descripcion, tipo, turno_id, hora_entrada)
        VALUES(?,?,?,?,?)
    """, (fecha, descripcion, tipo, turno_id, hora_entrada))
    con.commit()
    auditar(f"Creó día especial '{descripcion}' ({fecha})", "dias_especiales")
    return True, "Día especial creado."


def listar_dias_especiales():
    con = conexion()
    return pd.read_sql("""
        SELECT d.id, d.fecha, d.descripcion, d.tipo,
               COALESCE(t.nombre, 'Ambos') AS turno,
               COALESCE(d.hora_entrada, '-') AS hora_entrada
        FROM dias_especiales d
        LEFT JOIN turnos t ON d.turno_id = t.id
        WHERE d.fecha >= ?
        ORDER BY d.fecha
    """, con, params=[hoy()])


def eliminar_dia_especial(dia_id):
    con = conexion()
    con.execute("DELETE FROM dias_especiales WHERE id=?", (dia_id,))
    con.commit()
    auditar(f"Eliminó día especial id={dia_id}", "dias_especiales", dia_id)
    return True, "Día especial eliminado."


# ============================================================
# ASISTENCIAS
# ============================================================

def ventana_activa(turno_id, seccion_id=None):
    feriado = es_feriado_hoy()
    if feriado:
        return None
    con = conexion()
    ahora_str = hora_corta()
    dia = dia_especial_de_hoy(turno_id, seccion_id)
    ventanas = con.execute(
        "SELECT * FROM ventanas WHERE turno_id=? ORDER BY id", (turno_id,)
    ).fetchall()
    for v in ventanas:
        apertura = v["hora_apertura"]
        if dia and dia["tipo"] == "evento" and v["tipo"] == CLASES and dia["hora_entrada"]:
            apertura = dia["hora_entrada"]
        limite = v["hora_limite_puntual"] or apertura
        if apertura <= ahora_str <= v["hora_cierre"]:
            return {**dict(v), "hora_apertura_efectiva": apertura, "hora_limite_efectiva": limite}
    return None


def esta_bloqueado(alumno_id):
    con = conexion()
    fila = con.execute(
        "SELECT * FROM bloqueos WHERE alumno_id=? AND activo=1 ORDER BY id DESC LIMIT 1",
        (alumno_id,)
    ).fetchone()
    return dict(fila) if fila else None


def registrar_entrada(dni, usuario):
    dni = (dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni):
        return False, "ERROR", "DNI inválido", {}

    alumno = buscar_por_dni(dni)
    if not alumno:
        return False, "ERROR", "DNI no encontrado", {}

    bloqueo = esta_bloqueado(alumno["id"])
    if bloqueo:
        auditar(f"Intento de escaneo bloqueado DNI {dni}", "bloqueos", alumno["id"])
        return False, "BLOQUEADO", f"{nombre_completo(alumno)} | BLOQUEADO - retener y llevar a TOECE", {"alumno": alumno}

    feriado = es_feriado_hoy()
    if feriado:
        return False, "ERROR", f"Hoy es feriado: {feriado['descripcion']}", {}

    if es_fin_de_semana():
        dia = dia_especial_de_hoy(alumno["turno_id"], alumno["seccion_id"])
        if not dia or dia["tipo"] != "evento":
            return False, "ERROR", "Hoy no hay clases.", {}

    ventana = ventana_activa(alumno["turno_id"], alumno["seccion_id"])
    if not ventana:
        return False, "ERROR", f"Sin ventana activa ({hora_corta()})", {}

    con = conexion()
    tipo = ventana["tipo"]
    ahora_str = hora_corta()

    ya = con.execute(
        "SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo=?",
        (alumno["id"], hoy(), tipo)
    ).fetchone()
    if ya:
        nombre_tipo = "clases" if tipo == CLASES else "reforzamiento"
        return False, "ERROR", f"{nombre_completo(alumno)} ya registró {nombre_tipo} hoy", {}

    if tipo == REFORZAMIENTO:
        con.execute("""
            INSERT INTO asistencias(alumno_id, fecha, tipo, hora, estado)
            VALUES(?,?,?,?,?)
        """, (alumno["id"], hoy(), REFORZAMIENTO, hora(), ASISTIO))

        if alumno["turno"] == "Tarde":
            ya_clases = con.execute(
                "SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo='clases'",
                (alumno["id"], hoy())
            ).fetchone()
            if not ya_clases:
                con.execute("""
                    INSERT INTO asistencias(alumno_id, fecha, tipo, hora, estado)
                    VALUES(?,?,?,?,?)
                """, (alumno["id"], hoy(), CLASES, hora(), PUNTUAL))

        con.commit()
        auditar(f"Reforzamiento DNI {dni}", "asistencias", alumno["id"])
        mensaje = f"{nombre_completo(alumno)} | {alumno['grado']} {alumno['seccion']} | Reforzamiento {ahora_str}"
        if alumno["turno"] == "Tarde":
            mensaje += " + Clases Puntual"
        return True, "REFORZAMIENTO", mensaje, {"alumno": alumno}

    if alumno["turno"] == "Tarde":
        ya_clases = con.execute(
            "SELECT id FROM asistencias WHERE alumno_id=? AND fecha=? AND tipo='clases'",
            (alumno["id"], hoy())
        ).fetchone()
        if ya_clases:
            return False, "ERROR", f"{nombre_completo(alumno)} ya tiene clases registradas hoy.", {}

    limite = ventana["hora_limite_efectiva"]
    estado = PUNTUAL if ahora_str <= limite else TARDANZA

    con.execute("""
        INSERT INTO asistencias(alumno_id, fecha, tipo, hora, estado)
        VALUES(?,?,?,?,?)
    """, (alumno["id"], hoy(), CLASES, hora(), estado))

    if estado == TARDANZA:
        numero = _contar_tardanzas(alumno["id"]) + 1
        accion = _accion_por_tardanza(numero)
        try:
            con.execute("""
                INSERT INTO tardanzas(alumno_id, fecha, hora, numero, accion, registrado_por)
                VALUES(?,?,?,?,?,?)
            """, (alumno["id"], hoy(), hora(), numero, accion, usuario["usuario"]))
        except sqlite3.IntegrityError:
            auditar(f"Tardanza duplicada alumno_id={alumno['id']}", "tardanzas", alumno["id"])
        except sqlite3.Error as e:
            auditar(f"Error al insertar tardanza alumno_id={alumno['id']}: {e}", "tardanzas", alumno["id"])

        if numero >= 4 and not esta_bloqueado(alumno["id"]):
            _crear_bloqueo(alumno["id"], f"{numero}ta tardanza injustificada")

        con.commit()
        auditar(f"Tardanza {numero}a DNI {dni} → {accion}", "tardanzas", alumno["id"])
        return True, "TARDANZA", f"{nombre_completo(alumno)} | {alumno['grado']} {alumno['seccion']} | Tardanza {numero}a ({accion}) {ahora_str}", {"alumno": alumno, "numero": numero, "accion": accion}

    con.commit()
    auditar(f"Entrada puntual DNI {dni}", "asistencias", alumno["id"])
    return True, PUNTUAL, f"{nombre_completo(alumno)} | {alumno['grado']} {alumno['seccion']} | Puntual {ahora_str}", {"alumno": alumno}


def _contar_tardanzas(alumno_id):
    con = conexion()
    fila = con.execute(
        "SELECT COUNT(*) FROM tardanzas WHERE alumno_id=? AND justificada=0",
        (alumno_id,)
    ).fetchone()
    return fila[0] or 0


def _accion_por_tardanza(numero):
    if numero <= 2:
        return PERDONADO
    if numero == 3:
        return DERIVADO
    return RETENIDO


def _crear_bloqueo(alumno_id, motivo):
    con = conexion()
    con.execute("""
        INSERT INTO bloqueos(alumno_id, motivo, activo, fecha_inicio)
        VALUES(?,?,1,?)
    """, (alumno_id, motivo, hoy()))
    con.commit()
    auditar(f"Bloqueo automático alumno_id={alumno_id}", "bloqueos", alumno_id)


def liberar_bloqueo(alumno_id):
    con = conexion()
    u = usuario_actual()
    con.execute(
        "UPDATE bloqueos SET activo=0, liberado_por=? WHERE alumno_id=? AND activo=1",
        (u["usuario"] if u else "sistema", alumno_id)
    )
    con.commit()
    auditar(f"Liberó bloqueo alumno_id={alumno_id}", "bloqueos", alumno_id)
    return True, "Bloqueo liberado."


def listar_bloqueados():
    con = conexion()
    return pd.read_sql("""
        SELECT b.id, a.id AS alumno_id, a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') || ', ' || a.nombres AS alumno,
               g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               b.motivo, b.fecha_inicio
        FROM bloqueos b
        JOIN alumnos a ON b.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE b.activo = 1
        ORDER BY b.fecha_inicio DESC
    """, con)


# ============================================================
# MARCAR FALTAS
# ============================================================

def marcar_faltas_del_dia():
    feriado = es_feriado_hoy()
    if feriado:
        return 0

    if es_fin_de_semana():
        con = conexion()
        dia = con.execute(
            "SELECT id FROM dias_especiales WHERE fecha=? AND tipo='evento' AND activo=1",
            (hoy(),)
        ).fetchone()
        if not dia:
            return 0

    con = conexion()
    ahora_str = hora_corta()
    total = 0

    turnos = con.execute("SELECT id FROM turnos").fetchall()
    for t in turnos:
        ventanas_clases = con.execute("""
            SELECT * FROM ventanas WHERE turno_id=? AND tipo='clases'
        """, (t["id"],)).fetchall()

        if not ventanas_clases:
            continue

        todas_cerradas = all(ahora_str >= v["hora_cierre"] for v in ventanas_clases)
        if not todas_cerradas:
            continue

        alumnos = con.execute("""
            SELECT a.id FROM alumnos a
            JOIN secciones s ON a.seccion_id = s.id
            WHERE s.turno_id = ? AND a.activo = 1
        """, (t["id"],)).fetchall()

        for al in alumnos:
            ya = con.execute("""
                SELECT id FROM asistencias
                WHERE alumno_id=? AND fecha=? AND tipo='clases'
            """, (al["id"], hoy())).fetchone()

            if not ya:
                con.execute("""
                    INSERT INTO asistencias(alumno_id, fecha, tipo, hora, estado)
                    VALUES(?,?,?,?,?)
                """, (al["id"], hoy(), CLASES, hora(), FALTA))
                total += 1

    con.commit()
    return total


# ============================================================
# MÉTRICAS
# ============================================================

def metricas_de_hoy():
    con = conexion()
    f = hoy()

    total_alumnos = con.execute(
        "SELECT COUNT(*) FROM alumnos WHERE activo=1"
    ).fetchone()[0]

    puntuales = con.execute("""
        SELECT COUNT(*) FROM asistencias
        WHERE fecha=? AND tipo='clases' AND estado='Puntual'
    """, (f,)).fetchone()[0]

    tardanzas = con.execute("""
        SELECT COUNT(*) FROM asistencias
        WHERE fecha=? AND tipo='clases' AND estado='Tardanza'
    """, (f,)).fetchone()[0]

    faltas = con.execute("""
        SELECT COUNT(*) FROM asistencias
        WHERE fecha=? AND tipo='clases' AND estado='Falta'
    """, (f,)).fetchone()[0]

    reforzamiento = con.execute("""
        SELECT COUNT(*) FROM asistencias
        WHERE fecha=? AND tipo='reforzamiento' AND estado='Asistio'
    """, (f,)).fetchone()[0]

    bloqueados = con.execute(
        "SELECT COUNT(*) FROM bloqueos WHERE activo=1"
    ).fetchone()[0]

    return {
        "total": total_alumnos,
        "puntuales": puntuales,
        "tardanzas": tardanzas,
        "faltas": faltas,
        "reforzamiento": reforzamiento,
        "bloqueados": bloqueados,
    }


def registros_de_hoy(limite=30):
    con = conexion()
    return pd.read_sql("""
        SELECT a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') AS apellidos,
               a.nombres,
               g.nombre AS grado,
               s.nombre AS seccion,
               t.nombre AS turno,
               ast.tipo,
               ast.hora,
               ast.estado
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ast.fecha = ?
        ORDER BY ast.hora DESC
        LIMIT ?
    """, con, params=[hoy(), limite])


# ============================================================
# OBSERVADOS
# ============================================================

def crear_observado(alumno_id, motivo):
    con = conexion()
    con.execute("""
        INSERT INTO observados(alumno_id, fecha_ingreso, motivo, activo)
        VALUES(?,?,?,1)
    """, (alumno_id, hoy(), motivo))
    con.commit()
    auditar(f"Creó observado alumno_id={alumno_id}", "observados", alumno_id)
    return True, "Observado creado."


def cerrar_observado(observado_id, observacion):
    con = conexion()
    con.execute("""
        UPDATE observados SET activo=0, fecha_salida=?, observacion_cierre=?
        WHERE id=?
    """, (fecha_hora(), observacion, observado_id))
    con.commit()
    auditar(f"Cerró observado id={observado_id}", "observados", observado_id)
    return True, "Observado cerrado."


def listar_observados(solo_activos=True):
    con = conexion()
    sql = """
        SELECT o.id, a.id AS alumno_id, a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') AS apellidos,
               a.nombres, g.nombre AS grado, s.nombre AS seccion,
               o.fecha_ingreso, o.motivo, o.activo,
               COALESCE(o.fecha_salida, '-') AS fecha_salida,
               COALESCE(o.observacion_cierre, '') AS observacion_cierre
        FROM observados o
        JOIN alumnos a ON o.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
    """
    if solo_activos:
        sql += " WHERE o.activo = 1"
    sql += " ORDER BY o.fecha_ingreso DESC"
    return pd.read_sql(sql, con)


# ============================================================
# REPORTES
# ============================================================

LETRAS_ESTADO = {
    "Puntual": "P",
    "Tardanza": "T",
    "Falta": "F",
    "Asistio": "P",
    "No asistio": "F",
}


def _datos_para_pivote(desde, hasta, turno="Todos", tipo="clases", seccion_id=None):
    con = conexion()
    sql = """
        SELECT a.dni, a.apellido_paterno, a.apellido_materno, a.nombres,
               g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               ast.fecha, ast.estado
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ast.fecha BETWEEN ? AND ?
    """
    parametros = [desde, hasta]
    if tipo != "todas":
        sql += " AND ast.tipo = ?"
        parametros.append(tipo)
    if turno != "Todos":
        sql += " AND t.nombre = ?"
        parametros.append(turno)
    if seccion_id:
        sql += " AND s.id = ?"
        parametros.append(seccion_id)
    sql += " ORDER BY a.apellido_paterno, a.apellido_materno, a.nombres, ast.fecha"

    df = pd.read_sql(sql, con, params=parametros)
    if not df.empty:
        df["fecha_corta"] = pd.to_datetime(df["fecha"]).dt.strftime("%d/%m")
        df["letra"] = df["estado"].map(LETRAS_ESTADO).fillna("?")
    return df


def _todos_los_alumnos(turno="Todos", seccion_id=None):
    con = conexion()
    sql = """
        SELECT a.dni, a.apellido_paterno, a.apellido_materno, a.nombres,
               g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.activo = 1
    """
    parametros = []
    if turno != "Todos":
        sql += " AND t.nombre = ?"
        parametros.append(turno)
    if seccion_id:
        sql += " AND s.id = ?"
        parametros.append(seccion_id)
    sql += " ORDER BY a.apellido_paterno, a.apellido_materno, a.nombres"
    return pd.read_sql(sql, con, params=parametros)


def reporte_diario(desde, hasta, turno="Todos", tipo="clases", seccion_id=None):
    alumnos = _todos_los_alumnos(turno, seccion_id)
    if alumnos.empty:
        return pd.DataFrame()

    df = _datos_para_pivote(desde, hasta, turno, tipo, seccion_id)
    fechas = pd.date_range(pd.to_datetime(desde), pd.to_datetime(hasta)).strftime("%d/%m").tolist()

    if not df.empty:
        pivot = df.pivot_table(
            index=["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno"],
            columns="fecha_corta", values="letra", aggfunc="first"
        ).reset_index()
        pivot.columns.name = None
    else:
        pivot = pd.DataFrame(columns=["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno"])

    resultado = alumnos.merge(
        pivot,
        on=["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno"],
        how="left",
    )
    resultado = resultado.rename(columns={
        "apellido_paterno": "Apellido Paterno",
        "apellido_materno": "Apellido Materno",
        "nombres": "Nombres",
        "grado": "Grado",
        "seccion": "Sección",
        "turno": "Turno",
    })
    for f in fechas:
        if f not in resultado.columns:
            resultado[f] = None
    columnas_fijas = ["Apellido Paterno", "Apellido Materno", "Nombres", "Grado", "Sección", "Turno"]
    resultado = resultado[columnas_fijas + fechas]
    resultado = resultado.fillna("")
    return resultado


def reporte_mensual(desde, hasta, turno="Todos", tipo="clases", seccion_id=None):
    alumnos = _todos_los_alumnos(turno, seccion_id)
    if alumnos.empty:
        return pd.DataFrame()

    df = _datos_para_pivote(desde, hasta, turno, tipo, seccion_id)
    fechas = pd.date_range(pd.to_datetime(desde), pd.to_datetime(hasta)).strftime("%d/%m").tolist()

    if not df.empty:
        pivot = df.pivot_table(
            index=["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno"],
            columns="fecha_corta", values="letra", aggfunc="first"
        ).reset_index()
        pivot.columns.name = None
        conteos = df.groupby(
            ["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno"]
        )["letra"].apply(
            lambda x: pd.Series({
                "P": (x == "P").sum(),
                "T": (x == "T").sum(),
                "F": (x == "F").sum(),
            })
        ).unstack(fill_value=0).reset_index()
    else:
        pivot = pd.DataFrame(columns=["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno"])
        conteos = pd.DataFrame(columns=["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno", "P", "T", "F"])

    resultado = alumnos.merge(
        pivot,
        on=["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno"],
        how="left",
    )
    resultado = resultado.merge(
        conteos,
        on=["apellido_paterno", "apellido_materno", "nombres", "grado", "seccion", "turno"],
        how="left",
    )
    resultado = resultado.rename(columns={
        "apellido_paterno": "Apellido Paterno",
        "apellido_materno": "Apellido Materno",
        "nombres": "Nombres",
        "grado": "Grado",
        "seccion": "Sección",
        "turno": "Turno",
    })
    for f in fechas:
        if f not in resultado.columns:
            resultado[f] = None
    for col in ["P", "T", "F"]:
        if col not in resultado.columns:
            resultado[col] = 0
        resultado[col] = resultado[col].fillna(0).astype(int)
    columnas_fijas = ["Apellido Paterno", "Apellido Materno", "Nombres", "Grado", "Sección", "Turno"]
    resultado = resultado[columnas_fijas + fechas + ["P", "T", "F"]]
    resultado[fechas] = resultado[fechas].fillna("")
    return resultado


def conteo_faltas(desde, hasta, turno="Todos", seccion_id=None):
    con = conexion()
    sql = """
        SELECT g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               SUM(CASE WHEN ast.justificada=1 THEN 1 ELSE 0 END) AS faltas_justificadas,
               SUM(CASE WHEN ast.justificada=0 THEN 1 ELSE 0 END) AS faltas_injustificadas,
               COUNT(*) AS total
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE ast.estado='Falta' AND ast.tipo='clases' AND ast.fecha BETWEEN ? AND ?
    """
    parametros = [desde, hasta]
    if turno != "Todos":
        sql += " AND t.nombre = ?"
        parametros.append(turno)
    if seccion_id:
        sql += " AND s.id = ?"
        parametros.append(seccion_id)
    sql += " GROUP BY g.nombre, s.nombre, t.nombre ORDER BY t.nombre, g.nombre, s.nombre"
    return pd.read_sql(sql, con, params=parametros)


def exportar_excel(df, hoja="Datos"):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=hoja)
    buffer.seek(0)
    return buffer.getvalue()


# ============================================================
# CARNETS PDF
# ============================================================

def generar_qr_imagen(dni):
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(str(dni))
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def generar_pdf_carnets(alumnos, titulo="Carnets QR"):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    ancho, alto = A4
    columnas, filas = 3, 3
    ancho_carnet = ancho / columnas
    alto_carnet = alto / filas
    margen = 6
    color_naranja = colors.HexColor("#E65100")

    for i, alumno in enumerate(alumnos):
        posicion = i % (columnas * filas)
        if posicion == 0 and i > 0:
            c.showPage()
        col = posicion % columnas
        fila = posicion // columnas
        x = col * ancho_carnet + margen
        y = alto - (fila + 1) * alto_carnet + margen
        w = ancho_carnet - 2 * margen
        h = alto_carnet - 2 * margen

        c.setStrokeColor(colors.grey)
        c.setLineWidth(0.5)
        c.rect(x, y, w, h)
        c.setFillColor(color_naranja)
        c.rect(x, y + h - 6, w, 6, fill=1, stroke=0)

        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 9)
        apellidos = f"{alumno['apellido_paterno']} {alumno['apellido_materno'] or ''}".strip()
        c.drawCentredString(x + w / 2, y + h - 20, apellidos[:26])
        c.setFont("Helvetica", 8)
        c.drawCentredString(x + w / 2, y + h - 32, alumno["nombres"][:26])
        c.setFont("Helvetica-Bold", 7)
        c.setFillColor(color_naranja)
        c.drawCentredString(x + w / 2, y + h - 46,
                            f"{alumno['grado']} {alumno['seccion']} — {alumno['turno']}")

        qr_imagen = generar_qr_imagen(alumno["dni"])
        qr_buffer = BytesIO()
        qr_imagen.save(qr_buffer, format="PNG")
        qr_buffer.seek(0)
        img = ImageReader(qr_buffer)
        qr_size = min(w, h) - 90
        if qr_size < 60:
            qr_size = 60
        c.drawImage(img, x + (w - qr_size) / 2, y + 15, width=qr_size, height=qr_size)
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 7)
        c.drawCentredString(x + w / 2, y + 6, f"DNI: {alumno['dni']}")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()


# ============================================================
# UI ESTILOS
# ============================================================

def aplicar_estilos():
    st.markdown("""
    <style>
    html, body, [class*="css"] { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
    h1 { font-size: 1.9rem !important; }
    h2 { font-size: 1.4rem !important; }
    h3 { font-size: 1.15rem !important; }
    .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
        background: #808080; color: white; border-radius: 8px; font-weight: 600;
        border: none; padding: 11px 20px; min-height: 44px;
    }
    .stButton > button:hover { background: #666; }
    div[data-testid="stMetric"] { border: 1px solid rgba(128,128,128,0.3); border-radius: 8px; padding: 18px 20px; }
    .mensaje-puntual { background: #d4edda; color: #155724; padding: 14px; border-radius: 6px; border-left: 4px solid #66BB6A; font-weight: 600; margin: 8px 0; }
    .mensaje-tardanza { background: #fff3cd; color: #856404; padding: 14px; border-radius: 6px; border-left: 4px solid #FFB74D; font-weight: 600; margin: 8px 0; }
    .mensaje-bloqueado { background: #f8d7da; color: #721c24; padding: 14px; border-radius: 6px; border-left: 4px solid #EF5350; font-weight: 700; margin: 8px 0; }
    .mensaje-reforzamiento { background: #d1ecf1; color: #0c5460; padding: 14px; border-radius: 6px; border-left: 4px solid #42A5F5; font-weight: 600; margin: 8px 0; }
    .mensaje-error { background: #f0f0f0; color: #555; padding: 14px; border-radius: 6px; border-left: 4px solid #888; margin: 8px 0; }
    </style>
    """, unsafe_allow_html=True)


def mostrar_mensaje(tipo, texto):
    clases = {
        PUNTUAL: "mensaje-puntual",
        TARDANZA: "mensaje-tardanza",
        "BLOQUEADO": "mensaje-bloqueado",
        "REFORZAMIENTO": "mensaje-reforzamiento",
        "ERROR": "mensaje-error",
    }
    clase = clases.get(tipo, "mensaje-error")
    st.markdown(f'<div class="{clase}">{texto}</div>', unsafe_allow_html=True)


# ============================================================
# VISTAS
# ============================================================

def pantalla_login():
    st.markdown("""
    <div style="text-align:center; margin-top:60px;">
        <h1 style="font-size:42px;">Sistema de Asistencia</h1>
        <p style="font-size:18px; color:#E65100; font-weight:600;">I.E. Yarinacocha</p>
    </div>
    """, unsafe_allow_html=True)
    _, centro, _ = st.columns([1, 1.2, 1])
    with centro:
        with st.form("login"):
            st.markdown("### Iniciar sesión")
            usuario = st.text_input("Usuario")
            password = st.text_input("Contraseña", type="password")
            if st.form_submit_button("Ingresar", use_container_width=True):
                ok, error = iniciar_sesion(usuario, password)
                if ok:
                    st.rerun()
                else:
                    st.error(error)


def pantalla_cambio_password():
    st.title("Cambio obligatorio de contraseña")
    st.warning("Tu cuenta tiene una contraseña temporal.")
    with st.form("cambio"):
        nueva = st.text_input("Nueva contraseña", type="password")
        confirmar = st.text_input("Confirmar", type="password")
        if st.form_submit_button("Cambiar", use_container_width=True):
            ok, mensaje = validar_password(nueva)
            if not ok:
                st.error(mensaje)
            elif nueva != confirmar:
                st.error("Las contraseñas no coinciden.")
            else:
                con = conexion()
                con.execute(
                    "UPDATE usuarios SET password=?, debe_cambiar_password=0 WHERE id=?",
                    (hashear_password(nueva), usuario_actual()["id"])
                )
                con.commit()
                st.session_state["usuario"]["debe_cambiar_password"] = 0
                auditar("Cambio de contraseña obligatorio")
                st.rerun()


def vista_puerta():
    usuario = usuario_actual()
    feriado = es_feriado_hoy()
    if feriado:
        st.info(f"Hoy es feriado: **{feriado['descripcion']}**.")
        return

    dia = None
    for t in listar_turnos():
        d = dia_especial_de_hoy(t["id"])
        if d:
            dia = d
            break
    if dia and dia["tipo"] == "evento":
        st.success(f"Evento hoy: **{dia['descripcion']}** (entrada {dia['hora_entrada']})")

    if es_fin_de_semana() and not dia:
        st.warning("Hoy no hay clases.")
        return

    st.markdown("""
    <div style="background:#E65100; padding:22px 28px; border-radius:10px; color:white; margin-bottom:20px;">
        <div style="font-size:26px; font-weight:700;">Control de Puerta</div>
    </div>
    """, unsafe_allow_html=True)

    metricas = metricas_de_hoy()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Puntuales", metricas["puntuales"])
    c2.metric("Tardanzas", metricas["tardanzas"])
    c3.metric("Faltas", metricas["faltas"])
    c4.metric("Reforz.", metricas["reforzamiento"])
    c5.metric("Bloqueados", metricas["bloqueados"])

    st.markdown("---")

    try:
        from streamlit_qrcode_scanner import qrcode_scanner
        codigo = qrcode_scanner(key="escaner")
        if codigo:
            match = re.search(r"\b(\d{8})\b", str(codigo))
            if match:
                dni = match.group(1)
                ultimo = st.session_state.get("_ultimo_escaneo", {})
                if not (ultimo.get("dni") == dni and (ahora().timestamp() - ultimo.get("ts", 0)) < 3):
                    st.session_state["_ultimo_escaneo"] = {"dni": dni, "ts": ahora().timestamp()}
                    _, tipo, mensaje, _ = registrar_entrada(dni, usuario)
                    mostrar_mensaje(tipo, mensaje)
    except ImportError:
        st.error("Falta instalar streamlit-qrcode-scanner.")

    st.markdown("---")
    st.subheader("Registros de hoy")
    df = registros_de_hoy()
    if df.empty:
        st.info("Aún no hay escaneos hoy.")
    else:
        st.dataframe(df, use_container_width=True)


def vista_panel():
    st.title("Panel Dirección")

    if st.button("Actualizar", key="refresh_panel"):
        st.rerun()

    metricas = metricas_de_hoy()
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total alumnos", metricas["total"])
    c2.metric("Puntuales", metricas["puntuales"])
    c3.metric("Tardanzas", metricas["tardanzas"])
    c4.metric("Faltas", metricas["faltas"])
    c5.metric("Reforz.", metricas["reforzamiento"])
    c6.metric("Bloqueados", metricas["bloqueados"])

    st.markdown("---")
    st.subheader("Últimos escaneos")
    df = registros_de_hoy(50)
    if df.empty:
        st.info("Sin registros hoy.")
    else:
        st.dataframe(df, use_container_width=True)


def vista_alumnos():
    st.title("Alumnos")
    grados = listar_grados()
    if not grados:
        st.warning("No hay grados.")
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        grado = st.selectbox("Grado", grados, format_func=lambda g: g["nombre"], key="al_g")
    with c2:
        secciones = listar_secciones(grado["id"]) if grado else []
        seccion = st.selectbox("Sección", secciones, format_func=lambda s: s["nombre"], key="al_s") if secciones else None
    with c3:
        texto = st.text_input("Buscar por nombre", key="al_t")

    if seccion:
        df = listar_alumnos_de_seccion(seccion["id"])
    else:
        df = buscar_alumnos(texto=texto, grado_id=grado["id"] if grado else None)

    st.write(f"**{len(df)} alumnos**")
    if df.empty:
        st.info("Sin resultados.")
    else:
        st.dataframe(df[["dni", "nombre_completo"]], use_container_width=True)

    if usuario_actual()["rol"] == ADMIN:
        with st.expander("Crear alumno"):
            with st.form("crear_al"):
                c1, c2 = st.columns(2)
                with c1:
                    dni = st.text_input("DNI")
                    nombres = st.text_input("Nombres")
                    ap_pat = st.text_input("Apellido Paterno")
                with c2:
                    ap_mat = st.text_input("Apellido Materno")
                    secciones_todas = listar_secciones()
                    sec = st.selectbox("Sección", secciones_todas, format_func=lambda s: s["nombre"], key="crear_sec")
                    apo = st.text_input("Apoderado")
                    tel = st.text_input("Teléfono")
                if st.form_submit_button("Crear"):
                    ok, msg = crear_alumno(dni, nombres, ap_pat, ap_mat, sec["id"] if sec else None, apo, tel)
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)


def vista_reportes():
    st.title("Reportes")
    c1, c2, c3 = st.columns(3)
    with c1:
        desde = st.date_input("Desde", ahora().date() - timedelta(days=7))
    with c2:
        hasta = st.date_input("Hasta", ahora().date())
    with c3:
        turno = st.selectbox("Turno", ["Todos", "Mañana", "Tarde"])

    grados = listar_grados()
    c1, c2 = st.columns(2)
    with c1:
        grado = st.selectbox("Grado", [{"id": None, "nombre": "Todos"}] + grados,
                             format_func=lambda g: g["nombre"], key="rep_g")
    with c2:
        if grado and grado["id"]:
            secciones = listar_secciones(grado["id"])
            seccion = st.selectbox("Sección", [{"id": None, "nombre": "Todas"}] + secciones,
                                   format_func=lambda s: s["nombre"], key="rep_s")
        else:
            seccion = None

    seccion_id = seccion["id"] if seccion and seccion["id"] else None
    tipo_reporte = st.selectbox("Tipo", ["Diario", "Mensual", "Conteo de faltas"])
    tipo_asist = st.selectbox("Asistencia", ["clases", "reforzamiento", "todas"])

    if st.button("Generar reporte", type="primary"):
        if tipo_reporte == "Diario":
            df = reporte_diario(str(desde), str(hasta), turno, tipo_asist, seccion_id)
            nombre_archivo = f"reporte_diario_{desde}_{hasta}.xlsx"
        elif tipo_reporte == "Mensual":
            df = reporte_mensual(str(desde), str(hasta), turno, tipo_asist, seccion_id)
            nombre_archivo = f"reporte_mensual_{desde}_{hasta}.xlsx"
        else:
            df = conteo_faltas(str(desde), str(hasta), turno, seccion_id)
            nombre_archivo = f"conteo_faltas_{desde}_{hasta}.xlsx"

        if df.empty:
            st.info("Sin datos para ese rango.")
        else:
            st.write(f"**{len(df)} filas**")
            st.dataframe(df, use_container_width=True, height=500)
            st.download_button("Descargar Excel", exportar_excel(df), nombre_archivo,
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def vista_carnets():
    st.title("Carnets QR")
    grados = listar_grados()
    if not grados:
        st.warning("No hay grados.")
        return
    c1, c2 = st.columns(2)
    with c1:
        grado = st.selectbox("Grado", grados, format_func=lambda g: g["nombre"], key="carnet_g")
    with c2:
        secciones = listar_secciones(grado["id"]) if grado else []
        seccion = st.selectbox("Sección", secciones, format_func=lambda s: s["nombre"], key="carnet_s") if secciones else None
    if not seccion:
        return
    df = listar_alumnos_de_seccion(seccion["id"])
    if df.empty:
        st.info("Sin alumnos.")
        return
    seleccionados = []
    st.write("**Marca los alumnos:**")
    cols = st.columns(3)
    for i, (_, alumno) in enumerate(df.iterrows()):
        with cols[i % 3]:
            if st.checkbox(alumno["nombre_completo"], key=f"carnet_{alumno['id']}"):
                seleccionados.append(alumno["id"])
    if not seleccionados:
        st.info("Marca al menos un alumno.")
        return
    con = conexion()
    placeholders = ",".join("?" * len(seleccionados))
    alumnos = con.execute(f"""
        SELECT a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.id IN ({placeholders})
        ORDER BY a.apellido_paterno, a.apellido_materno
    """, seleccionados).fetchall()
    if st.button(f"Generar {len(alumnos)} carnet(s)", type="primary"):
        pdf = generar_pdf_carnets([dict(a) for a in alumnos])
        st.download_button("Descargar PDF", pdf, "carnets.pdf", "application/pdf")


def vista_dias_especiales():
    st.title("Días especiales y feriados")
    tabs = st.tabs(["Crear", "Listar / eliminar"])
    with tabs[0]:
        with st.form("crear_dia"):
            c1, c2 = st.columns(2)
            with c1:
                fecha = st.date_input("Fecha", min_value=ahora().date())
                descripcion = st.text_input("Descripción")
            with c2:
                tipo = st.radio("Tipo", ["evento", "feriado"], horizontal=True)
                if tipo == "evento":
                    turnos = listar_turnos()
                    turno_opts = [{"id": None, "nombre": "Ambos"}] + turnos
                    turno = st.selectbox("Turno", turno_opts, format_func=lambda t: t["nombre"])
                    hora_entrada = st.time_input("Hora de entrada", value=datetime.strptime("08:00", "%H:%M").time())
                else:
                    turno = None
                    hora_entrada = None
            if st.form_submit_button("Crear", type="primary"):
                if not descripcion.strip():
                    st.error("Descripción requerida.")
                else:
                    hora_txt = hora_entrada.strftime("%H:%M") if hora_entrada else None
                    turno_id = turno["id"] if turno and turno.get("id") else None
                    crear_dia_especial(str(fecha), descripcion.strip(), tipo, turno_id, hora_txt)
                    st.success("Día especial creado.")
                    st.rerun()
    with tabs[1]:
        df = listar_dias_especiales()
        if df.empty:
            st.info("Sin días especiales.")
        else:
            st.dataframe(df, use_container_width=True)
            ops = {f"{r['fecha']} - {r['descripcion']} ({r['tipo']})": r["id"] for _, r in df.iterrows()}
            sel = st.selectbox("Eliminar", list(ops.keys()))
            pwd = st.text_input("Contraseña de Admin", type="password", key="pwd_del")
            if st.button("Eliminar", type="primary"):
                if not verificar_admin(pwd):
                    st.error("Contraseña incorrecta.")
                else:
                    eliminar_dia_especial(ops[sel])
                    st.success("Eliminado.")
                    st.rerun()


def vista_ventanas():
    st.title("Ventanas de asistencia")
    st.caption("Configura apertura, límite puntual y cierre. NO deben solaparse dentro del mismo turno.")

    for turno in listar_turnos():
        st.subheader(f"Turno {turno['nombre']}")
        con = conexion()
        ventanas = con.execute(
            "SELECT * FROM ventanas WHERE turno_id=? ORDER BY id", (turno["id"],)
        ).fetchall()
        for v in ventanas:
            with st.expander(f"{v['nombre']} — {v['tipo']}"):
                with st.form(f"ventana_{v['id']}"):
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        try:
                            ap_dt = datetime.strptime(v["hora_apertura"], "%H:%M").time()
                        except (ValueError, TypeError):
                            ap_dt = datetime.strptime("08:00", "%H:%M").time()
                        ap = st.time_input("Apertura", value=ap_dt, key=f"ap_{v['id']}")
                    with c2:
                        try:
                            lim_dt = datetime.strptime(v["hora_limite_puntual"], "%H:%M").time() if v["hora_limite_puntual"] else ap_dt
                        except (ValueError, TypeError):
                            lim_dt = ap_dt
                        lim = st.time_input("Límite puntual", value=lim_dt, key=f"lim_{v['id']}")
                    with c3:
                        try:
                            ci_dt = datetime.strptime(v["hora_cierre"], "%H:%M").time()
                        except (ValueError, TypeError):
                            ci_dt = datetime.strptime("18:00", "%H:%M").time()
                        ci = st.time_input("Cierre", value=ci_dt, key=f"ci_{v['id']}")

                    if st.form_submit_button("Guardar", type="primary"):
                        con.execute("""
                            UPDATE ventanas
                            SET hora_apertura=?, hora_limite_puntual=?, hora_cierre=?
                            WHERE id=?
                        """, (ap.strftime("%H:%M"), lim.strftime("%H:%M"),
                              ci.strftime("%H:%M"), v["id"]))
                        con.commit()
                        auditar(f"Editó ventana id={v['id']}", "ventanas", v["id"])
                        st.success("Ventana actualizada.")
                        st.rerun()


def vista_bloqueados():
    st.title("Alumnos bloqueados")
    df = listar_bloqueados()
    if df.empty:
        st.info("Sin bloqueados.")
        return
    st.dataframe(df, use_container_width=True)
    ops = {f"{r['alumno']} ({r['dni']}) - {r['motivo']}": r["alumno_id"] for _, r in df.iterrows()}
    sel = st.selectbox("Liberar", list(ops.keys()))
    pwd = st.text_input("Contraseña de Admin", type="password", key="pwd_lib")
    if st.button("Liberar bloqueo", type="primary"):
        if not verificar_admin(pwd):
            st.error("Contraseña incorrecta.")
        else:
            liberar_bloqueo(ops[sel])
            st.success("Bloqueo liberado.")
            st.rerun()


def vista_observados():
    st.title("Alumnos observados")
    tabs = st.tabs(["Listar", "Crear", "Cerrar"])
    with tabs[0]:
        solo = st.checkbox("Solo activos", value=True)
        df = listar_observados(solo)
        if df.empty:
            st.info("Sin observados.")
        else:
            st.dataframe(df, use_container_width=True)
    with tabs[1]:
        texto = st.text_input("Buscar alumno", key="obs_buscar")
        if texto:
            df = buscar_alumnos(texto=texto, limite=30)
            if not df.empty:
                ops = {f"{r['nombre_completo']} ({r['dni']})": r["id"] for _, r in df.iterrows()}
                sel = st.selectbox("Alumno", list(ops.keys()))
                motivo = st.text_input("Motivo")
                if st.button("Crear observado", type="primary"):
                    if not motivo.strip():
                        st.error("Motivo requerido.")
                    else:
                        crear_observado(ops[sel], motivo.strip())
                        st.success("Observado creado.")
                        st.rerun()
    with tabs[2]:
        df = listar_observados(solo_activos=True)
        if df.empty:
            st.info("No hay observados activos.")
        else:
            ops = {f"{r['apellidos']}, {r['nombres']} ({r['dni']})": r["id"] for _, r in df.iterrows()}
            sel = st.selectbox("Observado a cerrar", list(ops.keys()))
            obs = st.text_input("Observación de cierre")
            if st.button("Cerrar observado", type="primary"):
                cerrar_observado(ops[sel], obs)
                st.success("Cerrado.")
                st.rerun()


def vista_usuarios():
    st.title("Usuarios")
    con = conexion()
    df = pd.read_sql("SELECT id, usuario, rol, nombres, activo FROM usuarios ORDER BY usuario", con)
    st.dataframe(df, use_container_width=True)

    st.markdown("---")
    st.subheader("Crear usuario")
    with st.form("crear_usuario"):
        c1, c2 = st.columns(2)
        with c1:
            usuario = st.text_input("Usuario")
            password = st.text_input("Contraseña", type="password")
        with c2:
            nombres = st.text_input("Nombres")
            rol = st.selectbox("Rol", [TOECE, AUXILIAR, DIRECCION])
        admin_password = st.text_input("Tu contraseña de Admin", type="password")
        if st.form_submit_button("Crear", type="primary"):
            if not verificar_admin(admin_password):
                st.error("Contraseña de Admin incorrecta.")
            else:
                try:
                    con.execute(
                        "INSERT INTO usuarios(usuario, password, rol, nombres) VALUES(?,?,?,?)",
                        (usuario, hashear_password(password), rol, nombres)
                    )
                    con.commit()
                    auditar(f"Creó usuario {usuario}", "usuarios")
                    st.success(f"Usuario {usuario} creado.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Usuario ya existe.")


def vista_auditoria():
    st.title("Auditoría")
    con = conexion()
    df = pd.read_sql("""
        SELECT id, usuario, accion, fecha, COALESCE(tabla_afectada, '-') AS tabla
        FROM auditoria
        ORDER BY id DESC
        LIMIT 500
    """, con)
    st.write(f"**{len(df)} registros**")
    if df.empty:
        st.info("Sin registros.")
    else:
        st.dataframe(df, use_container_width=True)


# ============================================================
# MENÚ Y ENRUTAMIENTO
# ============================================================

MENU_POR_ROL = {
    ADMIN: ["Puerta", "Panel Dirección", "Alumnos", "Reportes", "Carnets",
            "Días especiales", "Ventanas", "Bloqueados", "Observados", "Usuarios", "Auditoría"],
    TOECE: ["Puerta", "Bloqueados", "Observados", "Reportes", "Días especiales"],
    DIRECCION: ["Puerta", "Panel Dirección", "Reportes", "Carnets", "Ventanas", "Auditoría"],
    AUXILIAR: ["Puerta", "Reportes"],
}

VISTAS = {
    "Puerta": vista_puerta,
    "Panel Dirección": vista_panel,
    "Alumnos": vista_alumnos,
    "Reportes": vista_reportes,
    "Carnets": vista_carnets,
    "Días especiales": vista_dias_especiales,
    "Ventanas": vista_ventanas,
    "Bloqueados": vista_bloqueados,
    "Observados": vista_observados,
    "Usuarios": vista_usuarios,
    "Auditoría": vista_auditoria,
}


def menu_lateral():
    usuario = usuario_actual()
    opciones = MENU_POR_ROL.get(usuario["rol"], [])
    with st.sidebar:
        st.markdown(f"""
        <div style="text-align:center; padding:16px; border:1px solid #ddd; border-radius:8px; margin-bottom:10px;">
            <div style="font-size:32px;">👤</div>
            <div style="font-weight:700;">{usuario['nombres']}</div>
            <div style="font-size:11px; color:#666; text-transform:uppercase;">{usuario['rol']}</div>
        </div>
        """, unsafe_allow_html=True)
        opcion = st.radio("Menú", opciones, label_visibility="collapsed")
        st.markdown("---")
        if st.button("Cerrar sesión", use_container_width=True):
            cerrar_sesion()
            st.rerun()
    return opcion


# ============================================================
# MAIN
# ============================================================

def main():
    st.set_page_config(
        page_title="Asistencia I.E. Yarinacocha",
        page_icon="📋",
        layout="wide",
    )

    crear_tablas()
    aplicar_estilos()

    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=60_000, key="refresco_faltas")
    except ImportError:
        pass

    try:
        marcar_faltas_del_dia()
    except Exception:
        pass

    if "usuario" not in st.session_state:
        pantalla_login()
        return

    usuario = usuario_actual()
    if usuario.get("debe_cambiar_password"):
        pantalla_cambio_password()
        return

    opcion = menu_lateral()
    vista = VISTAS.get(opcion)
    if vista:
        vista()


if __name__ == "__main__":
    main()
