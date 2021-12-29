import functools
import re
from dataclasses import dataclass
from typing import Dict, List, Match, Optional, Set, Tuple

from django.db.models import Q

from zerver.models import Realm, UserGroup, UserProfile, get_active_streams

# Match multi-word string between @** ** or match any one-word
# sequences after @
MENTIONS_RE = re.compile(r"(?<![^\s\'\"\(,:<])@(?P<silent>_?)(\*\*(?P<match>[^\*]+)\*\*)")
USER_GROUP_MENTIONS_RE = re.compile(r"(?<![^\s\'\"\(,:<])@(?P<silent>_?)(\*(?P<match>[^\*]+)\*)")

wildcards = ["all", "everyone", "stream"]


@dataclass
class FullNameInfo:
    id: int
    full_name: str


@dataclass
class UserFilter:
    id: Optional[int]
    full_name: Optional[str]

    def Q(self) -> Q:
        if self.full_name is not None and self.id is not None:
            return Q(full_name__iexact=self.full_name, id=self.id)
        elif self.id is not None:
            return Q(id=self.id)
        elif self.full_name is not None:
            return Q(full_name__iexact=self.full_name)
        else:
            raise AssertionError("totally empty filter makes no sense")


@dataclass
class MentionBackend:
    realm_id: int

    def get_full_name_info_list(self, user_filters: List[UserFilter]) -> List[FullNameInfo]:
        q_list = [user_filter.Q() for user_filter in user_filters]

        rows = (
            UserProfile.objects.filter(
                realm_id=self.realm_id,
                is_active=True,
            )
            .filter(
                functools.reduce(lambda a, b: a | b, q_list),
            )
            .only(
                "id",
                "full_name",
            )
        )
        return [FullNameInfo(id=row.id, full_name=row.full_name) for row in rows]


def user_mention_matches_wildcard(mention: str) -> bool:
    return mention in wildcards


def extract_mention_text(m: Match[str]) -> Tuple[Optional[str], bool]:
    text = m.group("match")
    if text in wildcards:
        return None, True
    return text, False


def possible_mentions(content: str) -> Tuple[Set[str], bool]:
    # mention texts can either be names, or an extended name|id syntax.
    texts = set()
    message_has_wildcards = False
    for m in MENTIONS_RE.finditer(content):
        text, is_wildcard = extract_mention_text(m)
        if text:
            texts.add(text)
        if is_wildcard:
            message_has_wildcards = True
    return texts, message_has_wildcards


def possible_user_group_mentions(content: str) -> Set[str]:
    return {m.group("match") for m in USER_GROUP_MENTIONS_RE.finditer(content)}


def get_possible_mentions_info(
    mention_backend: MentionBackend, mention_texts: Set[str]
) -> List[FullNameInfo]:
    if not mention_texts:
        return []

    user_filters = list()

    name_re = r"(?P<full_name>.+)?\|(?P<mention_id>\d+)$"
    for mention_text in mention_texts:
        name_syntax_match = re.match(name_re, mention_text)
        if name_syntax_match:
            full_name = name_syntax_match.group("full_name")
            mention_id = name_syntax_match.group("mention_id")
            if full_name:
                # For **name|id** mentions as mention_id
                # cannot be null inside this block.
                user_filters.append(UserFilter(full_name=full_name, id=int(mention_id)))
            else:
                # For **|id** syntax.
                user_filters.append(UserFilter(full_name=None, id=int(mention_id)))
        else:
            # For **name** syntax.
            user_filters.append(UserFilter(full_name=mention_text, id=None))

    return mention_backend.get_full_name_info_list(user_filters)


class MentionData:
    def __init__(self, mention_backend: MentionBackend, content: str) -> None:
        realm_id = mention_backend.realm_id
        mention_texts, has_wildcards = possible_mentions(content)
        possible_mentions_info = get_possible_mentions_info(mention_backend, mention_texts)
        self.full_name_info = {row.full_name.lower(): row for row in possible_mentions_info}
        self.user_id_info = {row.id: row for row in possible_mentions_info}
        self.init_user_group_data(realm_id=realm_id, content=content)
        self.has_wildcards = has_wildcards

    def message_has_wildcards(self) -> bool:
        return self.has_wildcards

    def init_user_group_data(self, realm_id: int, content: str) -> None:
        self.user_group_name_info: Dict[str, UserGroup] = {}
        self.user_group_members: Dict[int, List[int]] = {}
        user_group_names = possible_user_group_mentions(content)
        if user_group_names:
            for group in UserGroup.objects.filter(
                realm_id=realm_id, name__in=user_group_names, is_system_group=False
            ).prefetch_related("direct_members"):
                self.user_group_name_info[group.name.lower()] = group
                self.user_group_members[group.id] = [m.id for m in group.direct_members.all()]

    def get_user_by_name(self, name: str) -> Optional[FullNameInfo]:
        # warning: get_user_by_name is not dependable if two
        # users of the same full name are mentioned. Use
        # get_user_by_id where possible.
        return self.full_name_info.get(name.lower(), None)

    def get_user_by_id(self, id: int) -> Optional[FullNameInfo]:
        return self.user_id_info.get(id, None)

    def get_user_ids(self) -> Set[int]:
        """
        Returns the user IDs that might have been mentioned by this
        content.  Note that because this data structure has not parsed
        the message and does not know about escaping/code blocks, this
        will overestimate the list of user ids.
        """
        return set(self.user_id_info.keys())

    def get_user_group(self, name: str) -> Optional[UserGroup]:
        return self.user_group_name_info.get(name.lower(), None)

    def get_group_members(self, user_group_id: int) -> List[int]:
        return self.user_group_members.get(user_group_id, [])


def get_stream_name_map(realm: Realm, stream_names: Set[str]) -> Dict[str, int]:
    if not stream_names:
        return {}

    q_list = {Q(name=name) for name in stream_names}

    rows = (
        get_active_streams(
            realm=realm,
        )
        .filter(
            functools.reduce(lambda a, b: a | b, q_list),
        )
        .values(
            "id",
            "name",
        )
    )

    dct = {row["name"]: row["id"] for row in rows}
    return dct


def silent_mention_syntax_for_user(user_profile: UserProfile) -> str:
    return f"@_**{user_profile.full_name}|{user_profile.id}**"
