# Sistema de Control de Asistencia Escolar

Sistema web para el registro y gestión de asistencia de la I.E. Yarinacocha, desarrollado con Python, Streamlit y SQLite como proyecto de tesis.

Permite registrar la entrada de los alumnos mediante código QR o desde una lista por sección, aplicando automáticamente las reglas institucionales de puntualidad, tardanzas, faltas y reforzamiento.

## Características

- Registro de asistencia por código QR o lista por sección
- Control de tardanzas con reglas institucionales: 1ª y 2ª perdonadas, 3ª derivado a TOECE, 4ª en adelante retenido
- Gestión de actas de compromiso
- Lista de alumnos observados
- Reportes exportables a PDF y Excel
- Generación de carnets con código QR
- Panel de dirección con gráficos en tiempo real
- Módulo de reforzamiento con ventanas de tiempo configurables
- Días especiales: feriados y eventos con horario personalizado
- Sistema de roles: Admin, TOECE, Dirección, Auxiliar y Docente de Reforzamiento
- Login persistente con cookies
- Auditoría de todas las acciones del sistema

## Módulos del sistema

Puerta: registro de entrada por QR o lista manual.

TOECE: gestión de derivados, actas y alumnos observados.

Panel Dirección: métricas del día, alertas y gráficos.

Reportes y Consultas: reportes filtrados por período, turno, grado y sección.

Alumnos: CRUD de alumnos e importación desde Excel.

Carnets: generación de PDFs con código QR.

Días Especiales: configuración de feriados, eventos y reforzamientos.

Reforzamiento: escaneo QR exclusivo para alumnos asignados.

Horarios: configuración de puertas de entrada y reforzamiento por turno.

Usuarios: gestión de cuentas y roles.

Auditoría: registro histórico de todas las acciones.

## Reglas de negocio

### Turnos

Cada turno tiene su propia ventana de tiempo.

Apertura de puerta: hora desde la cual se aceptan escaneos QR.

Tolerancia: minutos de gracia para considerar puntual.

Cierre de puerta: hora límite para escanear. Después se marca Falta.

### Estados de asistencia

Puntual: escaneo dentro de apertura más tolerancia.

Tardanza: escaneo después de la tolerancia pero antes del cierre.

Falta: no escaneó y ya cerró la puerta.

### Reglas de tardanzas

1ª y 2ª tardanza: perdonadas.

3ª tardanza: derivado a TOECE.

4ª tardanza en adelante: retenido hasta que llegue el apoderado.

### Reforzamiento

Cada turno tiene configurado su horario de reforzamiento.

Turno Mañana: reforzamiento después de clases, por ejemplo de 12:20 a 14:00.

Turno Tarde: reforzamiento antes de clases, por ejemplo de 11:00 a 12:20.

Un alumno puede tener dos escaneos el mismo día: uno en clases y otro en reforzamiento. Son registros independientes.

Solo los alumnos asignados al reforzamiento pueden escanear en esa ventana.

## Arquitectura

El sistema tiene tres capas.

La capa de presentación está hecha con Streamlit. Incluye las vistas, los fragmentos y el CSS.

La capa de lógica contiene las funciones de asistencia, horarios y autenticación.

La capa de datos usa SQLite en modo WAL para permitir lecturas concurrentes.

El navegador del usuario se conecta al servidor de Streamlit, que a su vez lee y escribe en la base de datos SQLite.

## Requisitos

Python 3.11 o superior.

pip, el gestor de paquetes de Python.

Navegador moderno: Chrome, Edge o Firefox.

Cámara para el escaneo QR.

HTTPS recomendado para el uso de cámara en móvil.

## Instalación

Primero clona el repositorio:

git clone https://github.com/asisyarina/qrcny.git
cd qrcny

Luego crea un entorno virtual. En Windows:

python -m venv .venv
.venv\Scripts\activate

En Linux o Mac:

python3 -m venv .venv
source .venv/bin/activate

Después instala las dependencias:

pip install -r requirements.txt

Y ejecuta la aplicación:

streamlit run f.py

La aplicación se abre en http://localhost:8501

## Configuración inicial

### Credenciales por defecto

Usuario admin con contraseña admin2026, rol Admin.

Usuario toece con contraseña toece2026, rol TOECE.

Usuario direccion con contraseña dir2026, rol Dirección.

