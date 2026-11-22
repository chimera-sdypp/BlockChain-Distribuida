# Puerta en Cloudflare

El firewall de la universidad filtra los nombres de DuckDNS, así que desde ahí no se llega a
`https://blockchain-chimera.duckdns.org`. Este Worker publica el mismo sistema en
`https://blockchain-chimera.<cuenta>.workers.dev`: recibe cada pedido y lo reenvía tal cual al Ingress
de GKE. No hace falta un dominio propio y el plan gratis alcanza (100 000 pedidos por día).

```
navegador (universidad) ──HTTPS──▶ Worker en workers.dev ──HTTPS──▶ blockchain-chimera.duckdns.org (Traefik en GKE)
```

- Reenvía todo: las páginas (`/`), la API (`/api`), los health-checks (`/salud/<servicio>`) y el SSE de
  `/eventos`, que va en streaming. El keepalive de 15 s del frontend evita que Cloudflare corte la
  conexión a los 100 s.
- Las redirecciones del origen (por ejemplo HTTP → HTTPS) se reescriben para que el navegador siga
  en `workers.dev` y no salte a DuckDNS.
- Si el origen no responde, devuelve 502 con `{"error": …}`.
- DuckDNS y cert-manager siguen igual: la salida de Cloudflare resuelve el nombre sin problemas y el
  tramo Worker → GKE también va cifrado.

## Ponerlo en marcha (una vez)

1. Crear la cuenta en <https://dash.cloudflare.com> y elegir el subdominio de `workers.dev`, en
   **Workers & Pages**. Ese subdominio es la `<cuenta>` de la URL.
2. Crear un API token con la plantilla **Edit Cloudflare Workers**, y copiar el **Account ID** que
   aparece en **Workers & Pages**.
3. Cargar los dos en el repo, en **Settings → Secrets and variables → Actions**:
   `CLOUDFLARE_API_TOKEN` y `CLOUDFLARE_ACCOUNT_ID`.
4. Correr el workflow **Pilar 3 · Cloudflare**. Después corre solo con cada push a `dev` que toque
   esta carpeta.

A mano, sin el workflow: `npx wrangler@4 login` y después
`npx wrangler@4 deploy --config pilar3/cloudflare/wrangler.toml`.

## Probarlo

```sh
node --test pilar3/cloudflare/worker.test.mjs                                # tests, sin wrangler
curl -s https://blockchain-chimera.<cuenta>.workers.dev/salud/backend        # una vez desplegado
```
