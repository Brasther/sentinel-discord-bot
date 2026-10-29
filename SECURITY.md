# Seguridad

La configuración y los datos del bot son locales. Mantén privados el token de Discord, el archivo `.env`, las bases de datos y los registros.

## Reportar una vulnerabilidad

No publiques tokens, credenciales ni instrucciones que permitan atacar una instalación en un issue público. Si GitHub muestra la opción **Report a vulnerability** en la pestaña **Security**, úsala para enviar un reporte privado. Si no aparece, abre un issue sin incluir secretos y pide un canal privado de contacto.

Si el token de un bot se filtra, regénéralo inmediatamente desde Discord Developer Portal y vuelve a guardarlo con `Configurar_Sentinel.cmd`.

El panel web no incluye autenticación para acceso remoto. Mantén `SENTINEL_HOST=127.0.0.1` y no expongas su puerto en Internet.
