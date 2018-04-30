from sqlalchemy import UniqueConstraint, ForeignKeyConstraint, CheckConstraint, MetaData, PrimaryKeyConstraint, func, create_engine, inspect
from sqlalchemy.orm import Session
from sqlalchemy.sql.base import ColumnCollection
from sqlalchemy.util._collections import immutabledict
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.schema import AddConstraint
from sqlalchemy.sql import select
from sqlalchemy.types import Numeric, Text, BigInteger, SmallInteger, Integer
from sqlalchemy.dialects.oracle import NUMBER
from sqlalchemy.dialects.mysql import (TINYINT as mysql_TINYINT,
                                       SMALLINT as mysql_SMALLINT,
                                       MEDIUMINT as mysql_MEDIUMINT,
                                       INTEGER as mysql_INTEGER,
                                       BIGINT as mysql_BIGINT
                                       )
import multiprocessing as mp


def fill_table(o_engine_conn, d_engine_conn, table_name, chunk_size):
    """
    Filling tables with data.
    """
    print("{} migrating".format(table_name))
    o_engine = create_engine(o_engine_conn)
    o_metadata = MetaData()
    o_metadata.reflect(o_engine)
    d_engine = create_engine(d_engine_conn)
    d_metadata = MetaData()
    d_metadata.reflect(d_engine)

    table = o_metadata.tables[table_name]
    pks = [c for c in table.primary_key.columns]
    pk = pks[0]
    single_pk = True if len(pks) == 1 else False

    # Check if the table exists in migrated db, if needs to be completed and mark starting id
    try:
        first_it = True
        d_table = d_metadata.tables[table_name]
        dpk = [c for c in d_table.primary_key.columns][0]
        count = o_engine.execute(select([func.count(pk)])).fetchone()[0]
        d_count = d_engine.execute(select([func.count(dpk)])).fetchone()[0]
        # Nothing to do here
        if count == d_count:
            print(table_name, "is already migrated")
            return True
        elif count != d_count and d_count != 0:
            q = select([d_table]).order_by(dpk.desc()).limit(1)
            res = d_engine.execute(q)
            next_id = res.fetchone().__getitem__(dpk.name)
            first_it = False
    except:
        print("need to create {} table before filling it".format(table_name))

    if not single_pk:
        if not first_it:
            offset = d_count
        else:
            offset = 0
        for ini in [x for x in range(offset, count-offset, chunk_size)]:
            # use offset and limit, terrible performance.
            q = select([table]).order_by(*pks).offset(ini).limit(chunk_size)
            res = o_engine.execute(q)
            data = res.fetchall()
            d_engine.execute(
                table.insert(),
                [
                    dict([(col_name, col_value) for col_name, col_value in zip(res.keys(), row)])
                    for row in data
                ]
            )
    else:
        while True:
            q = select([table]).order_by(pk).limit(chunk_size)
            if not first_it:
                q = q.where(pk > next_id)
            else:
                first_it = False
            res = o_engine.execute(q)
            data = res.fetchall()
            if len(data):
                next_id = data[-1].__getitem__(pk.name)
                d_engine.execute(
                    table.insert(),
                    [
                        dict([(col_name, col_value) for col_name, col_value in zip(res.keys(), row)])
                        for row in data
                    ]
                )
            else:
                break
    print(table_name, "migrated")
    return True


