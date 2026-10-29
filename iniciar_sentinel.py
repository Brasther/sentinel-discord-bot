"""Arranque local de Sentinel con su entorno de dependencias verificado."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parent
DEPENDENCIES = {
    "discord": "discord.py",
    "discord.ext.voice_recv": "discord-ext-voice-recv",
    "numpy": "numpy",
    "scipy": "scipy",
    "flask": "flask",
    "flask_socketio": "flask-socketio",
    "colorama": "colorama",
    "requests": "requests",
    "webrtcvad": "webrtcvad-wheels",
    "dotenv": "python-dotenv",
    "nacl": "PyNaCl",
    "davey": "davey",
}


def main():
    os.chdir(ROOT)
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Este iniciador requiere Python 3.12, la versión verificada para estas dependencias.")
    source = (ROOT / "main.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename="main.py")
    env_file = ROOT / '.env'
    env_token = os.getenv('DISCORD_TOKEN', '')
    if env_file.exists():
        for line in env_file.read_text(encoding='utf-8-sig').splitlines():
            if line.startswith('DISCORD_TOKEN='):
                env_token = line.partition('=')[2].strip().strip('\"\'')
    if 'os.getenv' in source:
        placeholder = not env_token or env_token == 'TU_TOKEN_AQUI'
    else:
        placeholder = False
    placeholder = placeholder or any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "TOKEN" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and node.value.value == "TU_TOKEN_AQUI"
        for node in tree.body
    )
    if "--check" in sys.argv:
        for filename in ['sentinel_core.py', 'sentinel_audio.py', 'sentinel_runtime.py', 'voice_compat.py', 'templates/dashboard.html', 'static/dashboard.css',
                         'static/dashboard.js', 'static/vendor/socket.io.min.js']:
            if not (ROOT / filename).is_file():
                raise RuntimeError('Falta un archivo del proyecto: ' + filename)
        print("OK: Python 3.12, carpeta de Sentinel y sintaxis de main.py.")
        if placeholder:
            print("Pendiente: configurar DISCORD_TOKEN en .env para conectar a Discord.")
        return
    if placeholder and '--preview' not in sys.argv:
        raise RuntimeError("Configura DISCORD_TOKEN en el archivo .env.")
    if '--preview' in sys.argv:
        # Permite volver a abrir el lanzador mientras la demo ya está funcionando.
        import json
        import urllib.request
        address = 'http://127.0.0.1:' + os.getenv('SENTINEL_PORT', '5000')
        try:
            with urllib.request.urlopen(address + '/api/status', timeout=1) as response:
                if json.load(response).get('preview'):
                    print('La demostracion ya esta disponible en ' + address, flush=True)
                    return
        except (OSError, ValueError):
            pass

    environment = ROOT / ".venv"
    interpreter = environment / "Scripts" / "python.exe"
    if not interpreter.exists():
        print("Preparando el entorno de Python para Sentinel...", flush=True)
        venv.create(environment, with_pip=True)
    check = (
        "import importlib.util, json; "
        "names=" + repr(list(DEPENDENCIES)) + "; "
        "missing=[]\n"
        "for name in names:\n"
        " try:\n"
        "  if importlib.util.find_spec(name) is None: missing.append(name)\n"
        " except ModuleNotFoundError: missing.append(name)\n"
        "print(json.dumps(missing))"
    )
    import json
    missing = json.loads(subprocess.check_output([str(interpreter), "-c", check], text=True))
    if missing:
        print("Instalando dependencias; la primera vez puede tardar unos minutos...", flush=True)
        requirements = ROOT / 'requirements.txt'
        packages = ['-r', str(requirements)] if requirements.exists() else [DEPENDENCIES[name] for name in missing]
        subprocess.check_call([str(interpreter), "-m", "pip", "install", *packages])
    print('Encendiendo Sentinel. Dashboard: http://localhost:' + os.getenv('SENTINEL_PORT', '5000'), flush=True)
    # En Windows execv puede terminar el iniciador antes que el nuevo proceso.
    # Esperar aqui mantiene la consola y transmite el resultado al archivo CMD.
    result = subprocess.call([str(interpreter), "-u", str(ROOT / "main.py"), *(['--preview'] if '--preview' in sys.argv else [])])
    sys.exit(result)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print("Sentinel no pudo iniciarse: " + str(error), file=sys.stderr)
        sys.exit(1)
