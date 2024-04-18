"""
QueryIteration: an object representing a linguistic query and possibly a second
query that fetches accompanying prepared_segment data that a frontend can use
to display query results as KWIC or similar

The object is created in one of two ways:

cls.from_request(): when a new query is created by the frontend, an existing
query is resumed (i.e. for pagination), or user does 'Search entire corpus'.
In this case, the data is given as HTTP POST JSON data via the /query endpoint.

cls.from_manual(): when an existing query does not yet have enough results, or
when a query is for all batches in a corpus and there are unqueried batches. In
this case, we are *almost* simulating a new HTTP request. The process is
started in sock.py when the query status is `partial`.

Once the object is made, it can be used to decide the batch to query next, as
well as whether or not a sentences query is also needed, and so on.

Most of the object's attributes are passed on to the Query Service in the query
and/or sentences query. The job's callback, usually running in a worker thread,
publishes a Redis message containing query results as well as most of the qi's
attributes. This message is heard by a listener in the main thread (sock.py),
which can broadcast it to frontends via websockets, and trigger new jobs if need
be. Then the process repeats...
"""

import json
import os
import re

# import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

from abstract_query.create import json_to_sql
from aiohttp import web

# we need the below to avoid a mypy keyerror in the type annotation:
from aiohttp.web import Application
from rq.job import Job

# wish there was a nicer way to do this...delete once we are sure of 3.11+
from typing import Self

from .configure import CorpusConfig
from .dqd_parser import convert
from .typed import Batch, JSONObject, Query, Results
from .utils import _determine_language, _layer_contains, push_msg

QI_KWARGS = dict(kw_only=True, slots=True)


