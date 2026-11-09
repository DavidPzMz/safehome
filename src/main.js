// Direccion del servidor de Python (corre en la misma compu)
const SERVIDOR = 'http://127.0.0.1:8765';
const URL_WS = 'ws://127.0.0.1:8765/ws/estado';

// Variables que usa todo el panel
let ws = null;
let temporizadorReconexion = null;
let zonasActuales = {};
let dibujandoZona = false;
let inicioZona = null;
let finZona = null;
let fondoLienzo = null;
let zonasVisibles = true;
let temporizadorVideo = null;
let pidiendoVideo = false;
let camaraActiva = false;
let datosCamarasCache = null;
let modoApp = 'final';
let temporizadorEventos = null;

// atajo para agarrar un elemento por su id
const $ = (id) => document.getElementById(id);

// avisito que sale abajo y se quita solo a los 4 segundos
function mostrarAviso(mensaje, tipo = 'info') {
    const contenedor = $('contenedorAvisos');
    const aviso = document.createElement('div');
    aviso.className = `aviso ${tipo}`;
    aviso.textContent = mensaje;
    contenedor.appendChild(aviso);
    setTimeout(() => aviso.remove(), 4000);
}

// cambia el color del puntito de estado
function ponerPuntoEstado(estado) {
    $('puntoEstado').className = 'punto ' + estado;
}

function ponerTextoEstado(texto) {
    $('textoEstado').textContent = texto;
}

// pinta en el panel lo que manda el servidor (persona, cerradura, trepar...)
function actualizarEstado(datos) {
    const mapa = {
        // si hay varias personas se pone cuantas, ej. "SI (3)"
        estPersona: {
            valor: datos.hay_persona ? (datos.num_personas > 1 ? `SI (${datos.num_personas})` : 'SI') : 'NO',
            clase: datos.hay_persona ? (datos.num_personas > 1 ? 'precaucion' : 'ok') : 'neutral',
        },
        estCerradura: { valor: datos.manos_cerradura ? `${datos.tiempo_cerradura.toFixed(1)}s` : '---', clase: datos.manos_cerradura ? 'alerta' : 'neutral' },
        estTrepar: { valor: datos.alerta_trepar ? 'DETECTADO' : '---', clase: datos.alerta_trepar ? 'alerta' : 'neutral' },
        estEmpujon: { valor: datos.empujon_puerta ? 'DETECTADO' : '---', clase: datos.empujon_puerta ? 'alerta' : 'neutral' },
        estMirando: { valor: datos.mirando_alrededor ? 'DETECTADO' : '---', clase: datos.mirando_alrededor ? 'precaucion' : 'neutral' },
        estMovimiento: { valor: datos.hay_movimiento ? 'SI' : 'NO', clase: datos.hay_movimiento ? 'info' : 'neutral' },
        estTiempo: { valor: `${datos.tiempo_transcurrido.toFixed(1)}s`, clase: 'info' },
    };

    for (const [id, info] of Object.entries(mapa)) {
        const elemento = $(id);
        elemento.textContent = info.valor;
        elemento.className = 'valor-estado ' + info.clase;
    }

    if (modoApp === 'pruebas' && datos.metricas) actualizarMetricas(datos);

    $('etiquetaCamara').textContent = `Cámara: ${camaraActiva ? 'ON' : 'OFF'}`;
    $('insigniaModo').textContent = `Modo: ${datos.modo}`;
    if (typeof datos.fps === 'number' && isFinite(datos.fps)) {
        $('etiquetaFps').textContent = `FPS: ${datos.fps.toFixed(1)}`;
    }
}

// --- App de pruebas ---------------------------------------------------------

// pone el valor de una metrica y la pinta verde si ya pasa el umbral
function ponerMetrica(id, texto, cumple) {
    const fila = $(id);
    fila.querySelector('b').textContent = texto;
    fila.className = 'metrica-fila ' + (cumple === null ? '' : (cumple ? 'cumple' : 'no-cumple'));
}

// muestra los numeros de las reglas de trepar y mirar contra sus umbrales
function actualizarMetricas(datos) {
    const m = datos.metricas || {};
    const u = datos.umbrales || {};
    const px = (v, umbral) => (typeof v === 'number' ? `${v} / ${umbral} px` : '—');
    const pasa = (v, umbral) => (typeof v === 'number' ? v >= umbral : null);
    ponerMetrica('mtCadera', px(m.sube_cadera, u.sube_cadera), pasa(m.sube_cadera, u.sube_cadera));
    ponerMetrica('mtPies', px(m.sube_pies, u.sube_pies), pasa(m.sube_pies, u.sube_pies));
    ponerMetrica('mtManos', px(m.manos_max, u.manos_max), pasa(m.manos_max, u.manos_max));
    ponerMetrica('mtPuerta', m.cerca_puerta === undefined ? '—' : (m.cerca_puerta ? 'SÍ' : 'NO'),
        m.cerca_puerta === undefined ? null : m.cerca_puerta);
    ponerMetrica('mtConfirmaciones', `${m.confirmaciones ?? 0} / ${u.confirmaciones ?? '—'}`,
        (m.confirmaciones ?? 0) >= (u.confirmaciones ?? Infinity));
    const mm = datos.metricas_mirar || {};
    const hay = mm.cerca_puerta !== undefined;
    ponerMetrica('mmPuerta', hay ? (mm.cerca_puerta ? 'SÍ' : 'NO') : '—', hay ? mm.cerca_puerta : null);
    const conDatos = typeof mm.permanencia_s === 'number';
    ponerMetrica('mmPermanencia', conDatos ? `${mm.permanencia_s} / ${u.permanencia_s} s` : '—',
        conDatos ? mm.permanencia_s >= u.permanencia_s : null);
    ponerMetrica('mmQuieto', conDatos ? `${mm.movimiento} / ${u.movimiento} torsos` : '—',
        conDatos ? mm.movimiento <= u.movimiento : null);
    ponerMetrica('mmGiros', conDatos ? `${mm.giros} / ${u.giros}` : '—',
        conDatos ? mm.giros >= u.giros : null);
    $('metricasTiempo').textContent = typeof datos.segundo_video === 'number' ? `t=${datos.segundo_video.toFixed(1)}s` : '';
}

