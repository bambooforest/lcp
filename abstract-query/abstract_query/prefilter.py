from __future__ import annotations

import sys

from collections import defaultdict
from typing import Any, Sequence, TypeVar, cast

from .typed import JSON, LabelLayer, QueryPart, JSONObject
from .utils import (
    Config,
    SUFFIXES,
    _get_underlang,
    _parse_comparison,
    arg_sort_key,
)

from typing import Self

MATCHES_ALL = {".*", ".+", ".*?", ".?", ".+?"}


class SingleNode:
    """
    Store a query like pos=NOUN , whether it can be prefiltered, and its realisation as prefilter
    """

    def __init__(self, field: str, op: str, query: str, isRegex: bool = False, *args, **kwargs) -> None:
        self.field = field
        self.op = op
        self.query = query
        self.is_regex = isRegex
        self.inverse = "!" in op
        self.ignore_diacritics = False
        self.case_sensitive = True
        self.is_listlike, list_regex = self._detect_listlike(query, True, self.is_regex)
        if list_regex:
            self.is_regex = True
        self.is_prefix = _is_prefix(self.query, op=op)

    def as_prefilter(self) -> str:
        """
        Turn node into a prefilter string
        """
        if not self._can_prefilter():
            return ""
        inv = "!" if self.inverse else ""
        joiner = " | " if not self.inverse else " & "
        fixed = []
        if self.is_listlike:
            search = self.query.strip("()").split("|")
        else:
            search = [self.query]
        for piece in search:
            pref = ""
            for pattern in sorted(SUFFIXES, key=len, reverse=True):
                if piece.strip().startswith("^") and piece.lstrip("^").isalnum():
                    pref = ":*"
                    piece = piece.lstrip("^")
                    break
                if piece.rstrip().endswith(pattern):
                    pref = ":*"
                    piece = piece[: -len(pattern)]
                    piece = piece.lstrip().lstrip("^")
                    break
            fixed.append(f"{piece}{pref}")
        tokens = [f" {inv}{{{self.field}}}{s}" for s in fixed]
        if len(tokens) > 1:
            return " (" + joiner.join(tokens) + " ) "
        else:
            return tokens[0]

    def _can_prefilter(self) -> bool:
        """
        Can this node be used as a prefilter?
        """
        if self.op not in ("=","!="):
            return False
        if self.is_prefix and self.query in MATCHES_ALL:
            return False
        if self.is_regex and not self.is_prefix:
            return False
        # if not self.mini:
        #    return False
        if not self.case_sensitive:
            return False
        if self.ignore_diacritics:
            return False
        return True

    def _detect_listlike(
        self, search: str, case_sensitive: bool, def_regex: bool
    ) -> tuple[bool, bool]:
        """
        Allow both search="x|y|z" and search="(x|y|z)" to be prefiltered,
        but not search="((x,y))" or search="x(y|z)"
        """
        if def_regex:
            return False, False
        if "|" not in search:
            return False, False
        if not case_sensitive:
            return False, False
        if (search.startswith("(") and "(" not in search[1:]) and (
            search.endswith(")") and ")" not in search[:-1]
        ):
            search = search[1:-1]
            return True, False
        elif (search.startswith("(") and "(" in search[1:]) or (
            search.endswith(")") and ")" in search[:-1]
        ):
            return False, True
        return True, False


class Conjuncted:
    """
    A sequence of objects held together within an AND/OR expression
    """

    def __init__(self, conj: str | None, items: Sequence[SingleNode | Self]) -> None:
        self.conj = conj
        self.items = items


