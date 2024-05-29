# needed for forward reference types
from __future__ import annotations

# TODO: call labeled_unbound_child_sequences on the main sequence and their generate as many selects on the CTEs as needed

import re

from typing import cast, Any, Self

from automathon import NFA  # type: ignore

from .constraint import _get_constraints
from .prefilter import Prefilter
from .sequence_members import Member, Disjunction, Sequence, Unit
from .typed import JSONObject, LabelLayer
from .utils import Config, _unique_label


# Helper function to retrieve a string of coordinated conditions for a token
def _where_conditions_from_constraints(
    conf: Config,
    label: str = "anonymous",
    constraints: list[dict] = [],
    entities: set[str] = set(),
    part_of: str = "",
    label_layer: LabelLayer = {},
) -> tuple[list[str], list[str]]:
    """
    Return a list of WHEREs to be conjoined and a list of LEFT JOINs to be conjoined
    """
    for e in entities:
        if e in label_layer:
            continue
        label_layer[e] = (conf.config["token"], {})

    cons = _get_constraints(
        cast(JSONObject, constraints),
        conf.config["token"],
        label,
        conf,
        entities=entities,
        part_of=part_of,
        label_layer=label_layer,
    )
    if cons is None:
        # return ([f"{label}.{conf.config['token'].lower()}_id > 0"], [])
        return (["1 = 1"], [])
    all_conditions: list[str] = []
    inner_conditions = cons.conditions()
    if inner_conditions:
        all_conditions.append(inner_conditions)
    join_conditions = []
    for k, v in cons.joins().items():
        if not isinstance(v, set):
            continue
        join_conditions += [c for c in v if isinstance(c, str) and len(c) > 0]
    all_conditions += join_conditions

    left_joins: list[str] = []
    for table, conds in cons.joins().items():
        if not isinstance(conds, set):
            continue
        real_conds: list = [c for c in conds if isinstance(c, str)]
        if not real_conds:
            continue
        left_joins.append(f"{table} ON {' AND '.join(real_conds)}")

    return (all_conditions, left_joins)


def _prefilter(conf: Config, subseq: list[Unit]) -> str:
    seq: dict = {"sequence": {"members": [s.obj for s in subseq]}}
    p = Prefilter([seq], conf, dict(), "")  # has_segment (remove?)
    return p._condition()


class State:

    def __init__(
        self, unit: Unit | None = None, constraints: list = [], destinations: list = []
    ):
        self.n: int = -1
        self.constraints: list = constraints
        self.destinations: list = [*destinations]
        self.unit: Unit | None = unit
        self.label: str = unit.label if unit else "anonymous"

    def __str__(self) -> str:
        destinations: list = []
        for d in self.destinations:
            if d is None:
                destinations.append("END")
            # todo: can this use typing.Self?
            elif isinstance(d, self.__class__):
                destinations.append(d.n)
        return f"State {self.n}: {self.constraints} > {destinations}"

    def to(self, state: Self) -> None:
        self.destinations.append(state)