class DbMigrator(object):

    o_engine_conn = None
    d_engine_conn = None
    n_cores = None

    def __init__(self, o_conn_string, d_conn_string, n_cores=mp.cpu_count()):
        self.o_engine_conn = o_conn_string
        self.d_engine_conn = d_conn_string
        self.n_cores = n_cores


    def fix_column_type(self, col, db_engine):
        """
        Adapt column types to the most reasonable generic types (ie. VARCHAR -> String)
        Based on sqlacodegen.
        """
        cls = col.type.__class__
        for supercls in cls.__mro__:
            if hasattr(supercls, '__visit_name__'):
                cls = supercls
            if supercls.__name__ != supercls.__name__.upper() and not supercls.__name__.startswith('_'):
                break
        col.type = col.type.adapt(cls)
        if isinstance(col.type, Numeric):
            if col.type.scale == 0:
                if db_engine == 'mysql':
                    if col.type.precision == 1:
                        col.type = mysql_TINYINT()
                    elif col.type.precision == 2:
                        col.type = mysql_SMALLINT()
                    elif col.type.precision == 3:
                        col.type = mysql_MEDIUMINT()
                    elif col.type.precision == 4:
                        col.type = mysql_INTEGER()
                    else:
                        col.type = mysql_BIGINT()
                elif db_engine == 'oracle':
                    if not col.type.precision:
                        pres = 38
                    else:
                        if col.type.precision > 38:
                            pres = 38
                        else:
                            pres = col.type.precision
                    col.type = NUMBER(precision=pres, scale=0)
                elif db_engine in ['postgresql', 'sqlite']:
                    if not col.type.precision:
                        col.type = col.type.adapt(BigInteger)
                    else:
                        if col.type.precision <= 2:
                            col.type = col.type.adapt(SmallInteger)
                        elif 2 < col.type.precision <= 4:
                            col.type = col.type.adapt(Integer)
                        elif col.type.precision > 4:
                            col.type = col.type.adapt(BigInteger)
            else:
                if db_engine == 'mysql':
                    if not col.type.precision and not col.type.scale:
                        # mysql max precision is 64
                        col.type.precision = 64
                        # mysql max scale is 30
                        col.type.scale = 30
        elif isinstance(col.type, Text):
            # Need mediumtext in mysql to store current CLOB we have in Oracle
            if db_engine == 'mysql':
                col.type.length = 100000
        return col


    def migrate_tables(self):
        """
        Copy schema to anther db, as we are migrating from release schema we copy everything.
        """
        o_engine = create_engine(self.o_engine_conn)
        d_engine = create_engine(self.d_engine_conn)
        metadata = MetaData()
        metadata.reflect(o_engine)
        insp = inspect(o_engine)

        new_metadata_tables = {}
        for table_name, table in metadata.tables.items():
            # Keep everything for sqlite. SQLite cant alter table ADD CONSTRAINT. Only 1 simultaneous process can write to it.
            # Keep only PKs for PostreSQL and MySQL. Restoring them after all data is copied.
            keep_constraints = list(filter(lambda cons: isinstance(cons, PrimaryKeyConstraint), table.constraints))
            if d_engine.name == 'sqlite':
                uks = insp.get_unique_constraints(table_name)
                for uk in uks:
                    uk_cols = filter(lambda c: c.name in uk['column_names'], table._columns)
                    keep_constraints.append(UniqueConstraint(*uk_cols, name=uk['name']))
                for fk in filter(lambda cons: isinstance(cons, ForeignKeyConstraint), table.constraints):
                    keep_constraints.append(fk)
                for cc in filter(lambda cons: isinstance(cons, CheckConstraint), table.constraints):
                    cc.sqltext = TextClause(str(cc.sqltext).replace("\"", ""))
                    keep_constraints.append(cc)
                table.constraints = set(keep_constraints)
            else:
                table.constraints = set(keep_constraints)

            table.indexes = set()

            # TODO: Hacky. Fails when reflecting column/table comments so removing it.
            new_metadata_cols = ColumnCollection()
            for col in table._columns:
                col.comment = None
                col = self.fix_column_type(col, d_engine.name)
                # be sure that no column has auto-increment
                col.autoincrement = False
                new_metadata_cols.add(col)
            table.columns = new_metadata_cols.as_immutable()
            table.comment = None
            new_metadata_tables[table_name] = table
        metadata.tables = immutabledict(new_metadata_tables)
        metadata.create_all(d_engine)


    def validate_migration(self):
        o_engine = create_engine(self.o_engine_conn)
        o_metadata = MetaData()
        o_metadata.reflect(o_engine)
        d_engine = create_engine(self.d_engine_conn)
        d_metadata = MetaData()
        d_metadata.reflect(d_engine)

        if set(o_metadata.tables.keys()) != set(d_metadata.tables.keys()):
            return False

        validated = True
        o_s = Session(o_engine)
        d_s = Session(d_engine)
        for table_name, table in o_metadata.tables.items():
            migrated_table = d_metadata.tables[table_name]
            if o_s.query(table).count() != d_s.query(migrated_table).count():
                print('Row count failed for table {}, {}, {}'.format(table_name,
                                                                     o_s.query(table).count(),
                                                                     d_s.query(migrated_table).count()))
                validated = False
        o_s.close()
        d_s.close()
        return validated


    def migrate_constraints(self):

        o_engine = create_engine(self.o_engine_conn)
        d_engine = create_engine(self.d_engine_conn)
        metadata = MetaData()
        metadata.reflect(o_engine)

        insp = inspect(o_engine)

        for table_name, table in metadata.tables.items():
            keep_constraints = []

            # keep unique constraints
            uks = insp.get_unique_constraints(table_name)
            for uk in uks:
                uk_cols = filter(lambda c: c.name in uk['column_names'], table._columns)
                uuk = UniqueConstraint(*uk_cols, name=uk['name'])
                uuk._set_parent(table)
                keep_constraints.append(uuk)

            # keep check constraints
            ccs = filter(lambda cons: isinstance(cons, CheckConstraint), table.constraints)
            for cc in ccs:
                cc.sqltext = TextClause(str(cc.sqltext).replace("\"", ""))
                keep_constraints.append(cc)

            # keep fks
            for fk in filter(lambda cons: isinstance(cons, ForeignKeyConstraint), table.constraints):
                keep_constraints.append(fk)

            # create all constraints
            for cons in keep_constraints:
                try:
                    d_engine.execute(AddConstraint(cons))
                except Exception as e:
                    print(e)


    def migrate_indexes(self):
        o_engine = create_engine(self.o_engine_conn)
        d_engine = create_engine(self.d_engine_conn)
        metadata = MetaData()
        metadata.reflect(o_engine)

        insp = inspect(o_engine)

        for table_name, table in metadata.tables.items():
            uks = insp.get_unique_constraints(table_name)
            indexes_to_keep = filter(lambda index: index.name not in [x['name'] for x in uks], table.indexes)

            for index in indexes_to_keep:
                try:
                    index.create(d_engine)
                except Exception as e:
                    print(e)


    def sort_tables(self):
        """
        Sort tables by FK dependency. 
        SQLite cannot ALTER to add constraints and only supports one write process simultaneously.
        """
        o_engine = create_engine(self.o_engine_conn)
        metadata = MetaData()
        metadata.reflect(o_engine)

        tables = [x[1] for x in metadata.tables.items()]
        ordered_tables = []
        # sort tables by fk dependency, required for sqlite
        while tables:
            current_table = tables[0]
            all_fk_done = True
            for fk in filter(lambda x: isinstance(x, ForeignKeyConstraint), current_table.constraints):
                if fk.referred_table.name not in ordered_tables:
                    all_fk_done = False
            if all_fk_done:
                ordered_tables.append(current_table.name)
                # delete first element of the list(current table) after data is copied
                del tables[0]
            else:
                # put current table in last position of the list
                tables.append(tables.pop(0))
        return ordered_tables


    def migrate(self, copy_schema=True, copy_data=True, copy_constraints=True, copy_indexes=True, chunk_size=1000):

        d_engine = create_engine(self.d_engine_conn)

        # copy schema
        if copy_schema:
            self.migrate_tables()

        # copy data
        if copy_data:
            tables = self.sort_tables()
            # SQLite accepts concurrent read but not write
            processes = 1 if d_engine.name == 'sqlite' else self.n_cores
            with mp.Pool(processes=processes) as pool:
                pool.starmap(fill_table, [(self.o_engine_conn, self.d_engine_conn, x, chunk_size) for x in tables])

        # check row counts for each table
        if not copy_data:
            all_migrated = True
        else:
            all_migrated = self.validate_migration()

        # create constraints
        if all_migrated:
            if copy_constraints and d_engine.name != 'sqlite':
                self.migrate_constraints()
            if copy_indexes:
                self.migrate_indexes()

        return all_migrated