import json
import os

from collections import Counter, defaultdict
from rq.connections import get_current_connection
from rq.job import Job, get_current_job
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from .utils import Interrupted, _get_kwics


async def _upload_data(**kwargs) -> bool:
    """
    Script to be run by rq worker, convert data and upload to postgres
    """
    # from .importer.corpus_data import CorpusData
    # from .importer.corpus_template import CorpusTemplate
    # from .importer.importer import Importer
    from import import Importer

    # these lines could be used if the data needs conversion...
    # from corpert import Corpert
    # corpus_data = Corpert(kwargs["path"]).run()

    # user and room are not really used yet...
    # user: Optional[str] = kwargs["user"]
    # room: Optional[str] = kwargs.get("room")

    # get template and understand it
    corpus_dir = os.path.join("uploads", kwargs["project"])
    template_path = os.path.join(corpus_dir, "template.json")

    print("CORPUS", corpus_dir)
    print("TEMPLATE", template_path)

    """
    conn = get_current_job()._db_conn
    importer = Importer(connection=conn)

    # @Jonathan: can you provide the sql-script (-> schema) and csv-files (-> data)? should work at least for bnc atm
    if not await importer.add_schema(CorpusTemplate(path_to_schema_setup_script="PATH/TO/SCRIPT/REQUIRED")):
        return False
    if not await importer.import_corpus(CorpusData(path_corpus="PATH/TO/CORPUS/REQUIRED")):
        return False

    constraints: str = kwargs["constraints"]
    with open(constraints, "r") as fo:
        constraints = fo.read()
        if not constraints.strip().endswith(";"):
            constraints = constraints + ";"

    async with conn:
        async with conn.cursor() as cur:
            await cur.execute(constraints)

    TODO: delete csv-files and sql-script (may be functions of CorpusTemplate and CorpusData)?
    """
    with open(template_path, "r") as fo:
        template = json.load(fo)

    with open(constraints, "r") as fo:
        constraints = fo.read()

    importer = Importer(conn, template)
    importer.import_corpus(corpus_dir)
    importer.create_constridx(constraints)

    return True

async def _create_schema(**kwargs) -> None:
    """
    To be run by rq worker, create schema
    """
    conn = get_current_job()._db_conn
    async with conn:
        async with conn.cursor() as cur:
            await cur.execute(kwargs["create"])
            # await cur.execute(kwargs["constraints"])
    return None


async def _db_query(query: str, **kwargs) -> Optional[Union[Dict, List]]:
    """
    The function queued by RQ, which executes our DB query
    """
    single_result = kwargs.get("single", False)
    params = kwargs.get("params", tuple())
    is_config = kwargs.get("config", False)
    is_store = kwargs.get("store", False)
    is_sentences = kwargs.get("is_sentences", False)
    current_batch = kwargs.get("current_batch")
    resuming = kwargs.get("resuming", False)
    start_at = 0
    hit_limit = False

    if is_sentences and current_batch:
        associated_query = kwargs["depends_on"]
        conn = get_current_connection()
        if isinstance(associated_query, list):
            associated_query = associated_query[-1]
        associated_query = Job.fetch(associated_query, connection=conn)
        hit_limit = associated_query.meta.get("hit_limit")
        if associated_query.get_status(refresh=True) in ("stopped", "canceled"):
            raise Interrupted()
        if associated_query.result is None:
            raise Interrupted()
        if not associated_query.result:
            return {}
        prev_results = associated_query.result
        # so we don't double count on resuming
        if resuming:
            start_at = associated_query.meta.get("start_at", 0)

        seg_ids = set()

        result_sets = associated_query.meta["result_sets"]
        kwics = _get_kwics(result_sets)
        counts: Dict[int, int] = defaultdict(int)

        for res in prev_results:
            key = int(res[0])
            rest = res[1]
            if key in kwics:
                counts[key] += 1
                if start_at and counts[key] < start_at:
                    continue
                elif hit_limit is not False and counts[key] > hit_limit:
                    continue
                seg_ids.add(str(rest[0]))

        form = ", ".join(sorted(seg_ids))

        query = query.format(
            schema=current_batch[1], table=current_batch[2], allowed=form
        )

    # this open call should be made before any other db calls in the app just in case
    await get_current_job()._pool.open()

    async with get_current_job()._pool.connection() as conn:
        # await conn.set_autocommit(True)
        async with conn.cursor() as cur:
            result = await cur.execute(query, params)
            if is_store:
                return None
            if is_config or is_sentences:
                result = await cur.fetchall()
                return result
            if single_result:
                result = await cur.fetchone()
                result = result[0]
            else:
                result = await cur.fetchall()
            return result
