# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Validate drawn/imported PNG signature references without embedding image bytes in JSON."""
from datetime import datetime


def validate_extras(payload, items, photos=None):
    signatures = payload.get('signatures', {})
    if not isinstance(signatures, dict):
        raise ValueError('Assinaturas inválidas')
    answers = payload.get('answers', {})
    for key, signature in signatures.items():
        if key not in items or not items[key].get('supports_signature') or not isinstance(signature, dict):
            raise ValueError('Item de assinatura inválido')
        for field, limit in [('photo_id', 128), ('signed_by', 8000), ('signed_at', 50)]:
            value = signature.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError('Dados de assinatura inválidos')
        if signature['signed_by'] != answers.get(key, {}).get('value', '').strip():
            raise ValueError('O nome do técnico foi alterado; assine novamente')
        try:
            instant = datetime.fromisoformat(signature['signed_at'].replace('Z', '+00:00'))
            if instant.tzinfo is None:
                raise ValueError()
        except ValueError:
            raise ValueError('Data de assinatura inválida') from None
        if photos is not None and not any(
            p['id'] == signature['photo_id'] and p['checkpoint'] == key and p['mime'] == 'image/png'
            for p in photos
        ):
            raise ValueError('Imagem de assinatura em falta; sincronize as evidências antes de concluir')
    for copy_key in ('qve_copy', 'measurement_copy'):
        origin = payload.get(copy_key)
        if origin is not None:
            if not isinstance(origin, dict):
                raise ValueError('Origem da cópia inválida')
            for field in ['source_report_id', 'source_charger_id', 'source_station_id', 'copied_by', 'copied_at']:
                if not isinstance(origin.get(field), str) or len(origin[field]) > 8000:
                    raise ValueError('Origem da cópia inválida')
