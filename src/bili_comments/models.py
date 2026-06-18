from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VideoPage:
    cid: int
    page: int
    title: str
    duration: int
    width: int = 0
    height: int = 0
    rotate: int = 0


@dataclass(frozen=True)
class VideoStats:
    view: int = 0
    danmaku: int = 0
    reply: int = 0
    favorite: int = 0
    coin: int = 0
    share: int = 0
    like: int = 0
    current_rank: int = 0
    historical_rank: int = 0


@dataclass(frozen=True)
class Video:
    aid: int
    bvid: str
    title: str
    owner_mid: str
    owner_name: str
    description: str = ""
    cover_url: str = ""
    category_id: int = 0
    category_name: str = ""
    published_at: int = 0
    created_at: int = 0
    duration: int = 0
    copyright: int = 0
    state: int = 0
    pages: tuple[VideoPage, ...] = field(default_factory=tuple)
    stats: VideoStats = field(default_factory=VideoStats)


@dataclass(frozen=True)
class Comment:
    rpid: int
    message: str
    ctime: int
    like_count: int
    reply_count: int
    author_mid: str
    author_name: str
    author_level: int | None
    root_rpid: int = 0
    parent_rpid: int = 0
    level: int = 0
    state: int = 0
    pin_type: str = ""
    sort_rank: int = 0


@dataclass(frozen=True)
class CommentPage:
    comments: tuple[Comment, ...]
    next_cursor: str | None
    all_count: int = 0

    @property
    def replies(self) -> tuple[Comment, ...]:
        """Compatibility alias for the v0.1 page attribute."""
        return self.comments


@dataclass(frozen=True)
class SubReplyPage:
    comments: tuple[Comment, ...]
    next_page: int | None
    total_count: int

