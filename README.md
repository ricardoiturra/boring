# Boring Classic

Publica servicios HTTP(S) propios mediante Cloudflare Tunnel en Debian con systemd. Boring mantiene un inventario local, configura un conector por túnel y detiene los conectores vencidos.

## Instalación

Necesitas un host Debian 12 o posterior con systemd como PID 1, una cuenta de Cloudflare y una zona activa en esa cuenta. Cloudflare Tunnel está disponible en todos los planes. La cobertura del certificado de edge y las funciones opcionales tienen condiciones propias. Para el primer ensayo, usa una zona de pruebas y un subdominio de un solo nivel. [Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/) · [Publicación por API](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel-api/)

1. Si falta `/usr/bin/cloudflared`, habilita el repositorio oficial de Cloudflare con la clave vigente y actualiza el índice APT. En Debian Trixie aplica el procedimiento recomendado para distribuciones Debian. [Paquetes oficiales de Cloudflare](https://pkg.cloudflare.com/index.html)
2. Desde una copia de confianza del proyecto, ejecuta `sudo ./install.sh`. Instala Python y las CA del sistema, instala o comprueba `cloudflared` con soporte de `--token-file`, registra las unidades y activa el timer.
3. Crea un token API limitado a la cuenta y la zona del ensayo. Otorga **Cloudflare Tunnel Write**, **Zone Read** y **DNS Write**. Boring usa la lista de rutas privadas antes de eliminar. Si tu token requiere un permiso adicional para esas consultas, añade **Cloudflare One Networks Read** con alcance de cuenta. Usa `sudoedit /etc/boring/api.token` y `sudo chmod 600 /etc/boring/api.token`. Guarda el valor como una sola línea. El operador debe verificar que el token esté limitado a los recursos necesarios. [API de túneles](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/) · [API de rutas](https://developers.cloudflare.com/api/resources/zero_trust/subresources/networks/subresources/routes/methods/list/)
4. Configura Boring con tus identificadores y ejecuta el diagnóstico.

```bash
sudo boring config --account-id 0123456789abcdef0123456789abcdef \
  --zone-id fedcba9876543210fedcba9876543210 --zone example.com \
  --api-token-file /etc/boring/api.token
sudo boring doctor --json
```

## Primer túnel

Para un servicio ya disponible en el host, sustituye el hostname y la URL del ejemplo. Antes de iniciar el conector, revisa la autenticación en la aplicación o habilita una política de Cloudflare Access para el hostname si debe ser privado. Classic crea un hostname público y el conector queda detenido por defecto.

```bash
sudo boring create --name grafana --hostname grafana.example.com \
  --url http://127.0.0.1:3000 --expires-in 24h
sudo boring start --name grafana
sudo boring status --name grafana --json
sudo boring logs --name grafana --lines 20
sudo boring renew --name grafana --expires-in 48h
sudo boring stop --name grafana
sudo boring delete --name grafana
```

[Operación y referencia CLI](docs/operations.md) · [Diseño y decisiones](docs/design.md) · [Seguridad](SECURITY.md)

## Pruebas locales

```bash
PYTHONPATH=lib python3 -m unittest discover -s tests -v
for script in boring.sh install.sh uninstall.sh; do bash -n "$script" || exit; done
./boring.sh version --json
```

Las pruebas usan servidores Cloudflare y systemd simulados a nivel de la interfaz de cada adaptador. Incluyen pérdida de respuesta tras crear o borrar, recuperación desde el inventario, protección de credenciales y verificación de caducidad al volver a cargar el estado. El ensayo de aceptación sobre Debian con systemd y una zona real requiere autorización.

Para retirar el software local y detener conectores, ejecuta `sudo ./uninstall.sh --yes`. El inventario, la configuración y las credenciales quedan disponibles para reinstalar y recuperar. Para eliminar recursos remotos usa `boring delete` por túnel mientras Boring esté instalado.
