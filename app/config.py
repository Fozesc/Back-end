import os
from datetime import timedelta

class Config:

    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL')
    
   
    if not SQLALCHEMY_DATABASE_URI:
        raise ValueError("DATABASE_URL não definida no ambiente. ")

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    

    SECRET_KEY = os.getenv('JWT_SECRET_KEY')
    
  
    if not SECRET_KEY:
        raise ValueError("JWT_SECRET_KEY não definida.")

    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)