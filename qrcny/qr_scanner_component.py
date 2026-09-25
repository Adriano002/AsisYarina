# qr_scanner_component.py
# Componente propio de escaneo QR para Streamlit.
# v10: libera camara + retraso de seguridad para moviles + pitidos diferenciados.
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v10",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <div id="qr-reader"></div>
        <div id="qr-status">Iniciando camara...</div>
        <div id="qr-error" style="display:none;"></div>
        <div id="qr-msg" style="display:none;"></div>
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
    #qr-msg {
        text-align: center; font-size: 16px; margin-top: 12px;
        font-weight: 700; padding: 14px; border-radius: 8px;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        transition: all 0.2s ease;
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
    """,
    js="""
    export default function(component) {
        const { setTriggerValue } = component;

        let ultimoDni = null;
        let ultimoTs = 0;
        let scanner = null;
        let iniciado = false;
        let ultimoPitidoTs = 0;
        let destroyed = false;

        // ============================================================
        // AUDIO: pitidos diferenciados (Web Audio API, sin archivos)
        // ============================================================
        let audioCtx = null;
        function getAudioCtx() {
            if (!audioCtx) {
                try {
                    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                } catch (e) { console.warn('[QR] AudioContext:', e); }
            }
            if (audioCtx && audioCtx.state === 'suspended') {
                audioCtx.resume().catch(() => {});
            }
            return audioCtx;
        }

        function tocarTono(freqs, dur, tipo, vol) {
            const ctx = getAudioCtx();
            if (!ctx) return;
            vol = vol || 0.35;
            tipo = tipo || 'sine';
            try {
                freqs.forEach((f, i) => {
                    const t0 = ctx.currentTime + i * (dur * 0.6);
                    const osc = ctx.createOscillator();
                    const g = ctx.createGain();
                    osc.connect(g); g.connect(ctx.destination);
                    osc.type = tipo;
                    osc.frequency.setValueAtTime(f, t0);
                    g.gain.setValueAtTime(0, t0);
                    g.gain.linearRampToValueAtTime(vol, t0 + 0.015);
                    g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
                    osc.start(t0);
                    osc.stop(t0 + dur + 0.02);
                });
            } catch (e) { console.warn('[QR] tono:', e); }
        }

        // Escaneo nuevo: 2 notas agudas ascendentes
        function pitidoNuevo() {
            const ahora = Date.now();
            if (ahora - ultimoPitidoTs < 400) return;
            ultimoPitidoTs = ahora;
            tocarTono([523, 659], 0.14, 'sine', 0.35);
        }

        // Duplicado: 2 notas graves descendentes
        function pitidoDuplicado() {
            const ahora = Date.now();
            if (ahora - ultimoPitidoTs < 400) return;
            ultimoPitidoTs = ahora;
            tocarTono([392, 294], 0.20, 'triangle', 0.40);
        }

        // Bloqueado: 3 notas muy graves tipo alarma
        function pitidoBloqueado() {
            const ahora = Date.now();
            if (ahora - ultimoPitidoTs < 400) return;
            ultimoPitidoTs = ahora;
            tocarTono([220, 180, 140], 0.22, 'sawtooth', 0.45);
        }

        // Error generico: 1 nota grave cuadrada
        function pitidoError() {
            const ahora = Date.now();
            if (ahora - ultimoPitidoTs < 400) return;
            ultimoPitidoTs = ahora;
            tocarTono([160], 0.30, 'square', 0.35);
        }

        // ============================================================
        // MENSAJE VISUAL GRANDE
        // ============================================================
        function mostrarMensaje(texto, colorFondo, colorTexto) {
            const el = document.getElementById('qr-msg');
            if (!el) return;
            el.textContent = texto;
            el.style.background = colorFondo;
            el.style.color = colorTexto;
            el.style.display = 'block';
            clearTimeout(el._timer);
            el._timer = setTimeout(() => { el.style.display = 'none'; }, 3500);
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

        // LIBERACION REAL DE CAMARA
        function liberarCamara() {
            if (scanner) {
                try { scanner.clear(); } catch (e) {}
                scanner = null;
            }
            try {
                document.querySelectorAll('video').forEach(v => {
                    const s = v.srcObject;
                    if (s && typeof s.getTracks === 'function') {
                        s.getTracks().forEach(t => { try { t.stop(); } catch(e){} });
                    }
                    v.srcObject = null;
                    if (v.pause) v.pause();
                });
            } catch (e) {}
            iniciado = false;
        }
        function destruirScanner() {
            destroyed = true;
            liberarCamara();
        }

        // ============================================================
        // SCANNER (con retraso de seguridad para moviles)
        // ============================================================
        function iniciarScanner() {
            if (iniciado || destroyed) return;
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

            liberarCamara();
            reader.innerHTML = '';
            iniciado = true;
            getAudioCtx();

            // RETRASO: dar tiempo al movil para soltar la camara
            setTimeout(() => {
                if (destroyed || !iniciado) return;

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
                        setStatus('QR detectado: ' + dni);
                        setTriggerValue("qr_dni", dni);
                    } catch (e) {
                        console.error('[QR] onScanSuccess:', e);
                    }
                };
                const onScanError = () => {};

                let resultado;
                try {
                    resultado = scanner.render(onScanSuccess, onScanError);
                } catch (e) {
                    setError('Error al iniciar: ' + (e.message || e));
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
                                setError('La camara esta siendo usada por otra aplicacion. Cierra otras pestanas/apps y espera unos segundos.');
                            } else {
                                setError('Error camara: ' + msg);
                            }
                            iniciado = false;
                        });
                } else {
                    setStatus('Camara activa.');
                }
            }, 700);
        }

        // ============================================================
        // API PUBLICA: Python llama esto para pedir pitido + mensaje
        // ============================================================
        window.__qrFeedback = function(kind, texto) {
            if (kind === 'nuevo') {
                pitidoNuevo();
                mostrarMensaje(texto || 'NUEVO', '#d4edda', '#155724');
            } else if (kind === 'duplicado') {
                pitidoDuplicado();
                mostrarMensaje(texto || 'YA REGISTRADO', '#fff3cd', '#856404');
            } else if (kind === 'bloqueado') {
                pitidoBloqueado();
                mostrarMensaje(texto || 'BLOQUEADO', '#f8d7da', '#721c24');
            } else {
                pitidoError();
                mostrarMensaje(texto || 'ERROR', '#e2e3e5', '#383d41');
            }
        };

        // ============================================================
        // LIMPIEZA AL DESMONTAR
        // ============================================================
        window.addEventListener('beforeunload', destruirScanner);
        window.addEventListener('pagehide', destruirScanner);
        window.addEventListener('unload', destruirScanner);

        if (component.onCleanup) {
            try { component.onCleanup(() => { destruirScanner(); }); } catch (e) {}
        }

        // ============================================================
        // CARGA DE LIBRERIA
        // ============================================================
        if (window.__qrV10Listo && typeof Html5QrcodeScanner !== 'undefined') {
            iniciarScanner();
            return;
        }
        if (window.__qrV10Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (typeof Html5QrcodeScanner !== 'undefined') {
                    clearInterval(t);
                    window.__qrV10Listo = true;
                    window.__qrV10Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrV10Cargando = false;
                    setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }
        window.__qrV10Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode';
        s.async = true;
        s.onload = () => {
            window.__qrV10Listo = true;
            window.__qrV10Cargando = false;
            setTimeout(() => {
                if (typeof Html5QrcodeScanner === 'undefined') {
                    setError('Libreria cargada pero sin Html5QrcodeScanner.');
                    return;
                }
                iniciarScanner();
            }, 50);
        };
        s.onerror = () => {
            window.__qrV10Cargando = false;
            setError('Error al cargar html5-qrcode del CDN.');
        };
        document.head.appendChild(s);
    }
    """,
)


def qr_scanner(key="qr_scanner", on_scan=None):
    """
    Monta el componente escaner QR.
    Devuelve el resultado con atributo .qr_dni
    """
    if on_scan is None:
        on_scan = lambda: None
    return QR_SCANNER_COMPONENT(
        key=key,
        on_qr_dni_change=on_scan,
    )