// trae los ultimos eventos del CSV y los pone en la lista
async function cargarEventosPruebas() {
    try {
        const r = await llamarServidor('/pruebas/eventos');
        $('registroArchivo').textContent = r.archivo;
        const lista = $('eventosPruebas');
        lista.innerHTML = '';
        for (const ev of r.eventos) {
            const li = document.createElement('li');
            li.className = 'elemento-alerta';
            const t = ev.segundo_video ? ` (t=${ev.segundo_video}s)` : '';
            li.textContent = `${ev.fecha_hora.slice(11)} ${ev.fuente}${t}: ${ev.evento}`;
            li.title = ev.metricas;
            lista.appendChild(li);
        }
    } catch (e) {}
}

// le pregunta a Tauri si es la app de pruebas y si si, prende lo de pruebas
async function prepararModoPruebas() {
    const tauri = window.__TAURI__;
    const invocar = tauri && ((tauri.core && tauri.core.invoke) || tauri.invoke);
    if (invocar) {
        try { modoApp = await invocar('modo_app'); } catch (e) {}
    }
    if (modoApp === 'pruebas') {
        document.body.classList.add('pruebas');
        document.title = 'SafeHome PRUEBAS - Videovigilancia';
        cargarEventosPruebas();
        temporizadorEventos = setInterval(cargarEventosPruebas, 3000);
    }
    // si quedo corriendo el servidor de la otra app se avisa que hay que cerrarlo
    if (await esperarServidor()) {
        try {
            const r = await llamarServidor('/');
            if (r.modo && r.modo !== modoApp) {
                const otra = r.modo === 'pruebas' ? 'SafeHome Pruebas' : 'SafeHome';
                mostrarAviso(`Está activo el servicio de ${otra}. Ciérralo (cerrar → todo el servicio) y vuelve a abrir esta app.`, 'error');
            }
        } catch (e) {}
    }
}

function ponerCamaraActiva(activa) {
    if (camaraActiva === activa) return;
    camaraActiva = activa;
    $('etiquetaCamara').textContent = `Cámara: ${activa ? 'ON' : 'OFF'}`;
}

// se conecta al websocket para recibir el estado en vivo (si se cae, reintenta)
function conectarWS() {
    if (ws && ws.readyState === WebSocket.OPEN) return;
    ws = new WebSocket(URL_WS);

    ws.onopen = () => {
        ponerPuntoEstado('conectado');
        ponerTextoEstado('Conectado - Esperando inicio');
        mostrarAviso('Conectado al servidor', 'exito');
    };

    ws.onmessage = (evento) => {
        try {
            const datos = JSON.parse(evento.data);
            if (datos.tipo === 'respuesta_latido') return;
            actualizarEstado(datos);
        } catch (e) {}
    };

    ws.onclose = () => {
        ponerPuntoEstado('desconectado');
        ponerTextoEstado('Desconectado');
        if (temporizadorReconexion) clearTimeout(temporizadorReconexion);
        temporizadorReconexion = setTimeout(conectarWS, 3000);
    };

    ws.onerror = () => {};
}

// hace una peticion al servidor y regresa el JSON
async function llamarServidor(ruta, opciones = {}) {
    const res = await fetch(`${SERVIDOR}${ruta}`, {
        headers: { 'Content-Type': 'application/json' },
        ...opciones,
    });
    if (!res.ok) {
        // si el servidor explica el error (detail) se muestra ese texto
        let detalle = `HTTP ${res.status}`;
        try { const d = await res.json(); if (d && typeof d.detail === 'string') detalle = d.detail; } catch (e) {}
        throw new Error(detalle);
    }
    return res.json();
}

// espera a que el servidor conteste (al abrir la app tarda unos segundos)
async function esperarServidor(intentos = 12, esperaMs = 800) {
    for (let i = 0; i < intentos; i++) {
        try {
            const r = await fetch(`${SERVIDOR}/`, { method: 'GET' });
            if (r.ok) return true;
        } catch (e) {}
        await new Promise(r => setTimeout(r, esperaMs));
    }
    return false;
}

// vuelve a escoger en la lista la fuente que estaba antes
function restaurarSeleccion(lista, anterior) {
    if (!anterior) return;
    if ([...lista.options].some(o => o.value === anterior)) lista.value = anterior;
    else if (anterior === '__ruta__') lista.value = '__ruta__';
    else if (typeof anterior === 'string' && anterior.length > 1) lista.value = '__ruta__';
    else if (!isNaN(parseInt(anterior, 10)) && [...lista.options].some(o => o.value === String(parseInt(anterior, 10)))) {
        lista.value = String(parseInt(anterior, 10));
    }
}

// grupo de opciones con las camaras que encontro el servidor
function grupoCamaras(dispositivos) {
    const grupo = document.createElement('optgroup');
    grupo.label = 'Cámaras detectadas';
    dispositivos.forEach(d => {
        const opcion = document.createElement('option');
        opcion.value = String(d.indice);
        const marca = d.disponible ? '✓' : '·';
        opcion.textContent = `${marca} Cámara ${d.indice} — ${d.nombre || d.ruta} (${d.ruta})`;
        grupo.appendChild(opcion);
    });
    return grupo;
}

// grupo con la opcion de escribir la ruta de un video a mano
function grupoRutaVideo() {
    const grupo = document.createElement('optgroup');
    grupo.label = 'Archivo de vídeo';
    const opcion = document.createElement('option');
    opcion.value = '__ruta__';
    opcion.textContent = '✎ Usar archivo de vídeo (escribe ruta/archivo abajo)';
    grupo.appendChild(opcion);
    return grupo;
}

// pide al servidor las camaras conectadas y llena la lista
async function cargarCamaras() {
    const lista = $('cfgFuenteCamara');
    const estado = $('estadoEscaneo');
    // se guarda lo que estaba escogido antes de rehacer la lista
    const anterior = lista.dataset.actual ?? '';
    try {
        if (estado) estado.textContent = 'Escaneando dispositivos...';
        const datos = await llamarServidor('/camaras');
        datosCamarasCache = datos;
        lista.innerHTML = '';

        const dispositivos = datos.dispositivos || [];
        if (!dispositivos.length) {
            const opcion = document.createElement('option');
            opcion.value = '';
            opcion.textContent = '⚠ No se encontraron dispositivos /dev/video*';
            const grupo = document.createElement('optgroup');
            grupo.appendChild(opcion);
            lista.appendChild(grupo);
        } else {
            lista.appendChild(grupoCamaras(dispositivos));
        }
        lista.appendChild(grupoRutaVideo());

        restaurarSeleccion(lista, anterior);
        if (estado) {
            const probadas = dispositivos.filter(d => d.disponible).length;
            estado.textContent = `Servidor OK · ${dispositivos.length} cámara(s) detectada(s) (${probadas} probada(s)✓)`;
        }
        return datos;
    } catch (e) {
        console.warn('No se pudieron listar cámaras:', e.message);
        if (estado) estado.textContent = `Error consultando el servidor (/camaras): ${e.message}`;
        mostrarAviso('No se pudieron detectar cámaras: ' + e.message, 'error');
        return null;
    }
}

