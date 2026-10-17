from copy import deepcopy
from dataclasses import fields


def snapshot(entity_type, row):
    if row is None:
        return None
    return entity_type(
        **{field.name: deepcopy(getattr(row, field.name)) for field in fields(entity_type)}
    )
