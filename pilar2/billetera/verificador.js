// El verificador de la billetera (BC-84): el port a JavaScript de comun/estado.py y de lo que usa de comun/contrato.py
// (contrato v3). Anda igual en el navegador (adentro de billetera.html) y en node (los tests lo comparan con Python
// sobre el mismo vector). Sin dependencias: MD5 propio (WebCrypto no lo trae) y Ed25519 con WebCrypto.
//
//   const estado = Verificador.estadoNuevo();
//   const { problemas, avisos } = await Verificador.aplicarBloque(estado, bloque, reglas);   // estricto
//
// reglas = { organizador: "" | cuenta, fondoEpoca: 100, shares: async (epoca) => detalle | null, verificarFirmas }
"use strict";

const Verificador = (() => {
  const EMISION = "emision";
  const CEROS = "0".repeat(32);
  const RAIZ_VACIA = CEROS;
  const MONTO_MAXIMO = 10 ** 12;
  const NONCE_MAXIMO = Number.MAX_SAFE_INTEGER;  // en JS no hay enteros de 64 bits en JSON: alcanza
  const BLOQUES_POR_EPOCA = 10;
  const FONDO_EPOCA = 100;
  const RACHA_MAXIMA = 6;
  const TIPOS_ALTA = ["usuario", "minero", "validador"];
  const ACTAS = ["alta", "baja", "permiso"];
  const MOTIVO_MAXIMO = 100;
  const CUENTA = /^[0-9a-f]{64}$/;
  const FIRMA = /^[0-9a-f]{128}$/;
  const NOMBRE = /^[A-Za-z0-9_.-]{1,32}$/;

  // ── MD5 (RFC 1321) sobre los bytes UTF-8, como hashlib.md5(texto.encode()) ──

  const K = Array.from({ length: 64 }, (_, i) => Math.floor(Math.abs(Math.sin(i + 1)) * 2 ** 32) >>> 0);
  const S = [7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21];
  const codificador = new TextEncoder();

  function md5(texto) {
    const datos = codificador.encode(texto);
    const largo = datos.length;
    const palabras = (((largo + 8) >>> 6) + 1) * 16;
    const m = new Uint32Array(palabras);
    for (let i = 0; i < largo; i++) m[i >> 2] |= datos[i] << ((i % 4) * 8);
    m[largo >> 2] |= 0x80 << ((largo % 4) * 8);
    m[palabras - 2] = (largo * 8) >>> 0;
    m[palabras - 1] = Math.floor((largo * 8) / 2 ** 32);
    let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;
    for (let j = 0; j < palabras; j += 16) {
      let a = a0, b = b0, c = c0, d = d0;
      for (let i = 0; i < 64; i++) {
        let f, g;
        if (i < 16) { f = (b & c) | (~b & d); g = i; }
        else if (i < 32) { f = (d & b) | (~d & c); g = (5 * i + 1) % 16; }
        else if (i < 48) { f = b ^ c ^ d; g = (3 * i + 5) % 16; }
        else { f = c ^ (b | ~d); g = (7 * i) % 16; }
        const x = (a + f + K[i] + m[j + g]) >>> 0;
        const s = S[(i >> 4) * 4 + (i % 4)];
        a = d; d = c; c = b;
        b = (b + ((x << s) | (x >>> (32 - s)))) >>> 0;
      }
      a0 = (a0 + a) >>> 0; b0 = (b0 + b) >>> 0; c0 = (c0 + c) >>> 0; d0 = (d0 + d) >>> 0;
    }
    return [a0, b0, c0, d0].map((w) => [0, 8, 16, 24].map((k) => ((w >>> k) & 0xff).toString(16).padStart(2, "0")).join("")).join("");
  }

  // ── Ed25519 (WebCrypto) ──

  const deHex = (hex) => Uint8Array.from(hex.match(/../g) || [], (b) => parseInt(b, 16));
  const claves = new Map();

  async function verificarFirma(cuenta, mensaje, firma) {
    if (!cuentaValida(cuenta) || typeof firma !== "string" || !FIRMA.test(firma)) return false;
    try {
      if (!claves.has(cuenta)) {
        claves.set(cuenta, await crypto.subtle.importKey("raw", deHex(cuenta), { name: "Ed25519" }, false, ["verify"]));
      }
      return await crypto.subtle.verify({ name: "Ed25519" }, claves.get(cuenta), deHex(firma), codificador.encode(mensaje));
    } catch {
      return false;
    }
  }

  // ── Contrato ──

  const cuentaValida = (v) => typeof v === "string" && CUENTA.test(v);
  const nombreValido = (v) => typeof v === "string" && NOMBRE.test(v);
  const entero = (v, minimo, maximo) => Number.isInteger(v) && v >= minimo && v <= maximo;
  const recompensa = (prefijo) => 10 * prefijo.length;
  const epocaDe = (altura) => Math.floor(altura / BLOQUES_POR_EPOCA);
  const esPrimerBloqueDeEpoca = (altura) => altura > 0 && altura % BLOQUES_POR_EPOCA === 0;
  const hashBloque = (texto, nonce) => md5(`${texto}${nonce}`);

  const serializar = (tx) => `${tx.id}:${tx.origen}:${tx.destino}:${tx.monto}:${tx.nonce}:${tx.timestamp}:${tx.datos ?? ""}:${tx.firma}`;
  const raizTransacciones = (txs) => md5(txs.map(serializar).join("|"));
  const raizBloque = (txs, raizEstado) => md5(`${raizTransacciones(txs)}|${raizEstado}`);
  const textoDeBloque = (b) => `${b.previous_hash};${b.timestamp};${raizBloque(b.transactions, b.raiz_estado)};`;

  const TIPOS_EMISION = [["genesis-", "genesis"], ["organizador-", "organizador"], ["cb-", "coinbase"], ["carga-", "carga"],
    ["part-", "participacion"], ["shares-", "shares"], ["alta-", "alta"], ["baja-", "baja"], ["permiso-", "permiso"]];

  function tipoDe(tx) {
    const id = String(tx.id ?? "");
    if (tx.origen !== EMISION) return ["alta", "baja"].find((a) => id.startsWith(`${a}-`)) || "transferencia";
    return (TIPOS_EMISION.find(([p]) => id.startsWith(p)) || [null, "desconocida"])[1];
  }
  const esActa = (tx) => ACTAS.includes(tipoDe(tx));
  const esDelegada = (tx) => esActa(tx) && tx.origen !== EMISION;
  const idActa = (tipo, origen, n) => origen === EMISION ? `${tipo}-${n}` : `${tipo}-${origen}-${n}`;
  const mensajeTransaccion = (tx) => `${tx.origen}:${tx.destino}:${tx.monto}:${tx.nonce}:${tx.timestamp}`;
  const mensajeActa = (tx) => `${tipoDe(tx)}:${tx.origen}:${tx.destino}:${tx.nonce}:${tx.timestamp}:${tx.datos ?? ""}`;
  const mensajeFirmaBloque = (altura, hash) => `firma:${altura}:${hash}`;
  const mensajeAtestacion = (a) => `atestacion:${a.nodo}:${a.altura}:${a.punta}:${a.raiz}:${a.tiempo}`;

  function emision(id, destino, monto, nonce, timestamp, datos = "") {
    const tx = { id, origen: EMISION, destino, monto, nonce, timestamp, firma: "" };
    if (datos) tx.datos = datos;
    return tx;
  }
  const coinbase = (altura, minero, monto, ts) => emision(`cb-${altura}-${minero}`, minero, monto, 0, ts);
  const participacion = (e, minero, monto, ts) => emision(`part-${e}-${minero}`, minero, monto, 0, ts);
  const sharesEpoca = (e, fondo, raiz, ts) => emision(`shares-${e}`, "", 0, 0, ts, `${fondo}:${raiz}`);

  // Dos transacciones son iguales si tienen los mismos campos (un `datos` vacío es lo mismo que no tenerlo).
  function igual(a, b) {
    const limpio = (tx) => Object.fromEntries(Object.entries(tx).filter(([k, v]) => !(k === "datos" && v === ""))
      .sort((x, y) => (x[0] < y[0] ? -1 : 1)));
    return JSON.stringify(limpio(a)) === JSON.stringify(limpio(b));
  }

  function datosAlta(tx) {
    const datos = String(tx.datos ?? "");
    const i = datos.indexOf(":");
    return i < 0 ? [datos, ""] : [datos.slice(0, i), datos.slice(i + 1)];
  }

  async function validarTransferencia(tx) {
    const errores = [];
    for (const campo of ["origen", "destino"]) if (!cuentaValida(tx[campo])) errores.push(`${campo}: no es una cuenta`);
    if (!entero(tx.monto, 1, MONTO_MAXIMO)) errores.push("monto");
    if (!entero(tx.nonce, 1, NONCE_MAXIMO)) errores.push("nonce");
    if (!entero(tx.timestamp, 0, NONCE_MAXIMO)) errores.push("timestamp");
    if (typeof tx.firma !== "string" || !FIRMA.test(tx.firma)) errores.push("firma");
    if ((tx.datos ?? "") !== "") errores.push("datos: una transferencia no lleva datos");
    if (errores.length) return errores;
    if (tx.origen === tx.destino) return ["origen y destino tienen que ser distintos"];
    if (!(await verificarFirma(tx.origen, mensajeTransaccion(tx), tx.firma))) return ["firma: no es la del origen"];
    if ("id" in tx && tx.id !== md5(`${mensajeTransaccion(tx)}:${tx.firma}`)) return ["id: no es el hash de la transacción"];
    return [];
  }

  async function validarCarga(tx, organizador) {
    const errores = [];
    if (tx.origen !== EMISION) return ["origen: tiene que ser «emision»"];
    if (!cuentaValida(tx.destino)) errores.push("destino");
    if (!entero(tx.monto, 1, MONTO_MAXIMO)) errores.push("monto");
    if (!entero(tx.nonce, 1, NONCE_MAXIMO) || tx.id !== `carga-${tx.nonce}`) errores.push("id: carga-<n>");
    if (!entero(tx.timestamp, 0, NONCE_MAXIMO)) errores.push("timestamp");
    if ((tx.datos ?? "") !== "") errores.push("datos: una carga no lleva datos");
    if (!errores.length && !(await verificarFirma(organizador, mensajeTransaccion(tx), tx.firma))) {
      errores.push("firma: no es la del organizador");
    }
    return errores;
  }

  async function validarActa(tx, organizador) {
    const tipo = tipoDe(tx);
    if (!ACTAS.includes(tipo)) return ["id: alta-<n>, baja-<n> o permiso-<n>"];
    const errores = [];
    if (tx.origen !== EMISION && (!cuentaValida(tx.origen) || tipo === "permiso")) errores.push("origen");
    if (!cuentaValida(tx.destino)) errores.push("destino");
    if (tx.monto !== 0) errores.push("monto: 0");
    if (!entero(tx.nonce, 1, NONCE_MAXIMO) || tx.id !== idActa(tipo, tx.origen, tx.nonce)) errores.push("id");
    if (!entero(tx.timestamp, 0, NONCE_MAXIMO)) errores.push("timestamp");
    const datos = tx.datos ?? "";
    if (typeof datos !== "string") errores.push("datos");
    else if (tipo === "alta") {
      const i = datos.indexOf(":");
      if (i < 0 || !TIPOS_ALTA.includes(datos.slice(0, i)) || !nombreValido(datos.slice(i + 1))) errores.push("datos: <tipo>:<nombre>");
    } else if (tipo === "baja") {
      if (datos.length > MOTIVO_MAXIMO || datos.includes("|") || datos.includes("\n")) errores.push("datos: el motivo");
    } else if (datos && !nombreValido(datos)) errores.push("datos: el prefijo");
    if (errores.length) return errores;
    const firmante = tx.origen === EMISION ? organizador : tx.origen;
    if (!firmante) return ["no hay organizador: nadie puede firmar actas"];
    if (!(await verificarFirma(firmante, mensajeActa(tx), tx.firma))) {
      return [tx.origen === EMISION ? "firma: no es la del organizador" : "firma: no es la del origen"];
    }
    return [];
  }

  function listaShares(detalle) {
    return Object.keys(detalle).sort().filter((m) => (detalle[m].shares || 0) > 0)
      .map((m) => `${m}:${detalle[m].shares}:${detalle[m].racha}`);
  }
  const raizShares = (detalle) => md5(listaShares(detalle).join("|"));

  function repartir(fondo, shares, rachas) {
    const pesos = {};
    for (const [m, n] of Object.entries(shares)) {
      if (n > 0) pesos[m] = BigInt(n) * BigInt(10 + Math.min(Math.max(rachas[m] ?? 1, 1), RACHA_MAXIMA) - 1);
    }
    const total = Object.values(pesos).reduce((a, b) => a + b, 0n);
    const montos = {};
    if (total > 0n) {
      for (const m of Object.keys(pesos).sort()) {
        const monto = Number((BigInt(fondo) * pesos[m]) / total);
        if (monto > 0) montos[m] = monto;
      }
    }
    return montos;
  }

  function alturaFinalizada(validadores, ultimas) {
    const umbral = validadores.size ? Math.ceil((2 * validadores.size) / 3) : 0;
    const alturas = [...validadores].filter((v) => v in ultimas).map((v) => ultimas[v]).sort((a, b) => b - a);
    return umbral && alturas.length >= umbral ? alturas[umbral - 1] : -1;
  }

  // ── Merkle ──

  const hashHoja = (hoja) => md5(`h:${hoja}`);
  const hashNodo = (a, b) => md5(`n:${a}${b}`);

  function siguienteNivel(nivel) {
    const otro = [];
    for (let i = 0; i < nivel.length; i += 2) otro.push(i + 1 < nivel.length ? hashNodo(nivel[i], nivel[i + 1]) : nivel[i]);
    return otro;
  }

  function raizMerkle(hashes) {
    let nivel = [...hashes];
    if (!nivel.length) return RAIZ_VACIA;
    while (nivel.length > 1) nivel = siguienteNivel(nivel);
    return nivel[0];
  }

  function raizDeRama(hoja, rama) {
    let actual = hashHoja(hoja);
    for (const paso of rama) actual = paso.lado === "izq" ? hashNodo(paso.hash, actual) : hashNodo(actual, paso.hash);
    return actual;
  }

  // ── El estado ──

  const estadoNuevo = () => ({ altura: -1, punta: null, timestamp: 0, saldos: {}, nonces: {}, cuentas: {}, permisos: {},
    organizador: "", shares: {} });
  const copiar = (e) => JSON.parse(JSON.stringify(e));

  function hojaCuenta(cuenta, tipo, saldo, nonce) {
    if (!saldo && !nonce && !tipo) return null;
    return `${cuenta}:${tipo || "-"}:${saldo}:${nonce}`;
  }

  // [[clave, hoja]] ordenadas por clave, como estado.hojas_estado.
  function hojas(e) {
    const lista = [];
    const todas = new Set([...Object.keys(e.saldos), ...Object.keys(e.nonces), ...Object.keys(e.cuentas)]);
    for (const c of todas) {
      const hoja = hojaCuenta(c, e.cuentas[c]?.tipo || "", e.saldos[c] || 0, e.nonces[c] || 0);
      if (hoja) lista.push([c, hoja]);
    }
    for (const [clave, prefijo] of Object.entries(e.permisos)) if (prefijo) lista.push([`permiso:${clave}`, `permiso:${clave}:${prefijo}`]);
    return lista.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  }

  const raiz = (e) => raizMerkle(hojas(e).map(([, h]) => hashHoja(h)));
  const sumar = (e, cuenta, monto) => { e.saldos[cuenta] = (e.saldos[cuenta] || 0) + monto; };

  function problemaDeActa(tx, e) {
    const tipo = tipoDe(tx), destino = tx.destino;
    if ((tipo === "alta" || tipo === "baja") && destino === e.organizador) return "el organizador no se da de alta ni de baja";
    if (!esDelegada(tx)) return null;
    const prefijo = e.permisos[tx.origen];
    if (!prefijo) return "sin permiso: la clave no tiene un permiso del organizador";
    const existente = e.cuentas[destino];
    const esSuyo = (c) => c && c.tipo === "minero" && String(c.nombre || "").startsWith(prefijo);
    if (tipo === "alta") {
      const [tipoCuenta, nombre] = datosAlta(tx);
      if (tipoCuenta !== "minero" || !nombre.startsWith(prefijo)) return `sin permiso: sólo puede dar de alta mineros ${prefijo}*`;
      if (existente && !esSuyo(existente)) return "sin permiso: la cuenta ya existe y no es uno de sus mineros";
    } else if (!esSuyo(existente)) return `sin permiso: sólo puede dar de baja mineros ${prefijo}*`;
    return null;
  }

  function siguienteNonce(e, cuenta, tx) {
    const esperado = (e.nonces[cuenta] || 0) + 1;
    return tx.nonce !== esperado ? `${tx.id}: nonce ${tx.nonce}, y le tocaba el ${esperado}` : null;
  }

  // Una transacción de la caja sobre `e` (una copia): el problema, o null si se aplicó.
  async function aplicarTransaccion(e, tx, reglas) {
    const tipo = tipoDe(tx);
    if (tipo === "transferencia") {
      const errores = reglas.verificarFirmas === false ? [] : await validarTransferencia(tx);
      if (errores.length) return `transferencia ${tx.id} inválida: ${errores.join("; ")}`;
      if (!cuentaValida(tx.origen) || !cuentaValida(tx.destino) || !entero(tx.monto, 1, MONTO_MAXIMO)) return `transferencia ${tx.id} mal formada`;
      if (!e.cuentas[tx.origen]) return `${tx.id}: el origen no tiene un alta en la cadena (cuenta no aprobada)`;
      const problema = siguienteNonce(e, tx.origen, tx);
      if (problema) return problema;
      if ((e.saldos[tx.origen] || 0) < tx.monto) return `${tx.id}: el origen no tiene saldo`;
      sumar(e, tx.origen, -tx.monto);
      sumar(e, tx.destino, tx.monto);
      e.nonces[tx.origen] = tx.nonce;
      return null;
    }
    if (tipo === "carga") {
      if (!e.organizador) return `carga ${tx.id}: no hay organizador en el génesis`;
      const errores = reglas.verificarFirmas === false ? [] : await validarCarga(tx, e.organizador);
      if (errores.length) return `carga ${tx.id} inválida: ${errores.join("; ")}`;
      const problema = siguienteNonce(e, EMISION, tx);
      if (problema) return problema;
      sumar(e, tx.destino, tx.monto);
      e.nonces[EMISION] = tx.nonce;
      return null;
    }
    if (ACTAS.includes(tipo)) {
      if (reglas.verificarFirmas !== false) {
        const errores = await validarActa(tx, e.organizador);
        if (errores.length) return `${tipo} ${tx.id} inválida: ${errores.join("; ")}`;
      }
      const firmante = tx.origen === EMISION ? EMISION : tx.origen;
      const problema = siguienteNonce(e, firmante, tx) || problemaDeActa(tx, e);
      if (problema) return problema.startsWith(tx.id) ? problema : `${tx.id}: ${problema}`;
      if (tipo === "alta") {
        const [tipoCuenta, nombre] = datosAlta(tx);
        e.cuentas[tx.destino] = { tipo: tipoCuenta, nombre, alta: tx.timestamp };
      } else if (tipo === "baja") delete e.cuentas[tx.destino];
      else if (tx.datos) e.permisos[tx.destino] = tx.datos;
      else delete e.permisos[tx.destino];
      e.nonces[firmante] = tx.nonce;
      return null;
    }
    return `${tx.id}: una transacción ${tipo} fuera de lugar`;
  }

  function genesis(e, bloque, reglas, problemas) {
    if (bloque.previous_hash !== CEROS || bloque.nonce !== 0) problemas.push("el génesis tiene que tener previous_hash en ceros y nonce 0");
    if (bloque.raiz_estado !== RAIZ_VACIA) problemas.push("la raíz de estado del génesis es la del árbol vacío");
    const ts = bloque.timestamp;
    (bloque.transactions || []).forEach((tx, i) => {
      const tipo = tipoDe(tx), destino = tx.destino;
      if (tipo === "organizador" && i === 0) {
        if (!igual(tx, emision(`organizador-${destino}`, destino, 0, 0, ts)) || !cuentaValida(destino)) {
          problemas.push("la transacción del organizador del génesis está mal formada");
          return;
        }
        if (reglas.organizador && destino !== reglas.organizador) problemas.push(`el organizador del génesis es ${destino} y no el conocido`);
        e.organizador = destino;
        e.cuentas[destino] = { tipo: "organizador", nombre: "organizador", alta: ts };
      } else if (tipo === "genesis") {
        const [tipoCuenta, nombre] = datosAlta(tx);
        if (!igual(tx, emision(`genesis-${destino}`, destino, tx.monto, 0, ts, `usuario:${nombre}`)) || !cuentaValida(destino)
            || tipoCuenta !== "usuario" || !Number.isInteger(tx.monto) || tx.monto < 0 || (nombre && !nombreValido(nombre))) {
          problemas.push(`transacción del génesis mal formada: ${tx.id}`);
          return;
        }
        sumar(e, destino, tx.monto);
        e.cuentas[destino] = { tipo: "usuario", nombre, alta: ts };
      } else problemas.push(`una transacción ${tipo} en el génesis: ${tx.id}`);
    });
    if (!e.organizador && reglas.organizador) e.organizador = reglas.organizador;
  }

  async function detalleDe(reglas, epoca, raizEsperada) {
    reglas.detalles = reglas.detalles || {};
    if (epoca in reglas.detalles) return reglas.detalles[epoca];
    let detalle = reglas.shares ? await reglas.shares(epoca) : null;
    if (detalle && raizShares(detalle) !== raizEsperada) detalle = false;
    if (detalle !== null && detalle !== undefined) reglas.detalles[epoca] = detalle;
    return detalle ?? null;
  }

  async function verificarReparto(antes, epoca, detalle, pagos, fondo, reglas, avisos) {
    const problemas = [];
    const shares = {}, rachas = {};
    for (const [m, d] of Object.entries(detalle)) if ((d.shares || 0) > 0) { shares[m] = d.shares; rachas[m] = d.racha; }
    const esperado = repartir(fondo, shares, rachas);
    const claves = (o) => JSON.stringify(Object.keys(o).sort().map((k) => [k, o[k]]));
    if (claves(esperado) !== claves(pagos)) problemas.push(`los pagos de la época ${epoca} no son el reparto de su lista de shares`);
    for (const [minero, racha] of Object.entries(rachas)) {
      let esperada = 1;
      while (esperada < RACHA_MAXIMA && epoca - esperada >= 0) {
        const anterior = epoca - esperada;
        const raizAnterior = antes.shares[anterior];
        if (raizAnterior === undefined) break;
        const previa = await detalleDe(reglas, anterior, raizAnterior);
        if (!previa) { avisos.push(`época ${epoca}: la racha no se verificó (falta la lista de la ${anterior})`); esperada = racha; break; }
        if (((previa[minero] || {}).shares || 0) <= 0) break;
        esperada += 1;
      }
      if (racha !== esperada) problemas.push(`época ${epoca}: ${minero.slice(0, 12)} tiene racha ${racha} y le corresponde ${esperada}`);
    }
    return problemas;
  }

  async function pagoDeEpoca(antes, e, txs, i, altura, reglas, problemas, avisos) {
    const epoca = epocaDe(altura) - 1;
    const ts = antes.timestamp;
    if (i < txs.length && tipoDe(txs[i]) === "shares") {
      const tx = txs[i];
      const datos = String(tx.datos ?? "");
      const j = datos.indexOf(":");
      const fondoTexto = j < 0 ? datos : datos.slice(0, j), raizS = j < 0 ? "" : datos.slice(j + 1);
      const fondo = /^[0-9]+$/.test(fondoTexto) ? Number(fondoTexto) : -1;
      if (!igual(tx, sharesEpoca(epoca, fondo, raizS, ts)) || raizS.length !== 32) {
        problemas.push(`${tx.id}: no es shares-${epoca} con el timestamp del último bloque de la época`);
        return i + 1;
      }
      i += 1;
      const pagos = {};
      while (i < txs.length && tipoDe(txs[i]) === "participacion") {
        const pago = txs[i], minero = pago.destino;
        if (!igual(pago, participacion(epoca, minero, pago.monto, ts)) || !cuentaValida(minero) || !Number.isInteger(pago.monto)
            || pago.monto < 1 || minero in pagos) problemas.push(`${pago.id}: no es un pago de la época ${epoca}`);
        else pagos[minero] = pago.monto;
        i += 1;
      }
      const orden = Object.keys(pagos);
      if (JSON.stringify(orden) !== JSON.stringify([...orden].sort())) problemas.push(`los pagos de la época ${epoca} no están en orden de minero`);
      if (fondo !== (reglas.fondoEpoca ?? FONDO_EPOCA)) problemas.push(`la época ${epoca} reparte ${fondo} y el fondo es ${reglas.fondoEpoca ?? FONDO_EPOCA}`);
      const detalle = await detalleDe(reglas, epoca, raizS);
      if (detalle === false) problemas.push(`la lista publicada de la época ${epoca} no da la raíz de la cadena (${raizS})`);
      else if (detalle === null) avisos.push(`época ${epoca}: el pago no se verificó (no se consiguió la lista de shares)`);
      else problemas.push(...await verificarReparto(antes, epoca, detalle, pagos, fondo, reglas, avisos));
      for (const [m, monto] of Object.entries(pagos)) sumar(e, m, monto);
      e.shares[epoca] = raizS;
    } else if (i < txs.length && tipoDe(txs[i]) === "participacion") {
      problemas.push(`${txs[i].id}: un pago sin la lista de shares de la época (shares-${epoca})`);
      while (i < txs.length && tipoDe(txs[i]) === "participacion") i += 1;
    }
    return i;
  }

  // Verifica `bloque` contra `estado` (el de la punta) y, si cierra, lo aplica. Siempre estricto: si hay un problema, el
  // estado queda como estaba. Devuelve {problemas, avisos}.
  async function aplicarBloque(estado, bloque, reglas = {}, hashEsperado = null) {
    const problemas = [], avisos = [];
    const altura = estado.altura + 1;
    const e = copiar(estado);
    try {
      const txs = bloque.transactions;
      if (hashEsperado && bloque.block_hash !== hashEsperado) problemas.push(`el bloque dice ser ${bloque.block_hash} y se pidió ${hashEsperado}`);
      if ((bloque.altura ?? altura) !== altura) problemas.push(`dice tener altura ${bloque.altura} y le toca la ${altura}`);
      if (hashBloque(textoDeBloque(bloque), bloque.nonce) !== bloque.block_hash) problemas.push("el hash no sale del texto (raíz de transacciones y de estado) y el nonce");
      if (altura === 0) genesis(e, bloque, reglas, problemas);
      else {
        const prefijo = bloque.prefijo || "";
        if (bloque.previous_hash !== estado.punta) problemas.push("no encadena: su previous_hash no es la punta");
        const raizAntes = raiz(estado);
        if (bloque.raiz_estado !== raizAntes) problemas.push(`la raíz de estado no es la del bloque anterior (dice ${bloque.raiz_estado}, y es ${raizAntes})`);
        if (!prefijo || !bloque.block_hash.startsWith(prefijo)) problemas.push(`el hash no cumple el prefijo «${prefijo}»`);
        let i;
        if (!txs.length || tipoDe(txs[0]) !== "coinbase") { problemas.push("la primera transacción no es la coinbase"); i = 0; }
        else {
          const minero = txs[0].destino;
          if (!igual(txs[0], coinbase(altura, minero, recompensa(prefijo), bloque.timestamp)) || !cuentaValida(minero)) {
            problemas.push(`la coinbase no es la del ganador con ${recompensa(prefijo)}`);
          } else sumar(e, minero, txs[0].monto);
          i = 1;
        }
        if (esPrimerBloqueDeEpoca(altura)) i = await pagoDeEpoca(estado, e, txs, i, altura, reglas, problemas, avisos);
        for (const tx of txs.slice(i)) {
          const problema = await aplicarTransaccion(e, tx, reglas);
          if (problema) problemas.push(problema);
        }
      }
    } catch (error) {
      problemas.push(`el bloque está mal formado: ${error.message}`);
    }
    if (problemas.length) return { problemas, avisos };
    Object.assign(estado, e, { altura, punta: bloque.block_hash, timestamp: bloque.timestamp });
    return { problemas, avisos };
  }

  // La altura finalizada según las firmas de los bloques (las de /api/bloques), verificadas acá contra los validadores
  // que salen de las altas de la cadena. `firmas`: [{validador, altura, block_hash, firma}]; `hashes`: altura → hash.
  async function finalidad(estado, firmas, hashes) {
    const validadores = new Set(Object.entries(estado.cuentas).filter(([, d]) => d.tipo === "validador").map(([c]) => c));
    const ultimas = {};
    for (const f of firmas) {
      if (!validadores.has(f.validador) || hashes[f.altura] !== f.block_hash) continue;
      if (!(await verificarFirma(f.validador, mensajeFirmaBloque(f.altura, f.block_hash), f.firma))) continue;
      ultimas[f.validador] = Math.max(ultimas[f.validador] ?? -1, f.altura);
    }
    return { altura: alturaFinalizada(validadores, ultimas), validadores: validadores.size,
      umbral: validadores.size ? Math.ceil((2 * validadores.size) / 3) : 0, firmasValidas: Object.keys(ultimas).length };
  }

  return { md5, verificarFirma, tipoDe, textoDeBloque, raizBloque, raizTransacciones, raizShares, listaShares, repartir,
    alturaFinalizada, raizMerkle, raizDeRama, hashHoja, hojas, raiz, estadoNuevo, copiar, aplicarBloque, finalidad,
    mensajeTransaccion, mensajeAtestacion, mensajeFirmaBloque, cuentaValida, nombreValido, EMISION };
})();

if (typeof module !== "undefined") module.exports = Verificador;