class Prefilter:
    """
    A class that can convert query json to a prefilter vector query, or return
    an empty string if a prefilter can't be made from this input
    """

    def __init__(
        self,
        query_json: QueryPart,
        conf: Config,
        label_layer: LabelLayer,
        has_segment: str,
    ) -> None:
        self.query_json = query_json
        self.conf = conf
        self.label_layer = label_layer
        self.has_segment = has_segment
        self.config = conf.config
        self.lang = conf.lang
        self._underlang = _get_underlang(self.lang, self.config)
        self._has_partitions = self.has_partitions()

    def has_partitions(self) -> bool:
        """
        Is this a partitioned corpus like sparcling?
        """
        mapping = cast(JSONObject, self.config["mapping"])
        layers = cast(JSONObject, mapping["layer"])
        _cols = cast(JSONObject, layers[self.config["segment"]])
        return self.lang in cast(JSONObject, _cols.get("partitions", {}))

    def make(self) -> str:
        """
        The main entrypoint: return iterables of conditions and joins to be
        added to the main query
        """
        # first_pass = self.initialise(self.query_json)
        # strung = set()
        # for k, v in first_pass.items():
        #     if not v:
        #         continue
        #     strung.add(self._build_one_prefilter(v))
        # prefilters = self._finalise_prefilters(list(strung))
        # condition = self._stringify(prefilters)
        condition = self._condition()
        if not condition:
            return ""

        batch_num: str = "rest"
        if self.conf.batch[-1].isnumeric():
            e: enumerate[str] = enumerate(reversed(self.conf.batch))
            first_num: int = next(i for i, c in e if not c.isnumeric())
            batch_num = self.conf.batch[-first_num:]

        vector_name = f"fts_vector{self._underlang}{batch_num}"
        return f"""
            (SELECT {self.config['segment']}_id
            FROM {self.conf.schema}.{vector_name} vec
            WHERE {condition}) AS
        """

    def _stringify(self, prefilters: str) -> str:
        """
        Build the actual SQL query part
        """
        if not self.config["mapping"].get("hasFTS", True):
            return ""
        if not prefilters.strip():
            return ""

        # todo: remove list when main.corpus standardised
        col_data = cast(
            dict[str, str] | dict[str, dict[str, str]] | list,
            self.config["mapping"].get("FTSvectorCols", {}),
        )
        if not col_data:
            return ""
        if isinstance(col_data, list):
            col_data = {str(ix): name for ix, name in enumerate(col_data, start=1)}
        elif self._has_partitions:
            col_data = cast(dict[str, str], col_data.get(self.lang, col_data))
        locations: dict[str, str] = {
            name.split()[0]: str(ix)
            for ix, name in cast(dict[str, str], col_data).items()
        }
        prefilters = prefilters.format(**locations)
        return f"vec.vector @@ E'{prefilters}'"

    def initialise(self, query_json: QueryPart) -> dict[int, Any]:
        """
        todo: update this for latest query json
        """
        # query_json = query_json["query"]
        sections = defaultdict(list)
        count = 0
        for obj in query_json:
            if not isinstance(obj, dict):
                continue
            if "sequence" not in obj:
                continue
            seq = cast(dict[str, JSON], obj["sequence"])
            members = cast(list[dict[str, JSON]], seq["members"])
            for member in members:
                if "unit" not in member: continue # TODO: handle nested sequences
                cons = cast(dict[str, Any], member["unit"].get("constraints", []))
                # if not cons:
                #     count += 1
                #     continue
                # res = self._process_unit(cons)
                res = None # No constraints
                if cons:
                    res = self._process_unit(cons)
                sections[count].append(res)
        return dict(sections)

    def _condition(self) -> str:
        first_pass = self.initialise(self.query_json)
        strung = set()
        for k, v in first_pass.items():
            strung.add(self._build_one_prefilter(v))
        prefilters = self._finalise_prefilters(list(strung))
        return self._stringify(prefilters)

    def _process_unit(self, filt: dict[str, Any]) -> SingleNode | Conjuncted | None:
        """
        Handle unit json object
        """
        if len(filt) > 1:
            result = self._attempt_conjunct(filt, "AND")
            return result
        else:
            if "comparison" in filt[0]:
                key, op, type, text = _parse_comparison(filt[0]["comparison"])
                if "function" in key or type == "functionComparison":
                    return None
                key = key.get("entity","")
                if _is_prefix(text,op,type):
                    return SingleNode(key, op, text, type=="regexComparison")
            elif next(iter(filt[0]),"").startswith("logicalOp"):
                logic = next(iter(filt[0].values()),{})
                result = self._attempt_conjunct(logic.get('args',[]), logic.get('operator',"AND"))
                return result
        return None

    def _attempt_conjunct(
        self, filt: list[dict[str, Any]], kind: str
    ) -> Conjuncted | None:
        """
        Try to make prefilters from this OR-conjuncted token
        """
        matches: list[SingleNode | Conjuncted] = []

        for arg in sorted(filt, key=arg_sort_key):
            # todo recursive ... how to handle?
            if next(iter(arg),"").startswith("logicalOp"):
                logic = next(iter(arg.values()),{})
                result = self._attempt_conjunct(logic.get('args',[]), logic.get('operator',"AND"))
                if result:
                    matches.append(result)
                continue
            if "comparison" not in arg:
                continue
            key, op, type, text = _parse_comparison(arg["comparison"])
            if "function" in key or type == "functionComparison":
                continue # todo: check this?
            key = key["entity"]
            if _is_prefix(text,op,type):
                matches.append(SingleNode(key, op, text, type=="regexComparison"))
        
        # Do not use any prefilter at all for the disjunction if one of the disjuncts could not be used
        if kind == "OR" and len(matches) < len(filt):
            return None
         
        return Conjuncted(kind, matches) if matches else None

    def _as_string(self, item: Conjuncted | SingleNode) -> str:
        """
        Create a prefilter string with everything but the column-conversion
        """
        if isinstance(item, Conjuncted):
            return self._build_one_prefilter([item])
        elif isinstance(item, SingleNode):
            return item.as_prefilter()
        raise NotImplementedError("should not be here")

    def _build_one_prefilter(self, prefilter: list[Conjuncted | SingleNode | None]) -> str:
        """
        Create the prefilter string from a single item in the defaultdict value
        """
        all_made = []

        for prefilt in prefilter:
            if prefilt is None:
                all_made.append("(!1a|1a)") # Add a tautology
                continue
            
            single = []
            if isinstance(prefilt, Conjuncted):
                connective = prefilt.conj
                items = prefilt.items
            else:
                connective = None
                items = [prefilt]

            for item in items:
                strung = self._as_string(item)
                single.append(strung)

            if connective == "AND":
                joined = " & ".join(single)
            elif connective == "OR":
                joined = " | ".join(single)
            else:
                if connective == "NOT":
                    single[0] = "!" + single[0]
                joined = " <1> ".join(single)

            if len(single) > 1:
                joined = f"({joined})"
            all_made.append(joined)

        final = " <1> ".join(all_made).strip()
        return final

    def _finalise_prefilters(self, prefilters: list[str]) -> str:
        """
        todo: not sure if this remove is needed anymore
        """
        removable = set()
        for ix, s in enumerate(prefilters):
            s = s.strip()
            if any(i != s and s in i and (" " in i or "\n" in i) for i in prefilters):
                removable.add(ix)
        final = set([x for i, x in enumerate(prefilters) if i not in removable])

        return " & ".join(sorted(final))


def _is_prefix(query, op: str = "=", type = "string") -> bool:
    """
    Can a query be treated as a prefix in a prefilter? regex like ^a.* for example
    """
    if op not in ("=","!="):
        return False
    if type == "stringComparison":
        return True
    if query.startswith("^") and query.lstrip("^").isalnum():
        return True
    for pattern in sorted(SUFFIXES, key=len, reverse=True):
        if query.rstrip().endswith(pattern):
            query = query[: -len(pattern)]
            query = query.lstrip().lstrip("^")
            if not query or any(i in query for i in ".^$*+-?[]{}\\/()|"): # there remain regex characters in the rest of the pattern
                continue
            return True
    return False
