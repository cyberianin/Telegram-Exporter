class TGXError(Exception):
    pass


class AuthenticationRequired(TGXError):
    pass


class PeerNotFound(TGXError):
    pass


class EmptyHistory(TGXError):
    pass


class ExportInvariantError(TGXError):
    pass
