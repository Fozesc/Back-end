from app import ma
from app.models.domain import Client

class ClientSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Client
        load_instance = True

client_schema = ClientSchema()
clients_schema = ClientSchema(many=True)