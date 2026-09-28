import streamlit as st
QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v15",
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
    #qr-reader video {
        border-radius: 6px;
        width: 100% !important; height: auto !important;
    }
    #qr-status {
        text-align: center; font-size: 13px; margin-top: 8px;
        color: #666;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    #qr-error {
        text-align: center; font-size: 13px; margin-top: 8px;
        color: #C62828; font-weight: 600;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        padding: 10px; border: 1px solid #C62828; border-radius: 6px;
        background: #f8d7da;
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
        let scanner = null;
        let iniciado = false;
        let ultimoFbKey = "";
        let audioCtx = null;

        console.log("[QR] init. args =", component.args);
      
        function getAudioCtx() {
            if (!audioCtx) {
                try {
                    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                    console.log("[QR] AudioContext creado. state:", audioCtx.state);
                } catch (e) {
                    console.warn("[QR] AudioContext error:", e);
                }
            }
            if (audioCtx && audioCtx.state === "suspended") {
                audioCtx.resume().then(() => {
                    console.log("[QR] AudioContext reanudado. state:", audioCtx.state);
                }).catch((e) => console.warn("[QR] resume error:", e));
            }
            return audioCtx;
        }

        function _tono(freq, dur, tipo, vol, delay) {
            const ctx = getAudioCtx();
            if (!ctx) { console.warn("[QR] no ctx"); return; }
            const t0 = ctx.currentTime + (delay || 0);
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.connect(g); g.connect(ctx.destination);
            osc.type = tipo || "sine";
            osc.frequency.setValueAtTime(freq, t0);
            g.gain.setValueAtTime(0, t0);
            g.gain.linearRampToValueAtTime(vol || 0.35, t0 + 0.015);
            g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
            osc.start(t0);
            osc.stop(t0 + dur + 0.02);
            console.log("[QR] tono:", freq, "Hz", tipo, "dur", dur);
        }

        function pitidoNuevo() {
            _tono(523, 0.12, "sine", 0.40, 0);
            _tono(659, 0.15, "sine", 0.40, 0.09);
        }
        function pitidoTardanza() {
            _tono(440, 0.25, "sine", 0.35, 0);
        }
        function pitidoDuplicado() {
            _tono(233, 0.18, "square", 0.30, 0);
            _tono(185, 0.22, "square", 0.30, 0.20);
        }
        function pitidoBloqueado() {
            _tono(180, 0.15, "sawtooth", 0.35, 0);
            _tono(140, 0.15, "sawtooth", 0.35, 0.18);
            _tono(100, 0.30, "sawtooth", 0.35, 0.36);
        }
        function pitidoError() {
            _tono(330, 0.10, "square", 0.32, 0);
        }

        
        function reproducirFeedback(kind, texto) {
            console.log("[QR] >>> reproducirFeedback:", kind, "|", texto);
            try {
                getAudioCtx();
                switch (kind) {
                    case "nuevo":      setStatus("QR: " + (texto || "")); pitidoNuevo(); break;
                    case "tardanza":   setStatus("QR tardanza: " + (texto || "")); pitidoTardanza(); break;
                    case "duplicado":  setStatus("QR duplicado: " + (texto || "")); pitidoDuplicado(); break;
                    case "bloqueado":  setStatus("BLOQUEADO: " + (texto || "")); pitidoBloqueado(); break;
                    case "error":
                    default:           setStatus("Error: " + (texto || "")); pitidoError(); break;
                }
            } catch (e) { console.error("[QR] reproducirFeedback error:", e); }
        }

        window.__qrFeedback = reproducirFeedback;

        function leerArgs() {
            try {
                const c = component || {};
                const fuentes = [c.args, c.data, c.value, c.props, c];
                for (const f of fuentes) {
                    if (f && typeof f === "object" &&
                        ("feedback_kind" in f || "feedback_ts" in f)) {
                        return {
                            kind: f.feedback_kind || "",
                            texto: f.feedback_texto || "",
                            ts: f.feedback_ts || 0
                        };
                    }
                }
                return null;
            } catch (e) { return null; }
        }

        function chequearFeedback() {
            const a = leerArgs();
            if (!a || !a.kind) return;
            const key = a.kind + "|" + a.texto + "|" + a.ts;
            if (key === ultimoFbKey) return;
            ultimoFbKey = key;
            reproducirFeedback(a.kind, a.texto);
        }

        chequearFeedback();
        setTimeout(chequearFeedback, 250);
        setTimeout(chequearFeedback, 600);
        setTimeout(chequearFeedback, 1200);
        setInterval(chequearFeedback, 800);

        if (typeof component.onArgsChange === "function") {
            try { component.onArgsChange(() => chequearFeedback()); }
            catch (e) {}
        }


        function setStatus(t) {
            const el = document.getElementById("qr-status");
            if (el) { el.textContent = t; el.style.display = "block"; }
        }
        function setError(t) {
            const e = document.getElementById("qr-error");
            const s = document.getElementById("qr-status");
            if (e) { e.textContent = t; e.style.display = "block"; }
            if (s) s.style.display = "none";
            console.error("[QR]", t);
        }

    
        function iniciarScanner() {
            if (iniciado) return;
            if (typeof Html5QrcodeScanner === "undefined") { setError("Libreria QR no cargada."); return; }
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                setError("Sin acceso a camara o no estas en HTTPS."); return;
            }
            const reader = document.getElementById("qr-reader");
            if (!reader) { setError("Contenedor no existe."); return; }
            if (scanner) { try { scanner.clear(); } catch(e){} scanner = null; }
            reader.innerHTML = "";
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
                    const m = texto.match(/\\b(\\d{8})\\b/);
                    if (!m) return;
                    setTriggerValue("qr_dni", m[1]);
                };
                const onScanError = () => {};
                let r;
                try { r = scanner.render(onScanSuccess, onScanError); }
                catch (e) {
                    setError("Error al iniciar: " + ((e && e.message) || e));
                    iniciado = false; return;
                }
                if (r && typeof r.then === "function") {
                    r.then(() => setStatus("Camara activa.")).catch((e) => {
                        setError("Error camara: " + ((e && e.message) || e));
                        iniciado = false;
                    });
                } else {
                    setStatus("Camara activa.");
                }
            }, 500);
        }

      
        if (window.__qrV15Listo && typeof Html5QrcodeScanner !== "undefined") {
            iniciarScanner();
        } else if (window.__qrV15Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (typeof Html5QrcodeScanner !== "undefined") {
                    clearInterval(t);
                    window.__qrV15Listo = true;
                    window.__qrV15Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrV15Cargando = false;
                    setError("Timeout cargando libreria.");
                }
            }, 100);
        } else {
            window.__qrV15Cargando = true;
            const s = document.createElement("script");
            s.src = "https://unpkg.com/html5-qrcode";
            s.async = true;
            s.onload = () => {
                window.__qrV15Listo = true;
                window.__qrV15Cargando = false;
                setTimeout(iniciarScanner, 50);
            };
            s.onerror = () => {
                window.__qrV15Cargando = false;
                setError("Error al cargar html5-qrcode del CDN.");
            };
            document.head.appendChild(s);
        }
    }
    """,
)

def qr_scanner(key="qr_scanner", on_scan=None,
               feedback_kind="", feedback_texto="", feedback_ts=0):
    if on_scan is None:
        on_scan = lambda: None
    return QR_SCANNER_COMPONENT(
        key=key,
        on_qr_dni_change=on_scan,
        feedback_kind=feedback_kind,
        feedback_texto=feedback_texto,
        feedback_ts=feedback_ts,
    )