// igual que cargarCamaras pero tambien mete los videos de la carpeta del servidor
async function cargarVideos() {
    const lista = $('cfgFuenteCamara');
    try {
        const datos = datosCamarasCache && datosCamarasCache.videos
            ? datosCamarasCache
            : await llamarServidor('/camaras');
        datosCamarasCache = datos;
        lista.innerHTML = '';

        const dispositivos = datos.dispositivos || [];
        if (dispositivos.length) lista.appendChild(grupoCamaras(dispositivos));

        const grupoVideos = document.createElement('optgroup');
        grupoVideos.label = 'Vídeos del servidor';
        (datos.videos || []).forEach(v => {
            const opcion = document.createElement('option');
            opcion.value = v.ruta;
            opcion.textContent = `🎞 ${v.nombre}`;
            grupoVideos.appendChild(opcion);
        });
        if (grupoVideos.children.length) lista.appendChild(grupoVideos);

        lista.appendChild(grupoRutaVideo());

        restaurarSeleccion(lista, lista.dataset.actual ?? '');
        const estado = $('estadoEscaneo');
        if (estado) estado.textContent = `Servidor OK · ${(datos.videos || []).length} vídeo(s) disponibles en el servidor`;
        return datos;
    } catch (e) {
        console.warn('No se pudieron listar vídeos:', e.message);
        mostrarAviso('No se pudieron listar vídeos: ' + e.message, 'error');
        return null;
    }
}

// trae la config y las zonas del servidor
async function cargarConfiguracion() {
    try {
        const cfg = await llamarServidor('/configuracion');
        llenarFormulario(cfg);
        zonasActuales = await llamarServidor('/zonas');
        return cfg;
    } catch (e) {
        mostrarAviso('Error cargando la configuración: ' + e.message, 'error');
        return {};
    }
}

// pone los valores de la config en el formulario
function llenarFormulario(cfg) {
    $('cfgTiempoCerradura').value = cfg.UMBRAL_CERRADURA_S ?? 4.5;
    $('cfgVentanaMirar').value = cfg.VENTANA_MIRAR_S ?? 5;
    $('cfgGirosMirar').value = cfg.GIROS_MIRAR ?? 3;
    $('cfgTiempoEmpujon').value = cfg.UMBRAL_EMPUJON_S ?? 5;
    $('cfgTiempoDeteccion').value = cfg.TIEMPO_DETECCION_S ?? 3;
    $('cfgSaltoVideo').value = cfg.SALTO_CUADROS_VIDEO ?? 3;
    $('cfgSaltoPose').value = cfg.SALTO_CUADROS_POSE ?? 5;
    $('cfgModo').value = cfg.MODO_DETECCION ?? 'cascada';
    $('cfgGuardarImagenes').checked = cfg.GUARDAR_IMAGENES !== false;
    $('cfgEsperaAlertas').value = cfg.ESPERA_ENTRE_ALERTAS_S ?? 5;
    $('cfgCorreoUsuario').value = cfg.CORREO_USUARIO ?? '';
    $('cfgCorreoContrasena').value = cfg.CORREO_CONTRASENA ?? '';
    $('cfgCorreoDestino').value = cfg.CORREO_DESTINATARIO ?? '';
    $('cfgCorreoServidor').value = cfg.CORREO_SERVIDOR_SMTP ?? 'smtp.gmail.com';
    $('cfgCorreoPuerto').value = cfg.CORREO_PUERTO_SMTP ?? 465;
    const fuente = cfg.FUENTE_CAMARA ?? 0;
    $('cfgRutaVideo').value = (typeof fuente === 'string' && fuente !== '') ? fuente : '';
    $('cfgFuenteCamara').dataset.actual = String(fuente);
    $('cfgAncho').value = cfg.ANCHO_CUADRO ?? 640;
    $('cfgAlto').value = cfg.ALTO_CUADRO ?? 480;
}

// lee el formulario y arma la config para mandarla al servidor
function leerFormulario() {
    const numero = (el, defecto) => { if (!el) return defecto; const v = parseFloat(el.value); return isNaN(v) ? defecto : v; };
    const texto = (el, defecto = '') => { if (!el) return defecto; return String(el.value); };
    const campoRuta = $('cfgRutaVideo');
    let fuente = $('cfgFuenteCamara') ? $('cfgFuenteCamara').value : '';
    if (fuente === '__ruta__') {
        fuente = (campoRuta && String(campoRuta.value).trim()) || 0;
    } else if (fuente !== '' && !isNaN(parseInt(fuente, 10))) {
        fuente = parseInt(fuente, 10);
    }
    return {
        UMBRAL_CERRADURA_S: numero($('cfgTiempoCerradura'), 4.5),
        VENTANA_MIRAR_S: numero($('cfgVentanaMirar'), 5),
        GIROS_MIRAR: numero($('cfgGirosMirar'), 3),
        UMBRAL_EMPUJON_S: numero($('cfgTiempoEmpujon'), 5),
        TIEMPO_DETECCION_S: numero($('cfgTiempoDeteccion'), 3),
        SALTO_CUADROS_VIDEO: numero($('cfgSaltoVideo'), 3),
        SALTO_CUADROS_POSE: numero($('cfgSaltoPose'), 5),
        MODO_DETECCION: texto($('cfgModo'), 'cascada'),
        GUARDAR_IMAGENES: !!($('cfgGuardarImagenes') && $('cfgGuardarImagenes').checked),
        ESPERA_ENTRE_ALERTAS_S: numero($('cfgEsperaAlertas'), 5),
        CORREO_USUARIO: texto($('cfgCorreoUsuario')),
        CORREO_CONTRASENA: texto($('cfgCorreoContrasena')),
        CORREO_DESTINATARIO: texto($('cfgCorreoDestino')),
        CORREO_SERVIDOR_SMTP: texto($('cfgCorreoServidor'), 'smtp.gmail.com'),
        CORREO_PUERTO_SMTP: numero($('cfgCorreoPuerto'), 465),
        FUENTE_CAMARA: fuente,
        ANCHO_CUADRO: numero($('cfgAncho'), 640),
        ALTO_CUADRO: numero($('cfgAlto'), 480),
    };
}

