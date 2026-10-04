# qr_scanner_component.py
# Componente de escaneo QR para Streamlit.
# Mantiene el sistema de audio (window.__qrFeedback), video cuadrado y boton de pausa.
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v31",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <div id="qr-reader"></div>
        <div id="qr-controls">
            <button id="btn-pausa" type="button">Pausar escaner</button>
        </div>
        <div id="qr-status">Iniciando camara...</div>
        <div id="qr-error" style="display:none;"></div>
    </div>
    """,
    css="""
    #qr-wrapper {
        width: 100%;
        max-width: 320px;
        margin: 0 auto;
        position: relative;
    }
    #qr-reader {
        border-radius: 8px;
        overflow: hidden;
        border: 2px solid #E65100;
        background: #000;
        aspect-ratio: 1 / 1;
    }
    #qr-reader video {
        border-radius: 6px;
        width: 100% !important;
        height: 100% !important;
        object-fit: cover !important;
    }
    #qr-wrapper.pausado #qr-reader {
        border-color: #2E7D32;
        opacity: 0.8;
    }
    #qr-controls {
        display: flex;
        gap: 8px;
        margin-top: 10px;
        justify-content: center;
        flex-wrap: wrap;
    }
    #qr-controls button {
        background: #E65100;
        color: #FFFFFF;
        border: none;
        border-radius: 6px;
        padding: 10px 18px;
        font-size: 13px;
        font-weight: 600;
        cursor: pointer;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        transition: background 120ms ease;
    }
    #qr-controls button:hover { background: #BF360C; }
    #qr-controls button.pausado { background: #2E7D32; }
    #qr-controls button.pausado:hover { background: #1B5E20; }
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
    #qr-reader button:hover { background: #BF360C !important; }
    #qr-reader select {
        border-radius: 6px !important;
        padding: 6px 10px !important;
        margin: 4px !important;
        border: 1px solid #ccc !important;
    }
    #qr-reader a { color: #E65100 !important; font-weight: 600 !important; }
    """,
    js="""
    export default function(component) {
        const { setTriggerValue } = component;
        let scanner = null;
        let iniciado = false;
        let pausado = false;

        const COOLDOWN_MS = 2500;
        let ultimoDniEmitido = null;
        let ultimoTimestampEmision = 0;

        // ============================================================
        // MOTOR DE AUDIO
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

        // PUNTUAL: DO -> MI -> SOL
        function sonidoPuntual() {
            _tono(523, 0.10, 'sine', 0.40, 0);
            _tono(659, 0.10, 'sine', 0.40, 0.10);
            _tono(784, 0.15, 'sine', 0.40, 0.20);
        }
        // TARDANZA: dos notas descendentes graves
        function sonidoTardanza() {
            _tono(392, 0.15, 'sine', 0.40, 0);
            _tono(294, 0.25, 'sine', 0.40, 0.18);
        }
        // DUPLICADO: buzz grave doble
        function sonidoDuplicado() {
            _tono(220, 0.18, 'square', 0.45, 0);
            _tono(220, 0.18, 'square', 0.45, 0.22);
        }
        // ERROR: disonancia ascendente
        function sonidoError() {
            _tono(180, 0.15, 'sawtooth', 0.45, 0);
            _tono(250, 0.15, 'sawtooth', 0.45, 0.15);
            _tono(330, 0.15, 'sawtooth', 0.45, 0.30);
            _tono(440, 0.25, 'sawtooth', 0.45, 0.45);
        }
        // BLOQUEADO: triple buzz grave
        function sonidoBloqueado() {
            _tono(160, 0.15, 'sawtooth', 0.45, 0);
            _tono(120, 0.15, 'sawtooth', 0.45, 0.18);
            _tono(90, 0.30, 'sawtooth', 0.45, 0.36);
        }
        // INCIDENCIA: dos notas neutras
        function sonidoIncidencia() {
            _tono(523, 0.10, 'triangle', 0.35, 0);
            _tono(523, 0.10, 'triangle', 0.35, 0.15);
        }

        function reproducir(kind) {
            try {
                getAudioCtx();
                switch (kind) {
                    case "puntual":     sonidoPuntual();     break;
                    case "tardanza":    sonidoTardanza();    break;
                    case "duplicado":   sonidoDuplicado();   break;
                    case "bloqueado":   sonidoBloqueado();   break;
                    case "incidencia":  sonidoIncidencia();  break;
                    case "error":
                    default:            sonidoError();       break;
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

        // Desbloquear audio con el primer toque
        function desbloquearAudio() { getAudioCtx(); }
        document.addEventListener('touchstart', desbloquearAudio, { once: true });
        document.addEventListener('click', desbloquearAudio, { once: true });

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
            pausado = false;
            if (scanner) {
                try { scanner.clear(); } catch (e) {}
                scanner = null;
            }
            iniciado = false;
            ultimoDniEmitido = null;
            ultimoTimestampEmision = 0;
        }

        // ============================================================
        // BOTON PAUSA
        // ============================================================
        function actualizarBotonPausa() {
            const btn = document.getElementById('btn-pausa');
            const wrap = document.getElementById('qr-wrapper');
            if (!btn) return;
            if (pausado) {
                btn.textContent = 'Reanudar escaner';
                btn.classList.add('pausado');
                if (wrap) wrap.classList.add('pausado');
            } else {
                btn.textContent = 'Pausar escaner';
                btn.classList.remove('pausado');
                if (wrap) wrap.classList.remove('pausado');
            }
        }
        function togglePausa() {
            if (!iniciado) return;
            if (pausado) {
                try { scanner.resume(); } catch(e) {}
                pausado = false;
                setStatus('Camara activa.');
                actualizarBotonPausa();
            } else {
                try { scanner.pause(true); } catch(e) {}
                pausado = true;
                setStatus('Escaner PAUSADO. Presiona Reanudar.');
                actualizarBotonPausa();
                try { _tono(330, 0.12, 'sine', 0.30, 0); } catch(e) {}
            }
        }
        function conectarBotonPausa() {
            const btn = document.getElementById('btn-pausa');
            if (btn && !btn.__conectado) {
                btn.addEventListener('click', togglePausa);
                btn.__conectado = true;
            }
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
                setError('Tu navegador no soporta camara, o no estas en HTTPS.');
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
            conectarBotonPausa();

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
                        if (pausado) return;
                        const m = texto.match(/\\b(\\d{8})\\b/);
                        if (!m) return;
                        const dni = m[1];
                        const ahora = Date.now();
                        const esMismoDni = (dni === ultimoDniEmitido);
                        const dentroCooldown = (ahora - ultimoTimestampEmision) < COOLDOWN_MS;
                        if (esMismoDni && dentroCooldown) return;
                        ultimoDniEmitido = dni;
                        ultimoTimestampEmision = ahora;
                        setStatus('QR: ' + dni);
                        setTriggerValue("qr_dni", dni);
                    } catch (e) {
                        console.error('[QR] error en onScanSuccess:', e);
                    }
                };

                let resultado;
                try {
                    resultado = scanner.render(onScanSuccess, () => {});
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
        // VISIBILITY
        // ============================================================
        function pausarScanner() {
            if (!scanner || !iniciado || pausado) return;
            try {
                scanner.pause(true);
                pausado = true;
                actualizarBotonPausa();
                setStatus('Camara en pausa.');
            } catch (e) {
                destruirScanner();
            }
        }
        function reanudarScanner() {
            if (!scanner || !iniciado) { iniciarScanner(); return; }
            if (!pausado) return;
            try {
                scanner.resume();
                pausado = false;
                actualizarBotonPausa();
                setStatus('Camara activa.');
            } catch (e) {
                destruirScanner();
                iniciarScanner();
            }
        }

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
                pausarScanner();
            } else if (document.visibilityState === 'visible') {
                reanudarScanner();
            }
        });
        window.addEventListener('blur', () => {
            if (document.visibilityState === 'hidden') pausarScanner();
        });
        window.addEventListener('focus', () => {
            if (document.visibilityState === 'visible') reanudarScanner();
        });

        // ============================================================
        // CARGA DE LIBRERIA
        // ============================================================
        window.addEventListener('beforeunload', destruirScanner);

        if (window.__qrV31Listo && typeof Html5QrcodeScanner !== 'undefined') {
            iniciarScanner();
            return;
        }
        if (window.__qrV31Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (typeof Html5QrcodeScanner !== 'undefined') {
                    clearInterval(t);
                    window.__qrV31Listo = true;
                    window.__qrV31Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrV31Cargando = false;
                    setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }
        window.__qrV31Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode';
        s.async = true;
        s.onload = () => {
            window.__qrV31Listo = true;
            window.__qrV31Cargando = false;
            setTimeout(() => {
                if (typeof Html5QrcodeScanner === 'undefined') {
                    setError('Libreria cargada pero sin Html5QrcodeScanner.');
                    return;
                }
                iniciarScanner();
            }, 50);
        };
        s.onerror = () => {
            window.__qrV31Cargando = false;
            setError('Error al cargar html5-qrcode del CDN.');
        };
        document.head.appendChild(s);
    }
    """,
)


def qr_scanner(key="qr_scanner", on_scan=None):
    if on_scan is None:
        on_scan = lambda: None
    return QR_SCANNER_COMPONENT(
        key=key,
        on_qr_dni_change=on_scan,
    )
