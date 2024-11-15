# need this for the forward-reference type Sequence
from __future__ import annotations

# Rethink the approach:
#   from_obj should return a list of Members:
#       one element when a unit
#       one element when a disjunction (= a single unit if disjunction of single units)
#       one sequence element if a sequence with min of 0, or if first member is a disjunction or a sequence
#       units folowed by a sequence if a sequence with min of at least 1, and first member(s) is/are unit(s)
#           followed by units if last members are units (ie. no more left/right!)

from typing import cast

from .utils import Config, QueryData, _parse_repetition


class Member:

    def __init__(self, obj: dict, parent_sequence: Sequence | None, depth: int = 0):
        self.obj: dict = obj
        self.parent_sequence: Sequence | None = parent_sequence
        self.depth: int = depth
        self.min_length: int = 0
        self.max_length: int = 0
        self.need_cte: bool = False

    @staticmethod
    def from_obj(
        obj: dict,
        parent_sequence: Sequence,
        depth: int = 0,
        flatten_sequences: bool = True,
    ) -> list[Member]:
        if "unit" in obj:
            return [Unit(obj, parent_sequence, depth)]

        elif logic := obj.get("logicalExpression"):
            operator = logic.get("naryOperator", "")
            # Convert disjunctions of single tokens as a single token with a disjunction of constraints
            assert operator == "OR", TypeError(
                "Invalid logical operator passed to sequence"
            )
            members: list[dict] = logic.get("args", [])
            if all("unit" in m for m in members):
                disjunction_constraints: list[dict] = [
                    {
                        "logicalExpression": {
                            "naryOperator": "OR",
                            "args": [
                                c
                                for m in members
                                for c in m["unit"].get("constraints", [])
                            ],
                        }
                    }
                ]
                return [
                    Unit(
                        {
                            "unit": {
                                "label": members[0]["unit"].get("label", "anonymous"),
                                "constraints": disjunction_constraints,
                            }
                        },
                        parent_sequence,
                        depth,
                    )
                ]
            else:
                return [Disjunction(obj, parent_sequence, depth)]

        elif "sequence" in obj:
            if not flatten_sequences:
                return [
                    Sequence(
                        parent_sequence.query_data,
                        parent_sequence.conf,
                        obj,
                        parent_sequence,
                        depth,
                        flatten=False,
                    )
                ]

            ret_members: list[Member] = list()

            mini, maxi = _parse_repetition(obj["sequence"].get("repetition", "1"))

            if mini == 0:
                return [
                    Sequence(
                        parent_sequence.query_data,
                        parent_sequence.conf,
                        obj,
                        parent_sequence,
                        depth,
                    )
                ]
            else:
                # Create an optional sequence from a new object
                diff: int = -1 if maxi < 0 else maxi - mini
                str_max: str = "*" if diff < 0 else str(diff)
                newseqobj: dict = {
                    "sequence": {
                        "repetition": {"min": "0", "max": str_max},
                        "members": obj["sequence"].get("members", []),
                        "label": obj["sequence"].get("label"),
                        "partOf": obj["sequence"].get("partOf", ""),
                    }
                }
                optional_sequence: Sequence = Sequence(
                    parent_sequence.query_data,
                    parent_sequence.conf,
                    newseqobj,
                    parent_sequence,
                    depth + 1,
                )
                # The members must appear min: return them as individual members
                for _ in range(mini):
                    for m in obj["sequence"].get("members", []):
                        ret_members += Member.from_obj(
                            m,
                            optional_sequence,
                            depth + 1,
                        )
                if diff:
                    ret_members.append(optional_sequence)

                return ret_members

        else:
            raise TypeError(f"Unsupported type of sequence member: {obj}")

    def get_all_parent_sequences(self) -> list[Sequence]:
        ret: list[Sequence] = []
        if isinstance(self.parent_sequence, Sequence):
            ret.append(self.parent_sequence)
            ret += self.parent_sequence.get_all_parent_sequences()
        return ret

    def get_all_units(self) -> list[Unit]:
        all_units: list[Unit] = []

        if isinstance(self, Unit):
            all_units.append(self)

        else:
            for m in cast(Sequence, self).members:
                if isinstance(m, Unit):
                    all_units.append(m)
                elif isinstance(m, Disjunction) or isinstance(m, Sequence):
                    all_units += m.get_all_units()

        return all_units


class Unit(Member):
    def __init__(
        self,
        obj: dict,
        parent_sequence: Sequence,
        depth: int = 0,
    ):
        super().__init__(obj, parent_sequence, depth)
        query_data: QueryData = parent_sequence.query_data
        unit_layer = parent_sequence.conf.config["firstClass"]["token"]
        self.label: str = str(obj["unit"].get("label", ""))
        self.internal_label = self.label
        sequence_unit_labels = [
            cast(Unit, u).internal_label
            for u in parent_sequence.members
            if isinstance(u, Unit)
        ]
        if not self.label or self.label in sequence_unit_labels:
            self.internal_label = query_data.unique_label(layer=unit_layer)
        self.depth: int = depth
        self.min_length: int = 1
        self.max_length: int = 1
        parent_sequence.members.append(self)

    def str_constraints(self) -> list[str]:
        cs: list[str] = []
        for c in self.obj["unit"].get("constraints", []):
            if "comparison" not in c:
                continue
            cs.append(
                f"{c['comparison'].get('entity','.')}{c['comparison'].get('operator','=')}{next(v for k,v in c['comparison'].items() if k.endswith('Comparison'))}"
            )
        return cs

    def __str__(self) -> str:
        ret: str = self.label
        cs: list[str] = self.str_constraints()
        if cs:
            ret += f"[{' & '.join(cs)}]"
        return ret


