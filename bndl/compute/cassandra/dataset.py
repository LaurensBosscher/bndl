import difflib
import logging

from bndl.compute.cassandra import partitioner, conf
from bndl.compute.cassandra.coscan import CassandraCoScanDataset
from bndl.compute.cassandra.session import cassandra_session
from bndl.compute.dataset.base import Dataset, Partition
from bndl.util import funcs
from cassandra import ConsistencyLevel
from cassandra.query import tuple_factory, named_tuple_factory, dict_factory


logger = logging.getLogger(__name__)


def _did_you_mean(msg, word, possibilities):
    matches = difflib.get_close_matches(word, possibilities, n=2)
    if matches:
        msg += ', did you mean ' + ' or '.join(matches) + '?'
    return msg


def get_table_meta(session, keyspace, table):
    try:
        keyspace_meta = session.cluster.metadata.keyspaces[keyspace]
    except KeyError as e:
        msg = 'Keyspace %s not found' % (keyspace,)
        msg = _did_you_mean(msg, keyspace, session.cluster.metadata.keyspaces.keys())
        raise KeyError(msg) from e
    try:
        return keyspace_meta.tables[table]
    except KeyError as e:
        msg = 'Table %s.%s not found' % (keyspace, table)
        msg = _did_you_mean(msg, table, keyspace_meta.tables.keys())
        raise KeyError(msg) from e


class CassandraScanDataset(Dataset):
    def __init__(self, ctx, keyspace, table, contact_points=None):
        '''
        Create a scan across keyspace.table.

        :param ctx:
            The compute context.
        :param keyspace: str
            Keyspace of the table to scan.
        :param table: str
            Name of the table to scan.
        :param contact_points: None or str or [str,str,str,...]
            None to use the default contact points or a list of contact points
            or a comma separated string of contact points.
        '''
        super().__init__(ctx)
        self.keyspace = keyspace
        self.table = table
        self.contact_points = contact_points
        self._row_factory = named_tuple_factory

        with ctx.cassandra_session(contact_points=self.contact_points) as session:
            table_meta = get_table_meta(session, self.keyspace, self.table)

        self._select = None
        self._limit = None
        self._where = '''
            token({partition_key_column_names}) > ? and
            token({partition_key_column_names}) <= ?
        '''.format(
            partition_key_column_names=', '.join(c.name for c in table_meta.partition_key)
        )


    def count(self, push_down=None):
        if push_down is True or (not self.cached and push_down is None):
            return self.select('count(*)').as_tuples().map(funcs.getter(0)).sum()
        else:
            return super().count()


    def as_tuples(self):
        return self._with('_row_factory', tuple_factory)

    def as_dicts(self):
        return self._with('_row_factory', dict_factory)

    def select(self, *columns):
        return self._with('_select', columns)


    def limit(self, num):
        wlimit = self._with('_limit', int(num))
        return wlimit

    def itake(self, num):
        if not self.cached and not self._limit:
            return self.limit(num).itake(num)
        else:
            return super().itake(num)


    def coscan(self, other, keys=None):
        assert isinstance(other, CassandraScanDataset)
        return CassandraCoScanDataset(self, other, keys=keys)



    def parts(self):
        with cassandra_session(self.ctx, contact_points=self.contact_points) as session:
            partitions = partitioner.partition_ranges(session, self.keyspace, self.table,
                                                      min_pcount=self.ctx.default_pcount,
                                                      part_size_keys=self.ctx.conf.get_int(conf.PART_SIZE_KEYS, defaults=conf.DEFAULTS),
                                                      part_size_mb=self.ctx.conf.get_int(conf.PART_SIZE_MB, defaults=conf.DEFAULTS))

        return [
            CassandraScanPartition(self, i, replicas, token_ranges)
            for i, (replicas, token_ranges) in enumerate(partitions)
        ]


    def query(self, session):
        select = ', '.join(self._select) if self._select else '*'
        limit = ' limit %s' % self._limit if self._limit else ''
        query = '''
            select {select}
            from {keyspace}.{table}
            where {where}{limit}
        '''.format(
            select=select,
            keyspace=self.keyspace,
            table=self.table,
            where=self._where,
            limit=limit
        )
        return session.prepare(query)



class CassandraScanPartition(Partition):
    def __init__(self, dset, part_idx, replicas, token_ranges):
        super().__init__(dset, part_idx)
        self.replicas = replicas
        self.token_ranges = token_ranges


    def _materialize(self, ctx):
        with ctx.cassandra_session(contact_points=self.dset.contact_points) as session:

            session.row_factory = self.dset._row_factory
            query = self.dset.query(session)
            logger.debug('scanning %s token ranges with query %s',
                         len(self.token_ranges), query.query_string.replace('\n', ''))

            query.consistency_level = ctx.conf.get_attr(conf.READ_CONSISTENCY_LEVEL, obj=ConsistencyLevel, defaults=conf.DEFAULTS)
            timeout = ctx.conf.get_int(conf.READ_TIMEOUT, defaults=conf.DEFAULTS)
            next_rs = session.execute_async(query, self.token_ranges[0], timeout=timeout)
            resultset = None

            for next_tr in self.token_ranges[1:] + [None]:
                resultset = next_rs.result()

                while True:
                    has_more = resultset.response_future.has_more_pages
                    if has_more:
                        resultset.response_future.start_fetching_next_page()
                    elif next_tr:
                        next_rs = session.execute_async(query, next_tr)
                    yield from resultset.current_rows
                    if has_more:
                        resultset = resultset.response_future.result()
                    else:
                        break


    def _preferred_workers(self, workers):
        return [
            worker
            for worker in workers
            if worker.ip_addresses & self.replicas
        ]
