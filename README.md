# Sentinel — bot de moderación de audio para Discord

Sentinel escucha el audio de un canal de voz y ofrece un panel web local para revisar niveles, calibrar sensibilidad y administrar medidas de moderación. Incluye un modo de demostración que funciona sin iniciar sesión en Discord.

> **Estado:** proyecto personal compartido para descarga. El bot y su detección de audio pueden cometer errores. Empieza en **Solo observar**, revisa los resultados en tu servidor y configura las medidas con criterio. Sentinel no identifica si el audio viene de una persona, un teléfono, una TV o una aplicación.

## Funciones

- Detección de ruido en audio recibido por Discord y controles por usuario.
- Panel en español en `http://localhost:5000`.
- Calibración y sensibilidad ajustable; configuración guardada localmente en SQLite.
- Modos de observación y de moderación configurable.
- Reconexión y recuperación ante fallos de voz.
- Panel de demostración en `http://localhost:5001`, sin conectar a Discord.

El audio se procesa localmente y Sentinel no lo graba ni lo envía a servicios externos. Discord sí transporta el audio hasta el bot para que pueda analizarlo.

## Requisitos

- Windows 10/11 (la ruta de inicio incluida está preparada para Windows).
- Python **3.12** y el lanzador `py.exe`.
- Una aplicación/bot creado en [Discord Developer Portal](https://discord.com/developers/applications).
- Un servidor de Discord donde puedas añadir el bot y configurar permisos.
- Conexión a Internet la primera vez para instalar dependencias.

## Instalación

1. Descarga el ZIP desde el botón **Code → Download ZIP** de GitHub y descomprímelo en una carpeta.
2. Instala Python 3.12. En Windows, comprueba en una terminal que `py -3.12 --version` muestre Python 3.12.
3. En Discord Developer Portal, crea una aplicación y añade un bot. Copia el token del bot; mantenlo privado y no lo publiques.
4. En **Bot → Privileged Gateway Intents**, activa **Message Content Intent**. Sentinel usa comandos de texto con prefijo `!`.
5. En OAuth2 → URL Generator, selecciona el scope `bot` y genera una invitación. Concede los permisos necesarios: ver canales, enviar mensajes, leer el historial, conectarse y ver los canales de voz, silenciar miembros y mover miembros. Instala el bot en tu servidor.
6. Copia `.env.example` como `.env` y edita `.env` con el Bloc de notas. Define al menos `DISCORD_TOKEN` con el token del bot. Los IDs de propietario y de canal de registros son opcionales; consulta la sección **Configuración**.
7. Haz doble clic en `Iniciar_Sentinel.cmd`. El primer inicio crea `.venv` e instala las dependencias; puede tardar unos minutos.
8. Abre `http://localhost:5000` en el mismo equipo donde corre Sentinel.

El token se guarda solo en `.env`, que está excluido de Git. Si el token se publicó por accidente, regenéralo en Discord Developer Portal antes de volver a iniciar.

## Primer uso

1. Entra al canal de voz que quieres supervisar.
2. En un canal de texto escribe `!vigilar`. Quien ejecuta el comando necesita el permiso **Silenciar miembros**.
3. Mantén el panel en **Solo observar** mientras compruebas el comportamiento y calibra el ruido de fondo en silencio.
4. Revisa sensibilidad, permisos y resultados antes de habilitar medidas automáticas.
5. Para detener el seguimiento, escribe `!salir` en un canal de texto. El comando también requiere **Silenciar miembros**.

Las medidas pueden silenciar o mover miembros según la configuración del panel y los permisos concedidos. Usa Sentinel solo en servidores donde tengas autorización para moderar.

## Demostración local

Haz doble clic en `Ver_Panel_Demo.cmd` y visita `http://localhost:5001`. Muestra datos simulados en una base separada y no se conecta a Discord ni aplica sanciones.

## Configuración (`.env`)

| Variable | Valor inicial | Uso |
| --- | --- | --- |
| `DISCORD_TOKEN` | Obligatorio | Token privado del bot de Discord. |
| `SENTINEL_HOST` | `127.0.0.1` | Dirección del panel. Déjala local para que solo sea accesible desde este equipo. |
| `SENTINEL_PORT` | `5000` | Puerto del panel real. |
| `SENTINEL_OWNER_ID` | `0` | ID de Discord que se agrega como propietario/inmune. Opcional. |
| `SENTINEL_LOG_CHANNEL_ID` | `0` | ID del canal donde enviar registros. Opcional. |

Para obtener un ID de Discord, activa **Modo desarrollador** en Ajustes → Avanzado y usa el menú contextual **Copiar ID**. No expongas el panel en Internet: no tiene autenticación para acceso remoto.

## Archivos de datos

El bot crea localmente `sentinel.db`, `logs/`, `.venv/` y archivos auxiliares. Estos archivos están excluidos de Git y no se necesitan para instalar. No compartas `.env` ni registros que puedan contener datos de tu servidor.

## Solución de problemas

- **Python no encontrado:** instala Python 3.12 y confirma que el lanzador `py.exe` esté disponible.
- **Token rechazado:** genera un token nuevo en Discord Developer Portal y actualiza `DISCORD_TOKEN` en `.env`.
- **PrivilegedIntentsRequired:** activa **Message Content Intent** en la página Bot del Developer Portal.
- **No entra al canal:** revisa que el bot pueda ver el canal y tenga permisos para conectarse; revisa también que haya alguien en voz.
- **`!vigilar` no responde:** el bot necesita Message Content Intent y permiso para ver el canal de texto, leer mensajes y enviar respuestas.
- **El panel no abre:** confirma que Sentinel sigue corriendo y que el puerto 5000 no esté ocupado.

## Desarrollo

Dependencias fijadas para Python 3.12 en [`requirements.txt`](requirements.txt). Pruebas automatizadas disponibles en `tests/`:

```powershell
py -3.12 -m unittest discover -s tests -v
```

Descripción técnica del motor de audio: [`AUDIO.md`](AUDIO.md).

## Licencia

Este repositorio todavía no incluye una licencia. El código está disponible para descargar y consultar en GitHub; no se han definido permisos generales para reutilizarlo, modificarlo o redistribuirlo.
