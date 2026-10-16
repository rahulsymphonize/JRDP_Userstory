class AppError(Exception):
    """Platform error -> {code, message, correlationId}. Messages stay generic."""

    def __init__(self, status, code, message):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message