function abrirVentana(id) {
    $(id).classList.add('activo');
}

function cerrarVentana(id) {
    $(id).classList.remove('activo');
}

// cambia de pestana en la configuracion
function prepararPestanas() {
    document.querySelectorAll('.boton-pestana').forEach(boton => {
        boton.onclick = () => {
            document.querySelectorAll('.boton-pestana').forEach(b => b.classList.remove('activo'));
            document.querySelectorAll('.contenido-pestana').forEach(c => c.classList.remove('activo'));
            boton.classList.add('activo');
            $(boton.dataset.pestana).classList.add('activo');
        };
    });
}

// cuando le pican a la X de la ventana pregunta si cerrar solo la ventana o todo
function prepararDialogoCerrar() {
    const tauri = window.__TAURI__;
    if (!tauri) return;

    const invocar = (tauri.core && tauri.core.invoke) || tauri.invoke;
    const escuchar = (tauri.event && tauri.event.listen) || tauri.listen;

    if (escuchar) {
        escuchar('cierre-solicitado', () => abrirVentana('modalCerrar'));
    }

    $('btnCancelarCierre').onclick = () => cerrarVentana('modalCerrar');
    $('cerrarDialogoCerrar').onclick = () => cerrarVentana('modalCerrar');

    $('btnSoloVentana').onclick = () => {
        cerrarVentana('modalCerrar');
        if (invocar) invocar('ocultar_ventana');
    };

    $('btnTodoServicio').onclick = async () => {
        cerrarVentana('modalCerrar');
        if (invocar) await invocar('cerrar_aplicacion');
    };
}

