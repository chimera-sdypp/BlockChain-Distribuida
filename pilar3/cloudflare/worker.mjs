// Puerta de entrada en Cloudflare: el firewall de la universidad filtra los nombres de DuckDNS, así que
// las páginas y la API también se publican en https://<nombre>.<cuenta>.workers.dev. El Worker reenvía
// cada pedido tal cual (método, ruta, encabezados y cuerpo) al sistema en GKE y devuelve la respuesta
// en streaming, así que el SSE de /eventos sigue andando. La salida de Cloudflare sí resuelve DuckDNS.

const ORIGEN = "https://blockchain-chimera.duckdns.org";

export default {
  async fetch(pedido, env = {}) {
    const origen = new URL(env.ORIGEN || ORIGEN);
    const entrada = new URL(pedido.url);
    const destino = new URL(entrada.pathname + entrada.search, origen);

    const encabezados = new Headers(pedido.headers);
    encabezados.delete("host");
    encabezados.set("X-Forwarded-Host", entrada.host);
    encabezados.set("X-Forwarded-Proto", entrada.protocol.replace(":", ""));

    let respuesta;
    try {
      respuesta = await fetch(destino, {
        method: pedido.method,
        headers: encabezados,
        body: ["GET", "HEAD"].includes(pedido.method) ? null : pedido.body,
        redirect: "manual", // las redirecciones las sigue el navegador, con la URL reescrita abajo
      });
    } catch (error) {
      return Response.json({ error: `el sistema no responde: ${error.message}` }, { status: 502 });
    }

    const salida = new Response(respuesta.body, respuesta);
    const ubicacion = salida.headers.get("Location");
    if (ubicacion) {
      const url = new URL(ubicacion, destino);
      if (url.host === origen.host) {
        url.protocol = entrada.protocol;
        url.host = entrada.host;
        salida.headers.set("Location", url.toString());
      }
    }
    return salida;
  },
};
