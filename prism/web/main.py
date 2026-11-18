"""uvicorn entry point: uvicorn prism.web.main:app --host 127.0.0.1 --port 8040"""
import logging

from .app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
