from .base_repository import BaseRepository
from app.models.domain import Client

class ClientRepository(BaseRepository):
    def __init__(self):
        super().__init__(Client)
    
   
    def find_by_document(self, document):
        return self.model.query.filter_by(document=document).first()