// todo lo del editor de zonas: tamano del lienzo, dibujar con el mouse y botones
function prepararLienzoZonas() {
    const lienzo = $('lienzoZonas');
    const ctx = lienzo.getContext('2d');
    const marco = $('marcoLienzoZonas');
    const agarradera = $('agarraderaLienzo');
    const etiquetaTamano = $('etiquetaTamanoZona');
    const CLAVE_TAMANO = 'safehome_tamano_lienzo_zonas';
    let tamanoGuardado = parseInt(localStorage.getItem(CLAVE_TAMANO) || '0', 10);

    // recuerda el tamano del lienzo para la proxima vez
    function guardarTamano() {
        tamanoGuardado = lienzo.width;
        localStorage.setItem(CLAVE_TAMANO, String(tamanoGuardado));
    }

    // cambia el tamano del lienzo manteniendo la proporcion 640x480
    function aplicarTamano(ancho) {
        ancho = Math.max(240, Math.round(ancho));
        marco.style.width = ancho + 'px';
        lienzo.width = ancho;
        lienzo.height = Math.round(ancho * 480 / 640);
        if (etiquetaTamano) etiquetaTamano.textContent = `${lienzo.width} x ${lienzo.height} px`;
        dibujarZonas();
        guardarTamano();
    }

    // ajusta el lienzo al espacio que hay en la ventana sin que salga scroll
    function ajustarTamano() {
        const cuerpoVentana = lienzo.closest('.cuerpo-modal');
        const rect = cuerpoVentana.getBoundingClientRect();
        const anchoDisponible = Math.max(240, Math.floor(rect.width));
        // espacio vertical que mas o menos queda para el lienzo
        const altoDisponible = Math.max(160, Math.floor(window.innerHeight * 0.55));
        const maximoPorAlto = Math.floor(altoDisponible * 640 / 480);
        const anchoQueCabe = Math.min(anchoDisponible, maximoPorAlto);

        let ancho = anchoQueCabe;
        if (tamanoGuardado >= 240 && tamanoGuardado <= anchoDisponible && tamanoGuardado <= maximoPorAlto) {
            ancho = tamanoGuardado;
        }
        aplicarTamano(ancho);
    }

    // hacer el lienzo mas grande o chico arrastrando la esquinita
    let arrastre = null;
    agarradera.addEventListener('mousedown', (e) => {
        e.preventDefault();
        arrastre = { xInicial: e.clientX, anchoInicial: marco.offsetWidth };
        document.body.style.cursor = 'se-resize';

        const alMover = (ev) => {
            if (!arrastre) return;
            aplicarTamano(arrastre.anchoInicial + (ev.clientX - arrastre.xInicial));
        };
        const alSoltar = () => {
            arrastre = null;
            document.body.style.cursor = '';
            window.removeEventListener('mousemove', alMover);
            window.removeEventListener('mouseup', alSoltar);
        };
        window.addEventListener('mousemove', alMover);
        window.addEventListener('mouseup', alSoltar);
    });

    // se dejan a la mano para que cargarFondoZonas ajuste y redibuje el lienzo
    window.ajustarLienzoZonas = ajustarTamano;
    window.redibujarZonas = () => dibujarZonas();

    // texto con orilla negra para que se lea sobre cualquier fondo
    function escribirTexto(texto, x, y, color) {
        ctx.font = 'bold 13px monospace';
        ctx.textBaseline = 'top';
        ctx.strokeStyle = '#000000';
        ctx.lineWidth = 3;
        ctx.strokeText(texto, x, y);
        ctx.fillStyle = color;
        ctx.fillText(texto, x, y);
    }

    // dibuja el fondo (foto de la camara) y encima todas las zonas
    function dibujarZonas() {
        if (fondoLienzo) {
            ctx.drawImage(fondoLienzo, 0, 0, lienzo.width, lienzo.height);
        } else {
            ctx.fillStyle = '#1a1a1a';
            ctx.fillRect(0, 0, lienzo.width, lienzo.height);
            // letrero grande para que se vea que no hay camara
            ctx.fillStyle = '#ffffff';
            ctx.font = 'bold 24px sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText('Esperando camara...', lienzo.width / 2, lienzo.height / 2 - 20);
            ctx.font = 'bold 14px sans-serif';
            ctx.fillStyle = '#00adb5';
            ctx.fillText('Inicia vigilancia o conecta camara', lienzo.width / 2, lienzo.height / 2 + 15);
            ctx.font = '12px sans-serif';
            ctx.fillStyle = '#aaaaaa';
            ctx.fillText('Arrastra para dibujar zonas', lienzo.width / 2, lienzo.height - 15);
            ctx.textAlign = 'start';
            ctx.textBaseline = 'top';
        }

        // las zonas estan en coordenadas de 640x480, se escalan al lienzo
        const escalaX = lienzo.width / 640;
        const escalaY = lienzo.height / 480;

        const colores = {
            ROI: '#00ff00',
            CERRADURA: '#ff0000',
            PUERTA: '#ffa500',
        };

        // dibuja un rectangulo con relleno transparente y su nombre
        function dibujarRectangulo(z, color, nombre) {
            if (!z) return;
            ctx.strokeStyle = color;
            ctx.lineWidth = 3;
            const r = parseInt(color.slice(1, 3), 16);
            const g = parseInt(color.slice(3, 5), 16);
            const b = parseInt(color.slice(5, 7), 16);
            ctx.fillStyle = `rgba(${r},${g},${b},0.2)`;

            if (z.ancho !== undefined) {
                // la ROI viene como x, y, ancho, alto
                const x = z.x * escalaX, y = z.y * escalaY, w = z.ancho * escalaX, h = z.alto * escalaY;
                ctx.fillRect(x, y, w, h);
                ctx.strokeRect(x, y, w, h);
                escribirTexto(nombre, x + 4, y + 4, color);
            } else if (z.x2 !== undefined) {
                // cerraduras y puertas vienen como esquinas x1, y1, x2, y2
                const x = z.x1 * escalaX, y = z.y1 * escalaY, w = (z.x2 - z.x1) * escalaX, h = (z.y2 - z.y1) * escalaY;
                ctx.fillRect(x, y, w, h);
                ctx.strokeRect(x, y, w, h);
                escribirTexto(nombre, x + 4, y + 4, color);
            }
        }

        if (zonasVisibles) {
            if (zonasActuales.ROI && zonasActuales.ROI.ancho > 0) dibujarRectangulo(zonasActuales.ROI, colores.ROI, 'ROI');
            (zonasActuales.CERRADURAS || []).forEach((z, i) => dibujarRectangulo(z, colores.CERRADURA, `Cerradura ${i + 1}`));
            (zonasActuales.PUERTAS || []).forEach((z, i) => dibujarRectangulo(z, colores.PUERTA, `Puerta ${i + 1}`));
        }

        // el rectangulo punteado mientras se esta arrastrando el mouse
        if (dibujandoZona && inicioZona && finZona) {
            ctx.strokeStyle = '#ffffff';
            ctx.lineWidth = 2;
            ctx.setLineDash([6, 4]);
            const xs = Math.min(inicioZona.x, finZona.x) * escalaX;
            const ys = Math.min(inicioZona.y, finZona.y) * escalaY;
            const ws = Math.abs(finZona.x - inicioZona.x) * escalaX;
            const hs = Math.abs(finZona.y - inicioZona.y) * escalaY;
            ctx.fillStyle = 'rgba(255,255,255,0.1)';
            ctx.fillRect(xs, ys, ws, hs);
            ctx.strokeRect(xs, ys, ws, hs);
            ctx.setLineDash([]);

            ctx.fillStyle = '#ffffff';
            ctx.font = '12px monospace';
            ctx.fillText(`${Math.round(Math.abs(finZona.x - inicioZona.x))}x${Math.round(Math.abs(finZona.y - inicioZona.y))}`, xs + 4, ys + 16);
        }
    }

    // pasa la posicion del mouse a coordenadas de 640x480
    function coordenadasMouse(e) {
        const rect = lienzo.getBoundingClientRect();
        return {
            x: Math.max(0, Math.min(639, Math.round((e.clientX - rect.left) / rect.width * 640))),
            y: Math.max(0, Math.min(479, Math.round((e.clientY - rect.top) / rect.height * 480))),
        };
    }

    lienzo.onmousedown = (e) => {
        dibujandoZona = true;
        inicioZona = coordenadasMouse(e);
        finZona = { ...inicioZona };
        dibujarZonas();
    };

    lienzo.onmousemove = (e) => {
        if (dibujandoZona) {
            finZona = coordenadasMouse(e);
            dibujarZonas();
        }
    };

    // al soltar el mouse se guarda la zona del tipo que este escogido
    lienzo.onmouseup = () => {
        if (dibujandoZona && inicioZona && finZona &&
            Math.abs(finZona.x - inicioZona.x) > 10 &&
            Math.abs(finZona.y - inicioZona.y) > 10) {
            const x1 = Math.min(inicioZona.x, finZona.x);
            const y1 = Math.min(inicioZona.y, finZona.y);
            const x2 = Math.max(inicioZona.x, finZona.x);
            const y2 = Math.max(inicioZona.y, finZona.y);

            const tipo = $('tipoZona').value;
            if (tipo === 'ROI') {
                zonasActuales.ROI = { x: x1, y: y1, ancho: x2 - x1, alto: y2 - y1 };
                mostrarAviso(`ROI marcada: ${x2 - x1}x${y2 - y1}`, 'exito');
            } else if (tipo === 'CERRADURA') {
                zonasActuales.CERRADURAS = zonasActuales.CERRADURAS || [];
                zonasActuales.CERRADURAS.push({ x1, y1, x2, y2 });
                mostrarAviso(`Cerradura ${zonasActuales.CERRADURAS.length} añadida`, 'exito');
            } else if (tipo === 'PUERTA') {
                zonasActuales.PUERTAS = zonasActuales.PUERTAS || [];
                zonasActuales.PUERTAS.push({ x1, y1, x2, y2 });
                mostrarAviso(`Puerta ${zonasActuales.PUERTAS.length} añadida`, 'exito');
            }
        } else if (dibujandoZona) {
            mostrarAviso('Arrastra al menos 10px para crear zona', 'advertencia');
        }
        dibujandoZona = false;
        inicioZona = null;
        finZona = null;
        dibujarZonas();
    };

    window.addEventListener('resize', ajustarTamano);
    ajustarTamano();

    $('btnAgregarZona').onclick = () => mostrarAviso('Dibuja un rectángulo en el lienzo arrastrando el mouse', 'info');

    // borra la ultima zona del tipo escogido
    $('btnBorrarZona').onclick = () => {
        const tipo = $('tipoZona').value;
        if (tipo === 'CERRADURA' && zonasActuales.CERRADURAS?.length) {
            zonasActuales.CERRADURAS.pop();
            mostrarAviso('Última cerradura eliminada', 'info');
        } else if (tipo === 'PUERTA' && zonasActuales.PUERTAS?.length) {
            zonasActuales.PUERTAS.pop();
            mostrarAviso('Última puerta eliminada', 'info');
        } else if (tipo === 'ROI') {
            zonasActuales.ROI = { x: 0, y: 0, ancho: 0, alto: 0 };
            mostrarAviso('ROI eliminada', 'info');
        }
        dibujarZonas();
    };

    // oculta o enseña las zonas en el lienzo
    $('btnVerZonas').onclick = () => {
        zonasVisibles = !zonasVisibles;
        $('btnVerZonas').textContent = zonasVisibles ? 'Ocultar zonas' : 'Mostrar zonas';
        dibujarZonas();
        mostrarAviso(zonasVisibles ? 'Zonas visibles' : 'Zonas ocultas', 'info');
    };

    // borra todas las zonas (pregunta antes)
    $('btnLimpiarZonas').onclick = () => {
        if (confirm('¿Limpiar todas las zonas?')) {
            zonasActuales = {
                ROI: { x: 0, y: 0, ancho: 0, alto: 0 },
                CERRADURAS: [],
                PUERTAS: [],
            };
            dibujarZonas();
            mostrarAviso('Todas las zonas eliminadas', 'info');
        }
    };
}

