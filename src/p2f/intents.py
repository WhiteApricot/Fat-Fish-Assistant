"""Identify external message material and label command receipts."""

def contains_external_material(event):
    if any(part.__class__.__name__ in {'Forward','Node','Nodes','Reply'} for part in event.get_messages()):
        return True
    raw = getattr(getattr(event, 'message_obj', None), 'raw_message', {})
    if isinstance(raw, dict) and isinstance(raw.get('message'), list):
        if any(isinstance(part, dict) and part.get('type') in {'forward','reply'} for part in raw['message']):
            return True
    return False

ROUTES = {
    'role': {'': {0}, 'list': {1, 2}, 'remove': {3}, 'add': {3}, 'global': {2, 3}, 'undo': {1}},
    'memory': {'reset': {1}, 'search': {2}, 'remember': {2}, 'update': {3}, 'delete': {2}, 'cancel': {1}},
    'groupmemory': {'reset': {1}, 'search': {2}, 'remember': {2}, 'update': {3}, 'delete': {2}, 'cancel': {1}},
    'reminder': {'add': {2}, 'addto': {3}, 'list': {1}, 'show': {2}, 'delete': {2},
                 'pause': {2}, 'resume': {2}, 'time': {3}, 'text': {3}, 'select': {2, 3}},
    'groupmode': {'mention': {1}, 'smart': {1}},
    'whitelist': {'add': {2}, 'remove': {2}, 'list': {1}},
    'blacklist': {'add': {2}, 'remove': {2}, 'list': {1}},
    'globaladmin': {'add': {2}, 'remove': {2}},
    'privatepersona': {'set': {2}, 'reset': {1}, 'undo': {1}},
    'grouppersona': {'set': {2}, 'reset': {1}},
    'groupsettings': {'cooldown': {2}},
    'personal': {'简短': {1}, '标准': {1}, '详细': {1}},
}
