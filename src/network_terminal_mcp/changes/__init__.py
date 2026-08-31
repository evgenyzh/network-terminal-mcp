"""Change plan lifecycle management.

Change plans hold the canonical command list created by the model. The manager
stores them server-side so ``apply_change`` executes exactly what was shown to
the user; the model cannot alter the commands between confirmation and apply.
"""
