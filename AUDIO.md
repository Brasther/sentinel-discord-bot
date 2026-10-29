# Motor de audio de Sentinel

El audio se procesa localmente en bloques de 20 ms a 48 kHz. No se graba ni se envía a servicios externos.

## Uso

1. Entra en un canal de voz y escribe `!vigilar` en un canal de texto del servidor. El bot necesita Ver canal y Conectar, además de Silenciar miembros y Mover miembros para los castigos.
2. El modo automático solo aprende como fondo niveles casi silenciosos (hasta 5,5 RMS para absorber el redondeo PCM). El audio audible ya puede acumular puntos desde el inicio.
3. Para afinar el resultado, pulsa **Ajustar ruido automáticamente** y mantengan silencio durante cinco segundos. Si Discord no transmite suficientes muestras, conserva el ajuste anterior.
4. Observa **Detalles de audio**: volumen RMS, fondo, umbral efectivo, saturación y segundos de audio continuo. La explicación de la tarjeta indica por qué se acumulan o no infracciones.
5. Empieza en **Solo observar** si quieres verificar tu ambiente antes de aplicar castigos.

## Modos

**Ruido: proteger las voces**, por defecto, confirma ruido repetido y protege el habla, incluyendo pausas breves. El ruido confirmado suma 8 puntos por bloque de 20 ms (9 tras el límite heredado de ruido continuo); un transitorio percusivo aislado suma 4 y la voz resta 1. El silencio resta 5. Audio audible que se mantiene durante 5 segundos se penaliza aunque el VAD lo clasifique como voz. Esto también puede penalizar habla humana prolongada: el detector no puede determinar el origen del audio. El cooldown de cinco segundos conserva el mecanismo existente.

Los picos cortos se comparan con el nivel RMS filtrado. Un pico aislado queda en análisis; tres o más ataques pronunciados dentro de 1,2 segundos se clasifican como clics o golpes repetidos y pueden acumular infracciones. Se conserva la gracia configurada por usuario y la protección de voz confirmada. Es una detección por forma y repetición de la señal, no una identificación del objeto que produjo el sonido.

**Limitar audio continuo (incluye voces)** permite controlar música, TV o TikTok que mantengan un sonido audible durante el tiempo configurado (15 segundos por defecto, entre 5 y 120). Tras ese tiempo, los bloques audibles suman infracciones aunque contengan voz, salvo que la tarjeta individual tenga activada la protección de voz. Una pausa de medio segundo interrumpe el contador. **También puede castigar a una persona que hable mucho tiempo**: es un límite de duración, no un reconocimiento del origen del sonido. Los ruidos sin voz siguen acumulando puntos antes de ese límite.

WebRTC VAD detecta presencia de voz, no determina si proviene de una persona, un altavoz o un teléfono. Ningún modo afirma identificar TikTok ni garantizar cero falsos positivos.

## Sensibilidad y calibración

- Sensibilidad de penalización por usuario: se ajusta desde su tarjeta en Inicio y queda guardada en SQLite por ID de Discord, incluso al salir y volver. La escala es 0–100: 50 conserva el umbral anterior, valores mayores bajan el umbral (más sensible) y valores menores lo suben (más tolerante). El ajuste multiplica el umbral tanto en modo automático como manual; el valor efectivo aparece en Detalles de audio.
- En cada tarjeta se puede aplicar un perfil **Tolerante**, **Normal** o **Estricto** y luego personalizar los controles: umbral RMS (0 hereda el automático/general), velocidad de acumulación (normal, 2× o 3×), gracia de 0–30 segundos para ruido sin voz protegida, duración individual del mute (0 hereda el ajuste general) y protección de voz. La opción de voz protegida conserva una ventana de 5 segundos para audio persistente en el modo de ruido. Los perfiles comienzan con 5 s, 2 s y 0 s de gracia, respectivamente; Tolerante usa mute de 5 s, Normal hereda la duración general y Estricto usa mute de 20 s. Se guardan por ID de Discord en SQLite. Estricto desactiva la protección de voz y puede castigar habla humana; el detector no identifica el origen del audio.

- Automática: solo aprende niveles casi silenciosos (hasta 5,5 RMS para absorber el redondeo PCM), tanto al inicio como después; un sonido audible no se incorpora como fondo. El umbral efectivo usa el mayor de 5 RMS, 1,6 veces el fondo y fondo + 5 RMS (máximo 10.000). El umbral manual guardado no se usa en este modo. Una señal fuerte con menos de 0,5 % de energía en la banda se descarta como fuga del filtro de sonidos graves.
- Manual: usa el RMS configurado (5 a 10.000), sin adaptación. Un valor demasiado bajo puede castigar la estática habitual.
- Calibración: usa el percentil 90 del fondo y un margen proporcional. Se necesitan al menos 25 bloques válidos sin voz. Reinicia los puntos y actualiza el fondo de cada micrófono con sus propias muestras. El umbral manual se calcula sobre las muestras del canal.
- La calibración manual aún puede tomar un ambiente ruidoso como referencia: ejecútala en silencio si quieres detectar ese sonido.

