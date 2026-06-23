# Форк zapret-discord-youtube для Linux

![Linux](https://img.shields.io/badge/Linux-nftables-2f6fed)
![Python](https://img.shields.io/badge/Python-3.x-3776ab)
![systemd](https://img.shields.io/badge/systemd-supported-4d4d4d)
![NFQUEUE](https://img.shields.io/badge/NFQUEUE-required-6f42c1)

Linux-лаунчер для стратегий `zapret-discord-youtube`. Он берет существующие `general*.bat` стратегии, переводит Windows-пути и переменные в Linux-окружение, ставит правила `nftables` и отправляет подходящий трафик в нативный `nfqws`.

Порт рассчитан на обычные desktop/server-дистрибутивы с `systemd`, `nftables` и `iproute2`: Arch, Fedora, Debian, Ubuntu, openSUSE и похожие системы. Для OpenRC/runit, OpenWrt, WSL, контейнеров и роутеров может понадобиться отдельная адаптация.

> [!IMPORTANT]
> Проект не содержит Linux-бинарник `nfqws`. Нужно установить или собрать нативный zapret отдельно, а затем указать путь к `nfqws` одним из способов ниже.

## Возможности

| | |
| --- | --- |
| Запуск стратегий | Использует любые `general*.bat` стратегии из корня репозитория |
| nftables | Создает отдельную таблицу `inet zapret_linux` |
| NFQUEUE | Передает TCP/UDP пакеты в очередь `nfqws` |
| systemd | Устанавливает автозапуск через `zapret-discord-youtube-linux.service` |
| Game Filter | Включает обработку портов выше `1023` для игр и приложений |
| IPSet Filter | Управляет режимами `loaded`, `none`, `any` для `lists/ipset-all.txt` |
| Диагностика | Проверяет зависимости, `nfqws`, WAN-интерфейс и текущие настройки |
| | |

## Требования

Нужны:

- `bash`
- `python3`
- `ip` из `iproute2`
- `nft` из `nftables`
- поддержка NFQUEUE в ядре
- `nfqws` из нативного zapret для Linux
- `systemd`, если нужен автозапуск через `service.sh`

Скрипт ищет `nfqws` в таком порядке:

1. переменная `NFQWS_BIN=/path/to/nfqws`
2. `bin/linux/nfqws`
3. `/opt/zapret/nfq/nfqws`
4. `nfqws` из `PATH`

Пример явного пути:

```bash
sudo NFQWS_BIN=/path/to/nfqws ./run.sh
```

⠀

# Использование

1. Установить зависимости своего дистрибутива: `python3`, `nftables`, `iproute2` и библиотеки для NFQUEUE.

2. Проверить окружение:

```bash
python3 linux/zapret_linux.py diagnostics
```

3. Посмотреть доступные стратегии:

```bash
python3 linux/zapret_linux.py list
```

4. Запустить стратегию вручную:

```bash
sudo ./run.sh "general (ALT9).bat"
```

Если не указать стратегию, будет использован `general.bat`.

5. Проверить состояние:

```bash
python3 linux/zapret_linux.py status
```

6. Остановить ручной запуск:

```bash
sudo python3 linux/zapret_linux.py stop
```

## Автозапуск

Установить systemd unit и сразу запустить стратегию:

```bash
sudo ./service.sh start "general (ALT9).bat"
```

Основные команды:

| | |
| --- | --- |
| `./service.sh status` | Показать systemd-статус и состояние лаунчера |
| `sudo ./service.sh restart` | Перезапустить текущую стратегию |
| `sudo ./service.sh restart "general (ALT11).bat"` | Перезапустить с другой стратегией |
| `sudo ./service.sh stop` | Остановить сервис |
| `sudo ./service.sh remove` | Удалить systemd unit |
| | |

Systemd unit называется `zapret-discord-youtube-linux.service`.

## Настройки

### Game Filter

Game Filter расширяет обработку на порты выше `1023`. Обычно это нужно для игр и приложений, которые используют нестандартные TCP/UDP-порты.

| Команда | Режим |
| --- | --- |
| `python3 linux/zapret_linux.py game-filter status` | Показать текущий режим |
| `python3 linux/zapret_linux.py game-filter all` | TCP и UDP |
| `python3 linux/zapret_linux.py game-filter tcp` | Только TCP |
| `python3 linux/zapret_linux.py game-filter udp` | Только UDP |
| `python3 linux/zapret_linux.py game-filter disable` | Выключить |

После изменения режима перезапустить zapret:

```bash
sudo ./service.sh restart
```

### IPSet Filter

IPSet Filter управляет файлом `lists/ipset-all.txt`.

| Режим | Что означает |
| --- | --- |
| `loaded` | Используется список IP и подсетей из `lists/ipset-all.txt` |
| `none` | Список заменен на `203.0.113.113/32`, IPSet-профили почти ничего не затрагивают |
| `any` | Файл пустой, под IPSet-профили попадает любой IP |
| | |

Команды:

```bash
python3 linux/zapret_linux.py ipset-filter status
python3 linux/zapret_linux.py ipset-filter loaded
python3 linux/zapret_linux.py ipset-filter none
python3 linux/zapret_linux.py ipset-filter any
```

После изменения режима нужен перезапуск:

```bash
sudo ./service.sh restart
```

### Обновления

```bash
python3 linux/zapret_linux.py update-ipset
sudo python3 linux/zapret_linux.py update-hosts
python3 linux/zapret_linux.py check-updates
python3 linux/zapret_linux.py update-check enable
python3 linux/zapret_linux.py update-check disable
```

`check-updates` показывает установленную версию форка, последнюю версию оригинального проекта и последнюю версию форка. Версия форка проверяется по `.service/version.txt` в ветке `main`, без GitHub Releases.

Чтобы сразу открыть страницу форка при доступном обновлении:

```bash
python3 linux/zapret_linux.py check-updates --open
```

## Переменные окружения

| Переменная | Назначение |
| --- | --- |
| `NFQWS_BIN` | Явный путь к `nfqws` |
| `NFQWS_USER` | Пользователь, под которым должен работать `nfqws` после старта |
| `NFQWS_UID` | UID вместо имени пользователя |
| `LINUX_AUTO_GOOGLE_QUIC=0` | Отключить автоматическое добавление `list-google.txt` в UDP 443 hostlist-профили |

## Файлы проекта

| Файл | Назначение |
| --- | --- |
| `linux/zapret_linux.py` | Основной Linux-лаунчер |
| `run.sh` | Ручной запуск в фоне |
| `service.sh` | Установка, статус и управление systemd-unit |
| `.runtime/state.json` | Состояние текущего запуска |
| `.runtime/nfqws.log` | Лог `nfqws` |
| `utils/game_filter.enabled` | Режим Game Filter |
| `lists/ipset-all.txt` | Основной IPSet-список |
| `lists/ipset-all.txt.backup` | Backup списка для переключения IPSet Filter |

⠀

## Диагностика

Базовая проверка:

```bash
python3 linux/zapret_linux.py diagnostics
```

Текущие правила `nftables`:

```bash
sudo nft list table inet zapret_linux
```

Лог `nfqws`:

```bash
tail -n 120 .runtime/nfqws.log
```

Текущий статус:

```bash
python3 linux/zapret_linux.py status
```

В рабочем игровом сценарии обычно должно быть видно что-то вроде:

```text
Game Filter: enabled (TCP and UDP)
IPSet Filter: loaded (30995 entries)
```

## Если игра не подключается

1. Проверь, что Game Filter включен:

```bash
python3 linux/zapret_linux.py game-filter status
```

2. Проверь, что IPSet Filter действительно `loaded`, а не `none`:

```bash
python3 linux/zapret_linux.py status
```

3. Перезапусти сервис после любых изменений Game Filter или IPSet Filter:

```bash
sudo ./service.sh restart
```

4. Если игра использует IPv6, этот порт может ее не затронуть. Для проверки временно отключи IPv6 в системе или в настройках сети.

5. Если IP серверов игры отсутствует в `lists/ipset-all.txt`, добавь нужные IP или подсети в этот файл и перезапусти zapret.

## Ограничения

- Порт рассчитан на сценарий с `systemd`, `nftables` и NFQUEUE.
- Скрипт требует root для изменения правил `nftables`.
- Стратегии остаются теми же `.bat` файлами. Если конкретная стратегия не работает у провайдера, следует попробовать другие `general*`.
- Для non-systemd систем автозапуск нужно настраивать отдельно, но ручной запуск через `run.sh` может подойти.

⠀

# Thank You!
⠀⠀❤︎
Этот репозиторий является форком [Flowseal/zapret-discord-youtube](https://github.com/Flowseal/zapret-discord-youtube).

⠀⠀❤︎ Нативный `nfqws` относится к проекту [bol-van/zapret](https://github.com/bol-van/zapret).
