# qr_scanner_component.py
# Componente de escaneo QR para Streamlit.
# Expone window.__qrFeedback(kind, texto) para que app.py le indique
# qué sonido/voz reproducir: 'nuevo' | 'tardanza' | 'duplicado' | 'bloqueado' | 'error'
# Todos los avisos usan voz robótica (Web Speech API) + pitido sintetizado.
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v12",
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
        let ultimaVozTs = 0;

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

        // ============================================================
        // VOZ ROBOTICA (Web Speech API)
        // ============================================================
        let vozElegida = null;
        function elegirVoz() {
            if (!window.speechSynthesis) return null;
            const voces = window.speechSynthesis.getVoices();
            if (!voces || voces.length === 0) return null;
            const orden = ["es-PE", "es-MX", "es-US", "es-419", "es-ES"];
            for (const lang of orden) {
                const v = voces.find(x => x.lang === lang);
                if (v) return v;
            }
            return voces.find(x => x.lang && x.lang.startsWith("es")) || voces[0];
        }
        if (window.speechSynthesis) {
            window.speechSynthesis.onvoiceschanged = () => { vozElegida = elegirVoz(); };
            vozElegida = elegirVoz();
        }

        function hablar(texto, opciones) {
            if (!window.speechSynthesis) return;
            const ahora = Date.now();
            if (ahora - ultimaVozTs < 400) return;
            ultimaVozTs = ahora;

            try {
                window.speechSynthesis.cancel();
                const u = new SpeechSynthesisUtterance(texto);
                if (!vozElegida) vozElegida = elegirVoz();
                if (vozElegida) u.voice = vozElegida;
                u.lang = (vozElegida && vozElegida.lang) || "es-PE";
                u.rate = (opciones && opciones.rate) || 1.0;
                u.pitch = (opciones && opciones.pitch) || 0.5;
                u.volume = (opciones && opciones.volume) || 1.0;
                window.speechSynthesis.speak(u);
            } catch (e) {
                console.warn('[QR] error voz:', e);
            }
        }

        // ============================================================
        // PITIDOS SINTETIZADOS
        // ============================================================
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
            g.gain.linearRampToValueAtTime(vol || 0.3, t0 + 0.015);
            g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
            osc.start(t0);
            osc.stop(t0 + dur + 0.02);
        }

        function pitidoNuevo() {           // Do -> Mi, agradable
            _tono(523, 0.12, 'sine', 0.35, 0);
            _tono(659, 0.15, 'sine', 0.35, 0.09);
        }
        function pitidoTardanza() {        // Una nota neutra
            _tono(440, 0.25, 'sine', 0.30, 0);
        }
        function pitidoDuplicado() {       // Buzz grave descendente
            _tono(233, 0.18, 'square', 0.26, 0);
            _tono(185, 0.22, 'square', 0.26, 0.20);
        }
        function pitidoBloqueado() {       // Triple buzz grave, mas agresivo
            _tono(180, 0.15, 'sawtooth', 0.30, 0);
            _tono(140, 0.15, 'sawtooth', 0.30, 0.18);
            _tono(100, 0.30, 'sawtooth', 0.30, 0.36);
        }
        function pitidoError() {           // Beep corto seco
            _tono(330, 0.10, 'square', 0.28, 0);
        }

        // ============================================================
        // API PUBLICA: la llama app.py via window.__qrFeedback(kind, texto)
        // ============================================================
        window.__qrFeedback = function(kind, texto) {
            try {
                getAudioCtx();

                switch (kind) {
                    case "nuevo":
                        setStatus("QR: " + (texto || ""));
                        pitidoNuevo();
                        hablar("Puntual", { pitch: 0.55, rate: 1.05 });
                        break;
                    case "tardanza":
                        setStatus("QR: " + (texto || "") + " (tardanza)");
                        pitidoTardanza();
                        hablar("Tardanza", { pitch: 0.55, rate: 0.95 });
                        break;
                    case "duplicado":
                        setStatus("QR duplicado: " + (texto || ""));
                        pitidoDuplicado();
                        hablar("Duplicado", { pitch: 0.40, rate: 0.95 });
                        break;
                    case "bloqueado":
                        setStatus("ALUMNO BLOQUEADO: " + (texto || ""));
                        pitidoBloqueado();
                        hablar("Alumno bloqueado", { pitch: 0.35, rate: 0.90, volume: 1.0 });
                        break;
                    case "error":
                    default:
                        setStatus("Error: " + (texto || ""));
                        pitidoError();
                        hablar("Error", { pitch: 0.50, rate: 1.00 });
                        break;
                }
            } catch (e) {
                console.error('[QR] __qrFeedback error:', e);
            }
        };

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
            if (window.speechSynthesis) {
                try {
                    const u = new SpeechSynthesisUtterance("");
                    window.speechSynthesis.speak(u);
                } catch (e) {}
            }

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
                        supportedScanTypes: [Html5QrcodeScanType.SCAN_TYPE_CAMERA]
                    },
                    false
                );

                const onScanSuccess = (texto) => {
                    try {
                        const m = texto.match(/\\b(\\d{8})\\b/);
                        if (!m) return;
                        const dni = m[1];
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
                            } else if (msg.includes('AbortError')) {
                                setError('Firefox aborto el inicio de la camara. Recarga la pagina e intenta de nuevo.');
                            } else {
                                setError('Error camara: ' + msg);
                            }
                            iniciado = false;
                        });
                } else {
                    setStatus('Camara activa.');
                }
            }, 500);
        }

        // ============================================================
        // CARGA DE LIBRERIA
        // ============================================================
        window.addEventListener('beforeunload', destruirScanner);

        if (window.__qrV12Listo && typeof Html5QrcodeScanner !== 'undefined') {
            iniciarScanner();
            return;
        }
        if (window.__qrV12Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (typeof Html5QrcodeScanner !== 'undefined') {
                    clearInterval(t);
                    window.__qrV12Listo = true;
                    window.__qrV12Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrV12Cargando = false;
                    setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }
        window.__qrV12Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode';
        s.async = true;
        s.onload = () => {
            window.__qrV12Listo = true;
            window.__qrV12Cargando = false;
            setTimeout(() => {
                if (typeof Html5QrcodeScanner === 'undefined') {
                    setError('Libreria cargada pero sin Html5QrcodeScanner.');
                    return;
                }
                iniciarScanner();
            }, 50);
        };
        s.onerror = () => {
            window.__qrV12Cargando = false;
            setError('Error al cargar html5-qrcode del CDN.');
        };
        document.head.appendChild(s);
    }
    """,
)


def qr_scanner(key="qr_scanner", on_scan=None):
    """
    Monta el componente escaner QR.
    El sonido/voz lo dispara el app.py via window.__qrFeedback(kind, texto)
    con kind: 'nuevo' | 'tardanza' | 'duplicado' | 'bloqueado' | 'error'
    """
    if on_scan is None:
        on_scan = lambda: None
    return QR_SCANNER_COMPONENT(
        key=key,
        on_qr_dni_change=on_scan,
    )
