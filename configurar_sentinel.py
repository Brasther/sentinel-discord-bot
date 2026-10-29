"""Configuración interactiva y local del token y opciones de Sentinel."""
from getpass import getpass
import os
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
ENV_TEMPLATE = ROOT / ".env.example"


def current_value(text, key, fallback):
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", text, re.MULTILINE)
    if not match:
        return fallback
    return match.group(1).strip().strip("\"'")


def ask_id(label, current):
    while True:
        value = input(f"{label} [{current}]: ").strip() or current
        if value == "0" or (value.isdecimal() and len(value) <= 20):
            return value
        print("Escribe un ID numérico de Discord o deja el valor sugerido.")


def main():
    print("=" * 58)
    print("Configuración de Sentinel")
    print("=" * 58)
    print("El token se escribirá solo en este equipo, dentro de .env.")
    print("No lo pegues en GitHub, Discord ni en una conversación.\n")

    existing = ENV_FILE.read_text(encoding="utf-8-sig") if ENV_FILE.exists() else ""
    if not existing and ENV_TEMPLATE.exists():
        existing = ENV_TEMPLATE.read_text(encoding="utf-8-sig")

    token = ""
    while not token:
        token = getpass("Pega el token del bot (no se mostrará mientras escribes): ").strip()
        if not token:
            print("El token es obligatorio para conectar el bot a Discord.")

    owner_id = ask_id(
        "Tu ID de Discord para dejar tu cuenta protegida",
        current_value(existing, "SENTINEL_OWNER_ID", "0"),
    )
    log_channel_id = ask_id(
        "ID del canal para enviar registros (opcional; 0 para omitir)",
        current_value(existing, "SENTINEL_LOG_CHANNEL_ID", "0"),
    )
    host = current_value(existing, "SENTINEL_HOST", "127.0.0.1")
    port = current_value(existing, "SENTINEL_PORT", "5000")

    contents = (
        "# Configuración privada de Sentinel. No compartas ni publiques este archivo.\n"
        f"DISCORD_TOKEN={token}\n"
        f"SENTINEL_HOST={host}\n"
        f"SENTINEL_PORT={port}\n"
        f"SENTINEL_OWNER_ID={owner_id}\n"
        f"SENTINEL_LOG_CHANNEL_ID={log_channel_id}\n"
    )
    temporary = ROOT / ".env.tmp"
    temporary.write_text(contents, encoding="utf-8", newline="\n")
    os.replace(temporary, ENV_FILE)
    print("\nListo: guardé la configuración en .env (archivo privado).")
    if "--continue" in sys.argv:
        print("Continuando con el inicio de Sentinel...")
    else:
        print("Ahora abre Iniciar_Sentinel.cmd para encender Sentinel.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nConfiguración cancelada. No se guardaron cambios.")
        raise SystemExit(1)
