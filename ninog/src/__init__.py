# ==============================================================
#   NiNog Raker V2.3 | THE RATTIKANS
#   src package marker
#
#   Import order is strict and load-bearing:
#
#       src.core      -> no sibling imports
#       src.ui        -> imports src.core only
#       src.workflow  -> imports src.core and src.ui
#       src.ops       -> imports src.core, src.ui and src.workflow
#       main.py       -> imports all four
#
#   src.workflow takes the op registry as a parameter rather than importing
#   it from src.ops, because ops.py imports workflow to register the WORKFLOWS
#   page. That is the one edge that would otherwise be circular.
#
#   Anything that breaks that direction creates a circular import. Keep
#   persistence, rate limiting and the REST client in core, drawing in ui,
#   chained operations in workflow, and interactive flows in ops.
# ==============================================================

__all__ = ["core", "ui", "workflow", "ops"]
