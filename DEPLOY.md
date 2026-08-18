# Деплой ATAMŪRA Core на administrator.atamura.group (94.131.94.239)

Кокпит читает снимки продуктов по HTTPS (dogovor/finance публичные), поэтому может жить на **любом**
сервере с интернетом. Ставим на admin-сервер, за basic-auth (внутри — управленческие данные,
наружу открывать НЕЛЬЗЯ).

Зависимостей нет — только `python3` (stdlib). Ни pip, ни Docker не обязательны.

## 1. Код на сервер

```bash
sudo mkdir -p /opt/atamura-core && cd /opt/atamura-core
# вариант A: git (если завёл remote)
git clone <repo_url> .
# вариант B: scp с компа
#   scp -r C:\Users\cafa1\Desktop\atamura-core\* niyaz@94.131.94.239:/opt/atamura-core/
```

## 2. Секреты в .env

```bash
cd /opt/atamura-core
cp .env.example .env
nano .env        # вписать METRICS_KEY (бот договоров) и FINANCE_KEY (SERVICE_KEY финблока)
chmod 600 .env
```

## 3. systemd (автозапуск, слушает 127.0.0.1:8090)

```bash
sudo cp deploy/atamura-core.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now atamura-core
curl -s http://127.0.0.1:8090/api/kosyaki | head -c 120     # проверка: JSON
```

## 4. nginx + basic-auth + TLS

```bash
# логин/пароль для входа в кокпит
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd-admin admin      # задать пароль

sudo cp deploy/nginx-admin.conf /etc/nginx/sites-available/administrator.atamura.group
sudo ln -s /etc/nginx/sites-available/administrator.atamura.group /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# TLS (DNS administrator.atamura.group уже указывает на этот сервер)
sudo certbot --nginx -d administrator.atamura.group
```

Готово: **https://administrator.atamura.group** (спросит логин/пароль) → кокпит на живых данных.

## Обновление

```bash
cd /opt/atamura-core && git pull && sudo systemctl restart atamura-core
```

## Безопасность

- `.env` (ключи финблока/договоров) — `chmod 600`, в git не коммитить (`.gitignore`).
- Кокпит слушает только `127.0.0.1` — снаружи доступен ТОЛЬКО через nginx с basic-auth.
- Ключи read-only (снимки метрик) — но всё равно секреты; при утечке ротировать на стороне продуктов.
