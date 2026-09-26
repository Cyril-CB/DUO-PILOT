"""Frozen discussion roles, also used for the future development phase."""
from datetime import date


def opening_actor_for(run_date, topic):
    if topic not in ('cspilot', 'self'):
        raise ValueError('Sujet inconnu.')
    cspilot_first = 'A' if date.fromisoformat(run_date).toordinal() % 2 == 0 else 'B'
    return cspilot_first if topic == 'cspilot' else ('B' if cspilot_first == 'A' else 'A')


def roles_for(opening_actor):
    if opening_actor not in ('A', 'B'):
        return None
    return {
        'opener': opening_actor,
        'developer': opening_actor,
        'reviewer': 'B' if opening_actor == 'A' else 'A',
    }
