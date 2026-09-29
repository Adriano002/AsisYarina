# qr_scanner_component.py
# Componente de escaneo QR para Streamlit con 5 sonidos diferenciados.
# Python dispara el sonido correspondiente via window.__qrFeedback(kind):
#   "puntual"   -> campanita alegre
#   "tardanza"  -> tono neutro
#   "duplicado" -> buzz feo
#   "error"     -> disonancia horrible
#   "bloqueado" -> triple buzz grave tenebroso
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v9",
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
        let scanner = null;
        let iniciado = false;
        let ultimoPitidoTs = 0;

        // ============================================================
        // MOTOR DE AUDIO (Web Audio API)
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

        // Helper: un tono
        function _tono(freq, dur, tipo, vol, delay) {
            const ctx = getAudioCtx();
            if (!ctx) return;
            const t0 = ctx.currentTime + (delay || 0);
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.connect(g); g.connect(ctx.destination);
            osc.type = tipo || 'sine';
            osc.frequency.setValueAtTime(freq, t0);
            g.gain.setValueAtTime(0, t0);
            g.gain.linearRampToValueAtTime(vol || 0.35, t0 + 0.015);
            g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
            osc.start(t0);
            osc.stop(t0 + dur + 0.02);
        }

        // ============================================================
        // 5 SONIDOS
        // ============================================================

        // 1) PUNTUAL: campanita alegre (3 notas ascendentes + brillo)
        function sonidoPuntual() {
            _tono(1047, 0.15, 'sine', 0.55, 0.00);   // DO6
            _tono(1319, 0.15, 'sine', 0.50, 0.08);   // MI6
            _tono(1568, 0.25, 'sine', 0.45, 0.16);   // SOL6
            _tono(2093, 0.30, 'sine', 0.30, 0.20);   // DO7 (brillo)
        }

        // 2) TARDANZA: nota neutra, plana
        function sonidoTardanza() {
            _tono(440, 0.35, 'sine', 0.40, 0);       // LA4
        }

        // 3) DUPLICADO: buzz doble grave feo
        function sonidoDuplicado() {
            _tono(220, 0.18, 'square', 0.40, 0.00);
            _tono(220, 0.18, 'square', 0.40, 0.22);
        }

        // 4) ERROR: disonancia horrible ascendente
        function sonidoError() {
            _tono(180, 0.15, 'sawtooth', 0.45, 0.00);
            _tono(250, 0.15, 'sawtooth', 0.45, 0.15);
            _tono(330, 0.15, 'sawtooth', 0.45, 0.30);
            _tono(440, 0.25, 'sawtooth', 0.45, 0.45);
        }

        // 5) BLOQUEADO: triple buzz grave tenebroso
        function sonidoBloqueado() {
            _tono(160, 0.15, 'sawtooth', 0.45, 0.00);
            _tono(120, 0.15, 'sawtooth', 0.45, 0.18);
            _tono(90,  0.35, 'sawtooth', 0.45, 0.36);
        }

        // ============================================================
        // API PARA PYTHON
        // ============================================================
        function reproducir(kind) {
            try {
                getAudioCtx();
                switch (kind) {
                    case "puntual":    sonidoPuntual();    break;
                    case "tardanza":   sonidoTardanza();   break;
                    case "duplicado":  sonidoDuplicado();  break;
                    case "bloqueado":  sonidoBloqueado();  break;
                    case "error":
                    default:           sonidoError();      break;
                }
            } catch (e) {
                console.error('[QR] reproducir error:', e);
            }
        }

        // Registrar en window propio Y en window.parent
        window.__qrFeedback = reproducir;
        try {
            window.parent.__qrFeedback = reproducir;
        } catch (e) {
            console.warn('[QR] no se pudo registrar en parent:', e);
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
                    setStatus('QR: ' + dni);
                    // Enviamos el DNI a Python. Python decide el sonido.
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

        if (window.__qrV9Listo && typeof Html5QrcodeScanner !== 'undefined') {
            iniciarScanner();
            return;
        }
        if (window.__qrV9Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (typeof Html5QrcodeScanner !== 'undefined') {
                    clearInterval(t);
                    window.__qrV9Listo = true;
                    window.__qrV9Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrV9Cargando = false;
                    setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }
        window.__qrV9Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode';
        s.async = true;
        s.onload = () => {
            window.__qrV9Listo = true;
            window.__qrV9Cargando = false;
            setTimeout(() => {
                if (typeof Html5QrcodeScanner === 'undefined') {
                    setError('Libreria cargada pero sin Html5QrcodeScanner.');
                    return;
                }
                iniciarScanner();
            }, 50);
        };
        s.onerror = () => {
            window.__qrV9Cargando = false;
            setError('Error al cargar html5-qrcode del CDN.');
        };
        document.head.appendChild(s);
    }
    """,
)


def qr_scanner(key="qr_scanner", on_scan=None):
    """
    Monta el componente escaner QR con 5 sonidos diferenciados.
    El sonido lo dispara Python via window.__qrFeedback(kind).
    """
    if on_scan is None:
        on_scan = lambda: None
    return QR_SCANNER_COMPONENT(
        key=key,
        on_qr_dni_change=on_scan,
    )