// pone de fondo del lienzo una foto actual de la camara
async function cargarFondoZonas() {
    fondoLienzo = null;

    // primero se ajusta el lienzo al tamano de la ventana
    if (window.ajustarLienzoZonas) window.ajustarLienzoZonas();

    const lienzo = $('lienzoZonas');
    const ctx = lienzo.getContext('2d');

    ctx.fillStyle = '#1a1a1a';
    ctx.fillRect(0, 0, lienzo.width, lienzo.height);

    try {
        const res = await fetch(`${SERVIDOR}/cuadro?_=${Date.now()}`);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const blob = await res.blob();
        if (blob.size < 1000) throw new Error('Imagen muy pequeña');
        const url = URL.createObjectURL(blob);
        const img = new Image();
        await new Promise((resolver, rechazar) => {
            img.onload = () => resolver();
            img.onerror = () => rechazar(new Error('Error en la imagen'));
            img.src = url;
        });
        fondoLienzo = img;
        mostrarAviso('Fondo de cámara cargado', 'exito');
    } catch (e) {
        console.warn('Sin cámara para el fondo:', e.message);
        mostrarAviso('Sin cámara - dibuja zonas sobre fondo oscuro', 'info');
    }

    // se vuelven a dibujar las zonas ya con el fondo nuevo
    if (window.redibujarZonas) window.redibujarZonas();
    return true;
}

// manda iniciar/detener al servidor
async function mandarAccion(accion) {
    return llamarServidor('/control', { method: 'POST', body: JSON.stringify({ accion }) });
}

// conecta todos los botones de la pantalla principal y de las ventanas
function prepararControles() {
    $('btnIniciar').onclick = async () => {
        try {
            await mandarAccion('iniciar');
            $('btnIniciar').disabled = true;
            $('btnDetener').disabled = false;
            ponerPuntoEstado('vigilando');
            ponerTextoEstado('Vigilando...');
            mostrarAviso('Vigilancia iniciada', 'exito');
            empezarVideo();
        } catch (e) { mostrarAviso('Error al iniciar: ' + e.message, 'error'); }
    };

    $('btnDetener').onclick = async () => {
        try {
            await mandarAccion('detener');
            $('btnIniciar').disabled = false;
            $('btnDetener').disabled = true;
            ponerPuntoEstado('conectado');
            ponerTextoEstado('Detenido');
            mostrarAviso('Vigilancia detenida', 'info');
        } catch (e) { mostrarAviso('Error al detener: ' + e.message, 'error'); }
    };

    $('btnRefrescarZonas').onclick = () => {
        abrirVentana('modalZonas');
        cargarFondoZonas();
        mostrarAviso('Refrescando fondo de zonas', 'info');
    };

    // antes de abrir el editor se piden las zonas al servidor: si se cambio
    // de video, las que estaban en memoria eran las del video anterior y al
    // guardar se le pegaban al video nuevo
    $('btnZonas').onclick = async () => {
        try {
            zonasActuales = await llamarServidor('/zonas');
        } catch (e) {
            mostrarAviso('No se pudieron leer las zonas: ' + e.message, 'error');
            return;
        }
        abrirVentana('modalZonas');
        cargarFondoZonas();
    };

    $('btnConfiguracion').onclick = async () => {
        if (!await esperarServidor()) {
            mostrarAviso('El servidor aún no está disponible, reintenta en un momento', 'advertencia');
            return;
        }
        await cargarConfiguracion();
        await cargarCamaras();
        if (modoApp !== 'pruebas') await cargarCorreo();
        abrirVentana('modalConfiguracion');
    };

    $('cerrarZonas').onclick = () => cerrarVentana('modalZonas');
    $('cancelarZonas').onclick = () => {
        zonasActuales = {};
        cerrarVentana('modalZonas');
    };
    $('cerrarConfiguracion').onclick = () => cerrarVentana('modalConfiguracion');
    $('cancelarConfiguracion').onclick = () => cerrarVentana('modalConfiguracion');
    prepararCorreo();

    $('guardarZonas').onclick = async () => {
        try {
            await llamarServidor('/zonas', { method: 'POST', body: JSON.stringify({ zonas: zonasActuales }) });
            mostrarAviso('Zonas guardadas', 'exito');
            cerrarVentana('modalZonas');
        } catch (e) { mostrarAviso('Error guardando zonas: ' + e.message, 'error'); }
    };

    $('guardarConfiguracion').onclick = async () => {
        try {
            const cfg = leerFormulario();
            await llamarServidor('/configuracion', { method: 'POST', body: JSON.stringify({ configuracion: cfg }) });
            mostrarAviso('Configuración guardada', 'exito');
            cerrarVentana('modalConfiguracion');
            if (cfg.FUENTE_CAMARA !== String(datosCamarasCache ? datosCamarasCache.fuente_actual ?? '' : '')) {
                mostrarAviso('Fuente cambiada - reinicia vigilancia para aplicarla', 'advertencia');
            }
        } catch (e) {
            console.error('Error guardando la configuración:', e);
            mostrarAviso('Error guardando la configuración [' + (e.name || 'Error') + ']: ' + e.message, 'error');
        }
    };

    // borra el historial de alertas (pregunta antes)
    $('btnLimpiarRegistro').onclick = async () => {
        if (!confirm('¿Borrar todas las alertas guardadas y sus fotos?')) return;
        try {
            await llamarServidor('/alertas/borrar', { method: 'POST' });
            await cargarAlertas();
            mostrarAviso('Registro de alertas borrado', 'info');
        } catch (e) { mostrarAviso('Error: ' + e.message, 'error'); }
    };
    $('cerrarAlerta').onclick = () => cerrarVentana('modalAlerta');
    $('btnRedetectarCamaras').onclick = async () => {
        if (!await esperarServidor()) {
            mostrarAviso('El servidor no responde, reintenta en un momento', 'advertencia');
            return;
        }
        await cargarCamaras();
        mostrarAviso('Cámaras re-detectadas', 'exito');
    };
    // guarda la fuente escogida y checa que de imagen
    $('btnProbarFuente').onclick = async () => {
        if (!await esperarServidor()) {
            mostrarAviso('El servidor no responde, reintenta en un momento', 'advertencia');
            return;
        }
        const cfg = leerFormulario();
        try {
            await llamarServidor('/configuracion', { method: 'POST', body: JSON.stringify({ configuracion: cfg }) });
            await new Promise(r => setTimeout(r, 500));
            const r = await fetch(`${SERVIDOR}/cuadro`);
            mostrarAviso(r.ok ? 'Fuente de vídeo probada correctamente' : 'La fuente no entrega imagen', r.ok ? 'exito' : 'error');
        } catch (e) {
            mostrarAviso('Error probando la fuente: ' + e.message, 'error');
        }
    };
    // si escogen "usar archivo" se pone el cursor en el campo de la ruta
    $('cfgFuenteCamara').onchange = () => {
        if ($('cfgFuenteCamara').value === '__ruta__') $('cfgRutaVideo').focus();
    };
    $('btnListarVideos').onclick = async () => {
        if (!await esperarServidor()) {
            mostrarAviso('El servidor no responde, reintenta en un momento', 'advertencia');
            return;
        }
        await cargarVideos();
        mostrarAviso('Vídeos del servidor listados', 'exito');
    };
}

