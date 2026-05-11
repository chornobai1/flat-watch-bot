# Flat Watch Bot — Sreality + Bezrealitky → Telegram

MVP-бот для моніторингу нових оголошень квартир на Sreality.cz та Bezrealitky.cz.

## Що робить

- перевіряє задані URL пошуку;
- шукає нові оголошення;
- фільтрує по локаціях: Praha 5, Praha 13, Stodůlky;
- фільтрує по плануванню: 3+kk і більше;
- не відправляє дублікати;
- надсилає нові оголошення в Telegram;
- може запускатися без сервера через GitHub Actions.

## Важливо

Сайти можуть змінювати HTML-структуру або блокувати часті запити. Для особистого моніторингу став інтервал 10–15 хвилин і не роби масових запитів.

## Швидкий запуск локально

1. Створи Telegram bot через @BotFather.
2. Отримай `TELEGRAM_BOT_TOKEN`.
3. Напиши своєму боту будь-яке повідомлення.
4. Дізнайся `TELEGRAM_CHAT_ID`, наприклад через:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`

5. Встанови залежності:

```bash
pip install -r requirements.txt
```

6. Скопіюй `.env.example` в `.env`:

```bash
cp .env.example .env
```

7. Заповни `.env`.

8. Запусти:

```bash
python monitor.py
```

## Запуск через GitHub Actions

1. Створи приватний GitHub repository.
2. Завантаж усі файли проєкту.
3. У GitHub відкрий:
   `Settings → Secrets and variables → Actions → New repository secret`

Додай:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

4. GitHub Actions буде запускати перевірку кожні 15 хвилин.

## Налаштування пошуку

URL пошуку задаються в `config.yaml`.

Краще створити фільтр прямо на сайті, наприклад:
- Sreality: pronájem, byty, Praha 5, 3+kk
- Bezrealitky: pronájem, byt, Praha, 3+kk

Потім вставити готовий URL у `config.yaml`.

## Якщо бот нічого не знаходить

Можливі причини:
- сайт змінив HTML;
- сайт віддає сторінку через JavaScript;
- сайт заблокував GitHub Actions IP;
- URL фільтра неправильний.

У такому разі треба підкрутити функції:
- `parse_sreality_html`
- `parse_bezrealitky_html`

у файлі `monitor.py`.