class Disjunction(Member):
    def __init__(self, obj: dict, parent_sequence: Sequence, depth: int = 0):
        super().__init__(obj, parent_sequence, depth)
        args: list = obj["logicalExpression"].get("args", [])
        # Don't extract units from sub-sequences when those are inside a disjunction (otherwise they would become disjuncts too!)
        self.members: list[Member] = [
            x
            for a in args
            for x in Member.from_obj(
                a,
                parent_sequence,
                depth + 1,
                flatten_sequences=False,
            )
        ]
        self.min_length: int = min(sm.min_length for sm in self.members)
        self.max_length: int = 0
        for sm in self.members:
            if sm.max_length == -1:
                self.max_length = -1
                break
            if sm.max_length > self.max_length:
                self.max_length = sm.max_length

    def __str__(self) -> str:
        return f"({' | '.join([str(m) for m in self.members])})"


class Sequence(Member):
    def __init__(
        self,
        query_data: QueryData,
        conf: Config,
        obj: dict,
        parent_sequence: Sequence | None = None,
        depth: int = 0,
        flatten: bool = False,
        # sequence_references: dict[str, list] = dict(),
    ):
        super().__init__(obj, parent_sequence, depth)

        self.query_data: QueryData = query_data
        self.conf: Config = conf

        if obj["sequence"].get("label"):
            self.anonymous = False
            self.label: str = obj["sequence"]["label"]
        else:
            self.anonymous = True
            self.label = str(
                obj["sequence"].get("label", self.query_data.unique_label())
            )

        self.members: list[Member] = []
        obj_members = obj["sequence"].get("members", [])
        for m in obj_members:
            Member.from_obj(
                m,
                self,
                depth + 1,
                flatten_sequences=flatten,
            )

        self.fixed: list[Member] = []

        self.need_cte: bool = any(
            isinstance(m, Disjunction) or (isinstance(m, Sequence) and m.need_cte)
            for m in self.members
        )

        mini, maxi = _parse_repetition(obj["sequence"].get("repetition", "1"))
        self.repetition: tuple[int, int] = (mini, maxi)

        if mini == 0:
            self.min_length = 0
        else:
            self.min_length = mini * sum(sm.min_length for sm in self.members)

        if maxi == -1:
            self.max_length = -1
        else:
            if any(sm.max_length < 0 for sm in self.members):
                self.max_length = -1
            else:
                self.max_length = maxi * sum(sm.max_length for sm in self.members)

    def is_simple(self) -> bool:
        """Whether the members of this sentence are all units"""
        return all(isinstance(m, Unit) for m in self.members)

    def includes(self, member: Member) -> bool:
        """Whether this sentence include the member anywhere down the pipe"""
        if member in self.members:
            return True
        parent_sequence: Sequence | None = member.parent_sequence
        while parent_sequence and parent_sequence is not self:
            parent_sequence = parent_sequence.parent_sequence
        return parent_sequence is self

    def labeled_unbound_child_sequences(self) -> list[Sequence]:
        """All the unbound user-labeled sub-sequences contained in this sequence"""
        # If this sequence is optional or repeats itself, all the references it contains are bound
        if self.repetition[0] != 1 or self.repetition[1] != 1:
            return []
        subseq: list[Sequence] = []
        for m in self.members:
            if isinstance(m, Unit):
                continue
            if isinstance(m, Disjunction):
                continue
            if isinstance(m, Sequence):
                if not m.anonymous:
                    subseq.append(m)
                subseq += m.labeled_unbound_child_sequences()
        return subseq

    def fixed_subsequences(self) -> list[int | Unit]:
        """All the fixed subsequences that can be built from this sequence (for prefiltering purposes)"""
        if self.repetition[0] == 0:
            return [-1]

        subseq: list[int | Unit] = []
        sep: int = 0
        for m in self.members:
            if isinstance(m, Unit):
                subseq.append(sep)
                subseq.append(m)
                sep = 0
            elif isinstance(m, Disjunction):
                if m.min_length > 0 and m.max_length == m.min_length and sep >= 0:
                    sep += m.min_length
                else:
                    sep = -1
            elif isinstance(m, Sequence):
                for e in m.fixed_subsequences():
                    if isinstance(e, int):
                        if e >= 0 and sep >= 0:
                            sep += e
                        else:
                            sep = -1
                    subseq.append(e)
                    sep = 0

        subseq.append(sep)

        repeated_subseq: list[int | Unit] = [
            x for a in [subseq for _ in range(self.repetition[0])] for x in a
        ]

        if self.repetition[1] < 0 or self.repetition[1] > self.repetition[0]:
            repeated_subseq.append(-1)
            repeated_subseq += subseq

        return repeated_subseq

    def __str__(self) -> str:
        """Helper string representation"""
        ret: str = " ".join([str(m) for m in self.members])
        ret = f"({ret})"
        mini, maxi = self.repetition
        if maxi == -1:
            if mini == 0:
                ret += "*"
            elif mini == 1:
                ret += "+"
            else:
                ret += "{" + str(mini) + ",}"
        elif maxi == 1:
            if mini == 0:
                ret += "?"
        elif mini == maxi:
            ret += "{" + str(mini) + "}"
        else:
            ret += "{" + str(mini) + "," + str(maxi) + "}"
        return ret