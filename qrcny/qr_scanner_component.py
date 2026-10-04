# qr_scanner_component.py
# Componente QR con BarcodeDetector API + Polyfill ZXing.
# Video cuadrado. Boton de pausa. Sistema de audio.
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v32",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <video id="qr-video" playsinline autoplay muted></video>
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
    #qr-video {
        border-radius: 8px;
        overflow: hidden;
        border: 2px solid #E65100;
        background: #000;
        width: 100%;
        height: 320px;
        object-fit: cover;
        display: block;
    }
    #qr-wrapper.pausado #qr-video {
        border-color: #2E7D32;
        opacity: 0.8;
    }
    #qr-controls {
        display: flex; gap: 8px; margin-top: 10px;
        justify-content: center; flex-wrap: wrap;
    }
    #qr-controls button {
        background: #E65100; color: #FFFFFF; border: none;
        border-radius: 6px; padding: 10px 18px;
        font-size: 13px; font-weight: 600; cursor: pointer;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        transition: background 120ms ease;
    }
    #qr-controls button:hover { background: #BF360C; }
    #qr-controls button.pausado { background: #2E7D32; }
    #qr-controls button.pausado:hover { background: #1B5E20; }
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
    """,
    js="""
    export default function(component) {
        const { setTriggerValue } = component;

        const DNI_REGEX = /\\b(\\d{8})\\b/;
        const SCAN_INTERVAL_MS = 250;
        const ANTI_REBOTE_MS = 1200;

        let stream = null;
        let detector = null;
        let videoElement = null;
        let scanLoop = null;
        let ultimoDni = null;
        let ultimoTimestampDni = 0;
        let iniciado = false;
        let pausado = false;
        let polyfillCargado = false;
        let intentosFallidos = 0;
        const MAX_INTENTOS = 5;

        // ============================================================
        // AUDIO
        // ============================================================
        let audioCtx = null;
        function getAudioCtx() {
            if (!audioCtx) {
                try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
                catch (e) { console.warn('[QR] no AudioContext:', e); }
            }
            if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
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
            osc.start(t0); osc.stop(t0 + dur + 0.02);
        }
        function sonidoPuntual() { _tono(523,0.10,'sine',0.40,0); _tono(659,0.10,'sine',0.40,0.10); _tono(784,0.15,'sine',0.40,0.20); }
        function sonidoTardanza() { _tono(392,0.15,'sine',0.40,0); _tono(294,0.25,'sine',0.40,0.18); }
        function sonidoDuplicado() { _tono(220,0.18,'square',0.45,0); _tono(220,0.18,'square',0.45,0.22); }
        function sonidoError() {
            _tono(180,0.15,'sawtooth',0.45,0); _tono(250,0.15,'sawtooth',0.45,0.15);
            _tono(330,0.15,'sawtooth',0.45,0.30); _tono(440,0.25,'sawtooth',0.45,0.45);
        }
        function sonidoBloqueado() {
            _tono(160,0.15,'sawtooth',0.45,0); _tono(120,0.15,'sawtooth',0.45,0.18);
            _tono(90,0.30,'sawtooth',0.45,0.36);
        }
        function sonidoIncidencia() { _tono(523,0.10,'triangle',0.35,0); _tono(523,0.10,'triangle',0.35,0.15); }

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
            } catch (e) { console.error('[QR] reproducir:', e); }
        }
        window.__qrFeedback = reproducir;
        try { window.parent.__qrFeedback = reproducir; } catch (e) {}

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
        function limpiarError() {
            const e = document.getElementById('qr-error');
            if (e) { e.textContent = ''; e.style.display = 'none'; }
        }
        function detenerCamara() {
            if (scanLoop) { clearTimeout(scanLoop); scanLoop = null; }
            if (stream) {
                stream.getTracks().forEach(track => { try { track.stop(); } catch(e) {} });
                stream = null;
            }
            if (videoElement) {
                try {
                    videoElement.pause();
                    videoElement.srcObject = null;
                    videoElement.src = '';
                    videoElement.load();
                } catch(e) {}
            }
            iniciado = false;
            ultimoDni = null;
            ultimoTimestampDni = 0;
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
                pausado = false;
                setStatus('Camara activa. Apunta al codigo QR.');
                actualizarBotonPausa();
                bucleEscaneo();
            } else {
                pausado = true;
                if (scanLoop) { clearTimeout(scanLoop); scanLoop = null; }
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
        // POLYFILL ZXING
        // ============================================================
        function cargarPolyfill() {
            return new Promise((resolve, reject) => {
                if (polyfillCargado) { resolve(); return; }
                if (typeof BarcodeDetector !== 'undefined') {
                    polyfillCargado = true;
                    resolve();
                    return;
                }
                setStatus('Cargando motor de escaneo...');
                const script = document.createElement('script');
                script.src = 'https://cdn.jsdelivr.net/npm/@sec-ant/barcode-detector@1.3/dist/iife/side-effects.min.js';
                script.async = true;
                script.onload = () => { polyfillCargado = true; resolve(); };
                script.onerror = () => { reject(new Error('No se pudo cargar polyfill.')); };
                document.head.appendChild(script);
            });
        }

        // ============================================================
        // INICIAR
        // ============================================================
        async function iniciarScanner() {
            if (iniciado) return;
            limpiarError();
            detenerCamara();
            videoElement = document.getElementById('qr-video');
            if (!videoElement) { setError('Contenedor de video no encontrado.'); return; }
            conectarBotonPausa();

            try {
                await cargarPolyfill();
                if (typeof BarcodeDetector === 'undefined') {
                    setError('Este navegador no soporta escaneo de codigos.');
                    return;
                }
                detector = new BarcodeDetector({ formats: ['qr_code'] });

                setStatus('Solicitando acceso a la camara...');
                stream = await navigator.mediaDevices.getUserMedia({
                    video: { facingMode: 'environment', width: { ideal: 1280 }, height: { ideal: 720 } },
                    audio: false
                });

                videoElement.srcObject = stream;
                videoElement.setAttribute('playsinline', 'true');
                videoElement.setAttribute('muted', 'true');
                await videoElement.play();

                iniciado = true;
                pausado = false;
                intentosFallidos = 0;
                setStatus('Camara activa. Apunta al codigo QR.');
                actualizarBotonPausa();
                bucleEscaneo();

            } catch (err) {
                console.error('[QR] Error al iniciar:', err);
                detenerCamara();
                intentosFallidos++;
                const esNotReadable = err.name === 'NotReadableError' ||
                                     (err.message && err.message.toLowerCase().includes('could not start'));
                if (esNotReadable && intentosFallidos < MAX_INTENTOS) {
                    setStatus('Camara ocupada. Reintentando en 2s... (' + intentosFallidos + '/' + MAX_INTENTOS + ')');
                    setTimeout(() => { iniciarScanner(); }, 2000);
                    return;
                }
                if (esNotReadable) {
                    setError('No se pudo acceder a la camara.');
                } else if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
                    setError('Permiso de camara denegado.');
                } else if (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError') {
                    setError('No se encontro ninguna camara.');
                } else {
                    setError('Error: ' + (err.message || err.name));
                }
            }
        }

        // ============================================================
        // BUCLE
        // ============================================================
        function bucleEscaneo() {
            if (!iniciado || pausado || !videoElement || !detector) return;
            if (videoElement.readyState >= 2) {
                detector.detect(videoElement)
                    .then(barcodes => {
                        if (pausado) return;
                        if (barcodes && barcodes.length > 0) {
                            const codigo = barcodes[0];
                            const texto = codigo.rawValue;
                            const match = texto.match(DNI_REGEX);
                            if (match) {
                                const dni = match[1];
                                const ahora = Date.now();
                                if (dni === ultimoDni && (ahora - ultimoTimestampDni) < ANTI_REBOTE_MS) {
                                    // ignorar
                                } else {
                                    ultimoDni = dni;
                                    ultimoTimestampDni = ahora;
                                    setStatus('QR: ' + dni);
                                    setTriggerValue("qr_dni", dni);
                                }
                            }
                        }
                    })
                    .catch(err => { console.debug('[QR] Error deteccion:', err); });
            }
            if (!pausado) scanLoop = setTimeout(bucleEscaneo, SCAN_INTERVAL_MS);
        }

        // ============================================================
        // VISIBILITY
        // ============================================================
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
                detenerCamara();
                pausado = false;
                actualizarBotonPausa();
                setStatus('Camara en pausa.');
            } else if (document.visibilityState === 'visible') {
                if (!iniciado) {
                    setTimeout(() => { if (!iniciado) iniciarScanner(); }, 1000);
                }
            }
        });
        window.addEventListener('beforeunload', detenerCamara);

        // ============================================================
        // ARRANQUE
        // ============================================================
        setTimeout(() => {
            conectarBotonPausa();
            iniciarScanner();
        }, 500);
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
