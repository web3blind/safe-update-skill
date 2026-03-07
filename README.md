# safe-update-skill

Сделал скилл безопасных обновлений для OpenClaw

🎯 Зачем

Обновление бота — всегда лотерея. После апдейта могут слететь конфиги, настройки, память. Написал этот скилл чтобы обновления были предсказуемыми и безопасными.

🔒 Принципы

1. Проверка места на диске — не начнёт без 500MB свободно
2. Бэкап перед обновлением — сохраняет всё нужное
3. Анализ релизов — показывает что изменилось и какие риски
4. Проверка после — убеждается что всё работает
5. Авто-откат — если что-то сломалось, восстанавливает сам

🚀 Команды

Dry-run
Показывает что произойдёт, но ничего не меняет. Проверь перед обновлением.

Run
Полный цикл: проверка → бэкап → анализ релизов → обновление → проверка → откат если нужно.

Resume
Продолжить после перезагрузки — шаги сохраняются.

Cleanup
Удалить старые бэкапы (оставляет последние 3, max 14 дней).

📋 Workflow

```
Dry-run → (всё ок?) → Run → (проверка прошла?) → готово
                              ↓ нет
                        Авто-откат из бэкапа
```

⚙️ Настройки

- SAFE_UPDATE_UPDATE_CMD — команда обновления
- SAFE_UPDATE_AUTO_ROLLBACK — вкл/выкл авто-откат
- SAFE_UPDATE_MIN_FREE_MB — минимум места (по умолчанию: 500MB)
- SAFE_UPDATE_KEEP_LAST_SUCCESS — сколько бэкапов хранить

📂 Что бекапится

- openclaw.json (конфиг)
- MEMORY.md, AGENTS.md, SOUL.md, USER.md
- memory/telegram-topics.json
- state/digest/configs

## Установка

```bash
cd ~/.openclaw/workspace/skills
git clone git@github.com:web3blind/safe-update-skill.git
```

## Использование

```bash
# Dry-run (проверить что будет)
python3 ~/.openclaw/workspace/skills/safe-update-skill/scripts/safe_update.py dry-run

# Полное обновление
python3 ~/.openclaw/workspace/skills/safe-update-skill/scripts/safe_update.py run

# Продолжить после перезагрузки
python3 ~/.openclaw/workspace/skills/safe-update-skill/scripts/safe_update.py resume

# Удалить старые бэкапы
python3 ~/.openclaw/workspace/skills/safe-update-skill/scripts/safe_update.py cleanup
```

🔗 Где взять

github.com/web3blind/safe-update-skill
