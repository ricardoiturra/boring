# Seguridad

Classic publica una ruta pública de Cloudflare Tunnel. Antes de iniciar un conector para un servicio privado, aplica la autenticación de la aplicación y la política de Cloudflare Access que corresponda. El estado activo de systemd solo describe el proceso local.

La frontera de confianza es el host administrativo y el alcance del token Cloudflare. Ejecutar la CLI con root permite administrar unidades, credenciales e inventario. Los integradores deben usar un servicio intermediario o reglas de permisos de comandos cuidadosamente acotadas. Evita exponer el token y una CLI root general a agentes.

| Amenaza | Control y límite |
| --- | --- |
| Lectura de tokens | Directorios root privados, ficheros 0600, credencial de systemd para el conector, cabecera HTTPS para la API y redacción del token actual en la salida de logs. Protege respaldos y rota las credenciales si se exponen. |
| Sustitución o apropiación de DNS | Identificador, hostname, tipo, destino, proxy y marcador de propiedad. La comprobación de propiedad y la mutación remota son llamadas distintas. Usa una zona dedicada y evita ediciones concurrentes fuera de Boring. |
| Eliminación accidental | Confirmación explícita, UUID persistido, comprobaciones de dependencias y recuperación por etapas. Las referencias en balanceadores u otras zonas requieren revisión administrativa. |
| Intercepción de tráfico | TLS del sistema hacia la API y verificación del certificado de origen para HTTPS. Usa HTTP únicamente donde la red entre el conector y el origen sea de confianza, por ejemplo loopback. Si HTTPS usa una CA privada o un certificado con otro nombre, prepara la confianza y el nombre del origen antes de publicar. Classic solo configura ajustes de TLS predeterminados. |
| Conectores adicionales o token robado | La expiración controla el conector local. Revoca o rota el token de túnel en Cloudflare ante una exposición, revisa las réplicas y reprovisiona la credencial local antes de habilitarlo. |
| Caída durante cambios remotos | Inventario atómico y recuperación con confirmación para solicitudes inciertas. Respaldar estado y configuración delimita las acciones recuperables. |

Para notificar un incidente, usa un canal privado con el operador del despliegue. Incluye versión, comando y códigos de error con credenciales y datos de cuenta redactados.