// --- Correo de alertas (app final): se verifica con un codigo de 4 digitos ---
// vistas: 'guardado' (correo ya verificado), 'actual' (confirmar el actual
// antes de cambiarlo) y 'nuevo' (escribir y verificar el correo nuevo)
let correoInfo = { correo: '', verificado: false, remitente_listo: true };

function mensajeCorreo(texto, esError = false) {
    $('mensajeCorreo').textContent = texto;
    $('mensajeCorreo').className = 'nota-modal' + (esError ? ' error' : '');
}

function mostrarVistaCorreo(vista) {
    const hayVerificado = correoInfo.verificado;
    $('vistaCorreoGuardado').style.display = vista === 'guardado' ? '' : 'none';
    $('pasoCorreoActual').style.display = vista === 'actual' ? '' : 'none';
    $('pasoCorreoNuevo').style.display = vista === 'nuevo' ? '' : 'none';
    // cancelar solo tiene sentido si ya habia un correo verificado
    $('btnCancelarCorreo').style.display = vista !== 'guardado' && hayVerificado ? '' : 'none';
    // el cuadro del codigo sale hasta que se manda el codigo
    $('filaCodigoActual').style.display = 'none';
    $('filaCodigoNuevo').style.display = 'none';
    $('codigoActual').value = '';
    $('codigoNuevo').value = '';
    if (vista === 'actual') $('correoActualPaso').textContent = correoInfo.correo;
    if (vista === 'nuevo') $('correoNuevo').value = hayVerificado ? '' : correoInfo.correo;
}

async function cargarCorreo() {
    try {
        correoInfo = await llamarServidor('/correo');
    } catch (e) {
        mensajeCorreo('No se pudo leer el correo: ' + e.message, true);
        return;
    }
    $('textoCorreoGuardado').textContent = correoInfo.correo;
    $('insigniaCorreo').textContent = '✅ Verificado';
    $('insigniaCorreo').className = 'insignia-ok';
    mostrarVistaCorreo(correoInfo.verificado ? 'guardado' : 'nuevo');
    if (!correoInfo.remitente_listo) {
        mensajeCorreo('Esta instalación no tiene configurada la cuenta que envía los correos.', true);
    } else if (!correoInfo.verificado) {
        mensajeCorreo(correoInfo.correo ? 'Este correo aún no está verificado: pulsa Verificar.'
                                        : 'Escribe tu correo y pulsa Verificar para recibir las alertas.');
    } else {
        mensajeCorreo('');
    }
}

// pide al servidor que mande un codigo (deshabilita el boton mientras tanto)
async function pedirCodigo(boton, paso, correo, fila, campo) {
    boton.disabled = true;
    mensajeCorreo('Enviando código...');
    try {
        const r = await llamarServidor('/correo/codigo', { method: 'POST', body: JSON.stringify({ paso, correo }) });
        fila.style.display = '';
        campo.value = '';
        campo.focus();
        mensajeCorreo(`Te mandamos un código de 4 dígitos a ${r.correo}. Revisa también la carpeta de spam.`);
    } catch (e) {
        mensajeCorreo(e.message, true);
    } finally {
        boton.disabled = false;
    }
}

async function confirmarCodigo(paso, campo) {
    const codigo = campo.value.trim();
    if (!/^\d{4}$/.test(codigo)) { mensajeCorreo('El código tiene 4 números', true); return; }
    try {
        const r = await llamarServidor('/correo/confirmar', { method: 'POST', body: JSON.stringify({ paso, codigo }) });
        if (paso === 'actual') {
            mostrarVistaCorreo('nuevo');
            mensajeCorreo('Listo, eres tú. Ahora escribe el correo nuevo y pulsa Verificar.');
        } else {
            await cargarCorreo();
            mensajeCorreo(`Correo verificado: las alertas llegarán a ${r.correo}`);
            mostrarAviso('Correo verificado', 'exito');
        }
    } catch (e) {
        mensajeCorreo(e.message, true);
    }
}

