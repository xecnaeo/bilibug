class BiliCommentsError(Exception):
    """Base error shown to CLI users."""


class InvalidTargetError(BiliCommentsError):
    pass


class ApiError(BiliCommentsError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class AuthenticationRequiredError(ApiError):
    pass


class RiskControlError(ApiError):
    pass


class VideoNotFoundError(ApiError):
    pass
