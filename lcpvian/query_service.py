"""
query_service.py: control submission of RQ jobs

We use RQ/Redis for long-running DB queries. The QueryService class has a method
for each type of DB query that we need to run (query, sentence-query, upload,
create schema, store query, fetch query, etc.). Jobs are run by a dedicated
worker process.

After the job is complete, callback functions can be run by worker for success and
failure. Typically, these callbacks will post-process the data and publish it
to Redis PubSub, which we listen on in the main process so we can send
the data to user via websocket.

Before submitting jobs to RQ/Redis, we can often hash the incoming job data to
create an identifier for the job. If identical input data is provided in a
subsequent job, the same hash will be generated, So, we can check in Redis for
this job ID; if it's available, we can trigger the callback manually, and thus
save ourselves from running duplicate DB queries.
"""

import json
import os

from collections.abc import Coroutine
from typing import Any, final, Unpack, cast

from aiohttp import web
from redis import Redis as RedisConnection
from rq import Callback
from rq.command import send_stop_job_command
from rq.exceptions import InvalidJobOperation, NoSuchJobError
from rq.job import Job

from .callbacks import (
    _config,
    _document,
    _document_ids,
    _general_failure,
    _upload_failure,
    _meta,
    _queries,
    _query,
    _schema,
    _sentences,
    _upload,
)
from .convert import _apply_filters
from .jobfuncs import _db_query, _upload_data, _create_schema
from .typed import (
    BaseArgs,
    Config,
    DocIDArgs,
    JSONObject,
    QueryArgs,
    Results,
    ResultsValue,
    SentJob,
)
from .utils import (
    _format_config_query,
    _set_config,
    PUBSUB_CHANNEL,
    CustomEncoder,
    range_to_array
)


