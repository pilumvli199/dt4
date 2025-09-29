# DhanHQ LTP Telegram Bot - Deploy Package

## Files included
- bot.py - entrypoint
- bot_auto_resolve.py - WebSocket based LTP fetcher + Telegram updates
- dhanhq_security_ids.py - sample security id reference (edit as needed)
- requirements.txt - dependencies
- imghdr.py - dummy shim for Python 3.13
- config.example.env - copy to .env and fill credentials
- .gitignore - recommended ignore
- Procfile - for deployment platform

## Deploy
1. Copy `config.example.env` to `.env` and fill tokens
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `python bot.py`

## Notes
- Use WebSocket feed (dhanhq SDK) to avoid REST rate limits.
- Do not commit real secrets to the repo.
