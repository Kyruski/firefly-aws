from __future__ import annotations

import firefly as ff


class DataApi(ff.LoggerAware):
    _rds_data_client = None
    _db_arn: str = None
    _db_secret_arn: str = None
    _db_name: str = None

    def __init__(self, db_arn: str = None, db_secret_arn: str = None, db_name: str = None):
        if db_arn is not None and db_secret_arn is not None and db_name is not None:
            self._db_arn = db_arn
            self._db_secret_arn = db_secret_arn
            self._db_name = db_name

    def execute(self, sql: str, params: list = None, db_arn: str = None, db_secret_arn: str = None,
                db_name: str = None):
        params = params or []
        self.info('%s - %s', sql, str(params))
        print('WE HAVE SQL', sql)
        for p in params:
            print('WE HAVE PARAMs', p)
            print('WE HAVE PARAMs', *p)
        return self._rds_data_client.execute_statement(
            resourceArn=(db_arn or self._db_arn),
            secretArn=(db_secret_arn or self._db_secret_arn),
            database=(db_name or self._db_name),
            includeResultMetadata=True,
            sql=sql,
            parameters=params
        )
