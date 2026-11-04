import re
from datetime import datetime, timedelta

from app.models.domain import AuditLog, BackupConfig, CompanySettings, User
from app import db
from app.utils.validacao import ler_numero, ler_texto, documento_valido
from app.services.audit_service import AuditService
from flask_jwt_extended import get_jwt, get_jwt_identity
from werkzeug.security import check_password_hash

FREQUENCIAS = {'diario': 'todo dia útil', 'semanal': 'toda semana', 'mensal': 'todo mês', 'desligado': 'não envia'}
ARQUIVOS = {'freq_banco': 'banco', 'freq_planilha': 'planilha', 'freq_relatorio': 'relatório'}
EMAIL = re.compile(r'[^@\s,;<>"]+@[^@\s,;<>"]+\.[^@\s,;<>".]+')
MAX_DESTINATARIOS = 20
# o que o worker de backup grava na auditoria (gerar_backup.auditar), separado pelo comeco do texto
TIPOS_BACKUP = (('banco', 'Backup %', 'A cópia do banco'), ('email', 'E-mail do backup%', 'O envio do backup por e-mail'),
                ('restauracao', 'Teste de restauração%', 'O teste de restauração do backup'))
BACKUP_PARADO_HORAS = 48   # o worker faz copia todo dia as 12h e as 15h