@final
class QueryService:
    """
    This magic class will handle our queries by alerting you when they are done
    """

    # __slots__: list[str] = ["app", "timeout", "upload_timeout", "query_ttl"]

    def __init__(self, app: web.Application) -> None:
        self.app = app
        self.timeout = int(os.getenv("QUERY_TIMEOUT", 1000))
        self.whole_corpus_timeout = int(
            os.getenv("QUERY_ENTIRE_CORPUS_CALLBACK_TIMEOUT", 99999)
        )
        self.callback_timeout = int(os.getenv("QUERY_CALLBACK_TIMEOUT", 5000))
        self.upload_timeout = int(os.getenv("UPLOAD_TIMEOUT", 43200))
        self.query_ttl = int(os.getenv("QUERY_TTL", 5000))
        self.use_cache = app["_use_cache"]

    async def send_all_data(self, job: Job, **kwargs: Unpack[QueryArgs]) -> bool:
        """
        Get the stored messages related to a query and send them to frontend
        """
        msg = job.meta["latest_stats_message"]
        print(f"Retrieving stats message: {msg}")
        jso = self.app["redis"].get(msg)
        if jso is None:
            return False
        payload: JSONObject = json.loads(jso)
        payload["user"] = kwargs["user"]
        payload["room"] = kwargs["room"]
        # we may have to apply the latest post-processes...
        pps = cast(
            dict[int, Any], kwargs["post_processes"] or payload["post_processes"]
        )
        # json serialises the Results keys to strings, so we have to convert
        # them back into int for the Results object to be correctly typed:
        full_res = cast(dict[str, ResultsValue], payload["full_result"])
        res = cast(Results, {int(k): v for k, v in full_res.items()})
        if pps and pps != cast(dict[int, Any], payload["post_processes"]):
            filtered = _apply_filters(res, pps)
            payload["result"] = cast(JSONObject, filtered)
        payload["no_restart"] = True
        self.app["redis"].expire(msg, self.query_ttl)
        strung: str = json.dumps(payload, cls=CustomEncoder)

        failed = False
        tasks: list[Coroutine[None, None, None]] = [
            self.app["aredis"].publish(PUBSUB_CHANNEL, strung)
        ]

        sent_and_meta_msgs = {
            **job.meta.get("sent_job_ws_messages", {}),
            **job.meta.get("meta_job_ws_messages", {}),
        }
        for msg in sent_and_meta_msgs:
            # print(f"Retrieving sentences message: {msg}")
            print(f"Retrieving sentences or metadata message: {msg}")
            jso = self.app["redis"].get(msg)
            if jso is None:
                failed = True
                continue
            payload = json.loads(jso)
            payload["user"] = kwargs["user"]
            payload["room"] = kwargs["room"]
            payload["no_update_progress"] = True
            payload["no_restart"] = True
            self.app["redis"].expire(msg, self.query_ttl)
            task = self.app["aredis"].publish(
                PUBSUB_CHANNEL, json.dumps(payload, cls=CustomEncoder)
            )
            tasks.append(task)
        if not failed:
            for task in tasks:
                await task
            return True
        return False

    async def query(
        self,
        query: str,
        queue: str = "query",
        **kwargs: Unpack[QueryArgs],
    ) -> tuple[Job | None, bool | None]:
        """
        Here we send the query to RQ and therefore to redis
        """
        hashed = str(hash(query))
        job: Job | None

        if self.use_cache:
            job, submitted = await self._attempt_query_from_cache(hashed, **kwargs)
            if job is not None:
                return job, submitted

        timeout = self.timeout if not kwargs.get("full") else self.whole_corpus_timeout

        job = self.app[queue].enqueue(
            _db_query,
            on_success=Callback(_query, timeout),
            on_failure=Callback(_general_failure, timeout),
            result_ttl=self.query_ttl,
            job_timeout=timeout,
            job_id=hashed,
            args=(query,),
            kwargs=kwargs,
        )
        return cast(Job, job), True


    async def _attempt_query_from_cache(
        self, hashed: str, **kwargs: Unpack[QueryArgs]
    ) -> tuple[Job | None, bool | None]:
        """
        Try to get a query from cache. Return the job and an indicator of
        whether or not a job was submitted
        """
        job: Job | None
        try:
            job = Job.fetch(hashed, connection=self.app["redis"])
            first_job_id = cast(str, kwargs.get("first_job", str(job.id)))
            first_job = Job.fetch(first_job_id, connection=self.app["redis"])
            is_first = first_job.id == job.id
            self.app["redis"].expire(job.id, self.query_ttl)
            if job.get_status() == "finished":
                if is_first and not kwargs["full"]:
                    success = await self.send_all_data(job, **kwargs)
                    if success:
                        return job, None
                if not kwargs["full"]:
                    _query(
                        job,
                        self.app["redis"],
                        job.result,
                        user=kwargs["user"],
                        room=kwargs["room"],
                        full=kwargs["full"],
                        post_processes=kwargs["post_processes"],
                        current_kwic_lines=kwargs["current_kwic_lines"],
                        from_memory=True,
                    )
                return job, False
        except NoSuchJobError:
            pass
        return None, None

    def document_ids(
        self,
        schema: str,
        corpus_id: int,
        user: str,
        room: str | None,
        queue: str = "internal",
    ) -> Job:
        """
        Fetch document id: name data from DB.

        The fetch from cache should not be needed, as on subsequent jobs
        we can get the data from app["config"]
        """
        query = f"SELECT document_id, name, media, frame_range FROM {schema}.document;"
        kwargs: DocIDArgs = {
            "user": user,
            "room": room,
            "corpus_id": corpus_id,
        }
        hashed = str(hash((query, corpus_id)))
        job: Job
        if self.use_cache:
            try:
                job = Job.fetch(hashed, connection=self.app["redis"])
                if job and job.get_status(refresh=True) == "finished":
                    _document_ids(job, self.app["redis"], job.result, **kwargs)
                    return job
            except NoSuchJobError:
                pass
        job = self.app[queue].enqueue(
            _db_query,
            on_success=Callback(_document_ids, self.timeout),
            on_failure=Callback(_general_failure, self.callback_timeout),
            args=(query, {}),
            kwargs=kwargs,
        )
        return job

    def document(
        self,
        schema: str,
        corpus: int,
        doc_id: int,
        user: str,
        room: str | None,
        queue: str = "internal",
    ) -> Job:
        """
        Fetch info about a document from DB/cache
        """
        query = f"SELECT {schema}.doc_export(:doc_id);"
        # Start work on new logic with Tangram4 (corpus_id 59)
        if corpus == 59:
            # TODO: replace what's hard-coded in this with reading 'tracks' from corpus_template (pass config to this method)
            query = f"""WITH doc AS (SELECT frame_range FROM {schema}.document WHERE document_id = 3),
    seg AS (SELECT 'layer', 'Segment', jsonb_build_object('frame_range', {range_to_array('s.frame_range')}, 'prepared', ps.content, 'agent_id', s.agent_id) AS props FROM {schema}.segment0 s, doc CROSS JOIN {schema}.prepared_segment ps WHERE s.frame_range && doc.frame_range AND s.segment_id = ps.segment_id),
    ges AS (SELECT 'layer', 'Gesture', jsonb_build_object('frame_range', {range_to_array('g.frame_range')}, 'type', g.type, 'bodyPart', g.body_part, 'agent_id', g.agent_id) AS props FROM {schema}.gesture g, doc WHERE g.frame_range && doc.frame_range),
    ag AS (SELECT DISTINCT 'glob', 'agent', jsonb_build_object('agent', a.agent, 'agent_id', a.agent_id) AS props FROM {schema}.agent a, seg, ges WHERE a.agent_id IN ((seg.props->'agent_id')::int, (ges.props->'agent_id')::int))
SELECT * FROM seg
UNION ALL SELECT * FROM ges
UNION ALL SELECT * FROM ag;"""
        params = {"doc_id": doc_id}
        hashed = str(hash((query, doc_id)))
        job: Job
        if self.use_cache:
            try:
                job = Job.fetch(hashed, connection=self.app["redis"])
                if job and job.get_status(refresh=True) == "finished":
                    kwa: BaseArgs = {"user": user, "room": room}
                    _document(job, self.app["redis"], job.result, **kwa)
                    return job
            except NoSuchJobError:
                pass

        kwargs = {
            "document": True,
            "corpus": corpus,
            "user": user,
            "room": room,
            "doc": doc_id,
        }
        job = self.app[queue].enqueue(
            _db_query,
            on_success=Callback(_document, self.timeout),
            on_failure=Callback(_general_failure, self.callback_timeout),
            result_ttl=self.query_ttl,
            job_timeout=self.timeout,
            args=(query, params),
            kwargs=kwargs,
        )
        return job

    def sentences(
        self,
        query: str,
        meta: str = "",
        queue: str = "query",
        **kwargs: Unpack[SentJob],
    ) -> list[str]:
        depends_on = kwargs["depends_on"]
        hash_dep = tuple(depends_on) if isinstance(depends_on, list) else depends_on
        hashed = str(
            hash((query, hash_dep, kwargs["offset"], kwargs["needed"], kwargs["full"]))
        )
        kwargs["sentences_query"] = query
        hashed_meta = str(
            hash((meta, hash_dep, kwargs["offset"], kwargs["needed"], kwargs["full"]))
        )
        job_sent: Job

        if self.use_cache:
            ids: list[str] = self._attempt_sent_from_cache(hashed, **kwargs)
            if meta:
                ids += self._attempt_meta_from_cache(hashed_meta, **kwargs)
            if ids:
                return ids

        timeout = self.timeout if not kwargs.get("full") else self.whole_corpus_timeout

        job_sent = self.app[queue].enqueue(
            _db_query,
            on_success=Callback(_sentences, timeout),
            on_failure=Callback(_general_failure, timeout),
            result_ttl=self.query_ttl,
            depends_on=depends_on,
            job_timeout=timeout,
            job_id=hashed,
            args=(query,),
            kwargs=kwargs,
        )

        return_jobs = [job_sent.id]

        if meta:
            kwargs["meta_query"] = meta
            job_meta: Job = self.app[queue].enqueue(
                _db_query,
                on_success=Callback(_meta, timeout),
                on_failure=Callback(_general_failure, timeout),
                result_ttl=self.query_ttl,
                depends_on=depends_on,
                job_timeout=timeout,
                job_id=hashed_meta,
                args=(meta,),
                kwargs=kwargs,
            )
            return_jobs.append(job_meta.id)

        return return_jobs


    def _attempt_meta_from_cache(
        self, hashed: str, **kwargs: Unpack[SentJob]
    ) -> list[str]:
        """
        Try to return meta from redis cache, instead of doing a new query
        """
        jobs: list[str] = []
        kwa: SentJob
        job: Job
        try:
            job = Job.fetch(hashed, connection=self.app["redis"])
            self.app["redis"].expire(job.id, self.query_ttl)
            if job.get_status() == "finished":
                print("Meta found in redis memory. Retrieving...")
                kwa = {
                    "full": kwargs["full"],
                    "user": kwargs["user"],
                    "room": kwargs["room"],
                    "from_memory": True,
                    "total_results_requested": kwargs["total_results_requested"],
                }
                _meta(job, self.app["redis"], job.result, **kwa)
                return [job.id]
        except NoSuchJobError:
            pass
        return jobs

    def _attempt_sent_from_cache(
        self, hashed: str, **kwargs: Unpack[SentJob]
    ) -> list[str]:
        """
        Try to return sentences from redis cache, instead of doing a new query
        """
        jobs: list[str] = []
        kwa: dict[str, int | bool | str | None] = {}
        job: Job
        try:
            job = Job.fetch(hashed, connection=self.app["redis"])
            self.app["redis"].expire(job.id, self.query_ttl)
            if job.get_status() == "finished":
                print("Sentences found in redis memory. Retrieving...")
                kwa = {
                    "full": kwargs["full"],
                    "user": kwargs["user"],
                    "room": kwargs["room"],
                    "from_memory": True,
                    "total_results_requested": kwargs["total_results_requested"],
                }
                _sentences(job, self.app["redis"], job.result, **kwa)
                return [job.id]
        except NoSuchJobError:
            pass
        return jobs

    async def get_config(self, force_refresh: bool = False) -> Job:
        """
        Get initial app configuration JSON
        """
        job: Job
        job_id = "app_config"

        query = _format_config_query(
            "SELECT {selects} FROM main.corpus mc {join} WHERE mc.enabled = true;"
        )

        redis: RedisConnection[bytes] = self.app["redis"]
        opts: dict[str, bool] = {"config": True}
        if self.use_cache and not force_refresh:
            try:
                already = Job.fetch(job_id, connection=redis)
                if already and already.result is not None:
                    payload: dict[str, str | bool | Config] = _config(
                        already, redis, already.result, publish=False
                    )
                    await _set_config(cast(JSONObject, payload), self.app)
                    print("Loaded config from redis (flush redis if new corpora added)")
                    return already
            except NoSuchJobError:
                pass
        job = self.app["internal"].enqueue(
            _db_query,
            on_success=Callback(_config, self.callback_timeout),
            job_id=job_id,
            on_failure=Callback(_general_failure, self.callback_timeout),
            result_ttl=self.query_ttl,
            job_timeout=self.timeout,
            args=(query,),
            kwargs=opts,
        )
        return job

    def fetch_queries(
        self, user: str, room: str, queue: str = "internal", limit: int = 10
    ) -> Job:
        """
        Get previous saved queries for this user/room
        """
        params: dict[str, str] = {"user": user}
        room_info: str = ""
        if room:
            room_info = " AND room = :room"
            params["room"] = room

        query = f"""SELECT * FROM lcp_user.queries
                    WHERE username = :user {room_info}
                    ORDER BY created_at DESC LIMIT {limit};
                """
        opts = {
            "user": user,
            "room": room,
            "config": True,
        }
        job: Job = self.app[queue].enqueue(
            _db_query,
            on_success=Callback(_queries, self.callback_timeout),
            on_failure=Callback(_general_failure, self.callback_timeout),
            result_ttl=self.query_ttl,
            job_timeout=self.timeout,
            args=(query.strip(), params),
            kwargs=opts,
        )
        return job

    def store_query(
        self,
        query_data: JSONObject,
        idx: int,
        user: str,
        room: str,
        queue: str = "internal",
    ) -> Job:
        """
        Add a saved query to the db
        """
        query = f"INSERT INTO lcp_user.queries VALUES(:idx, :query, :user, :room);"
        kwargs = {
            "user": user,
            "room": room,
            "store": True,
            "config": True,
            "query_id": idx,
        }
        params: dict[str, str | int | None | JSONObject] = {
            "idx": idx,
            "query": query_data,
            "user": user,
            "room": room,
        }
        job: Job = self.app[queue].enqueue(
            _db_query,
            result_ttl=self.query_ttl,
            on_success=Callback(_queries, self.callback_timeout),
            on_failure=Callback(_general_failure, self.callback_timeout),
            job_timeout=self.timeout,
            args=(query, params),
            kwargs=kwargs,
        )
        return job

    def update_metadata(
        self,
        corpus_id: int,
        query_data: JSONObject,
        queue: str = "internal",
    ) -> Job:
        """
        Update metadata for a corpus
        """
        query = """UPDATE main.corpus SET
            corpus_template = jsonb_set(
                corpus_template,
                '{meta}',
                corpus_template->'meta' || :metadata_json
            )
        WHERE corpus_id = :corpus_id;"""
        kwargs = {
            "store": True,
            "config": True,
            "refresh_config": True,
        }
        params: dict[str, str | int | None | JSONObject] = {
            "corpus_id": corpus_id,
            "metadata_json": json.dumps(query_data),
        }
        # TODO: use the uploader user/pool instead (ie the one that can upload corpora)
        # and update app's config once done
        job: Job = self.app[queue].enqueue(
            _db_query,
            result_ttl=self.query_ttl,
            job_timeout=self.timeout,
            args=(query, params),
            kwargs=kwargs,
        )
        return job

    def upload(
        self,
        user: str,
        project: str,
        room: str | None = None,
        queue: str = "background",
        gui: bool = False,
        user_data: JSONObject | None = None,
        is_vian: bool = False,
    ) -> Job:
        """
        Upload a new corpus to the system
        """
        kwargs = {
            "gui": gui,
            "user_data": user_data,
            "is_vian": is_vian,
        }
        job: Job = self.app[queue].enqueue(
            _upload_data,
            on_success=Callback(_upload, self.callback_timeout),
            on_failure=Callback(_upload_failure, self.callback_timeout),
            result_ttl=self.query_ttl,
            job_timeout=self.upload_timeout,
            args=(project, user, room, self.app["_debug"]),
            kwargs=kwargs,
        )
        return job

    def create(
        self,
        create: str,
        project: str,
        path: str,
        schema_name: str,
        user: str | None,
        room: str | None,
        project_name: str,
        queue: str = "background",
        drops: list[str] | None = None,
        gui: bool = False,
    ) -> Job:
        kwargs = {
            "project": project,
            "user": user,
            "room": room,
            "path": path,
            "project_name": project_name,
            "gui": gui,
        }
        job: Job = self.app[queue].enqueue(
            _create_schema,
            # schema job remembered for one day?
            result_ttl=60 * 60 * 24,
            job_timeout=self.timeout,
            on_success=Callback(_schema, self.callback_timeout),
            on_failure=Callback(_upload_failure, self.callback_timeout),
            args=(create, schema_name, drops),
            kwargs=kwargs,
        )
        return job

    def cancel(self, job: Job | str) -> str:
        """
        Cancel a running job
        """
        if isinstance(job, str):
            job_id = job
            job = Job.fetch(job, connection=self.app["redis"])
        else:
            job_id = job.id
        job.cancel()
        send_stop_job_command(self.app["redis"], job_id)
        if job not in self.app["canceled"]:
            self.app["canceled"].append(job)
        return job.get_status()

    def cancel_running_jobs(
        self,
        user: str,
        room: str,
        specific_job: str | None = None,
        base: str | None = None,
    ) -> list[str]:
        """
        Cancel all running jobs for a user/room, or a `specific_job`.

        Return the ids of canceled jobs in a list.
        """
        if specific_job:
            rel_jobs = [str(specific_job)]
        else:
            rel_jobs = self.app["query"].started_job_registry.get_job_ids()
            rel_jobs += self.app["query"].scheduled_job_registry.get_job_ids()

        jobs = set(rel_jobs)
        ids = []

        for job in jobs:
            maybe = Job.fetch(job, connection=self.app["redis"])
            if base and maybe.kwargs.get("simultaneous") != base:
                continue
            if base and maybe.kwargs.get("is_sentences"):
                continue
            if job in self.app["canceled"]:
                continue
            if room and maybe.kwargs.get("room") != room:
                continue
            if user and maybe.kwargs.get("user") != user:
                continue
            try:
                self.cancel(maybe)
                ids.append(job)
            except InvalidJobOperation:
                print(f"Already canceled: {job}")
            except Exception as err:
                print("Unknown error, please debug", err, job)
        return ids

    def get(self, job_id: str) -> Job | None:
        try:
            job = Job.fetch(job_id, connection=self.app["redis"])
            return job
        except NoSuchJobError:
            return None
