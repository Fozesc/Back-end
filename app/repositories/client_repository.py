from app.models.domain import Client
from .base_repository import BaseRepository

class ClientRepository(BaseRepository):
    def __init__(self):
        super().__init__(Client)

 
    def get_by_document(self, document):
        if not document:
            return None
        # Busca no banco onde a coluna 'document' é igual ao valor passado
        return self.model.query.filter_by(document=document).first()