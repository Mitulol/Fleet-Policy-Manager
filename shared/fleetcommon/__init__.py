"""Small support library shared by the Fleet Policy Manager services.

This is deliberately thin. It holds the things that would otherwise be
copy-pasted into four services -- environment config, structured logging and
the event-bus wrapper -- and nothing that encodes another service's domain
rules. Each service still owns its own schema, storage and business logic.
"""

__version__ = "1.0.0"
