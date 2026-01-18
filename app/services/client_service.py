from app.utils.sanitizer import sanitize_input
from app.repositories.client_repository import ClientRepository

class ClientService:
    def __init__(self):
        self.repository = ClientRepository()

    def get_all_clients(self):
        return self.repository.get_all()

    def create_client(self, data):
        
        clean_data = sanitize_input(data)

   
        if clean_data.get('document'):
            exists = self.repository.find_by_document(clean_data['document'])
            if exists:
                raise ValueError("Cliente com este documento já existe.")
        

        return self.repository.create(clean_data)

    def get_client(self, id):
        return self.repository.get_by_id(id)