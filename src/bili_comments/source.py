from __future__ import annotations

from typing import Protocol, Self

from .models import CommentPage, SubReplyPage, Video


class SourceAdapter(Protocol):
    name: str

    def get_video(self, bvid: str) -> Video: ...

    def get_comment_page(
        self, aid: int, cursor: str = "", *, order: str = "hot"
    ) -> CommentPage: ...

    def get_sub_reply_page(
        self, aid: int, root_rpid: int, page: int
    ) -> SubReplyPage: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *_: object) -> None: ...

