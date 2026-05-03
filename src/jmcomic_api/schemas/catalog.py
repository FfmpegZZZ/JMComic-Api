from __future__ import annotations

from enum import StrEnum

from jmcomic.jm_config import JmMagicConstants
from pydantic import BaseModel


class TimeRange(StrEnum):
    today = "today"
    week = "week"
    month = "month"
    all = "all"
    t = "t"
    w = "w"
    m = "m"
    a = "a"

    def to_jm(self) -> str:
        match self:
            case TimeRange.today | TimeRange.t:
                return JmMagicConstants.TIME_TODAY
            case TimeRange.week | TimeRange.w:
                return JmMagicConstants.TIME_WEEK
            case TimeRange.month | TimeRange.m:
                return JmMagicConstants.TIME_MONTH
            case _:
                return JmMagicConstants.TIME_ALL


class Category(StrEnum):
    all = "all"
    doujin = "doujin"
    single = "single"
    short = "short"
    another = "another"
    hanman = "hanman"
    meiman = "meiman"
    doujin_cosplay = "doujin_cosplay"
    cosplay = "cosplay"
    three_d = "3d"
    english_site = "english_site"

    def to_jm(self) -> str:
        match self:
            case Category.all:
                return JmMagicConstants.CATEGORY_ALL
            case Category.doujin:
                return JmMagicConstants.CATEGORY_DOUJIN
            case Category.single:
                return JmMagicConstants.CATEGORY_SINGLE
            case Category.short:
                return JmMagicConstants.CATEGORY_SHORT
            case Category.another:
                return JmMagicConstants.CATEGORY_ANOTHER
            case Category.hanman:
                return JmMagicConstants.CATEGORY_HANMAN
            case Category.meiman:
                return JmMagicConstants.CATEGORY_MEIMAN
            case Category.doujin_cosplay | Category.cosplay:
                return JmMagicConstants.CATEGORY_DOUJIN_COSPLAY
            case Category.three_d:
                return JmMagicConstants.CATEGORY_3D
            case Category.english_site:
                return JmMagicConstants.CATEGORY_ENGLISH_SITE


class OrderBy(StrEnum):
    latest = "latest"
    view = "view"
    picture = "picture"
    like = "like"
    month_rank = "month_rank"
    week_rank = "week_rank"
    day_rank = "day_rank"

    def to_jm(self) -> str:
        match self:
            case OrderBy.latest:
                return JmMagicConstants.ORDER_BY_LATEST
            case OrderBy.view:
                return JmMagicConstants.ORDER_BY_VIEW
            case OrderBy.picture:
                return JmMagicConstants.ORDER_BY_PICTURE
            case OrderBy.like:
                return JmMagicConstants.ORDER_BY_LIKE
            case OrderBy.month_rank:
                return JmMagicConstants.ORDER_MONTH_RANKING
            case OrderBy.week_rank:
                return JmMagicConstants.ORDER_WEEK_RANKING
            case OrderBy.day_rank:
                return JmMagicConstants.ORDER_DAY_RANKING


class AlbumSummary(BaseModel):
    id: str
    title: str


class PageData(BaseModel):
    results: list[AlbumSummary]
    current_page: int
    has_next_page: bool
    total: int
    params_used: dict[str, str] | None = None


class AlbumDetailData(BaseModel):
    id: str
    title: str
    tags: list[str]
