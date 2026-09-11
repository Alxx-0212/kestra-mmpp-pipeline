import uvicorn

from .config import DEFAULT_LISTEN_HOST, DEFAULT_LISTEN_PORT, load_settings


def main():
    settings = load_settings()
    uvicorn.run(
        "services.telegram_bot.app:app",
        host=settings.listen_host,
        port=settings.listen_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