La medición RMS muestra el audio de banda completa sin offset DC. Los castigos y la calibración usan el RMS tras el filtro 300–4.000 Hz. La normalización del VAD solo mejora el análisis de señales bajas y no modifica el RMS que decide los castigos.

## Recepción y recuperación

- Estéreo a mono usando el canal con mayor energía útil: evita cancelación por fase y pérdida de señal cuando solo un canal contiene el micrófono.
- Buffer de fragmentos por usuario, reiniciado tras pausas mayores de 500 ms; los puntos no dependen del agrupamiento de los paquetes.
- Paquetes corruptos o que no se pueden descifrar se descartan; no se fabrican silencios para justificar un buen estado del receptor.
- El audio reconstruido por FEC/PLC no genera infracciones ni entra en la calibración.
- Diagnóstico muestra audio decodificado, descartado, reconstruido y último error de DAVE/Opus.
- El watchdog y su reinicio nuclear siguen usando el límite original de 30 segundos. Discord puede no transmitir PCM durante silencios largos, por lo que esta regla todavía puede provocar reinicios durante un silencio real.
- El canal vigilado se guarda en el entorno del proceso para recuperarlo después del reinicio nuclear. `!salir` cancela esa recuperación. Al abrir una consola nueva, vuelve a usar `!vigilar`.
- Ante una desconexión inesperada, Sentinel empieza a reconectar inmediatamente y hace dos reintentos breves sin reiniciar el proceso. Si el canal anterior ya no tiene personas, prioriza el destino reciente de quienes se movieron; después busca otro canal de voz ocupado. Puedes activar o desactivar esta defensa con **Volver si lo desconectan** en la web. Al apagarla, Sentinel respeta la desconexión; Discord sigue controlando el tiempo del saludo de voz, así que el bot no puede garantizar una desconexión menor a un segundo ni impedir que un moderador lo desconecte.
- Si un moderador mueve directamente a Sentinel, el bot sigue el nuevo canal. Si las personas se cambian de canal, Sentinel agrupa los eventos durante 80 ms y conserva el destino ocupado más reciente, incluso mientras ya se está trasladando. El traslado espera hasta 2,5 segundos la confirmación de Discord; si falla durante una cadena de cambios, actualiza el destino y reintenta sobre el mismo cliente para evitar sesiones de voz duplicadas. Si alguien permanece en el canal vigilado, Sentinel se queda allí. `!salir` cancela el seguimiento y conserva la salida voluntaria.
- Con **Entrada automática a voz** activada, Sentinel se conecta al iniciar si ya hay personas y también cuando la primera persona entra después. Al activar el interruptor en la web, busca canales ocupados de inmediato. `!salir` pausa esta entrada en el servidor hasta usar `!vigilar` o volver a activar la opción.
- Si la entrada automática falla, el botón **Conectar a voz** del panel busca un canal ocupado y permite iniciar la escucha manualmente; indica si no hay nadie en voz o si faltan permisos.
- Sentinel revisa `sentinel_audio.py` mientras está conectado. Cuando detecta una edición guardada y estable, carga el motor nuevo, conserva el puntaje y el fondo por usuario y cambia los motores activos sin desconectar el cliente de voz. Si el archivo nuevo tiene un error, mantiene el motor anterior. Las actualizaciones de módulos distintos requieren todavía reiniciar el proceso.
- Cada versión nueva de `AudioEngine` debe implementar `migrate_state_from(previous)` para conservar el puntaje, el fondo y el perfil al actualizarse en caliente. La primera implementación de esta función ya está incluida.

## Verificación

Pruebas con `python -m unittest discover -s tests -q`: RMS/int16, estéreo con fase invertida, señales bajas, offset DC, saturación, VAD real con ruido sintético, calibración, fragmentos, pausas DTX, modos, inmunidad, cooldown, cleanup de conexión y recuperación de mute ante errores.

Medición local de 1.200 bloques sintéticos: 0,245 ms de media y 0,310 ms en el percentil 95 por bloque de 20 ms. Es una medición de DSP en esta máquina, no una garantía de latencia de Discord o de rendimiento con muchos usuarios.

La suite completa pasó 50 pruebas. En la prueba real del 5 de octubre de 2026, Sentinel entró al canal de voz, detectó ruido, aplicó un mute a las 18:18:20 y lo retiró a las 18:18:30 (hora de Santiago). El umbral adaptativo observado fue 21,2 RMS, con fondo de 9,6 RMS. El modo de audio continuo se verificó con pruebas automatizadas y controles de la demo; la clasificación de TikTok concreto sigue dependiendo del audio que Discord transmita.

Fuentes: [WebRTC VAD](https://github.com/wiseman/py-webrtcvad), [recepción de voz](https://github.com/imayhaveborkedit/discord-ext-voice-recv). Dependencias fijadas en `requirements.txt`.