class Cte:
    def __init__(
        self, sequence: SQLSequence, n: int = 0, fixed_token: Unit | None = None
    ):
        self.n: int = n
        self.sequence: SQLSequence = sequence
        self.prev_fixed_token: Unit | None = fixed_token
        self.members: list[Member] = []
        self.next_fixed_token: Unit | None
        self.no_transition: bool = False
        self.closed: bool = False

    def add_member(self, m: Member) -> None:
        self.members.append(m)

    def close(self, m: Unit | None) -> None:
        self.next_fixed_token = m

        self.states: dict[State, None] = dict()  # dict as ordered set

        self.start_state: State = State()
        self.end_state: State = State()
        self.states[self.start_state] = None

        current_state: State = self.start_state

        for me in self.members:
            current_state = self.add_state(me, current_state)

        current_state.to(self.end_state)
        self.states[self.end_state] = None

        # Create a graph dict to feed to automathon
        # effectivelty create indices (n) to refer to states
        state_map: dict[State, tuple[str, bool]] = {}
        for n, state in enumerate(self.states):
            state_map[state] = (str(n), not state.constraints)
        delta: dict[str, dict[str, set[str]]] = {}
        for state, (i, epsilon) in state_map.items():
            entry: dict[str, Any] = {}
            for d in state.destinations:
                key = "" if epsilon else str(i)
                set_entry = entry.get(key, set())
                set_entry.add(str(state_map[d][0]))
                entry[key] = set_entry
            delta[str(i)] = entry

        # Prepare params for automathon
        q = {str(s) for s in delta.keys()}
        sigma = {y for x in delta.values() for y in x if y}
        initial_state = str(state_map[self.start_state][0])
        f = {str(state_map[self.end_state][0])}

        # Create the raw automaton first, epsilon-free one then, and finally minimize it
        automaton_epsilon = NFA(q, sigma, delta, initial_state, f)
        automaton_no_epsilon = automaton_epsilon.remove_epsilon_transitions()
        automaton_minimized = automaton_no_epsilon.minimize()

        # We need to keep a map from the indices in the optimized automaton to the states themselves
        states_needed: dict[str, State] = {
            n: state
            for state, (n, _) in state_map.items()
            if n in automaton_minimized.sigma
        }
        self.optimal_graph: tuple[NFA, dict[str, State]] = (
            automaton_minimized,
            states_needed,
        )

        self.optional = automaton_minimized.initial_state in automaton_minimized.f

        self.closed = True

    def add_state(self, member: Member, source: State) -> State:

        if isinstance(member, Unit):
            next_state: State = State(
                unit=member, constraints=member.obj["unit"].get("constraints", [])
            )
            self.states[next_state] = None
            source.to(next_state)
            return next_state

        elif isinstance(member, Disjunction):
            destination: State = State()
            self.states[destination] = None
            for m in member.members:
                transit: State = self.add_state(m, source)
                transit.to(destination)
            return destination

        elif isinstance(member, Sequence):
            seq_start: State = State()
            seq_end: State = State()
            self.states[seq_start] = None
            self.states[seq_end] = None
            source.to(seq_start)

            transit = seq_start
            for m in member.members:
                transit = self.add_state(m, transit)
            transit.to(seq_end)

            if member.repetition[1] == -1:
                seq_end.to(seq_start)
            elif member.repetition[1] > 1:
                transit = seq_end
                for _ in range(member.repetition[1] - 1):
                    re_seq_start: State = State()
                    re_seq_end: State = State()
                    self.states[re_seq_start] = None
                    self.states[re_seq_end] = None
                    transit.to(re_seq_start)

                    transit = re_seq_start
                    for m in member.members:
                        transit = self.add_state(m, transit)
                    transit.to(re_seq_end)
                    transit = re_seq_end
                seq_end = transit

            if member.repetition[0] == 0:
                seq_start.to(seq_end)

            return seq_end

        return source

    def get_final_states(self) -> list[int]:
        automaton, _ = self.optimal_graph
        s = sorted(int(x) for x in automaton.f)
        if automaton.initial_state in automaton.f:
            s.append(-1)
        return s

    def where(
        self, which: str = "start", from_table: str = "fixed_parts", tok: str = "token"
    ) -> tuple[str, set[str]]:

        automaton, sigma_states = self.optimal_graph

        state_conds: set[str] = set()  # The conditions on each state transition
        state_left_joins: set[str] = (
            set()
        )  # The left joins necessary for the cross-table references

        infixwheres: list[str] = []

        delta_items: list[tuple] = [x for x in automaton.delta.items()]

        if which == "start":
            delta_items = [
                (k, v) for k, v in delta_items if k == automaton.initial_state
            ]

            pl: str = (
                self.prev_fixed_token.internal_label if self.prev_fixed_token else ""
            )
            nl: str = (
                self.next_fixed_token.internal_label if self.next_fixed_token else ""
            )

            if pl:
                infixwheres.append(f"token.{tok}_id = {from_table}.{pl} + 1")
            elif nl:
                infixwheres.append(
                    f"token.{tok}_id < {from_table}.{nl}"
                )  # In case this is the beginning of the segment

            if self.optional:
                # void_transition: str = f"transition{self.n}.dest_state = {max_n}"
                void_transition: str = f"transition{self.n}.dest_state = -1"
                if nl:
                    void_transition += f" AND token.{tok}_id = {from_table}.{nl} AND transition{self.n}.label = 'void'"
                    # Make sure the previous and next fixed tokens are adjacent!
                    if pl:
                        void_transition += (
                            f" AND {from_table}.{nl} = {from_table}.{pl} + 1"
                        )
                elif pl:
                    void_transition += f" AND token.{tok}_id = {from_table}.{pl} AND transition{self.n}.label = 'void'"
                else:
                    raise RuntimeError(
                        f"Cannot query a fully optional sequence ({self.sequence.sequence.label})"
                    )
                infixwheres.append(void_transition)
        else:
            all_destinations: set = {
                next(z for z in y) for x in automaton.delta.values() for y in x.values()
            }
            if automaton.initial_state not in all_destinations:
                delta_items = [
                    (k, v) for k, v in delta_items if k != automaton.initial_state
                ]

        for source_n, d in delta_items:

            for state_n, destinations in d.items():
                state: State = sigma_states[state_n]
                destination_n = next(x for x in destinations)
                override_references: dict[str, str] = {}
                for k, v in self.sequence._internal_references.items():
                    override_references[k] = f"{from_table}.{v}"
                override_references[state.label] = "token"
                where_conditions, ljs = self.sequence.get_constraints(
                    label="token",
                    constraints=[c for c in state.constraints if isinstance(c, dict)],
                    entities={"token"},
                    override=override_references,
                    part_of=state.unit.obj.get("partOf", "") if state.unit else "",
                )
                constraints = f"{' AND '.join(where_conditions)} AND transition{self.n}.label = '{state.label}'"
                state_left_joins = state_left_joins.union({ls for ls in ljs})
                source: str = ""
                if which != "start":
                    source = f"transition{self.n}.source_state = {source_n} AND "
                cond: str = (
                    f"{source}transition{self.n}.dest_state = {destination_n} AND {constraints}"
                )
                if self.next_fixed_token:
                    ref_table: str = (
                        from_table if which == "start" else f"traversal{self.n}"
                    )
                    cond += f" AND {ref_table}.{self.next_fixed_token.internal_label} = token.{tok}_id + 1"
                state_conds.add(f"({cond})")

        big_disjunction: str = "\n                OR ".join(state_conds)
        if not infixwheres:
            big_disjunction = f"{big_disjunction}"
        elif len(infixwheres) > 1:
            big_disjunction = f"""({infixwheres[-1]}) OR ( {infixwheres[0]} AND
            (
                {big_disjunction}
            )
        )"""
        else:
            big_disjunction = f"""({infixwheres[0]}) AND
            (
                {big_disjunction}
            )"""

        big_disjunction = big_disjunction.lstrip().rstrip()

        return (big_disjunction, state_left_joins)

    def transition(self) -> str:
        automaton, states = self.optimal_graph

        dict_table: dict[str, None] = (
            dict()
        )  # Make sure there's no duplicate, but keep order

        if automaton.initial_state in automaton.f:
            dict_table[f"({automaton.initial_state}, -1, 'void', '')"] = None

        for source, destinations in automaton.delta.items():
            for state_n, destination in destinations.items():
                state: State = states[state_n]
                references: dict[str, list] = {
                    y: []
                    for y in {
                        *{x for x in self.sequence._reserved_labels},
                        *{x for x in self.sequence._sequence_references},
                        *{x for x in self.sequence._internal_references},
                        *{x for x in self.sequence._internal_references.values()},
                    }
                }
                name_seq: str = (
                    state.unit.parent_sequence.label
                    if state.unit and state.unit.parent_sequence
                    else _unique_label(references)
                )
                for d in destination:
                    dict_table[f"({source}, {d}, '{state.label}', '{name_seq}')"] = None

        values: str = ",\n            ".join(dict_table.keys())
        return f"""transition{self.n} (source_state, dest_state, label, sequence) AS (
        VALUES
            {values}
    )"""

    def traversal(
        self,
        from_table: str = "fixed_parts",
        state_prev_cte: list[int] = [0],
        schema: str = "sparcling1",
        tok: str = "token",
        batch_suffix: str = "_enrest",
        seg: str = "segment",
    ) -> str:
        n: str = str(self.n)
        join_first_token: str = (
            f"{schema}.{tok}{batch_suffix} token ON token.{seg}_id = prev_cte.s"
        )
        if self.prev_fixed_token:
            pl: str = self.prev_fixed_token.internal_label
            joins: list[str] = [f"token.{tok}_id = prev_cte.{pl} + 1"]
            if self.optional:
                join_void: str = f"token.{tok}_id = prev_cte.{pl}"
                # Make sure the previous and next fixed tokens are adjacent!
                if self.next_fixed_token:
                    nl: str = self.next_fixed_token.internal_label
                    join_void = f"({join_void} AND prev_cte.{nl} = prev_cte.{pl} + 1)"
                joins.append(join_void)
            join_first_token += " AND " + (
                f"({' OR '.join(joins)})" if len(joins) > 1 else joins[0]
            )

        # We keep track of the traversed tokens in this CTE, but also across CTEs
        token_list: str = (
            f"jsonb_build_array(jsonb_build_array(token.token_id, transition{n}.label, transition{n}.sequence))"
        )
        # We keep track of the very first token also across CTEs
        start_id: str = f"token.{tok}_id"

        if not (from_table == "fixed_parts" or from_table.startswith("subseq")):
            # We need to make sure the previous CTE was run before selecting from it!
            prev_cte: Cte = next(
                c
                for n, c in enumerate(self.sequence.ctes)
                if self.sequence.ctes[n + 1] == self
            )
            orderby: str = (
                "" if prev_cte.no_transition else f"\n                ORDER BY ordercol"
            )
            from_table = f"""(
            SELECT *
                FROM {from_table}
                WHERE {from_table}.state IN ({','.join([str(s) for s in state_prev_cte])}){orderby}
            )"""
            # Append to the list of tokens from the previous CTE
            token_list = f"prev_cte.token_list || jsonb_build_array(jsonb_build_array(token.{tok}_id, transition{n}.label, transition{n}.sequence))"
            # Fetch start_id from the previous table
            start_id = f"prev_cte.start_id"

        where_start, left_joins_start = self.where("start", from_table="prev_cte")
        where_union, left_joins_union = self.where("union", from_table=f"traversal{n}")

        if left_joins_start:
            ljs: str = "\n        LEFT JOIN ".join(left_joins_start)
            where_start = f"""LEFT JOIN {ljs}
        WHERE {where_start}"""
        else:
            where_start = f"WHERE {where_start}"

        if where_union and left_joins_union:
            lju: str = "\n        LEFT JOIN ".join(left_joins_union)
            where_union = f"""LEFT JOIN {lju}
        WHERE {where_union}"""
        else:
            where_union = f"WHERE {where_union}" if where_union else ""

        retval: str = f"""traversal{n} AS (
        SELECT  prev_cte.s,
                {', '.join(['prev_cte.'+t.internal_label+' '+t.internal_label for t,_,_,_ in self.sequence.fixed_tokens])},
                {start_id}           start_id,
                token.{tok}_id           id,
                transition{n}.dest_state    state,
                {token_list} token_list
        FROM {from_table} prev_cte
        JOIN {join_first_token}
        JOIN transition{n} ON transition{n}.source_state = 0
        {where_start}"""

        if where_union:
            retval += f"""
        UNION ALL
        SELECT  traversal{n}.s,
                {', '.join(['traversal'+str(n)+'.'+t.internal_label+' '+t.internal_label for t,_,_,_ in self.sequence.fixed_tokens])},
                traversal{n}.start_id,
                token.token_id   id,
                transition{n}.dest_state,
                traversal{n}.token_list || jsonb_build_array(jsonb_build_array(token.{tok}_id, transition{n}.label, transition{n}.sequence))
        FROM traversal{n} traversal{n}
        JOIN transition{n} ON transition{n}.source_state = traversal{n}.state
        JOIN {schema}.{tok}{batch_suffix} token ON token.{tok}_id = traversal{n}.id + 1 AND token.{seg}_id = traversal{n}.s
        {where_union}
    )
    SEARCH DEPTH FIRST BY id SET ordercol"""

        else:
            self.no_transition = True
            retval += f"""
    )"""

        return retval


