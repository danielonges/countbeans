"""Domain-rule errors raised by the service core."""


class DomainError(ValueError):
    """A domain-rule violation whose message is safe to show the user.

    Subclasses ValueError so an unmigrated ``except ValueError`` still catches
    it; handlers catch DomainError specifically, so genuine bugs (plain
    ValueError and friends) propagate to the transactional middleware
    (rollback + log) instead of being echoed to the chat.
    """
