# utils/database.py
from pymongo import MongoClient
from utils.settings import MONGODB_DB_URI

client = MongoClient(MONGODB_DB_URI)
db = client.get_default_database()  # <-- usa "default" automaticamente
