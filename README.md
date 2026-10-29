# 🛡️ Sentinel

### Control de ruido para canales de voz de Discord, con panel local en español.

Sentinel analiza el audio recibido en un canal de voz y muestra niveles, alertas y herramientas de moderación en un panel web. Incluye una demo para explorar el panel sin conectar un bot.

> **Empieza en modo de observación.** El análisis puede equivocarse y no sabe distinguir una voz humana de audio reproducido por una TV, teléfono o aplicación. Revisa el comportamiento antes de habilitar medidas automáticas.

**Windows · Python 3.12 · Discord**

---

## ⚡ Inicio rápido

1. Descarga el proyecto: **[Code → Download ZIP](https://github.com/Brasther/sentinel-discord-bot/archive/refs/heads/main.zip)** y descomprímelo.
2. Instala [Python 3.12](https://www.python.org/downloads/windows/). Si ya tienes Python Install Manager, puedes instalarlo desde una terminal con `py install 3.12`.
3. Crea tu bot en el [Discord Developer Portal](https://discord.com/developers/applications). En **Bot**, activa **Message Content Intent** y copia el token.
4. Haz doble clic en **`Iniciar_Sentinel.cmd`**. La primera vez te pedirá el token de forma privada y creará el archivo local `.env`.
5. Espera mientras instala las dependencias. Cuando aparezca el mensaje de inicio, abre **http://localhost:5000**.
6. En Discord, entra a un canal de voz y escribe **`!vigilar`** en un canal de texto.

¿Primera vez creando un bot? Sigue la [instalación paso a paso con imágenes y detalles](docs/INSTALACION_WINDOWS.md).

> El token no se muestra mientras lo escribes. No lo publiques ni lo envíes a nadie. `.env` está excluido de Git.

## ✨ Qué incluye

- **Panel local:** estado del canal, niveles de audio, historial y controles por persona.
- **Calibración:** ajusta el umbral al ambiente del canal.
- **Modo de observación:** analiza el audio sin aplicar nuevos castigos.
- **Demo segura:** usa `Ver_Panel_Demo.cmd` y abre **http://localhost:5001**. No requiere token ni se conecta a Discord.
- **Recuperación de voz:** intenta reconectar ante cortes inesperados.

El audio se procesa localmente y Sentinel no lo guarda ni lo envía a servicios externos. Discord transporta el audio hasta el bot para su análisis.

## 🎮 Primer uso

1. Entra a un canal de voz.
2. En un canal de texto, escribe `!vigilar`. Quien lo ejecuta necesita el permiso **Silenciar miembros**.
3. En el panel, empieza en **Solo observar** y calibra con el canal en silencio.
4. Revisa las alertas y ajusta la sensibilidad antes de activar castigos.
5. Para desconectar el bot, escribe `!salir`.

Para usar medidas de moderación, asigna al bot solo los permisos que necesite en tu servidor. Lee [cómo invitar y configurar el bot](docs/INSTALACION_WINDOWS.md#3-crea-e-invita-tu-bot-a-discord).

## 🔐 Seguridad y privacidad

- Mantén `DISCORD_TOKEN` privado. Si se filtra, regénéralo desde Discord Developer Portal.
- El archivo `.env`, bases SQLite, registros, respaldos y entorno virtual se ignoran en Git.
- El panel escucha en `127.0.0.1`; no lo expongas a Internet. No tiene inicio de sesión para acceso remoto.
- Usa Sentinel únicamente en servidores donde tengas autorización para moderar.

## 🧰 Configuración

La primera ejecución de `Iniciar_Sentinel.cmd` abre el asistente si todavía no configuraste el token. Para cambiarlo después, ejecuta **`Configurar_Sentinel.cmd`**.

| Opción | Para qué sirve | Valor predeterminado |
| --- | --- | --- |
| Token del bot | Conectar Sentinel a Discord | Obligatorio |
| ID del propietario | Añadir tu cuenta a la lista protegida | Opcional |
| ID del canal de registros | Enviar eventos al canal elegido | Opcional |
| Dirección del panel | Equipo donde escucha el panel | `127.0.0.1` |
| Puerto del panel | Puerto local | `5000` |

El asistente solicita el token sin mostrarlo en pantalla. También puedes editar `.env` manualmente; las variables están descritas en [`.env.example`](.env.example).

## 🩹 Ayuda rápida

| Problema | Qué hacer |
| --- | --- |
| `py` no se reconoce | Instala Python 3.12 desde el enlace oficial y vuelve a abrir la consola. |
| El bot no inicia sesión | Vuelve a generar el token y ejecuta `Configurar_Sentinel.cmd`. |
| `PrivilegedIntentsRequired` | Activa **Message Content Intent** en la sección **Bot** del Developer Portal. |
| `!vigilar` no responde | Comprueba el intent, los permisos del canal de texto y el permiso **Silenciar miembros** de quien usa el comando. |
| No entra a voz | Revisa **Ver canal** y **Conectar** para el bot y confirma que haya alguien en el canal. |
| No abre `localhost:5000` | Mantén abierta la ventana de Sentinel y comprueba que el puerto no esté ocupado. |

Más soluciones: [guía de instalación y solución de problemas](docs/INSTALACION_WINDOWS.md#solución-de-problemas).

## 📚 Documentación

- [Instalación completa en Windows](docs/INSTALACION_WINDOWS.md)
- [Motor y comportamiento del audio](AUDIO.md)
- [Cambios del proyecto](CHANGELOG.md)
- [Cómo contribuir](CONTRIBUTING.md)
- [Política de seguridad](SECURITY.md)

## 🧑‍💻 Desarrollo

Se requiere Python 3.12. Para instalar dependencias y ejecutar las pruebas:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 📄 Licencia

Este proyecto aún no tiene una licencia. Puedes descargarlo y consultarlo; no se han concedido permisos generales para reutilizarlo, modificarlo o redistribuirlo.
