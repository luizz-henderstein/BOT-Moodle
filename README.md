# BOT Moodle — EVA UTEC

Bot de Telegram que supervisa la **Línea de tiempo de EVA UTEC/Moodle** y notifica una sola vez cuando aparece una tarea nueva.

Está pensado para ejecutarse permanentemente en Ubuntu Server. Conserva la sesión de Moodle, vuelve a autenticarse solamente cuando la sesión vence y guarda localmente los identificadores ya notificados.

## Funciones

- Detecta únicamente vencimientos de actividades `mod_assign`.
- No envía recordatorios ni repite tareas conocidas.
- La primera ejecución registra las tareas existentes sin notificarlas.
- Mantiene una cola persistente y reintenta si Telegram falla.
- Incluye el comando `/pendientes`.
- `/pendientes` muestra tareas no vencidas que vencen en los próximos 15 días, agrupadas por curso.
- Permite limitar las consultas automáticas a una ventana horaria.
- Funciona como servicio `systemd` y arranca después de reiniciar el servidor.

## Compatibilidad

El bot utiliza el método AJAX estándar de Moodle:

```text
core_calendar_get_action_events_by_timesort
```

La instalación debe usar el formulario normal de acceso de Moodle y exponer ese método a la sesión autenticada. Instalaciones con SSO, CAPTCHA, autenticación multifactor obligatoria o modificaciones institucionales pueden requerir adaptación.

## Instalación rápida en Ubuntu

Clona el repositorio y ejecuta:

```bash
chmod +x deploy/install-ubuntu.sh
sudo ./deploy/install-ubuntu.sh
sudo nano /etc/eva-telegram/eva-telegram.env
```

Configura:

```dotenv
EVA_USERNAME=usuario_de_moodle
EVA_PASSWORD=contraseña_de_moodle
TELEGRAM_BOT_TOKEN=token_completo_del_bot
TELEGRAM_CHAT_ID=id_del_chat
EVA_BASE_URL=https://ev1.utec.edu.uy/moodle/
```

No confirmes al repositorio el archivo real de configuración, cookies ni tokens.

Activa el servicio:

```bash
sudo systemctl enable --now eva-telegram
sudo journalctl -u eva-telegram -f
```

El primer ciclo crea la línea base sin mandar las tareas antiguas.

## Configuración

| Variable | Valor inicial | Descripción |
|---|---:|---|
| `EVA_BASE_URL` | EVA UTEC | URL raíz de Moodle, terminada en `/` |
| `EVA_TIMEZONE` | `America/Montevideo` | Zona horaria para fechas y horario activo |
| `EVA_LOOKBACK_DAYS` | `30` | Ventana de eventos recientes |
| `CHECK_INTERVAL_SECONDS` | `600` | Frecuencia automática |
| `ACTIVE_START_TIME` | `17:00` | Inicio de consultas automáticas |
| `ACTIVE_END_TIME` | `23:30` | Fin de consultas automáticas |
| `TELEGRAM_POLL_SECONDS` | `20` | Espera para recibir comandos |
| `EVA_DATA_DIR` | `data/` | Directorio de estado |

## Seguridad

- Los secretos se cargan desde un archivo externo con permisos restringidos.
- La cookie de Moodle se guarda con modo `0600`.
- El servicio se ejecuta con un usuario sin privilegios.
- Solo el `TELEGRAM_CHAT_ID` configurado puede ejecutar comandos.
- El repositorio no debe contener contraseñas, cookies, tokens ni archivos de estado.

Usa este proyecto únicamente con cuentas propias o con autorización expresa. Revisa las políticas de tu institución antes de automatizar consultas.

## Pruebas

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## Licencia

MIT. Consulta [LICENSE](LICENSE).
