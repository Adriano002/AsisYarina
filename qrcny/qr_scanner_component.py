# qr_scanner_component.py
# Componente propio de escaneo QR para Streamlit.
# Incluye pitido moderno al detectar un QR (para referencia del auxiliar).
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v8",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <div id="qr-reader"></div>
        <div id="qr-status">Iniciando camara...</div>
        <div id="qr-error" style="display:none;"></div>
    </div>
    """,
    css="""
    #qr-wrapper {
        width: 100%;
        max-width: 500px;
        margin: 0 auto;
    }
    #qr-reader {
        border-radius: 8px;
        overflow: hidden;
        border: 2px solid #E65100;
        background: #000;
        min-height: 260px;
    }
    #qr-reader video {
        border-radius: 6px;
        width: 100% !important;
        height: auto !important;
    }
    #qr-status {
        text-align: center;
        font-size: 13px;
        margin-top: 8px;
        color: #666;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    #qr-error {
        text-align: center;
        font-size: 13px;
        margin-top: 8px;
        color: #C62828;
        font-weight: 600;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        padding: 10px;
        border: 1px solid #C62828;
        border-radius: 6px;
        background: #f8d7da;
    }
    #qr-reader button {
        background: #E65100 !important;
        color: white !important;
        border: none !important;
        border-radius: 6px !important;
        padding: 8px 16px !important;
        font-weight: 600 !important;
        cursor: pointer !important;
        margin: 4px !important;
    }
    #qr-reader button:hover {
        background: #BF360C !important;
    }
    #qr-reader select {
        border-radius: 6px !important;
        padding: 6px 10px !important;
        margin: 4px !important;
        border: 1px solid #ccc !important;
    }
    #qr-reader a {
        color: #E65100 !important;
        font-weight: 600 !important;
    }
    """,
    js="""
    export default function(component) {
        const { setTriggerValue } = component;
        let ultimoDni = null;
        let ultimoTs = 0;
        let scanner = null;
        let iniciado = false;
        let ultimoPitidoTs = 0;

        // ============================================================
        // MOTOR DE AUDIO (Web Audio API) - sin archivos externos
        // ============================================================
        let audioCtx = null;
        function getAudioCtx() {
            if (!audioCtx) {
                try {
                    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                } catch (e) {
                    console.warn('[QR] no se pudo crear AudioContext:', e);
                }
            }
            if (audioCtx && audioCtx.state === 'suspended') {
                audioCtx.resume().catch(() => {});
            }
            return audioCtx;
        }

        // Pitido moderno: dos notas ascendentes, suave y limpio
        function pitidoSimple() {
            const ctx = getAudioCtx();
            if (!ctx) return;

            const ahora = Date.now();
            if (ahora - ultimoPitidoTs < 800) return; // anti-spam
            ultimoPitidoTs = ahora;

            try {
                // Nota 1: DO agudo (523 Hz) - suave
                const osc1 = ctx.createOscillator();
                const gain1 = ctx.createGain();
                osc1.connect(gain1);
                gain1.connect(ctx.destination);
                osc1.type = 'sine';
                osc1.frequency.setValueAtTime(523, ctx.currentTime);

                // Envolvente suave: fade-in 15ms, fade-out 100ms
                gain1.gain.setValueAtTime(0, ctx.currentTime);
                gain1.gain.linearRampToValueAtTime(0.35, ctx.currentTime + 0.015);
                gain1.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.12);

                osc1.start(ctx.currentTime);
                osc1.stop(ctx.currentTime + 0.14);

                // Nota 2: MI agudo (659 Hz) - sube un poquito
                const t2 = ctx.currentTime + 0.09;
                const osc2 = ctx.createOscillator();
                const gain2 = ctx.createGain();
                osc2.connect(gain2);
                gain2.connect(ctx.destination);
                osc2.type = 'sine';
                osc2.frequency.setValueAtTime(659, t2);

                gain2.gain.setValueAtTime(0, t2);
                gain2.gain.linearRampToValueAtTime(0.35, t2 + 0.015);
                gain2.gain.exponentialRampToValueAtTime(0.001, t2 + 0.15);

                osc2.start(t2);
                osc2.stop(t2 + 0.17);
            } catch (e) {
                console.warn('[QR] error pitido:', e);
            }
        }

        // ============================================================
        // UTILIDADES
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
        function destruirScanner() {
            if (scanner) {
                try { scanner.clear(); } catch (e) {}
                scanner = null;
            }
            iniciado = false;
        }

        // ============================================================
        // SCANNER
        // ============================================================
        function iniciarScanner() {
            if (iniciado) return;
            if (typeof Html5QrcodeScanner === 'undefined') {
                setError('Libreria QR no cargada.');
                return;
            }
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                setError('Tu navegador no soporta acceso a camara, o no estas en HTTPS.');
                return;
            }
            const reader = document.getElementById('qr-reader');
            if (!reader) {
                setError('Contenedor qr-reader no existe.');
                return;
            }

            destruirScanner();
            reader.innerHTML = '';
            iniciado = true;

            // Desbloquear audio con la primera interaccion
            getAudioCtx();

            scanner = new Html5QrcodeScanner(
                "qr-reader",
                {
                    fps: 10,
                    qrbox: { width: 250, height: 250 },
                    aspectRatio: 1.0,
                    rememberLastUsedCamera: true,
                    supportedScanTypes: [Html5QrcodeScanType.SCAN_TYPE_CAMERA]
                },
                false
            );

            const onScanSuccess = (texto) => {
                try {
                    const m = texto.match(/\\b(\\d{8})\\b/);
                    if (!m) return;
                    const dni = m[1];
                    const t = Date.now() / 1000;
                    if (dni === ultimoDni && (t - ultimoTs) < 3) return;
                    ultimoDni = dni;
                    ultimoTs = t;
                    setStatus('QR: ' + dni);
                    pitidoSimple();               // ← PITIDO
                    setTriggerValue("qr_dni", dni);
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
                        if (msg.includes('NotAllowedError')) {
                            setError('Permiso de camara denegado. Acepta el permiso o usa HTTPS.');
                        } else if (msg.includes('NotFoundError')) {
                            setError('No se encontro ninguna camara en este dispositivo.');
                        } else if (msg.includes('NotReadableError')) {
                            setError('La camara esta siendo usada por otra aplicacion.');
                        } else {
                            setError('Error camara: ' + msg);
                        }
                        iniciado = false;
                    });
            } else {
                setStatus('Camara activa.');
            }
        }

        // ============================================================
        // CARGA DE LIBRERIA
        // ============================================================
        window.addEventListener('beforeunload', destruirScanner);

        if (window.__qrV8Listo && typeof Html5QrcodeScanner !== 'undefined') {
            iniciarScanner();
            return;
        }
        if (window.__qrV8Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (typeof Html5QrcodeScanner !== 'undefined') {
                    clearInterval(t);
                    window.__qrV8Listo = true;
                    window.__qrV8Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrV8Cargando = false;
                    setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }
        window.__qrV8Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode';
        s.async = true;
        s.onload = () => {
            window.__qrV8Listo = true;
            window.__qrV8Cargando = false;
            setTimeout(() => {
                if (typeof Html5QrcodeScanner === 'undefined') {
                    setError('Libreria cargada pero sin Html5QrcodeScanner.');
                    return;
                }
                iniciarScanner();
            }, 50);
        };
        s.onerror = () => {
            window.__qrV8Cargando = false;
            setError('Error al cargar html5-qrcode del CDN.');
        };
        document.head.appendChild(s);
    }
    """,
)


def qr_scanner(key="qr_scanner", on_scan=None):
    """
    Monta el componente escaner QR con pitido moderno al detectar.
    Devuelve el resultado con atributo .qr_dni
    """
    if on_scan is None:
        on_scan = lambda: None
    return QR_SCANNER_COMPONENT(
        key=key,
        on_qr_dni_change=on_scan,
    )
