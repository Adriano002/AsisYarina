# qr_scanner_component.py
# Componente de escaneo QR para Streamlit con feedback de sonido en TIEMPO REAL.
#
# - El JS recibe del backend la lista de DNIs ya registrados hoy y los bloqueados.
# - Al detectar un QR, el JS decide el sonido LOCALMENTE y lo dispara AL INSTANTE.
# - No espera a Python para sonar (elimina la latencia del rerun de Streamlit).
# - Anti-rebote y MUTE en window.parent (compartido entre iframes hermanos).
# - Cola interna de sonidos que se vacia si esta muteado.
# - Pausa el scanner 3s despues de cada lectura (evita loop infinito).
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v23",
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
        let pausadoPorLectura = false;

        // ============================================================
        // ACCESO A window.parent (compartido entre TODOS los iframes hijos)
        // ============================================================
        // Cada iframe de Streamlit tiene su propio localStorage (sandboxed),
        // pero TODOS comparten el mismo window.parent. Por eso usamos parent
        // para el mute y el cache: sobrevive a remounts y se comparte entre
        // el iframe viejo y el nuevo durante la transicion.
        function parentGet(key, defaultVal) {
            try {
                if (window.parent && window.parent[key] !== undefined) {
                    return window.parent[key];
                }
            } catch (e) {}
            return defaultVal;
        }

        function parentSet(key, val) {
            try {
                window.parent[key] = val;
            } catch (e) {
                try { window[key] = val; } catch (e2) {}
            }
        }

        // ============================================================
        // MUTE GLOBAL
        // ============================================================
        function estaMuteado() {
            const muteHasta = parseInt(parentGet('__qrMuteHasta', 0), 10) || 0;
            return Date.now() < muteHasta;
        }

        function mutearPor(ms) {
            parentSet('__qrMuteHasta', Date.now() + ms);
        }

        // ============================================================
        // CACHE GLOBAL DE ESCANEOS
        // ============================================================
        function leerCacheScan() {
            return parentGet('__qrCacheScan', {}) || {};
        }

        function guardarCacheScan(cache) {
            parentSet('__qrCacheScan', cache);
        }

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

        function sonidoPuntual() {
            _tono(880,  0.12, 'sine', 0.75, 0.00);
            _tono(1108, 0.12, 'sine', 0.75, 0.14);
            _tono(1318, 0.22, 'sine', 0.75, 0.28);
        }
        function sonidoTardanza() {
            _tono(196, 0.50, 'sine',     0.80, 0.00);
            _tono(196, 0.50, 'triangle', 0.40, 0.00);
        }
        function sonidoDuplicado() {
            _tono(1568, 0.10, 'square', 0.65, 0.00);
            _tono(1568, 0.10, 'square', 0.65, 0.14);
        }
        function sonidoBloqueado() {
            _tono(300, 0.18, 'sawtooth', 0.85, 0.00);
            _tono(220, 0.18, 'sawtooth', 0.85, 0.22);
            _tono(300, 0.18, 'sawtooth', 0.85, 0.44);
            _tono(220, 0.18, 'sawtooth', 0.85, 0.66);
            _tono(160, 0.40, 'sawtooth', 0.85, 0.88);
        }
        function sonidoError() {
            _tono(400, 0.18, 'sawtooth', 0.75, 0.00);
            _tono(300, 0.18, 'sawtooth', 0.75, 0.22);
            _tono(200, 0.35, 'sawtooth', 0.75, 0.44);
        }

        // ============================================================
        // COLA DE SONIDOS
        // ============================================================
        let colaSonidos = [];
        let reproduciendo = false;

        function encolarSonido(kind) {
            // Chequeo 1: al ENCOLAR, si esta muteado, no encolar
            if (estaMuteado()) {
                console.log('[QR] encolar bloqueado por mute:', kind);
                return;
            }
            colaSonidos.push(kind);
            if (!reproduciendo) procesarCola();
        }

        function procesarCola() {
            if (colaSonidos.length === 0) { reproduciendo = false; return; }

            // Chequeo 2: al REPRODUCIR, si esta muteado, vaciar cola
            if (estaMuteado()) {
                console.log('[QR] cola vaciada por mute (' + colaSonidos.length + ' sonidos)');
                colaSonidos = [];
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
        // DECISION LOCAL DE SONIDO
        // ============================================================
        function decidirSonidoLocal(dni) {
            if (bloqueados.has(dni)) return "bloqueado";
            if (yaRegistrados.has(dni)) {
                const info = yaRegistrados.get(dni);
                if (info && info.estado === "Tardanza") return "tardanza";
                return "duplicado";
            }
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
        // PAUSAR SCANNER DESPUES DE CADA LECTURA
        // ============================================================
        function pausarYReanudar() {
            if (pausadoPorLectura) return;
            pausadoPorLectura = true;

            try {
                if (scanner && typeof scanner.pause === 'function') {
                    scanner.pause(true);
                    console.log('[QR] Scanner pausado 3s');
                }
            } catch (e) {
                console.warn('[QR] pause fallo:', e);
            }

            setTimeout(() => {
                try {
                    if (scanner && typeof scanner.resume === 'function') {
                        scanner.resume();
                        console.log('[QR] Scanner reanudado');
                    }
                } catch (e) {
                    console.warn('[QR] resume fallo:', e);
                }
                pausadoPorLectura = false;
            }, 3000);
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
                        fps: 5,
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
                        const m = texto.match(/\\b(\\d{8})\\b/);
                        if (!m) return;
                        const dni = m[1];

                        // === ANTI-REBOTE GLOBAL (window.parent, sobrevive a remounts) ===
                        const ahora = Date.now();
                        let cache = leerCacheScan();

                        // Limpiar entradas viejas (>10s)
                        for (const k of Object.keys(cache)) {
                            if ((ahora - cache[k]) > 10000) delete cache[k];
                        }

                        // Si este DNI ya fue escaneado hace <10s, IGNORAR
                        if (cache[dni]) {
                            setStatus('QR ya leido: ' + dni);
                            // Renovar el mute para que el iframe viejo no suene
                            mutearPor(3000);
                            return;
                        }

                        cache[dni] = ahora;
                        guardarCacheScan(cache);

                        setStatus('QR: ' + dni);

                        // Sonido en tiempo real
                        const sonido = decidirSonidoLocal(dni);
                        encolarSonido(sonido);

                        // MUTE GLOBAL 3 segundos: cualquier iframe que intente
                        // sonar durante este tiempo, no sonara.
                        mutearPor(3000);

                        // Actualizar cache local
                        if (sonido === "puntual") {
                            yaRegistrados.set(dni, { estado: "Puntual", hora: "ahora" });
                        }

                        // Enviar a Python
                        setTriggerValue("qr_dni", dni);

                        // Pausar el scanner 3s
                        pausarYReanudar();
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

        if (window.__qrV23Listo && typeof Html5QrcodeScanner !== 'undefined') {
            iniciarScanner();
            return;
        }
        if (window.__qrV23Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (typeof Html5QrcodeScanner !== 'undefined') {
                    clearInterval(t);
                    window.__qrV23Listo = true;
                    window.__qrV23Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrV23Cargando = false;
                    setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }
        window.__qrV23Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode';
        s.async = true;
        s.onload = () => {
            window.__qrV23Listo = true;
            window.__qrV23Cargando = false;
            setTimeout(() => {
                if (typeof Html5QrcodeScanner === 'undefined') {
                    setError('Libreria cargada pero sin Html5QrcodeScanner.');
                    return;
                }
                iniciarScanner();
            }, 50);
        };
        s.onerror = () => {
            window.__qrV23Cargando = false;
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