function prepararCorreo() {
    $('btnCambiarCorreo').onclick = () => { mostrarVistaCorreo('actual'); mensajeCorreo(''); };
    $('btnCodigoActual').onclick = () =>
        pedirCodigo($('btnCodigoActual'), 'actual', null, $('filaCodigoActual'), $('codigoActual'));
    $('btnConfirmarActual').onclick = () => confirmarCodigo('actual', $('codigoActual'));
    $('btnVerificarNuevo').onclick = () =>
        pedirCodigo($('btnVerificarNuevo'), 'nuevo', $('correoNuevo').value.trim(), $('filaCodigoNuevo'), $('codigoNuevo'));
    $('btnConfirmarNuevo').onclick = () => confirmarCodigo('nuevo', $('codigoNuevo'));
    $('btnCancelarCorreo').onclick = async () => {
        try { await llamarServidor('/correo/cancelar', { method: 'POST' }); } catch (e) {}
        mostrarVistaCorreo('guardado');
        mensajeCorreo('');
    };
    // Enter en el cuadro del codigo = Confirmar
    $('codigoActual').onkeydown = (e) => { if (e.key === 'Enter') $('btnConfirmarActual').click(); };
    $('codigoNuevo').onkeydown = (e) => { if (e.key === 'Enter') $('btnConfirmarNuevo').click(); };
    $('correoNuevo').onkeydown = (e) => { if (e.key === 'Enter') $('btnVerificarNuevo').click(); };
}

// pide una imagen nueva al servidor cada 40 ms y la pone en pantalla
function empezarVideo() {
    if (pidiendoVideo) return;
    pidiendoVideo = true;
    const img = $('imagenVideo');
    const padre = img.parentElement;
    const pedirCuadro = async () => {
        if (!pidiendoVideo) return;
        try {
            const res = await fetch(`${SERVIDOR}/cuadro?_=${Date.now()}`);
            if (!res.ok) throw new Error('HTTP ' + res.status);
            ponerCamaraActiva(res.headers.get('X-Camara-Activa') !== '0');
            const blob = await res.blob();
            if (blob.size < 1000) throw new Error('Imagen muy pequeña');
            const urlVieja = img.src;
            const urlNueva = URL.createObjectURL(blob);
            img.src = urlNueva;
            // se libera la imagen anterior para no llenar la memoria
            if (urlVieja && urlVieja.startsWith('blob:')) URL.revokeObjectURL(urlVieja);
            img.style.display = 'block';
            const espera = padre.querySelector('.espera-video');
            if (espera) espera.style.display = 'none';
        } catch (e) {
            // no hay imagen: se ensena el letrero de "Conectando..."
            ponerCamaraActiva(false);
            img.style.display = 'none';
            let espera = padre.querySelector('.espera-video');
            if (!espera) {
                espera = document.createElement('div');
                espera.className = 'espera-video';
                espera.innerHTML = '<span style="font-size:48px">&#128247;</span><br><span style="font-size:14px;color:#666">Conectando a la cámara...</span>';
                espera.style.cssText = 'display:flex;flex-direction:column;align-items:center;justify-content:center;width:100%;height:100%;background:#111;';
                padre.appendChild(espera);
            }
            espera.style.display = 'flex';
        }
        if (pidiendoVideo) temporizadorVideo = setTimeout(pedirCuadro, 40);
    };
    pedirCuadro();
}

// --- Registro de alertas ---------------------------------------------------

// texto e icono segun como le fue al correo
const ESTADOS_CORREO = {
    enviado: { icono: '✅', texto: 'Enviada por correo', clase: 'ok' },
    enviando: { icono: '⏳', texto: 'Enviando correo...', clase: 'info' },
    error: { icono: '❌', texto: 'No se pudo enviar el correo', clase: 'alerta' },
    sin_configurar: { icono: '⚠️', texto: 'Correo no configurado', clase: 'precaucion' },
};
let alertasGuardadas = [];
let firmaAlertas = '';

// trae el historial del servidor y rehace la lista solo si cambio algo
async function cargarAlertas() {
    try {
        const r = await llamarServidor('/alertas');
        const firma = JSON.stringify(r.alertas);
        if (firma === firmaAlertas) return;
        firmaAlertas = firma;
        alertasGuardadas = r.alertas;
        pintarAlertas();
    } catch (e) {}
}

function pintarAlertas() {
    const lista = $('listaAlertas');
    lista.innerHTML = '';
    if (alertasGuardadas.length === 0) {
        lista.innerHTML = '<li class="sin-alertas">Todavía no hay alertas</li>';
        return;
    }
    for (const alerta of alertasGuardadas) {
        const correo = ESTADOS_CORREO[alerta.correo] || ESTADOS_CORREO.error;
        const li = document.createElement('li');
        li.className = 'elemento-alerta seleccionable';
        li.title = 'Clic para ver la alerta';

        const hora = document.createElement('span');
        hora.className = 'hora';
        hora.textContent = `${alerta.fecha} ${alerta.hora}`;
        const tipos = document.createElement('div');
        tipos.textContent = alerta.tipos.join(' · ');
        const estado = document.createElement('div');
        estado.className = 'estado-correo ' + correo.clase;
        estado.textContent = `${correo.icono} ${correo.texto}`;

        li.append(hora, tipos, estado);
        li.onclick = () => verAlerta(alerta);
        lista.appendChild(li);
    }
}

// abre la ventana con la foto y los datos de la alerta
function verAlerta(alerta) {
    const correo = ESTADOS_CORREO[alerta.correo] || ESTADOS_CORREO.error;
    $('tituloAlerta').textContent = `🚨 Alerta del ${alerta.fecha} a las ${alerta.hora}`;
    $('imagenAlerta').src = `${SERVIDOR}/alertas/${alerta.id}/imagen`;
    const tipos = $('tiposAlerta');
    tipos.innerHTML = '';
    for (const t of alerta.tipos) {
        const li = document.createElement('li');
        li.textContent = t;
        tipos.appendChild(li);
    }
    const detalle = alerta.detalle_correo ? ` (${alerta.detalle_correo})` : '';
    $('correoAlerta').textContent = `${correo.icono} ${correo.texto}${detalle}`;
    $('correoAlerta').className = 'correo-alerta ' + correo.clase;
    abrirVentana('modalAlerta');
}

// arranca todo cuando ya cargo la pagina
async function iniciar() {
    prepararPestanas();
    prepararLienzoZonas();
    prepararDialogoCerrar();
    prepararControles();

    // si le pican afuera de una ventana emergente se cierra
    document.querySelectorAll('.fondo-modal').forEach(m => {
        m.onclick = (e) => { if (e.target === m) cerrarVentana(m.id); };
    });

    // con Escape se cierran las ventanas abiertas
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            document.querySelectorAll('.fondo-modal.activo').forEach(m => cerrarVentana(m.id));
        }
    });

    conectarWS();
    empezarVideo();
    prepararModoPruebas();
    // el registro de alertas se revisa cada 3 segundos
    setTimeout(cargarAlertas, 1500);
    setInterval(cargarAlertas, 3000);
    await cargarConfiguracion();
}

document.addEventListener('DOMContentLoaded', iniciar);
