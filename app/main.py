"""ASGI entrypoint. Render start command: uvicorn app.web.api:app --host 0.0.0.0 --port $PORT"""
from app.web.api import app

__all__ = ["app"]
