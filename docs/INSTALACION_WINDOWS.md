# Instalación de Sentinel en Windows

Esta guía te lleva desde la descarga hasta el primer uso. No necesitas saber programar.

## Lo que necesitas

- Windows 10 o 11.
- Python 3.12.
- Un servidor de Discord donde puedas invitar bots y gestionar permisos.
- Internet durante la primera instalación, para instalar las dependencias.

## 1. Descarga y descomprime

1. Abre el [repositorio de Sentinel](https://github.com/Brasther/sentinel-discord-bot).
2. Pulsa **Code** y luego **Download ZIP**. También puedes [descargar el ZIP directamente](https://github.com/Brasther/sentinel-discord-bot/archive/refs/heads/main.zip).
3. En el Explorador de archivos, abre **Descargas**, haz clic derecho en el ZIP y selecciona **Extraer todo**.
4. Mueve la carpeta extraída a un lugar fácil de encontrar, por ejemplo `Documentos\Sentinel`.
5. No ejecutes Sentinel dentro de la vista previa del ZIP. Usa la carpeta ya extraída.

## 2. Instala Python 3.12

1. Abre la página oficial de [Python para Windows](https://www.python.org/downloads/windows/).
2. Instala Python 3.12 o el administrador oficial de Python. La [guía oficial de Python para Windows](https://docs.python.org/3.12/using/windows.html) explica las opciones de instalación.
3. Si usas el administrador de Python, puedes instalar la versión desde una terminal con:

   ```powershell
   py install 3.12
   ```

4. Para confirmar la instalación, abre **Símbolo del sistema** y ejecuta:

   ```powershell
   py -3.12 --version
   ```

   Debe aparecer `Python 3.12.x`. Si el comando `py` no existe, vuelve a instalar Python desde la página oficial y asegúrate de incluir el lanzador de Python.

## 3. Crea e invita tu bot a Discord

1. Abre el [Discord Developer Portal](https://discord.com/developers/applications) e inicia sesión.
2. Pulsa **New Application**, escribe un nombre y crea la aplicación.
3. En el menú **Bot**, crea o añade el bot si el portal te lo solicita.
4. En **Bot → Privileged Gateway Intents**, activa **Message Content Intent** y guarda los cambios. Sentinel necesita leer el comando `!vigilar`. Consulta la [documentación oficial de intents de Discord](https://discord.com/developers/docs/events/gateway#privileged-intents).
5. En la página **Bot**, pulsa **Reset Token** o **Copy Token** y guárdalo temporalmente en un lugar privado. Nunca publiques ese token.
6. En **OAuth2 → URL Generator**, marca el scope **bot**.
7. Elige los permisos del bot:

   **Necesarios para conectarse:**
   - View Channels (Ver canales)
   - Connect (Conectar)
   - Send Messages (Enviar mensajes)
   - Read Message History (Leer el historial de mensajes)

   **Para moderación opcional:**
   - Moderate Members / Mute Members (Silenciar miembros)
   - Move Members (Mover miembros)

8. Copia la URL generada, ábrela y selecciona tu servidor. Necesitas permiso para administrar o añadir aplicaciones a ese servidor.
9. Revisa los permisos solicitados y confirma la invitación.

No concedas **Administrator**; configura los permisos concretos de la lista. Quien use `!vigilar` también necesita el permiso de servidor **Silenciar miembros**.

Discord puede requerir revisión para intents privilegiados en aplicaciones que alcanzan sus umbrales de verificación. Sentinel requiere Message Content Intent porque utiliza comandos tradicionales con prefijo `!`. También puedes consultar el [tutorial oficial para crear tu primera aplicación de Discord](https://discord.com/developers/docs/getting-started).

## 4. Configura y enciende Sentinel

1. Abre la carpeta que extrajiste.
2. Haz doble clic en `Iniciar_Sentinel.cmd`.
3. Si falta la configuración, aparece el asistente. Pega el token en la línea indicada; los caracteres no se mostrarán al escribir. Pulsa **Enter**.
4. El asistente pregunta tu ID de Discord y el ID del canal de registros. Puedes dejar `0` y pulsar **Enter** para omitirlos.
5. El asistente guarda los datos privados en `.env`. Ese archivo no se sube a GitHub.
6. En el primer inicio Sentinel crea `.venv` e instala sus paquetes. Deja abierta la ventana mientras trabaja; la primera instalación puede tardar varios minutos.
7. Cuando diga que el panel está disponible, abre [http://localhost:5000](http://localhost:5000).

Para cambiar el token o los IDs más adelante, cierra Sentinel y ejecuta `Configurar_Sentinel.cmd`. El token anterior se reemplazará por el nuevo.

Para configurar esos IDs, activa **Modo desarrollador** en Discord → **Ajustes de usuario → Avanzado**. Después, abre el menú contextual de tu usuario y elige **Copiar ID** para `SENTINEL_OWNER_ID`; abre el menú contextual del canal de texto y elige **Copiar ID** para `SENTINEL_LOG_CHANNEL_ID`. Puedes omitir ambas opciones si no las necesitas.

## 5. Activa la escucha

1. Entra a un canal de voz del servidor.
2. En un canal de texto visible para el bot, escribe `!vigilar`.
3. En el panel web comprueba que Sentinel indique que está escuchando.
4. Deja **Solo observar** activado. Ajusta el ruido de fondo mientras nadie habla.
5. Observa el resultado antes de configurar castigos automáticos.
6. Para detener la escucha, escribe `!salir` en un canal de texto.

El bot debe tener permiso **Ver canal** y **Conectar** en el canal de voz. Para silenciar o mover miembros necesita los permisos de moderación correspondientes. Los permisos de canales pueden sobrescribir los permisos generales del servidor.

## Probar sin Discord

Haz doble clic en `Ver_Panel_Demo.cmd` y abre [http://localhost:5001](http://localhost:5001). La demo usa información inventada y no requiere token, invitación ni permisos. Para volver al bot real, cierra la demo y ejecuta `Iniciar_Sentinel.cmd`.

## Solución de problemas

### Windows dice que `py` no se reconoce

Instala Python desde [python.org](https://www.python.org/downloads/windows/), cierra las ventanas de terminal abiertas y prueba de nuevo `py -3.12 --version`.

### Discord dice `PrivilegedIntentsRequired`

Abre la aplicación en Developer Portal, entra en **Bot**, activa **Message Content Intent**, guarda y reinicia Sentinel.

### El token es rechazado

Cierra Sentinel, abre `Configurar_Sentinel.cmd`, pega el token vigente y vuelve a iniciar. Si el token quedó expuesto, primero regénéralo en Developer Portal.

### El comando `!vigilar` no responde

- Comprueba que Sentinel esté conectado a Discord y que Message Content Intent esté activado.
- Asegúrate de que el bot pueda ver el canal, enviar mensajes y leer el historial.
- Asegúrate de tener tú el permiso de servidor **Silenciar miembros**.

### Sentinel no puede entrar al canal de voz

Comprueba que haya alguien en el canal y que el bot tenga **Ver canal** y **Conectar**. Revisa también los permisos específicos del canal.

### La página no carga

Deja abierta la ventana de Sentinel. La dirección normal es `http://localhost:5000`; la demo usa `http://localhost:5001`. Si el puerto está ocupado, cierra la otra instancia antes de iniciar Sentinel.

### SmartScreen muestra una advertencia

El iniciador incluido es un archivo `.cmd` de texto y el proyecto se distribuye como código. Descarga el ZIP solo desde el [repositorio oficial](https://github.com/Brasther/sentinel-discord-bot). No ejecutes archivos de otros sitios que digan ser Sentinel.

## Seguridad y privacidad

- El token se guarda en `.env`, solo en tu equipo. No lo pegues en capturas, issues públicos, chats o commits.
- El panel local no tiene login. Mantén `SENTINEL_HOST=127.0.0.1` y no publiques el puerto en Internet.
- Sentinel procesa audio de Discord en memoria; no lo graba ni lo envía a otro servicio.
- El detector puede confundir habla larga con audio reproducido. Empieza con observación y revisa falsos positivos.
