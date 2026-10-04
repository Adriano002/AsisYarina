# qr_scanner_component.py
# Componente de escaneo QR para Streamlit.
# Version robusta que evita NotReadableError en Chrome Android.
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v21",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <div id="qr-reader"></div>
        <div id="qr-status">Iniciando camara...</div>
        <div id="qr-error" style="display:none;"></div>
    </div>
    """,
    css="""
    #qr-wrapper { width: 100%; max-width: 500px; margin: 0 auto; }
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
        font-weight: 600; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        padding: 10px; border: 1px solid #C62828; border-radius: 6px; background: #f8d7da;
    }
    #qr-reader button {
        background: #E65100 !important; color: white !important; border: none !important;
        border-radius: 6px !important; padding: 8px 16px !important;
        font-weight: 600 !important; cursor: pointer !important; margin: 4px !important;
    }
    #qr-reader button:hover { background: #BF360C !important; }
    #qr-reader select {
        border-radius: 6px !important; padding: 6px 10px !important;
        margin: 4px !important; border: 1px solid #ccc !important;
    }
    #qr-reader a { color: #E65100 !important; font-weight: 600 !important; }
    """,
    js="""
    export default function(component) {
        const { setTriggerValue } = component;
        let scanner = null;
        let iniciado = false;
        let pausado = false;
        let heartbeatTimer = null;
        let reiniciando = false;
        let intentosFallidos = 0;
        const MAX_INTENTOS = 5;

        const FRAME_COOLDOWN_MS = 400;
        const DNI_COOLDOWN_MS = 3000;

        let ultimoTextoHash = null;
        let ultimoTimestampFrame = 0;
        let ultimoDniEmitido = null;
        let ultimoTimestampDni = 0;

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
        function sonidoTardanza() { _tono(440,0.30,'sine',0.35,0); }
        function sonidoDuplicado() { _tono(220,0.18,'square',0.45,0); _tono(220,0.18,'square',0.45,0.22); }
        function sonidoError() {
            _tono(180,0.15,'sawtooth',0.45,0); _tono(250,0.15,'sawtooth',0.45,0.15);
            _tono(330,0.15,'sawtooth',0.45,0.30); _tono(440,0.25,'sawtooth',0.45,0.45);
        }
        function sonidoBloqueado() {
            _tono(160,0.15,'sawtooth',0.45,0); _tono(120,0.15,'sawtooth',0.45,0.18);
            _tono(90,0.30,'sawtooth',0.45,0.36);
        }
        function reproducir(kind) {
            try {
                getAudioCtx();
                switch (kind) {
                    case "puntual":   sonidoPuntual();   break;
                    case "tardanza":  sonidoTardanza();  break;
                    case "duplicado": sonidoDuplicado(); break;
                    case "bloqueado": sonidoBloqueado(); break;
                    case "error":
                    default:          sonidoError();     break;
                }
            } catch (e) { console.error('[QR] reproducir:', e); }
        }
        window.__qrFeedback = reproducir;
        try { window.parent.__qrFeedback = reproducir; } catch (e) {}

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
        function detenerHeartbeat() {
            if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
        }

        // ============================================================
        // LIBERACION FORZADA DE CAMARA
        // Esto es la clave para evitar NotReadableError en Android.
        // Detiene TODOS los tracks de video activos en el documento.
        // ============================================================
        function forzarLiberacionCamara() {
            try {
                const videos = document.querySelectorAll('video');
                videos.forEach(v => {
                    try {
                        if (v.srcObject) {
                            v.srcObject.getTracks().forEach(track => {
                                try { track.stop(); } catch (e) {}
                            });
                            v.srcObject = null;
                        }
                        v.pause();
                        v.src = '';
                        v.load();
                    } catch (e) {}
                });
                // Tambien intenta liberar tracks a nivel global de mediaDevices
                if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
                    navigator.mediaDevices.enumerateDevices()
                        .then(devices => {
                            devices.forEach(d => {
                                if (d.kind === 'videoinput') {
                                    // No podemos cerrar streams ajenos, pero forzamos GC
                                }
                            });
                        })
                        .catch(() => {});
                }
            } catch (e) {
                console.warn('[QR] error en forzarLiberacionCamara:', e);
            }
        }

        // ============================================================
        // DESTRUIR
        // ============================================================
        function destruirScanner() {
            pausado = false;
            detenerHeartbeat();
            if (scanner) {
                try { scanner.clear(); } catch (e) {}
                scanner = null;
            }
            iniciado = false;
            const reader = document.getElementById('qr-reader');
            if (reader) reader.innerHTML = '';
            // Fuerza la liberacion de tracks
            forzarLiberacionCamara();
            // Reset cooldowns
            ultimoTextoHash = null;
            ultimoTimestampFrame = 0;
            ultimoDniEmitido = null;
            ultimoTimestampDni = 0;
        }

        // ============================================================
        // HEARTBEAT
        // Cada 3s verifica que el video siga vivo. Si no, reinicia.
        // ============================================================
        function iniciarHeartbeat() {
            detenerHeartbeat();
            heartbeatTimer = setInterval(() => {
                if (!iniciado || reiniciando) return;
                const v = document.querySelector('#qr-reader video');
                if (!v) {
                    console.warn('[QR] heartbeat: no hay video, reiniciando');
                    reiniciarScanner();
                    return;
                }
                if (v.readyState < 2) {
                    console.warn('[QR] heartbeat: video muerto (readyState=' + v.readyState + '), reiniciando');
                    reiniciarScanner();
                }
            }, 3000);
        }

        // ============================================================
        // REINICIAR (con guard para evitar loops)
        // ============================================================
        function reiniciarScanner() {
            if (reiniciando) return;
            reiniciando = true;
            intentosFallidos++;
            if (intentosFallidos > MAX_INTENTOS) {
                setError('No se pudo acceder a la camara. Cierra otras apps que usen la camara y recarga la pagina.');
                reiniciando = false;
                return;
            }
            console.warn('[QR] reiniciando scanner, intento ' + intentosFallidos);
            destruirScanner();
            // Espera mas larga si hemos fallado varias veces
            const espera = 1500 + (intentosFallidos * 500);
            setTimeout(() => {
                reiniciando = false;
                iniciarScanner();
            }, espera);
        }

        // ============================================================
        // INICIAR SCANNER
        // ============================================================
        function iniciarScanner() {
            if (iniciado || reiniciando) return;
            limpiarError();

            if (typeof Html5QrcodeScanner === 'undefined') {
                setError('Libreria QR no cargada.'); return;
            }
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                setError('Tu navegador no soporta camara, o no estas en HTTPS.'); return;
            }
            const reader = document.getElementById('qr-reader');
            if (!reader) { setError('Contenedor qr-reader no existe.'); return; }

            // Destruye cualquier instancia previa y libera camara
            destruirScanner();
            reader.innerHTML = '';
            iniciado = true;
            setStatus('Iniciando camara...');
            getAudioCtx();

            // ------------------------------------------------------------
            // CAPA 1: espera inicial de 1.5s
            // ------------------------------------------------------------
            setTimeout(() => {
                if (!iniciado) return;

                // ------------------------------------------------------------
                // CAPA 2: liberar camara una vez mas antes de pedirla
                // ------------------------------------------------------------
                forzarLiberacionCamara();

                // ------------------------------------------------------------
                // CAPA 3: otra espera de 800ms antes de crear el scanner
                // ------------------------------------------------------------
                setTimeout(() => {
                    if (!iniciado) return;

                    try {
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
                    } catch (e) {
                        setError('Error al crear scanner: ' + ((e && e.message) || e));
                        iniciado = false;
                        reiniciarScanner();
                        return;
                    }

                    const onScanSuccess = (texto) => {
                        try {
                            const m = texto.match(/\\b(\\d{8})\\b/);
                            if (!m) return;
                            const dni = m[1];
                            const ahora = Date.now();

                            const textoHash = dni + '|' + texto.length;
                            if (textoHash === ultimoTextoHash && (ahora - ultimoTimestampFrame) < FRAME_COOLDOWN_MS) {
                                return;
                            }
                            ultimoTextoHash = textoHash;
                            ultimoTimestampFrame = ahora;

                            if (dni === ultimoDniEmitido && (ahora - ultimoTimestampDni) < DNI_COOLDOWN_MS) {
                                return;
                            }
                            ultimoDniEmitido = dni;
                            ultimoTimestampDni = ahora;

                            setStatus('QR: ' + dni);
                            setTriggerValue("qr_dni", dni);
                        } catch (e) { console.error('[QR] onScanSuccess:', e); }
                    };

                    let resultado;
                    try {
                        resultado = scanner.render(onScanSuccess, () => {});
                    } catch (e) {
                        const msg = (e && e.message) || String(e);
                        if (msg.toLowerCase().includes('notreadable') ||
                            msg.toLowerCase().includes('could not start')) {
                            console.warn('[QR] NotReadableError capturado, reintentando...');
                            iniciado = false;
                            reiniciarScanner();
                            return;
                        }
                        setError('Error al iniciar: ' + msg);
                        iniciado = false;
                        reiniciarScanner();
                        return;
                    }

                    if (resultado && typeof resultado.then === 'function') {
                        resultado
                            .then(() => {
                                intentosFallidos = 0;
                                setStatus('Camara activa. Apunta al codigo.');
                                iniciarHeartbeat();
                            })
                            .catch((e) => {
                                const msg = (e && e.message) || String(e);
                                if (msg.toLowerCase().includes('notreadable') ||
                                    msg.toLowerCase().includes('could not start')) {
                                    console.warn('[QR] NotReadableError en promise, reintentando...');
                                    iniciado = false;
                                    reiniciarScanner();
                                    return;
                                }
                                setError('Error camara: ' + msg);
                                iniciado = false;
                                reiniciarScanner();
                            });
                    } else {
                        intentosFallidos = 0;
                        setStatus('Camara activa. Apunta al codigo.');
                        iniciarHeartbeat();
                    }
                }, 800);
            }, 1500);
        }

        // ============================================================
        // VISIBILITY: destruir al ocultar, reiniciar al volver
        // ============================================================
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
                destruirScanner();
            } else if (document.visibilityState === 'visible') {
                // Espera 2s al volver para dar tiempo a que el sistema libere la camara
                setTimeout(() => {
                    if (!iniciado && !reiniciando) {
                        intentosFallidos = 0;
                        iniciarScanner();
                    }
                }, 2000);
            }
        });
        window.addEventListener('blur', () => {
            if (document.visibilityState === 'hidden') destruirScanner();
        });
        window.addEventListener('focus', () => {
            if (document.visibilityState === 'visible') {
                setTimeout(() => {
                    if (!iniciado && !reiniciando) {
                        intentosFallidos = 0;
                        iniciarScanner();
                    }
                }, 2000);
            }
        });
        window.addEventListener('beforeunload', () => {
            destruirScanner();
            forzarLiberacionCamara();
        });

        // ============================================================
        // CARGA DE LIBRERIA
        // ============================================================
        if (window.__qrV21Listo && typeof Html5QrcodeScanner !== 'undefined') {
            iniciarScanner();
            return;
        }
        if (window.__qrV21Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (typeof Html5QrcodeScanner !== 'undefined') {
                    clearInterval(t);
                    window.__qrV21Listo = true;
                    window.__qrV21Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrV21Cargando = false;
                    setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }
        window.__qrV21Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode';
        s.async = true;
        s.onload = () => {
            window.__qrV21Listo = true;
            window.__qrV21Cargando = false;
            setTimeout(() => {
                if (typeof Html5QrcodeScanner === 'undefined') {
                    setError('Libreria cargada sin Html5QrcodeScanner.');
                    return;
                }
                iniciarScanner();
            }, 100);
        };
        s.onerror = () => {
            window.__qrV21Cargando = false;
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
