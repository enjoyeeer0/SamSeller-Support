# SamSeller Support

FunPay -> Telegram bridge that forwards client messages and lets you answer from Telegram.

## Features

- New message notifications from FunPay to Telegram.
- Reply to notification in Telegram to answer client in FunPay.
- Client short codes (`C001`, `C002`, ...).
- `/clients` command to list recent clients.
- `/to CODE your text` command to send by client code.
- Admin-only Telegram control by `TELEGRAM_ADMIN_ID`.

## Requirements

- Python 3.11+
- Telegram bot token from `@BotFather`
- FunPay Golden Key

## Installation (for developers)

```bash
pip install -r requirements.txt
```

## Configuration

On first run, if no env vars or local secrets file are found, the app starts interactive setup in console and creates `secrets.local.json`.

Priority order:

1. Environment variables
2. `secrets.local.json`
3. Interactive setup wizard

Environment variables:

- `FUNPAY_GOLDEN_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_ID`

You can copy `secrets.example.json` to `secrets.local.json` and fill values manually.

## Run

```bash
python Main.py
```

## Telegram commands

- `/help`
- `/clients`
- `/to C001 your text`

## Security

- Keep `secrets.local.json` private.
- `secrets.local.json` is ignored by `.gitignore`.
- Bot processes commands only from configured admin chat ID.

## Distribution for non-technical users (EXE)

If your users do not know Python, distribute a ready `zip` with `SamSeller-Support.exe`.

Inside release archive:

- `SamSeller-Support.exe`
- `README.md`
- `QUICK_START_RU.txt`
- `secrets.example.json`

First run flow for end user:

1. Run `SamSeller-Support.exe`
2. Enter `Golden Key`, `Telegram Bot Token`, `Admin ID`
3. Bot saves settings to `secrets.local.json` and starts

## Build EXE locally (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1
```

Result:

- `SamSeller-Support-windows-x64.zip`

## GitHub Releases (automatic)

This repository includes GitHub Actions workflow:

- `.github/workflows/windows-release.yml`

How to publish release:

1. Commit and push changes to `main`
2. Create tag and push it:

```bash
git tag v1.0.0
git push origin v1.0.0
```

3. GitHub Actions builds `.exe`, creates `SamSeller-Support-windows-x64.zip`, and attaches it to Release.

You can also run workflow manually from `Actions` tab (`workflow_dispatch`).

## Deploy on GitHub

1. Ensure `secrets.local.json` is not committed.
2. Commit source files, workflow, and docs.
3. Push repository.
