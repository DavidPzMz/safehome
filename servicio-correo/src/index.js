// Servicio de correo de SafeHome (Cloudflare Worker).
// La app ya no lleva ninguna contrasena: le pide a este servicio que mande
// los correos y la clave de Brevo solo vive aqui (como secreto de Cloudflare).
//
// Rutas (todas POST con JSON):
//   /codigo     {correo}                 -> manda un codigo de 4 digitos
//   /confirmar  {correo, codigo}         -> si es correcto regresa un token
//   /alerta     {correo, token, fecha, hora, mensaje, imagen}
//                                         -> manda la alerta con la foto
// El token solo sirve para ESE correo, asi que aunque alguien saque la
// direccion de este servicio solo podria mandarse alertas a si mismo.

const VIGENCIA_CODIGO_S = 600;      // el codigo sirve 10 minutos
const MAX_INTENTOS = 5;             // despues hay que pedir otro codigo
const ESPERA_REENVIO_S = 30;        // para no mandar codigos seguidos
const MAX_CODIGOS_HORA_CORREO = 5;  // codigos por hora al mismo correo
const MAX_CODIGOS_HORA_IP = 20;     // codigos por hora desde la misma IP
const MAX_ALERTAS_HORA = 30;        // alertas por hora al mismo correo
const MAX_IMAGEN_BYTES = 2_000_000; // la foto de la alerta (jpg)

const FORMATO_CORREO = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export default {
  async fetch(peticion, env) {
    if (peticion.method !== "POST") return respuesta(405, { error: "Solo POST" });
    let datos;
    try {
      datos = await peticion.json();
    } catch {
      return respuesta(400, { error: "JSON no valido" });
    }
    const ruta = new URL(peticion.url).pathname;
    try {
      if (ruta === "/codigo") return await pedirCodigo(datos, peticion, env);
      if (ruta === "/confirmar") return await confirmarCodigo(datos, env);
      if (ruta === "/alerta") return await mandarAlerta(datos, env);
      return respuesta(404, { error: "Ruta no encontrada" });
    } catch (e) {
      console.log("Error:", e.message);
      return respuesta(502, { error: "No se pudo enviar el correo" });
    }
  },
};

