import os
from dotenv import load_dotenv
from datetime import timedelta
load_dotenv() 

class Config:

    user = os.getenv('POSTGRES_USER', 'admin')
    pw   = os.getenv('POSTGRES_PASSWORD', 'admin')
    host = os.getenv('POSTGRES_HOST', 'localhost')
    port = os.getenv('POSTGRES_PORT', '5432')
    db   = os.getenv('POSTGRES_DB', 'fozesc_db')
    
    SQLALCHEMY_DATABASE_URI = f"postgresql://{user}:{pw}@{host}:{port}/{db}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    SECRET_KEY = os.getenv('SECRET_KEY', 'chave_padrao_dev')
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=30)