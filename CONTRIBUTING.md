# Contribuir

Gracias por ayudar a mejorar Sentinel. Antes de abrir un cambio grande, crea un issue para conversar sobre el alcance.

## Preparar el entorno

Se requiere Windows y Python 3.12 para usar el iniciador incluido.

```powershell
git clone https://github.com/Brasther/sentinel-discord-bot.git
cd sentinel-discord-bot
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

No hace falta configurar un token para trabajar en el motor, la interfaz o la demo. Nunca subas `.env`, tokens, logs, bases de datos ni datos reales de Discord.

## Ejecutar la demo

```powershell
$env:SENTINEL_PORT = '5001'
py -3.12 iniciar_sentinel.py --preview
```

Abre `http://localhost:5001`.

## Verificar cambios

Antes de abrir un pull request, ejecuta la suite existente:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Describe qué cambió, cómo se puede reproducir y qué comportamiento observaste. Para cambios de audio, incluye ejemplos sintéticos o pasos de reproducción; no publiques audio privado de usuarios.

## Pull requests

- Mantén cada cambio enfocado y explica el motivo en lenguaje claro.
- Actualiza la documentación cuando cambie una opción, permiso o paso de instalación.
- No agregues dependencias sin explicar por qué hacen falta.
- No incluyas datos generados localmente ni archivos compilados.
