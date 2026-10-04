# qr_scanner_component.py
# Componente de escaneo QR para Streamlit usando BarcodeDetector API + Polyfill ZXing.
# Diseñado para evitar el error NotReadableError en Android.
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v22",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <video id="qr-video" style="width:100%; border-radius:8px; background:#000;" playsinline autoplay muted></video>
        <div id="qr-status">Iniciando camara...</div>
        <div id="qr-error" style="display:none;"></div>
    </div>
    """,
    css="""
    #qr-wrapper { width: 100%; max-width: 500px; margin: 0 auto; }
    #qr-video {
        border-radius: 8px;
        overflow: hidden;
        border: 2px solid #E65100;
        background: #000;
        min-height: 260px;
        object-fit: cover;
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
    """,
    js="""
    export default function(component) {
        const { setTriggerValue } = component;
        
        // ============================================================
        // CONFIGURACION
        // ============================================================
        const DNI_REGEX = /\\b(\\d{8})\\b/;
        const SCAN_INTERVAL_MS = 250; // Intervalo entre frames (mas alto = menos CPU)
        const DNI_COOLDOWN_MS = 3000;  // Evita repetir el mismo DNI muy rapido
        
        let stream = null;
        let detector = null;
        let videoElement = null;
        let scanLoop = null;
        let ultimoDni = null;
        let ultimoTimestampDni = 0;
        let iniciado = false;
        let polyfillCargado = false;
        let intentosFallidos = 0;
        const MAX_INTENTOS = 5;

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
                stream.getTracks().forEach(track => {
                    try { track.stop(); } catch(e) {}
                });
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
        // CARGA DEL POLYFILL (ZXing WASM)
        // Solo se carga si el navegador no tiene BarcodeDetector nativo.
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
                // Polyfill de ZXing (WASM). Liviano y compatible con Firefox/Safari.
                script.src = 'https://cdn.jsdelivr.net/npm/@sec-ant/barcode-detector@1.3/dist/iife/side-effects.min.js';
                script.async = true;
                script.onload = () => {
                    polyfillCargado = true;
                    console.log('[QR] Polyfill cargado.');
                    resolve();
                };
                script.onerror = () => {
                    reject(new Error('No se pudo cargar el polyfill de escaneo.'));
                };
                document.head.appendChild(script);
            });
        }

        // ============================================================
        // INICIAR ESCANER
        // ============================================================
        async function iniciarScanner() {
            if (iniciado) return;
            limpiarError();
            detenerCamara();

            videoElement = document.getElementById('qr-video');
            if (!videoElement) {
                setError('Contenedor de video no encontrado.');
                return;
            }

            try {
                // 1. Cargar polyfill si es necesario
                await cargarPolyfill();

                // 2. Verificar soporte de BarcodeDetector
                if (typeof BarcodeDetector === 'undefined') {
                    setError('Este navegador no soporta escaneo de codigos. Usa Chrome o Firefox actualizado.');
                    return;
                }

                // 3. Crear detector (solo QR)
                // Nota: El polyfill acepta 'qr_code'. La API nativa tambien.
                detector = new BarcodeDetector({ formats: ['qr_code'] });

                // 4. Solicitar camara (trasera)
                setStatus('Solicitando acceso a la camara...');
                stream = await navigator.mediaDevices.getUserMedia({
                    video: {
                        facingMode: 'environment',
                        width: { ideal: 1280 },
                        height: { ideal: 720 }
                    },
                    audio: false
                });

                // 5. Asignar stream al video
                videoElement.srcObject = stream;
                videoElement.setAttribute('playsinline', 'true');
                videoElement.setAttribute('muted', 'true');
                
                await videoElement.play();
                iniciado = true;
                intentosFallidos = 0;
                setStatus('Camara activa. Apunta al codigo QR.');
                console.log('[QR] Camara iniciada correctamente.');

                // 6. Iniciar bucle de escaneo
                bucleEscaneo();

            } catch (err) {
                console.error('[QR] Error al iniciar:', err);
                detenerCamara();
                intentosFallidos++;

                // Manejar error especifico de camara ocupada
                const esNotReadable = err.name === 'NotReadableError' || 
                                     (err.message && err.message.toLowerCase().includes('could not start'));
                
                if (esNotReadable && intentosFallidos < MAX_INTENTOS) {
                    setStatus('Camara ocupada. Reintentando en 2 segundos... (intento ' + intentosFallidos + '/' + MAX_INTENTOS + ')');
                    // Esperar antes de reintentar (da tiempo a que el sistema libere la camara)
                    setTimeout(() => {
                        iniciarScanner();
                    }, 2000);
                    return;
                }

                if (esNotReadable) {
                    setError('No se pudo acceder a la camara. Cierra otras aplicaciones que la esten usando y recarga la pagina.');
                } else if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
                    setError('Permiso de camara denegado. Concede el permiso en la configuracion del navegador.');
                } else if (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError') {
                    setError('No se encontro ninguna camara en este dispositivo.');
                } else {
                    setError('Error al acceder a la camara: ' + (err.message || err.name));
                }
            }
        }

        // ============================================================
        // BUCLE DE ESCANEO
        // ============================================================
        function bucleEscaneo() {
            if (!iniciado || !videoElement || !detector) return;

            if (videoElement.readyState >= 2) {
                detector.detect(videoElement)
                    .then(barcodes => {
                        if (barcodes && barcodes.length > 0) {
                            const codigo = barcodes[0];
                            const texto = codigo.rawValue;
                            const match = texto.match(DNI_REGEX);
                            
                            if (match) {
                                const dni = match[1];
                                const ahora = Date.now();

                                // Evitar repetir el mismo DNI muy rapido
                                if (dni === ultimoDni && (ahora - ultimoTimestampDni) < DNI_COOLDOWN_MS) {
                                    // Ignorar silenciosamente
                                } else {
                                    ultimoDni = dni;
                                    ultimoTimestampDni = ahora;
                                    setStatus('QR detectado: ' + dni);
                                    setTriggerValue("qr_dni", dni);
                                    console.log('[QR] DNI emitido:', dni);
                                }
                            }
                        }
                    })
                    .catch(err => {
                        // Errores de deteccion son normales (frames borrosos), no los mostramos.
                        console.debug('[QR] Error de deteccion:', err);
                    });
            }

            // Programar siguiente escaneo
            scanLoop = setTimeout(bucleEscaneo, SCAN_INTERVAL_MS);
        }

        // ============================================================
        // LIMPIEZA AL DESMONTAR / OCULTAR
        // ============================================================
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
                detenerCamara();
                setStatus('Camara en pausa.');
            } else if (document.visibilityState === 'visible') {
                // Reanudar automaticamente al volver
                if (!iniciado) {
                    setTimeout(() => {
                        if (!iniciado) iniciarScanner();
                    }, 1000);
                }
            }
        });

        window.addEventListener('beforeunload', detenerCamara);

        // ============================================================
        // ARRANQUE
        // ============================================================
        // Esperar un poco a que el DOM este listo, luego iniciar.
        setTimeout(() => {
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
