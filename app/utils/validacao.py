"""Leitura de datas e numeros que vem da tela, num lugar so (versao 2.11.3).

Antes cada tela lia do seu jeito: data com ano errado (2206, 1902) passava e enchia o Historico
de meses vazios, e numero como "abc", "NaN" ou "Infinity" virava erro 500 ou NaN gravado no
banco (o painel passava a mostrar NaN). Aqui tudo que nao for valido vira ValueError com uma
mensagem que a pessoa entende - e o controller devolve 400.
"""
import math
import re
from datetime import date, datetime

ANO_MIN, ANO_MAX = 2000, 2100


def ler_data(texto, campo='Data'):
    """AAAA-MM-DD (o resto da string e' ignorado) com o ano entre 2000 e 2100."""
    if isinstance(texto, datetime):
        d = texto.date()
    elif isinstance(texto, date):
        d = texto
    else:
        try:
            d = datetime.strptime(str(texto)[:10], '%Y-%m-%d').date()
        except (TypeError, ValueError):
            raise ValueError(f'{campo} inválida (use AAAA-MM-DD)')
    if not ANO_MIN <= d.year <= ANO_MAX:
        raise ValueError(f'{campo}: ano {d.year} fora da faixa permitida ({ANO_MIN} a {ANO_MAX})')
    return d


def ler_numero(valor, campo, minimo=None, maximo=None, casas=2):
    """Numero finito (sem NaN/Infinity), dentro da faixa, arredondado em `casas`. Aceita
    numero ou texto com virgula decimal ("12,5"); "1.234,56" e' recusado, nao lido errado."""
    if isinstance(valor, bool):
        raise ValueError(f'{campo}: valor inválido')
    if isinstance(valor, str):
        valor = valor.strip().replace(',', '.')
    try:
        n = float(valor)
    except (TypeError, ValueError):
        raise ValueError(f'{campo}: valor inválido')
    if not math.isfinite(n):
        raise ValueError(f'{campo}: valor inválido')
    if minimo is not None and n < minimo:
        raise ValueError(f'{campo}: não pode ser menor que {minimo:g}')
    if maximo is not None and n > maximo:
        raise ValueError(f'{campo}: não pode ser maior que {maximo:g}')
    return round(n, casas) if casas is not None else n


def ler_texto(valor, campo, maximo, obrigatorio=False):
    """Texto sem espacos sobrando, cortado no tamanho da coluna. Vazio vira None."""
    texto = ' '.join(str(valor or '').split())[:maximo]
    if obrigatorio and not texto:
        raise ValueError(f'Informe {campo}')
    return texto or None


def _dv_cnpj(base):
    """Digito do CNPJ (numerico ou alfanumerico, IN RFB 2.229/2024): cada caractere vale ord(c) - 48."""
    pesos = list(range(len(base) - 7, 1, -1)) + list(range(9, 1, -1))
    soma = sum((ord(c) - 48) * p for c, p in zip(base, pesos))
    resto = soma % 11
    return '0' if resto < 2 else str(11 - resto)


def documento_valido(texto):
    """CPF (11 digitos) ou CNPJ (14 caracteres, os 12 primeiros podem ter letras) com os digitos verificadores
    certos. Vazio conta como valido (o campo e' opcional)."""
    doc = re.sub(r'[^0-9A-Za-z]', '', str(texto or '')).upper()
    if not doc:
        return True
    if re.fullmatch(r'\d{11}', doc):
        if doc == doc[0] * 11:
            return False
        for n in (9, 10):
            soma = sum(int(doc[i]) * (n + 1 - i) for i in range(n))
            if int(doc[n]) != (soma * 10 % 11) % 10:
                return False
        return True
    if re.fullmatch(r'[0-9A-Z]{12}\d{2}', doc):
        if doc == doc[0] * 14:
            return False
        return _dv_cnpj(doc[:12]) == doc[12] and _dv_cnpj(doc[:13]) == doc[13]
    return False
