# Operación de Classic

## CLI

| Comando | Uso |
| --- | --- |
| `help`, `version`, `doctor` | Ayuda, versión y comprobaciones locales. `COMANDO --help` muestra sus opciones. |
| `config` | Lee la configuración local. Con `--account-id --zone-id --zone --api-token-file` reemplaza los cuatro campos. |
| `list` | Lista el inventario local, inclusive las operaciones pendientes. |
| `create --name N --hostname FQDN --url ORIGEN` | Crea túnel, ingress, DNS y credencial. Añade `--start` para arrancar inmediatamente. |
| `status --name N`, `inspect --name N` | Estado local, túnel y conectores en Cloudflare, DNS, origen y candidatos de recuperación. |
| `start --name N`, `stop --name N`, `restart --name N` | Controlan el conector local y su habilitación tras reinicios. |
| `expire --name N` | Vence y detiene el conector inmediatamente. |
| `renew --name N` | Cambia el vencimiento con `--expires-in`, `--expires-at` o `--persistent`. Tras vencer se requiere `start` para volver a publicar. |
| `logs --name N [--lines 50]` | Lee el journal del conector local. |
| `audit` | Enumera candidatos remotos sin registro local para la cuenta y la zona configuradas. |
| `delete --name N [--yes]` | Verifica recursos y dependencias, detiene el conector y elimina recursos registrados. |
| `recover --name N` | Reanuda una creación. Para reanudar una eliminación, confirma igual que con `delete`. |

`--expires-in` usa enteros positivos seguidos de `s`, `m`, `h` o `d`. `--expires-at` requiere una fecha y hora con desplazamiento, por ejemplo `2027-01-01T09:00:00-03:00`. Al crear, omitir ambas opciones produce un túnel persistente. Las fechas del JSON son segundos Unix.

`--json` devuelve un documento JSON en stdout. `list` devuelve un arreglo de registros, `status` y `inspect` un objeto de diagnóstico, y los errores esperados un objeto con `error` y `code`. `doctor` y el temporizador devuelven sus comprobaciones incluso al fallar. La ayuda de argumentos de argparse mediante `--help` es textual. Para ayuda JSON general, usa `boring help --json`.

| Código | Significado |
| --- | --- |
| 0 | Operación completada |
| 2 | Argumentos o credenciales locales inválidos |
| 3 | Error de API o transporte remoto |
| 4 | Fallo local, requisitos ausentes o respuesta inesperada |
| 5 | Conflicto, confirmación ausente o estado pendiente |
| 130 | Interrupción del operador |

## Diagnóstico

El campo `service_state` viene de systemd. `connector_connected` usa el endpoint dedicado de conexiones en Cloudflare y abarca las réplicas que Cloudflare reporta. `tunnel_exists` y `tunnel_status` describen el túnel remoto. `dns_configured` compara el registro guardado con el CNAME proxied esperado en la API. La propagación y el TLS de edge se comprueban aparte. `origin` intenta HEAD con TLS normal, sin redirecciones ni credenciales. Una respuesta 2xx indica éxito de esa comprobación, una 5xx indica fallo y otras respuestas dejan la salud indeterminada. `public_access: not_verified` requiere un ensayo desde una red externa con una identidad de prueba. Verifica acceso permitido y denegado si hay una política de Access.

Los conectores se reinician ante fallos del proceso. El inicio evalúa el estado y la caducidad leídos del disco, incluso tras reiniciar el host. El timer reevalúa cada 15 segundos después del ciclo anterior. Una operación bajo el bloqueo local, el reloj del host o un fallo del supervisor puede demorar la detención. `expire` conserva el CNAME y los recursos remotos. En una emergencia detén todas las réplicas del conector y revisa el estado desde Cloudflare.

## Recuperación

- Una creación repetida con los mismos valores de identidad y origen retoma el registro. Para cambiar caducidad o estado operativo después, usa `renew`, `start` o `stop`.
- Tras un POST de túnel de resultado incierto, ejecuta `inspect` y verifica el UUID con el historial de Cloudflare, la cuenta y la hora de la operación. Si confirmas que es el túnel creado por esa solicitud, ejecuta `boring recover --name N --adopt-tunnel UUID --yes`. Si verificas que la solicitud anterior terminó sin crearlo, ejecuta `boring recover --name N --retry-create --yes`. Haz estas comprobaciones antes de autorizar un nuevo POST.
- Tras perder una respuesta DNS, Boring reanuda únicamente con el CNAME, destino y marcador esperados. Un DNS ajeno bloquea la creación. Retira un conflicto mediante el procedimiento habitual del propietario de la zona.
- La eliminación revisa el UUID registrado, el nombre remoto y el ámbito, el registro DNS y su marcador, las demás referencias CNAME en la zona, el ingress y las rutas privadas. Confirma que no hay balanceadores, otras zonas ni sistemas externos que dependan del túnel y evita cambios en paralelo desde el dashboard. Si Cloudflare sigue reportando conexiones al detener systemd, espera que converja o detén las réplicas adicionales antes de repetir. Con el estado `deleting`, usa `boring recover --name N --yes` para continuar.
- Si un túnel fue borrado fuera de Boring, `inspect` puede marcar su DNS huérfano. Usa `delete` para el registro que aún coincida. Si perdiste el inventario, restaura primero un respaldo privado. `audit` informa candidatos y exige verificar su procedencia por separado.
- Para revisar fallos del timer usa `journalctl -u boring-expiry.service`. Las operaciones exitosas se registran con el identificador de syslog `boring`.

## Ensayo de aceptación con autorización

En una VM Debian y una zona de pruebas, levanta dos orígenes HTTP de prueba en puertos elegidos por el operador. Crea dos túneles, inicia ambos y comprueba cada hostname desde otra máquina. Detén uno y comprueba que el otro sigue sirviendo. Configura un vencimiento cercano, reinicia la VM y verifica que un túnel vencido queda detenido. Simula una interrupción de red a la API en un ensayo aislado y reconcilia el registro. Finalmente elimina ambos con confirmación y verifica en el dashboard que desaparecieron los CNAME y los túneles del ensayo.

El ensayo automatizado de este repositorio cubre el reinicio como reconstrucción del inventario y evaluación de la condición de arranque. La prueba con reinicio de VM, Cloudflare real y propagación de DNS queda pendiente de autorización.

## Resultado local

En Debian 13 se ejecutaron 20 pruebas automatizadas con dobles locales. Pasaron la comprobación de sintaxis de los tres scripts Bash y la validación de las tres unidades con `systemd-analyze verify` 257 en un directorio temporal, usando las rutas de los ejecutables disponibles. `cloudflared` 2026.11.0 anunció soporte de `--token-file`. El entorno de prueba carece de systemd como PID 1. Consulta el ensayo de aceptación para validar el comportamiento operativo en una VM.
