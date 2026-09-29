# qr_scanner_component.py
# Componente de escaneo QR para Streamlit con feedback de sonido en TIEMPO REAL.
#
# - El JS recibe del backend la lista de DNIs ya registrados hoy y los bloqueados.
# - Al detectar un QR, el JS decide el sonido LOCALMENTE y lo dispara AL INSTANTE.
# - No espera a Python para sonar (elimina la latencia del rerun de Streamlit).
# - Python igual procesa el escaneo despues (para actualizar BD y estado).
# - Cola interna de sonidos para que nunca se pisen.
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v20",
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
    """,
    js="""
    export default function(component) {
        const { setTriggerValue, data } = component;

        // ============================================================
        // DATOS INICIALES DEL BACKEND
        // ============================================================
        // data.ya_registrados: { "12345678": {"estado": "Puntual", "hora": "06:45"}, ... }
        // data.bloqueados: ["11111111", "22222222", ...]
        let yaRegistrados = new Map();
        let bloqueados = new Set();

        try {
            const yr = (data && data.ya_registrados) || {};
            for (const k of Object.keys(yr)) yaRegistrados.set(k, yr[k]);
            const bl = (data && data.bloqueados) || [];
            for (const b of bl) bloqueados.add(b);
            console.log('[QR] Estado inicial cargado:', yaRegistrados.size, 'registrados,', bloqueados.size, 'bloqueados');
        } catch (e) {
            console.warn('[QR] no se pudo cargar estado inicial:', e);
        }

        let scanner = null;
        let iniciado = false;
        let reiniciando = false;

        // ============================================================
        // AUDIO
        // ============================================================
        let audioCtx = null;
        let audioDesbloqueado = false;

        function getAudioCtx() {
            if (!audioCtx) {
                try {
                    const AC = window.AudioContext || window.webkitAudioContext;
                    audioCtx = new AC();
                } catch (e) {
                    console.warn('[QR] no se pudo crear AudioContext:', e);
                    return null;
                }
            }
            if (audioCtx.state === 'suspended') {
                audioCtx.resume().catch(() => {});
            }
            return audioCtx;
        }

        function desbloquearAudio() {
            if (audioDesbloqueado) return;
            const ctx = getAudioCtx();
            if (!ctx) return;
            try {
                const osc = ctx.createOscillator();
                const g = ctx.createGain();
                g.gain.value = 0.0001;
                osc.connect(g); g.connect(ctx.destination);
                osc.start(0);
                osc.stop(ctx.currentTime + 0.01);
                audioDesbloqueado = true;
            } catch (e) {}
        }

        document.addEventListener('click', desbloquearAudio, { capture: true });
        document.addEventListener('touchstart', desbloquearAudio, { capture: true });
        document.addEventListener('keydown', desbloquearAudio, { capture: true });

        // ============================================================
        // MOTOR DE AUDIO
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
            g.gain.linearRampToValueAtTime(vol || 0.7, t0 + 0.008);
            g.gain.setValueAtTime(vol || 0.7, t0 + dur * 0.7);
            g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
            osc.start(t0);
            osc.stop(t0 + dur + 0.05);
        }

        // Sonidos fuertes, inconfundibles para adultos mayores
        function sonidoPuntual() {
            // 3 notas agudas ascendentes
            _tono(880,  0.12, 'sine', 0.75, 0.00);
            _tono(1108, 0.12, 'sine', 0.75, 0.14);
            _tono(1318, 0.22, 'sine', 0.75, 0.28);
        }
        function sonidoTardanza() {
            // Gong grave largo
            _tono(196, 0.50, 'sine',     0.80, 0.00);
            _tono(196, 0.50, 'triangle', 0.40, 0.00);
        }
        function sonidoDuplicado() {
            // 2 beeps agudos "uh-uh"
            _tono(1568, 0.10, 'square', 0.65, 0.00);
            _tono(1568, 0.10, 'square', 0.65, 0.14);
        }
        function sonidoBloqueado() {
            // Sirena grave repetida - urgente
            _tono(300, 0.18, 'sawtooth', 0.85, 0.00);
            _tono(220, 0.18, 'sawtooth', 0.85, 0.22);
            _tono(300, 0.18, 'sawtooth', 0.85, 0.44);
            _tono(220, 0.18, 'sawtooth', 0.85, 0.66);
            _tono(160, 0.40, 'sawtooth', 0.85, 0.88);
        }
        function sonidoError() {
            // 3 notas descendentes graves
            _tono(400, 0.18, 'sawtooth', 0.75, 0.00);
            _tono(300, 0.18, 'sawtooth', 0.75, 0.22);
            _tono(200, 0.35, 'sawtooth', 0.75, 0.44);
        }

        // ============================================================
        // COLA DE SONIDOS - evita que se pisen
        // ============================================================
        let colaSonidos = [];
        let reproduciendo = false;

        function encolarSonido(kind) {
            colaSonidos.push(kind);
            if (!reproduciendo) procesarCola();
        }

        function procesarCola() {
            if (colaSonidos.length === 0) {
                reproduciendo = false;
                return;
            }
            reproduciendo = true;
            const kind = colaSonidos.shift();
            const duracionMs = reproducir(kind);
            setTimeout(() => procesarCola(), duracionMs);
        }

        function reproducir(kind) {
            try {
                getAudioCtx();
                switch (kind) {
                    case "puntual":     sonidoPuntual();    return 450;
                    case "tardanza":    sonidoTardanza();   return 600;
                    case "duplicado":   sonidoDuplicado();  return 300;
                    case "bloqueado":   sonidoBloqueado();  return 1300;
                    case "error":
                    default:            sonidoError();      return 850;
                }
            } catch (e) {
                console.error('[QR] reproducir error:', e);
                return 100;
            }
        }

        window.__qrFeedback = (kind) => encolarSonido(kind);
        try { window.parent.__qrFeedback = (kind) => encolarSonido(kind); } catch (e) {}

        // ============================================================
        // DECISION LOCAL DE SONIDO (SIN ESPERAR A PYTHON)
        // ============================================================
        function decidirSonidoLocal(dni) {
            // Bloqueado tiene prioridad
            if (bloqueados.has(dni)) return "bloqueado";

            // Ya registrado hoy?
            if (yaRegistrados.has(dni)) {
                const info = yaRegistrados.get(dni);
                // Si el estado es Tardanza ya registrada, suena tardanza de nuevo (informativo)
                // Si es cualquier otro estado, es duplicado
                if (info && info.estado === "Tardanza") return "tardanza";
                return "duplicado";
            }

            // No registrado -> puntual (optimista)
            return "puntual";
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
                setError('Tu navegador no soporta camara, o no estas en HTTPS.');
                return;
            }
            const reader = document.getElementById('qr-reader');
            if (!reader) { setError('Contenedor qr-reader no existe.'); return; }

            destruirScanner();
            reader.innerHTML = '';
            iniciado = true;

            getAudioCtx();

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

                        // Evitar duplicado de la MISMA lectura en <1.5s (rebote del escaner)
                        const ahora = Date.now();
                        if (window.__qrUltimoDni === dni && (ahora - window.__qrUltimoTs) < 1500) return;
                        window.__qrUltimoDni = dni;
                        window.__qrUltimoTs = ahora;

                        setStatus('QR: ' + dni);

                        // === SONIDO EN TIEMPO REAL (SIN ESPERAR A PYTHON) ===
                        const sonido = decidirSonidoLocal(dni);
                        encolarSonido(sonido);

                        // Actualizar cache local para siguientes escaneos rapidos
                        if (sonido === "puntual") {
                            yaRegistrados.set(dni, { estado: "Puntual", hora: "ahora" });
                        }

                        // Enviar a Python para procesar la BD
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
            setTimeout(() => { if (iniciado) reanudarCamara(); }, 300);
        });

        // ============================================================
        // CARGA DE LIBRERIA
        // ============================================================
        window.addEventListener('beforeunload', destruirScanner);

        if (window.__qrV20Listo && typeof Html5QrcodeScanner !== 'undefined') {
            iniciarScanner();
            return;
        }
        if (window.__qrV20Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (typeof Html5QrcodeScanner !== 'undefined') {
                    clearInterval(t);
                    window.__qrV20Listo = true;
                    window.__qrV20Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrV20Cargando = false;
                    setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }
        window.__qrV20Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode';
        s.async = true;
        s.onload = () => {
            window.__qrV20Listo = true;
            window.__qrV20Cargando = false;
            setTimeout(() => {
                if (typeof Html5QrcodeScanner === 'undefined') {
                    setError('Libreria cargada pero sin Html5QrcodeScanner.');
                    return;
                }
                iniciarScanner();
            }, 50);
        };
        s.onerror = () => {
            window.__qrV20Cargando = false;
            setError('Error al cargar html5-qrcode del CDN.');
        };
        document.head.appendChild(s);
    }
    """,
)


def qr_scanner(key="qr_scanner", on_scan=None, ya_registrados=None, bloqueados=None):
    """
    ya_registrados: dict {dni: {"estado": "Puntual"|"Tardanza"|..., "hora": "HH:MM"}}
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
