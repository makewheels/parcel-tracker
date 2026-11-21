"""运单记录和条件更新。"""
import os

from tablestore import (
    ComparatorType, Condition, Direction, INF_MAX, INF_MIN, OTSClient, OTSServiceError, Row,
    RowExistenceExpectation, SingleColumnCondition,
)


class Store:
    def __init__(self):
        self.table = os.getenv("TABLESTORE_TABLE", "parcels")
        self.client = OTSClient(
            os.environ["TABLESTORE_ENDPOINT"],
            os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
            os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
            os.environ["TABLESTORE_INSTANCE"],
            sts_token=os.environ["ALIBABA_CLOUD_SECURITY_TOKEN"],
        )

    def get(self, number):
        _, row, _ = self.client.get_row(self.table, [("number", number)], max_version=1)
        return {key: value for key, value, _ in row.attribute_columns} if row else None

    def scan(self):
        start = [("number", INF_MIN)]
        end = [("number", INF_MAX)]
        columns = ["number", "name", "company", "subscription", "monitor_status",
                   "notification_paused", "attempted_at", "kind"]
        while start:
            _, start, rows, _ = self.client.get_range(
                self.table, Direction.FORWARD, start, end, columns_to_get=columns, limit=100
            )
            for row in rows:
                number = dict(row.primary_key)["number"]
                yield number, {key: value for key, value, _ in row.attribute_columns}

    def create(self, number, values):
        row = Row([("number", number)], list(values.items()))
        try:
            self.client.put_row(self.table, row, Condition(RowExistenceExpectation.EXPECT_NOT_EXIST))
            return True
        except OTSServiceError as error:
            if error.get_error_code() == "OTSConditionCheckFail":
                return False
            raise

    def update(self, number, values, expected=None):
        comparison = None
        if expected is not None:
            column, value = expected
            comparison = SingleColumnCondition(
                column, value, ComparatorType.EQUAL, pass_if_missing=False
            )
        row = Row([("number", number)], {"PUT": list(values.items())})
        try:
            self.client.update_row(
                self.table, row, Condition(RowExistenceExpectation.EXPECT_EXIST, comparison)
            )
            return True
        except OTSServiceError as error:
            if error.get_error_code() == "OTSConditionCheckFail":
                return False
            raise