@dataclass(**QI_KWARGS)
class QueryIteration:
    """
    Model an iteration of a query, with all its associated settings
    """

    config: dict[str, CorpusConfig]
    user: str
    room: str | None
    query: str
    corpora: list[int]
    all_batches: list[Batch]
    total_results_requested: int
    needed: int
    page_size: int
    languages: set[str]
    simultaneous: str
    sentences: bool
    is_vian: bool
    app: Application
    resume: bool = False
    previous: str = ""
    current_kwic_lines: int = 0
    current_batch: Batch | None = None
    total_duration: float = 0.0
    done_batches: list[Batch] = field(default_factory=list)
    total_results_so_far: int = 0
    existing_results: Results = field(default_factory=dict)
    job: Job | None = None
    job_id: str = ""
    from_memory: bool = False
    dqd: str = ""
    sql: str = ""
    start_query_from_sents: bool = False
    no_more_data: bool = False
    full: bool = False
    offset: int = 0
    jso: Query = field(default_factory=dict)
    meta: dict[str, list[JSONObject]] = field(default_factory=dict)
    job_info: dict[str, str | bool | list[str]] = field(default_factory=dict)
    word_count: int = 0
    iteration: int = 0
    first_job: str = ""
    query_depends: list[str] = field(default_factory=list)
    dep_chain: list[str] = field(default_factory=list)
    post_processes: dict[int, Any] = field(default_factory=dict)
    to_export: dict[str, Any] = field(default_factory=dict)

    def make_query(self) -> None:
        """
        Do any necessary query conversions

        Produces: the DQD/None, JSON, SQL and SQL metadata objects
        """
        if self.current_batch is None:
            raise ValueError("Batch not found")

        kwa = dict(
            schema=self.current_batch[1],
            batch=self.current_batch[2],
            config=self.app["config"][str(self.current_batch[0])],
            lang=self._determine_language(self.current_batch[2]),
            vian=self.is_vian or "tangram" in self.current_batch[1].lower(),
        )
        if self.jso:
            json_query = self.jso
        else:
            try:
                json_query = json.loads(self.query)
                json_query = json.loads(json.dumps(json_query, sort_keys=True))
            except json.JSONDecodeError:
                json_query = convert(self.query, self.config)
                self.dqd = self.query

        res = cast(list[dict[str, dict[str, Any]]], json_query.get("results", []))
        has_kwic = any("resultsPlain" in r for r in res)

        if not has_kwic:
            self.sentences = False

        sql_query, meta_json, post_processes = json_to_sql(json_query, **kwa)
        self.jso = json_query
        self.sql = sql_query
        self.meta = meta_json
        self.post_processes = post_processes
        return None

    @staticmethod
    def _get_query_batches(
        corpora: list[int],
        config: dict[str, CorpusConfig],
        languages: set[str],
        is_vian: bool,
    ) -> list[Batch]:
        """
        Get a list of tuples in the format of (corpus, batch, size) to be queried

        todo: make this not static
        """
        out: list[Batch] = []
        all_languages = ["en", "de", "fr", "ca", "it", "rm"]
        all_langs = tuple([f"_{la}" for la in all_languages])
        langs = tuple([f"_{la}" for la in languages])
        for corpus in corpora:
            batches = config[str(corpus)]["_batches"]
            for name, size in batches.items():
                stripped = name.rstrip("0123456789")
                if stripped.endswith("rest"):
                    stripped = stripped[:-4]
                if not stripped.endswith(langs) and stripped.endswith(all_langs):
                    continue
                schema = config[str(corpus)]["schema_path"]
                out.append((corpus, schema, name, size))
        return sorted(out, key=lambda x: x[-1])

    @classmethod
    async def from_request(cls, request: web.Request) -> Self:
        """
        The first time we encounter the data, it's an aiohttp request

        Normalise it into this dataclass.

        Also used when query is resumed, or when user does `Search entire corpus`
        """
        request_data = await request.json()
        corp = request_data.get("corpora", [])
        if not isinstance(corp, list):
            corp = [corp]
        corpora_to_use = [int(i) for i in corp]
        langs = [i.strip() for i in request_data.get("languages", ["en"])]
        languages = set(langs)
        total_requested = request_data.get("total_results_requested", 1000)
        previous = request_data.get("previous", "")
        first_job_id = ""
        total_duration = 0.0
        total_results_so_far = 0
        needed = total_requested
        if previous:
            prev = Job.fetch(previous, connection=request.app["redis"])
            first_job_id = prev.kwargs.get("first_job") or previous
            total_duration = prev.kwargs.get("total_duration", 0.0)
            total_results_so_far = prev.meta.get("total_results_so_far", 0)
            needed = -1  # to be figured out later
        is_vian = request_data.get("appType") == "vian"
        sim = request_data.get("simultaneous", False)
        all_batches = cls._get_query_batches(
            corpora_to_use, request.app["config"], languages, is_vian
        )

        details = {
            "corpora": corpora_to_use,
            "user": request_data["user"],
            "app": request.app,
            "room": request_data["room"],
            "config": request.app["config"],
            "page_size": request_data.get("page_size", 20),
            "all_batches": all_batches,
            "sentences": request_data.get("sentences", True),
            "languages": set(langs),
            "full": request_data.get("full", False),
            "query": request_data["query"],
            "resume": request_data.get("resume", False),
            "total_results_requested": total_requested,
            "needed": needed,
            "current_kwic_lines": request_data.get("current_kwic_lines", 0),
            "total_duration": total_duration,
            "first_job": first_job_id,
            "total_results_so_far": total_results_so_far,
            "simultaneous": str(uuid4()) if sim else "",
            "previous": previous,
            # "is_vian": is_vian,
            "is_vian": False
        }
        if request_data.get("to_export", False):
            details["to_export"] = {
                "format": str(request_data["to_export"]),
                "config": request.app["config"][str(corpora_to_use[0])],
                "user": request_data.get("user", ""),
                "room": request_data.get("room", "")
            }
        made: Self = cls(**details)
        made.get_word_count()
        return made

    def get_word_count(self) -> None:
        """
        Sum the word counts for corpora being searched
        """
        if self.word_count:
            return None
        total = 0
        for corpus in self.corpora:
            conf = self.app["config"][str(corpus)]
            try:
                has_partitions = "partitions" in conf["mapping"]["layer"][conf["token"]]
            except (KeyError, TypeError):
                has_partitions = False
            if not has_partitions or not self.languages:
                total += sum(conf["token_counts"].values())
            else:
                counts = conf["token_counts"]
                for name, num in counts.items():
                    for lang in self.languages:
                        if name.rstrip("0").endswith(lang):
                            total += num
                            break
        self.word_count = total
        return None

    async def submit_query(self) -> tuple[Job, bool | None]:
        """
        Helper to submit a query job to the Query Service
        """
        job: Job
        if self.offset > 0:
            job = Job.fetch(self.previous, connection=self.app["redis"])
            self.job = job
            self.job_id = job.id
            return job, False

        parent: str | None = None
        parent = self.job_id if self.job is not None else None

        query_kwargs = dict(
            original_query=self.query,
            user=self.user,
            room=self.room,
            needed=self.needed,
            total_results_requested=self.total_results_requested,
            done_batches=self.done_batches,
            all_batches=self.all_batches,
            current_batch=self.current_batch,
            total_results_so_far=self.total_results_so_far,
            corpora=self.corpora,
            existing_results=self.existing_results,
            sentences=self.sentences,
            page_size=self.page_size,
            post_processes=self.post_processes,
            debug=self.app["_debug"],
            resume=self.resume,
            languages=list(self.languages),
            simultaneous=self.simultaneous,
            full=self.full,
            total_duration=self.total_duration,
            is_vian=self.is_vian,
            current_kwic_lines=self.current_kwic_lines,
            dqd=self.dqd,
            first_job=self.first_job,
            jso=self.jso,
            sql=self.sql,
            meta_json=self.meta,
            word_count=self.word_count,
            parent=parent,
            to_export=self.to_export
        )

        queue = "query" if not self.full else "background"

        do_sents: bool | None
        job, do_sents = await self.app["query_service"].query(
            self.sql, depends_on=self.query_depends, queue=queue, **query_kwargs
        )
        self.job = job
        self.job_id = job.id
        if not self.first_job:
            self.first_job = job.id
        return job, do_sents

    def submit_sents(self, query_started: bool | None) -> list[str]:
        """
        Helper to submit a sentences job to the Query Service
        """
        depends_on = self.job_id if self.job_id else self.previous
        to_use: list[str] | str = []
        if self.simultaneous and depends_on:
            self.dep_chain.append(depends_on)
            to_use = self.dep_chain
        elif depends_on:
            to_use = depends_on

        offset = max(0, self.offset)
        needed = self.needed if not self.full else -1

        kwargs = dict(
            user=self.user,
            room=self.room,
            full=self.full,
            resume=self.resume,
            query_started=query_started,
            from_memory=self.from_memory,
            simultaneous=self.simultaneous,
            no_more_data=self.no_more_data,
            debug=self.app["_debug"],
            first_job=self.first_job or self.job_id,
            dqd=self.dqd,
            current_kwic_lines=self.current_kwic_lines,
            start_query_from_sents=self.start_query_from_sents,
            jso=json.dumps(self.jso, indent=4),
            sql=self.sql,
            offset=offset,
            needed=needed,
            total_results_requested=self.total_results_requested,
        )
        queue = "query" if not self.full else "background"
        qs = self.app["query_service"]
        sents_jobs: list[str] = qs.sentences(
            self.sents_query(),
            meta=self.meta_query(),
            depends_on=to_use,
            queue=queue,
            **kwargs,
        )
        return sents_jobs

    @staticmethod
    def _determine_language(batch: str) -> str | None:
        """
        Helper to find language from batch
        """
        return _determine_language(batch)

    def _parent_of(self, child: str, parent: str) -> bool:
        if not self.current_batch:
            raise ValueError("Need batch")
        config = self.config[str(self.current_batch[0])]
        return _layer_contains(config, parent, child)

    def _is_time_anchored(self, layer: str) -> bool:
        if not self.current_batch:
            raise ValueError("Need batch")
        config = self.config[str(self.current_batch[0])]
        layer_config = config["layer"].get(layer)
        if not layer_config:
            return False
        if "anchoring" in layer_config:
            return layer_config["anchoring"].get("time", False)
        if "contains" in layer_config:
            return self._is_time_anchored(
                layer_config.get("contains", config["firstClass"]["token"])
            )
        return False

    def meta_query(self) -> str:
        """
        Build a query to fetch meta of connected layers, with a placeholder param :ids

        The placeholder cannot be calculated until the associated query job
        has finished, so we get those in jobfuncs._db_query with _get_sent_ids
        """
        if not self.current_batch:
            raise ValueError("Need batch")
        config = self.config[str(self.current_batch[0])]
        seg = config["segment"]
        schema = self.current_batch[1]
        lang = self._determine_language(self.current_batch[2])
        name = seg.strip()
        underlang = f"_{lang}" if lang else ""
        batch_suffix: str = ""
        # token: str = config['token']
        # token_mapping = config['mapping']['layer'][token]
        # n_batches: int = token_mapping.get("batches", token_mapping.get("partitions",{}).get(lang,{}).get("batches",1))
        # if n_batches > 1:
        batch_rgx = f"{config['token'].lower()}{underlang}([0-9]+|rest)$"
        batch_match = re.match(
            rf"{batch_rgx.lower()}", str(self.current_batch[2]).lower()
        )
        if batch_match:
            batch_suffix = batch_match[1]
        seg_name = f"{name}{underlang}{batch_suffix}".lower()

        parents_of_seg = [k for k in config["layer"] if self._parent_of(seg, k)]
        parents_with_attributes = {
            k: None for k in parents_of_seg if config["layer"][k].get("attributes")
        }
        # Make sure to include Document in there, even if it's not a parent of Segment
        if config["layer"][config["document"]].get("attributes"):
            parents_with_attributes[config["document"]] = None

        parents_with_attributes[seg] = None  # Also query the segment layer itself
        selects = [f"s.{name}_id AS seg_id"]
        froms = [f"{schema}.{seg_name} s"]
        wheres = [f"s.{name}_id = ANY(:ids)"]
        joins = []
        for layer in parents_with_attributes:
            alias = layer
            layer_mapping = config["mapping"]["layer"].get(layer, {})
            mapping_attrs = layer_mapping.get("attributes", {})
            attributes: dict[str, Any] = {
                k: v for k, v in config["layer"][layer].get("attributes", {}).items()
            }
            prefix_id: str
            partitions = layer_mapping.get("partitions")
            alignment = layer_mapping.get("alignment", {})
            relation = (
                alignment.get("relation")
                if alignment
                else layer_mapping.get("relation", layer.lower())
            )
            if not relation and lang and partitions:
                relation = partitions.get(lang, {}).get("relation")
            if layer == seg:
                # The segment table is the main from table aliased as 's' so make sure to use it
                alias = "s"
                relation = None
                partitions = None
                alignment = None
            if alignment:
                prefix_id = "alignment"
            # hard-coded exception management -- change document_id for movie_id in open subtitles
            elif (
                layer == config["document"] and layer.lower() == "movie"
            ):  # not partitions:
                prefix_id = "document"
            else:
                prefix_id = layer.lower()
            # Select the ID
            selects.append(f"{alias}.{prefix_id}_id AS {layer}_id")
            for attr, v in attributes.items():
                # Quote attribute name (is arbitrary)
                attr_name = f'"{attr}"'
                attr_mapping = mapping_attrs.get(attr, {})
                # Mapping is "relation" for dict-like attributes (eg ufeat or agent)
                if attr_mapping.get("type","") == "relation":
                    attr_table = attr_mapping.get("name","")
                    # Join the lookup table
                    joins.append(
                        f"{schema}.{attr_table} {attr_table} ON {alias}.{attr}_id = {attr_table}.{attr}_id"
                    )
                    # Select the attribute from the lookup table
                    selects.append(f'{attr_table}.{attr_name} AS {layer}_{attr}')
                else:
                    # Make sure one gets the data in a pure JSON format (not just a string representation of a JSON object)
                    if attr == "meta":
                        attr_name = f"meta::jsonb"
                    selects.append(f'{alias}.{attr_name} AS {layer}_{attr}')
            # Will get char_range from the appropriate table
            char_range_table: str = alias
            # join tables
            if lang and partitions:
                interim_relation = partitions.get(lang, {}).get("relation")
                if not interim_relation:
                    # This should never happen?
                    continue
                if alignment and relation:
                    # The partition table is aligned to a main document table
                    joins.append(
                        f"{schema}.{interim_relation} {alias}_{lang} ON {alias}_{lang}.char_range @> s.char_range"
                    )
                    joins.append(
                        f"{schema}.{relation} {alias} ON {alias}_{lang}.alignment_id = {alias}.alignment_id"
                    )
                    char_range_table = f"{alias}_{lang}"
                else:
                    # This is the main document table for this partition
                    joins.append(
                        f"{schema}.{interim_relation} {layer} ON {alias}.char_range @> s.char_range"
                    )
            elif relation:
                joins.append(
                    f"{schema}.{relation} {alias} ON {layer}.char_range @> s.char_range"
                )
            # Get char_range from the main table
            selects.append(f'{char_range_table}."char_range" AS {layer}_char_range')
            # And frame_range if applicable
            if self._is_time_anchored(layer):
                selects.append(
                    f'{char_range_table}."frame_range" AS {layer}_frame_range'
                )

        selects_formed = ", ".join(selects)
        froms_formed = ", ".join(froms)
        wheres_formed = " AND ".join(wheres)
        joins_formed = " LEFT JOIN ".join(
            joins
        )  # left join = include non-empty entities even if other ones are empty
        joins_formed = "" if not joins_formed else f" LEFT JOIN {joins_formed}"
        script = f"SELECT -2::int2 AS rstype, {selects_formed} FROM {froms_formed}{joins_formed} WHERE {wheres_formed};"
        print("meta script", script)
        return script

    def sents_query(self) -> str:
        """
        Build a query to fetch sentences, with a placeholder param :ids

        The placeholder cannot be calculated until the associated query job
        has finished, so we get those in jobfuncs._db_query with _get_sent_ids
        """
        if not self.current_batch:
            raise ValueError("Need batch")
        schema = self.current_batch[1]
        lang = self._determine_language(self.current_batch[2])
        config = self.config[str(self.current_batch[0])]
        seg = config["segment"]
        name = seg.strip()
        underlang = f"_{lang}" if lang else ""
        seg_name = f"prepared_{name}{underlang}"
        annotations: str = ""
        for layer, properties in config['layer'].items():
            if layer == seg or properties.get("contains","") != config["token"]:
                continue
            annotations = ", annotations"
            break
        script = f"SELECT {name}_id, off_set, content{annotations} FROM {schema}.{seg_name} WHERE {name}_id = ANY(:ids);"
        return script

    @classmethod
    async def from_manual(cls, manual: JSONObject, app: web.Application) -> Self:
        """
        For subsequent queries (i.e. over non-initial batches), there is no request;
        the request handler is manually called with JSON data instead of a request object.

        The non-serialisable `app` is passed in separately.
        """
        job_id = cast(str, manual["job"])
        job = Job.fetch(job_id, connection=app["redis"])

        done_batches = [
            tuple(i) for i in cast(list[Sequence[str | int]], manual["done_batches"])
        ]
        cur = cast(Sequence[int | str], manual["current_batch"])
        # sorry about this:
        current: Batch = (int(cur[0]), str(cur[1]), str(cur[2]), int(cur[3]))
        if current not in done_batches:
            done_batches.append(current)
        all_batches = [
            tuple(i) for i in cast(list[Sequence[int | str]], manual["all_batches"])
        ]

        corpora_to_use = cast(list[int], manual["corpora"])

        tot_req = cast(int, manual["total_results_requested"])
        tot_so_far = cast(int, manual["total_results_so_far"])
        needed = tot_req - tot_so_far if tot_req > 0 else -1
        from_memory = manual.get("from_memory", False)
        sentences = manual.get("sentences", True)

        details = {
            "corpora": corpora_to_use,
            "existing_results": manual.get("full_result", manual["result"]),
            "user": manual["user"],
            "room": manual["room"],
            "job": job,
            "app": app,
            "jso": job.kwargs["jso"],
            "job_id": manual["job"],
            "config": app["config"],
            "full": manual.get("full", False),
            "word_count": manual["word_count"],
            "simultaneous": manual.get("simultaneous", ""),
            "needed": needed,
            "previous": manual.get("previous", ""),
            "page_size": job.kwargs.get("page_size", 20),
            "resume": manual.get("resume", False),
            "total_results_requested": tot_req,
            "first_job": manual["first_job"],
            "query": job.kwargs["original_query"],
            "sentences": sentences,
            "from_memory": from_memory,
            "offset": manual["offset"],
            "total_duration": manual["total_duration"],
            "all_batches": all_batches,
            "current_kwic_lines": manual["current_kwic_lines"],
            "total_results_so_far": tot_so_far,
            "languages": set(cast(list[str], manual["languages"])),
            "done_batches": done_batches,
            "is_vian": manual.get("is_vian", False),
            "to_export": manual.get("to_export", job.kwargs.get("to_export", ""))
        }
        return cls(**details)

    def decide_batch(self) -> Batch | None:
        """
        Find the best next batch to query.

        Pick the smallest batch first. Query the smallest available result until
        `page_size` results are collected. Then, using the result count of the
        queried batches and the word counts of the remaining batches, predict the
        smallest next batch that's likely to yield enough results and return it.

        If no batch is predicted to have enough results, pick the smallest
        available (so more results go to the frontend faster).
        """
        if self.current_batch is not None:
            return None

        if self.done_batches and self.resume:
            if len(self.done_batches) == len(self.all_batches):
                self.start_query_from_sents = False
                self.no_more_data = True
            return self.done_batches[-1]

        if not self.resume and len(self.done_batches) == len(self.all_batches):
            self.no_more_data = True
            return None

        buffer = 0.1  # set to zero for picking smaller batches

        so_far = self.total_results_so_far
        if self.is_vian:
            if self.done_batches:
                raise ValueError("VIAN corpora have only one batch!?")
            return self.all_batches[0]

        if not len(self.done_batches):
            # return the "rest" batch or the next smallest
            return next(
                (x for x in self.all_batches if x[-2].endswith("rest")),
                self.all_batches[0],
            )

        # set here ensures we don't double count, even though it should not happen
        total_words_processed_so_far = sum([x[-1] for x in set(self.done_batches)])
        proportion_that_matches = so_far / total_words_processed_so_far
        first_not_done: Batch | None = None

        for batch in self.all_batches:
            if batch in self.done_batches:
                continue

            if self.full or self.needed == -1:
                return batch

            if not first_not_done:
                first_not_done = batch

            # todo: should we do this? next-smallest for low number of matches?
            if self.page_size > 0 and so_far < min(self.page_size, 25):
                return batch
            expected = batch[-1] * proportion_that_matches
            if float(expected) >= float(self.needed + (self.needed * buffer)):
                return batch

        if not first_not_done:
            raise ValueError("Could not find batch")
        return first_not_done

    async def no_batch(self) -> web.Response:
        """
        What we do when there is no available batch
        """
        max_kwic = int(os.getenv("DEFAULT_MAX_KWIC_LINES", 9999))
        reached_kwic_limit = self.current_kwic_lines >= max_kwic
        if reached_kwic_limit and not self.full:
            info = "Could not create query: hit kwic limit"
            action = "kwic_limit"
        else:
            info = "Could not create query: no batches"
            action = "no_batch"

        msg: dict[str, str] = {
            "status": "error",
            "action": action,
            "user": self.user,
            "room": self.room or "",
            "info": info,
        }
        err = f"Error: {info} ({self.user}/{self.room})"
        # alert everyone possible about this problem:
        print(f"{err}: {info}")
        # logging.error(err, extra=msg)
        payload = cast(JSONObject, msg)
        room: str = self.room or ""
        just: tuple[str, str] = (room, self.user)
        await push_msg(self.app["websockets"], room, payload, just=just)
        return web.json_response(msg)
