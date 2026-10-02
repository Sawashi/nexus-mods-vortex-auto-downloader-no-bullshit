"""Exceptions shared between the job, the resolver and the browser flow."""


class Cancelled(Exception):
    """The user pressed Stop."""
