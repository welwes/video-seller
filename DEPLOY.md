# Деплой с нуля: от покупки VPS до работающего бота

Гайд рассчитан на чистый VPS с Ubuntu 22.04/24.04 (Debian 12 — то же самое).
Времени займёт минут 20–30, из них половина — ожидание DNS.

## Что должно быть на руках

- VPS (1 vCPU / 1 ГБ RAM хватает) и root-доступ по SSH: IP, пароль или ключ.
- Домен для страницы подписки (любой, хоть самый дешёвый).
- Работающий VLESS-сервер, установленный по [доке vlesshelp](https://vlesshelp.h1cloud.net/).
  От него нужны три вещи: адрес API (`https://IP:API_PORT`), API-токен и адрес sub-порта
  (`https://IP:SUB_PORT`) — как их взять, [ниже](#0-данные-vless-сервера-api-и-sub).
- Токен бота от [@BotFather](https://t.me/BotFather) (`/newbot`).
- Свой Telegram ID (пишем [@userinfobot](https://t.me/userinfobot) — он ответит числом).
- Для приёма крипты — токен Crypto Pay: [@CryptoBot](https://t.me/CryptoBot) → Crypto Pay → Create App.

## 0. Данные VLESS-сервера (API и sub)

Все команды вводятся в консоли VLESS-сервера (там, где ставили vlesshelp).

Смотрим, какие порты выделены (API и sub вешаются только на выделенные порты):

```
vpn ports
```

Включаем API на свободном порту (номер — ваш, из списка выше):

```
vpn api 25626
```

Берём API-токен (он же лежит в `api_token.txt` в рабочей папке сервера):

```
vpn api token
```

Включаем сервер подписок на втором свободном порту:

```
vpn sub 25627
```

Проверяем, что оба сервиса живы:

```
vpn api status
vpn sub status
```

И контрольная проверка снаружи — с ПК или с VPS (`-k`, потому что сертификат самоподписанный):

```bash
curl -k https://IP_VLESS:25626/api/health
curl -k -H "Authorization: Bearer ТОКЕН" https://IP_VLESS:25626/api/status
```

Первая команда отвечает без токена, вторая показывает имя ноды и число клиентов.
Если обе ответили — записываем себе три значения, они пойдут в `config.yml` на шаге 6:

| Что | Откуда | Куда в config.yml |
|-----|--------|-------------------|
| `https://IP_VLESS:25626` | порт из `vpn api` | `api_url` |
| токен | `vpn api token` | `api_token` |
| `https://IP_VLESS:25627` | порт из `vpn sub` | `sub_url` |

Опционально: `vpn sub name МойVPN` — имя подписки, которое клиенты увидят в приложениях.

## 1. Заходим на VPS и обновляемся

С Windows — прямо из PowerShell:

```
ssh root@IP_СЕРВЕРА
```

На сервере:

```bash
apt update && apt upgrade -y
apt install -y curl git ufw
```

## 2. Файрвол

Открываем только SSH и веб. Порт 8080 наружу открывать не надо — веб слушает
только localhost, наружу его отдаёт Caddy.

```bash
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw enable
```

Важно: `ufw allow 22/tcp` — до `ufw enable`, иначе отрежете себе доступ.

## 3. Docker

```bash
curl -fsSL https://get.docker.com | sh
docker --version
```

Docker Compose идёт в комплекте (`docker compose`, без дефиса).

## 4. Домен

В панели регистратора домена создаём A-запись на IP VPS. Обычно делают поддомен:

| Тип | Имя | Значение |
|-----|-----|----------|
| A   | sub | IP_СЕРВЕРА |

Итог: `sub.вашдомен.com` указывает на VPS. Проверка (может занять от минуты до пары часов):

```bash
ping sub.вашдомен.com
```

Пока DNS не обновился — дальше по шагам можно идти, но Caddy сертификат не получит.

## 5. Заливаем проект на сервер

Вариант через Git (если проект в репозитории):

```bash
cd /opt
git clone https://github.com/welwes/video-seller.git vpn-shop
cd vpn-shop
```

Вариант с Windows напрямую — из папки проекта в PowerShell:

```
scp -r C:\путь\до\проекта root@IP_СЕРВЕРА:/opt/vpn-shop
```

(`.venv`, если есть, можно не тащить — на сервере она не нужна). Либо WinSCP, кому удобнее мышкой.

## 6. Конфиги

```bash
cd /opt/vpn-shop
cp .env.example .env
cp config.example.yml config.yml
nano .env
```

В `.env` заполняем:

- `BOT_TOKEN` — от BotFather;
- `ADMIN_IDS` — свой ID (несколько — через запятую);
- `CRYPTOBOT_TOKEN` — токен Crypto Pay, или оставить пустым (крипта выключится);
- `PUBLIC_BASE_URL=https://sub.вашдомен.com` — без слэша на конце;
- `DOMAIN=sub.вашдомен.com` — тот же домен, для Caddy;
- `DB_PATH` и `CONFIG_PATH` не трогаем.

Дальше `nano config.yml`:

- `shop_name`, `support_url` — название и ссылка на поддержку;
- в `servers` — данные своего VLESS-сервера из шага 0: `api_url`, `api_token`, `sub_url`
  (`verify_ssl: false` оставить — сертификат там самоподписанный);
- `plans` — свои тарифы и цены;
- `trial`, `referral_percent`, `topup_amounts` — по вкусу.

## 7. Запуск

```bash
docker compose --profile proxy up -d --build
```

Первая сборка — пара минут. Проверяем:

```bash
docker compose ps          # все три контейнера должны быть Up
docker compose logs bot    # в конце: "Starting polling as ..."
docker compose logs caddy  # "certificate obtained successfully" для домена
curl https://sub.вашдомен.com/healthz   # должно ответить: ok
```

## 8. Проверяем руками

1. Пишем боту `/start` — приходит меню.
2. `/admin` — открывается админка (если нет — проверь `ADMIN_IDS`).
3. Активируем пробный период — бот выдаёт ссылку вида `https://sub.вашдомен.com/<uuid>`,
   страница открывается, кнопки приложений работают.
4. Тестовая покупка за Stars — самый честный тест всей цепочки.

Если всё так — магазин работает.

## Обслуживание

Логи в реальном времени:

```bash
docker compose logs -f bot
docker compose logs -f web
```

Перезапуск после правки `config.yml` (тарифы, серверы):

```bash
docker compose restart bot web
```

Обновление кода:

```bash
cd /opt/vpn-shop
git pull            # или заново залить файлы
docker compose --profile proxy up -d --build
```

Бэкап — вся база лежит в одном файле:

```bash
cp data/shop.db /root/backup-$(date +%F).db
```

Можно повесить в крон раз в сутки:

```bash
(crontab -l; echo "0 5 * * * cp /opt/vpn-shop/data/shop.db /root/shop-\$(date +\%F).db") | crontab -
```

После ребута сервера всё поднимется само (`restart: unless-stopped` в compose).

## Частые проблемы

**Caddy не получает сертификат.** DNS ещё не обновился, либо закрыт 80-й порт,
либо в `.env` домен с опечаткой. Смотреть `docker compose logs caddy`, после
исправления — `docker compose restart caddy`.

**Бот падает с `Unauthorized`.** Неверный `BOT_TOKEN`. Поправить `.env` и
`docker compose up -d` (контейнер пересоздастся с новым env).

**`502` на `/<uuid>/sub` или «Сервер временно недоступен» в боте.** Веб/бот не
достучались до VLESS-сервера: проверь `api_url`, `api_token`, `sub_url` в
`config.yml` и что порты VLESS-сервера доступны снаружи:

```bash
curl -k https://IP_VLESS:API_PORT/api/health
```

**Страница открывается, но приложения не подхватывают подписку.** В `.env`
`PUBLIC_BASE_URL` должен быть именно `https://` и совпадать с реальным доменом —
кнопки строят ссылки от него.

**Telegram API недоступен с VPS.** Бывает на серверах в РФ. Решение — VPS за
пределами РФ (бот может стоять где угодно, на скорость VPN это не влияет).
