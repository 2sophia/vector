"""Logging configuration"""

import logging

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Disabilita tutti i log di pymongo
logging.getLogger("pymongo").setLevel(logging.WARNING)
logging.getLogger("pymongo.topology").setLevel(logging.ERROR)
logging.getLogger("pymongo.connection").setLevel(logging.ERROR)
logging.getLogger("pymongo.pool").setLevel(logging.ERROR)
logging.getLogger("pymongo.server").setLevel(logging.ERROR)
logging.getLogger("pymongo.heartbeat").setLevel(logging.ERROR)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance"""
    return logging.getLogger(name)