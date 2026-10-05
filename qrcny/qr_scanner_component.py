import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v53",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <video id="qr-video" playsinline autoplay muted></video>
        <div id="qr-status">Iniciando camara...</div>
        <div id="qr-error" style="display:none;"></div>
    </div>
    """,
    css="""
    #qr-wrapper {
        width: 100%;
        max-width: 400px;
        margin: 0 auto;
        position: relative;
    }
    #qr-video {
        border-radius: 10px;
        overflow: hidden;
        border: 2px solid #E65100;
        background: #000;
        width: 100%;
        height: 300px;
        object-fit: cover;
        display: block;
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
        padding: 10px;
        border: 1px solid #C62828;
        border-radius: 6px;
        background: #f8d7da;
    }
    """,
    js="""
    export default function(component) {
        const { setTriggerValue, data } = component;

        const DNI_REGEX = /\\b(\\d{8})\\b/;
        const SCAN_INTERVAL_MS = 500;
        const ANTI_REBOTE_MS = 3500;
        const ARRANQUE_ESPERA_MS = 2000;
        const SONIDO_CHECK_MS = 150;

        let stream = null;
        let detector = null;
        let videoElement = null;
        let scanLoop = null;
        let sonidoLoop = null;
        let ultimoDni = null;
        let ultimoTimestampDni = 0;
        let iniciado = false;
        let polyfillCargado = false;
        let intentosFallidos = 0;
        let arranqueTimestamp = 0;
        let ultimoSonidoNonce = 0;
        let ultimoDniEnviado = null;
        let contadorFrameSinQR = 0;
        const MAX_INTENTOS = 5;
        const MAX_FRAMES_SIN_QR = 3;

        // ══════════════════════════════════════════════════════
        // AUDIO - con rotación de AudioContext (evita saturación)
        // ══════════════════════════════════════════════════════
        let audioCtx = null;
        let sonidosReproducidos = 0;
        const MAX_SONIDOS_POR_CTX = 40;
        const MIN_MS_ENTRE_SONIDOS = 200;
        let ultimoMsSonido = 0;

        function crearAudioContext() {
            try {
                if (audioCtx && audioCtx.state !== 'closed') {
                    try { audioCtx.close(); } catch(e) {}
                }
                const Ctx = window.AudioContext || window.webkitAudioContext;
                if (!Ctx) return null;
                audioCtx = new Ctx();
                sonidosReproducidos = 0;
                console.log('[QR] AudioContext NUEVO. Estado:', audioCtx.state);
                return audioCtx;
            } catch(e) {
                console.warn('[QR] No se pudo crear AudioContext:', e);
                audioCtx = null;
                return null;
            }
        }

        function getAudioCtx() {
            if (!audioCtx || audioCtx.state === 'closed') {
                return crearAudioContext();
            }
            return audioCtx;
        }

        function asegurarAudioActivo() {
            try {
                let ctx = getAudioCtx();
                if (!ctx) return null;
                if (sonidosReproducidos >= MAX_SONIDOS_POR_CTX) {
                    console.log('[QR] Rotando AudioContext tras', sonidosReproducidos, 'sonidos');
                    ctx = crearAudioContext();
                }
                if (!ctx) return null;
                if (ctx.state === 'suspended') {
                    ctx.resume().catch(() => {});
                    setTimeout(() => {
                        if (audioCtx && audioCtx.state !== 'running') {
                            console.warn('[QR] Resume fallo, recreando');
                            crearAudioContext();
                        }
                    }, 50);
                }
                return ctx;
            } catch(e) {
                console.warn('[QR] asegurarAudioActivo error:', e);
                return crearAudioContext();
            }
        }

        function _tono(freq, dur, tipo, vol, delay) {
            const ctx = asegurarAudioActivo();
            if (!ctx) return;
            try {
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
                osc.onended = () => {
                    try { osc.disconnect(); g.disconnect(); } catch(e) {}
                };
                sonidosReproducidos++;
            } catch(e) { console.warn('[QR] _tono error:', e); }
        }

        function sonidoPuntual() {
            _tono(523, 0.10, 'sine', 0.40, 0);
            _tono(659, 0.10, 'sine', 0.40, 0.10);
            _tono(784, 0.15, 'sine', 0.40, 0.20);
        }
        function sonidoTardanza() {
            _tono(440, 0.15, 'sine', 0.40, 0);
            _tono(330, 0.25, 'sine', 0.40, 0.18);
        }
        function sonidoDuplicado() {
            _tono(220, 0.18, 'square', 0.45, 0);
            _tono(220, 0.18, 'square', 0.45, 0.22);
        }
        function sonidoError() {
            _tono(180, 0.15, 'sawtooth', 0.45, 0);
            _tono(250, 0.15, 'sawtooth', 0.45, 0.15);
            _tono(330, 0.15, 'sawtooth', 0.45, 0.30);
            _tono(440, 0.25, 'sawtooth', 0.45, 0.45);
        }
        function sonidoBloqueado() {
            _tono(160, 0.15, 'sawtooth', 0.45, 0);
            _tono(120, 0.15, 'sawtooth', 0.45, 0.18);
            _tono(90, 0.30, 'sawtooth', 0.45, 0.36);
        }

        function _reproducirAhora(kind) {
            try {
                switch (kind) {
                    case "puntual":     sonidoPuntual();     break;
                    case "tardanza":    sonidoTardanza();    break;
                    case "duplicado":   sonidoDuplicado();   break;
                    case "bloqueado":   sonidoBloqueado();   break;
                    case "error":
                    default:            sonidoError();       break;
                }
            } catch(e) { console.error('[QR] _reproducirAhora:', e); }
        }

        function reproducir(kind) {
            try {
                const ahora = Date.now();
                if (ahora - ultimoMsSonido < MIN_MS_ENTRE_SONIDOS) {
                    console.log('[QR] Sonido bloqueado por anti-saturación');
                    return;
                }
                ultimoMsSonido = ahora;
                asegurarAudioActivo();
                _reproducirAhora(kind);
                console.log('[QR] Sonando:', kind, '(ctx:', sonidosReproducidos + ')');
            } catch(e) { console.error('[QR] reproducir:', e); }
        }

        window.__qrFeedback = reproducir;
        try { window.parent.__qrFeedback = reproducir; } catch(e) {}

        window.addEventListener('message', (event) => {
            try {
                if (event.data && event.data.type === 'qr_sound' && event.data.kind) {
                    reproducir(event.data.kind);
                }
            } catch(e) {}
        });

        function desbloquearAudio() {
            try {
                if (!audioCtx || audioCtx.state === 'closed') crearAudioContext();
                if (audioCtx && audioCtx.state === 'suspended') {
                    audioCtx.resume().catch(() => {});
                }
            } catch(e) {}
        }

        ['click', 'touchstart', 'touchend', 'keydown', 'mousedown', 'pointerdown']
            .forEach(evt => document.addEventListener(evt, desbloquearAudio, { passive: true }));
        window.addEventListener('focus', desbloquearAudio);
        window.addEventListener('pageshow', desbloquearAudio);

        // ══════════════════════════════════════════════════════
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
            if (e) { e.style.display = 'none'; }
        }

        function detenerCamara() {
            if (scanLoop) { clearTimeout(scanLoop); scanLoop = null; }
            if (stream) {
                try {
                    stream.getTracks().forEach(t => { try { t.stop(); } catch(e) {} });
                } catch(e) {}
                stream = null;
            }
            if (videoElement) {
                try {
                    videoElement.pause();
                    videoElement.srcObject = null;
                    videoElement.removeAttribute('src');
                    videoElement.load();
                } catch(e) {}
            }
            iniciado = false;
            arranqueTimestamp = 0;
            ultimoDni = null;
            ultimoTimestampDni = 0;
            ultimoDniEnviado = null;
            contadorFrameSinQR = 0;
        }

        function cargarPolyfill() {
            return new Promise((resolve, reject) => {
                if (polyfillCargado) { resolve(); return; }
                if (typeof BarcodeDetector !== 'undefined') {
                    polyfillCargado = true; resolve(); return;
                }
                setStatus('Cargando motor de escaneo...');
                const script = document.createElement('script');
                script.src = 'https://cdn.jsdelivr.net/npm/@sec-ant/barcode-detector@1.3/dist/iife/side-effects.min.js';
                script.async = true;
                script.onload = () => { polyfillCargado = true; resolve(); };
                script.onerror = () => reject(new Error('No se pudo cargar polyfill.'));
                document.head.appendChild(script);
            });
        }

        async function iniciarScanner() {
            if (iniciado) return;
            limpiarError();
            detenerCamara();
            videoElement = document.getElementById('qr-video');
            if (!videoElement) { setError('Video no encontrado.'); return; }

            try {
                await cargarPolyfill();
                if (typeof BarcodeDetector === 'undefined') {
                    setError('Navegador no soporta escaneo.');
                    return;
                }
                detector = new BarcodeDetector({ formats: ['qr_code'] });

                setStatus('Solicitando camara...');
                stream = await navigator.mediaDevices.getUserMedia({
                    video: {
                        facingMode: 'environment',
                        width: { ideal: 1280 },
                        height: { ideal: 720 }
                    },
                    audio: false
                });

                videoElement.srcObject = stream;
                videoElement.setAttribute('playsinline', 'true');
                videoElement.setAttribute('muted', 'true');
                await videoElement.play();

                iniciado = true;
                intentosFallidos = 0;
                arranqueTimestamp = Date.now();
                setStatus('Camara activa. Apunta al QR.');
                bucleEscaneo();

            } catch (err) {
                console.error('[QR] Error:', err);
                detenerCamara();
                intentosFallidos++;
                const esNotReadable = err.name === 'NotReadableError' ||
                                     (err.message && err.message.toLowerCase().includes('could not start'));
                if (esNotReadable && intentosFallidos < MAX_INTENTOS) {
                    setStatus('Camara ocupada. Reintentando (' + intentosFallidos + '/' + MAX_INTENTOS + ')...');
                    setTimeout(() => { iniciarScanner(); }, 2000);
                    return;
                }
                if (esNotReadable) setError('No se pudo acceder a la camara.');
                else if (err.name === 'NotAllowedError') setError('Permiso de camara denegado.');
                else if (err.name === 'NotFoundError') setError('No hay camara.');
                else setError('Error: ' + (err.message || err.name));
            }
        }

        function bucleEscaneo() {
            if (!iniciado || !videoElement || !detector) return;
            if (videoElement.readyState >= 2) {
                detector.detect(videoElement)
                    .then(barcodes => {
                        if (!iniciado) return;
                        if (Date.now() - arranqueTimestamp < ARRANQUE_ESPERA_MS) return;

                        if (barcodes && barcodes.length > 0) {
                            contadorFrameSinQR = 0;
                            const codigo = barcodes[0];
                            const texto = codigo.rawValue;
                            const match = texto.match(DNI_REGEX);
                            if (match) {
                                const dni = match[1];
                                const ahora = Date.now();

                                if (dni === ultimoDni && (ahora - ultimoTimestampDni) < ANTI_REBOTE_MS) {
                                    return;
                                }

                                ultimoDni = dni;
                                ultimoTimestampDni = ahora;

                                if (dni !== ultimoDniEnviado) {
                                    ultimoDniEnviado = dni;
                                    desbloquearAudio();
                                    setStatus('QR: ' + dni);
                                    setTriggerValue("qr_dni", dni);
                                }
                            }
                        } else {
                            contadorFrameSinQR++;
                            if (contadorFrameSinQR >= MAX_FRAMES_SIN_QR) {
                                ultimoDniEnviado = null;
                            }
                        }
                    })
                    .catch(err => { console.debug('[QR] Error detect:', err); });
            }
            if (iniciado) scanLoop = setTimeout(bucleEscaneo, SCAN_INTERVAL_MS);
        }

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
                detenerCamara();
                setStatus('Camara en pausa.');
            } else if (document.visibilityState === 'visible') {
                if (!iniciado) {
                    setTimeout(() => { if (!iniciado) iniciarScanner(); }, 1000);
                }
            }
        });
        window.addEventListener('beforeunload', detenerCamara);

        function revisarSonidoPendiente() {
            try {
                if (!data) return;
                const nonce = data.sonido_nonce || 0;
                const kind = data.sonido_kind || '';
                if (nonce > 0 && nonce !== ultimoSonidoNonce && kind) {
                    ultimoSonidoNonce = nonce;
                    console.log('[QR] Sonido pendiente:', kind, 'nonce:', nonce);
                    const ctx = getAudioCtx();
                    if (ctx && ctx.state === 'suspended') {
                        ctx.resume().then(() => {
                            reproducir(kind);
                        }).catch(() => {
                            reproducir(kind);
                        });
                    } else {
                        reproducir(kind);
                    }
                }
            } catch(e) { console.error('[QR] revisarSonido:', e); }
        }

        setTimeout(() => {
            iniciarScanner();
            revisarSonidoPendiente();
            if (sonidoLoop) clearInterval(sonidoLoop);
            sonidoLoop = setInterval(revisarSonidoPendiente, SONIDO_CHECK_MS);
        }, 500);
    }
    """,
)


def qr_scanner(key="qr_scanner", on_scan=None, sonido_kind="", sonido_nonce=0):
    if on_scan is None:
        on_scan = lambda: None
    return QR_SCANNER_COMPONENT(
        key=key,
        on_qr_dni_change=on_scan,
        data={
            "sonido_kind": sonido_kind,
            "sonido_nonce": sonido_nonce,
        },
    )
