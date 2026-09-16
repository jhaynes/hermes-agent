"""Load admission composes with, never replaces, Kanban's memory guard."""
from gateway.system_load import load_status


def kanban_spawn_permitted(spawn_budget, level, policy):
    if not policy.enabled:
        return spawn_budget
    if level == 'critical':
        return 0
    if level == 'elevated':
        return 1 if spawn_budget is None else max(0, min(spawn_budget, 1))
    return spawn_budget


def load_spawn_budget(spawn_budget):
    return load_status('kanban', spawn_budget, kanban_spawn_permitted).effective_cap
