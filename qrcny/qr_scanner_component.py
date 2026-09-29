# qr_scanner_component.py
# Componente de escaneo QR para Streamlit con SOLO LUCES (sin sonido).
#
# LUCES:
#   Verde      -> Puntual
#   Amarillo   -> Tardanza
#   Rojo       -> Duplicado / ya registrado
#   Azul       -> Reforzamiento
#   Rojo flash -> Bloqueado
#
# FLUJO:
#   - Al detectar un QR, se muestra la luz.
#   - Se destruye el scanner inmediatamente.
#   - Aparece boton "Escanear siguiente alumno".
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_luz_v1",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <div id="qr-luz" class="luz-off">
            <div class="luz-circulo"></div>
            <div class="luz-texto">Listo</div>
        </div>
        <div id="qr-reader"></div>
        <div id="qr-status">Iniciando camara...</div>
        <div id="qr-error" style="display:none;"></div>
        <button id="qr-reanudar" style="display:none; margin-top:14px; padding:14px 24px; background:#E65100; color:white; border:none; border-radius:8px; font-weight:700; font-size:16px; cursor:pointer; width:100%; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
            Escanear siguiente alumno
        </button>
    </div>
    """,
    css="""
    #qr-wrapper { width: 100%; max-width: 500px; margin: 0 auto; }

    /* LUZ GRANDE */
    #qr-luz {
        border-radius: 14px;
        padding: 22px 16px;
        margin-bottom: 14px;
        text-align: center;
        transition: background 0.25s ease, box-shadow 0.25s ease;
        border: 3px solid transparent;
    }
    #qr-luz .luz-circulo {
        width: 100px;
        height: 100px;
        border-radius: 50%;
        margin: 0 auto 12px auto;
        background: #333;
        transition: background 0.25s ease, box-shadow 0.25s ease;
        box-shadow: inset 0 4px 12px rgba(0,0,0,0.35);
    }
    #qr-luz .luz-texto {
        font-size: 22px;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        color: #FFF;
    }

    .luz-off {
        background: #1a1a1a;
        border-color: #333;
    }
    .luz-off .luz-circulo { background: #333; }
    .luz-off .luz-texto { color: #888; }

    .luz-verde {
        background: #0d3d13;
        border-color: #2ecc40;
        box-shadow: 0 0 40px rgba(46, 204, 64, 0.6);
    }
    .luz-verde .luz-circulo {
        background: #2ecc40;
        box-shadow: 0 0 40px #2ecc40, 0 0 80px rgba(46, 204, 64, 0.5);
    }
    .luz-verde .luz-texto { color: #b8ffb8; }

    .luz-amarillo {
        background: #3d3305;
        border-color: #ffcc00;
        box-shadow: 0 0 40px rgba(255, 204, 0, 0.6);
    }
    .luz-amarillo .luz-circulo {
        background: #ffcc00;
        box-shadow: 0 0 40px #ffcc00, 0 0 80px rgba(255, 204, 0, 0.5);
    }
    .luz-amarillo .luz-texto { color: #fff3a8; }

    .luz-rojo {
        background: #3d0808;
        border-color: #ff2b2b;
        box-shadow: 0 0 40px rgba(255, 43, 43, 0.7);
    }
    .luz-rojo .luz-circulo {
        background: #ff2b2b;
        box-shadow: 0 0 40px #ff2b2b, 0 0 80px rgba(255, 43, 43, 0.5);
    }
    .luz-rojo .luz-texto { color: #ffb8b8; }

    .luz-azul {
        background: #05233d;
        border-color: #2196f3;
        box-shadow: 0 0 40px rgba(33, 150, 243, 0.6);
    }
    .luz-azul .luz-circulo {
        background: #2196f3;
        box-shadow: 0 0 40px #2196f3, 0 0 80px rgba(33, 150, 243, 0.5);
    }
    .luz-azul .luz-texto { color: #b8e0ff; }

    .luz-bloqueado {
        background: #3d0808;
        border-color: #ff2b2b;
        animation: parpadeo 0.5s infinite alternate;
    }
    .luz-bloqueado .luz-circulo {
        background: #ff2b2b;
        box-shadow: 0 0 60px #ff2b2b, 0 0 120px rgba(255, 43, 43, 0.8);
    }
    .luz-bloqueado .luz-texto { color: #fff; }
    @keyframes parpadeo {
        from { opacity: 1; box-shadow: 0 0 60px #ff2b2b; }
        to   { opacity: 0.55; box-shadow: 0 0 20px #ff2b2b; }
    }

    /* Camara */
    #qr-reader {
        border-radius: 8px; overflow: hidden;
        border: 2px solid #E65100; background: #000; min-height: 260px;
    }
    #qr-reader video { border-radius: 6px; width: 100% !important; height: auto !important; }
    #qr-status {
        text-align: center; font-size: 13px; margin-top: 8px; color: #666;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    #qr-error {
        text-align: center; font-size: 13px; margin-top: 8px; color: #C62828;
        font-weight: 600;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        padding: 10px; border: 1px solid #C62828; border-radius: 6px; background: #f8d7da;
    }
    #qr-reader button {
        background: #E65100 !important; color: white !important;
        border: none !important; border-radius: 6px !important;
        padding: 8px 16px !important; font-weight: 600 !important;
        cursor: pointer !important; margin: 4px !important;
    }
    #qr-reader button:hover { background: #BF360C !important; }
    #qr-reader select {
        border-radius: 6px !important; padding: 6px 10px !important;
        margin: 4px !important; border: 1px solid #ccc !important;
    }
    #qr-reader a { color: #E65100 !important; font-weight: 600 !important; }
    #qr-reanudar:hover { background: #BF360C !important; }
    """,
    js="""
    export default function(component) {
        const { setTriggerValue, data } = component;

        // ============================================================
        // DATOS INICIALES DEL BACKEND
        // ============================================================
        let yaRegistrados = new Map();
        let bloqueados = new Set();

        try {
            const yr = (data && data.ya_registrados) || {};
            for (const k of Object.keys(yr)) yaRegistrados.set(k, yr[k]);
            const bl = (data && data.bloqueados) || [];
            for (const b of bl) bloqueados.add(b);
            console.log('[QR] Estado inicial:', yaRegistrados.size, 'registrados,', bloqueados.size, 'bloqueados');
        } catch (e) {
            console.warn('[QR] no se pudo cargar estado inicial:', e);
        }

        let scanner = null;
        let iniciado = false;
        let reiniciando = false;
        let detenidoPorLectura = false;

        // ============================================================
        // DECISION LOCAL DE LUZ (sin sonido)
        // ============================================================
        function decidirLocal(dni) {
            if (bloqueados.has(dni)) {
                return { luz: "bloqueado", texto: "BLOQUEADO" };
            }
            if (yaRegistrados.has(dni)) {
                const info = yaRegistrados.get(dni);
                if (info && info.estado === "Tardanza") {
                    return { luz: "amarillo", texto: "TARDANZA" };
                }
                if (info && info.estado === "Reforzamiento") {
                    return { luz: "azul", texto: "REFORZAMIENTO" };
                }
                return { luz: "rojo", texto: "YA REGISTRO" };
            }
            return { luz: "verde", texto: "PUNTUAL" };
        }

        // ============================================================
        // LUCES
        // ============================================================
        function apagarLuz() {
            const luz = document.getElementById('qr-luz');
            if (!luz) return;
            luz.className = 'luz-off';
            const txt = luz.querySelector('.luz-texto');
            if (txt) txt.textContent = 'Listo';
        }

        function encenderLuz(color, texto) {
            const luz = document.getElementById('qr-luz');
            if (!luz) return;
            luz.className = '';
            luz.classList.add('luz-' + color);
            const txt = luz.querySelector('.luz-texto');
            if (txt) txt.textContent = texto;
        }

        // ============================================================
        // UTILIDADES UI
        // ============================================================
        function setStatus(t) {
            const el = document.getElementById('qr-status');
            if (el) { el.textContent = t; el.style.display = 'block'; }
        }
        function setError(t) {
            const e = document.getElementById('qr-error');
            const s = document.getElementById('qr-status');
            if (e) { e.textContent = t; e.style.display = 'block'; }
            if (s) s.style.display = 'none';
            console.error('[QR]', t);
        }

        function mostrarBotonReanudar() {
            const btn = document.getElementById('qr-reanudar');
            if (btn) btn.style.display = 'block';
        }
        function ocultarBotonReanudar() {
            const btn = document.getElementById('qr-reanudar');
            if (btn) btn.style.display = 'none';
        }

        function destruirScanner() {
            if (scanner) {
                try { scanner.clear().catch(() => {}); } catch (e) {}
                scanner = null;
            }
            iniciado = false;
        }

        // ============================================================
        // DETENER SCANNER TRAS CADA LECTURA
        // ============================================================
        function detenerScanner() {
            if (detenidoPorLectura) return;
            detenidoPorLectura = true;

            try {
                if (scanner && typeof scanner.clear === 'function') {
                    scanner.clear().catch(() => {});
                }
            } catch (e) {}
            scanner = null;
            iniciado = false;

            mostrarBotonReanudar();
            setStatus('Escaneo completado. Click en el boton para el siguiente.');

            console.log('[QR] Scanner detenido - esperando click');
        }

        // ============================================================
        // SCANNER
        // ============================================================
        function iniciarScanner() {
            ocultarBotonReanudar();
            detenidoPorLectura = false;
            apagarLuz();

            if (iniciado) return;
            if (typeof Html5QrcodeScanner === 'undefined') {
                setError('Libreria QR no cargada.');
                return;
            }
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                setError('Tu navegador no soporta camara, o no estas en HTTPS.');
                return;
            }
            const reader = document.getElementById('qr-reader');
            if (!reader) { setError('Contenedor qr-reader no existe.'); return; }

            destruirScanner();
            reader.innerHTML = '';
            iniciado = true;

            setTimeout(() => {
                if (!iniciado) return;

                scanner = new Html5QrcodeScanner(
                    "qr-reader",
                    {
                        fps: 10,
                        qrbox: { width: 250, height: 250 },
                        aspectRatio: 1.0,
                        rememberLastUsedCamera: true,
                        videoConstraints: { facingMode: "environment" },
                        disableFlip: true,
                        supportedScanTypes: [Html5QrcodeScanType.SCAN_TYPE_CAMERA]
                    },
                    false
                );

                const onScanSuccess = (texto) => {
                    try {
                        if (detenidoPorLectura) return;

                        const m = texto.match(/\\b(\\d{8})\\b/);
                        if (!m) return;
                        const dni = m[1];

                        setStatus('QR: ' + dni);

                        // Decidir luz
                        const d = decidirLocal(dni);
                        encenderLuz(d.luz, d.texto);

                        // Actualizar cache local
                        if (d.luz === "verde") {
                            yaRegistrados.set(dni, { estado: "Puntual", hora: "ahora" });
                        }

                        // Enviar a Python
                        setTriggerValue("qr_dni", dni);

                        // Detener scanner
                        detenerScanner();
                    } catch (e) {
                        console.error('[QR] error en onScanSuccess:', e);
                    }
                };

                const onScanError = () => {};

                let resultado;
                try {
                    resultado = scanner.render(onScanSuccess, onScanError);
                } catch (e) {
                    const msg = (e && e.message) ? e.message : String(e);
                    setError('Error al iniciar: ' + msg);
                    iniciado = false;
                    return;
                }

                if (resultado && typeof resultado.then === 'function') {
                    resultado
                        .then(() => setStatus('Camara activa.'))
                        .catch((e) => {
                            const msg = (e && e.message) ? e.message : String(e);
                            setError('Error camara: ' + msg);
                            iniciado = false;
                        });
                } else {
                    setStatus('Camara activa.');
                }
            }, 500);
        }

        // ============================================================
        // FIX MOVIL
        // ============================================================
        function pausarCamara() {
            try {
                document.querySelectorAll('#qr-reader video').forEach(v => {
                    if (v.srcObject) {
                        v.srcObject.getVideoTracks().forEach(t => {
                            try { t.enabled = false; } catch (e) {}
                        });
                    }
                });
            } catch (e) {}
        }

        function reanudarCamara() {
            try {
                let vivo = false;
                document.querySelectorAll('#qr-reader video').forEach(v => {
                    if (v.srcObject) {
                        v.srcObject.getVideoTracks().forEach(t => {
                            try {
                                if (t.readyState === 'live') { t.enabled = true; vivo = true; }
                            } catch (e) {}
                        });
                    }
                });
                return vivo;
            } catch (e) { return false; }
        }

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                setTimeout(() => {
                    if (detenidoPorLectura) return;
                    const ok = reanudarCamara();
                    if (!ok && iniciado && !reiniciando) {
                        reiniciando = true;
                        try {
                            const viejo = scanner;
                            scanner = null;
                            iniciado = false;
                            if (viejo) { try { viejo.clear(); } catch(e){} }
                        } catch (e) {}
                        setTimeout(() => {
                            reiniciando = false;
                            iniciarScanner();
                        }, 600);
                    } else if (ok) {
                        setStatus('Camara activa.');
                    }
                }, 400);
            } else {
                pausarCamara();
            }
        });

        window.addEventListener('pageshow', () => {
            setTimeout(() => { if (iniciado && !detenidoPorLectura) reanudarCamara(); }, 300);
        });

        // ============================================================
        // BOTON REANUDAR
        // ============================================================
        setTimeout(() => {
            const btn = document.getElementById('qr-reanudar');
            if (btn) {
                btn.addEventListener('click', () => {
                    console.log('[QR] Click en reanudar');
                    detenidoPorLectura = false;
                    iniciarScanner();
                });
            }
        }, 200);

        // ============================================================
        // CARGA DE LIBRERIA
        // ============================================================
        window.addEventListener('beforeunload', destruirScanner);

        if (window.__qrLuzV1Listo && typeof Html5QrcodeScanner !== 'undefined') {
            iniciarScanner();
            return;
        }
        if (window.__qrLuzV1Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (typeof Html5QrcodeScanner !== 'undefined') {
                    clearInterval(t);
                    window.__qrLuzV1Listo = true;
                    window.__qrLuzV1Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrLuzV1Cargando = false;
                    setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }
        window.__qrLuzV1Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode';
        s.async = true;
        s.onload = () => {
            window.__qrLuzV1Listo = true;
            window.__qrLuzV1Cargando = false;
            setTimeout(() => {
                if (typeof Html5QrcodeScanner === 'undefined') {
                    setError('Libreria cargada pero sin Html5QrcodeScanner.');
                    return;
                }
                iniciarScanner();
            }, 50);
        };
        s.onerror = () => {
            window.__qrLuzV1Cargando = false;
            setError('Error al cargar html5-qrcode del CDN.');
        };
        document.head.appendChild(s);
    }
    """,
)


def qr_scanner(key="qr_scanner", on_scan=None, ya_registrados=None, bloqueados=None):
    """
    ya_registrados: dict {dni: {"estado": "Puntual"|"Tardanza"|"Reforzamiento"|..., "hora": "HH:MM"}}
    bloqueados: iterable de DNIs bloqueados
    """
    if on_scan is None:
        on_scan = lambda: None
    data = {
        "ya_registrados": ya_registrados or {},
        "bloqueados": list(bloqueados or []),
    }
    return QR_SCANNER_COMPONENT(
        key=key,
        data=data,
        on_qr_dni_change=on_scan,
    )