class SettingsService:

    def __init__(self):
        self.audit = AuditService()

    def _get_current_user(self):
        try:
       
            return get_jwt().get('name', 'Sistema')
        except:
            return 'Sistema'

    def get_settings(self):
      
        settings = CompanySettings.query.first()
        if not settings:
            settings = CompanySettings(
                company_name="Minha Fatoring",
                default_monthly_rate=4.0,
                default_compensation_days=2
            )
            db.session.add(settings)
            db.session.commit()
        
        return self._serialize(settings)

    # campo da tela -> (rotulo, coluna, como ler). Antes gravava o que viesse: texto na taxa dava
    # erro 500, "NaN" ia para o banco, campo apagado travava o salvar.
    CAMPOS = {
        'nomeEmpresa': ('Nome', 'company_name', lambda v: ler_texto(v, 'o nome da empresa', 100, obrigatorio=True)),
        'cnpj': ('CNPJ', 'cnpj', lambda v: ler_texto(v, 'o CNPJ', 20)),
        'telefone': ('Telefone', 'phone', lambda v: ler_texto(v, 'o telefone', 20)),
        'endereco': ('Endereço', 'address', lambda v: ler_texto(v, 'o endereço', 200)),
        'pqNome': ('Nome PQ', 'pq_nome', lambda v: ler_texto(v, 'o nome da PQ', 100)),
        'pqCnpj': ('CNPJ PQ', 'pq_cnpj', lambda v: ler_texto(v, 'o CNPJ da PQ', 20)),
        'pqEndereco': ('Endereço PQ', 'pq_endereco', lambda v: ler_texto(v, 'o endereço da PQ', 200)),
        # dados para o contrato (e-mail, representante legal e se e' Empresa Simples de Credito)
        'email': ('E-mail', 'email', lambda v: ler_texto(v, 'o e-mail', 100)),
        'repNome': ('Nome do representante', 'rep_nome', lambda v: ler_texto(v, 'nome do representante', 100)),
        'repCpf': ('CPF do representante', 'rep_cpf', lambda v: ler_texto(v, 'CPF do representante', 20)),
        'repRg': ('RG do representante', 'rep_rg', lambda v: ler_texto(v, 'RG do representante', 30)),
        'repNacionalidade': ('Nacionalidade do representante', 'rep_nacionalidade', lambda v: ler_texto(v, 'nacionalidade do representante', 50)),
        'repEstadoCivil': ('Estado civil do representante', 'rep_estado_civil', lambda v: ler_texto(v, 'estado civil do representante', 30)),
        'repEndereco': ('Endereço do representante', 'rep_endereco', lambda v: ler_texto(v, 'endereço do representante', 200)),
        'pqEmail': ('E-mail da PQ', 'pq_email', lambda v: ler_texto(v, 'o e-mail da PQ', 100)),
        'pqTelefone': ('Telefone da PQ', 'pq_telefone', lambda v: ler_texto(v, 'o telefone da PQ', 20)),
        'pqRepNome': ('Nome do representante da PQ', 'pq_rep_nome', lambda v: ler_texto(v, 'nome do representante da PQ', 100)),
        'pqRepCpf': ('CPF do representante da PQ', 'pq_rep_cpf', lambda v: ler_texto(v, 'CPF do representante da PQ', 20)),
        'pqRepRg': ('RG do representante da PQ', 'pq_rep_rg', lambda v: ler_texto(v, 'RG do representante da PQ', 30)),
        'pqRepNacionalidade': ('Nacionalidade do representante da PQ', 'pq_rep_nacionalidade', lambda v: ler_texto(v, 'nacionalidade do representante da PQ', 50)),
        'pqRepEstadoCivil': ('Estado civil do representante da PQ', 'pq_rep_estado_civil', lambda v: ler_texto(v, 'estado civil do representante da PQ', 30)),
        'pqRepEndereco': ('Endereço do representante da PQ', 'pq_rep_endereco', lambda v: ler_texto(v, 'endereço do representante da PQ', 200)),
        # padroes do contrato (aba Contratos)
        'contratoJurosMora': ('Contrato: juros de mora', 'contrato_juros_mora', lambda v: ler_numero(v, 'Juros de mora', 0, 20, 4)),
        'contratoMulta': ('Contrato: multa', 'contrato_multa', lambda v: ler_numero(v, 'Multa contratual', 0, 20, 4)),
        'contratoHonorarios': ('Contrato: honorários', 'contrato_honorarios', lambda v: ler_numero(v, 'Honorários', 0, 30, 4)),
        'contratoPrazoReembolso': ('Contrato: prazo de reembolso', 'contrato_prazo_reembolso',
                                   lambda v: int(ler_numero(v, 'Prazo de reembolso (dias)', 1, 365, 0))),
        'contratoVias': ('Contrato: vias', 'contrato_vias', lambda v: int(ler_numero(v, 'Número de vias', 1, 10, 0))),
        'contratoCidade': ('Contrato: cidade', 'contrato_cidade', lambda v: ler_texto(v, 'a cidade do contrato', 100, obrigatorio=True)),
        'esc': ('Fozesc é ESC', 'esc', lambda v: v is True or str(v).lower() in ('true', '1', 'sim')),
        'pqEsc': ('PQ é ESC', 'pq_esc', lambda v: v is True or str(v).lower() in ('true', '1', 'sim')),
        'taxaPadrao': ('Taxa Mensal', 'default_monthly_rate', lambda v: ler_numero(v, 'Taxa padrão', 0, 100, 4)),
        'diasCompensacaoPadrao': ('Dias Comp.', 'default_compensation_days',
                                  lambda v: int(ler_numero(v, 'Dias de compensação', 0, 30, 0))),
        'iof_rate': ('IOF', 'iof_rate', lambda v: ler_numero(v, 'IOF', 0, 10, 4)),
        'iof_daily_rate': ('IOF Diário', 'iof_daily_rate', lambda v: ler_numero(v, 'IOF diário', 0, 1, 6)),
        'extension_rate': ('Juros Prorrogação', 'extension_rate', lambda v: ler_numero(v, 'Taxa de prorrogação', 0, 100, 4)),
        'fine_rate': ('Multa Devolução', 'fine_rate', lambda v: ler_numero(v, 'Multa de devolução', 0, 100, 4)),
    }

    DOCUMENTOS = {'cnpj': 'CNPJ da Fozesc', 'pqCnpj': 'CNPJ da PQ', 'repCpf': 'CPF do representante da Fozesc',
                  'pqRepCpf': 'CPF do representante da PQ'}

    def update_settings(self, data):
        if not isinstance(data, dict):
            raise ValueError('Dados inválidos')
        # le e confere TUDO antes de mexer: um campo ruim nao deixa os outros meio gravados
        novos = {chave: (rotulo, coluna, ler(data[chave]))
                 for chave, (rotulo, coluna, ler) in self.CAMPOS.items() if chave in data}

        settings = CompanySettings.query.first()
        if not settings:
            settings = CompanySettings()
            db.session.add(settings)
        # CPF/CNPJ so e' conferido quando muda (o que ja estava gravado nao trava o salvar)
        for chave, rotulo in self.DOCUMENTOS.items():
            if chave in novos:
                valor = novos[chave][2]
                if valor and valor != getattr(settings, novos[chave][1]) and not documento_valido(valor):
                    raise ValueError(f'{rotulo} inválido: confira os dígitos')

        mudancas = []
        for rotulo, coluna, valor_novo in novos.values():
            valor_antigo = getattr(settings, coluna)
            if valor_novo != valor_antigo:
                mudancas.append(f"{rotulo}: {valor_antigo} -> {valor_novo}")
                setattr(settings, coluna, valor_novo)
        db.session.commit()

        if mudancas:
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Configuracao', "Alterou: " + " | ".join(mudancas))
        return self._serialize(settings)

    def _serialize(self, s):
        return {
            'nomeEmpresa': s.company_name,
            'cnpj': s.cnpj,
            'telefone': s.phone,
            'endereco': s.address,
            'pqNome': s.pq_nome,
            'pqCnpj': s.pq_cnpj,
            'pqEndereco': s.pq_endereco,
            **{chave: getattr(s, coluna) for chave, (_, coluna, _) in self.CAMPOS.items()
               if coluna.startswith(('email', 'rep_', 'pq_email', 'pq_telefone', 'pq_rep_', 'esc', 'pq_esc', 'contrato_'))},
            'taxaPadrao': s.default_monthly_rate,
            'diasCompensacaoPadrao': s.default_compensation_days,
            'iof_rate': s.iof_rate,
            'iof_daily_rate': s.iof_daily_rate,
            'extension_rate': s.extension_rate,
            'fine_rate': s.fine_rate
        }

    def status_backup(self):
        """O aviso da tela inicial: o ultimo registro de cada tipo (copia do banco, e-mail, teste de
        restauracao) que foi FALHA, e o backup parado ha mais de 2 dias. So conta a partir do
        primeiro registro do worker, para nao avisar antes de ele rodar a 1a vez."""
        do_worker = AuditLog.query.filter(AuditLog.target == 'Backup', AuditLog.action.in_(('BACKUP', 'FALHA')))
        alertas, ultimo_ok = [], None
        for tipo, padrao, nome in TIPOS_BACKUP:
            ultimo = do_worker.filter(AuditLog.description.like(padrao)).order_by(AuditLog.timestamp.desc()).first()
            if ultimo and ultimo.action == 'FALHA':
                alertas.append({'tipo': tipo, 'quando': ultimo.timestamp.isoformat(timespec='minutes'),
                                'texto': f"{nome} falhou", 'detalhe': (ultimo.description or '')[:300]})
        ok = do_worker.filter(AuditLog.action == 'BACKUP', AuditLog.description.like('Backup %'))\
            .order_by(AuditLog.timestamp.desc()).first()
        if ok:
            ultimo_ok = ok.timestamp.isoformat(timespec='minutes')
            if ok.timestamp < datetime.now() - timedelta(hours=BACKUP_PARADO_HORAS) and \
                    not any(a['tipo'] == 'banco' for a in alertas):
                alertas.append({'tipo': 'parado', 'quando': ultimo_ok, 'texto': 'O backup parou de rodar',
                                'detalhe': f"A última cópia do banco que deu certo foi em {ok.timestamp:%d/%m/%Y às %H:%M}."})
        return {'alertas': alertas, 'ultimo_ok': ultimo_ok}

    def get_backup(self):
        return self._serialize_backup(db.session.get(BackupConfig, 1) or BackupConfig(destinatarios=[]))

    def _exigir_admin(self):
        usuario = db.session.get(User, int(get_jwt_identity()))
        if not usuario or usuario.role != 'Admin':
            raise PermissionError('Só o administrador mexe no backup')
        return usuario

    def pedir_envio_backup(self):
        self._exigir_admin()
        cfg = db.session.get(BackupConfig, 1)
        if not cfg or not cfg.destinatarios:
            raise ValueError('Salve a lista de e-mails antes de enviar')
        if not cfg.envio_pedido_em:
            cfg.envio_pedido_em = datetime.now()
            db.session.commit()
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Backup',
                                  'Pediu o envio do backup completo agora para: '
                                  + ', '.join(d['email'] for d in cfg.destinatarios))
        return self._serialize_backup(cfg)

    def update_backup(self, data):
        usuario = self._exigir_admin()
        if not usuario or not check_password_hash(usuario.password_hash, str(data.get('senha_login') or '')):
            self.audit.log_action(self._get_current_user(), 'NEGADO', 'Backup',
                                  'Senha incorreta ao tentar mudar a configuração do backup')
            raise PermissionError('Senha incorreta - nada foi alterado')

        lista = data.get('destinatarios')
        if not isinstance(lista, list) or len(lista) > MAX_DESTINATARIOS:
            raise ValueError(f'Informe até {MAX_DESTINATARIOS} e-mails')
        destinatarios = []
        for d in lista:
            email = str(d.get('email') or '').strip().lower() if isinstance(d, dict) else ''
            if len(email) > 120 or not EMAIL.fullmatch(email):
                raise ValueError(f'E-mail inválido: {email or "(vazio)"}')
            if any(x['email'] == email for x in destinatarios):
                raise ValueError(f'E-mail repetido: {email}')
            destinatarios.append({'email': email, 'banco': d.get('banco') is True})

        if any(data.get(campo) not in FREQUENCIAS for campo in ARQUIVOS):
            raise ValueError('Frequência inválida')

        nova = data.get('senha_arquivos') or ''
        if not isinstance(nova, str) or (nova and not (12 <= len(nova) <= 64 and nova.isprintable())):
            raise ValueError('A senha dos arquivos precisa ter de 12 a 64 caracteres')

        cfg = db.session.get(BackupConfig, 1)
        if not cfg:
            cfg = BackupConfig(id=1, destinatarios=[], freq_banco='diario', freq_planilha='semanal',
                               freq_relatorio='mensal')
            db.session.add(cfg)
        antes = {d['email']: d['banco'] for d in (cfg.destinatarios or [])}
        depois = {d['email']: d['banco'] for d in destinatarios}

        mudancas = [f"incluiu {e}" + (' (com o banco)' if b else '') for e, b in depois.items() if e not in antes]
        mudancas += [f"retirou {e}" for e in antes if e not in depois]
        mudancas += [f"{e} {'passou a receber' if b else 'deixou de receber'} o banco"
                     for e, b in depois.items() if e in antes and antes[e] != b]
        for campo, nome in ARQUIVOS.items():
            if getattr(cfg, campo) != data[campo]:
                mudancas.append(f"{nome}: {FREQUENCIAS.get(getattr(cfg, campo), getattr(cfg, campo))} -> {FREQUENCIAS[data[campo]]}")
                setattr(cfg, campo, data[campo])
        if nova and nova != cfg.senha:
            mudancas.append('trocou a senha dos arquivos')
            cfg.senha = nova
        cfg.destinatarios = destinatarios
        db.session.commit()

        if mudancas:
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Backup', 'Backup: ' + ' | '.join(mudancas))
        return self._serialize_backup(cfg)

    def _serialize_backup(self, c):
        return {
            'destinatarios': c.destinatarios or [],
            'freq_banco': c.freq_banco or 'diario',
            'freq_planilha': c.freq_planilha or 'semanal',
            'freq_relatorio': c.freq_relatorio or 'mensal',
            'tem_senha': bool(c.senha),
            'configurado': c.id is not None,
            'ultimo_envio': c.ultimo_envio,
            'envio_pedido_em': c.envio_pedido_em.strftime('%Y-%m-%d %H:%M') if c.envio_pedido_em else None,
        }