class SQLSequence:
    def __init__(
        self,
        sequence: Sequence,
        config: Config,
        entities: set[str] = set(),
        label_layer: LabelLayer = {},
    ):
        self.sequence: Sequence = sequence
        self.fixed_tokens: list[tuple[Unit, int, int, int]] = (
            []
        )  # A list of (fixed_token,min_separation,max_separation,modulo)
        self.ctes: list[Cte] = []  # We might need more than one recursive CTE
        self.simple_sequences: list[tuple[Unit, Sequence, Unit | None]] = (
            []
        )  # (fixed_token_before,simple_sequence,fixed_token_after)
        self._reserved_labels: dict[str, None] = {
            x: None for x in entities
        }  # Labels used by other sequences
        self._internal_references: dict[str, str] = (
            {}
        )  # A mapping from the original to the internal labels of the units
        self._sequence_references: dict[str, list[Member | Cte]] = (
            {}
        )  # A mapping from the labels of the sequences to the members/ctes it contains
        self._members: list[Member] | None = (
            None  # Set on first call to get_members; returned afterward
        )
        self.config: Config = config
        self.label_layer: LabelLayer = label_layer

    def get_members(self) -> list[Member]:
        if self._members is None:
            self._members = []
            # for m in self.sequence.members:
            for m in Member.from_obj(
                obj=self.sequence.obj, parent_sequence=self.sequence
            ):
                if isinstance(m, Unit):
                    self._members.append(m)
                else:
                    parent_sequence: Sequence = self.sequence
                    if isinstance(m, Sequence):
                        parent_sequence = m
                    self._members += Member.from_obj(
                        m.obj,
                        parent_sequence,
                        sequence_references=self._sequence_references,
                    )
        return self._members

    # No longer used?
    # def get_sequence_anchors(self):
    #     sequence_to_units_ctes: dict[str, tuple[ list[Unit], dict[Cte, set[str]] ]] = dict()

    #     for t, _, _, _ in self.fixed_tokens:
    #         for s in t.get_all_parent_sequences():
    #             if s.label not in sequence_to_units_ctes:
    #                 sequence_to_units_ctes[s.label] = ([], dict())
    #             sequence_to_units_ctes[s.label][0].append(t)

    #     for c in self.ctes:
    #         for m in c.members:
    #             for u in m.get_all_units():
    #                 for s in t.get_all_parent_sequences():
    #                     if s.label not in sequence_to_units_ctes:
    #                         sequence_to_units_ctes[s.label] = ([], dict())
    #                     old_set: set[str] = sequence_to_units_ctes[s.label][1].get(c, set())
    #                     sequence_to_units_ctes[s.label][1][c] = old_set.union({u.parent_sequence.label})

    #     return sequence_to_units_ctes

    # No longer used?
    # def get_sequence_member_occurrences(self, sequence: Sequence) -> tuple[ list[Unit], list[tuple[Cte,set[str]]] ]:
    #     """Return all the fixed tokens and tokens in CTEs (with their direct parent sequence's label) contained in the passed sequence"""
    #     units_in_fixed_tokens: list[Unit] = []
    #     for t, _, _, _ in self.fixed_tokens:
    #         if sequence.includes(t):
    #             units_in_fixed_tokens.append(t)

    #     list_cte: list[tuple[Cte,list[str]]] = []
    #     for c in self.ctes:
    #         units_in_cte: list[Unit] = []
    #         for m in c.members:
    #             if isinstance(m, Unit):
    #                 units_in_cte.append(m)
    #             elif isinstance(m, Disjunction) or isinstance(m, Sequence):
    #                 units_in_cte += m.get_all_units()
    #         labels: set[str] = set()
    #         for u in units_in_cte:
    #             if not sequence.includes(u):
    #                 continue
    #             if not isinstance(u.parent_sequence, Sequence):
    #                 continue
    #             labels.add(m.parent_sequence.label)
    #         list_cte.append( (c, labels) )

    #     return ( units_in_fixed_tokens , list_cte )

    def add_to_cte(
        self, to_add: Member, fixed_token: Unit | None, current_cte: Cte | None
    ) -> Cte:
        if current_cte is None:
            current_cte = Cte(self, len(self.ctes), fixed_token)
            self.ctes.append(current_cte)
        current_cte.add_member(to_add)
        return current_cte

    def close_cte(self, current_cte: Cte | None, token: Unit) -> None:
        if current_cte:
            current_cte.close(token)
        return None

    def next_member(self, n: int) -> Member | None:
        assert n >= 0, ValueError(
            f"Can only get next members starting from 0 (got {str(n)})"
        )
        if n + 1 >= len(self.get_members()):
            return None
        else:
            return self.get_members()[n + 1]

    def prev_member(self, n: int) -> Member | None:
        if n - 1 < 0 or n - 1 >= len(self.get_members()):
            return None
        else:
            return self.get_members()[n - 1]

    def fixed_ns(self, plus_one: bool = False) -> list[str]:
        # ret: list[str] = [f"t{str(n)}" for n, _ in enumerate(self.fixed_tokens)]
        ret: list[str] = [t.internal_label for t, _, _, _ in self.fixed_tokens]
        if plus_one:
            pass
            # ret.append(f"t{len(self.fixed_tokens)}")
        return ret

    def n_token(self, token: Unit) -> int:
        for n, u in enumerate(self.fixed_tokens):
            if token != u[0]:
                continue
            return n
        return -1

    def internal_reference(self, reference: str) -> str:
        return self._internal_references.get(reference, reference)

    def shifted_references_constraints(
        self, constraints: list[dict], override: dict[str, str] = {}
    ) -> list[dict]:
        """Shift the entity references in the constraints written by the user to their internal labels"""
        ret_constraints: list[dict] = []
        for c in constraints:
            new_constraint: dict[str, Any] = {}
            for k, v in c.items():
                if (k == "entity" or k == "entityComparison") and isinstance(v, str):
                    new_ref: str = self.internal_reference(v)
                    if override:
                        new_ref = override.get(v, v)
                    new_constraint[k] = new_ref
                elif isinstance(v, list) and all(isinstance(x, dict) for x in v):
                    new_constraint[k] = self.shifted_references_constraints(v, override)
                elif isinstance(v, dict):
                    new_constraint[k] = self.shifted_references_constraints(
                        [v], override
                    )[0]
                else:
                    new_constraint[k] = v
            ret_constraints.append(new_constraint)
        return ret_constraints

    def get_constraints(
        self,
        label: str = "anonymous",
        constraints: list[dict] = [],
        entities: set[str] = set(),
        override: dict[str, str] = dict(),
        part_of: str = "",
    ) -> tuple[list[str], list[str]]:
        """
        SQL-friendly constraints on a token using the provided label and entity references

        Provide an override dict to bypass the entity references (useful for bound references in subsequence tables and CTEs)
        """
        shifted_constraints: list[dict] = self.shifted_references_constraints(
            constraints=constraints, override=override
        )
        wheres_joins = _where_conditions_from_constraints(
            self.config,
            label,
            shifted_constraints,
            entities=entities,
            part_of=part_of,
            label_layer=self.label_layer,
        )
        # This hack needs to be handled upstream:
        # override is only set when the table in not the main one (fixed_parts)
        # so we replace any occurrence of l.layer_id with just l
        seg_lab: str = next(
            (
                lab
                for lab, lay in self.label_layer.items()
                if lay[0].lower() == self.config.config["segment"].lower()
            ),
            "",
        )
        if override and seg_lab:
            override[str(seg_lab)] = f"fixed_parts.{seg_lab}"
        refs_to_replace = "|".join(
            [
                f"{e}.{self.label_layer[e][0].lower()}_id"
                for e in override
                if e in self.label_layer
            ]
        )
        replacer = lambda m: m[1].split(".")[0]
        ret: list[list[str]] = [
            [re.sub(rf"\b({refs_to_replace})\b", replacer, x) for x in conds]
            for conds in wheres_joins
        ]
        return (ret[0], ret[1])

    def prefilters(self) -> set[str]:

        prefilters: list[list[Unit]] = [[]]
        for e in self.sequence.fixed_subsequences():
            if isinstance(e, Unit):
                prefilters[-1].append(e)
            elif isinstance(e, int):
                if e == 0:
                    continue
                if e < 0:
                    prefilters.append([])
                else:
                    for _ in range(e):
                        prefilters[-1].append(Unit({"unit": {}}, None))
        all_prefilters: set[str] = {_prefilter(self.config, p) for p in prefilters}
        return {
            p
            for p in all_prefilters
            if not any(p in p2 for p2 in all_prefilters if p2 != p)
        }

    def categorize_members(self, entities: dict[str, list] = dict()) -> None:
        """Identify and reference the sequence's fixed tokens, simple subsequences and recursive CTEs"""

        last_fixed_token: Unit | None = (
            None  # Keep track of the last processed fixed token
        )
        min_separation: int = (
            0  # Keep track of how far at least we are from the last fixed token
        )
        max_separation: int = (
            0  # Keep track of how far at most we are from the last fixed token
        )
        modulo: int = (
            -1
        )  # For simple sequences: separation is a multiple of len(subsequence.members)
        current_cte: Cte | None = None  # The CTE we're currently working on

        # Go through all the members and identify the fixed tokens, which can serve as anchors
        for n, m in enumerate(self.get_members()):

            if isinstance(m, Unit):
                # Do not use a label that's already used
                references: dict[str, list] = {
                    y: []
                    for y in {
                        *{x for x in entities},
                        *{x for x in self._reserved_labels},
                        *{x for x in self._sequence_references},
                        *{x for x in self._internal_references},
                        *{x for x in self._internal_references.values()},
                    }
                }
                m.internal_label = _unique_label(references, m.label)
                self._internal_references[m.label] = (
                    m.internal_label
                )  # f"t{len(self.fixed_tokens)}"
                self.fixed_tokens.append((m, min_separation, max_separation, modulo))
                self.close_cte(current_cte, m)
                current_cte = None
                last_fixed_token = m
                min_separation = 0
                max_separation = 0
                if m.parent_sequence:
                    self._sequence_references[m.parent_sequence.label] = (
                        self._sequence_references.get(m.parent_sequence.label, [])
                    )
                    self._sequence_references[m.parent_sequence.label].append(m)

            elif isinstance(m, Disjunction):
                # For now, treat all disjunctions as requiring a CTE
                current_cte = self.add_to_cte(m, last_fixed_token, current_cte)
                self._sequence_references[self.sequence.label] = [*m.members]

            elif isinstance(m, Sequence):

                if m.is_simple():
                    prev_member: Member | None = self.prev_member(n)
                    next_member: Member | None = self.next_member(n)

                    if isinstance(prev_member, Unit) and isinstance(next_member, Unit):
                        # Simple subsequences between two fixed tokens do not require a CTE
                        self.simple_sequences.append(
                            (cast(Unit, last_fixed_token), m, next_member)
                        )
                        modulo = len(m.members)
                        self._sequence_references[m.label] = [*m.members]
                    else:
                        # Simple subsequences still require a CTE if they start the sequence or if followed by a non-fixed member
                        current_cte = self.add_to_cte(m, last_fixed_token, current_cte)
                        self._sequence_references[m.label] = [current_cte]

                else:
                    # Use a CTE
                    current_cte = self.add_to_cte(m, last_fixed_token, current_cte)
                    self._sequence_references[m.label] = [current_cte]

            min_separation += m.min_length
            if max_separation == -1 or m.max_length == -1:
                max_separation = -1
            else:
                max_separation += m.max_length

            if m.max_length == m.min_length:
                modulo = -1

        if current_cte and not current_cte.closed:
            current_cte.close(None)

        self.entities: dict[str, list] = entities

    def where_fixed_members(
        self,
        entities: set[str] = set(),
        tok: str = "token",
        seg: str = "segment",
        seg_table_alias: str = "s",
    ) -> tuple[list[str], list[str]]:
        """Go through the sequence's fixed tokens and return a list of conditions + left joins as needed"""

        wheres: list[str] = []
        left_joins: list[str] = []
        for n, (token, min_sep, max_sep, modulo) in enumerate(self.fixed_tokens):
            l: str = token.internal_label
            part_of: str = token.obj["unit"].get(
                "partOf", self.sequence.obj["sequence"].get("partOf", "")
            )
            token_conds, token_left_joins = self.get_constraints(
                # label = f"t{n}",
                label=l,
                constraints=token.obj["unit"].get("constraints", []),
                entities=entities,
                part_of=part_of,
            )
            if token_left_joins:
                left_joins += token_left_joins
            #  conds: list[str] = [ f"{l}.{seg}_id = {seg_table_alias}.{seg}_id", *token_conds ]
            conds: list[str] = [*token_conds]
            if n > 0:
                pl: str = self.fixed_tokens[n - 1][0].internal_label
                if min_sep == max_sep:
                    conds.append(f"{l}.{tok}_id - {pl}.{tok}_id = {str(min_sep)}")
                else:
                    conds.append(f"{l}.{tok}_id - {pl}.{tok}_id > {str(min_sep-1)}")
                    if max_sep > min_sep:
                        conds.append(f"{l}.{tok}_id - {pl}.{tok}_id < {str(max_sep+1)}")
                    if modulo >= 0:
                        conds.append(
                            f"({l}.{tok}_id - {pl}.{tok}_id - 1) % {modulo} = 0"
                        )
            wheres += conds

        return (wheres, left_joins)

    def simple_sequences_table(
        self,
        fixed_part_ts: str,
        from_table: str = "fixed_parts",
        tok: str = "token",
        batch_suffix: str = "_enrest",
        seg: str = "segment",
        schema: str = "sparcling1",
    ) -> str:
        """Go through the sequence's simple subsequences and return an SQL string for a CTE named subseq"""

        if not self.simple_sequences:
            return ""

        simple_seq_conds: list[tuple[int, str]] = []
        # Go through the subsequences (will add an ALL for each)
        for n, (prev, s, nxt) in enumerate(self.simple_sequences):
            pl: str = prev.internal_label
            nl: str = nxt.internal_label if isinstance(nxt, Unit) else ""
            np: int = len(self.fixed_tokens)
            # n+1 of the last fixed token, used only if next_n<0:
            n_tokens: int = len(s.members)
            # lenght of the subsequence, used as modulo only if next_n<0:
            mod: int = len(s.members)
            sselect: str = ""
            wheres: str = ""
            from_cross: str = "\n                CROSS JOIN ".join(
                [
                    f"{schema}.{tok}{batch_suffix} s{n}_t{i}"
                    for i in range(len(s.members))
                ]
            )

            override_internal_references: dict[str, str] = {}
            # Map fixed tokens to the fixed_parts table
            for k, v in self._internal_references.items():
                override_internal_references[k] = f"{from_table}.{v}"
            # Temporarily map fixed token labels to subsequence-internal labels as applicable
            for i, m in enumerate(s.members):
                override_internal_references[cast(Unit, m).label] = f"s{n}_t{i}"
            # Entities is the set of local variables that point to a token row; references to token IDs are not in it
            entities: set[str] = {
                e for e in override_internal_references.values() if "." not in e
            }

            # Add conditions for each member in the subsequence
            for i, m in enumerate(s.members):
                token_conds, ljs = self.get_constraints(
                    f"s{n}_t{i}",
                    m.obj["unit"].get("constraints", []),
                    entities=entities,
                    override=override_internal_references,
                    part_of=m.obj["unit"].get("partOf", ""),
                )
                if ljs:
                    from_cross += f"\n                LEFT JOIN "
                    from_cross += f"\n                LEFT JOIN ".join(ljs)
                sselect += (",\n                    " if sselect else "") + (
                    " AND ".join(token_conds)
                )
                wheres += "\n                  AND " if wheres else ""
                wheres += " AND ".join(
                    [
                        x
                        for x in [
                            (
                                f"s{n}_t{i}.{tok}_id > {from_table}.{pl} AND (s{n}_t{i}.{tok}_id - {from_table}.{pl} - 1) % {n_tokens} = {i}"
                                if prev
                                else ""
                            ),
                            (f"s{n}_t{i}.{tok}_id < {from_table}.{nl}" if nxt else ""),
                            (
                                f"s{n}_t{i}.{tok}_id - s{n}_t{i-1}.{tok}_id = 1"
                                if i > 0
                                else ""
                            ),
                            f"s{n}_t{i}.{seg}_id = {from_table}.s",
                            (
                                f"s{n}_t{i}.{tok}_id <= t{np} AND ({from_table}.t{np} - s{n}_t{i}.{tok}_id) % {mod} = 0"
                                if nxt is None and i + 1 == len(s.members)
                                else ""
                            ),
                        ]
                        if x
                    ]
                )

            # The conditions on the tokens go in the SELECT; WHERE simply filters in tokens between the surrounding fixed token
            if not sselect:
                sselect = "1 = 1"
            every: str = f"""                SELECT
                {sselect}
            FROM {from_cross}
            WHERE {wheres}"""
            simple_seq_conds.append((n_tokens, every))
        # END for on subsequences

        # Now coordinate the subsequence conditions with AND ALL()
        simple_seq: str = "\n            AND ".join(
            [
                f"({','.join(['true' for _ in range(n)])}) = ALL(\n{a}\n            )"
                for n, a in simple_seq_conds
            ]
        )
        return f"""SELECT {fixed_part_ts}
            FROM {from_table}
            WHERE {simple_seq}"""
