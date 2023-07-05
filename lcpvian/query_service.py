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

from __future__ import annotations

import json
import os

from typing import final, Unpack, cast

from aiohttp import web
from redis import Redis as RedisConnection
from rq.command import send_stop_job_command
from rq.exceptions import InvalidJobOperation, NoSuchJobError
from rq.job import Job

from .callbacks import (
    _config,
    _document,
    _document_ids,
    _general_failure,
    _upload_failure,
    _queries,
    _query,
    _schema,
    _sentences,
    _upload,
)
from .jobfuncs import _db_query, _upload_data, _create_schema
from .typed import JSONObject, QueryArgs, Config
from .utils import _set_config, PUBSUB_CHANNEL, CustomEncoder
from .worker import SQLJob


@final
class QueryService:
    """
    This magic class will handle our queries by alerting you when they are done
    """

    # __slots__: list[str] = ["app", "timeout", "upload_timeout", "query_ttl"]

    def __init__(self, app: web.Application) -> None:
        self.app = app
        self.timeout = int(os.getenv("QUERY_TIMEOUT", 1000))
        self.upload_timeout = int(os.getenv("UPLOAD_TIMEOUT", 43200))
        self.query_ttl = int(os.getenv("QUERY_TTL", 5000))
        self.remembered_queries = int(os.getenv("MAX_REMEMBERED_QUERIES", 999))

    async def send_all_data(self, job, **kwargs):
        """
        Get the stored messages related to a query and send them to frontend
        """
        msg = job.meta["latest_stats_message"]
        print(f"Retrieving stats message: {msg}")
        jso = self.app["redis"].get(msg)
        payload = json.loads(jso)
        payload["user"] = kwargs["user"]
        payload["room"] = kwargs["room"]
        payload["no_restart"] = True
        self.app["redis"].expire(msg, self.query_ttl)
        self.app["redis"].publish(
            PUBSUB_CHANNEL, json.dumps(payload, cls=CustomEncoder)
        )

        for msg in job.meta.get("sent_job_ws_messages", {}):
            print(f"Retrieving sentences message: {msg}")
            jso = self.app["redis"].get(msg)
            if jso is None:
                continue
            payload = json.loads(jso)
            payload["user"] = kwargs["user"]
            payload["room"] = kwargs["room"]
            payload["no_restart"] = True
            self.app["redis"].expire(msg, self.query_ttl)
            self.app["redis"].publish(
                PUBSUB_CHANNEL, json.dumps(payload, cls=CustomEncoder)
            )

    async def query(
        self,
        query: str,
        queue: str = "query",
        **kwargs: Unpack[QueryArgs],  # type: ignore
    ) -> tuple[SQLJob | Job, bool | None]:
        """
        Here we send the query to RQ and therefore to redis
        """
        hashed = str(hash(query))
        job: SQLJob | Job | None

        job, submitted = await self._attempt_query_from_cache(hashed, **kwargs)
        if job is not None:
            return job, submitted

        job = self.app[queue].enqueue(
            _db_query,
            on_success=_query,
            on_failure=_general_failure,
            result_ttl=self.query_ttl,
            job_timeout=self.timeout,
            job_id=hashed,
            args=(query,),
            kwargs=kwargs,
        )
        return job, True

    async def _attempt_query_from_cache(
        self, hashed: str, **kwargs
    ) -> tuple[Job | SQLJob | None, bool | None]:
        """
        Try to get a query from cache. Return the job and an indicator of
        whether or not a job was submitted
        """
        job: SQLJob | Job | None
        try:
            # raise NoSuchJobError()  # uncomment to not use cache
            job = Job.fetch(hashed, connection=self.app["redis"])
            is_first = not job.kwargs["first_job"] or job.kwargs["first_job"] == job.id
            self.app["redis"].expire(job.id, self.query_ttl)
            same_pp = job.kwargs["post_processes"] == kwargs["post_processes"]
            if job.get_status() == "finished":
                print("Query found in redis memory. Retrieving...")
                if is_first and not kwargs["full"] and same_pp:
                    await self.send_all_data(job, **kwargs)
                    return job, None
                elif not kwargs["full"]:
                    _query(
                        job,
                        self.app["redis"],
                        job.result,
                        user=kwargs["user"],
                        room=kwargs["room"],
                        full=kwargs["full"],
                        post_processes=kwargs["post_processes"],
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
    ) -> SQLJob | Job:
        """
        Fetch document id: name data from DB.

        The fetch from cache should not be needed, as on subsequent jobs
        we can get the data from app["config"]
        """
        query = f"SELECT document_id, name FROM {schema}.document;"
        kwargs = {"user": user, "room": room, "corpus_id": corpus_id}
        hashed = str(hash((query, corpus_id)))
        job: SQLJob | Job
        try:
            # raise NoSuchJobError()  # uncomment to not use cache
            job = Job.fetch(hashed, connection=self.app["redis"])
            if job and job.get_status(refresh=True) == "finished":
                _document_ids(job, self.app["redis"], job.result, **kwargs)
                return job
        except NoSuchJobError:
            pass
        job = self.app[queue].enqueue(
            _db_query,
            on_success=_document_ids,
            on_failure=_general_failure,
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
    ) -> SQLJob | Job:
        """
        Fetch info about a document from DB/cache
        """
        query = f"SELECT {schema}.doc_export(:doc_id);"
        params = {"doc_id": doc_id}
        hashed = str(hash((query, doc_id)))
        job: SQLJob | Job
        try:
            # raise NoSuchJobError()  # uncomment to not use cache
            job = Job.fetch(hashed, connection=self.app["redis"])
            if job and job.get_status(refresh=True) == "finished":
                kwa: dict[str, str | None] = {"user": user, "room": room}
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
            on_success=_document,
            on_failure=_general_failure,
            result_ttl=self.query_ttl,
            job_timeout=self.timeout,
            args=(query, params),
            kwargs=kwargs,
        )
        return job

    def sentences(
        self,
        query: str,
        queue: str = "query",
        **kwargs: int | bool | str | None | list[str],
    ) -> list[str]:
        depend = cast(str | list[str], kwargs["depends_on"])
        hash_dep = tuple(depend) if isinstance(depend, list) else depend
        hashed = str(
            hash((query, hash_dep, kwargs["offset"], kwargs["needed"], kwargs["full"]))
        )
        kwargs["sentences_query"] = query
        job: SQLJob | Job

        # never do this, right now...
        if kwargs.get("from_memory", False) and not kwargs["full"] and False:
            dones, need_to_do = self._multiple_sent_jobs(**kwargs)
            if need_to_do:
                print(f"Warning: jobs not processed: {need_to_do}")
            return dones

        ids: list[str] = self._attempt_sent_from_cache(hashed, **kwargs)
        if ids:
            return ids

        job = self.app[queue].enqueue(
            _db_query,
            on_success=_sentences,
            on_failure=_general_failure,
            result_ttl=self.query_ttl,
            depends_on=kwargs["depends_on"],
            job_timeout=self.timeout,
            job_id=hashed,
            args=(query,),
            kwargs=kwargs,
        )
        return [job.id]

    def _attempt_sent_from_cache(self, hashed: str, **kwargs) -> list[str]:
        """
        Try to return sentences from redis cache, instead of doing a new query
        """
        jobs: list[str] = []
        kwa: dict[str, int | bool | str | None] = {}
        job: SQLJob | Job
        try:
            # raise NoSuchJobError()  # uncomment to not use cache
            job = Job.fetch(hashed, connection=self.app["redis"])
            self.app["redis"].expire(job.id, self.query_ttl)
            if job.get_status() == "finished":
                print("Sentences found in redis memory. Retrieving...")
                kwa = {
                    "full": cast(bool, kwargs["full"]),
                    "user": cast(str, kwargs["user"]),
                    "room": cast(str | None, kwargs["room"]),
                    "from_memory": True,
                    "total_results_requested": cast(
                        int, kwargs["total_results_requested"]
                    ),
                }
                _sentences(job, self.app["redis"], job.result, **kwa)
                return [job.id]
        except NoSuchJobError:
            pass
        return jobs

    def _multiple_sent_jobs(self, **kwargs) -> tuple[list[str], list[str]]:
        """
        All sentence job ids are stored on the associated base query job. This
        method gets all those ids and iterates over them, fetching their data
        and triggering their callbacks.
        """
        first_job = kwargs["first_job"]
        base = Job.fetch(first_job, connection=self.app["redis"])
        sent_jobs = base.meta.get("_sent_jobs", {})
        need_to_do: list[str] = []
        dones: list[str] = []
        for i, sent_job in enumerate(sent_jobs):
            # total = cast(int, kwargs["total_results_requested"])
            # if i:
            #    total = -1
            job = Job.fetch(sent_job, connection=self.app["redis"])
            self.app["redis"].expire(sent_job, self.query_ttl)
            if job.get_status() == "finished":
                print(f"Sentences found in redis memory. Retrieving: {sent_job}")
                kwa = {
                    "user": cast(str, kwargs["user"]),
                    "room": cast(str | None, kwargs["room"]),
                    "full": cast(bool, kwargs["full"]),
                    "total_results_requested": kwargs["total_results_requested"],
                    "from_memory": True,
                }
                _sentences(job, self.app["redis"], job.result, **kwa)
                dones.append(sent_job)
            else:
                need_to_do.append(sent_job)
        return dones, need_to_do

    async def get_config(self) -> SQLJob | Job:
        """
        Get initial app configuration JSON
        """
        job: Job | SQLJob
        job_id = "app_config"
        query = "SELECT * FROM main.corpus WHERE enabled = true;"
        redis: RedisConnection[bytes] = self.app["redis"]
        opts: dict[str, bool] = {"config": True}
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
            on_success=_config,
            job_id=job_id,
            on_failure=_general_failure,
            result_ttl=self.query_ttl,
            job_timeout=self.timeout,
            args=(query,),
            kwargs=opts,
        )
        return job

    def fetch_queries(
        self, user: str, room: str, queue: str = "internal", limit: int = 10
    ) -> SQLJob | Job:
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
        job: Job | SQLJob = self.app[queue].enqueue(
            _db_query,
            on_success=_queries,
            on_failure=_general_failure,
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
    ) -> SQLJob | Job:
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
        job: Job | SQLJob = self.app[queue].enqueue(
            _db_query,
            result_ttl=self.query_ttl,
            on_success=_queries,
            on_failure=_general_failure,
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
    ) -> SQLJob | Job:
        """
        Upload a new corpus to the system
        """
        kwargs = {
            "gui": gui,
            "user_data": user_data,
            "is_vian": is_vian,
        }
        job: Job | SQLJob = self.app[queue].enqueue(
            _upload_data,
            on_success=_upload,
            on_failure=_upload_failure,
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
    ) -> SQLJob | Job:
        kwargs = {
            "project": project,
            "user": user,
            "room": room,
            "path": path,
            "project_name": project_name,
            "gui": gui,
        }
        job: Job | SQLJob = self.app[queue].enqueue(
            _create_schema,
            # schema job remembered for one day?
            result_ttl=60 * 60 * 24,
            job_timeout=self.timeout,
            on_success=_schema,
            on_failure=_upload_failure,
            args=(create, schema_name, drops),
            kwargs=kwargs,
        )
        return job

    def cancel(self, job: SQLJob | Job | str) -> str:
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

    def get(self, job_id: str) -> Job | SQLJob | None:
        try:
            job = Job.fetch(job_id, connection=self.app["redis"])
            return job
        except NoSuchJobError:
            return None