Usuario aux_m con contraseña auxm2026, rol Auxiliar Mañana.

Usuario aux_t con contraseña auxt2026, rol Auxiliar Tarde.

Es importante cambiar todas las contraseñas después del primer inicio de sesión desde el módulo Usuarios.

### Orden de configuración recomendado

Primero configura los horarios: apertura, tolerancia, cierre y reforzamiento por turno.

Luego importa los alumnos desde Excel o créalos manualmente.

Después crea las cuentas del personal en el módulo Usuarios.

Genera e imprime los códigos QR desde el módulo Carnets.

Finalmente programa los feriados, eventos y reforzamientos en Días Especiales.

## Uso

### Para Auxiliares

Inicia sesión con aux_m o aux_t.

Ve al módulo Puerta.

Escanea el QR del alumno con la cámara.

El sistema registra automáticamente Puntual, Tardanza o Falta.

### Para TOECE

Revisa la sección Derivados hoy en el módulo TOECE.

Firma las actas de compromiso.

Gestiona la lista de alumnos observados.

### Para Dirección

Consulta el Panel Dirección con las métricas del día.

Revisa Reportes y Consultas por período.

Genera carnets de alumnos.

### Para Admin

Importa alumnos desde Excel.

Configura horarios y días especiales.

Gestiona usuarios y roles.

Revisa la auditoría.

## Estructura del proyecto

El proyecto tiene los siguientes archivos en su raíz.

f.py es la aplicación principal.

escudo.png es el logo institucional.

asistencia.db es la base de datos SQLite.

requirements.txt contiene las dependencias del proyecto.

README.md es este archivo.

La carpeta .streamlit contiene la configuración de Streamlit en el archivo config.toml.

El sistema está implementado como un solo archivo, f.py, para simplificar el despliegue en el colegio. Internamente está organizado por secciones bien delimitadas.

## Base de datos

El sistema usa SQLite en modo WAL para permitir lecturas concurrentes.

Las tablas principales son las siguientes.

turnos guarda la configuración de horarios por turno.

grados guarda los grados académicos.

secciones guarda las secciones, por ejemplo 1°A o 2°B.

alumnos guarda los datos personales y del apoderado.

asistencias guarda el registro diario, tanto de clases como de reforzamiento.

tardanzas guarda el historial de tardanzas por alumno.

actas_compromiso guarda las actas firmadas.

observados guarda los alumnos en seguimiento.

dias_especiales guarda los feriados, eventos y reforzamientos.

reforzamiento_alumnos guarda los alumnos asignados a cada reforzamiento.

usuarios guarda las cuentas del sistema.

auditoria guarda el registro de acciones.

sesiones_tokens guarda los tokens para el login persistente.

### Backup

Se recomienda hacer backup diario del archivo asistencia.db.

En Windows:

copy asistencia.db backups\asistencia_%date%.db

En Linux o Mac:

cp asistencia.db backups/asistencia_$(date +%Y%m%d).db

## Seguridad

Las contraseñas se almacenan con PBKDF2-HMAC-SHA256 y 260,000 iteraciones.

Los tokens de sesión tienen una expiración de 30 días.

Los hashes antiguos con SHA-256 plano se migran automáticamente al primer inicio de sesión.

Todas las acciones sensibles quedan registradas en la auditoría.

Cada rol tiene acceso solo a sus módulos correspondientes.

## Limitaciones conocidas

La cámara en móvil a veces no abre la cámara trasera con st.camera_input. Se recomienda usar HTTPS.

SQLite soporta pocos usuarios simultáneos, lo cual es adecuado para el colegio.

No se puede instalar como aplicación nativa con ícono propio.

El Panel Dirección se actualiza cada 30 segundos, no en tiempo real.

## Trabajo futuro

Migración a PostgreSQL para mayor concurrencia.

Aplicación móvil nativa con Flet o Kivy.

Módulo de informes con análisis automático y recomendaciones.

Notificaciones por WhatsApp a apoderados.

Integración con el sistema SIAGIE del MINEDU.

Modo offline con sincronización diferida.

Dashboard con indicadores por bimestre.

## Autor

JUAN ADRIANO DEL AGUILA MANANIYA

Proyecto de Tesis.

2026

## Licencia

Este proyecto fue desarrollado con fines académicos para la I.E. Yarinacocha. Todos los derechos reservados.