function respuesta(estado, cuerpo) {
  return new Response(JSON.stringify(cuerpo), {
    status: estado,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function limpiarCorreo(correo) {
  const limpio = String(correo || "").trim().toLowerCase();
  return FORMATO_CORREO.test(limpio) && limpio.length <= 254 ? limpio : "";
}

// --- Limites de envio (contadores por hora guardados en KV) ---

async function superaLimite(env, tipo, id, maximo) {
  const hora = Math.floor(Date.now() / 3_600_000);
  const clave = `lim:${tipo}:${id}:${hora}`;
  const cuenta = parseInt((await env.LIMITES.get(clave)) || "0", 10);
  if (cuenta >= maximo) return true;
  await env.LIMITES.put(clave, String(cuenta + 1), { expirationTtl: 3700 });
  return false;
}

// --- Token: firma HMAC del correo (solo este servicio conoce la llave) ---

async function llaveFirma(env) {
  return crypto.subtle.importKey("raw", new TextEncoder().encode(env.FIRMA_SECRETA),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

function aBase64Url(bytes) {
  let texto = "";
  for (const b of new Uint8Array(bytes)) texto += String.fromCharCode(b);
  return btoa(texto).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function deBase64Url(texto) {
  const normal = texto.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(normal), (c) => c.charCodeAt(0));
}

async function crearToken(env, correo) {
  const firma = await crypto.subtle.sign("HMAC", await llaveFirma(env),
    new TextEncoder().encode("safehome:v1:" + correo));
  return aBase64Url(firma);
}

async function tokenValido(env, correo, token) {
  try {
    // verify compara en tiempo constante
    return await crypto.subtle.verify("HMAC", await llaveFirma(env), deBase64Url(String(token)),
      new TextEncoder().encode("safehome:v1:" + correo));
  } catch {
    return false;
  }
}

// --- Envio con la API de Brevo ---

async function enviarCorreo(env, para, asunto, texto, adjunto) {
  if (env.SIMULAR_ENVIO === "1") {
    // para probar en local sin gastar correos
    console.log(`[simulado] a ${para}: ${asunto}\n${texto}${adjunto ? "\n(con foto)" : ""}`);
    return;
  }
  const cuerpo = {
    sender: { name: "SafeHome", email: env.REMITENTE_CORREO },
    to: [{ email: para }],
    subject: asunto,
    textContent: texto,
  };
  if (adjunto) cuerpo.attachment = [adjunto];
  const r = await fetch("https://api.brevo.com/v3/smtp/email", {
    method: "POST",
    headers: { "api-key": env.BREVO_API_KEY || env.Brevo_api_key, "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(cuerpo),
  });
  if (!r.ok) throw new Error(`Brevo respondio ${r.status}: ${await r.text()}`);
}

// --- Rutas ---

async function pedirCodigo(datos, peticion, env) {
  const correo = limpiarCorreo(datos.correo);
  if (!correo) return respuesta(400, { error: "Escribe un correo válido" });

  const claveCodigo = `cod:${correo}`;
  const anterior = await env.LIMITES.get(claveCodigo, "json");
  const ahora = Date.now() / 1000;
  if (anterior && ahora - anterior.enviado < ESPERA_REENVIO_S) {
    const falta = Math.ceil(ESPERA_REENVIO_S - (ahora - anterior.enviado));
    return respuesta(429, { error: `Espera ${falta} s para pedir otro código` });
  }
  const ip = peticion.headers.get("cf-connecting-ip") || "desconocida";
  if (await superaLimite(env, "ip", ip, MAX_CODIGOS_HORA_IP) ||
      await superaLimite(env, "codigo", correo, MAX_CODIGOS_HORA_CORREO)) {
    return respuesta(429, { error: "Pediste demasiados códigos, intenta en una hora" });
  }

  const codigo = String(crypto.getRandomValues(new Uint32Array(1))[0] % 10000).padStart(4, "0");
  await env.LIMITES.put(claveCodigo, JSON.stringify({ codigo, intentos: 0, enviado: ahora, vence: ahora + VIGENCIA_CODIGO_S }),
    { expirationTtl: VIGENCIA_CODIGO_S });
  try {
    await enviarCorreo(env, correo, `SafeHome - código de verificación ${codigo}`,
      `Tu código de verificación de SafeHome es: ${codigo}\n\n` +
      "Vence en 10 minutos. Si no lo pediste, ignora este correo.");
  } catch (e) {
    await env.LIMITES.delete(claveCodigo);  // si no salio el correo el codigo no sirve
    throw e;
  }
  return respuesta(200, { estado: "enviado", correo });
}

async function confirmarCodigo(datos, env) {
  const correo = limpiarCorreo(datos.correo);
  if (!correo) return respuesta(400, { error: "Escribe un correo válido" });
  const claveCodigo = `cod:${correo}`;
  const guardado = await env.LIMITES.get(claveCodigo, "json");
  const ahora = Date.now() / 1000;
  if (!guardado) return respuesta(400, { error: "Primero pide un código (o ya venció)" });
  if (ahora > guardado.vence) {
    await env.LIMITES.delete(claveCodigo);
    return respuesta(400, { error: "El código ya venció, pide otro" });
  }
  if (String(datos.codigo || "").trim() !== guardado.codigo) {
    guardado.intentos += 1;
    if (guardado.intentos >= MAX_INTENTOS) {
      await env.LIMITES.delete(claveCodigo);
      return respuesta(400, { error: "Demasiados intentos, pide otro código" });
    }
    // KV pide minimo 60 s de vida
    const resta = Math.max(60, Math.ceil(guardado.vence - ahora));
    await env.LIMITES.put(claveCodigo, JSON.stringify(guardado), { expirationTtl: resta });
    return respuesta(400, { error: `Código incorrecto (quedan ${MAX_INTENTOS - guardado.intentos} intentos)` });
  }
  await env.LIMITES.delete(claveCodigo);
  return respuesta(200, { estado: "confirmado", correo, token: await crearToken(env, correo) });
}

async function mandarAlerta(datos, env) {
  const correo = limpiarCorreo(datos.correo);
  if (!correo || !(await tokenValido(env, correo, datos.token || ""))) {
    return respuesta(403, { error: "Correo no verificado" });
  }
  if (await superaLimite(env, "alerta", correo, MAX_ALERTAS_HORA)) {
    return respuesta(429, { error: "Demasiadas alertas en una hora" });
  }

  // los textos se recortan para que no se pueda mandar cualquier cosa
  const corto = (v, n) => String(v || "").replace(/[\r\n]+/g, " ").slice(0, n);
  const mensaje = corto(datos.mensaje, 300) || "ALERTA: Actividad sospechosa detectada";
  const fecha = corto(datos.fecha, 20);
  const hora = corto(datos.hora, 20);
  // el correo no dice que se detecto: eso solo se ve en la app
  const texto = `${mensaje}\n\nFecha: ${fecha}\nHora: ${hora}\n\nRevisa la foto adjunta.`;

  let adjunto = null;
  if (datos.imagen) {
    const imagen = String(datos.imagen);
    // solo jpg (empieza con FF D8 FF = "/9j/" en base64) y de tamano limitado
    if (!imagen.startsWith("/9j/") || imagen.length > MAX_IMAGEN_BYTES * 4 / 3) {
      return respuesta(400, { error: "Imagen no valida" });
    }
    adjunto = { content: imagen, name: `alerta_${fecha}_${hora}.jpg`.replace(/[^0-9A-Za-z_.-]/g, "-") };
  }
  await enviarCorreo(env, correo, `ALERTA SafeHome - ${hora}`, texto, adjunto);
  return respuesta(200, { estado: "enviado" });
}
